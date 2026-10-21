/* Crexi owner-contact credits: how many are left this month, the daily pace that spends them all
 * before they expire, the plan (which owners to spend them on, in order), and a log of credits used.
 *
 * The app never touches Crexi: Seth opens the Crexi link, uses the credit there, and records here what
 * it showed. Uses BM.work helpers from drill.js.
 */
(() => {
  'use strict';

  const BM = window.BM;
  const W = BM && BM.work;
  if (!BM || !W) {
    console.error('credits.js needs core.js and drill.js to load first.');
    return;
  }

  const esc = (v) => BM.esc(v == null ? '' : String(v));
  const num = (n) => BM.fmt.num(Number(n) || 0);
  const PAGE = 25;

  const S = {
    root: null,
    market: null,
    seq: 0,
    limit: PAGE,
    data: null,
    open: null,      // property id whose "Used a credit" form is open
    busy: false,
  };

  // ---------------------------------------------------------------------------------------------

  async function render(root) {
    S.root = root;
    if (S.market !== BM.state.market) {
      S.market = BM.state.market;
      S.limit = PAGE;
      S.data = null;
      S.open = null;
    }
    const seq = ++S.seq;
    paint(S.data ? pageHtml(S.data) : head(null) + W.loading('Loading the credit plan…'));
    try {
      const data = await BM.api(W.mpath(`/credits?limit=${S.limit}`));
      if (seq !== S.seq) return;
      S.data = data || {};
      paint(pageHtml(S.data));
    } catch (err) {
      if (seq !== S.seq) return;
      paint(head(null) + W.errorBox(err));
    }
  }

  /** Reload after a change, keeping the scroll position. */
  async function reload() {
    const y = window.scrollY;
    await render(S.root);
    window.scrollTo(0, y);
  }

  function paint(html) {
    if (!S.root) return null;
    S.root.innerHTML = `<div class="dr dr-credits">${html}</div>`;
    const el = S.root.firstElementChild;
    el.addEventListener('click', onClick);
    el.addEventListener('submit', onSubmit);
    return el;
  }

  // ---------------------------------------------------------------------------------------------
  // Page
  // ---------------------------------------------------------------------------------------------

  function head(status) {
    const per = status ? num(status.per_month) : 'Your';
    return `
      <div class="eyebrow">Crexi owner-contact credits</div>
      <h1>Spend every credit, every month</h1>
      <ul class="lines">
        <li>${per} owner-contact credits arrive each month. Unused ones expire.</li>
        <li>The plan picks the best possible owners the free sources could not reach.</li>
        <li>Open Crexi, use the credit there, then record here what it showed.</li>
      </ul>`;
  }

  function pageHtml(d) {
    const status = d.status || {};
    const log = d.log || [];
    const thisMonth = log.filter((r) => r.month === status.month);
    const emptyMarket = BM.state.summary && BM.state.summary.properties === 0;
    return head(status) + heroHtml(status, d) + statsHtml(status) + pendingHtml(thisMonth) +
      (emptyMarket ? W.emptyMarket('The plan then lines up the best possible owners for your credits.') : planHtml(d, status)) +
      logHtml(log, thisMonth, status);
  }

  function heroHtml(st, d) {
    const per = Number(st.per_month) || 0;
    const used = Number(st.used) || 0;
    const left = Number(st.left) || 0;
    const usedPct = per ? Math.min(100, Math.round((used / per) * 100)) : 0;
    let pace;
    if (!per) pace = esc(st.message || 'No Crexi credits are set up (config.yaml crexi.credits_per_month).');
    else if (!left) pace = 'All used for this month. New ones arrive on the 1st.';
    else {
      pace = `Use about <b>${num(st.daily_pace)} a day</b> for the next ${num(st.days_left)} ${st.days_left === 1 ? 'day' : 'days'} to spend them all before they expire.`;
    }
    const planned = Number(d.planned) || 0;
    const short = Number(d.shortfall) || 0;
    let plan = '';
    if (left) {
      plan = planned
        ? `The plan has ${num(planned)} ${planned === 1 ? 'owner' : 'owners'} lined up${short ? '' : ', enough for every credit'}.`
        : 'No owner qualifies for a credit yet.';
    }
    const shortNote = short
      ? `<p class="dr-banner warn">${num(short)} ${short === 1 ? 'credit has' : 'credits have'} no owner to go to yet. Work the free stages in the DRILL to turn more owners into possible owners.
           <button class="btn" data-go="drill">Open the DRILL</button></p>`
      : '';
    return `
      <section class="panel dr-hero" aria-label="Credits left this month">
        <div class="dr-hero-main">
          <span class="big num">${num(left)}</span>
          <div><b>credits left for ${esc(W.monthName(st.month))}</b>
            <div class="note num">${num(used)} of ${num(per)} used · they expire at month end</div></div>
        </div>
        <div class="dr-meter" role="img" aria-label="${used} of ${per} credits used"><i style="width:${usedPct}%"></i></div>
        <ul class="lines dr-tight">
          <li>${pace}</li>
          ${plan ? `<li>${esc(plan)}</li>` : ''}
        </ul>
        ${shortNote}
      </section>`;
  }

  function statsHtml(st) {
    const judged = (Number(st.found) || 0) + (Number(st.nothing) || 0);
    const tiles = [
      [`${num(st.used_today)}${st.daily_pace ? ` / ${num(st.daily_pace)}` : ''}`, 'Used today / daily pace'],
      [judged ? W.pct0(st.hit_rate) : '—', judged ? `Found contact info (${num(st.found)} of ${num(judged)})` : 'Found contact info'],
      [num(st.pending), 'Used, result not recorded yet'],
      [num(st.days_left), 'Days left this month'],
    ];
    return `<div class="strip dr-strip">${tiles.map(([v, l]) => BM.ui.stat(v, l)).join('')}</div>`;
  }

  function pendingHtml(rows) {
    const pending = rows.filter((r) => r.result == null);
    if (!pending.length) return '';
    return `
      <section class="panel dr-pending" aria-label="Credits waiting for a result">
        <h2>Waiting for a result</h2>
        <p class="note dr-tight">You used these credits but haven't recorded what Crexi showed. Record it so the drill moves on.</p>
        <ul class="dr-log">${pending.map((r) => `
          <li class="dr-log-row" data-pid="${r.property_id}">
            <span class="dr-log-when note">${esc(W.when(r.created_at))}</span>
            <span class="dr-log-what"><button class="dr-linklike" data-act="open" data-pid="${r.property_id}">${esc(W.address(r))}</button>
              <span class="note">${esc(W.titleCase(r.city))}</span></span>
            <button class="btn" data-act="use" data-pid="${r.property_id}" data-pending="1">Record result</button>
            ${S.open === r.property_id ? creditForm(r.property_id, true) : ''}
          </li>`).join('')}</ul>
      </section>`;
  }

  function whenText(it) {
    if (it.when === 'today') return 'Today';
    if (it.when === 'tomorrow') return 'Tomorrow';
    return it.use_on ? BM.fmt.date(it.use_on) : '';
  }

  function freeChips(it) {
    const tried = it.free_tried || [];
    const notTried = it.free_not_tried || [];
    if (!tried.length && !notTried.length) return '<p class="note dr-tight">No free reach stage applies to this owner.</p>';
    return `<div class="dr-tags"><span class="note">Free stages:</span>${
      tried.map((n) => `<span class="dr-tag ok" title="Tried">✓ ${esc(n)}</span>`).join('')}${
      notTried.map((n) => `<span class="dr-tag" title="Not tried yet">○ ${esc(n)}</span>`).join('')}</div>`;
  }

  function planItem(it) {
    const crexi = W.extLink(it.crexi_url, 'Open in Crexi ↗', 'btn');
    return `
      <li class="dr-plan-item" data-pid="${it.property_id}">
        <span class="dr-plan-n num" aria-label="Number ${it.order}">${num(it.order)}</span>
        <div class="dr-plan-body">
          <div class="dr-plan-top">
            <button class="dr-linklike dr-addr-sm" data-act="open" data-pid="${it.property_id}">${esc(W.address(it))}</button>
            <span class="note">${esc(W.place(it))}</span>
            <span class="pill${it.when === 'today' ? ' hot' : ''}">${esc(whenText(it))}</span>
          </div>
          <div class="dr-plan-meta">${W.moonScore(it.score)}${BM.ui.levelPips(it.level)}${it.best_guess_person ? `<span class="note">Best guess: <b>${esc(it.best_guess_person)}</b></span>` : ''}</div>
          <p class="note dr-tight">${esc(it.reason || '')}</p>
          ${freeChips(it)}
          <div class="dr-plan-actions">
            ${crexi || '<span class="note">No Crexi link on this record.</span>'}
            <button class="btn primary" data-act="use" data-pid="${it.property_id}" aria-expanded="${S.open === it.property_id}">Used a credit</button>
          </div>
          ${S.open === it.property_id ? creditForm(it.property_id, false) : ''}
        </div>
      </li>`;
  }

  function planHtml(d, st) {
    const plan = d.plan || [];
    const planned = Number(d.planned) || plan.length;
    if (!plan.length) {
      const text = Number(st.left) === 0 && Number(st.per_month) > 0
        ? 'Every credit is used for this month. New ones arrive on the 1st.'
        : 'Possible owners (level 2) the free stages could not reach show up here. Work the DRILL to add more.';
      return `<section class="panel" aria-label="The plan"><h2>The plan</h2>${BM.ui.empty('Nobody is lined up for a credit', text,
        '<button class="btn" data-go="drill">Open the DRILL</button>')}</section>`;
    }
    const more = plan.length < planned
      ? `<div class="dr-more"><button class="btn" data-act="more">Show ${num(Math.min(planned - plan.length, nextLimit() - plan.length))} more</button>
         <span class="note">${num(plan.length)} of ${num(planned)} shown</span></div>`
      : '';
    return `
      <section class="panel" aria-label="The plan">
        <div class="dr-row-between"><h2>The plan</h2><span class="note num">${num(planned)} lined up</span></div>
        <p class="note dr-tight">Best possible owners first, preferring ones the free stages already tried. Each one has a day so the credits last the month.</p>
        <ol class="dr-plan">${plan.map(planItem).join('')}</ol>
        ${more}
      </section>`;
  }

  const nextLimit = () => (S.limit < 100 ? 100 : S.limit + 200);

  function logHtml(log, thisMonth, st) {
    const older = log.length - thisMonth.length;
    const label = { found: ['Found', 'good'], nothing: ['Nothing', ''] };
    const rows = thisMonth.map((r) => {
      const [text, cls] = r.result == null ? ['Waiting for result', 'warn'] : label[r.result] || [W.labelText(r.result), ''];
      return `
        <li class="dr-log-row">
          <span class="dr-log-when note num">${esc(BM.fmt.date(r.created_at))}</span>
          <span class="dr-log-what"><button class="dr-linklike" data-act="open" data-pid="${r.property_id}">${esc(W.address(r))}</button>
            <span class="note">${esc(W.titleCase(r.city))}</span></span>
          <span class="dr-result ${cls}">${esc(text)}</span>
        </li>`;
    }).join('');
    return `
      <section class="panel" aria-label="Credits used">
        <h2>Credits used in ${esc(W.monthName(st.month))}</h2>
        ${rows ? `<ul class="dr-log">${rows}</ul>` : '<p class="note">No credits used yet this month.</p>'}
        ${older > 0 ? `<p class="note">${num(older)} from earlier months not shown.</p>` : ''}
      </section>`;
  }

  /** The "Used a credit" form: what did Crexi show? */
  function creditForm(pid, pending) {
    const field = (f, label, attrs = '') =>
      `<label class="dr-field"><span>${label}</span><input data-f="${f}" autocomplete="off" ${attrs}></label>`;
    return `
      <form class="dr-credit-form" data-form="credit" data-pid="${pid}" data-result="found" novalidate>
        <div class="dr-field"><span>What did Crexi show?</span>
          <div class="seg" role="group" aria-label="Result">
            <button type="button" data-act="result" data-r="found" aria-pressed="true">Found contact info</button>
            <button type="button" data-act="result" data-r="nothing" aria-pressed="false">Nothing useful</button>
          </div>
        </div>
        <div class="dr-fields" data-when="found">
          ${field('person', 'Person\'s name')}
          ${field('phone', 'Phone', 'type="tel" inputmode="tel"')}
          ${field('email', 'Email', 'type="email" inputmode="email"')}
          ${field('detail', 'Note (optional)')}
        </div>
        <p class="dr-form-err" role="alert" hidden></p>
        <div class="bar">
          <button class="btn primary" type="submit">${pending ? 'Save the result' : 'Save: 1 credit used'}</button>
          <button class="btn" type="button" data-act="cancel">Cancel</button>
        </div>
      </form>`;
  }

  // ---------------------------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------------------------

  function onClick(e) {
    const b = e.target.closest('[data-act]');
    if (!b || !S.root || !S.root.contains(b) || b.disabled) return;
    const pid = Number(b.dataset.pid);
    const act = b.dataset.act;
    if (act === 'retry') { e.preventDefault(); render(S.root); }
    else if (act === 'open' && pid) { e.preventDefault(); BM.openProperty(pid); }
    else if (act === 'use' && pid) { e.preventDefault(); toggleForm(pid); }
    else if (act === 'cancel') { e.preventDefault(); toggleForm(null); }
    else if (act === 'result') { e.preventDefault(); setResult(b); }
    else if (act === 'more') { e.preventDefault(); S.limit = nextLimit(); reload(); }
  }

  /** Open one form at a time (repainting from the cached data keeps the page still). */
  function toggleForm(pid) {
    S.open = S.open === pid ? null : pid;
    const y = window.scrollY;
    paint(pageHtml(S.data || {}));
    window.scrollTo(0, y);
    const form = S.open && S.root.querySelector(`[data-form="credit"][data-pid="${S.open}"]`);
    if (form) {
      const first = form.querySelector('[data-f="person"]');
      if (first) first.focus({ preventScroll: true });
      if (form.getBoundingClientRect().bottom > window.innerHeight) form.scrollIntoView({ block: 'nearest' });
    }
  }

  function setResult(btn) {
    const form = btn.closest('form');
    if (!form) return;
    const r = btn.dataset.r;
    form.dataset.result = r;
    form.querySelectorAll('[data-act="result"]').forEach((x) => x.setAttribute('aria-pressed', String(x === btn)));
    form.querySelectorAll('[data-when="found"] [data-f]').forEach((x) => {
      if (x.dataset.f !== 'detail') x.closest('.dr-field').hidden = r !== 'found';
    });
    formError(form, '');
  }

  function formError(form, text) {
    const el = form.querySelector('.dr-form-err');
    if (!el) return;
    el.textContent = text;
    el.hidden = !text;
  }

  async function onSubmit(e) {
    const form = e.target;
    if (!form.matches('[data-form="credit"]')) return;
    e.preventDefault();
    if (S.busy) return;
    const pid = Number(form.dataset.pid);
    const result = form.dataset.result === 'nothing' ? 'nothing' : 'found';
    const f = W.fields(form);
    const contacts = result === 'found' ? W.contactsFrom(f) : [];
    if (result === 'found' && !contacts.length && !f.person) {
      formError(form, 'Type the phone, email or name Crexi showed, or choose Nothing useful.');
      return;
    }
    const body = { result, person: result === 'found' ? f.person || null : null, contacts, detail: f.detail || null };
    const buttons = [...form.querySelectorAll('button')];
    buttons.forEach((b) => { b.disabled = true; });
    S.busy = true;
    try {
      const res = await BM.api(W.mpath(`/properties/${pid}/credit`), { method: 'POST', body });
      BM.toast((res && res.message) || 'Credit recorded.');
      S.open = null;
      W.refreshSoon();
      await reload();
    } catch (err) {
      formError(form, W.errText(err));
      buttons.forEach((b) => { b.disabled = false; });
    } finally {
      S.busy = false;
    }
  }

  // ---------------------------------------------------------------------------------------------

  BM.registerView('credits', {
    label: 'Credits',
    group: 'work',
    order: 3,
    badge: (s) => {
      const left = s && s.credits && s.credits.left;
      return left == null ? null : W.compact(left);
    },
    render,
  });
})();
