/* The DRILL screens.
 *
 *   #drill            the board: penetration, the reach-out card, credits, and the stage pipeline
 *   #drill/validate   reach out on untested phones, emails and social accounts, one owner at a time
 *   #drill/<stage>    work one stage's queue, one owner at a time: open the links, check it off
 *
 * Also defines BM.work: small helpers shared with credits.js and review.js (loaded after this file).
 */
(() => {
  'use strict';

  const BM = window.BM;
  if (!BM || typeof BM.registerView !== 'function') {
    console.error('drill.js needs core.js to load first.');
    return;
  }

  // ---------------------------------------------------------------------------------------------
  // Shared helpers for the work screens (BM.work)
  // ---------------------------------------------------------------------------------------------

  const esc = (v) => BM.esc(v == null ? '' : String(v));
  const num = (n) => BM.fmt.num(Number(n) || 0);
  const W = (BM.work = BM.work || {});

  /** API path inside the current market: W.mpath('/drill') -> /api/m/<slug>/drill */
  W.mpath = (rest = '') => `/api/m/${encodeURIComponent(BM.state.market || '')}${rest}`;

  W.errText = (err) => (err && err.message) || String(err || 'Something went wrong.');

  W.isTyping = (el) => !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

  /** True while the property drawer (or any dialog) is open: work-screen shortcuts pause then. */
  W.overlayOpen = () => !!document.querySelector(
    '.drawer:not([hidden]), .drawer.open, [aria-modal="true"]:not([hidden])');

  /** Shortcut keys apply only with no modifier, no focused text field and no open drawer. */
  W.keysBlocked = (e) => e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented ||
    W.isTyping(e.target) || W.overlayOpen();

  /** The current screen: {key, params}. core.js keeps it (the hash may also carry ?p=<property>). */
  W.route = () => {
    if (BM.route && typeof BM.route.key === 'string') return { key: BM.route.key, params: BM.route.params || [] };
    const [key = '', ...params] = (location.hash || '').replace(/^#/, '').split('?')[0].split('/');
    return { key, params: params.filter(Boolean) };
  };

  /** Only web, phone, text and mail links are ever put in an href. */
  W.safeUrl = (u) => {
    const s = String(u == null ? '' : u).trim();
    return /^(https?:\/\/|tel:|sms:|mailto:)/i.test(s) ? s : '';
  };

  W.extLink = (url, inner, cls = '') => {
    const u = W.safeUrl(url);
    return u ? `<a class="${cls}" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${inner}</a>` : '';
  };

  // Buttons with data-go="view" (and data-params="a/b") navigate: core.js handles those clicks.

  W.loading = (text) => (BM.ui.loading ? BM.ui.loading(text)
    : `<div class="panel dr-loading" role="status" aria-live="polite"><span class="dr-spin" aria-hidden="true"></span>${esc(text || 'Loading…')}</div>`);

  /** An error with a Try again button (core.js re-renders the screen on data-rerender). */
  W.errorBox = (err) => (BM.ui.error ? BM.ui.error(err)
    : `<div class="panel dr-error" role="alert"><p class="note">${esc(W.errText(err))}</p>
         <div class="bar"><button class="btn" data-act="retry">Try again</button></div></div>`);

  W.emptyMarket = (text) => BM.ui.empty('No properties in this market yet', text,
    '<button class="btn primary" type="button" data-go="import">Import a Crexi file</button>');

  W.toastError = (err) => BM.toast(W.errText(err), { error: true });

  /** 'EXTERIOR_CORRIDOR' -> 'Exterior corridor' */
  W.labelText = (code) => {
    const s = String(code || '').replace(/_/g, ' ').trim().toLowerCase();
    return s ? s[0].toUpperCase() + s.slice(1) : '';
  };

  /** 'SPRINGFIELD' -> 'Springfield' (ALL-CAPS record text only; mixed case is left alone). */
  W.titleCase = (s) => (BM.fmt.title ? BM.fmt.title(s || '')
    : String(s || '').toLowerCase().replace(/\b[a-z]/g, (c) => c.toUpperCase()));

  W.address = (it) => W.titleCase(it.address || it.name || '') || `Property ${it.property_id || it.id}`;

  /** 2812 -> '2.8k' for tab badges. */
  W.compact = (n) => {
    const v = Number(n) || 0;
    if (v >= 10000) return `${Math.round(v / 1000)}k`;
    if (v >= 1000) return `${(v / 1000).toFixed(1).replace(/\.0$/, '')}k`;
    return String(v);
  };

  /** 0..1 -> '42%' (whole percent; hit rates and shares). */
  W.pct0 = (p) => (p == null || Number.isNaN(Number(p)) ? '—' : `${Math.round(Number(p) * 100)}%`);

  W.moonScore = (p, size = 20) => (p == null
    ? '<span class="muted">Not scored yet</span>'
    : `<span class="dr-moon">${BM.ui.moon(Number(p), size)}<span class="num">${esc(BM.fmt.pct(Number(p)))}</span></span>`);

  const OWNER_TYPES = {
    individual: 'Individual', trust: 'Trust', estate: 'Estate or heirs', small_llc: 'Small LLC',
    corporation: 'Corporation', portfolio: 'Portfolio owner', institutional: 'Institution',
    government: 'Government', nonprofit: 'Nonprofit', unknown: 'Unknown type',
  };
  W.ownerType = (t) => OWNER_TYPES[t] || W.labelText(t) || 'Unknown type';

  /** 'Springfield, OH 45503' */
  W.place = (it) => [W.titleCase(it.city), [it.state, it.zip].filter(Boolean).join(' ')].filter(Boolean).join(', ');

  W.monthName = (ym) => {
    const m = /^(\d{4})-(\d{2})/.exec(ym || '');
    if (!m) return ym || 'this month';
    return new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleString(undefined, { month: 'long', year: 'numeric' });
  };

  W.when = (iso) => {
    if (!iso) return '';
    try { return (BM.fmt.ago ? BM.fmt.ago(iso) : BM.fmt.date(iso)) || ''; } catch (_) { return String(iso).slice(0, 10); }
  };

  W.debounce = (fn, ms) => {
    let t = null;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  };

  /** Tab badges and top-bar numbers catch up a moment after a burst of check-offs. */
  W.refreshSoon = W.debounce(() => {
    try { Promise.resolve(BM.refreshSummary && BM.refreshSummary()).catch(() => {}); } catch (_) { /* ignore */ }
  }, 1500);

  /** Values of every [data-f] field inside el, trimmed. */
  W.fields = (el) => {
    const out = {};
    if (el) el.querySelectorAll('[data-f]').forEach((f) => { out[f.dataset.f] = (f.value || '').trim(); });
    return out;
  };

  /** Contact points typed into a form: [{channel, value}] for the filled ones. */
  W.contactsFrom = (f) => [['phone', f.phone], ['email', f.email], ['social', f.social], ['mail', f.mail]]
    .filter(([, v]) => v).map(([channel, value]) => ({ channel, value }));

  /** Copy text; works on plain http (e.g. over Tailscale) where the clipboard API is missing. */
  W.copy = async (text) => {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      BM.toast(`Copied ${text}`);
    } catch (_) {
      BM.toast('Could not copy. Select the text and copy it by hand.');
    }
  };

  /** Scroll so the top of el is visible under the sticky top bar. */
  W.scrollToTop = (el) => {
    if (!el) return;
    const top = el.getBoundingClientRect().top;
    if (top < 60 || top > window.innerHeight * 0.6) window.scrollTo({ top: Math.max(0, window.scrollY + top - 84) });
  };

  // ---------------------------------------------------------------------------------------------
  // DRILL constants
  // ---------------------------------------------------------------------------------------------

  const PAGE = 20;
  const COST = { free: ['Free', 'free'], credit: ['Crexi credit', 'credit'], paid: ['Paid', 'paid'] };
  const AUTOMATION = { auto: 'Runs by itself', assisted: 'Assisted: the app opens the pages', manual: 'By hand' };
  const OUTCOME_TEXT = { found: 'Found', nothing: 'Nothing found', skipped: 'Skipped' };
  const CHANNEL_TEXT = { phone: 'Call', text: 'Text', email: 'Email', social: 'Social', vm: 'Voicemail', mail: 'Mail' };
  const STATUS_CLS = { verified: 'v', untested: 'u', wrong: 'w', dnc: 'w dnc' };
  const STATUS_TEXT = { verified: 'works', untested: 'not tested yet', wrong: 'wrong', dnc: 'do not contact' };
  const OFF_TEXT = 'Off - turn on in config/drill.yaml when you decide to pay';
  const TCPA = 'Calls and texts to cell phones fall under telemarketing rules (TCPA, Do Not Call). Check the number before you call or text.';
  const PASS_MARKER = '_new_pass';

  const ROLES = [
    ['owner_of_record', 'Owner of record'],
    ['possible_owner', 'Possible owner'],
    ['member', 'LLC member or manager'],
    ['officer', 'Officer'],
    ['trustee', 'Trustee'],
    ['statutory_agent', 'Statutory agent'],
    ['deed_signer', 'Signed the deed or mortgage'],
    ['heir', 'Heir or estate contact'],
    ['manager', 'Property manager (not the owner)'],
    ['other', 'Other'],
  ];
  const ROLE_TEXT = Object.fromEntries([...ROLES,
    ['confirmed_owner', 'Confirmed owner'], ['portfolio_owner', 'Owner of a sibling property']]);
  // The role a person found at each stage usually has (the engine uses the same defaults).
  const ROLE_DEFAULT = {
    records_baseline: 'owner_of_record', county_auditor: 'owner_of_record', entity_unmask: 'member',
    recorder_documents: 'deed_signer', property_contacts: 'manager', court_records: 'heir',
  };
  const SOURCE_TEXT = { you: 'you', manual: 'you', conversation: 'a conversation', crexi_export: 'the Crexi export' };

  // Used only if the API sends no outcome list (they normally come from config/drill.yaml).
  const FALLBACK_OUTCOMES = [
    { key: 'no_answer', label: 'No answer', effect: 'none' },
    { key: 'left_message', label: 'Left a message', effect: 'none' },
    { key: 'replied_unconfirmed', label: 'Replied, not confirmed yet', effect: 'none' },
    { key: 'wrong_person', label: 'Wrong person', effect: 'mark_wrong' },
    { key: 'bounced', label: 'Bounced or disconnected', effect: 'mark_wrong' },
    { key: 'reached_confirmed', label: 'Talked to owner, confirmed', effect: 'confirm' },
    { key: 'do_not_contact', label: 'Asked us not to contact', effect: 'dnc' },
  ];

  // ---------------------------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------------------------

  const S = {
    root: null,
    market: null,
    seq: 0,           // guards against a slow response painting over a newer screen
    mode: 'board',    // board | work
    overview: null,   // last GET /drill (also used for stage numbers and the queue switcher)
    q: null,          // the open work queue (see newQueue)
  };

  const newQueue = (key) => ({
    key, stage: null, items: [], total: 0, idx: 0, done: 0,
    form: false, busy: false, loadingMore: null, logged: new Set(), message: '',
  });

  // ---------------------------------------------------------------------------------------------
  // Rendering entry point
  // ---------------------------------------------------------------------------------------------

  async function render(root, params = []) {
    S.root = root;
    if (S.market !== BM.state.market) {
      S.market = BM.state.market;
      S.overview = null;
      S.q = null;
    }
    const key = params[0] || '';
    return key ? renderWork(key) : renderBoard();
  }

  function paint(html) {
    if (!S.root) return null;
    S.root.innerHTML = `<div class="dr dr-drill">${html}</div>`;
    const el = S.root.firstElementChild;
    el.addEventListener('click', onClick);
    el.addEventListener('submit', onSubmit);
    el.addEventListener('change', onChange);
    return el;
  }

  // ---------------------------------------------------------------------------------------------
  // The board
  // ---------------------------------------------------------------------------------------------

  async function renderBoard() {
    const seq = ++S.seq;
    S.mode = 'board';
    paint(S.overview ? boardHtml(S.overview) : boardHead() + W.loading('Loading the DRILL board…'));
    try {
      const ov = await BM.api(W.mpath('/drill'));
      if (seq !== S.seq) return;
      S.overview = ov || {};
      paint(boardHtml(S.overview));
    } catch (err) {
      if (seq !== S.seq) return;
      paint(boardHead() + W.errorBox(err));
    }
  }

  const levelName = (n) => (S.overview && S.overview.level_names && S.overview.level_names[n]) ||
    { 1: 'No owner data', 2: 'Possible owner', 3: 'Confirmed owner' }[n];

  function boardHead() {
    return `
      <div class="eyebrow">Owner contact · the DRILL</div>
      <h1>Drill until every owner is confirmed</h1>
      <ul class="lines">
        <li>Others blast a list and hope. We drill, and drill again, until we have talked to every owner.</li>
        <li><b>Level 1</b> ${esc(levelName(1))} · <b>Level 2</b> ${esc(levelName(2))} · <b>Level 3</b> ${esc(levelName(3))}.</li>
        <li>Only a real conversation confirms an owner. Then it goes to the CRM.</li>
      </ul>`;
  }

  function boardHtml(ov) {
    if (!ov.totals || !ov.totals.properties) {
      return boardHead() + W.emptyMarket('Every owner then gets a contact level and a next step here.');
    }
    return boardHead() + statsStrip(ov) + reachCard(ov) +
      `<div class="grid2 dr-grid">${penetrationPanel(ov)}${creditsPanel(ov.credits || {})}</div>` +
      pipelinePanel(ov);
  }

  /** Owners whose next step is to reach out on untested contact info. */
  const waitingToReach = (ov) => (ov.validate && ov.validate.waiting != null
    ? Number(ov.validate.waiting) || 0 : queueCount(ov, 'validate'));

  function statsStrip(ov) {
    const st = ov.statuses || ov.status_counts || {};
    const tiles = [
      [BM.fmt.pct(Number(ov.penetration) || 0), 'Owners confirmed (penetration)'],
      [num(waitingToReach(ov)), 'Owners with contact info to test'],
      [num(st.active), 'Open owners in the drill'],
      [num(ov.totals && ov.totals.handoff_waiting), 'Confirmed, ready for the CRM'],
      [num(st.exhausted), 'Every source tried'],
      [num(st.dnc), 'Do not contact'],
    ];
    return `<div class="strip dr-strip">${tiles.map(([v, l]) => BM.ui.stat(v, l)).join('')}</div>`;
  }

  function reachCard(ov) {
    const waiting = waitingToReach(ov) || 0;
    const v = ov.validate || {};
    const counts = [
      ['u', v.untested, 'not tested yet'], ['v', v.verified, 'working'],
      ['w', v.wrong, 'wrong'], ['w dnc', v.dnc, 'do not contact'],
    ].filter(([, n]) => n).map(([cls, n, label]) =>
      `<span class="dr-count"><span class="ch ${cls}">${num(n)}</span> ${label}</span>`).join('');
    const countsLine = counts ? `<div class="dr-counts"><span>Phones, emails and social accounts:</span>${counts}</div>` : '';
    return `
      <section class="panel dr-reach${waiting ? ' on' : ''}" aria-label="Reach out">
        <div class="dr-reach-text">
          <div class="eyebrow">Reach out · confirmations move penetration</div>
          <h2>${waiting ? `${num(waiting)} ${waiting === 1 ? 'owner has' : 'owners have'} contact info to test`
                       : 'No contact info to test right now'}</h2>
          <p class="note">${waiting
            ? 'Call, text or email them, then log what happened. A real conversation is the only thing that confirms an owner.'
            : 'When a stage turns up a phone, email or social account, the owner lands here so you can reach out.'}</p>
          ${countsLine}
        </div>
        ${waiting ? '<button class="btn primary dr-big" data-go="drill" data-params="validate">Start reaching out</button>' : ''}
      </section>`;
  }

  function penetrationPanel(ov) {
    const pd = ov.penetration_detail || {};
    const inBox = pd.basis === 'in_buybox' && ov.levels_in_buybox;
    const lv = (inBox ? ov.levels_in_buybox : ov.levels) || {};
    const at = (n) => Number(lv[n] ?? lv[String(n)]) || 0;
    const rows = [
      { label: `Level 1 · ${levelName(1)}`, value: at(1) },
      { label: `Level 2 · ${levelName(2)}`, value: at(2) },
      { label: `Level 3 · ${levelName(3)}`, value: at(3), cls: 'close good' },
    ];
    const max = Math.max(1, at(1) + at(2) + at(3));
    return `
      <section class="panel" aria-label="Penetration">
        <h2>Penetration</h2>
        <div class="dr-hero-num"><span class="big num">${esc(BM.fmt.pct(Number(ov.penetration) || 0))}</span>
          <span class="note">${esc(pd.text || '')}${pd.text ? ' · ' : ''}goal 100%</span></div>
        ${BM.ui.bars(rows, { max })}
        <p class="note">${inBox ? 'Owners of properties in the buy box. ' : ''}Do-not-contact owners are left out.</p>
      </section>`;
  }

  function creditsPanel(c) {
    const per = Number(c.per_month) || 0;
    const left = Number(c.left) || 0;
    const line = per
      ? (left ? `Use about <b>${num(c.daily_pace)} a day</b> for the next ${num(c.days_left)} ${c.days_left === 1 ? 'day' : 'days'} to spend them all. They don't roll over.`
              : 'All used for this month. New ones arrive on the 1st.')
      : esc(c.message || 'No Crexi credits are set up.');
    return `
      <section class="panel" aria-label="Crexi credits">
        <h2>Crexi credits · ${esc(W.monthName(c.month))}</h2>
        <div class="dr-hero-num"><span class="big num">${num(left)}</span><span class="note">of ${num(per)} left</span></div>
        <p class="note dr-tight">${line}</p>
        <p class="note dr-tight">The credit plan picks the best possible owners the free stages could not reach.</p>
        <div><button class="btn" data-go="credits">Open the credit plan</button></div>
      </section>`;
  }

  function costChip(cost) {
    const [label, cls] = COST[cost] || [W.labelText(cost) || 'Free', 'free'];
    return `<span class="dr-cost ${cls}">${esc(label)}</span>`;
  }

  /** What to put in a stage row's last column. */
  function stageAction(row, firstOpen) {
    if (row.enabled === false) return `<span class="pill" title="${esc(OFF_TEXT)}">Off</span>`;
    if (row.automation === 'auto') {
      return row.queue
        ? `<button class="btn" data-go="drill" data-params="${esc(row.stage_key)}">Check ${num(row.queue)}</button>`
        : '<span class="pill">Runs by itself</span>';
    }
    if (!row.queue) return '<span class="muted">Nobody waiting</span>';
    return `<button class="btn${row.stage_key === firstOpen ? ' primary' : ''}" data-go="drill" data-params="${esc(row.stage_key)}">Work this · ${num(row.queue)}</button>`;
  }

  function unresolvedHtml(row) {
    const n = Number(row.unresolved) || 0;
    if (!n) return '<span class="muted num">0</span>';
    const waiting = Number(row.queue) || 0;
    return waiting
      ? `<span class="dr-n warn num">${num(n)}</span><div class="note">${num(waiting)} waiting here</div>`
      : `<span class="dr-n bad num">${num(n)}</span><div class="note">moved on</div>`;
  }

  function hitHtml(row) {
    const guess = row.hit_rate_hypothesis == null ? '' : `guess ${W.pct0(row.hit_rate_hypothesis)}`;
    if (row.hit_rate_actual == null) {
      return `<span class="muted">—</span>${guess ? `<div class="note">${guess}</div>` : ''}`;
    }
    return `<b class="num">${W.pct0(row.hit_rate_actual)}</b><div class="note">${guess}${guess ? ' · ' : ''}${num(row.tried)} tried</div>`;
  }

  function stageNote(row) {
    if (row.enabled === false) return `<div class="note dr-off-note">${esc(OFF_TEXT)}</div>`;
    return `<div class="note">${esc(AUTOMATION[row.automation] || '')}</div>`;
  }

  function pipelinePanel(ov) {
    const rows = (ov.pipeline || []).map((r) => ({ ...r, queue: r.queue ?? queueCount(ov, r.stage_key) }));
    if (!rows.length) return '';
    const first = rows.find((r) => r.enabled !== false && r.automation !== 'auto' && r.queue);
    const firstOpen = first ? first.stage_key : null;
    const table = rows.map((r, i) => `
      <tr class="${r.enabled === false ? 'off' : ''}">
        <td class="num dr-idx">${i + 1}</td>
        <td><div class="dr-sname">${esc(r.name)}</div>${stageNote(r)}</td>
        <td>${costChip(r.cost)}</td>
        <td class="num c">${num(r.input)}</td>
        <td class="num c"><span class="dr-n ${r.resolved ? 'good' : 'zero'}">${num(r.resolved)}</span></td>
        <td class="num c">${unresolvedHtml(r)}</td>
        <td class="num c">${hitHtml(r)}</td>
        <td class="c">${stageAction(r, firstOpen)}</td>
      </tr>`).join('');
    const cards = rows.map((r, i) => stageCard(r, i, firstOpen)).join('');
    return `
      <section class="panel dr-pipe" aria-label="The drill, stage by stage">
        <h2>The drill, stage by stage</h2>
        <ul class="lines dr-tight">
          <li>Free first. Each stage only gets the owners the stages before it could not resolve.</li>
          <li>Hit rates start as guesses and become your real numbers as you check owners off.</li>
        </ul>
        <div class="dr-scroll dr-wide-only">
          <table class="wt dr-pipe-table">
            <thead><tr>
              <th>#</th><th>Stage</th><th>Cost</th><th class="c">In</th><th class="c">Resolved</th>
              <th class="c">Unresolved</th><th class="c">Hit rate</th><th class="c"><span class="dr-sr">Action</span></th>
            </tr></thead>
            <tbody>${table}</tbody>
          </table>
        </div>
        <div class="dr-stagecards dr-narrow-only">${cards}</div>
      </section>`;
  }

  /** One stage as a card (phones). Stages nobody has reached yet get one short line. */
  function stageCard(r, i, firstOpen) {
    const top = `<div class="dr-stagecard-top"><span class="dr-idx num">${i + 1}</span><b>${esc(r.name)}</b>${costChip(r.cost)}</div>`;
    const guess = r.hit_rate_hypothesis == null ? '' : `guess ${W.pct0(r.hit_rate_hypothesis)}`;
    if (!Number(r.input) && !r.queue) {
      const why = r.enabled === false ? esc(OFF_TEXT) : `Nobody has reached this stage yet${guess ? ` · ${guess}` : ''}`;
      return `<div class="dr-stagecard quiet${r.enabled === false ? ' off' : ''}">${top}<div class="note">${why}</div></div>`;
    }
    const unresolved = Number(r.unresolved) || 0;
    const uCls = !unresolved ? 'zero' : r.queue ? 'warn' : 'bad';
    return `
      <div class="dr-stagecard${r.enabled === false ? ' off' : ''}">
        ${top}
        <div class="dr-stagecard-nums num">
          <span>${num(r.input)} in</span>
          <span class="dr-n ${r.resolved ? 'good' : 'zero'}">${num(r.resolved)} resolved</span>
          <span class="dr-n ${uCls}">${num(unresolved)} unresolved</span>
        </div>
        <div class="note">Hit rate ${r.hit_rate_actual == null ? 'not measured yet' : W.pct0(r.hit_rate_actual)}${guess ? ` · ${guess}` : ''}${r.queue ? ` · ${num(r.queue)} waiting` : ''}</div>
        ${r.enabled === false ? `<div class="note dr-off-note">${esc(OFF_TEXT)}</div>` : ''}
        <div class="dr-stagecard-act">${stageAction(r, firstOpen)}</div>
      </div>`;
  }

  function queueCount(ov, key) {
    const q = (ov.queues || []).find((x) => (x.key || x.stage_key) === key);
    return q ? Number(q.count) || 0 : 0;
  }

  // ---------------------------------------------------------------------------------------------
  // Work mode: one owner at a time
  // ---------------------------------------------------------------------------------------------

  async function renderWork(key) {
    const seq = ++S.seq;
    S.mode = 'work';
    const same = S.q && S.q.key === key;
    if (!same) S.q = newQueue(key);
    const q = S.q;
    q.form = false;
    if (!same || !q.items.length) paint(W.loading('Loading the queue…'));
    const [queue, ov] = await Promise.allSettled([
      fetchQueue(q, Math.max(PAGE, q.items.length)),
      S.overview ? Promise.resolve(S.overview) : BM.api(W.mpath('/drill')),
    ]);
    if (seq !== S.seq) return;
    if (ov.status === 'fulfilled' && ov.value) S.overview = ov.value;
    if (queue.status === 'rejected') {
      paint(`<div class="dr-crumbs"><button class="dr-back" data-go="drill">← DRILL board</button></div>${W.errorBox(queue.reason)}`);
      return;
    }
    applyQueue(q, queue.value);
    paintWork();
  }

  function fetchQueue(q, limit, offset = 0) {
    return BM.api(W.mpath(`/drill/queue/${encodeURIComponent(q.key)}?limit=${Math.min(200, limit)}&offset=${offset}`));
  }

  /** Replace the loaded window with a fresh first page, staying on the same owner if still waiting. */
  function applyQueue(q, data) {
    const current = q.items[q.idx] && q.items[q.idx].property_id;
    q.stage = (data && data.stage) || q.stage || { key: q.key, name: W.labelText(q.key) };
    q.items = (data && data.items) || [];
    q.total = Math.max(Number(data && data.total) || 0, q.items.length);
    q.message = (data && data.message) || '';
    const at = q.items.findIndex((it) => it.property_id === current);
    q.idx = at >= 0 ? at : Math.min(q.idx, q.items.length);
  }

  /** Load the next page. Owners already handled leave the server's queue, so the next page starts after
   *  what is loaded here. One request at a time: callers share the one in flight. */
  function loadMore(q) {
    if (q.items.length >= q.total) return Promise.resolve();
    if (!q.loadingMore) {
      q.loadingMore = fetchQueue(q, PAGE, q.items.length)
        .then((data) => {
          const seen = new Set(q.items.map((it) => it.property_id));
          ((data && data.items) || []).forEach((it) => { if (!seen.has(it.property_id)) q.items.push(it); });
          q.total = Math.max(Number(data && data.total) || 0, q.items.length);
        })
        .catch(() => { /* the next advance tries again */ })
        .finally(() => { q.loadingMore = null; });
    }
    return q.loadingMore;
  }

  /** Make sure the owner at position `want` is loaded when the server has more (waits for a prefetch). */
  async function fillTo(q, want) {
    let misses = 0;
    while (want >= q.items.length && q.items.length < q.total && misses < 2) {
      const had = q.items.length;
      await loadMore(q);
      misses = q.items.length > had ? 0 : misses + 1;
    }
  }

  function prefetch(q) {
    if (q.items.length - q.idx < 5) loadMore(q);
  }

  const currentItem = () => (S.q ? S.q.items[S.q.idx] : null);

  function paintWork() {
    const q = S.q;
    const el = paint(workHtml(q));
    if (q.form && el) {
      const f = el.querySelector('[data-f="person"]');
      if (f) f.focus();
    }
    return el;
  }

  function workHtml(q) {
    const st = q.stage || { key: q.key, name: q.key };
    const it = q.items[q.idx];
    let body;
    if (BM.state.summary && BM.state.summary.properties === 0) {
      body = W.emptyMarket('Then every owner gets a next step, and this queue fills up.');
    } else if (st.enabled === false) body = offPanel(st);
    else if (!it) body = queueEnd(q, st);
    else body = (st.automation === 'auto' ? autoNote() : '') + ownerCard(q, st, it);
    return workHead(q, st) + body;
  }

  function stageNumber(key) {
    const list = (S.overview && S.overview.pipeline) || [];
    const i = list.findIndex((r) => r.stage_key === key);
    return i >= 0 ? [i + 1, list.length] : null;
  }

  function stageName(key) {
    const row = ((S.overview && S.overview.pipeline) || []).find((r) => r.stage_key === key);
    return row ? row.name : '';
  }

  /** A stage's method, one sentence per line. */
  function methodLines(text) {
    return String(text || '').replace(/([.!?])\s+/g, '$1\u0000').split('\u0000')
      .map((s) => s.trim()).filter(Boolean).map((s) => `<li>${esc(s)}</li>`).join('');
  }

  function stageSwitcher(current) {
    const qs = (S.overview && S.overview.queues) || [];
    const opts = qs.filter((x) => {
      const k = x.key || x.stage_key;
      return k === current || (x.enabled !== false && x.automation !== 'auto' && x.count);
    });
    if (opts.length < 2) return '';
    return `<label class="dr-switch-wrap"><span class="dr-sr">Jump to another queue</span>
      <select class="dr-switch">${opts.map((x) => {
        const k = x.key || x.stage_key;
        const count = k === current && S.q ? S.q.total : x.count;   // the open queue's count is the freshest
        return `<option value="${esc(k)}" ${k === current ? 'selected' : ''}>${esc(x.name)} (${num(count)})</option>`;
      }).join('')}</select></label>`;
  }

  function workHead(q, st) {
    const isV = st.key === 'validate';
    const n = stageNumber(st.key);
    const eyebrow = isV ? 'DRILL · reach out and confirm' : `DRILL · stage ${n ? `${n[0]} of ${n[1]}` : ''}`;
    const method = isV
      ? '<li>Reach out on each untested contact and log what happened.</li><li>Confirmations are what move penetration.</li>'
      : methodLines(st.method);
    return `
      <div class="dr-crumbs"><button class="dr-back" data-go="drill">← DRILL board</button>${stageSwitcher(st.key)}</div>
      <div class="eyebrow">${esc(eyebrow)}</div>
      <h1>${esc(st.name || W.labelText(st.key))}</h1>
      <ul class="lines">${method}</ul>
      <div class="dr-chips">${costChip(st.cost || 'free')}${st.automation ? `<span class="pill">${esc(AUTOMATION[st.automation] || st.automation)}</span>` : ''}</div>
      ${progressHtml(q)}`;
  }

  function progressHtml(q) {
    const left = Number(q.total) || 0;
    const all = left + q.done;
    const w = all ? Math.round((q.done / all) * 100) : 0;
    const where = left ? `Owner ${num(Math.min(q.idx + 1, left))} of ${num(left)} waiting` : 'Nobody waiting';
    return `
      <div class="dr-prog">
        <div class="dr-progress" role="progressbar" aria-label="Done this session" aria-valuemin="0" aria-valuemax="${all}" aria-valuenow="${q.done}"><i style="width:${w}%"></i></div>
        <div class="note num">${where}${q.done ? ` · ${num(q.done)} done this session` : ''}</div>
      </div>`;
  }

  function offPanel(st) {
    return BM.ui.empty(`${st.name || 'This stage'} is off`,
      `${OFF_TEXT}. Nothing is charged while it is off.`,
      '<button class="btn" data-go="drill">Back to the DRILL board</button>');
  }

  function autoNote() {
    return `<p class="note dr-banner">This stage runs by itself when the DRILL step runs (Run screen). You can also check owners off here by hand.
      <button class="btn" data-go="run">Open the Run screen</button></p>`;
  }

  /** The next queue with owners waiting, for the 'queue is empty' screen. */
  function nextBusyQueue(after) {
    const qs = ((S.overview && S.overview.queues) || [])
      .filter((x) => x.enabled !== false && x.automation !== 'auto' && x.count && (x.key || x.stage_key) !== after);
    return qs[0] || null;
  }

  function queueEnd(q, st) {
    if (q.total > 0) {
      return BM.ui.empty('End of the list',
        `${num(q.total)} ${q.total === 1 ? 'owner you passed over is' : 'owners you passed over are'} still waiting here.`,
        '<button class="btn primary" data-act="restart">Back to the first owner</button> <button class="btn" data-go="drill">DRILL board</button>');
    }
    const isV = st.key === 'validate';
    const nxt = nextBusyQueue(st.key);
    const title = isV ? 'Nobody to reach out to right now' : `Nobody is waiting on ${st.name || 'this stage'}`;
    const text = isV
      ? 'When a stage finds a phone, email or social account, the owner shows up here so you can reach out and confirm.'
      : `${q.done ? `Nice work: you checked off ${num(q.done)} ${q.done === 1 ? 'owner' : 'owners'}. ` : ''}Every owner has moved past this stage, or it does not apply to them.`;
    const go = nxt
      ? `<button class="btn primary" data-go="drill" data-params="${esc(nxt.key || nxt.stage_key)}">Next: ${esc(nxt.name)} · ${num(nxt.count)}</button> `
      : '';
    return BM.ui.empty(title, text, `${go}<button class="btn" data-go="drill">Back to the DRILL board</button>`);
  }

  function ownerCard(q, st, it) {
    const isV = st.key === 'validate';
    return `
      ${navBar(q)}
      <div class="dr-work">
        <div class="dr-col">
          ${propertyPanel(it)}
          ${isV ? validatePanel(q, st, it) : linksPanel(it) + checkPanel(q, st)}
        </div>
        <div class="dr-col dr-side">
          ${knownPanel(it)}
          ${historyPanel(it)}
        </div>
      </div>
      <p class="note dr-keys">Keys: ${isV ? '' : '<kbd>F</kbd> found · <kbd>N</kbd> nothing · <kbd>S</kbd> skip · '}<kbd>J</kbd> next owner · <kbd>K</kbd> previous${isV ? '' : ' · <kbd>O</kbd> open the first link'}</p>`;
  }

  function navBar(q) {
    const atEnd = q.idx >= q.items.length - 1 && q.items.length >= q.total;
    return `
      <div class="dr-nav">
        <button class="btn" data-act="prev" ${q.idx <= 0 ? 'disabled' : ''}>← Previous <kbd>K</kbd></button>
        <button class="btn" data-act="open-property">All details</button>
        <button class="btn" data-act="next" ${atEnd ? 'disabled' : ''}>Pass for now <kbd>J</kbd> →</button>
      </div>`;
  }

  function mailingText(it) {
    const cityLine = [W.titleCase(it.mailing_city), [it.mailing_state, it.mailing_zip].filter(Boolean).join(' ')]
      .filter(Boolean).join(', ');
    return [it.mailing_address, cityLine].filter(Boolean).join(', ');
  }

  function propertyPanel(it) {
    const meta = [
      it.units ? `${num(it.units)} units` : '',
      it.year_built ? `built ${it.year_built}` : '',
      it.rank ? `rank #${num(it.rank)}` : (it.in_buybox === false ? 'outside the buy box' : ''),
    ].filter(Boolean).join(' · ');
    const mailing = mailingText(it);
    const score = it.score == null
      ? '<div class="dr-score"><div><div class="dr-score-v muted">—</div><div class="note">Not scored yet</div></div></div>'
      : `<div class="dr-score" title="Blue Moon score: how worth chasing this owner is">${BM.ui.moon(Number(it.score), 44)}
           <div><div class="dr-score-v num">${esc(BM.fmt.pct(Number(it.score)))}</div><div class="note">Blue Moon</div></div></div>`;
    const stageUrls = new Set((it.links || []).map((l) => l.url));   // already shown under "Open these"
    const plinks = (it.property_links || []).filter((l) => !stageUrls.has(l.url))
      .map((l) => W.extLink(l.url, `${esc(l.label || 'Open')} ↗`, 'chip dr-chiplink')).join('');
    return `
      <section class="panel dr-prop" aria-label="Property">
        <div class="dr-prop-top">
          <div class="dr-prop-id">
            <div class="eyebrow">${esc(W.place(it))}${it.county ? ` · ${esc(W.titleCase(it.county))} County` : ''}</div>
            <h2 class="dr-addr">${esc(W.address(it))}</h2>
            <div class="dr-prop-meta">${BM.ui.levelPips(it.level, it.status)}${meta ? `<span class="note">${esc(meta)}</span>` : ''}</div>
          </div>
          ${score}
        </div>
        <dl class="kv">
          <dt>Owner on record</dt>
          <dd>${it.owner_name ? esc(it.owner_name) : '<span class="muted">Not on record</span>'} <span class="pill">${esc(W.ownerType(it.owner_type))}</span></dd>
          <dt>Best-guess person</dt>
          <dd>${it.best_guess_person ? `<b>${esc(it.best_guess_person)}</b>` : '<span class="muted">Nobody yet</span>'}</dd>
          ${it.apn ? `<dt>Parcel number</dt><dd><span class="dr-mono">${esc(it.apn)}</span> <button class="dr-mini" data-act="copy" data-copy="${esc(it.apn)}">Copy</button></dd>` : ''}
          ${mailing ? `<dt>Owner mailing</dt><dd>${esc(mailing)}</dd>` : ''}
        </dl>
        ${plinks ? `<div class="dr-plinks">${plinks}</div>` : ''}
      </section>`;
  }

  function linkKind(l) {
    if (l.deep_link) return ['dr-k-deep', 'Deep link: opens this record'];
    if (l.fallback) return ['dr-k-find', 'Search page: finds the right site'];
    return ['dr-k-search', 'Search page'];
  }

  function linksPanel(it) {
    const links = (it.links || []).filter((l) => W.safeUrl(l.url));
    if (!links.length) {
      return '<section class="panel"><h2>Open these</h2><p class="note">No pages to open for this stage. Do it by hand, then check it off below.</p></section>';
    }
    const items = links.map((l, i) => {
      const [cls, text] = linkKind(l);
      const cost = l.cost && l.cost !== 'free' ? ` · ${esc((COST[l.cost] || [l.cost])[0])}` : '';
      const help = l.how || l.finds;
      return `
        <a class="dr-link${i === 0 ? ' first' : ''}" href="${esc(W.safeUrl(l.url))}" target="_blank" rel="noopener noreferrer">
          <span class="dr-link-l">${esc(l.label || l.source_key || 'Open')}&nbsp;<span aria-hidden="true">↗</span>${i === 0 ? ' <kbd>O</kbd>' : ''}</span>
          <span class="dr-link-k ${cls}">${text}${cost}</span>
          ${help ? `<span class="dr-link-how">${esc(help)}</span>` : ''}
        </a>`;
    }).join('');
    return `<section class="panel"><h2>Open these</h2><div class="dr-links">${items}</div></section>`;
  }

  function checkPanel(q, st) {
    const role = st.person_role || ROLE_DEFAULT[st.key] || 'other';
    const hidden = q.form ? '' : 'hidden';
    const field = (f, label, attrs = '', cls = '') =>
      `<label class="dr-field ${cls}"><span>${label}</span><input data-f="${f}" autocomplete="off" ${attrs}></label>`;
    return `
      <section class="panel dr-check" aria-label="Check off this stage">
        <h2>What did you find?</h2>
        <div class="dr-outcomes">
          <button class="btn primary" data-act="found" aria-expanded="${q.form ? 'true' : 'false'}">Found <kbd>F</kbd></button>
          <button class="btn" data-act="nothing">Nothing <kbd>N</kbd></button>
          <button class="btn" data-act="skip">Skip: not applicable <kbd>S</kbd></button>
        </div>
        <form data-form="found" novalidate>
          <div class="dr-found" ${hidden}>
            <div class="dr-fields">
              ${field('person', 'Person\'s name')}
              <label class="dr-field"><span>Their role</span><select data-f="role">${ROLES.map(([k, l]) =>
                `<option value="${k}" ${k === role ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
              ${field('phone', 'Phone', 'type="tel" inputmode="tel"')}
              ${field('email', 'Email', 'type="email" inputmode="email"')}
              ${field('social', 'Social profile link', 'type="url" inputmode="url" placeholder="https://"')}
              ${field('mail', 'Mailing address')}
            </div>
          </div>
          ${field('detail', 'Note (optional)', 'placeholder="What you saw, for next time"', 'dr-note')}
          <p class="dr-form-err" role="alert" hidden></p>
          <div class="bar dr-found" ${hidden}>
            <button class="btn primary" type="submit">Save what I found</button>
            <button class="btn" type="button" data-act="cancel-found">Cancel</button>
          </div>
        </form>
      </section>`;
  }

  const toValidate = (it) => (it.contacts || []).filter((c) => c.needs_validation);

  function contactValue(c) {
    const v = String(c.value || '');
    const digits = v.replace(/[^\d+]/g, '');
    let href = '';
    if ((c.channel === 'phone' || c.channel === 'vm') && digits) href = `tel:${digits}`;
    else if (c.channel === 'text' && digits) href = `sms:${digits}`;
    else if (c.channel === 'email') href = `mailto:${v}`;
    else if (c.channel === 'social') href = W.safeUrl(v);
    const inner = `<span class="dr-cval">${esc(v)}</span>`;
    if (!href) return inner;
    const target = c.channel === 'social' ? ' target="_blank" rel="noopener noreferrer"' : '';
    return `<a href="${esc(href)}"${target}>${inner}</a>`;
  }

  const outcomeLabel = (outcomes, key) => (outcomes.find((o) => o.key === key) || {}).label || W.labelText(key);

  const sourceName = (src) => SOURCE_TEXT[src] || stageName(src) || W.labelText(src);

  function outcomeButton(c, o) {
    const cls = { confirm: 'good', mark_wrong: 'bad', dnc: 'dnc' }[o.effect] || '';
    return `<button class="btn dr-oc ${cls}" data-act="outcome" data-cid="${c.id}" data-oc="${esc(o.key)}" data-effect="${esc(o.effect || 'none')}">${esc(o.label)}</button>`;
  }

  function contactBlock(q, it, c, outcomes) {
    const logged = q.logged.has(c.id);
    const tries = c.tries ? `${c.tries} unanswered ${c.tries === 1 ? 'try' : 'tries'}` : 'Not tried yet';
    const last = c.last_outcome ? ` · last: ${esc(outcomeLabel(outcomes, c.last_outcome))}` : '';
    const who = c.person_name || it.best_guess_person || '';
    return `
      <div class="dr-contact${logged ? ' logged' : ''}" data-cid="${c.id}">
        <div class="dr-contact-top">
          <span class="ch u">${esc(CHANNEL_TEXT[c.channel] || c.channel)}</span>
          ${contactValue(c)}
          ${c.person_name ? `<span class="note">${esc(c.person_name)}</span>` : ''}
        </div>
        <div class="note">${esc(tries)}${last}${c.source ? ` · found by ${esc(sourceName(c.source))}` : ''}${logged ? ' · <b>logged just now</b>' : ''}</div>
        <div class="dr-ocs">${outcomes.map((o) => outcomeButton(c, o)).join('')}</div>
        <div class="dr-confirm" hidden>
          <label class="dr-field"><span>Who confirmed they own it? (full name)</span>
            <input data-f="confirm-name" autocomplete="off" value="${esc(who)}"></label>
          <div class="bar">
            <button class="btn primary" data-act="confirm-send" data-cid="${c.id}">Confirm owner: level 3</button>
            <button class="btn" data-act="confirm-cancel">Cancel</button>
          </div>
        </div>
      </div>`;
  }

  function validatePanel(q, st, it) {
    const list = toValidate(it);
    const outcomes = (st.outcomes && st.outcomes.length ? st.outcomes : FALLBACK_OUTCOMES)
      .filter((o) => o && o.key);
    const phoneish = list.some((c) => ['phone', 'text', 'vm'].includes(c.channel));
    const step = it.next_step || {};
    return `
      <section class="panel dr-check" aria-label="Reach out">
        <h2>Reach out, then log what happened</h2>
        ${step.text ? `<p class="note dr-tight">${esc(step.text)}</p>` : ''}
        ${phoneish ? `<p class="dr-legal">${esc(step.note || TCPA)}</p>` : ''}
        ${list.map((c) => contactBlock(q, it, c, outcomes)).join('') || '<p class="note">No untested contacts left on this owner.</p>'}
        <label class="dr-field dr-note"><span>Note (optional)</span><input data-f="detail" autocomplete="off" placeholder="What they said, best time to call…"></label>
      </section>`;
  }

  function knownPanel(it) {
    const people = (it.people || []).map((p) => `
      <li><b>${esc(p.name)}</b> <span class="note">${esc(ROLE_TEXT[p.role] || W.labelText(p.role) || 'Other')}${p.source ? ` · from ${esc(sourceName(p.source))}` : ''}</span></li>`).join('');
    const contacts = (it.contacts || []).map((c) => `
      <li><span class="ch ${STATUS_CLS[c.status] || 'none'}" title="${esc(STATUS_TEXT[c.status] || c.status || '')}">${esc(CHANNEL_TEXT[c.channel] || c.channel)}</span>
        <span class="dr-cval">${esc(c.value)}</span>${c.person_name ? ` <span class="note">· ${esc(c.person_name)}</span>` : ''}</li>`).join('');
    return `
      <section class="panel" aria-label="What we know">
        <h2>What we know</h2>
        <div class="dr-chs">${BM.ui.channelBadges(it.channels || {})}</div>
        <h3 class="dr-sub">People</h3>
        ${people ? `<ul class="dr-list">${people}</ul>` : '<p class="note">No people recorded yet.</p>'}
        <h3 class="dr-sub">Contact info</h3>
        ${contacts ? `<ul class="dr-list">${contacts}</ul>` : '<p class="note">No phone, email or social account yet.</p>'}
      </section>`;
  }

  function historyPanel(it) {
    const items = (it.history || []).map((a) => {
      const pass = a.stage_key === PASS_MARKER;
      const cls = pass ? '' : { found: 'hit', nothing: 'miss' }[a.outcome] || '';
      const what = pass ? 'New pass' : (a.stage_name || W.labelText(a.stage_key));
      const result = pass ? '' : OUTCOME_TEXT[a.outcome] || a.outcome;
      return `
        <li class="${cls}"><div>
          <span class="dr-tl-src">${esc(what)}</span>
          <span class="dr-tl-res">${result ? `· ${esc(result)}` : ''}${a.detail ? `${result ? ': ' : '· '}${esc(a.detail)}` : ''}</span>
          <div class="note">${esc(W.when(a.created_at))}</div>
        </div></li>`;
    }).join('');
    return `
      <section class="panel" aria-label="Drill so far">
        <h2>Drill so far</h2>
        ${items ? `<ul class="dr-timeline">${items}</ul>` : '<p class="note">Nothing tried on this owner yet.</p>'}
      </section>`;
  }

  // ---------------------------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------------------------

  function onClick(e) {
    const b = e.target.closest('[data-act]');
    if (!b || !S.root || !S.root.contains(b) || b.disabled) return;
    const handlers = {
      retry: () => render(S.root, W.route().params),
      found: () => (S.q && S.q.form ? closeFound() : openFound()),
      'cancel-found': closeFound,
      nothing: () => postAttempt('nothing'),
      skip: () => postAttempt('skipped'),
      next: () => step(1),
      prev: () => step(-1),
      restart: () => { S.q.idx = 0; paintWork(); },
      'open-property': () => { const it = currentItem(); if (it) BM.openProperty(it.property_id); },
      copy: () => W.copy(b.dataset.copy || ''),
      outcome: () => onOutcome(b),
      'confirm-send': () => sendConfirm(b),
      'confirm-cancel': () => toggleConfirm(b, false),
    };
    const h = handlers[b.dataset.act];
    if (!h) return;
    e.preventDefault();
    h();
  }

  function onSubmit(e) {
    if (!e.target.matches('[data-form="found"]')) return;
    e.preventDefault();
    if (S.q && S.q.form) postAttempt('found');
  }

  function onChange(e) {
    if (e.target.matches('.dr-switch') && e.target.value) BM.go('drill', [e.target.value]);
  }

  function setFoundOpen(open) {
    if (!S.q || !S.root) return;
    S.q.form = open;
    S.root.querySelectorAll('.dr-found').forEach((el) => { el.hidden = !open; });
    const btn = S.root.querySelector('[data-act="found"]');
    if (btn) btn.setAttribute('aria-expanded', String(open));
    showFormError('');
  }

  function openFound() {
    setFoundOpen(true);
    const f = S.root && S.root.querySelector('[data-f="person"]');
    if (f) f.focus();
  }

  function closeFound() {
    setFoundOpen(false);
    if (W.isTyping(document.activeElement)) document.activeElement.blur();
  }

  function showFormError(text) {
    const el = S.root && S.root.querySelector('.dr-form-err');
    if (!el) return;
    el.textContent = text;
    el.hidden = !text;
  }

  /** Disable the check-off buttons while a save is in flight; report failures in a toast. */
  async function withBusy(fn) {
    const q = S.q;
    if (!q || q.busy) return;
    q.busy = true;
    const btns = S.root ? [...S.root.querySelectorAll('.dr-check button, .dr-nav button')] : [];
    btns.forEach((b) => { b.disabled = true; });
    try {
      await fn();
    } catch (err) {
      W.toastError(err);
      btns.forEach((b) => { if (b.isConnected) b.disabled = false; });
    } finally {
      q.busy = false;
    }
  }

  function shortNext(next) {
    if (!next) return '';
    if (next.kind === 'validate') return 'reach out on the new contact';
    if (next.kind === 'handoff') return 'hand off to the CRM';
    if (next.kind === 'exhausted') return 'every source has been tried';
    if (next.kind === 'dnc') return 'do not contact';
    return stageName(next.stage_key) || String(next.text || '').split(':')[0];
  }

  async function postAttempt(outcome) {
    const q = S.q;
    const it = currentItem();
    if (!q || !it || q.busy || q.key === 'validate') return;
    const f = W.fields(S.root.querySelector('[data-form="found"]'));
    const body = { stage_key: q.key, outcome, detail: f.detail || null };
    if (outcome === 'found') {
      const contacts = W.contactsFrom(f);
      if (!f.person && !contacts.length && !f.detail) {
        showFormError('Type what you found (a name, phone, email, link or address), or press Nothing.');
        return;
      }
      Object.assign(body, { person: f.person || null, person_role: f.person ? f.role || null : null, contacts });
    } else if (outcome === 'skipped' && !body.detail) {
      body.detail = 'Not applicable.';
    }
    await withBusy(async () => {
      const res = await BM.api(W.mpath(`/properties/${it.property_id}/attempt`), { method: 'POST', body });
      const head = { found: 'Saved: found', nothing: 'Saved: nothing found', skipped: 'Skipped' }[outcome];
      const next = shortNext(res && res.next_step);
      BM.toast(`${head}.${next ? ` Next for that owner: ${next}.` : ''}`);
      q.form = false;
      await advance(q, false);
    });
  }

  /** Move to the next owner after a save. stillQueued: the owner is still waiting in this queue. */
  async function advance(q, stillQueued) {
    q.done += 1;
    q.logged = new Set();
    if (stillQueued) q.idx += 1;
    else {
      q.items.splice(q.idx, 1);
      q.total = Math.max(0, q.total - 1);
    }
    await fillTo(q, q.idx);
    if (S.q !== q || S.mode !== 'work') return;
    W.scrollToTop(paintWork() && S.root.querySelector('.dr-nav, .dr-crumbs'));
    prefetch(q);
    W.refreshSoon();
  }

  /** J / K: pass over an owner for now (nothing is saved), or go back. */
  async function step(delta) {
    const q = S.q;
    if (!q || q.busy) return;
    const target = q.idx + delta;
    if (target < 0) return;
    await fillTo(q, target);
    if (S.q !== q) return;
    if (target > q.items.length - 1 && q.items.length >= q.total) return;
    q.idx = Math.min(target, q.items.length);
    q.form = false;
    q.logged = new Set();
    paintWork();
    prefetch(q);
  }

  function openFirstLink() {
    const it = currentItem();
    const link = it && (it.links || []).find((l) => W.safeUrl(l.url));
    if (link) window.open(W.safeUrl(link.url), '_blank', 'noopener,noreferrer');
    else BM.toast('No link to open for this stage.');
  }

  // --- reach out (validate) ---

  function toggleConfirm(btn, open) {
    const box = btn.closest('.dr-contact');
    const panel = box && box.querySelector('.dr-confirm');
    if (!panel) return;
    panel.hidden = !open;
    if (open) {
      const input = panel.querySelector('input');
      if (input) { input.focus(); input.select(); }
    }
  }

  function onOutcome(btn) {
    const effect = btn.dataset.effect;
    if (effect === 'confirm') { toggleConfirm(btn, true); return; }
    if (effect === 'dnc' && btn.dataset.armed !== '1') {
      btn.dataset.armed = '1';
      btn.dataset.label = btn.textContent;
      btn.textContent = 'Tap again: never contact them';
      setTimeout(() => {
        if (btn.isConnected && btn.dataset.armed === '1') { btn.dataset.armed = ''; btn.textContent = btn.dataset.label; }
      }, 4000);
      return;
    }
    postOutreach(Number(btn.dataset.cid), btn.dataset.oc, effect, null);
  }

  function sendConfirm(btn) {
    const box = btn.closest('.dr-contact');
    const name = ((box && box.querySelector('[data-f="confirm-name"]')) || {}).value || '';
    const oc = (box && box.querySelector('[data-effect="confirm"]')) || null;
    postOutreach(Number(btn.dataset.cid), oc ? oc.dataset.oc : 'reached_confirmed', 'confirm', name.trim() || null);
  }

  function outreachToast(effect, label, res) {
    const who = res && res.drill && res.drill.confirmed_person;
    if (effect === 'confirm') return `Confirmed owner${who ? `: ${who}` : ''}. Level 3, ready for the CRM.`;
    if (effect === 'mark_wrong') return 'Marked wrong. The drill moves on.';
    if (effect === 'dnc') return 'Marked do not contact. They are off every list.';
    return `Logged: ${label}.`;
  }

  async function postOutreach(cid, outcomeKey, effect, person) {
    const q = S.q;
    const it = currentItem();
    const c = it && (it.contacts || []).find((x) => x.id === cid);
    if (!q || !c || q.busy) return;
    const notes = (S.root.querySelector('.dr-note [data-f="detail"]') || {}).value || '';
    const outcomes = (q.stage && q.stage.outcomes) || FALLBACK_OUTCOMES;
    await withBusy(async () => {
      let res = await BM.api(W.mpath(`/properties/${it.property_id}/outreach`), {
        method: 'POST',
        body: { channel: c.channel, outcome: outcomeKey, contact_point_id: c.id, notes: notes.trim() || null },
      });
      if (effect === 'confirm' && person && person !== (res && res.drill && res.drill.confirmed_person)) {
        res = await BM.api(W.mpath(`/properties/${it.property_id}/level`), {
          method: 'POST', body: { action: 'confirm', person },
        });
      }
      BM.toast(outreachToast(effect, outcomeLabel(outcomes, outcomeKey), res));
      await afterOutreach(q, it, c, res);
    });
  }

  /** Stay on this owner while other untested contacts are left; otherwise move on. */
  async function afterOutreach(q, it, c, res) {
    const d = (res && res.drill) || {};
    const kind = (d.next_step || (res && res.next_step) || {}).kind;
    q.logged.add(c.id);
    if (kind !== 'validate') return advance(q, false);
    Object.assign(it, {
      contacts: d.contacts || it.contacts, next_step: d.next_step || it.next_step,
      channels: d.channels || it.channels, level: d.level ?? it.level,
      best_guess_person: d.best_guess_person ?? it.best_guess_person,
    });
    if (toValidate(it).some((x) => !q.logged.has(x.id))) {
      paintWork();
      W.refreshSoon();
      return undefined;
    }
    return advance(q, true);
  }

  // ---------------------------------------------------------------------------------------------
  // Keyboard: F found · N nothing · S skip · J next · K previous · O open the first link
  // ---------------------------------------------------------------------------------------------

  document.addEventListener('keydown', (e) => {
    if (S.mode !== 'work' || !S.q || !S.root || !S.root.isConnected || W.route().key !== 'drill') return;
    if (W.overlayOpen()) return;
    if (e.key === 'Escape' && S.q.form) { e.preventDefault(); closeFound(); return; }
    if (W.keysBlocked(e)) return;
    const isV = S.q.key === 'validate';
    const k = e.key.toLowerCase();
    const map = isV
      ? { j: () => step(1), k: () => step(-1) }
      : {
        f: openFound, n: () => postAttempt('nothing'), s: () => postAttempt('skipped'),
        j: () => step(1), k: () => step(-1), o: openFirstLink,
      };
    if (!map[k] || !currentItem()) return;
    e.preventDefault();
    map[k]();
  });

  // ---------------------------------------------------------------------------------------------

  BM.registerView('drill', {
    label: 'DRILL',
    group: 'work',
    order: 2,
    // Owners still open in the drill: each one waits in a stage queue or the reach-out queue.
    badge: (s) => {
      const n = s && (s.drill_waiting ?? (s.statuses && s.statuses.active));
      return n ? W.compact(n) : null;
    },
    render,
  });
})();
