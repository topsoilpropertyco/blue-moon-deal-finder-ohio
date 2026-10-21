"""The pipeline: run one step, or all of them, with a cost estimate and a safety stop before spending.

`all` runs the steps in this order (free first; paid last, and only on what survives the filters):

    owners -> drive -> rent -> score -> photos -> classify -> score -> drill

- Free: owners, rent (HUD), score, drill, and drive without a Google key (straight-line estimate).
- Paid: drive with a Google key (Routes), photos (Street View + satellite), classify (Claude).
  A paid step bigger than `safety.confirm_over_properties` stops before spending when `confirm` is
  False. Its result then has `needs_confirm=True` and the `estimate`, and nothing was charged.

Every step returns a plain dict:

    {step, label, ok, dry_run, needs_confirm, message, warnings: [...], counts: {...},
     estimate (dry runs and runs waiting for an OK), details (the backend module's own result)}

`message` and `warnings` are written for Seth. `all` and `after_import` also return `steps: [...]`.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import get_settings, has_secret
from .db import market_by_slug, now_iso, one, rows, scalar, upsert
from .drill import engine
from .enrich import drive, geo, owners, rent
from .imagery import calibrate as imagery_calibrate
from .imagery import (
    classify_estimate,
    classify_properties,
    fetch_imagery,
    imagery_estimate,
    vision_config,
)
from .ingest import IngestError, import_file, preview_file
from .scoring.rank import compute_scores

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None
PathArg = str | Path | Iterable[str | Path] | None

# The steps Seth can run, in the order the dashboard lists them.
STEPS: list[str] = ["import", "owners", "drive", "rent", "photos", "classify", "score", "drill", "all"]
# Also runnable (CLI commands and background jobs), but not part of `all`.
EXTRA_STEPS: list[str] = ["calibrate", "export", "estimate", "after_import"]
STEP_LABELS: dict[str, str] = {
    "import": "Import", "owners": "Owners", "drive": "Drive times", "rent": "Rent check", "photos": "Photos",
    "classify": "Photo check", "score": "Scores", "drill": "DRILL automatic stages", "all": "Everything",
    "calibrate": "Calibrate the photo check", "export": "Export files", "estimate": "Cost estimate",
    "after_import": "Rank the new import",
}
# Other names for the same steps (older docs used visual_fetch / visual_classify).
STEP_ALIASES: dict[str, str] = {
    "visual_fetch": "photos", "imagery": "photos", "visual_classify": "classify", "vision": "classify",
    "drive_times": "drive", "scores": "score", "rank": "score", "auto": "drill", "everything": "all",
}
PAID_STEPS: frozenset[str] = frozenset({"drive", "photos", "classify", "calibrate"})
# Run on their own, these steps change what the buy box, colors and rank are made from, so the scores are
# brought up to date right after (free, about a second). `all` and after_import already have score steps.
RESCORE_AFTER: frozenset[str] = frozenset({"drive", "rent", "photos", "classify", "calibrate"})
# (step, share of the progress bar) for `all` and for the free steps after an import.
ALL_PLAN: tuple[tuple[str, float], ...] = (
    ("owners", 0.04), ("drive", 0.16), ("rent", 0.18), ("score", 0.04),
    ("photos", 0.26), ("classify", 0.24), ("score", 0.04), ("drill", 0.04),
)
AFTER_IMPORT_PLAN: tuple[tuple[str, float], ...] = (
    ("owners", 0.2), ("drive_estimate", 0.1), ("score", 0.3), ("drill", 0.4),
)
# Which numbers from each backend result are worth showing (dicts of numbers are flattened).
COUNT_KEYS: dict[str, tuple[str, ...]] = {
    "owners": ("properties", "portfolio_groups", "portfolio_properties", "owner_types"),
    "drive": ("properties", "google", "estimated", "cached", "fetched", "to_fetch", "elements",
              "missing_coordinates", "beyond_range", "no_route", "errors"),
    "rent": ("properties", "with_location", "saved", "fits", "no_data", "errors"),
    "photos": ("properties", "to_fetch", "cached", "fetched", "no_imagery", "errors", "street_view_images",
               "satellite_images"),
    "classify": ("properties", "calls", "cached", "classified", "needs_review", "colors", "errors"),
    "calibrate": ("properties", "calls", "classified"),
    "score": ("properties", "in_buybox", "excluded", "exclusion_counts", "by_hand"),
    "drill": ("properties", "levels", "changed"),
}
KEY_NAMES: dict[str, str] = {
    "google": "GOOGLE_MAPS_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "hud": "HUD_API_TOKEN",
    "census": "CENSUS_API_KEY", "app_password": "APP_PASSWORD",
}
SKU_LABELS: dict[str, str] = {
    "route_matrix_element": "Google Routes (drive times)", "geocoding": "Google Geocoding",
    "street_view_static": "Street View photos", "static_maps": "Satellite photos",
    "street_view_metadata": "Street View lookups (free)",
}


@dataclass(frozen=True)
class StepRun:
    """One request to run a step."""
    conn: sqlite3.Connection
    market: str
    limit: int | None = None
    dry_run: bool = False
    confirm: bool = False
    progress: Progress = None
    path: PathArg = None


# --- public API --------------------------------------------------------------------------------

def normalize_step(step: str) -> str:
    """The canonical step name, or ValueError with the list of steps."""
    key = (step or "").strip().lower().replace("-", "_")
    key = STEP_ALIASES.get(key, key)
    if key not in STEP_LABELS:
        raise ValueError(f"Unknown step '{step}'. Steps: {', '.join(STEPS + EXTRA_STEPS)}.")
    return key


def step_label(step: str) -> str:
    return STEP_LABELS.get(STEP_ALIASES.get(step, step), step)


def run_step(conn: sqlite3.Connection, market_slug: str, step: str, limit: int | None = None,
             dry_run: bool = False, confirm: bool = False, progress: Progress = None, *,
             path: PathArg = None) -> dict:
    """Run one step for one market and return its result (see the module docstring).

    limit: at most this many properties (test with 5 first); owners, score and drill always do the
      whole market. dry_run: count and price only, call nothing. confirm: OK to spend on big paid runs.
    path: the file(s) for the import step.
    Problems Seth can fix (bad file, unknown market, config value) come back as ok=False with a
    plain-English message; anything unexpected raises.
    """
    key = normalize_step(step)
    try:
        _market(conn, market_slug)
        run = StepRun(conn, market_slug, _clean_limit(limit), bool(dry_run), bool(confirm), progress, path)
        res = RUNNERS[key](run)
    except ValueError as exc:  # IngestError is a ValueError; messages are written for Seth
        return _result(key, ok=False, message=str(exc))
    if key in RESCORE_AFTER and res.get("ok") and not run.dry_run and not res.get("needs_confirm"):
        _rescore_after(conn, market_slug, res)
    return res


def _rescore_after(conn: sqlite3.Connection, slug: str, res: dict) -> None:
    """Bring buy box, colors, score and rank up to date after a step that changed their inputs."""
    try:
        res["score"] = _from_module("score", compute_scores(conn, slug))
        res["message"] = (res.get("message") or "").rstrip() + " Buy box, scores and rank updated."
    except Exception:  # never lose the step's own result
        log.exception("Re-score after a step failed for %s", slug)
        res.setdefault("warnings", []).append("Scores were not updated. Run Score.")


def _scores_stale(conn: sqlite3.Connection, slug: str) -> bool:
    """True when drive times, rent fits or buy-box decisions made by hand are newer than the scores, or some
    properties have no score yet (for example a drive run that stopped before its re-score)."""
    market_id = _market(conn, slug)["id"]
    r = one(conn, """
        SELECT (SELECT MIN(s.computed_at) FROM scores s JOIN properties p ON p.id = s.property_id
                WHERE p.market_id = ?) AS scored,
               (SELECT COUNT(*) FROM properties p LEFT JOIN scores s ON s.property_id = p.id
                WHERE p.market_id = ? AND s.property_id IS NULL) AS unscored,
               (SELECT MAX(d.fetched_at) FROM drive_times d JOIN properties p ON p.id = d.property_id
                WHERE p.market_id = ?) AS drive,
               (SELECT MAX(r.computed_at) FROM rent_fits r JOIN properties p ON p.id = r.property_id
                WHERE p.market_id = ?) AS rent,
               (SELECT MAX(l.created_at) FROM buybox_override_log l JOIN properties p ON p.id = l.property_id
                WHERE p.market_id = ?) AS decided""", (market_id,) * 5) or {}
    if not r.get("scored"):
        return bool(r.get("unscored"))
    return bool(r.get("unscored")) or any(r.get(k) and str(r[k]) > str(r["scored"])
                                          for k in ("drive", "rent", "decided"))


def _fresh_scores(conn: sqlite3.Connection, slug: str) -> None:
    """Photos are only bought for buy-box properties: make sure the buy box reflects the latest drive times
    and rents first (local and free; not on a dry run, which saves nothing)."""
    try:
        if _scores_stale(conn, slug):
            compute_scores(conn, slug)
    except Exception:  # an estimate or a photo run must not fail because of this check
        log.exception("Could not bring scores up to date before photos for %s", slug)


def run_all(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
            confirm: bool = False, progress: Progress = None) -> dict:
    """Every step in order. See ALL_PLAN."""
    return run_step(conn, market_slug, "all", limit, dry_run, confirm, progress)


def after_import(conn: sqlite3.Connection, market_slug: str, progress: Progress = None) -> dict:
    """The free steps right after an import, so the new list is ranked at once.

    owners -> free drive estimates (only for properties with none; Google is never called) -> score ->
    DRILL automatic stages. Nothing here touches the network or costs money.
    """
    run = StepRun(conn, market_slug, progress=progress)
    results = _run_plan(AFTER_IMPORT_PLAN, run)
    failed = [r for r in results if not r["ok"]]
    score = _last(results, "score")
    message = _ranked_message(score, _last(results, "drill"))
    if _drive_is_approximate(conn, market_slug):
        message += (" Drive times are approximate until you run the drive step." if keys_present()["google"]
                    else " Drive times are straight-line estimates (no Google key yet).")
    return _result("after_import", ok=not failed, message=message, steps=results,
                   warnings=_collect_warnings(results), counts=(score or {}).get("counts", {}))


def estimate(conn: sqlite3.Connection, market_slug: str, limit: int | None = None) -> dict:
    """Dry run of every step: what each would do and what it would cost. Calls nothing.

    Google costs use this month's usage so far and the free monthly calls per SKU (google.prices in
    config.yaml). Photo checks on the Claude plan are counted, not priced.
    """
    market = _market(conn, market_slug)
    limit = _clean_limit(limit)
    photos = _estimate_or_note("photos", lambda: _estimate_photos(conn, market_slug, limit))
    lines = [
        _estimate_local(conn, market, "owners"),
        _estimate_or_note("drive", lambda: _estimate_drive(conn, market_slug, limit)),
        _estimate_or_note("rent", lambda: _estimate_rent(conn, market_slug, limit)),
        photos,
        _estimate_or_note("classify", lambda: _estimate_classify(conn, market_slug, limit, photos)),
        _estimate_local(conn, market, "score"),
        _estimate_local(conn, market, "drill"),
    ]
    skus = [c for line in lines for c in line["skus"]]
    total = round(sum(line["usd"] for line in lines), 2)
    waiting = [line["step"] for line in lines if line["needs_confirm"]]
    classify = _line(lines, "classify")
    result = {
        "market": market_slug, "name": market["name"], "limit": limit,
        "properties": _property_count(conn, market["id"]),
        "steps": lines, "google": skus, "total_usd": total,
        "claude_plan_calls": classify.get("plan_calls", 0), "claude_api_usd": classify.get("api_usd", 0.0),
        "needs_confirm": bool(waiting), "needs_confirm_steps": waiting,
        "confirm_over": _confirm_over(), "has_google_key": has_secret(KEY_NAMES["google"]),
    }
    result["message"] = _estimate_summary(result)
    return result


def layer_status(conn: sqlite3.Connection, market_slug: str) -> list[dict]:
    """How far each enrichment layer has got: [{key, label, done, total, note}]."""
    market = _market(conn, market_slug)
    ac = get_settings().asset_class(market["asset_class"])
    c = one(conn, """
        SELECT COUNT(*) AS total,
               COUNT(o.property_id) AS owners,
               COUNT(d.property_id) AS drive, COALESCE(SUM(d.method = 'google_routes'), 0) AS drive_google,
               COUNT(r.property_id) AS rent,
               COUNT(s.property_id) AS scored, COALESCE(SUM(s.in_buybox = 1), 0) AS in_buybox,
               COALESCE(SUM(v.status IN ('done', 'no_imagery')), 0) AS photos,
               COALESCE(SUM(v.ai_label IS NOT NULL OR v.reviewed_label IS NOT NULL), 0) AS checked,
               COALESCE(SUM(s.in_buybox = 1 AND v.status IN ('done', 'no_imagery')), 0) AS photos_box,
               COALESCE(SUM(s.in_buybox = 1 AND (v.ai_label IS NOT NULL OR v.reviewed_label IS NOT NULL)), 0)
                   AS checked_box
        FROM properties p
        LEFT JOIN owners o ON o.property_id = p.id
        LEFT JOIN drive_times d ON d.property_id = p.id
        LEFT JOIN rent_fits r ON r.property_id = p.id
        LEFT JOIN scores s ON s.property_id = p.id
        LEFT JOIN visual_checks v ON v.property_id = p.id
        WHERE p.market_id = ?""", (market["id"],)) or {}
    total = int(c.get("total") or 0)
    drive_note = f"{int(c['drive_google']):,} from Google" if c.get("drive_google") else "approximate"
    layers = [
        _layer("owners", "Owners", c.get("owners"), total),
        _layer("drive", "Drive times", c.get("drive"), total, drive_note if c.get("drive") else None),
    ]
    if ac.rent_enabled:
        layers.append(_layer("rent", "Rent check", c.get("rent"), total))
    if ac.visual_enabled:  # photos are only taken inside the buy box (once there are scores)
        box = "_box" if c.get("scored") else ""
        photo_total = int(c.get("in_buybox") or 0) if c.get("scored") else total
        layers.append(_layer("photos", "Photos", c.get(f"photos{box}"), photo_total, "buy box only"))
        layers.append(_layer("visual", "Photo check", c.get(f"checked{box}"), photo_total, "buy box only"))
    layers.append(_layer("score", "Scores", c.get("scored"), total))
    return layers


def market_status(conn: sqlite3.Connection, market_slug: str) -> dict:
    """One market at a glance: size, buy box, contact levels, penetration, layers, last import."""
    market = _market(conn, market_slug)
    ac = get_settings().asset_class(market["asset_class"])
    count = _property_count(conn, market["id"])
    status: dict[str, Any] = {
        "market": market_slug, "name": market["name"], "asset_class": ac.key, "asset_label": ac.label,
        "properties": count, "in_buybox": _in_buybox_count(conn, market["id"]),
        "scored": int(scalar(conn, "SELECT COUNT(*) FROM scores s JOIN properties p ON p.id = s.property_id "
                                   "WHERE p.market_id = ?", (market["id"],)) or 0),
        "levels": {1: 0, 2: 0, 3: 0}, "level_names": dict(engine.LEVEL_NAMES_DEFAULT),
        "penetration": 0.0, "penetration_text": "No owners yet",
        "layers": layer_status(conn, market_slug), "last_import": last_import(conn, market["id"]),
    }
    if count:
        board = engine.overview(conn, market_slug)
        status.update(levels=board["levels"], level_names=board["level_names"], penetration=board["penetration"],
                      penetration_text=board["penetration_detail"]["text"])
    return status


def last_import(conn: sqlite3.Connection, market_id: int) -> dict | None:
    return one(conn, "SELECT filename, created_at, rows_total, rows_new, rows_updated, rows_skipped FROM imports "
                     "WHERE market_id = ? ORDER BY id DESC LIMIT 1", (market_id,))


def keys_present() -> dict[str, bool]:
    """Which keys are set in .env (True/False only; values are never read out)."""
    return {name: has_secret(env) for name, env in KEY_NAMES.items()}


# --- the steps ---------------------------------------------------------------------------------

IMPORT_COUNTS = ("rows_total", "rows_new", "rows_updated", "rows_unchanged", "rows_skipped")


def _run_import(run: StepRun) -> dict:
    paths = _paths(run.path)
    if not paths:
        raise ValueError("Which file? Give the Crexi export to import (CSV or XLSX).")
    if run.dry_run:
        return _preview(run, paths)
    files: list[dict] = []
    for i, path in enumerate(paths):
        sub = _scaled(run.progress, 0.7 * i / len(paths), 0.7 * (i + 1) / len(paths))
        files.append(_import_one(run, path, sub))
    imported = [f for f in files if f["ok"]]
    ranked = after_import(run.conn, run.market, _scaled(run.progress, 0.7, 1.0)) if imported else None
    counts = {k: sum(f.get(k, 0) for f in imported) for k in IMPORT_COUNTS}
    counts["files"] = len(imported)
    message = _import_message(files, counts)
    if ranked:
        message += f" {ranked['message']}"
        message += _rent_next(run.conn, run.market)
    warnings = [w for f in files for w in f["warnings"]] + (ranked["warnings"] if ranked else [])
    return _result("import", ok=all(f["ok"] for f in files) and (ranked is None or ranked["ok"]), message=message,
                   warnings=warnings, counts=counts, files=files, after_import=ranked)


def _import_one(run: StepRun, path: Path, progress: Progress) -> dict:
    try:
        res = import_file(run.conn, run.market, path, limit=run.limit, progress=progress)
    except IngestError as exc:
        return {"file": path.name, "ok": False, "message": f"{path.name}: {exc}", "warnings": []}
    prefix = f"{res['filename']}: "
    message = (f"{prefix}{res['message']}" if res.get("provider") == "bluemoon" else   # edits of our own export
               f"{prefix}{res['rows_total']:,} rows: {res['rows_new']:,} new, {res['rows_updated']:,} "
               f"updated{_unchanged_text(res.get('rows_unchanged'))}, {res['rows_skipped']:,} skipped.")
    return {"file": res["filename"], "ok": True, "import_id": res["import_id"], "provider": res.get("provider"),
            **{k: int(res.get(k) or 0) for k in IMPORT_COUNTS},
            "unmapped_columns": res.get("unmapped_columns") or [],
            "warnings": [prefix + w for w in res.get("warnings") or []], "message": message}


def _unchanged_text(n: Any) -> str:
    n = int(n or 0)
    return f" ({n:,} of them already up to date)" if n else ""


def _rent_next(conn: sqlite3.Connection, slug: str) -> str:
    """The import stays offline; say how to run the free HUD rent check when it has rents to fill in."""
    market = _market(conn, slug)
    if not get_settings().asset_class(market["asset_class"]).rent_enabled:
        return ""
    missing = scalar(conn, "SELECT COUNT(*) FROM properties p LEFT JOIN rent_fits r ON r.property_id = p.id "
                           "WHERE p.market_id = ? AND r.property_id IS NULL", (market["id"],))
    if not missing:
        return ""
    return f" Next: uv run run.py rent --market {slug} (free; the scores update by themselves)."


def _import_message(files: list[dict], counts: dict) -> str:
    failed = [f["message"] for f in files if not f["ok"]]
    edits = [f["message"] for f in files if f["ok"] and f.get("provider") == "bluemoon"]
    if edits and len(edits) == counts["files"]:   # only our own exports with edits: say what changed
        return " ".join(edits + (["Not imported: " + " ".join(failed)] if failed else []))
    parts = []
    if counts["files"]:
        parts.append(f"Imported {counts['files']} file{'s' if counts['files'] != 1 else ''}: "
                     f"{counts['rows_new']:,} new, {counts['rows_updated']:,} updated"
                     f"{_unchanged_text(counts.get('rows_unchanged'))}, {counts['rows_skipped']:,} skipped "
                     f"(nothing deleted).")
    if failed:
        parts.append("Not imported: " + " ".join(failed))
    return " ".join(parts)


def _preview(run: StepRun, paths: list[Path]) -> dict:
    previews, warnings, lines = [], [], []
    ok = True
    for path in paths:
        try:
            p = preview_file(path, market_slug=run.market, conn=run.conn)
        except IngestError as exc:
            ok = False
            lines.append(f"{path.name}: {exc}")
            continue
        missing = ", ".join(m["label"] for m in p["missing_fields"]) or "none"
        if p.get("provider") == "bluemoon":       # our own export with edits: say what would change
            lines.append(f"{p['filename']}: {p['message']}")
        else:
            lines.append(f"{p['filename']}: {p['rows']:,} rows ({p['provider_label']}); {len(p['column_map']):,} "
                         f"columns matched, {len(p['unmapped_columns']):,} kept as-is; missing: {missing}.")
        warnings += [f"{p['filename']}: {w}" for w in p.get("warnings") or []]
        previews.append({k: p[k] for k in ("filename", "sheet", "provider", "provider_label", "rows", "column_map",
                                            "unmapped_columns", "missing_fields")})
    return _result("import", ok=ok, dry_run=True, message="Preview only, nothing saved. " + " ".join(lines),
                   warnings=warnings, counts={"files": len(previews)}, previews=previews)


def _run_owners(run: StepRun) -> dict:
    if run.dry_run:
        return _local_dry_run(run, "owners")
    return _from_module("owners", owners.compute_owners(run.conn, run.market))


def _run_drive(run: StepRun) -> dict:
    if not run.dry_run and not run.confirm:
        waiting = _geocoding_needs_ok(run)
        if waiting:
            return waiting
    raw = drive.compute_drive_times(run.conn, run.market, limit=run.limit, dry_run=run.dry_run,
                                    progress=run.progress, confirm=run.confirm)
    return _from_module("drive", raw)


def _geocoding_needs_ok(run: StepRun) -> dict | None:
    """The drive step places properties without coordinates before its own OK check, and with a Google key
    the addresses the free Census geocoder can't place go to Google. Ask first when that could be a big run."""
    geo_est = geo.geocode_estimate(run.conn, run.market, run.limit)
    if int(geo_est.get("google_max") or 0) <= _confirm_over():
        return None
    est = drive.drive_estimate(run.conn, run.market, run.limit)
    est.update(dry_run=False, needs_confirm=True,
               message=f"Waiting for your OK: {geo_est['message']} {est['message']} Run again with confirm (CLI: --yes).")
    return _from_module("drive", est)


def _run_drive_estimate(run: StepRun) -> dict:
    """Free straight-line drive times for properties that have none (never replaces a Google answer)."""
    market = _market(run.conn, run.market)
    if market.get("origin_lat") is None or market.get("origin_lng") is None:
        return _result("drive", ok=True, message="No drive estimates: this market has no starting point in "
                                                 "config.yaml (markets: origin).")
    saved = _fill_drive_estimates(run.conn, market)
    missing = int(scalar(run.conn, "SELECT COUNT(*) FROM properties WHERE market_id = ? AND (lat IS NULL OR "
                                   "lng IS NULL)", (market["id"],)) or 0)
    total = _property_count(run.conn, market["id"])
    message = (f"Saved free straight-line drive estimates for {saved:,} "
               f"propert{'y' if saved == 1 else 'ies'}." if saved else "Every property already had a drive time.")
    if missing:
        message += f" No map location yet: {missing:,} (the drive step looks them up)."
    return _result("drive", ok=True, message=message,
                   counts={"properties": total, "estimated": saved, "kept": total - saved - missing,
                           "missing_coordinates": missing})


def _fill_drive_estimates(conn: sqlite3.Connection, market: dict) -> int:
    """Estimate rows for properties with coordinates but no drive time (or an estimate from an old origin)."""
    cfg = drive.drive_config()
    olat, olng = round(float(market["origin_lat"]), 5), round(float(market["origin_lng"]), 5)
    origin_key = drive.coord_key(olat, olng)
    todo = rows(conn, "SELECT p.id, p.lat, p.lng FROM properties p LEFT JOIN drive_times d ON d.property_id = p.id "
                      "WHERE p.market_id = ? AND p.lat IS NOT NULL AND p.lng IS NOT NULL AND (d.property_id IS NULL "
                      "OR (d.method = ? AND COALESCE(d.origin_key, '') <> ?))",
                (market["id"], drive.ESTIMATE, origin_key))
    now = now_iso()
    with conn:
        for p in todo:
            # Same formula and rounding the drive step uses, so its next run sees these as finished.
            minutes, meters = drive._estimate(round(float(p["lat"]), 5), round(float(p["lng"]), 5), olat, olng, cfg)
            upsert(conn, "drive_times", "property_id", {
                "property_id": p["id"], "minutes": minutes, "meters": meters, "method": drive.ESTIMATE,
                "origin_key": origin_key, "fetched_at": now})
    return len(todo)


def _run_rent(run: StepRun) -> dict:
    raw = rent.compute_rent_fits(run.conn, run.market, progress=run.progress, limit=run.limit, dry_run=run.dry_run)
    return _from_module("rent", raw)


def _run_photos(run: StepRun) -> dict:
    if not run.dry_run:
        _fresh_scores(run.conn, run.market)
    raw = fetch_imagery(run.conn, run.market, limit=run.limit, dry_run=run.dry_run, progress=run.progress,
                        confirm=run.confirm)
    return _from_module("photos", raw)


def _run_classify(run: StepRun) -> dict:
    raw = classify_properties(run.conn, run.market, limit=run.limit, dry_run=run.dry_run, progress=run.progress,
                              confirm=run.confirm)
    return _from_module("classify", raw)


def _run_calibrate(run: StepRun) -> dict:
    raw = imagery_calibrate(run.conn, run.market, limit=run.limit, dry_run=run.dry_run, confirm=run.confirm,
                            progress=run.progress)
    return _from_module("calibrate", raw)


def _run_score(run: StepRun) -> dict:
    if run.dry_run:
        return _local_dry_run(run, "score")
    return _from_module("score", compute_scores(run.conn, run.market, run.progress))


def _run_drill(run: StepRun) -> dict:
    if run.dry_run:
        return _local_dry_run(run, "drill")
    raw = engine.run_auto_stages(run.conn, run.market, progress=run.progress)
    raw["changed"] = engine.refresh_states(run.conn, run.market)["changed"]
    return _from_module("drill", raw)


def _run_export(run: StepRun) -> dict:
    from . import exports  # imported here: exports itself uses pipeline helpers
    market = _market(run.conn, run.market)
    new_handoffs = int(scalar(run.conn, "SELECT COUNT(*) FROM drill_state d JOIN properties p ON p.id = "
                                        "d.property_id WHERE p.market_id = ? AND d.status = 'confirmed' AND "
                                        "d.confirmed_at IS NOT NULL AND d.handed_off_at IS NULL",
                              (market["id"],)) or 0)
    if run.dry_run:   # nothing is written and nobody is marked as handed off
        folder = get_settings().path("output") / run.market   # the same default as exports.export_market
        n = len(exports.EXPORT_FILES)
        msg = f"Preview only, nothing saved: would write {n} files to {folder}."
        if new_handoffs:
            msg += (f" {new_handoffs:,} newly confirmed owner{'s' if new_handoffs != 1 else ''} would be "
                    f"marked as handed off to the CRM.")
        return _result("export", ok=True, dry_run=True, message=msg, counts={"files": n, "handed_off": new_handoffs},
                       files={}, folder=str(folder))
    files = exports.export_market(run.conn, run.market, progress=run.progress)
    folder = next(iter(files.values())).parent if files else None
    message = f"Saved {len(files)} files to {folder}."
    if new_handoffs:
        message += (f" {new_handoffs:,} newly confirmed owner{'s are' if new_handoffs != 1 else ' is'} in "
                    f"crm_handoff.csv and now marked as handed off to the CRM.")
    return _result("export", ok=True, message=message, counts={"files": len(files), "handed_off": new_handoffs},
                   files={name: str(p) for name, p in files.items()}, folder=str(folder) if folder else None)


def _run_estimate(run: StepRun) -> dict:
    est = estimate(run.conn, run.market, run.limit)
    return _result("estimate", ok=True, dry_run=True, message=est["message"], estimate=est,
                   counts={"total_usd": est["total_usd"], "claude_plan_calls": est["claude_plan_calls"]})


def _run_after_import(run: StepRun) -> dict:
    return after_import(run.conn, run.market, run.progress)


def _run_all_steps(run: StepRun) -> dict:
    if run.dry_run:
        est = estimate(run.conn, run.market, run.limit)
        return _result("all", ok=True, dry_run=True, message=est["message"], estimate=est)
    results = _run_plan(ALL_PLAN, run)
    waiting = [r["step"] for r in results if r["needs_confirm"]]
    failed = [r for r in results if _is_failure(r)]
    warnings = _collect_warnings(results)
    warnings += [f"{r['label']}: {r['message']}" for r in results if not r["ok"] and not _is_failure(r)]
    message = _ranked_message(_last(results, "score"), _last(results, "drill"))
    extra: dict[str, Any] = {}
    if waiting:
        est = estimate(run.conn, run.market, run.limit)
        extra["estimate"] = est
        names = ", ".join(step_label(s).lower() for s in waiting)
        message = f"Everything free is done. Waiting for your OK before: {names}. {est['message']} {message}"
    elif not failed:
        message = f"All steps done. {message}"
    if failed:
        message = "Some steps failed: " + " ".join(f"{r['label']}: {r['message']}" for r in failed) + f" {message}"
    return _result("all", ok=not failed, needs_confirm=bool(waiting), message=message, warnings=warnings,
                   steps=results, counts=(_last(results, "score") or {}).get("counts", {}), **extra)


def _is_failure(result: dict) -> bool:
    """In `all`, a paid step that can't run yet (no key, not signed in) is only a note; a free step that
    fails, or any step that crashed, is a failure."""
    return not result["ok"] and (result["step"] not in PAID_STEPS or bool(result.get("crashed")))


RUNNERS: dict[str, Callable[[StepRun], dict]] = {
    "import": _run_import, "owners": _run_owners, "drive": _run_drive, "drive_estimate": _run_drive_estimate,
    "rent": _run_rent, "photos": _run_photos, "classify": _run_classify, "score": _run_score,
    "drill": _run_drill, "all": _run_all_steps, "calibrate": _run_calibrate, "export": _run_export,
    "estimate": _run_estimate, "after_import": _run_after_import,
}


def _run_plan(plan: tuple[tuple[str, float], ...], run: StepRun) -> list[dict]:
    """Run steps in order. Once a paid step waits for an OK, later paid steps wait too; free steps still run.
    A failing step is reported and the rest still run (a cancel still stops everything)."""
    results: list[dict] = []
    start = 0.0
    waiting_on: str | None = None
    for key, share in plan:
        _tell(run.progress, start, f"{step_label(key)}...")  # also where a cancel lands between steps
        sub = replace(run, progress=_scaled(run.progress, start, start + share))
        start += share
        if key in PAID_STEPS and waiting_on:
            results.append(_result(key, ok=True, needs_confirm=True,
                                   message=f"Not run yet: waiting for your OK on {step_label(waiting_on).lower()}."))
            continue
        res = _guarded(key, sub)
        results.append(res)
        if res["needs_confirm"] and key in PAID_STEPS:
            waiting_on = key
    return results


def _guarded(key: str, run: StepRun) -> dict:
    try:
        return RUNNERS[key](run)
    except Exception as exc:  # report and carry on with the other steps; Cancelled is not an Exception
        log.exception("Step %s failed for %s", key, run.market)
        if isinstance(exc, ValueError):
            return _result(key, ok=False, message=str(exc))
        return _result(key, ok=False, crashed=True, message=f"It stopped with an error ({type(exc).__name__}: {exc}).")


# --- estimates ---------------------------------------------------------------------------------

def _estimate_line(step: str, *, paid: bool, message: str, properties: int = 0, to_do: int = 0,
                   skus: list[dict] | None = None, usd: float = 0.0, needs_confirm: bool = False,
                   **extra: Any) -> dict:
    return {"step": step, "label": step_label(step), "paid": paid, "properties": properties, "to_do": to_do,
            "skus": skus or [], "usd": round(usd, 2), "needs_confirm": needs_confirm, "message": message, **extra}


def _estimate_or_note(step: str, build: Callable[[], dict]) -> dict:
    try:
        return build()
    except ValueError as exc:  # e.g. a config value the step can't use
        return _estimate_line(step, paid=step in PAID_STEPS, message=str(exc), error=str(exc))


def _estimate_local(conn: sqlite3.Connection, market: dict, step: str) -> dict:
    n = _property_count(conn, market["id"])
    return _estimate_line(step, paid=False, properties=n, to_do=n,
                          message=f"Free (runs on this computer) for {n:,} propert{'y' if n == 1 else 'ies'}.")


def _sku(cost: dict | None) -> list[dict]:
    if not cost or not cost.get("units"):
        return []
    return [{**cost, "label": SKU_LABELS.get(cost.get("sku", ""), cost.get("sku", ""))}]


def _estimate_drive(conn: sqlite3.Connection, slug: str, limit: int | None) -> dict:
    est = drive.drive_estimate(conn, slug, limit)
    geo_est = est.get("geocode") or {}
    skus = _sku(est.get("cost")) + _sku(geo_est.get("cost"))
    message = est["message"]
    if geo_est.get("properties"):
        message += f" {geo_est['message']}"
    return _estimate_line("drive", paid=est["provider"] == drive.PROVIDER, properties=est["properties"],
                          to_do=est["to_fetch"], skus=skus, usd=sum(s["usd"] for s in skus),
                          needs_confirm=bool(est["needs_confirm"]), message=message, provider=est["provider"])


def _estimate_rent(conn: sqlite3.Connection, slug: str, limit: int | None) -> dict:
    est = rent.compute_rent_fits(conn, slug, limit=limit, dry_run=True)
    return _estimate_line("rent", paid=False, properties=est.get("properties", 0),
                          to_do=est.get("with_location", est.get("properties", 0)), message=est["message"])


def _estimate_photos(conn: sqlite3.Connection, slug: str, limit: int | None) -> dict:
    est = imagery_estimate(conn, slug, limit)
    if not est.get("enabled"):
        return _estimate_line("photos", paid=False, message=est["message"], enabled=False)
    cost = est.get("cost") or {}
    skus = _sku(cost.get("street_view")) + _sku(cost.get("satellite"))
    message = est["message"]
    if _scores_stale(conn, slug):
        message += (" The scores are older than the latest drive times or rents; the photo run re-scores first, "
                    "so the buy box (and this count) may change.")
    return _estimate_line("photos", paid=True, properties=est["properties"], to_do=est["to_fetch"], skus=skus,
                          usd=cost.get("usd", 0.0), needs_confirm=bool(est["needs_confirm"]), message=message,
                          enabled=True, has_key=bool(est.get("has_key")))


def _estimate_classify(conn: sqlite3.Connection, slug: str, limit: int | None, photos: dict) -> dict:
    est = classify_estimate(conn, slug, limit)
    if not est.get("enabled"):
        return _estimate_line("classify", paid=False, message=est["message"], enabled=False)
    cfg = vision_config()
    calls = int(est.get("calls") or 0)
    later = min(int(photos.get("to_do") or 0), max(0, cfg.max_calls_per_run - calls))
    on_plan = est.get("backend") == "claude_cli"
    api_usd = 0.0
    if est.get("backend") == "anthropic_api":
        api_usd = round(float(est.get("est_cost_usd") or 0.0) + later * cfg.anthropic_api.est_cost_per_call_usd, 2)
    message = est["message"]
    if later:
        message += f" Up to {later:,} more once the new photos arrive."
    return _estimate_line("classify", paid=True, properties=est.get("properties", 0), to_do=calls, usd=api_usd,
                          needs_confirm=bool(est.get("needs_confirm")), message=message, enabled=True,
                          backend=est.get("backend"), calls=calls, calls_after_photos=later,
                          plan_calls=calls + later if on_plan else 0, api_usd=api_usd, ready=est.get("ready", True))


def _estimate_summary(est: dict) -> str:
    parts = [f"About ${est['total_usd']:,.2f} in charges after Google's free monthly calls"]
    if est["claude_api_usd"]:
        parts[0] += f" (including about ${est['claude_api_usd']:,.2f} on the pay-per-use Anthropic API)"
    if est["claude_plan_calls"]:
        parts.append(f"plus up to {est['claude_plan_calls']:,} photo checks on your Claude plan (no per-call bill)")
    text = ", ".join(parts) + "."
    if est["needs_confirm_steps"]:
        names = ", ".join(step_label(s).lower() for s in est["needs_confirm_steps"])
        text += f" Needs your OK first (more than {est['confirm_over']:,} properties): {names}."
    if not est["has_google_key"]:
        text += " No Google key yet: drive times are free estimates and photos can't be fetched."
    return text


def _line(lines: list[dict], step: str) -> dict:
    return next((x for x in lines if x["step"] == step), {})


# --- results -----------------------------------------------------------------------------------

def _result(step: str, *, ok: bool, message: str, dry_run: bool = False, needs_confirm: bool = False,
            warnings: Iterable[str] | None = None, counts: dict | None = None, **extra: Any) -> dict:
    return {"step": step, "label": step_label(step), "ok": ok, "dry_run": dry_run, "needs_confirm": needs_confirm,
            "message": message, "warnings": [w for w in (warnings or []) if w], "counts": counts or {}, **extra}


def _from_module(step: str, raw: dict) -> dict:
    """A step result from a backend module's result (which always has a plain-English message)."""
    dry = bool(raw.get("dry_run"))
    waiting = bool(raw.get("needs_confirm")) and not dry
    message = raw.get("message") or ""
    warnings = [w for w in raw.get("warnings") or [] if w and w not in message]  # don't say it twice
    out = _result(step, ok=not raw.get("error"), dry_run=dry, needs_confirm=waiting, message=message,
                  warnings=warnings, counts=_counts(step, raw), details=raw)
    if dry or waiting:
        out["estimate"] = raw
    return out


def _counts(step: str, raw: dict) -> dict:
    out: dict[str, Any] = {}
    for key in COUNT_KEYS.get(step, ()):
        value = raw.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, dict):
            out.update({f"{key}_{k}": v for k, v in value.items() if isinstance(v, (int, float))})
    usd = _cost_usd(raw)
    if usd is not None:
        out["cost_usd"] = usd
    return out


def _cost_usd(raw: dict) -> float | None:
    cost = raw.get("cost")
    if isinstance(cost, dict) and isinstance(cost.get("usd"), (int, float)):
        return float(cost["usd"])
    if isinstance(raw.get("est_cost_usd"), (int, float)):
        return float(raw["est_cost_usd"])
    return None


def _local_dry_run(run: StepRun, step: str) -> dict:
    n = _property_count(run.conn, _market(run.conn, run.market)["id"])
    return _result(step, ok=True, dry_run=True, counts={"properties": n},
                   message=f"Free (runs on this computer): would update {n:,} propert{'y' if n == 1 else 'ies'}.")


def _collect_warnings(results: list[dict]) -> list[str]:
    seen: dict[str, None] = {}
    for r in results:
        for w in r.get("warnings") or []:
            seen.setdefault(w, None)
    return list(seen)


def _last(results: list[dict], step: str) -> dict | None:
    return next((r for r in reversed(results) if r["step"] == step and not r.get("dry_run")), None)


def _ranked_message(score: dict | None, drill_result: dict | None) -> str:
    parts = []
    if score and score["ok"]:
        c = score["counts"]
        n, box = int(c.get("properties", 0)), int(c.get("in_buybox", 0))
        parts.append(f"{box:,} of {n:,} properties fit the buy box and are ranked." if n else
                     "No properties in this market yet. Import a Crexi export first.")
    if drill_result and drill_result["ok"] and drill_result["counts"].get("properties"):
        c = drill_result["counts"]
        parts.append(f"Contact levels: {c.get('levels_1', 0):,} no owner data, {c.get('levels_2', 0):,} possible "
                     f"owner, {c.get('levels_3', 0):,} confirmed.")
    return " ".join(parts)


def _drive_is_approximate(conn: sqlite3.Connection, slug: str) -> bool:
    market_id = _market(conn, slug)["id"]
    return bool(scalar(conn, "SELECT COUNT(*) FROM drive_times d JOIN properties p ON p.id = d.property_id "
                             "WHERE p.market_id = ? AND d.method = ?", (market_id, drive.ESTIMATE)))


# --- small helpers -----------------------------------------------------------------------------

def _market(conn: sqlite3.Connection, slug: str) -> dict:
    try:
        return market_by_slug(conn, slug)
    except KeyError:
        known = ", ".join(r["slug"] for r in rows(conn, "SELECT slug FROM markets ORDER BY id"))
        raise ValueError(f"Unknown market '{slug}'. Markets: {known}.") from None


def _property_count(conn: sqlite3.Connection, market_id: int) -> int:
    return int(scalar(conn, "SELECT COUNT(*) FROM properties WHERE market_id = ?", (market_id,)) or 0)


def _in_buybox_count(conn: sqlite3.Connection, market_id: int) -> int:
    return int(scalar(conn, "SELECT COUNT(*) FROM scores s JOIN properties p ON p.id = s.property_id "
                            "WHERE p.market_id = ? AND s.in_buybox = 1", (market_id,)) or 0)


def _layer(key: str, label: str, done: Any, total: int, note: str | None = None) -> dict:
    return {"key": key, "label": label, "done": int(done or 0), "total": int(total), "note": note}


def _confirm_over() -> int:
    return int(get_settings().section("safety").get("confirm_over_properties", 50) or 50)


def _clean_limit(limit: Any) -> int | None:
    if limit is None or limit == "":
        return None
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise ValueError(f"The limit must be a whole number (got '{limit}').") from None
    if n < 1:
        raise ValueError("The limit must be 1 or more.")
    return n


def _paths(path: PathArg) -> list[Path]:
    if path is None:
        return []
    if isinstance(path, (str, Path)):
        return [Path(path)]
    return [Path(p) for p in path]


def _tell(progress: Progress, fraction: float, message: str) -> None:
    """Report progress. A broken callback never stops a run; a job's cancel (a BaseException) still does."""
    if progress is None:
        return
    try:
        progress(fraction, message)
    except Exception:
        log.debug("progress callback failed", exc_info=True)


def _scaled(progress: Progress, start: float, end: float) -> Progress:
    """A progress callback for a sub-step that fills [start, end] of the parent's bar."""
    if progress is None:
        return None
    return lambda fraction, message: progress(start + (end - start) * max(0.0, min(1.0, fraction)), message)


__all__ = [
    "ALL_PLAN", "EXTRA_STEPS", "PAID_STEPS", "STEPS", "STEP_ALIASES", "STEP_LABELS", "after_import", "estimate",
    "keys_present", "last_import", "layer_status", "market_status", "normalize_step", "run_all", "run_step",
    "step_label",
]
