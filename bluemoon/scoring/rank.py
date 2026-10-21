"""Score and rank every property in a market (table `scores`).

Every property gets a row, including the ones that fail a filter (in_buybox=0, rank NULL, with the
reasons). Nothing is deleted. Properties in the buy box are ranked 1..n by:
Blue Moon score (high first), then photo-check final_p (high first), then drive minutes (short first).

When you have recorded outcomes (deals marked Blue Moon / bought / passed / lost), the score uses the
learned multipliers from learn.py instead of the starting ones in config/asset_classes.yaml.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any

from ..config import Settings, get_settings
from ..db import dumps, market_by_slug, now_iso, rows, scalar, upsert
from .blue_moon import coverage, probability, signals_for
from .buybox import EXCLUSION_LABELS, EXCLUSION_REASONS, evaluate, scoring_config

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

ENRICH_TABLES: dict[str, str] = {"drive": "drive_times", "rent": "rent_fits", "visual": "visual_checks",
                                 "owner": "owners"}
_CHUNK = 900  # SQLite parameter limit is 999


def _tell(progress: Progress, fraction: float, message: str) -> None:
    if progress is None:
        return
    try:
        progress(max(0.0, min(1.0, fraction)), message)
    except Exception:  # a broken progress callback must never stop scoring
        log.debug("progress callback failed", exc_info=True)


def _chunks(ids: list[int]) -> Iterable[list[int]]:
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def load_scoring_rows(conn: sqlite3.Connection, market_ids: Iterable[int]) -> list[tuple[dict, dict]]:
    """[(properties row, {"drive", "rent", "visual", "owner"} rows or None)] for these markets, by id."""
    ids = list(market_ids)
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    props = rows(conn, f"SELECT * FROM properties WHERE market_id IN ({marks}) ORDER BY id", ids)
    pids = [p["id"] for p in props]
    layers: dict[str, dict[int, dict]] = {k: {} for k in ENRICH_TABLES}
    for key, table in ENRICH_TABLES.items():
        for chunk in _chunks(pids):
            q = ",".join("?" for _ in chunk)
            for r in rows(conn, f"SELECT * FROM {table} WHERE property_id IN ({q})", chunk):
                layers[key][r["property_id"]] = r
    return [(p, {k: layers[k].get(p["id"]) for k in ENRICH_TABLES}) for p in props]


def _ensure_owners(conn: sqlite3.Connection, market_id: int, market_slug: str, settings: Settings) -> bool:
    """Run the owner step first when some properties have no owner row yet. Returns True if it ran."""
    missing = scalar(conn, "SELECT COUNT(*) FROM properties p LEFT JOIN owners o ON o.property_id=p.id "
                           "WHERE p.market_id=? AND o.property_id IS NULL", (market_id,))
    if not missing:
        return False
    from ..enrich.owners import compute_owners
    compute_owners(conn, market_slug, settings=settings)
    return True


def rank_key(item: dict) -> tuple:
    """Sort key: score high first, then photo final_p high first, then drive minutes short first."""
    p = item.get("visual_p")
    minutes = item.get("drive_minutes")
    return (-(item.get("score") or 0.0), -(p if p is not None else -1.0),
            minutes if minutes is not None else float("inf"), item["property_id"])


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "median": None, "p90": None, "max": None, "mean": None}
    v = sorted(values)
    p90 = v[min(len(v) - 1, max(0, round(0.9 * (len(v) - 1))))]
    return {"count": len(v), "min": round(v[0], 6), "median": round(statistics.median(v), 6),
            "p90": round(p90, 6), "max": round(v[-1], 6), "mean": round(statistics.fmean(v), 6)}


def _weights(conn: sqlite3.Connection, market_slug: str, asset_class: Any, settings: Settings) -> tuple[dict, dict]:
    from .learn import learned_weights
    try:
        lw = learned_weights(conn, market_slug, settings=settings)
    except Exception:  # learning must never block scoring; fall back to the starting weights
        log.exception("learned_weights failed; using starting weights")
        return asset_class.signals, {"learned": False, "outcomes": 0, "positives": 0}
    info = {"learned": bool(lw.get("learned")), "outcomes": lw.get("n", 0), "positives": lw.get("positives", 0)}
    return (lw["weights"] if lw.get("learned") else asset_class.signals), info


def compute_scores(conn: sqlite3.Connection, market_slug: str, progress: Progress = None, *,
                   settings: Settings | None = None, today: date | None = None) -> dict:
    """Buy box, colors, Blue Moon score and rank for every property in the market. Safe to re-run."""
    s = settings or get_settings()
    cfg = scoring_config(s)
    market = market_by_slug(conn, market_slug)
    ac = s.asset_class(market["asset_class"])
    _tell(progress, 0.0, "Getting owner facts ready")
    owners_ran = _ensure_owners(conn, market["id"], market_slug, s)
    weights, learn_info = _weights(conn, market_slug, ac, s)
    data = load_scoring_rows(conn, [market["id"]])
    total = len(data)
    _tell(progress, 0.05, f"Scoring {total:,} properties")

    items: list[dict] = []
    for i, (prop, enrich) in enumerate(data):
        ev = evaluate(prop, enrich, ac, market, s)
        sigs = signals_for(prop, enrich, ac, s, weights=weights, today=today)
        have, of = coverage(sigs)
        visual = enrich.get("visual") or {}
        items.append({
            "property_id": prop["id"], "ev": ev, "signals": sigs, "have": have, "of": of,
            "score": round(probability(sigs, cfg.base_rate), 6),
            "visual_p": visual.get("final_p"), "drive_minutes": ev["drive_minutes"],
        })
        if total and i % 250 == 0:
            _tell(progress, 0.05 + 0.75 * i / total, f"Scored {i:,} of {total:,}")

    ranked = sorted((it for it in items if it["ev"]["in_buybox"]), key=rank_key)
    ranks = {it["property_id"]: n for n, it in enumerate(ranked, start=1)}
    _tell(progress, 0.85, "Saving scores")
    _save(conn, items, ranks)
    _tell(progress, 1.0, "Scores saved")
    return _summary(market_slug, ac, items, ranked, owners_ran, learn_info)


def _save(conn: sqlite3.Connection, items: list[dict], ranks: dict[int, int]) -> None:
    now = now_iso()
    with conn:
        for it in items:
            ev = it["ev"]
            upsert(conn, "scores", "property_id", {
                "property_id": it["property_id"],
                "in_buybox": 1 if ev["in_buybox"] else 0,
                "exclusion_reasons": dumps(ev["exclusion_reasons"]),
                "size_value": ev["size_value"],
                "size_basis": ev["size_basis"],
                "size_color": ev["size_color"],
                "visual_color": ev["visual_color"],
                "rent_color": ev["rent_color"],
                "score": it["score"],
                "signals_json": dumps(it["signals"]),
                "coverage_have": it["have"],
                "coverage_total": it["of"],
                "rank": ranks.get(it["property_id"]),
                "computed_at": now,
            })


def _summary(market_slug: str, ac: Any, items: list[dict], ranked: list[dict], owners_ran: bool,
             learn_info: dict) -> dict:
    reasons: Counter[str] = Counter()
    colors: dict[str, Counter[str]] = {"size": Counter(), "visual": Counter(), "rent": Counter()}
    basis: Counter[str] = Counter()
    for it in items:
        ev = it["ev"]
        reasons.update(ev["exclusion_reasons"])
        basis[ev["size_basis"]] += 1
        for axis, counter in colors.items():
            c = ev[f"{axis}_color"]
            if c is not None:
                counter[c] += 1
    in_box = len(ranked)
    result = {
        "market": market_slug,
        "asset_class": ac.key,
        "properties": len(items),
        "in_buybox": in_box,
        "excluded": len(items) - in_box,
        "exclusion_counts": {r: reasons.get(r, 0) for r in EXCLUSION_REASONS},
        "size_basis": dict(basis),
        "colors": {k: dict(v) for k, v in colors.items()},
        "score": _distribution([it["score"] for it in items]),
        "score_in_buybox": _distribution([it["score"] for it in ranked]),
        "coverage_avg": round(statistics.fmean([it["have"] / it["of"] for it in items if it["of"]]), 3)
        if items else None,
        "weights": "learned" if learn_info.get("learned") else "starting",
        "outcomes": learn_info.get("outcomes", 0),
        "owners_computed": owners_ran,
        "top": [it["property_id"] for it in ranked[:10]],
    }
    result["message"] = _message(result)
    return result


def _message(r: dict) -> str:
    if not r["properties"]:
        return "No properties in this market yet. Import a Crexi export first."
    parts = [f"Scored {r['properties']:,} properties. {r['in_buybox']:,} fit the buy box and are ranked "
             f"1-{r['in_buybox']:,}." if r["in_buybox"] else
             f"Scored {r['properties']:,} properties. None fit the buy box yet."]
    if r["excluded"]:
        why = ", ".join(f"{EXCLUSION_LABELS[k].lower()} {n:,}" for k, n in r["exclusion_counts"].items() if n)
        parts.append(f"{r['excluded']:,} were set aside ({why}); nothing was deleted.")
    dist = r["score_in_buybox"] if r["in_buybox"] else r["score"]
    if dist["max"] is not None:
        parts.append(f"Blue Moon scores run from {dist['min']:.1%} to {dist['max']:.1%} "
                     f"(middle {dist['median']:.1%}).")
    parts.append("Using weights learned from your outcomes." if r["weights"] == "learned"
                 else "Using the starting weights (no outcomes recorded yet).")
    return " ".join(parts)
