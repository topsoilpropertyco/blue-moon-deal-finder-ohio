"""One button: photos and the AI's call for every lead.

Seth: "Use AI, pull every photo and tell me: is it a walk-up or not? Then I look at what's needed by hand."
This module is the glue behind the Run screen's one photo button and `run.py photos all`. The paid work is
still done by `streetview.fetch_imagery` (photos) and `vision.classify_properties` (the AI's call), so every
safety rule stays:

- **Every lead**: every property in the market that has no photos yet (buy-box properties first, best Blue Moon
  rank first, then the rest), then the AI's call on every property that has photos and no call yet.
- **Estimate first** (`estimate`): how many properties, how many photos, what Google would charge after its
  free monthly calls (usually $0), and how many AI calls on your Claude plan. Nothing is called.
- **One OK**: a run over `safety.confirm_over_properties` (50) waits for `confirm` (the dashboard's single
  "Yes, start" click, or `--yes` on the command line). Then the whole thing runs as one background job.
- **The caps still hold**: `google.max_street_view_images_per_run`, `google.max_satellite_images_per_run`
  and `vision.max_calls_per_run`. When one would stop the run early, the estimate says so in plain words.
- **Resumable**: photos already saved and AI answers already given are never fetched, asked or paid for again
  (files on disk plus the SQLite cache). After a stop, a cancel, a cap or a Claude usage limit, pressing the
  button again carries on where it left off.
- Progress reads "312 of 2,981 done": properties with the AI's call (or your own answer) out of every lead.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any

from ..config import Settings, get_settings
from ..db import market_by_slug, now_iso, one, rows
from . import streetview
from .vision import VisionBackend, classify_estimate, classify_properties, get_backend, vision_config

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

PHOTO_SHARE = 0.15      # share of the progress bar for the photos (fast) when the AI (slow) follows


# --- where things stand -----------------------------------------------------------------------

_STATUS_SQL = """
SELECT COUNT(*) AS total,
       SUM(CASE WHEN p.lat IS NULL OR p.lng IS NULL THEN 1 ELSE 0 END) AS no_location,
       SUM(CASE WHEN v.reviewed_label IS NOT NULL
                  OR (v.ai_label IS NOT NULL AND v.checked_at IS NOT NULL) THEN 1 ELSE 0 END) AS done,
       SUM(CASE WHEN v.reviewed_label IS NULL AND v.status IN ('done', 'no_imagery')
                 AND (v.ai_label IS NULL OR v.checked_at IS NULL) THEN 1 ELSE 0 END) AS ai_left,
       SUM(CASE WHEN p.lat IS NOT NULL AND p.lng IS NOT NULL AND v.reviewed_label IS NULL
                 AND (v.status IS NULL OR v.status NOT IN ('done', 'no_imagery'))
                 AND COALESCE(s.in_buybox, 1) != 0 THEN 1 ELSE 0 END) AS photos_left_buybox,
       SUM(CASE WHEN p.lat IS NOT NULL AND p.lng IS NOT NULL AND v.reviewed_label IS NULL
                 AND (v.status IS NULL OR v.status NOT IN ('done', 'no_imagery'))
                 AND s.in_buybox = 0 THEN 1 ELSE 0 END) AS photos_left_rest
FROM properties p
LEFT JOIN visual_checks v ON v.property_id = p.id
LEFT JOIN scores s ON s.property_id = p.id
WHERE p.market_id = ?
"""


def _target_name(settings: Settings, asset_class: str) -> str:
    ac = settings.asset_class(asset_class)
    return str(ac.visual_check.get("target_name") or "target")


def button_label(target_name: str) -> str:
    """'Get photos and the AI's walk-up call for every lead'."""
    return f"Get photos and the AI's {target_name.lower()} call for every lead"


def status(conn: sqlite3.Connection, market_slug: str, settings: Settings | None = None) -> dict:
    """'312 of 2,981 done': leads with the AI's call (or your answer) out of every lead, and what is left."""
    s = settings or get_settings()
    market = market_by_slug(conn, market_slug)
    ac = s.asset_class(market["asset_class"])
    if not ac.visual_enabled:
        return {"enabled": False, "message": f"{ac.label} has no photo check."}
    r = one(conn, _STATUS_SQL, (market["id"],)) or {}
    total = int(r.get("total") or 0)
    done = int(r.get("done") or 0)
    box, rest = int(r.get("photos_left_buybox") or 0), int(r.get("photos_left_rest") or 0)
    name = _target_name(s, market["asset_class"])
    return {
        "enabled": True, "total": total, "done": done, "left": max(0, total - done),
        "pct": round(100.0 * done / total, 1) if total else 0.0,
        "photos_left": {"buybox": box, "rest": rest, "total": box + rest},
        "ai_left": int(r.get("ai_left") or 0), "no_location": int(r.get("no_location") or 0),
        "target_name": name, "button": button_label(name),
        "message": f"{done:,} of {total:,} done." if total else "No properties in this market yet.",
    }


# --- estimate (calls nothing) -----------------------------------------------------------------

def _plural(n: int, one_word: str, many: str | None = None) -> str:
    return f"{n:,} {one_word if n == 1 else (many or one_word + 's')}"


def _backend(backend: Any, settings: Settings) -> VisionBackend:
    return backend if isinstance(backend, VisionBackend) else get_backend(backend, settings)


def estimate(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, backend: Any = None) -> dict:
    """What pressing the button would do and cost (a dry run). `limit`: at most this many properties (Test on 5).

    `lines` holds the plain-English sentences the Run screen and the command line show, one per point."""
    s = get_settings()
    vcfg = vision_config(s)
    gcfg = streetview.imagery_config(s)
    market = market_by_slug(conn, market_slug)
    ac = s.asset_class(market["asset_class"])
    st = status(conn, market_slug, s)
    if not ac.visual_enabled:
        return {"dry_run": True, "enabled": False, "needs_confirm": False, "properties": 0, "photos": 0, "calls": 0,
                "cost": {"usd": 0.0}, "lines": [st["message"]], "message": st["message"], "status": st}
    be = _backend(backend, s)
    name = st["target_name"].lower()
    photos = streetview.imagery_estimate(conn, market_slug, limit, include_excluded=True)
    has_key = bool(photos.get("has_key"))
    to_fetch = int(photos.get("to_fetch") or 0)
    this_run = int(photos.get("within_caps", to_fetch) or 0) if has_key else 0
    google_capped = has_key and this_run < to_fetch
    ai = classify_estimate(conn, market_slug, limit, be)
    cached = int(ai.get("cached") or 0)
    # A lead with some photos saved and one still missing is in both lists: count each property once.
    photo_ids = set(photos.pop("within_caps_ids", None) or []) if has_key else set()
    ai_total = len(set(ai.pop("property_ids", None) or []) | photo_ids)
    if limit is not None:
        ai_total = min(int(limit), ai_total)
    needed = max(0, ai_total - cached)
    calls = min(vcfg.max_calls_per_run, needed)
    ai_capped = needed > calls
    ready = bool(ai.get("ready", True))
    photo_usd = float((photos.get("cost") or {}).get("usd") or 0.0) if has_key else 0.0
    ai_usd = round(calls * vcfg.anthropic_api.est_cost_per_call_usd, 2) if be.name == "anthropic_api" else 0.0
    usd = round(photo_usd + ai_usd, 2)
    sv = int(photos.get("street_view_images") or 0) if has_key else 0
    sat = int(photos.get("satellite_images") or 0) if has_key else 0
    properties = ai_total
    over = vcfg.confirm_over_properties
    needs_confirm = this_run > over or (be.name != "mock" and calls > over)
    nothing = not this_run and not ai_total

    lines: list[str] = []
    if nothing:
        if to_fetch and not has_key:
            lines.append(f"{_plural(to_fetch, 'lead')} still need photos, but there's no Google key yet. "
                         f"Add it first (Settings has the steps).")
        else:
            lines.append(f"Nothing to do: every lead with a map location has photos and the AI's {name} call.")
    else:
        if this_run:
            free = " (inside Google's free monthly allowance)" if photo_usd == 0 else " after Google's free monthly calls"
            lines.append(f"Photos for {_plural(this_run, 'property', 'properties')} with none yet, buy box first: "
                         f"up to {_plural(sv, 'Street View photo')} and {_plural(sat, 'satellite photo')}. "
                         f"Google: about ${photo_usd:.2f}{free}.")
        if ai_total:
            plan = {"claude_cli": "on your Claude plan (no bill; it uses your plan's usage)",
                    "anthropic_api": f"on the pay-per-use Anthropic API (about ${ai_usd:.2f})",
                    "mock": "in free test mode"}.get(be.name, be.name)
            reused = f" ({cached:,} already answered, so free)" if cached else ""
            lines.append(f"Then the AI makes its {name} call on {_plural(ai_total, 'property', 'properties')}"
                         f"{reused}: {_plural(calls, 'AI call')} this run, {plan}.")
        if calls > 20 and be.name == "claude_cli":
            lines.append("That can take hours. It runs in the background, so you can close this page. "
                         "If your Claude plan hits its usage limit, it stops; press the button again later to carry on.")
    if google_capped:
        lines.append(f"Your per-run Google limit ({gcfg.max_street_view_images_per_run:,} Street View and "
                     f"{gcfg.max_satellite_images_per_run:,} satellite photos) stops this run after about "
                     f"{_plural(this_run, 'property', 'properties')}: {to_fetch - this_run:,} more need photos. "
                     f"Press the button again afterwards for the rest.")
    if ai_capped:
        lines.append(f"Your per-run AI limit ({vcfg.max_calls_per_run:,} calls) stops the AI after "
                     f"{_plural(calls, 'call')}. Press the button again afterwards to continue.")
    if to_fetch and not has_key and not nothing:
        lines.append(f"No Google key yet, so no new photos: only photos you already have get the AI's call "
                     f"({to_fetch:,} leads still need photos).")
    if st.get("no_location"):
        lines.append(f"{_plural(st['no_location'], 'lead has', 'leads have')} no map location yet, so no photos "
                     f"(the Drive times step places them).")
    if not ready and ai_total:
        lines.append(str(ai.get("not_ready_reason") or "The AI isn't set up on this computer yet."))
    if not nothing:
        lines.append("Photos and answers you already have are never paid for twice.")
    return {
        "dry_run": True, "enabled": True, "nothing_to_do": nothing,
        "properties": properties, "photos": this_run, "photos_needed": to_fetch, "ai_checks": ai_total,
        "calls": calls, "cached": cached, "street_view_images": sv, "satellite_images": sat,
        "cost": {"usd": usd, "photos_usd": photo_usd, "ai_usd": ai_usd}, "est_cost_usd": usd,
        "free": photo_usd == 0 and ai_usd == 0, "needs_confirm": needs_confirm, "has_key": has_key,
        "backend": be.name, "ready": ready, "not_ready_reason": ai.get("not_ready_reason"),
        "capped": google_capped or ai_capped, "google_capped": google_capped, "ai_capped": ai_capped,
        "limit": limit, "lines": lines, "message": " ".join(lines), "status": st,
        "details": {"photos": photos, "ai": ai},
    }


# --- running ----------------------------------------------------------------------------------

def _answered(conn: sqlite3.Connection, market_id: int) -> int:
    r = one(conn, "SELECT COUNT(*) AS n FROM visual_checks v JOIN properties p ON p.id = v.property_id "
                  "WHERE p.market_id = ? AND (v.reviewed_label IS NOT NULL "
                  "OR (v.ai_label IS NOT NULL AND v.checked_at IS NOT NULL))", (market_id,))
    return int((r or {}).get("n") or 0)


def _phase(progress: Progress, start: float, end: float, words: Callable[[str], str]) -> Progress:
    if progress is None:
        return None

    def report(fraction: float, message: str) -> None:
        f = max(0.0, min(1.0, float(fraction)))
        progress(start + (end - start) * f, words(message))
    return report


def _first_count(message: str, one_word: str, many: str) -> str:
    """'84 properties' from a step's progress message ('Photo check: 84 of 1,000 properties')."""
    m = re.search(r"(\d[\d,]*)", message or "")
    n = int(m.group(1).replace(",", "")) if m else 0
    return _plural(n, one_word, many)


def _pulled_since(conn: sqlite3.Connection, since: str) -> list[int]:
    return [r["property_id"] for r in rows(conn, "SELECT DISTINCT property_id FROM photo_pulls WHERE pulled_at >= ?",
                                           (since,))]


def run(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
        confirm: bool = False, progress: Progress = None, backend: Any = None, client: Any = None,
        approved: int | None = None) -> dict:
    """Photos for every lead that has none, then the AI's call on every lead with photos and no call.

    limit: at most this many properties (Test on 5). dry_run: the estimate only. confirm: the OK for a run over
    safety.confirm_over_properties; without it a big run returns needs_confirm and calls nothing.
    approved: the number of properties that OK was given for (the Run screen sends the estimate it showed). If the
    job finds more when it starts (new leads came in, or it waited behind another job) and the run needs an OK,
    it asks again instead of spending."""
    s = get_settings()
    be = _backend(backend, s)
    est = estimate(conn, market_slug, limit, be)
    if dry_run or not est.get("enabled"):
        return est
    if est["nothing_to_do"]:
        return {**est, "dry_run": False, "fetched": 0, "checked": 0}
    if est["needs_confirm"] and not confirm:
        return {**est, "dry_run": False, "message": f"Waiting for your OK: {est['message']}"}
    if est["needs_confirm"] and approved is not None and int(est["properties"]) > int(approved):
        return {**est, "dry_run": False,
                "message": f"The numbers changed since you said yes to {_plural(int(approved), 'property', 'properties')}, "
                           f"so nothing was started. Waiting for your OK again: {est['message']}"}
    if not est["ready"] and not est["photos"]:
        why = est.get("not_ready_reason") or "The AI isn't set up on this computer yet."
        return {**est, "dry_run": False, "error": why, "message": why, "fetched": 0, "checked": 0}

    market = market_by_slug(conn, market_slug)
    total = int(est["status"]["total"])
    warnings: list[str] = []
    split = PHOTO_SHARE if (est["photos"] and est["ai_checks"]) else (1.0 if est["photos"] else 0.0)

    photos: dict = {}
    started = now_iso()
    if est["has_key"] and est["photos"]:
        done0 = _answered(conn, market["id"])
        photos = streetview.fetch_imagery(
            conn, market_slug, limit=limit, confirm=True, include_excluded=True, client=client,
            progress=_phase(progress, 0.0, split,
                            lambda m: f"{done0:,} of {total:,} done. Getting photos: {_first_count(m, 'property', 'properties')} "
                                      f"so far this run"))
        if photos.get("error"):
            warnings.append(str(photos["error"]))

    ai: dict = {}
    if est["ai_checks"]:
        prefer = _pulled_since(conn, started) if limit is not None else ()

        def words(m: str) -> str:
            return (f"{_answered(conn, market['id']):,} of {total:,} done. The AI has looked at "
                    f"{_first_count(m, 'property', 'properties')} so far this run")
        ai = classify_properties(conn, market_slug, limit=limit, backend=be, confirm=True, prefer_ids=prefer,
                                 progress=_phase(progress, split, 1.0, words))
        if ai.get("error") and ai["error"] not in warnings:
            warnings.append(str(ai["error"]))
    # The per-run cap notes of the two steps name config keys and say "run photos again"; the summary already
    # says "press the button again" in plain words.
    warnings += [w for w in (photos.get("warnings") or []) + (ai.get("warnings") or [])
                 if w and w not in warnings and "per-run" not in w]
    return _finish(conn, market_slug, est, photos, ai, warnings, be)


def _finish(conn: sqlite3.Connection, market_slug: str, est: dict, photos: dict, ai: dict, warnings: list[str],
            be: VisionBackend) -> dict:
    st = status(conn, market_slug)
    name = st["target_name"].lower()
    fetched, checked = int(photos.get("fetched") or 0), int(ai.get("classified") or 0)
    calls = int(ai.get("calls") or 0)
    colors = ai.get("colors") or {}
    photo_usd = float((photos.get("cost") or {}).get("usd") or 0.0)
    ai_usd = round(calls * vision_config().anthropic_api.est_cost_per_call_usd, 2) if be.name == "anthropic_api" else 0.0
    usd = round(photo_usd + ai_usd, 2)
    parts = [f"{st['done']:,} of {st['total']:,} done."]
    parts.append(f"This run: photos for {_plural(fetched, 'property', 'properties')} (about ${photo_usd:.2f} from Google)"
                 f", and the AI made its {name} call on {checked:,}"
                 + (f" (green {colors.get('green', 0):,} likely a {name}, yellow {colors.get('yellow', 0):,} unsure, "
                    f"red {colors.get('red', 0):,} likely not, {colors.get('na', 0):,} need your look)." if checked else "."))
    if checked:
        parts.append("Photo review has them: the unsure ones first, then every call the AI made.")
    stopped = bool(ai.get("stopped") or photos.get("error") or ai.get("capped") or photos.get("capped"))
    left = st["left"] - st["no_location"]
    if left > 0 and est.get("limit") is None and (stopped or warnings):
        parts.append(f"{left:,} left. Press the button again to carry on where it left off.")
    message = " ".join(parts)
    for w in warnings:
        if w not in message:
            message += f" {w}"
    nothing = not fetched and not checked
    error = warnings[0] if (nothing and warnings) else None
    log.info("Photos for every lead %s: %s", market_slug, message)
    return {"dry_run": False, "enabled": True, "needs_confirm": False, "properties": est.get("properties"),
            "photos": est.get("photos"), "fetched": fetched, "checked": checked, "calls": calls,
            "errors": int(photos.get("errors") or 0) + int(ai.get("errors") or 0),
            "needs_review": int(ai.get("needs_review") or 0), "colors": colors,
            "done": st["done"], "total": st["total"], "left": st["left"], "stopped": stopped,
            "capped": bool(photos.get("capped") or ai.get("capped")), "cost": {"usd": usd}, "est_cost_usd": usd,
            "error": error, "warnings": warnings, "message": message, "status": st,
            "details": {"photos": photos, "ai": ai}}


__all__ = ["button_label", "estimate", "run", "status"]
