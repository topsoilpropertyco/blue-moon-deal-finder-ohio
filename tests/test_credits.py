"""Tests for Crexi credits: status, the ledger, and the monthly spend-them-all plan (bluemoon/drill/credits.py)."""
from __future__ import annotations

import itertools
from datetime import date

import pytest

from bluemoon.db import market_by_slug, now_iso, one, rows, scalar, this_month
from bluemoon.drill import credits as C
from bluemoon.drill import engine as E

MARKET = "apartments-springfield-oh"
TODAY = date(2026, 10, 21)          # 11 days left in October, counting today
_ids = itertools.count(1)


def add_prop(conn, market=MARKET, **kw) -> int:
    n = next(_ids)
    m = market_by_slug(conn, market)
    data = {"market_id": m["id"], "property_key": f"credit:{n}", "address": f"{n} CREDIT AVE", "city": "SPRINGFIELD",
            "state": "OH", "zip": "45503", "county": "CLARK", "apn": f"C-{n}", "owner_name": f"OWNER{n} JOHN",
            "crexi_url": f"https://www.crexi.com/property-records/c-{n}", "raw_json": "{}", "flags_json": "[]",
            "created_at": now_iso(), "updated_at": now_iso()}
    data.update(kw)
    cur = conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                       tuple(data.values()))
    conn.commit()
    return int(cur.lastrowid)


def set_score(conn, pid, score, in_buybox=1, rank=None):
    conn.execute("INSERT OR REPLACE INTO scores(property_id, in_buybox, exclusion_reasons, score, rank, computed_at) "
                 "VALUES(?,?,?,?,?,?)", (pid, in_buybox, "[]", score, rank, now_iso()))
    conn.commit()


def try_free_reach(conn, pid):
    """Check off every free reach stage before the credit (nothing found)."""
    cfg = E.drill_config()
    for st in cfg.stages:
        if st.key == E.CREDIT_STAGE:
            break
        if st.goal == "reach" and st.cost == "free" and st.enabled:
            ctx = E.load_contexts(conn, property_ids=[pid])[0]
            if st.applies(ctx.owner_type, ctx.asset_class, ctx.prop.get("state")):
                E.record_attempt(conn, pid, st.key, "nothing")


def ledger(conn):
    return rows(conn, "SELECT property_id, month, result FROM credit_ledger ORDER BY id")


# --- status ---------------------------------------------------------------------------------------

def test_days_left_in_month():
    assert C.days_left_in_month(date(2026, 10, 1)) == 31
    assert C.days_left_in_month(date(2026, 10, 31)) == 1
    assert C.days_left_in_month(date(2028, 2, 1)) == 29


def test_credit_status_counts_this_month_only(conn):
    pid = add_prop(conn)
    for month, result in [("2026-10", "found"), ("2026-10", "nothing"), ("2026-10", None), ("2026-09", "found")]:
        conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at) VALUES(?,?,?,?)",
                     (pid, month, result, now_iso()))
    conn.commit()
    s = C.credit_status(conn, today=TODAY)
    assert s["month"] == "2026-10" and s["per_month"] == 500
    assert s["used"] == 3 and s["left"] == 497
    assert (s["found"], s["nothing"], s["pending"]) == (1, 1, 1)
    assert s["hit_rate"] == 0.5
    assert s["days_left"] == 11 and s["daily_pace"] == 46          # ceil(497 / 11)
    assert "497 of 500" in s["message"] and "do not roll over" in s["message"]


def test_credit_status_uses_config_and_handles_used_up(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 2
    pid = add_prop(conn)
    for _ in range(3):
        conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at) VALUES(?,?,?,?)",
                     (pid, "2026-10", "found", now_iso()))
    conn.commit()
    s = C.credit_status(conn, today=TODAY, settings=settings)
    assert s["left"] == 0 and s["daily_pace"] == 0 and "used" in s["message"]
    settings.raw["crexi"]["credits_per_month"] = "lots"
    assert C.credits_per_month(settings) == C.DEFAULT_CREDITS_PER_MONTH


# --- using a credit -------------------------------------------------------------------------------

def test_use_credit_then_record_the_result(conn):
    pid = add_prop(conn)
    E.run_auto_stages(conn, MARKET)
    r = C.use_credit(conn, pid, today=TODAY)
    assert r["ok"] and r["result"] is None and "record what Crexi showed" in r["message"]
    assert ledger(conn) == [{"property_id": pid, "month": "2026-10", "result": None}]
    assert r["credits"]["used"] == 1 and r["credits"]["pending"] == 1
    again = C.use_credit(conn, pid, today=TODAY)                      # no double spend
    assert "already open" in again["message"] and len(ledger(conn)) == 1
    r = C.use_credit(conn, pid, "found", today=TODAY,
                     contacts=[{"channel": "phone", "value": "937-555-0300"},
                               {"channel": "email", "value": "owner@example.com"}])
    assert ledger(conn) == [{"property_id": pid, "month": "2026-10", "result": "found"}]
    st = {s["key"]: s["status"] for s in r["drill"]["stages"]}
    assert st["crexi_credit"] == "done_found"
    assert {c["value"] for c in r["drill"]["contacts"] if c["source"] == "crexi_credit"} == {
        "(937) 555-0300", "owner@example.com"}
    assert r["next_step"]["kind"] == "validate"
    assert r["drill"]["credit_used"] is True
    assert r["credits"]["found"] == 1 and r["credits"]["left"] == 499


def test_use_credit_with_result_in_one_step(conn):
    pid = add_prop(conn)
    E.run_auto_stages(conn, MARKET)
    r = C.use_credit(conn, pid, "nothing", today=TODAY)
    assert ledger(conn) == [{"property_id": pid, "month": "2026-10", "result": "nothing"}]
    assert {s["key"]: s["status"] for s in r["drill"]["stages"]}["crexi_credit"] == "done_nothing"
    att = rows(conn, "SELECT outcome, detail FROM drill_attempts WHERE property_id=? AND stage_key='crexi_credit'",
               (pid,))
    assert att == [{"outcome": "nothing", "detail": "Used a Crexi owner-contact credit."}]


def test_checking_off_the_credit_stage_keeps_the_ledger_in_step(conn):
    pid = add_prop(conn)
    E.run_auto_stages(conn, MARKET)
    E.record_attempt(conn, pid, "crexi_credit", "found")
    assert ledger(conn) == [{"property_id": pid, "month": this_month(), "result": "found"}]
    E.record_attempt(conn, pid, "crexi_credit", "skipped")             # skipping spends nothing
    assert len(ledger(conn)) == 1
    assert C.credit_status(conn)["used"] == 1


def test_use_credit_validates_input_before_writing(conn):
    pid = add_prop(conn)
    E.ensure_states(conn, MARKET)
    with pytest.raises(ValueError, match="not a credit result"):
        C.use_credit(conn, pid, "maybe")
    with pytest.raises(ValueError, match="10-digit"):
        C.use_credit(conn, pid, "found", contacts=[{"channel": "phone", "value": "123"}])
    with pytest.raises(KeyError):
        C.use_credit(conn, 999999, "found")
    assert ledger(conn) == []


def test_use_credit_when_used_up_still_records_with_a_warning(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 0
    pid = add_prop(conn)
    E.ensure_states(conn, MARKET)
    r = C.use_credit(conn, pid, "found", today=TODAY, settings=settings)
    assert "look used up" in r["message"] and len(ledger(conn)) == 1


def test_credit_log(conn):
    a, b = add_prop(conn), add_prop(conn, market="self-storage-springfield-oh")
    E.ensure_states(conn, MARKET)
    E.ensure_states(conn, "self-storage-springfield-oh")
    C.use_credit(conn, a, "found", today=TODAY)
    C.use_credit(conn, b, "nothing", today=TODAY)
    log = C.credit_log(conn)
    assert [x["property_id"] for x in log] == [b, a] and log[1]["address"]
    assert [x["property_id"] for x in C.credit_log(conn, MARKET)] == [a]
    assert C.credit_status(conn, today=TODAY)["used"] == 2          # credits are account-wide


# --- the plan -------------------------------------------------------------------------------------

def _plan_setup(conn):
    """Six owners in the buy box plus a few that must never be planned."""
    p = {k: add_prop(conn) for k in ("tried_lo", "tried_hi", "fresh_top", "fresh_mid", "no_score_tried", "unscored")}
    set_score(conn, p["tried_lo"], 0.02, 1, 5)
    set_score(conn, p["tried_hi"], 0.04, 1, 3)
    set_score(conn, p["fresh_top"], 0.09, 1, 1)
    set_score(conn, p["fresh_mid"], 0.05, 1, 2)
    set_score(conn, p["no_score_tried"], None, 1, None)
    p["outside"] = add_prop(conn)
    set_score(conn, p["outside"], 0.20, 0, None)                    # out of the buy box
    p["level1"] = add_prop(conn, owner_name=None)
    set_score(conn, p["level1"], 0.30, 1, None)
    p["verified"] = add_prop(conn)
    set_score(conn, p["verified"], 0.08, 1, None)
    p["dnc"] = add_prop(conn)
    set_score(conn, p["dnc"], 0.07, 1, None)
    p["credited"] = add_prop(conn)
    set_score(conn, p["credited"], 0.06, 1, None)
    p["confirmed"] = add_prop(conn)
    set_score(conn, p["confirmed"], 0.10, 1, None)
    E.run_auto_stages(conn, MARKET)
    for k in ("tried_lo", "tried_hi", "no_score_tried"):
        try_free_reach(conn, p[k])
    cid = E.add_contact(conn, p["verified"], "phone", "937-555-0310")["contact"]["id"]
    E.set_contact_status(conn, cid, "verified")
    E.level_action(conn, p["dnc"], "dnc")
    C.use_credit(conn, p["credited"], "nothing", today=TODAY)
    E.level_action(conn, p["confirmed"], "confirm", person="Done Deal")
    return p


def test_credit_plan_order_and_filters(conn):
    p = _plan_setup(conn)
    plan = C.credit_plan(conn, MARKET, today=TODAY)
    order = [x["property_id"] for x in plan]
    # free reach stages already tried first (by score), then the rest by score; unscored last in each group
    assert order == [p["tried_hi"], p["tried_lo"], p["no_score_tried"], p["fresh_top"], p["fresh_mid"]]
    for bad in ("outside", "level1", "verified", "dnc", "credited", "confirmed"):
        assert p[bad] not in order
    first = plan[0]
    assert first["order"] == 1 and first["all_free_tried"] is True and first["free_not_tried"] == []
    assert "Rank #3" in first["reason"] and "gave no working phone or email" in first["reason"]
    fresh = next(x for x in plan if x["property_id"] == p["fresh_top"])
    assert fresh["all_free_tried"] is False and "Try those first" in fresh["reason"]
    assert first["links"] and first["links"][0]["url"].startswith("https://www.crexi.com/")
    assert first["level"] == 2 and first["owner_name"] and first["best_guess_person"]


def test_unscored_property_in_a_scored_market_is_left_out(conn):
    p = _plan_setup(conn)
    assert p["unscored"] not in [x["property_id"] for x in C.credit_plan(conn, MARKET, today=TODAY)]


def test_credit_plan_never_exceeds_credits_left_and_paces_by_day(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 4                 # 1 already used by _plan_setup -> 3 left
    _plan_setup(conn)
    plan = C.credit_plan(conn, MARKET, limit=25, today=date(2026, 10, 30), settings=settings)
    assert len(plan) == 3
    # 3 left over 2 days (30th and 31st): 2 a day
    assert [x["when"] for x in plan] == ["today", "today", "tomorrow"]
    assert plan[2]["use_on"] == "2026-10-31"
    assert len(C.credit_plan(conn, MARKET, limit=2, today=TODAY, settings=settings)) == 2
    assert C.credit_plan(conn, MARKET, limit=0, today=TODAY, settings=settings) == []


def test_credit_plan_days_never_pass_month_end(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 50
    for _ in range(5):
        set_score(conn, add_prop(conn), 0.01, 1, None)
    E.run_auto_stages(conn, MARKET)
    plan = C.credit_plan(conn, MARKET, today=date(2026, 10, 31), settings=settings)
    assert {x["use_on"] for x in plan} == {"2026-10-31"}


def test_credit_plan_without_scores_includes_every_possible_owner(conn):
    a = add_prop(conn)
    add_prop(conn, owner_name=None)                                  # level 1: never planned
    E.run_auto_stages(conn, MARKET)
    plan = C.credit_plan(conn, MARKET, today=TODAY)
    assert [x["property_id"] for x in plan] == [a]
    assert plan[0]["reason"].startswith("Not scored yet.")


def test_credit_plan_empty_when_no_credits_left(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 0
    add_prop(conn)
    E.run_auto_stages(conn, MARKET)
    assert C.credit_plan(conn, MARKET, today=TODAY, settings=settings) == []


def test_plan_summary_reports_shortfall(conn):
    add_prop(conn)
    E.run_auto_stages(conn, MARKET)
    s = C.plan_summary(conn, MARKET, today=TODAY)
    assert s["planned"] == 1 and s["shortfall"] == 499
    assert "have no target yet" in s["message"]


def test_overview_includes_credit_status(conn):
    pid = add_prop(conn)
    E.run_auto_stages(conn, MARKET)
    C.use_credit(conn, pid, "found")
    ov = E.overview(conn, MARKET)
    assert ov["credits"]["used"] == 1 and ov["credits"]["left"] == 499
    assert scalar(conn, "SELECT COUNT(*) FROM credit_ledger") == 1
    assert one(conn, "SELECT result FROM credit_ledger")["result"] == "found"
