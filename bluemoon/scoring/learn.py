"""Tune the Blue Moon multipliers from real outcomes (empirical Bayes).

You mark deals in the app: blue_moon or bought (good outcomes, `scoring.positive_outcomes`), passed or
lost (the rest). For each signal, the learner compares how often each bucket shows up among the good
outcomes with how often it shows up among the other properties in the buy box (the reference group).

The starting multipliers in config/asset_classes.yaml act as a prior worth
`scoring.learn_prior_strength` (30) pseudo-outcomes. For a signal with buckets b:

    q_b      = share of the reference group in bucket b (Laplace-smoothed)
    p0_b     = m0_b x q_b / sum(m0 x q)          bucket share among good outcomes the prior expects
    p_b      = (n_b + k x p0_b) / (n + k)        blended with the n good outcomes seen (n_b in bucket b)
    learned  = m0_b x p_b / p0_b

With no outcomes p_b = p0_b, so the learned multipliers equal the starting ones exactly. With many
outcomes the learned multiplier approaches the observed likelihood ratio (kept on the same scale as the
starting weights). Outcomes are pooled across every market of the same asset class.
"""
from __future__ import annotations

import copy
import logging
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date

from ..config import AssetClass, Settings, get_settings
from ..db import market_by_slug, now_iso, one, rows, upsert
from .blue_moon import SIGNAL_LABELS, active_signal_keys, bucket_label, multipliers, signals_for
from .buybox import ScoringConfig, evaluate, scoring_config

log = logging.getLogger(__name__)

OUTCOMES: tuple[str, ...] = ("blue_moon", "bought", "passed", "lost")
OUTCOME_LABELS: dict[str, str] = {"blue_moon": "Blue Moon deal", "bought": "Bought", "passed": "Passed",
                                  "lost": "Lost to someone else"}


# --- recording outcomes --------------------------------------------------------------------------

def set_outcome(conn: sqlite3.Connection, property_id: int, outcome: str | None, note: str | None = None) -> dict:
    """Record (or clear, with outcome None/"" / "clear") how a deal turned out. Re-score to use it."""
    if not one(conn, "SELECT id FROM properties WHERE id=?", (property_id,)):
        raise KeyError(f"Property {property_id} not found")
    o = (outcome or "").strip().lower()
    if o in ("", "clear", "none"):
        conn.execute("DELETE FROM outcomes WHERE property_id=?", (property_id,))
        conn.commit()
        return {"property_id": property_id, "outcome": None, "message": "Outcome cleared."}
    if o not in OUTCOMES:
        raise ValueError(f"Unknown outcome '{outcome}'. Use one of: {', '.join(OUTCOMES)}.")
    upsert(conn, "outcomes", "property_id", {"property_id": property_id, "outcome": o, "note": note,
                                             "created_at": now_iso()})
    conn.commit()
    return {"property_id": property_id, "outcome": o, "note": note,
            "message": f"Saved: {OUTCOME_LABELS[o]}. Re-run scoring to update the weights."}


# --- the blend (pure) ----------------------------------------------------------------------------

def blend(start: Mapping[str, float], positive_counts: Mapping[str, int], reference_counts: Mapping[str, int],
          prior_strength: float, clip: tuple[float, float] = (0.02, 20.0)) -> dict[str, dict[str, float]]:
    """Empirical-Bayes blend for one signal. Returns {bucket: {learned, p0, p, q}}.

    `start`: bucket -> starting multiplier. Counts for buckets not in `start` are ignored.
    """
    buckets = [b for b in start if start[b] is not None]
    if not buckets:
        return {}
    ref_total = sum(reference_counts.get(b, 0) for b in buckets)
    q = {b: (reference_counts.get(b, 0) + 1) / (ref_total + len(buckets)) for b in buckets}
    z = sum(max(start[b], 0.0) * q[b] for b in buckets)
    n = sum(positive_counts.get(b, 0) for b in buckets)
    k = max(float(prior_strength), 0.0)
    out: dict[str, dict[str, float]] = {}
    for b in buckets:
        m0 = max(start[b], 0.0)
        p0 = (m0 * q[b] / z) if z > 0 else 1.0 / len(buckets)
        if n + k <= 0 or p0 <= 0:
            learned = m0
            p = p0
        else:
            p = (positive_counts.get(b, 0) + k * p0) / (n + k)
            learned = m0 * p / p0
        lo, hi = clip
        learned = m0 if (n == 0) else min(max(learned, lo), hi)
        out[b] = {"learned": round(learned, 4), "p0": round(p0, 6), "p": round(p, 6), "q": round(q[b], 6)}
    return out


# --- gathering data ------------------------------------------------------------------------------

def _pooled_markets(conn: sqlite3.Connection, asset_class: str) -> list[dict]:
    return rows(conn, "SELECT * FROM markets WHERE asset_class=? ORDER BY id", (asset_class,))


def _outcome_rows(conn: sqlite3.Connection, market_ids: list[int]) -> list[dict]:
    if not market_ids:
        return []
    marks = ",".join("?" for _ in market_ids)
    return rows(conn, f"SELECT o.property_id, o.outcome, p.market_id FROM outcomes o "
                      f"JOIN properties p ON p.id=o.property_id WHERE p.market_id IN ({marks})", market_ids)


def _buckets_by_property(conn: sqlite3.Connection, markets: list[dict], ac: AssetClass, settings: Settings,
                         today: date | None) -> dict[int, tuple[bool, dict[str, str | None]]]:
    """property id -> (in buy box, {signal: bucket}). Buckets don't depend on the weights."""
    from .rank import load_scoring_rows  # lazy: rank imports this module
    by_id = {m["id"]: m for m in markets}
    out: dict[int, tuple[bool, dict[str, str | None]]] = {}
    for prop, enrich in load_scoring_rows(conn, list(by_id)):
        ev = evaluate(prop, enrich, ac, by_id[prop["market_id"]], settings)
        sigs = signals_for(prop, enrich, ac, settings, today=today)
        out[prop["id"]] = (bool(ev["in_buybox"]), {s["key"]: s["bucket"] for s in sigs})
    return out


def _counts(buckets: dict[int, tuple[bool, dict[str, str | None]]], positives: set[int],
            keys: list[str]) -> tuple[dict[str, Counter[str]], dict[str, Counter[str]]]:
    pos: dict[str, Counter[str]] = defaultdict(Counter)
    ref: dict[str, Counter[str]] = defaultdict(Counter)
    in_box_ref = [pid for pid, (inb, _) in buckets.items() if inb and pid not in positives]
    ref_ids = in_box_ref or [pid for pid in buckets if pid not in positives]
    for pid in positives:
        if pid in buckets:
            for k in keys:
                b = buckets[pid][1].get(k)
                if b is not None:
                    pos[k][b] += 1
    for pid in ref_ids:
        for k in keys:
            b = buckets[pid][1].get(k)
            if b is not None:
                ref[k][b] += 1
    return pos, ref


def _start_weights(ac: AssetClass) -> dict:
    return copy.deepcopy(dict(ac.signals))


# --- public API ----------------------------------------------------------------------------------

def learned_weights(conn: sqlite3.Connection, market_slug: str, settings: Settings | None = None,
                    today: date | None = None) -> dict:
    """Multipliers tuned from outcomes, pooled across markets of the same asset class.

    Returns `{market, asset_class, n, positives, learned, prior_strength, weights, start, changes, message}`.
    `weights` has the same shape as `asset_class.signals`. With no outcomes: the starting weights, n=0.
    """
    s = settings or get_settings()
    cfg = scoring_config(s)
    market = market_by_slug(conn, market_slug)
    ac = s.asset_class(market["asset_class"])
    start = _start_weights(ac)
    markets = _pooled_markets(conn, ac.key)
    outcomes = _outcome_rows(conn, [m["id"] for m in markets])
    base = {"market": market_slug, "asset_class": ac.key, "prior_strength": cfg.learn_prior_strength,
            "n": len(outcomes), "by_outcome": dict(Counter(o["outcome"] for o in outcomes)),
            "start": start, "weights": copy.deepcopy(start), "changes": [], "per_signal": {}}
    positives = {o["property_id"] for o in outcomes if o["outcome"] in cfg.positive_outcomes}
    base["positives"] = len(positives)
    if not outcomes or not positives:
        base["learned"] = False
        base["message"] = (_no_outcomes_text() if not outcomes else
                           f"{len(outcomes)} outcome(s) recorded, but none are good deals "
                           f"({', '.join(cfg.positive_outcomes)}) yet, so the starting weights stay.")
        return base

    keys = active_signal_keys(ac)
    buckets = _buckets_by_property(conn, markets, ac, s, today)
    pos, ref = _counts(buckets, positives, keys)
    weights = copy.deepcopy(start)
    changes: list[dict] = []
    per_signal: dict[str, dict] = {}
    for k in keys:
        mults = multipliers(start.get(k))
        res = blend(mults, pos[k], ref[k], cfg.learn_prior_strength,
                    (cfg.learn_min_multiplier, cfg.learn_max_multiplier))
        per_signal[k] = {b: {**v, "start": mults[b], "positives": pos[k].get(b, 0), "reference": ref[k].get(b, 0)}
                         for b, v in res.items()}
        for b, v in res.items():
            weights.setdefault(k, {})[b] = v["learned"]
            if abs(v["learned"] - mults[b]) >= 0.005:
                changes.append({"signal": k, "bucket": b, "start": mults[b], "learned": v["learned"],
                                "positives": pos[k].get(b, 0), "reference": ref[k].get(b, 0)})
    changes.sort(key=lambda c: -abs(c["learned"] - c["start"]))
    base.update(weights=weights, changes=changes, per_signal=per_signal, learned=True,
                message=(f"Learned from {len(positives)} good outcome(s) out of {len(outcomes)} recorded. "
                         f"Each starting weight counts as {cfg.learn_prior_strength:g} outcomes, so a few deals "
                         f"nudge the score and many deals move it."))
    return base


def _no_outcomes_text() -> str:
    return ("No outcomes recorded yet, so the score uses the starting weights from config/asset_classes.yaml. "
            "Mark deals as Blue Moon, bought, passed or lost and the weights will tune themselves.")


def model_summary(conn: sqlite3.Connection, market_slug: str, settings: Settings | None = None) -> dict:
    """Everything the Score screen shows: base rate, each signal's buckets with starting and learned
    multipliers, outcome counts and how much data the scores rest on."""
    s = settings or get_settings()
    cfg: ScoringConfig = scoring_config(s)
    market = market_by_slug(conn, market_slug)
    ac = s.asset_class(market["asset_class"])
    lw = learned_weights(conn, market_slug, settings=s)
    signals = []
    for k in active_signal_keys(ac):
        start = multipliers(lw["start"].get(k))
        learned = multipliers(lw["weights"].get(k))
        stats = lw["per_signal"].get(k, {})
        signals.append({
            "key": k, "label": SIGNAL_LABELS.get(k, k),
            "buckets": [{"bucket": b, "label": bucket_label(k, b), "start": m, "learned": learned.get(b, m),
                         "positives": stats.get(b, {}).get("positives", 0),
                         "reference": stats.get(b, {}).get("reference", 0)} for b, m in start.items()],
        })
    scored = one(conn, "SELECT COUNT(*) AS n, AVG(CAST(coverage_have AS REAL) / NULLIF(coverage_total, 0)) AS cov, "
                       "SUM(in_buybox) AS inb FROM scores s JOIN properties p ON p.id=s.property_id "
                       "WHERE p.market_id=?", (market["id"],)) or {}
    return {
        "market": market_slug,
        "asset_class": ac.key,
        "asset_label": ac.label,
        "base_rate": cfg.base_rate,
        "prior_strength": cfg.learn_prior_strength,
        "positive_outcomes": list(cfg.positive_outcomes),
        "learned": lw["learned"],
        "outcomes": {"total": lw["n"], "positive": lw["positives"], "by_outcome": lw["by_outcome"],
                     "labels": OUTCOME_LABELS},
        "signals": signals,
        "changes": lw["changes"][:10],
        "scored": {"properties": scored.get("n") or 0, "in_buybox": scored.get("inb") or 0,
                   "coverage_avg": round(scored["cov"], 3) if scored.get("cov") is not None else None},
        "message": lw["message"],
        "how_it_works": (f"Every property starts at a {cfg.base_rate:.1%} chance of being a Blue Moon deal. "
                         "Each thing we know multiplies the odds up or down; anything we don't know is skipped, "
                         "never counted against it. The multipliers start from config/asset_classes.yaml and "
                         "learn from the outcomes you record."),
    }
