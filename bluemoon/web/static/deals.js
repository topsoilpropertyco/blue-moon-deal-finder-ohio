/* Deals: the ranked list (or drive-time rings) for the current market.
 * Data: GET /api/m/{slug}/summary (tiles, labels, the Why? panel's set_aside) and GET /api/m/{slug}/properties
 * (pages of PropertyRow).
 *
 * Buy-box decisions by hand: the Why? panel lists each reason with its groups (set_aside.reasons[].buckets),
 * each with Review (the list filtered to that group: #deals/review/<group key>, ?bucket=) and Decide
 * (POST /buybox/decide, undone with POST /buybox/undo). Set-aside rows get 'Add to buy box' and decided rows
 * 'Undo' (POST /properties/{id}/buybox).
 */
(function () {
  'use strict';
  if (!window.BM) return;
  const {esc, fmt, ui} = BM;

  const PAGE = 100;
  const RING_PAGE = 500;
  const RING_LIMIT = 2000;
  const LS_PREFS = 'bm.deals';
  const SORTS = [
    ['rank', 'Best first'], ['score', 'Blue Moon score'], ['drive', 'Drive time'], ['units', 'Size'],
    ['level', 'Contact level'], ['years', 'Years owned'], ['address', 'Address A–Z'],
  ];
  const OWNER_TYPES = {
    individual: 'Individual', trust: 'Trust', estate: 'Estate', small_llc: 'Small LLC', corporation: 'Corporation',
    portfolio: 'Portfolio', institutional: 'Institution', government: 'Government', nonprofit: 'Nonprofit', unknown: 'Unknown',
  };
  const SET_ASIDE = {
    drive_time: 'too far', year: 'built too early', size: 'wrong size', state: 'other state',
    skip_county: 'skipped county', property_type: 'other property type', manual: 'set aside by hand',
  };
  const ESTIMATED = new Set(['address_estimate', 'sqft_estimate', 'acres_estimate']);
  const ROW_KEYS = ['level', 'drill_status', 'channels', 'next_step_text', 'next_step_kind', 'best_guess_person',
    'score', 'coverage_have', 'coverage_total', 'rank', 'visual', 'rent_fit', 'in_buybox', 'exclusion_reasons',
    'rule_reasons', 'by_hand', 'aside_buckets'];
  const GROUP_KEY = /^[a-z_]+:[a-z0-9-]+$/;   // a set-aside group: 'drive_time:76-90' (scoring/buckets.py)

  const prefsByMarket = {};
  const originCache = {};
  let active = null;   // the Deals screen currently on the page
  let scrollTarget = null; // after Review / Back to all deals re-renders the screen: '[data-review]' or '[data-why-wrap]'

  BM.registerView('deals', {label: 'Deals', group: 'work', order: 1, render});

  /* ---------- preferences ---------- */

  function savedPrefs() {
    try { return JSON.parse(window.localStorage.getItem(LS_PREFS) || '{}') || {}; } catch (e) { return {}; }
  }

  function storePrefs(p) {
    try { window.localStorage.setItem(LS_PREFS, JSON.stringify({sort: p.sort, mode: p.mode})); } catch (e) { /* fine */ }
  }

  function prefsFor(slug) {
    if (!prefsByMarket[slug]) {
      const saved = savedPrefs();
      prefsByMarket[slug] = {
        view: 'all', q: '', bucket: null,
        sort: SORTS.some((s) => s[0] === saved.sort) ? saved.sort : 'rank',
        mode: saved.mode === 'rings' ? 'rings' : 'list',
      };
    }
    return prefsByMarket[slug];
  }

  /* ---------- render ---------- */

  async function render(root, params) {
    const slug = BM.state.market;
    let s = BM.state.summary;
    if (!s || !s.market || s.market.slug !== slug) {
      try { s = await BM.refreshSummary(); } catch (err) { root.innerHTML = ui.error(err); return; }
    }
    if (!root.isConnected || slug !== BM.state.market) return;
    const prefs = prefsFor(slug);
    const chips = chipDefs(s);
    // #deals/review/<group key> is the Review list of one set-aside group; #deals/<view> is a chip.
    const review = params && params[0] === 'review' && GROUP_KEY.test(params[1] || '') ? params[1] : null;
    prefs.bucket = review;
    if (!review && params && params[0] && knownView(s, chips, params[0])) prefs.view = params[0];
    if (!knownView(s, chips, prefs.view)) prefs.view = 'all';

    const ctx = {root, slug, s, prefs, chips, cols: columns(s), items: [], total: 0, counts: {}, token: 0,
      loading: false, observer: null, group: null, sheet: null, busy: false};
    if (active && active.observer) active.observer.disconnect();
    active = ctx;

    if (!s.properties) {
      root.innerHTML = headHtml(s) + ui.emptyMarket();
      return;
    }
    if (review) ctx.group = groupFromSummary(ctx, null);
    root.innerHTML = headHtml(s) + '<div class="strip" data-tiles>' + tilesHtml(s) + '</div>' +
      '<div class="why-wrap" data-why-wrap></div><div class="review-wrap" data-review></div>' + toolbarHtml(ctx) +
      '<div class="deals-status note" data-status aria-live="polite"></div><div data-body></div>';
    drawWhy(ctx);
    drawReview(ctx);
    bind(ctx);
    if (scrollTarget) {
      const target = root.querySelector(scrollTarget);
      scrollTarget = null;
      if (target && target.firstChild && target.scrollIntoView) target.scrollIntoView({block: 'start'});
    }
    await refresh(ctx);
  }

  function headHtml(s) {
    const ac = s.asset_class || {};
    const m = s.market || {};
    const eyebrow = [ac.label, m.geography].filter(Boolean).join(' · ');
    return '<div class="deals-head"><div class="eyebrow">' + esc(eyebrow) + '</div>' +
      '<h1>Where the rare deals are</h1>' +
      '<ul class="lines">' +
      '<li><b>Blue Moon score:</b> how worth chasing the owner is. Work from the top.</li>' +
      '<li><b>Contact levels:</b> move every owner in the buy box up to level 3. Click a level to see those properties.</li>' +
      '</ul>' + ui.levelLadder(s.levels_in_buybox || s.levels, {label: 'Contact levels in the buy box'}) + '</div>';
  }

  /* ---------- why only some are in the buy box ---------- */

  const LS_WHY = 'bm.deals.whyClosed';
  const LS_WHY_OPEN = 'bm.deals.whyOpen';     // the reasons whose groups are showing
  let whyClosed = readStore(LS_WHY) === '1';
  let whyOpen = null;                          // Set of reason keys; null = not decided yet (the first is open)

  function readStore(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }   // private mode: fine
  }

  function writeStore(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private mode: fine */ }
  }

  function openReasons(sa) {
    if (!whyOpen) {
      let saved = null;
      try { saved = JSON.parse(readStore(LS_WHY_OPEN) || 'null'); } catch (e) { saved = null; }
      whyOpen = new Set(Array.isArray(saved) ? saved : (sa.reasons[0] ? [sa.reasons[0].key] : []));
    }
    return whyOpen;
  }

  function setAsideHtml(s, ctx) {
    const sa = s.set_aside;
    if (!sa || !Array.isArray(sa.reasons) || !sa.reasons.length) return '';
    const hand = sa.by_hand || {};
    const max = Math.max(1, ...sa.reasons.map((r) => r.count));
    const opened = openReasons(sa);
    const rows = sa.reasons.map((r) => reasonHtml(s, ctx, r, max, opened.has(r.key))).join('');
    const notes = [];
    if (sa.several) notes.push(fmt.num(sa.several) + ' have more than one reason, so the counts add up to more than ' + fmt.num(sa.total) + '.');
    if (sa.drive_estimated) {
      let t = 'Drive times are straight-line estimates until the Google key is in.';
      if (sa.drive_close_calls) {
        t += ' ' + fmt.num(sa.drive_close_calls) + ' are set aside only for an estimated ' + fmt.num(sa.max_drive_minutes + 1) + '–' +
          fmt.num(sa.max_drive_minutes + sa.close_call_minutes) + ' min drive; some may come back once Google measures the real drive.';
      }
      notes.push(t);
    }
    notes.push('Missing data never sets a property aside: unknown units, year or drive time stay in.');
    notes.push('Review shows one group in the list below. Decide adds a whole group to the buy box or keeps it set aside. Every decision can be undone.');
    return '<details class="why panel"' + (whyClosed ? '' : ' open') + ' data-why>' +
      '<summary><b>' + fmt.num(s.in_buybox) + ' of ' + fmt.num(s.properties) + '</b> are in the buy box' +
      (hand.added ? ' (' + fmt.num(hand.added) + ' added by you)' : '') + '. ' +
      '<b>' + fmt.num(sa.total) + '</b> are set aside (never deleted)' +
      (hand.set_aside ? ', ' + fmt.num(hand.set_aside) + ' of them by you' : '') + '. <span class="why-toggle">Why?</span></summary>' +
      '<ul class="why-list">' + rows + '</ul>' +
      notes.map((n) => '<p class="note">' + esc(n) + '</p>').join('') +
      '<div class="why-acts"><button type="button" class="btn" data-why-view="excluded">See the ' + fmt.num(sa.total) + ' set aside</button>' +
      '<button type="button" class="btn quiet" data-why-view="everything">See all ' + fmt.num(s.properties) + '</button></div></details>';
  }

  /** '12 added by you · kept set aside' for a reason or a group. */
  function decidedText(x) {
    const parts = [];
    if (x.added) parts.push(fmt.num(x.added) + ' added by you');
    if (x.kept) parts.push(x.kept >= x.count ? 'kept set aside' : fmt.num(x.kept) + ' kept set aside');
    if (x.added && x.still_aside) parts.push(fmt.num(x.still_aside) + ' still set aside');
    return parts.join(' · ');
  }

  function reasonHtml(s, ctx, r, max, open) {
    const groups = Array.isArray(r.buckets) ? r.buckets : [];
    const decided = decidedText(r);
    const n = groups.length;
    return '<li class="why-r"><details data-why-reason="' + esc(r.key) + '"' + (open ? ' open' : '') + '>' +
      '<summary class="why-rs">' +
      '<span class="why-l">' + esc(r.label) + '</span>' +
      '<span class="why-bar" aria-hidden="true"><i style="width:' + Math.round((100 * r.count) / max) + '%"></i></span>' +
      '<span class="why-n num">' + fmt.num(r.count) + '</span>' +
      '<span class="why-only note">' + (r.only ? fmt.num(r.only) + ' for this reason alone' : 'always with another reason') + '</span>' +
      (r.explain ? '<span class="why-ex">' + esc(r.explain) + '</span>' : '') +
      (decided ? '<span class="why-st">' + esc(decided) + '</span>' : '') +
      '<span class="why-more"><span class="show">Show ' + (n === 1 ? 'the group' : 'the ' + n + ' groups') + '</span>' +
      '<span class="hide">Hide the ' + (n === 1 ? 'group' : 'groups') + '</span></span></summary>' +
      '<ul class="why-gs">' + groups.map((g) => groupHtml(s, ctx, r, g)).join('') + '</ul></details></li>';
  }

  function groupHtml(s, ctx, r, g) {
    const decided = decidedText(g);
    const open = ctx.sheet === g.key;
    return '<li class="why-g" data-group="' + esc(g.key) + '">' +
      '<div class="why-gl"><b>' + esc(g.label) + '</b>' + (decided ? '<span class="why-st">' + esc(decided) + '</span>' : '') + '</div>' +
      '<span class="why-gn num">' + fmt.num(g.count) + '</span>' +
      '<span class="why-go note">Only for this reason: ' + fmt.num(g.only) + '</span>' +
      '<span class="why-gb"><button type="button" class="btn small" data-review-group="' + esc(g.key) + '" aria-label="' +
      esc('Review the ' + fmt.num(g.count) + ' in ' + g.label) + '">Review</button>' +
      '<button type="button" class="btn small" data-decide-open="' + esc(g.key) + '" aria-expanded="' + open + '" aria-label="' +
      esc('Decide about the ' + fmt.num(g.count) + ' in ' + g.label) + '">Decide</button></span>' +
      (open ? decideHtml(s, Object.assign({reason: r.key, reason_label: r.label}, g)) : '') + '</li>';
  }

  /* Where the rule itself lives, so a whole rule (and future imports) can change instead. */
  function ruleHint(s, reason) {
    const ac = s.asset_class || {};
    const m = s.market || {};
    const ok = Array.isArray(ac.size_ok) ? ac.size_ok.join(', ') : '';
    switch (reason) {
      case 'drive_time': return {line: 'max_drive_minutes: ' + m.max_drive_minutes, where: 'config.yaml, under the ' + m.slug + ' market'};
      case 'size': return {line: 'ok: [' + ok + ']', where: 'config/asset_classes.yaml, under ' + ac.key + ' → size'};
      case 'year': return {line: 'year_built_min: ' + ac.year_built_min, where: 'config/asset_classes.yaml, under ' + ac.key};
      case 'skip_county': return {line: 'skip_counties:', where: 'config.yaml, under geography'};
      case 'state': return {line: 'allowed_states:', where: 'config.yaml, under geography'};
      case 'property_type': return {line: 'crexi_property_types:', where: 'config/asset_classes.yaml, under ' + ac.key};
      default: return null;
    }
  }

  function choiceHtml(key, decision, title, text, primary) {
    return '<button type="button" class="dec-opt' + (primary ? ' primary' : '') + '" data-decide="' + decision + '" data-bucket="' + esc(key) + '">' +
      '<b>' + esc(title) + '</b>' + (text ? '<span>' + esc(text) + '</span>' : '') + '</button>';
  }

  /** The Decide choices for one group g ({key, reason, label, count, only, added, kept, ...}). */
  function decideHtml(s, g) {
    const n = Number(g.count) || 0;
    const only = Math.min(n, Number(g.only) || 0);
    const other = n - only;
    const choices = [];
    if (g.reason === 'manual') {
      choices.push(choiceHtml(g.key, 'clear', 'Let the rules decide again for all ' + fmt.num(n),
        'Takes off the set-aside by hand. Each one is in or out by the rules again.', true));
      choices.push(choiceHtml(g.key, 'add_all', 'Add all ' + fmt.num(n) + ' to the buy box', 'Whatever the rules say.'));
    } else {
      if (only) {
        choices.push(choiceHtml(g.key, 'add_only', 'Add the ' + fmt.num(only) + ' set aside only for this',
          other ? 'The other ' + fmt.num(other) + ' also have another reason, so they stay set aside. This rule stops counting for them.'
            : 'All of them come into the buy box.', true));
      } else {
        choices.push(choiceHtml(g.key, 'add_only', 'Stop counting this rule for all ' + fmt.num(n),
          'Every one of them also has another reason, so they stay set aside for now.', false));
      }
      choices.push(choiceHtml(g.key, 'add_all', other ? 'Add all ' + fmt.num(n) + ' (also the ' + fmt.num(other) + ' with other reasons)'
        : 'Add all ' + fmt.num(n) + ', whatever the rules say', 'In the buy box by hand, whatever any rule says.'));
      choices.push(choiceHtml(g.key, 'keep', 'Keep them set aside', 'Marks them as looked at. Nothing else changes.'));
      if (g.added || g.kept) {
        choices.push(choiceHtml(g.key, 'clear', 'Undo my decisions for this group', 'The rules decide again for these ' + fmt.num(n) + '.'));
      }
    }
    const hint = ruleHint(s, g.reason);
    return '<div class="dec-sheet" data-sheet="' + esc(g.key) + '" role="group" aria-label="' + esc('Decide about ' + g.label) + '">' +
      '<p class="dec-q">What should happen to the ' + esc(fmt.num(n)) + ' in “' + esc(g.label) + '”?</p>' +
      '<div class="dec-opts">' + choices.join('') + '</div>' +
      '<p class="note">Properties you add get photos, photo checks and Crexi credits when those steps run. ' +
      'Properties you import later' + (g.reason === 'manual' ? '' : ' into this group') + ' are still checked by the rules.' +
      (hint ? ' To change the rule itself, edit <code>' + esc(hint.line) + '</code> in ' + esc(hint.where) + ', then restart the dashboard.' : '') + '</p>' +
      '<div class="dec-foot"><button type="button" class="btn small quiet" data-decide-close>Cancel</button></div></div>';
  }

  /* ---------- Review: the list of one set-aside group ---------- */

  /** The group being reviewed, from the summary's Why? panel (counts) and the list's review block (labels). */
  function groupFromSummary(ctx, review) {
    const key = ctx.prefs.bucket;
    const reason = key.split(':')[0];
    const sa = ctx.s.set_aside || {};
    const r = (sa.reasons || []).find((x) => x.key === reason) || null;
    const b = r ? (r.buckets || []).find((x) => x.key === key) || null : null;
    const old = ctx.group || {};
    const rv = review || {};
    const pick = (f) => Number(b && b[f] !== undefined ? b[f] : rv[f] !== undefined ? rv[f] : old[f]) || 0;
    return {
      key, reason,
      reason_label: rv.reason_label || old.reason_label || (r && r.label) || reason.replace(/_/g, ' '),
      label: rv.label || old.label || (b && b.label) || key.split(':')[1],
      explain: rv.explain || old.explain || (r && r.explain) || '',
      count: pick('count'), only: pick('only'), still_aside: pick('still_aside'), added: pick('added'), kept: pick('kept'),
    };
  }

  /** 'Reviewing: 738 properties 76–90 min away'. */
  function groupPhrase(g) {
    const props = fmt.plural(g.count, 'property', 'properties');
    const l = String(g.label || '');
    if (/:other$/.test(g.key)) return props + ' (' + String(g.reason_label).toLowerCase() + ', not grouped yet)';
    switch (g.reason) {
      case 'drive_time': return props + ' ' + l.replace(/^Over/, 'over') + ' away';
      case 'size': return props + ' with ' + l;
      case 'year': return props + ' built ' + (/^\d{4}s$/.test(l) ? 'in the ' + l : /^before/i.test(l) ? l.toLowerCase() : 'in ' + l);
      case 'skip_county': case 'state': return props + ' in ' + l;
      case 'property_type': return props + ' listed as ' + l;
      case 'manual': return props + ' set aside by hand';
      default: return props + ': ' + l;
    }
  }

  function reviewHtml(ctx) {
    const g = ctx.group;
    if (!g) return '';
    const stats = [fmt.num(g.still_aside) + ' set aside now', fmt.num(g.only) + ' only for this reason'];
    const decided = decidedText(Object.assign({}, g, {still_aside: 0}));
    if (decided) stats.push(decided);
    const open = ctx.sheet === g.key;
    return '<section class="review-head panel" aria-labelledby="reviewTitle">' +
      '<div class="rv-top"><div class="rv-title"><div class="eyebrow">' + esc(g.reason_label) + '</div>' +
      '<h2 id="reviewTitle">Reviewing: ' + esc(groupPhrase(g)) + '</h2></div>' +
      '<button type="button" class="btn small" data-review-exit>← Back to all deals</button></div>' +
      (g.explain ? '<p class="rv-ex">' + esc(g.explain) + '</p>' : '') +
      '<p class="note rv-stats">' + esc(stats.join(' · ')) + '</p>' +
      '<div class="rv-acts"><button type="button" class="btn primary small" data-decide-open="' + esc(g.key) + '" aria-expanded="' + open + '">' +
      'Decide for all ' + esc(fmt.num(g.count)) + '</button>' +
      '<span class="note">Or open one to look closer, and use Add to buy box on its row.</span></div>' +
      (open ? decideHtml(ctx.s, g) : '') + '</section>';
  }

  /* Redraw only when something shown changed, so a summary refresh never moves the panel under the finger. */
  function drawWhy(ctx) {
    const wrap = ctx.root.querySelector('[data-why-wrap]');
    if (!wrap) return;
    const sig = JSON.stringify([ctx.prefs.bucket, ctx.sheet, ctx.s.in_buybox, ctx.s.properties, ctx.s.set_aside]);
    if (sig === ctx.whySig) return;
    ctx.whySig = sig;
    wrap.innerHTML = ctx.prefs.bucket ? '' : setAsideHtml(ctx.s, ctx);
  }

  function drawReview(ctx) {
    const wrap = ctx.root.querySelector('[data-review]');
    if (!wrap) return;
    const sig = JSON.stringify([ctx.prefs.bucket, ctx.sheet, ctx.group]);
    if (sig === ctx.reviewSig) return;
    ctx.reviewSig = sig;
    wrap.innerHTML = ctx.prefs.bucket ? reviewHtml(ctx) : '';
  }

  function startReview(key) {
    if (!GROUP_KEY.test(key || '')) return;
    scrollTarget = '[data-review]';
    BM.go('deals', ['review', key]);
  }

  function exitReview(ctx) {
    scrollTarget = '[data-why-wrap]';
    BM.go('deals', ctx.prefs.view === 'all' ? [] : [ctx.prefs.view]);
  }

  /** Open or close the Decide choices of one group (in the Why? panel or the Review header). */
  function toggleSheet(ctx, key) {
    ctx.sheet = ctx.sheet === key ? null : key;
    if (ctx.prefs.bucket) drawReview(ctx); else drawWhy(ctx);
    const scope = ctx.root.querySelector(ctx.prefs.bucket ? '[data-review]' : '[data-why-wrap]');
    if (!scope) return;
    const focus = ctx.sheet ? scope.querySelector('[data-sheet] .dec-opt')
      : scope.querySelector('[data-decide-open="' + key + '"]');
    if (focus) focus.focus({preventScroll: false});
  }

  async function decide(ctx, key, decision) {
    if (ctx.busy || !GROUP_KEY.test(key || '')) return;
    ctx.busy = true;
    const sheet = ctx.root.querySelector('[data-sheet]');
    if (sheet) {
      sheet.classList.add('busy');
      sheet.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    }
    let res;
    try {
      res = await BM.api(BM.m('/buybox/decide'), {method: 'POST', body: {bucket: key, decision}});
    } catch (err) {
      ctx.busy = false;
      if (sheet && sheet.isConnected) {
        sheet.classList.remove('busy');
        sheet.querySelectorAll('button').forEach((b) => { b.disabled = false; });
      }
      BM.toast(err.message, {error: true});
      return;
    }
    ctx.busy = false;
    ctx.sheet = null;
    afterDecision(ctx, res);
  }

  /** After a group decision or its undo: say what happened (with Undo), then refresh the panel and the list. */
  function afterDecision(ctx, res) {
    undoToast(res && res.message ? res.message : 'Done.', res && res.batch_id);
    if (res && res.set_aside && ctx.s) {
      ctx.s = Object.assign({}, ctx.s, {set_aside: res.set_aside, in_buybox: res.in_buybox});
    }
    if (ctx.root.isConnected) {
      if (ctx.prefs.bucket) {
        ctx.group = groupFromSummary(ctx, null);
        drawReview(ctx);
      } else {
        drawWhy(ctx);
      }
      refresh(ctx);
    }
    BM.refreshSummary().catch(() => {});
  }

  /* A toast with an Undo button (the shared #toast can't be clicked). */
  let undoEl = null;
  let undoTimer = null;

  function undoToast(message, batchId) {
    if (!undoEl) {
      undoEl = document.createElement('div');
      undoEl.className = 'bb-toast';
      undoEl.setAttribute('role', 'status');
      undoEl.setAttribute('aria-live', 'polite');
      undoEl.hidden = true;
      document.body.appendChild(undoEl);
      undoEl.addEventListener('click', onUndoClick);
    }
    const plain = document.getElementById('toast');
    if (plain) plain.hidden = true;
    undoEl.innerHTML = '<span class="bb-msg">' + esc(message) + '</span>' +
      (batchId ? '<button type="button" class="btn small" data-undo-batch="' + esc(batchId) + '">Undo</button>' : '') +
      '<button type="button" class="x" data-undo-close aria-label="Close">✕</button>';
    undoEl.hidden = false;
    clearTimeout(undoTimer);
    undoTimer = setTimeout(hideUndo, batchId ? 20000 : Math.min(9000, 3000 + String(message).length * 35));
  }

  function hideUndo() {
    clearTimeout(undoTimer);
    if (undoEl) undoEl.hidden = true;
  }

  async function onUndoClick(ev) {
    if (ev.target.closest('[data-undo-close]')) { hideUndo(); return; }
    const b = ev.target.closest('[data-undo-batch]');
    if (!b || b.disabled) return;
    b.disabled = true;
    b.textContent = 'Undoing…';
    let res;
    try {
      res = await BM.api(BM.m('/buybox/undo'), {method: 'POST', body: {batch_id: b.getAttribute('data-undo-batch')}});
    } catch (err) {
      hideUndo();
      BM.toast(err.message, {error: true});
      return;
    }
    if (active && active.root.isConnected) {
      afterDecision(active, Object.assign({}, res, {batch_id: null}));
    } else {
      undoToast(res && res.message ? res.message : 'Undone.', null);
      BM.refreshSummary().catch(() => {});
    }
  }

  function knownView(s, chips, key) {
    return chips.some((c) => c.key === key) || (Array.isArray(s.views) && s.views.some((v) => v.key === key));
  }

  function viewLabel(ctx, key) {
    const chip = ctx.chips.find((c) => c.key === key);
    if (chip) return chip.label;
    const v = Array.isArray(ctx.s.views) ? ctx.s.views.find((x) => x.key === key) : null;
    return v ? v.label : key;
  }

  function tilesHtml(s) {
    const ac = s.asset_class || {};
    const credits = s.credits || {};
    const moonCut = (s.thresholds && s.thresholds.moon_score) || 0.10;
    const tiles = [
      [fmt.num(s.in_buybox), 'Properties in the buy box', 'view:all'],
      [fmt.pct(s.penetration || 0), 'Owners confirmed', 'go:drill'],
      [fmt.num(s.blue_moon_10), 'Blue Moon ' + cut(moonCut) + ' or higher', 'view:moon'],
      [credits.left === undefined ? '—' : fmt.num(credits.left), 'Crexi credits left this month', 'go:credits'],
    ];
    if (ac.visual_enabled) tiles.push([fmt.num(s.needs_review || 0), 'Need your photo review', 'go:review']);
    return tiles.map(([v, l, act]) =>
      '<button type="button" class="stat" data-tile="' + esc(act) + '"><span class="v num">' + esc(v) +
      '</span><span class="l">' + esc(l) + '</span></button>').join('');
  }

  function rangeText(range) {
    if (!Array.isArray(range) || range.length !== 2) return '';
    return fmt.num(range[0]) + '–' + fmt.num(range[1]);
  }

  function chipDefs(s) {
    const ac = s.asset_class || {};
    const th = s.thresholds || {};
    const ideal = rangeText(ac.size_ideal);
    const list = [
      ['all', 'All in buy box'],
      ['below', 'Not confirmed yet'],
      ['sweet', ideal ? ideal + ' ' + (ac.size_unit || '') : 'Ideal size'],
      ac.visual_enabled ? ['walk', ac.target_word || 'Photo target'] : null,
      ['mom', 'Mom-and-pop'],
      ['long', 'Owned ' + fmt.num(th.long_owned_years === undefined ? 15 : th.long_owned_years) + '+ yrs'],
      ['moon', 'Blue Moon ' + cut(th.moon_score === undefined ? 0.10 : th.moon_score) + '+'],
      ac.visual_enabled ? ['review', 'Needs photo review'] : null,
      ['confirmed', 'Confirmed owner'],
      ['excluded', 'Excluded'],
      ['everything', 'Everything'],
    ];
    return list.filter(Boolean).map(([key, label]) => ({key, label: label.trim()}));
  }

  function columns(s) {
    const ac = s.asset_class || {};
    const unit = ac.size_unit || 'units';
    const target = ac.target_word && ac.target_word !== 'Photo target' ? ac.target_word : 'Photo check';
    return {
      visual: !!ac.visual_enabled, rent: !!ac.rent_enabled, unit, target,
      sizeHead: unit.charAt(0).toUpperCase() + unit.slice(1),
      moonCut: (s.thresholds && s.thresholds.moon_score) || 0.10,
      warmCut: (s.thresholds && s.thresholds.funnel_score) || 0.05,
      origin: (s.market && s.market.origin_label) || 'the starting point',
    };
  }

  function toolbarHtml(ctx) {
    const p = ctx.prefs;
    const allowed = Array.isArray(ctx.s.sorts) ? ctx.s.sorts : SORTS.map((x) => x[0]);
    const sorts = SORTS.filter(([k]) => allowed.includes(k));
    return '<div class="deals-chips" role="group" aria-label="Filter" data-chips>' + chipsHtml(ctx) + '</div>' +
      '<div class="deals-tools">' +
      '<input class="search" type="search" placeholder="Search address, owner, parcel" aria-label="Search" value="' + esc(p.q) + '" data-search>' +
      '<select class="select" aria-label="Sort" data-sort>' + sorts.map(([k, l]) =>
        '<option value="' + k + '"' + (k === p.sort ? ' selected' : '') + '>Sort: ' + esc(l) + '</option>').join('') + '</select>' +
      '<span class="spacer"></span>' +
      '<div class="seg" role="group" aria-label="Show as">' +
      '<button type="button" data-mode="list" aria-pressed="' + (p.mode === 'list') + '">List</button>' +
      '<button type="button" data-mode="rings" aria-pressed="' + (p.mode === 'rings') + '">Drive-time rings</button>' +
      '</div></div>';
  }

  function chipsHtml(ctx) {
    return ctx.chips.map((c) => {
      const n = ctx.counts[c.key];
      const on = !ctx.prefs.bucket && c.key === ctx.prefs.view;
      return '<button type="button" class="chip" data-view="' + esc(c.key) + '" aria-pressed="' + on + '">' +
        esc(c.label) + (n === undefined ? '' : '<span class="cnt">' + fmt.num(n) + '</span>') + '</button>';
    }).join('');
  }

  /* ---------- events ---------- */

  function bind(ctx) {
    const r = ctx.root;
    r.querySelector('[data-chips]').addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-view]');
      if (b) setView(ctx, b.getAttribute('data-view'));
    });
    // The Why? panel and the Review header are redrawn after every decision, so their events are delegated
    // to wrappers that stay. 'toggle' doesn't bubble: it is caught on the way down (capture).
    const why = r.querySelector('[data-why-wrap]');
    why.addEventListener('toggle', (ev) => {
      const d = ev.target;
      if (d.matches('[data-why]')) {
        whyClosed = !d.open;
        writeStore(LS_WHY, whyClosed ? '1' : '0');
      } else if (d.matches('[data-why-reason]')) {
        const set = openReasons(ctx.s.set_aside || {reasons: []});
        if (d.open) set.add(d.getAttribute('data-why-reason')); else set.delete(d.getAttribute('data-why-reason'));
        writeStore(LS_WHY_OPEN, JSON.stringify(Array.from(set)));
      }
    }, true);
    why.addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-why-view]');
      if (b) {
        setView(ctx, b.getAttribute('data-why-view'));
        r.querySelector('[data-chips]').scrollIntoView({behavior: 'smooth', block: 'start'});
        return;
      }
      onDecideClick(ctx, ev);
    });
    r.querySelector('[data-review]').addEventListener('click', (ev) => onDecideClick(ctx, ev));
    r.querySelector('[data-tiles]').addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-tile]');
      if (!b) return;
      const [kind, value] = b.getAttribute('data-tile').split(':');
      if (kind === 'go') BM.go(value);
      else setView(ctx, value);
    });
    let timer = null;
    const search = r.querySelector('[data-search]');
    const onSearch = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const q = search.value.trim();
        if (q === ctx.prefs.q) return;
        ctx.prefs.q = q;
        refresh(ctx);
      }, 250);
    };
    search.addEventListener('input', onSearch);
    search.addEventListener('search', onSearch);
    r.querySelector('[data-sort]').addEventListener('change', (ev) => {
      ctx.prefs.sort = ev.target.value;
      storePrefs(ctx.prefs);
      refresh(ctx);
    });
    r.querySelector('.seg').addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-mode]');
      if (!b || b.getAttribute('data-mode') === ctx.prefs.mode) return;
      ctx.prefs.mode = b.getAttribute('data-mode');
      storePrefs(ctx.prefs);
      r.querySelectorAll('[data-mode]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      refresh(ctx);
    });
    const body = r.querySelector('[data-body]');
    body.addEventListener('click', (ev) => {
      if (ev.target.closest('a')) return;
      const act = ev.target.closest('[data-row-act]');     // a row's own buttons never open the drawer
      if (act) { rowAct(ctx, act); return; }
      const more = ev.target.closest('[data-more]');
      if (more) { load(ctx, true); return; }
      if (ev.target.closest('[data-clear]')) { clearFilters(ctx); return; }
      if (ev.target.closest('[data-review-exit]')) { exitReview(ctx); return; }
      const hit = ev.target.closest('[data-id]');
      if (hit) BM.openProperty(hit.getAttribute('data-id'));
    });
    body.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      if (ev.target.closest('button, a, input, select, textarea, summary')) return;   // let them do their own thing
      const row = ev.target.closest('tr[data-id], g[data-id]');
      if (!row) return;
      ev.preventDefault();
      BM.openProperty(row.getAttribute('data-id'));
    });
  }

  /** Clicks in the Why? panel and the Review header: Review, Decide, a choice, Cancel, Back to all deals. */
  function onDecideClick(ctx, ev) {
    const t = ev.target;
    let b = t.closest('[data-review-group]');
    if (b) { startReview(b.getAttribute('data-review-group')); return; }
    b = t.closest('[data-decide-open]');
    if (b) { toggleSheet(ctx, b.getAttribute('data-decide-open')); return; }
    b = t.closest('[data-decide]');
    if (b) { decide(ctx, b.getAttribute('data-bucket'), b.getAttribute('data-decide')); return; }
    if (t.closest('[data-decide-close]')) { toggleSheet(ctx, ctx.sheet); return; }
    if (t.closest('[data-review-exit]')) exitReview(ctx);
  }

  function setView(ctx, view) {
    if (!knownView(ctx.s, ctx.chips, view)) return;
    if (ctx.prefs.bucket) {        // a chip ends the Review of a group
      ctx.prefs.view = view;
      exitReview(ctx);
      return;
    }
    if (view === ctx.prefs.view && ctx.prefs.mode === 'list') return;
    ctx.prefs.view = view;
    BM.setParams(view === 'all' ? [] : [view]);
    ctx.root.querySelector('[data-chips]').innerHTML = chipsHtml(ctx);
    refresh(ctx);
  }

  function clearFilters(ctx) {
    if (ctx.prefs.bucket) {
      ctx.prefs.view = 'all';
      ctx.prefs.q = '';
      exitReview(ctx);
      return;
    }
    ctx.prefs.view = 'all';
    ctx.prefs.q = '';
    BM.setParams([]);
    ctx.root.querySelector('[data-search]').value = '';
    ctx.root.querySelector('[data-chips]').innerHTML = chipsHtml(ctx);
    refresh(ctx);
  }

  function refresh(ctx) {
    return ctx.prefs.mode === 'rings' ? drawRings(ctx) : load(ctx, false);
  }

  /* ---------- the list ---------- */

  function query(ctx, offset, limit, sort) {
    const q = new URLSearchParams({view: ctx.prefs.view, sort: sort || ctx.prefs.sort, limit: String(limit), offset: String(offset)});
    if (ctx.prefs.q) q.set('q', ctx.prefs.q);
    if (ctx.prefs.bucket) q.set('bucket', ctx.prefs.bucket);   // one group, in or out of the buy box
    return BM.m('/properties?' + q.toString());
  }

  async function load(ctx, append) {
    if (append && ctx.loading) return;
    const token = ++ctx.token;
    ctx.loading = true;
    const body = ctx.root.querySelector('[data-body]');
    const more = body.querySelector('[data-more]');
    if (append && more) {
      more.disabled = true;
      more.textContent = 'Loading…';
    } else {
      if (ctx.observer) ctx.observer.disconnect();
      body.innerHTML = ui.loading('Loading properties…');
    }
    let data;
    try {
      data = await BM.api(query(ctx, append ? ctx.items.length : 0, PAGE));
    } catch (err) {
      if (token !== ctx.token) return;
      ctx.loading = false;
      if (append && more) {
        more.disabled = false;
        more.textContent = 'Show more';
        BM.toast(err.message, {error: true});
      } else {
        body.innerHTML = ui.error(err);
        body.querySelector('[data-rerender]').addEventListener('click', (e) => { e.stopPropagation(); load(ctx, false); });
      }
      return;
    }
    if (token !== ctx.token || !ctx.root.isConnected) return;
    ctx.loading = false;
    const items = Array.isArray(data && data.items) ? data.items : [];
    ctx.items = append ? ctx.items.concat(items) : items;
    ctx.total = Number(data && data.total) || 0;
    if (data && data.counts) {
      ctx.counts = data.counts;
      ctx.root.querySelector('[data-chips]').innerHTML = chipsHtml(ctx);
    }
    if (ctx.prefs.bucket && data && data.review && data.review.bucket === ctx.prefs.bucket && !append) {
      ctx.group = groupFromSummary(ctx, data.review);
      drawReview(ctx);
    }
    if (append) appendRows(ctx, items);
    else drawList(ctx);
    drawStatus(ctx);
  }

  function drawStatus(ctx) {
    const el = ctx.root.querySelector('[data-status]');
    if (!el) return;
    const sort = (SORTS.find((x) => x[0] === ctx.prefs.sort) || SORTS[0])[1].toLowerCase();
    let text = fmt.plural(ctx.total, 'property', 'properties');
    if (ctx.prefs.bucket) text += ' in this group, in or out of the buy box';
    else if (ctx.prefs.view !== 'all') text += ' · ' + viewLabel(ctx, ctx.prefs.view);
    if (ctx.prefs.q) text += ' · matching “' + ctx.prefs.q + '”';
    if (ctx.prefs.mode === 'list') {
      text += ' · sorted ' + sort;
      if (ctx.total > ctx.items.length) text += ' · showing ' + fmt.num(ctx.items.length);
    }
    el.textContent = text;
  }

  function drawList(ctx) {
    const body = ctx.root.querySelector('[data-body]');
    if (!ctx.items.length) {
      body.innerHTML = noMatchHtml(ctx);
      return;
    }
    const c = ctx.cols;
    const head = '<th>Blue Moon</th><th>Property</th><th>Contact level</th><th class="c">' + esc(c.sizeHead) +
      '</th><th class="c">Drive</th>' + (c.visual ? '<th>' + esc(c.target) + '</th>' : '') + (c.rent ? '<th>Rent fit</th>' : '') +
      '<th>Owner type</th><th class="c">Owned</th><th title="How many score signals we have data for">Signals</th>';
    const sel = BM.route.property;
    body.innerHTML = '<div class="tablewrap"><table class="deals-table"><thead><tr>' + head + '</tr></thead><tbody data-rows>' +
      ctx.items.map((p) => rowHtml(p, c, sel)).join('') + '</tbody></table></div>' +
      '<div class="cards" data-cards>' + ctx.items.map((p) => cardHtml(p, c, sel)).join('') + '</div>' + moreHtml(ctx);
    observeMore(ctx);
  }

  function appendRows(ctx, items) {
    const body = ctx.root.querySelector('[data-body]');
    const sel = BM.route.property;
    body.querySelector('[data-rows]').insertAdjacentHTML('beforeend', items.map((p) => rowHtml(p, ctx.cols, sel)).join(''));
    body.querySelector('[data-cards]').insertAdjacentHTML('beforeend', items.map((p) => cardHtml(p, ctx.cols, sel)).join(''));
    const old = body.querySelector('.deals-more');
    if (old) old.remove();
    body.insertAdjacentHTML('beforeend', moreHtml(ctx));
    observeMore(ctx);
  }

  function moreHtml(ctx) {
    if (ctx.items.length >= ctx.total) return '';
    const left = ctx.total - ctx.items.length;
    return '<div class="deals-more"><button type="button" class="btn" data-more>Show ' + fmt.num(Math.min(PAGE, left)) +
      ' more <span class="muted">(' + fmt.num(left) + ' left)</span></button></div>';
  }

  function observeMore(ctx) {
    if (ctx.observer) ctx.observer.disconnect();
    ctx.observer = null;
    const btn = ctx.root.querySelector('[data-more]');
    if (!btn || !('IntersectionObserver' in window)) return;
    ctx.observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !ctx.loading && ctx.root.isConnected) load(ctx, true);
    }, {rootMargin: '600px 0px'});
    ctx.observer.observe(btn);
  }

  function noMatchHtml(ctx) {
    if (ctx.prefs.bucket) {
      return ui.empty(ctx.prefs.q ? 'No properties in this group match' : 'No properties in this group now',
        ctx.prefs.q ? 'Try another search.' : 'A new import or a re-score may have moved them to another group.',
        '<button type="button" class="btn" data-review-exit>Back to all deals</button>');
    }
    const filtered = ctx.prefs.view !== 'all' || ctx.prefs.q;
    return ui.empty(filtered ? 'No properties match' : 'Nothing in the buy box yet',
      filtered ? 'Try another filter or clear the search.'
        : 'Every property was set aside by a filter. Look at Excluded to see why, or adjust the buy box in config.',
      filtered ? '<button type="button" class="btn" data-clear>Show all in the buy box</button>' : '');
  }

  /* ---------- cells ---------- */

  function place(p) {
    const addr = fmt.title(p.address || p.name || 'No address') + (p.unit ? ' ' + fmt.title(String(p.unit)) : '');
    const city = [fmt.title(p.city || ''), p.state].filter(Boolean).join(', ');
    return {addr, city};
  }

  function reasonText(r) {
    return SET_ASIDE[r] || String(r).replace(/_/g, ' ');
  }

  /** {text, cls}: why a row is out of the buy box (red), or what the rules said about one added by hand. */
  function setAsideText(p) {
    const now = Array.isArray(p.exclusion_reasons) ? p.exclusion_reasons : [];
    const rules = Array.isArray(p.rule_reasons) ? p.rule_reasons : now;
    if (p.by_hand === 'added') {
      return {text: rules.length ? 'Rules said: ' + rules.map(reasonText).join(', ') : '', cls: 'setaside was'};
    }
    if (p.in_buybox !== false) return {text: '', cls: ''};
    const why = now.filter((r) => r !== 'manual').map(reasonText);
    const ignored = rules.filter((r) => !now.includes(r)).map(reasonText);
    let text = p.by_hand === 'aside' ? (why.length ? 'Rules also say: ' + why.join(', ') : '')
      : 'Set aside' + (why.length ? ': ' + why.join(', ') : '');
    if (ignored.length) text += (text ? ' · ' : '') + 'you said ' + ignored.join(', ') + " doesn't count";
    return {text, cls: 'setaside'};
  }

  /** The row's buy-box buttons: Add to buy box, or what was decided by hand with Undo. */
  function rowActHtml(p) {
    const id = esc(p.id);
    const addr = esc(place(p).addr);
    const undo = '<button type="button" class="linkbtn" data-row-act="rules" data-pid="' + id + '" aria-label="Undo: let the rules decide for ' +
      addr + '">Undo</button>';
    if (p.by_hand === 'added') return '<span class="row-acts"><span class="pill good">Added by you</span><span class="sep" aria-hidden="true">·</span>' + undo + '</span>';
    if (p.by_hand === 'aside') return '<span class="row-acts"><span class="pill bad">Set aside by you</span><span class="sep" aria-hidden="true">·</span>' + undo + '</span>';
    if (p.in_buybox === false) {
      return '<span class="row-acts"><button type="button" class="btn small" data-row-act="include" data-pid="' + id + '" aria-label="Add ' +
        addr + ' to the buy box">Add to buy box</button></span>';
    }
    return '';
  }

  function moonCell(p) {
    return '<div class="moon">' + ui.moon(p.score) + '<span class="pct num">' + fmt.pct(p.score) + '</span></div>';
  }

  function sizeText(p, c) {
    const v = p.size_value !== null && p.size_value !== undefined ? p.size_value : (c.unit === 'units' ? p.units : null);
    if (v === null || v === undefined) return {text: '?', est: false, unknown: true};
    const n = Number(v);
    const text = c.unit === 'units' ? fmt.num(Math.round(n)) : fmt.num(n);
    return {text, est: ESTIMATED.has(p.size_basis), unknown: false};
  }

  function sizeCell(p, c) {
    const sz = sizeText(p, c);
    if (sz.unknown) return '<span class="muted" title="Size unknown: never rules a property out">?</span>';
    if (sz.est) return '<span class="muted" title="Estimated, not counted">~' + esc(sz.text) + '</span> <span class="hint">est.</span>';
    return esc(sz.text);
  }

  function driveCell(p) {
    if (p.drive_minutes === null || p.drive_minutes === undefined) return '<span class="muted">?</span>';
    const est = p.drive_method === 'estimate';
    return '<span' + (est ? ' title="Straight-line estimate until Google drive times run"' : '') + '>' +
      esc(fmt.minutes(p.drive_minutes, p.drive_method)) + '</span>';
  }

  function photoInfo(v) {
    const vis = v || {};
    if (vis.needs_review) return {dot: 'n', text: 'Review', muted: true, title: 'The photo check is unsure: needs your look'};
    if (vis.p === null || vis.p === undefined) {
      const none = vis.status === 'no_imagery';
      return {dot: 'n', text: none ? 'No photo' : 'Not yet', muted: true,
        title: none ? 'Google has no Street View here' : 'The photo check has not run yet'};
    }
    const dot = {green: 'g', yellow: 'y', red: 'r'}[vis.color] || 'n';
    return {dot, text: Math.round(vis.p) + '%', muted: false, title: vis.reviewed ? 'Answered by you' : 'From the photo check',
      you: !!vis.reviewed};
  }

  function photoCell(v) {
    const i = photoInfo(v);
    return '<span title="' + esc(i.title) + '"><span class="dot ' + i.dot + '"></span>' +
      (i.muted ? '<span class="muted">' + esc(i.text) + '</span>' : esc(i.text)) +
      (i.you ? ' <span class="hint">you</span>' : '') + '</span>';
  }

  const RENT = {in: ['g', 'Match'], near: ['y', 'Close'], out: ['r', 'Off'], na: ['n', 'No data']};
  function rentCell(fit) {
    const r = RENT[fit];
    if (!r) return '<span class="muted" title="Rent step has not run yet">—</span>';
    return '<span class="dot ' + r[0] + '"></span>' + (fit === 'na' ? '<span class="muted">' + r[1] + '</span>' : r[1]);
  }

  function ownerText(p) {
    return OWNER_TYPES[p.owner_type] || (p.owner_type ? String(p.owner_type) : '—');
  }

  /** A short 'what to do next' for a row: the stage name, or what happens after the drill. */
  function nextLabel(p) {
    const text = String(p.next_step_text || '');
    if (!text) return '';
    switch (p.next_step_kind) {
      case 'stage': return text.split(':')[0].trim();
      case 'validate': return 'Reach out';
      case 'handoff': return /handed off/i.test(text) ? 'Handed off to the CRM' : 'Hand off to the CRM';
      case 'dnc': return 'Do not contact';
      case 'exhausted': return 'All sources tried';
      default: return '';
    }
  }

  function yearsText(p) {
    const y = p.years_owned;
    return y === null || y === undefined ? null : Math.round(Number(y)) + ' yrs';
  }

  function rowHtml(p, c, sel) {
    const {addr, city} = place(p);
    const aside = setAsideText(p);
    const acts = rowActHtml(p);
    const years = yearsText(p);
    const cls = [p.in_buybox === false ? 'out' : '', Number(sel) === Number(p.id) ? 'sel' : ''].filter(Boolean).join(' ');
    return '<tr data-id="' + esc(p.id) + '" tabindex="0"' + (cls ? ' class="' + cls + '"' : '') + '>' +
      '<td>' + moonCell(p) + '</td>' +
      '<td><div class="addr" title="' + esc(addr) + '">' + esc(addr) + '</div><div class="city">' + esc(city) + '</div>' +
      (aside.text ? '<span class="' + aside.cls + '">' + esc(aside.text) + '</span>' : '') + acts + '</td>' +
      '<td><div class="lvcell" title="' + esc(p.next_step_text ? 'Next: ' + p.next_step_text : '') + '">' +
      ui.levelPips(p.level, p.drill_status) + ui.channelBadges(p.channels) + '</div>' +
      (nextLabel(p) ? '<div class="city">Next: ' + esc(nextLabel(p)) + '</div>' : '') + '</td>' +
      '<td class="num c size">' + sizeCell(p, c) + '</td>' +
      '<td class="num c">' + driveCell(p) + '</td>' +
      (c.visual ? '<td>' + photoCell(p.visual) + '</td>' : '') +
      (c.rent ? '<td>' + rentCell(p.rent_fit) + '</td>' : '') +
      '<td>' + esc(ownerText(p)) + '</td>' +
      '<td class="num c">' + (years ? esc(years) : '<span class="muted">?</span>') + '</td>' +
      '<td>' + ui.coverage(p.coverage_have, p.coverage_total) + '</td></tr>';
  }

  function cardHtml(p, c, sel) {
    const {addr, city} = place(p);
    const aside = setAsideText(p);
    const sz = sizeText(p, c);
    const ph = photoInfo(p.visual);
    const rent = RENT[p.rent_fit];
    const years = yearsText(p);
    const drive = p.drive_minutes === null || p.drive_minutes === undefined ? '' : ' · ' + fmt.minutes(p.drive_minutes, p.drive_method);
    const cls = 'card' + (p.in_buybox === false ? ' out' : '') + (Number(sel) === Number(p.id) ? ' sel' : '');
    const acts = rowActHtml(p);
    const meta = [
      '<span class="lvline">' + ui.levelPips(p.level, p.drill_status) + ui.channelBadges(p.channels) + '</span>',
      nextLabel(p) ? '<span>Next: ' + esc(nextLabel(p)) + '</span>' : '',
      '<span>' + (sz.unknown ? '? ' : (sz.est ? '~' : '') + esc(sz.text) + ' ') + esc(c.unit) + '</span>',
      c.visual ? '<span><span class="dot ' + ph.dot + '"></span>' + esc(c.target) + (ph.muted ? '' : ' ' + esc(ph.text)) + '</span>' : '',
      c.rent ? '<span><span class="dot ' + (rent ? rent[0] : 'n') + '"></span>Rent</span>' : '',
      '<span>' + esc(ownerText(p)) + '</span>',
      years ? '<span>' + esc(years) + '</span>' : '',
    ].filter(Boolean).join('');
    // The card is a button, so its own buttons sit next to it (a button inside a button breaks the card).
    return '<div class="card-row' + (acts ? ' has-acts' : '') + '" data-card="' + esc(p.id) + '">' +
      '<button type="button" class="' + cls + '" data-id="' + esc(p.id) + '">' +
      '<span class="side">' + ui.moon(p.score, 30) + '<span class="pct num">' + fmt.pct(p.score) + '</span></span>' +
      '<span class="who"><span class="addr">' + esc(addr) + '</span><span class="city">' + esc(city + drive) + '</span>' +
      (aside.text ? '<span class="' + aside.cls + '">' + esc(aside.text) + '</span>' : '') + '</span>' +
      '<span class="meta">' + meta + '</span></button>' +
      (acts ? '<div class="card-acts">' + acts + '</div>' : '') + '</div>';
  }

  /* ---------- buy-box decisions on one row ---------- */

  function redrawItem(ctx, item) {
    if (ctx.prefs.mode !== 'list') return;
    const sel = BM.route.property;
    const tr = ctx.root.querySelector('tr[data-id="' + item.id + '"]');
    if (tr) tr.outerHTML = rowHtml(item, ctx.cols, sel);
    const card = ctx.root.querySelector('[data-card="' + item.id + '"]');
    if (card) card.outerHTML = cardHtml(item, ctx.cols, sel);
  }

  /** 'Add to buy box' (include) or 'Undo' (rules) on a row. The row stays where it is and shows the answer. */
  async function rowAct(ctx, btn) {
    const pid = Number(btn.getAttribute('data-pid'));
    const decision = btn.getAttribute('data-row-act');
    if (!pid || btn.disabled || !['include', 'exclude', 'rules'].includes(decision)) return;
    const inCard = !!btn.closest('[data-card]');
    btn.disabled = true;
    let res;
    try {
      res = await BM.api(BM.m('/properties/' + pid + '/buybox'), {method: 'POST', body: {decision}});
    } catch (err) {
      if (btn.isConnected) btn.disabled = false;
      BM.toast(err.message, {error: true});
      return;
    }
    if (res && res.message) BM.toast(res.message);
    if (active === ctx && ctx.root.isConnected && res && res.row) {
      const item = ctx.items.find((p) => Number(p.id) === pid);
      if (item) {
        Object.assign(item, res.row);
        redrawItem(ctx, item);
        const again = ctx.root.querySelector((inCard ? '[data-card="' : 'tr[data-id="') + pid + '"] [data-row-act]');
        if (again) again.focus({preventScroll: true});    // keyboard users stay on the row
      }
    }
    BM.refreshSummary().catch(() => {});
  }

  /* ---------- drive-time rings ---------- */

  async function originFor(ctx) {
    const m = ctx.s.market || {};
    const nested = m.origin && typeof m.origin === 'object' ? m.origin : {};
    let lat = num(m.origin_lat !== undefined ? m.origin_lat : nested.lat);
    let lng = num(m.origin_lng !== undefined ? m.origin_lng : nested.lng);
    if (lat !== null && lng !== null) return {lat, lng};
    if (!originCache[ctx.slug]) {
      originCache[ctx.slug] = BM.api('/api/settings').then((st) => {
        const list = (st && st.config && st.config.markets) || [];
        const found = list.find((x) => x && x.slug === ctx.slug) || {};
        const o = found.origin || {};
        return {lat: num(o.lat), lng: num(o.lng)};
      }).catch(() => ({lat: null, lng: null}));
    }
    const o = await originCache[ctx.slug];
    lat = o.lat;
    lng = o.lng;
    return lat !== null && lng !== null ? {lat, lng} : null;
  }

  /* A threshold as a short percent: 0.1 -> '10%', 0.05 -> '5%', 0.025 -> '2.5%'. */
  function cut(v) {
    return parseFloat((Number(v) * 100).toFixed(1)) + '%';
  }

  function num(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  async function ringPoints(ctx, token) {
    let items = [];
    let total = 0;
    for (let offset = 0; offset < RING_LIMIT; offset += RING_PAGE) {
      const data = await BM.api(query(ctx, offset, RING_PAGE, 'score'));
      if (token !== ctx.token) return null;
      const page = Array.isArray(data && data.items) ? data.items : [];
      items = items.concat(page);
      total = Number(data && data.total) || items.length;
      if (data && data.counts && offset === 0) {
        ctx.counts = data.counts;
        ctx.root.querySelector('[data-chips]').innerHTML = chipsHtml(ctx);
      }
      if (page.length < RING_PAGE || items.length >= total) break;
    }
    return {items, total};
  }

  async function drawRings(ctx) {
    const token = ++ctx.token;
    ctx.loading = true;
    if (ctx.observer) ctx.observer.disconnect();
    const body = ctx.root.querySelector('[data-body]');
    body.innerHTML = ui.loading('Placing properties by drive time…');
    let origin;
    let got;
    try {
      [origin, got] = await Promise.all([originFor(ctx), ringPoints(ctx, token)]);
    } catch (err) {
      if (token !== ctx.token) return;
      ctx.loading = false;
      body.innerHTML = ui.error(err);
      body.querySelector('[data-rerender]').addEventListener('click', (e) => { e.stopPropagation(); drawRings(ctx); });
      return;
    }
    if (!got || token !== ctx.token || !ctx.root.isConnected) return;
    ctx.loading = false;
    ctx.items = got.items;
    ctx.total = got.total;
    drawStatus(ctx);
    if (!got.items.length) {
      body.innerHTML = noMatchHtml(ctx);
      return;
    }
    if (!origin) {
      body.innerHTML = ui.empty('No starting point for this market',
        "The rings need the market's origin (lat and lng) in config.yaml under markets.");
      return;
    }
    body.innerHTML = ringsHtml(ctx, origin, got);
  }

  function bearingDeg(lat1, lng1, lat2, lng2) {
    const rad = Math.PI / 180;
    const p1 = lat1 * rad;
    const p2 = lat2 * rad;
    const dl = (lng2 - lng1) * rad;
    const y = Math.sin(dl) * Math.cos(p2);
    const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
    return (Math.atan2(y, x) / rad + 360) % 360;
  }

  function ringsHtml(ctx, origin, got) {
    const c = ctx.cols;
    const s = ctx.s;
    const maxDrive = num(s.market && s.market.max_drive_minutes) || 60;
    const maxRing = Math.max(60, Math.ceil(maxDrive / 15) * 15);
    const C = 320;
    const k = ctx.root.clientWidth && ctx.root.clientWidth < 560 ? 1.7 : 1;   // bigger marks on phones
    const R = 270;
    const scale = R / maxRing;
    let svg = '';
    for (let m = 15; m <= maxRing; m += 15) {
      svg += '<circle cx="' + C + '" cy="' + C + '" r="' + (m * scale).toFixed(1) + '" fill="none" stroke="var(--line)" stroke-width="1"/>' +
        '<text x="' + (C + 4) + '" y="' + (C - m * scale + 14 * k).toFixed(1) + '" fill="var(--faint)" font-size="' + Math.round(11 * k) + '" font-family="var(--f-ui)">' + m + ' min</text>';
    }
    [['N', 0], ['E', 90], ['S', 180], ['W', 270]].forEach(([d, a]) => {
      const t = a * Math.PI / 180;
      svg += '<text x="' + (C + Math.sin(t) * 300).toFixed(1) + '" y="' + (C - Math.cos(t) * 300 + 4).toFixed(1) +
        '" text-anchor="middle" fill="var(--faint)" font-size="' + Math.round(12 * k) + '" font-weight="600" font-family="var(--f-ui)">' + d + '</text>';
    });
    const placed = [];
    let skipped = 0;
    let beyond = 0;
    got.items.forEach((p) => {
      const lat = num(p.lat);
      const lng = num(p.lng);
      const mins = num(p.drive_minutes);
      if (lat === null || lng === null || mins === null) { skipped++; return; }
      placed.push({p, lat, lng, mins});
    });
    placed.sort((a, b) => (num(a.p.score) || 0) - (num(b.p.score) || 0));   // best on top
    const sel = Number(BM.route.property);
    placed.forEach(({p, lat, lng, mins}) => {
      const a = bearingDeg(origin.lat, origin.lng, lat, lng) * Math.PI / 180;
      const far = mins > maxRing;
      if (far) beyond++;
      const rr = far ? R + 12 : Math.max(0, mins) * scale;
      const x = (C + Math.sin(a) * rr).toFixed(1);
      const y = (C - Math.cos(a) * rr).toFixed(1);
      const score = num(p.score);
      const tier = score === null ? 0 : score >= c.moonCut ? 3 : score >= c.warmCut ? 2 : 1;
      const fill = ['var(--na)', 'var(--bad)', 'var(--warn)', 'var(--good)'][tier];
      const r = [4, 5, 7, 9][tier] * (k > 1 ? 1.4 : 1);
      const {addr, city} = place(p);
      svg += '<g data-id="' + esc(p.id) + '" tabindex="0" role="button" aria-label="' + esc(addr + ', ' + fmt.pct(score)) + '">' +
        '<circle class="hit" cx="' + x + '" cy="' + y + '" r="' + Math.round(13 * k) + '"/>' +
        '<circle cx="' + x + '" cy="' + y + '" r="' + r + '" fill="' + fill + '" fill-opacity="' + (far ? 0.45 : tier >= 2 ? 1 : 0.75) +
        '" stroke="var(--surface)" stroke-width="1.5"' + (sel === Number(p.id) ? ' class="sel"' : '') + '/>' +
        '<title>' + esc(addr + ', ' + city + ' · ' + fmt.pct(score) + ' · ' + fmt.minutes(mins, p.drive_method)) + '</title></g>';
    });
    svg += '<circle cx="' + C + '" cy="' + C + '" r="' + (5 * k) + '" fill="var(--ink)"/>' +
      '<text x="' + (C + 9 * k) + '" y="' + (C + 4 * k) + '" font-size="' + Math.round(12 * k) + '" fill="var(--ink)" font-weight="600" font-family="var(--f-ui)">' +
      esc(shortOrigin(c.origin)) + '</text>';
    const rings = [];
    for (let m = 15; m <= maxRing; m += 15) rings.push(m);
    const notes = [];
    if (skipped) notes.push(fmt.plural(skipped, 'property', 'properties') + ' without a map location or drive time are not shown.');
    if (beyond) notes.push(fmt.num(beyond) + ' beyond ' + maxRing + ' min sit just outside the last ring.');
    if (got.total > got.items.length) notes.push('Showing the top ' + fmt.num(got.items.length) + ' by Blue Moon score.');
    return '<div class="rings"><svg viewBox="0 0 640 640" role="img" aria-label="' + esc('Properties by drive time from ' + c.origin) + '">' + svg + '</svg>' +
      '<div class="legend"><h2>Drive time from ' + esc(c.origin) + '</h2>' +
      '<span>Rings at ' + listText(rings) + ' minutes. Direction is real; distance is drive time, not miles.</span>' +
      '<span><span class="dot g"></span>Blue Moon ' + cut(c.moonCut) + ' or higher</span>' +
      '<span><span class="dot y"></span>' + cut(c.warmCut).replace('%', '') + '–' + cut(c.moonCut) + '</span>' +
      '<span><span class="dot r"></span>Under ' + cut(c.warmCut) + '</span>' +
      '<span>Tap a dot to open the property.</span>' +
      notes.map((n) => '<span class="note">' + esc(n) + '</span>').join('') + '</div></div>';
  }

  function shortOrigin(label) {
    return String(label || '').replace(/^Downtown\s+/i, '').split(',')[0] || 'Start';
  }

  function listText(list) {
    if (list.length < 2) return list.join('');
    return list.slice(0, -1).join(', ') + ' and ' + list[list.length - 1];
  }

  /* ---------- live updates from the drawer and the summary ---------- */

  document.addEventListener('bm:drawer', (ev) => {
    if (!active || !active.root.isConnected) return;
    const id = ev.detail && ev.detail.id;
    active.root.querySelectorAll('tr.sel, .card.sel, circle.sel').forEach((el) => el.classList.remove('sel'));
    if (!id) return;
    active.root.querySelectorAll('tr[data-id="' + id + '"], .card[data-id="' + id + '"]').forEach((el) => el.classList.add('sel'));
    const dotEl = active.root.querySelector('g[data-id="' + id + '"] circle:not(.hit)');
    if (dotEl) dotEl.classList.add('sel');
  });

  document.addEventListener('bm:property-changed', (ev) => {
    const d = ev.detail || {};
    if (!active || !active.root.isConnected || !d.id || !d.row) return;
    const item = active.items.find((p) => Number(p.id) === Number(d.id));
    if (!item) return;
    ROW_KEYS.forEach((k) => { if (k in d.row) item[k] = d.row[k]; });
    redrawItem(active, item);
  });

  document.addEventListener('bm:summary', (ev) => {
    const s = ev.detail;
    if (!active || !active.root.isConnected || !s || !s.market || s.market.slug !== active.slug) return;
    active.s = s;
    const tiles = active.root.querySelector('[data-tiles]');
    if (tiles) tiles.innerHTML = tilesHtml(s);
    if (active.busy || !active.root.querySelector('[data-why-wrap]')) return;   // a decision is on its way
    if (active.prefs.bucket) {
      active.group = groupFromSummary(active, null);
      drawReview(active);
    } else {
      drawWhy(active);
    }
  });
})();
