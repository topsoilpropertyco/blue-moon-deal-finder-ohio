"""Corrections made by hand to a property's facts (table property_edits).

Seth fixes a unit count or a year built in a Blue Moon export, in Excel, and imports the file back
(ingest/roundtrip.py). Each change is one row in property_edits: who, when, through which import, the value
before and the new one. The table is append-only; the newest row per (property, field) is the correction in
force. A later Crexi import merges as usual and then puts these corrections back (crexi._update), so a new
Crexi file never quietly undoes them. The original Crexi values stay in raw_json.

Only the fields in EDITABLE can be corrected this way (v1). Owner names are left out on purpose: the DRILL
reads a new owner name as a sale.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..db import dumps, loads, rows

_CHUNK = 900   # SQLite parameter limit is 999


@dataclass(frozen=True)
class Editable:
    """One field a Blue Moon export can change: its properties column, the export header, the label Seth
    sees, and how it is read (int | float | year | text | county | zip)."""
    field: str
    header: str
    label: str
    kind: str


# The left-hand columns of a Blue Moon export that an import reads back, in the order the review file shows
# them. Headers are bluemoon/exports.py property_columns() names.
EDITABLE: tuple[Editable, ...] = (
    Editable("units", "Units", "units", "int"),
    Editable("building_sqft", "Building sq ft", "building sq ft", "float"),
    Editable("year_built", "Year built", "year built", "year"),
    Editable("property_type", "Property type", "property type", "text"),
    Editable("county", "County", "county", "county"),
    Editable("zip", "ZIP", "ZIP", "zip"),
    Editable("lot_acres", "Lot acres", "lot acres", "float"),
)
EDITABLE_BY_FIELD: dict[str, Editable] = {e.field: e for e in EDITABLE}
EDITABLE_FIELDS: tuple[str, ...] = tuple(e.field for e in EDITABLE)
FLOAT_TOLERANCE = 1e-4      # exports round decimals to 4 places


def same_value(field: str, a: Any, b: Any) -> bool:
    """True when two values of a field mean the same (decimals within rounding, text ignoring case)."""
    if a is None or b is None:
        return a is None and b is None
    kind = EDITABLE_BY_FIELD[field].kind if field in EDITABLE_BY_FIELD else ""
    if kind == "float" or (isinstance(a, float) or isinstance(b, float)):
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return str(a) == str(b)
        return math.isclose(fa, fb, rel_tol=1e-9, abs_tol=FLOAT_TOLERANCE)
    if isinstance(a, str) and isinstance(b, str):
        return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()
    return a == b


def latest_edits(conn: sqlite3.Connection, market_id: int,
                 property_ids: Iterable[int] | None = None) -> dict[int, dict[str, dict]]:
    """{property id: {field: {value, old_value, actor, created_at, via}}}: the corrections in force for this
    market (the newest edit of each field), optionally only for some properties."""
    sql = ("SELECT e.* FROM property_edits e JOIN properties p ON p.id = e.property_id WHERE p.market_id = ? "
           "AND e.id = (SELECT MAX(e2.id) FROM property_edits e2 WHERE e2.property_id = e.property_id "
           "AND e2.field = e.field)")
    found: list[dict] = []
    if property_ids is None:
        found = rows(conn, sql + " ORDER BY e.id", (market_id,))
    else:
        ids = sorted({int(i) for i in property_ids})
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            found += rows(conn, sql + f" AND e.property_id IN ({','.join('?' for _ in chunk)}) ORDER BY e.id",
                          (market_id, *chunk))
    out: dict[int, dict[str, dict]] = {}
    for r in found:
        if r["field"] not in EDITABLE_BY_FIELD:
            continue
        out.setdefault(int(r["property_id"]), {})[r["field"]] = {
            "value": loads(r["new_value"]), "old_value": loads(r["old_value"]), "actor": r["actor"],
            "created_at": r["created_at"], "via": r["via"]}
    return out


def record_edit(conn: sqlite3.Connection, property_id: int, field: str, old_value: Any, new_value: Any, *,
                via: str | None, actor: str, created_at: str) -> None:
    """Add one correction (inside the caller's transaction)."""
    if field not in EDITABLE_BY_FIELD:
        raise ValueError(f"'{field}' can't be corrected by hand.")
    conn.execute("INSERT INTO property_edits(property_id, field, old_value, new_value, via, actor, created_at) "
                 "VALUES(?,?,?,?,?,?,?)", (int(property_id), field, dumps(old_value), dumps(new_value), via,
                                           (actor or "unknown").strip()[:320] or "unknown", created_at))


def edits_count_text(counts: Mapping[str, int]) -> str:
    """'units 12, year built 3' in EDITABLE order."""
    return ", ".join(f"{e.label} {counts[e.field]:,}" for e in EDITABLE if counts.get(e.field))


__all__ = ["EDITABLE", "EDITABLE_BY_FIELD", "EDITABLE_FIELDS", "Editable", "edits_count_text", "latest_edits",
           "record_edit", "same_value"]
