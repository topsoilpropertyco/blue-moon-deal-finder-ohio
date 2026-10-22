"""Bring an edited Blue Moon export back in: the safe end of "download, fix in Excel, import".

Our own files (ranked_list.xlsx/.csv, excluded_*.csv, a review file) also carry Crexi's column names, so
before this module a re-import went through the Crexi merge: it wrote our Property ID over the Crexi record
id and copied every computed column into the original data. Now columns.detect_provider() knows our files
(the 'Row as of' column, or our distinctive headers on older ones) and import_file() sends them here.

What an import of a Blue Moon export does:
- matches rows by 'Property ID', only inside the chosen market; it never adds a property;
- reads only the EDITABLE columns (Units, Building sq ft, Year built, Property type, County, ZIP, Lot acres)
  and 'Buy box decision'; every other column (scores, owner, links, the original Crexi columns) is ignored;
- changes only what differs from the app (an emptied cell clears the value, and the preview counts those
  apart), and records each change in property_edits (who, when, through which import), so a later Crexi file
  never quietly undoes it. A cell it can't read (text in a number column, a negative number, a formula with
  no result saved by Excel) is left alone and listed;
- leaves a row alone when the property changed in the app after the file was downloaded ('Row as of' is
  older than the change), so an old file never brings back old data. A workbook without 'Row as of' (an
  older export, or the column deleted) goes by the export time saved in the workbook instead; a CSV without
  it changes nothing;
- 'Buy box decision' set to 'Add to buy box', 'Set aside' or 'Follow the rules' becomes a decision by hand
  (scoring/overrides.py, note 'From spreadsheet', one batch the dashboard can undo);
- never touches raw_json, the source id, owner names or addresses; then re-scores the market.

Several sheets of one workbook (Ranked, All properties, Excluded) can each carry edits: a cell that differs
from the app counts, from whichever sheet. Two sheets that disagree about one value leave it alone.
The preview (preview_blue_moon_edits) counts the changes and never shows owner data.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..db import dumps, loads, market_by_slug, now_iso, rows
from ..util import clean_str, normalize_county, normalize_zip
from .columns import PROVIDER_LABELS, is_blue_moon_export, norm_header
from .crexi import (
    INGEST_FLAGS,
    RECORD_COLUMNS,
    ROW_NOTES,
    IngestError,
    Progress,
    PropertyRecord,
    Table,
    _file_hash,
    _is_formula,
    _num,
    _options,
    _plural,
    _previous_import_warning,
    _progress,
    _start_import,
    _text,
    _year,
    final_flags,
    read_sheets,
)
from .edits import EDITABLE, EDITABLE_BY_FIELD, edits_count_text, record_edit, same_value

log = logging.getLogger(__name__)

PROVIDER = "bluemoon"
ID_HEADER = "Property ID"
AS_OF_HEADER = "Row as of"
DECISION_HEADER = "Buy box decision"
ADDRESS_HEADER = "Address"
DECISION_NOTE = "From spreadsheet"
DEFAULT_ACTOR = "cli"
CONFIRM_CLEARS_OVER = 10    # config.yaml safety.confirm_clears_over: the preview asks before erasing more values
MAX_EXAMPLES = 5
_CHUNK = 900
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")   # exports.py puts a ' before these in CSV cells

Intent = Literal["include", "exclude", "rules"]
# What 'Buy box decision' may say (case, spaces and punctuation ignored). The export writes 'Added by hand',
# 'Set aside by hand' and 'Rules'; a cell still showing what the export wrote changes nothing.
DECISION_WORDS: dict[str, tuple[str, ...]] = {
    "include": ("add to buy box", "add to the buy box", "added by hand", "add", "added", "include", "in"),
    "exclude": ("set aside", "set aside by hand", "exclude", "out", "leave out"),
    "rules": ("follow the rules", "follows the rules", "rules", "back to the rules", "clear"),
}
DECISION_HELP = "'Add to buy box', 'Set aside' or 'Follow the rules'"
DECISION_LABELS: dict[str, str] = {"include": "added to the buy box", "exclude": "set aside",
                                   "rules": "back to the rules"}


# --- parsed rows --------------------------------------------------------------------------------------

class EditRow(BaseModel):
    """One row of a Blue Moon export, as read back: the editable values present and the decision cell."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sheet: str | None = None
    row_number: int
    property_id: int | None = None
    address: str | None = None
    as_of: datetime | None = None
    as_of_unreadable: bool = False
    values: dict[str, Any] = Field(default_factory=dict)       # field -> parsed value (None = cleared)
    problems: list[str] = Field(default_factory=list)          # cells that couldn't be read (left alone)
    decision_text: str | None = None


class EditPlan(BaseModel):
    """What importing the file would do. Nothing in it is owner data."""
    market: str
    filename: str
    sheets: list[str] = Field(default_factory=list)
    properties_total: int = 0                 # distinct Property IDs read
    rows_without_id: int = 0
    fields: dict[int, dict[str, Any]] = Field(default_factory=dict)       # pid -> {field: new value}
    old: dict[int, dict[str, Any]] = Field(default_factory=dict)          # pid -> {field: value now}
    decisions: dict[int, str] = Field(default_factory=dict)               # pid -> include | exclude | rules
    unchanged: int = 0
    stale: list[int] = Field(default_factory=list)
    unknown_ids: list[int] = Field(default_factory=list)
    other_market: dict[str, int] = Field(default_factory=dict)            # market name -> properties
    conflicts: int = 0
    problems: list[str] = Field(default_factory=list)
    unsupported_decisions: int = 0
    no_as_of_column: bool = False
    file_as_of: datetime | None = None        # no 'Row as of': when the workbook was exported, used instead
    unchecked: list[int] = Field(default_factory=list)   # changes left alone: nothing to check their age by
    sheets_without_as_of: list[str] = Field(default_factory=list)   # property sheets not read (column deleted)
    addresses: dict[int, str] = Field(default_factory=dict)

    def changed_ids(self) -> list[int]:
        return sorted(set(self.fields) | set(self.decisions))

    def field_counts(self) -> dict[str, int]:
        c: Counter[str] = Counter(f for changes in self.fields.values() for f in changes)
        return {e.field: c[e.field] for e in EDITABLE if c[e.field]}

    def clear_counts(self) -> dict[str, int]:
        """{field: n} of values the import erases (an empty cell where the app has a value)."""
        c: Counter[str] = Counter(f for pid, changes in self.fields.items() for f, v in changes.items()
                                  if v is None and self.old.get(pid, {}).get(f) is not None)
        return {e.field: c[e.field] for e in EDITABLE if c[e.field]}

    def decision_counts(self) -> dict[str, int]:
        c = Counter(self.decisions.values())
        return {k: c[k] for k in DECISION_WORDS if c[k]}


# --- reading cells ------------------------------------------------------------------------------------

def _unguard(v: Any) -> Any:
    """Undo the ' the CSV export puts before text that would otherwise run as a formula."""
    if isinstance(v, str) and len(v) > 1 and v[0] == "'" and v[1:].lstrip(" ").startswith(_FORMULA_START):
        return v[1:]
    return v


def _blank(v: Any) -> bool:
    return _text(v) is None


def parse_cell(field: str, v: Any, today_year: int | None = None) -> tuple[bool, Any]:
    """(ok, value) for one editable cell. An empty cell (or 0 in a number column) is (True, None): the value
    is cleared. A cell that can't be read (a negative number, a whole number that rounds to 0, a formula with
    no saved result) is (False, None) and changes nothing."""
    if _is_formula(v):                     # read_sheets put Excel's saved result here when there was one
        return False, None
    v = _unguard(v)
    kind = EDITABLE_BY_FIELD[field].kind
    if _blank(v):
        return True, None
    if kind in ("int", "float"):
        f = _num(v)
        if f is None or f < 0:             # a minus sign is a typo, never "erase this"
            return False, None
        if f == 0:                         # 0 means "not recorded", as in Crexi files
            return True, None
        if kind == "int":
            n = int(round(f))
            return (False, None) if n <= 0 else (True, n)
        return True, float(f)
    if kind == "year":
        year, invalid = _year(v, today_year or date.today().year)
        return (False, None) if invalid else (True, year)
    text = _text(v)
    if kind == "county":
        return True, normalize_county(text)
    if kind == "zip":
        z = normalize_zip(text)
        return (True, z) if z and re.fullmatch(r"\d{5}", z) else (False, None)
    return True, text


def parse_as_of(v: Any) -> datetime | None:
    """'Row as of' back as a time (UTC). Accepts what the export writes and what Excel may turn it into."""
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            dt = None
            for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%y %H:%M", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _db_time(s: Any) -> datetime | None:
    return parse_as_of(s) if s else None


def _words(text: str) -> str:
    return re.sub(r"[^a-z]+", " ", text.lower()).strip()


def decision_intent(text: str | None) -> Intent | None:
    """include | exclude | rules for a 'Buy box decision' cell, None for anything else."""
    if not text:
        return None
    w = _words(text)
    for intent, words in DECISION_WORDS.items():
        if w in words:
            return intent    # type: ignore[return-value]
    return None


def current_decision_text(found: Mapping[str, Mapping[str, Any]], rule_reasons: Iterable[str]) -> str:
    """What the export writes in 'Buy box decision' for these decisions (exports._decision_fields)."""
    from ..scoring.buybox import EXCLUSION_LABELS, WHOLE
    reasons = list(rule_reasons)

    def labels(keys: list[str]) -> str:
        return ", ".join(EXCLUSION_LABELS.get(k, k).lower() for k in keys)

    whole = (found.get(WHOLE) or {}).get("decision")
    waived = [r for r in reasons if (found.get(r) or {}).get("decision") == "include"]
    kept = [r for r in reasons if (found.get(r) or {}).get("decision") == "keep"]
    if whole == "exclude":
        return "Set aside by hand"
    if whole == "include":
        return "Added by hand"
    if waived:
        return "Rule waived: " + labels(waived)
    if kept:
        return "Kept set aside: " + labels(kept)
    return "Rules"


def _header(headers: list[str], wanted: str) -> str | None:
    """Our column, by its exact name first. Crexi's own columns can differ only in case ('Year Built' next to
    our 'Year built'), so a case-insensitive match is used only when it is the only one."""
    if wanted in headers:
        return wanted
    hits = [h for h in headers if norm_header(h) == norm_header(wanted)]
    return hits[0] if len(hits) == 1 else None


def _pid(v: Any) -> int | None:
    f = _num(v)
    if f is None or f <= 0 or not float(f).is_integer():
        return None
    return int(f)


def _read_rows(tables: list[Table], today_year: int) -> tuple[list[EditRow], list[str], bool, int, list[str]]:
    """(rows, sheet names used, whether 'Row as of' was missing, rows without a Property ID, property sheets
    not read because their 'Row as of' column was deleted while another sheet still has it)."""
    use = [t for t in tables if _header(t.headers, ID_HEADER)]
    with_as_of = [t for t in use if _header(t.headers, AS_OF_HEADER)]
    without: list[str] = []
    if with_as_of:
        # The property sheets; the DRILL worklist and Summary carry no edits. A property sheet without the
        # column can't be checked for age, so it isn't read (and the preview says so).
        without = [t.sheet or t.filename for t in use
                   if not _header(t.headers, AS_OF_HEADER) and is_blue_moon_export(t.headers)]
        use = with_as_of
    else:
        use = [t for t in use if is_blue_moon_export(t.headers)] or use[:1]
    out: list[EditRow] = []
    no_id = 0
    for t in use:
        hid, has, hdec, hadr = (_header(t.headers, h) for h in (ID_HEADER, AS_OF_HEADER, DECISION_HEADER,
                                                                   ADDRESS_HEADER))
        cols = {e.field: h for e in EDITABLE if (h := _header(t.headers, e.header))}
        for i, r in enumerate(t.rows):
            pid = _pid(r.get(hid)) if hid else None
            if pid is None:
                no_id += 1
                continue
            row = EditRow(sheet=t.sheet, row_number=t.header_row + 1 + i, property_id=pid,
                          address=_text(r.get(hadr)) if hadr else None)
            if has:
                row.as_of = parse_as_of(r.get(has))
                row.as_of_unreadable = row.as_of is None
            for f, h in cols.items():
                ok, value = parse_cell(f, r.get(h), today_year)
                if ok:
                    row.values[f] = value
                else:
                    row.problems.append(f"{EDITABLE_BY_FIELD[f].header} '{_text(r.get(h))}'")
            if hdec:
                row.decision_text = _text(_unguard(r.get(hdec)))
            out.append(row)
    return out, [t.sheet or t.filename for t in use], not with_as_of, no_id, without


# --- what the app has now ----------------------------------------------------------------------------

def _chunks(ids: list[int]) -> Iterable[list[int]]:
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def _properties(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for chunk in _chunks(ids):
        q = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT p.*, m.name AS market_name FROM properties p JOIN markets m ON m.id = p.market_id "
                            f"WHERE p.id IN ({q})", chunk):
            out[int(r["id"])] = r
    return out


def _rule_reasons(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for chunk in _chunks(ids):
        q = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT property_id, COALESCE(rule_reasons, exclusion_reasons) AS rr FROM scores "
                            f"WHERE property_id IN ({q})", chunk):
            out[int(r["property_id"])] = loads(r["rr"], []) or []
    return out


def _last_decided(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for chunk in _chunks(ids):
        q = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT property_id, MAX(created_at) AS at FROM buybox_override_log "
                            f"WHERE property_id IN ({q}) GROUP BY property_id", chunk):
            out[int(r["property_id"])] = r["at"]
    return out


def _decisions(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = {}
    for chunk in _chunks(ids):
        q = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT * FROM buybox_overrides WHERE property_id IN ({q})", chunk):
            out.setdefault(int(r["property_id"]), {})[r["reason"]] = r
    return out


def _wanted_change(intent: Intent, found: Mapping[str, Mapping[str, Any]]) -> bool:
    from ..scoring.buybox import WHOLE
    whole = (found.get(WHOLE) or {}).get("decision")
    if intent == "include":
        return whole != "include"
    if intent == "exclude":
        return whole != "exclude"
    return bool(found)


# --- the plan ----------------------------------------------------------------------------------------

def plan_edits(conn: sqlite3.Connection, market_slug: str, path: str | Path, limit: int | None = None) -> EditPlan:
    """Read the file and work out what importing it would change. Writes nothing."""
    path = Path(path).expanduser()
    try:
        market = market_by_slug(conn, market_slug)
    except KeyError:
        raise IngestError(f"There is no market called '{market_slug}'.") from None
    tables = read_sheets(path)
    read, sheets, no_as_of, no_id, without = _read_rows(tables, date.today().year)
    if limit is not None:
        first = list(dict.fromkeys(r.property_id for r in read))[:max(0, int(limit))]
        keep = set(first)
        read = [r for r in read if r.property_id in keep]
    file_as_of = None
    if no_as_of:      # no 'Row as of' column: our workbook's own export time stands in (Excel keeps it when saving)
        file_as_of = next((parse_as_of(t.created) for t in tables if t.made_by_blue_moon and t.created), None)
    plan = EditPlan(market=market_slug, filename=path.name, sheets=sheets, rows_without_id=no_id,
                    no_as_of_column=no_as_of, file_as_of=file_as_of, sheets_without_as_of=without)
    ids = sorted({int(r.property_id) for r in read if r.property_id is not None})
    plan.properties_total = len(ids)
    props = _properties(conn, ids)
    here = [pid for pid in ids if pid in props and int(props[pid]["market_id"]) == int(market["id"])]
    here_set = set(here)
    plan.unknown_ids = [pid for pid in ids if pid not in props]
    plan.other_market = dict(Counter(props[pid]["market_name"] for pid in ids if pid in props and pid not in here_set))
    reasons, decided_at, found = _rule_reasons(conn, here), _last_decided(conn, here), _decisions(conn, here)

    wanted: dict[tuple[int, str], list[Any]] = defaultdict(list)
    wanted_decision: dict[int, list[str]] = defaultdict(list)
    stale: set[int] = set()
    unchecked: set[int] = set()
    for r in read:
        pid = int(r.property_id or 0)
        if pid not in here_set:
            continue
        prop = props[pid]
        plan.addresses.setdefault(pid, r.address or prop.get("address") or f"Property {pid}")
        for problem in r.problems:
            plan.problems.append(f"{plan.addresses[pid]} ({r.sheet or 'file'} row {r.row_number}): {problem} "
                                 f"isn't a value I can read, so it was left as it was.")
        diffs = {f: v for f, v in r.values.items() if not same_value(f, prop.get(f), v)}
        intent = None
        text = r.decision_text
        if text and _words(text) != _words(current_decision_text(found.get(pid, {}), reasons.get(pid, []))):
            intent = decision_intent(text)
            if intent is None:
                plan.unsupported_decisions += 1
            elif not _wanted_change(intent, found.get(pid, {})):
                intent = None
        if not diffs and intent is None:
            continue
        if no_as_of and file_as_of is None:
            unchecked.add(pid)             # an old file can't be told from a fresh one: change nothing
            continue
        as_of = file_as_of if no_as_of else r.as_of
        changed_at = _db_time(prop.get("updated_at"))
        if diffs and (as_of is None or (changed_at and changed_at > as_of)):
            stale.add(pid)
            diffs = {}
        last = _db_time(decided_at.get(pid))
        if intent and (as_of is None or (last and last > as_of)):
            stale.add(pid)
            intent = None
        for f, v in diffs.items():
            wanted[(pid, f)].append(v)
        if intent:
            wanted_decision[pid].append(intent)

    for (pid, f), values in wanted.items():
        distinct: list[Any] = []
        for v in values:
            if not any(same_value(f, v, d) for d in distinct):
                distinct.append(v)
        if len(distinct) > 1:
            plan.conflicts += 1
            continue
        plan.fields.setdefault(pid, {})[f] = distinct[0]
        plan.old.setdefault(pid, {})[f] = props[pid].get(f)
    for pid, intents in wanted_decision.items():
        if len(set(intents)) > 1:
            plan.conflicts += 1
            continue
        plan.decisions[pid] = intents[0]
    changed = set(plan.changed_ids())
    plan.stale = sorted(stale)             # something in the file was left alone for these
    plan.unchecked = sorted(unchecked)
    plan.unchanged = len([pid for pid in here if pid not in changed and pid not in stale and pid not in unchecked])
    return plan


def plan_message(plan: EditPlan, market_name: str | None = None, done: bool = False) -> str:
    """'This is a Blue Moon export. 15 properties have changes: units 12, year built 3, buy box decisions 5.'"""
    n = len(plan.changed_ids())
    parts = [c for c in (edits_count_text(plan.field_counts()),
                         f"buy box decisions {len(plan.decisions):,}" if plan.decisions else "") if c]
    if done and n:
        text = f"Imported your edits: {_plural(n, 'property', 'properties')} changed ({', '.join(parts)})."
    elif done:
        text = "Imported the file, but nothing changed." + (
            "" if plan.stale or plan.unchecked else " Nothing in it differed from the app.")
    elif n:
        text = (f"This is a Blue Moon export. {_plural(n, 'property has', 'properties have')} changes: "
                f"{', '.join(parts)}.")
    elif plan.stale or plan.unchecked:
        text = "This is a Blue Moon export. Importing it would change nothing."
    else:
        text = ("This is a Blue Moon export. Nothing in it differs from what is in the app, so importing it "
                "changes nothing.")
    if plan.stale:
        one = len(plan.stale) == 1
        text += (f" {_plural(len(plan.stale), 'property', 'properties')} changed in the app after this file was "
                 f"downloaded, so the file's changes to {'it were' if one else 'them were'} left alone. Download a "
                 f"fresh copy to edit {'it' if one else 'them'}.")
    if plan.unchecked:
        one = len(plan.unchecked) == 1
        text += (f" {_plural(len(plan.unchecked), 'property has', 'properties have')} changes I left alone: this "
                 f"file has no 'Row as of' column, so I can't tell whether the app changed {'it' if one else 'them'} "
                 f"after the file was downloaded. Download a fresh copy, make your changes there and import that.")
    if plan.other_market and not (set(plan.fields) | set(plan.decisions)):
        where = ", ".join(plan.other_market)
        text += (f" {_plural(sum(plan.other_market.values()), 'property in it belongs', 'properties in it belong')}"
                 f" to another market ({where}): switch to it in the top bar and import the file there.")
    return text


def plan_warnings(plan: EditPlan, done: bool = False) -> list[str]:
    out: list[str] = []
    clears = plan.clear_counts()
    if clears:
        n = sum(clears.values())
        it = "it" if n == 1 else "them"
        if done:
            out.append(f"Erased {_plural(n, 'value', 'values')} where this file had an empty cell "
                       f"({edits_count_text(clears)}). A later Crexi import keeps {it} blank. To put {it} back, "
                       f"type {'the value' if n == 1 else 'the values'} into a fresh download and import it.")
        else:
            out.append(f"{_plural(n, 'cell is', 'cells are')} empty in this file where the app has a value "
                       f"({edits_count_text(clears)}). Importing erases {'that value' if n == 1 else 'those values'}"
                       f", and a later Crexi import keeps {it} blank. If you didn't mean to, put "
                       f"{'it' if n == 1 else 'the values'} back in the file and import it again.")
    if plan.no_as_of_column and plan.file_as_of:
        out.append("This file has no 'Row as of' column, so I went by the time it was downloaded (saved in the "
                   "workbook): rows that changed in the app after that were left alone. Download a fresh copy "
                   "next time.")
    for name in plan.sheets_without_as_of:
        out.append(f"The '{name}' sheet has no 'Row as of' column, so its changes were not read. Download a fresh "
                   f"copy, or make your changes on a sheet that still has that column.")
    if plan.unknown_ids:
        out.append(f"{_plural(len(plan.unknown_ids), 'Property ID is', 'Property IDs are')} not in the app, so "
                   f"{'that row was' if len(plan.unknown_ids) == 1 else 'those rows were'} skipped. Imports of "
                   f"Blue Moon files never add properties.")
    other = sum(plan.other_market.values())
    if other and (plan.fields or plan.decisions):
        out.append(f"{_plural(other, 'property belongs', 'properties belong')} to another market "
                   f"({', '.join(plan.other_market)}), so {'it was' if other == 1 else 'they were'} skipped.")
    if plan.conflicts:
        one_ = plan.conflicts == 1
        out.append(f"{_plural(plan.conflicts, 'value differs', 'values differ')} between two sheets or rows of this "
                   f"file, so I left {'it as it was' if one_ else 'them as they were'}. Make the sheets agree and "
                   f"import again.")
    if plan.unsupported_decisions:
        out.append(f"{_plural(plan.unsupported_decisions, 'Buy box decision cell says', 'Buy box decision cells say')} "
                   f"something I can't use. Type {DECISION_HELP}, or change it in the app.")
    if plan.problems:
        out += plan.problems[:MAX_EXAMPLES]
        if len(plan.problems) > MAX_EXAMPLES:
            out.append(f"... and {len(plan.problems) - MAX_EXAMPLES:,} more cells like that.")
    return out


def _change_list(plan: EditPlan, limit: int = 20) -> list[dict]:
    """A few changes to show before importing: address, what, from, to. Never owner data (not editable)."""
    out: list[dict] = []
    for pid in plan.changed_ids():
        for f, v in plan.fields.get(pid, {}).items():
            out.append({"property_id": pid, "address": plan.addresses.get(pid), "field": f,
                        "label": EDITABLE_BY_FIELD[f].header, "old": plan.old.get(pid, {}).get(f), "new": v})
        if pid in plan.decisions:
            out.append({"property_id": pid, "address": plan.addresses.get(pid), "field": "buybox",
                        "label": DECISION_HEADER, "old": None, "new": DECISION_LABELS[plan.decisions[pid]]})
        if len(out) >= limit:
            break
    return out[:limit]


def _confirm_clears_over() -> int:
    v = get_settings().section("safety").get("confirm_clears_over")
    return int(v) if v is not None else CONFIRM_CLEARS_OVER


def _summary(plan: EditPlan) -> dict:
    """fields_changed counts every change; clears ({field: n}) the ones that erase a value. confirm_clears: the
    Import screen asks "erase these values?" before saving (more of them than safety.confirm_clears_over)."""
    clears = plan.clear_counts()
    return {"properties_changed": len(plan.changed_ids()), "fields_changed": plan.field_counts(),
            "decisions_changed": plan.decision_counts(), "skipped_stale": len(plan.stale),
            "skipped_unchecked": len(plan.unchecked), "unknown_ids": len(plan.unknown_ids),
            "other_market": plan.other_market, "conflicts": plan.conflicts, "unchanged": plan.unchanged,
            "clears": clears, "confirm_clears": sum(clears.values()) > _confirm_clears_over(),
            "changes": _change_list(plan)}


# --- preview ------------------------------------------------------------------------------------------

def preview_blue_moon_edits(conn: sqlite3.Connection | None, market_slug: str | None, path: str | Path) -> dict:
    """The import screen's preview of a Blue Moon export: what would change. Writes nothing."""
    path = Path(path).expanduser()
    tables = read_sheets(path)
    base = {
        "filename": path.name, "provider": PROVIDER, "provider_label": PROVIDER_LABELS[PROVIDER],
        "sheet": ", ".join(t.sheet for t in tables if t.sheet) or None,
        "headers": tables[0].headers, "column_map": {}, "column_labels": {}, "unmapped_columns": [],
        "missing_fields": [], "key_fields": [], "size_metric": None, "type_mismatch": None,
        "rows": 0, "sample": [], "skipped_columns": [], "warnings": [],
    }
    if not market_slug:
        return {**base, "message": "This is a Blue Moon export. Pick a market to see what importing it would change."}
    own = conn is None
    if own:
        from ..db import connect
        conn = connect()
    try:
        plan = plan_edits(conn, market_slug, path)
        name = market_by_slug(conn, market_slug)["name"]
    finally:
        if own:
            conn.close()
    headers = {h for t in tables for h in t.headers}
    column_map = {e.field: e.header for e in EDITABLE if e.header in headers}
    if any(DECISION_HEADER in t.headers for t in tables):
        column_map["buybox_decision"] = DECISION_HEADER
    return {**base, "sheet": ", ".join(plan.sheets) or None, "rows": plan.properties_total,
            "column_map": column_map,
            "column_labels": {f: (EDITABLE_BY_FIELD[f].header if f in EDITABLE_BY_FIELD else DECISION_HEADER)
                              for f in column_map},
            "message": plan_message(plan, name), "warnings": plan_warnings(plan), "edits": _summary(plan)}


# --- import --------------------------------------------------------------------------------------------

def _new_flags(prop: Mapping[str, Any], changes: Mapping[str, Any], asset_class: str) -> list[str]:
    """flags_json after a correction (a year typed in drops 'no year built', and so on)."""
    opts = _options(get_settings(), asset_class, PROVIDER)
    old_flags = set(loads(prop.get("flags_json"), []) or [])
    rec = PropertyRecord.model_construct(**{**{c: prop.get(c) for c in RECORD_COLUMNS}, **changes})
    notes = old_flags & ROW_NOTES
    if "units" in changes:
        notes.discard("units_from_addresses")
    if "year_built" in changes:
        notes.discard("year_invalid")
    return sorted(set(final_flags(rec, notes, opts.units_metric, opts.year_in_buybox)) | (old_flags - INGEST_FLAGS))


def _detail_text(details: Mapping[str, str]) -> str:
    from ..scoring.buybox import EXCLUSION_LABELS, MANUAL_REASON
    parts = [f"{EXCLUSION_LABELS.get(k, k)}: {v}" for k, v in details.items() if k != MANUAL_REASON]
    return "; ".join(parts) or "Fits the buy box by the rules"


def _apply_decisions(conn: sqlite3.Connection, market: dict, plan: EditPlan, actor: str) -> str | None:
    """Write the spreadsheet's buy-box decisions as one batch (undo: POST /buybox/undo). Returns the batch id."""
    if not plan.decisions:
        return None
    from ..scoring import overrides
    from ..scoring.buybox import WHOLE
    batch = overrides.new_batch_id()
    by_intent: dict[str, list[int]] = defaultdict(list)
    for pid, intent in plan.decisions.items():
        by_intent[intent].append(pid)
    for intent in ("include", "exclude"):
        ids = by_intent.get(intent) or []
        if ids:
            details = overrides.rule_details(conn, market, ids)
            overrides.set_decision(conn, ids, WHOLE, intent, actor, note=DECISION_NOTE, bucket=None, batch_id=batch,
                                   details={pid: _detail_text(d) for pid, d in details.items()})
    if by_intent.get("rules"):
        overrides.clear(conn, by_intent["rules"], actor, batch_id=batch, note=DECISION_NOTE)
    return batch


def import_blue_moon_edits(conn: sqlite3.Connection, market_slug: str, path: str | Path, limit: int | None = None,
                           actor: str | None = None, progress: Progress = None) -> dict:
    """Apply the edits in a Blue Moon export (see the module notes). Returns the import result: {import_id,
    filename, market, provider: 'bluemoon', rows_total, rows_new: 0, rows_updated, rows_unchanged, rows_skipped,
    properties_changed, fields_changed: {field: n}, decisions_changed: {include|exclude|rules: n}, skipped_stale,
    unknown_ids, batch_id, message, warnings, ...}."""
    who = (actor or DEFAULT_ACTOR).strip() or DEFAULT_ACTOR
    path = Path(path).expanduser()
    _progress(progress, 0.05, "Reading your edits")
    try:
        market = market_by_slug(conn, market_slug)
    except KeyError:
        known = ", ".join(r["slug"] for r in rows(conn, "SELECT slug FROM markets ORDER BY id"))
        raise IngestError(f"There is no market called '{market_slug}'. Markets: {known}.") from None
    plan = plan_edits(conn, market_slug, path, limit)
    file_hash = _file_hash(path)
    warnings = plan_warnings(plan, done=True) + _previous_import_warning(conn, market["id"], file_hash)
    column_map = {e.field: e.header for e in EDITABLE}
    now = now_iso()
    _progress(progress, 0.3, f"Saving changes to {_plural(len(plan.changed_ids()), 'property', 'properties')}")
    with conn:
        import_id = _start_import(conn, market["id"], path.name, file_hash, column_map)
        via = f"import:{import_id}"
        props = _properties(conn, list(plan.fields))
        for pid, changes in plan.fields.items():
            prop = props[pid]
            flags = _new_flags(prop, changes, market["asset_class"])
            sets = ", ".join(f"{f} = ?" for f in changes)
            conn.execute(f"UPDATE properties SET {sets}, flags_json = ?, updated_at = ? WHERE id = ?",
                         (*changes.values(), dumps(flags), now, pid))
            for f, v in changes.items():
                record_edit(conn, pid, f, prop.get(f), v, via=via, actor=who, created_at=now)
    batch = _apply_decisions(conn, market, plan, who)
    if plan.changed_ids():
        _progress(progress, 0.7, "Re-scoring the market")
        from ..scoring.rank import compute_scores
        compute_scores(conn, market_slug)
    message = plan_message(plan, market["name"], done=True)
    stale_only = len((set(plan.stale) | set(plan.unchecked)) - set(plan.changed_ids()))
    skipped = stale_only + len(plan.unknown_ids) + sum(plan.other_market.values()) + plan.rows_without_id
    found_here = len(plan.changed_ids()) + plan.unchanged
    with conn:
        conn.execute("UPDATE imports SET rows_total=?, rows_new=0, rows_updated=?, rows_skipped=?, notes=? WHERE id=?",
                     (plan.properties_total, len(plan.changed_ids()), skipped, "\n".join([message, *warnings]),
                      import_id))
    _progress(progress, 1.0, "Edits imported")
    log.info("Imported edits from %s into %s: %d properties changed, %d left alone (changed since)", path.name,
             market_slug, len(plan.changed_ids()), len(plan.stale))
    summary = _summary(plan)
    return {
        "import_id": import_id, "filename": path.name, "market": market_slug, "provider": PROVIDER,
        "provider_label": PROVIDER_LABELS[PROVIDER], "sheet": ", ".join(plan.sheets) or None,
        "rows_total": plan.properties_total, "rows_new": 0, "rows_updated": found_here,
        "rows_unchanged": plan.unchanged, "rows_skipped": skipped, "duplicates_collapsed": 0,
        "rows_without_id": plan.rows_without_id, "column_map": column_map, "unmapped_columns": [],
        "skipped_columns": [], "mapped_columns": len(column_map), "total_columns": len(column_map),
        "flag_counts": {}, "edits_kept": 0, "batch_id": batch, "message": message, "warnings": warnings,
        **summary,
    }


__all__ = ["DECISION_WORDS", "EditPlan", "EditRow", "current_decision_text", "decision_intent",
           "import_blue_moon_edits", "parse_as_of", "parse_cell", "plan_edits", "plan_message", "plan_warnings",
           "preview_blue_moon_edits"]
