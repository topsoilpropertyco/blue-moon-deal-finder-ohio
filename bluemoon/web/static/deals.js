/* Deals: the ranked list (or drive-time rings) for the current market.
 * Data: GET /api/m/{slug}/summary (tiles, labels) and GET /api/m/{slug}/properties (pages of PropertyRow).
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
    skip_county: 'skipped county', property_type: 'other property type',
  };
  const ESTIMATED = new Set(['address_estimate', 'sqft_estimate', 'acres_estimate']);
  const ROW_KEYS = ['level', 'drill_status', 'channels', 'next_step_text', 'next_step_kind', 'best_guess_person',
    'score', 'coverage_have', 'coverage_total', 'rank', 'visual', 'rent_fit', 'in_buybox', 'exclusion_reasons'];

  const prefsByMarket = {};
  const originCache = {};
  let active = null;   // the Deals screen currently on the page

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
        view: 'all', q: '',
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
    if (params && params[0] && chips.some((c) => c.key === params[0])) prefs.view = params[0];
    if (!chips.some((c) => c.key === prefs.view)) prefs.view = 'all';

    const ctx = {root, slug, s, prefs, chips, cols: columns(s), items: [], total: 0, counts: {}, token: 0,
      loading: false, observer: null};
    if (active && active.observer) active.observer.disconnect();
    active = ctx;

    if (!s.properties) {
      root.innerHTML = headHtml(s) + ui.emptyMarket();
      return;
    }
    root.innerHTML = headHtml(s) + '<div class="strip" data-tiles>' + tilesHtml(s) + '</div>' + toolbarHtml(ctx) +
      '<div class="deals-status note" data-status aria-live="polite"></div><div data-body></div>';
    bind(ctx);
    await refresh(ctx);
  }

  function headHtml(s) {
    const ac = s.asset_class || {};
    const m = s.market || {};
    const eyebrow = [ac.label, m.geography].filter(Boolean).join(' · ');
    return '<div class="deals-head"><div class="eyebrow">' + esc(eyebrow) + '</div>' +
      '<h1>Where the rare deals are</h1>' +
      '<ul class="lines">' +
      '<li><b>Blue Moon score:</b> how worth chasing the owner is.</li>' +
      '<li><b>Contact level:</b> 1 no owner data, 2 possible owner, 3 confirmed.</li>' +
      '<li>Work from the top. Get every owner to level 3.</li>' +
      '</ul></div>';
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
      return '<button type="button" class="chip" data-view="' + esc(c.key) + '" aria-pressed="' + (c.key === ctx.prefs.view) + '">' +
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
      const more = ev.target.closest('[data-more]');
      if (more) { load(ctx, true); return; }
      if (ev.target.closest('[data-clear]')) { clearFilters(ctx); return; }
      const hit = ev.target.closest('[data-id]');
      if (hit) BM.openProperty(hit.getAttribute('data-id'));
    });
    body.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      const row = ev.target.closest('tr[data-id], g[data-id]');
      if (!row) return;
      ev.preventDefault();
      BM.openProperty(row.getAttribute('data-id'));
    });
  }

  function setView(ctx, view) {
    if (!ctx.chips.some((c) => c.key === view)) return;
    if (view === ctx.prefs.view && ctx.prefs.mode === 'list') return;
    ctx.prefs.view = view;
    BM.setParams(view === 'all' ? [] : [view]);
    ctx.root.querySelector('[data-chips]').innerHTML = chipsHtml(ctx);
    refresh(ctx);
  }

  function clearFilters(ctx) {
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
    if (append) appendRows(ctx, items);
    else drawList(ctx);
    drawStatus(ctx);
  }

  function drawStatus(ctx) {
    const el = ctx.root.querySelector('[data-status]');
    if (!el) return;
    const chip = ctx.chips.find((c) => c.key === ctx.prefs.view);
    const sort = (SORTS.find((x) => x[0] === ctx.prefs.sort) || SORTS[0])[1].toLowerCase();
    let text = fmt.plural(ctx.total, 'property', 'properties');
    if (ctx.prefs.view !== 'all' && chip) text += ' · ' + chip.label;
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
      '<th>Owner</th><th class="c">Owned</th><th title="How many score signals we have data for">Data</th>';
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

  function setAsideText(p) {
    if (p.in_buybox !== false) return '';
    const why = (p.exclusion_reasons || []).map((r) => SET_ASIDE[r] || String(r).replace(/_/g, ' '));
    return 'Set aside' + (why.length ? ': ' + why.join(', ') : '');
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

  function yearsText(p) {
    const y = p.years_owned;
    return y === null || y === undefined ? null : Math.round(Number(y)) + ' yrs';
  }

  function rowHtml(p, c, sel) {
    const {addr, city} = place(p);
    const aside = setAsideText(p);
    const years = yearsText(p);
    const cls = [p.in_buybox === false ? 'out' : '', Number(sel) === Number(p.id) ? 'sel' : ''].filter(Boolean).join(' ');
    return '<tr data-id="' + esc(p.id) + '" tabindex="0"' + (cls ? ' class="' + cls + '"' : '') + '>' +
      '<td>' + moonCell(p) + '</td>' +
      '<td><div class="addr" title="' + esc(addr) + '">' + esc(addr) + '</div><div class="city">' + esc(city) + '</div>' +
      (aside ? '<span class="setaside">' + esc(aside) + '</span>' : '') + '</td>' +
      '<td><div class="lvcell" title="' + esc(p.next_step_text ? 'Next: ' + p.next_step_text : '') + '">' +
      ui.levelPips(p.level, p.drill_status) + ui.channelBadges(p.channels) + '</div></td>' +
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
    const meta = [
      '<span class="lvline">' + ui.levelPips(p.level, p.drill_status) + ui.channelBadges(p.channels) + '</span>',
      '<span>' + (sz.unknown ? '? ' : (sz.est ? '~' : '') + esc(sz.text) + ' ') + esc(c.unit) + '</span>',
      c.visual ? '<span><span class="dot ' + ph.dot + '"></span>' + esc(c.target) + (ph.muted ? '' : ' ' + esc(ph.text)) + '</span>' : '',
      c.rent ? '<span><span class="dot ' + (rent ? rent[0] : 'n') + '"></span>Rent</span>' : '',
      '<span>' + esc(ownerText(p)) + '</span>',
      years ? '<span>' + esc(years) + '</span>' : '',
    ].filter(Boolean).join('');
    return '<button type="button" class="' + cls + '" data-id="' + esc(p.id) + '">' +
      '<span class="side">' + ui.moon(p.score, 30) + '<span class="pct num">' + fmt.pct(p.score) + '</span></span>' +
      '<span class="who"><span class="addr">' + esc(addr) + '</span><span class="city">' + esc(city + drive) + '</span>' +
      (aside ? '<span class="setaside">' + esc(aside) + '</span>' : '') + '</span>' +
      '<span class="meta">' + meta + '</span></button>';
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
    if (active.prefs.mode !== 'list') return;
    const sel = BM.route.property;
    const tr = active.root.querySelector('tr[data-id="' + item.id + '"]');
    if (tr) tr.outerHTML = rowHtml(item, active.cols, sel);
    const card = active.root.querySelector('.card[data-id="' + item.id + '"]');
    if (card) card.outerHTML = cardHtml(item, active.cols, sel);
  });

  document.addEventListener('bm:summary', (ev) => {
    const s = ev.detail;
    if (!active || !active.root.isConnected || !s || !s.market || s.market.slug !== active.slug) return;
    active.s = s;
    const tiles = active.root.querySelector('[data-tiles]');
    if (tiles) tiles.innerHTML = tilesHtml(s);
  });
})();
