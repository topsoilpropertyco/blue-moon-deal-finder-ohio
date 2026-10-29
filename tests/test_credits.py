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


@pytest.fixture(autouse=True)
def calendar_months(settings):
    """These tests pin the credit counting itself with calendar-month periods (reset_day 1). Periods that reset on
    another day (the real config uses 29) are tested in tests/test_crexi_batches.py."""
    settings.raw["crexi"]["reset_day"] = 1


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
        conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at, period) VALUES(?,?,?,?,?)",
                     (pid, month, result, now_iso(), month + "-01"))     # (calendar-month periods: reset_day 1)
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
    assert s["planned"] == 1 and s["planned_all"] == 1 and s["shortfall"] == 499
    assert "1 possible owner in the buy box qualifies" in s["message"] and "499 credits have no target yet" in s["message"]
    assert "free stages" not in s["message"]          # working the free stages alone can't close the gap


def test_overview_includes_credit_status(conn):
    pid = add_prop(conn)
    E.run_auto_stages(conn, MARKET)
    C.use_credit(conn, pid, "found")
    ov = E.overview(conn, MARKET)
    assert ov["credits"]["used"] == 1 and ov["credits"]["left"] == 499
    assert scalar(conn, "SELECT COUNT(*) FROM credit_ledger") == 1
    assert one(conn, "SELECT result FROM credit_ledger")["result"] == "found"


# --- review fixes -----------------------------------------------------------------------------------

STORAGE = "self-storage-springfield-oh"


def test_owner_with_untested_phone_waits_in_reach_out_not_the_plan(conn):
    a, b = add_prop(conn), add_prop(conn)
    set_score(conn, a, 0.09, 1, 1)
    set_score(conn, b, 0.05, 1, 2)
    E.run_auto_stages(conn, MARKET)
    E.record_attempt(conn, a, "county_auditor", "found", contacts=[{"channel": "phone", "value": "937-555-0101"}])
    assert [x["property_id"] for x in C.credit_plan(conn, MARKET, today=TODAY)] == [b]
    cid = one(conn, "SELECT id FROM contact_points WHERE property_id=? AND channel='phone'", (a,))["id"]
    E.set_contact_status(conn, cid, "wrong")
    assert [x["property_id"] for x in C.credit_plan(conn, MARKET, today=TODAY)] == [a, b]


def test_one_plan_for_every_market_never_plans_more_than_the_credits(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 4
    springfield = [add_prop(conn) for _ in range(3)]
    storage = [add_prop(conn, market=STORAGE) for _ in range(3)]
    for i, pid in enumerate(springfield):
        set_score(conn, pid, 0.09 - i * 0.02, 1, i + 1)
    for i, pid in enumerate(storage):
        set_score(conn, pid, 0.08 - i * 0.02, 1, i + 1)
    E.run_auto_stages(conn, MARKET)
    E.run_auto_stages(conn, STORAGE)
    today = date(2026, 10, 30)                                   # 4 credits over 2 days: 2 a day
    a = C.credit_plan(conn, MARKET, today=today, settings=settings)
    b = C.credit_plan(conn, STORAGE, today=today, settings=settings)
    assert len(a) + len(b) == 4
    orders = [x["order"] for x in a + b]
    assert sorted(orders) == [1, 2, 3, 4]
    assert sum(1 for x in a + b if x["when"] == "today") == 2
    assert [x["property_id"] for x in sorted(a + b, key=lambda x: x["order"])][:2] == [springfield[0], storage[0]]
    for slug in (MARKET, STORAGE):
        info = C.plan_summary(conn, slug, today=today, settings=settings)
        assert info["shortfall"] == 0 and info["planned_all"] == 4
        assert info["other_markets"] == 4 - info["planned"]
        assert "planned credits are for owners in other markets" in info["message"]


def test_credit_hit_rates_by_owner_type_order_the_plan(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 500
    history = []
    for i in range(15):                                          # trusts: credits never found anyone
        pid = add_prop(conn, owner_name=f"OLD{i} FAMILY TRUST")
        conn.execute("INSERT INTO owners(property_id, owner_type, computed_at) VALUES(?,?,?)",
                     (pid, "trust", now_iso()))
        history.append((pid, "nothing"))
    for i in range(15):                                          # small LLCs: always found someone
        pid = add_prop(conn, owner_name=f"TEST{i} HOLDINGS LLC")
        conn.execute("INSERT INTO owners(property_id, owner_type, computed_at) VALUES(?,?,?)",
                     (pid, "small_llc", now_iso()))
        history.append((pid, "found"))
    trust = add_prop(conn, owner_name="NEW FAMILY TRUST")
    llc = add_prop(conn, owner_name="NEW TEST HOLDINGS LLC")
    conn.execute("INSERT INTO owners(property_id, owner_type, computed_at) VALUES(?,?,?)", (trust, "trust", now_iso()))
    conn.execute("INSERT INTO owners(property_id, owner_type, computed_at) VALUES(?,?,?)", (llc, "small_llc", now_iso()))
    conn.commit()
    set_score(conn, trust, 0.81, 1, 1)
    set_score(conn, llc, 0.80, 1, 2)
    E.run_auto_stages(conn, MARKET)
    for pid, result in history:
        C.use_credit(conn, pid, result, today=TODAY)
    for pid in (trust, llc):
        try_free_reach(conn, pid)
    plan = [x for x in C.credit_plan(conn, MARKET, today=TODAY, settings=settings) if x["property_id"] in (trust, llc)]
    assert [x["property_id"] for x in plan] == [llc, trust]
    assert "Crexi has turned up an owner name or contact for" in plan[0]["reason"]
    settings.drill["learn_order"] = False                        # off: the score alone decides
    plan = [x for x in C.credit_plan(conn, MARKET, today=TODAY, settings=settings) if x["property_id"] in (trust, llc)]
    assert [x["property_id"] for x in plan] == [trust, llc]


def _fallback_setup(conn):
    p = {"main": add_prop(conn), "level1": add_prop(conn, owner_name=None),
         "aside_one": add_prop(conn), "aside_two": add_prop(conn), "aside_hi_two": add_prop(conn),
         "aside_state": add_prop(conn), "aside_credited": add_prop(conn)}
    set_score(conn, p["main"], 0.05, 1, 1)
    set_score(conn, p["level1"], 0.04, 1, 2)
    for key, score, reasons in (("aside_one", 0.03, '["drive_time"]'), ("aside_two", 0.02, '["drive_time", "year"]'),
                                ("aside_hi_two", 0.09, '["size", "year"]'), ("aside_state", 0.20, '["state"]'),
                                ("aside_credited", 0.10, '["year"]')):
        conn.execute("INSERT OR REPLACE INTO scores(property_id, in_buybox, exclusion_reasons, score, rank, computed_at) "
                     "VALUES(?,?,?,?,?,?)", (p[key], 0, reasons, score, None, now_iso()))
    conn.commit()
    E.run_auto_stages(conn, MARKET)
    C.use_credit(conn, p["aside_credited"], "nothing", today=TODAY)
    return p


def test_fallbacks_use_credits_that_would_otherwise_expire(conn):
    p = _fallback_setup(conn)
    info = C.market_plan(conn, MARKET, limit=50, today=TODAY)
    assert [x["property_id"] for x in info["plan"]] == [p["main"]]
    extras = info["extras"]
    assert [x["property_id"] for x in extras] == [p["level1"], p["aside_one"], p["aside_hi_two"], p["aside_two"]]
    assert [x["tier"] for x in extras] == ["level1_buybox", "set_aside", "set_aside", "set_aside"]
    assert "No owner name in the export" in extras[0]["reason"]
    assert "Only because credits would otherwise expire" in extras[1]["reason"]
    assert [x["order"] for x in extras] == [2, 3, 4, 5]
    assert info["extra_count"] == 4 and info["shortfall"] == 499 - 5
    assert "so none expire" in info["message"]
    assert C.credit_extras(conn, MARKET, 1, today=TODAY)[0]["property_id"] == p["level1"]


def test_fallbacks_are_empty_when_the_main_plan_covers_every_credit(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 2                # 1 used by the setup -> 1 left
    p = _fallback_setup(conn)
    info = C.market_plan(conn, MARKET, today=TODAY, settings=settings)
    assert [x["property_id"] for x in info["plan"]] == [p["main"]] and info["extras"] == []


def test_fallbacks_can_be_turned_off(conn, settings):
    settings.raw["crexi"]["plan_fallbacks"] = []
    _fallback_setup(conn)
    info = C.market_plan(conn, MARKET, today=TODAY, settings=settings)
    assert info["extras"] == [] and "plan_fallbacks" in info["message"]


def test_used_today_follows_the_local_day(conn, monkeypatch):
    import time
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        pid = add_prop(conn)
        conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at) VALUES(?,?,?,?)",
                     (pid, "2026-10", "found", "2026-10-22T01:30:00+00:00"))   # 9:30 pm on the 21st in Ohio
        conn.commit()
        assert C.credit_status(conn, today=date(2026, 10, 21))["used_today"] == 1
        assert C.credit_status(conn, today=date(2026, 10, 22))["used_today"] == 0
    finally:
        monkeypatch.undo()
        time.tzset()


# --- a steady daily pace ----------------------------------------------------------------------------

def _ledger_today(conn, pid, n, month="2026-10", when="2026-10-21T12:00:00+00:00"):
    for _ in range(n):
        conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at, period) VALUES(?,?,?,?,?)",
                     (pid, month, "nothing", when, month + "-01"))        # every new row carries its credit period
    conn.commit()


def test_daily_pace_stays_the_same_while_credits_are_used_today(conn):
    """The pace is set by the credits left when the day began: using credits today doesn't lower it, and the
    plan gives today only the credits still to use today."""
    owners = [add_prop(conn) for _ in range(40)]
    E.run_auto_stages(conn, MARKET)
    before = C.credit_status(conn, today=TODAY)
    assert (before["daily_pace"], before["today_left"]) == (46, 46)            # ceil(500 / 11)
    _ledger_today(conn, add_prop(conn), 10)
    s = C.credit_status(conn, today=TODAY)
    assert (s["left"], s["used_today"], s["daily_pace"], s["today_left"]) == (490, 10, 46, 36)
    assert "use about 46 a day (36 more today)" in s["message"]
    plan = C.market_plan(conn, MARKET, limit=100, today=TODAY)["plan"]
    assert [x["property_id"] for x in plan] and {x["property_id"] for x in plan} <= set(owners)
    assert [x["when"] for x in plan] == ["today"] * 36 + ["tomorrow"] * 4
    _ledger_today(conn, add_prop(conn), 40)                                     # 50 used today: past the pace
    s = C.credit_status(conn, today=TODAY)
    assert (s["daily_pace"], s["today_left"]) == (46, 0)
    plan = C.market_plan(conn, MARKET, limit=100, today=TODAY)["plan"]
    assert plan[0]["when"] == "tomorrow" and plan[0]["use_on"] == "2026-10-22"
    # credits used in another month never count toward today
    _ledger_today(conn, add_prop(conn), 5, month="2026-09")
    assert C.credit_status(conn, today=TODAY)["used_today"] == 50


def test_the_last_day_gets_every_credit_left(conn, settings):
    settings.raw["crexi"]["credits_per_month"] = 10
    owners = [add_prop(conn) for _ in range(8)]
    E.run_auto_stages(conn, MARKET)
    _ledger_today(conn, owners[0], 2, when="2026-10-31T12:00:00+00:00")
    s = C.credit_status(conn, today=date(2026, 10, 31), settings=settings)
    assert (s["left"], s["daily_pace"], s["today_left"]) == (8, 10, 8)
    plan = C.credit_plan(conn, MARKET, today=date(2026, 10, 31), settings=settings)
    assert len(plan) == 7 and {x["when"] for x in plan} == {"today"}


# --- one account, every market ---------------------------------------------------------------------

def test_market_plan_reports_the_whole_account(conn, settings):
    """planned is this market's; planned_all, shortfall and level1_open count every market; others lists the
    other markets with owners in the plan."""
    here = [add_prop(conn) for _ in range(2)]
    there = [add_prop(conn, market=STORAGE) for _ in range(3)]
    nameless = add_prop(conn, owner_name=None)                    # level 1 in the buy box
    for pid in (*here, nameless):
        set_score(conn, pid, 0.05, 1, None)
    E.run_auto_stages(conn, MARKET)
    E.run_auto_stages(conn, STORAGE)
    info = C.market_plan(conn, MARKET, today=TODAY, settings=settings)
    assert (info["planned"], info["planned_all"], info["extra_count"], info["extras_all"]) == (2, 5, 1, 1)
    storage_name = market_by_slug(conn, STORAGE)["name"]
    assert info["others"] == [{"slug": STORAGE, "name": storage_name, "planned": 3, "extras": 0}]
    assert info["shortfall"] == 500 - 6 and info["other_markets"] == 3
    assert info["level1_open"] == 0            # the nameless owner already has a (fallback) credit planned
    assert "shared by every market" in info["message"]
    assert "5 possible owners in the buy box qualify across all markets (2 in this market)." in info["message"]
    assert "494 credits have no target yet in any market." in info["message"]
    other = C.market_plan(conn, STORAGE, today=TODAY, settings=settings)
    assert other["planned"] == 3 and other["planned_all"] == 5 and other["shortfall"] == info["shortfall"]
    assert [o["slug"] for o in other["others"]] == [MARKET] and other["others"][0]["planned"] == 2
    settings.raw["crexi"]["plan_fallbacks"] = []                  # no fallback: naming them is the way in
    info = C.market_plan(conn, MARKET, today=TODAY, settings=settings)
    assert info["extras_all"] == 0 and info["level1_open"] == 1 and info["shortfall"] == 495
    assert "The free DRILL stages can add up to 1 more" in info["message"]
    E.record_attempt(conn, nameless, "county_auditor", "found", contacts=[{"channel": "phone", "value": "937-555-0123"}])
    assert C.market_plan(conn, MARKET, today=TODAY, settings=settings)["level1_open"] == 0   # a phone to call first


# --- the fast plan gives the same answer as working out every owner ----------------------------------

def _reference_schedule(conn, today, settings):
    """The plan worked out the slow, obvious way (every open owner of every market loaded and evaluated), as
    before the speed-up: [(property id, tier)] in order, and level1_open."""
    cfg = E.drill_config(settings)
    status = C.credit_status(conn, today=today, settings=settings)
    if status["left"] <= 0:
        return [], 0
    tiers, hard = C._fallbacks(settings)
    main, extras, level1 = [], {t: [] for t in tiers}, set()
    for m in rows(conn, "SELECT DISTINCT m.id, m.slug FROM markets m JOIN properties p ON p.market_id = m.id "
                        "ORDER BY m.id"):
        E.ensure_states(conn, m["slug"])
        need = C._need_buybox(conn, m["id"])
        rates = C._credit_rates(conn, m["slug"], settings) if cfg.learn_order else {}
        for ctx in E.load_contexts(conn, market_id=m["id"], status="active"):
            ev = E.evaluate(ctx, cfg)
            c = C._candidate(cfg, ctx, ev, need) or (C._extra(cfg, ctx, ev, need, tiers, hard) if tiers else None)
            if ev.level == 1 and (ctx.in_buybox or not need) and C._eligible(cfg, ctx, ev):
                level1.add(ctx.id)
            if not c:
                continue
            c["rate_info"] = rates.get(ctx.owner_type) or rates.get("all")
            c["credit_rate"] = c["rate_info"]["blended"] if c["rate_info"] else 1.0
            (main if c["tier"] == "main" else extras[c["tier"]]).append(c)
    main.sort(key=C._plan_sort)
    plan = (main + [c for t in tiers for c in sorted(extras[t], key=C._extra_sort)])[:status["left"]]
    return [(c["ctx"].id, c["tier"]) for c in plan], len(level1 - {c["ctx"].id for c in plan})


def _mixed_account(conn):
    """Two markets of owners in every state the plan cares about (seeded, so the same every run)."""
    import random
    rnd = random.Random(20261021)
    reasons = ['["drive_time"]', '["size"]', '["drive_time", "year"]', '["size", "year", "drive_time"]',
               '["state"]', '["skip_county"]', '["drive_time", "property_type"]', "[]", None]
    made = []
    for i in range(90):
        named = rnd.random() > 0.2
        pid = add_prop(conn, owner_name=f"OWNER{i} {'TRUST' if i % 7 == 0 else 'JOHN'}" if named else None)
        made.append(pid)
        kind = rnd.random()
        if kind < 0.85:   # scored; some unscored rows stay in a scored market (never planned)
            in_box = 1 if rnd.random() < 0.35 else 0
            score = rnd.choice([None, 0.01, 0.02, 0.02, 0.05, round(rnd.random() / 5, 3)])
            conn.execute("INSERT OR REPLACE INTO scores(property_id, in_buybox, exclusion_reasons, score, rank, "
                         "computed_at) VALUES(?,?,?,?,?,?)",
                         (pid, in_box, "[]" if in_box else rnd.choice(reasons), score, rnd.choice([None, i + 1]),
                          now_iso()))
    for i in range(25):   # an unscored market: every open owner counts
        made.append(add_prop(conn, market=STORAGE, owner_name=f"KEEPER{i} JANE" if i % 4 else None))
    conn.commit()
    E.run_auto_stages(conn, MARKET)
    E.run_auto_stages(conn, STORAGE)
    for n, pid in enumerate(made):
        pick = n % 11
        if pick == 1:
            try_free_reach(conn, pid)
        elif pick == 2:
            E.add_contact(conn, pid, "phone", f"937-555-{1000 + n:04d}")                 # untested: call first
        elif pick == 3:
            cid = E.add_contact(conn, pid, "email", f"owner{n}@example.com")["contact"]["id"]
            E.set_contact_status(conn, cid, "verified")                                  # already reachable
        elif pick == 4 and n % 2:
            C.use_credit(conn, pid, "nothing", today=TODAY)                              # one credit each
        elif pick == 5 and n % 3 == 0:
            E.level_action(conn, pid, "dnc")
        elif pick == 6 and n % 3 == 0:
            E.level_action(conn, pid, "confirm", person="Pat Owner")
        elif pick == 7:
            E.record_attempt(conn, pid, "county_auditor", "found", person="Pat Owner")
    return made


@pytest.mark.parametrize("batch", [3, 50])
def test_fast_plan_is_the_same_as_evaluating_every_owner(conn, settings, monkeypatch, batch):
    _mixed_account(conn)
    monkeypatch.setattr(C, "SET_ASIDE_BATCH", batch)
    checked, cut_in_set_aside = 0, 0
    for per_month in (6, 40, 47, 60, 500):     # 5 used by the setup; 40-60 cut the plan inside the fallbacks
        for tiers in (["level1_buybox", "set_aside"], ["set_aside", "level1_buybox"], ["set_aside"], []):
            for learn in (True, False):
                settings.raw["crexi"]["credits_per_month"] = per_month
                settings.raw["crexi"]["plan_fallbacks"] = tiers
                settings.drill["learn_order"] = learn
                fast = C._schedule(conn, today=TODAY, settings=settings)
                want, level1 = _reference_schedule(conn, TODAY, settings)
                got = [(c["ctx"].id, c["tier"]) for c in fast.plan]
                assert got == want, (per_month, tiers, learn)
                assert fast.level1_open == level1, (per_month, tiers, learn)
                assert [c["order"] for c in fast.plan] == list(range(1, len(got) + 1))
                checked += len(got)
                left = fast.status["left"]
                cut_in_set_aside += len(got) == left and any(t == "set_aside" for _, t in got)
    assert checked > 500 and cut_in_set_aside >= 4   # real plans, some of them cut among the set-aside owners
