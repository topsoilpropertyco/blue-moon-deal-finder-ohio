/* Data sources (Setup): the free-first source list the DRILL uses (priority, what it gives, cost, which
 * DRILL stages use it, a link), filtered by stage and asset class, plus the county table (auditor deep
 * link, rental registration, recorder, probate, clerk). People-search sites and Crexi are never automated.
 */
(() => {
  'use strict';

  const BM = window.BM;
  const H = BM && BM.iv;
  if (!BM || !H) {
    console.error('sources.js needs core.js and funnel.js to load first.');
    return;
  }
  const {esc, num, plural, extLink, safeUrl} = H;

  const COST = {
    free: ['pill', 'Free'],
    credit: ['pill hot', 'Crexi credit'],
    paid: ['pill warn', 'Paid'],
  };
  const KIND_TEXT = {
    auto: 'Built in: the app does it',
    county: 'Per county: see the table below',
    manual: 'By hand, outside the app',
  };

  const S = {data: null, stage: '', asset: null};

  /* ---------- the source list ---------- */

  function stageNames(data) {
    const map = {};
    (data.stages || []).forEach((s) => { map[s.key] = s.name || s.key; });
    return map;
  }

  function stagesOf(src) {
    const set = new Set([...(src.stages || []), ...(src.drill_stages || [])]);
    return Array.from(set);
  }

  function assetClasses() {
    const seen = new Map();
    (BM.state.markets || []).forEach((m) => {
      if (m.asset_class && !seen.has(m.asset_class)) seen.set(m.asset_class, m.asset_label || m.asset_class);
    });
    return seen;
  }

  function matches(src) {
    if (S.stage && !stagesOf(src).includes(S.stage)) return false;
    if (S.asset && Array.isArray(src.asset_classes) && src.asset_classes.length && !src.asset_classes.includes(S.asset)) return false;
    return true;
  }

  /** A plain link for the source: its own page, a fixed template, or the site behind a template. */
  function sourceUrl(src) {
    if (safeUrl(src.url)) return src.url;
    const t = String(src.template || '');
    if (safeUrl(t) && !t.includes('{')) return t;
    try {
      return safeUrl(t) ? new URL(t.replace(/\{[^}]*\}/g, '')).origin : '';
    } catch (e) {
      return '';
    }
  }

  function linkCell(src) {
    if (src.key === 'crexi_contact') return '<span class="muted">In Crexi, by hand</span>';
    const url = sourceUrl(src);
    if (url) return extLink(url, 'Open', src.name);
    return '<span class="muted">' + esc(KIND_TEXT[src.kind] || '—') + '</span>';
  }

  function costChip(src) {
    const c = COST[src.cost] || COST.free;
    return '<span class="' + c[0] + '">' + esc(c[1]) + '</span>';
  }

  function stageChips(src, names) {
    const list = stagesOf(src);
    if (!list.length) return '<span class="muted">—</span>';
    return '<span class="so-stages">' + list.map((k) => '<span class="so-stage">' + esc(names[k] || k) + '</span>').join('') + '</span>';
  }

  function scopeText(src) {
    const parts = [];
    if (Array.isArray(src.states) && src.states.length) parts.push(src.states.join(', ') + ' only');
    if (Array.isArray(src.asset_classes) && src.asset_classes.length) {
      const labels = assetClasses();
      parts.push(src.asset_classes.map((a) => labels.get(a) || a.replace(/_/g, ' ')).join(', '));
    }
    return parts.join(' · ');
  }

  function nameCell(src) {
    const scope = scopeText(src);
    return '<div class="addr">' + esc(src.name || src.key) + '</div>' +
      (src.finds ? '<div class="so-finds">' + esc(src.finds) + '</div>' : '') +
      (src.how ? '<details class="so-how"><summary>How to use it</summary><p class="note">' + esc(src.how) + '</p></details>' : '') +
      (scope ? '<div class="hint">' + esc(scope) + '</div>' : '');
  }

  function sourceTable(list, names) {
    const rows = list.map((src) => '<tr><td class="c num">' + esc(src.priority || '') + '</td>' +
      '<td class="wrap">' + nameCell(src) + '</td><td>' + costChip(src) + '</td>' +
      '<td class="wrap">' + stageChips(src, names) + '</td><td>' + linkCell(src) + '</td></tr>').join('');
    const cards = list.map((src) => '<div class="card so-card"><span class="so-pri num" title="Priority (1 = try first)">' +
      esc(src.priority || '') + '</span><div class="so-body">' + nameCell(src) +
      '<div class="so-meta">' + costChip(src) + stageChips(src, names) + '<span>' + linkCell(src) + '</span></div></div></div>').join('');
    return '<div class="tablewrap"><table class="so-table"><thead><tr><th class="c" title="1 = try first">#</th><th>Source and what it gives</th>' +
      '<th>Cost</th><th>DRILL stages</th><th>Link</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<div class="cards">' + cards + '</div>';
  }

  function filters(data, names) {
    const stageOpts = ['<option value="">Every DRILL stage</option>'].concat((data.stages || []).map((s) =>
      '<option value="' + esc(s.key) + '"' + (S.stage === s.key ? ' selected' : '') + '>' + esc(names[s.key]) + '</option>'));
    const acOpts = ['<option value="">Every asset class</option>'];
    assetClasses().forEach((label, key) => acOpts.push('<option value="' + esc(key) + '"' +
      (S.asset === key ? ' selected' : '') + '>' + esc(label) + '</option>'));
    return '<div class="bar so-filters"><label class="sr" for="so-stage">DRILL stage</label>' +
      '<select class="select" id="so-stage" data-so="stage">' + stageOpts.join('') + '</select>' +
      '<label class="sr" for="so-asset">Asset class</label>' +
      '<select class="select" id="so-asset" data-so="asset">' + acOpts.join('') + '</select>' +
      '<span class="note" data-so-count></span></div>';
  }

  function listHtml(data) {
    const names = stageNames(data);
    const all = data.sources || [];
    const list = all.filter(matches);
    const count = 'Showing ' + num(list.length) + ' of ' + plural(all.length, 'source') + '.';
    const body = list.length ? sourceTable(list, names) :
      '<p class="note">No source matches both filters. Try "Every DRILL stage".</p>';
    return {count, body};
  }

  /* ---------- the county table ---------- */

  function entryOf(county, slot) {
    const e = county[slot];
    if (!e) return null;
    return typeof e === 'string' ? {url: e} : e;
  }

  function slotLink(county, slot, text) {
    const e = entryOf(county, slot);
    if (!e || !safeUrl(e.url)) return '<span class="muted">—</span>';
    return extLink(e.url, text, [e.name, e.how].filter(Boolean).join('. '));
  }

  function auditorCell(county) {
    const e = entryOf(county, 'auditor');
    if (!e) return '<span class="muted">—</span>';
    const deep = !!e.template && e.template_status !== 'none';
    const tip = [e.name, e.how].filter(Boolean).join('. ');
    return '<span class="' + (deep ? 'pill good' : 'pill') + '" title="' + esc(tip) + '">' +
      (deep ? 'Yes' : 'Search page') + '</span>' + (safeUrl(e.url) ? ' ' + extLink(e.url, 'Open', tip) : '');
  }

  function rentalCell(county) {
    const e = entryOf(county, 'rental_registration');
    const city = entryOf(county, 'city_rental');
    if (e && e.available === false) {
      const tip = e.how || 'No county rental registry.';
      if (city && safeUrl(city.url)) return extLink(city.url, 'City only', [tip, city.how].filter(Boolean).join(' '));
      return '<span class="muted" title="' + esc(tip) + '">None</span>';
    }
    if (e && safeUrl(e.url)) return extLink(e.url, e.includes_phone ? 'Yes, with phone' : 'Yes', [e.name, e.how].filter(Boolean).join('. '));
    return '<span class="muted">—</span>';
  }

  function countyName(c) {
    const name = BM.fmt.title ? BM.fmt.title(String(c.county || '')) : String(c.county || '');
    return '<span title="' + esc(c.note || '') + '">' + esc(name) + ', ' + esc(c.state) + '</span>';
  }

  function countyTable(counties) {
    if (!counties.length) return '<p class="note">No counties in config/sources.yaml yet.</p>';
    const rows = counties.map((c) => '<tr><td class="addr">' + countyName(c) + '</td><td>' + auditorCell(c) + '</td>' +
      '<td>' + rentalCell(c) + '</td><td>' + slotLink(c, 'recorder', 'Open') + '</td>' +
      '<td>' + slotLink(c, 'probate', 'Open') + '</td><td>' + slotLink(c, 'clerk', 'Open') + '</td></tr>').join('');
    return '<div class="tablewrap so-counties"><table><thead><tr><th>County</th><th title="Does the app open the exact parcel page?">Auditor deep link</th>' +
      '<th>Rental registration</th><th>Recorder</th><th>Probate</th><th>Clerk of courts</th></tr></thead><tbody>' +
      rows + '</tbody></table></div>';
  }

  /* ---------- page ---------- */

  const HEAD = H.head('Setup · Data sources', 'Where owner data comes from', [
    'Free sources first. The DRILL tries them in priority order (1 = first).',
    'Crexi credits come after the free stages. Paid skip tracing is off.',
  ]);

  const NEVER = '<div class="notice so-never"><p><b>Never automated:</b> <span>people-search sites and Crexi. ' +
    'The app only builds links; you open them and look yourself. County sites are links for a person, too.</span></p></div>';

  function page(data) {
    const names = stageNames(data);
    const counties = data.counties || [];
    return HEAD + NEVER +
      '<section class="vw-sec"><h2 class="vw-sub">Sources</h2>' + filters(data, names) +
      '<div data-so-list></div></section>' +
      '<section class="vw-sec"><h2 class="vw-sub">Counties</h2>' +
      '<p class="note vw-tight">' + plural(counties.length, 'county', 'counties') + ' researched. A deep link opens the exact parcel page; ' +
      'otherwise the app opens the search page. Hover a link for how to use it. Full notes: docs/DATA_SOURCES.md.</p>' +
      countyTable(counties) + '</section>';
  }

  function updateList(root) {
    const {count, body} = listHtml(S.data);
    const box = root.querySelector('[data-so-list]');
    if (box) box.innerHTML = body;
    const c = root.querySelector('[data-so-count]');
    if (c) c.textContent = count;
  }

  function onChange(root, ev) {
    const t = ev.target;
    if (!(t instanceof HTMLSelectElement) || !t.dataset.so) return;
    if (t.dataset.so === 'stage') S.stage = t.value;
    if (t.dataset.so === 'asset') S.asset = t.value;
    updateList(root);
  }

  async function render(root) {
    if (S.asset === null) {    // first visit: show what applies to the current market's asset class
      const m = H.currentMarket();
      S.asset = (m && m.asset_class) || '';
    }
    root.innerHTML = HEAD + H.loading('Loading the source list…');
    try {
      S.data = (await BM.api('/api/sources')) || {};
    } catch (err) {
      if (root.isConnected) root.innerHTML = HEAD + H.errorBox(err);
      return;
    }
    if (!root.isConnected) return;
    root.innerHTML = page(S.data);
    updateList(root);
    root.addEventListener('change', (ev) => onChange(root, ev));
  }

  BM.registerView('sources', {label: 'Data sources', group: 'setup', order: 5, render});
})();
