/* Run (Setup): one card per pipeline step with what it does, whether it costs money and how far its layer
 * has got. "Estimate" shows calls, Google's free calls left and dollars without calling anything. "Run"
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
  const ACTIVE = ['queued', 'running'];

  /* The steps, in the order the pipeline runs them. `layers`: keys in summary.layers. */
  const STEPS = [
    {key: 'owners', title: 'Owners', layers: ['owners'], limit: false,
      what: () => 'Owner type (individual, trust, LLC, portfolio...), years owned, loan status and whether the owner lives far away.'},
    {key: 'drive', title: 'Drive times', layers: ['drive'], limit: true,
      what: (c) => 'Minutes from ' + esc(c.origin) + ' to each property' + (c.google ? ', from Google Routes.' :
        '. With no Google key yet, these are free straight-line estimates.')},
    {key: 'rent', title: 'Rent', layers: ['rent'], limit: true, needs: 'rent_enabled',
      what: () => 'HUD area rents (free HUD map service) compared with the rent scale.'},
    {key: 'photos', title: 'Photos', layers: ['photos'], limit: true, needs: 'visual_enabled',
      what: () => '3 Street View photos and 1 satellite photo of each buy-box property. The first paid step.'},
    {key: 'classify', title: 'Photo check', layers: ['photo_check', 'visual'], limit: true, needs: 'visual_enabled',
      what: (c) => 'Claude looks at the photos' + (c.question ? ': "' + esc(c.question) + '"' : '') + '. Unsure answers go to Photo review.'},
    {key: 'score', title: 'Score', layers: ['score'], limit: false,
      what: () => 'Buy box, colors, Blue Moon score and rank. Properties that fail a filter are set aside, never deleted.'},
    {key: 'drill', title: 'DRILL auto-stages', layers: ['drill'], limit: false,
      what: () => 'Records baseline and portfolio match, then every owner\'s contact level.'},
    {key: 'calibrate', title: 'Calibrate the photo check', layers: [], limit: false, needs: 'visual_enabled', noEstimate: true,
      what: () => 'Claude checks again every property you answered in Photo review, using your answers as examples, ' +
        'then updates the Scorecard (the 90% check).'},
    {key: 'all', title: 'Run everything', layers: [], limit: true, wide: true,
      what: () => 'Every step in order: owners, drive, rent, score, photos, photo check, score, DRILL. ' +
        'Free steps run first. Big paid steps stop and wait for your OK.'},
  ];

  const S = {
    root: null, market: null, ctx: null,
    est: null, estAt: 0, estBusy: false, estAgain: false, estError: null, estShown: new Set(),
    jobs: {},          // step key -> latest job this session (kept until dismissed)
    recent: null,      // recent jobs list, or an Error
    poll: null, polling: false,
  };

  function resetForMarket() {
    stopPolling();
    Object.assign(S, {est: null, estAt: 0, estBusy: false, estAgain: false, estError: null, estShown: new Set(), jobs: {},
      recent: null});
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
      case 'all': return chip('warn', 'Paid steps ask first');
      default: return chip('', 'Free');
    }
  }

  function stepNote(step, c) {
    if (step.key === 'photos' && !c.google) return 'Needs GOOGLE_MAPS_API_KEY in .env first (see Settings).';
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
      (limit ? ' data-limit="' + limit + '"' : '') + '>Yes, spend it</button>' +
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

  function cardInner(step, c) {
    const off = isOff(step, c);
    const note = off ? 'Not used for ' + esc(c.ac.label || 'this asset class') + '.' : stepNote(step, c);
    return '<div class="ru-top"><h2>' + esc(step.title) + '</h2>' + costChip(step, c) + '</div>' +
      '<p class="note vw-tight">' + step.what(c) + '</p>' +
      (off ? '' : layerHtml(step, c)) +
      (note ? '<p class="note vw-tight ru-note">' + esc(note) + '</p>' : '') +
      buttonsHtml(step, c) + estimateHtml(step, c) + '<div data-ru-jobbox>' + jobHtml(step) + '</div>';
  }

  function card(step, c) {
    return '<article class="panel ru-card' + (step.wide ? ' ru-wide' : '') + (isOff(step, c) ? ' ru-off' : '') +
      '" data-step="' + esc(step.key) + '">' + cardInner(step, c) + '</article>';
  }

  function stepByKey(key) {
    return STEPS.find((s) => s.key === key) || null;
  }

  function cardEl(key) {
    return S.root ? S.root.querySelector('.ru-card[data-step="' + key + '"]') : null;
  }

  function paintCard(key) {
    const el = cardEl(key);
    const step = stepByKey(key);
    if (el && step && S.ctx) el.innerHTML = cardInner(step, S.ctx);
  }

  function paintAll() {
    if (!S.root || !S.root.isConnected) return;
    STEPS.forEach((s) => paintCard(s.key));
  }

  /** While a job runs, move its bar and message in place (keeps keyboard focus on Cancel). */
  function paintJob(key) {
    const el = cardEl(key);
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
      if (!isActive(j) || !stepByKey(j.kind)) return;
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
    if (act === 'run') start(key, {});
    else if (act === 'test') start(key, {limit: TEST_N});
    else if (act === 'confirm') start(key, {confirm: true, limit: toNum(b.getAttribute('data-limit'))});
    else if (act === 'cancel') cancel(b.getAttribute('data-id'));
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

  /* ---------- page ---------- */

  function heading(c) {
    const over = c ? num(c.confirmOver) : '50';
    return H.head('Setup · Run', 'Run the pipeline', [
      'Free steps first. Paid steps only on what survives the filters.',
      'Paid runs over ' + over + ' properties stop and wait for your OK.',
      'Try <b>Test on ' + TEST_N + '</b> before a full run. Every answer is saved, so re-runs don\'t pay twice.',
    ]);
  }

  function page(c) {
    return heading(c) +
      '<div class="bar ru-toolbar"><button type="button" class="btn" data-ru="estimate-all">Estimate everything</button>' +
      '<span class="note">Counts calls and dollars without calling anything.</span></div>' +
      '<div class="ru-grid">' + STEPS.map((s) => card(s, c)).join('') + '</div>' +
      '<section class="vw-sec"><h2 class="vw-sub">Recent jobs</h2><div data-ru-recent>' + recentHtml() + '</div></section>';
  }

  async function render(root) {
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
    root.addEventListener('click', onClick);
    if (Object.values(S.jobs).some(isActive)) startPolling();
    await loadRecent();
  }

  BM.registerView('run', {label: 'Run', group: 'setup', order: 2, render});
})();
