/* Offers: the drawer's "Offers" section (BM.offers.html(detail) draws it, BM.offers.mount(root, detail) wires it;
 * property.js calls both) and the Offers screen in the Work menu (every offer of the market).
 *
 * An offer is one version of a price for a property, based on a frozen copy of the rent figures. It starts at
 * 4x the yearly rent and follows that default until a price is typed. A draft is not a sent offer: only
 * "Mark as sent" records a send (how, when, who). The LOI is made from the owner's own Google Doc (the app never
 * writes letter wording), and only a letter made from the offer's current numbers counts. A counteroffer is written
 * down next to the offer and never changes it. All money arrives as whole cents. Every change goes to the offer
 * routes in ARCHITECTURE.md and the answer redraws the section.
 */
(function () {
  'use strict';
  if (!window.BM) return;
  const {esc} = BM;

  const HOW = [['email', 'By email'], ['in person', 'In person'], ['mail', 'By mail']];
  const STATUS_CLASS = {sent: 'good', internal_draft: ''};

  let st = null;   // {id, d, root, busy, previews: {offer id: field review}}

  /* ---------- small helpers ---------- */

  function has(v) {
    return v !== null && v !== undefined && v !== '';
  }

  function usd(cents) {
    return BM.pricing.usd(cents);
  }

  function today() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  function opts(list, selected) {
    return list.map(([v, l]) => '<option value="' + esc(v) + '"' + (v === selected ? ' selected' : '') + '>' + esc(l) + '</option>').join('');
  }

  function statusPill(o) {
    return '<span class="pill ' + (STATUS_CLASS[o.status] || 'warn') + '">' + esc(o.status_label) + '</span>';
  }

  function priceLine(o) {
    const how = o.price_mode === 'default' ? 'Following the 4x default' : 'Manual override';
    const mult = o.implied_multiple ? ' · ' + esc(o.implied_multiple) + 'x the yearly rent' : '';
    return '<span class="muted">' + how + mult + '</span>';
  }

  function counterText(c) {
    if (!c) return '';
    const range = usd(c.low_cents) + (has(c.high_cents) ? ' to ' + usd(c.high_cents) : '');
    const m = c.low_multiple ? ' (' + c.low_multiple + 'x' + (c.high_multiple ? ' to ' + c.high_multiple + 'x' : '') + ')' : '';
    return range + m;
  }

  function dueText(a) {
    if (!a) return '';
    const late = a.due_on && a.due_on < today();
    return esc(a.text) + (a.due_on ? ' · due ' + esc(BM.fmt.date(a.due_on)) : '') + (late ? ' <span class="pill bad">Overdue</span>' : '');
  }

  function sentText(s) {
    return 'Sent ' + esc(s.how) + (s.on ? ' on ' + esc(BM.fmt.date(s.on)) : '') + (s.by ? ' by ' + esc(s.by) : '') + ' · ' + esc(s.via_label);
  }

  /* ---------- the drawer section ---------- */

  function prepareHtml(b) {
    if (!b.can_prepare) return '<p class="note">Version ' + esc(b.next_version - 1) + ' is still a draft. Change it below, or mark it sent before preparing a revised offer.</p>';
    const cur = b.current;
    const start = has(cur.default_cents)
      ? 'Blue Moon price (4x rent): <b class="num">' + esc(usd(cur.default_cents)) + '</b> from a yearly rent of ' + esc(usd(cur.annual_cents)) + '.'
      : 'There is no Blue Moon price yet because the yearly rent is unknown or $0. Enter a rent figure above, or type the price you want.';
    return '<form class="of-prepare" data-of-form="prepare"><p class="note">' + start + '</p>' +
      '<label class="field">Price in dollars (leave blank to use the 4x price)<input name="price" inputmode="decimal" autocomplete="off" maxlength="30"' +
      (has(cur.default_cents) ? '' : ' required') + '></label>' +
      '<button type="submit" class="btn small primary">' + (b.next_version > 1 ? 'Prepare revised offer (version ' + esc(b.next_version) + ')' : 'Prepare offer') + '</button></form>';
  }

  function scenariosHtml(o) {
    const sc = (o.rent && o.rent.scenarios) || [];
    if (!sc.length) return '';
    return '<div class="note">Rent basis at the time: ' + esc(usd(o.rent.annual_cents)) + ' a year. ' +
      sc.map((s) => esc(s.multiple + 'x ' + usd(s.cents))).join(' · ') + ' (5x and 6x are comparisons only).</div>';
  }

  function priceFormHtml(o) {
    return '<form class="of-inline" data-of-form="price" data-offer="' + esc(o.id) + '">' +
      '<label class="field">Offer price in dollars<input name="price" inputmode="decimal" autocomplete="off" maxlength="30" required value="' +
      esc((o.price_cents / 100).toFixed(2)) + '"></label><button type="submit" class="btn small">Save price</button>' +
      (o.price_mode === 'override' ? '<button type="button" class="btn small" data-of-act="default-price" data-offer="' + esc(o.id) + '">Use the 4x price</button>' : '') + '</form>';
  }

  function sentFormHtml(o) {
    return '<form class="of-inline" data-of-form="sent" data-offer="' + esc(o.id) + '">' +
      '<label class="field">How was it sent?<select name="how">' + opts(HOW, 'email') + '</select></label>' +
      '<label class="field">Day sent<input type="date" name="when" max="' + today() + '" value="' + today() + '"></label>' +
      '<button type="submit" class="btn small primary">Mark as sent</button></form>' +
      '<p class="note">This only records that you sent it. Nothing is sent from here.</p>';
  }

  function sendsHtml(o) {
    const list = o.sends.length ? '<ul class="of-list">' + o.sends.map((x) => '<li><span class="pill ' + (x.state === 'failed' ? 'bad' : 'warn') + '">' + esc(x.state_label) + '</span> ' +
      esc(x.recipient_contact || '') + ' <span class="hint">' + esc(BM.fmt.date(x.updated_at)) + ' · ' + esc(x.created_by) + (x.error ? ' · ' + esc(x.error) : '') + '</span></li>').join('') + '</ul>' : '';
    const retry = o.sends.some((x) => x.state === 'failed');
    const allowed = o.status === 'ready_to_send' && !o.sends.some((x) => x.state !== 'failed');
    return '<h4>Send</h4><p class="note">There is no live email yet. The test outbox only saves what would be sent, in <code>data/outbox</code>, and the offer stays unsent until you mark it sent.</p>' + list +
      (allowed ? '<div class="of-inline"><button type="button" class="btn small" data-of-act="send" data-offer="' + esc(o.id) + '">' + (retry ? 'Try the test outbox again' : 'Send a test copy to the outbox') + '</button></div>' : '');
  }

  function responseHtml(r, b, o) {
    const range = has(r.counter_low_cents) ? '<div><b>Asked for ' + esc(usd(r.counter_low_cents) + (has(r.counter_high_cents) ? ' to ' + usd(r.counter_high_cents) : '')) + '</b></div>' : '';
    const fix = '<details class="p-more"><summary>Fix the category</summary><form class="of-inline" data-of-form="fix-response" data-offer="' + esc(o.id) + '" data-response="' + esc(r.id) + '">' +
      '<label class="field">Category<select name="classification">' + opts(b.class_choices, r.classification) + '</select></label>' +
      '<label class="field">Asked for (low)<input name="counter_low" inputmode="decimal" maxlength="30" value="' + (has(r.counter_low_cents) ? esc((r.counter_low_cents / 100).toFixed(2)) : '') + '"></label>' +
      '<label class="field">Asked for (high, optional)<input name="counter_high" inputmode="decimal" maxlength="30" value="' + (has(r.counter_high_cents) ? esc((r.counter_high_cents / 100).toFixed(2)) : '') + '"></label>' +
      '<button type="submit" class="btn small">Save</button></form></details>';
    return '<li class="of-resp"><div><span class="pill ' + (r.classification === 'needs_review' ? 'warn' : '') + '">' + esc(r.classification_label) + '</span> ' +
      '<span class="hint">' + esc(BM.fmt.date(r.received_on)) + ' · entered by ' + esc(r.recorded_by) + (r.corrected_by ? ' · category fixed by ' + esc(r.corrected_by) : '') + '</span></div>' +
      range + '<blockquote class="of-quote">' + esc(r.original_text) + '</blockquote>' + fix + '</li>';
  }

  function responseFormHtml(b, o) {
    return '<details class="p-more"><summary>Write down what they said</summary><form class="of-stack" data-of-form="response" data-offer="' + esc(o.id) + '">' +
      '<label class="field">Their words, as they said or wrote them<textarea name="text" required maxlength="5000" rows="3"></textarea></label>' +
      '<label class="field">Category<select name="classification"><option value="">Not sure yet (needs review)</option>' +
      opts(b.class_choices.filter((c) => c[0] !== 'needs_review'), '') + '</select></label>' +
      '<label class="field">Asked for (low)<input name="counter_low" inputmode="decimal" maxlength="30"></label>' +
      '<label class="field">Asked for (high, optional)<input name="counter_high" inputmode="decimal" maxlength="30"></label>' +
      '<label class="field">Day they replied<input type="date" name="received_on" max="' + today() + '" value="' + today() + '"></label>' +
      '<button type="submit" class="btn small">Save response</button>' +
      '<p class="note">A counteroffer is only written down. It never changes your offer.</p></form></details>';
  }

  function actionHtml(a, o) {
    return '<li class="of-act' + (a.done ? ' done' : '') + '"><label class="of-check"><input type="checkbox" data-of-act="done" data-offer="' + esc(o.id) + '" data-action="' + esc(a.id) + '"' + (a.done ? ' checked' : '') + '> ' +
      '<span>' + esc(a.text) + '</span></label>' +
      '<label class="field of-due">Due<input type="date" data-of-act="due" data-offer="' + esc(o.id) + '" data-action="' + esc(a.id) + '" value="' + esc(a.due_on || '') + '" aria-label="Due date for ' + esc(a.text) + '"></label>' +
      '<span class="hint">' + esc(a.owner) + '</span></li>';
  }

  function safeUrl(url) {
    return /^https:\/\/(docs|drive)\.google\.com\//.test(url || '') ? url : null;
  }

  function documentHtml(d) {
    const link = (url, label) => safeUrl(url) ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>' : '';
    return '<li><span class="pill ' + (d.current ? 'good' : 'warn') + '">' + (d.current ? 'Matches this offer' : 'Made from older numbers') + '</span> ' +
      link(d.doc_url, 'Google Doc') + ' ' + link(d.pdf_url, 'PDF') +
      ' <span class="hint">' + esc(BM.fmt.date(d.created_at)) + ' · ' + esc(d.created_by) + (d.template_version ? ' · template version ' + esc(d.template_version) : '') + '</span></li>';
  }

  function reviewHtml(p) {
    const rows = p.rows.map((r) => '<tr><td><code>' + esc(r.token) + '</code></td><td>' + (r.label ? esc(r.label) : '<span class="muted">Not mapped</span>') + '</td><td>' +
      (r.value ? esc(r.value) : '<span class="muted">Nothing yet</span>') + '</td><td><span class="pill ' + (r.state === 'filled' ? 'good' : r.state === 'unused' ? '' : 'warn') + '">' + esc(r.state_text) + '</span></td></tr>').join('');
    return '<div class="of-review"><p class="note"><b>' + esc(p.note) + '</b>' + (p.checked ? '' : ' The template has not been checked in Settings yet, so every mapped field is listed.') + '</p>' +
      '<div class="vw-scroll"><table class="wt"><thead><tr><th>Field</th><th>Filled with</th><th>Text</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div></div>';
  }

  function letterHtml(o) {
    const docs = o.documents || [];
    const list = docs.length ? '<ul class="of-list">' + docs.map(documentHtml).join('') + '</ul>' : '';
    if (o.status === 'sent') return docs.length ? '<h4>Letters made</h4>' + list : '';
    const made = docs.some((d) => d.current);
    const next = {loi_generated: ['ready_for_review', 'Mark ready for review'], ready_for_review: ['ready_to_send', 'Mark ready to send']}[o.status];
    const id = esc(o.id);
    const preview = st.previews[o.id];
    return '<h4>Letter (LOI)</h4><p class="note">Made from your own Google Doc. The app never writes the wording.</p>' + list +
      '<div class="of-inline"><button type="button" class="btn small" data-of-act="preview" data-offer="' + id + '">Check the fields</button>' +
      '<button type="button" class="btn small' + (made ? '' : ' primary') + '" data-of-act="make-loi" data-again="' + (made ? '1' : '') + '" data-offer="' + id + '">' + (made ? 'Make a new copy' : 'Make the LOI') + '</button>' +
      (next && made ? '<button type="button" class="btn small primary" data-of-act="stage" data-stage="' + next[0] + '" data-offer="' + id + '">' + next[1] + '</button>' : '') + '</div>' +
      (preview ? reviewHtml(preview) : '');
  }

  function eventsHtml(o) {
    return '<details class="p-more"><summary>History (' + o.events.length + ')</summary><ul class="of-events">' +
      o.events.map((e) => '<li><b>' + esc(e.kind.replace(/_/g, ' ')) + '</b> <span class="hint">' + esc(BM.fmt.date(e.at)) + ' · ' + esc(e.actor) + '</span></li>').join('') + '</ul></details>';
  }

  function offerHtml(o, b) {
    const stale = o.stale
      ? '<div class="p-banner" role="note"><b>The rent figures changed after this draft was made.</b> Its price and numbers are unchanged until you refresh it.' +
        ' <button type="button" class="btn small" data-of-act="refresh" data-offer="' + esc(o.id) + '">Refresh the draft</button></div>' : '';
    const recipient = o.recipient
      ? '<b>' + esc(o.recipient.name) + '</b>' + (o.recipient.contact ? ' (' + esc(o.recipient.contact) + ')' : '')
      : '<span class="muted">Not chosen yet</span>';
    const ready = '<span class="pill ' + (o.readiness.template.ready ? 'good' : 'warn') + '">' + esc(o.readiness.template.text) + '</span> ' +
      '<span class="pill ' + (o.readiness.recipient.ready ? 'good' : 'warn') + '">' + esc(o.readiness.recipient.text) + '</span>';
    const counter = o.counter ? '<dt>Their counter</dt><dd>' + esc(counterText(o.counter)) + ' <span class="hint">(your offer did not change)</span></dd>' : '';
    const next = o.next_action ? '<dt>Next action</dt><dd>' + dueText(o.next_action) + '</dd>' : '';
    return '<article class="of-card" data-offer-card="' + esc(o.id) + '"><h3>Version ' + esc(o.version) + ' ' + statusPill(o) +
      (o.incomplete_snapshot ? ' <span class="pill warn" title="Entered by hand after the fact. The rent it was based on is not known.">Entered after the fact</span>' : '') + '</h3>' +
      stale +
      '<div class="of-price"><b class="big num">' + esc(usd(o.price_cents)) + '</b>' + priceLine(o) + '</div>' + scenariosHtml(o) +
      '<dl class="kv"><dt>Seller (owner of record)</dt><dd>' + (o.seller_name ? esc(o.seller_name) : '<span class="muted">Not on record</span>') + '</dd>' +
      '<dt>Addressed to</dt><dd>' + recipient + '</dd>' + (o.sent ? '<dt>Sent</dt><dd>' + sentText(o.sent) + '</dd>' : '<dt>Letter and recipient</dt><dd>' + ready + '</dd>') +
      counter + next + '</dl>' +
      (o.status === 'sent' ? '' : priceFormHtml(o)) + letterHtml(o) + (o.status === 'sent' ? '' : sendsHtml(o) + sentFormHtml(o)) +
      (o.responses.length ? '<h4>Responses</h4><ul class="of-list">' + o.responses.map((r) => responseHtml(r, b, o)).join('') + '</ul>' : '') +
      (o.status === 'sent' ? responseFormHtml(b, o) : '') +
      '<h4>Next actions</h4>' + (o.actions.length ? '<ul class="of-list">' + o.actions.map((a) => actionHtml(a, o)).join('') + '</ul>' : '<p class="note">None yet.</p>') +
      '<form class="of-inline" data-of-form="action" data-offer="' + esc(o.id) + '"><label class="field">New next action<input name="text" required maxlength="500" autocomplete="off"></label>' +
      '<label class="field">Due<input type="date" name="due_on"></label><button type="submit" class="btn small">Add</button></form>' +
      (o.events ? eventsHtml(o) : '') + '</article>';
  }

  function html(d) {
    const b = d && d.offers;
    if (!b) return '';
    if (!st || st.id !== d.id) st = {id: d.id, root: null, busy: false, previews: {}};
    st.d = d;
    return '<section class="sec of" data-part="offers" aria-labelledby="of-h"><h2 id="of-h">Offers</h2>' +
      '<p class="note">Blue Moon price (4x rent) is the starting price here. It is not the Blue Moon score, which is the chance the owner sells. A draft is not a sent offer.</p>' +
      prepareHtml(b) + (b.offers.length ? b.offers.map((o) => offerHtml(o, b)).join('') : '<p class="note">No offers yet.</p>') +
      '<details class="p-more"><summary>Record an offer you made before</summary><form class="of-stack" data-of-form="historical">' +
      '<label class="field">Price in dollars<input name="price" inputmode="decimal" required maxlength="30"></label>' +
      '<label class="field">How was it sent?<select name="how">' + opts(HOW, 'email') + '</select></label>' +
      '<label class="field">Day sent<input type="date" name="sent_on" required max="' + today() + '"></label>' +
      '<label class="field">Sent to (name, optional)<input name="recipient_name" maxlength="300" autocomplete="off"></label>' +
      '<label class="field">Note (optional)<input name="note" maxlength="500" autocomplete="off"></label>' +
      '<button type="submit" class="btn small">Save earlier offer</button></form></details></section>';
  }

  function redraw() {
    const el = st.root.querySelector('[data-part="offers"]');
    if (el) el.outerHTML = html(st.d);
  }

  /* ---------- writes ---------- */

  async function write(path, body, method) {
    const mine = st;
    if (!mine || mine.busy) return null;
    mine.busy = true;
    try {
      const res = await BM.api(path, {method: method || 'POST', body});
      if (st !== mine) return res;
      mine.d.offers = res.offers;
      mine.previews = {};
      redraw();
      if (res.message) BM.toast(res.message);
      return res;
    } catch (err) {
      if (st === mine) BM.toast(err.message, {error: true});
      return null;
    } finally {
      mine.busy = false;
    }
  }

  async function loadPreview(el) {
    const mine = st;
    const id = Number(el.getAttribute('data-offer'));
    try {
      const res = await BM.api(offerPath(el, '/loi'));
      if (st !== mine) return;
      mine.previews[id] = res;
      redraw();
    } catch (err) {
      if (st === mine) BM.toast(err.message, {error: true});
    }
  }

  function prop() {
    return BM.m('/properties/' + st.id);
  }

  function offerPath(el, tail) {
    return BM.m('/offers/' + Number(el.getAttribute('data-offer')) + tail);
  }

  function text(form, name) {
    const el = form.elements[name];
    const v = el ? String(el.value || '').trim() : '';
    return v === '' ? null : v;
  }

  /* ---------- events (bound once on the drawer) ---------- */

  function onClick(ev) {
    const b = ev.target instanceof Element ? ev.target.closest('[data-of-act]') : null;
    if (!b || !st || !st.d || b.tagName === 'INPUT') return;
    const act = b.getAttribute('data-of-act');
    if (act === 'refresh') write(offerPath(b, '/refresh'), undefined);
    else if (act === 'default-price') write(offerPath(b, '/price'), {price: null}, 'PUT');
    else if (act === 'send') write(offerPath(b, '/send'), undefined);
    else if (act === 'preview') loadPreview(b);
    else if (act === 'make-loi') write(offerPath(b, '/loi'), {again: !!b.getAttribute('data-again')});
    else if (act === 'stage') write(offerPath(b, '/status'), {status: b.getAttribute('data-stage')}, 'PUT');
  }

  function onChange(ev) {
    const t = ev.target;
    if (!(t instanceof Element) || !st || !st.d) return;
    const act = t.getAttribute('data-of-act');
    const path = act ? offerPath(t, '/actions/' + Number(t.getAttribute('data-action'))) : null;
    if (act === 'done') write(path, {done: !!t.checked}, 'PUT');
    else if (act === 'due') write(path, {due_on: t.value || null}, 'PUT');
  }

  function onSubmit(ev) {
    const form = ev.target instanceof Element ? ev.target.closest('form[data-of-form]') : null;
    if (!form || !st || !st.d) return;
    ev.preventDefault();
    if (st.busy || (form.reportValidity && !form.reportValidity())) return;
    const kind = form.getAttribute('data-of-form');
    const t = (n) => text(form, n);
    if (kind === 'prepare') write(prop() + '/offers', {price: t('price')});
    else if (kind === 'historical') write(prop() + '/offers/historical', {price: t('price'), how: t('how'), sent_on: t('sent_on'), recipient_name: t('recipient_name'), note: t('note')});
    else if (kind === 'price') write(offerPath(form, '/price'), {price: t('price')}, 'PUT');
    else if (kind === 'sent') write(offerPath(form, '/sent'), {how: t('how'), when: t('when')});
    else if (kind === 'response') write(offerPath(form, '/responses'), {text: String(form.elements.text.value || ''), classification: t('classification'), counter_low: t('counter_low'), counter_high: t('counter_high'), received_on: t('received_on')});
    else if (kind === 'fix-response') write(offerPath(form, '/responses/' + Number(form.getAttribute('data-response'))), {classification: t('classification'), counter_low: t('counter_low'), counter_high: t('counter_high')}, 'PUT');
    else if (kind === 'action') write(offerPath(form, '/actions'), {text: t('text'), due_on: t('due_on')});
  }

  function mount(root, d) {
    if (!st || st.id !== d.id) return;
    st.root = root;
    if (root.__bmOffersBound) return;
    root.__bmOffersBound = true;
    root.addEventListener('click', onClick);
    root.addEventListener('change', onChange);
    root.addEventListener('submit', onSubmit);
  }

  BM.offers = {html, mount};

  /* ---------- the Offers screen ---------- */

  const S = {filter: ''};

  function rowHtml(o) {
    const where = BM.fmt.title(o.address || 'Unknown address') + (o.city ? ', ' + BM.fmt.title(o.city) : '');
    const sent = o.sent ? '<div class="hint">' + sentText(o.sent) + '</div>' : '';
    return '<li class="of-item"><div class="of-who"><a href="#" class="addr" data-open-property="' + esc(o.property_id) + '">' + esc(where) + '</a>' +
      '<div>Version ' + esc(o.version) + ' ' + statusPill(o) + (o.stale ? ' <span class="pill warn">Rent changed</span>' : '') +
      (o.incomplete_snapshot ? ' <span class="pill warn">Entered after the fact</span>' : '') + '</div>' + sent + '</div>' +
      '<div class="of-nums"><b class="num">' + esc(usd(o.price_cents)) + '</b><div>' + priceLine(o) + '</div>' +
      (o.counter ? '<div>Their counter: <b class="num">' + esc(counterText(o.counter)) + '</b></div>' : '') + '</div>' +
      '<div class="of-next">' + (o.next_action ? '<span class="hint">Next action</span><div>' + dueText(o.next_action) + '</div>' : '<span class="muted">No next action</span>') + '</div></li>';
  }

  function screenHtml(data) {
    const head = '<div class="vw-head"><div class="eyebrow">Work</div><h1>Offers</h1><ul class="lines"><li>Every offer in this market, with where it stands.</li>' +
      '<li>A draft is not a sent offer. Only an offer marked as sent counts as sent.</li></ul></div>';
    if (!data.total) {
      return head + BM.ui.empty('No offers yet', 'Open a property and choose Prepare offer in its Offers section.', '');
    }
    const shown = data.items.filter((o) => !S.filter || o.status === S.filter);
    const choices = '<option value="">All offers (' + data.total + ')</option>' + data.status_labels.filter(([k]) => data.counts[k])
      .map(([k, l]) => '<option value="' + esc(k) + '"' + (k === S.filter ? ' selected' : '') + '>' + esc(l) + ' (' + data.counts[k] + ')</option>').join('');
    return head + '<div class="of-bar"><label class="field">Show<select data-of-filter>' + choices + '</select></label></div>' +
      '<ul class="of-items">' + shown.map(rowHtml).join('') + '</ul>';
  }

  async function render(root) {
    root.innerHTML = BM.ui.loading('Loading offers…');
    let data;
    try {
      data = await BM.api(BM.m('/offers'));
    } catch (err) {
      if (root.isConnected) root.innerHTML = BM.ui.error(err);
      return;
    }
    if (!root.isConnected) return;
    root.innerHTML = screenHtml(data);
    root.addEventListener('change', (ev) => {
      if (!(ev.target instanceof Element) || !ev.target.matches('[data-of-filter]')) return;
      S.filter = ev.target.value;
      root.innerHTML = screenHtml(data);
    });
  }

  BM.registerView('offers', {label: 'Offers', group: 'work', order: 5, render});
})();
