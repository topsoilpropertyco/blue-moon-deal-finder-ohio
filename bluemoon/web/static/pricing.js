/* The drawer's "Rent and Blue Moon price" and "Age and history" sections: BM.pricing.html(detail) draws them,
 * BM.pricing.mount(root, detail) wires them (property.js calls both).
 *
 * The yearly rent, the Blue Moon price (4x rent, with 5x and 6x as comparisons only), the last-sale check, the
 * napkin that shows how the rent was worked out (unit mix, HUD rents, what is known and what is assumed, with
 * every line editable), the rent records and the age and history facts. All money arrives as whole cents and is
 * shown to the cent here (never rounded to "$1.6M"). Every change goes to the routes in ARCHITECTURE.md and the
 * answer redraws the two sections. Clicking any rent figure opens the napkin.
 */
(function () {
  'use strict';
  if (!window.BM) return;
  const {esc} = BM;

  const SOURCES = [['rent_roll', 'Rent roll'], ['listing', 'Listing'], ['owner', 'Owner conversation'], ['broker', 'Broker'], ['manual', 'Entered by hand']];
  const BASES = [['actual', 'Actual rent collected'], ['scheduled', 'Scheduled rent (what leases say)'], ['estimated_full_occupancy', 'Estimated at full occupancy'],
    ['estimated_market', 'Estimated at market rents'], ['unconfirmed', 'Unconfirmed (not checked)']];
  // How the figure is given -> the fields the route wants.
  const KINDS = [
    ['total_monthly', 'A total for the whole property, per month', {total_period: 'monthly'}],
    ['total_annual', 'A total for the whole property, per year', {total_period: 'annual'}],
    ['t12', 'The last 12 months added up (T12)', {total_period: 't12'}],
    ['single_month', 'One month of actual rent (a projection, not a T12)', {total_period: 'single_month'}],
    ['partial', 'Some other stretch of months (needs review)', {total_period: 'partial'}],
    ['unit_monthly', 'Rent per unit, per month', {rate_period: 'monthly', quantity_kind: 'units'}],
    ['unit_annual', 'Rent per unit, per year', {rate_period: 'annual', quantity_kind: 'units'}],
    ['sqft_monthly', 'Rent per rentable sq ft, per month', {rate_period: 'monthly', quantity_kind: 'rentable_sqft'}],
    ['sqft_annual', 'Rent per rentable sq ft, per year', {rate_period: 'annual', quantity_kind: 'rentable_sqft'}],
  ];
  const BUILDER = [['unknown', 'Unknown'], ['suggested', 'Suggested, not confirmed'], ['supported', 'Supported by a source'], ['contradicted', 'Contradicted by a source']];
  const AREA = [['', 'Not known'], ['rentable', 'Net rentable area'], ['gross', 'Gross building area']];
  const CONF_CLASS = {Estimated: 'warn', 'Partly known': 'warn', Reported: 'good'};
  const PRIOR_DOT = {below: 'g', at: 'y', above: 'n', unavailable: 'n'};

  let st = null;   // {id, d, root, open, busy}

  /* ---------- small helpers ---------- */

  function has(v) {
    return v !== null && v !== undefined && v !== '';
  }

  function usd(cents) {
    if (typeof cents !== 'number' || !Number.isFinite(cents)) return '—';
    const a = Math.abs(cents);
    const rem = a % 100;
    return (cents < 0 ? '-' : '') + '$' + Math.floor(a / 100).toLocaleString('en-US') + (rem ? '.' + String(rem).padStart(2, '0') : '');
  }

  function dayText(iso) {
    return has(iso) ? BM.fmt.date(iso) : null;
  }

  function opts(list, selected) {
    return list.map(([v, l]) => '<option value="' + esc(v) + '"' + (v === selected ? ' selected' : '') + '>' + esc(l) + '</option>').join('');
  }

  function field(label, inner, cls) {
    return '<label class="field' + (cls ? ' ' + cls : '') + '">' + esc(label) + inner + '</label>';
  }

  function figure(cents, what) {
    if (typeof cents !== 'number') return '<span class="muted">—</span>';
    return '<button type="button" class="pr-fig num" data-pr-act="napkin" aria-expanded="' + !!(st && st.open) +
      '" title="Show how this was worked out">' + esc(usd(cents)) + '<span class="sr"> (' + esc(what) + '; open the back of the napkin)</span></button>';
  }

  /* ---------- rent and price ---------- */

  function sourceLine(p) {
    const r = p.rent;
    if (r.source === 'record') {
      const rec = (p.records || []).find((x) => x.selected) || {};
      return [rec.source_label, dayText(rec.evidence_date), r.basis_label].filter(Boolean).join(' · ') + (rec.pinned ? ' · chosen by you' : '');
    }
    return 'Estimated: units x HUD area rent by bedroom count' + (r.basis_label ? ' (' + r.basis_label.toLowerCase() + ')' : '');
  }

  function incomeHtml(p) {
    const r = p.rent;
    if (r.annual_cents === null) {
      return '<p class="note pr-unknown">Rent unknown. ' + esc(p.estimate.reason || 'Nothing gives a rent yet.') +
        ' <button type="button" class="linkbtn" data-pr-act="napkin" aria-expanded="' + !!st.open + '">Open the napkin</button></p>' +
        (r.needs_review ? reviewNote(r) : '');
    }
    return '<div class="pr-income"><div><div class="eyebrow">Yearly gross rent</div>' + figure(r.annual_cents, 'yearly gross rent') + '</div>' +
      '<div><div class="eyebrow">Monthly</div>' + figure(r.monthly_cents, 'monthly rent') + '</div>' +
      '<span class="pill ' + (CONF_CLASS[r.confidence] || '') + '" title="Estimated, then Partly known, then Reported as better evidence arrives">' + esc(r.confidence) + '</span></div>' +
      '<p class="note pr-src">' + esc(sourceLine(p)) + '. Rent only: no parking, laundry or fees.</p>' +
      (r.zero ? '<p class="note">Confirmed zero rent: kept as zero, not treated as unknown.</p>' : '') +
      (r.needs_review ? reviewNote(r) : '');
  }

  function reviewNote(r) {
    return '<p class="note">' + esc(r.needs_review + (r.needs_review === 1 ? ' rent figure needs' : ' rent figures need') + ' review and ' + (r.needs_review === 1 ? 'is' : 'are') + ' not used.') + '</p>';
  }

  function priceHtml(p) {
    const ref = p.scenarios.find((s) => s.role === 'reference');
    if (!ref) return '<p class="note">No Blue Moon price yet: it needs a yearly rent.</p>';
    const compare = p.scenarios.filter((s) => s.role !== 'reference');
    const sq = (m) => (p.per_sqft || []).find((x) => x.multiple === m);
    const sqText = (m) => (sq(m) && sq(m).cents !== null ? usd(sq(m).cents) + ' per rentable sq ft' : '');
    const rows = compare.map((s) => '<tr><td>' + esc(s.multiple) + 'x rent</td><td class="c num">' + esc(usd(s.cents)) + '</td><td class="c">' + esc(sqText(s.multiple)) + '</td></tr>').join('');
    return '<div class="pr-price"><div class="eyebrow">Blue Moon price (' + esc(ref.multiple) + 'x rent)</div>' +
      '<div class="big num">' + esc(usd(ref.cents)) + '</div>' +
      (sqText(ref.multiple) ? '<div class="note">' + esc(sqText(ref.multiple)) + '</div>' : '') +
      '<p class="note">Yearly rent ' + esc(usd(p.rent.annual_cents)) + ' x ' + esc(ref.multiple) + '. A starting reference, not a limit. ' +
      'It is not the Blue Moon score, which is the chance the owner sells.</p>' +
      (rows ? '<div class="tablewrap"><table class="p-rent"><caption class="sr">Comparison prices (scenarios only)</caption><thead><tr><th>Comparison only</th><th class="c">Price</th><th class="c">Per sq ft</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
        '<p class="note">The comparison prices are scenarios to look at. They are never a ceiling and never the next offer.</p>' : '') +
      '<p class="note">Proposed offer: ' + esc(usd(ref.cents)) + ', exactly ' + esc(ref.multiple) + 'x (following the default).</p></div>';
  }

  function priorHtml(p) {
    const q = p.prior;
    if (q.state === 'unavailable') {
      return '<div class="pr-prior"><div class="verdict"><span class="dot n"></span><b>Last sale check unavailable</b></div><p class="note">' + esc(q.reason) + '</p></div>';
    }
    const amount = usd(q.gap_cents);
    const where = q.state === 'below' ? amount + ' below half' : q.state === 'above' ? amount + ' above half' : 'exactly half';
    const when = [dayText(q.sale_date)].filter(Boolean).join('');
    return '<div class="pr-prior"><div class="verdict"><span class="dot ' + PRIOR_DOT[q.state] + '"></span><b>' + esc(q.label) + '</b></div>' +
      '<p class="note">Last sale ' + esc(usd(q.sale_cents)) + (when ? ' (' + esc(when) + ')' : '') + ' is ' + esc(Number(q.share_pct).toFixed(1)) + '% of ' +
      esc(usd(q.reference_cents)) + ', the 4x price. Half is ' + esc(usd(q.halfway_cents)) + ', so it sold ' + esc(where) + '.</p></div>';
  }

  function askingHtml(p) {
    return has(p.asking_price) ? '<p class="note">Asking price ' + esc(usd(Math.round(Number(p.asking_price) * 100))) +
      ' (what the seller or listing asks). It is not the last sale price and not the Blue Moon price.</p>' : '';
  }

  /* ---------- the napkin ---------- */

  function bedroomKeys(p) {
    const keys = new Set([].concat(Object.keys(p.defaults.mix || {}), Object.keys(p.hud.rents || {}), Object.keys(p.assumptions.mix || {}), Object.keys(p.assumptions.rents || {})));
    return Array.from(keys).sort((a, b) => Number(a) - Number(b));
  }

  function knownList(p, d) {
    const e = p.estimate;
    const hud = [p.hud.area_name, p.hud.fiscal_year].filter(Boolean).join(', ');
    const item = (state, what, text) => '<li><span class="pill ' + (state === 'Known' || state === 'Typed' ? 'good' : 'warn') + '">' + esc(state) + '</span> <b>' + esc(what) + '</b> ' + esc(text) + '</li>';
    return '<ul class="pr-known">' +
      item(e.units_source === 'typed' ? 'Typed' : e.units ? 'Known' : 'Unknown', 'Units', e.units ? e.units + (e.units_source === 'typed' ? ' (you typed it)' : ' (from the export)') : 'not in the export') +
      item(e.mix_source === 'typed' ? 'Typed' : 'Assumed', 'Bedroom mix', e.mix_source === 'typed' ? 'you typed the real mix' : 'the usual split for this kind of property') +
      item((e.lines || []).some((l) => l.rent_source === 'typed') ? 'Typed' : 'Assumed', 'Rent by bedroom', 'HUD area rents' + (hud ? ' for ' + hud : '') + ', unless you typed one') +
      item(p.rent.source === 'record' ? 'Known' : 'Assumed', 'Rent used for the price', p.rent.source === 'record' ? 'a saved rent figure (below)' : 'this estimate') + '</ul>';
  }

  function napkinTable(p) {
    const lines = {};
    (p.estimate.lines || []).forEach((l) => { lines[l.bedrooms] = l; });
    const typed = p.estimate.mix_source === 'typed';
    const rows = bedroomKeys(p).map((k) => {
      const l = lines[k] || {};
      const label = k === '0' ? 'Studio' : k + ' BR';
      const hud = p.hud.rents[k];
      const mix = (p.assumptions.mix || {})[k];
      const rent = (p.assumptions.rents || {})[k];
      return '<tr><th scope="row">' + esc(label) + '</th>' +
        '<td class="c num shr">' + (has(l.share_pct) ? esc(l.share_pct + '%') : '—') + '</td>' +
        '<td><input name="mix_' + esc(k) + '" type="number" inputmode="numeric" min="0" step="1" class="pr-in" aria-label="Units, ' + esc(label) + '" value="' + (typed && has(mix) ? esc(mix) : '') + '" placeholder="' + esc(l.units === undefined ? '' : l.units) + '"></td>' +
        '<td><input name="rent_' + esc(k) + '" type="text" inputmode="decimal" class="pr-in" aria-label="Monthly rent, ' + esc(label) + '" value="' + (has(rent) ? esc(rent / 100) : '') + '" placeholder="' + (has(hud) ? esc(hud / 100) : '') + '"></td>' +
        '<td class="c num">' + esc(has(l.monthly_cents) ? usd(l.monthly_cents) : '—') + '</td></tr>';
    }).join('');
    return '<div class="tablewrap"><table class="p-rent pr-table"><thead><tr><th>Type</th><th class="c shr">Share</th><th>Units</th><th>Rent a month</th><th class="c">Subtotal</th></tr></thead><tbody>' + rows + '</tbody>' +
      '<tfoot><tr><th scope="row" colspan="4">Total a month (x 12 = yearly)</th><td class="c num">' + esc(usd(p.estimate.monthly_cents)) + '</td></tr></tfoot></table></div>';
  }

  function recordsHtml(p) {
    if (!p.records.length) return '<p class="note">No rent figures saved yet. Add a rent roll, a listing or what the owner said below.</p>';
    return '<ul class="pr-records">' + p.records.map((r) => {
      const pin = r.state === 'needs_review' ? '' : '<button type="button" class="btn small" data-pr-act="pin" data-rid="' + esc(r.id) + '" data-pinned="' + (r.pinned ? '0' : '1') + '">' +
        (r.pinned ? 'Back to the automatic choice' : 'Use this one') + '</button>';
      return '<li class="' + (r.selected ? 'sel' : '') + '"><div><b class="num">' + esc(r.annual_cents === null ? 'Needs review' : usd(r.annual_cents) + ' a year') + '</b> ' +
        (r.selected ? '<span class="pill good">Used for the price</span> ' : '') + (r.state === 'projection' ? '<span class="pill warn">Projection</span> ' : '') +
        (r.state === 'needs_review' ? '<span class="pill bad">Needs review</span>' : '') + '</div>' +
        '<div class="note">' + esc([r.source_label, dayText(r.evidence_date), r.basis_label].filter(Boolean).join(' · ')) + '</div>' +
        (r.formula ? '<div class="note">' + esc(r.formula) + '</div>' : '') + (r.reason ? '<div class="note">' + esc(r.reason) + '</div>' : '') +
        (r.note ? '<div class="note">' + esc(r.note) + '</div>' : '') + '<div class="note">Saved by ' + esc(r.created_by) + '</div>' + pin + '</li>';
    }).join('') + '</ul>';
  }

  function recordForm(p) {
    return '<details class="p-more"><summary>Add rent evidence</summary>' +
      '<form class="p-form pr-form" data-pr-form="record" data-rate="0" data-sqft="0">' +
      '<div class="fields">' + field('Where it came from', '<select name="source">' + opts(SOURCES, 'rent_roll') + '</select>') +
      field('What it means', '<select name="basis">' + opts(BASES, 'actual') + '</select>') +
      field('The day the figure is from', '<input name="evidence_date" type="date">') +
      field('How it is given', '<select name="kind">' + KINDS.map(([v, l]) => '<option value="' + v + '">' + esc(l) + '</option>').join('') + '</select>', 'full') +
      field('Amount in dollars', '<input name="amount" type="text" inputmode="decimal" required>') +
      field('How many (units or sq ft)', '<input name="quantity" type="text" inputmode="decimal">', 'rate-only') +
      field('Which area is it?', '<select name="area_type">' + opts(AREA, '') + '</select>', 'sqft-only') +
      field('Also a total (optional)', '<input name="check_total" type="text" inputmode="decimal">', 'rate-only') +
      field('That total is', '<select name="check_period"><option value="monthly">per month</option><option value="annual">per year</option><option value="t12">the last 12 months</option></select>', 'rate-only') +
      field('If both are given, trust', '<select name="trust"><option value="">Choose…</option><option value="total">the total</option><option value="rate">the rate x count</option></select>', 'rate-only') +
      field('Note (optional)', '<textarea name="note" rows="2" maxlength="2000"></textarea>', 'full') + '</div>' +
      '<p class="note">A rate per sq ft counts only with the net rentable area. A figure that cannot be turned into a year honestly is saved as "needs review" and is not used.</p>' +
      '<div class="row"><button type="submit" class="btn primary">Save this figure</button></div></form></details>';
  }

  function napkinHtml(p, d) {
    const e = p.estimate;
    return '<div class="pr-napkin" role="region" aria-labelledby="pr-napkin-h" data-part="napkin"><div class="pr-nhead"><h3 id="pr-napkin-h">Back of the napkin</h3>' +
      '<button type="button" class="btn small quiet" data-pr-act="napkin">Close</button></div>' +
      '<p class="note">The rent is worked out in layers, and each one is only as good as what we know. Better evidence replaces the estimate.</p>' +
      knownList(p, d) +
      '<form class="p-form pr-form" data-pr-form="napkin"><h4>Estimate: units x rent by bedroom type</h4>' +
      field('Number of units', '<input name="units" type="number" inputmode="numeric" min="1" step="1" class="pr-in" value="' + (e.units_source === 'typed' ? esc(e.units) : '') + '" placeholder="' + esc((d.property || {}).units || '') + '">') +
      napkinTable(p) +
      '<p class="note">Type the real count for every bedroom type, or leave the unit boxes empty to use the usual split. Leave a rent box empty to use the HUD rent (shown grey).</p>' +
      (e.reason ? '<p class="note pr-warn">' + esc(e.reason) + '</p>' : '') + (e.warnings || []).map((w) => '<p class="note pr-warn">' + esc(w) + '</p>').join('') +
      '<div class="row"><button type="submit" class="btn primary">Re-run the price</button>' +
      '<button type="button" class="btn quiet" data-pr-act="reset">Back to the defaults</button></div></form>' +
      '<p class="note">HUD rents: ' + esc([p.hud.area_name, p.hud.fiscal_year].filter(Boolean).join(', ') || 'none for this area') + (p.hud.source ? ' (' + esc(p.hud.source.replace(/_/g, ' ')) + ')' : '') + '. They are area rents for the whole county or ZIP, not this building.</p>' +
      '<h4>Saved rent figures</h4>' + recordsHtml(p) + recordForm(p) + '</div>';
  }

  /* ---------- age and history ---------- */

  function historyHtml(d) {
    const h = d.history;
    const p = d.property || {};
    const a = h.age;
    const o = h.ownership;
    const flag = (x) => (x.flag ? ' <span class="pill warn">' + esc(x.flag === 'future' ? 'Future date' : 'Impossible year') + '</span>' : '');
    const sale = has(p.sale_price) ? usd(Math.round(Number(p.sale_price) * 100)) + (has(p.sale_date) ? ' on ' + (dayText(p.sale_date)) : '') +
      (h.sale.source ? ' (source: ' + h.sale.source + ')' : '') : null;
    const rows = [
      ['Age', esc(a.text) + flag(a)],
      ['Years owned', esc(o.text) + flag(o)],
      ['Last sale', sale ? esc(sale) + (h.sale.bundled ? ' <span class="pill warn">Bundled or portfolio sale</span>' : '') : null],
      ['Built by the owner?', esc(h.builder.label) + (h.builder.source ? ' · ' + esc(h.builder.source) : '') + (h.builder.note ? '<br>' + esc(h.builder.note) : '')],
      ['Building size is', h.area_type ? esc(h.area_type === 'rentable' ? 'net rentable area' : 'gross building area') : 'not known (rentable or gross)'],
    ].filter((r) => r[1] !== null);
    return '<section class="sec" data-part="history"><h2>Age and history</h2><dl class="kv">' +
      rows.map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' + v + '</dd>').join('') + '</dl>' +
      '<p class="note">Matching years do not prove an owner built the building. This note is only what was recorded, with its source, and says nothing about condition or management.</p>' +
      '<details class="p-more"><summary>Update what is known</summary><form class="p-form" data-pr-form="history"><div class="fields">' +
      field('Builder or original owner', '<select name="builder_state">' + opts(BUILDER, h.builder.state) + '</select>') +
      field('Source (needed for supported or contradicted)', '<input name="builder_source" maxlength="500" value="' + esc(h.builder.source || '') + '">') +
      field('Note', '<textarea name="builder_note" rows="2" maxlength="500">' + esc(h.builder.note || '') + '</textarea>', 'full') +
      field('Where the last sale price comes from', '<input name="sale_source" maxlength="500" value="' + esc(h.sale.source || '') + '">') +
      field('The building size is', '<select name="building_area_type">' + opts(AREA, h.area_type || '') + '</select>') +
      '<label class="field full"><span><input type="checkbox" name="sale_bundled"' + (h.sale.bundled ? ' checked' : '') + '> The last sale was a bundled or portfolio sale</span></label></div>' +
      '<div class="row"><button type="submit" class="btn primary">Save</button></div></form></details></section>';
  }

  /* ---------- the sections ---------- */

  function sectionHtml(d) {
    const p = d.pricing;
    return '<section class="sec pr" data-part="pricing"><h2>Rent and Blue Moon price</h2>' + incomeHtml(p) + priceHtml(p) + priorHtml(p) + askingHtml(p) +
      (st.open ? napkinHtml(p, d) : '') + '</section>';
  }

  function html(d) {
    if (!d || !d.pricing || !d.history) return '';
    if (!st || st.id !== d.id) st = {id: d.id, root: null, open: false, busy: false};
    st.d = d;
    return sectionHtml(d) + historyHtml(d);
  }

  function redraw(focus) {
    const root = st.root;
    const pr = root.querySelector('[data-part="pricing"]');
    const hist = root.querySelector('[data-part="history"]');
    if (pr) pr.outerHTML = sectionHtml(st.d);
    if (hist) hist.outerHTML = historyHtml(st.d);
    if (focus) {
      const el = root.querySelector(focus);
      if (el) el.focus();
    }
  }

  /* ---------- writes ---------- */

  async function write(path, body, method) {
    const mine = st;
    if (!mine || mine.busy) return;
    mine.busy = true;
    try {
      const res = await BM.api(path, {method: method || 'POST', body});
      if (st !== mine) return;
      mine.d.pricing = res.pricing;
      mine.d.history = res.history;
      Object.assign(mine.d.property, res.property || {});
      redraw(null);
      if (res.message) BM.toast(res.message);
    } catch (err) {
      if (st === mine) BM.toast(err.message, {error: true});
    } finally {
      mine.busy = false;
    }
  }

  function base() {
    return BM.m('/properties/' + st.id);
  }

  function text(form, name) {
    const el = form.elements[name];
    const v = el ? String(el.value || '').trim() : '';
    return v === '' ? null : v;
  }

  function napkinBody(form) {
    const mix = {};
    const rents = {};
    let any = false;
    Array.from(form.querySelectorAll('input[name^="mix_"]')).forEach((el) => {
      const v = String(el.value || '').trim();
      if (v === '') return;
      mix[el.name.slice(4)] = Number(v);
      any = true;
    });
    Array.from(form.querySelectorAll('input[name^="rent_"]')).forEach((el) => {
      const v = String(el.value || '').trim();
      if (v !== '') rents[el.name.slice(5)] = v;
    });
    const units = text(form, 'units');
    return {units: units === null ? null : Number(units), mix: any ? mix : null, rents: Object.keys(rents).length ? rents : null};
  }

  function recordBody(form) {
    const kind = KINDS.find((k) => k[0] === text(form, 'kind')) || KINDS[0];
    const body = Object.assign({source: text(form, 'source'), basis: text(form, 'basis'), evidence_date: text(form, 'evidence_date'), note: text(form, 'note')}, kind[2]);
    const amount = text(form, 'amount');
    if (kind[2].total_period) {
      body.total = amount;
    } else {
      body.rate = amount;
      body.quantity = text(form, 'quantity');
      if (kind[2].quantity_kind === 'rentable_sqft') body.area_type = text(form, 'area_type');
      const total = text(form, 'check_total');
      if (total !== null) {
        body.total = total;
        body.total_period = text(form, 'check_period');
        body.authoritative = text(form, 'trust');
      }
    }
    return body;
  }

  function historyBody(form) {
    const checked = form.elements.sale_bundled && form.elements.sale_bundled.checked;
    return {builder_state: text(form, 'builder_state'), builder_source: text(form, 'builder_source'), builder_note: text(form, 'builder_note'),
      sale_source: text(form, 'sale_source'), building_area_type: text(form, 'building_area_type'), sale_bundled: !!checked};
  }

  /* ---------- events (bound once on the drawer) ---------- */

  function onClick(ev) {
    const b = ev.target instanceof Element ? ev.target.closest('[data-pr-act]') : null;
    if (!b || !st || !st.d) return;
    const act = b.getAttribute('data-pr-act');
    if (act === 'napkin') {
      st.open = !st.open;
      redraw(st.open ? '#pr-napkin-h' : '[data-pr-act="napkin"]');
    } else if (act === 'pin') {
      write(base() + '/rent/records/' + Number(b.getAttribute('data-rid')) + '/pin', {pinned: b.getAttribute('data-pinned') === '1'});
    } else if (act === 'reset') {
      write(base() + '/rent/assumptions', {units: null, mix: null, rents: null}, 'PUT');
    }
  }

  function onSubmit(ev) {
    const form = ev.target instanceof Element ? ev.target.closest('form[data-pr-form]') : null;
    if (!form || !st || !st.d) return;
    ev.preventDefault();
    if (st.busy || (form.reportValidity && !form.reportValidity())) return;
    const kind = form.getAttribute('data-pr-form');
    if (kind === 'napkin') write(base() + '/rent/assumptions', napkinBody(form), 'PUT');
    else if (kind === 'record') write(base() + '/rent/records', recordBody(form));
    else if (kind === 'history') write(base() + '/history', historyBody(form), 'PUT');
  }

  function onChange(ev) {
    const t = ev.target;
    if (!(t instanceof Element) || !t.matches('select[name="kind"]')) return;
    const kind = KINDS.find((k) => k[0] === t.value) || KINDS[0];
    const form = t.closest('form');
    if (!form) return;
    form.setAttribute('data-rate', kind[2].rate_period ? '1' : '0');
    form.setAttribute('data-sqft', kind[2].quantity_kind === 'rentable_sqft' ? '1' : '0');
  }

  function mount(root, d) {
    if (!st || st.id !== d.id) return;
    st.root = root;
    if (root.__bmPricingBound) return;
    root.__bmPricingBound = true;
    root.addEventListener('click', onClick);
    root.addEventListener('submit', onSubmit);
    root.addEventListener('change', onChange);
  }

  BM.pricing = {html, mount, usd};
})();
