"""Scoring: Brad's buy box, the Blue Moon score, ranking and learning from outcomes.

- buybox.evaluate(prop, enrich, asset_class, market, settings) -> pass/fail, reasons and colors
- blue_moon.signals_for(...) / probability(...) -> the score and its explanation
- rank.compute_scores(conn, market_slug) -> writes `scores` for every property and ranks the buy box
- learn.learned_weights(conn, market_slug) / model_summary(...) -> multipliers tuned from outcomes
"""
from __future__ import annotations

from .blue_moon import SIGNAL_LABELS, coverage, probability, signals_for
from .buybox import EXCLUSION_LABELS, EXCLUSION_REASONS, SIZE_BASIS_LABELS, evaluate, size_info
from .learn import OUTCOME_LABELS, OUTCOMES, learned_weights, model_summary, set_outcome
from .rank import compute_scores, load_scoring_rows

__all__ = [
    "EXCLUSION_LABELS", "EXCLUSION_REASONS", "OUTCOMES", "OUTCOME_LABELS", "SIGNAL_LABELS", "SIZE_BASIS_LABELS",
    "compute_scores", "coverage", "evaluate", "learned_weights", "load_scoring_rows", "model_summary",
    "probability", "set_outcome", "signals_for", "size_info",
]
