/* The score (Insight): how the Blue Moon score works, the weight of every signal (starting multiplier,
 * and the learned one once outcomes exist), and how it learns from the outcomes Seth records.
 */
(() => {
  'use strict';

  const BM = window.BM;
  const H = BM && BM.iv;
  if (!BM || !H) {
    console.error('score.js needs core.js and funnel.js to load first.');
    return;
  }
  const {esc, toNum, num, plural} = H;

  /* Why each signal moves the odds (plain-English copy; the numbers themselves come from YAML). */
  const REASONS = {
    size: 'Fits the buy box. The sweet spot is the easiest to finance, manage and sell.',
    visual: 'The photo check: does it look like what the buyer wants (for apartments, outdoor entry)?',
    rent_fit: 'HUD area rents compared with the buyer\'s rent scale.',
    drive: 'Closer properties are easier to visit and manage.',
    owner_type: 'Individuals, trusts and heirs sell below market more often. Institutions rarely do.',
    years_owned: 'Long holds mean a low basis, more equity and less attachment to price.',
    debt: 'No loan leaves room to sell low. A loan coming due adds timing pressure.',
    absentee: 'Owners who live far away sell more readily.',
    vacancy: 'A USPS vacancy flag can be a sign of distress.',
    condition: 'Low building value against the land suggests deferred upkeep.',
    distress: 'A pre-foreclosure filing means the owner may need to sell.',
  };

  function mult(m) {
    const v = toNum(m);
    if (v === null) return '—';
    const t = parseFloat(v.toFixed(2));          // 1 -> ×1.0, 1.3 -> ×1.3, 0.15 -> ×0.15
    return '×' + (Number.isInteger(t) ? t.toFixed(1) : String(t));
  }

  /* ---------- the four "how it works" tiles ---------- */

  function extremes(signals) {
    let up = null;
    let down = null;
    (signals || []).forEach((s) => (s.buckets || []).forEach((b) => {
      const m = toNum(b.start);
      if (m === null) return;
      if (!up || m > up.m) up = {m, label: b.label};
      if (!down || m < down.m) down = {m, label: b.label};
    }));
    return {up, down};
  }

  function howTiles(model) {
    const base = toNum(model.base_rate) || 0;
    const oneIn = base > 0 ? Math.round(1 / base) : null;
    const signals = model.signals || [];
    const {up, down} = extremes(signals);
    const cov = toNum(model.scored && model.scored.coverage_avg);
    const known = cov === null ? null : Math.round(cov * signals.length);
    const tile = (k, title, text) => '<div><div class="k">' + k + '</div><b>' + esc(title) + '</b><p>' + text + '</p></div>';
    return '<div class="how">' +
      tile(esc(BM.fmt.pct(base)), 'Start rare',
        'Before we know anything, about ' + (oneIn ? '1 in ' + num(oneIn) : 'very few') +
        ' properties in the buy box could be bought at a Blue Moon price.') +
      tile('×', 'Each signal multiplies',
        (up ? esc(up.label) + ' raises the odds ' + esc(mult(up.m)) + '. ' : '') +
        (down ? esc(down.label) + ' cuts them ' + esc(mult(down.m)) + '.' : 'Each thing we know moves the odds up or down.')) +
      tile('—', 'Missing means skipped',
        'No data on a signal? It is left out, never counted against the property.') +
      tile('▮▮▯', 'Data shows certainty',
        (known === null ? 'The Data column on Deals shows how many signals we know.' :
          'On average we know ' + num(known) + ' of ' + num(signals.length) + ' signals. ') +
        ' A score built on more signals is firmer.') +
      '</div>';
  }

  /* ---------- weights table ---------- */

  function startCell(s) {
    return (s.buckets || []).map((b) => '<span class="sc-b">' + esc(b.label) + ' <span class="sc-m">' + esc(mult(b.start)) + '</span></span>').join('');
  }

  function learnedCell(s, learned) {
    if (!learned) return '<span class="muted">Not yet</span>';
    const moved = (s.buckets || []).filter((b) => Math.abs((toNum(b.learned) || 0) - (toNum(b.start) || 0)) >= 0.005);
    if (!moved.length) return '<span class="muted">Same as the start</span>';
    return moved.map((b) => {
      const dir = toNum(b.learned) > toNum(b.start) ? 'up' : 'down';
      return '<span class="sc-b ' + dir + '" title="' + esc(b.label + ': started ' + mult(b.start) + ', now ' + mult(b.learned) +
        '. ' + num(b.positives) + ' good deals were in this group.') + '">' + esc(b.label) +
        ' <span class="sc-m">' + esc(mult(b.learned)) + '</span> <span class="hint">n=' + num(b.positives) + '</span></span>';
    }).join('');
  }

  function weightsTable(model) {
    const learned = !!model.learned;
    const rows = (model.signals || []).map((s) => '<tr><td class="sc-sig">' + esc(s.label) + '</td>' +
      '<td data-l="Starting multipliers"><div class="sc-bs">' + startCell(s) + '</div></td>' +
      '<td data-l="Learned"><div class="sc-bs">' + learnedCell(s, learned) + '</div></td>' +
      '<td class="muted">' + esc(REASONS[s.key] || '') + '</td></tr>').join('');
    return '<div class="vw-scroll"><table class="wt sc-table"><thead><tr><th>Signal</th><th>Starting multipliers</th>' +
      '<th>Learned</th><th>Why it matters</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  /* ---------- learning ---------- */

  function outcomeCounts(model) {
    const o = model.outcomes || {};
    const labels = o.labels || {};
    const by = o.by_outcome || {};
    const keys = Object.keys(labels).length ? Object.keys(labels) : Object.keys(by);
    return keys.map((k) => esc(labels[k] || k) + ' ' + num(by[k] || 0)).join(' · ');
  }

  function learnPanel(model) {
    const o = model.outcomes || {};
    const good = (model.positive_outcomes || []).map((k) => (o.labels || {})[k] || k).join(' or ');
    const prior = toNum(model.prior_strength);
    return '<div class="panel"><h2>How it learns</h2>' +
      '<dl class="kv"><dt>Outcomes logged</dt><dd class="num">' + num(o.total) + (toNum(o.total) ? ' (' + outcomeCounts(model) + ')' : '') + '</dd>' +
      '<dt>Good deals</dt><dd class="num">' + num(o.positive) + (good ? ' <span class="muted">(' + esc(good) + ')</span>' : '') + '</dd>' +
      '<dt>Weights adjusted</dt><dd>' + (model.learned ? 'Yes. The Learned column shows what moved.' : 'Not yet.') + '</dd></dl>' +
      '<p class="note vw-tight">' + esc(model.message || '') + '</p>' +
      '<p class="note vw-tight"><b>Mark outcomes on properties:</b> open a property and use "How did it end?" at the bottom: ' +
      'Blue Moon deal, Bought, Passed or Lost.</p>' +
      (prior ? '<p class="note vw-tight">Each starting weight counts like ' + num(prior) +
        ' outcomes, so a few deals nudge it and many deals move it. Outcomes are shared by every market of the same asset class.</p>' : '') +
      '</div>';
  }

  function blindPanel() {
    return '<div class="panel"><h2>What it can\'t see yet</h2>' +
      '<p class="note vw-tight">Unpaid property taxes, code violations, probate and evictions are strong signs a seller may take a low price. ' +
      'Counties publish some of these (see Data sources). They are good candidates for a later layer.</p>' +
      '<p class="note vw-tight">Pricing and the Blue Moon strike price come from Brad\'s spreadsheet in a separate buildout.</p></div>';
  }

  function heading(model) {
    const label = model && model.asset_label ? model.asset_label : '';
    return H.head('The model' + (label ? ' · ' + label : ''), 'How the Blue Moon score works', [
      'The Blue Moon score answers one question: is this owner worth chasing?',
      'It works like a weather forecast. Start rare, then each signal pushes the odds up or down.',
    ]);
  }

  function page(model) {
    const scored = model.scored || {};
    return heading(model) + howTiles(model) +
      '<div class="grid2">' +
      '<div class="panel vw-full"><h2>' + (model.learned ? 'Weights' : 'Starting weights') + '</h2>' +
      '<p class="note vw-tight">Starting estimates come from common investor practice for finding motivated sellers ' +
      '(config/asset_classes.yaml). They stay estimates until your own outcomes confirm or correct them.</p>' +
      weightsTable(model) +
      (toNum(scored.properties) ? '<p class="note vw-tight">Scored now: ' + plural(scored.properties, 'property', 'properties') +
        ', ' + num(scored.in_buybox) + ' in the buy box.</p>' : '') + '</div>' +
      learnPanel(model) + blindPanel() + '</div>';
  }

  async function render(root) {
    root.innerHTML = heading(null) + H.loading('Loading the score model…');
    let model;
    try {
      model = await BM.api(H.mpath('/model'));
    } catch (err) {
      if (root.isConnected) root.innerHTML = heading(null) + H.errorBox(err);
      return;
    }
    if (root.isConnected) root.innerHTML = page(model || {});
  }

  BM.registerView('score', {label: 'The score', group: 'insight', order: 3, render});
})();
