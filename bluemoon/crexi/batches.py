"""Crexi bulk batches: pick the next N properties, write the file Seth uploads to Crexi, keep the books.

The flow (files only; the app never logs in to Crexi or opens it):
  1. export  `export_batch` picks the best N possible owners (the plan in drill/credits.py), writes
             output/crexi/<period>/batch-<id>-<N>.csv, and counts the N as "in flight" against the 500.
  2. upload  Seth uploads that file to Crexi's bulk tool by hand.
  3. import  `reply.apply_reply` reads Crexi's file back and FINISHES every batch the file touches at once
             (`finish_batches`): a row Crexi matched is 'returned' (one credit spent); a row it answered
             'Not Matched', or left out of the file, is 'missing' (stored as status 'not_returned'): it costs
             nothing, its credit is free again right away, and the DRILL's Crexi stage records "nothing" so the
             DRILL moves that owner on to its other sources (county records, AI research...). There is no
             close step. A batch with no row in the file is not touched (that file was not its reply).
  A late file: a row that comes back after it was marked missing is recorded as returned then (the credit is
  charged then, and the DRILL result is upgraded).

Credit rule: Crexi charges one credit for each row it MATCHES (even with no phone or email) and nothing for
a "Not Matched" row. used = returned + in flight; free = per period - used.
"""
from __future__ import annotations

import csv
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..db import dumps, loads, now_iso, one, rows, scalar
from ..drill import credits as drill_credits
from ..drill import engine as E
from ..scoring.buybox import MANUAL_REASON
from ..util import clean_str, normalize_address
from . import period as P

CSV_ENCODING = "utf-8-sig"        # Excel opens it with the right characters
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# The columns of the file Seth uploads to Crexi: Crexi's own names first, in the order Crexi sends them back,
# then ours (the ids are only for our own records; matching never depends on Crexi sending them back).
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Property Link", "crexi_url"), ("Property Type", "property_type"), ("Address", "address"),
    ("City", "city"), ("State", "state"), ("Zip Code", "zip"), ("County", "county"), ("APN", "apn"),
    ("Unit", "unit"), ("Latitude", "lat"), ("Longitude", "lng"), ("Property Name", "name"),
    ("Number of Units", "units"), ("Property ID", "id"), ("Batch ID", "batch_id"), ("Row", "position"),
)

TIER_TEXT = {
    "main": "possible owners in the buy box",
    "level1_buybox": "buy-box owners with no owner name yet",
    "set_aside": "owners just outside the buy box (so no credit expires)",
    "retry": "second pass: Crexi did not return these last time",
}


class CrexiError(ValueError):
    """A problem Seth can fix; the message is plain English."""


# --- small helpers ----------------------------------------------------------------------------------------

def local_day(iso: str | None) -> str:
    """'Oct 29' for a stored UTC time, on this computer's calendar."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone()
    return f"{dt.strftime('%b')} {dt.day}"


def safe_cell(value: Any) -> Any:
    """A CSV cell a spreadsheet can't run as a formula: text that starts with = + - @ (or a tab or return) gets a
    leading quote. Real numbers (a negative longitude) are written as numbers."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    return "'" + text if text.lstrip(" ").startswith(FORMULA_START) else text


def output_dir(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).path("output")


def _zip5(z: Any) -> str:
    s = clean_str(z) or ""
    return s.split("-")[0].split(".")[0].zfill(5)[:5] if s else ""


def link_key(url: str | None) -> str | None:
    """The Crexi record id inside a property link (lower case), or the whole link when it has no id."""
    from ..ingest.crexi import source_id_from_link
    u = clean_str(url)
    if not u:
        return None
    return (source_id_from_link(u) or u).lower()


def apn_key(prop: dict) -> str | None:
    apn = re.sub(r"[^a-z0-9]", "", (clean_str(prop.get("apn")) or "").lower())
    if not apn:
        return None
    return f"{(prop.get('state') or '').upper()}|{(prop.get('county') or '').upper()}|{apn}"


def has_house_number(street: str | None) -> bool:
    """'123 MAIN ST' has one; 'CHANNINGWAY DR' does not (several properties can sit on that street)."""
    return bool(street and re.match(r"\d", street))


def address_key(prop: dict) -> str | None:
    """Street + city + ZIP, only when the street has a house number: without one, different properties on the same
    street (each with its own owner and parcel) would look like one building and never be sent."""
    street = normalize_address(prop.get("address"))
    if not has_house_number(street):
        return None
    return f"{street}|{(clean_str(prop.get('city')) or '').upper()}|{_zip5(prop.get('zip'))}"


def duplicate_keys(prop: dict) -> list[str]:
    """The ways one property can be the same as another: Crexi link, then parcel number, then address."""
    out = []
    for kind, k in (("link", link_key(prop.get("crexi_url"))), ("apn", apn_key(prop)), ("addr", address_key(prop))):
        if k:
            out.append(f"{kind}:{k}")
    return out


# --- status and top-up -------------------------------------------------------------------------------------

def batch_counts(conn: sqlite3.Connection, batch_id: int) -> dict:
    r = one(conn, """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(status = 'sent'), 0) AS waiting,
               COALESCE(SUM(status = 'returned'), 0) AS returned,
               COALESCE(SUM(status = 'not_returned'), 0) AS not_returned,
               COALESCE(SUM(status = 'withdrawn'), 0) AS withdrawn,
               COALESCE(SUM(status = 'returned' AND result = 'found'), 0) AS found,
               COALESCE(SUM(status = 'returned' AND result = 'owner_only'), 0) AS owner_only,
               COALESCE(SUM(status = 'returned' AND result = 'nothing'), 0) AS no_data,
               COALESCE(SUM(status = 'sent' AND not_matched_at IS NOT NULL), 0) AS not_matched,
               COALESCE(SUM(is_retry), 0) AS second_pass
        FROM crexi_batch_items WHERE batch_id = ?""", (batch_id,)) or {}
    out = {k: int(v or 0) for k, v in r.items()}
    sent = out.get("total", 0) - out.get("withdrawn", 0)
    out["sent"] = sent
    out["missing"] = out.get("not_returned", 0)          # Crexi didn't have it (Not Matched or left out)
    out["matched_pct"] = round(out.get("returned", 0) / sent, 4) if sent else None
    return out


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict:
    b = one(conn, "SELECT * FROM crexi_batches WHERE id = ?", (int(batch_id),))
    if not b:
        raise KeyError(f"Crexi batch {batch_id} was not found.")
    b["markets"] = loads(b.get("markets"), []) or []
    b["column_overrides"] = loads(b.get("column_overrides"), None)
    return b


def open_batches(conn: sqlite3.Connection) -> list[dict]:
    """Batches that still have properties waiting at Crexi (any period), oldest first."""
    out = []
    for b in rows(conn, "SELECT id FROM crexi_batches WHERE status != 'closed' ORDER BY id"):
        full = get_batch(conn, b["id"])
        counts = batch_counts(conn, b["id"])
        if counts["waiting"]:
            out.append({**full, **counts})
    return out


def returned_text(c: dict) -> str:
    """'Crexi returned 450 of 500. 50 missing.' for one batch (or totals with the same keys)."""
    return (f"Crexi returned {c['returned']:,} of {c['sent']:,}. {c['missing']:,} missing."
            + (f" {c['waiting']:,} still waiting." if c.get("waiting") else ""))


def credits_left_text(st: dict) -> str:
    """'You have 50 credits left until Nov 28.'"""
    n = st["left"]
    return f"You have {n:,} credit{'s' if n != 1 else ''} left until {_short_day(st['period_end'])}."


def _short_day(iso: str | None) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso or "")
    return f"{d.strftime('%b')} {d.day}"


def topup_suggestion(conn: sqlite3.Connection, st: dict) -> dict:
    """What to do next, in plain English: {suggested, message, batch_id, credits_text, last}. 'suggested' is the
    number to export next (every credit left)."""
    latest = one(conn, "SELECT id FROM crexi_batches WHERE period = ? AND status = 'closed' AND "
                       "(note IS NULL OR note NOT LIKE '%Cancelled%') ORDER BY id DESC LIMIT 1", (st["period"],))
    waiting = open_batches(conn)
    last = None
    parts = []
    if latest:
        c = batch_counts(conn, latest["id"])
        if c["sent"]:
            last = {"batch_id": latest["id"], **c, "text": returned_text(c)}
            parts.append(f"Batch #{latest['id']}: {returned_text(c)}")
    if waiting:
        b = max(waiting, key=lambda x: x["id"])
        parts.append(f"Batch #{b['id']}: {b['waiting']:,} waiting at Crexi. Import Crexi's file when it comes back.")
        older = [o for o in waiting if o.get("period") != st["period"]]
        if older and b.get("period") == st["period"]:
            o = max(older, key=lambda x: x["id"])
            parts.append(f"Batch #{o['id']} from an earlier credit period still has {o['waiting']:,} waiting; it "
                         f"does not use this period's credits.")
        elif b.get("period") != st["period"]:
            parts[-1] += " It is from an earlier credit period, so it does not use this period's credits."
    left = st["left"]
    credits = credits_left_text(st)
    if left:
        parts.append(f"{credits} Export the next {left:,}.")
    else:
        parts.append(f"No credits are free this period. New ones arrive on {_short_day(st['next_reset'])}.")
    return {"suggested": left, "batch_id": (waiting[-1]["id"] if waiting else (latest or {}).get("id")),
            "message": " ".join(parts), "credits_text": credits, "last": last}


def _finish_legacy(conn: sqlite3.Connection, settings: Settings) -> None:
    """Batches imported before the close step was retired ('Partly back'): a reply was imported, so they are
    finished now, the same way an import finishes a batch today."""
    ids = [int(r["id"]) for r in rows(conn, "SELECT id FROM crexi_batches WHERE status = 'imported'")]
    if ids:
        finish_batches(conn, ids, settings=settings, actor="auto")


def status(conn: sqlite3.Connection, *, today: date | None = None, settings: Settings | None = None) -> dict:
    """The Credits screen's top: the credit period, returned / in flight / free, days left, open batches and what
    to do next."""
    s = settings or get_settings()
    _finish_legacy(conn, s)
    st = drill_credits.credit_status(conn, today=today, settings=s)
    st["per_period"] = st["per_month"]
    return {"status": st, "open_batches": open_batches(conn), "topup": topup_suggestion(conn, st),
            "default_count": st["left"]}


# --- picking ----------------------------------------------------------------------------------------------

@dataclass
class Pick:
    items: list[dict] = field(default_factory=list)       # property rows + tier / reason / is_retry, in order
    wanted: int = 0
    left_out: dict[str, int] = field(default_factory=dict)  # why properties were left out (counts)
    duplicates_left_out: int = 0
    short: int = 0                                          # wanted but nobody qualifies


def _period_sent_keys(conn: sqlite3.Connection, period: str) -> tuple[set[int], set[str]]:
    """(property ids sent in this period and not withdrawn, duplicate keys of every property already paid for or
    waiting at Crexi in any period). A second record of a building whose credit was spent in an earlier period
    (returned, used by hand, or still waiting) is never sent again."""
    ids: set[int] = set()
    keys: set[str] = set()
    for r in rows(conn, "SELECT p.* FROM crexi_batch_items i JOIN crexi_batches b ON b.id = i.batch_id "
                        "JOIN properties p ON p.id = i.property_id "
                        "WHERE b.period = ? AND i.status IN ('sent', 'returned')", (period,)):
        ids.add(int(r["id"]))
        keys.update(duplicate_keys(r))
    for r in rows(conn, "SELECT p.* FROM properties p WHERE p.id IN "
                        "(SELECT property_id FROM crexi_batch_items WHERE status IN ('sent', 'returned')) "
                        "OR p.id IN (SELECT property_id FROM credit_ledger WHERE property_id IS NOT NULL)"):
        keys.update(duplicate_keys(r))
    return ids, keys


def _retry_candidates(conn: sqlite3.Connection, cfg: E.DrillConfig, sent_ids: set[int]) -> list[dict]:
    """Properties whose last batch row was never returned, still open and not set aside by hand, best score first."""
    ids = [int(r["property_id"]) for r in rows(conn, """
        SELECT i.property_id FROM crexi_batch_items i
        WHERE i.status = 'not_returned'
          AND i.property_id NOT IN (SELECT property_id FROM crexi_batch_items WHERE status IN ('sent', 'returned'))
          AND i.property_id NOT IN (SELECT property_id FROM credit_ledger WHERE property_id IS NOT NULL)
        GROUP BY i.property_id""")]
    out = []
    for ctx in E.load_contexts(conn, property_ids=ids) if ids else []:
        ev = E.evaluate(ctx, cfg)
        if ev.status != "active" or ev.level >= 3 or ctx.id in sent_ids:
            continue
        reasons = drill_credits._exclusions(ctx)
        if MANUAL_REASON in reasons:
            continue
        if ctx.in_buybox is False:
            _, hard = drill_credits._fallbacks(None)
            if hard & set(reasons):
                continue
        out.append({"id": ctx.id, "market_slug": ctx.prop.get("market_slug"), "tier": "retry",
                    "score": ctx.score_value or 0.0})
    out.sort(key=lambda c: (-c["score"], c["id"]))
    return out


def pick(conn: sqlite3.Connection, n: int, *, today: date | None = None, settings: Settings | None = None,
         retry: bool = False) -> Pick:
    """The next `n` properties for Crexi: highest value first (the credit plan's order: buy box first, free
    stages tried first, then score x Crexi's hit rate for the owner type, then the fallbacks), never one already
    sent this period, never the same property twice (Crexi link, then parcel number, then address), never
    do-not-contact, confirmed or set-aside-by-hand owners."""
    s = settings or get_settings()
    st = drill_credits.credit_status(conn, today=today, settings=s)
    cfg = E.drill_config(s)
    sent_ids, seen = _period_sent_keys(conn, st["period"])
    picked: list[dict] = []
    ids: set[int] = set()
    dup_ids: set[int] = set()
    base = {"already_sent_this_period": len(sent_ids)}

    def take(p: dict, tier: str, is_retry: bool) -> bool:
        if p["id"] in ids or p["id"] in sent_ids:
            return False
        keys = duplicate_keys(p)
        if any(k in seen for k in keys):
            dup_ids.add(p["id"])
            return False
        seen.update(keys)
        ids.add(p["id"])
        picked.append({**p, "tier": tier, "is_retry": is_retry})
        return True

    def prop_row(pid: int) -> dict:
        r = one(conn, "SELECT p.*, m.slug AS market_slug FROM properties p JOIN markets m ON m.id = p.market_id "
                      "WHERE p.id = ?", (pid,))
        return r or {}

    if retry:
        for c in _retry_candidates(conn, cfg, sent_ids):
            if len(picked) >= n:
                break
            take(prop_row(c["id"]), "retry", True)
    want = max(n - len(picked), 0)
    asked = want
    while want > 0:
        sched = drill_credits._schedule(conn, today=today, settings=s, want=asked)
        for c in sched.plan:
            if len(picked) >= n:
                break
            pid = int(c["ctx"].id)
            if pid in ids or pid in sent_ids:
                continue
            p = prop_row(pid)
            p["reason"] = None
            take(p, c["tier"], False)
        if len(picked) >= n or len(sched.plan) < asked:
            break                      # enough, or the plan has nobody else
        asked = asked + (n - len(picked)) * 2 + 10
    out = Pick(items=picked[:n], wanted=n, duplicates_left_out=len(dup_ids), left_out=base)
    # the rest of this period's sent rows: the ones Crexi didn't have (left out unless second pass is ticked)
    missing = {int(r["property_id"]) for r in rows(
        conn, "SELECT DISTINCT i.property_id FROM crexi_batch_items i JOIN crexi_batches b ON b.id = i.batch_id "
              "WHERE b.period = ? AND i.status = 'not_returned'", (st["period"],))}
    out.left_out["missing_this_period"] = len(missing - sent_ids - {int(it["id"]) for it in out.items})
    out.short = max(0, n - len(out.items))
    out.left_out.update(_other_left_out(conn))
    return out


def _other_left_out(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "do_not_contact": int(scalar(conn, "SELECT COUNT(*) FROM drill_state WHERE status = 'dnc'") or 0),
        "confirmed_owner": int(scalar(conn, "SELECT COUNT(*) FROM drill_state WHERE status = 'confirmed'") or 0),
        "set_aside_by_hand": int(scalar(conn, "SELECT COUNT(*) FROM scores WHERE exclusion_reasons LIKE ?",
                                        (f'%"{MANUAL_REASON}"%',)) or 0),
    }


def _check_count(conn: sqlite3.Connection, n: int | None, st: dict) -> int:
    free = st["left"]
    if n is None:
        n = free
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise CrexiError("Type a whole number of properties to send.") from None
    if n < 1:
        raise CrexiError("Type a number of properties to send (1 or more).")
    if n > free:
        raise CrexiError(
            f"Only {free:,} credits are free this period ({st['returned']:,} returned, {st['in_flight']:,} waiting "
            f"at Crexi, {st['per_month']:,} in all), so I can't send {n:,}. Send {free:,} or fewer, or import "
            f"Crexi's file first: the rows it didn't have free their credits at once." if st["in_flight"] else
            f"Only {free:,} credits are free this period ({st['returned']:,} of {st['per_month']:,} are used), so "
            f"I can't send {n:,}. Send {free:,} or fewer.")
    return n


def preview_export(conn: sqlite3.Connection, n: int | None = None, *, today: date | None = None,
                   settings: Settings | None = None, retry: bool = False, sample: int = 15) -> dict:
    """Who would be in the next batch, without writing anything: counts by reason, the duplicates left out, and
    the first few addresses."""
    s = settings or get_settings()
    st = drill_credits.credit_status(conn, today=today, settings=s)
    n = _check_count(conn, n, st)
    pk = pick(conn, n, today=today, settings=s, retry=retry)
    by_tier: dict[str, int] = {}
    by_market: dict[str, int] = {}
    for it in pk.items:
        by_tier[it["tier"]] = by_tier.get(it["tier"], 0) + 1
        by_market[it["market_slug"]] = by_market.get(it["market_slug"], 0) + 1
    names = {r["slug"]: r["name"] for r in rows(conn, "SELECT slug, name FROM markets")}
    reasons = [{"key": k, "label": TIER_TEXT.get(k, k), "count": v} for k, v in by_tier.items()]
    left_out = pk.left_out
    parts = []
    if pk.duplicates_left_out:
        parts.append(f"{pk.duplicates_left_out:,} duplicate{'s' if pk.duplicates_left_out != 1 else ''} left out "
                     f"(same Crexi link, parcel number or address as one already in the list or already sent to Crexi)")
    if left_out.get("already_sent_this_period"):
        parts.append(f"{left_out['already_sent_this_period']:,} Crexi already returned (or is still looking up) "
                     f"this period")
    if left_out.get("missing_this_period"):
        parts.append(f"{left_out['missing_this_period']:,} Crexi didn't have this period"
                     + ("" if retry else " (tick second pass to send them again)"))
    if left_out.get("do_not_contact"):
        parts.append(f"{left_out['do_not_contact']:,} do-not-contact owners")
    if left_out.get("confirmed_owner"):
        parts.append(f"{left_out['confirmed_owner']:,} confirmed owners")
    if left_out.get("set_aside_by_hand"):
        parts.append(f"{left_out['set_aside_by_hand']:,} set aside by hand")
    text = (f"{len(pk.items):,} {'property' if len(pk.items) == 1 else 'properties'} would go to Crexi"
            + (f" ({pk.short:,} fewer than the {n:,} you asked for: nobody else qualifies yet)" if pk.short else "")
            + ".")
    return {"count": len(pk.items), "requested": n, "short": pk.short, "free": st["left"], "period": st["period"],
            "reasons": reasons, "by_market": [{"slug": k, "name": names.get(k, k), "count": v}
                                               for k, v in sorted(by_market.items(), key=lambda kv: -kv[1])],
            "duplicates_left_out": pk.duplicates_left_out, "left_out": left_out, "left_out_text": "; ".join(parts),
            "message": text,
            "sample": [{"property_id": it["id"], "address": it.get("address"), "city": it.get("city"),
                        "market": it["market_slug"], "tier": it["tier"], "second_pass": it["is_retry"]}
                       for it in pk.items[:sample]]}


# --- export -----------------------------------------------------------------------------------------------

def _export_rows(items: list[dict], batch_id: int) -> list[list[Any]]:
    out = []
    for pos, it in enumerate(items, start=1):
        rec = {**it, "batch_id": batch_id, "position": pos}
        out.append([safe_cell(rec.get(src)) for _, src in EXPORT_COLUMNS])
    return out


def write_export(path: Path, items: list[dict], batch_id: int) -> Path:
    """Write the upload file (atomic: a half-written file never appears)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    try:
        with tmp.open("w", newline="", encoding=CSV_ENCODING) as f:
            w = csv.writer(f)
            w.writerow([h for h, _ in EXPORT_COLUMNS])
            w.writerows(_export_rows(items, batch_id))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


_export_lock = threading.Lock()   # one export at a time in this program (the web app runs requests in threads)


def export_batch(conn: sqlite3.Connection, n: int | None = None, *, created_by: str = "cli",
                 today: date | None = None, settings: Settings | None = None, retry: bool = False,
                 note: str | None = None) -> dict:
    """Pick the next `n` (default: every free credit), save the batch, write the file, and count the rows as
    waiting at Crexi. Returns {batch, file, count, preview...}.

    Two exports at once (two people, or the command line and the web app) never share the free credits: one
    waits for the other, and the free count and the picked properties are checked again inside the database's
    write lock before anything is saved."""
    with _export_lock:
        return _export_batch(conn, n, created_by=created_by, today=today, settings=settings, retry=retry,
                             note=note)


def _recheck_before_insert(conn: sqlite3.Connection, items: list[dict], *, today: date | None,
                           settings: Settings) -> None:
    """Inside the write lock: still enough free credits, and none of the picked properties already sent."""
    st = drill_credits.credit_status(conn, today=today, settings=settings)
    if len(items) > st["left"]:
        raise CrexiError(f"Another export just used some of the free credits: only {st['left']:,} are free now, so "
                         f"I can't send {len(items):,}. Nothing was saved. Look at the Credits screen and try again.")
    ids = [int(it["id"]) for it in items]
    taken = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        taken += int(scalar(conn, "SELECT COUNT(DISTINCT property_id) FROM crexi_batch_items WHERE status IN "
                                  f"('sent', 'returned') AND property_id IN ({','.join('?' * len(chunk))})",
                            tuple(chunk)) or 0)
    if taken:
        raise CrexiError(f"Another export just sent {taken:,} of these properties to Crexi. Nothing was saved, so "
                         f"no credit is spent twice. Look at the Credits screen and try again.")


def _export_batch(conn: sqlite3.Connection, n: int | None, *, created_by: str, today: date | None,
                  settings: Settings | None, retry: bool, note: str | None) -> dict:
    s = settings or get_settings()
    st = drill_credits.credit_status(conn, today=today, settings=s)
    n = _check_count(conn, n, st)
    pk = pick(conn, n, today=today, settings=s, retry=retry)
    if not pk.items:
        raise CrexiError("Nobody qualifies for a Crexi credit right now, so there is nothing to export. Import a "
                         "newer Crexi list or work the DRILL to add owners.")
    period = st["period"]
    slugs = sorted({it["market_slug"] for it in pk.items})
    market_id = one(conn, "SELECT id FROM markets WHERE slug = ?", (slugs[0],))["id"] if len(slugs) == 1 else None
    stamp = now_iso()
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")      # the database's write lock: covers a second program exporting too
        _recheck_before_insert(conn, pk.items, today=today, settings=s)
        first = int(scalar(conn, "SELECT COUNT(*) FROM crexi_batch_items i JOIN crexi_batches b ON b.id = "
                                 "i.batch_id WHERE b.period = ? AND i.status != 'withdrawn'", (period,)) or 0) == 0
        cur = conn.execute(
            "INSERT INTO crexi_batches(market_id, markets, period, kind, requested, created_at, created_by, status, note) "
            "VALUES(?,?,?,?,?,?,?, 'open', ?)",
            (market_id, dumps(slugs), period, "first" if first else "topup", len(pk.items), stamp,
             (created_by or "cli")[:320], clean_str(note)))
        batch_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO crexi_batch_items(batch_id, property_id, position, sent_at, status, is_retry) "
            "VALUES(?,?,?,?, 'sent', ?)",
            [(batch_id, it["id"], pos, stamp, int(it["is_retry"])) for pos, it in enumerate(pk.items, start=1)])
        rel = f"crexi/{period}/batch-{batch_id}-{len(pk.items)}.csv"
        write_export(output_dir(s) / rel, pk.items, batch_id)
        conn.execute("UPDATE crexi_batches SET file_name = ? WHERE id = ?", (rel, batch_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    after = drill_credits.credit_status(conn, today=today, settings=s)
    return {"batch": get_batch(conn, batch_id), "batch_id": batch_id, "file": rel, "count": len(pk.items),
            "kind": "first" if first else "topup", "short": pk.short, "duplicates_left_out": pk.duplicates_left_out,
            "status": after,
            "message": (f"Batch #{batch_id}: {len(pk.items):,} properties are ready in {rel}. Upload that file to "
                        f"Crexi's bulk tool, then import the file Crexi sends back. {after['left']:,} credits are "
                        f"still free.")}


def export_file(conn: sqlite3.Connection, batch_id: int, settings: Settings | None = None) -> Path:
    """The batch's export file, looked up by batch id (never by a name from the browser), and checked to be
    inside the output folder."""
    s = settings or get_settings()
    b = get_batch(conn, batch_id)
    rel = b.get("file_name")
    if not rel:
        raise KeyError(f"Batch {batch_id} has no file.")
    root = output_dir(s).resolve()
    target = (root / rel).resolve()
    if root not in target.parents or not target.is_file():
        raise KeyError("That export file is no longer on this computer.")
    return target


# --- finish and cancel ------------------------------------------------------------------------------------

def _mark_nothing(conn: sqlite3.Connection, property_ids: list[int], detail: str, cfg: E.DrillConfig) -> int:
    """DRILL stage 'Crexi credit' = nothing, for properties Crexi never returned (no credit is counted)."""
    if E.CREDIT_STAGE not in cfg.by_key:
        return 0
    n = 0
    with E._tx(conn):
        for pid in property_ids:
            E._ensure_state_row(conn, pid)
            E._insert_attempt(conn, pid, E.CREDIT_STAGE, "nothing", detail, "you")
            E._refresh(conn, pid, cfg)
            n += 1
    return n


def finish_batches(conn: sqlite3.Connection, batch_ids: list[int], *, settings: Settings | None = None,
                   actor: str = "auto") -> dict[int, dict]:
    """Finish batches after Crexi's reply: every row still waiting is 'missing' (Crexi didn't have it: it
    answered 'Not Matched' or left the row out). Missing rows cost no credit, so their credits are free again
    at once, and the DRILL's Crexi stage records 'nothing' right away, so the DRILL moves those owners on to its
    other sources. Returns {batch id: counts after}."""
    s = settings or get_settings()
    cfg = E.drill_config(s)
    out: dict[int, dict] = {}
    stamp = now_iso()
    for bid in sorted(set(int(b) for b in batch_ids)):
        b = get_batch(conn, bid)
        waiting = rows(conn, "SELECT property_id, not_matched_at FROM crexi_batch_items "
                             "WHERE batch_id = ? AND status = 'sent'", (bid,))
        conn.execute("UPDATE crexi_batch_items SET status = 'not_returned' WHERE batch_id = ? AND status = 'sent'",
                     (bid,))
        if b["status"] != "closed":
            conn.execute("UPDATE crexi_batches SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?",
                         (stamp, (actor or "auto")[:320], bid))
        conn.commit()
        for nm in (True, False):
            ids = [int(r["property_id"]) for r in waiting if bool(r["not_matched_at"]) == nm]
            why = "answered 'Not Matched'" if nm else "left it out of its reply"
            _mark_nothing(conn, ids, f"Crexi batch #{bid}: Crexi didn't have it ({why}). No credit was spent; "
                                     f"the DRILL moves on to the other sources.", cfg)
        out[bid] = batch_counts(conn, bid)
    return out


MISSING_HEADERS = ("Batch", "Address", "City", "State", "ZIP", "County", "APN", "Owner", "Why missing", "Property ID")


def missing_rows(conn: sqlite3.Connection, batch_ids: list[int]) -> list[dict]:
    """The missing rows of these batches (Crexi didn't have them), batch by batch in file order."""
    if not batch_ids:
        return []
    mark = ",".join("?" for _ in batch_ids)
    out = []
    for r in rows(conn, f"""
            SELECT i.batch_id, i.position, i.not_matched_at, p.id AS property_id, p.address, p.city, p.state, p.zip,
                   p.county, p.apn, p.owner_name
            FROM crexi_batch_items i JOIN properties p ON p.id = i.property_id
            WHERE i.batch_id IN ({mark}) AND i.status = 'not_returned' ORDER BY i.batch_id, i.position""",
            [int(b) for b in batch_ids]):
        out.append({"Batch": r["batch_id"], "Address": r["address"], "City": r["city"], "State": r["state"],
                    "ZIP": r["zip"], "County": r["county"], "APN": r["apn"], "Owner": r["owner_name"],
                    "Why missing": ("Crexi answered 'Not Matched'" if r["not_matched_at"]
                                    else "Not in Crexi's file"),
                    "Property ID": r["property_id"]})
    return out


def write_missing(conn: sqlite3.Connection, batch_ids: list[int], rel: str,
                  settings: Settings | None = None) -> str | None:
    """Write the missing list (address, owner, why) to output/<rel>; None when nothing is missing."""
    found = missing_rows(conn, batch_ids)
    if not found:
        return None
    path = output_dir(settings) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    try:
        with tmp.open("w", newline="", encoding=CSV_ENCODING) as f:
            w = csv.writer(f)
            w.writerow(MISSING_HEADERS)
            for r in found:
                w.writerow([safe_cell(r[h]) for h in MISSING_HEADERS])
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return rel


def missing_file(conn: sqlite3.Connection, batch_id: int, settings: Settings | None = None) -> Path:
    """One batch's missing list, written fresh (so a late return drops off it). KeyError when none are missing."""
    s = settings or get_settings()
    b = get_batch(conn, batch_id)
    rel = write_missing(conn, [batch_id], f"crexi/{b['period']}/missing-batch-{batch_id}.csv", s)
    if not rel:
        raise KeyError(f"Nothing is missing from batch {batch_id}.")
    return output_dir(s) / rel


def withdraw_batch(conn: sqlite3.Connection, batch_id: int, *, confirm: bool = False, actor: str = "cli",
                   settings: Settings | None = None) -> dict:
    """Cancel a batch that was never uploaded to Crexi: its rows are 'withdrawn' (they count nowhere and the
    DRILL is not told anything). Not allowed once any row has come back."""
    s = settings or get_settings()
    b = get_batch(conn, batch_id)
    counts = batch_counts(conn, batch_id)
    if counts["returned"]:
        raise CrexiError(f"Batch #{batch_id} already has {counts['returned']:,} rows back from Crexi, so it can't be "
                         f"cancelled.")
    if b["status"] == "closed":
        raise CrexiError(f"Batch #{batch_id} is already finished.")
    text = (f"Cancel batch #{batch_id}? Use this only if you never uploaded the file to Crexi. Its {counts['waiting']:,} "
            f"properties go back to the pool and their credits are free again.")
    if not confirm:
        return {"ok": True, "withdrawn": False, "needs_confirm": True, "message": text}
    conn.execute("UPDATE crexi_batch_items SET status = 'withdrawn' WHERE batch_id = ? AND status = 'sent'", (batch_id,))
    conn.execute("UPDATE crexi_batches SET status = 'closed', closed_at = ?, closed_by = ?, "
                 "note = COALESCE(note || ' ', '') || 'Cancelled: never uploaded to Crexi.' WHERE id = ?",
                 (now_iso(), (actor or "cli")[:320], batch_id))
    conn.commit()
    st = drill_credits.credit_status(conn, settings=s)
    return {"ok": True, "withdrawn": True, "needs_confirm": False, "status": st,
            "message": f"Batch #{batch_id} is cancelled. {st['left']:,} credits are free."}


# --- history ----------------------------------------------------------------------------------------------

def _batch_row(conn: sqlite3.Connection, b: dict) -> dict:
    counts = batch_counts(conn, b["id"])
    imports = []
    for r in rows(conn, "SELECT id, batch_ids, file_name, rows_total, returned, found, owner_only, no_data, "
                        "not_matched, duplicates, unmatched, contacts_added, summary_json, created_at, created_by "
                        "FROM crexi_batch_imports ORDER BY id DESC"):
        if b["id"] in (loads(r.pop("batch_ids"), []) or []):
            r["summary"] = loads(r.pop("summary_json"), {}) or {}
            imports.append(r)
    kind = {"first": "First export", "topup": "Top-up", "manual": "By hand"}.get(b["kind"], b["kind"])
    return {**b, **counts, "kind_label": kind, "created_day": local_day(b["created_at"]),
            "has_file": bool(b.get("file_name")), "imports": imports,
            "enrichment": _batch_enrichment(imports)}


def _batch_enrichment(imports: list[dict]) -> dict:
    total: dict[str, int] = {}
    for imp in imports:
        for k, v in ((imp.get("summary") or {}).get("enrichment") or {}).items():
            total[k] = total.get(k, 0) + int(v or 0)
    return total


def history(conn: sqlite3.Connection, *, settings: Settings | None = None) -> dict:
    """Every credit period, newest first: its batches (when, who, sent, returned, not returned, matched %, status,
    the file), credits used by hand, and totals. The current period is always listed."""
    s = settings or get_settings()
    current = P.period_key(None, s)
    keys = {r["period"] for r in rows(conn, "SELECT DISTINCT period FROM crexi_batches")} | {current}
    hand: dict[str, int] = {}
    for r in rows(conn, "SELECT period, date(created_at, 'localtime') AS day FROM credit_ledger WHERE batch_id IS NULL"):
        k = r["period"] or P.period_key(date.fromisoformat(r["day"]), s)
        hand[k] = hand.get(k, 0) + 1
    keys |= set(hand)
    periods = []
    per = drill_credits.credits_per_month(s)
    for k in sorted(keys, reverse=True):
        batches = [_batch_row(conn, get_batch(conn, r["id"])) for r in rows(
            conn, "SELECT id FROM crexi_batches WHERE period = ? ORDER BY id DESC", (k,))]
        sent = sum(b["sent"] for b in batches)
        returned = sum(b["returned"] for b in batches)
        enrich = _batch_enrichment(list({imp["id"]: imp for b in batches for imp in b["imports"]}.values()))
        periods.append({
            "period": k, "label": P.label(k, s), "current": k == current, "per_period": per, "batches": batches,
            "sent": sent, "returned": returned, "by_hand": hand.get(k, 0), "waiting": sum(b["waiting"] for b in batches),
            "not_returned": sum(b["not_returned"] for b in batches),
            "missing": sum(b["missing"] for b in batches),
            "found": sum(b["found"] for b in batches), "owner_only": sum(b["owner_only"] for b in batches),
            "no_data": sum(b["no_data"] for b in batches), "second_passes": sum(b["second_pass"] for b in batches),
            "credits_used": returned + hand.get(k, 0),
            "matched_pct": round(returned / sent, 4) if sent else None, "enrichment": enrich})
    return {"periods": periods, "current": current}


def batch_items(conn: sqlite3.Connection, batch_id: int, *, limit: int = 100, offset: int = 0) -> dict:
    """The properties of one batch with what came of each (no owner names)."""
    b = get_batch(conn, batch_id)
    items = rows(conn, """
        SELECT i.position, i.property_id, i.status, i.result, i.phones, i.emails, i.is_retry, i.sent_at, i.returned_at,
               i.not_matched_at, p.address, p.city, p.state
        FROM crexi_batch_items i JOIN properties p ON p.id = i.property_id
        WHERE i.batch_id = ? ORDER BY i.position LIMIT ? OFFSET ?""", (batch_id, max(1, min(int(limit), 500)),
                                                                          max(0, int(offset))))
    return {"batch": b, "counts": batch_counts(conn, batch_id), "items": items}


# --- one property -----------------------------------------------------------------------------------------

def property_history(conn: sqlite3.Connection, property_id: int) -> dict:
    """Everything Crexi did for one property, newest first, as plain sentences for the drawer and the timeline:
    {state: 'data'|'none'|'waiting'|'not_sent', events:[{at, batch_id, text, kind}], text}."""
    events = []
    for r in rows(conn, """
            SELECT i.*, b.period, b.kind FROM crexi_batch_items i JOIN crexi_batches b ON b.id = i.batch_id
            WHERE i.property_id = ? AND i.status != 'withdrawn' ORDER BY i.batch_id DESC""", (property_id,)):
        events.append({"at": r["returned_at"] or r["sent_at"], "batch_id": r["batch_id"], "status": r["status"],
                       "result": r["result"], "kind": "batch", "text": _item_text(r)})
    for r in rows(conn, "SELECT result, created_at FROM credit_ledger WHERE property_id = ? AND batch_id IS NULL "
                        "ORDER BY id DESC", (property_id,)):
        word = {"found": "found contact info", "nothing": "found nothing"}.get(r["result"], "result not recorded yet")
        events.append({"at": r["created_at"], "batch_id": None, "status": "returned", "result": r["result"],
                       "kind": "hand", "text": f"Crexi: used a credit by hand on {local_day(r['created_at'])}, {word}."})
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    if any(e["status"] == "returned" and e["result"] in ("found", "owner_only") for e in events):
        state = "data"
    elif any(e["status"] == "sent" for e in events):
        state = "waiting"
    elif any(e["status"] in ("returned", "not_returned") for e in events):
        state = "none"
    else:
        state = "not_sent"
    return {"state": state, "events": events, "text": events[0]["text"] if events else "Not sent to Crexi yet."}


def _item_text(r: dict) -> str:
    head = f"Crexi: sent in batch #{r['batch_id']} on {local_day(r['sent_at'])}"
    if r.get("is_retry"):
        head += " (second pass)"
    if r["status"] == "sent":
        return head + ", waiting for Crexi."
    if r["status"] == "not_returned":
        why = "it answered 'Not Matched'" if r.get("not_matched_at") else "it was not in Crexi's file"
        return head + f"; missing: Crexi didn't have it ({why}). No credit was spent; the DRILL moved on to the " \
                      "other sources."
    got = {"found": "found " + (" and ".join(
        x for x in (f"{r['phones']} phone{'s' if r['phones'] != 1 else ''}" if r["phones"] else "",
                    f"{r['emails']} email{'s' if r['emails'] != 1 else ''}" if r["emails"] else "") if x) or "contact info"),
           "owner_only": "returned an owner name but no phone or email",
           "nothing": "returned with no owner or contact info"}.get(r["result"], "returned")
    return head + f", returned {local_day(r['returned_at'])}, {got}."


# --- the DRILL screen's Crexi box ---------------------------------------------------------------------------

def drill_box(conn: sqlite3.Connection, market_slug: str | None = None) -> dict:
    """For the DRILL screen: what Crexi did for this market's owners, every batch so far, one count per
    property (its latest batch row): sent, returned, missing (Crexi didn't have it), waiting, penetration
    (returned of returned + missing), and where the missing ones are now in the DRILL: still being worked by the
    other sources (active), every source tried (exhausted), confirmed, or do not contact."""
    where, args = "", []
    if market_slug:
        where, args = "AND p.market_id = (SELECT id FROM markets WHERE slug = ?)", [market_slug]
    latest = rows(conn, f"""
        SELECT i.property_id, i.status, COALESCE(d.status, 'active') AS drill_status
        FROM crexi_batch_items i JOIN properties p ON p.id = i.property_id
        LEFT JOIN drill_state d ON d.property_id = i.property_id
        WHERE i.status != 'withdrawn' {where}
          AND i.batch_id = (SELECT MAX(j.batch_id) FROM crexi_batch_items j
                            WHERE j.property_id = i.property_id AND j.status != 'withdrawn')""", args)

    def count(st: str) -> int:
        return sum(1 for r in latest if r["status"] == st)

    returned, missing, waiting = count("returned"), count("not_returned"), count("sent")
    after = {k: sum(1 for r in latest if r["status"] == "not_returned" and r["drill_status"] == k)
             for k in ("active", "exhausted", "confirmed", "dnc")}
    done = returned + missing
    pct = round(returned / done, 4) if done else None
    if not latest:
        text = "Nothing sent to Crexi yet."
    else:
        text = (f"{len(latest):,} sent, {returned:,} returned, {missing:,} missing"
                + (f" ({round(pct * 100)}% returned)" if pct is not None else "")
                + (f", {waiting:,} waiting at Crexi" if waiting else "") + ".")
        if missing:
            text += (f" {after['active']:,} of the {missing:,} missing are being worked by the DRILL's other "
                     f"sources now (county records, AI research...).")
    return {"sent": len(latest), "returned": returned, "missing": missing, "waiting": waiting,
            "penetration": pct, "missing_working": after["active"], "missing_exhausted": after["exhausted"],
            "missing_confirmed": after["confirmed"], "missing_dnc": after["dnc"], "text": text}
