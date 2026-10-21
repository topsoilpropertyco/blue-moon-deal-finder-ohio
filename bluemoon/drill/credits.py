"""Crexi owner-contact credits: a fixed number per month (config.yaml crexi.credits_per_month), no roll-over.

Because unused credits are lost at the end of the month, the plan spends them ALL, at a steady daily pace,
on the best possible owners the free stages could not reach:

- candidates: level 2 (possible owner), still open, in the buy box (once scores exist), no working
  phone/text/email/social/voicemail contact yet, and no credit used on them before;
- order: owners whose free reach stages were all tried first, then the highest Blue Moon score;
- never more items than credits left; each item gets a day to use it on and a plain-English reason.

The app never touches Crexi itself: you open the Crexi link, use the credit there, and record what it showed.
"""
from __future__ import annotations

import calendar
import math
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..config import Settings, get_settings
from ..db import market_by_slug, now_iso, one, rows, scalar
from ..util import clean_str
from . import engine as E

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


def days_left_in_month(today: date | None = None) -> int:
    """Days left including today (31 on the 1st of a 31-day month, 1 on the last day)."""
    d = _today(today)
    return calendar.monthrange(d.year, d.month)[1] - d.day + 1


def credit_status(conn: sqlite3.Connection, *, today: date | None = None, settings: Settings | None = None) -> dict:
    """{month, per_month, used, left, used_today, days_left, daily_pace, found, nothing, pending, hit_rate, message}.

    Credits belong to the Crexi account, so they are counted across every market.
    """
    d = _today(today)
    month = d.strftime("%Y-%m")
    per = credits_per_month(settings)
    r = one(conn, "SELECT COUNT(*) AS n_used, SUM(result='found') AS n_found, SUM(result='nothing') AS n_nothing, "
                  "SUM(result IS NULL) AS n_pending FROM credit_ledger WHERE month=?", (month,)) or {}
    used = int(r.get("n_used") or 0)
    found, nothing, pending = (int(r.get(k) or 0) for k in ("n_found", "n_nothing", "n_pending"))
    utc_today = datetime.now(UTC).date().isoformat() if today is None else d.isoformat()
    used_today = int(scalar(conn, "SELECT COUNT(*) FROM credit_ledger WHERE month=? AND substr(created_at,1,10)=?",
                            (month, utc_today)) or 0)
    left = max(0, per - used)
    days = days_left_in_month(d)
    pace = math.ceil(left / days) if left else 0
    status = {"month": month, "per_month": per, "used": used, "left": left, "used_today": used_today,
              "days_left": days, "daily_pace": pace, "found": found, "nothing": nothing, "pending": pending,
              "hit_rate": round(found / (found + nothing), 4) if (found + nothing) else None}
    status["message"] = _status_message(status)
    return status


def _status_message(s: dict) -> str:
    if not s["per_month"]:
        return "No Crexi credits are set up (config.yaml crexi.credits_per_month)."
    if not s["left"]:
        return f"All {s['per_month']:,} Crexi credits are used for {s['month']}. New ones arrive next month."
    day_word = "day" if s["days_left"] == 1 else "days"
    return (f"{s['left']:,} of {s['per_month']:,} Crexi credits left this month and {s['days_left']} {day_word} to "
            f"go: use about {s['daily_pace']:,} a day. They do not roll over.")


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
    before = credit_status(conn, today=today, settings=settings)
    month = _today(today).strftime("%Y-%m")
    pending = one(conn, "SELECT id FROM credit_ledger WHERE property_id=? AND result IS NULL ORDER BY id DESC LIMIT 1",
                  (property_id,))
    warn = "" if before["left"] or pending else " Your credits for this month look used up; Crexi may not allow it."
    if pending is None:
        with E._tx(conn):
            conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at) VALUES(?,?,?,?)",
                         (property_id, month, None, now_iso()))
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
    sql = ("SELECT c.id, c.property_id, c.month, c.result, c.created_at, p.address, p.city, p.state, p.owner_name "
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
    for st in E._applicable(cfg, ctx.prop, ctx.owner_type, ctx.asset_class):
        if st.key == E.CREDIT_STAGE:
            break
        if st.enabled and st.goal == "reach" and st.cost == "free":
            out.append(st)
    return out


def _candidate(cfg: E.DrillConfig, ctx: E.Ctx, ev: E.Evaluation, need_buybox: bool) -> dict | None:
    if ev.status != "active" or ev.level != 2 or ctx.credits:
        return None
    if need_buybox and not ctx.in_buybox:
        return None
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
            "untested": len(ev.to_validate)}


def _plan_sort(c: dict) -> tuple:
    ctx = c["ctx"]
    score = ctx.score_value
    rank = (ctx.score or {}).get("rank")
    return (0 if c["all_tried"] else 1, 0 if score is not None else 1, -(score or 0.0),
            rank if rank is not None else float("inf"), ctx.id)


def _reason(c: dict) -> str:
    ctx = c["ctx"]
    rank = (ctx.score or {}).get("rank")
    score = ctx.score_value
    head = []
    if rank:
        head.append(f"Rank #{rank:,}")
    if score is not None:
        head.append(f"Blue Moon {score:.1%}")
    parts = [", ".join(head) + "." if head else "Not scored yet."]
    if c["tried"] and c["all_tried"]:
        parts.append(f"Free sources tried ({', '.join(c['tried'])}) gave no working phone or email.")
    elif c["all_tried"]:
        parts.append("No free reach source applies to this owner, so a credit is the next step.")
    else:
        parts.append(f"Not tried yet for free: {', '.join(c['not_tried'])}. Try those first if you have time.")
    if c["untested"]:
        parts.append(f"It also has {c['untested']} untested contact{'s' if c['untested'] != 1 else ''} to try.")
    return " ".join(parts)


def credit_plan(conn: sqlite3.Connection, market_slug: str, limit: int = 25, *, today: date | None = None,
                settings: Settings | None = None) -> list[dict]:
    """Which owners to spend this month's remaining credits on, in order, with a day to use each one."""
    cfg = E.drill_config(settings)
    status = credit_status(conn, today=today, settings=settings)
    n = min(max(0, int(limit)), status["left"])
    if n <= 0:
        return []
    market = market_by_slug(conn, market_slug)
    E.ensure_states(conn, market_slug)
    need_buybox = bool(scalar(conn, "SELECT COUNT(*) FROM scores s JOIN properties p ON p.id=s.property_id "
                                    "WHERE p.market_id=?", (market["id"],)))
    cands = []
    for ctx in E.load_contexts(conn, market_id=market["id"], status="active"):
        c = _candidate(cfg, ctx, E.evaluate(ctx, cfg), need_buybox)
        if c:
            cands.append(c)
    cands.sort(key=_plan_sort)
    pace = max(1, status["daily_pace"])
    start = _today(today)
    last = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    out = []
    for i, c in enumerate(cands[:n]):
        ctx, ev = c["ctx"], c["ev"]
        day = min(start + timedelta(days=i // pace), last)
        when = "today" if day == start else ("tomorrow" if day == start + timedelta(days=1) else day.isoformat())
        out.append({**E._basics(ctx), "order": i + 1, "level": ev.level, "best_guess_person": ev.best_guess,
                    "next_step_text": ev.next_step["text"], "free_tried": c["tried"], "free_not_tried": c["not_tried"],
                    "all_free_tried": c["all_tried"], "untested_contacts": c["untested"], "use_on": day.isoformat(),
                    "when": when, "reason": _reason(c),
                    "links": E.L.links_for(ctx.prop, E.CREDIT_STAGE, ev.best_guess, settings=settings,
                                           parse_owner=False)
                    if E.CREDIT_STAGE in cfg.by_key else []})
    return out


def plan_summary(conn: sqlite3.Connection, market_slug: str, *, today: date | None = None,
                 settings: Settings | None = None) -> dict:
    """{status, candidates, covered, shortfall, message}: can this month's credits all be spent well?"""
    status = credit_status(conn, today=today, settings=settings)
    plan = credit_plan(conn, market_slug, limit=max(1, status["left"]), today=today, settings=settings) \
        if status["left"] else []
    short = max(0, status["left"] - len(plan))
    msg = status["message"]
    if short:
        msg += (f" Only {len(plan):,} owners qualify right now, so {short:,} credits have no target yet: work the "
                f"free stages to turn more owners into possible owners.")
    return {"status": status, "planned": len(plan), "shortfall": short, "message": msg}
