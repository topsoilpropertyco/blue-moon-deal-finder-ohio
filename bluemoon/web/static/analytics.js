/* Analytics (Insight): what is in the data. Last import, the buy-box filter funnel, how complete each
 * field is, properties by county, size and Blue Moon score distributions, owner types, contact levels,
 * and this month's Google and Claude usage with the estimated bill. Every chart is a simple horizontal
 * bar with its number; hover a bar for the detail.
 */
(() => {
  'use strict';

  const BM = window.BM;
  const H = BM && BM.iv;
  if (!BM || !H) {
    console.error('analytics.js needs core.js and funnel.js to load first.');
    return;
  }
  const {esc, toNum, num, plural, bars} = H;

  const TOP_COUNTIES = 10;
  const SKU_LABELS = {
    route_matrix_element: 'Google Routes (drive times)',
    geocoding: 'Google Geocoding (map locations)',
    street_view_static: 'Street View photos',
    static_maps: 'Satellite photos',
    street_view_metadata: 'Street View lookups (free)',
    county_gis: 'County records (free county websites)',
    vision_claude_cli: 'Photo check on your Claude plan',
    vision_anthropic_api: 'Photo check on the Anthropic API',
  };
  const COLOR_CLASS = {green: 'good', yellow: 'warn', red: 'bad', na: 'na'};

  function skuLabel(sku) {
    return SKU_LABELS[sku] || String(sku || 'Other').replace(/_/g, ' ');
  }

  /* ---------- heading ---------- */

  function lastImportLine(data, s) {
    const imp = (data.imports && data.imports[0]) || (s && s.last_import);
    const total = toNum(data.properties) || 0;
    if (!imp) return '<b>Last import:</b> none yet.';
    const files = (data.imports || []).length;
    return '<b>Last import:</b> ' + esc(BM.fmt.date(imp.created_at)) + ' · ' + esc(imp.filename || 'file') +
      (files > 1 ? ' · ' + plural(files, 'import') + ' so far' : '') + ' · ' + plural(total, 'property', 'properties') + '.';
  }

  function heading(data, s) {
    return H.head('Analytics', "What's in the data", [
      data ? lastImportLine(data, s) : '',
      'Every number on this screen comes from your own imports.',
    ]);
  }

  /* ---------- panels ---------- */

  function panel(title, inner, note, cls) {
    return '<div class="panel' + (cls ? ' ' + cls : '') + '"><h2>' + esc(title) + '</h2>' + inner +
      (note ? '<p class="note vw-tight">' + note + '</p>' : '') + '</div>';
  }

  function filterFunnel(data, s) {
    const steps = data.filter_funnel || [];
    const rows = steps.map((st, i) => ({
      label: st.label, value: st.count, cls: i === steps.length - 1 && i > 0 ? 'close' : '',
      drop: i > 0 && st.removed ? num(st.removed) + ' set aside: ' + String(st.removed_label || '').toLowerCase() : '',
    }));
    const drive = ((s && s.layers) || []).find((l) => l.key === 'drive');
    const approx = drive && drive.note ? 'Drive times are straight-line estimates for now. Real drive times will tighten it.' : '';
    return panel('Filter funnel', bars(rows, {max: toNum(steps[0] && steps[0].count), drops: true}),
      esc(['Set-aside properties are never deleted; they go to the excluded files.', approx].filter(Boolean).join(' ')));
  }

  function coverage(data) {
    const rows = (data.coverage || []).map((c) => ({
      label: c.label, value: toNum(c.pct) === null ? 0 : c.pct, cls: (toNum(c.pct) || 0) < 50 ? 'warn' : '',
      title: c.label + ': ' + num(c.count) + ' of ' + num(data.properties) + ' properties (' + num(c.pct) + '%)',
    }));
    return panel('How complete the data is', bars(rows, {max: 100, unit: '%'}),
      'Missing data never rules a property out. It just leaves that signal out of the score.');
  }

  function counties(data) {
    const list = data.counties || [];
    const top = list.slice(0, TOP_COUNTIES).map((c) => ({
      label: BM.fmt.title ? BM.fmt.title(c.county) + ', ' + c.state : c.county + ', ' + c.state,
      value: c.count, hint: num(c.in_buybox) + ' in the buy box',
    }));
    const rest = list.slice(TOP_COUNTIES);
    if (rest.length) {
      const n = rest.reduce((a, c) => a + (toNum(c.count) || 0), 0);
      const box = rest.reduce((a, c) => a + (toNum(c.in_buybox) || 0), 0);
      top.push({label: plural(rest.length, 'other county', 'other counties'), value: n, cls: 'na', hint: num(box) + ' in the buy box'});
    }
    return panel('Properties by county', bars(top));
  }

  function sizeNote(s) {
    const ac = s && s.asset_class;
    if (!ac || !ac.size_ok || !ac.size_ideal) return '';
    const unit = ac.size_unit || ac.size_metric || '';
    const r = (a) => num(a[0]) + '–' + num(a[1]);
    return 'The buy box is ' + esc(r(ac.size_ok) + ' ' + unit) + '. The sweet spot is ' + esc(r(ac.size_ideal)) + '.';
  }

  function sizes(data, s) {
    const list = data.size_dist || [];
    const unit = (list[0] && list[0].unit) || (s && s.asset_class && s.asset_class.size_unit) || '';
    const rows = list.map((b) => ({
      label: b.label, value: b.count, cls: COLOR_CLASS[b.color] || '',
      hint: toNum(b.estimated) ? num(b.estimated) + ' estimated' : '',
    }));
    return panel('Properties by size' + (unit ? ' (' + unit + ')' : ''), bars(rows), sizeNote(s));
  }

  function scores(data) {
    const rows = (data.score_dist || []).map((b) => ({
      label: b.label, value: b.count, cls: b.lo === null || b.lo === undefined ? 'na' : '',
    }));
    return panel('Blue Moon scores', bars(rows),
      'Properties in the buy box. The score is the odds the owner sells at a Blue Moon price.');
  }

  function ownerTypes(data) {
    const rows = (data.owner_types || []).map((o) => ({
      label: o.label, value: o.count, cls: o.owner_type === 'not_checked' ? 'na' : '',
    }));
    return panel('Owner types', bars(rows), 'From the owner name and how many properties that owner holds.');
  }

  /** Every property's contact level, with the buy-box share as a hint so the numbers line up with the DRILL and
   *  Funnel (which count buy-box owners only). Uses the server's per-row in_buybox; older servers only give
   *  the summary's levels_in_buybox, which has no do-not-contact count. */
  function levels(data, s) {
    const box = (s && s.levels_in_buybox) || {};
    const rows = (data.levels || []).map((l) => {
      const inBox = toNum(l.in_buybox) !== null ? toNum(l.in_buybox) : (l.key === 'dnc' ? null : toNum(box[l.key]));
      return {
        label: l.label, value: l.count, cls: l.level === 3 ? 'close' : (l.key === 'dnc' ? 'bad' : ''),
        hint: inBox === null ? '' : num(inBox) + ' in the buy box',
      };
    });
    return panel('Contact levels · every property', bars(rows),
      'Counts set-aside properties too. The DRILL and Funnel count buy-box owners only (the hint under each bar). ' +
      'Level 3 only after a real conversation confirms the owner.');
  }

  /* ---------- usage this month ---------- */

  function usageRow(u) {
    const plan = u.sku === 'vision_claude_cli';
    const free = toNum(u.free_per_month);
    const usd = toNum(u.usd);
    return '<tr><td>' + esc(skuLabel(u.sku)) + (u.note ? '<div class="note">' + esc(u.note) + '</div>' : '') + '</td>' +
      '<td class="c num">' + num(u.units) + '</td>' +
      '<td class="c num">' + (free === null ? (plan ? 'Plan' : '—') : num(free)) + '</td>' +
      '<td class="c num">' + (toNum(u.billable) === null ? '—' : num(u.billable)) + '</td>' +
      '<td class="c num">' + (usd === null ? (plan ? 'No bill' : '—') : '$' + usd.toFixed(2)) + '</td></tr>';
  }

  function usage(data) {
    const list = data.api_usage || [];
    if (!list.length) {
      return panel('Google and Claude this month', '<p class="note vw-tight">No metered calls yet this month. ' +
        'Free steps (owners, rent, score, DRILL) never show here.</p>');
    }
    const total = list.reduce((a, u) => a + (toNum(u.usd) || 0), 0);
    const table = '<div class="vw-scroll"><table class="wt vw-usage"><thead><tr><th>Service</th><th class="c">Calls</th>' +
      '<th class="c">Free a month</th><th class="c">Billable</th><th class="c">Est. cost</th></tr></thead><tbody>' +
      list.map(usageRow).join('') + '</tbody></table></div>';
    return panel('Google and Claude this month',
      '<div class="vw-bigline"><span class="big num">$' + total.toFixed(2) + '</span>' +
      '<span class="note">estimated bill this month, after Google\'s free monthly calls (all markets)</span></div>' + table,
      'Prices come from config.yaml (google.prices). Check them against Google\'s pricing page now and then.', 'vw-full');
  }

  function page(data, s) {
    return heading(data, s) + '<div class="grid2">' +
      filterFunnel(data, s) + coverage(data) + counties(data) + sizes(data, s) +
      scores(data) + ownerTypes(data) + levels(data, s) + usage(data) + '</div>';
  }

  async function render(root) {
    root.innerHTML = heading(null) + H.loading('Adding up the data…');
    let data;
    let s;
    try {
      [data, s] = await Promise.all([BM.api(H.mpath('/analytics')), H.summary()]);
    } catch (err) {
      if (root.isConnected) root.innerHTML = heading(null) + H.errorBox(err);
      return;
    }
    if (!root.isConnected) return;
    if (!toNum(data && data.properties)) {
      root.innerHTML = heading(data || {}, s) + H.emptyMarket() + (data && (data.api_usage || []).length ?
        '<div class="grid2">' + usage(data) + '</div>' : '');
      return;
    }
    root.innerHTML = page(data, s);
  }

  BM.registerView('analytics', {label: 'Analytics', group: 'insight', order: 2, render});
})();
