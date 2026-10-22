/* Import (Setup): drop a Crexi CSV or XLSX export.
 * Step 1 previews it (detected source, sheet, rows, how its columns map to ours, sample rows) without
 * saving anything. Step 2 imports it, then shows what changed and what ran automatically (owners, free
 * drive estimates, score, DRILL). Re-importing is safe: it merges and never deletes.
 * A Blue Moon export Seth fixed in Excel (provider 'bluemoon') gets its own preview: what would change, from
 * the editable columns only; its import saves just those changes (buy-box decisions can be undone here).
 */
(() => {
  'use strict';

  const BM = window.BM;
  const H = BM && BM.iv;
  if (!BM || !H) {
    console.error('importer.js needs core.js and funnel.js to load first.');
    return;
  }
  const {esc, toNum, num, plural} = H;

  const ACCEPT = '.csv,.tsv,.xlsx,.xlsm,text/csv,text/tab-separated-values,' +
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel.sheet.macroEnabled.12';
  const OK_EXT = /\.(csv|tsv|xlsx|xlsm)$/i;
  const TEST_ROWS = 5;
  /* The fields the import screen cares most about (bluemoon/ingest/columns.py KEY_FIELDS). */
  const KEY_FIELDS = ['address', 'city', 'state', 'zip', 'county', 'apn', 'lat', 'lng', 'units', 'building_sqft',
    'year_built', 'owner_name', 'mailing_address'];
  const FLAG_TEXT = {
    year_unknown: 'No year built',
    year_invalid: 'Impossible year built (like 0 or 9999), left blank',
    units_unknown: 'No unit count',
    units_from_addresses: 'Unit count taken from the number of addresses',
    shared_apn: 'Parcel number shared with another property or Crexi record (check it)',
    no_owner_name: 'No owner name',
    no_coordinates: 'No map location yet (the drive step looks it up)',
    no_address: 'No street address',
  };
  const SAMPLE_COLS = [['address', 'Address'], ['city', 'City'], ['zip', 'ZIP'], ['county', 'County'],
    ['units', 'Units'], ['building_sqft', 'Sq ft'], ['year_built', 'Built'], ['flags', 'Flags']];

  /* One import at a time. Reset when the market changes. */
  const S = {root: null, market: null, file: null, preview: null, result: null, busy: '', error: null, history: null,
    undone: false, askClears: false};

  function reset() {
    Object.assign(S, {file: null, preview: null, result: null, busy: '', error: null, undone: false, askClears: false});
  }

  /* ---------- pieces ---------- */

  function marketName() {
    const m = H.currentMarket();
    return m ? m.name || (m.asset_label + ' · ' + m.geography) : 'this market';
  }

  function heading() {
    return H.head('Setup · Import', 'Bring in a Crexi export', [
      'Export Property Records from Crexi as CSV or Excel, then drop the file here.',
      '<b>Re-importing is safe:</b> it merges with what is here and never deletes anything.',
      'Fixed a Blue Moon export in Excel? Drop it here too: only the columns you can fix are read.',
      'Importing into <b>' + esc(marketName()) + '</b>. Switch markets in the top bar.',
    ]);
  }

  function dropZone() {
    return '<label class="im-drop" data-im-drop>' +
      '<input type="file" class="sr" accept="' + ACCEPT + '" data-im-file>' +
      '<span class="im-drop-t">Drop the Crexi file here</span>' +
      '<span class="note">or <span class="im-pick">choose a file</span> (CSV or XLSX). Nothing is saved until you press Import.</span>' +
      '</label>';
  }

  function busyBox(text) {
    return '<div class="panel im-busy">' + H.loading(text) + '</div>';
  }

  function errorPanel(err) {
    return '<div class="errbox" role="alert"><p>' + esc((err && err.message) || String(err)) + '</p>' +
      '<button class="btn small" type="button" data-im="again">Choose another file</button></div>';
  }

  /* ---------- step 1: preview ---------- */

  function previewFacts(p) {
    const facts = [
      ['Looks like', p.provider_label || p.provider || 'A property list'],
      ['File', p.filename || (S.file && S.file.name) || ''],
    ];
    if (p.sheet) facts.push(['Sheet', p.sheet]);
    facts.push(['Rows', num(p.rows)]);
    facts.push(['Columns matched', num(Object.keys(p.column_map || {}).length) + ' of ' + num((p.headers || []).length)]);
    return '<dl class="kv">' + facts.map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>').join('') + '</dl>';
  }

  function mapRow(field, p, missing) {
    const label = (p.column_labels || {})[field] || (missing.get(field) || field);
    const theirs = (p.column_map || {})[field];
    if (!theirs) {
      return '<tr class="im-missing"><td>' + esc(label) + '</td><td><span class="dot y" aria-hidden="true"></span>Not in this file</td></tr>';
    }
    return '<tr><td>' + esc(label) + '</td><td>← ' + esc(theirs) + '</td></tr>';
  }

  function mappingTable(p) {
    const map = p.column_map || {};
    const missing = new Map((p.missing_fields || []).map((m) => [m.field, m.label]));
    const keys = KEY_FIELDS.filter((f) => map[f] || missing.has(f));
    missing.forEach((_, f) => { if (!keys.includes(f)) keys.push(f); });
    const others = Object.keys(map).filter((f) => !keys.includes(f));
    const table = (fields) => '<div class="vw-scroll"><table class="wt im-map"><thead><tr><th>Our field</th><th>Their column</th></tr></thead><tbody>' +
      fields.map((f) => mapRow(f, p, missing)).join('') + '</tbody></table></div>';
    const unmapped = p.unmapped_columns || [];
    const skipped = p.skipped_columns || [];
    return table(keys) +
      (others.length ? '<details class="im-more"><summary>' + plural(others.length, 'other matched column') + '</summary>' + table(others) + '</details>' : '') +
      (unmapped.length ? '<details class="im-more"><summary>' + plural(unmapped.length, 'column') + ' kept as they are</summary>' +
        '<p class="note">Not matched to one of our fields, but saved with every property under its own name: ' + esc(unmapped.join(', ')) + '.</p></details>' : '') +
      (skipped.length ? '<p class="note">Skipped (formulas or blank headers): ' + esc(skipped.join(', ')) + '.</p>' : '');
  }

  function missingNote(p) {
    const missing = p.missing_fields || [];
    if (!missing.length) return '<p class="note vw-tight"><span class="dot g" aria-hidden="true"></span>Every key field was found.</p>';
    return '<p class="note vw-tight"><span class="dot y" aria-hidden="true"></span>Not in this file: ' +
      esc(missing.map((m) => m.label).join(', ')) + '. That is OK: missing data never rules a property out. ' +
      'If a column is matched wrong, set it in config.yaml under crexi: column_overrides.</p>';
  }

  function sampleCell(row, key) {
    if (key === 'flags') {
      const flags = Array.isArray(row.flags) ? row.flags : [];
      return flags.length ? flags.map((f) => '<span class="pill" title="' + esc(FLAG_TEXT[f] || f) + '">' + esc(f.replace(/_/g, ' ')) + '</span>').join(' ') : '';
    }
    const v = row[key];
    if (v === null || v === undefined || v === '') return '<span class="faint">—</span>';
    if (key === 'units' || key === 'building_sqft') return num(v);
    return esc(BM.fmt.title && typeof v === 'string' ? BM.fmt.title(v) : v);
  }

  function sampleTable(p) {
    const rows = (p.sample || []).slice(0, 5);
    if (!rows.length) return '';
    return '<h3>First rows, as they will be saved</h3><div class="vw-scroll"><table class="wt im-sample"><thead><tr>' +
      SAMPLE_COLS.map(([, l]) => '<th>' + esc(l) + '</th>').join('') + '</tr></thead><tbody>' +
      rows.map((r) => '<tr>' + SAMPLE_COLS.map(([k]) => '<td>' + sampleCell(r, k) + '</td>').join('') + '</tr>').join('') +
      '</tbody></table></div>';
  }

  function warningsList(list, title) {
    const items = (list || []).filter(Boolean);
    if (!items.length) return '';
    return '<div class="im-warn"><h3>' + esc(title || 'Notes') + '</h3><ul>' + items.map((w) => '<li>' + esc(w) + '</li>').join('') + '</ul></div>';
  }

  function previewPanel(p) {
    if (p.provider === 'bluemoon') return bluemoonPreview(p);
    const rows = toNum(p.rows) || 0;
    const test = rows > TEST_ROWS ? '<button class="btn quiet" type="button" data-im="import" data-limit="' + TEST_ROWS + '">Import the first ' + TEST_ROWS + ' only (test)</button>' : '';
    return '<div class="panel im-step"><div class="im-stephead"><span class="im-n">1</span><h2>Check the file</h2></div>' +
      previewFacts(p) + missingNote(p) + mappingTable(p) + sampleTable(p) + warningsList(p.warnings) + '</div>' +
      '<div class="panel im-step"><div class="im-stephead"><span class="im-n">2</span><h2>Import it</h2></div>' +
      '<p class="note vw-tight">Matching properties are updated, new ones are added, nothing is deleted. ' +
      'Then the free steps run on their own: owners, drive estimates, score and DRILL.</p>' +
      '<div class="bar vw-actions"><button class="btn primary" type="button" data-im="import">Import ' + plural(rows, 'property', 'properties') + '</button>' +
      test + '<button class="btn" type="button" data-im="again">Choose a different file</button></div></div>';
  }

  /* ---------- a Blue Moon export with Seth's edits ---------- */

  function editValue(v) {
    if (v === null || v === undefined || v === '') return '<span class="faint">blank</span>';
    return esc(String(v));
  }

  function changesTable(changes, more) {
    const rows = changes || [];
    if (!rows.length) return '';
    return '<h3>What will change</h3><div class="vw-scroll"><table class="wt im-sample"><thead><tr><th>Property</th>' +
      '<th>Column</th><th>Now</th><th>From the file</th></tr></thead><tbody>' +
      rows.map((c) => '<tr><td>' + esc(c.address || ('Property ' + c.property_id)) + '</td><td>' + esc(c.label || c.field) +
        '</td><td>' + (c.field === 'buybox' ? '<span class="faint">—</span>' : editValue(c.old)) + '</td><td>' +
        editValue(c.new) + '</td></tr>').join('') + '</tbody></table></div>' +
      (more > 0 ? '<p class="note">… and ' + plural(more, 'more change') + '.</p>' : '');
  }

  function bluemoonFacts(p) {
    const facts = [['Looks like', p.provider_label || 'Blue Moon export'], ['File', p.filename || (S.file && S.file.name) || '']];
    if (p.sheet) facts.push(['Read from', p.sheet]);
    facts.push(['Properties in it', num(p.rows)]);
    return '<dl class="kv">' + facts.map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>').join('') + '</dl>';
  }

  function editCount(e) {
    const fields = Object.values((e && e.fields_changed) || {}).reduce((a, n) => a + (toNum(n) || 0), 0);
    const decisions = Object.values((e && e.decisions_changed) || {}).reduce((a, n) => a + (toNum(n) || 0), 0);
    return fields + decisions;
  }

  /* Empty cells that erase values: above safety.confirm_clears_over the first Save click asks once more. */
  function clearsAsk(e) {
    const clears = Object.values(e.clears || {}).reduce((a, n) => a + (toNum(n) || 0), 0);
    return '<div class="im-warn"><h3>Erase ' + plural(clears, 'value') + '?</h3><p>This file has ' +
      plural(clears, 'empty cell') + ' where the app has a value. Saving erases ' + (clears === 1 ? 'it' : 'them') +
      ', and a later Crexi import keeps ' + (clears === 1 ? 'it' : 'them') + ' blank.</p></div>';
  }

  function bluemoonPreview(p) {
    const e = p.edits || {};
    const n = toNum(e.properties_changed) || 0;
    const shown = (e.changes || []).length;
    const label = 'Save the changes to ' + plural(n, 'property', 'properties');
    let save = n ? '<button class="btn primary" type="button" data-im="import">' + label + '</button>' : '';
    let ask = '';
    if (n && e.confirm_clears) {
      ask = S.askClears ? clearsAsk(e) : '';
      save = S.askClears
        ? '<button class="btn primary" type="button" data-im="import">Yes, erase them and save</button>'
        : '<button class="btn primary" type="button" data-im="ask-clears">' + label + '</button>';
    }
    return '<div class="panel im-step"><div class="im-stephead"><span class="im-n">1</span><h2>Check your edits</h2></div>' +
      bluemoonFacts(p) + '<p class="vw-tight">' + esc(p.message || '') + '</p>' + changesTable(e.changes, editCount(e) - shown) +
      warningsList(p.warnings) + '</div>' +
      '<div class="panel im-step"><div class="im-stephead"><span class="im-n">2</span><h2>Import it</h2></div>' +
      '<p class="note vw-tight">Only Units, Building sq ft, Year built, Property type, County, ZIP, Lot acres and Buy box decision ' +
      'are read. Owner names, scores and the original Crexi columns are never changed, and no property is added. ' +
      'Each change is saved with your name, and a later Crexi import keeps it.</p>' + ask +
      '<div class="bar vw-actions">' + save + '<button class="btn" type="button" data-im="again">Choose a different file</button></div></div>';
  }

  function bluemoonResult(r) {
    const decisions = Object.values(r.decisions_changed || {}).reduce((a, n) => a + (toNum(n) || 0), 0);
    const tiles = [BM.ui.stat(toNum(r.properties_changed) || 0, 'Properties changed'),
      BM.ui.stat(decisions, 'Buy box decisions')];
    if (toNum(r.skipped_stale)) tiles.push(BM.ui.stat(r.skipped_stale, 'Left alone (changed in the app since)'));
    const away = (toNum(r.unknown_ids) || 0) + Object.values(r.other_market || {}).reduce((a, n) => a + (toNum(n) || 0), 0);
    if (away) tiles.push(BM.ui.stat(away, 'Not in this market'));
    const undo = r.batch_id && !S.undone
      ? '<button class="btn" type="button" data-im="undo" data-batch="' + esc(r.batch_id) + '">Undo the buy box decisions</button>' : '';
    const ok = !r.after_import || r.after_import.ok !== false;
    return '<div class="panel im-step im-done"><div class="im-stephead"><span class="im-n ' + (ok ? 'ok' : '') + '">' + (ok ? '✓' : '!') + '</span>' +
      '<h2>Imported your edits</h2></div><div class="strip vw-strip">' + tiles.join('') + '</div>' +
      '<p class="vw-tight">' + esc(r.message || '') + '</p>' + warningsList(r.warnings) + autoSteps(r.after_import) +
      '<div class="bar vw-actions"><button class="btn primary" type="button" data-go="deals">See the deals</button>' + undo +
      '<button class="btn" type="button" data-im="again">Import another file</button></div></div>';
  }

  /* ---------- step 2: result ---------- */

  function flagsList(counts) {
    const keys = Object.keys(counts || {}).filter((k) => toNum(counts[k]));
    if (!keys.length) return '';
    return '<h3>Flagged to check</h3><ul class="im-flags">' + keys.map((k) => '<li><span class="num">' + num(counts[k]) + '</span> ' +
      esc(FLAG_TEXT[k] || k.replace(/_/g, ' ')) + '</li>').join('') + '</ul>';
  }

  function autoSteps(after) {
    if (!after) return '';
    const steps = (after.steps || []).map((s) => '<li class="' + (s.ok ? 'hit' : 'miss') + '"><div><span class="src">' + esc(s.label || s.step) +
      '</span> <span class="res">· ' + esc(s.message || (s.ok ? 'Done' : 'Did not finish')) + '</span></div></li>').join('');
    return '<h3>What ran automatically</h3><p class="vw-tight">' + esc(after.message || '') + '</p>' +
      (steps ? '<ul class="timeline im-auto">' + steps + '</ul>' : '') + warningsList(after.warnings, 'Notes from those steps');
  }

  function resultPanel(r) {
    if (r.provider === 'bluemoon') return bluemoonResult(r);
    // The tiles add up to the rows read: duplicates are part of rows_skipped, so they get their own tile,
    // and rows already stored with the same values are not counted as 'Updated'.
    const unchanged = toNum(r.rows_unchanged) || 0;
    const tiles = [BM.ui.stat(toNum(r.rows_new) || 0, 'New properties'),
      BM.ui.stat(Math.max(0, (toNum(r.rows_updated) || 0) - unchanged), 'Updated')];
    if (unchanged) tiles.push(BM.ui.stat(unchanged, 'Already here, no change'));
    const dup = toNum(r.duplicates_collapsed) || 0;
    const noId = toNum(r.rows_without_id) !== null ? toNum(r.rows_without_id) : Math.max(0, (toNum(r.rows_skipped) || 0) - dup);
    if (dup) tiles.push(BM.ui.stat(dup, 'Duplicate rows combined'));
    tiles.push(BM.ui.stat(noId, 'Skipped (no address or parcel)'));   // shown at 0 too: nothing was dropped
    const ok = !r.after_import || r.after_import.ok !== false;
    return '<div class="panel im-step im-done"><div class="im-stephead"><span class="im-n ' + (ok ? 'ok' : '') + '">' + (ok ? '✓' : '!') + '</span>' +
      '<h2>Imported ' + esc(r.filename ? r.filename.replace(/^\d{8}-\d{6}_/, '') : 'the file') + '</h2></div>' +
      '<div class="strip vw-strip">' + tiles.join('') + '</div>' +
      '<p class="note vw-tight">' + plural(r.rows_total, 'row') + ' read. Nothing was deleted. A copy of the file is kept on the Mac Mini in data/crexi.</p>' +
      flagsList(r.flag_counts) + warningsList(r.warnings) + autoSteps(r.after_import) +
      '<div class="bar vw-actions"><button class="btn primary" type="button" data-go="deals">See the ranked deals</button>' +
      '<button class="btn" type="button" data-im="again">Import another file</button></div></div>';
  }

  /* ---------- history ---------- */

  function historyHtml() {
    if (S.history === null) return H.loading('Loading past imports…');
    if (S.history instanceof Error) return '<p class="note">' + esc(S.history.message) + '</p>';
    if (!S.history.length) return '<p class="note">No imports in this market yet.</p>';
    return '<ul class="im-history">' + S.history.map((h) => '<li><div><b>' + esc(String(h.filename || '').replace(/^\d{8}-\d{6}_/, '')) + '</b>' +
      '<span class="note"> · ' + esc(BM.fmt.ago ? BM.fmt.ago(h.created_at) : BM.fmt.date(h.created_at)) + '</span></div>' +
      '<div class="note">' + plural(h.rows_total, 'row') + ': ' + num(h.rows_new) + ' new, ' + num(h.rows_updated) + ' updated, ' +
      num(h.rows_skipped) + ' combined or skipped' + '</div>' +
      (h.notes ? '<details class="im-more"><summary>Notes</summary><p class="note">' + esc(h.notes).replace(/\n/g, '<br>') + '</p></details>' : '') +
      '</li>').join('') + '</ul>';
  }

  async function loadHistory() {
    const market = S.market;
    let history;
    try {
      const data = await BM.api(H.mpath('/analytics'));
      history = (data && data.imports) || [];
    } catch (err) {
      history = err;
    }
    if (S.market !== market) return;
    S.history = history;
    const box = S.root && S.root.isConnected ? S.root.querySelector('[data-im-history]') : null;
    if (box) box.innerHTML = historyHtml();
  }

  /* ---------- page ---------- */

  function stepHtml() {
    if (S.busy) return busyBox(S.busy);
    if (S.error) return errorPanel(S.error);
    if (S.result) return resultPanel(S.result);
    if (S.preview) return previewPanel(S.preview);
    return dropZone();
  }

  /** Repaint the step area of whichever Import screen is showing (a re-render mid-import still gets the result). */
  function paint() {
    const box = S.root && S.root.isConnected ? S.root.querySelector('[data-im-step]') : null;
    if (box) box.innerHTML = stepHtml();
  }

  function page() {
    return heading() + '<div class="im-wrap"><div data-im-step>' + stepHtml() + '</div>' +
      '<section class="vw-sec"><h2 class="vw-sub">Past imports</h2><div data-im-history>' + historyHtml() + '</div></section></div>';
  }

  /* ---------- actions ---------- */

  function fileForm(limit) {
    const form = new FormData();
    form.append('file', S.file, S.file.name);
    if (limit) form.append('limit', String(limit));
    return form;
  }

  /** Run a request for the current market; drop its answer if Seth switched markets meanwhile. */
  async function forThisMarket(request, apply) {
    const market = S.market;
    let value;
    let error = null;
    try {
      value = await request();
    } catch (err) {
      error = err;
    }
    if (S.market !== market) return false;
    S.busy = '';
    if (error) S.error = error;
    else apply(value);
    paint();
    return !error;
  }

  async function choose(file) {
    if (!file) return;
    reset();
    if (!OK_EXT.test(file.name)) {
      S.error = new Error(/\.xls$/i.test(file.name)
        ? 'Old .xls files can\'t be read. Open it in Excel, save it as .xlsx or .csv, and drop that.'
        : 'That isn\'t a CSV or Excel (.xlsx) file. Export Property Records from Crexi as CSV or Excel.');
      paint();
      return;
    }
    S.file = file;
    S.busy = 'Reading ' + file.name + '…';
    paint();
    await forThisMarket(() => BM.api(H.mpath('/import/preview'), {method: 'POST', form: fileForm()}),
      (preview) => { S.preview = preview || {}; });
  }

  async function runImport(limit) {
    if (!S.file) return;
    const rows = toNum(S.preview && S.preview.rows) || 0;
    const n = limit ? Math.min(limit, rows || limit) : rows;
    S.busy = 'Importing ' + plural(n, 'row') + ', then ranking them (owners, drive estimates, score, DRILL). Big files take a minute…';
    paint();
    const done = await forThisMarket(() => BM.api(H.mpath('/import'), {method: 'POST', form: fileForm(limit)}),
      (result) => { S.result = result || {}; S.preview = null; });
    if (!done) return;
    BM.toast(S.result.provider === 'bluemoon' ? (S.result.message || 'Imported your edits.')
      : 'Imported: ' + num(S.result.rows_new) + ' new, ' + num(S.result.rows_updated) + ' updated.');
    BM.refreshSummary({fresh: true}).catch(() => {});
    S.history = null;
    loadHistory();
  }

  function onClick(root, ev) {
    const b = ev.target instanceof Element ? ev.target.closest('[data-im]') : null;
    if (!b || S.busy) return;
    ev.preventDefault();
    const act = b.getAttribute('data-im');
    if (act === 'again') {
      reset();
      paint();
      const input = root.querySelector('[data-im-file]');
      if (input) input.click();
    } else if (act === 'ask-clears') {
      S.askClears = true;
      paint();
    } else if (act === 'import') {
      runImport(toNum(b.getAttribute('data-limit')));
    } else if (act === 'undo') {
      undoDecisions(b.getAttribute('data-batch'));
    }
  }

  /** Put back the buy-box decisions the imported file changed (one batch, like the Deals screen's Undo). */
  async function undoDecisions(batch) {
    if (!batch || !S.result) return;
    const result = S.result;
    S.busy = 'Undoing the buy box decisions from this file…';
    paint();
    await forThisMarket(() => BM.api(H.mpath('/buybox/undo'), {method: 'POST', body: {batch_id: batch}}), (res) => {
      S.undone = true;
      result.message = (res && res.message) || 'Undone.';
      BM.toast(result.message);
      BM.refreshSummary({fresh: true}).catch(() => {});
    });
  }

  function wireDrop(root) {
    const over = (on) => {
      const zone = root.querySelector('[data-im-drop]');
      if (zone) zone.classList.toggle('over', on);
    };
    root.addEventListener('change', (ev) => {
      if (ev.target instanceof HTMLInputElement && ev.target.matches('[data-im-file]')) {
        choose(ev.target.files && ev.target.files[0]);
        ev.target.value = '';
      }
    });
    root.addEventListener('dragover', (ev) => {
      if (!root.querySelector('[data-im-drop]')) return;
      ev.preventDefault();
      over(true);
    });
    root.addEventListener('dragleave', (ev) => {
      if (!root.contains(ev.relatedTarget)) over(false);
    });
    root.addEventListener('drop', (ev) => {
      ev.preventDefault();
      over(false);
      if (!S.busy && root.querySelector('[data-im-drop]')) choose(ev.dataTransfer && ev.dataTransfer.files[0]);
    });
    // A file dropped just outside the box must not make the browser leave the app to open it.
    const guard = (ev) => {
      if (!root.isConnected) {
        window.removeEventListener('dragover', guard);
        window.removeEventListener('drop', guard);
        return;
      }
      if (ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('Files')) ev.preventDefault();
    };
    window.addEventListener('dragover', guard);
    window.addEventListener('drop', guard);
  }

  async function render(root) {
    if (S.market !== BM.state.market) {
      S.market = BM.state.market;
      reset();
      S.history = null;
    }
    S.root = root;
    root.innerHTML = page();
    root.addEventListener('click', (ev) => onClick(root, ev));
    wireDrop(root);
    await loadHistory();
  }

  BM.registerView('import', {
    label: 'Import', group: 'setup', order: 1, render,
    badge: (s) => (s && s.properties === 0 ? 'Start' : null),
  });
})();
