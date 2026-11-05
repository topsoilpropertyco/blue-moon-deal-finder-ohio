"""Offers: price from the 4x rent, snapshots, versions, the stale-draft rule, marking sent, responses and counter
figures, next actions, hand-entered history, and the routes and columns that show them. Made-up properties only."""
from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from bluemoon import napkin, offers, pricing
from bluemoon.db import dumps, loads, market_by_slug, now_iso, one, rows, scalar
from bluemoon.drill import engine

MARKET = "apartments-springfield-oh"
TODAY = date(2026, 11, 5)
CFG = pricing.load_config({"evaluation_date": TODAY, "follow_up_days": 7})
_ids = itertools.count(1)


def add_prop(conn, **kw) -> int:
    n = next(_ids)
    data = {"market_id": market_by_slug(conn, MARKET)["id"], "property_key": f"offers:{n}", "address": f"{n} OFFER LN",
            "city": "SPRINGFIELD", "state": "OH", "units": 32, "owner_name": "EXAMPLE HOLDINGS LLC", "raw_json": "{}",
            "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso()}
    data.update(kw)
    pid = int(conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                           tuple(data.values())).lastrowid)
    conn.commit()
    return pid


def rent(conn, pid, monthly="34240") -> None:
    napkin.add_record(conn, pid, source="manual", basis="scheduled", actor="seth", total=monthly, total_period="monthly")


def prop_of(conn, pid):
    return one(conn, "SELECT * FROM properties WHERE id=?", (pid,))


def snap(conn, pid):
    return napkin.snapshot(conn, prop_of(conn, pid), "apartments", CFG, None)


def prepare(conn, pid, **kw):
    return offers.prepare(conn, prop_of(conn, pid), snap(conn, pid), "seth", **kw)


def priced(conn):
    pid = add_prop(conn)
    rent(conn, pid)
    return pid


def sent(conn, pid, how="email", when=None):
    o = prepare(conn, pid)
    return offers.mark_sent(conn, o["id"], how, when, "seth", CFG)


def kinds(conn, oid):
    return [r["kind"] for r in rows(conn, "SELECT kind FROM offer_events WHERE offer_id=? ORDER BY id", (oid,))]


# --- price ----------------------------------------------------------------------------------------------

def test_a_new_offer_starts_at_exactly_4x_and_follows_the_default(conn):
    pid = priced(conn)
    o = prepare(conn, pid)
    assert (o["version"], o["status"], o["price_cents"], o["price_mode"]) == (1, "internal_draft", 164352000, "default")
    v = offers.views(conn, [o])[0]
    assert v["implied_multiple"] == "4.00" and v["rent"]["annual_cents"] == 41088000
    assert [s["cents"] for s in v["rent"]["scenarios"]] == [164352000, 205440000, 246528000]
    assert v["sent"] is None and o["sent_on"] is None


def test_a_typed_price_is_an_override_with_its_implied_multiple(conn):
    pid = priced(conn)
    o = prepare(conn, pid, price="1,700,000")
    assert (o["price_cents"], o["price_mode"]) == (170000000, "override")
    assert offers.views(conn, [o])[0]["implied_multiple"] == "4.14"
    o = offers.set_price(conn, o["id"], None, "seth")                    # back to following the default
    assert (o["price_cents"], o["price_mode"]) == (164352000, "default")
    o = offers.set_price(conn, o["id"], 1_650_000, "seth")
    assert (o["price_cents"], o["price_mode"]) == (165000000, "override")


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "nan", "inf", 10**12])
def test_prices_that_cannot_be_right_are_refused(conn, bad):
    pid = priced(conn)
    with pytest.raises(ValueError):
        prepare(conn, pid, price=bad)
    assert scalar(conn, "SELECT COUNT(*) FROM offers") == 0


def test_missing_or_zero_rent_never_gives_a_zero_offer(conn):
    pid = add_prop(conn, units=None)                                    # no rent, no estimate
    with pytest.raises(ValueError, match="no Blue Moon price to start from"):
        prepare(conn, pid)
    assert prepare(conn, pid, price="900000")["price_mode"] == "override"      # a typed price still works
    zero = add_prop(conn)
    napkin.add_record(conn, zero, source="owner", basis="actual", actor="seth", total="0", total_period="monthly")
    with pytest.raises(ValueError, match="unknown or \\$0"):
        prepare(conn, zero)


# --- snapshots, versions and the stale rule ------------------------------------------------------------

def test_only_one_unsent_draft_and_a_revision_is_a_new_version(conn):
    pid = priced(conn)
    first = prepare(conn, pid)
    with pytest.raises(ValueError, match="still a draft"):
        prepare(conn, pid)
    offers.mark_sent(conn, first["id"], "mail", None, "seth", CFG)
    second = prepare(conn, pid, price="1,600,000")
    assert second["version"] == 2 and second["id"] != first["id"]
    assert one(conn, "SELECT status, price_cents FROM offers WHERE id=?", (first["id"],)) == {"status": "sent", "price_cents": 164352000}
    assert [e for e in kinds(conn, second["id"])] == ["prepared"]
    assert loads(rows(conn, "SELECT detail_json FROM offer_events WHERE offer_id=?", (second["id"],))[0]["detail_json"])["revises"] == first["id"]


def test_a_sent_version_keeps_its_numbers_when_the_rent_changes(conn):
    pid = priced(conn)
    o = sent(conn, pid)
    before = one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],))
    napkin.add_record(conn, pid, source="rent_roll", basis="actual", actor="seth", total="40000", total_period="monthly")
    assert snap(conn, pid)["annual_cents"] == 48000000
    assert one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],)) == before          # nothing was rewritten
    v = offers.views(conn, [before], {pid: snap(conn, pid)["hash"]})[0]
    assert v["stale"] is False and v["rent"]["annual_cents"] == 41088000 and v["price_cents"] == 164352000
    with pytest.raises(ValueError, match="already sent"):
        offers.refresh(conn, o["id"], prop_of(conn, pid), snap(conn, pid), "seth")
    with pytest.raises(ValueError, match="already sent"):
        offers.set_price(conn, o["id"], "1", "seth")
    assert prepare(conn, pid)["price_cents"] == 192000000                               # the revision uses the new rent


def test_a_draft_goes_stale_and_refresh_moves_only_a_default_price(conn):
    pid = priced(conn)
    follow, typed = prepare(conn, pid), None
    napkin.add_record(conn, pid, source="rent_roll", basis="actual", actor="seth", total="40000", total_period="monthly")
    now = snap(conn, pid)
    v = offers.views(conn, [follow], {pid: now["hash"]})[0]
    assert v["stale"] is True and v["price_cents"] == 164352000                         # warned, not changed
    assert offers.views(conn, [follow], {pid: follow["snapshot_hash"]})[0]["stale"] is False
    refreshed = offers.refresh(conn, follow["id"], prop_of(conn, pid), now, "seth")
    assert refreshed["price_cents"] == 192000000 and refreshed["snapshot_hash"] == now["hash"]
    assert offers.views(conn, [refreshed], {pid: now["hash"]})[0]["stale"] is False

    other = add_prop(conn)
    rent(conn, other)
    typed = prepare(conn, other, price="1,700,000")
    napkin.add_record(conn, other, source="rent_roll", basis="actual", actor="seth", total="40000", total_period="monthly")
    kept = offers.refresh(conn, typed["id"], prop_of(conn, other), snap(conn, other), "seth")
    assert (kept["price_cents"], kept["price_mode"]) == (170000000, "override")
    assert offers.views(conn, [kept])[0]["implied_multiple"] == "3.54"                  # against the new yearly rent


def test_the_default_price_follows_changes_in_what_the_draft_is_based_on_only_after_refresh(conn):
    pid = priced(conn)
    o = prepare(conn, pid)
    napkin.save_assumptions(conn, pid, {"units": 10}, "seth")      # estimate inputs change, but a record is selected
    assert one(conn, "SELECT price_cents FROM offers WHERE id=?", (o["id"],))["price_cents"] == 164352000


# --- sending -------------------------------------------------------------------------------------------

def test_a_draft_is_never_counted_as_sent(conn):
    pid = priced(conn)
    o = prepare(conn, pid)
    v = offers.views(conn, [o])[0]
    assert v["status"] == "internal_draft" and v["sent"] is None
    assert scalar(conn, "SELECT COUNT(*) FROM offers WHERE status='sent'") == 0
    with pytest.raises(ValueError, match="Only an offer that was sent"):
        offers.add_response(conn, o["id"], "We might be interested", "seth", CFG)
    with pytest.raises(ValueError):
        offers.set_status(conn, o["id"], "sent", "seth")                       # only mark_sent can do that
    with pytest.raises(ValueError, match="no LOI made"):
        offers.set_status(conn, o["id"], "loi_generated", "seth")


def test_marking_sent_records_how_when_and_who_and_adds_the_follow_up(conn):
    pid = priced(conn)
    o = prepare(conn, pid)
    done = offers.mark_sent(conn, o["id"], "in person", "2026-11-03", "seth@example.com", CFG)
    assert (done["status"], done["sent_how"], done["sent_on"], done["sent_by"], done["sent_via"]) == (
        "sent", "in person", "2026-11-03", "seth@example.com", "manual")
    a = rows(conn, "SELECT * FROM offer_actions WHERE offer_id=?", (o["id"],))
    assert len(a) == 1 and a[0]["text"] == "Follow up: did they get the LOI?" and a[0]["due_on"] == "2026-11-10"
    assert (a[0]["auto"], a[0]["owner"], a[0]["done"]) == (1, "seth@example.com", 0)
    assert kinds(conn, o["id"]) == ["prepared", "marked_sent", "next_action"]
    with pytest.raises(ValueError, match="already sent"):                      # a retry never double counts
        offers.mark_sent(conn, o["id"], "email", None, "seth", CFG)
    assert scalar(conn, "SELECT COUNT(*) FROM offer_actions") == 1


def test_sending_checks_the_method_and_the_day(conn):
    pid = priced(conn)
    o = prepare(conn, pid)
    for how, when, why in (("carrier pigeon", None, "Say how"), ("email", "not a date", "not a date"),
                           ("email", (TODAY + timedelta(days=1)).isoformat(), "future")):
        with pytest.raises(ValueError, match=why):
            offers.mark_sent(conn, o["id"], how, when, "seth", CFG)
    with pytest.raises(ValueError, match="'via'"):
        offers.mark_sent(conn, o["id"], "email", None, "seth", CFG, via="pigeon")
    assert one(conn, "SELECT status FROM offers WHERE id=?", (o["id"],))["status"] == "internal_draft"
    assert offers.mark_sent(conn, o["id"], "email", None, "seth", CFG)["sent_on"] == TODAY.isoformat()


def test_a_hand_marked_send_is_told_apart_from_a_provider_send(conn):
    a, b = priced(conn), priced(conn)
    hand = offers.mark_sent(conn, prepare(conn, a)["id"], "email", None, "seth", CFG)
    auto = offers.mark_sent(conn, prepare(conn, b)["id"], "email", None, "mail-provider", CFG, via="provider")
    vh, va = offers.views(conn, [hand, auto])
    assert (vh["sent"]["via"], vh["sent"]["via_label"]) == ("manual", "Marked by hand")
    assert (va["sent"]["via"], va["sent"]["via_label"]) == ("provider", "Reported by the mail provider")


def test_letter_stages_need_a_letter_made_from_the_current_numbers(conn):
    pid = priced(conn)
    o = prepare(conn, pid)
    conn.execute("INSERT INTO offer_documents(offer_id, template_id, snapshot_hash, created_by, created_at) VALUES(?,?,?,?,?)",
                 (o["id"], "t", "an-older-snapshot", "seth", now_iso()))
    with pytest.raises(ValueError, match="no LOI made"):
        offers.set_status(conn, o["id"], "ready_for_review", "seth")
    conn.execute("INSERT INTO offer_documents(offer_id, template_id, snapshot_hash, created_by, created_at) VALUES(?,?,?,?,?)",
                 (o["id"], "t", o["snapshot_hash"], "seth", now_iso()))
    conn.commit()
    assert offers.set_status(conn, o["id"], "ready_for_review", "seth")["status"] == "ready_for_review"
    assert offers.views(conn, [o])[0]["readiness"]["template"]["ready"] is True
    assert offers.set_status(conn, o["id"], "loi_template_needed", "seth")["status"] == "loi_template_needed"
    offers.set_status(conn, o["id"], "ready_to_send", "seth")
    offers.set_price(conn, o["id"], "1,500,000", "seth")                      # the letter no longer matches
    assert one(conn, "SELECT status FROM offers WHERE id=?", (o["id"],))["status"] == "internal_draft"
    with pytest.raises(ValueError, match="not a stage"):
        offers.set_status(conn, o["id"], "nonsense", "seth")


# --- who it is addressed to --------------------------------------------------------------------------------

def test_the_recipient_is_frozen_apart_from_the_seller(conn):
    from bluemoon import people
    pid = priced(conn)
    engine.add_person(conn, pid, "Bea Broker", role="broker")
    engine.add_contact(conn, pid, "email", "bea@example.com", person_name="Bea Broker")
    bea = scalar(conn, "SELECT id FROM people WHERE property_id=?", (pid,))
    contact = scalar(conn, "SELECT id FROM contact_points WHERE property_id=?", (pid,))
    early = prepare(conn, pid)
    assert early["recipient_name"] is None and early["seller_name"] == "EXAMPLE HOLDINGS LLC"
    assert offers.views(conn, [early])[0]["readiness"]["recipient"] == {"ready": False, "text": "Recipient needed"}
    people.choose_recipient(conn, pid, bea, contact, None, "seth")
    assert one(conn, "SELECT recipient_name FROM offers WHERE id=?", (early["id"],))["recipient_name"] is None
    later = offers.refresh(conn, early["id"], prop_of(conn, pid), snap(conn, pid), "seth")
    assert (later["recipient_name"], later["recipient_contact"], later["seller_name"]) == (
        "Bea Broker", "email: bea@example.com", "EXAMPLE HOLDINGS LLC")
    conn.execute("UPDATE contact_points SET status='dnc' WHERE id=?", (contact,))
    conn.commit()
    again = offers.refresh(conn, early["id"], prop_of(conn, pid), snap(conn, pid), "seth")
    assert again["recipient_name"] == "Bea Broker" and again["recipient_contact"] is None   # a blocked contact is not copied


# --- responses -----------------------------------------------------------------------------------------

def test_a_counter_is_recorded_with_its_range_and_never_changes_the_offer(conn):
    pid = priced(conn)
    o = sent(conn, pid)
    r = offers.add_response(conn, o["id"], "  We'd do 1.9 to 2.0 million. ", "seth", CFG, classification="counter",
                            counter_low="1,900,000", counter_high="2,000,000", received_on="2026-11-04")
    assert (r["classification"], r["counter_low_cents"], r["counter_high_cents"], r["received_on"]) == (
        "counter", 190000000, 200000000, "2026-11-04")
    assert r["original_text"] == "We'd do 1.9 to 2.0 million."
    after = one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],))
    assert after["price_cents"] == 164352000 and after["price_mode"] == "default" and after["status"] == "sent"
    v = offers.views(conn, [after])[0]
    assert v["counter"]["low_cents"] == 190000000 and v["counter"]["high_cents"] == 200000000
    assert (v["counter"]["low_multiple"], v["counter"]["high_multiple"]) == ("4.62", "4.87")
    assert v["price_cents"] == 164352000 and "response" in kinds(conn, o["id"])


def test_a_single_figure_counter_keeps_no_top_of_range(conn):
    o = sent(conn, priced(conn))
    r = offers.add_response(conn, o["id"], "2 million", "seth", CFG, classification="counter", counter_low="2000000")
    assert (r["counter_low_cents"], r["counter_high_cents"]) == (200000000, None)


def test_an_unclear_reply_waits_for_review_and_amounts_follow_the_category(conn):
    o = sent(conn, priced(conn))
    r = offers.add_response(conn, o["id"], "Let's talk", "seth", CFG)
    assert r["classification"] == "needs_review"
    for kw, why in (({"classification": "counter"}, "needs the amount"),
                    ({"classification": "rejection", "counter_low": "5"}, "Only a counteroffer"),
                    ({"classification": "counter", "counter_low": "2000000", "counter_high": "1000000"}, "below the bottom"),
                    ({"classification": "counter", "counter_high": "2000000"}, "lower figure"),
                    ({"classification": "agreed"}, "must be one of")):
        with pytest.raises(ValueError, match=why):
            offers.add_response(conn, o["id"], "x", "seth", CFG, **kw)
    with pytest.raises(ValueError, match="Write the response"):
        offers.add_response(conn, o["id"], "   ", "seth", CFG)
    with pytest.raises(ValueError, match="longer than"):
        offers.add_response(conn, o["id"], "x" * 5001, "seth", CFG)
    with pytest.raises(ValueError, match="future"):
        offers.add_response(conn, o["id"], "x", "seth", CFG, received_on=(TODAY + timedelta(days=2)).isoformat())
    assert scalar(conn, "SELECT COUNT(*) FROM offer_responses") == 1


def test_correcting_a_response_keeps_the_original_text(conn):
    o = sent(conn, priced(conn))
    r = offers.add_response(conn, o["id"], "Maybe 1.8?", "seth", CFG)
    fixed = offers.correct_response(conn, r["id"], "pat", classification="counter", counter_low="1800000")
    assert (fixed["classification"], fixed["counter_low_cents"], fixed["corrected_by"]) == ("counter", 180000000, "pat")
    assert fixed["original_text"] == "Maybe 1.8?" and fixed["recorded_by"] == "seth"
    assert one(conn, "SELECT price_cents FROM offers WHERE id=?", (o["id"],))["price_cents"] == 164352000
    back = offers.correct_response(conn, r["id"], "pat", classification="info_request")
    assert back["counter_low_cents"] is None
    with pytest.raises(KeyError):
        offers.correct_response(conn, 999, "pat", classification="counter", counter_low="1")


# --- next actions --------------------------------------------------------------------------------------

def test_next_actions_can_be_added_changed_and_ticked_off(conn):
    o = sent(conn, priced(conn))
    mine = offers.add_action(conn, o["id"], "Call the broker", "seth", due_on="2026-11-06")
    assert mine["owner"] == "seth" and mine["due_on"] == "2026-11-06"
    v = offers.views(conn, [one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],))])[0]
    assert v["next_action"]["text"] == "Call the broker"                       # the earliest due date comes first
    follow = next(a for a in v["actions"] if a["auto"])
    moved = offers.update_action(conn, follow["id"], {"due_on": "2026-11-20", "owner": "pat"}, "seth")
    assert (moved["due_on"], moved["owner"]) == ("2026-11-20", "pat")
    done = offers.update_action(conn, mine["id"], {"done": True}, "seth")
    assert (done["done"], done["done_by"]) == (1, "seth") and done["done_at"]
    v = offers.views(conn, [one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],))])[0]
    assert v["next_action"]["id"] == follow["id"]
    assert offers.update_action(conn, mine["id"], {"done": False}, "seth")["done_at"] is None
    assert offers.update_action(conn, follow["id"], {"due_on": None}, "seth")["due_on"] is None
    with pytest.raises(ValueError, match="not a date"):
        offers.update_action(conn, follow["id"], {"due_on": "soon"}, "seth")
    with pytest.raises(ValueError, match="Write the next action"):
        offers.add_action(conn, o["id"], " ", "seth")
    assert {"added", "done", "reopened", "changed"} <= {loads(r["detail_json"])["change"] for r in rows(
        conn, "SELECT detail_json FROM offer_events WHERE kind='next_action'")}


# --- offers entered by hand after the fact --------------------------------------------------------------

def test_an_earlier_offer_is_a_sent_record_with_an_incomplete_snapshot(conn):
    pid = add_prop(conn)                                                       # no rent needed to record history
    h = offers.record_historical(conn, prop_of(conn, pid), "seth", CFG, price="1,250,000", how="mail",
                                 sent_on="2026-06-01", recipient_name="Old Broker", note="From my notes")
    assert (h["status"], h["origin"], h["incomplete_snapshot"], h["version"], h["price_cents"]) == (
        "sent", "historical", 1, 1, 125000000)
    v = offers.views(conn, [h])[0]
    assert v["rent"] is None and v["implied_multiple"] is None and v["recipient"]["name"] == "Old Broker"
    assert v["sent"]["via"] == "manual" and v["stale"] is False
    assert scalar(conn, "SELECT COUNT(*) FROM offer_actions") == 0              # history gets no automatic follow-up
    with pytest.raises(ValueError, match="already recorded as version 1"):
        offers.record_historical(conn, prop_of(conn, pid), "seth", CFG, price="1250000", how="mail", sent_on="2026-06-01")
    with pytest.raises(ValueError, match="already sent"):                       # sent is final, however it was entered
        offers.refresh(conn, h["id"], prop_of(conn, pid), snap(conn, pid), "seth")
    r = offers.add_response(conn, h["id"], "Said no", "seth", CFG, classification="rejection", received_on="2026-06-10")
    assert r["classification"] == "rejection"


def test_correcting_a_hand_entered_offer_does_not_add_a_second_one(conn):
    pid = add_prop(conn)
    one_ = offers.record_historical(conn, prop_of(conn, pid), "seth", CFG, price="1,250,000", how="mail", sent_on="2026-06-01")
    two = offers.record_historical(conn, prop_of(conn, pid), "seth", CFG, price="1,300,000", how="email", sent_on="2026-07-01")
    fixed = offers.correct_historical(conn, one_["id"], {"price": "1,255,000", "how": "email"}, "pat", CFG)
    assert (fixed["price_cents"], fixed["sent_how"], fixed["sent_on"], fixed["version"]) == (125500000, "email", "2026-06-01", 1)
    assert scalar(conn, "SELECT COUNT(*) FROM offers WHERE property_id=?", (pid,)) == 2 and two["version"] == 2
    with pytest.raises(ValueError, match="already recorded"):
        offers.correct_historical(conn, one_["id"], {"price": "1,300,000", "sent_on": "2026-07-01"}, "pat", CFG)
    assert "corrected" in kinds(conn, one_["id"])
    live = prepare(conn, priced(conn))
    with pytest.raises(ValueError, match="Only an offer entered after the fact"):
        offers.correct_historical(conn, live["id"], {"price": "1"}, "pat", CFG)
    assert prepare(conn, pid, price="1,400,000")["version"] == 3                 # a new offer continues the numbering


# --- the log ---------------------------------------------------------------------------------------------

def test_every_step_is_logged_in_order_with_who_did_it(conn):
    o = priced(conn)
    d = prepare(conn, o)
    offers.set_price(conn, d["id"], "1,700,000", "pat")
    offers.refresh(conn, d["id"], prop_of(conn, o), snap(conn, o), "pat")
    offers.mark_sent(conn, d["id"], "email", None, "seth", CFG)
    r = offers.add_response(conn, d["id"], "ok", "seth", CFG)
    offers.correct_response(conn, r["id"], "seth", classification="rejection")
    assert kinds(conn, d["id"]) == ["prepared", "price_changed", "refreshed", "marked_sent", "next_action", "response",
                                    "response_corrected"]
    assert [e["actor"] for e in rows(conn, "SELECT actor FROM offer_events WHERE offer_id=? ORDER BY id", (d["id"],))][:3] == [
        "seth", "pat", "pat"]
    assert [e["kind"] for e in offers.views(conn, [one(conn, "SELECT * FROM offers WHERE id=?", (d["id"],))], events=True)[0]["events"]][0] == "response_corrected"


# --- routes ---------------------------------------------------------------------------------------------

@pytest.fixture
def client(conn):
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def url(pid, tail=""):
    return f"/api/m/{MARKET}/properties/{pid}{tail}"


def oroute(oid, tail=""):
    return f"/api/m/{MARKET}/offers/{oid}{tail}"


def test_routes_run_the_whole_offer_flow(client, conn):
    pid = priced(conn)
    detail = client.get(url(pid)).json()["offers"]
    assert detail["can_prepare"] is True and detail["next_version"] == 1 and detail["offers"] == []
    assert detail["current"]["default_cents"] == 164352000 and detail["recipient"] is None

    r = client.post(url(pid, "/offers"), json={})
    assert r.status_code == 200 and "not sent" in r.json()["message"]
    o = r.json()["offers"]["offers"][0]
    assert (o["version"], o["price_cents"], o["price_mode"], o["status"]) == (1, 164352000, "default", "internal_draft")
    assert o["created_by"] and r.json()["offers"]["can_prepare"] is False
    assert client.post(url(pid, "/offers"), json={}).status_code == 400            # a draft is already open

    r = client.put(oroute(o["id"], "/price"), json={"price": "1,700,000"})
    assert r.json()["offers"]["offers"][0]["implied_multiple"] == "4.14"
    assert client.put(oroute(o["id"], "/price"), json={"price": "0"}).status_code == 400
    assert client.post(oroute(o["id"], "/refresh")).status_code == 200

    assert client.post(oroute(o["id"], "/sent"), json={"how": "fax"}).status_code == 400
    r = client.post(oroute(o["id"], "/sent"), json={"how": "email"})
    sent_offer = r.json()["offers"]["offers"][0]
    assert sent_offer["status"] == "sent" and sent_offer["sent"]["via"] == "manual" and sent_offer["sent"]["by"]
    assert sent_offer["next_action"]["text"] == "Follow up: did they get the LOI?"
    assert client.post(oroute(o["id"], "/sent"), json={"how": "email"}).status_code == 400
    assert client.put(oroute(o["id"], "/price"), json={"price": "1"}).status_code == 400    # sent is final

    r = client.post(oroute(o["id"], "/responses"), json={"text": "2 million", "classification": "counter", "counter_low": "2000000"})
    got = r.json()["offers"]["offers"][0]
    assert got["counter"]["low_cents"] == 200000000 and got["price_cents"] == 170000000
    rid = got["responses"][0]["id"]
    assert client.put(oroute(o["id"], f"/responses/{rid}"), json={"classification": "rejection"}).status_code == 200
    assert client.put(oroute(o["id"], "/responses/99999"), json={"classification": "rejection"}).status_code == 404

    r = client.post(oroute(o["id"], "/actions"), json={"text": "Call Bea", "due_on": "2099-01-01"})
    aid = next(a["id"] for a in r.json()["offers"]["offers"][0]["actions"] if a["text"] == "Call Bea")
    r = client.put(oroute(o["id"], f"/actions/{aid}"), json={"done": True})
    assert next(a for a in r.json()["offers"]["offers"][0]["actions"] if a["id"] == aid)["done"] == 1
    assert client.put(oroute(o["id"], "/actions/99999"), json={"done": True}).status_code == 404

    assert client.get(url(pid, "/offers")).json()["offers"]["can_prepare"] is True        # now a revision is possible
    assert client.post(url(pid, "/offers"), json={}).json()["offers"]["offers"][0]["version"] == 2


def test_routes_historical_offers_and_corrections(client, conn):
    pid = add_prop(conn)
    body = {"price": "1,250,000", "how": "mail", "sent_on": "2026-06-01", "recipient_name": "Old Broker"}
    r = client.post(url(pid, "/offers/historical"), json=body)
    assert r.status_code == 200 and r.json()["offers"]["offers"][0]["incomplete_snapshot"] is True
    assert client.post(url(pid, "/offers/historical"), json=body).status_code == 400
    assert client.post(url(pid, "/offers/historical"), json={**body, "how": None}).status_code == 400
    oid = r.json()["offers"]["offers"][0]["id"]
    r = client.put(oroute(oid, "/historical"), json={"price": "1,260,000"})
    assert r.json()["offers"]["offers"][0]["price_cents"] == 126000000
    assert len(r.json()["offers"]["offers"]) == 1


def test_routes_are_scoped_to_the_market_and_validate_input(client, conn):
    pid = priced(conn)
    oid = client.post(url(pid, "/offers"), json={}).json()["offers"]["offers"][0]["id"]
    other = next(m["slug"] for m in rows(conn, "SELECT slug FROM markets") if m["slug"] != MARKET)
    for method, tail, body in (("post", "/refresh", None), ("put", "/price", {"price": "5"}),
                               ("post", "/sent", {"how": "email"}), ("post", "/responses", {"text": "x"}),
                               ("post", "/actions", {"text": "x"})):
        r = getattr(client, method)(f"/api/m/{other}/offers/{oid}{tail}", **({"json": body} if body is not None else {}))
        assert r.status_code == 404, tail
    assert client.get(f"/api/m/{other}/properties/{pid}/offers").status_code == 404
    assert client.post(f"/api/m/{other}/properties/{pid}/offers", json={}).status_code == 404
    assert client.post(url(pid, "/offers"), json={"price": "x" * 100}).status_code in (400, 422)
    assert client.post(oroute(oid, "/sent"), json={"how": "x" * 50}).status_code == 422
    assert client.post(oroute(oid, "/responses"), json={"text": "x" * 6000}).status_code == 422
    assert client.post(oroute(99999, "/refresh")).status_code == 404
    other_prop = priced(conn)
    other_oid = client.post(url(other_prop, "/offers"), json={}).json()["offers"]["offers"][0]["id"]
    client.post(oroute(other_oid, "/sent"), json={"how": "email"})
    rid = client.post(oroute(other_oid, "/responses"), json={"text": "hi"}).json()["offers"]["offers"][0]["responses"][0]["id"]
    assert client.put(oroute(oid, f"/responses/{rid}"), json={"classification": "rejection"}).status_code == 404   # not that offer's


def test_the_offers_screen_lists_every_offer_with_stage_price_counter_and_next_action(client, conn):
    a, b, c = priced(conn), priced(conn), add_prop(conn)
    draft = client.post(url(a, "/offers"), json={}).json()["offers"]["offers"][0]
    oid = client.post(url(b, "/offers"), json={"price": "1,700,000"}).json()["offers"]["offers"][0]["id"]
    client.post(oroute(oid, "/sent"), json={"how": "email"})
    client.post(oroute(oid, "/responses"), json={"text": "2m", "classification": "counter", "counter_low": "2000000"})
    client.post(url(c, "/offers/historical"), json={"price": "900000", "how": "mail", "sent_on": "2026-05-01"})
    napkin.add_record(conn, a, source="rent_roll", basis="actual", actor="x", total="40000", total_period="monthly")
    listing = client.get(f"/api/m/{MARKET}/offers").json()
    assert listing["total"] == 3 and listing["counts"] == {"internal_draft": 1, "sent": 2}
    by = {o["property_id"]: o for o in listing["items"]}
    assert by[a]["status_label"] == "Internal draft" and by[a]["stale"] is True and by[a]["id"] == draft["id"]
    assert by[a]["address"] and by[a]["next_action"] is None
    assert by[b]["counter"]["low_cents"] == 200000000 and by[b]["implied_multiple"] == "4.14"
    assert by[b]["next_action"]["text"].startswith("Follow up")
    assert by[c]["incomplete_snapshot"] is True and by[c]["implied_multiple"] is None
    assert ["internal_draft", "Internal draft"] in listing["status_labels"]


def test_deals_rows_and_the_property_detail_carry_the_latest_offer_stage(client, conn):
    pid = priced(conn)
    rows_of = lambda: {r["id"]: r for r in client.get(f"/api/m/{MARKET}/properties?view=everything").json()["items"]}  # noqa: E731
    assert rows_of()[pid]["offer_status"] is None
    oid = client.post(url(pid, "/offers"), json={}).json()["offers"]["offers"][0]["id"]
    assert rows_of()[pid]["offer_status"] == "internal_draft"                  # a draft is never shown as sent
    client.post(oroute(oid, "/sent"), json={"how": "mail"})
    assert rows_of()[pid]["offer_status"] == "sent"
    client.post(url(pid, "/offers"), json={})
    assert rows_of()[pid]["offer_status"] == "internal_draft"                  # the newest version leads
    assert [o["version"] for o in client.get(url(pid)).json()["offers"]["offers"]] == [2, 1]


def test_exports_gain_offer_columns_and_a_draft_is_not_called_sent(conn, settings):
    from bluemoon import exports
    pid = priced(conn)
    other = priced(conn)
    d = prepare(conn, pid)
    offers.mark_sent(conn, prepare(conn, other)["id"], "email", None, "seth", CFG)
    data = exports.load_market(conn, MARKET)
    by = {r["property_id"]: r for r in data.properties}
    assert (by[pid]["offer_status"], by[pid]["offer_price"]) == ("Internal draft", d["price_cents"] / 100)
    assert (by[other]["offer_status"], by[other]["offer_price"]) == ("Sent", 1643520.0)
    headers = [c.header for c in exports.property_columns(settings.asset_class("apartments"))]
    assert "Offer status" in headers and "Offer price" in headers
