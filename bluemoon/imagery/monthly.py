"""The monthly photo goal: photos, then the AI's first look, a batch at a time.

Seth wants every month's first 600 properties (a setting) to have Street View photos and an AI first pass,
with an easy "Keep going" button. This module is the glue; the paid work is still done by
`streetview.fetch_imagery` (photos) and `vision.classify_properties` (the AI check), so every safety rule
stays: dry-run estimate first, an explicit OK over `safety.confirm_over_properties`, the Google caps per run,
and cached answers that are never paid for twice.

- `photos:` in config.yaml (`monthly_target: 600`, `batch: 100`); put your own numbers in config.local.yaml.
- The **photo pool** is priority-ordered: buy-box properties with no photos yet, best Blue Moon rank first,
  then the rest of the properties, ranked the same way (`photo_pool`).
- Each time photos are pulled from Google a row goes into `photo_pulls`; "412 of 600 done this month" counts
  the properties pulled in the current calendar month (`status`). Photos pulled before this table existed are
  counted once, from the files' dates (`backfill`).
- `run_batch`: photos for the next `batch` properties, then the AI check for them, in one job.
- `run_keep_going`: batch after batch until the month's goal is reached (one more batch when it already is),
  stopping at the caps, at a problem, or when nothing is left.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import Settings, get_settings
from ..db import market_by_slug, now_iso, rows, scalar
from . import streetview
from .vision import classify_estimate, classify_properties, get_backend, vision_config

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

BACKFILL_KEY = "photo_pulls_backfilled"


# --- settings ---------------------------------------------------------------------------------

class PhotosConfig(BaseModel):
    """`photos:` in config.yaml (your own numbers go in config.local.yaml)."""
    model_config = ConfigDict(extra="ignore")
    monthly_target: int = Field(600, ge=1)     # properties that should have photos each calendar month
    batch: int = Field(100, ge=1)              # properties per "Do the next batch" click


def photos_config(settings: Settings | None = None) -> PhotosConfig:
    s = settings or get_settings()
    raw = {k: v for k, v in dict(s.section("photos")).items() if v is not None}
    try:
        return PhotosConfig(**raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"config.yaml photos setting '{where}' has a value I can't use: {first.get('msg')}") from None


# --- counting this month ----------------------------------------------------------------------

def month_label(now: datetime | None = None) -> str:
    return (now or datetime.now().astimezone()).strftime("%B %Y")


def _this_month() -> str:
    return datetime.now().astimezone().strftime("%Y-%m")


def backfill(conn: sqlite3.Connection, market_slug: str, settings: Settings | None = None) -> int:
    """Count photos pulled before `photo_pulls` existed, once per market: each property with saved photos and
    no pull on record gets one, dated by its newest photo file. Returns how many were added."""
    s = settings or get_settings()
    market = market_by_slug(conn, market_slug)
    key = f"{BACKFILL_KEY}:{market['id']}"
    if scalar(conn, "SELECT value FROM meta WHERE key = ?", (key,)):
        return 0
    added = 0
    found = rows(conn, "SELECT v.property_id AS id FROM visual_checks v JOIN properties p ON p.id = v.property_id "
                       "WHERE p.market_id = ? AND v.status IN ('done', 'no_imagery') AND NOT EXISTS "
                       "(SELECT 1 FROM photo_pulls x WHERE x.property_id = v.property_id)", (market["id"],))
    with conn:
        for r in found:
            folder = streetview.property_dir(s, r["id"])
            stamps = [(folder / n).stat().st_mtime for n in streetview.existing_images(s, r["id"])
                      if not n.startswith(streetview.YOURS_PREFIX)]
            if not stamps:
                continue
            when = datetime.fromtimestamp(max(stamps), UTC).replace(microsecond=0).isoformat()
            conn.execute("INSERT INTO photo_pulls(property_id, pulled_at) VALUES(?, ?)", (r["id"], when))
            added += 1
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, now_iso()))
    return added


def done_this_month(conn: sqlite3.Connection, market_slug: str) -> int:
    """Properties of this market whose photos were pulled in the current calendar month."""
    market = market_by_slug(conn, market_slug)
    return int(scalar(conn, "SELECT COUNT(DISTINCT x.property_id) FROM photo_pulls x "
                            "JOIN properties p ON p.id = x.property_id "
                            "WHERE p.market_id = ? AND strftime('%Y-%m', x.pulled_at, 'localtime') = ?",
                      (market["id"], _this_month())) or 0)


_POOL_SQL = """
SELECT p.id, p.address, p.city, s.in_buybox, s.rank, s.score, v.status
FROM properties p
LEFT JOIN scores s ON s.property_id = p.id
LEFT JOIN visual_checks v ON v.property_id = p.id
WHERE p.market_id = ? AND p.lat IS NOT NULL AND p.lng IS NOT NULL AND v.reviewed_label IS NULL
  AND (v.status IS NULL OR v.status NOT IN ('done', 'no_imagery'))
"""


def photo_pool(conn: sqlite3.Connection, market_slug: str, limit: int | None = None) -> list[dict]:
    """Properties that still need photos, in the order they get them: buy-box properties first (best Blue Moon
    rank first), then the rest, ranked the same way. Each: {id, address, city, group, rank, score}."""
    market = market_by_slug(conn, market_slug)
    out = []
    for r in rows(conn, _POOL_SQL, (market["id"],)):
        out.append({"id": r["id"], "address": r["address"], "city": r["city"], "rank": r["rank"],
                    "score": r["score"], "group": "rest" if r["in_buybox"] == 0 else "buybox"})
    out.sort(key=lambda r: (r["group"] == "rest", r["rank"] is None, r["rank"] or 0, -(r["score"] or 0.0), r["id"]))
    return out[:max(0, int(limit))] if limit is not None else out


def status(conn: sqlite3.Connection, market_slug: str, settings: Settings | None = None) -> dict:
    """'412 of 600 done this month', what is left to do and what the next batch is."""
    s = settings or get_settings()
    cfg = photos_config(s)
    market = market_by_slug(conn, market_slug)
    ac = s.asset_class(market["asset_class"])
    if not ac.visual_enabled:
        return {"enabled": False, "message": f"{ac.label} has no photo check, so there is no monthly photo goal."}
    backfill(conn, market_slug, s)
    done = done_this_month(conn, market_slug)
    pool = photo_pool(conn, market_slug)
    in_box = sum(1 for r in pool if r["group"] == "buybox")
    waiting = int(scalar(conn, "SELECT COUNT(*) FROM visual_checks v JOIN properties p ON p.id = v.property_id "
                               "WHERE p.market_id = ? AND v.status = 'done' AND v.ai_label IS NULL "
                               "AND v.reviewed_label IS NULL", (market["id"],)) or 0)
    remaining = max(0, cfg.monthly_target - done)
    return {
        "enabled": True, "month": month_label(), "target": cfg.monthly_target, "batch": cfg.batch,
        "done": done, "remaining": remaining, "reached": done >= cfg.monthly_target,
        "pct": round(100.0 * min(done, cfg.monthly_target) / cfg.monthly_target, 1),
        "pool": {"buybox": in_box, "rest": len(pool) - in_box, "total": len(pool)},
        "ai_waiting": waiting,
        "next_batch": min(cfg.batch, max(len(pool), waiting)) if (pool or waiting) else 0,
        "keep_going_to": remaining if remaining else cfg.batch,
        "message": (f"{done:,} of {cfg.monthly_target:,} done in {month_label()}."
                    + (" Goal reached; more is fine." if done >= cfg.monthly_target else "")),
    }


# --- estimate ---------------------------------------------------------------------------------

def estimate_batch(conn: sqlite3.Connection, market_slug: str, n: int, backend: Any = None) -> dict:
    """What `n` more properties would cost (photos, then the AI check). Calls nothing."""
    s = get_settings()
    vcfg = vision_config(s)
    market = market_by_slug(conn, market_slug)
    ac = s.asset_class(market["asset_class"])
    n = max(1, int(n))
    if not ac.visual_enabled:
        return {"dry_run": True, "enabled": False, "needs_confirm": False, "properties": 0, "photos": 0, "calls": 0,
                "cost": {"usd": 0.0}, "message": f"{ac.label} has no photo check."}
    be = backend if backend is not None and not isinstance(backend, str) else get_backend(backend, s)
    photos = streetview.imagery_estimate(conn, market_slug, n, include_excluded=True)
    ai = classify_estimate(conn, market_slug, n, be)
    to_fetch = int(photos.get("to_fetch") or 0)
    waiting = int(ai.get("properties") or 0)
    ai_total = min(n, waiting + to_fetch)
    calls = min(vcfg.max_calls_per_run, max(0, ai_total - int(ai.get("cached") or 0)))
    photo_usd = float((photos.get("cost") or {}).get("usd") or 0.0)
    ai_usd = round(calls * vcfg.anthropic_api.est_cost_per_call_usd, 2) if be.name == "anthropic_api" else 0.0
    usd = round(photo_usd + ai_usd, 2)
    needs_confirm = bool(photos.get("needs_confirm")) or (be.name != "mock" and calls > vcfg.confirm_over_properties)
    where = {"claude_cli": "your Claude plan, no per-call bill", "anthropic_api": f"about ${ai_usd:.2f} on the Anthropic API",
             "mock": "free test mode"}.get(be.name, be.name)
    msg = (f"Next {n:,}: photos for {to_fetch:,} properties (about ${photo_usd:.2f} after Google's free monthly calls), "
           f"then the AI looks at up to {ai_total:,} ({where}). About ${usd:.2f} in all.")
    if not photos.get("has_key") and to_fetch:
        msg += " There's no Google key yet, so only photos you already have get the AI check."
    return {"dry_run": True, "enabled": True, "properties": n, "photos": to_fetch, "calls": calls,
            "ai_checks": ai_total, "cost": {"usd": usd, "photos_usd": photo_usd, "ai_usd": ai_usd},
            "est_cost_usd": usd, "needs_confirm": needs_confirm, "has_key": bool(photos.get("has_key")),
            "backend": be.name, "ready": bool(ai.get("ready", True)), "not_ready_reason": ai.get("not_ready_reason"),
            "street_view_images": int(photos.get("street_view_images") or 0),
            "satellite_images": int(photos.get("satellite_images") or 0),
            "capped": bool(photos.get("capped") or ai.get("capped")),
            "message": msg, "details": {"photos": photos, "ai": ai}}


# --- running ----------------------------------------------------------------------------------

def _scaled(progress: Progress, start: float, end: float) -> Progress:
    if progress is None:
        return None
    return lambda fraction, message: progress(start + (end - start) * max(0.0, min(1.0, fraction)), message)


def _pulled_since(conn: sqlite3.Connection, since: str) -> list[int]:
    return [r["property_id"] for r in rows(conn, "SELECT DISTINCT property_id FROM photo_pulls WHERE pulled_at >= ?",
                                           (since,))]


def _one_batch(conn: sqlite3.Connection, market_slug: str, n: int, backend: Any, progress: Progress,
               client: Any = None) -> dict:
    """Photos for the next n properties, then the AI check, with no questions asked (the caller got the OK)."""
    started = now_iso()
    warnings: list[str] = []
    photos = streetview.fetch_imagery(conn, market_slug, limit=n, confirm=True, include_excluded=True,
                                      progress=_scaled(progress, 0.0, 0.5), client=client)
    if photos.get("error"):
        warnings.append(str(photos["error"]))
    pulled = _pulled_since(conn, started)
    ai = classify_properties(conn, market_slug, limit=n, backend=backend, confirm=True, prefer_ids=pulled,
                             progress=_scaled(progress, 0.5, 1.0))
    if ai.get("error") and ai["error"] not in warnings:
        warnings.append(str(ai["error"]))
    warnings += [w for w in (photos.get("warnings") or []) + (ai.get("warnings") or []) if w and w not in warnings]
    return {"photos": photos, "ai": ai, "fetched": int(photos.get("fetched") or 0),
            "checked": int(ai.get("classified") or 0), "ai_calls": int(ai.get("calls") or 0),
            "sv_images": int(photos.get("street_view_images") or 0),
            "sat_images": int(photos.get("satellite_images") or 0),
            "photo_errors": int(photos.get("errors") or 0), "ai_errors": int(ai.get("errors") or 0),
            "photo_usd": float((photos.get("cost") or {}).get("usd") or 0.0),
            "stopped": bool(ai.get("stopped") or photos.get("error")), "capped": bool(photos.get("capped") or ai.get("capped")),
            "colors": ai.get("colors") or {}, "needs_review": int(ai.get("needs_review") or 0), "warnings": warnings}


def _summary(conn: sqlite3.Connection, market_slug: str, parts: dict) -> str:
    st = status(conn, market_slug)
    colors = parts.get("colors") or {}
    msg = (f"Photos pulled for {parts['fetched']:,} properties, the AI looked at {parts['checked']:,} "
           f"(green {colors.get('green', 0):,}, yellow {colors.get('yellow', 0):,}, red {colors.get('red', 0):,}, "
           f"needs your look {colors.get('na', 0):,}). {st['done']:,} of {st['target']:,} done in {st['month']}.")
    if parts.get("batches", 1) > 1:
        msg = f"{parts['batches']} batches. " + msg
    return msg


def run_batch(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
              confirm: bool = False, progress: Progress = None, backend: Any = None, client: Any = None) -> dict:
    """Photos for the next `limit` properties (default: the batch size), then the AI check for them."""
    s = get_settings()
    cfg = photos_config(s)
    n = int(limit) if limit else cfg.batch
    be = backend if backend is not None and not isinstance(backend, str) else get_backend(backend, s)
    est = estimate_batch(conn, market_slug, n, be)
    if dry_run or not est.get("enabled"):
        return est
    if est["needs_confirm"] and not confirm:
        return {**est, "dry_run": False, "message": f"Waiting for your OK: {est['message']}"}
    if not est.get("ready", True) and not est["photos"] and not est["calls"]:
        return {**est, "dry_run": False, "error": est.get("not_ready_reason"), "message": est.get("not_ready_reason") or ""}
    parts = _one_batch(conn, market_slug, n, be, progress, client)
    parts["batches"] = 1
    return _finish(conn, market_slug, est, parts, be)


def _finish(conn: sqlite3.Connection, market_slug: str, est: dict, parts: dict, be: Any) -> dict:
    message = _summary(conn, market_slug, parts)
    for w in parts["warnings"]:
        if w not in message:
            message += f" {w}"
    nothing = not parts["fetched"] and not parts["checked"]
    error = parts["warnings"][0] if (nothing and parts["warnings"]) else None
    ai_usd = parts["ai_calls"] * vision_config().anthropic_api.est_cost_per_call_usd \
        if be.name == "anthropic_api" else 0.0
    usd = round(parts["photo_usd"] + ai_usd, 2)
    return {"dry_run": False, "enabled": True, "needs_confirm": False, "properties": est.get("properties"),
            "fetched": parts["fetched"], "checked": parts["checked"], "calls": parts["ai_calls"],
            "errors": parts["photo_errors"] + parts["ai_errors"], "needs_review": parts["needs_review"],
            "colors": parts.get("colors") or {}, "batches": parts.get("batches", 1), "stopped": parts["stopped"],
            "capped": parts["capped"], "done_this_month": status(conn, market_slug)["done"],
            "cost": {"usd": usd}, "est_cost_usd": usd, "error": error, "warnings": parts["warnings"],
            "message": message, "details": {"photos": parts["photos"], "ai": parts["ai"]}}


def run_keep_going(conn: sqlite3.Connection, market_slug: str, dry_run: bool = False, confirm: bool = False,
                   progress: Progress = None, backend: Any = None, client: Any = None) -> dict:
    """Batch after batch until this month's goal is reached (one batch when it already is). One OK covers it all;
    the Google and Claude caps per run still stop it."""
    s = get_settings()
    cfg = photos_config(s)
    vcfg = vision_config(s)
    gcfg = streetview.imagery_config(s)
    be = backend if backend is not None and not isinstance(backend, str) else get_backend(backend, s)
    st = status(conn, market_slug, s)
    if not st.get("enabled"):
        return {"dry_run": dry_run, "enabled": False, "needs_confirm": False, "message": st["message"]}
    total = st["remaining"] or cfg.batch
    est = estimate_batch(conn, market_slug, total, be)
    est["goal"] = st
    if dry_run or not est.get("enabled"):
        return est
    if est["needs_confirm"] and not confirm:
        return {**est, "dry_run": False, "message": f"Waiting for your OK: {est['message']}"}
    if not est.get("ready", True) and not est["photos"] and not est["calls"]:
        return {**est, "dry_run": False, "error": est.get("not_ready_reason"), "message": est.get("not_ready_reason") or ""}
    chunks = [min(cfg.batch, total - i) for i in range(0, total, cfg.batch)]
    agg = {"fetched": 0, "checked": 0, "ai_calls": 0, "sv_images": 0, "sat_images": 0, "photo_errors": 0,
           "ai_errors": 0, "photo_usd": 0.0, "needs_review": 0, "stopped": False, "capped": False,
           "colors": {}, "warnings": [], "photos": {}, "ai": {}, "batches": 0}
    sv_per = max(1, len(gcfg.street_view_files))
    for i, size in enumerate(chunks):
        # a run is one run: the per-run Google and Claude caps cover all the batches together
        if (agg["sv_images"] + size * sv_per > gcfg.max_street_view_images_per_run and agg["batches"]) or \
           (agg["ai_calls"] + size > vcfg.max_calls_per_run and agg["batches"]):
            agg["capped"] = True
            agg["warnings"].append("Stopped at the per-run limit in config.yaml (google.max_*_per_run or "
                                   "vision.max_calls_per_run). Press Keep going again for the rest.")
            break
        part = _one_batch(conn, market_slug, size, be, _scaled(progress, i / len(chunks), (i + 1) / len(chunks)),
                          client)
        agg["batches"] += 1
        for k in ("fetched", "checked", "ai_calls", "sv_images", "sat_images", "photo_errors", "ai_errors",
                  "photo_usd", "needs_review"):
            agg[k] += part[k]
        for color, count in (part["colors"] or {}).items():
            agg["colors"][color] = agg["colors"].get(color, 0) + int(count)
        agg["warnings"] += [w for w in part["warnings"] if w not in agg["warnings"]]
        agg["photos"], agg["ai"] = part["photos"], part["ai"]
        agg["capped"] = agg["capped"] or part["capped"]
        if part["stopped"]:
            agg["stopped"] = True
            break
        if not part["fetched"] and not part["checked"]:     # nothing left to do
            break
    return _finish(conn, market_slug, est, agg, be)


__all__ = ["PhotosConfig", "backfill", "done_this_month", "estimate_batch", "month_label", "photo_pool",
           "photos_config", "run_batch", "run_keep_going", "status"]
