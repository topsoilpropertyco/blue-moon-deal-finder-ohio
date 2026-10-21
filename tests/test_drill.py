"""Tests for the DRILL engine (bluemoon/drill/engine.py) and its stats (bluemoon/drill/stats.py).

Properties are inserted directly so these tests do not depend on the importer.
"""
from __future__ import annotations

import itertools
import os
import time

import pytest

from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.drill import engine as E
from bluemoon.drill import stats as S

MARKET = "apartments-springfield-oh"
_ids = itertools.count(1)


def add_prop(conn, market=MARKET, **kw) -> int:
    n = next(_ids)
    m = market_by_slug(conn, market)
    data = {
        "market_id": m["id"], "property_key": f"test:{n}", "address": f"{100 + n} TEST ST", "city": "SPRINGFIELD",
        "state": "OH", "zip": "45503", "county": "CLARK", "apn": f"340-01-00-{n:03d}-000", "lat": 39.9 + n / 1000,
        "lng": -83.8, "units": 24, "property_type": "Multifamily", "owner_name": None, "mailing_address": None,
        "mailing_city": None, "mailing_state": None, "mailing_zip": None,
        "crexi_url": f"https://www.crexi.com/property-records/test-{n}",
        "raw_json": "{}", "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso(),
    }
    data.update(kw)
    cols = ",".join(data)
    cur = conn.execute(f"INSERT INTO properties({cols}) VALUES({','.join('?' for _ in data)})", tuple(data.values()))
    conn.commit()
    return int(cur.lastrowid)


def set_owner(conn, pid, owner_type="individual", portfolio_key=None):
    conn.execute("INSERT OR REPLACE INTO owners(property_id, owner_type, portfolio_key, computed_at) VALUES(?,?,?,?)",
                 (pid, owner_type, portfolio_key, now_iso()))
    conn.commit()


def set_score(conn, pid, score, in_buybox=1, rank=None):
    conn.execute("INSERT OR REPLACE INTO scores(property_id, in_buybox, exclusion_reasons, score, rank, computed_at) "
                 "VALUES(?,?,?,?,?,?)", (pid, in_buybox, "[]", score, rank, now_iso()))
    conn.commit()


def stage_status(drill: dict) -> dict[str, str]:
    return {s["key"]: s["status"] for s in drill["stages"]}


def walk_to(conn, pid, stop_at: str, outcome: str = "nothing"):
    """Check off every stage before `stop_at` with `outcome`."""
    for _ in range(30):
        d = E.property_drill(conn, pid, with_links=False)
        nxt = d["next_step"]
        if nxt["kind"] != "stage" or nxt["stage_key"] == stop_at:
            return d
        E.record_attempt(conn, pid, nxt["stage_key"], outcome)
    raise AssertionError("never reached " + stop_at)


# --- config and basics ----------------------------------------------------------------------------

def test_drill_config_parses_the_real_yaml(settings):
    cfg = E.drill_config(settings)
    keys = [s.key for s in cfg.stages]
    assert keys[:3] == ["records_baseline", "portfolio_match", "county_auditor"]
    assert "crexi_credit" in keys and "deep_research" == keys[-1]
    assert cfg.by_key["paid_skip_trace"].enabled is False
    assert cfg.validate.outcomes["reached_confirmed"].effect == "confirm"
    assert cfg.validate.outcomes["bounced"].effect == "mark_wrong"
    assert cfg.validate.tries_before_moving_on >= 1
    assert cfg.levels == {1: "No owner data", 2: "Possible owner", 3: "Confirmed owner"}


def test_ensure_states_keeps_signature_and_behaviour(conn):
    a, b = add_prop(conn), add_prop(conn)
    assert E.ensure_states(conn, MARKET) == 2
    assert E.ensure_states(conn, MARKET) == 0
    st = one(conn, "SELECT level, status FROM drill_state WHERE property_id=?", (a,))
    assert st == {"level": 1, "status": "active"}
    assert scalar(conn, "SELECT COUNT(*) FROM drill_state WHERE property_id IN (?,?)", (a, b)) == 2


def test_applicable_stages_filters(settings):
    oh = {"state": "OH"}
    def keys(items):
        return [s["key"] for s in items]

    indiv = keys(E.applicable_stages(oh, "individual", "apartments"))
    assert "entity_unmask" not in indiv and "recorder_documents" not in indiv
    assert "rental_registration" in indiv and "property_contacts" in indiv
    llc = keys(E.applicable_stages(oh, "small_llc", "apartments"))
    assert "entity_unmask" in llc and "recorder_documents" in llc
    unknown = keys(E.applicable_stages(oh, None, "apartments"))
    assert "entity_unmask" in unknown                       # missing owner type counts as 'unknown'
    storage = keys(E.applicable_stages(oh, "individual", "self_storage"))
    assert "rental_registration" not in storage and "property_contacts" in storage
    land = keys(E.applicable_stages(oh, "individual", settings.asset_class("raw_land")))
    assert "property_contacts" not in land
    indiana = keys(E.applicable_stages({"state": "IN"}, "individual", "apartments"))
    assert "rental_registration" not in indiana             # Ohio law only
    paid = [s for s in E.applicable_stages(oh, "individual", "apartments") if s["key"] == "paid_skip_trace"]
    assert paid and paid[0]["enabled"] is False


# --- automatic stages -----------------------------------------------------------------------------

def test_records_baseline_with_owner_name(conn):
    pid = add_prop(conn, owner_name="DOE JOHN & JANE", mailing_address="88 TESTER LN", mailing_city="SPRINGFIELD",
                   mailing_state="OH", mailing_zip="45504", mailing_care_of="JOHN DOE")
    res = E.run_auto_stages(conn, MARKET)
    assert res["stages"]["records_baseline"]["found"] == 1
    assert "owner names found" in res["message"]
    d = E.property_drill(conn, pid)
    assert d["level"] == 2 and d["level_name"] == "Possible owner"
    assert d["best_guess_person"] == "John & Jane Doe"
    assert [(p["name"], p["role"]) for p in d["people"]] == [("DOE JOHN & JANE", "owner_of_record")]
    mail = [c for c in d["contacts"] if c["channel"] == "mail"]
    assert mail[0]["value"] == "88 TESTER LN, SPRINGFIELD, OH 45504" and mail[0]["notes"] == "c/o JOHN DOE"
    # A mailing address is not something to "validate": letters are their own stage.
    assert d["next_step"]["kind"] == "stage" and d["next_step"]["stage_key"] == "county_auditor"
    st = stage_status(d)
    assert st["records_baseline"] == "done_found" and st["portfolio_match"] == "done_nothing"
    assert st["county_auditor"] == "next" and st["rental_registration"] == "pending"
    assert st["paid_skip_trace"] == "disabled"
    assert d["channels"]["mail"] == "untested" and d["channels"]["phone"] == "none"


def test_auto_stages_are_idempotent_and_notice_a_new_owner(conn):
    pid = add_prop(conn, owner_name=None)
    E.run_auto_stages(conn, MARKET)
    n1 = scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE property_id=?", (pid,))
    again = E.run_auto_stages(conn, MARKET)
    assert again["stages"]["records_baseline"]["unchanged"] == 1
    assert again["stages"]["portfolio_match"]["unchanged"] == 1
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE property_id=?", (pid,)) == n1
    assert E.property_drill(conn, pid)["level"] == 1
    # A re-import fills in the owner name: the baseline records it.
    conn.execute("UPDATE properties SET owner_name='ROE RICHARD' WHERE id=?", (pid,))
    conn.commit()
    res = E.run_auto_stages(conn, MARKET)
    assert res["stages"]["records_baseline"]["found"] == 1
    d = E.property_drill(conn, pid)
    assert d["level"] == 2 and d["best_guess_person"] == "Richard Roe"
    # And a sale to a new owner is noticed too.
    conn.execute("UPDATE properties SET owner_name='SMITH JOHN A' WHERE id=?", (pid,))
    conn.commit()
    E.run_auto_stages(conn, MARKET)
    latest = rows(conn, "SELECT detail FROM drill_attempts WHERE property_id=? AND stage_key='records_baseline' "
                        "ORDER BY id DESC LIMIT 1", (pid,))[0]
    assert "changed to SMITH JOHN A" in latest["detail"]
    assert E.property_drill(conn, pid)["best_guess_person"] == "John A Smith"


def test_records_baseline_without_owner_keeps_mailing_for_letters(conn):
    pid = add_prop(conn, owner_name="Unknown", mailing_address="PO BOX 5", mailing_city="DAYTON",
                   mailing_state="OH", mailing_zip="45401")
    E.run_auto_stages(conn, MARKET)
    d = E.property_drill(conn, pid)
    assert d["level"] == 1                       # 'Unknown' is a placeholder, not an owner
    assert stage_status(d)["records_baseline"] == "done_nothing"
    assert [c["channel"] for c in d["contacts"]] == ["mail"]


def test_run_auto_stages_reports_progress(conn):
    add_prop(conn, owner_name="DOE JOHN")
    seen = []
    E.run_auto_stages(conn, MARKET, progress=lambda f, m: seen.append((f, m)))
    assert seen[0][0] == 0.0 and seen[-1][0] == 1.0
    E.run_auto_stages(conn, MARKET, progress=lambda f, m: 1 / 0)   # a broken callback never stops it


# --- the full walk: level 1 to level 3 ------------------------------------------------------------

def test_full_drill_walk_from_level_1_to_3(conn):
    pid = add_prop(conn, owner_name=None)
    E.ensure_states(conn, MARKET)
    d = E.property_drill(conn, pid)
    assert d["level"] == 1 and d["status"] == "active"
    assert d["next_step"]["kind"] == "stage" and d["next_step"]["stage_key"] == "records_baseline"

    E.run_auto_stages(conn, MARKET)
    d = E.property_drill(conn, pid)
    assert d["level"] == 1 and d["next_step"]["stage_key"] == "county_auditor"
    assert d["next_step"]["text"].startswith("County auditor:")

    # County auditor names the owner -> level 2 (possible owner).
    r = E.record_attempt(conn, pid, "county_auditor", "found", detail="Owner per auditor", person="DOE JOHN")
    assert r["ok"] and r["level"] == 2 and "Saved: County auditor" in r["message"]
    d = r["drill"]
    assert d["best_guess_person"] == "John Doe"
    assert d["people"][0]["role"] == "owner_of_record"
    assert d["next_step"]["stage_key"] == "rental_registration"

    # Rental registration gives a phone -> next step: reach out on it.
    r = E.record_attempt(conn, pid, "rental_registration", "found",
                         contacts=[{"channel": "phone", "value": "937.555.0101", "notes": "owner phone"}])
    assert len(r["contacts_added"]) == 1
    phone = r["contacts_added"][0]
    assert phone["value"] == "(937) 555-0101" and phone["status"] == "untested"
    assert r["next_step"]["kind"] == "validate" and "(937) 555-0101" in r["next_step"]["text"]
    assert "TCPA" in r["next_step"]["note"]
    assert r["drill"]["channels"]["phone"] == "untested"

    # No answer twice: the drill moves on, the number stays untested.
    r = E.log_outreach(conn, pid, "phone", "no_answer", contact_point_id=phone["id"])
    assert r["next_step"]["kind"] == "validate"
    r = E.log_outreach(conn, pid, "phone", "left_message", contact_point_id=phone["id"], notes="voicemail")
    assert r["next_step"]["kind"] == "stage" and r["next_step"]["stage_key"] == "entity_unmask"
    c = one(conn, "SELECT status, last_tested_at FROM contact_points WHERE id=?", (phone["id"],))
    assert c["status"] == "untested" and c["last_tested_at"]

    # They call back and confirm: level 3, confirmed, ready for the CRM (not yet handed off).
    r = E.log_outreach(conn, pid, "phone", "reached_confirmed", contact_point_id=phone["id"])
    assert r["effect"] == "confirm" and r["level"] == 3 and r["status"] == "confirmed"
    st = one(conn, "SELECT * FROM drill_state WHERE property_id=?", (pid,))
    assert st["level"] == 3 and st["confirmed_person"] == "John Doe" and st["confirmed_at"]
    assert st["handed_off_at"] is None
    assert one(conn, "SELECT status FROM contact_points WHERE id=?", (phone["id"],))["status"] == "verified"
    assert r["next_step"]["kind"] == "handoff" and "Hand off to the CRM" in r["next_step"]["text"]
    assert [o["outcome"] for o in r["drill"]["outreach"]] == ["reached_confirmed", "left_message", "no_answer"]

    rows_ = S.crm_handoff_rows(conn, MARKET)
    assert len(rows_) == 1 and rows_[0]["confirmed_person"] == "John Doe"
    assert rows_[0]["phone"] == "(937) 555-0101" and "voicemail" in rows_[0]["notes"]

    assert E.mark_handed_off(conn, [pid]) == 1
    assert E.mark_handed_off(conn, [pid]) == 0
    d = E.property_drill(conn, pid)
    assert "Handed off to the CRM on" in d["next_step"]["text"]
    assert S.crm_handoff_rows(conn, MARKET, include_handed_off=False) == []


def test_records_never_set_level_3(conn):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    for key in ("county_auditor", "entity_unmask", "recorder_documents"):
        E.record_attempt(conn, pid, key, "found", person="Jane Roe", person_role="member")
    E.add_contact(conn, pid, "email", "Jane@Example.com")
    E.set_contact_status(conn, one(conn, "SELECT id FROM contact_points WHERE channel='email'")["id"], "verified")
    assert E.property_drill(conn, pid)["level"] == 2


# --- validate effects -----------------------------------------------------------------------------

def _with_contacts(conn, *contacts):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    ids = []
    for ch, val in contacts:
        ids.append(E.add_contact(conn, pid, ch, val, source="web_footprint")["contact"]["id"])
    return pid, ids


def test_wrong_and_bounced_mark_contacts_wrong(conn):
    pid, (ph, em) = _with_contacts(conn, ("phone", "937-555-0102"), ("email", "owner@example.com"))
    r = E.log_outreach(conn, pid, "phone", "wrong_person", contact_point_id=ph)
    assert "marked wrong" in r["message"]
    assert r["next_step"]["kind"] == "validate" and r["next_step"]["contact_ids"] == [em]
    r = E.log_outreach(conn, pid, "email", "bounced", contact_point_id=em)
    assert r["next_step"]["kind"] == "stage"
    statuses = {c["id"]: c["status"] for c in r["drill"]["contacts"]}
    assert statuses[ph] == "wrong" and statuses[em] == "wrong"
    assert r["drill"]["channels"]["phone"] == "wrong"
    assert r["level"] == 2                     # wrong info never drops the owner data itself


def test_reached_not_owner_and_implicit_contact(conn):
    pid, (ph,) = _with_contacts(conn, ("phone", "937-555-0103"))
    r = E.log_outreach(conn, pid, "text", "reached_not_owner")    # one untested number: it is that one
    assert r["contact_point_id"] == ph
    assert one(conn, "SELECT status FROM contact_points WHERE id=?", (ph,))["status"] == "wrong"


def test_mark_wrong_without_a_contact_asks_which(conn):
    pid, _ = _with_contacts(conn, ("phone", "937-555-0104"), ("phone", "937-555-0105"))
    r = E.log_outreach(conn, pid, "phone", "wrong_person")
    assert "Pick which contact" in r["message"]
    assert r["contact_point_id"] is None


def test_replied_unconfirmed_stays_in_validate(conn):
    pid, (em,) = _with_contacts(conn, ("email", "a@b.co"))
    for _ in range(3):
        r = E.log_outreach(conn, pid, "email", "replied_unconfirmed", contact_point_id=em)
    assert r["next_step"]["kind"] == "validate"
    E.log_outreach(conn, pid, "email", "no_answer", contact_point_id=em)
    assert E.property_drill(conn, pid)["next_step"]["kind"] == "stage"


def test_tries_before_moving_on_comes_from_yaml(conn, settings):
    settings.drill["validate"]["tries_before_moving_on"] = 1
    pid, (ph,) = _with_contacts(conn, ("phone", "937-555-0106"))
    r = E.log_outreach(conn, pid, "phone", "no_answer", contact_point_id=ph, settings=settings)
    assert r["next_step"]["kind"] == "stage"


def test_do_not_contact_is_sticky(conn):
    pid, (ph, em) = _with_contacts(conn, ("phone", "937-555-0107"), ("email", "x@y.com"))
    r = E.log_outreach(conn, pid, "phone", "do_not_contact", contact_point_id=ph)
    assert r["status"] == "dnc" and r["next_step"]["kind"] == "dnc"
    assert {c["status"] for c in r["drill"]["contacts"]} == {"dnc"}
    with pytest.raises(ValueError, match="do-not-contact"):
        E.log_outreach(conn, pid, "email", "no_answer", contact_point_id=em)
    with pytest.raises(ValueError, match="do-not-contact"):
        E.level_action(conn, pid, "confirm", person="John Doe")
    # New contacts on a do-not-contact owner arrive as do-not-contact.
    added = E.add_contact(conn, pid, "phone", "937-555-0199")
    assert added["contact"]["status"] == "dnc"
    # The same number on another property is do-not-contact too.
    other = add_prop(conn, owner_name="ELSE SOMEONE")
    E.run_auto_stages(conn, MARKET)
    assert E.add_contact(conn, other, "text", "(937) 555-0107")["contact"]["status"] == "dnc"
    assert E.stage_queue(conn, MARKET, "county_auditor")["total"] == 1   # only `other` is open
    # Undo keeps contacts blocked.
    r = E.level_action(conn, pid, "undnc")
    assert r["status"] in ("active", "exhausted") and "stay marked do-not-contact" in r["message"]
    assert {c["status"] for c in r["drill"]["contacts"]} == {"dnc"}


def test_set_contact_status_dnc_spreads_and_validates(conn):
    _, (ph,) = _with_contacts(conn, ("phone", "937-555-0110"))
    b = add_prop(conn, owner_name="B OWNER")
    E.ensure_states(conn, MARKET)
    E.add_contact(conn, b, "vm", "+1 937 555 0110")
    r = E.set_contact_status(conn, ph, "dnc")
    assert "1 other property had it too" in r["message"]
    assert one(conn, "SELECT status FROM contact_points WHERE property_id=?", (b,))["status"] == "dnc"
    with pytest.raises(ValueError, match="not a contact status"):
        E.set_contact_status(conn, ph, "maybe")
    with pytest.raises(KeyError):
        E.set_contact_status(conn, 999999, "verified")


# --- people, contacts, best guess -----------------------------------------------------------------

def test_contacts_are_normalized_and_deduped(conn):
    pid = add_prop(conn)
    E.ensure_states(conn, MARKET)
    a = E.add_contact(conn, pid, "Phone", "937.555.0120")
    b = E.add_contact(conn, pid, "cell", "(937) 555-0120")
    assert a["created"] and not b["created"] and "already on this property" in b["message"]
    e1 = E.add_contact(conn, pid, "e-mail", " Owner@Example.COM ")
    assert e1["contact"]["value"] == "owner@example.com"
    assert not E.add_contact(conn, pid, "email", "owner@example.com")["created"]
    ext = E.add_contact(conn, pid, "phone", "937-555-0121 ext 12")
    assert ext["contact"]["value"] == "(937) 555-0121 x12"
    social = E.add_contact(conn, pid, "facebook", "https://www.facebook.com/john.doe/", person_name="John Doe")
    assert social["contact"]["channel"] == "social" and social["contact"]["person_id"]
    assert not E.add_contact(conn, pid, "social", "facebook.com/john.doe")["created"]
    with pytest.raises(ValueError, match="10-digit"):
        E.add_contact(conn, pid, "phone", "555-0101")
    with pytest.raises(ValueError, match="email address"):
        E.add_contact(conn, pid, "email", "not-an-email")
    with pytest.raises(ValueError, match="not a contact type"):
        E.add_contact(conn, pid, "pigeon", "coo")
    with pytest.raises(ValueError, match="blank"):
        E.add_contact(conn, pid, "phone", "")


def test_bad_contact_in_a_check_off_saves_nothing(conn):
    pid = add_prop(conn)
    E.ensure_states(conn, MARKET)
    with pytest.raises(ValueError, match="10-digit"):
        E.record_attempt(conn, pid, "county_auditor", "found", person="Jo Ann",
                         contacts=[{"channel": "phone", "value": "12"}])
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE property_id=?", (pid,)) == 0
    assert scalar(conn, "SELECT COUNT(*) FROM people WHERE property_id=?", (pid,)) == 0


def test_best_guess_person_priority(conn):
    pid = add_prop(conn, owner_name="ACME APARTMENTS LLC")
    E.run_auto_stages(conn, MARKET)
    assert E.property_drill(conn, pid)["best_guess_person"] is None       # an LLC is not a person
    E.add_person(conn, pid, "Mike Manager", role="manager", source="property_contacts")
    assert E.property_drill(conn, pid)["best_guess_person"] is None       # managers never count
    E.add_person(conn, pid, "CT CORPORATION SYSTEM", role="statutory_agent")
    assert E.property_drill(conn, pid)["best_guess_person"] is None       # agent companies are not people
    E.add_person(conn, pid, "Sam Agent", role="statutory_agent", source="entity_unmask")
    assert E.property_drill(conn, pid)["best_guess_person"] == "Sam Agent"
    E.record_attempt(conn, pid, "recorder_documents", "found", person="Dana Signer")
    assert E.property_drill(conn, pid)["best_guess_person"] == "Dana Signer"   # deed signer beats the agent
    E.record_attempt(conn, pid, "entity_unmask", "found", person="Morgan Member", person_role="member")
    assert E.property_drill(conn, pid)["best_guess_person"] == "Morgan Member"  # most recent in the top group
    dup = E.add_person(conn, pid, "morgan  member", role="officer")
    assert dup["created"] is False


def test_best_guess_respects_owner_type(conn):
    llc = add_prop(conn, owner_name="BLUE SKY")          # no LLC word, but the owner step knows it is one
    person = add_prop(conn, owner_name="BLUE SKY")
    set_owner(conn, llc, "small_llc")
    set_owner(conn, person, "individual")
    E.run_auto_stages(conn, MARKET)
    assert E.property_drill(conn, llc)["best_guess_person"] is None
    assert E.property_drill(conn, person)["best_guess_person"] == "Sky Blue"
    links = E.stage_queue(conn, MARKET, "county_auditor")["items"]
    llc_item = next(i for i in links if i["property_id"] == llc)
    assert llc_item["best_guess_person"] is None


def test_owner_of_record_from_auditor_beats_crexi_name(conn):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    E.record_attempt(conn, pid, "county_auditor", "found", person="ROE RICHARD", detail="Sold last month")
    assert E.property_drill(conn, pid)["best_guess_person"] == "Richard Roe"


# --- statuses, exhausted, reopen, actions ---------------------------------------------------------

def test_stage_statuses_and_skip(conn):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    E.record_attempt(conn, pid, "county_auditor", "nothing")
    E.record_attempt(conn, pid, "rental_registration", "skipped", detail="county has no registry")
    d = E.property_drill(conn, pid)
    st = stage_status(d)
    assert st["county_auditor"] == "done_nothing" and st["rental_registration"] == "skipped"
    assert list(st.values()).count("next") == 1
    nxt = next(k for k, v in st.items() if v == "next")
    assert d["next_step"]["stage_key"] == nxt
    # The latest check-off of a stage wins.
    E.record_attempt(conn, pid, "county_auditor", "found")
    assert stage_status(E.property_drill(conn, pid))["county_auditor"] == "done_found"
    attempts = next(s for s in E.property_drill(conn, pid)["stages"] if s["key"] == "county_auditor")["attempts"]
    assert [a["outcome"] for a in attempts] == ["found", "nothing"]


def test_checked_off_stage_stays_visible_when_owner_type_changes(conn):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    E.record_attempt(conn, pid, "entity_unmask", "nothing")         # unknown owner type: it applies
    set_owner(conn, pid, "individual")                              # now it does not
    st = stage_status(E.property_drill(conn, pid))
    assert st["entity_unmask"] == "done_nothing"
    assert "recorder_documents" not in st


def test_letter_reply_from_an_llc_confirms_the_person_not_the_company(conn):
    pid = add_prop(conn, owner_name="ACME APARTMENTS LLC", mailing_address="1 MAIN ST", mailing_city="DAYTON",
                   mailing_state="OH", mailing_zip="45402")
    E.run_auto_stages(conn, MARKET)
    E.add_person(conn, pid, "Pat Member", role="member")
    mail = one(conn, "SELECT id FROM contact_points WHERE property_id=? AND channel='mail'", (pid,))["id"]
    r = E.log_outreach(conn, pid, "mail", "reached_confirmed", contact_point_id=mail)
    assert r["drill"]["confirmed_person"] == "Pat Member"


def test_log_outreach_accepts_channel_aliases(conn):
    pid, (ph,) = _with_contacts(conn, ("phone", "937-555-0111"))
    r = E.log_outreach(conn, pid, "Call", "no_answer", contact_point_id=ph)
    assert r["drill"]["outreach"][0]["channel"] == "phone"


def test_exhausted_automatically_then_reopened_by_new_info(conn):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    d = walk_to(conn, pid, "never")
    assert d["next_step"]["kind"] == "exhausted" and d["status"] == "exhausted"
    assert one(conn, "SELECT status FROM drill_state WHERE property_id=?", (pid,))["status"] == "exhausted"
    assert E.overview(conn, MARKET)["statuses"]["exhausted"] == 1
    # Something new turns up: the owner is open again with a reach-out to do.
    r = E.add_contact(conn, pid, "phone", "937-555-0130", source="neighbor_network")
    assert r["status"] == "active" and r["next_step"]["kind"] == "validate"


def test_reimport_with_an_owner_name_reopens_an_exhausted_property(conn):
    pid = add_prop(conn, owner_name=None)
    E.run_auto_stages(conn, MARKET)
    E.level_action(conn, pid, "exhausted")
    conn.execute("UPDATE properties SET owner_name='DOE JOHN' WHERE id=?", (pid,))
    conn.commit()
    E.run_auto_stages(conn, MARKET)
    d = E.property_drill(conn, pid)
    assert d["status"] == "active" and d["level"] == 2


def test_reopen_starts_a_new_pass(conn):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    E.record_attempt(conn, pid, "county_auditor", "found")
    walk_to(conn, pid, "never")
    r = E.level_action(conn, pid, "reopen")
    assert "pass 2" in r["message"] and r["status"] == "active" and r["drill"]["pass"] == 2
    st = stage_status(r["drill"])
    assert st["county_auditor"] == "done_found"            # found stays done
    assert st["records_baseline"] == "done_found"
    assert r["next_step"]["kind"] == "stage" and r["next_step"]["stage_key"] == "portfolio_match"
    E.run_auto_stages(conn, MARKET)                          # the auto stage re-runs in the new pass
    assert stage_status(E.property_drill(conn, pid))["portfolio_match"] == "done_nothing"
    assert E.property_drill(conn, pid)["next_step"]["stage_key"] == "rental_registration"


def test_manual_exhausted_and_reopen(conn):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    r = E.level_action(conn, pid, "exhausted", note="Owner is in prison, no way to reach")
    assert r["status"] == "exhausted" and r["next_step"]["kind"] == "exhausted"
    assert "in prison" in r["drill"]["notes"]
    assert E.stage_queue(conn, MARKET, "county_auditor")["total"] == 0
    r = E.level_action(conn, pid, "reopen")
    assert r["status"] == "active" and r["next_step"]["stage_key"] == "county_auditor"
    assert "picks up where it left off" in r["message"]


def test_level_actions(conn):
    pid = add_prop(conn, owner_name=None)
    E.ensure_states(conn, MARKET)
    with pytest.raises(ValueError, match="owner's name"):
        E.level_action(conn, pid, "owner_found")
    r = E.level_action(conn, pid, "owner_found", person="Pat Lee")
    assert r["level"] == 2 and r["drill"]["best_guess_person"] == "Pat Lee"
    with pytest.raises(ValueError, match="Only a confirmed owner"):
        E.level_action(conn, pid, "handoff")
    r = E.level_action(conn, pid, "confirm")
    assert r["level"] == 3 and r["drill"]["confirmed_person"] == "Pat Lee"
    r = E.level_action(conn, pid, "handoff")
    assert r["drill"]["handed_off_at"]
    r = E.level_action(conn, pid, "reopen")                 # undo a mistaken confirmation
    assert r["level"] == 2 and r["status"] == "active" and r["drill"]["confirmed_person"] is None
    r = E.level_action(conn, pid, "dnc")
    assert r["status"] == "dnc"
    with pytest.raises(ValueError, match="undo do-not-contact"):
        E.level_action(conn, pid, "reopen")
    assert E.level_action(conn, pid, "undnc")["status"] in ("active", "exhausted")
    r = E.level_action(conn, pid, "note", note="Call after 5pm")
    assert "Call after 5pm" in r["drill"]["notes"]
    with pytest.raises(ValueError, match="not an action"):
        E.level_action(conn, pid, "explode")
    with pytest.raises(KeyError):
        E.level_action(conn, 999999, "note", note="x")


def test_confirm_with_a_new_person_records_them(conn):
    pid = add_prop(conn, owner_name="ACME LLC")
    E.run_auto_stages(conn, MARKET)
    r = E.level_action(conn, pid, "confirm", person="Chris Owner")
    assert r["drill"]["confirmed_person"] == "Chris Owner"
    assert ("Chris Owner", "confirmed_owner") in [(p["name"], p["role"]) for p in r["drill"]["people"]]


def test_bad_inputs(conn):
    pid = add_prop(conn)
    E.ensure_states(conn, MARKET)
    with pytest.raises(ValueError, match="Unknown DRILL stage"):
        E.record_attempt(conn, pid, "nope", "found")
    with pytest.raises(ValueError, match="not a result"):
        E.record_attempt(conn, pid, "county_auditor", "maybe")
    with pytest.raises(KeyError):
        E.record_attempt(conn, 999999, "county_auditor", "found")
    with pytest.raises(KeyError):
        E.property_drill(conn, 999999)
    with pytest.raises(ValueError, match="not an outcome"):
        E.log_outreach(conn, pid, "phone", "ghosted")
    with pytest.raises(ValueError, match="not a channel"):
        E.log_outreach(conn, pid, "carrier pigeon", "no_answer")
    other = add_prop(conn)
    E.ensure_states(conn, MARKET)
    cid = E.add_contact(conn, other, "phone", "9375550140")["contact"]["id"]
    with pytest.raises(ValueError, match="does not belong"):
        E.log_outreach(conn, pid, "phone", "no_answer", contact_point_id=cid)


def test_property_drill_without_state_row_creates_it(conn):
    pid = add_prop(conn)
    d = E.property_drill(conn, pid)
    assert d["level"] == 1 and d["status"] == "active"
    assert one(conn, "SELECT level FROM drill_state WHERE property_id=?", (pid,))["level"] == 1
    assert {"key", "name", "goal", "automation", "cost", "status", "attempts", "links"} <= set(d["stages"][0])
    assert {o["key"] for o in d["outcome_options"]} >= {"reached_confirmed", "do_not_contact"}


# --- portfolio match ------------------------------------------------------------------------------

def _mail(street="PO BOX 77"):
    return {"mailing_address": street, "mailing_city": "COLUMBUS", "mailing_state": "OH", "mailing_zip": "43215"}


def test_portfolio_match_carries_contacts_by_mailing_address(conn):
    a = add_prop(conn, owner_name="ALPHA ONE LLC", **_mail())
    b = add_prop(conn, owner_name="ALPHA TWO LLC", **_mail("P.O. Box 77"))
    c = add_prop(conn, owner_name="UNRELATED OWNER", **_mail("1 OTHER RD"))
    E.run_auto_stages(conn, MARKET)
    E.record_attempt(conn, a, "entity_unmask", "found", person="Alex Alpha", person_role="member",
                     contacts=[{"channel": "phone", "value": "614-555-0150"},
                               {"channel": "email", "value": "alex@alpha.com"}])
    res = E.run_auto_stages(conn, MARKET)
    assert res["stages"]["portfolio_match"]["found"] == 1
    db = E.property_drill(conn, b)
    carried = [x for x in db["contacts"] if x["source"] == "portfolio_match"]
    assert sorted(x["value"] for x in carried) == ["(614) 555-0150", "alex@alpha.com"]
    assert all(x["status"] == "untested" and "From " in x["notes"] for x in carried)
    assert ("Alex Alpha", "member") in [(p["name"], p["role"]) for p in db["people"]]
    assert db["best_guess_person"] == "Alex Alpha"
    assert stage_status(db)["portfolio_match"] == "done_found"
    assert db["next_step"]["kind"] == "validate"
    assert not [x for x in E.property_drill(conn, c)["contacts"] if x["source"] == "portfolio_match"]
    # Idempotent: nothing new, nothing recorded.
    n = scalar(conn, "SELECT COUNT(*) FROM drill_attempts")
    again = E.run_auto_stages(conn, MARKET)
    assert again["stages"]["portfolio_match"]["found"] == 0
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts") == n
    # Carried contacts are not carried on again (no chains), and the source's own mail is never carried.
    assert not [x for x in E.property_drill(conn, a)["contacts"] if x["source"] == "portfolio_match"]


def test_confirmation_carries_to_siblings_right_away(conn):
    a = add_prop(conn, owner_name="DOE JOHN", **_mail("9 ELM ST"))
    b = add_prop(conn, owner_name="DOE JOHN", **_mail("12 OAK ST"))      # same owner name
    E.run_auto_stages(conn, MARKET)
    cid = E.add_contact(conn, a, "phone", "937-555-0160", source="web_footprint")["contact"]["id"]
    E.log_outreach(conn, a, "phone", "reached_confirmed", contact_point_id=cid)
    db = E.property_drill(conn, b)
    assert ("John Doe", "portfolio_owner") in [(p["name"], p["role"]) for p in db["people"]]
    assert any(x["value"] == "(937) 555-0160" and x["status"] == "untested" for x in db["contacts"])
    assert db["level"] == 2 and db["status"] == "active"                 # still needs its own conversation
    assert db["next_step"]["kind"] == "validate"


def test_portfolio_key_from_owners_table(conn):
    a = add_prop(conn, owner_name="NORTH HOLDINGS LLC", **_mail("1 A ST"))
    b = add_prop(conn, owner_name="SOUTH HOLDINGS LLC", **_mail("2 B ST"))
    set_owner(conn, a, "portfolio", portfolio_key="grp-1")
    set_owner(conn, b, "portfolio", portfolio_key="grp-1")
    E.run_auto_stages(conn, MARKET)
    E.add_contact(conn, a, "email", "boss@north.com")
    E.run_auto_stages(conn, MARKET)
    assert any(x["value"] == "boss@north.com" for x in E.property_drill(conn, b)["contacts"])


def test_statutory_agent_carries_only_within_the_same_entity(conn):
    a = add_prop(conn, owner_name="GAMMA LLC", **_mail("5 C ST"))
    b = add_prop(conn, owner_name="GAMMA LLC", **_mail("6 D ST"))
    c = add_prop(conn, owner_name="DELTA LLC", **_mail("5 C ST"))
    E.run_auto_stages(conn, MARKET)
    E.add_person(conn, a, "Avery Agent", role="statutory_agent", source="entity_unmask")
    E.run_auto_stages(conn, MARKET)
    assert "Avery Agent" in [p["name"] for p in E.property_drill(conn, b)["people"]]
    assert "Avery Agent" not in [p["name"] for p in E.property_drill(conn, c)["people"]]


def test_big_shared_mailing_address_is_not_one_owner(conn, settings):
    settings.raw["owners"] = {"mailing_group_max_names": 2}
    ids = [add_prop(conn, owner_name=f"OWNER {n} LLC", **_mail("100 MANAGER WAY")) for n in range(3)]
    E.run_auto_stages(conn, MARKET, settings=settings)
    E.add_contact(conn, ids[0], "phone", "937-555-0170")
    res = E.run_auto_stages(conn, MARKET, settings=settings)
    assert res["stages"]["portfolio_match"]["found"] == 0


def test_dnc_and_confirmed_properties_get_no_carry(conn):
    a = add_prop(conn, owner_name="DOE JANE", **_mail("7 F ST"))
    b = add_prop(conn, owner_name="DOE JANE", **_mail("8 G ST"))
    E.run_auto_stages(conn, MARKET)
    E.level_action(conn, b, "dnc")
    E.add_contact(conn, a, "phone", "937-555-0180")
    E.run_auto_stages(conn, MARKET)
    assert not [x for x in E.property_drill(conn, b)["contacts"] if x["source"] == "portfolio_match"]


# --- queues and overview --------------------------------------------------------------------------

def _queue_setup(conn):
    p_hi = add_prop(conn, owner_name="HIGH JOHN")
    p_lo = add_prop(conn, owner_name="LOW JOHN")
    p_out = add_prop(conn, owner_name="OUT JOHN")
    p_none = add_prop(conn, owner_name="NONE JOHN")
    set_score(conn, p_hi, 0.05, 1, 1)
    set_score(conn, p_lo, 0.02, 1, 2)
    set_score(conn, p_out, 0.09, 0, None)
    E.run_auto_stages(conn, MARKET)
    return p_hi, p_lo, p_out, p_none


def test_stage_queue_order_pagination_and_items(conn):
    p_hi, p_lo, p_out, p_none = _queue_setup(conn)
    q = E.stage_queue(conn, MARKET, "county_auditor")
    assert q["total"] == 4
    assert [i["property_id"] for i in q["items"]] == [p_hi, p_lo, p_out, p_none]
    item = q["items"][0]
    assert item["owner_name"] == "HIGH JOHN" and item["best_guess_person"] == "John High"
    assert item["level"] == 2 and item["score"] == 0.05 and item["rank"] == 1 and item["in_buybox"] is True
    assert item["links"] and all(x["url"].startswith("http") for x in item["links"])
    assert {x["source_key"] for x in item["property_links"]} >= {"street_view", "crexi"}
    assert [c["channel"] for c in item["contacts"]] == []
    assert item["history"][0]["stage_key"] == "portfolio_match"
    assert q["stage"]["key"] == "county_auditor" and {o["key"] for o in q["stage"]["outcomes"]} == {
        "found", "nothing", "skipped"}
    page = E.stage_queue(conn, MARKET, "county_auditor", limit=2, offset=2)
    assert page["total"] == 4 and [i["property_id"] for i in page["items"]] == [p_out, p_none]
    # Checking one off moves it to the next queue.
    E.record_attempt(conn, p_hi, "county_auditor", "nothing")
    assert E.stage_queue(conn, MARKET, "county_auditor")["total"] == 3
    assert E.stage_queue(conn, MARKET, "rental_registration")["items"][0]["property_id"] == p_hi
    with pytest.raises(ValueError):
        E.stage_queue(conn, MARKET, "bogus")


def test_validate_queue(conn):
    p_hi, p_lo, _, _ = _queue_setup(conn)
    E.add_contact(conn, p_lo, "phone", "937-555-0190")
    E.add_contact(conn, p_hi, "email", "hi@example.com")
    E.add_contact(conn, p_hi, "mail", "1 MAIL ST, DAYTON, OH 45401")
    q = E.stage_queue(conn, MARKET, "validate")
    assert q["total"] == 2 and [i["property_id"] for i in q["items"]] == [p_hi, p_lo]
    assert [c["value"] for c in q["items"][0]["to_validate"]] == ["hi@example.com"]
    assert {o["key"] for o in q["stage"]["outcomes"]} >= {"no_answer", "reached_confirmed", "do_not_contact"}
    assert E.validate_queue(conn, MARKET, limit=1)["items"][0]["property_id"] == p_hi
    assert E.stage_queue(conn, MARKET, "county_auditor")["total"] == 2


def test_overview_board(conn):
    p_hi, p_lo, _, p_none = _queue_setup(conn)
    E.record_attempt(conn, p_hi, "county_auditor", "found")
    E.record_attempt(conn, p_lo, "county_auditor", "nothing")
    E.level_action(conn, p_hi, "confirm", person="John High")
    E.level_action(conn, p_none, "dnc")
    ov = E.overview(conn, MARKET)
    assert ov["levels"] == {1: 0, 2: 3, 3: 1}
    assert ov["statuses"]["confirmed"] == 1 and ov["statuses"]["dnc"] == 1 and ov["statuses"]["active"] == 2
    # Penetration counts buy-box owners only (scores exist): 1 confirmed of 2 open/confirmed in the box.
    assert ov["penetration"] == 0.5
    assert ov["penetration_detail"] == {"confirmed": 1, "of": 2, "basis": "in_buybox",
                                        "text": "1 of 2 buy-box owners confirmed"}
    pipe = {r["stage_key"]: r for r in ov["pipeline"]}
    assert [r["stage_key"] for r in ov["pipeline"]][:3] == ["records_baseline", "portfolio_match", "county_auditor"]
    ca = pipe["county_auditor"]
    assert ca["input"] == 3 and ca["resolved"] == 1 and ca["unresolved"] == 2 and ca["tried"] == 2
    assert ca["hit_rate_actual"] == 0.5 and ca["hit_rate_hypothesis"] == 0.85 and ca["queue"] == 1
    rb = pipe["records_baseline"]
    assert rb["input"] == 4 and rb["resolved"] == 4 and rb["hit_rate_actual"] == 1.0
    assert pipe["paid_skip_trace"]["enabled"] is False
    assert ov["credits"]["per_month"] == 500 and ov["credits"]["left"] == 500
    queues = {q["key"]: q["count"] for q in ov["queues"]}
    assert queues["county_auditor"] == 1 and queues["rental_registration"] == 1 and queues["validate"] == 0
    assert ov["totals"]["properties"] == 4 and ov["totals"]["handoff_waiting"] == 1
    assert "penetration" in ov["message"]


def test_overview_without_scores_uses_every_owner(conn):
    a = add_prop(conn, owner_name="DOE JOHN")
    add_prop(conn, owner_name="ROE JANE")
    E.run_auto_stages(conn, MARKET)
    E.level_action(conn, a, "confirm")
    ov = E.overview(conn, MARKET)
    assert ov["penetration"] == 0.5 and ov["penetration_detail"]["basis"] == "all"


def test_overview_empty_market(conn):
    ov = E.overview(conn, "self-storage-springfield-oh")
    assert ov["levels"] == {1: 0, 2: 0, 3: 0} and ov["penetration"] == 0.0
    assert "Import a Crexi export" in ov["message"]


def test_summaries_for_lists(conn):
    a = add_prop(conn, owner_name="DOE JOHN")
    b = add_prop(conn)
    E.run_auto_stages(conn, MARKET)
    E.add_contact(conn, a, "phone", "937-555-0195")
    s = E.summaries(conn, MARKET)
    assert set(s) >= {a, b}
    assert s[a]["level"] == 2 and s[a]["channels"]["phone"] == "untested" and s[a]["next_step"]["kind"] == "validate"
    assert s[b]["level"] == 1 and s[b]["next_step_text"].startswith("County auditor")
    assert set(E.summaries(conn, property_ids=[a])) == {a}


def test_refresh_states_auto_exhausts(conn, settings):
    pid = add_prop(conn, owner_name="DOE JOHN")
    E.run_auto_stages(conn, MARKET)
    for st in settings.drill["stages"]:
        if st["key"] not in ("records_baseline", "portfolio_match"):
            st["enabled"] = False
    res = E.refresh_states(conn, MARKET, settings=settings)
    assert res["changed"] >= 1
    assert one(conn, "SELECT status FROM drill_state WHERE property_id=?", (pid,))["status"] == "exhausted"


# --- stats ----------------------------------------------------------------------------------------

def test_stage_hit_rates_and_advice(conn, settings):
    settings.drill["advice_min_tries"] = 1
    a = add_prop(conn, owner_name="ONE LLC")
    b = add_prop(conn, owner_name="DOE JOHN")
    c = add_prop(conn, owner_name="TWO LLC")
    set_owner(conn, a, "small_llc")
    set_owner(conn, b, "individual")
    set_owner(conn, c, "small_llc")
    E.run_auto_stages(conn, MARKET)
    E.record_attempt(conn, a, "county_auditor", "found")
    E.record_attempt(conn, c, "county_auditor", "nothing")
    E.record_attempt(conn, b, "county_auditor", "nothing")
    E.record_attempt(conn, b, "county_auditor", "found")      # latest counts
    E.record_attempt(conn, a, "rental_registration", "skipped")
    rates = S.stage_hit_rates(conn, MARKET, settings=settings)
    ca = {r["owner_type"]: r for r in rates if r["stage_key"] == "county_auditor"}
    assert ca["all"]["tried"] == 3 and ca["all"]["found"] == 2 and ca["all"]["rate"] == pytest.approx(0.6667, 1e-3)
    assert ca["small_llc"]["tried"] == 2 and ca["small_llc"]["rate"] == 0.5
    assert ca["individual"]["rate"] == 1.0
    assert ca["all"]["hypothesis"] == 0.85
    assert ca["all"]["blended"] == pytest.approx((2 + 10 * 0.85) / (3 + 10), abs=1e-4)
    rr = {r["owner_type"]: r for r in rates if r["stage_key"] == "rental_registration"}
    assert rr["all"]["skipped"] == 1 and rr["all"]["tried"] == 0 and rr["all"]["rate"] is None
    # every stage has an 'all' row, even untouched ones
    assert {r["stage_key"] for r in rates if r["owner_type"] == "all"} == {s.key for s in E.drill_config().stages}
    summary = {r["stage_key"]: r for r in S.stage_summary(conn, MARKET, settings=settings)}
    assert summary["county_auditor"]["best_for"] == "individual"
    assert "Individuals" in summary["county_auditor"]["best_for_text"]
    tips = S.stage_advice(conn, MARKET, settings=settings)
    assert any(t["owner_type"] == "small_llc" and "worked best" in t["text"] for t in tips)
    assert S.blend(0, 0, None, 10) is None and S.blend(1, 2, None, 10) == 0.5


def test_outreach_rates_and_report(conn):
    pid, (ph, em) = _with_contacts(conn, ("phone", "937-555-0201"), ("email", "z@z.com"))
    E.log_outreach(conn, pid, "phone", "no_answer", contact_point_id=ph)
    E.log_outreach(conn, pid, "email", "bounced", contact_point_id=em)
    E.log_outreach(conn, pid, "phone", "reached_confirmed", contact_point_id=ph)
    rates = {r["channel"]: r for r in S.outreach_rates(conn, MARKET)}
    assert rates["phone"]["logged"] == 2 and rates["phone"]["confirmed"] == 1 and rates["phone"]["confirm_rate"] == 0.5
    assert rates["email"]["wrong"] == 1
    rep = S.drill_report(conn, MARKET)
    assert set(rep) == {"stages", "by_owner_type", "advice", "outreach"}


def test_crm_handoff_skips_dnc_and_open_owners(conn):
    a = add_prop(conn, owner_name="DOE JOHN")
    b = add_prop(conn, owner_name="ROE JANE")
    c = add_prop(conn, owner_name="POE EDGAR")
    E.run_auto_stages(conn, MARKET)
    E.level_action(conn, a, "confirm", person="John Doe")
    E.level_action(conn, b, "confirm", person="Jane Roe")
    E.level_action(conn, b, "dnc")
    rows_ = S.crm_handoff_rows(conn, MARKET)
    assert [r["property_id"] for r in rows_] == [a]
    r = rows_[0]
    assert r["owner_name"] == "DOE JOHN" and r["address"] and "John Doe (confirmed owner)" in r["people"]
    assert c not in [x["property_id"] for x in rows_]


# --- real data (local only) -----------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("BLUEMOON_REAL_CREXI"), reason="BLUEMOON_REAL_CREXI not set")
def test_real_export_runs_through_the_drill(conn, real_crexi):
    try:
        from bluemoon.ingest.crexi import import_file
        imp = import_file(conn, MARKET, real_crexi)
    except Exception as e:  # noqa: BLE001 - the importer is a separate module; this test is about the drill
        pytest.skip(f"importer not usable here: {e}")
    assert imp["rows_total"] > 0
    t0 = time.time()
    res = E.run_auto_stages(conn, MARKET)
    t_auto = time.time() - t0
    assert res["properties"] > 0
    t0 = time.time()
    ov = E.overview(conn, MARKET)
    q = E.stage_queue(conn, MARKET, "county_auditor", limit=25)
    t_read = time.time() - t0
    assert sum(ov["levels"].values()) == res["properties"]
    assert ov["levels"][3] == 0                    # records never confirm an owner
    assert q["total"] >= len(q["items"])
    assert E.run_auto_stages(conn, MARKET)["stages"]["records_baseline"]["found"] == 0   # idempotent
    assert t_auto < 120 and t_read < 30
