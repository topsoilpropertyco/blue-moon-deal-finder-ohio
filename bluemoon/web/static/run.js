/* Run (Setup): every step from import to spending Crexi credits, as one numbered list top to bottom. Each row
 * says what the step does, whether it costs money, and where it stands (done, do next, waiting on another
 * step, or blocked until something is set up); "Do this next" at the top names the first things to do. "Estimate" shows calls, Google's free calls left and dollars without calling anything. "Run"
 * starts a background job; "Test on 5" runs it on 5 properties first. A paid run that is too big stops and
 * waits: the card shows the estimate and a "Yes, spend it" button that starts it again with confirm.
 * Jobs are polled every 1.5 s while this screen is open; they keep running if you leave it.
 */
(() => {
  'use strict';

  const BM = window.BM;
  const H = BM && BM.iv;
  if (!BM || !H) {
    console.error('run.js needs core.js and funnel.js to load first.');
    return;
  }
  const {esc, toNum, num, plural} = H;

  const POLL_MS = 1500;
  const TEST_N = 5;
  const EST_FRESH_MS = 60000;
  const RECENT_N = 10;
  const PHOTO_COUNT_MS = 3000;    // refresh the photo button's count this often while its job runs
  const ACTIVE = ['queued', 'running'];

  /* The steps, top to bottom in the order to do them. `layers`: keys in summary.layers. `go`: a screen where a
     person does the step (no Run button). Run everything runs the machine steps in its own order. */
  const STEPS = [
    {key: 'import', title: 'Import a Crexi list', layers: [], go: 'import', goLabel: 'Import a file',
      what: () => 'Export a list from Crexi (CSV or Excel) and drop it on the Import screen. Duplicates merge; the free steps below run by themselves.'},
    {key: 'owners', title: 'Owners', layers: ['owners'], limit: false,
      what: () => 'Owner type (individual, trust, LLC, portfolio...), years owned, loan status and whether the owner lives far away.'},
    {key: 'drive', title: 'Drive times', layers: ['drive'], limit: true,
      what: (c) => 'Minutes from ' + esc(c.origin) + ' to each property' + (c.google ? ', from Google Routes.' :
        '. With no Google key yet, these are free straight-line estimates.')},
    {key: 'rent', title: 'Rent', layers: ['rent'], limit: true, needs: 'rent_enabled',
      what: () => 'HUD area rents (free HUD map service) compared with the rent scale.'},
    {key: 'score', title: 'Score', layers: ['score'], limit: false,
      what: () => 'Buy box, colors, Blue Moon score and rank. Properties that fail a filter are set aside, never deleted.'},
    {key: 'parcels', title: 'County records', layers: ['parcels'], limit: true,
      what: () => 'The owner and the tax mailing address from each Ohio county\'s free public parcel map, buy-box properties first. ' +
        'Fills the DRILL\'s county auditor step. Counties without a map of their own use older (2023-24) statewide data. Needs a map location (the drive step places them).'},
    {key: 'photos', title: 'Photos', layers: ['photos'], limit: true, needs: 'visual_enabled',
      what: () => '3 Street View photos and 1 satellite photo of each buy-box property. The first paid step.'},
    {key: 'classify', title: 'Photo check', layers: ['photo_check', 'visual'], limit: true, needs: 'visual_enabled',
      what: (c) => 'Claude looks at the photos' + (c.question ? ': "' + esc(c.question) + '"' : '') + '. Unsure answers go to Photo review. Scores update by themselves.'},
    {key: 'review', title: 'Photo review (you)', layers: [], needs: 'visual_enabled', go: 'review', goLabel: 'Open Photo review',
      what: () => 'You answer the photos Claude was unsure about. Your answers become its examples.'},
    {key: 'calibrate', title: 'Calibrate the photo check', layers: [], limit: false, needs: 'visual_enabled', noEstimate: true,
      what: () => 'Claude checks again every property you answered in Photo review, using your answers as examples, ' +
        'then updates the Scorecard (the 90% check). Optional; worth it after 20 or more answers.'},
    {key: 'drill', title: 'DRILL auto-stages', layers: ['drill'], limit: false,
      what: () => 'Records baseline and portfolio match, then every owner\'s contact level.'},
    {key: 'research', title: 'AI research (owners)', layers: [], limit: true,
      what: () => 'Claude searches the web for each buy-box owner\'s business: LLC filing, officers and agent, website, ' +
        'business phone and email, and social pages, with the link where it found each. It never uses people-search ' +
        'sites or Crexi. Everything it finds stays "not tested yet" until you reach out. One owner takes a few minutes.'},
    {key: 'credits', title: 'Spend Crexi credits in batches (you)', layers: [], go: 'credits', goLabel: 'Open Credits',
      what: () => 'On the Credits screen, export the next batch of owners, upload the file to Crexi yourself, then drop Crexi\'s file back here. Credits don\'t roll over.'},
    {key: 'all', title: 'Run everything', layers: [], limit: true, wide: true,
      what: () => 'Every machine step in order: owners, drive, rent, score, county records, photos, photo check, score, DRILL. ' +
        'Free steps run first. Big paid steps stop and wait for your OK.'},
  ];
  const LIST = STEPS.filter((st) => st.key !== 'all');

  /* The one photo button: photos for every lead with none, then the AI's call on each, as one job (imagery/photos_all.py). */
  const PHOTO_STEP = {key: 'photos_all', title: 'Photos and the AI\'s call for every lead', photo: true};
  const isPhotoKey = (key) => key === PHOTO_STEP.key;

  const S = {
    root: null, market: null, ctx: null,
    ph: null,          // the photo button's status ("312 of 2,981 done"), or an Error
    phEst: null,       // its estimate once pressed: null | 'loading' | Error | the estimate
    phAt: 0,           // when the photo count was last refreshed during a run
    est: null, estAt: 0, estBusy: false, estAgain: false, estError: null, estShown: new Set(),
    jobs: {},          // step key -> latest job this session (kept until dismissed)
    recent: null,      // recent jobs list, or an Error
    poll: null, polling: false,
  };

  function resetForMarket() {
    stopPolling();
    Object.assign(S, {est: null, estAt: 0, estBusy: false, estAgain: false, estError: null, estShown: new Set(), jobs: {},
      recent: null, ph: null, phEst: null});
  }

  /* ---------- context: summary, health, settings ---------- */

  async function loadContext() {
    const health = BM.state.health ? Promise.resolve(BM.state.health) : BM.api('/api/health').catch(() => ({}));
    const settings = BM.api('/api/settings').catch(() => ({}));
    const [summary, h, cfg] = await Promise.all([H.summary(), health, settings]);
    const ac = (summary && summary.asset_class) || {};
    const market = (summary && summary.market) || {};
    const safety = ((cfg && cfg.config) || {}).safety || {};
    return {
      summary: summary || {},
      ac,
      origin: market.origin_label || 'the market\'s starting point',
      question: ac.visual_question || '',
      google: !!(h && h.keys && h.keys.google),
      backend: (h && h.vision_backend) || '',
      claudeCli: !!(h && h.claude_cli),
      confirmOver: toNum(safety.confirm_over_properties) || 50,
    };
  }

  function costChip(step, c) {
    const chip = (cls, text) => '<span class="pill ' + cls + '">' + esc(text) + '</span>';
    switch (step.key) {
      case 'drive': return c.google ? chip('warn', 'Costs money: Google') : chip('', 'Free estimate');
      case 'photos': return chip('warn', 'Costs money: Google');
      case 'classify': case 'calibrate':
        if (c.backend === 'anthropic_api') return chip('warn', 'Costs money: Anthropic API');
        if (c.backend === 'mock') return chip('', 'Test mode');
        return chip('hot', 'Your Claude plan');
      case 'research': return chip('hot', 'Your Claude plan');
      case 'all': return chip('warn', 'Paid steps ask first');
      default: return chip('', 'Free');
    }
  }

  function stepNote(step, c) {
    if (step.key === 'photos' && !c.google) return 'Needs the Google key first. On the Mac Mini: uv run run.py key google (Settings has the steps).';
    const vision = step.key === 'classify' || step.key === 'calibrate';
    if (vision && c.backend === 'claude_cli' && !c.claudeCli) {
      return 'Claude Code isn\'t set up on this computer yet. Install it and sign in once (type claude, then /login).';
    }
    if (vision && c.backend === 'anthropic_api') return 'Uses ANTHROPIC_API_KEY (pay per photo check).';
    return '';
  }

  function isOff(step, c) {
    return !!(step.needs && c.ac && c.ac[step.needs] === false);
  }

  /* ---------- layer progress ---------- */

  function layerOf(step, c) {
    const list = (c.summary && c.summary.layers) || [];
    return list.find((l) => step.layers.includes(l.key)) || null;
  }

  function progressBar(fraction, label, full) {
    const w = Math.max(0, Math.min(100, (toNum(fraction) || 0) * 100));
    return '<div class="progress ru-bar' + (full && w >= 100 ? ' full' : '') + '" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' +
      Math.round(w) + '"' + (label ? ' aria-label="' + esc(label) + '"' : '') + '><i style="width:' + w.toFixed(1) + '%"></i></div>';
  }

  function layerHtml(step, c) {
    if (step.key === 'all') {
      const layers = ((c.summary && c.summary.layers) || []).filter((l) => l.enabled !== false && toNum(l.total));
      const done = layers.filter((l) => toNum(l.done) >= toNum(l.total)).length;
      return '<div class="ru-layer">' + progressBar(layers.length ? done / layers.length : 0, 'Layers complete', true) +
        '<span class="note">' + num(done) + ' of ' + plural(layers.length, 'layer') + ' complete for ' +
        plural(c.summary.properties, 'property', 'properties') + '</span></div>';
    }
    const l = layerOf(step, c);
    if (!l) return '';
    const total = toNum(l.total) || 0;
    const done = toNum(l.done) || 0;
    const text = total ? num(done) + ' of ' + num(total) + ' done' : 'Nothing to do yet';
    return '<div class="ru-layer">' + progressBar(total ? done / total : 0, step.title + ' progress', true) +
      '<span class="note">' + text + (l.note ? ' · ' + esc(l.note) : '') + '</span></div>';
  }

  /* ---------- where each step stands ---------- */

  function layerNums(key, c) {
    const step = stepByKey(key);
    const l = step ? layerOf(step, c) : null;
    return l ? {done: toNum(l.done) || 0, total: toNum(l.total) || 0, note: l.note || ''} : null;
  }

  function estToDo(key) {
    const line = S.est && (S.est.steps || []).find((l) => l.step === key);
    return line ? toNum(line.to_do) : null;
  }

  /** {state, text}: done | next (do it now) | wait (another step first) | blocked (set something up) | optional | off. */
  function stepState(step, c) {
    const s = c.summary || {};
    if (isOff(step, c)) return {state: 'off', text: 'Not used for ' + (c.ac.label || 'this asset class')};
    const L = layerNums(step.key, c);
    const complete = !!(L && L.total && L.done >= L.total);
    const left = L ? Math.max(0, L.total - L.done) : 0;
    switch (step.key) {
      case 'import': {
        const li = s.last_import;
        return s.properties ? {state: 'done', text: plural(s.properties, 'property', 'properties') +
          (li && li.filename ? ' · last file ' + li.filename : '')} : {state: 'next', text: 'No properties yet'};
      }
      case 'drive': {
        if (L && !complete) return {state: 'next', text: plural(left, 'property', 'properties') + ' to do'};
        if (!c.google) return {state: 'done', text: 'Free estimates for now. Real drive times come with the Google key.', warn: true};
        const more = estToDo('drive');
        if (more) return {state: 'next', text: plural(more, 'property', 'properties') + ' can get real Google drive times'};
        return {state: 'done', text: ''};
      }
      case 'photos': {
        if (!c.google) return {state: 'blocked', text: 'Needs the Google key'};
        const more = estToDo('photos');
        const todo = more === null ? left : more;
        if (todo) return {state: 'next', text: plural(todo, 'property', 'properties') + ' to photograph'};
        return {state: 'done', text: L && L.total ? '' : 'Nothing to photograph yet'};
      }
      case 'classify': {
        const P = layerNums('photos', c);
        if (!P || !P.done) return {state: 'wait', text: 'Waiting for photos'};
        if (c.backend === 'claude_cli' && !c.claudeCli) return {state: 'blocked', text: 'Claude Code isn\'t set up on the Mac yet'};
        return complete ? {state: 'done', text: ''} : {state: 'next', text: plural(left, 'property', 'properties') + ' to check'};
      }
      case 'review': {
        const Q = layerNums('classify', c);
        const need = toNum(s.needs_review) || 0;
        if (need) return {state: 'next', text: plural(need, 'property', 'properties') + ' need your answer'};
        if (!Q || !Q.done) return {state: 'wait', text: 'Waiting for the photo check'};
        return {state: 'done', text: 'Nothing waiting for you'};
      }
      case 'calibrate': {
        const answered = toNum((s.review_counts || {}).reviewed) || 0;
        if (!answered) return {state: 'wait', text: 'Waiting for your photo answers'};
        return {state: 'optional', text: plural(answered, 'answer') + ' to learn from'};
      }
      case 'parcels': {
        if (!L || L.enabled === false) return {state: 'off', text: 'No Ohio properties with a map location yet'};
        if (complete) return {state: 'done', text: ''};
        return {state: 'next', text: plural(left, 'property', 'properties') + ' to look up (free)'};
      }
      case 'research': {
        const line = S.est && (S.est.steps || []).find((l) => l.step === 'research');
        if (line && line.enabled === false) return {state: 'off', text: line.message || 'Switched off'};
        if (!c.claudeCli && !(line && line.backend === 'mock')) return {state: 'blocked', text: 'Claude Code isn\'t set up on the Mac yet'};
        const owners = estToDo('research');
        if (owners === null) return {state: 'optional', text: ''};
        if (!owners) return {state: 'done', text: 'Every owner in the buy box has been researched'};
        return {state: 'next', text: plural(owners, 'owner') + ' not researched yet'};
      }
      case 'credits': {
        const cr = s.credits || {};
        const n = toNum(cr.left);
        if (n === null) return {state: 'optional', text: ''};
        if (!n) return {state: 'done', text: 'All used or waiting at Crexi this period'};
        return {state: 'next', text: num(n) + ' free this period' + (toNum(cr.in_flight) ? ' · ' + num(cr.in_flight) + ' waiting at Crexi' : '')};
      }
      default:
        if (!L) return {state: 'optional', text: ''};
        return complete ? {state: 'done', text: ''} : {state: 'next', text: plural(left, 'property', 'properties') + ' to do'};
    }
  }

  const MARK = {done: '✓', blocked: '!', off: '–'};
  const STATE_WORD = {done: 'Done', next: 'To do', wait: 'Waiting', blocked: 'Blocked', optional: 'Optional', off: 'Off'};

  /** The first few things to do, in order: set-up blockers and steps that are ready. */
  function nextItems(c) {
    const out = [];
    LIST.forEach((step) => {
      const st = stepState(step, c);
      if (st.state === 'blocked' && step.key === 'photos') {
        out.push({key: 'google', html: '<b>Add your Google key.</b> It turns on photos, the photo check and real drive times ' +
          '(some set-aside properties may come back). Google gives free calls each month.', go: 'settings', label: 'How to add it'});
      } else if (st.state === 'blocked') {
        out.push({key: step.key, html: '<b>' + esc(step.title) + ':</b> ' + esc(st.text) + '.', jump: step.key, label: 'Show'});
      } else if (st.state === 'next') {
        const how = step.go ? '' : step.limit ? ' Try <b>Test on ' + TEST_N + '</b> first.' : '';
        out.push({key: step.key, html: '<b>' + esc(step.title) + ':</b> ' + esc(st.text) + '.' + how,
          go: step.go, jump: step.go ? null : step.key, label: step.go ? step.goLabel : 'Go to step'});
      }
    });
    const p = S.ph;
    if (p && p.enabled && (c.google ? (p.photos_left || {}).total || p.ai_left : p.ai_left)) {
      out.unshift({key: 'photos', html: '<b>Photos and the AI\'s ' + esc(String(p.target_name || 'photo').toLowerCase()) + ' call:</b> ' +
        num(p.done) + ' of ' + num(p.total) + ' done. One button below does the rest.', jump: 'photos', label: 'Show'});
    }
    return out.slice(0, 3);
  }

  function nextHtml(c) {
    const items = nextItems(c);
    if (!items.length) {
      return '<section class="panel ru-next done"><h2>All caught up</h2><p class="note">Every step is done. Import a new list, or keep working the DRILL.</p></section>';
    }
    return '<section class="panel ru-next" aria-label="Do this next"><h2>Do this next</h2><ol>' + items.map((it) =>
      '<li><span>' + it.html + '</span>' +
      (it.go ? '<button type="button" class="btn small" data-ru="go" data-go="' + esc(it.go) + '">' + esc(it.label) + '</button>'
        : '<button type="button" class="btn small quiet" data-ru="jump" data-step="' + esc(it.jump) + '">' + esc(it.label) + '</button>') +
      '</li>').join('') + '</ol></section>';
  }

  function paintNext() {
    const box = S.root && S.root.isConnected ? S.root.querySelector('[data-ru-next]') : null;
    if (box && S.ctx) box.innerHTML = nextHtml(S.ctx);
  }

  /* ---------- estimates ---------- */

  function money(usd) {
    const v = toNum(usd) || 0;
    return '$' + v.toFixed(2);
  }

  const SKU_LABELS = {
    route_matrix_element: 'Google Routes (drive times)', geocoding: 'Google Geocoding (map locations)',
    street_view_static: 'Street View photos', static_maps: 'Satellite photos', street_view_metadata: 'Street View lookups (free)',
  };

  /** The Google calls inside an estimate: the pipeline's list, or a single step's cost blocks. */
  function estimateSkus(e) {
    if (!e) return [];
    if (Array.isArray(e.google)) return e.google;
    const c = e.cost || {};
    const g = (e.geocode || {}).cost;
    return [c, c.street_view, c.satellite, g].filter((x) => x && x.sku && toNum(x.units));
  }

  function skuLine(sku) {
    return '<li><b>' + esc(sku.label || SKU_LABELS[sku.sku] || sku.sku) + ':</b> ' + plural(sku.units, 'call') +
      ' · ' + num(sku.free_left) + ' free calls left this month · ' + num(sku.billable) + ' billable · about ' + money(sku.usd) + '</li>';
  }

  function estimateLine(line, c) {
    if (!line) return '<p class="note">No estimate for this step.</p>';
    const skus = line.skus || [];
    const parts = ['<p class="vw-tight">' + esc(line.message || '') + '</p>'];
    if (skus.length) parts.push('<ul class="ru-skus">' + skus.map(skuLine).join('') + '</ul>');
    if (line.paid) parts.push('<p class="vw-tight"><b>About ' + money(line.usd) + '</b> for this step after Google\'s free monthly calls.</p>');
    if (line.needs_confirm) parts.push('<p class="vw-tight ru-ask">More than ' + num(c.confirmOver) + ' properties: it will ask for your OK before spending.</p>');
    return parts.join('');
  }

  function estimateAll(est) {
    const rows = (est.steps || []).map((l) => '<tr><td>' + esc(l.label || l.step) + '</td><td class="c num">' + num(l.to_do) + '</td>' +
      '<td class="c num">' + (l.paid ? money(l.usd) : '<span class="muted">Free</span>') + '</td></tr>').join('');
    return '<p class="vw-tight">' + esc(est.message || '') + '</p>' +
      '<div class="vw-scroll"><table class="wt ru-esttable"><thead><tr><th>Step</th><th class="c">To do</th><th class="c">Cost</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>';
  }

  function estimateHtml(step, c) {
    if (!S.estShown.has(step.key)) return '';
    let inner;
    if (S.estBusy && !S.est) inner = H.loading('Counting calls and costs (nothing is called)…');
    else if (S.estError) inner = '<p class="note">' + esc(S.estError.message) + '</p>';
    else if (!S.est) inner = H.loading('Counting calls and costs (nothing is called)…');
    else if (step.key === 'all') inner = estimateAll(S.est);
    else inner = estimateLine((S.est.steps || []).find((l) => l.step === step.key), c);
    return '<div class="ru-est"><div class="ru-boxhead"><b>Estimate</b><span class="note">dry run, nothing called</span>' +
      '<button type="button" class="x" data-ru="hide-est" data-step="' + esc(step.key) + '" aria-label="Hide the estimate">✕</button></div>' + inner + '</div>';
  }

  async function loadEstimate(force) {
    if (S.estBusy) {             // one at a time; a forced refresh asked for meanwhile runs right after
      if (force) S.estAgain = true;
      return;
    }
    if (!force && S.est && Date.now() - S.estAt < EST_FRESH_MS) return;
    const market = S.market;
    S.estBusy = true;
    S.estError = null;
    paintAll();
    try {
      const est = await BM.api(H.mpath('/estimate'));
      if (market !== S.market) return;
      S.est = est || {};
      S.estAt = Date.now();
    } catch (err) {
      if (market !== S.market) return;
      S.estError = err;
    } finally {
      if (market === S.market) {
        S.estBusy = false;
        paintAll();
        if (S.estAgain) {
          S.estAgain = false;
          loadEstimate(true);
        }
      }
    }
  }

  /* ---------- jobs ---------- */

  function isActive(job) {
    return !!job && !job.done && ACTIVE.includes(job.status);
  }

  function costUsd(result) {
    const r = result || {};
    const e = r.estimate || {};
    const candidates = [r.counts && r.counts.cost_usd, e.cost && e.cost.usd, e.est_cost_usd, e.total_usd];
    const hit = candidates.map(toNum).find((v) => v !== null);
    return hit === undefined ? null : hit;
  }

  function warningsHtml(list) {
    const items = (list || []).filter(Boolean);
    return items.length ? '<ul class="ru-warn">' + items.map((w) => '<li>' + esc(w) + '</li>').join('') + '</ul>' : '';
  }

  function stepsHtml(steps) {
    const list = steps || [];
    if (!list.length) return '';
    return '<details class="im-more"><summary>What each step did</summary><ul class="timeline ru-steps">' + list.map((s) =>
      '<li class="' + (s.needs_confirm ? 'next' : s.ok ? 'hit' : 'miss') + '"><div><span class="src">' + esc(s.label || s.step) +
      '</span> <span class="res">· ' + esc(s.message || '') + '</span></div></li>').join('') + '</ul></details>';
  }

  function activeJobHtml(job) {
    const queued = job.status === 'queued';
    return '<div class="ru-job ru-live" data-job-id="' + esc(job.id) + '">' +
      progressBar(queued ? 0 : job.progress, 'Job progress') +
      '<div class="ru-jobline"><span class="spin" aria-hidden="true"></span><span class="note" data-ru-msg>' +
      esc(job.message || (queued ? 'Waiting for the job ahead of it' : 'Working…')) + '</span>' +
      '<span class="num note" data-ru-pct>' + (queued ? '' : Math.round((toNum(job.progress) || 0) * 100) + '%') + '</span>' +
      (job.id === null || job.id === undefined ? '' :
        '<button type="button" class="btn small" data-ru="cancel" data-id="' + esc(job.id) + '">Cancel</button>') + '</div></div>';
  }

  function confirmHtml(step, job) {
    const r = job.result || {};
    const usd = costUsd(r);
    const limit = toNum(job.params && job.params.limit);
    // The button below is the OK, so drop the command-line hint the backend adds for the Terminal.
    const message = String(r.message || '').replace(/^Waiting for your OK:\s*/i, '')
      .replace(/\s*Run again with confirm[^.]*\.\s*/i, ' ').trim();
    return '<div class="ru-job ru-confirm" role="alert"><div class="ru-boxhead"><b>Waiting for your OK</b>' +
      '<span class="note">nothing was charged</span></div>' +
      '<p class="vw-tight">' + esc(message || 'This run is big enough that it needs your OK first.') + '</p>' +
      warningsHtml(r.warnings) +
      (estimateSkus(r.estimate).length ? '<ul class="ru-skus">' + estimateSkus(r.estimate).map(skuLine).join('') + '</ul>' : '') +
      (usd !== null ? '<p class="vw-tight"><b>Estimated cost: ' + money(usd) + '</b> after Google\'s free monthly calls.</p>' : '') +
      '<div class="bar vw-actions"><button type="button" class="btn primary" data-ru="confirm" data-step="' + esc(step.key) + '"' +
      (limit ? ' data-limit="' + limit + '"' : '') + '>' +
      (step.photo ? 'Yes, start it' + (usd !== null ? ' (about ' + money(usd) + ')' : '') : 'Yes, spend it') + '</button>' +
      '<button type="button" class="btn" data-ru="dismiss" data-step="' + esc(step.key) + '">Not now</button></div></div>';
  }

  function finishedHtml(step, job) {
    const r = job.result || {};
    const ok = job.status === 'done' && r.ok !== false;
    const cls = ok ? 'good' : (job.status === 'cancelled' ? '' : 'bad');
    const title = ok ? 'Done' : job.status === 'cancelled' ? 'Cancelled' : 'Stopped';
    const when = job.finished_at ? ' · ' + esc(BM.fmt.ago ? BM.fmt.ago(job.finished_at) : BM.fmt.date(job.finished_at)) : '';
    const test = toNum(job.params && job.params.limit) ? ' (test on ' + num(job.params.limit) + ')' : '';
    return '<div class="ru-job ru-result ' + cls + '"><div class="ru-boxhead"><b>' + title + esc(test) + '</b><span class="note">' + when + '</span>' +
      '<button type="button" class="x" data-ru="dismiss" data-step="' + esc(step.key) + '" aria-label="Hide this result">✕</button></div>' +
      '<p class="vw-tight">' + esc(r.message || job.message || '') + '</p>' + warningsHtml(r.warnings) + stepsHtml(r.steps) + '</div>';
  }

  function jobHtml(step) {
    const job = S.jobs[step.key];
    if (!job) return '';
    if (job.startError) {
      return '<div class="ru-job ru-result bad"><div class="ru-boxhead"><b>Didn\'t start</b>' +
        '<button type="button" class="x" data-ru="dismiss" data-step="' + esc(step.key) + '" aria-label="Hide">✕</button></div>' +
        '<p class="vw-tight">' + esc(job.startError) + '</p></div>';
    }
    if (isActive(job)) return activeJobHtml(job);
    if (job.result && job.result.needs_confirm) return confirmHtml(step, job);
    return finishedHtml(step, job);
  }

  /* ---------- cards ---------- */

  function buttonsHtml(step, c) {
    if (step.go) {
      return isOff(step, c) ? '' : '<div class="bar ru-actions"><button type="button" class="btn" data-ru="go" data-go="' +
        esc(step.go) + '">' + esc(step.goLabel || 'Open') + '</button></div>';
    }
    const busy = isActive(S.jobs[step.key]);
    const off = isOff(step, c) || busy ? ' disabled' : '';
    return '<div class="bar ru-actions">' +
      '<button type="button" class="btn primary" data-ru="run" data-step="' + esc(step.key) + '"' + off + '>' +
      (step.key === 'all' ? 'Run everything' : 'Run') + '</button>' +
      (step.limit ? '<button type="button" class="btn" data-ru="test" data-step="' + esc(step.key) + '"' + off +
        ' title="Run it on ' + TEST_N + ' properties first">Test on ' + TEST_N + '</button>' : '') +
      (step.noEstimate ? '' : '<button type="button" class="btn quiet" data-ru="estimate" data-step="' + esc(step.key) + '"' +
        (isOff(step, c) ? ' disabled' : '') + '>Estimate</button>') + '</div>';
  }

  function statusHtml(step, c) {
    if (step.wide) return '';
    const st = stepState(step, c);
    return '<p class="ru-status ' + st.state + (st.warn ? ' warn' : '') + '"><b>' + STATE_WORD[st.state] + '</b>' +
      (st.text ? ' · ' + esc(st.text) : '') + '</p>';
  }

  function cardInner(step, c) {
    const off = isOff(step, c);
    const st = step.wide ? null : stepState(step, c);
    const note = off ? '' : stepNote(step, c);
    const showWork = !off && !(st && (st.state === 'wait' || (st.state === 'blocked' && step.key === 'photos')));
    return '<div class="ru-top"><h2>' + esc(step.title) + '</h2>' + (step.go ? '<span class="pill">You</span>' : costChip(step, c)) + '</div>' +
      statusHtml(step, c) +
      (step.wide ? '<p class="note vw-tight">' + step.what(c) + '</p>'
        : '<details class="im-more ru-help"><summary>What this step does</summary><p class="note vw-tight">' + step.what(c) + '</p></details>') +
      (off || step.go ? '' : layerHtml(step, c)) +
      (note ? '<p class="note vw-tight ru-note">' + esc(note) + '</p>' : '') +
      (showWork || isActive(S.jobs[step.key]) ? buttonsHtml(step, c) : '') +
      estimateHtml(step, c) + '<div data-ru-jobbox>' + jobHtml(step) + '</div>';
  }

  function card(step, c) {
    if (step.wide) {
      return '<article class="panel ru-card ru-wide" data-step="' + esc(step.key) + '">' + cardInner(step, c) + '</article>';
    }
    return '<li class="ru-card ru-row is-' + stepState(step, c).state + '" data-step="' + esc(step.key) + '">' +
      rowInner(step, c) + '</li>';
  }

  function rowInner(step, c) {
    const n = LIST.indexOf(step) + 1;
    return '<span class="ru-mark" aria-hidden="true">' + (MARK[stepState(step, c).state] || n) + '</span>' +
      '<div class="ru-body"><span class="sr">Step ' + n + '. </span>' + cardInner(step, c) + '</div>';
  }

  function stepByKey(key) {
    return STEPS.find((s) => s.key === key) || (isPhotoKey(key) ? PHOTO_STEP : null);
  }

  function cardEl(key) {
    return S.root ? S.root.querySelector('.ru-card[data-step="' + key + '"]') : null;
  }

  function paintCard(key) {
    if (isPhotoKey(key)) return paintPhotos();
    const el = cardEl(key);
    const step = stepByKey(key);
    if (!el || !step || !S.ctx) return;
    if (step.wide) {
      el.innerHTML = cardInner(step, S.ctx);
      return;
    }
    el.className = 'ru-card ru-row is-' + stepState(step, S.ctx).state;
    el.innerHTML = rowInner(step, S.ctx);
  }

  function paintAll() {
    if (!S.root || !S.root.isConnected) return;
    STEPS.forEach((s) => paintCard(s.key));
    paintPhotos();
    markFirstNext();
    paintNext();
  }

  /** Highlight the first step that is ready to do. */
  function markFirstNext() {
    if (!S.root || !S.root.querySelectorAll) return;
    const rows = Array.from(S.root.querySelectorAll('.ru-row') || []);
    const first = rows.find((el) => /\bis-(next|blocked)\b/.test(el.className || ''));
    rows.forEach((el) => {
      const cls = String(el.className || '').replace(/\s*\bfirst\b/g, '');
      el.className = el === first ? cls + ' first' : cls;
    });
  }

  /** While a job runs, move its bar and message in place (keeps keyboard focus on Cancel). */
  function paintJob(key) {
    const el = isPhotoKey(key) ? (S.root && S.root.querySelector('[data-ru-photos]')) : cardEl(key);
    const job = S.jobs[key];
    if (!el) return;
    const live = el.querySelector('.ru-live');
    if (live && isActive(job) && live.getAttribute('data-job-id') === String(job.id)) {
      const queued = job.status === 'queued';
      const w = queued ? 0 : Math.max(0, Math.min(100, (toNum(job.progress) || 0) * 100));
      live.querySelector('.ru-bar i').style.width = w.toFixed(1) + '%';
      live.querySelector('.ru-bar').setAttribute('aria-valuenow', String(Math.round(w)));
      live.querySelector('[data-ru-msg]').textContent = job.message || (queued ? 'Waiting for the job ahead of it' : 'Working…');
      live.querySelector('[data-ru-pct]').textContent = queued ? '' : Math.round(w) + '%';
      return;
    }
    paintCard(key);
  }

  /* ---------- recent jobs ---------- */

  const STATUS = {
    done: ['pill good', 'Done'], error: ['pill bad', 'Stopped'], cancelled: ['pill', 'Cancelled'],
    running: ['pill hot', 'Running'], queued: ['pill', 'Waiting'],
  };

  function recentHtml() {
    if (S.recent === null) return H.loading('Loading recent jobs…');
    if (S.recent instanceof Error) return '<p class="note">' + esc(S.recent.message) + '</p>';
    if (!S.recent.length) return '<p class="note">No jobs in this market yet.</p>';
    return '<ul class="ru-recent">' + S.recent.map((j) => {
      const st = STATUS[j.status] || ['pill', j.status];
      const p = j.params || {};
      const extras = [toNum(p.limit) ? 'test on ' + num(p.limit) : '', p.confirm ? 'OK given' : '', p.dry_run ? 'dry run' : '']
        .filter(Boolean).join(' · ');
      return '<li><div class="ru-rhead"><b>' + esc(j.label || j.kind) + '</b><span class="' + st[0] + '">' + esc(st[1]) + '</span>' +
        '<span class="note">' + esc(BM.fmt.ago ? BM.fmt.ago(j.created_at) : BM.fmt.date(j.created_at)) + (extras ? ' · ' + esc(extras) : '') + '</span></div>' +
        '<p class="note ru-rmsg" title="' + esc(j.message || '') + '">' + esc(j.message || '') + '</p></li>';
    }).join('') + '</ul>';
  }

  function paintRecent() {
    const box = S.root && S.root.isConnected ? S.root.querySelector('[data-ru-recent]') : null;
    if (box) box.innerHTML = recentHtml();
  }

  async function loadRecent() {
    const market = S.market;
    let list;
    try {
      list = await BM.api('/api/jobs?market=' + encodeURIComponent(market) + '&limit=' + RECENT_N);
    } catch (err) {
      list = err;
    }
    if (market !== S.market) return;
    S.recent = Array.isArray(list) || list instanceof Error ? list : [];
    paintRecent();
    if (Array.isArray(list)) adoptActive(list);
  }

  /** Jobs still running from an earlier visit (or another tab) show on their step's card. */
  function adoptActive(list) {
    let found = false;
    list.slice().reverse().forEach((j) => {
      if (!isActive(j) || !stepByKey(j.kind)) return;       // (the photo button's job is a step too)
      const mine = S.jobs[j.kind];
      if (!mine || mine.id !== j.id) {
        S.jobs[j.kind] = j;
        paintCard(j.kind);
      }
      found = true;
    });
    if (found) startPolling();
  }

  /* ---------- polling ---------- */

  function startPolling() {
    if (!S.poll) S.poll = setInterval(pollOnce, POLL_MS);
  }

  function stopPolling() {
    clearInterval(S.poll);
    S.poll = null;
  }

  async function pollOnce() {
    if (!S.root || !S.root.isConnected) return stopPolling();
    const active = Object.entries(S.jobs).filter(([, j]) => isActive(j) && j.id !== null && j.id !== undefined);
    if (!Object.values(S.jobs).some(isActive)) return stopPolling();
    if (S.polling) return undefined;
    S.polling = true;
    const market = S.market;
    let finished = false;
    try {
      for (const [key, job] of active) {
        let fresh;
        try {
          fresh = await BM.api('/api/jobs/' + encodeURIComponent(job.id));
        } catch (err) {
          continue;     // a missed poll is fine; the next one tries again
        }
        if (market !== S.market || !fresh) return undefined;
        S.jobs[key] = fresh;
        if (isActive(fresh) && isPhotoKey(key) && Date.now() - S.phAt > PHOTO_COUNT_MS) {
          S.phAt = Date.now();
          loadPhotos();          // keep "312 of 2,981 done" moving while the photo job runs
        }
        if (isActive(fresh)) paintJob(key);
        else {
          finished = true;
          onFinished(key, fresh);
        }
      }
    } finally {
      S.polling = false;
    }
    if (finished) await afterFinish();
    return undefined;
  }

  function onFinished(key, job) {
    const r = job.result || {};
    if (r.needs_confirm) BM.toast(job.label + ': waiting for your OK.');
    else if (job.status === 'done') BM.toast(job.label + ': done.');
    else if (job.status === 'cancelled') BM.toast(job.label + ': cancelled.');
    else BM.toast(job.label + ': stopped. ' + (job.message || ''), {error: true});
    paintCard(key);
  }

  async function afterFinish() {
    S.est = null;              // counts changed; the next estimate is fresh
    S.estAt = 0;
    try {
      const summary = await BM.refreshSummary();
      if (S.ctx && summary) S.ctx.summary = summary;
    } catch (e) { /* keep the old layer numbers */ }
    paintAll();
    loadRecent();
    loadPhotos();
    if (S.estShown.size) loadEstimate(true);   // keep any open estimate boxes current
  }

  /* ---------- actions ---------- */

  async function start(stepKey, opts) {
    const step = stepByKey(stepKey);
    if (!step) return;
    const body = {step: stepKey};
    if (opts.limit) body.limit = opts.limit;
    if (opts.confirm) body.confirm = true;
    S.jobs[stepKey] = {id: null, status: 'queued', done: false, message: 'Starting…', progress: 0, params: body};
    paintCard(stepKey);
    try {
      const res = await BM.api(H.mpath('/jobs'), {method: 'POST', body});
      const job = (res && res.job) || {id: res && res.job_id, status: 'queued', done: false, progress: 0};
      if (job.id === undefined || job.id === null) job.id = res && res.job_id;
      S.jobs[stepKey] = job;
      BM.toast('Started: ' + step.title + (opts.limit ? ' (test on ' + opts.limit + ')' : '') + '.');
      startPolling();
      loadRecent();
    } catch (err) {
      S.jobs[stepKey] = {startError: err.message, done: true};
    }
    paintCard(stepKey);
  }

  async function cancel(id) {
    try {
      const job = await BM.api('/api/jobs/' + encodeURIComponent(id) + '/cancel', {method: 'POST'});
      const key = Object.keys(S.jobs).find((k) => S.jobs[k] && String(S.jobs[k].id) === String(id));
      if (key && job) {
        S.jobs[key] = job;
        paintJob(key);
      }
      BM.toast('Stopping at the next safe point. Work already saved is kept.');
    } catch (err) {
      BM.toast(err.message, {error: true});
    }
  }

  function onClick(ev) {
    const b = ev.target instanceof Element ? ev.target.closest('[data-ru]') : null;
    if (!b || b.disabled) return;
    const act = b.getAttribute('data-ru');
    const key = b.getAttribute('data-step');
    if (act === 'ph-ask') askPhotos();
    else if (act === 'ph-go') {
      S.phEst = null;
      start(PHOTO_STEP.key, {confirm: true});
    } else if (act === 'ph-test') {
      S.phEst = null;
      start(PHOTO_STEP.key, {limit: TEST_N});
    } else if (act === 'ph-close') {
      S.phEst = null;
      paintPhotos();
    } else if (act === 'run') start(key, {});
    else if (act === 'test') start(key, {limit: TEST_N});
    else if (act === 'confirm') start(key, {confirm: true, limit: toNum(b.getAttribute('data-limit'))});
    else if (act === 'cancel') cancel(b.getAttribute('data-id'));
    else if (act === 'go') BM.go(b.getAttribute('data-go'));
    else if (act === 'jump') {
      const row = key === 'photos' ? (S.root && S.root.querySelector('[data-ru-photos]')) : cardEl(key);
      if (row && row.scrollIntoView) row.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
    else if (act === 'dismiss') {
      delete S.jobs[key];
      paintCard(key);
    } else if (act === 'estimate') {
      S.estShown.add(key);
      paintCard(key);
      loadEstimate(false);
    } else if (act === 'estimate-all') {
      // One table on the "Run everything" card (every step, its calls and cost) instead of a box on every card.
      S.estShown.add('all');
      loadEstimate(true);
      const allCard = S.root && S.root.querySelector('.ru-card[data-step="all"]');
      if (allCard && allCard.scrollIntoView) allCard.scrollIntoView({behavior: 'smooth', block: 'start'});
    } else if (act === 'hide-est') {
      S.estShown.delete(key);
      paintCard(key);
    }
  }

  /* ---------- the one photo button ---------- */

  function photoBusy() {
    return isActive(S.jobs[PHOTO_STEP.key]);
  }

  function photoEstHtml() {
    const e = S.phEst;
    if (!e) return '';
    let inner;
    let buttons = '<button type="button" class="btn" data-ru="ph-close">Close</button>';
    if (e === 'loading') inner = H.loading('Counting properties, photos and cost (nothing is called)…');
    else if (e instanceof Error) inner = '<p class="note">' + esc(e.message) + '</p>';
    else {
      const usd = toNum((e.cost || {}).usd) || 0;
      inner = '<ul class="ru-phlines">' + (e.lines || []).map((l) => '<li>' + esc(l) + '</li>').join('') + '</ul>' +
        (e.nothing_to_do ? '' : '<p class="vw-tight"><b>About ' + money(usd) + ' in all</b>' +
          (usd === 0 ? ' (Google\'s free monthly allowance covers it)' : '') + '.</p>');
      const canStart = !e.nothing_to_do && (toNum(e.photos) || e.ready !== false);
      if (canStart) {
        buttons = '<button type="button" class="btn primary" data-ru="ph-go">Yes, start it' +
          (usd ? ' (about ' + money(usd) + ')' : '') + '</button>' +
          '<button type="button" class="btn" data-ru="ph-close">Not now</button>';
      }
    }
    return '<div class="ru-est ru-phest" role="region" aria-label="What it will do"><div class="ru-boxhead"><b>Before it starts</b>' +
      '<span class="note">estimate, nothing called yet</span></div>' + inner +
      '<div class="bar vw-actions">' + buttons + '</div></div>';
  }

  function photoBody(c) {
    const p = S.ph;
    if (p === null) return H.loading('Counting photos and AI calls…');
    if (p instanceof Error) return '<p class="note">' + esc(p.message) + '</p>';
    if (!p.enabled) return '<p class="note">' + esc(p.message || 'No photo check for this market.') + '</p>';
    const off = photoBusy() || S.phEst === 'loading' ? ' disabled' : '';
    const left = p.photos_left || {};
    const waiting = [];
    if (left.total) {
      waiting.push(plural(left.total, 'lead') + ' with no photos yet' +
        (left.buybox ? ' (' + num(left.buybox) + ' in the buy box, done first)' : ''));
    }
    if (p.ai_left) waiting.push(plural(p.ai_left, 'lead') + ' with photos waiting for the AI');
    const target = String(p.target_name || 'photo').toLowerCase();
    const allDone = p.total && !p.left;
    return '<div class="ru-phhead"><p class="ru-phnum"><b>' + num(p.done) + '</b> of <b>' + num(p.total) + '</b> done</p>' +
      '<span class="pill ' + (allDone ? 'good' : '') + '">' + (allDone ? 'All done' : num(p.left) + ' to go') + '</span></div>' +
      progressBar(p.total ? p.done / p.total : 0, 'Leads with the AI\'s call', true) +
      '<p class="note vw-tight">Every lead gets Street View and satellite photos, then the AI says whether it is a ' + esc(target) +
      '. You check its calls in Photo review.' + (waiting.length ? ' Waiting: ' + esc(waiting.join(', ')) + '.' : '') +
      (p.no_location ? ' ' + esc(plural(p.no_location, 'lead has', 'leads have')) + ' no map location yet.' : '') + '</p>' +
      (c.google ? '' : '<p class="note vw-tight ru-note">Needs the Google key for new photos (Settings has the steps). The AI can still look at photos you already have.</p>') +
      '<div class="bar ru-actions">' +
      '<button type="button" class="btn primary ru-phbtn" data-ru="ph-ask"' + off + '>' +
      esc(p.button || 'Get photos and the AI\'s call for every lead') + '</button>' +
      '<button type="button" class="btn" data-ru="ph-test"' + off + ' title="Photos and the AI\'s call for ' + TEST_N +
      ' properties">Test on ' + TEST_N + '</button></div>' +
      photoEstHtml() +
      '<p class="note vw-tight">It shows the cost first and starts on your OK. Stopped, cancelled or hit a limit? Press it again: ' +
      'it carries on where it left off, and photos and answers you already have are never paid for twice.</p>';
  }

  function photosHtml(c) {
    if (c.ac && c.ac.visual_enabled === false) return '';
    return '<section class="panel ru-photos" data-ru-photos aria-label="Photos and the AI\'s call for every lead">' +
      '<div class="ru-top"><h2>Photos and the AI\'s call for every lead</h2>' +
      '<span class="pill warn">Google photos + your Claude plan</span></div>' +
      '<div data-ru-photobody>' + photoBody(c) + '</div><div data-ru-photojob>' + jobHtml(PHOTO_STEP) + '</div></section>';
  }

  function paintPhotos() {
    const box = S.root && S.root.isConnected ? S.root.querySelector('[data-ru-photos]') : null;
    if (!box || !S.ctx) return;
    const body = S.root.querySelector('[data-ru-photobody]');
    const jobBox = S.root.querySelector('[data-ru-photojob]');
    if (body) body.innerHTML = photoBody(S.ctx);
    const live = jobBox && jobBox.querySelector ? jobBox.querySelector('.ru-live') : null;
    const job = S.jobs[PHOTO_STEP.key];
    if (live && isActive(job) && live.getAttribute('data-job-id') === String(job.id)) return;   // paintJob moves its bar
    if (jobBox) jobBox.innerHTML = jobHtml(PHOTO_STEP);
  }

  async function loadPhotos() {
    const market = S.market;
    try {
      const p = await BM.api(H.mpath('/photos/all'));
      if (market !== S.market) return;
      S.ph = p || {enabled: false};
    } catch (err) {
      if (market !== S.market) return;
      S.ph = err;
    }
    paintPhotos();
    paintNext();
  }

  /** The big button: show what it would do and cost (a dry run); then one "Yes, start it" starts the job. */
  async function askPhotos() {
    const market = S.market;
    S.phEst = 'loading';
    paintPhotos();
    try {
      const p = await BM.api(H.mpath('/photos/all?estimate=1'));
      if (market !== S.market) return;
      S.phEst = (p && p.estimate) || new Error('No estimate came back. Try again.');
      if (p && p.enabled !== undefined) S.ph = p;
    } catch (err) {
      if (market !== S.market) return;
      S.phEst = err;
    }
    paintPhotos();
    const box = S.root && S.root.querySelector ? S.root.querySelector('.ru-phest') : null;
    if (box && box.scrollIntoView) box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }

  /* ---------- page ---------- */

  function heading(c) {
    const over = c ? num(c.confirmOver) : '50';
    return H.head('Setup · Run', 'Run the pipeline, top to bottom', [
      'Each step needs the ones above it. Free steps first; paid steps only on what survives the filters.',
      'Paid runs over ' + over + ' properties stop and wait for your OK. Try <b>Test on ' + TEST_N + '</b> first.',
      'Every answer is saved, so re-runs don\'t pay twice.',
    ]);
  }

  function page(c) {
    return heading(c) + '<div data-ru-next>' + nextHtml(c) + '</div>' + photosHtml(c) +
      '<ol class="ru-list">' + LIST.map((s) => card(s, c)).join('') + '</ol>' +
      '<div class="bar ru-toolbar"><button type="button" class="btn" data-ru="estimate-all">Estimate everything</button>' +
      '<span class="note">Counts calls and dollars without calling anything.</span></div>' +
      card(stepByKey('all'), c) +
      '<section class="vw-sec"><h2 class="vw-sub">Recent jobs</h2><div data-ru-recent>' + recentHtml() + '</div></section>';
  }

  async function render(root, params) {
    if (S.market !== BM.state.market) {
      resetForMarket();
      S.market = BM.state.market;
    }
    S.root = root;
    root.innerHTML = heading(null) + H.loading('Loading the pipeline…');
    let c;
    try {
      c = await loadContext();
    } catch (err) {
      if (root.isConnected) root.innerHTML = heading(null) + H.errorBox(err);
      return;
    }
    if (!root.isConnected || S.root !== root) return;
    S.ctx = c;
    if (H.isEmptyMarket(c.summary)) {
      root.innerHTML = heading(c) + H.emptyMarket();
      return;
    }
    root.innerHTML = page(c);
    markFirstNext();
    root.addEventListener('click', onClick);
    if (Object.values(S.jobs).some(isActive)) startPolling();
    loadEstimate(false);          // free dry run: tells which steps still have work (no calls are made)
    loadPhotos();                 // "312 of 2,981 done" for the photo button
    await loadRecent();
    if (params && params[0] === 'photos') {      // linked from Photo review: show the photo button
      const box = root.querySelector('[data-ru-photos]');
      if (box && box.scrollIntoView) box.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
  }

  BM.registerView('run', {label: 'Run', group: 'setup', order: 2, render});
})();
