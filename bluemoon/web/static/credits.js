/* Crexi owner-contact credits, in bulk.
 *
 * You never open properties in Crexi one at a time. This screen exports a CSV of the best owners (the next N),
 * you upload it to Crexi yourself, Crexi spends credits and sends a file back, and you drop that file here.
 * The app tracks every export and import exactly, per credit period (Crexi resets on config.yaml crexi.reset_day).
 * It never logs in to Crexi or opens it. Server side: bluemoon/crexi/ and bluemoon/web/crexi_api.py.
 *
 * Parts: the credit period, 1 Export the next N, 2 Drop Crexi's file (importing it finishes the batch: "Crexi
 * returned 450 of 500. 50 missing." with the missing list; the missing cost nothing and go to the DRILL's other
 * sources), batches still waiting at Crexi (cancel one never uploaded), History per credit period, and a folded
 * "Do one by hand" for the old one-at-a-time plan. There is no close step.
 * Uses BM.work helpers from drill.js.
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
  const pct = (p) => (p == null ? '—' : `${Math.round(Number(p) * 100)}%`);
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  /** 'YYYY-MM-DD' -> 'Oct 29' (date-only text, so no time zone can move it a day). */
  const day = (iso) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : '';
  };
  const ordinal = (n) => {
    const v = Number(n) || 0;
    const s = ['th', 'st', 'nd', 'rd'];
    const k = v % 100;
    return v + (s[(k - 20) % 10] || s[k] || s[0]);
  };
  const TIER = {
    main: 'Possible owner in the buy box', level1_buybox: 'Buy box, no owner name yet',
    set_aside: 'Just outside the buy box', retry: 'Second pass',
  };
  const STATUS = { open: 'Waiting for Crexi', imported: 'Waiting for Crexi', closed: 'Finished' };
  const PICK_PAGE = 25;

  const S = {
    root: null, seq: 0,
    status: null, history: null, error: null,
    count: '', retry: false, preview: null, exported: null, exportBusy: '', exportError: '',
    imp: { file: null, preview: null, columns: null, adopt: false, busy: '', error: '', result: null, change: false },
    confirm: null,        // {id, kind: 'withdraw', message}
    hand: { open: false, data: null, limit: PICK_PAGE, form: null, busy: false, error: null },
    note: '',
  };

  // ---------------------------------------------------------------------------------------------
  // Loading
  // ---------------------------------------------------------------------------------------------

  async function render(root) {
    S.root = root;
    const seq = ++S.seq;
    paint(S.status ? pageHtml() : head() + W.loading('Loading your Crexi credits…'));
    try {
      const [status, history] = await Promise.all([BM.api('/api/crexi/status'), BM.api('/api/crexi/history')]);
      if (seq !== S.seq) return;
      S.status = status;
      S.history = history;
      S.error = null;
      if (S.count === '') S.count = String(status.default_count || '');
      paint(pageHtml());
    } catch (err) {
      if (seq !== S.seq) return;
      paint(head() + W.errorBox(err));
    }
  }

  /** Reload the numbers after a change (keeps the scroll position and what you typed). */
  async function reload() {
    const y = window.scrollY;
    try {
      const [status, history] = await Promise.all([BM.api('/api/crexi/status'), BM.api('/api/crexi/history')]);
      S.status = status;
      S.history = history;
      if (S.count === '' || Number(S.count) > Number(status.default_count || 0)) S.count = String(status.default_count || '');
    } catch (err) {
      S.error = err;
    }
    paint(pageHtml());
    window.scrollTo(0, y);
    W.refreshSoon();
  }

  function paint(html) {
    if (!S.root) return null;
    S.root.innerHTML = `<div class="dr dr-credits">${html}</div>`;
    const el = S.root.firstElementChild;
    el.addEventListener('click', onClick);
    el.addEventListener('submit', onSubmit);
    el.addEventListener('input', onInput);
    el.addEventListener('change', onChange);
    el.addEventListener('toggle', onToggle, true);
    el.addEventListener('dragover', (e) => { if (e.target.closest('[data-cx-drop]')) { e.preventDefault(); e.target.closest('[data-cx-drop]').classList.add('over'); } });
    el.addEventListener('dragleave', (e) => { const z = e.target.closest('[data-cx-drop]'); if (z) z.classList.remove('over'); });
    el.addEventListener('drop', (e) => {
      const z = e.target.closest('[data-cx-drop]');
      if (!z) return;
      e.preventDefault();
      z.classList.remove('over');
      chooseFile(e.dataTransfer && e.dataTransfer.files[0]);
    });
    return el;
  }

  // ---------------------------------------------------------------------------------------------
  // Page
  // ---------------------------------------------------------------------------------------------

  function head() {
    return `
      <div class="eyebrow">Crexi owner-contact credits</div>
      <h1>Send Crexi a batch, drop its answer back</h1>
      <ol class="cr-how" aria-label="How it works">
        <li><b>Export the next batch</b><span>The app picks the owners most worth a credit and makes a file.</span></li>
        <li><b>Upload it to Crexi</b><span>You do this in Crexi's bulk tool. The app never logs in to Crexi.</span></li>
        <li><b>Drop Crexi's file here</b><span>That finishes the batch. What Crexi didn't have costs nothing and goes to the DRILL's other sources.</span></li>
      </ol>`;
  }

  function pageHtml() {
    const st = (S.status || {}).status || {};
    const empty = BM.state.summary && BM.state.summary.properties === 0 && !(S.history && S.history.periods.some((p) => p.sent));
    return head() + periodHtml(st) + (S.error ? W.errorBox(S.error) : '') +
      exportHtml(st) + importHtml() + waitingHtml() + historyHtml() + handHtml(empty);
  }

  // ---- the credit period -----------------------------------------------------------------------

  function periodHtml(st) {
    const per = Number(st.per_month) || 0;
    const returned = Number(st.returned) || 0;
    const flight = Number(st.in_flight) || 0;
    const left = Number(st.left) || 0;
    const w = (n) => (per ? Math.min(100, (n / per) * 100) : 0);
    const reset = Number(st.reset_day) || 29;
    return `
      <section class="panel dr-hero" aria-label="Credits this period">
        <div class="dr-hero-main">
          <span class="big num">${num(left)}</span>
          <div><b>credits free</b>
            <div class="note">${esc(st.period_label || '')}</div></div>
        </div>
        <div class="dr-meter dr-meter3" role="img" aria-label="${returned} returned, ${flight} in flight, ${left} free of ${per}">
          <i class="m-returned" style="width:${w(returned)}%"></i><i class="m-flight" style="width:${w(flight)}%"></i>
        </div>
        <ul class="cx-nums">
          <li><b class="num">${num(per)}</b> per period</li>
          <li><b class="num">${num(returned)}</b> returned</li>
          <li><b class="num">${num(flight)}</b> in flight</li>
          <li><b class="num">${num(left)}</b> free</li>
          <li><b class="num">${num(st.days_left)}</b> ${Number(st.days_left) === 1 ? 'day' : 'days'} left</li>
        </ul>
        <p class="note dr-tight">This period ends ${esc(day(st.period_end))}; new credits arrive ${esc(day(st.next_reset))} (the ${esc(ordinal(reset))} of each month). They do not roll over.
          <b>Returned</b> means Crexi matched the property and spent a credit (even with no phone or email). <b>In flight</b> means you sent it and have not imported Crexi's answer yet; it counts against the ${num(per)} so you can't overspend. Rows Crexi does not match cost nothing.</p>
      </section>`;
  }

  // ---- 1. export --------------------------------------------------------------------------------

  function exportHtml(st) {
    const info = S.status || {};
    const left = Number(st.left) || 0;
    const n = S.count === '' ? left : S.count;
    const tip = info.topup || {};
    const tipBtn = tip.suggested ? `<button class="btn" data-cx="use-count" data-n="${Number(tip.suggested)}">Export the next ${num(tip.suggested)}</button>` : '';
    const banner = tip.message ? `<p class="dr-banner"><span>${esc(tip.message)}</span>${tipBtn}</p>` : '';
    return `
      <section class="panel" aria-label="Export the next batch">
        <h2>1. Export the next <span class="num" data-cx-n>${esc(n || '')}</span> for Crexi</h2>
        <p class="note dr-tight">Picks the possible owners most worth a credit, never one already sent this period, never the same property twice, never do-not-contact, confirmed or set-aside-by-hand owners.</p>
        ${banner}
        <form data-cx-form="export" novalidate>
          <div class="cx-row">
            <label class="dr-field cx-count"><span>How many (up to ${num(left)})</span>
              <input data-cx-count type="number" min="1" max="${left}" inputmode="numeric" value="${esc(S.count)}" ${left ? '' : 'disabled'}></label>
            <label class="cx-check"><input type="checkbox" data-cx-retry ${S.retry ? 'checked' : ''}>
              <span>Also send the ones Crexi didn't have before (second pass)</span></label>
          </div>
          <p class="dr-form-err" role="alert" ${S.exportError ? '' : 'hidden'}>${esc(S.exportError)}</p>
          <div class="bar">
            <button type="button" class="btn" data-cx="preview" ${left && !S.exportBusy ? '' : 'disabled'}>Show who is in it</button>
            <button type="submit" class="btn primary dr-big" ${left && !S.exportBusy ? '' : 'disabled'}>Export the next <span data-cx-n>${esc(n || '')}</span> for Crexi</button>
          </div>
        </form>
        ${S.exportBusy ? W.loading(S.exportBusy) : ''}
        ${previewHtml()}${exportedHtml()}
      </section>`;
  }

  function previewHtml() {
    const p = S.preview;
    if (!p) return '';
    const reasons = (p.reasons || []).map((r) => `<li><b class="num">${num(r.count)}</b> ${esc(r.label)}</li>`).join('');
    const markets = (p.by_market || []).length > 1
      ? `<p class="note dr-tight">By market: ${p.by_market.map((m) => `${esc(m.name)} ${num(m.count)}`).join(' · ')}</p>` : '';
    const sample = (p.sample || []).map((s) => `<li><span>${esc(W.titleCase(s.address || ''))}</span> <span class="note">${esc(W.titleCase(s.city || ''))}${s.second_pass ? ' · second pass' : ''}</span></li>`).join('');
    return `
      <div class="cx-box" aria-label="Who would be in the batch">
        <p><b>${esc(p.message)}</b> Nothing was written yet.</p>
        <ul class="cx-nums cx-reasons">${reasons}</ul>${markets}
        ${p.duplicates_left_out ? `<p class="note dr-tight"><b>${num(p.duplicates_left_out)}</b> duplicate${p.duplicates_left_out === 1 ? '' : 's'} left out (same Crexi link, parcel number or address as a property already in the list).</p>` : ''}
        ${p.left_out_text ? `<p class="note dr-tight">Left out: ${esc(p.left_out_text)}.</p>` : ''}
        ${sample ? `<details><summary>First ${num((p.sample || []).length)} addresses</summary><ul class="cx-sample">${sample}</ul></details>` : ''}
      </div>`;
  }

  function exportedHtml() {
    const e = S.exported;
    if (!e) return '';
    return `
      <div class="cx-box good" role="status">
        <p><b>${esc(e.message)}</b></p>
        <div class="bar"><a class="btn primary" href="/api/crexi/batches/${Number(e.batch_id)}/file" download>Download the file (${num(e.count)} properties)</a></div>
        <p class="note dr-tight">Next: upload it to Crexi's bulk tool yourself. When Crexi sends its file back, drop it in step 2.${e.duplicates_left_out ? ` ${num(e.duplicates_left_out)} duplicates were left out.` : ''}</p>
      </div>`;
  }

  // ---- 2. import --------------------------------------------------------------------------------

  function dropZone() {
    return `<label class="im-drop" data-cx-drop>
      <input type="file" class="sr" accept=".xlsx,.xlsm,.csv,.tsv" data-cx-file>
      <span class="im-drop-t">Drop Crexi's file here</span>
      <span class="note">or <span class="im-pick">choose a file</span> (Excel or CSV). You see what it holds before anything is saved.</span>
    </label>`;
  }

  function importHtml() {
    const I = S.imp;
    let body;
    if (I.busy) body = W.loading(I.busy);
    else if (I.result) body = resultHtml(I.result);
    else if (I.preview) body = reviewHtml(I.preview);
    else body = dropZone();
    return `
      <section class="panel" aria-label="Import Crexi's file">
        <h2>2. I got Crexi's file back</h2>
        ${I.error ? `<p class="dr-form-err" role="alert">${esc(I.error)}</p>` : ''}
        ${body}
      </section>`;
  }

  function columnsHtml(p) {
    const c = p.columns || {};
    const list = (a) => (a && a.length ? a.map(esc).join(', ') : 'none found');
    const base = `<ul class="cx-cols"><li>Phones: <b>${list(c.phone)}</b></li><li>Emails: <b>${list(c.email)}</b></li>
      <li>Contact name: <b>${c.name ? esc(c.name) : 'none found'}</b></li></ul>`;
    const cur = S.imp.columns || { phone: c.phone || [], email: c.email || [], name: c.name || '' };
    const rows = (p.headers || []).map((h) => `<tr><td>${esc(h)}</td>
      <td><input type="checkbox" data-cx-col="phone" value="${esc(h)}" ${cur.phone.includes(h) ? 'checked' : ''} aria-label="${esc(h)} holds phones"></td>
      <td><input type="checkbox" data-cx-col="email" value="${esc(h)}" ${cur.email.includes(h) ? 'checked' : ''} aria-label="${esc(h)} holds emails"></td>
      <td><input type="radio" name="cx-name" data-cx-col="name" value="${esc(h)}" ${cur.name === h ? 'checked' : ''} aria-label="${esc(h)} is the contact name"></td></tr>`).join('');
    return `${base}
      <details ${S.imp.change ? 'open' : ''} data-cx-change><summary>Change which columns are phones, emails and the contact name</summary>
        <div class="dr-scroll"><table class="cx-pick"><thead><tr><th>Column</th><th>Phone</th><th>Email</th><th>Name</th></tr></thead><tbody>${rows}</tbody></table></div>
        <div class="bar"><button class="btn" data-cx="recheck">Update the preview</button></div>
      </details>`;
  }

  function reviewHtml(p) {
    const s = p.summary || {};
    const c = s.counts || {};
    const batches = (s.batches || []).map((b) => `<tr><td>#${Number(b.batch_id)}${b.kind === 'manual' ? ' (by hand)' : ''}</td><td class="num">${num(b.sent)}</td>
      <td class="num">${num(b.returned)}</td><td class="num">${num(b.missing)}</td><td class="num">${pct(b.matched_pct)}</td>
      <td>${b.closed ? '<span class="pill">Finished before: a late reply</span>' : esc(STATUS[b.status] || b.status)}</td></tr>`).join('');
    const warns = (p.warnings || []).map((w) => `<li>${esc(w)}</li>`).join('');
    const hand = Number(c.no_batch) || Number(c.hand)
      ? `<label class="cx-check"><input type="checkbox" data-cx-adopt ${S.imp.adopt ? 'checked' : ''}>
           <span>Record the ${num((Number(c.no_batch) || 0) + (Number(c.hand) || 0))} rows for properties that were in no batch as a hand batch (counts their credits)</span></label>` : '';
    return `
      <div class="cx-box">
        <p><b>${esc(p.message)}</b></p>
        ${batches ? `<div class="dr-scroll"><table class="cx-table"><thead><tr><th>Batch</th><th class="num">Sent</th><th class="num">Returned</th><th class="num">Missing</th><th class="num">Matched</th><th></th></tr></thead><tbody>${batches}</tbody></table></div>
          ${(s.batches || []).some((b) => !b.closed) ? `<p class="note dr-tight">Importing finishes ${(s.batches || []).length === 1 ? 'this batch' : 'these batches'}: the missing ones (Crexi said “Not Matched” or left them out) cost no credit and go to the DRILL's other sources right away.</p>` : ''}` : ''}
        <h3>What Crexi's file would add</h3>
        <p class="note dr-tight">${esc(p.enrichment_text || '')}</p>
        <h3>Columns I read</h3>${columnsHtml(p)}
        ${warns ? `<ul class="lines cx-warn">${warns}</ul>` : ''}
        ${hand}
        <div class="bar">
          <button class="btn primary dr-big" data-cx="apply" ${p.can_apply ? '' : 'disabled'}>Import it</button>
          <button class="btn" data-cx="again">Choose another file</button>
        </div>
        ${p.can_apply ? '' : '<p class="note dr-tight">Nothing in this file is new, so there is nothing to import.</p>'}
      </div>`;
  }

  function resultHtml(r) {
    const links = [];
    if (r.import_id && r.unmatched_file) links.push(`<a class="btn" href="/api/crexi/imports/${Number(r.import_id)}/files/unmatched" download>Rows that could not be matched</a>`);
    if (r.import_id && r.unreadable_file) links.push(`<a class="btn" href="/api/crexi/imports/${Number(r.import_id)}/files/unreadable" download>Phones and emails I could not read</a>`);
    const warns = (r.warnings || []).map((w) => `<li>${esc(w)}</li>`).join('');
    const left = Number(((S.status || {}).status || {}).left ?? (r.status || {}).left) || 0;
    const head = r.headline
      ? `<p class="cx-headline">${esc(r.headline)}</p>`
      : `<p><b>${esc(r.message)}</b></p>`;
    const missing = r.missing && r.import_id && r.missing_file
      ? `<div class="cx-missing"><p>${esc(r.missing_text || '')}</p>
          <a class="btn" href="/api/crexi/imports/${Number(r.import_id)}/files/missing" download>Download the ${num(r.missing)} missing (address, owner, why)</a></div>` : '';
    const credits = r.credits_text
      ? `<div class="cx-next"><p><b>${esc(r.credits_text)}</b></p>${left ? `<button class="btn primary" data-cx="use-count" data-n="${left}" data-go-export="1">Export the next ${num(left)}</button>` : ''}</div>` : '';
    return `
      <div class="cx-box good cx-result" role="status">
        ${head}${missing}${credits}
        ${r.headline && r.found != null ? `<p class="note dr-tight">${esc(`${num(r.found)} with a phone or email, ${num(r.owner_only)} with a name only, ${num(r.no_data)} with nothing.`)}</p>` : ''}
        ${r.enrichment_text ? `<p class="note dr-tight">${esc(r.enrichment_text)}</p>` : ''}
        ${warns ? `<ul class="lines cx-warn">${warns}</ul>` : ''}
        <div class="bar">${links.join('')}<button class="btn" data-cx="again">Import another file</button></div>
      </div>`;
  }

  // ---- batches still waiting --------------------------------------------------------------------

  function waitingHtml() {
    const open = (S.status || {}).open_batches || [];
    if (!open.length && !S.confirm) return '';
    const rows = open.map((b) => `
      <li class="cx-batch">
        <div><b>Batch #${Number(b.id)}</b> <span class="note">${esc(b.created_at ? BM.fmt.date(b.created_at) : '')} · ${num(b.sent)} sent, ${num(b.returned)} returned, ${num(b.waiting)} waiting${b.not_matched ? `, ${num(b.not_matched)} Crexi could not match` : ''}</span></div>
        <div class="bar">
          ${b.has_file !== false && b.file_name ? `<a class="btn" href="/api/crexi/batches/${Number(b.id)}/file" download>Download the file</a>` : ''}
          ${Number(b.returned) === 0 ? `<button class="btn" data-cx="ask-withdraw" data-id="${Number(b.id)}">Cancel: I never uploaded it</button>` : ''}
        </div>
        ${S.confirm && S.confirm.id === b.id ? confirmHtml() : ''}
      </li>`).join('');
    return `
      <section class="panel" aria-label="Batches waiting for Crexi">
        <h2>Batches waiting for Crexi</h2>
        <p class="note dr-tight">When Crexi's file comes back, drop it in step 2. That finishes the batch: what Crexi didn't have costs nothing and its credits are free again. Never uploaded a file? Cancel it.</p>
        <ul class="cx-batches">${rows}</ul>
      </section>`;
  }

  function confirmHtml() {
    const c = S.confirm;
    return `<div class="dr-banner warn cx-confirm" role="alert"><span>${esc(c.message)}</span>
      <span class="dr-short-acts"><button class="btn primary" data-cx="do-${c.kind}" data-id="${Number(c.id)}">Yes, cancel it</button>
      <button class="btn" data-cx="no-confirm">Not yet</button></span></div>`;
  }

  // ---- history ----------------------------------------------------------------------------------

  function who(v) {
    if (!v || v === 'cli') return 'Command line';
    return v;
  }

  function historyHtml() {
    const periods = ((S.history || {}).periods || []).filter((p) => p.current || p.batches.length || p.by_hand);
    if (!periods.length) return '';
    return `
      <section class="panel" aria-label="History">
        <h2>History</h2>
        <p class="note dr-tight">Every credit period, every export and every import. “Missing” means Crexi didn't have it (no credit spent). “Matched” is the share of what you sent that Crexi returned.</p>
        ${periods.map(periodHistory).join('')}
      </section>`;
  }

  function periodHistory(p) {
    const rows = p.batches.map((b) => {
      const files = [b.file_name ? `<a href="/api/crexi/batches/${Number(b.id)}/file" download>Export file</a>` : '',
        Number(b.missing) ? `<a href="/api/crexi/batches/${Number(b.id)}/missing" download>Missing list</a>` : ''];
      (b.imports || []).forEach((i) => {
        const sm = i.summary || {};
        if (sm.saved_as) files.push(`<a href="/api/crexi/imports/${Number(i.id)}/files/reply" download>Crexi's reply</a>`);
        if (sm.unmatched_file) files.push(`<a href="/api/crexi/imports/${Number(i.id)}/files/unmatched" download>Unmatched rows</a>`);
      });
      return `<tr>
        <td><b>#${Number(b.id)}</b> <span class="note">${esc(b.kind_label || '')}</span></td>
        <td>${esc(b.created_day || '')}</td><td class="cx-who">${esc(who(b.created_by))}</td>
        <td class="num">${num(b.sent)}</td>
        <td class="num" title="${num(b.found)} with a phone or email, ${num(b.owner_only)} with a name only, ${num(b.no_data)} with nothing">${num(b.returned)}</td>
        <td class="num">${num(b.missing)}${b.second_pass ? ` <span class="note">(${num(b.second_pass)} second pass)</span>` : ''}</td>
        <td class="num">${b.waiting ? num(b.waiting) : '—'}</td><td class="num">${pct(b.matched_pct)}</td>
        <td><span class="pill${b.status === 'closed' ? '' : ' hot'}">${esc(STATUS[b.status] || b.status)}</span></td>
        <td class="cx-files">${files.filter(Boolean).join(' · ') || '—'}</td></tr>`;
    }).join('');
    const imports = [];
    p.batches.forEach((b) => (b.imports || []).forEach((i) => imports.push({ b, i })));
    const seen = new Set();
    const detail = imports.filter(({ i }) => (seen.has(i.id) ? false : seen.add(i.id))).map(({ i }) => {
      const sm = i.summary || {};
      return `<li><b>${esc(i.file_name || 'Import')}</b> <span class="note">${esc(BM.fmt.date(i.created_at))} · ${esc(who(i.created_by))}</span><br>
        ${sm.headline ? `<b>${esc(sm.headline)}</b><br>` : ''}<span class="note">${esc(sm.enrichment_text || `${num(i.returned)} returned, ${num(i.not_matched)} not matched, ${num(i.duplicates)} duplicates, ${num(i.unmatched)} unmatched.`)}</span></li>`;
    }).join('');
    const totals = `${num(p.sent)} sent · ${num(p.returned)} returned · ${num(p.missing)} missing · ${pct(p.matched_pct)} matched${p.by_hand ? ` · ${num(p.by_hand)} used by hand` : ''}${p.waiting ? ` · ${num(p.waiting)} waiting` : ''}${p.second_passes ? ` · ${num(p.second_passes)} second pass` : ''}`;
    return `
      <div class="cx-period">
        <div class="dr-row-between"><h3>${esc(p.label)}${p.current ? ' <span class="pill hot">This period</span>' : ''}</h3>
          <span class="note num">${num(p.credits_used)} of ${num(p.per_period)} credits used</span></div>
        <p class="note dr-tight">${esc(totals)}</p>
        ${rows ? `<div class="dr-scroll"><table class="cx-table"><thead><tr><th>Batch</th><th>Date</th><th>Who</th><th class="num">Sent</th><th class="num">Returned</th><th class="num">Missing</th><th class="num">Waiting</th><th class="num">Matched</th><th>Status</th><th>Files</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : '<p class="note dr-tight">No batches yet this period.</p>'}
        ${detail ? `<details><summary>What each import found</summary><ul class="cx-imports">${detail}</ul></details>` : ''}
      </div>`;
  }

  // ---- do one by hand (the old way, folded away) ------------------------------------------------

  function handHtml(emptyMarket) {
    return `
      <details class="panel cx-hand" data-cx-hand ${S.hand.open ? 'open' : ''}>
        <summary>Do one by hand</summary>
        <p class="note dr-tight">The old way: open one property in Crexi, use a credit there, and record what it showed. Everything here counts toward the same credits.</p>
        ${S.hand.open ? handBody(emptyMarket) : ''}
      </details>`;
  }

  function handBody(emptyMarket) {
    const h = S.hand;
    if (h.error) return W.errorBox(h.error);
    if (!h.data) return W.loading('Loading the plan…');
    if (emptyMarket) return W.emptyMarket('The plan then lines up the best possible owners for your credits.');
    const plan = h.data.plan || [];
    const log = (h.data.log || []).filter((r) => r.result == null);
    const pending = log.length ? `<h3>Waiting for a result</h3><ul class="dr-log">${log.map((r) => `
      <li class="dr-log-row"><span class="dr-log-when note">${esc(W.when(r.created_at))}</span>
        <span class="dr-log-what"><button class="dr-linklike" data-cx="open" data-pid="${r.property_id}">${esc(W.address(r))}</button></span>
        <button class="btn" data-cx="use" data-pid="${r.property_id}">Record result</button>
        ${h.form === r.property_id ? creditForm(r.property_id, true) : ''}</li>`).join('')}</ul>` : '';
    const items = plan.map((it) => `
      <li class="dr-plan-item"><span class="dr-plan-n num">${num(it.order)}</span>
        <div class="dr-plan-body">
          <div class="dr-plan-top"><button class="dr-linklike dr-addr-sm" data-cx="open" data-pid="${it.property_id}">${esc(W.address(it))}</button>
            <span class="note">${esc(W.place(it))}</span></div>
          <p class="note dr-tight">${esc(it.reason || '')}</p>
          <div class="dr-plan-actions">${W.extLink(it.crexi_url, 'Open in Crexi ↗', 'btn') || '<span class="note">No Crexi link on this record.</span>'}
            <button class="btn" data-cx="use" data-pid="${it.property_id}">Used a credit</button></div>
          ${h.form === it.property_id ? creditForm(it.property_id, false) : ''}
        </div></li>`).join('');
    return `${pending}${items ? `<ol class="dr-plan">${items}</ol>` : '<p class="note">Nobody is lined up for a hand credit right now.</p>'}
      ${plan.length < (Number(h.data.planned) || 0) ? `<div class="bar"><button class="btn" data-cx="hand-more">Show more</button></div>` : ''}`;
  }

  function creditForm(pid, pending) {
    const field = (f, label, attrs = '') => `<label class="dr-field"><span>${label}</span><input data-f="${f}" autocomplete="off" ${attrs}></label>`;
    return `
      <form class="dr-credit-form" data-cx-form="credit" data-pid="${pid}" data-result="found" novalidate>
        <div class="dr-field"><span>What did Crexi show?</span>
          <div class="seg" role="group" aria-label="Result">
            <button type="button" data-cx="result" data-r="found" aria-pressed="true">Found contact info</button>
            <button type="button" data-cx="result" data-r="nothing" aria-pressed="false">Nothing useful</button>
          </div></div>
        <div class="dr-fields" data-when="found">${field('person', 'Person\'s name')}${field('phone', 'Phone', 'type="tel" inputmode="tel"')}
          ${field('email', 'Email', 'type="email" inputmode="email"')}${field('detail', 'Note (optional)')}</div>
        <p class="dr-form-err" role="alert" hidden></p>
        <div class="bar"><button class="btn primary" type="submit">${pending ? 'Save the result' : 'Save: 1 credit used'}</button>
          <button class="btn" type="button" data-cx="cancel-form">Cancel</button></div>
      </form>`;
  }

  // ---------------------------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------------------------

  const bodyCount = () => {
    const n = Number(S.count);
    return Number.isFinite(n) && n >= 1 ? Math.floor(n) : null;
  };

  async function postJson(path, body) {
    return BM.api(path, { method: 'POST', body });
  }

  async function doPreview() {
    S.exportError = '';
    S.exported = null;
    S.exportBusy = 'Working out who is in the batch…';
    paint(pageHtml());
    try {
      S.preview = await postJson('/api/crexi/export/preview', { count: bodyCount(), retry: S.retry });
    } catch (err) {
      S.preview = null;
      S.exportError = W.errText(err);
    }
    S.exportBusy = '';
    paint(pageHtml());
  }

  async function doExport() {
    S.exportError = '';
    S.exportBusy = 'Making the file…';
    paint(pageHtml());
    try {
      S.exported = await postJson('/api/crexi/export', { count: bodyCount(), retry: S.retry });
      S.preview = null;
      S.count = '';
      BM.toast(S.exported.message);
    } catch (err) {
      S.exportError = W.errText(err);
    }
    S.exportBusy = '';
    await reload();
  }

  function importForm() {
    const f = new FormData();
    f.append('file', S.imp.file, S.imp.file.name);
    if (S.imp.columns) f.append('columns', JSON.stringify(S.imp.columns));
    if (S.imp.adopt) f.append('adopt_hand', 'true');
    return f;
  }

  async function chooseFile(file) {
    if (!file) return;
    if (!/\.(xlsx|xlsm|csv|tsv)$/i.test(file.name)) {
      S.imp.error = 'Choose Crexi\'s file as .xlsx, .xlsm or .csv.';
      paint(pageHtml());
      return;
    }
    S.imp = { file, preview: null, columns: null, adopt: false, busy: 'Reading the file…', error: '', result: null, change: false };
    paint(pageHtml());
    await preview();
  }

  async function preview() {
    S.imp.busy = 'Matching every row to your batches…';
    S.imp.error = '';
    paint(pageHtml());
    try {
      S.imp.preview = await BM.api('/api/crexi/import/preview', { method: 'POST', form: importForm() });
    } catch (err) {
      S.imp.preview = null;
      S.imp.error = W.errText(err);
    }
    S.imp.busy = '';
    paint(pageHtml());
  }

  async function apply() {
    S.imp.busy = 'Importing…';
    paint(pageHtml());
    try {
      const res = await BM.api('/api/crexi/import', { method: 'POST', form: importForm() });
      S.imp.result = res;
      S.imp.preview = null;
      S.count = '';         // missing rows freed credits: "Export the next N" starts again at every free credit
      BM.toast(res.message);
    } catch (err) {
      S.imp.error = W.errText(err);
    }
    S.imp.busy = '';
    await reload();
  }

  async function askClose(id, kind) {
    try {
      const res = await postJson(`/api/crexi/batches/${id}/withdraw`, { confirm: false });
      S.confirm = { id, kind, message: res.message };
    } catch (err) {
      S.confirm = { id, kind, message: W.errText(err) };
    }
    paint(pageHtml());
  }

  async function doConfirm(id, kind) {
    try {
      const res = await postJson(`/api/crexi/batches/${id}/withdraw`, { confirm: true });
      BM.toast(res.message);
      S.confirm = null;
      S.preview = null;
      S.count = '';         // the cancelled batch's credits are free again: N starts at every free credit
    } catch (err) {
      S.confirm = { id, kind, message: W.errText(err) };
    }
    await reload();
  }

  async function loadHand() {
    S.hand.error = null;
    try {
      S.hand.data = await BM.api(W.mpath(`/credits?limit=${S.hand.limit}`));
    } catch (err) {
      S.hand.error = err;
    }
    paint(pageHtml());
  }

  function onToggle(e) {
    const d = e.target;
    if (!(d instanceof HTMLElement) || !d.matches('[data-cx-hand]')) return;
    if (d.open === S.hand.open) return;
    S.hand.open = d.open;
    if (d.open && !S.hand.data) { paint(pageHtml()); loadHand(); } else paint(pageHtml());
  }

  function onInput(e) {
    const t = e.target;
    if (t.matches('[data-cx-count]')) {
      S.count = t.value;
      S.root.querySelectorAll('[data-cx-n]').forEach((el) => { el.textContent = t.value; });
    }
  }

  function onChange(e) {
    const t = e.target;
    if (t.matches('[data-cx-retry]')) { S.retry = t.checked; S.preview = null; }
    else if (t.matches('[data-cx-adopt]')) { S.imp.adopt = t.checked; preview(); }
    else if (t.matches('[data-cx-file]')) { chooseFile(t.files && t.files[0]); t.value = ''; }
    else if (t.matches('[data-cx-col]')) {
      const cur = S.imp.columns || { phone: [], email: [], name: '' };
      const kind = t.dataset.cxCol;
      if (kind === 'name') cur.name = t.value;
      else cur[kind] = [...S.root.querySelectorAll(`[data-cx-col="${kind}"]:checked`)].map((x) => x.value);
      S.imp.columns = cur;
      S.imp.change = true;
    }
  }

  function onClick(e) {
    const b = e.target.closest('[data-cx], [data-act]');
    if (!b || !S.root || !S.root.contains(b) || b.disabled) return;
    const act = b.dataset.cx || b.dataset.act;
    const id = Number(b.dataset.id);
    const pid = Number(b.dataset.pid);
    if (act === 'retry') { e.preventDefault(); render(S.root); }
    else if (act === 'preview') { e.preventDefault(); doPreview(); }
    else if (act === 'use-count') {
      e.preventDefault();
      S.count = String(b.dataset.n);
      S.preview = null;
      const el = paint(pageHtml());
      if (b.dataset.goExport && el) {
        const form = el.querySelector('[data-cx-form="export"]');
        if (form) { form.scrollIntoView({ block: 'center' }); const inp = form.querySelector('[data-cx-count]'); if (inp) inp.focus(); }
      }
    }
    else if (act === 'apply') { e.preventDefault(); apply(); }
    else if (act === 'again') {
      e.preventDefault();
      S.imp = { file: null, preview: null, columns: null, adopt: false, busy: '', error: '', result: null, change: false };
      paint(pageHtml());
    } else if (act === 'recheck') { e.preventDefault(); S.imp.change = true; preview(); }
    else if (act === 'ask-withdraw') { e.preventDefault(); askClose(id, 'withdraw'); }
    else if (act === 'do-withdraw') { e.preventDefault(); doConfirm(id, 'withdraw'); }
    else if (act === 'no-confirm') { e.preventDefault(); S.confirm = null; paint(pageHtml()); }
    else if (act === 'open' && pid) { e.preventDefault(); BM.openProperty(pid); }
    else if (act === 'use' && pid) {
      e.preventDefault();
      S.hand.form = S.hand.form === pid ? null : pid;
      const y = window.scrollY;
      paint(pageHtml());
      window.scrollTo(0, y);
    } else if (act === 'cancel-form') { e.preventDefault(); S.hand.form = null; paint(pageHtml()); }
    else if (act === 'result') { e.preventDefault(); setResult(b); }
    else if (act === 'hand-more') { e.preventDefault(); S.hand.limit = S.hand.limit < 100 ? 100 : S.hand.limit + 200; loadHand(); }
  }

  function setResult(btn) {
    const form = btn.closest('form');
    if (!form) return;
    form.dataset.result = btn.dataset.r;
    form.querySelectorAll('[data-cx="result"]').forEach((x) => x.setAttribute('aria-pressed', String(x === btn)));
    form.querySelectorAll('[data-when="found"] [data-f]').forEach((x) => {
      if (x.dataset.f !== 'detail') x.closest('.dr-field').hidden = btn.dataset.r !== 'found';
    });
  }

  function formError(form, text) {
    const el = form.querySelector('.dr-form-err');
    if (!el) return;
    el.textContent = text;
    el.hidden = !text;
  }

  async function onSubmit(e) {
    const form = e.target;
    e.preventDefault();
    if (form.matches('[data-cx-form="export"]')) {
      if (bodyCount() == null) { S.exportError = 'Type how many properties to send.'; paint(pageHtml()); return; }
      doExport();
      return;
    }
    if (!form.matches('[data-cx-form="credit"]') || S.hand.busy) return;
    const pid = Number(form.dataset.pid);
    const result = form.dataset.result === 'nothing' ? 'nothing' : 'found';
    const f = W.fields(form);
    const contacts = result === 'found' ? W.contactsFrom(f) : [];
    if (result === 'found' && !contacts.length && !f.person) {
      formError(form, 'Type the phone, email or name Crexi showed, or choose Nothing useful.');
      return;
    }
    S.hand.busy = true;
    form.querySelectorAll('button').forEach((x) => { x.disabled = true; });
    try {
      const res = await BM.api(W.mpath(`/properties/${pid}/credit`), { method: 'POST',
        body: { result, person: result === 'found' ? f.person || null : null, contacts, detail: f.detail || null } });
      BM.toast((res && res.message) || 'Credit recorded.');
      S.hand.form = null;
      await Promise.all([loadHand(), reload()]);
    } catch (err) {
      formError(form, W.errText(err));
      form.querySelectorAll('button').forEach((x) => { x.disabled = false; });
    } finally {
      S.hand.busy = false;
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
