"""Crexi owner-contact credits: a fixed number per credit period (config.yaml crexi.credits_per_month), no
roll-over. A period starts on crexi.reset_day (29 by default: Oct 29 to Nov 28, then Nov 29...); see
bluemoon/crexi/period.py. Bulk batches (bluemoon/crexi) spend them.

Credits used = credits Crexi already spent (the ledger: rows Crexi matched and returned in a batch, or used by
hand) + the properties sent to Crexi that are still waiting ("in flight"). Counting the waiting ones is what
stops the 500 being overspent. Crexi charges a credit only for a row it MATCHES (a matched row costs one even
with no phone or email; a 'Not Matched' row costs nothing), so when Crexi's reply is imported the rows that
did not come back (missing) free those credits again at once.

Because unused credits are lost when the period ends, the plan spends them ALL, at a steady daily pace,
on the best possible owners the free stages could not reach. Credits belong to the Crexi account, so there
is ONE plan for every market; each market's Credits screen shows its share of it.

- candidates: level 2 (possible owner), still open, in the buy box (once scores exist), no working
  phone/text/email/social/voicemail contact yet, no untested phone/email still waiting in Reach out, and
  no credit used on them before;
- order: owners whose free reach stages were all tried first, then the highest expected value: Blue Moon
  score x the Crexi credit's hit rate for the owner type (the drill.yaml guess until real results come in);
- when they can't use every credit, config.yaml crexi.plan_fallbacks fills the rest: buy-box owners with no
  owner name (a credit may show it), then the best owners just outside the buy box;
- never more items than credits left; each item gets a day to use it on and a plain-English reason.

The app never touches Crexi itself: you open the Crexi link, use the credit there, and record what it showed.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Settings, get_settings
from ..crexi import period as P
from ..db import loads, market_by_slug, now_iso, one, rows, scalar
from ..scoring.buybox import EXCLUSION_LABELS, MANUAL_REASON
from ..util import clean_str
from . import engine as E
from . import stats as S

DEFAULT_CREDITS_PER_MONTH = 500
RESULTS = ("found", "nothing")


def credits_per_month(settings: Settings | None = None) -> int:
    s = settings or get_settings()
    v = (s.section("crexi") or {}).get("credits_per_month", DEFAULT_CREDITS_PER_MONTH)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return DEFAULT_CREDITS_PER_MONTH


def _today(today: date | None) -> date:
    return today or datetime.now().date()


def days_left_in_month(today: date | None = None, settings: Settings | None = None) -> int:
    """Days left in the credit period, counting today (the name is from when periods were calendar months)."""
    return P.days_left(_today(today), settings)


def _ledger_in_period(conn: sqlite3.Connection, start: date, end: date, key: str) -> list[dict]:
    """The ledger rows of one credit period. A row made before periods existed (period NULL) belongs to the
    period its local day falls in."""
    return rows(conn, "SELECT id, property_id, result, batch_id, date(created_at, 'localtime') AS day "
                      "FROM credit_ledger WHERE period = ? OR "
                      "(period IS NULL AND date(created_at, 'localtime') BETWEEN ? AND ?)",
                (key, start.isoformat(), end.isoformat()))


def credit_status(conn: sqlite3.Connection, *, today: date | None = None, settings: Settings | None = None) -> dict:
    """{month, period, period_end, next_reset, reset_day, per_month, used, left, returned, in_flight, used_today,
    days_left, daily_pace, today_left, found, nothing, pending, hit_rate, message}.

    Credits belong to the Crexi account, so they are counted across every market, per credit period.
    used = returned + in_flight (spent, or waiting at Crexi); left = per_month - used, so the budget can never
    be overspent. `returned` counts the period's ledger rows (rows Crexi matched and returned in a batch, and
    credits used by hand); `in_flight` the properties sent in a batch and not returned yet. `month` is the
    'YYYY-MM' the period started in.

    The daily pace is worked out from the credits left when today began, so it stays the same all day while
    credits are used (it would otherwise drop with every credit used today). `today_left` is how many more to
    use today at that pace; the plan gives today exactly that many and `daily_pace` to every later day.
    """
    d = _today(today)
    info = P.describe(d, settings)
    start, end = date.fromisoformat(info["start"]), date.fromisoformat(info["end"])
    per = credits_per_month(settings)
    ledger = _ledger_in_period(conn, start, end, info["key"])
    returned = len(ledger)
    found = sum(1 for r in ledger if r["result"] == "found")
    nothing = sum(1 for r in ledger if r["result"] == "nothing")
    pending = sum(1 for r in ledger if r["result"] is None)
    in_flight = int(scalar(conn, "SELECT COUNT(*) FROM crexi_batch_items i JOIN crexi_batches b ON b.id = i.batch_id "
                                 "WHERE b.period = ? AND i.status = 'sent'", (info["key"],)) or 0)
    # created_at is stored in UTC; 'localtime' turns it into this computer's date, the same clock as `d`.
    # Today's use: credits used by hand today, plus everything sent in a batch today.
    sent_today = int(scalar(conn, "SELECT COUNT(*) FROM crexi_batch_items i JOIN crexi_batches b ON b.id = i.batch_id "
                                  "WHERE b.period = ? AND i.status IN ('sent', 'returned') "
                                  "AND date(i.sent_at, 'localtime') = ?", (info["key"], d.isoformat())) or 0)
    used_today = sum(1 for r in ledger if r["batch_id"] is None and r["day"] == d.isoformat()) + sent_today
    used = returned + in_flight
    left = max(0, per - used)
    days = P.days_left(d, settings)
    left_this_morning = max(0, per - max(0, used - used_today))
    pace = math.ceil(left_this_morning / days) if left else 0
    status = {"month": info["key"][:7], "period": info["key"], "period_label": P.label(info["key"], settings),
              "period_end": info["end"],
              "next_reset": info["next_reset"], "reset_day": info["reset_day"],
              "per_month": per, "used": used, "left": left, "returned": returned, "in_flight": in_flight,
              "used_today": used_today, "days_left": days, "daily_pace": pace,
              "today_left": min(left, max(0, pace - used_today)),
              "found": found, "nothing": nothing, "pending": pending,
              "hit_rate": round(found / (found + nothing), 4) if (found + nothing) else None}
    status["message"] = _status_message(status)
    return status


def _status_message(s: dict) -> str:
    if not s["per_month"]:
        return "No Crexi credits are set up (config.yaml crexi.credits_per_month)."
    day_word = "day" if s["days_left"] == 1 else "days"
    waiting = f", {s['in_flight']:,} waiting at Crexi" if s["in_flight"] else ""
    if not s["left"]:
        return (f"All {s['per_month']:,} Crexi credits are used or waiting at Crexi for this period "
                f"({s['returned']:,} returned{waiting}). New ones arrive on {s['next_reset']} "
                f"({s['days_left']} {day_word} to go).")
    today = f" ({s['today_left']:,} more today)" if s["used_today"] else ""
    return (f"{s['left']:,} of {s['per_month']:,} Crexi credits free this period ({s['returned']:,} returned"
            f"{waiting}), shared by every market, and {s['days_left']} {day_word} to go: use about "
            f"{s['daily_pace']:,} a day{today}. They do not roll over.")


def use_credit(conn: sqlite3.Connection, property_id: int, result: str | None = None, *,
               contacts: Iterable[Mapping[str, Any]] | None = None, person: str | None = None,
               detail: str | None = None, today: date | None = None, settings: Settings | None = None) -> dict:
    """Record that a Crexi credit was used on this property.

    result None  -> the credit is spent; record what Crexi showed later (call again with found/nothing).
    found/nothing -> checks off the 'Crexi credit' stage too (with any contacts or person it showed).
    Calling with a result after a pending credit completes that credit instead of spending another.
    """
    res = (clean_str(result) or "").lower() or None
    if res is not None and res not in RESULTS:
        raise ValueError(f"'{result}' is not a credit result. Use found or nothing (or leave it blank).")
    items = E.parse_contacts(contacts)
    for c in items:  # check every value before anything is written
        E.normalize_contact(c.channel, c.value)
    E._require_property(conn, property_id)
    waiting = E.waiting_at_crexi(conn, property_id)
    if waiting is not None:
        raise ValueError(E.waiting_message(waiting))
    before = credit_status(conn, today=today, settings=settings)
    period = P.period_key(_today(today), settings)
    month = period[:7]
    pending = one(conn, "SELECT id FROM credit_ledger WHERE property_id=? AND result IS NULL ORDER BY id DESC LIMIT 1",
                  (property_id,))
    warn = "" if before["left"] or pending else " Your credits for this month look used up; Crexi may not allow it."
    if pending is None:
        with E._tx(conn):
            conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at, period) VALUES(?,?,?,?,?)",
                         (property_id, month, None, now_iso(), period))
    elif res is None:
        return _done(conn, property_id, "A credit is already open on this property. Record what Crexi showed.",
                     None, today, settings)
    if res is None:
        return _done(conn, property_id, "Credit used. Now record what Crexi showed (found or nothing)." + warn,
                     None, today, settings)
    cfg = E.drill_config(settings)
    if E.CREDIT_STAGE in cfg.by_key:  # checks off the stage and fills in the open ledger row
        E.record_attempt(conn, property_id, E.CREDIT_STAGE, res, detail=detail or "Used a Crexi owner-contact credit.",
                         person=person, contacts=[c.model_dump() for c in items], settings=settings)
    else:  # the stage was removed from drill.yaml: keep the ledger anyway
        with E._tx(conn):
            conn.execute("UPDATE credit_ledger SET result=? WHERE property_id=? AND result IS NULL", (res, property_id))
    what = "found contact info" if res == "found" else "found nothing new"
    return _done(conn, property_id, f"Credit recorded: Crexi {what}." + warn, res, today, settings)


def _done(conn: sqlite3.Connection, property_id: int, message: str, result: str | None, today: date | None,
          settings: Settings | None) -> dict:
    status = credit_status(conn, today=today, settings=settings)
    drill = E.property_drill(conn, property_id, settings=settings)
    return {"ok": True, "property_id": property_id, "result": result, "credits": status,
            "message": f"{message} {status['left']:,} left this month.", "level": drill["level"],
            "status": drill["status"], "next_step": drill["next_step"], "drill": drill}


def credit_log(conn: sqlite3.Connection, market_slug: str | None = None, limit: int = 100) -> list[dict]:
    """Credits used, newest first, with the property address."""
    sql = ("SELECT c.id, c.property_id, c.month, c.period, c.batch_id, c.result, c.created_at, p.address, p.city, "
           "p.state, p.owner_name "
           "FROM credit_ledger c LEFT JOIN properties p ON p.id=c.property_id")
    params: list[Any] = []
    if market_slug:
        sql += " WHERE p.market_id=?"
        params.append(market_by_slug(conn, market_slug)["id"])
    sql += " ORDER BY c.id DESC LIMIT ?"
    params.append(max(0, int(limit)))
    return rows(conn, sql, params)


def _free_reach(cfg: E.DrillConfig, ctx: E.Ctx) -> list[E.Stage]:
    """Free reach stages that come before the Crexi credit for this property."""
    out = []
    for st in E._applicable(cfg, ctx.prop, ctx.owner_type, ctx.asset_class, ctx.stage_order):
        if st.key == E.CREDIT_STAGE:
            break
        if st.enabled and st.goal == "reach" and st.cost == "free":
            out.append(st)
    return out


def _eligible(cfg: E.DrillConfig, ctx: E.Ctx, ev: E.Evaluation) -> dict | None:
    """Checks every planned owner passes (main plan and fallbacks alike), plus what the free stages tried."""
    if ev.status != "active" or ctx.credits:
        return None
    if ev.to_validate:
        return None  # an untested phone/email waits in Reach out first: call it before spending a credit
    if any(c["channel"] in E.REACH_CHANNELS and c["status"] == "verified" for c in ctx.contacts):
        return None
    credit = cfg.by_key.get(E.CREDIT_STAGE)
    if credit is not None:
        if not credit.enabled or not credit.applies(ctx.owner_type, ctx.asset_class, ctx.prop.get("state")):
            return None
        if E.CREDIT_STAGE in E._latest_by_stage(ctx.attempts):
            return None
    latest = E.current_latest(ctx.attempts)
    free = _free_reach(cfg, ctx)
    tried = [st.name for st in free if st.key in latest]
    not_tried = [st.name for st in free if st.key not in latest]
    return {"ctx": ctx, "ev": ev, "tried": tried, "not_tried": not_tried, "all_tried": not not_tried,
            "untested": len(ev.to_validate), "tier": "main", "market_slug": ctx.prop.get("market_slug")}


def _candidate(cfg: E.DrillConfig, ctx: E.Ctx, ev: E.Evaluation, need_buybox: bool) -> dict | None:
    """The main plan: possible owners (level 2) in the buy box (once the market is scored)."""
    if ev.level != 2 or (need_buybox and not ctx.in_buybox):
        return None
    return _eligible(cfg, ctx, ev)


FALLBACK_TIERS = ("level1_buybox", "set_aside")
DEFAULT_FALLBACKS = list(FALLBACK_TIERS)
DEFAULT_HARD_EXCLUSIONS = ["state", "skip_county", "property_type"]


def _fallbacks(settings: Settings | None) -> tuple[list[str], set[str]]:
    """(crexi.plan_fallbacks in order, crexi.plan_hard_exclusions) from config.yaml. A property set aside by
    hand ('manual') never gets a fallback credit, whatever config.yaml says."""
    c = (settings or get_settings()).section("crexi") or {}
    tiers = c.get("plan_fallbacks", DEFAULT_FALLBACKS)
    hard = c.get("plan_hard_exclusions", DEFAULT_HARD_EXCLUSIONS)
    tiers = [t for t in ([tiers] if isinstance(tiers, str) else (tiers or [])) if t in FALLBACK_TIERS]
    hard_set = {str(h) for h in ([hard] if isinstance(hard, str) else (hard or []))} | {MANUAL_REASON}
    return list(dict.fromkeys(tiers)), hard_set


def _reasons(raw: Any) -> list[str]:
    """scores.exclusion_reasons (JSON text or a list) as a list of reason keys."""
    found = loads(raw, []) if isinstance(raw, str) else (raw or [])
    return [str(r) for r in found] if isinstance(found, list) else []


def _exclusions(ctx: E.Ctx) -> list[str]:
    return _reasons((ctx.score or {}).get("exclusion_reasons"))


def _extra(cfg: E.DrillConfig, ctx: E.Ctx, ev: E.Evaluation, need_buybox: bool, tiers: list[str],
           hard: set[str]) -> dict | None:
    """A fallback owner (only used when the main plan can't use every credit), with its tier."""
    if ev.level not in (1, 2):
        return None
    if "level1_buybox" in tiers and ev.level == 1 and (ctx.in_buybox or not need_buybox):
        c = _eligible(cfg, ctx, ev)
        return {**c, "tier": "level1_buybox"} if c else None
    if "set_aside" in tiers and need_buybox and ctx.in_buybox is False:
        reasons = _exclusions(ctx)
        if hard & set(reasons):
            return None
        c = _eligible(cfg, ctx, ev)
        return {**c, "tier": "set_aside", "reasons": reasons} if c else None
    return None


def _credit_rates(conn: sqlite3.Connection, market_slug: str, settings: Settings | None,
                  counts: Mapping[tuple[int, str, str], Mapping[str, int]] | None = None) -> dict[str, dict]:
    """{owner_type or 'all': {blended, tried, found, label}} for the Crexi credit stage (the drill.yaml guess,
    blended with real results as they come in). `counts`: stats.counts_by_market, when the caller has them."""
    out = {}
    for r in S.stage_hit_rates(conn, market_slug, settings=settings, counts=counts):
        if r["stage_key"] == E.CREDIT_STAGE and r["blended"] is not None:
            out[r["owner_type"]] = {"blended": float(r["blended"]), "tried": r["tried"], "found": r["found"],
                                    "label": r["owner_label"]}
    return out


def _plan_sort(c: dict) -> tuple:
    """Free reach stages all tried first, then the highest expected value (score x the credit's hit rate for
    this owner type); the score, then the rank, break ties."""
    ctx = c["ctx"]
    score = ctx.score_value
    rank = (ctx.score or {}).get("rank")
    value = (score or 0.0) * c.get("credit_rate", 1.0)
    return (0 if c["all_tried"] else 1, 0 if score is not None else 1, -value, -(score or 0.0),
            rank if rank is not None else float("inf"), ctx.id)


def _extra_sort(c: dict) -> tuple:
    """Fallbacks: fewest reasons for being set aside first, then the highest score."""
    ctx = c["ctx"]
    score = ctx.score_value
    return (len(c.get("reasons") or []), 0 if score is not None else 1, -(score or 0.0), ctx.id)


def _reason(c: dict, min_tries: int) -> str:
    ctx = c["ctx"]
    rank = (ctx.score or {}).get("rank")
    score = ctx.score_value
    head = []
    if rank:
        head.append(f"Rank #{rank:,}")
    if score is not None:
        head.append(f"Blue Moon {score:.1%}")
    parts = [", ".join(head) + "." if head else "Not scored yet."]
    if c["tier"] == "level1_buybox":
        parts.append("No owner name in the export; a Crexi credit may show it.")
    elif c["tier"] == "set_aside":
        why = ", ".join(EXCLUSION_LABELS.get(r, r).lower() for r in c.get("reasons") or []) or "set aside"
        parts.append(f"Outside the buy box ({why}). Only because credits would otherwise expire.")
    if c["tried"] and c["all_tried"]:
        parts.append(f"Free sources tried ({', '.join(c['tried'])}) gave no working phone or email.")
    elif c["all_tried"]:
        parts.append("No free reach source applies to this owner, so a credit is the next step.")
    else:
        parts.append(f"Not tried yet for free: {', '.join(c['not_tried'])}. Try those first if you have time.")
    rate = c.get("rate_info")
    if rate and rate["tried"] >= min_tries:
        parts.append(f"Crexi has turned up a phone or email for {rate['found'] / rate['tried']:.0%} of {rate['label']} "
                     f"so far ({rate['found']:,} of {rate['tried']:,}).")
    return " ".join(parts)


def _need_buybox(conn: sqlite3.Connection, market_id: int) -> bool:
    return bool(scalar(conn, "SELECT COUNT(*) FROM scores s JOIN properties p ON p.id=s.property_id "
                             "WHERE p.market_id=?", (market_id,)))


def _open_rows(conn: sqlite3.Connection, market_id: int) -> list[dict]:
    """The properties of one market that could get a credit, found with one query and no DRILL evaluation:
    still open (status active), no credit used on them before and no working phone, text, email, social or
    voicemail contact (the same checks _eligible makes). The other checks need the full evaluation."""
    marks = ",".join("?" for _ in E.REACH_CHANNELS)
    return rows(conn, f"""
        SELECT p.id, s.in_buybox, s.score, s.exclusion_reasons
        FROM properties p
        LEFT JOIN drill_state d ON d.property_id = p.id
        LEFT JOIN scores s ON s.property_id = p.id
        WHERE p.market_id = ? AND COALESCE(d.status, 'active') = 'active'
          AND p.id NOT IN (SELECT property_id FROM credit_ledger WHERE property_id IS NOT NULL)
          AND p.id NOT IN (SELECT property_id FROM crexi_batch_items WHERE status IN ('sent', 'returned'))
          AND p.id NOT IN (SELECT property_id FROM contact_points WHERE status = 'verified' AND channel IN ({marks}))
        ORDER BY p.id""", (market_id, *E.REACH_CHANNELS))


@dataclass
class Schedule:
    """The one plan for the whole Crexi account (see _schedule)."""
    status: dict
    plan: list[dict] = field(default_factory=list)
    level1_open: int = 0      # level-1 buy-box owners who would qualify for a credit once named, not planned yet


SET_ASIDE_BATCH = 50    # at least this many set-aside owners are worked out at a time


def _schedule(conn: sqlite3.Connection, *, today: date | None = None, settings: Settings | None = None,
              want: int | None = None) -> Schedule:
    """The one plan for the whole Crexi account.

    Credits belong to the account, so every market's owners compete for them: the main candidates of every
    market in one order, then the fallbacks (crexi.plan_fallbacks), cut at the credits left. Each entry gets
    its place in the whole plan (`order`) and a day to use it on: today gets the credits still to use today
    (status today_left), every later day the daily pace.

    Fast on big lists: one query per market finds the owners who could get a credit at all, the DRILL is
    worked out only for buy-box owners (the main plan and the no-name fallback come from them), and the
    set-aside owners are worked out best first, only until the credits left are all planned."""
    cfg = E.drill_config(settings)
    status = credit_status(conn, today=today, settings=settings)
    if want is None:
        want = status["left"]      # a bulk export asks for its own N (bluemoon/crexi/batches.py)
    if want <= 0:
        return Schedule(status)
    tiers, hard = _fallbacks(settings)
    main: list[dict] = []
    extras: dict[str, list[dict]] = {t: [] for t in tiers}
    need: dict[int, bool] = {}
    rates: dict[int, dict[str, dict]] = {}
    box_ids: list[int] = []
    aside: list[tuple[tuple, int]] = []      # (the same order as _extra_sort, property id)
    parsed: dict[Any, list[str]] = {}
    markets = rows(conn, "SELECT DISTINCT m.id, m.slug FROM markets m JOIN properties p ON p.market_id = m.id "
                         "ORDER BY m.id")
    # The DRILL results by stage and owner type, counted once for the credit hit rates and the learned stage
    # order (load_contexts would count them again for every batch).
    ids = [int(m["id"]) for m in markets]
    counts = S.counts_by_market(conn, ids) if ids and cfg.learn_order else None
    learned = E._learned_orders(conn, ids, counts=counts)
    for m in markets:
        E.ensure_states(conn, m["slug"])
        mid = int(m["id"])
        need[mid] = _need_buybox(conn, mid)
        rates[mid] = _credit_rates(conn, m["slug"], settings, counts) if cfg.learn_order else {}
        for r in _open_rows(conn, mid):
            in_box = None if r["in_buybox"] is None else bool(r["in_buybox"])
            if in_box or not need[mid]:
                box_ids.append(int(r["id"]))
            elif in_box is False and "set_aside" in tiers:
                raw = r["exclusion_reasons"]
                if raw not in parsed:          # a few dozen distinct reason lists, thousands of rows
                    parsed[raw] = _reasons(raw)
                reasons = parsed[raw]
                if not hard & set(reasons):
                    score = r["score"]
                    aside.append(((len(reasons), 0 if score is not None else 1, -(score or 0.0), int(r["id"])),
                                  int(r["id"])))
            # anything else (an unscored property in a scored market) never gets a credit

    level1: set[int] = set()

    def take(ids: list[int]) -> None:
        for ctx in E.load_contexts(conn, property_ids=ids, learned=learned):
            ev = E.evaluate(ctx, cfg)
            mid = int(ctx.prop["market_id"])
            c = _candidate(cfg, ctx, ev, need[mid]) or (_extra(cfg, ctx, ev, need[mid], tiers, hard) if tiers else None)
            if ev.level == 1 and (ctx.in_buybox or not need[mid]) and (c or _eligible(cfg, ctx, ev)):
                level1.add(ctx.id)
            if not c:
                continue
            c["rate_info"] = rates[mid].get(ctx.owner_type) or rates[mid].get("all")
            c["credit_rate"] = c["rate_info"]["blended"] if c["rate_info"] else 1.0
            (main if c["tier"] == "main" else extras[c["tier"]]).append(c)

    if box_ids:
        take(box_ids)
    main.sort(key=_plan_sort)
    plan = main
    left = want
    aside.sort()
    done = 0
    for t in tiers:
        if t == "set_aside":
            while (short := left - len(plan) - len(extras[t])) > 0 and done < len(aside):
                batch = max(SET_ASIDE_BATCH, 2 * short)      # some won't qualify (a phone to call first, ...)
                take([pid for _, pid in aside[done:done + batch]])
                done += batch
        plan = plan + sorted(extras[t], key=_extra_sort)
    plan = plan[:left]
    pace = max(1, status["daily_pace"])
    first = min(status["today_left"], len(plan))
    start = _today(today)
    last = date.fromisoformat(status["period_end"])
    for i, c in enumerate(plan):
        day = start if i < first else min(start + timedelta(days=1 + (i - first) // pace), last)
        c["order"], c["day"] = i + 1, day
        c["when"] = "today" if day == start else ("tomorrow" if day == start + timedelta(days=1) else day.isoformat())
    planned = {c["ctx"].id for c in plan}
    return Schedule(status, plan, len(level1 - planned))


def _item(c: dict, cfg: E.DrillConfig, settings: Settings | None) -> dict:
    ctx, ev = c["ctx"], c["ev"]
    return {**E._basics(ctx), "market_slug": c["market_slug"], "order": c["order"], "tier": c["tier"],
            "level": ev.level, "best_guess_person": ev.best_guess, "next_step_text": ev.next_step["text"],
            "free_tried": c["tried"], "free_not_tried": c["not_tried"], "all_free_tried": c["all_tried"],
            "untested_contacts": c["untested"], "use_on": c["day"].isoformat(), "when": c["when"],
            "reason": _reason(c, S._min_tries(settings)),
            "links": E.L.links_for(ctx.prop, E.CREDIT_STAGE, ev.best_guess, settings=settings, parse_owner=False)
            if E.CREDIT_STAGE in cfg.by_key else []}


def account_plan(conn: sqlite3.Connection, *, today: date | None = None,
                 settings: Settings | None = None) -> list[dict]:
    """The whole account's plan for this month's remaining credits (every market, main plan then fallbacks)."""
    cfg = E.drill_config(settings)
    return [_item(c, cfg, settings) for c in _schedule(conn, today=today, settings=settings).plan]


def market_plan(conn: sqlite3.Connection, market_slug: str, limit: int = 25, *, today: date | None = None,
                settings: Settings | None = None) -> dict:
    """This market's share of the account plan, worked out once:

    {status, plan (main, first `limit`), extras (fallbacks, first `limit`), planned, extra_count, planned_all,
     extras_all, extra_tiers, other_markets, others, shortfall, level1_open, message}.

    planned/extra_count count this market; planned_all/extras_all, shortfall and level1_open the whole account
    (every market shares the credits). others: [{slug, name, planned, extras}] for the other markets with owners
    in this month's plan, most planned first. level1_open: buy-box owners with no owner name yet who would
    qualify for a credit once the free DRILL stages name them, and are not planned yet."""
    market_by_slug(conn, market_slug)   # KeyError for an unknown market
    cfg = E.drill_config(settings)
    sched = _schedule(conn, today=today, settings=settings)
    status, plan = sched.status, sched.plan
    here = [c for c in plan if c["market_slug"] == market_slug]
    main_here = [c for c in here if c["tier"] == "main"]
    extra_here = [c for c in here if c["tier"] != "main"]
    n = max(0, int(limit))
    names = {r["slug"]: r["name"] for r in rows(conn, "SELECT slug, name FROM markets")}
    others: dict[str, dict] = {}
    for c in plan:
        slug = c["market_slug"]
        if slug != market_slug:
            o = others.setdefault(slug, {"slug": slug, "name": names.get(slug) or slug, "planned": 0, "extras": 0})
            o["planned" if c["tier"] == "main" else "extras"] += 1
    info = {"status": status,
            "plan": [_item(c, cfg, settings) for c in main_here[:n]],
            "extras": [_item(c, cfg, settings) for c in extra_here[:n]],
            "planned": len(main_here), "extra_count": len(extra_here),
            "planned_all": sum(1 for c in plan if c["tier"] == "main"),
            "extras_all": sum(1 for c in plan if c["tier"] != "main"),
            "extra_tiers": {t: sum(1 for c in plan if c["tier"] == t) for t in FALLBACK_TIERS},
            "other_markets": len(plan) - len(here),
            "others": sorted(others.values(), key=lambda o: (-o["planned"], -o["extras"], o["slug"])),
            "shortfall": max(0, status["left"] - len(plan)),
            "level1_open": sched.level1_open}
    info["message"] = _plan_message(info, bool(_fallbacks(settings)[0]))
    return info


def _plan_message(i: dict, fallbacks_on: bool) -> str:
    status = i["status"]
    msg = status["message"]
    if not status["left"]:
        return msg
    everywhere = " across all markets" if i["others"] else ""
    if i["planned_all"]:
        msg += (f" {i['planned_all']:,} possible owner{'s' if i['planned_all'] != 1 else ''} in the buy box "
                f"qualif{'y' if i['planned_all'] != 1 else 'ies'}{everywhere}"
                + (f" ({i['planned']:,} in this market)." if everywhere else "."))
    if i["extras_all"]:
        tiers = i["extra_tiers"]
        parts = []
        if tiers.get("level1_buybox"):
            parts.append(f"{tiers['level1_buybox']:,} to buy-box owners with no owner name yet")
        if tiers.get("set_aside"):
            parts.append(f"{tiers['set_aside']:,} to the best owners just outside the buy box")
        msg += f" {i['extras_all']:,} more credits go to the next-best owners ({'; '.join(parts)}) so none expire."
    if i["other_markets"]:
        msg += f" {i['other_markets']:,} of this month's planned credits are for owners in other markets."
    if i["shortfall"]:
        msg += f" {i['shortfall']:,} credits have no target yet in any market."
        if i["level1_open"]:
            msg += (f" The free DRILL stages can add up to {i['level1_open']:,} more by finding owner names for "
                    f"buy-box properties.")
        if not fallbacks_on:
            msg += " Turn on crexi.plan_fallbacks in config.yaml to plan them for the next-best owners."
    return msg


def credit_plan(conn: sqlite3.Connection, market_slug: str, limit: int = 25, *, today: date | None = None,
                settings: Settings | None = None) -> list[dict]:
    """This market's owners in the account-wide plan (main plan only), in order, with a day to use each one."""
    return market_plan(conn, market_slug, limit, today=today, settings=settings)["plan"]


def credit_extras(conn: sqlite3.Connection, market_slug: str, n: int = 25, *, today: date | None = None,
                  settings: Settings | None = None) -> list[dict]:
    """This market's fallback owners (crexi.plan_fallbacks), planned only because credits would otherwise expire."""
    return market_plan(conn, market_slug, n, today=today, settings=settings)["extras"]


def plan_summary(conn: sqlite3.Connection, market_slug: str, *, today: date | None = None,
                 settings: Settings | None = None) -> dict:
    """{status, planned, extra_count, planned_all, extras_all, other_markets, shortfall, message}: can this
    month's credits all be spent well?"""
    info = market_plan(conn, market_slug, 0, today=today, settings=settings)
    return {k: v for k, v in info.items() if k not in ("plan", "extras")}
