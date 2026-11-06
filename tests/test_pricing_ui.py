"""The drawer's 'Rent and Blue Moon price' and 'Age and history' sections (pricing.js), run under Node with a
small fake DOM: what is shown for an estimate, a saved record and unknown rent, the napkin and its editable
lines, and the exact bodies sent to the routes. The data is made up. Skipped when Node isn't installed."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "bluemoon" / "web" / "static"

HARNESS = r"""
const [coreJs, pricingJs, peopleJs, offersJs, leadsJs, propertyJs] = process.argv.slice(2);
const docListeners = {};
class Element {}
class Stub extends Element {
  constructor(sel) {
    super();
    this.sel = sel; this.innerHTML = ''; this.outerHTML = ''; this.textContent = ''; this.value = '';
    this.listeners = {}; this.hidden = false; this.disabled = false; this.isConnected = true; this.style = {};
    this.attrs = {}; this.children = {}; this.root = true;
    this.classList = {add() {}, remove() {}, toggle() {}, contains() { return false; }};
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  querySelector(sel) { return this.child(sel); }
  child(sel) { if (!this.children[sel]) this.children[sel] = new Stub(sel); return this.children[sel]; }
  querySelectorAll() { return []; }
  closest() { return null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  focus() {}
}
function target(attrs, tag, extra) {
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
  t.matches = (sel) => !!t.closest(sel);
  return Object.assign(t, extra || {});
}
global.Element = Element;
global.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o && o.detail; } };
global.window = {location: {hash: '', hostname: 'localhost'}, localStorage: {getItem: () => null, setItem: () => {}}};
global.history = {replaceState: () => {}};
global.document = {
  readyState: 'loading',
  addEventListener: (k, f) => { (docListeners[k] = docListeners[k] || []).push(f); },
  dispatchEvent: (ev) => { (docListeners[ev.type] || []).forEach((f) => f(ev)); },
  querySelector: () => null, getElementById: () => null,
  createElement: (tag) => new Stub(tag), body: {appendChild() {}},
};
require(coreJs);
const BM = window.BM;
global.BM = BM;
require(pricingJs);
require(peopleJs);
require(offersJs);
require(leadsJs);
require(propertyJs);

const calls = [];
const toasts = [];
BM.state.market = 'apartments-test';
BM.toast = (m) => toasts.push(String(m));
let next = null;
BM.api = async (path, opts) => {
  const o = opts || {};
  calls.push({path, method: o.method || 'GET', body: o.body || null});
  if (/\/properties\/7$/.test(path)) return detail;
  if (next) return next;
  throw new Error('unexpected ' + path);
};

const scen = (a) => [{multiple: '4', cents: a * 4, role: 'reference'}, {multiple: '5', cents: a * 5, role: 'comparison'}, {multiple: '6', cents: a * 6, role: 'comparison'}];
const est = {available: true, reason: null, units: 32, units_source: 'export', mix_source: 'default', user_inputs: false, warnings: [],
  monthly_cents: 4450000, annual_cents: 53400000,
  lines: [{bedrooms: 1, label: '1 BR', units: 10, share_pct: 31.3, rent_cents: 100000, rent_source: 'hud', monthly_cents: 1000000},
          {bedrooms: 2, label: '2 BR', units: 22, share_pct: 68.8, rent_cents: 165000, rent_source: 'hud', monthly_cents: 3630000}]};
function pricing(over) {
  return Object.assign({
    rent: {annual_cents: 41088000, monthly_cents: 3424000, basis: 'estimated_market', source: 'estimate', confidence: 'Estimated', needs_review: 0,
      basis_label: 'Estimated at market rents', label: 'Estimated', record_id: null, zero: false},
    scenarios: scen(41088000), reference_cents: 164352000, per_sqft: null, rentable_sqft: null,
    estimate: est, assumptions: {units: null, mix: {}, rents: {}}, hud: {rents: {1: 100000, 2: 165000}, area_name: 'Clark County', fiscal_year: 'FY2026', source: 'hud_arcgis'},
    records: [], defaults: {mix: {1: 30, 2: 50}},
    prior: {state: 'below', label: 'Below half', reason: null, sale_cents: 52700000, reference_cents: 110000000, share_pct: 47.9, halfway_cents: 55000000, gap_cents: 2300000, sale_date: '2019-05-01', bundled: false},
    asking_price: 1900000}, over || {});
}
const history = {age: {year: 1978, age: 48, approximate: true, flag: null, text: 'About 48 years old (built 1978; the year only, so the age is approximate)'},
  ownership: {since: '2019-05-01', years: 7.5, precision: 'exact', flag: null, text: 'Owned 7.5 years (exact: sold 2019-05-01)'},
  builder: {state: 'unknown', label: 'Unknown', source: null, note: null}, sale: {bundled: false, source: 'crexi'}, area_type: null};
const detail = {id: 7, address: '7 FAKE MEADOW CT', city: 'TESTVILLE', state: 'OH', property: {units: 32, sale_price: 527000, sale_date: '2019-05-01', asking_price: 1900000},
  pricing: pricing(), history, drill: {level: 1, stages: [], contacts: []}, raw: {}, signals: [], buybox: {rules: []}, flags: []};

const flush = () => new Promise((r) => setTimeout(r, 0));
const out = {};
(async () => {
  // 1. the estimate
  const html = BM.pricing.html(detail);
  out.estimate = html;
  out.longMoney = /\$\d+(\.\d+)?[MK]/.test(html);
  // 2. unknown rent, a saved record, confirmed zero
  out.unknown = BM.pricing.html(Object.assign({}, detail, {pricing: pricing({rent: {annual_cents: null, monthly_cents: null, basis: null, source: null, confidence: 'Estimated', needs_review: 2, basis_label: null, label: null, record_id: null, zero: false},
    scenarios: [], reference_cents: null, estimate: {available: false, reason: 'No rent for 3 BR. HUD has none for this area: type one in.', lines: [], warnings: [], units: 32, units_source: 'export', mix_source: 'default'},
    prior: {state: 'unavailable', reason: 'There is no Blue Moon price to compare with (the rent is unknown or zero).'}})}));
  const rec = {id: 5, source: 'owner', source_label: 'Owner conversation', basis: 'actual', basis_label: 'Actual rent collected', state: 'ok', state_label: 'Ready', annual_cents: 41088000, evidence_date: '2026-10-01', formula: 'monthly total x 12', reason: null, note: null, created_by: 'this Mac', selected: true, pinned: false};
  out.record = BM.pricing.html(Object.assign({}, detail, {pricing: pricing({rent: Object.assign({}, pricing().rent, {source: 'record', confidence: 'Reported', basis: 'actual', basis_label: 'Actual rent collected', record_id: 5}), records: [rec]})}));
  out.zero = BM.pricing.html(Object.assign({}, detail, {pricing: pricing({rent: Object.assign({}, pricing().rent, {annual_cents: 0, monthly_cents: 0, zero: true}), scenarios: scen(0), prior: {state: 'unavailable', reason: 'no price'}})}));

  // 3. the napkin opens, and every line is an input
  const root = new Stub('root');
  BM.pricing.html(detail);
  BM.pricing.mount(root, detail);
  const click = (attrs) => root.listeners.click.forEach((f) => f({type: 'click', target: target(attrs), preventDefault() {}}));
  click({'data-pr-act': 'napkin'});
  out.napkin = root.child('[data-part="pricing"]').outerHTML;
  out.history = root.child('[data-part="history"]').outerHTML;

  // the napkin lists the saved figures, and the chosen one can be pinned
  out.recordNapkin = BM.pricing.html(Object.assign({}, detail, {pricing: pricing({rent: Object.assign({}, pricing().rent, {source: 'record', confidence: 'Reported', record_id: 5}), records: [rec]})}));

  // 4. editing the napkin sends only what was typed
  const inputs = (list) => list.map(([name, value]) => ({name, value}));
  const form = target({'data-pr-form': 'napkin'}, 'form', {
    elements: {units: {value: ' 40 '}},
    querySelectorAll: (sel) => sel.includes('mix_') ? inputs([['mix_1', '12'], ['mix_2', '20'], ['mix_3', '']]) : inputs([['rent_1', ''], ['rent_2', '1,500']]),
  });
  next = {pricing: pricing({rent: Object.assign({}, pricing().rent, {confidence: 'Partly known'})}), history, property: {units: 40}, message: 'ok'};
  root.listeners.submit.forEach((f) => f({type: 'submit', target: form, preventDefault() {}}));
  await flush();
  out.afterNapkin = root.child('[data-part="pricing"]').outerHTML;
  const blank = target({'data-pr-form': 'napkin'}, 'form', {elements: {units: {value: ''}}, querySelectorAll: () => inputs([['mix_1', ''], ['rent_2', '']])});
  root.listeners.submit.forEach((f) => f({type: 'submit', target: blank, preventDefault() {}}));
  await flush();
  click({'data-pr-act': 'reset'});
  await flush();

  // 5. a rent record: a rate per sq ft with an area type and a total to check against
  const rate = target({'data-pr-form': 'record'}, 'form', {elements: {
    source: {value: 'listing'}, basis: {value: 'scheduled'}, evidence_date: {value: '2026-10-02'}, note: {value: ''}, kind: {value: 'sqft_monthly'},
    amount: {value: '1'}, quantity: {value: '35000'}, area_type: {value: 'rentable'}, check_total: {value: '34,000'}, check_period: {value: 'monthly'}, trust: {value: 'rate'}}});
  root.listeners.submit.forEach((f) => f({type: 'submit', target: rate, preventDefault() {}}));
  await flush();
  const total = target({'data-pr-form': 'record'}, 'form', {elements: {
    source: {value: 'rent_roll'}, basis: {value: 'actual'}, evidence_date: {value: ''}, note: {value: 'T12 from the broker'}, kind: {value: 't12'}, amount: {value: '410,880'},
    quantity: {value: '99'}, area_type: {value: ''}, check_total: {value: ''}, check_period: {value: 'monthly'}, trust: {value: ''}}});
  root.listeners.submit.forEach((f) => f({type: 'submit', target: total, preventDefault() {}}));
  await flush();
  click({'data-pr-act': 'pin', 'data-rid': '5', 'data-pinned': '1'});
  await flush();
  const hist = target({'data-pr-form': 'history'}, 'form', {elements: {builder_state: {value: 'supported'}, builder_source: {value: 'County deed'}, builder_note: {value: ''},
    sale_source: {value: ''}, building_area_type: {value: 'rentable'}, sale_bundled: {checked: true, value: 'on'}}});
  root.listeners.submit.forEach((f) => f({type: 'submit', target: hist, preventDefault() {}}));
  await flush();

  // 6. a failed write shows the message and keeps the page
  next = null;
  root.listeners.submit.forEach((f) => f({type: 'submit', target: blank, preventDefault() {}}));
  await flush();

  // 7. the drawer draws both sections
  const drawer = new Stub('drawer');
  await BM.renderProperty(drawer, 7);
  out.drawer = drawer.innerHTML;
  out.calls = calls.filter((c) => !/\/properties\/7$/.test(c.path));
  out.toasts = toasts;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path_factory.mktemp("js") / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([node, str(script), str(STATIC / "core.js"), str(STATIC / "pricing.js"), str(STATIC / "people.js"), str(STATIC / "offers.js"), str(STATIC / "leads.js"),
                           str(STATIC / "property.js")], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_estimate_shows_exact_money_and_the_right_labels(ran):
    h = ran["estimate"]
    assert "Blue Moon price (4x rent)" in h and "$1,643,520" in h
    assert "$410,880" in h and "$34,240" in h
    assert "$2,054,400" in h and "$2,465,280" in h and "Comparison only" in h
    assert "never a ceiling and never the next offer" in h and "not the Blue Moon score" in h
    assert "Estimated" in h and "Proposed offer: $1,643,520, exactly 4x (following the default)" in h
    assert "Rent only: no parking, laundry or fees" in h
    assert not ran["longMoney"]                                   # never "$1.6M"
    assert "Below half" in h and "47.9% of $1,100,000" in h and "$23,000 below half" in h
    assert "Asking price $1,900,000" in h and "not the last sale price" in h


def test_unknown_rent_is_unknown_not_zero(ran):
    h = ran["unknown"]
    assert "Rent unknown" in h and "No rent for 3 BR" in h and "$0" not in h
    assert "No Blue Moon price yet" in h and "Last sale check unavailable" in h
    assert "2 rent figures need review" in h


def test_a_saved_record_is_reported_and_zero_rent_keeps_its_note(ran):
    assert "Reported" in ran["record"] and "Owner conversation" in ran["record"] and "Actual rent collected" in ran["record"]
    n = ran["recordNapkin"]
    assert "Used for the price" in n and "Use this one" in n and "Back to the automatic choice" not in n
    assert "Confirmed zero rent" in ran["zero"] and "$0" in ran["zero"]


def test_the_napkin_shows_known_versus_assumed_and_every_line_is_editable(ran):
    n = ran["napkin"]
    assert "Back of the napkin" in n and 'role="region"' in n
    assert "Assumed" in n and "from the export" in n and "Clark County, FY2026" in n
    for name in ("units", "mix_1", "mix_2", "rent_1", "rent_2"):
        assert f'name="{name}"' in n
    assert "Re-run the price" in n and "Add rent evidence" in n and "Saved rent figures" in n
    assert 'placeholder="1650"' in n                               # the HUD rent shows grey, not as a typed value
    assert "net rentable area" in n.lower()


def test_napkin_edits_send_only_what_was_typed_and_redraw(ran):
    c = ran["calls"]
    assert c[0]["method"] == "PUT" and c[0]["path"].endswith("/properties/7/rent/assumptions")
    assert c[0]["body"] == {"units": 40, "mix": {"1": 12, "2": 20}, "rents": {"2": "1,500"}}
    assert "Partly known" in ran["afterNapkin"]
    assert c[1]["body"] == {"units": None, "mix": None, "rents": None}      # nothing typed: back to the defaults
    assert c[2]["body"] == {"units": None, "mix": None, "rents": None}      # the reset button


def test_record_forms_map_to_the_route_fields(ran):
    c = ran["calls"]
    assert c[3]["path"].endswith("/rent/records") and c[3]["method"] == "POST"
    assert c[3]["body"] == {"source": "listing", "basis": "scheduled", "evidence_date": "2026-10-02", "note": None,
                            "rate_period": "monthly", "quantity_kind": "rentable_sqft", "rate": "1", "quantity": "35000",
                            "area_type": "rentable", "total": "34,000", "total_period": "monthly", "authoritative": "rate"}
    assert c[4]["body"] == {"source": "rent_roll", "basis": "actual", "evidence_date": None, "note": "T12 from the broker",
                            "total_period": "t12", "total": "410,880"}           # a total ignores the count box
    assert c[5]["path"].endswith("/rent/records/5/pin") and c[5]["body"] == {"pinned": True}
    assert c[6]["method"] == "PUT" and c[6]["path"].endswith("/properties/7/history")
    assert c[6]["body"] == {"builder_state": "supported", "builder_source": "County deed", "builder_note": None,
                            "sale_source": None, "building_area_type": "rentable", "sale_bundled": True}


def test_failed_write_toasts_and_history_never_invents_a_single_owner_flag(ran):
    assert any("unexpected" in t for t in ran["toasts"])
    h = ran["history"]
    assert "About 48 years old" in h and "approximate" in h and "Owned 7.5 years (exact" in h
    assert "Matching years do not prove" in h and "Unknown" in h
    assert "single owner" not in h.lower() and "local owner" not in h.lower()


def test_the_drawer_draws_the_new_sections(ran):
    d = ran["drawer"]
    assert 'data-part="pricing"' in d and 'data-part="history"' in d and "Rent and Blue Moon price" in d
