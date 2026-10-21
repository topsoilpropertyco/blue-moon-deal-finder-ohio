/* Funnel (Insight): "Find the property" and "Reach the owner" as horizontal bar funnels, with the share
 * of each step that moved on to the next. Refreshes itself while it is on screen.
 *
 * This file loads first of the Insight and Setup screens, so it also defines BM.iv: the few helpers those
 * screens share (page heading, bars with "% move on", loading/error/empty boxes, copy-paste command
 * boxes, safe links, live refresh). The other screens check for BM.iv before they register.
 */
(() => {
  'use strict';

  const BM = window.BM;
  if (!BM || typeof BM.registerView !== 'function') {
    console.error('funnel.js needs core.js to load first.');
    return;
  }

  /* ---------- BM.iv: shared helpers for the Insight and Setup screens ---------- */

  const esc = (v) => BM.esc(v === null || v === undefined ? '' : String(v));

  function toNum(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  const num = (v) => BM.fmt.num(toNum(v) === null ? 0 : v);

  function plural(n, one, many) {
    const v = toNum(n) || 0;
    return num(v) + ' ' + (v === 1 ? one : (many || one + 's'));
  }

  function mpath(rest) {
    if (typeof BM.m === 'function') return BM.m(rest || '');
    return '/api/m/' + encodeURIComponent(BM.state.market || '') + (rest || '');
  }

  function loading(text) {
    if (BM.ui.loading) return BM.ui.loading(text);
    return '<div class="loading"><span class="spin" aria-hidden="true"></span>' + esc(text || 'Loading…') + '</div>';
  }

  function errorBox(err) {
    if (BM.ui.error) return BM.ui.error(err);
    const msg = (err && err.message) || String(err || 'Something went wrong.');
    return '<div class="errbox" role="alert"><p>' + esc(msg) + '</p>' +
      '<button class="btn small" type="button" data-rerender>Try again</button></div>';
  }

  function emptyMarket() {
    if (BM.ui.emptyMarket) return BM.ui.emptyMarket();
    return BM.ui.empty('No properties in this market yet',
      'Import a Crexi export to fill this market. Nothing is ever deleted.',
      '<button class="btn primary" type="button" data-go="import">Import a Crexi file</button>');
  }

  /** Page heading in the mockup's style: eyebrow, serif title, then one idea per line. */
  function head(eyebrow, title, lines) {
    const items = (lines || []).filter(Boolean).map((l) => '<li>' + l + '</li>').join('');
    return '<div class="vw-head"><div class="eyebrow">' + esc(eyebrow) + '</div><h1>' + esc(title) + '</h1>' +
      (items ? '<ul class="lines">' + items + '</ul>' : '') + '</div>';
  }

  /** The current market's summary (fetched if core has none yet). Never throws. */
  async function summary() {
    if (BM.state.summary && BM.state.summary.market && BM.state.summary.market.slug === BM.state.market) {
      return BM.state.summary;
    }
    try {
      return await BM.refreshSummary();
    } catch (e) {
      return BM.state.summary || null;
    }
  }

  function isEmptyMarket(s) {
    return !!s && toNum(s.properties) === 0;
  }

  function currentMarket() {
    return (BM.state.markets || []).find((m) => m.slug === BM.state.market) || null;
  }

  /** Horizontal bars like the mockup's .hb rows.
   * rows: [{label, value, cls?, hint?, title?, drop?}] ; opts: {max, unit, drops, digits}
   * With drops, a gray line before each bar says what share of the previous bar moved on (row.drop
   * replaces that text). */
  function bars(rows, opts) {
    const list = Array.isArray(rows) ? rows : [];
    const o = opts || {};
    const values = list.map((r) => toNum(r && r.value) || 0);
    const max = toNum(o.max) || Math.max(0, ...values);
    const unit = o.unit || '';
    const html = list.map((r, i) => {
      const v = values[i];
      let h = '';
      if (o.drops && i > 0) {
        const prev = values[i - 1];
        const text = r.drop || (prev > 0 ? Math.round((v / prev) * 100) + '% move on' : '');
        if (text) h += '<div class="drop num">' + esc(text) + '</div>';
      }
      const w = max > 0 ? Math.max(0, Math.min(100, (v / max) * 100)) : 0;
      const shown = toNum(r.value) === null ? '—' : BM.fmt.num(v) + unit;
      const tip = r.title || ((r.label || '') + ': ' + shown + (r.hint ? ' · ' + r.hint : ''));
      h += '<div class="hb" title="' + esc(tip) + '"><span class="lab">' + esc(r.label) +
        (r.hint ? '<small>' + esc(r.hint) + '</small>' : '') + '</span>' +
        '<div class="t"><i class="' + esc(r.cls || '') + (v === 0 ? ' zero' : '') + '" style="width:' + w.toFixed(1) + '%"></i></div>' +
        '<span class="c num">' + esc(shown) + '</span></div>';
      return h;
    }).join('');
    return '<div class="hbars">' + (html || '<p class="note">Nothing to show yet.</p>') + '</div>';
  }

  /** Only web links ever go in an href. */
  function safeUrl(u) {
    const s = String(u === null || u === undefined ? '' : u).trim();
    return /^https?:\/\//i.test(s) ? s : '';
  }

  function extLink(url, text, title) {
    const href = safeUrl(url);
    if (!href) return esc(text || '');
    return '<a href="' + esc(href) + '" target="_blank" rel="noopener noreferrer"' +
      (title ? ' title="' + esc(title) + '"' : '') + '>' + esc(text || href) + '</a>';
  }

  /** A command to copy and paste into Terminal, with a Copy button. */
  function command(text, label) {
    return '<div class="vw-cmd">' + (label ? '<div class="vw-cmd-l">' + esc(label) + '</div>' : '') +
      '<div class="vw-cmd-row"><pre><code>' + esc(text) + '</code></pre>' +
      '<button type="button" class="btn small" data-iv-copy aria-label="Copy this command">Copy</button></div></div>';
  }

  async function copyFrom(button) {
    const code = button.closest('.vw-cmd') && button.closest('.vw-cmd').querySelector('code');
    const text = code ? code.textContent : '';
    try {
      await navigator.clipboard.writeText(text);
      BM.toast('Copied. Paste it into Terminal.');
    } catch (e) {
      const range = document.createRange();   // no clipboard access (plain http on a phone): select it instead
      range.selectNodeContents(code);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      BM.toast('Selected. Copy it with your keyboard or the Copy menu.');
    }
  }

  document.addEventListener('click', (ev) => {
    const b = ev.target instanceof Element ? ev.target.closest('[data-iv-copy]') : null;
    if (b) {
      ev.preventDefault();
      copyFrom(b);
    }
  });

  /** Call refresh() when the summary changes, when the tab comes back into view, and every `every` ms,
   * for as long as root is on screen. Returns a stop function. */
  function live(root, refresh, every) {
    let busy = false;
    let timer = null;
    const stop = () => {
      clearInterval(timer);
      document.removeEventListener('bm:summary', tick);
      document.removeEventListener('visibilitychange', tick);
    };
    async function tick() {
      if (!root.isConnected) return stop();
      if (document.visibilityState !== 'visible' || busy) return undefined;
      busy = true;
      try { await refresh(); } catch (e) { /* the next tick tries again */ } finally { busy = false; }
      return undefined;
    }
    document.addEventListener('bm:summary', tick);
    document.addEventListener('visibilitychange', tick);
    timer = setInterval(tick, every || 30000);
    return stop;
  }

  BM.iv = {esc, toNum, num, plural, mpath, loading, errorBox, emptyMarket, head, summary, isEmptyMarket,
    currentMarket, bars, safeUrl, extLink, command, live};

  /* ---------- the Funnel screen ---------- */

  const HEAD = head('Funnel · live', 'From raw list to reachable owner', [
    'Updates as you review photos and move owners up a level.',
    'The gray % under each bar is how many moved on.',
  ]);

  function findRows(find) {
    return (find || []).map((s) => ({label: s.label, value: s.count}));
  }

  function reachRows(reach) {
    const list = reach || [];
    return list.map((s, i) => ({label: s.label, value: s.count, cls: i === list.length - 1 ? 'close' : ''}));
  }

  function firstCount(list) {
    return toNum(list && list[0] && list[0].count) || 0;
  }

  function hitsStrip(data) {
    const pen = toNum(data.penetration);
    const tiles = [BM.ui.stat(pen === null ? '—' : BM.fmt.pct(pen), 'Owners confirmed (penetration)')];
    (data.hits || []).forEach((h) => tiles.push(BM.ui.stat(toNum(h.count) || 0, h.label)));
    return '<div class="strip vw-strip">' + tiles.join('') + '</div>';
  }

  function body(data) {
    const find = data.find || [];
    const reach = data.reach || [];
    return '<div class="grid2">' +
      '<div class="panel"><h2>Find the property</h2>' +
        bars(findRows(find), {max: firstCount(find), drops: true}) + '</div>' +
      '<div class="panel"><h2>Reach the owner</h2>' +
        bars(reachRows(reach), {max: firstCount(reach), drops: true}) +
        '<p class="note vw-tight">Confirmed means we talked to them and they said they own it. Then it goes to the CRM.</p></div>' +
      '</div>' +
      '<h2 class="vw-sub">DRILL work so far</h2>' + hitsStrip(data) +
      '<p class="note vw-tight" data-iv-updated>Updated ' + esc(new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})) + '.</p>';
  }

  async function render(root) {
    root.innerHTML = HEAD + loading('Counting the funnel…');
    let data;
    try {
      data = await BM.api(mpath('/funnel'));
    } catch (err) {
      if (root.isConnected) root.innerHTML = HEAD + errorBox(err);
      return;
    }
    if (!root.isConnected) return;
    if (!firstCount(data && data.find)) {
      root.innerHTML = HEAD + emptyMarket();
      return;
    }
    root.innerHTML = HEAD + '<div data-iv-body>' + body(data) + '</div>';
    live(root, async () => {
      const fresh = await BM.api(mpath('/funnel'));
      const box = root.querySelector('[data-iv-body]');
      if (box && fresh && firstCount(fresh.find)) box.innerHTML = body(fresh);
    }, 20000);
  }

  BM.registerView('funnel', {label: 'Funnel', group: 'insight', order: 1, render});
})();
