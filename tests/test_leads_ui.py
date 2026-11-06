"""The Add a property panel, the batch panel and the drawer lines (leads.js), run under Node with a small fake DOM:
the exact requests sent, unsure matches waiting for an answer, left-out rows sent as excluded, and nothing from the
server written into the page unescaped. Made-up data. Skipped when Node isn't installed."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "bluemoon" / "web" / "static"

HARNESS = r"""
const [coreJs, pricingJs, leadsJs] = process.argv.slice(2);
class Element {}
class Stub extends Element {
  constructor(sel) {
    super();
    this.sel = sel; this.innerHTML = ''; this.listeners = {}; this.attrs = {}; this.children = {}; this.lists = {}; this.cleared = 0;
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  querySelector(sel) { if (!this.children[sel]) this.children[sel] = new Stub(sel); return this.children[sel]; }
  querySelectorAll(sel) { return this.lists[sel] || []; }
  replaceChildren(el) { if (el) this.child = el; else this.cleared++; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  focus() {}
}
function target(attrs) {
  const t = new Stub('t');
  t.attrs = Object.assign({}, attrs);
  t.closest = (sel) => sel.split(',').map((s) => s.trim()).some((s) => { const m = /^\[([\w-]+)\]$/.exec(s); return m && m[1] in t.attrs; }) ? t : null;
  return t;
}
global.Element = Element;
global.window = {location: {hash: '', hostname: 'localhost'}, localStorage: {getItem: () => null, setItem: () => {}}};
global.history = {replaceState: () => {}};
global.document = {readyState: 'loading', addEventListener() {}, dispatchEvent() {}, querySelector: () => null, getElementById: () => null,
  createElement: (tag) => {
    const el = new Stub(tag);
    const form = new Stub('form');
    form.elements = {address: {value: '', focus() {}}, city: {value: ''}, state: {value: ''}, zip: {value: ''}, link: {value: ''}};
    el.children['[data-ld-form]'] = form;
    return el;
  }, body: {appendChild() {}}};
require(coreJs);
const BM = window.BM;
global.BM = BM;
BM.registerView = () => {};
require(pricingJs);
require(leadsJs);

const calls = [];
const toasts = [];
const opened = [];
BM.state.market = 'apartments-test';
BM.toast = (m) => toasts.push(String(m));
BM.openProperty = (id) => opened.push(id);
let next = null;
BM.api = async (path, opts) => {
  const o = opts || {};
  calls.push({path, method: o.method || 'GET', body: o.body === undefined ? null : o.body});
  if (next instanceof Error) throw next;
  return next;
};
const flush = () => new Promise((r) => setTimeout(r, 0));
const out = {};

(async () => {
  out.none = BM.leads.html({lead_events: []});
  out.lines = BM.leads.html({lead_events: [{kind: 'new_listing', actor: '<i>seth</i>', at: '2026-11-05T10:00:00+00:00', detail: {link: 'https://www.crexi.com/properties/1/x'}}]});

  // Add a property: an unsure match waits, then "same property" is sent with its id
  const wrap = new Stub('wrap');
  let added = 0;
  BM.leads.addPanel(wrap, () => { added++; });
  const box = wrap.child;
  const form = box.querySelector('[data-ld-form]');
  Object.assign(form.elements.address, {value: ' 40 Elm St '});
  form.elements.state.value = 'oh';
  form.elements.link.value = 'https://www.crexi.com/properties/1234567/x';
  next = {outcome: 'needs_resolution', message: 'This may be a property you already have.', matches: [{property_id: 5, certainty: 'maybe', why: 'same street <b>address</b>, but the city differs', address: '40 ELM ST', city: 'SPRINGFIELD', state: 'OH', zip: '45503'}]};
  form.listeners.submit[0]({preventDefault() {}});
  await flush();
  out.unsure = box.querySelector('[data-ld-result]').innerHTML;
  box.listeners.click[0]({target: target({'data-same-as': '5'})});
  await flush();
  next = {outcome: 'created', property_id: 77, message: 'Property added.'};
  box.listeners.click[0]({target: target({'data-as-new': ''})});
  await flush();
  out.add = {calls: calls.slice(), opened: opened.slice(), added, cleared: wrap.cleared, toasts: toasts.slice()};
  calls.length = 0;

  // Prepare offers for selected
  const bw = new Stub('bw');
  next = {letters_ready: false, note: 'No letter template is active yet.', rows: [
    {property_id: 1, address: '1 TEST ST', city: 'SPRINGFIELD', owner: 'EX <LLC>', recipient: null, rent_basis: 'Scheduled rent', annual_cents: 41088000, default_cents: 164352000, can_prepare: true, flags: [{key: 'no_recipient', text: 'No recipient chosen yet', blocks: false}]},
    {property_id: 2, address: '2 TEST ST', city: 'SPRINGFIELD', owner: null, recipient: null, rent_basis: 'Estimated', annual_cents: null, default_cents: null, can_prepare: false, flags: [{key: 'draft_exists', text: 'Version 1 is already a draft', blocks: false}]}]};
  await BM.leads.batchPanel(bw, [1, 2], () => {});
  const bbox = bw.child;
  const body = bbox.querySelector('[data-bp-body]');
  out.preview = body.innerHTML;
  const li = (id, use, price) => { const l = target({'data-bp': String(id)}); l.children['[data-bp-use]'] = {checked: use}; l.children['[data-bp-price]'] = {value: price}; return l; };
  body.lists['[data-bp]'] = [li(1, true, ' 1,700,000 '), li(2, false, '')];
  next = {id: 9, status: 'partial', letters_ready: false, text: '1 offer prepared, 1 left out.', counts: {created: 1, existing: 0, failed: 1, excluded: 0},
    rows: [{property_id: 1, address: '1 TEST ST', status: 'failed', status_label: 'Failed (can retry)', message: 'Boom <script>'}]};
  calls.length = 0;
  bbox.listeners.click[0]({target: target({'data-bp-go': ''})});
  await flush();
  out.report = body.innerHTML;
  next = Object.assign({}, next, {status: 'done', counts: {created: 2, existing: 0, failed: 0, excluded: 0}, rows: []});
  bbox.listeners.click[0]({target: target({'data-bp-retry': '9'})});
  await flush();
  bbox.listeners.click[0]({target: target({'data-bp-close': ''})});
  out.batchCalls = calls;
  out.closed = bw.cleared;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path_factory.mktemp("js") / "leads.js"
    script.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([node, str(script), str(STATIC / "core.js"), str(STATIC / "pricing.js"), str(STATIC / "leads.js")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_drawer_lines_only_show_for_a_property_added_by_hand_and_are_escaped(ran):
    assert ran["none"] == ""
    assert "Added by hand as a new property" in ran["lines"] and "link saved as the source, not fetched" in ran["lines"]
    assert "&lt;i&gt;seth" in ran["lines"] and "<i>seth" not in ran["lines"]


def test_an_unsure_match_waits_and_each_answer_sends_the_right_request(ran):
    assert "same street &lt;b&gt;address&lt;/b&gt;" in ran["unsure"] and "It is the same property" in ran["unsure"]
    assert "add it as a new property" in ran["unsure"]
    first, same, new = ran["add"]["calls"]
    base = "/api/m/apartments-test/leads"
    assert first == {"path": base, "method": "POST", "body": {"address": "40 Elm St", "city": None, "state": "oh", "zip": None,
                                                              "link": "https://www.crexi.com/properties/1234567/x"}}
    assert same["body"]["same_as"] == 5 and new["body"]["as_new"] is True
    assert ran["add"]["opened"] == [77] and ran["add"]["added"] == 1 and ran["add"]["cleared"] == 1


def test_the_batch_preview_shows_the_4x_price_and_what_is_missing(ran):
    h = ran["preview"]
    assert "$1,643,520" in h and "Scheduled rent" in h and "No recipient chosen yet" in h and "EX &lt;LLC&gt;" in h
    assert "No letter template is active yet" in h and "not the Blue Moon score" in h
    assert h.count("disabled") == 1 and h.count("data-bp-price") == 1      # a row that can't be prepared has no price box


def test_the_batch_sends_typed_prices_and_left_out_rows_then_retries_only_by_batch_id(ran):
    create, retry = ran["batchCalls"]
    assert create == {"path": "/api/m/apartments-test/offer-batches", "method": "POST", "body": {"rows": [
        {"property_id": 1, "exclude": False, "price": "1,700,000"}, {"property_id": 2, "exclude": True, "price": None}]}}
    assert retry["path"] == "/api/m/apartments-test/offer-batches/9/retry" and ran["closed"] == 1
    h = ran["report"]
    assert "1 offer prepared, 1 left out." in h and "Nothing was sent" in h and "Boom &lt;script&gt;" in h and "<script>" not in h
    assert "Retry the 1 that failed" in h and "report.csv" in h
