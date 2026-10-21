/* Exports (Setup): download each file for Brad, the CRM and the mail house. Every download is built
 * fresh from the current data (a copy also lands in output/<market>/ on the Mac Mini).
 */
(() => {
  'use strict';

  const BM = window.BM;
  const H = BM && BM.iv;
  if (!BM || !H) {
    console.error('exports.js needs core.js and funnel.js to load first.');
    return;
  }
  const {esc, toNum, plural} = H;

  /* [file, title, what it is for, count(summary) -> text or ''] */
  const FILES = [
    ['ranked_list.xlsx', 'Ranked list for Brad', 'Excel workbook for Brad and Google Sheets: the ranked buy box, every property, the set-aside ones, ' +
      'the DRILL worklist and a summary, each on its own sheet, with every original Crexi column.',
    (s) => (toNum(s.in_buybox) ? plural(s.in_buybox, 'property', 'properties') + ' ranked' : '')],
    ['ranked_list.csv', 'Ranked list (CSV)', 'The same list as one plain CSV, ranked ones first. Opens anywhere.',
      (s) => (toNum(s.properties) ? plural(s.properties, 'row') : '')],
    ['crm_handoff.csv', 'CRM handoff', 'Confirmed owners (level 3) to load into your CRM. Downloading it marks them as handed off.',
      (s) => plural(toNum(s.levels && s.levels['3']) || 0, 'confirmed owner')],
    ['mailing_list.csv', 'Mailing list', 'Names and mailing addresses for owners whose next step is a letter.', () => ''],
    ['drill_worklist.csv', 'DRILL worklist', 'What to do next for every owner still being worked, best first.', () => ''],
    ['excluded.zip', 'Set-aside properties', 'One CSV per filter (too far, wrong size, built too early...) with the reason. Nothing is ever deleted.',
      (s) => (toNum(s.excluded) ? plural(s.excluded, 'property', 'properties') : '')],
    ['run_summary.md', 'Run summary', 'A one-page report: counts per step and color, API calls and cost this month, and how far the credits reach.', () => ''],
  ];

  const busy = new Set();

  function fileRow([name, title, what, count], s) {
    const extra = count(s || {});
    const working = busy.has(name);
    return '<li class="ex-row"><div class="ex-text"><div class="ex-title"><b>' + esc(title) + '</b>' +
      '<code class="ex-name">' + esc(name) + '</code>' + (extra ? '<span class="pill">' + esc(extra) + '</span>' : '') + '</div>' +
      '<p class="note vw-tight">' + esc(what) + '</p></div>' +
      '<button type="button" class="btn' + (name === 'ranked_list.xlsx' ? ' primary' : '') + '" data-ex="' + esc(name) + '"' +
      (working ? ' disabled' : '') + '>' + (working ? 'Preparing…' : 'Download') + '</button></li>';
  }

  function heading() {
    return H.head('Setup · Exports', 'Files to share', [
      'Each download is built fresh from what is in the app right now.',
      'The ranked list is the one for Brad.',
    ]);
  }

  function sheetsNote() {
    return '<div class="panel ex-sheets"><h2>Put the ranked list in Google Sheets</h2>' +
      '<ol class="ex-steps"><li>Download <b>ranked_list.xlsx</b> above.</li>' +
      '<li>Open <a href="https://sheets.google.com" target="_blank" rel="noopener noreferrer">Google Sheets</a> and open Brad\'s sheet (or a blank one).</li>' +
      '<li>Choose <b>File &gt; Import &gt; Upload</b> and pick the file.</li>' +
      '<li>Pick <b>Replace spreadsheet</b> to refresh it, or <b>Insert new sheet(s)</b> to keep the old tabs.</li></ol>' +
      '<p class="note vw-tight">Copies are also saved on the Mac Mini in the output folder. They hold owner data, so they never go to GitHub.</p></div>';
  }

  function page(s) {
    return heading() + '<ul class="ex-list panel">' + FILES.map((f) => fileRow(f, s)).join('') + '</ul>' + sheetsNote();
  }

  function fileName(res, name) {
    const cd = res.headers.get('content-disposition') || '';
    const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
    try {
      if (m) return decodeURIComponent(m[1] || m[2]);
    } catch (e) { /* use the default below */ }
    return (BM.state.market || 'export') + '_' + name;
  }

  async function errorText(res) {
    try {
      const data = await res.json();
      if (data && typeof data.detail === 'string') return data.detail;
    } catch (e) { /* not JSON */ }
    return 'The download did not work (' + res.status + ').';
  }

  /** Fetch the file first, so a problem shows as a message instead of a page of JSON. */
  async function download(name) {
    const url = H.mpath('/export/' + encodeURIComponent(name));
    let res;
    try {
      res = await fetch(url, {credentials: 'same-origin'});
    } catch (e) {
      throw new Error('Can\'t reach the Blue Moon server. Check that it is still running on the Mac Mini.');
    }
    if (!res.ok) throw new Error(await errorText(res));
    const blob = await res.blob();
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href;
    a.download = fileName(res, name);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 60000);
  }

  async function onClick(root, ev) {
    const b = ev.target instanceof Element ? ev.target.closest('[data-ex]') : null;
    if (!b || b.disabled) return;
    const name = b.getAttribute('data-ex');
    busy.add(name);
    b.disabled = true;
    b.textContent = 'Preparing…';
    try {
      await download(name);
      BM.toast('Downloaded ' + name + '.');
      if (name === 'crm_handoff.csv') BM.refreshSummary().catch(() => {});
    } catch (err) {
      BM.toast(err.message, {error: true});
    } finally {
      busy.delete(name);
      if (b.isConnected) {
        b.disabled = false;
        b.textContent = 'Download';
      }
    }
  }

  async function render(root) {
    root.innerHTML = heading() + H.loading('Loading…');
    const s = await H.summary();
    if (!root.isConnected) return;
    if (H.isEmptyMarket(s)) {
      root.innerHTML = heading() + H.emptyMarket();
      return;
    }
    root.innerHTML = page(s || {});
    root.addEventListener('click', (ev) => onClick(root, ev));
  }

  BM.registerView('exports', {label: 'Exports', group: 'setup', order: 3, render});
})();
