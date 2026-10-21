/* Blue Moon Deal Finder: the app shell.
 *
 * Provides window.BM (the contract every screen uses): the API helper, app state, view registry,
 * hash router (#view/param/...?p=<property id>), top bar with the market switcher, grouped navigation,
 * the property drawer container, toast, formatters (BM.fmt) and small HTML helpers (BM.ui).
 * Screens live in their own files and call BM.registerView(); a missing file never breaks the app.
 */
(function () {
  'use strict';

  const LS_MARKET = 'bm.market';
  const LS_DISMISSED = 'bm.dismissed';
  const GROUPS = [['work', 'Work'], ['insight', 'Insight'], ['setup', 'Setup']];
  const DEFAULT_VIEW = 'deals';
  const SUMMARY_STALE_MS = 60000;
  const LEVEL_NAMES = {1: 'No owner data', 2: 'Possible owner', 3: 'Confirmed owner'};
  const CHANNELS = [['mail', 'Mail'], ['phone', 'Call'], ['text', 'Text'], ['email', 'Email'], ['social', 'Social'], ['vm', 'VM']];
  const CHANNEL_STATES = {
    verified: ['v', 'works'], v: ['v', 'works'],
    untested: ['u', 'found, not tested yet'], u: ['u', 'found, not tested yet'],
    wrong: ['w', 'wrong'], w: ['w', 'wrong'],
    dnc: ['w dnc', 'do not contact'],
    none: ['none', 'none found'],
  };

  const views = new Map();
  const state = {market: null, markets: [], summary: null, health: null};
  const route = {key: null, params: [], path: null, requested: null};
  const drawer = {id: null, pushed: false, lastFocus: null};
  let booted = false;
  let summaryAt = 0;
  let summaryInFlight = null;
  let loginShown = false;
  let moonSeq = 0;

  const $ = (sel) => document.querySelector(sel);

  /* ---------- small utilities ---------- */

  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function readStore(key, fallback) {
    try {
      const v = window.localStorage.getItem(key);
      return v === null ? fallback : v;
    } catch (e) {
      return fallback;
    }
  }

  function writeStore(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private mode: fine */ }
  }

  function emit(name, detail) {
    document.dispatchEvent(new CustomEvent(name, {detail}));
  }

  /* ---------- API ---------- */

  function detailText(data, res) {
    const d = data && data.detail;
    if (typeof d === 'string' && d.trim()) return d;
    if (Array.isArray(d) && d.length) return d.map((x) => (x && (x.msg || x.message)) || String(x)).join(' ');
    if (data && typeof data.message === 'string') return data.message;
    if (res.status === 404) return 'That was not found.';
    if (res.status >= 500) return 'The server had a problem (' + res.status + '). Details are in the server log.';
    return 'The server said no (' + res.status + (res.statusText ? ' ' + res.statusText : '') + ').';
  }

  async function api(path, opts) {
    const o = opts || {};
    const init = {method: o.method || 'GET', headers: {Accept: 'application/json'}, credentials: 'same-origin'};
    if (o.form) {
      init.body = o.form;
      if (init.method === 'GET') init.method = 'POST';
    } else if (o.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(o.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      throw new Error("Can't reach the Blue Moon server. Check that it is still running on the Mac Mini, then try again.");
    }
    const type = res.headers.get('content-type') || '';
    let data = null;
    if (type.includes('json')) {
      try { data = await res.json(); } catch (e) { data = null; }
    } else if (res.status !== 204) {
      try { data = await res.text(); } catch (e) { data = null; }
    }
    if (res.status === 401 && !String(path).startsWith('/api/login')) showLogin();
    if (!res.ok) {
      const err = new Error(detailText(data && typeof data === 'object' ? data : null, res));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function marketPath(path) {
    return '/api/m/' + encodeURIComponent(state.market || '') + (path || '');
  }

  /* ---------- login (only when APP_PASSWORD is set) ---------- */

  function showLogin() {
    if (loginShown) return;
    loginShown = true;
    const box = document.createElement('div');
    box.className = 'login';
    box.innerHTML =
      '<form class="panel" novalidate>' +
      '<div class="brand"><svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true"><circle cx="11" cy="11" r="10" fill="var(--accent)"/>' +
      '<circle cx="15" cy="8" r="9" fill="var(--surface)"/></svg><b>Blue Moon</b></div>' +
      '<h2>This dashboard is locked</h2>' +
      '<p class="note" style="margin:0">Type the dashboard password (APP_PASSWORD in the .env file on the Mac Mini).</p>' +
      '<label class="field">Password<input type="password" name="password" autocomplete="current-password" required></label>' +
      '<p class="err" hidden></p>' +
      '<button class="btn primary" type="submit">Unlock</button></form>';
    document.body.appendChild(box);
    const form = box.querySelector('form');
    const err = box.querySelector('.err');
    form.password.focus();
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.hidden = true;
      const btn = form.querySelector('button');
      btn.disabled = true;
      try {
        await api('/api/login', {method: 'POST', body: {password: form.password.value}});
        window.location.reload();
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
        btn.disabled = false;
        form.password.select();
      }
    });
  }

  /* ---------- formatters ---------- */

  function toNumber(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function parseDate(iso) {
    if (!iso) return null;
    const s = String(iso);
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    const d = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  const fmt = {
    pct(p) {
      const v = toNumber(p);
      if (v === null) return '—';
      if (v <= 0) return '0%';
      if (v < 0.001) return '<0.1%';
      if (v < 0.1) return (v * 100).toFixed(1) + '%';
      return Math.round(v * 100) + '%';
    },
    num(n) {
      const v = toNumber(n);
      if (v === null) return '—';
      return v.toLocaleString('en-US', {maximumFractionDigits: Number.isInteger(v) ? 0 : 1});
    },
    money(n) {
      const v = toNumber(n);
      if (v === null) return '—';
      const a = Math.abs(v);
      const sign = v < 0 ? '-' : '';
      if (a >= 1e6) return sign + '$' + parseFloat((a / 1e6).toFixed(a >= 1e7 ? 1 : 2)) + 'M';
      if (a >= 1e5) return sign + '$' + Math.round(a / 1000) + 'K';
      return sign + '$' + Math.round(a).toLocaleString('en-US');
    },
    minutes(m, method) {
      const v = toNumber(m);
      if (v === null) return '—';
      return (method === 'estimate' ? '~' : '') + Math.round(v) + ' min';
    },
    date(iso) {
      const d = parseDate(iso);
      if (!d) return iso ? String(iso) : '—';
      return d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric'});
    },
    ago(iso) {
      const d = parseDate(iso);
      if (!d) return iso ? String(iso) : '—';
      const s = (Date.now() - d.getTime()) / 1000;
      if (s < 45) return 'just now';
      if (s < 90) return '1 min ago';
      if (s < 2700) return Math.round(s / 60) + ' min ago';
      if (s < 5400) return '1 hour ago';
      if (s < 79200) return Math.round(s / 3600) + ' hours ago';
      if (s < 129600) return 'yesterday';
      if (s < 7 * 86400) return Math.round(s / 86400) + ' days ago';
      return fmt.date(iso);
    },
    /* Extras (not in the core contract): readable casing for ALL-CAPS addresses, and plurals. */
    title(text) {
      if (!text) return '';
      const s = String(text);
      if (s !== s.toUpperCase()) return s;   // already mixed case: leave it alone
      const KEEP = new Set(['N', 'S', 'E', 'W', 'NE', 'NW', 'SE', 'SW', 'PO', 'US', 'SR', 'CR', 'OH', 'IN', 'KY', 'MI', 'PA', 'WV', 'LLC', 'LP', 'II', 'III', 'IV']);
      return s.toLowerCase().replace(/[a-z0-9'’]+/g, (w) => {
        const up = w.toUpperCase();
        if (KEEP.has(up)) return up;
        if (/^\d/.test(w)) return w;          // 4th, 123b
        return w.charAt(0).toUpperCase() + w.slice(1);
      });
    },
    plural(n, one, many) {
      const v = toNumber(n) || 0;
      return fmt.num(v) + ' ' + (v === 1 ? one : (many || one + 's'));
    },
  };

  /* ---------- HTML helpers ---------- */

  /* Moon glyph: the lit share grows with the score (30% = full moon), as in the mockup. */
  function moon(p, size) {
    const sz = size || 22;
    const v = toNumber(p);
    const r = sz / 2 - 1;
    const f = v === null ? 0 : Math.max(0, Math.min(1, v / 0.30));
    const d = (2 * r) * (1 - f);
    const id = 'bmm' + (++moonSeq);
    const c = sz / 2;
    return '<svg width="' + sz + '" height="' + sz + '" viewBox="0 0 ' + sz + ' ' + sz + '" aria-hidden="true" focusable="false">' +
      '<defs><clipPath id="' + id + '"><circle cx="' + c + '" cy="' + c + '" r="' + r + '"/></clipPath></defs>' +
      '<circle cx="' + c + '" cy="' + c + '" r="' + r + '" fill="var(--moon-dim)"/>' +
      (f > 0 ? '<circle clip-path="url(#' + id + ')" cx="' + (c + d) + '" cy="' + c + '" r="' + r + '" fill="var(--accent)"/>' : '') +
      '</svg>';
  }

  /* levelPips(level, status?) or levelPips(level, {status, label:false}) */
  function levelPips(level, opts) {
    const o = typeof opts === 'string' ? {status: opts} : (opts || {});
    const lv = [1, 2, 3].includes(Number(level)) ? Number(level) : 1;
    const dnc = o.status === 'dnc';
    let name = dnc ? 'Do not contact' : LEVEL_NAMES[lv];
    let extra = '';
    if (o.status === 'exhausted') extra = ' <span class="lvs">· all tried</span>';
    const title = 'Level ' + lv + ': ' + LEVEL_NAMES[lv] + (dnc ? ' · do not contact' : '') +
      (o.status === 'exhausted' ? ' · every source tried' : '');
    const label = o.label === false ? '' : '<span class="lvn">' + esc(name) + '</span>' + extra;
    return '<span class="lv l' + lv + (dnc ? ' dnc' : '') + '" title="' + esc(title) + '">' +
      '<span class="pips" aria-hidden="true"><i></i><i></i><i></i></span>' + label + '</span>';
  }

  function channelBadges(channels) {
    const c = channels || {};
    return '<span class="chs">' + CHANNELS.map(([key, label]) => {
      const st = CHANNEL_STATES[c[key]] || CHANNEL_STATES.none;
      return '<span class="ch ' + st[0] + '" title="' + esc(label + ': ' + st[1]) + '">' + label + '</span>';
    }).join('') + '</span>';
  }

  function showValue(v, unit) {
    const n = toNumber(v);
    return (n === null ? esc(v === null || v === undefined ? '—' : v) : fmt.num(n)) + esc(unit || '');
  }

  /* bars([{label, value, cls?, hint?, head?}], {max, unit, drops}) -> rows like the mockup's .hb */
  function bars(rows, opts) {
    const list = Array.isArray(rows) ? rows : [];
    const o = opts || {};
    const values = list.map((r) => toNumber(r && r.value) || 0);
    const max = toNumber(o.max) || Math.max(0, ...values);
    const unit = o.unit || '';
    const html = list.map((r, i) => {
      const v = values[i];
      const w = max > 0 ? Math.max(0, Math.min(100, (v / max) * 100)) : 0;
      let h = r.head ? '<div class="stagehead">' + esc(r.head) + '</div>' : '';
      const tip = (r.label || '') + ': ' + (toNumber(r.value) === null ? (r.value || '—') : fmt.num(v)) + unit +
        (r.hint ? ' · ' + r.hint : '');
      h += '<div class="hb" title="' + esc(tip) + '"><span class="lab">' + esc(r.label) +
        (r.hint ? '<small>' + esc(r.hint) + '</small>' : '') + '</span>' +
        '<div class="t"><i class="' + esc(r.cls || '') + (v === 0 ? ' zero' : '') + '" style="width:' + w.toFixed(1) + '%"></i></div>' +
        '<span class="c num">' + showValue(r.value, unit) + '</span></div>';
      const next = list[i + 1];
      if (o.drops && next && !next.head) {
        const pct = v > 0 ? Math.round(((toNumber(next.value) || 0) / v) * 100) : 0;
        h += '<div class="drop num">' + pct + '% move on</div>';
      }
      return h;
    }).join('');
    return '<div class="hbars">' + html + '</div>';
  }

  function stat(value, label) {
    const n = toNumber(value);
    const v = n === null ? esc(value === null || value === undefined ? '—' : value) : fmt.num(n);
    return '<div class="stat"><div class="v num">' + v + '</div><div class="l">' + esc(label) + '</div></div>';
  }

  function empty(title, text, actionHtml) {
    return '<div class="empty">' + moon(0.12, 40) + '<h2>' + esc(title) + '</h2>' +
      (text ? '<p class="note">' + esc(text) + '</p>' : '') + (actionHtml || '') + '</div>';
  }

  /* Extras (not in the core contract) that keep every screen consistent. */
  function loading(text) {
    return '<div class="loading"><span class="spin" aria-hidden="true"></span>' + esc(text || 'Loading…') + '</div>';
  }

  function errorBox(err, retryLabel) {
    const msg = (err && err.message) || String(err || 'Something went wrong.');
    return '<div class="errbox" role="alert"><p>' + esc(msg) + '</p>' +
      (retryLabel === false ? '' : '<button class="btn small" type="button" data-rerender>' + esc(retryLabel || 'Try again') + '</button>') +
      '</div>';
  }

  function emptyMarket() {
    const m = currentMarket();
    return empty('No properties in this market yet',
      'Import a Crexi export to fill ' + (m ? m.name : 'this market') + '. Nothing is ever deleted, and re-importing a newer export just updates it.',
      '<button class="btn primary" type="button" data-go="import">Import a Crexi file</button>');
  }

  function dot(color) {
    const c = {green: 'g', yellow: 'y', red: 'r', g: 'g', y: 'y', r: 'r'}[color] || 'n';
    return '<span class="dot ' + c + '" aria-hidden="true"></span>';
  }

  function coverage(have, total) {
    const t = Math.max(0, Math.min(20, toNumber(total) || 0));
    const h = Math.max(0, Math.min(t, toNumber(have) || 0));
    let s = '';
    for (let i = 0; i < t; i++) s += '<i class="' + (i < h ? 'on' : '') + '"></i>';
    return '<span class="cov" title="' + h + ' of ' + t + ' signals known">' + s + '</span>';
  }

  /* ---------- toast ---------- */

  let toastTimer = null;
  function toast(message, opts) {
    const el = $('#toast');
    if (!el) return;
    const text = String(message || '');
    el.textContent = text;
    el.classList.toggle('bad', !!(opts && opts.error));
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, Math.min(9000, 2600 + text.length * 35));
  }

  /* ---------- markets + summary ---------- */

  function currentMarket() {
    return state.markets.find((m) => m.slug === state.market) || null;
  }

  function pickMarket(markets) {
    const saved = readStore(LS_MARKET, null);
    if (saved && markets.some((m) => m.slug === saved)) return saved;
    const withData = markets.find((m) => (m.properties || 0) > 0);
    return (withData || markets[0] || {}).slug || null;
  }

  function marketOption(m) {
    const n = m.properties || 0;
    const label = (m.asset_label || m.asset_class || '') + ' · ' + (m.geography || m.slug) +
      (n ? ' (' + fmt.num(n) + ')' : ' (none yet)');
    return '<option value="' + esc(m.slug) + '"' + (m.slug === state.market ? ' selected' : '') + '>' + esc(label) + '</option>';
  }

  function renderMarketSelect() {
    const sel = $('#market');
    if (!sel) return;
    const groups = new Map();
    state.markets.forEach((m) => {
      const g = m.asset_label || m.asset_class || 'Other';
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(m);
    });
    sel.innerHTML = Array.from(groups.entries()).map(([g, list]) =>
      '<optgroup label="' + esc(g) + '">' + list.map(marketOption).join('') + '</optgroup>').join('');
    sel.disabled = state.markets.length < 2;
    sel.value = state.market || '';
    const m = currentMarket();
    sel.title = m ? 'Market: ' + m.name + '. ' + fmt.plural(m.properties, 'property', 'properties') + '.' : 'Market';
  }

  async function loadMarkets() {
    const list = await api('/api/markets');
    state.markets = Array.isArray(list) ? list : [];
    if (!state.market || !state.markets.some((m) => m.slug === state.market)) state.market = pickMarket(state.markets);
    renderMarketSelect();
    return state.markets;
  }

  function refreshSummary() {
    const slug = state.market;
    if (!slug) return Promise.resolve(null);
    if (summaryInFlight && summaryInFlight.slug === slug) return summaryInFlight.promise;
    const promise = api(marketPath('/summary')).then((s) => {
      if (slug === state.market) {
        state.summary = s;
        summaryAt = Date.now();
        updateMarketCount(s);
        renderNav();
        emit('bm:summary', s);
      }
      return s;
    }).finally(() => {
      if (summaryInFlight && summaryInFlight.promise === promise) summaryInFlight = null;
    });
    summaryInFlight = {slug, promise};
    return promise;
  }

  function updateMarketCount(s) {
    const m = currentMarket();
    if (!m || !s || typeof s.properties !== 'number' || m.properties === s.properties) return;
    m.properties = s.properties;
    renderMarketSelect();
  }

  async function setMarket(slug) {
    if (!slug || slug === state.market) return;
    state.market = slug;
    state.summary = null;
    writeStore(LS_MARKET, slug);
    renderMarketSelect();
    closeDrawer();
    try { await refreshSummary(); } catch (e) { /* each screen shows its own error */ }
    window.scrollTo(0, 0);
    rerender();
    // Market counts change after imports; refresh them quietly.
    loadMarkets().catch(() => {});
  }

  /* ---------- views + navigation ---------- */

  function registerView(key, def) {
    if (!key || !def || typeof def.render !== 'function') return;
    views.set(String(key), {
      key: String(key),
      label: def.label || String(key),
      group: GROUPS.some((g) => g[0] === def.group) ? def.group : 'work',
      order: Number.isFinite(Number(def.order)) ? Number(def.order) : 100,
      render: def.render,
      badge: typeof def.badge === 'function' ? def.badge : null,
    });
    if (!booted) return;
    renderNav();
    if (route.requested === key && route.key !== key) onHashChange();
  }

  function sortedViews(group) {
    return Array.from(views.values()).filter((v) => v.group === group)
      .sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));
  }

  function badgeHtml(v) {
    if (!v.badge || !state.summary) return '';
    let b = null;
    try { b = v.badge(state.summary); } catch (e) { b = null; }
    if (b === null || b === undefined || b === '' || b === 0 || b === false) return '';
    const text = typeof b === 'number' ? fmt.num(b) : String(b);
    return '<span class="badge">' + esc(text) + '</span>';
  }

  function renderNav() {
    const nav = $('#nav');
    if (!nav) return;
    nav.innerHTML = GROUPS.map(([group, label]) => {
      const items = sortedViews(group);
      if (!items.length) return '';
      return '<div class="navgroup" role="group" aria-label="' + esc(label) + '"><span class="navlabel">' + esc(label) + '</span>' +
        items.map((v) => '<a class="tab" href="#' + esc(v.key) + '" data-key="' + esc(v.key) + '"' +
          (v.key === route.key ? ' aria-current="page"' : '') + '>' + esc(v.label) + badgeHtml(v) + '</a>').join('') +
        '</div>';
    }).join('');
    const cur = nav.querySelector('[aria-current="page"]');
    if (cur && nav.scrollWidth > nav.clientWidth) {
      const left = cur.offsetLeft - nav.clientWidth / 2 + cur.offsetWidth / 2;
      nav.scrollLeft = Math.max(0, left);
    }
  }

  function defaultView() {
    if (views.has(DEFAULT_VIEW)) return DEFAULT_VIEW;
    for (const [group] of GROUPS) {
      const list = sortedViews(group);
      if (list.length) return list[0].key;
    }
    return null;
  }

  function parseHash() {
    let h = (window.location.hash || '').replace(/^#\/?/, '');
    let query = '';
    const qi = h.indexOf('?');
    if (qi >= 0) {
      query = h.slice(qi + 1);
      h = h.slice(0, qi);
    }
    const parts = h.split('/').filter(Boolean).map((p) => {
      try { return decodeURIComponent(p); } catch (e) { return p; }
    });
    const key = parts.shift() || '';
    const pid = new URLSearchParams(query).get('p');
    return {key, params: parts, pid: pid && /^\d+$/.test(pid) ? Number(pid) : null};
  }

  function pathOf(key, params) {
    return [key].concat((params || []).map((p) => encodeURIComponent(String(p)))).join('/');
  }

  function onHashChange() {
    if (!booted) return;
    const r = parseHash();
    route.requested = r.key || null;
    let key = r.key;
    let params = r.params;
    if (!views.has(key)) {
      key = defaultView();
      params = [];
      if (key && r.key) {   // unknown view: show the default and fix the address bar
        history.replaceState(null, '', '#' + key + (r.pid ? '?p=' + r.pid : ''));
      }
    }
    if (!key) {
      showNoViews();
      return;
    }
    const path = pathOf(key, params);
    if (path !== route.path) {
      route.key = key;
      route.params = params;
      route.path = path;
      renderNav();
      renderCurrent({scrollTop: true});
    }
    syncDrawer(r.pid);
  }

  function showNoViews() {
    const main = $('#main');
    main.innerHTML = '<section class="view">' + errorBox(new Error('The screens did not load. Reload the page; if that does not help, restart the app.'), 'Reload') + '</section>';
    const b = main.querySelector('[data-rerender]');
    if (b) b.addEventListener('click', () => window.location.reload());
  }

  let renderSeq = 0;
  function renderCurrent(opts) {
    const v = views.get(route.key);
    const main = $('#main');
    if (!v || !main) return;
    const seq = ++renderSeq;
    const old = main.querySelector('section.view');
    const keepHeight = old && !(opts && opts.scrollTop) ? old.offsetHeight : 0;
    const root = document.createElement('section');
    root.className = 'view view-' + v.key;
    root.dataset.view = v.key;
    root.innerHTML = loading();
    if (keepHeight) main.style.minHeight = (keepHeight + 88) + 'px';
    if (old) old.replaceWith(root); else main.appendChild(root);
    if (opts && opts.scrollTop) window.scrollTo(0, 0);
    document.title = v.label + ' · Blue Moon Deal Finder';
    Promise.resolve()
      .then(() => v.render(root, route.params.slice()))
      .catch((err) => {
        if (root.isConnected) root.innerHTML = errorBox(err);
        if (window.console) console.error(err);
      })
      .finally(() => {
        if (seq === renderSeq) main.style.minHeight = '';
      });
  }

  function rerender() {
    if (!booted || !route.key) return;
    renderCurrent({scrollTop: false});
  }

  function go(key, params) {
    const list = Array.isArray(params) ? params : (params === undefined || params === null ? [] : [params]);
    window.location.hash = '#' + key + (list.length ? '/' + list.map((p) => encodeURIComponent(String(p))).join('/') : '');
  }

  /* Extra: change the current view's params in the address bar without re-rendering it. */
  function setParams(params) {
    if (!route.key) return;
    route.params = (params || []).map(String);
    route.path = pathOf(route.key, route.params);
    history.replaceState(null, '', '#' + route.path + (drawer.id ? '?p=' + drawer.id : ''));
  }

  /* ---------- property drawer ---------- */

  function openProperty(id) {
    const pid = Number(id);
    if (!Number.isInteger(pid) || pid <= 0) return;
    const base = '#' + (route.path || DEFAULT_VIEW);
    const target = base + '?p=' + pid;
    if (window.location.hash === target) {
      showDrawer(pid);
      return;
    }
    if (drawer.id) {     // already open: swap the property without adding history entries
      history.replaceState(null, '', target);
      showDrawer(pid);
      return;
    }
    drawer.pushed = true;
    window.location.hash = target;   // hashchange opens it (and Back closes it)
  }

  function syncDrawer(pid) {
    if (pid) showDrawer(pid);
    else hideDrawer();
  }

  function showDrawer(pid) {
    const el = $('#drawer');
    const body = $('#drawerBody');
    if (!el || !body) return;
    if (drawer.id === pid && !el.hidden) return;
    if (!drawer.id && !el.contains(document.activeElement)) drawer.lastFocus = document.activeElement;
    drawer.id = pid;
    $('#scrim').hidden = false;
    el.hidden = false;
    el.scrollTop = 0;
    el.classList.remove('scrolled');
    $('#drawerTitle').textContent = 'Property';
    document.body.classList.add('drawer-open');
    if (typeof BM.renderProperty === 'function') {
      Promise.resolve()
        .then(() => BM.renderProperty(body, pid))
        .catch((err) => {
          if (drawer.id === pid) body.innerHTML = errorBox(err, false);
        });
    } else {
      body.innerHTML = errorBox(new Error('The property panel did not load (property.js is missing). Reload the page.'), false);
    }
    try { el.focus({preventScroll: true}); } catch (e) { el.focus(); }
    emit('bm:drawer', {id: pid});
  }

  function hideDrawer() {
    const el = $('#drawer');
    if (!el || (el.hidden && !drawer.id)) return;
    drawer.id = null;
    drawer.pushed = false;
    el.hidden = true;
    $('#scrim').hidden = true;
    document.body.classList.remove('drawer-open');
    const back = drawer.lastFocus;
    drawer.lastFocus = null;
    if (back && back.isConnected && typeof back.focus === 'function') {
      try { back.focus({preventScroll: true}); } catch (e) { /* ignore */ }
    }
    emit('bm:drawer', {id: null});
  }

  function closeDrawer() {
    const r = parseHash();
    if (r.pid) {
      if (drawer.pushed) {
        drawer.pushed = false;
        history.back();       // hashchange hides the drawer
        return;
      }
      history.replaceState(null, '', '#' + (route.path || DEFAULT_VIEW));
    }
    hideDrawer();
  }

  /* ---------- health notices ---------- */

  function dismissed() {
    try { return JSON.parse(readStore(LS_DISMISSED, '[]')) || []; } catch (e) { return []; }
  }

  function renderNotices(list) {
    const box = $('#notices');
    if (!box) return;
    const gone = dismissed();
    box.innerHTML = list.filter((n) => !gone.includes(n.key)).map((n) =>
      '<div class="notice' + (n.bad ? ' bad' : '') + '" data-notice="' + esc(n.key) + '"><p><b>' + esc(n.text) + '</b>' +
      (n.more ? ' <span>' + esc(n.more) + '</span>' : '') + '</p>' +
      (n.sticky ? '' : '<button class="x" type="button" data-dismiss="' + esc(n.key) + '" aria-label="Dismiss">✕</button>') +
      '</div>').join('');
  }

  async function checkHealth() {
    let h;
    try { h = await api('/api/health'); } catch (e) { return; }
    state.health = h;
    const notes = [];
    if (h && h.ok === false) {
      notes.push({key: 'db', bad: true, sticky: true, text: "The database didn't open.", more: 'Details are in the server log. Restart the app on the Mac Mini.'});
    }
    if (h && h.keys && h.keys.google === false) {
      notes.push({key: 'google-key', text: 'Drive times are estimates and photos are off until you add your Google key.',
        more: 'Put GOOGLE_MAPS_API_KEY in the .env file on the Mac Mini, then restart the app.'});
    }
    if (h && h.vision_backend === 'claude_cli' && h.claude_cli === false) {
      notes.push({key: 'claude-cli', text: "Claude Code isn't set up on this computer, so the photo check can't run yet.",
        more: 'Install Claude Code on the Mac Mini and sign in once (type claude, then /login).'});
    }
    renderNotices(notes);
    emit('bm:health', h);
  }

  /* ---------- global events ---------- */

  function onClick(ev) {
    const t = ev.target;
    if (!(t instanceof Element)) return;
    const goEl = t.closest('[data-go]');
    if (goEl) {
      ev.preventDefault();
      const params = (goEl.getAttribute('data-params') || '').split('/').filter(Boolean);
      go(goEl.getAttribute('data-go'), params);
      return;
    }
    const prop = t.closest('[data-open-property]');
    if (prop) {
      ev.preventDefault();
      openProperty(prop.getAttribute('data-open-property'));
      return;
    }
    if (t.closest('[data-close-drawer]')) {
      ev.preventDefault();
      closeDrawer();
      return;
    }
    const rr = t.closest('[data-rerender]');
    if (rr && !rr.closest('#drawer')) {
      ev.preventDefault();
      refreshSummary().catch(() => {}).finally(rerender);
      return;
    }
    const dis = t.closest('[data-dismiss]');
    if (dis) {
      const key = dis.getAttribute('data-dismiss');
      const list = dismissed();
      if (!list.includes(key)) list.push(key);
      writeStore(LS_DISMISSED, JSON.stringify(list));
      const n = dis.closest('.notice');
      if (n) n.remove();
      return;
    }
    const tab = t.closest('.tab[data-key]');
    if (tab && tab.getAttribute('data-key') === route.key && !route.params.length && !drawer.id) {
      ev.preventDefault();     // tapping the current tab refreshes it
      refreshSummary().catch(() => {}).finally(rerender);
    }
  }

  function onKey(ev) {
    if (ev.key === 'Escape' && drawer.id && !loginShown) {
      const tag = (ev.target && ev.target.tagName) || '';
      if (tag === 'SELECT') return;
      ev.preventDefault();
      closeDrawer();
    }
  }

  function onVisible() {
    if (document.visibilityState === 'visible' && booted && Date.now() - summaryAt > SUMMARY_STALE_MS) {
      refreshSummary().catch(() => {});
    }
  }

  /* ---------- boot ---------- */

  async function boot() {
    document.addEventListener('click', onClick);
    document.addEventListener('keydown', onKey);
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('hashchange', onHashChange);
    $('#scrim').addEventListener('click', closeDrawer);
    $('#drawer').addEventListener('scroll', (ev) => {
      ev.currentTarget.classList.toggle('scrolled', ev.currentTarget.scrollTop > 72);
    }, {passive: true});
    $('#market').addEventListener('change', (ev) => setMarket(ev.target.value));
    checkHealth();
    try {
      await loadMarkets();
    } catch (e) {
      if (loginShown) return;
      const main = $('#main');
      main.innerHTML = '<section class="view">' + errorBox(e, 'Try again') + '</section>';
      main.querySelector('[data-rerender]').addEventListener('click', () => window.location.reload());
      return;
    }
    if (!state.markets.length) {
      $('#main').innerHTML = '<section class="view">' + empty('No markets yet',
        'Add a market (an asset class and a geography) to config.yaml under markets:, then restart the app.') + '</section>';
      return;
    }
    try { await refreshSummary(); } catch (e) { /* each screen shows its own error */ }
    booted = true;
    renderNav();
    onHashChange();
  }

  const BM = {
    api,
    state,
    registerView,
    go,
    openProperty,
    closeDrawer,
    toast,
    refreshSummary,
    rerender,
    esc,
    fmt,
    ui: {moon, levelPips, channelBadges, bars, stat, empty, loading, error: errorBox, emptyMarket, dot, coverage},
    /* extras */
    m: marketPath,
    setParams,
    LEVEL_NAMES,
    get route() { return {key: route.key, params: route.params.slice(), property: drawer.id}; },
  };
  window.BM = BM;

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
