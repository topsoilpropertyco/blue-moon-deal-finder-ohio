/* Exports (Setup): download the files, grouped as Work lists, Hand off, Fix data in a spreadsheet and Summary. Every download is built
 * fresh from the current data (a copy also lands in output/<market>/ on the Mac Mini).
 * "Fix data in a spreadsheet" explains the round trip (download, fix in Excel, drop it on Import) and offers
 * one set-aside group as a small review workbook (GET /export/review.xlsx?bucket=<group key>).
 */
(() => {
  'use strict';

  const BM = window.BM;
  const H = BM && BM.iv;
  if (!BM || !H) {
    console.error('exports.js needs core.js and funnel.js to load first.');
    return;
  }
  const {esc, toNum, num, plural} = H;

  /* The Exports screen, in four groups. [file, title, what it is, when to use it, count(summary) -> text or '']
     The file names are fixed (the server, Brad's Sheets and the tests use them); only the wording here changes. */
  const GROUPS = [
    {key: 'work', title: 'Work lists', blurb: 'The lists you work from day to day.', files: [
      ['ranked_list.xlsx', 'Ranked list (Excel)', 'Every property, best first, with the buy box, the set-aside ones and the DRILL worklist on their own sheets.',
        'you want to review the list, or open it in Google Sheets.',
        (s) => (toNum(s.in_buybox) ? plural(s.in_buybox, 'property', 'properties') + ' ranked' : '')],
      ['ranked_list.csv', 'Ranked list (plain CSV)', 'The same ranked list as one plain file.',
        'a tool cannot open Excel files.',
        (s) => (toNum(s.properties) ? plural(s.properties, 'row') : '')],
      ['drill_worklist.csv', 'DRILL worklist', 'What to do next for every owner still being worked, best first.',
        'you are about to make calls, write letters or spend Crexi credits.', () => ''],
      ['mailing_list.csv', 'Mailing list', 'Names and mailing addresses of owners whose next step is a letter.',
        'you are sending letters or giving the addresses to the mail house.', () => ''],
    ]},
    {key: 'handoff', title: 'Hand off', blurb: 'Files that leave Blue Moon: to your CRM, or to someone who checks the filters.', files: [
      ['crm_handoff.csv', 'CRM handoff', 'Confirmed owners (level 3), ready to load into your CRM.',
        'a conversation confirmed the owner. Downloading it marks them as handed off.',
        (s) => plural(toNum(s.levels && s.levels['3']) || 0, 'confirmed owner')],
      ['excluded.zip', 'Set-aside properties (zip)', 'One CSV per filter (too far, wrong size, built too early...) with the reason. Nothing is ever deleted.',
        'you want to double-check what the filters left out.',
        (s) => (toNum(s.excluded) ? plural(s.excluded, 'property', 'properties') : '')],
    ], links: [
      // Not a download here: the batch file is made on the Credits screen (it counts against the 500 credits).
      ['Crexi batch', 'The next batch of owners to send to Crexi, as a file to upload in Crexi\'s bulk tool. It is made on the Credits screen, ' +
        'where you also drop Crexi\'s reply back.', 'you are ready to spend Crexi credits.', 'credits', 'Open Credits',
        (s) => (toNum(s.credits && s.credits.left) !== null ? num(toNum(s.credits.left)) + ' credits free' : '')],
    ]},
  ];
  const SUMMARY = ['run_summary.md', 'Run summary', 'A one-page report: counts per step and color, API calls and cost this month, and how far the credits reach.',
    'you want the numbers at a glance, or something to paste into an update.', () => ''];

  const busy = new Set();

  function fileRow([name, title, what, when, count], s) {
    const extra = count(s || {});
    const working = busy.has(name);
    return '<li class="ex-row"><div class="ex-text"><div class="ex-title"><b>' + esc(title) + '</b>' +
      '<code class="ex-name">' + esc(name) + '</code>' + (extra ? '<span class="pill">' + esc(extra) + '</span>' : '') + '</div>' +
      '<p class="note vw-tight">' + esc(what) + ' <span class="ex-when">Use it when ' + esc(when) + '</span></p></div>' +
      '<button type="button" class="btn' + (name === 'ranked_list.xlsx' ? ' primary' : '') + '" data-ex="' + esc(name) + '"' +
      (working ? ' disabled' : '') + '>' + (working ? 'Preparing…' : 'Download') + '</button></li>';
  }

  function linkRow([title, what, when, go, label, count], s) {
    const extra = count(s || {});
    return '<li class="ex-row"><div class="ex-text"><div class="ex-title"><b>' + esc(title) + '</b>' +
      (extra ? '<span class="pill">' + esc(extra) + '</span>' : '') + '</div>' +
      '<p class="note vw-tight">' + esc(what) + ' <span class="ex-when">Use it when ' + esc(when) + '</span></p></div>' +
      '<button type="button" class="btn" data-go="' + esc(go) + '">' + esc(label) + '</button></li>';
  }

  function group(title, blurb, inner) {
    return '<section class="ex-group"><h2 class="vw-sub">' + esc(title) + '</h2><p class="note ex-blurb">' + esc(blurb) + '</p>' + inner + '</section>';
  }

  function heading() {
    return H.head('Setup · Exports', 'Files to download', [
      'Each download is built fresh from what is in the app right now.',
      'Pick the group that matches what you are about to do.',
    ]);
  }

  function sheetsNote() {
    return '<details class="ex-more"><summary>Put the ranked list in Google Sheets</summary>' +
      '<ol class="ex-steps"><li>Download <b>ranked_list.xlsx</b> above.</li>' +
      '<li>Open <a href="https://sheets.google.com" target="_blank" rel="noopener noreferrer">Google Sheets</a> and open Brad\'s sheet (or a blank one).</li>' +
      '<li>Choose <b>File &gt; Import &gt; Upload</b> and pick the file.</li>' +
      '<li>Pick <b>Replace spreadsheet</b> to refresh it, or <b>Insert new sheet(s)</b> to keep the old tabs.</li></ol>' +
      '<p class="note vw-tight">Copies are also saved on the Mac Mini in the output folder. They hold owner data, so they never go to GitHub.</p></details>';
  }

  /** The set-aside groups with properties in them, from the summary's Why? panel data. */
  function groupOptions(s) {
    const out = [];
    (((s && s.set_aside) || {}).reasons || []).forEach((r) => (r.buckets || []).forEach((b) => {
      if (toNum(b.count)) {
        out.push('<option value="' + esc(b.key) + '">' + esc((r.label || r.key) + ': ' + (b.label || b.key) + ' (' + num(b.count) + ')') + '</option>');
      }
    }));
    return out;
  }

  function roundTripNote(s) {
    const opts = groupOptions(s);
    const working = busy.has('review');
    return '<div class="panel ex-sheets">' +
      '<ol class="ex-steps"><li>Download <b>ranked_list.xlsx</b> above, or just one set-aside group below.</li>' +
      '<li>In Excel, fix <b>Units</b>, <b>Building sq ft</b>, <b>Year built</b>, <b>Property type</b>, <b>County</b>, <b>ZIP</b> or ' +
      '<b>Lot acres</b>. In <b>Buy box decision</b> you can type <b>Add to buy box</b>, <b>Set aside</b> or <b>Follow the rules</b>.</li>' +
      '<li>Save it and drop it on the Import screen. It shows what will change before anything is saved.</li></ol>' +
      (opts.length ? '<div class="ex-review"><label class="sr" for="ex-group">Set-aside group</label>' +
        '<select class="select" id="ex-group" data-ex-group>' + opts.join('') + '</select>' +
        '<button type="button" class="btn" data-ex="review"' + (working ? ' disabled' : '') + '>' +
        (working ? 'Preparing…' : 'Download this group') + '</button></div>' : '') +
      '<div class="bar vw-actions"><button class="btn quiet" type="button" data-go="import">Go to Import</button></div>' +
      '<details class="ex-more"><summary>What is read, and what is left alone</summary>' +
      '<p class="note vw-tight">Only those columns are read. Nothing is added or deleted, a row changed in the app after you ' +
      'downloaded the file is left alone, and a later Crexi import keeps your fixes.</p></details></div>';
  }

  function page(s) {
    const lists = GROUPS.map((g) => group(g.title, g.blurb,
      '<ul class="ex-list panel">' + g.files.map((f) => fileRow(f, s)).join('') + (g.links || []).map((l) => linkRow(l, s)).join('') + '</ul>' + (g.key === 'work' ? sheetsNote() : ''))).join('');
    return heading() + lists +
      group('Fix data in a spreadsheet', 'Download, correct it in Excel, and drop it back on Import. Use it when Crexi has a unit count, year or type wrong.', roundTripNote(s)) +
      group('Summary', 'The numbers on one page.', '<ul class="ex-list panel">' + fileRow(SUMMARY, s) + '</ul>');
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

  /** Fetch the file first, so a problem shows as a message instead of a page of JSON. A GET never changes
   *  anything, so the CRM handoff is POSTed with {mark: true}: that download marks the owners in it as handed off. */
  async function download(name, url) {
    url = url || H.mpath('/export/' + encodeURIComponent(name));
    const init = name === 'crm_handoff.csv'
      ? {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mark: true})}
      : {credentials: 'same-origin'};
    let res;
    try {
      res = await fetch(url, init);
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
    let url = null;
    if (name === 'review') {   // one set-aside group, to fix in Excel and import back
      const pick = root.querySelector ? root.querySelector('[data-ex-group]') : null;
      if (!pick || !pick.value) return;
      url = H.mpath('/export/review.xlsx') + '?bucket=' + encodeURIComponent(pick.value);
    }
    busy.add(name);
    b.disabled = true;
    b.textContent = 'Preparing…';
    try {
      await download(name === 'review' ? 'review.xlsx' : name, url);
      BM.toast(name === 'review' ? 'Downloaded the group. Fix it in Excel, then drop it on the Import screen.'
        : 'Downloaded ' + name + '.');
      if (name === 'crm_handoff.csv') BM.refreshSummary().catch(() => {});
    } catch (err) {
      BM.toast(err.message, {error: true});
    } finally {
      busy.delete(name);
      if (b.isConnected) {
        b.disabled = false;
        b.textContent = name === 'review' ? 'Download this group' : 'Download';
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
