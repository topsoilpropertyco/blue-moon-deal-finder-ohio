"""The buy-box decision screens in deals.js and property.js, run under Node with a small fake DOM.

Covers the Why? panel's groups (Review / Decide), the Review list of one group (#deals/review/<key>), the
Decide choices and their Undo, a row's 'Add to buy box' (which must never open the drawer, by click or by
Enter), the phone card markup (no button inside a button) and the drawer's Buy box section. The data is made
up. Skipped when Node isn't installed (the dashboard never needs Node).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "bluemoon" / "web" / "static"

HARNESS = r"""
const [coreJs, propertyJs, dealsJs] = process.argv.slice(2);
const docListeners = {};
class Element {}
class Stub extends Element {
  constructor(sel) {
    super();
    this.sel = sel; this.innerHTML = ''; this.outerHTML = ''; this.textContent = ''; this.value = '';
    this.listeners = {}; this.hidden = false; this.disabled = false; this.isConnected = true; this.style = {};
    this.attrs = {}; this.children = {};
    this.classList = {add() {}, remove() {}, toggle() {}, contains() { return false; }};
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  querySelector(sel) { return this.root ? this.child(sel) : null; }
  child(sel) { if (!this.children[sel]) { this.children[sel] = new Stub(sel); this.children[sel].root = true; } return this.children[sel]; }
  querySelectorAll() { return []; }
  closest() { return null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  insertAdjacentHTML(where, html) { this.innerHTML += html; }
  scrollIntoView() {}
  focus() {}
  remove() {}
  appendChild() {}
}
/* An event target with these attributes (and tag); closest() matches '[attr]', '[attr="v"]' and tag names. */
function target(attrs, tag) {
  const t = new Stub(tag || 'button');
  t.tagName = (tag || 'button').toUpperCase();
  t.attrs = Object.assign({}, attrs);
  t.closest = (sel) => sel.split(',').map((s) => s.trim()).some((s) => {
    const m = /^(\w+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/.exec(s.split(' ').pop());
    if (!m) return false;
    if (m[1] && m[1].toUpperCase() !== t.tagName) return false;
    if (m[2] && !(m[2] in t.attrs)) return false;
    if (m[3] !== undefined && t.attrs[m[2]] !== m[3]) return false;
    return !!(m[1] || m[2]);
  }) ? t : null;
  return t;
}
const made = [];
global.Element = Element;
global.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o && o.detail; } };
global.window = {location: {hash: '', hostname: 'localhost'}, localStorage: {getItem: () => null, setItem: () => {}}};
global.history = {replaceState: () => {}};
global.document = {
  readyState: 'loading',
  addEventListener: (k, f) => { (docListeners[k] = docListeners[k] || []).push(f); },
  dispatchEvent: (ev) => { (docListeners[ev.type] || []).forEach((f) => f(ev)); },
  querySelector: () => null, getElementById: () => null,
  createElement: (tag) => { const el = new Stub(tag); made.push(el); return el; },
  body: {appendChild() {}},
};
require(coreJs);
const BM = window.BM;
global.BM = BM;              // in a browser window.BM is a global name
let dealsView = null;
const realRegister = BM.registerView;
BM.registerView = (key, def) => { if (key === 'deals') dealsView = def; realRegister(key, def); };
require(propertyJs);
require(dealsJs);

const KEY = 'drive_time:76-90';
const summary = {
  market: {slug: 'apartments-test', name: 'Apartments test', max_drive_minutes: 60, origin_label: 'Downtown Testville'},
  asset_class: {key: 'apartments', label: 'Apartments', size_unit: 'units', size_ok: [10, 80], size_ideal: [20, 50],
    year_built_min: 1970, visual_enabled: false, rent_enabled: false},
  properties: 11, in_buybox: 6, levels: {1: 2, 2: 4, 3: 0}, levels_in_buybox: {1: 2, 2: 4, 3: 0}, credits: {left: 500},
  thresholds: {moon_score: 0.1, funnel_score: 0.05, long_owned_years: 15}, views: [], sorts: ['rank', 'score'],
  set_aside: {total: 5, several: 3, drive_estimated: 0, drive_close_calls: 0, close_call_minutes: 15, max_drive_minutes: 60,
    by_hand: {added: 1, set_aside: 0},
    reasons: [
      {key: 'drive_time', label: 'More than 60 min drive', count: 5, only: 2, still_aside: 4, added: 1, kept: 0,
        explain: "More than 60 minutes' drive from Downtown Testville.",
        buckets: [
          {key: 'drive_time:61-75', label: '61–75 min', count: 2, only: 0, still_aside: 2, added: 0, kept: 0, estimated: 0},
          {key: KEY, label: '76–90 min', count: 3, only: 2, still_aside: 2, added: 1, kept: 0, estimated: 0}]},
      {key: 'skip_county', label: 'County on the skip list', count: 1, only: 0, still_aside: 1, added: 0, kept: 0, waived: 1,
        explain: 'Counties Brad said to skip, even inside the drive limit: Sampleton (OH).',
        buckets: [{key: 'skip_county:oh-sampleton', label: 'Sampleton County, OH', count: 1, only: 0, still_aside: 1, added: 0, kept: 0, waived: 1, estimated: 0}]},
    ]},
};
function row(id, extra) {
  return Object.assign({id, address: id + ' FAKE MEADOW CT', city: 'TESTVILLE', state: 'OH', score: 0.08, units: 24,
    size_value: 24, drive_minutes: 80, drive_method: 'estimate', level: 2, drill_status: 'active', channels: {},
    coverage_have: 5, coverage_total: 9, rank: null, in_buybox: false, exclusion_reasons: ['drive_time'],
    rule_reasons: ['drive_time'], by_hand: null, aside_buckets: [KEY]}, extra || {});
}
const items = [row(7), row(8, {in_buybox: true, rank: 3, exclusion_reasons: [], by_hand: 'added'})];
const review = {bucket: KEY, reason: 'drive_time', reason_label: 'More than 60 min drive', label: '76–90 min',
  explain: "More than 60 minutes' drive from Downtown Testville.", count: 3, still_aside: 2, added: 1, kept: 0, only: 2};
const calls = [];
const opened = [];
const toasts = [];
BM.state.market = 'apartments-test';
BM.state.summary = summary;
BM.refreshSummary = () => Promise.resolve(summary);
BM.openProperty = (id) => opened.push(String(id));
BM.toast = (m) => toasts.push(String(m));
BM.api = async (path, opts) => {
  const o = opts || {};
  calls.push({path, method: o.method || 'GET', body: o.body || null});
  if (path.includes('/buybox/decide')) return {batch_id: 'b1', changed: 2, now_in: 2, still_out: [], message: '2 of 3 are now in the buy box.', set_aside: summary.set_aside, in_buybox: 8};
  if (path.includes('/buybox/undo')) return {batch_id: 'b2', changed: 2, message: 'Undone. 3 properties are back the way they were.', set_aside: summary.set_aside, in_buybox: 6};
  if (/\/properties\/7\/buybox$/.test(path)) return {row: row(7, {in_buybox: true, exclusion_reasons: [], by_hand: 'added'}), message: 'Added to the buy box by hand.'};
  if (/\/properties\/7$/.test(path)) return detail;
  if (path.includes('/properties?')) return {total: 2, items, counts: {all: 6}, review: path.includes('bucket=') ? review : null};
  throw new Error('unexpected ' + path);
};
const detail = Object.assign(row(7), {raw: {}, signals: [], drill: {level: 2, status: 'active', stages: [], contacts: []},
  buybox: {in_buybox: false, rules_in_buybox: false, exclusion_reasons: ['drive_time'], rule_reasons: ['drive_time'],
    rules: [{reason: 'drive_time', label: 'Too far to drive', detail: 'about 80 min drive; the limit is 60 min', waived: false, kept: false}],
    waived: [], kept: [], manual: null, decisions: [], history: [{id: 1, reason: '*', decision: 'cleared', previous: 'include', actor: 'this Mac',
      created_at: '2026-10-21T12:00:00+00:00', note: 'Undo of b20261021T120000-abc123', reason_label: 'The whole property'}],
    groups: [{key: KEY, reason: 'drive_time', label: 'More than 60 min drive: 76–90 min'}], size_detail: '24 units', flags: []}});

const flush = () => new Promise((r) => setTimeout(r, 0));
const click = async (stub, attrs, tag) => { const ev = {type: 'click', target: target(attrs, tag), preventDefault() {}};
  (stub.listeners.click || []).forEach((f) => f(ev)); await flush(); await flush(); };
const out = {};

(async () => {
  // 1. The Why? panel with groups, Review and Decide.
  let root = new Stub('root'); root.root = true;
  await dealsView.render(root, []);
  const why = root.child('[data-why-wrap]');
  out.panel = why.innerHTML;
  out.listQuery = calls.filter((c) => c.path.includes('/properties?')).pop().path;
  await click(why, {'data-decide-open': KEY});
  out.sheet = why.innerHTML;
  calls.length = 0;
  await click(why, {'data-decide': 'add_only', 'data-bucket': KEY});
  out.decideCall = calls[0];
  out.reloaded = calls.some((c) => c.path.includes('/properties?'));
  const toastEl = made.find((el) => /bb-toast/.test(el.className || ''));
  out.undoToast = toastEl ? toastEl.innerHTML : null;
  calls.length = 0;
  if (toastEl) await click(toastEl, {'data-undo-batch': 'b1'});
  out.undoCall = calls[0];
  out.undoneToast = toastEl ? toastEl.innerHTML : null;
  await click(why, {'data-decide-open': 'skip_county:oh-sampleton'});
  out.waivedSheet = why.innerHTML;
  await click(why, {'data-decide-open': 'skip_county:oh-sampleton'});
  await click(why, {'data-review-group': KEY});
  out.hashAfterReview = window.location.hash;

  // 2. The Review list of one group.
  calls.length = 0;
  root = new Stub('root'); root.root = true;
  await dealsView.render(root, ['review', KEY]);
  out.reviewQuery = calls.filter((c) => c.path.includes('/properties?')).pop().path;
  out.reviewHead = root.child('[data-review]').innerHTML;
  out.reviewWhy = root.child('[data-why-wrap]').innerHTML;
  out.status = root.child('[data-status]').textContent;
  out.body = root.child('[data-body]').innerHTML;
  await click(root.child('[data-review]'), {'data-decide-open': KEY});
  out.reviewSheet = root.child('[data-review]').innerHTML;

  // 3. A row's Add to buy box: never opens the drawer (click or Enter); Enter on the row itself does.
  const body = root.child('[data-body]');
  calls.length = 0;
  await click(body, {'data-row-act': 'include', 'data-pid': '7'});
  out.rowCall = calls[0];
  out.rowAfter = root.child('tr[data-id="7"]').outerHTML;
  out.cardAfter = root.child('[data-card="7"]').outerHTML;
  const key = (attrs, tag) => (body.listeners.keydown || []).forEach((f) => f({type: 'keydown', key: 'Enter', target: target(attrs, tag), preventDefault() {}}));
  key({'data-row-act': 'include', 'data-pid': '7'}, 'button');
  out.openedByButton = opened.slice();
  key({'data-id': '7'}, 'tr');
  out.openedByRow = opened.slice();
  await click(root.child('[data-review]'), {'data-review-exit': ''});
  out.hashAfterExit = window.location.hash;

  // 4. The drawer's Buy box section.
  const drawer = new Stub('drawer'); drawer.root = true;
  await BM.renderProperty(drawer, 7);
  out.drawer = drawer.innerHTML;
  Object.assign(detail, {in_buybox: true, by_hand: 'added', rank: 4, exclusion_reasons: []});
  Object.assign(detail.buybox, {in_buybox: true, exclusion_reasons: [], manual: 'include',
    decisions: [{reason: '*', reason_label: 'The whole property', decision: 'include', bucket: KEY, note: null,
      decided_by: 'this Mac', decided_at: '2026-10-21T12:00:00+00:00', detail_at_decision: 'Too far to drive: about 80 min drive'}]});
  await BM.renderProperty(drawer, 7);
  out.drawerAdded = drawer.innerHTML;
  calls.length = 0;
  await click(drawer, {'data-act': 'buybox', 'data-decision': 'rules'});
  out.drawerCall = calls[0];
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def ui(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path_factory.mktemp("node") / "buybox_ui.js"
    script.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(script), str(STATIC / "core.js"), str(STATIC / "property.js"), str(STATIC / "deals.js")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_why_panel_lists_each_reason_with_its_groups(ui):
    panel = ui["panel"]
    assert "(1 added by you)" in panel
    assert "More than 60 minutes&#39; drive from Downtown Testville." in panel     # the plain explanation
    for key in ("drive_time:61-75", "drive_time:76-90", "skip_county:oh-sampleton"):
        assert f'data-review-group="{key}"' in panel and f'data-decide-open="{key}"' in panel
    assert "Only for this reason: 2" in panel and "1 added by you" in panel
    assert "bucket=" not in ui["listQuery"]


def test_decide_offers_the_choices_and_says_how_to_change_the_rule(ui):
    sheet = ui["sheet"]
    assert "Add the 2 set aside only for this" in sheet
    assert "The other 1 also have another reason, so they stay set aside" in sheet
    assert "Add all 3 (also the 1 with other reasons)" in sheet
    assert "Keep them set aside" in sheet and "Any Add you made from this group is taken back." in sheet
    assert "Nothing else changes" not in sheet
    assert "Undo my decisions for this group" in sheet            # the group has a decision already
    assert "photos, photo checks and Crexi credits" in sheet
    assert "<code>max_drive_minutes: 60</code>" in sheet


def test_a_waived_rule_shows_in_the_panel_and_can_be_cleared(ui):
    # 'Stop counting this rule' on a group whose members all have another reason: nothing is added, so the
    # panel says the rule no longer counts and offers the way back.
    sheet = ui["waivedSheet"]
    assert "this rule no longer counts, still set aside for other reasons" in sheet
    assert 'data-decide="clear" data-bucket="skip_county:oh-sampleton"' in sheet
    assert "Undo my decisions for this group" in sheet


def test_a_decision_posts_the_group_and_can_be_undone(ui):
    assert ui["decideCall"] == {"path": "/api/m/apartments-test/buybox/decide", "method": "POST",
                                "body": {"bucket": "drive_time:76-90", "decision": "add_only"}}
    assert ui["reloaded"]
    assert "2 of 3 are now in the buy box." in ui["undoToast"] and 'data-undo-batch="b1"' in ui["undoToast"]
    assert ui["undoCall"] == {"path": "/api/m/apartments-test/buybox/undo", "method": "POST", "body": {"batch_id": "b1"}}
    assert "Undone." in ui["undoneToast"] and "data-undo-batch" not in ui["undoneToast"]


def test_review_lists_one_group_with_its_own_header(ui):
    assert ui["hashAfterReview"] == "#deals/review/drive_time%3A76-90"
    assert "bucket=drive_time%3A76-90" in ui["reviewQuery"]
    head = ui["reviewHead"]
    assert "Reviewing: 3 properties 76–90 min away" in head
    assert "data-review-exit" in head and "Back to all deals" in head
    assert "Decide for all 3" in head
    assert ui["reviewWhy"] == ""                                   # the panel steps aside while reviewing
    assert "in this group" in ui["status"]
    assert "Add the 2 set aside only for this" in ui["reviewSheet"]
    assert ui["hashAfterExit"] == "#deals"


def test_rows_have_buy_box_buttons_and_cards_never_nest_buttons(ui):
    body = ui["body"]
    assert 'data-row-act="include" data-pid="7"' in body           # set aside: Add to buy box
    assert "Added by you" in body and 'data-row-act="rules" data-pid="8"' in body
    for card in body.split('<div class="card-row')[1:]:
        inner = card.split('<button type="button" class="card', 1)[1].split("</button>", 1)[0]
        assert "<button" not in inner


def test_row_button_acts_without_opening_the_drawer(ui):
    assert ui["rowCall"] == {"path": "/api/m/apartments-test/properties/7/buybox", "method": "POST",
                             "body": {"decision": "include"}}
    assert "Added by you" in ui["rowAfter"] and "Added by you" in ui["cardAfter"]
    assert ui["openedByButton"] == []
    assert ui["openedByRow"] == ["7"]


def test_drawer_buy_box_section(ui):
    d = ui["drawer"]
    assert 'data-part="buybox"' in d and "<h2>Buy box</h2>" in d
    assert "Set aside by the rules." in d and "Too far to drive" in d and "about 80 min drive" in d
    assert 'data-act="buybox" data-decision="include"' in d        # Add to buy box
    assert 'data-act="bb-review" data-bucket="drive_time:76-90"' in d
    assert "Back to the rules (was: added to the buy box)" in d and "on the Mac" in d and "an Undo" in d
    assert "b20261021T120000" not in d                             # batch ids stay out of sight
    a = ui["drawerAdded"]
    assert "In the buy box: added by you" in a                      # header pill and verdict
    assert "Added to the buy box · on the Mac, Oct 21, 2026" in a
    assert 'data-decision="exclude"' in a and 'data-decision="rules"' in a and "Undo: follow the rules" in a
    assert ui["drawerCall"] == {"path": "/api/m/apartments-test/properties/7/buybox", "method": "POST",
                                "body": {"decision": "rules"}}


def test_the_paths_the_screens_post_to_exist():
    from bluemoon.web.app import router
    routes = {(getattr(r, "path", ""), m) for r in router.routes for m in (getattr(r, "methods", None) or ())}
    for path in ("/api/m/{slug}/buybox/decide", "/api/m/{slug}/buybox/undo", "/api/m/{slug}/properties/{pid}/buybox"):
        assert (path, "POST") in routes, path
    src = (STATIC / "deals.js").read_text(encoding="utf-8") + (STATIC / "property.js").read_text(encoding="utf-8")
    assert "BM.m('/buybox/decide')" in src and "BM.m('/buybox/undo')" in src and "'/buybox'" in src
