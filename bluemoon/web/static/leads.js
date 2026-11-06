/* Leads and batches: "Add a property" (an address or a Crexi link) and "Prepare offers for selected" on Deals, and the
 * drawer's "How it got on the list" lines. Deals (deals.js) owns the checkboxes and calls BM.leads.addPanel(...) and
 * BM.leads.batchPanel(...) with an empty element to draw into.
 *
 * POST /api/m/{slug}/leads: a sure duplicate is never added twice; an unsure match is shown and waits for
 * "Same property" or "New property". A Crexi link is only saved as the source: nothing is fetched from it.
 * POST /offer-batches/preview, then POST /offer-batches: every row is a normal internal draft (4x the yearly rent
 * unless a price is typed), one row failing never stops the rest, and Retry only touches failed rows.
 */
(function () {
  'use strict';
  if (!window.BM) return;
  const {esc} = BM;

  const EVENT_TEXT = {
    new_listing: 'Added by hand as a new property', duplicate: 'Added by hand again: it was already on the list, so nothing was added',
    same_property: 'Added by hand and matched to this property by you',
  };

  function usd(cents) {
    return cents === null || cents === undefined ? '—' : BM.pricing.usd(cents);
  }

  function field(label, name, extra) {
    return '<label class="field">' + esc(label) + '<input name="' + name + '" maxlength="' + ((extra && extra.max) || 300) + '" autocomplete="off"' +
      (extra && extra.mode ? ' inputmode="' + extra.mode + '"' : '') + '></label>';
  }

  /* ---------- the drawer ---------- */

  function html(d) {
    const ev = Array.isArray(d && d.lead_events) ? d.lead_events : [];
    if (!ev.length) return '';
    return '<section class="sec" data-part="lead" aria-labelledby="ld-h"><h2 id="ld-h">How it got on the list</h2><ul class="of-events">' +
      ev.map((e) => '<li>' + esc(EVENT_TEXT[e.kind] || e.kind) + ' · ' + esc(BM.fmt.date(e.at)) + ' · ' + esc(e.actor) +
        (e.detail && e.detail.link ? ' · link saved as the source, not fetched' : '') + '</li>').join('') + '</ul></section>';
  }

  /* ---------- Add a property ---------- */

  function matchHtml(m) {
    const where = [BM.fmt.title(m.address || 'No address'), BM.fmt.title(m.city || ''), m.state, m.zip].filter(Boolean).join(', ');
    return '<li class="of-card"><b>' + esc(where) + '</b><span class="muted">' + esc(m.why) + '</span>' +
      '<span class="of-inline"><button type="button" class="btn small" data-same-as="' + esc(m.property_id) + '">It is the same property</button>' +
      '<a href="#" data-open-property="' + esc(m.property_id) + '">Look at it</a></span></li>';
  }

  /** A fresh element inside `wrap` for one panel, so the listeners of an earlier panel go away with it. */
  function host(wrap) {
    const el = document.createElement('div');
    wrap.replaceChildren(el);
    return el;
  }

  function addPanel(wrap, onAdded) {
    const box = host(wrap);
    box.innerHTML = '<section class="panel of-stack" aria-labelledby="ld-add"><h2 id="ld-add">Add a property</h2>' +
      '<p class="note">Type a street address, paste a Crexi link, or both. A link is saved as the source only: nothing is fetched from Crexi, so the details stay blank until an import or the other steps fill them.</p>' +
      '<form class="of-stack" data-ld-form>' + field('Street address', 'address') + field('City', 'city', {max: 100}) +
      field('State (two letters)', 'state', {max: 2}) + field('ZIP', 'zip', {max: 10, mode: 'numeric'}) + field('Crexi link', 'link', {max: 600}) +
      '<span class="of-inline"><button type="submit" class="btn primary">Add property</button><button type="button" class="btn" data-ld-close>Cancel</button></span></form>' +
      '<div data-ld-result aria-live="polite"></div></section>';
    const form = box.querySelector('[data-ld-form]');
    const out = box.querySelector('[data-ld-result]');
    let busy = false;

    function body(extra) {
      const v = (n) => String(form.elements[n].value || '').trim() || null;
      return Object.assign({address: v('address'), city: v('city'), state: v('state'), zip: v('zip'), link: v('link')}, extra || {});
    }

    async function send(extra) {
      if (busy) return;
      busy = true;
      try {
        const res = await BM.api(BM.m('/leads'), {method: 'POST', body: body(extra)});
        if (res.outcome === 'needs_resolution') {
          out.innerHTML = '<p>' + esc(res.message) + '</p><ul class="of-list">' + res.matches.map(matchHtml).join('') + '</ul>' +
            '<button type="button" class="btn" data-as-new>None of these: add it as a new property</button>';
          return;
        }
        BM.toast(res.message);
        if (res.outcome === 'duplicate') {
          out.innerHTML = '<p>' + esc(res.message) + '</p><a href="#" class="btn" data-open-property="' + esc(res.property_id) + '">Open that property</a>';
          return;
        }
        wrap.replaceChildren();
        if (onAdded) onAdded();
        BM.openProperty(res.property_id);
      } catch (err) {
        BM.toast(err.message, {error: true});
      } finally {
        busy = false;
      }
    }

    form.addEventListener('submit', (ev) => { ev.preventDefault(); send(); });
    box.addEventListener('click', (ev) => {
      const t = ev.target instanceof Element ? ev.target : null;
      if (!t) return;
      const same = t.closest('[data-same-as]');
      if (same) send({same_as: Number(same.getAttribute('data-same-as'))});
      else if (t.closest('[data-as-new]')) send({as_new: true});
      else if (t.closest('[data-ld-close]')) wrap.replaceChildren();
    });
    form.elements.address.focus();
  }

  /* ---------- Prepare offers for selected ---------- */

  function rowLine(r) {
    const where = BM.fmt.title(r.address || 'Unknown address') + (r.city ? ', ' + BM.fmt.title(r.city) : '');
    const flags = r.flags.map((f) => '<li class="' + (f.blocks ? 'bad' : 'muted') + '">' + esc(f.text) + '</li>').join('');
    return '<li class="of-card" data-bp="' + esc(r.property_id) + '"><label class="of-use"><input type="checkbox" data-bp-use' + (r.can_prepare ? ' checked' : ' disabled') + '>' +
      '<span><b>' + esc(where) + '</b></span></label>' +
      '<span>' + esc(r.rent_basis || '') + ' · yearly rent ' + esc(usd(r.annual_cents)) + ' · Blue Moon price (4x rent) <b class="num">' + esc(usd(r.default_cents)) + '</b></span>' +
      '<span class="muted">Seller: ' + esc(r.owner || 'none on record') + ' · Recipient: ' + esc(r.recipient || 'none chosen') + '</span>' +
      (r.can_prepare ? '<label class="field">Price in dollars (leave blank to follow the 4x default)<input data-bp-price inputmode="decimal" maxlength="30" autocomplete="off"></label>' : '') +
      (flags ? '<ul class="of-events">' + flags + '</ul>' : '') + '</li>';
  }

  function reportHtml(b) {
    return '<h2>Offers prepared</h2><p><b>' + esc(b.text) + '</b> Nothing was sent: these are drafts' + (b.letters_ready ? '.' : ' and internal records (no letter template is active yet).') + '</p>' +
      '<ul class="of-list">' + b.rows.map((r) => '<li class="of-card"><b>' + esc(BM.fmt.title(r.address || 'Unknown address')) + '</b><span class="pill ' +
        (r.status === 'failed' ? 'bad' : r.status === 'created' ? 'good' : 'warn') + '">' + esc(r.status_label) + '</span><span class="muted">' + esc(r.message || '') + '</span></li>').join('') + '</ul>' +
      '<span class="of-inline">' + (b.counts.failed ? '<button type="button" class="btn primary" data-bp-retry="' + esc(b.id) + '">Retry the ' + esc(b.counts.failed) + ' that failed</button>' : '') +
      '<a class="btn" href="' + esc(BM.m('/offer-batches/' + b.id + '/report.csv')) + '" download>Download the report</a>' +
      '<a class="btn" href="#" data-go="offers">Open the Offers screen</a><button type="button" class="btn" data-bp-close>Close</button></span>';
  }

  async function batchPanel(wrap, ids, onDone) {
    const box = host(wrap);
    box.innerHTML = '<section class="panel of-stack" aria-labelledby="bp-h"><h2 id="bp-h">Prepare offers for selected</h2><div data-bp-body>' + BM.ui.loading('Checking the properties…') + '</div></section>';
    const bodyEl = box.querySelector('[data-bp-body]');
    let busy = false;
    try {
      const pre = await BM.api(BM.m('/offer-batches/preview'), {method: 'POST', body: {property_ids: ids}});
      bodyEl.innerHTML = '<p class="note">Each row is a draft at 4x the yearly rent (the Blue Moon price, not the Blue Moon score) unless you type a price. Untick a row to leave it out. Nothing is sent.' +
        (pre.note ? ' ' + esc(pre.note) : '') + '</p><ul class="of-list">' + pre.rows.map(rowLine).join('') +
        '</ul><span class="of-inline"><button type="button" class="btn primary" data-bp-go>Prepare offers</button><button type="button" class="btn" data-bp-close>Cancel</button></span>';
    } catch (err) {
      bodyEl.innerHTML = BM.ui.error(err);
      return;
    }

    async function run(path, body) {
      if (busy) return;
      busy = true;
      try {
        const res = await BM.api(BM.m(path), {method: 'POST', body});
        bodyEl.innerHTML = reportHtml(res);
        if (onDone) onDone(res);
      } catch (err) {
        BM.toast(err.message, {error: true});
      } finally {
        busy = false;
      }
    }

    box.addEventListener('click', (ev) => {
      const t = ev.target instanceof Element ? ev.target : null;
      if (!t) return;
      if (t.closest('[data-bp-close]')) { wrap.replaceChildren(); return; }
      const retry = t.closest('[data-bp-retry]');
      if (retry) { run('/offer-batches/' + Number(retry.getAttribute('data-bp-retry')) + '/retry', {}); return; }
      if (!t.closest('[data-bp-go]')) return;
      const items = Array.from(bodyEl.querySelectorAll('[data-bp]')).map((li) => ({
        property_id: Number(li.getAttribute('data-bp')),
        exclude: !li.querySelector('[data-bp-use]').checked,
        price: ((li.querySelector('[data-bp-price]') || {}).value || '').trim() || null,
      }));
      if (items.every((i) => i.exclude)) { BM.toast('Tick at least one property.', {error: true}); return; }
      run('/offer-batches', {rows: items});
    });
  }

  BM.leads = {html, addPanel, batchPanel};
})();
