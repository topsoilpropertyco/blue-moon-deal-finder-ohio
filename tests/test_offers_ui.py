"""The drawer's Offers section and the Offers screen (offers.js), run under Node with a small fake DOM: a draft is
never shown as sent, the price mode and implied multiple, the stale-draft warning, counter figures kept next to
(not in place of) the offer, and the exact requests sent. The data is made up. Skipped when Node isn't installed."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "bluemoon" / "web" / "static"

HARNESS = r"""
const [coreJs, pricingJs, offersJs] = process.argv.slice(2);
const docListeners = {};
class Element {}
class Stub extends Element {
  constructor(sel) {
    super();
    this.sel = sel; this.innerHTML = ''; this.outerHTML = ''; this.listeners = {}; this.attrs = {}; this.children = {};
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  querySelector(sel) { if (!this.children[sel]) this.children[sel] = new Stub(sel); return this.children[sel]; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
}
function target(attrs, tag, extra) {
  const t = new Stub(tag || 'button');
  t.tagName = (tag || 'button').toUpperCase();
  t.attrs = Object.assign({}, attrs);
  t.closest = (sel) => sel.split(',').map((s) => s.trim()).some((s) => {
    const m = /^(\w+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/.exec(s);
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
  dispatchEvent: () => {}, querySelector: () => null, getElementById: () => null,
  createElement: (tag) => new Stub(tag), body: {appendChild() {}},
};
require(coreJs);
const BM = window.BM;
global.BM = BM;
const views = {};
BM.registerView = (key, def) => { views[key] = def; };
require(pricingJs);
require(offersJs);

const calls = [];
const toasts = [];
BM.state.market = 'apartments-test';
BM.toast = (m) => toasts.push(String(m));
let next = null;
BM.api = async (path, opts) => {
  const o = opts || {};
  calls.push({path, method: o.method || 'GET', body: o.body === undefined ? null : o.body});
  if (next instanceof Error) throw next;
  return next;
};

const classes = [['serious_interest', 'Serious interest'], ['counter', 'Counteroffer'], ['rejection', 'Rejected'], ['needs_review', 'Needs review']];
const scenarios = [{multiple: '4', cents: 164352000, role: 'reference'}, {multiple: '5', cents: 205440000, role: 'comparison'}, {multiple: '6', cents: 246528000, role: 'comparison'}];
const offer = (over) => Object.assign({
  id: 5, property_id: 7, version: 1, status: 'internal_draft', status_label: 'Internal draft', origin: 'prepared', incomplete_snapshot: false,
  seller_name: 'EXAMPLE HOLDINGS LLC', recipient: {person_id: 1, name: 'Bea Broker', contact: 'email: bea@example.com'},
  price_cents: 164352000, price_mode: 'default', implied_multiple: '4.00', rent: {annual_cents: 41088000, basis: 'scheduled', source: 'record', scenarios},
  stale: false, sent: null, counter: null, responses: [], actions: [], next_action: null,
  readiness: {template: {ready: false, text: 'LOI template needed'}, recipient: {ready: true, text: 'Recipient chosen'}},
  created_by: 'seth', created_at: '2026-11-05T10:00:00+00:00', events: [{kind: 'prepared', actor: 'seth', at: '2026-11-05T10:00:00+00:00', detail: {}}]}, over || {});
const block = (over) => Object.assign({offers: [offer()], can_prepare: false, next_version: 2,
  current: {annual_cents: 41088000, basis: 'scheduled', source: 'record', default_cents: 164352000}, recipient: 'Bea Broker',
  sent_how: ['email', 'in person', 'mail'], class_choices: classes}, over || {});
const detail = (b) => ({id: 7, offers: b || block()});
const sentOffer = (over) => offer(Object.assign({status: 'sent', status_label: 'Sent', price_mode: 'override', price_cents: 170000000, implied_multiple: '4.14',
  sent: {how: 'email', on: '2026-11-03', by: 'seth@example.com', via: 'manual', via_label: 'Marked by hand'},
  counter: {low_cents: 190000000, high_cents: 200000000, low_multiple: '4.62', high_multiple: '4.87', response_id: 9, received_on: '2026-11-04'},
  responses: [{id: 9, classification: 'counter', classification_label: 'Counteroffer', original_text: 'We would do 1.9 to 2.0 <b>million</b>', counter_low_cents: 190000000,
    counter_high_cents: 200000000, received_on: '2026-11-04', recorded_by: 'seth', corrected_by: null}],
  actions: [{id: 3, text: 'Follow up: did they get the LOI?', owner: 'seth', due_on: '2020-11-10', done: 0, auto: 1}],
  next_action: {id: 3, text: 'Follow up: did they get the LOI?', owner: 'seth', due_on: '2020-11-10', done: 0}}, over || {}));

const flush = () => new Promise((r) => setTimeout(r, 0));
const out = {};
(async () => {
  out.draft = BM.offers.html(detail());
  out.empty = BM.offers.html({id: 7, offers: block({offers: [], can_prepare: true, next_version: 1})});
  out.norent = BM.offers.html({id: 7, offers: block({offers: [], can_prepare: true, next_version: 1, current: {annual_cents: null, basis: null, source: null, default_cents: null}})});
  out.stale = BM.offers.html(detail(block({offers: [offer({stale: true})]})));
  out.sent = BM.offers.html(detail(block({offers: [sentOffer()], can_prepare: true, next_version: 2})));
  out.history = BM.offers.html(detail(block({offers: [offer({status: 'sent', status_label: 'Sent', incomplete_snapshot: true, rent: null, implied_multiple: null, sent: {how: 'mail', on: '2026-06-01', by: 'seth', via: 'manual', via_label: 'Marked by hand'}})], can_prepare: true})));
  out.provider = BM.offers.html(detail(block({offers: [sentOffer({sent: {how: 'email', on: '2026-11-03', by: 'mail-provider', via: 'provider', via_label: 'Reported by the mail provider'}})]})));
  out.none = BM.offers.html({id: 7});

  const root = new Stub('root');
  BM.offers.html(detail());
  BM.offers.mount(root, detail());
  const fire = (type, t) => root.listeners[type].forEach((f) => f({type, target: t, preventDefault() {}}));
  const form = (kind, attrs, values) => target(Object.assign({'data-of-form': kind}, attrs), 'form', {elements: values, reportValidity: () => true});
  const res = (b, message) => ({offers: b, message});

  next = res(detail().offers, 'prepared');
  fire('submit', form('prepare', {}, {price: {value: ' 1,700,000 '}}));
  await flush();
  fire('submit', form('prepare', {}, {price: {value: ''}}));
  await flush();
  fire('submit', form('price', {'data-offer': '5'}, {price: {value: '1650000.00'}}));
  await flush();
  fire('click', target({'data-of-act': 'default-price', 'data-offer': '5'}));
  await flush();
  fire('click', target({'data-of-act': 'refresh', 'data-offer': '5'}));
  await flush();
  fire('submit', form('sent', {'data-offer': '5'}, {how: {value: 'in person'}, when: {value: '2026-11-03'}}));
  await flush();
  fire('submit', form('response', {'data-offer': '5'}, {text: {value: '  keep my spaces  '}, classification: {value: ''}, counter_low: {value: ''}, counter_high: {value: ''}, received_on: {value: '2026-11-04'}}));
  await flush();
  fire('submit', form('fix-response', {'data-offer': '5', 'data-response': '9'}, {classification: {value: 'counter'}, counter_low: {value: '1900000'}, counter_high: {value: ''}}));
  await flush();
  fire('submit', form('action', {'data-offer': '5'}, {text: {value: 'Call Bea'}, due_on: {value: ''}}));
  await flush();
  fire('change', target({'data-of-act': 'done', 'data-offer': '5', 'data-action': '3'}, 'input', {checked: true}));
  await flush();
  fire('change', target({'data-of-act': 'due', 'data-offer': '5', 'data-action': '3'}, 'input', {value: '2026-11-20'}));
  await flush();
  fire('submit', form('historical', {}, {price: {value: '1250000'}, how: {value: 'mail'}, sent_on: {value: '2026-06-01'}, recipient_name: {value: ''}, note: {value: 'old'}}));
  await flush();
  next = new Error('Version 1 was already sent.');
  fire('click', target({'data-of-act': 'refresh', 'data-offer': '5'}));
  await flush();
  out.calls = calls;
  out.toasts = toasts;

  // the Offers screen
  const screen = new Stub('screen');
  screen.isConnected = true;
  const listing = {total: 3, counts: {internal_draft: 1, sent: 2},
    status_labels: [['internal_draft', 'Internal draft'], ['sent', 'Sent']],
    items: [Object.assign(offer({id: 1, property_id: 11, stale: true}), {address: '1 FAKE ST', city: 'TESTVILLE'}),
            Object.assign(sentOffer({id: 2, property_id: 12}), {address: '2 FAKE ST', city: 'TESTVILLE'}),
            Object.assign(sentOffer({id: 3, property_id: 13, next_action: {id: 4, text: 'Call', owner: 'seth', due_on: '2000-01-01', done: 0}, counter: null}), {address: '3 FAKE ST', city: 'TESTVILLE'})]};
  next = listing;
  await views.offers.render(screen);
  out.screen = screen.innerHTML;
  screen.listeners.change[0]({type: 'change', target: target({'data-of-filter': ''}, 'select', {value: 'sent'})});
  out.filtered = screen.innerHTML;
  next = Object.assign({}, listing, {total: 0, items: [], counts: {}});
  await views.offers.render(screen);
  out.screenEmpty = screen.innerHTML;
  out.view = {label: views.offers.label, group: views.offers.group};
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path_factory.mktemp("js") / "offers.js"
    script.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([node, str(script), str(STATIC / "core.js"), str(STATIC / "pricing.js"), str(STATIC / "offers.js")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_draft_says_it_is_not_sent_and_shows_price_mode_and_multiple(ran):
    h = ran["draft"]
    assert "Internal draft" in h and "$1,643,520" in h and "Following the 4x default" in h and "4.00x the yearly rent" in h
    assert "4x $1,643,520" in h and "5x $2,054,400" in h and "6x $2,465,280" in h and "comparisons only" in h
    assert "LOI template needed" in h and "Recipient chosen" in h and "Bea Broker" in h and "EXAMPLE HOLDINGS LLC" in h
    assert "Mark as sent" in h and "Nothing is sent from here" in h and "Sent by" not in h
    assert "Blue Moon price (4x rent)" in h and "not the Blue Moon score" in h
    assert "Prepare" not in h.split("Version 1")[0].split("Blue Moon score")[1]       # one open draft: no second prepare form


def test_preparing_starts_from_the_4x_price_or_asks_for_one(ran):
    assert "Prepare offer" in ran["empty"] and "$1,643,520" in ran["empty"] and "leave blank to use the 4x price" in ran["empty"]
    assert "unknown or $0" in ran["norent"] and " required" in ran["norent"]


def test_a_stale_draft_warns_and_offers_a_refresh(ran):
    assert "The rent figures changed after this draft was made" in ran["stale"] and 'data-of-act="refresh"' in ran["stale"]
    assert "rent figures changed" not in ran["draft"]


def test_a_sent_offer_shows_how_and_who_and_keeps_the_counter_beside_it(ran):
    h = ran["sent"]
    assert "Sent email on" in h and "seth@example.com" in h and "Marked by hand" in h and "Manual override" in h and "4.14x" in h
    assert "$1,700,000" in h and "$1,900,000 to $2,000,000 (4.62x to 4.87x)" in h and "your offer did not change" in h
    assert "Save price" not in h and "Mark as sent</button>" not in h               # a sent version cannot be edited
    assert "Prepare revised offer (version 2)" in h
    assert "Follow up: did they get the LOI?" in h and "Overdue" in h
    assert "We would do 1.9 to 2.0 &lt;b&gt;million&lt;/b&gt;" in h and "<b>million" not in h      # their words are escaped
    assert "Reported by the mail provider" in ran["provider"] and "Marked by hand" not in ran["provider"]


def test_an_offer_entered_after_the_fact_is_flagged(ran):
    h = ran["history"]
    assert "Entered after the fact" in h and "Rent basis at the time" not in h and "x the yearly rent" not in h


def test_missing_data_draws_nothing(ran):
    assert ran["none"] == ""


def test_actions_send_the_right_requests(ran):
    c = ran["calls"]
    base = "/api/m/apartments-test"
    assert c[0] == {"path": base + "/properties/7/offers", "method": "POST", "body": {"price": "1,700,000"}}
    assert c[1]["body"] == {"price": None}
    assert c[2] == {"path": base + "/offers/5/price", "method": "PUT", "body": {"price": "1650000.00"}}
    assert c[3] == {"path": base + "/offers/5/price", "method": "PUT", "body": {"price": None}}
    assert c[4] == {"path": base + "/offers/5/refresh", "method": "POST", "body": None}
    assert c[5] == {"path": base + "/offers/5/sent", "method": "POST", "body": {"how": "in person", "when": "2026-11-03"}}
    assert c[6]["path"].endswith("/offers/5/responses") and c[6]["body"] == {
        "text": "  keep my spaces  ", "classification": None, "counter_low": None, "counter_high": None, "received_on": "2026-11-04"}
    assert c[7] == {"path": base + "/offers/5/responses/9", "method": "PUT",
                    "body": {"classification": "counter", "counter_low": "1900000", "counter_high": None}}
    assert c[8]["path"].endswith("/offers/5/actions") and c[8]["body"] == {"text": "Call Bea", "due_on": None}
    assert c[9] == {"path": base + "/offers/5/actions/3", "method": "PUT", "body": {"done": True}}
    assert c[10]["body"] == {"due_on": "2026-11-20"}
    assert c[11]["path"].endswith("/properties/7/offers/historical") and c[11]["body"] == {
        "price": "1250000", "how": "mail", "sent_on": "2026-06-01", "recipient_name": None, "note": "old"}
    assert "Version 1 was already sent." in ran["toasts"] and "prepared" in ran["toasts"]


def test_the_offers_screen_lists_each_offer_with_stage_price_counter_and_next_action(ran):
    h = ran["screen"]
    assert "Offers" in h and "A draft is not a sent offer" in h and "All offers (3)" in h and "Internal draft (1)" in h
    assert 'data-open-property="11"' in h and "1 Fake St, Testville" in h and "Rent changed" in h
    assert "Their counter" in h and "$1,900,000 to $2,000,000" in h and "Overdue" in h
    assert h.count('class="of-item"') == 3
    assert ran["filtered"].count('class="of-item"') == 2 and "1 Fake St" not in ran["filtered"]
    assert "No offers yet" in ran["screenEmpty"]
    assert ran["view"] == {"label": "Offers", "group": "work"}
