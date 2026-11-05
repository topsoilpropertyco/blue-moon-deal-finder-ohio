"""Age and ownership history for the drawer: how old the building is, how long it has been owned, and what
is known about who built it.

Every figure says how sure it is. A year built gives an approximate age (the year, not a date). A sale
recorded with an exact date gives exact years owned; a sale recorded as a year alone is approximate. Unknown
stays unknown, and a date in the future (or a year that cannot be right) is flagged instead of used. Nothing
here is ever turned into a "single owner" or "local" label, and matching years are never proof that an
owner is the builder: the builder note is only what a person recorded, with where it came from.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from .db import now_iso, one
from .pricing import PricingConfig
from .util import parse_date

BUILDER_STATES = ("supported", "suggested", "contradicted", "unknown")
BUILDER_LABELS = {"supported": "Supported by a source", "suggested": "Suggested, not confirmed",
                  "contradicted": "Contradicted by a source", "unknown": "Unknown"}
AREA_TYPES = ("rentable", "gross")
AREA_LABELS = {"rentable": "Net rentable area", "gross": "Gross building area"}
MAX_TEXT = 500
YEAR_ONLY = re.compile(r"^\s*(\d{4})\s*$")


def age_info(year_built: Any, cfg: PricingConfig) -> dict:
    """{year, age, approximate, flag, text}. age = evaluation year - year built; flag: None | impossible | future."""
    today = cfg.today()
    try:
        year = int(year_built)
    except (TypeError, ValueError):
        return {"year": None, "age": None, "approximate": True, "flag": None, "text": "Age unknown"}
    if year > today.year:
        return {"year": year, "age": None, "approximate": True, "flag": "future",
                "text": f"Year built {year} is in the future, so no age is shown"}
    if year < cfg.earliest_year_built:
        return {"year": year, "age": None, "approximate": True, "flag": "impossible",
                "text": f"Year built {year} can't be right, so no age is shown"}
    age = today.year - year
    return {"year": year, "age": age, "approximate": True, "flag": None,
            "text": f"About {age} years old (built {year}; the year only, so the age is approximate)"}


def ownership_info(sale_date: Any, cfg: PricingConfig) -> dict:
    """{since, years, precision, flag, text}. precision: exact (a full date) | year (a year alone) | None."""
    today = cfg.today()
    unknown = {"since": None, "years": None, "precision": None, "flag": None, "text": "Years owned unknown"}
    if sale_date is None or str(sale_date).strip() == "":
        return unknown
    year_only = YEAR_ONLY.match(str(sale_date))
    if year_only:
        since, precision = year_only.group(1), "year"
        years = today.year - int(since)
        future = int(since) > today.year
    else:
        d = parse_date(sale_date)
        if d is None:
            return unknown
        since, precision = d.isoformat(), "exact"
        years = round((today - d).days / 365.25, 1)
        future = d > today
    if future:
        return {"since": since, "years": None, "precision": precision, "flag": "future",
                "text": f"The recorded sale ({since}) is in the future, so years owned is not shown"}
    if precision == "exact":
        return {"since": since, "years": years, "precision": precision, "flag": None,
                "text": f"Owned {years:g} years (exact: sold {since})"}
    return {"since": since, "years": years, "precision": precision, "flag": None,
            "text": f"About {years} years owned (sale recorded as the year {since} only, so approximate)"}


def history_row(conn: sqlite3.Connection, property_id: int) -> dict:
    return one(conn, "SELECT * FROM property_history WHERE property_id = ?", (property_id,)) or {}


def history_block(conn: sqlite3.Connection, prop: Mapping[str, Any], cfg: PricingConfig) -> dict:
    """Everything the drawer's history lines need for one property."""
    h = history_row(conn, int(prop["id"]))
    state = h.get("builder_state") or "unknown"
    return {
        "age": age_info(prop.get("year_built"), cfg),
        "ownership": ownership_info(prop.get("sale_date"), cfg),
        "builder": {"state": state, "label": BUILDER_LABELS[state], "source": h.get("builder_source"),
                    "note": h.get("builder_note")},
        "sale": {"bundled": bool(h.get("sale_bundled")), "source": h.get("sale_source") or prop.get("source")},
        "area_type": prop.get("building_area_type"),
        "updated_by": h.get("updated_by"), "updated_at": h.get("updated_at"),
    }


def _text(value: Any, what: str) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) > MAX_TEXT:
        raise ValueError(f"The {what} is too long (most {MAX_TEXT} letters).")
    return text or None


def save_history(conn: sqlite3.Connection, property_id: int, changes: Mapping[str, Any], actor: str) -> None:
    """Record what a person knows about a property's history. Only the keys in `changes` are touched
    (builder_state, builder_source, builder_note, sale_bundled, sale_source, building_area_type)."""
    row = history_row(conn, property_id)
    new = {k: row.get(k) for k in ("builder_state", "builder_source", "builder_note", "sale_bundled", "sale_source")}
    if "builder_state" in changes:
        state = changes["builder_state"] or "unknown"
        if state not in BUILDER_STATES:
            raise ValueError(f"The builder status must be one of: {', '.join(BUILDER_STATES)}.")
        new["builder_state"] = state
    for key, what in (("builder_source", "source"), ("builder_note", "note"), ("sale_source", "sale source")):
        if key in changes:
            new[key] = _text(changes[key], what)
    if "sale_bundled" in changes:
        new["sale_bundled"] = 1 if changes["sale_bundled"] else 0
    if new["builder_state"] in ("supported", "contradicted") and not new["builder_source"]:
        raise ValueError("Say where that comes from: a builder claim needs a source.")
    with conn:
        conn.execute(
            "INSERT INTO property_history(property_id, builder_state, builder_source, builder_note, sale_bundled, "
            "sale_source, updated_by, updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(property_id) DO UPDATE SET "
            "builder_state=excluded.builder_state, builder_source=excluded.builder_source, "
            "builder_note=excluded.builder_note, sale_bundled=excluded.sale_bundled, sale_source=excluded.sale_source, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (property_id, new["builder_state"] or "unknown", new["builder_source"], new["builder_note"],
             new["sale_bundled"] or 0, new["sale_source"], actor, now_iso()))
        if "building_area_type" in changes:
            kind = changes["building_area_type"] or None
            if kind is not None and kind not in AREA_TYPES:
                raise ValueError(f"The area type must be one of: {', '.join(AREA_TYPES)}.")
            conn.execute("UPDATE properties SET building_area_type = ? WHERE id = ?", (kind, property_id))


__all__ = ["AREA_LABELS", "AREA_TYPES", "BUILDER_LABELS", "BUILDER_STATES", "age_info", "history_block",
           "history_row", "ownership_info", "save_history"]
