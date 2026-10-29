"""The merge rule for incoming Crexi data about a property we already hold.

KEEP THE FIRST ORIGINAL RECORD and look at every incoming field against it. Nothing is ever overwritten or
deleted:

1. a field that is blank in the original and filled in the new row is FILLED (counted as a blank filled);
2. contacts (phones, emails, contact names, mailing addresses, owner names) are a UNION: each one not already
   known (compared normalized: digits-only phones, lower-case emails, normalized names and addresses) is
   ADDED as a new untested contact or person with source 'crexi';
3. the same value changes nothing;
4. a factual field that differs from the original (units, year built, sq ft, owner name, mailing address...)
   keeps the original, and the new value is stored as an ALTERNATE (table property_alternates). The property
   gets the flag `crexi_differs`; the drawer shows each alternate with "Use this one" (recorded in
   property_edits, with the value it replaced) and "Ignore". A different owner name is also added as another person,
   so the DRILL can use it.

The same rule runs for the reply file of a batch, for duplicate rows inside any import file, and for a later
re-import of a property already in the app (ingest/crexi.py `_update`). First-time imports are unchanged.
`plan_facts` is pure (the import preview uses it to count what would happen); `apply_alternates` writes.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..db import dumps, loads, now_iso, rows
from ..util import clean_str, normalize_address, normalize_owner

FLAG = "crexi_differs"

# Fields that say WHICH property a row is (or where it is). A blank one is filled, but a different value is
# never an alternate: Crexi writing "ST" for "STREET" is not a disagreement about the property.
IDENTITY_FIELDS = frozenset({"source", "source_id", "crexi_url", "address", "unit", "city", "state", "zip", "apn",
                             "lat", "lng", "geocode_quality"})
RAW_PREFIX = "raw:"     # an alternate for a column kept only with the original columns (raw_json), e.g. raw:Occupancy
MAILING_FIELDS = ("mailing_address", "mailing_unit", "mailing_city", "mailing_state", "mailing_zip",
                  "mailing_care_of")

LABELS = {
    "name": "property name", "property_type": "property type", "county": "county", "units": "units",
    "building_sqft": "building sq ft", "lot_acres": "lot acres", "building_count": "buildings",
    "stories": "stories", "year_built": "year built", "sale_date": "last sale date", "sale_price": "last sale price",
    "lender": "lender", "loan_amount": "loan amount", "loan_type": "loan type", "interest_rate": "interest rate",
    "loan_maturity": "loan maturity", "improvement_value": "building value", "land_value": "land value",
    "total_value": "total value", "value_type": "value type", "tax_year": "tax year", "annual_tax": "annual tax",
    "usps_vacant": "USPS vacant", "opportunity_zone": "opportunity zone", "preforeclosure": "pre-foreclosure",
    "reo": "bank-owned", "owner_name": "owner name", "mailing_address": "owner mailing address",
}


def label(field_name: str) -> str:
    if field_name.startswith(RAW_PREFIX):
        return field_name[len(RAW_PREFIX):]
    return LABELS.get(field_name) or field_name.replace("_", " ")


# --- the report ---------------------------------------------------------------------------------------------

REPORT_KEYS = ("rows_read", "matched", "duplicates", "new_phones", "new_emails", "new_names", "new_mailing",
               "blanks_filled", "alternates", "gained_nothing", "properties")


@dataclass
class Report:
    """Plain-English counts of what an import found that we did not already have."""
    counts: Counter = field(default_factory=Counter)

    def add(self, key: str, n: int = 1) -> None:
        if n:
            self.counts[key] += n

    def merge(self, other: Mapping[str, int]) -> None:
        for k, v in other.items():
            self.counts[k] += int(v)

    def to_dict(self) -> dict[str, int]:
        return {k: int(self.counts.get(k, 0)) for k in REPORT_KEYS}

    def text(self) -> str:
        return report_text(self.to_dict())


def report_text(c: Mapping[str, int]) -> str:
    """'New phones 12, new emails 3, ...' for the preview and the history."""
    g = lambda k: int(c.get(k) or 0)  # noqa: E731
    head = [f"{g('rows_read'):,} rows read"]
    if "matched" in c:
        head.append(f"{g('matched'):,} matched")
    head.append(f"{g('duplicates'):,} duplicate{'s' if g('duplicates') != 1 else ''}")
    new = (f"New data found: {g('new_phones'):,} new phone{'s' if g('new_phones') != 1 else ''}, "
           f"{g('new_emails'):,} new email{'s' if g('new_emails') != 1 else ''}, "
           f"{g('new_names'):,} new owner or contact name{'s' if g('new_names') != 1 else ''}, "
           f"{g('new_mailing'):,} new mailing address{'es' if g('new_mailing') != 1 else ''}, "
           f"{g('blanks_filled'):,} blank{'s' if g('blanks_filled') != 1 else ''} filled, "
           f"{g('alternates'):,} differing value{'s' if g('alternates') != 1 else ''} kept as "
           f"alternate{'s' if g('alternates') != 1 else ''}; "
           f"{g('gained_nothing'):,} propert{'ies' if g('gained_nothing') != 1 else 'y'} gained nothing.")
    return ", ".join(head) + ". " + new


# --- comparing values --------------------------------------------------------------------------------------

def _same(field_name: str, a: Any, b: Any) -> bool:
    from ..ingest.edits import same_value       # imported here: bluemoon.ingest imports this module
    if field_name.startswith(RAW_PREFIX):
        return _raw_same(a, b)
    if field_name == "owner_name":
        return (normalize_owner(a) or "") == (normalize_owner(b) or "")
    if field_name == "county":
        return (clean_str(a) or "").upper() == (clean_str(b) or "").upper()
    try:
        return same_value(field_name, a, b)
    except Exception:
        return str(a) == str(b)


def mailing_text(rec: Mapping[str, Any]) -> str | None:
    """The mailing address as one line ('123 MAIN ST APT 2, DAYTON, OH 45402'), or None when it has no street."""
    street = clean_str(rec.get("mailing_address"))
    if not street:
        return None
    parts = [street]
    unit = clean_str(rec.get("mailing_unit"))
    if unit and unit.upper() not in street.upper():
        parts[0] = f"{street} {unit}"
    city, state, zip_ = (clean_str(rec.get(k)) for k in ("mailing_city", "mailing_state", "mailing_zip"))
    tail = " ".join(x for x in (state, zip_) if x)
    parts += [x for x in (city, tail) if x]
    return ", ".join(parts)


def mailing_key(rec: Mapping[str, Any]) -> str | None:
    street = clean_str(rec.get("mailing_address"))
    if not street:
        return None
    unit = clean_str(rec.get("mailing_unit"))
    line = f"{street} {unit}" if unit and unit.upper() not in street.upper() else street
    return normalize_address(line, rec.get("mailing_city"), rec.get("mailing_state"), rec.get("mailing_zip"))


@dataclass
class FactsPlan:
    """What the merge rule does with one incoming record: `merged` (the values to store: the original, with
    blanks filled), `fills` {field: value}, `alternates` {field: (new value, original value)} and the
    owner name / mailing address that are new knowledge (`new_owner`, `new_mailing`: text or None)."""
    merged: dict[str, Any] = field(default_factory=dict)
    fills: dict[str, Any] = field(default_factory=dict)
    alternates: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    new_owner: str | None = None
    new_mailing: str | None = None


def plan_facts(existing: Mapping[str, Any], incoming: Mapping[str, Any], columns: Iterable[str], *,
               skip: Iterable[str] = (), replace_blank_like: Iterable[str] = ()) -> FactsPlan:
    """Apply the merge rule to one property. `existing` is the stored row, `incoming` the new values (None =
    blank in the new row), `columns` the fields to look at. `skip`: fields a correction made by hand already
    settled (never an alternate). `replace_blank_like`: stored values that are only an estimate (the unit count
    taken from the address count): a real value replaces them like a blank."""
    plan = FactsPlan()
    cols = list(columns)
    skip_set, soft = set(skip), set(replace_blank_like)
    mailing_differs = False
    for f in cols:
        old, new = existing.get(f), incoming.get(f)
        if f in MAILING_FIELDS:
            continue
        if new is None:
            plan.merged[f] = old
        elif old is None or f in soft and not _same(f, old, new):
            plan.merged[f] = new
            plan.fills[f] = new
        else:
            plan.merged[f] = old
            if not _same(f, old, new) and f not in IDENTITY_FIELDS and f not in skip_set:
                plan.alternates[f] = (new, old)
                if f == "owner_name":
                    plan.new_owner = clean_str(new)
    # Mailing address: one group. A blank original is filled (all its parts); a different address is an alternate
    # and also a new mailing address to try; the same address just gets its blank parts filled.
    old_addr, new_addr = clean_str(existing.get("mailing_address")), clean_str(incoming.get("mailing_address"))
    for f in MAILING_FIELDS:
        old, new = existing.get(f), incoming.get(f)
        if f not in cols:
            continue
        if new is None or (new_addr and old_addr and mailing_key(existing) != mailing_key(incoming)):
            plan.merged[f] = old
        elif old is None:
            plan.merged[f] = new
            plan.fills[f] = new
        else:
            plan.merged[f] = old
    if new_addr and old_addr and mailing_key(existing) != mailing_key(incoming):
        mailing_differs = True
    if mailing_differs and "mailing_address" not in skip_set:
        plan.alternates["mailing_address"] = (mailing_text(incoming), mailing_text(existing))
        plan.new_mailing = mailing_text(incoming)
    return plan


def _raw_same(a: Any, b: Any) -> bool:
    try:
        return float(str(a).replace(",", "")) == float(str(b).replace(",", ""))
    except (TypeError, ValueError):
        return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def raw_alternates(stored: Mapping[str, Any], incoming: Mapping[str, Any], keys: Iterable[str]) -> dict:
    """Columns kept only with the original columns (Days on Market, Occupancy, lease fields...): the stored value
    stays, and a different non-blank value in the new file is returned as an alternate
    {'raw:<column>': (new, original)} so it is not lost."""
    out = {}
    for k in keys:
        old, new = clean_str(stored.get(k)), clean_str(incoming.get(k))
        if old is not None and new is not None and not _raw_same(old, new):
            out[RAW_PREFIX + k] = (incoming.get(k), stored.get(k))
    return out


def filled_count(plan: FactsPlan) -> int:
    """How many blanks the plan fills. A mailing address counts once, not once for each of its parts."""
    fills = {f for f in plan.fills if f not in MAILING_FIELDS}
    if any(f in plan.fills for f in MAILING_FIELDS):
        fills.add("mailing_address")
    return len(fills)


# --- writing alternates -----------------------------------------------------------------------------------

def apply_alternates(conn: sqlite3.Connection, property_id: int, plan: FactsPlan, *, import_id: int | None,
                     source: str = "crexi", now: str | None = None) -> int:
    """Store the plan's alternates (inside the caller's transaction) and raise the flag. A value already stored
    for this field (open, used or dismissed) is not stored twice, so re-importing a file adds nothing. Returns
    how many new alternates were stored."""
    stamp = now or now_iso()
    added = 0
    for f, (new, old) in plan.alternates.items():
        have = rows(conn, "SELECT value, original, status FROM property_alternates WHERE property_id=? AND field=?",
                    (property_id, f))
        if any(_same(f, loads(r["value"]), new) for r in have):
            continue
        if any(r["status"] == "used" and _same(f, loads(r["original"]), new) for r in have):
            continue        # the value 'Use this one' replaced: that choice was already made
        conn.execute("INSERT INTO property_alternates(property_id, field, value, original, source, import_id, seen_at) "
                     "VALUES(?,?,?,?,?,?,?)", (property_id, f, dumps(new), dumps(old), source, import_id, stamp))
        added += 1
    if plan.alternates:
        sync_flag(conn, property_id)
    return added


def sync_flag(conn: sqlite3.Connection, property_id: int) -> int:
    """Raise `crexi_differs` in the property's flags while it has open alternates and drop it when none are
    left. Returns the open count."""
    n = int(conn.execute("SELECT COUNT(*) FROM property_alternates WHERE property_id=? AND status='open' "
                         "AND field NOT LIKE ?", (property_id, RAW_PREFIX + "%")).fetchone()[0])
    row = conn.execute("SELECT flags_json FROM properties WHERE id=?", (property_id,)).fetchone()
    if row is None:
        return n
    flags = list(loads(row[0], []) or [])
    if n and FLAG not in flags:
        flags.append(FLAG)
    elif not n and FLAG in flags:
        flags.remove(FLAG)
    else:
        return n
    conn.execute("UPDATE properties SET flags_json=? WHERE id=?", (dumps(sorted(flags)), property_id))
    return n


def add_new_knowledge(conn: sqlite3.Connection, property_id: int, plan: FactsPlan, *, notes: str,
                      source: str = "crexi") -> dict[str, int]:
    """A different owner name becomes another person (role 'other'), and a different mailing address another
    untested mailing address, so the DRILL can use them. Inside the caller's transaction. Returns
    {'new_names': n, 'new_mailing': n} for what was really new."""
    from ..drill import engine as E
    out = {"new_names": 0, "new_mailing": 0}
    if plan.new_owner:
        _, created = E._add_person(conn, property_id, plan.new_owner, role="other", source=source, notes=notes)
        out["new_names"] += int(created)
    if plan.new_mailing:
        _, created = E._add_contact(conn, property_id, "mail", plan.new_mailing, source=source, notes=notes)
        out["new_mailing"] += int(created)
    return out


# --- deciding an alternate -------------------------------------------------------------------------------

USABLE_FIELDS_NOTE = ("A different mailing address can't be swapped in: it is kept as an extra address to try "
                      "(see the contact list).")


def open_alternates(conn: sqlite3.Connection, property_id: int) -> list[dict]:
    """The property's alternates (newest first), each with a plain label and whether 'Use this one' works."""
    out = []
    for r in rows(conn, "SELECT * FROM property_alternates WHERE property_id=? ORDER BY id DESC", (property_id,)):
        out.append({"id": r["id"], "field": r["field"], "label": label(r["field"]), "value": loads(r["value"]),
                    "original": loads(r["original"]), "source": r["source"], "seen_at": r["seen_at"],
                    "status": r["status"], "decided_by": r["decided_by"], "decided_at": r["decided_at"],
                    "usable": r["field"] != "mailing_address" and not r["field"].startswith(RAW_PREFIX)})
    return out


def decide_alternate(conn: sqlite3.Connection, property_id: int, alternate_id: int, action: str, actor: str) -> dict:
    """'use' (swap it in, recorded in property_edits with the value it replaced) or 'ignore'. ValueError says why
    not."""
    from ..ingest.edits import EDITABLE_BY_FIELD, record_edit
    alt = conn.execute("SELECT * FROM property_alternates WHERE id=? AND property_id=?",
                       (alternate_id, property_id)).fetchone()
    if alt is None:
        raise KeyError(f"Alternate {alternate_id} was not found on this property.")
    if alt["status"] != "open":
        raise ValueError("That one was already decided.")
    stamp = now_iso()
    who = (actor or "unknown")[:320]
    if action == "ignore":
        conn.execute("UPDATE property_alternates SET status='dismissed', decided_by=?, decided_at=? WHERE id=?",
                     (who, stamp, alternate_id))
        message = "Ignored. The original value stays."
    elif action == "use":
        from ..ingest.crexi import RECORD_COLUMNS
        f = alt["field"]
        if f.startswith(RAW_PREFIX):
            raise ValueError("This column is only kept with the original columns, so it can't be swapped in. "
                             "Both values stay listed here; choose Ignore when you have seen it.")
        if f not in RECORD_COLUMNS or f == "mailing_address":
            raise ValueError(USABLE_FIELDS_NOTE)
        value = loads(alt["value"])
        old = conn.execute(f"SELECT {f} FROM properties WHERE id=?", (property_id,)).fetchone()[0]
        if f in EDITABLE_BY_FIELD:
            record_edit(conn, property_id, f, old, value, via=f"alternate:{alternate_id}", actor=who, created_at=stamp)
        else:     # not a column a spreadsheet can correct: the same history row, written here
            conn.execute("INSERT INTO property_edits(property_id, field, old_value, new_value, via, actor, created_at) "
                         "VALUES(?,?,?,?,?,?,?)", (property_id, f, dumps(old), dumps(value),
                                                   f"alternate:{alternate_id}", who, stamp))
        conn.execute(f"UPDATE properties SET {f}=?, updated_at=? WHERE id=?", (value, stamp, property_id))
        conn.execute("UPDATE property_alternates SET status='used', decided_by=?, decided_at=? WHERE id=?",
                     (who, stamp, alternate_id))
        message = f"Now using this {label(f)}. The edit is recorded; the original value is kept in the history."
    else:
        raise ValueError("Choose use or ignore.")
    left = sync_flag(conn, property_id)
    conn.commit()
    return {"ok": True, "message": message, "open_left": left}
