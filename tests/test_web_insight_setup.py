"""The Insight and Setup screens: funnel.js, analytics.js, score.js, sources.js, importer.js, run.js,
exports.js, settings.js and views.css.

Static checks always run: each screen registers under the agreed key, group and order; every API path the
screens call exists in bluemoon/web/app.py; views.css uses only the design tokens.

When Node is installed, every screen is also rendered (with a tiny DOM stub) from real API answers: the made-up
fixture export is imported through the FastAPI TestClient and each endpoint's JSON is fed to the screens. That
checks the JavaScript against the real response shapes, including the import preview -> import flow and the
Run screen's "waiting for your OK" -> "Yes, spend it" flow. No browser, network or keys are needed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "bluemoon" / "web" / "static"
MARKET = "apartments-springfield-oh"
EMPTY = "self-storage-springfield-oh"
SPARE = "warehouses-springfield-oh"
FILES = ["funnel.js", "analytics.js", "score.js", "sources.js", "importer.js", "run.js", "exports.js", "settings.js"]
VIEWS = {   # key: (file, label, group, order)
    "funnel": ("funnel.js", "Funnel", "insight", 1),
    "analytics": ("analytics.js", "Analytics", "insight", 2),
    "score": ("score.js", "The score", "insight", 3),
    "import": ("importer.js", "Import", "setup", 1),
    "run": ("run.js", "Run", "setup", 2),
    "exports": ("exports.js", "Exports", "setup", 3),
    "settings": ("settings.js", "Settings", "setup", 4),
    "sources": ("sources.js", "Data sources", "setup", 5),
}
FAKE_KEY = "AIzaFAKE-never-shown-123"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# --- static checks ------------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(VIEWS))
def test_each_screen_registers_with_the_agreed_key_group_and_order(key):
    file, label, group, order = VIEWS[key]
    src = read(file)
    m = re.search(r"BM\.registerView\('" + key + r"',\s*\{\s*label:\s*'([^']+)',\s*group:\s*'(\w+)',\s*order:\s*(\d+)", src)
    assert m, f"{file} does not register '{key}'"
    assert (m.group(1), m.group(2), int(m.group(3))) == (label, group, order)


def test_screens_use_the_shared_helpers_and_never_load_outside_code():
    assert "BM.iv = {" in read("funnel.js")           # defined by the first Insight/Setup file to load
    for name in FILES[1:]:
        src = read(name)
        assert "BM.iv" in src and "needs core.js and funnel.js" in src, name
    for name in FILES:
        src = read(name)
        assert "<script" not in src and "import(" not in src and "eval(" not in src, name
        assert not re.search(r"fetch\(\s*['\"]https?:", src), name


def _api_literals() -> set[str]:
    """API paths named in the screens: H.mpath('/x') -> /api/m/{slug}/x, '/api/...' as written (query dropped)."""
    found: set[str] = set()
    for name in FILES:
        src = read(name)
        found |= {"/api/m/{slug}" + p for p in re.findall(r"mpath\('(/[^'?]*)", src)}
        found |= {p for p in re.findall(r"'(/api/[^'?]*)", src)}
    found.discard("/api/m/")      # the prefix itself (funnel.js builds market paths with it)
    return found


def test_every_api_path_the_screens_call_exists(tmp_root):
    from bluemoon.web.app import router     # the app's own routes (newer FastAPI wraps included routers)
    routes = [getattr(r, "path", "") for r in router.routes]
    literals = _api_literals()
    assert {"/api/m/{slug}/funnel", "/api/m/{slug}/analytics", "/api/m/{slug}/model", "/api/sources", "/api/settings",
            "/api/m/{slug}/import/preview", "/api/m/{slug}/import", "/api/m/{slug}/estimate", "/api/m/{slug}/jobs",
            "/api/jobs/", "/api/m/{slug}/export/"} <= literals
    for lit in literals:
        assert any(r == lit or (lit.endswith("/") and r.startswith(lit)) for r in routes), f"no route for {lit}"


def test_views_css_uses_only_the_design_tokens():
    css = read("views.css")
    assert re.findall(r"#[0-9A-Fa-f]{3,8}\b", css) == []
    assert not re.search(r"\brgba?\(", css)
    tokens = set(re.findall(r"(--[\w-]+):", read("styles.css").split("/* ---------- base ---------- */", 1)[0]))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    assert used and used <= tokens, used - tokens


@pytest.mark.parametrize("name", FILES)
def test_javascript_parses(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    result = subprocess.run([node, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# --- rendering every screen from real API answers --------------------------------------------------

HARNESS = r"""
const fs = require('fs');
const path = require('path');
const [fxPath, staticDir, filesCsv, outPath] = process.argv.slice(2);
const fx = JSON.parse(fs.readFileSync(fxPath, 'utf8'));
const out = {};
const toasts = [];
const calls = [];
const intervals = [];

class Element { closest() { return this; } }
class HTMLInputElement extends Element {}
class HTMLSelectElement extends Element {}
Object.assign(global, {Element, HTMLInputElement, HTMLSelectElement});
global.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o && o.detail; } };
global.setInterval = (fn) => { intervals.push(fn); return intervals.length; };
global.clearInterval = (id) => { if (id) intervals[id - 1] = null; };
global.window = {location: {hash: ''}, localStorage: {getItem: () => null, setItem: () => {}},
  addEventListener() {}, removeEventListener() {}, scrollTo() {}};
global.document = {readyState: 'loading', addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
  querySelector: () => null, visibilityState: 'visible'};
global.history = {replaceState() {}};
Object.defineProperty(global, 'navigator', {value: {clipboard: {writeText: async () => {}}}, configurable: true});

require(path.join(staticDir, 'core.js'));
const BM = window.BM;
const views = {};
BM.registerView = (key, def) => { views[key] = def; };
BM.api = async (p, opts) => {
  const o = opts || {};
  const key = (o.method || (o.form ? 'POST' : 'GET')) + ' ' + p;
  calls.push({key, body: o.body || null, form: !!o.form});
  if (!(key in fx.api)) throw new Error('no fixture for ' + key);
  return JSON.parse(JSON.stringify(fx.api[key]));
};
BM.refreshSummary = async () => BM.state.summary;
BM.toast = (m) => toasts.push(String(m));

function setMarket(slug) {
  Object.assign(BM.state, {market: slug, markets: fx.markets, summary: fx.summaries[slug], health: fx.health});
}
setMarket(fx.market);
filesCsv.split(',').forEach((f) => require(path.join(staticDir, f)));

function makeRoot() {
  const listeners = {};
  const parts = {};
  return {innerHTML: '', isConnected: true, parts, listeners,
    addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); }, removeEventListener() {},
    contains: () => true, querySelectorAll: () => [],
    querySelector(sel) {
      if (!parts[sel]) parts[sel] = {innerHTML: '', textContent: '', style: {}, querySelector: () => null,
        setAttribute() {}, classList: {toggle() {}, add() {}, remove() {}}};
      return parts[sel];
    }};
}
function text(root) {
  return root.innerHTML + Object.values(root.parts).map((p) => p.innerHTML + (p.textContent || '')).join('');
}
function target(cls, attrs) {
  const e = new cls();
  e.disabled = false;
  e.getAttribute = (k) => (k in attrs ? String(attrs[k]) : null);
  e.matches = () => true;
  Object.assign(e, attrs.props || {});
  return e;
}
async function fire(root, type, t) {
  (root.listeners[type] || []).forEach((f) => f({type, target: t, preventDefault() {}, dataTransfer: null, relatedTarget: null}));
  await settle();
}
async function settle() { for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0)); }
async function tick() { for (const fn of intervals.slice()) if (fn) await fn(); await settle(); }
async function render(key) {
  const root = makeRoot();
  await views[key].render(root, []);
  await settle();
  return root;
}

(async () => {
  out.registered = Object.keys(views).sort();
  for (const key of ['funnel', 'analytics', 'score', 'sources', 'import', 'run', 'exports', 'settings']) {
    out[key] = text(await render(key));
  }
  out.barsEscaped = BM.iv.bars([{label: '<img src=x>', value: 4}, {label: 'b', value: 1}], {drops: true});
  out.barsZero = BM.iv.bars([{label: 'a', value: 0}, {label: 'b', value: 0}], {drops: true});

  // Sources: changing a filter redraws the list.
  const so = await render('sources');
  await fire(so, 'change', target(HTMLSelectElement, {props: {dataset: {so: 'stage'}, value: 'entity_unmask'}}));
  out.sourcesFiltered = text(so);

  // Import: choose a file -> preview -> import.
  const im = await render('import');
  const file = new File([fs.readFileSync(fx.sampleCsv)], 'crexi_sample.csv', {type: 'text/csv'});
  await fire(im, 'change', target(HTMLInputElement, {props: {files: [file], value: ''}}));
  out.importPreview = text(im);
  await fire(im, 'click', target(Element, {'data-im': 'import'}));
  out.importResult = text(im);

  // Run: a big paid run waits for an OK, then "Yes, spend it" starts it again with confirm.
  const run = await render('run');
  out.runPhotos = text(run);
  await fire(run, 'click', target(Element, {'data-ru': 'run', 'data-step': 'drive'}));
  await tick();
  out.runWaiting = text(run);
  await fire(run, 'click', target(Element, {'data-ru': 'confirm', 'data-step': 'drive'}));
  out.runConfirmed = text(run);
  await fire(run, 'click', target(Element, {'data-ru': 'cancel', 'data-id': 901}));
  await fire(run, 'click', target(Element, {'data-ru': 'test', 'data-step': 'photos'}));
  // The one photo button: pressing it shows the estimate (a dry run); one "Yes, start it" starts the whole job.
  await fire(run, 'click', target(Element, {'data-ru': 'ph-ask'}));
  out.runPhotosEstimate = text(run);
  await fire(run, 'click', target(Element, {'data-ru': 'ph-go'}));
  out.runPhotosStarted = text(run);

  // A market with no properties: friendly empty states with a way to the Import screen.
  setMarket(fx.empty);
  for (const key of ['funnel', 'analytics', 'run', 'exports']) out['empty_' + key] = text(await render(key));

  out.calls = calls;
  out.toasts = toasts;
  fs.writeFileSync(outPath, JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _job(job_id: int, *, done: bool, params: dict, result: dict | None, message: str) -> dict:
    """A job as GET /api/jobs/{id} returns it (bluemoon/jobs.py JobRunner._view)."""
    return {"id": job_id, "kind": "drive", "label": "Drive times", "market": MARKET,
            "status": "done" if done else "running", "progress": 1.0 if done else 0.2, "message": message,
            "params": params, "created_at": "2026-10-21T08:00:00+00:00",
            "finished_at": "2026-10-21T08:00:05+00:00" if done else None, "result": result, "done": done}


@pytest.fixture
def fixtures(tmp_root, settings, sample_csv, monkeypatch, tmp_path) -> Path:
    """Real API answers for the made-up fixture export, saved as JSON for the Node harness."""
    from fastapi.testclient import TestClient

    from bluemoon.db import connect
    from bluemoon.web.app import create_app

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", FAKE_KEY)     # the Settings screen must never show a value
    connect(settings=settings).close()
    api: dict[str, object] = {}
    with TestClient(create_app()) as c:
        def get(path: str) -> dict:
            res = c.get(path)
            assert res.status_code == 200, (path, res.text)
            api["GET " + path] = res.json()
            return api["GET " + path]

        def upload(path: str, market_path: str) -> dict:
            with sample_csv.open("rb") as fh:
                res = c.post(path, files={"file": ("crexi_sample.csv", fh, "text/csv")})
            assert res.status_code == 200, (path, res.text)
            api["POST " + market_path] = res.json()
            return res.json()

        upload(f"/api/m/{MARKET}/import/preview", f"/api/m/{MARKET}/import/preview")
        upload(f"/api/m/{MARKET}/import", f"/api/m/{MARKET}/import")
        for rest in ("/funnel", "/analytics", "/model", "/estimate", "/photos/all", "/photos/all?estimate=1"):
            get(f"/api/m/{MARKET}{rest}")
        for path in ("/api/sources", "/api/settings", "/api/health", f"/api/jobs?market={MARKET}&limit=10",
                     f"/api/m/{EMPTY}/funnel", f"/api/m/{EMPTY}/analytics"):
            get(path)
        markets = c.get("/api/markets").json()
        summaries = {slug: c.get(f"/api/m/{slug}/summary").json() for slug in (MARKET, EMPTY)}
        health = api["GET /api/health"]

    waiting = {"step": "drive", "label": "Drive times", "ok": True, "dry_run": False, "needs_confirm": True,
               "message": "Waiting for your OK: Google Routes would be asked about 2,981 properties.",
               "warnings": [], "counts": {"cost_usd": 4.9},
               "estimate": {"cost": {"sku": "route_matrix_element", "units": 2981, "used_this_month": 0,
                                     "free_left": 10000, "billable": 0, "usd": 4.9}}}
    api[f"POST /api/m/{MARKET}/jobs"] = {"job_id": 901, "job": _job(901, done=False, params={}, result=None, message="Starting")}
    api["GET /api/jobs/901"] = _job(901, done=True, params={"confirm": False}, result=waiting,
                                    message=waiting["message"])
    api["POST /api/jobs/901/cancel"] = {**_job(901, done=False, params={"confirm": True}, result=None,
                                               message="Stopping at the next safe point..."), "status": "running"}
    data = {"api": api, "markets": markets, "summaries": summaries, "health": health, "market": MARKET,
            "empty": EMPTY, "sampleCsv": str(sample_csv)}
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def rendered(fixtures, tmp_path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    out = tmp_path / "rendered.json"
    result = subprocess.run([node, str(script), str(fixtures), str(STATIC), ",".join(FILES), str(out)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_every_screen_registers(rendered):
    assert rendered["registered"] == sorted(VIEWS)


def test_funnel(rendered):
    html = rendered["funnel"]
    for text in ("From raw list to reachable owner", "Find the property", "Reach the owner", "In buy box",
                 "Confirmed owner", "% move on", "DRILL work so far", "Crexi credits used"):
        assert text in html, text


def test_bars_escape_labels_and_skip_drops_after_zero(rendered):
    assert "&lt;img src=x&gt;" in rendered["barsEscaped"] and "<img" not in rendered["barsEscaped"]
    assert "25% move on" in rendered["barsEscaped"]
    assert "move on" not in rendered["barsZero"]


def test_analytics(rendered):
    html = rendered["analytics"]
    for text in ("What&#39;s in the data", "Last import:", "crexi_sample.csv", "Filter funnel", "set aside",
                 "How complete the data is", "Map location", "Properties by county", "Clark, OH",
                 "(sweet spot)", "Blue Moon scores", "Owner types", "Contact levels", "Level 1 · No owner data",
                 "Google and Claude this month"):
        assert text in html, text


def test_score(rendered):
    html = rendered["score"]
    for text in ("How the Blue Moon score works", "1.5%", "1 in 67", "Starting weights", "Owner type",
                 "×1.3", "×0.15", "Not yet", "How it learns", "How did it end?", "Blue Moon deal"):
        assert text in html, text


def test_sources(rendered):
    html = rendered["sources"]
    for text in ("Never automated", "people-search sites and Crexi", "County auditor parcel page", "Showing",
                 "Clark, OH", "Auditor deep link", "Rental registration"):
        assert text in html, text
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "LLC and trust lookup" in rendered["sourcesFiltered"]
    m = re.search(r"Showing (\d+) of (\d+) sources", rendered["sourcesFiltered"])
    assert m and 0 < int(m.group(1)) < int(m.group(2))


def test_import_preview_then_import(rendered):
    assert "Drop the Crexi file here" in rendered["import"] and "Re-importing is safe:" in rendered["import"]
    preview = rendered["importPreview"]
    for text in ("Check the file", "Crexi export", "Our field", "Their column", "Street address", "← Address",
                 "First rows, as they will be saved", "Import 12 properties", "Import the first 5 only (test)"):
        assert text in preview, text
    result = rendered["importResult"]
    for text in ("New properties", "Updated", "Skipped", "What ran automatically", "Flagged to check",
                 "See the ranked deals", 'data-go="deals"'):
        assert text in result, text
    posts = [c for c in rendered["calls"] if c["key"].startswith("POST ") and "/import" in c["key"]]
    assert [c["key"] for c in posts] == [f"POST /api/m/{MARKET}/import/preview", f"POST /api/m/{MARKET}/import"]
    assert all(c["form"] for c in posts)


def test_run_cards_and_the_ok_before_spending(rendered):
    html = rendered["run"]
    for text in ("Run the pipeline", "Owners", "Drive times", "Rent", "Photos", "Photo check", "Score",
                 "DRILL auto-stages", "Run everything", "Test on 5", "Estimate", "Recent jobs", "of 11 done"):
        assert text in html, text
    waiting = rendered["runWaiting"]
    assert "Waiting for your OK" in waiting and "Yes, spend it" in waiting and "$4.90" in waiting
    assert "Google Routes (drive times):</b> 2,981 calls" in waiting
    jobs = [c for c in rendered["calls"] if c["key"] == f"POST /api/m/{MARKET}/jobs"]
    assert [j["body"] for j in jobs] == [{"step": "drive"}, {"step": "drive", "confirm": True},
                                         {"step": "photos", "limit": 5}, {"step": "photos_all", "confirm": True}]
    keys = [c["key"] for c in rendered["calls"]]
    assert "GET /api/jobs/901" in keys and "POST /api/jobs/901/cancel" in keys
    assert any("Stopping at the next safe point" in t for t in rendered["toasts"])


def test_run_has_one_photo_button(rendered):
    html = re.sub(r"&#0?39;|&apos;", "'", rendered["runPhotos"])
    box = html.split("data-ru-photos", 1)[1].split("</section>", 1)[0]
    for text in ("Photos and the AI's call for every lead", "Get photos and the AI's walk-up call for every lead",
                 "Test on 5", " done", "It shows the cost first and starts on your OK", "carries on where it left off"):
        assert text in html, text
    for gone in ("this month", "Keep going", "Do the next", "monthly", "batch", "goal"):
        assert gone not in box, gone
    est = rendered["runPhotosEstimate"]
    assert "Before it starts" in est and "nothing called yet" in est and "Yes, start it" in est
    assert "buy box first" in est and "in all" in est
    assert any(t.startswith("Started: Photos and the AI") for t in rendered["toasts"])
    keys = [c["key"] for c in rendered["calls"]]
    assert f"GET /api/m/{MARKET}/photos/all" in keys and f"GET /api/m/{MARKET}/photos/all?estimate=1" in keys


def test_exports(rendered):
    html = rendered["exports"]
    for name in ("ranked_list.xlsx", "ranked_list.csv", "crm_handoff.csv", "mailing_list.csv", "drill_worklist.csv",
                 "excluded.zip", "run_summary.md"):
        assert f'data-ex="{name}"' in html, name
    assert "File &gt; Import &gt; Upload" in html and "marks them as handed off" in html


def test_settings_shows_keys_as_yes_no_only(rendered):
    html = rendered["settings"]
    for text in ("Markets", "Buy box: Apartments", "20–50 units", "Studio $800", "GOOGLE_MAPS_API_KEY",
                 "Photo check", "Google free usage this month", "Where the data lives", "data/bluemoon.sqlite",
                 "How to change a setting", "open -e config.yaml", "launchctl kickstart",
                 "Updates", "Version: <b>", "Last update: ", "uv run run.py autoupdate now"):
        assert text in html, text
    assert FAKE_KEY not in html
    assert FAKE_KEY not in json.dumps(rendered)


def test_empty_market_points_to_import(rendered):
    for key in ("funnel", "analytics", "run", "exports"):
        html = rendered["empty_" + key]
        assert "No properties in this market yet" in html, key
        assert 'data-go="import"' in html, key


EXPORTS_HARNESS = r"""
const path = require('path');
const staticDir = process.argv[2];
const calls = [];
const toasts = [];
class Element {}
global.Element = Element;
global.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o && o.detail; } };
global.window = {location: {hash: ''}, localStorage: {getItem: () => null, setItem: () => {}},
  addEventListener() {}, removeEventListener() {}, scrollTo() {}};
global.document = {readyState: 'loading', addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
  querySelector: () => null, visibilityState: 'visible',
  createElement: () => ({click() {}, remove() {}}), body: {appendChild() {}}};
global.history = {replaceState() {}};
global.URL.createObjectURL = () => 'blob:x';
global.URL.revokeObjectURL = () => {};
global.fetch = async (url, init) => {
  calls.push({url, method: (init && init.method) || 'GET', body: (init && init.body) || null,
    type: ((init && init.headers) || {})['Content-Type'] || null});
  return {ok: true, status: 200, headers: {get: () => null}, blob: async () => ({})};
};
require(path.join(staticDir, 'core.js'));
const BM = window.BM;
const views = {};
BM.registerView = (key, def) => { views[key] = def; };
BM.toast = (m) => toasts.push(String(m));
BM.refreshSummary = async () => null;
Object.assign(BM.state, {market: 'm1', summary: {market: {slug: 'm1'}, properties: 3, in_buybox: 1, levels: {3: 1}}});
require(path.join(staticDir, 'funnel.js'));
require(path.join(staticDir, 'exports.js'));
(async () => {
  const listeners = [];
  const root = {innerHTML: '', isConnected: true, addEventListener: (t, f) => listeners.push(f)};
  await views.exports.render(root, []);
  for (const name of ['ranked_list.csv', 'crm_handoff.csv']) {
    const b = new Element();
    Object.assign(b, {disabled: false, isConnected: true, textContent: '', closest: () => b,
      getAttribute: (k) => (k === 'data-ex' ? name : null)});
    await Promise.all(listeners.map((f) => f({target: b})));
  }
  console.log(JSON.stringify({calls, toasts}));
  process.exit(0);   // exports.js revokes the blob URL after a minute; don't wait for that timer
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_crm_handoff_download_posts_mark_so_owners_are_marked(tmp_path):
    """A GET never changes data, so the Exports screen POSTs {mark: true} for crm_handoff.csv (the screen says
    downloading it marks the owners as handed off); every other file is a plain GET."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path / "exports_harness.js"
    script.write_text(EXPORTS_HARNESS, encoding="utf-8")
    res = subprocess.run([node, str(script), str(STATIC)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    calls = {c["url"]: c for c in out["calls"]}
    plain = calls["/api/m/m1/export/ranked_list.csv"]
    assert plain["method"] == "GET" and plain["body"] is None
    handoff = calls["/api/m/m1/export/crm_handoff.csv"]
    assert handoff["method"] == "POST" and handoff["type"] == "application/json"
    assert json.loads(handoff["body"]) == {"mark": True}
    assert out["toasts"] == ["Downloaded ranked_list.csv.", "Downloaded crm_handoff.csv."]
