"""Sending through the test outbox: a send is a record of an attempt and never makes an offer "sent" (V40), the
send-intent key that makes retries safe while a revised version can go again (V41), the transport and its states
(V42), a batch that reports every record and survives a failure (V43), and replies that are entered by hand, kept
word for word and sorted by category (V44). Made-up people and a made-up letter link: nothing is real."""
from __future__ import annotations

import itertools
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from bluemoon import napkin, offers, people, pricing, sending
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.drill import engine
from bluemoon.sending import OutboxTransport, TransportError

MARKET = "apartments-springfield-oh"
TODAY = date(2026, 11, 5)
CFG = pricing.load_config({"evaluation_date": TODAY, "follow_up_days": 7})
DOC = {"template_id": "t1", "template_version": "1", "doc_id": "d1", "doc_url": "https://docs.google.com/document/d/d1",
       "pdf_id": "p1", "pdf_url": "https://drive.google.com/file/d/p1"}
_ids = itertools.count(1)


class Down:
    """A transport that cannot take messages."""
    name = "down"

    def submit(self, key, message):
        raise TransportError("The outbox is closed (test).")


class Odd:
    name = "odd"

    def submit(self, key, message):
        return "teleported", None


def add_prop(conn) -> int:
    n = next(_ids)
    data = {"market_id": market_by_slug(conn, MARKET)["id"], "property_key": f"send:{n}", "address": f"{n} SEND LN",
            "city": "SPRINGFIELD", "state": "OH", "units": 32, "owner_name": "EXAMPLE HOLDINGS LLC", "raw_json": "{}",
            "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso()}
    pid = int(conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                           tuple(data.values())).lastrowid)
    conn.commit()
    return pid


def prop_of(conn, pid):
    return one(conn, "SELECT * FROM properties WHERE id=?", (pid,))


def snap(conn, pid):
    return napkin.snapshot(conn, prop_of(conn, pid), "apartments", CFG, None)


def recipient(conn, pid, email="bea@example.com"):
    engine.add_person(conn, pid, "Bea Broker", role="broker")
    engine.add_contact(conn, pid, "email", email, person_name="Bea Broker")
    people.choose_recipient(conn, pid, scalar(conn, "SELECT id FROM people WHERE property_id=?", (pid,)),
                            scalar(conn, "SELECT id FROM contact_points WHERE property_id=?", (pid,)), None, "seth")


def ready(conn, pid=None, with_recipient=True) -> dict:
    """A property with rent and an offer that is Ready to send, with a letter made from its current numbers."""
    pid = pid or add_prop(conn)
    if not scalar(conn, "SELECT 1 FROM rent_records WHERE property_id=?", (pid,)):
        napkin.add_record(conn, pid, source="manual", basis="scheduled", actor="seth", total="34240", total_period="monthly")
    if with_recipient and not scalar(conn, "SELECT 1 FROM property_recipients WHERE property_id=?", (pid,)):
        recipient(conn, pid)
    o = offers.prepare(conn, prop_of(conn, pid), snap(conn, pid), "seth")
    offers.add_document(conn, o["id"], DOC, "seth")
    return offers.set_status(conn, o["id"], "ready_to_send", "seth")


def outbox(tmp_path):
    return OutboxTransport(tmp_path / "outbox")


def kinds(conn, oid):
    return [r["kind"] for r in rows(conn, "SELECT kind FROM offer_events WHERE offer_id=? ORDER BY id", (oid,))]


# --- V40: a send attempt is never "sent" ---------------------------------------------------------------------

def test_submitting_to_the_outbox_never_makes_an_offer_sent(conn, tmp_path):
    o = ready(conn)
    r = sending.send_offer(conn, o["id"], outbox(tmp_path), "seth")
    assert r["ok"] and r["state"] == "submitted" and "not marked as sent" in r["message"]
    after = one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],))
    assert after["status"] == "ready_to_send" and after["sent_on"] is None and after["sent_via"] is None
    assert scalar(conn, "SELECT COUNT(*) FROM offer_actions WHERE offer_id=?", (o["id"],)) == 0     # no follow-up yet
    assert "send_submitted" in kinds(conn, o["id"]) and "marked_sent" not in kinds(conn, o["id"])
    assert offers.views(conn, [after])[0]["sent"] is None


def test_a_failed_send_is_not_sent_either_and_marking_sent_stays_a_separate_step(conn, tmp_path):
    o = ready(conn)
    r = sending.send_offer(conn, o["id"], Down(), "seth")
    assert not r["ok"] and r["state"] == "failed" and "Nothing was sent" in r["message"]
    assert one(conn, "SELECT status FROM offers WHERE id=?", (o["id"],))["status"] == "ready_to_send"
    done = offers.mark_sent(conn, o["id"], "email", None, "seth", CFG)
    assert done["status"] == "sent" and done["sent_via"] == "manual"
    with pytest.raises(ValueError):                                     # a sent version is final: no more sends
        sending.send_offer(conn, o["id"], outbox(tmp_path), "seth")


def test_only_a_ready_offer_with_an_email_and_a_current_letter_can_be_sent(conn, tmp_path):
    t = outbox(tmp_path)
    pid = add_prop(conn)
    napkin.add_record(conn, pid, source="manual", basis="scheduled", actor="seth", total="34240", total_period="monthly")
    draft = offers.prepare(conn, prop_of(conn, pid), snap(conn, pid), "seth")
    with pytest.raises(ValueError, match="Ready to send"):
        sending.send_offer(conn, draft["id"], t, "seth")
    no_mail = ready(conn, with_recipient=False)
    with pytest.raises(ValueError, match="email address"):
        sending.send_offer(conn, no_mail["id"], t, "seth")
    stale = ready(conn)
    offers.set_price(conn, stale["id"], "1,700,000", "seth")           # the letter no longer matches the numbers
    conn.execute("UPDATE offers SET status='ready_to_send' WHERE id=?", (stale["id"],))
    with pytest.raises(ValueError, match="current numbers"):
        sending.send_offer(conn, stale["id"], t, "seth")
    assert scalar(conn, "SELECT COUNT(*) FROM offer_sends") == 0 and not (tmp_path / "outbox").exists()


# --- V41: the send-intent key --------------------------------------------------------------------------------

def test_the_same_intent_is_one_send_so_a_retry_never_submits_twice(conn, tmp_path):
    o = ready(conn)
    t = outbox(tmp_path)
    first = sending.send_offer(conn, o["id"], t, "seth")
    again = sending.send_offer(conn, o["id"], t, "seth")
    assert again["duplicate"] and again["send_id"] == first["send_id"] and "nothing was submitted again" in again["message"]
    assert scalar(conn, "SELECT COUNT(*) FROM offer_sends") == 1 and len(list((tmp_path / "outbox").iterdir())) == 1
    assert kinds(conn, o["id"]).count("send_submitted") == 1


def test_the_key_names_offer_version_recipient_and_intent(conn):
    a = ready(conn)
    key = sending.intent_key(a)
    assert key == sending.intent_key(a) and key != sending.intent_key(a, "other")
    assert key != sending.intent_key({**a, "recipient_contact": "email: other@example.com"})
    assert key != sending.intent_key({**a, "version": 2}) and key != sending.intent_key({**a, "id": a["id"] + 1})


def test_a_revised_version_can_be_sent_again_but_the_old_one_cannot(conn, tmp_path):
    t = outbox(tmp_path)
    v1 = ready(conn)
    pid = v1["property_id"]
    sending.send_offer(conn, v1["id"], t, "seth")
    offers.mark_sent(conn, v1["id"], "email", None, "seth", CFG)
    v2 = ready(conn, pid)
    assert v2["version"] == 2
    r = sending.send_offer(conn, v2["id"], t, "seth")
    assert r["ok"] and not r["duplicate"] and scalar(conn, "SELECT COUNT(*) FROM offer_sends") == 2
    assert len(list((tmp_path / "outbox").iterdir())) == 2


def test_a_failed_send_is_retried_on_the_same_record(conn, tmp_path):
    o = ready(conn)
    bad = sending.send_offer(conn, o["id"], Down(), "seth")
    good = sending.send_offer(conn, o["id"], outbox(tmp_path), "seth")
    assert good["send_id"] == bad["send_id"] and good["state"] == "submitted" and not good["duplicate"]
    s = one(conn, "SELECT * FROM offer_sends")
    assert s["error"] is None and s["outbox_file"].endswith(".json")
    assert kinds(conn, o["id"]).count("send_failed") == 1 and kinds(conn, o["id"]).count("send_submitted") == 1


# --- V42: the transport, its states and the outbox file ----------------------------------------------------

def test_the_outbox_file_holds_who_it_would_go_to_and_the_letter_link_but_no_wording_or_internal_labels(conn, tmp_path):
    o = ready(conn)
    sending.send_offer(conn, o["id"], outbox(tmp_path), "seth")
    (f,) = (tmp_path / "outbox").iterdir()
    msg = json.loads(f.read_text(encoding="utf-8"))
    assert msg["to"] == "bea@example.com" and msg["to_name"] == "Bea Broker" and msg["letter_pdf"] == DOC["pdf_url"]
    assert msg["version"] == 1 and msg["offer_id"] == o["id"]
    assert set(msg) == {"intent", "offer_id", "version", "property_id", "to_name", "to", "letter_pdf", "letter_doc", "send_key"}
    assert "EXAMPLE HOLDINGS" not in f.read_text() and "1643520" not in f.read_text()    # no seller, no price, no body


def test_the_recorded_state_is_what_the_transport_reported(conn, tmp_path):
    o = ready(conn)
    sending.send_offer(conn, o["id"], outbox(tmp_path), "seth")
    v = offers.views(conn, [one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],))])[0]
    assert [(s["state"], s["transport"]) for s in v["sends"]] == [("submitted", "test outbox")]
    assert "nothing was emailed" in v["sends"][0]["state_label"]
    assert set(offers.SEND_STATES) == {"prepared", "queued", "submitted", "failed", "delivered"}


def test_an_unknown_state_from_a_transport_is_a_failure(conn):
    o = ready(conn)
    r = sending.send_offer(conn, o["id"], Odd(), "seth")
    assert r["state"] == "failed" and "unknown state" in one(conn, "SELECT error FROM offer_sends")["error"]


def test_an_unwritable_outbox_fails_the_send_without_a_crash(conn, tmp_path):
    blocker = tmp_path / "outbox"
    blocker.write_text("a file where the folder should be")
    o = ready(conn)
    r = sending.send_offer(conn, o["id"], OutboxTransport(blocker), "seth")
    assert r["state"] == "failed" and "could not be written" in r["message"]


# --- V43: a batch reports every record ---------------------------------------------------------------------

def test_a_batch_reports_each_record_and_one_problem_does_not_stop_the_rest(conn, tmp_path):
    good, other = ready(conn), ready(conn)
    t = outbox(tmp_path)
    sending.send_offer(conn, other["id"], t, "seth")                      # already done before the batch
    res = sending.send_many(conn, [good["id"], other["id"], 999999, good["id"]], t, "seth")
    by = {r["offer_id"]: r for r in res["results"]}
    assert len(res["results"]) == 3                                       # a repeated id is one record
    assert by[good["id"]]["ok"] and not by[good["id"]]["duplicate"]
    assert by[other["id"]]["duplicate"] and by[other["id"]]["ok"]
    assert not by[999999]["ok"] and "not found" in by[999999]["message"] and by[999999]["send_id"] is None
    assert res["text"] == "1 submitted to the test outbox, 1 already done, 1 failed. No offer was marked as sent."


def test_a_failing_transport_fails_every_record_and_a_rerun_makes_no_duplicates(conn, tmp_path):
    a, b = ready(conn), ready(conn)
    res = sending.send_many(conn, [a["id"], b["id"]], Down(), "seth")
    assert [r["state"] for r in res["results"]] == ["failed", "failed"] and res["text"].startswith("0 submitted")
    redo = sending.send_many(conn, [a["id"], b["id"]], outbox(tmp_path), "seth")
    assert [r["state"] for r in redo["results"]] == ["submitted", "submitted"]
    assert scalar(conn, "SELECT COUNT(*) FROM offer_sends") == 2


# --- routes ------------------------------------------------------------------------------------------------

@pytest.fixture
def client(conn):
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def test_routes_send_to_the_outbox_and_report_each_record(client, conn, settings):
    a, b = ready(conn), ready(conn)
    base = f"/api/m/{MARKET}/offers"
    r = client.post(f"{base}/{a['id']}/send")
    body = r.json()
    assert r.status_code == 200 and body["result"]["state"] == "submitted"
    card = body["offers"]["offers"][0]
    assert card["status"] == "ready_to_send" and card["sends"][0]["created_by"]
    assert (settings.path("db").parent / "outbox").is_dir()
    assert client.post(f"{base}/{a['id']}/send").json()["result"]["duplicate"] is True

    r = client.post(f"{base}/send", json={"offer_ids": [a["id"], b["id"], 424242]})
    assert r.status_code == 404                                           # an offer from nowhere stops the whole request
    r = client.post(f"{base}/send", json={"offer_ids": [a["id"], b["id"]]})
    assert [x["duplicate"] for x in r.json()["results"]] == [True, False]
    assert client.post(f"{base}/send", json={"offer_ids": []}).status_code == 422


def test_a_route_refuses_an_offer_that_is_not_ready(client, conn):
    pid = add_prop(conn)
    napkin.add_record(conn, pid, source="manual", basis="scheduled", actor="seth", total="34240", total_period="monthly")
    o = offers.prepare(conn, prop_of(conn, pid), snap(conn, pid), "seth")
    r = client.post(f"/api/m/{MARKET}/offers/{o['id']}/send")
    assert r.status_code == 400 and "Ready to send" in r.json()["detail"]


# --- V44: replies, entered by hand -------------------------------------------------------------------------

def test_a_reply_after_marking_sent_keeps_its_words_range_and_category_and_leaves_the_offer_alone(conn, tmp_path):
    o = ready(conn)
    sending.send_offer(conn, o["id"], outbox(tmp_path), "seth")
    with pytest.raises(ValueError, match="was sent"):                    # an outbox submission is not a send: no reply yet
        offers.add_response(conn, o["id"], "Hello", "seth", CFG)
    offers.mark_sent(conn, o["id"], "email", None, "seth", CFG)
    r = offers.add_response(conn, o["id"], "Maybe 1.9 to 2.0 million, call me", "seth", CFG,
                            classification="counter", counter_low="1900000", counter_high="2000000")
    assert (r["counter_low_cents"], r["counter_high_cents"]) == (190000000, 200000000)
    vague = offers.add_response(conn, o["id"], "Let's talk sometime", "seth", CFG)
    assert vague["classification"] == "needs_review"                     # unclear stays unclear
    fixed = offers.correct_response(conn, vague["id"], "seth", classification="serious_interest")
    assert fixed["classification"] == "serious_interest" and fixed["original_text"] == "Let's talk sometime"
    view = offers.views(conn, [one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],))])[0]
    assert view["price_cents"] == o["price_cents"] and view["counter"]["low_cents"] == 190000000
    assert view["next_action"]["text"] == offers.FOLLOW_UP_TEXT           # the next action stays visible
