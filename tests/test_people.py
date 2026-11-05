"""The people block (seller, people with roles and sources, "no person confirmed yet"), the offer recipient kept
apart from the seller, role changes and their routes. Made-up properties only."""
from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from bluemoon import people
from bluemoon.db import dumps, market_by_slug, now_iso, one, rows, scalar
from bluemoon.drill import engine

MARKET = "apartments-springfield-oh"
_ids = itertools.count(1)


def add_prop(conn, **kw) -> int:
    n = next(_ids)
    data = {"market_id": market_by_slug(conn, MARKET)["id"], "property_key": f"people:{n}", "address": f"{n} PEOPLE LN",
            "city": "SPRINGFIELD", "state": "OH", "owner_name": "EXAMPLE HOLDINGS LLC", "raw_json": "{}",
            "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso()}
    data.update(kw)
    pid = int(conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                           tuple(data.values())).lastrowid)
    conn.commit()
    return pid


def block(conn, pid):
    return people.block(conn, one(conn, "SELECT * FROM properties WHERE id=?", (pid,)))


def person_id(conn, pid, name):
    return scalar(conn, "SELECT id FROM people WHERE property_id=? AND name=?", (pid, name))


# --- the block ------------------------------------------------------------------------------------------

def test_a_company_with_nobody_confirmed_says_so(conn):
    pid = add_prop(conn)
    b = block(conn, pid)
    assert b["seller"] == {"name": "EXAMPLE HOLDINGS LLC", "is_entity": True}
    assert b["confirmed"] == [] and b["people"] == [] and b["recipient"] is None
    assert b["property"] == {"id": pid, "address": scalar(conn, "SELECT address FROM properties WHERE id=?", (pid,)),
                             "city": "SPRINGFIELD"}


def test_people_show_role_and_source_and_owners_come_first(conn):
    pid = add_prop(conn)
    engine.add_person(conn, pid, "Bea Broker", role="broker", source="you", actor="seth@example.com")
    engine.add_person(conn, pid, "Mo Member", role="member", source="entity_unmask")
    engine.add_person(conn, pid, "Rae Registered", role="statutory_agent", source="entity_unmask")
    engine.add_person(conn, pid, "Una Unknown")
    b = block(conn, pid)
    assert [p["name"] for p in b["people"]] == ["Mo Member", "Bea Broker", "Rae Registered", "Una Unknown"]
    by = {p["name"]: p for p in b["people"]}
    assert by["Bea Broker"]["role_label"] == "Broker" and by["Bea Broker"]["edited_by"] == "seth@example.com"
    assert by["Bea Broker"]["source_label"] == "you"
    assert by["Rae Registered"]["role_label"].startswith("Registered contact")
    assert by["Una Unknown"]["role_label"] == "Unconfirmed association"
    assert by["Mo Member"]["source_label"] != "entity unmask" and by["Mo Member"]["edited_by"] is None
    assert b["confirmed"] == []                        # nobody confirmed them: still no person confirmed


def test_only_a_confirmed_owner_counts_as_confirmed(conn):
    pid = add_prop(conn)
    engine.add_person(conn, pid, "Pat Possible", role="possible_owner")
    assert block(conn, pid)["confirmed"] == []
    conn.execute("INSERT INTO people(property_id, name, role, source, created_at) VALUES(?,?,?,?,?)",
                 (pid, "Chris Confirmed", "confirmed_owner", "conversation", now_iso()))
    conn.commit()
    assert block(conn, pid)["confirmed"] == ["Chris Confirmed"]


def test_rejected_names_are_hidden(conn):
    pid = add_prop(conn)
    engine.add_person(conn, pid, "Wrong Person", role="member")
    conn.execute("UPDATE people SET role='rejected' WHERE property_id=?", (pid,))
    conn.commit()
    assert block(conn, pid)["people"] == []


def test_conflicting_owner_names_stay_inspectable(conn):
    pid = add_prop(conn, flags_json=dumps(["county_owner_differs"]))
    conn.execute("INSERT INTO property_alternates(property_id, field, value, original, source, seen_at, status) "
                 "VALUES(?,?,?,?,?,?,?)", (pid, "owner_name", dumps("OTHER NAME LLC"), dumps("EXAMPLE HOLDINGS LLC"),
                                           "crexi", now_iso(), "kept"))
    conn.commit()
    c = block(conn, pid)["conflicts"]
    assert c["county_differs"] is True
    assert [x["value"] for x in c["owner_names"]] == ["OTHER NAME LLC"] and c["owner_names"][0]["header"] == "Owner 2"
    assert block(conn, add_prop(conn))["conflicts"] == {"owner_names": [], "county_differs": False}


# --- roles ----------------------------------------------------------------------------------------------

def test_a_broker_is_not_an_owner_and_a_role_change_is_attributed(conn):
    pid = add_prop(conn, owner_name=None)
    engine.add_person(conn, pid, "Bea Broker", role="broker")
    assert engine.property_drill(conn, pid)["level"] == 1          # a broker never makes it "possible owner"
    engine.add_person(conn, pid, "Pat Pal")
    res = engine.set_person_role(conn, pid, person_id(conn, pid, "Pat Pal"), "manager", "seth@example.com")
    assert "now recorded as manager" in res["message"]
    row = one(conn, "SELECT role, edited_by, edited_at FROM people WHERE name='Pat Pal'")
    assert row["role"] == "manager" and row["edited_by"] == "seth@example.com" and row["edited_at"]


def test_role_changes_are_checked(conn):
    pid = add_prop(conn)
    other = add_prop(conn)
    engine.add_person(conn, pid, "Pat Pal")
    pat = person_id(conn, pid, "Pat Pal")
    with pytest.raises(ValueError, match="Pick a role"):
        engine.set_person_role(conn, pid, pat, "confirmed_owner", "x")       # only a conversation confirms
    with pytest.raises(ValueError, match="Pick a role"):
        engine.set_person_role(conn, pid, pat, None, "x")
    with pytest.raises(KeyError):
        engine.set_person_role(conn, other, pat, "broker", "x")             # not on that property
    conn.execute("INSERT INTO people(property_id, name, role, source, created_at) VALUES(?,?,?,?,?)",
                 (pid, "Chris Confirmed", "confirmed_owner", "conversation", now_iso()))
    conn.commit()
    with pytest.raises(ValueError, match="can't be changed here"):
        engine.set_person_role(conn, pid, person_id(conn, pid, "Chris Confirmed"), "broker", "x")
    assert scalar(conn, "SELECT role FROM people WHERE id=?", (pat,)) == "other"


# --- the recipient --------------------------------------------------------------------------------------

def test_the_recipient_is_stored_apart_from_the_seller(conn):
    pid = add_prop(conn)
    engine.add_contact(conn, pid, "email", "bea@example.com", person_name="Bea Broker")
    engine.set_person_role(conn, pid, person_id(conn, pid, "Bea Broker"), "broker", "seth")
    bea = person_id(conn, pid, "Bea Broker")
    contact = scalar(conn, "SELECT id FROM contact_points WHERE property_id=?", (pid,))
    people.choose_recipient(conn, pid, bea, contact, "Brad's broker", "seth")
    b = block(conn, pid)
    assert b["recipient"]["name"] == "Bea Broker" and b["recipient"]["role_label"] == "Broker"
    assert b["recipient"]["contact_text"] == "email: bea@example.com" and b["recipient"]["chosen_by"] == "seth"
    assert [p["is_recipient"] for p in b["people"]] == [True]
    assert b["seller"]["name"] == "EXAMPLE HOLDINGS LLC"                      # the seller is untouched
    assert scalar(conn, "SELECT owner_name FROM properties WHERE id=?", (pid,)) == "EXAMPLE HOLDINGS LLC"
    people.clear_recipient(conn, pid)
    assert block(conn, pid)["recipient"] is None


def test_choosing_again_replaces_and_bad_choices_are_refused(conn):
    pid = add_prop(conn)
    other = add_prop(conn)
    engine.add_contact(conn, pid, "email", "bea@example.com", person_name="Bea Broker")
    engine.add_contact(conn, pid, "phone", "937-555-0100", person_name="Mo Member")
    engine.add_contact(conn, pid, "email", "bad@example.com", person_name="Bea Broker")
    engine.add_contact(conn, other, "email", "far@example.com", person_name="Far Away")
    bea, mo = person_id(conn, pid, "Bea Broker"), person_id(conn, pid, "Mo Member")
    emails = [r["id"] for r in rows(conn, "SELECT id FROM contact_points WHERE property_id=? ORDER BY id", (pid,))]
    foreign = scalar(conn, "SELECT id FROM contact_points WHERE property_id=?", (other,))
    people.choose_recipient(conn, pid, bea, None, None, "seth")
    people.choose_recipient(conn, pid, mo, None, None, "seth")
    assert scalar(conn, "SELECT COUNT(*) FROM property_recipients WHERE property_id=?", (pid,)) == 1
    assert block(conn, pid)["recipient"]["name"] == "Mo Member"
    with pytest.raises(ValueError, match="not recorded for Bea Broker"):
        people.choose_recipient(conn, pid, bea, emails[1], None, "seth")        # Mo's phone is not Bea's
    with pytest.raises(KeyError):
        people.choose_recipient(conn, pid, bea, foreign, None, "seth")          # another property's contact
    with pytest.raises(KeyError):
        people.choose_recipient(conn, pid, person_id(conn, other, "Far Away"), None, None, "seth")
    conn.execute("UPDATE contact_points SET status='dnc' WHERE id=?", (emails[2],))
    conn.commit()
    with pytest.raises(ValueError, match="do-not-contact"):
        people.choose_recipient(conn, pid, bea, emails[2], None, "seth")
    with pytest.raises(ValueError, match="longer than"):
        people.choose_recipient(conn, pid, bea, None, "x" * 501, "seth")
    assert block(conn, pid)["recipient"]["name"] == "Mo Member"                 # refusals changed nothing


def test_a_recipient_marked_wrong_drops_off(conn):
    pid = add_prop(conn)
    engine.add_person(conn, pid, "Bea Broker", role="broker")
    people.choose_recipient(conn, pid, person_id(conn, pid, "Bea Broker"), None, None, "seth")
    conn.execute("UPDATE people SET role='rejected' WHERE property_id=?", (pid,))
    conn.commit()
    assert block(conn, pid)["recipient"] is None


# --- routes ---------------------------------------------------------------------------------------------

@pytest.fixture
def client(conn):
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def url(pid, tail=""):
    return f"/api/m/{MARKET}/properties/{pid}{tail}"


def test_routes_add_change_role_and_choose_a_recipient(client, conn):
    pid = add_prop(conn)
    detail = client.get(url(pid)).json()
    assert detail["people"]["seller"]["name"] == "EXAMPLE HOLDINGS LLC" and detail["people"]["confirmed"] == []

    r = client.post(url(pid, "/people"), json={"name": "Bea Broker", "role": "broker", "notes": "Listing agent"})
    assert r.status_code == 200 and r.json()["created"] is True
    bea = r.json()["people"]["people"][0]
    assert bea["role_label"] == "Broker" and bea["edited_by"] and bea["notes"] == "Listing agent"
    assert client.post(url(pid, "/people"), json={"name": "Zed", "role": "confirmed_owner"}).status_code == 400

    r = client.put(url(pid, f"/people/{bea['id']}/role"), json={"role": "manager"})
    assert r.status_code == 200 and r.json()["people"]["people"][0]["role"] == "manager" and r.json()["drill"]
    assert client.put(url(pid, f"/people/{bea['id']}/role"), json={"role": "confirmed_owner"}).status_code == 400
    assert client.put(url(pid, "/people/99999/role"), json={"role": "broker"}).status_code == 404

    r = client.put(url(pid, "/recipient"), json={"person_id": bea["id"], "note": "send here"})
    assert r.status_code == 200 and r.json()["people"]["recipient"]["name"] == "Bea Broker"
    assert r.json()["people"]["seller"]["name"] == "EXAMPLE HOLDINGS LLC"
    assert client.put(url(pid, "/recipient"), json={"person_id": 99999}).status_code == 404
    assert client.put(url(pid, "/recipient"), json={"person_id": bea["id"], "contact_id": 99999}).status_code == 404
    assert client.get(url(pid)).json()["people"]["recipient"]["note"] == "send here"
    assert client.delete(url(pid, "/recipient")).json()["people"]["recipient"] is None


def test_routes_refuse_a_property_from_another_market(client, conn):
    pid = add_prop(conn)
    engine.add_person(conn, pid, "Bea Broker", role="broker")
    bea = person_id(conn, pid, "Bea Broker")
    other = next(m["slug"] for m in rows(conn, "SELECT slug FROM markets") if m["slug"] != MARKET)
    assert client.put(f"/api/m/{other}/properties/{pid}/recipient", json={"person_id": bea}).status_code == 404
    assert client.put(f"/api/m/{other}/properties/{pid}/people/{bea}/role", json={"role": "member"}).status_code == 404
