"""The drawer's "Who is behind this property" block (people.js), run under Node with a small fake DOM: the
seller and people with roles, the "No person confirmed yet" state, the recipient kept apart from the seller,
conflicting owner names, and the exact requests sent. The data is made up. Skipped when Node isn't installed."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "bluemoon" / "web" / "static"

HARNESS = r"""
const [coreJs, peopleJs] = process.argv.slice(2);
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
require(peopleJs);

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

const choices = [['owner_of_record', 'Owner of record'], ['manager', 'Manager'], ['broker', 'Broker'], ['other', 'Unconfirmed association']];
const person = (over) => Object.assign({id: 1, name: 'Bea Broker', role: 'broker', role_label: 'Broker', can_change_role: true, notes: null,
  source: 'you', source_label: 'you', edited_by: 'seth@example.com', edited_at: '2026-11-01T10:00:00+00:00', is_recipient: false,
  contacts: [{id: 11, channel: 'email', value: 'bea@example.com', status: 'untested', can_receive: true},
             {id: 12, channel: 'phone', value: '937-555-0100', status: 'dnc', can_receive: false}]}, over || {});
const people = (over) => Object.assign({
  property: {id: 7, address: '7 FAKE MEADOW CT', city: 'TESTVILLE'}, seller: {name: 'EXAMPLE HOLDINGS LLC', is_entity: true},
  confirmed: [], people: [person()], loose_contacts: [{id: 13, channel: 'mail', value: 'PO Box 1', status: 'untested', can_receive: true}],
  recipient: null, conflicts: {owner_names: [], county_differs: false}, role_choices: choices}, over || {});
const detail = {id: 7, address: '7 FAKE MEADOW CT', city: 'TESTVILLE', people: people()};

const flush = () => new Promise((r) => setTimeout(r, 0));
const out = {};
(async () => {
  out.plain = BM.people.html(detail);
  out.none = BM.people.html(Object.assign({}, detail, {people: people({people: [], loose_contacts: []})}));
  out.confirmed = BM.people.html(Object.assign({}, detail, {people: people({confirmed: ['Chris Confirmed']})}));
  out.conflict = BM.people.html(Object.assign({}, detail, {people: people({conflicts: {owner_names: [{header: 'Owner 2', value: 'OTHER NAME LLC', source: 'Crexi', day: 'Oct 29'}], county_differs: true}})}));
  out.locked = BM.people.html(Object.assign({}, detail, {people: people({people: [person({can_change_role: false, role_label: 'Owner (confirmed)'})]})}));
  out.recipient = BM.people.html(Object.assign({}, detail, {people: people({
    people: [person({is_recipient: true})],
    recipient: {person_id: 1, name: 'Bea Broker', role_label: 'Broker', contact_id: 11, contact_text: 'email: bea@example.com', note: null, chosen_by: 'seth@example.com', chosen_at: '2026-11-02T10:00:00+00:00'}})}));
  out.empty = BM.people.html({id: 7});

  const root = new Stub('root');
  const drills = [];
  BM.people.html(detail);
  BM.people.mount(root, detail, (res) => drills.push(res));
  const fire = (type, t) => root.listeners[type].forEach((f) => f({type, target: t, preventDefault() {}}));

  next = {people: people({recipient: {person_id: 1, name: 'Bea Broker', role_label: 'Broker', contact_id: null, contact_text: null, note: null, chosen_by: 'x', chosen_at: null}}), message: 'chosen'};
  fire('click', target({'data-pe-act': 'recipient', 'data-person': '1'}));
  await flush();
  fire('click', target({'data-pe-act': 'recipient', 'data-person': '1', 'data-contact': '11'}));
  await flush();
  next = {people: people(), message: 'cleared'};
  fire('click', target({'data-pe-act': 'clear-recipient'}));
  await flush();
  next = {people: people(), drill: {level: 1}, message: 'role'};
  fire('change', target({'data-pe-act': 'role', 'data-person': '1'}, 'select', {value: 'manager'}));
  await flush();
  next = {people: people(), drill: {level: 1}, message: 'added'};
  const form = target({'data-pe-form': 'person'}, 'form', {elements: {name: {value: ' Pat Pal '}, role: {value: 'other'}, notes: {value: ''}}});
  fire('submit', form);
  await flush();
  next = new Error('That was not found.');
  fire('click', target({'data-pe-act': 'clear-recipient'}));
  await flush();
  out.calls = calls;
  out.drills = drills.length;
  out.toasts = toasts;
  out.section = root.querySelector('[data-part="people"]').outerHTML;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path_factory.mktemp("js") / "people.js"
    script.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([node, str(script), str(STATIC / "core.js"), str(STATIC / "people.js")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_seller_and_each_person_show_role_and_source(ran):
    h = ran["plain"]
    assert "Property 7 · 7 Fake Meadow Ct, Testville" in h
    assert "EXAMPLE HOLDINGS LLC" in h and "Company" in h and "Seller (owner of record)" in h
    assert "Bea Broker" in h and ">Broker<" in h and "From you" in h and "by seth@example.com" in h
    assert 'aria-label="Role for Bea Broker"' in h and '<option value="broker" selected>' in h
    assert "bea@example.com" in h and "Do not contact" in h and "Contacts not tied to a person" in h and "PO Box 1" in h


def test_a_company_with_nobody_confirmed_says_so(ran):
    assert "No person confirmed yet" in ran["plain"]
    assert "No people recorded yet" in ran["none"]
    c = ran["confirmed"]
    assert "Chris Confirmed" in c and "Confirmed</span>" in c and "No person confirmed yet" not in c


def test_do_not_contact_values_cannot_be_chosen(ran):
    h = ran["plain"]
    assert h.count('data-contact="11"') == 1 and 'data-contact="12"' not in h


def test_conflicting_owner_names_stay_visible(ran):
    c = ran["conflict"]
    assert "Sources disagree about the owner" in c and "Owner 2: OTHER NAME LLC" in c and "Crexi, Oct 29" in c
    assert "county tax record shows a different owner" in c
    assert "Sources disagree" not in ran["plain"]


def test_the_recipient_is_shown_apart_from_the_seller(ran):
    assert "No offer recipient chosen yet" in ran["plain"]
    r = ran["recipient"]
    assert "Offers go to Bea Broker" in r and "Using email: bea@example.com" in r and "The seller stays EXAMPLE HOLDINGS LLC" in r
    assert "Offer recipient</span>" in r and "Clear recipient" in r


def test_a_locked_role_has_no_dropdown_and_missing_data_draws_nothing(ran):
    assert "Role set by the owner-contact steps" in ran["locked"] and 'data-pe-act="role"' not in ran["locked"]
    assert ran["empty"] == ""


def test_actions_send_the_right_requests(ran):
    c = ran["calls"]
    assert c[0] == {"path": "/api/m/apartments-test/properties/7/recipient", "method": "PUT", "body": {"person_id": 1, "contact_id": None}}
    assert c[1]["body"] == {"person_id": 1, "contact_id": 11}
    assert c[2] == {"path": "/api/m/apartments-test/properties/7/recipient", "method": "DELETE", "body": None}
    assert c[3] == {"path": "/api/m/apartments-test/properties/7/people/1/role", "method": "PUT", "body": {"role": "manager"}}
    assert c[4]["path"].endswith("/properties/7/people") and c[4]["method"] == "POST"
    assert c[4]["body"] == {"name": "Pat Pal", "role": "other", "notes": None, "source": "you"}
    assert ran["drills"] == 2                              # the role change and the new person carry an owner-contact state
    assert "That was not found." in ran["toasts"] and "chosen" in ran["toasts"]
