"""Files for Brad, the CRM and the mail house, written to output/<market>/.

    export_market(conn, "apartments-springfield-oh")      # -> {file name: Path}
    export_zip(conn, "apartments-springfield-oh", "excluded")

- ranked_list.xlsx   Sheets: Ranked (buy box, by rank), All properties, Excluded, DRILL worklist, Summary.
                     Bold frozen header, filters, colored green/yellow/red cells, clickable links.
                     Ranked and All properties also carry every original column.
- ranked_list.csv    Every property, ranked ones first (opens in Google Sheets).
- excluded_<reason>.csv  One file per filter with a plain-English reason, plus excluded_manual.csv (set aside
                     by hand). Rows are never deleted. A property added to the buy box by hand is on the
                     ranked list instead, with 'Buy box decision' and 'Rules said' telling why.
- crm_handoff.csv    Confirmed owners for the CRM. Exporting it stamps them as handed off.
- mailing_list.csv   Name and mailing address where letters are next, or a buy-box owner's address is untested.
- drill_worklist.csv What to do next for each owner still being worked.
- run_summary.md     Counts, colors, layers, API use and cost this month, credits and how far they reach.
- review/review_<group>.xlsx  One set-aside group to check and fix in Excel (export_review), then import back.

Every property list ends with 'Property ID' and 'Row as of', and workbooks name this app in their metadata:
an edited copy imported back is known as ours and only its editable columns are read (ingest/roundtrip.py).

Our columns come first, then every original column from the import, under its original name.
This module never reads API keys.
"""
from __future__ import annotations

import csv
import logging
import math
import os
import re
import sqlite3
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import pipeline
from .config import AssetClass, Settings, get_settings
from .db import loads, market_by_slug, rows, this_month
from .drill import credits as drill_credits
from .drill import engine, links, stats
from .enrich.owners import OWNER_TYPE_LABELS
from .ingest.edits import EDITABLE
from .scoring import (
    EXCLUSION_LABELS,
    MANUAL_REASON,
    SET_ASIDE_REASONS,
    SIZE_BASIS_LABELS,
    apply_overrides,
    evaluate,
    load_scoring_rows,
)
from .scoring import buckets, overrides
from .scoring.blue_moon import fmt_mult

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

MAIN_FILES = ("ranked_list.xlsx", "ranked_list.csv", "crm_handoff.csv", "mailing_list.csv", "drill_worklist.csv",
              "run_summary.md")
EXCLUDED_FILES = tuple(f"excluded_{r}.csv" for r in SET_ASIDE_REASONS)
EXPORT_FILES = MAIN_FILES[:2] + EXCLUDED_FILES + MAIN_FILES[2:]
CSV_ENCODING = "utf-8-sig"          # with a BOM so Excel reads accents correctly; Google Sheets ignores it
FILLS = {"green": "C6EFCE", "yellow": "FFEB9C", "red": "FFC7CE"}
HEADER_FILL = "E7E6E6"
MAX_WIDTH, MIN_WIDTH, WIDTH_SAMPLE = 50, 8, 300
_ILLEGAL_XLSX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"-?\d+(\.\d+)?")   # e.g. a raw Crexi Longitude "-83.93" from a CSV import
_WEB_LINK = re.compile(r"^https?://", re.IGNORECASE)   # same rule as the dashboard's safeUrl
ABSENTEE_LABELS = {"local": "Local", "instate": "In state, other city", "outofstate": "Out of state"}
DEBT_LABELS = {"free_and_clear": "Likely free and clear", "maturing_soon": "Loan maturing soon",
               "active": "Active loan"}
DRIVE_LABELS = {"google_routes": "Google", "estimate": "Estimate (approximate)"}
RENT_FIT_LABELS = {"in": "In range", "near": "Near", "out": "Out of range", "na": "No HUD rent"}
CHANNELS = ("mail", "phone", "text", "email", "social", "vm")
CHANNEL_HEADERS = {"mail": "Mail", "phone": "Phone", "text": "Text", "email": "Email", "social": "Social",
                   "vm": "Voicemail"}
CRM_COLUMNS = ("property_id", "rank", "score", "confirmed_person", "confirmed_at", "handed_off_at", "phone", "text",
               "email", "social", "vm", "other_contacts_untested", "mailing_address", "owner_name", "owner_type",
               "people", "address", "city", "state", "zip", "county", "apn", "property_type", "units",
               "building_sqft", "year_built", "crexi_url", "last_outreach", "notes")


# --- columns -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Col:
    """One output column. kind: text | int | num | color | link (link_text is what Excel shows)."""
    header: str
    get: Callable[[dict], Any]
    kind: str = "text"
    link_text: str = ""


def _col(key: str, header: str, kind: str = "text", link_text: str = "") -> Col:
    return Col(header, lambda r, k=key: r.get(k), kind, link_text)


def property_columns(ac: AssetClass) -> list[Col]:
    """Our columns for property lists, in order. Photo and rent columns only where the asset class uses them."""
    cols = [
        _col("rank", "Rank", "int"), _col("score_pct", "Blue Moon score %", "num"),
        _col("why_score", "Why this score"),
        _col("in_buybox", "In buy box"), _col("why_set_aside", "Why set aside"),
        _col("decision", "Buy box decision"), _col("rules_said", "Rules said"), _col("decided_by", "Decided by"),
        _col("decided_on", "Decided on"), _col("groups", "Set-aside groups"),
        _col("address", "Address"), _col("unit", "Unit"), _col("city", "City"), _col("state", "State"),
        _col("zip", "ZIP"), _col("county", "County"), _col("property_type", "Property type"),
        _col("units", "Units", "int"), _col("building_sqft", "Building sq ft", "int"),
        _col("year_built", "Year built", "int"), _col("lot_acres", "Lot acres", "num"),
        _col("size_value", "Size used", "num"), _col("size_basis", "Size based on"),
        _col("size_color", "Size color", "color"),
        _col("drive_minutes", "Drive minutes", "num"), _col("drive_method", "Drive time from"),
    ]
    if ac.visual_enabled:
        cols += [_col("photo_label", "Photo label"), _col("photo_p", "Photo check %", "num"),
                 _col("photo_color", "Photo color", "color"), _col("photo_review", "Photo needs review"),
                 _col("photo_reasons", "Photo reasons")]
    if ac.rent_enabled:
        cols += [_col("rent_fit", "Rent fit"), _col("rent_color", "Rent color", "color"),
                 _col("rent_detail", "Rent detail")]
    cols += [
        _col("owner_name", "Owner"), _col("owner_type", "Owner type"), _col("years_owned", "Years owned", "num"),
        _col("absentee", "Absentee owner"), _col("debt", "Debt"),
        _col("portfolio_count", "Owner's properties", "int"), _col("signals_known", "Score signals known"),
        _col("level", "Contact level"), _col("drill_status", "DRILL status"), _col("next_step", "Next DRILL step"),
        _col("best_guess_person", "Best-guess person"), _col("confirmed_person", "Confirmed person"),
        _col("contact_info", "Contact info"), _col("credit_used", "Crexi credit used"), _col("flags", "Flags"),
        _col("crexi_url", "Crexi", "link", "Crexi"), _col("maps_url", "Google Maps", "link", "Map"),
        _col("street_view_url", "Street View", "link", "Street View"),
        _col("auditor_url", "County auditor", "link", "Auditor"),
        _col("property_id", "Property ID", "int"),
        # When this row's data was last changed (a correction, an import or a buy-box decision). Bringing the
        # file back in (ingest/roundtrip.py) leaves a row alone when the app changed it after this time.
        _col("row_as_of", ROW_AS_OF),
    ]
    return cols


def raw_columns(raw_headers: list[str], taken: Iterable[str]) -> list[Col]:
    """The original import columns, under their own names ('(Crexi)' added if one clashes with ours)."""
    used = set(taken)
    out = []
    for h in raw_headers:
        name = h if h not in used else f"{h} (Crexi)"
        used.add(name)
        out.append(Col(name, lambda r, k=h: r["_raw"].get(k)))
    return out


WORKLIST_COLUMNS: list[Col] = [
    _col("rank", "Rank", "int"), _col("score_pct", "Blue Moon score %", "num"), _col("in_buybox", "In buy box"),
    _col("address", "Address"), _col("city", "City"), _col("state", "State"), _col("zip", "ZIP"),
    _col("county", "County"), _col("owner_name", "Owner"), _col("owner_type", "Owner type"),
    _col("level", "Contact level"), _col("drill_status", "DRILL status"), _col("next_step", "Next step"),
    _col("next_step_key", "Next step key"), _col("best_guess_person", "Best-guess person"),
    _col("confirmed_person", "Confirmed person"),
    *[_col(f"channel_{c}", CHANNEL_HEADERS[c]) for c in CHANNELS],
    _col("credit_used", "Crexi credit used"), _col("crexi_url", "Crexi", "link", "Crexi"),
    _col("property_id", "Property ID", "int"),
]

MAILING_COLUMNS: list[Col] = [
    _col("name", "Name"), _col("owner_name", "Owner on record"), _col("care_of", "Care of"),
    _col("line1", "Mailing address"), _col("unit", "Mailing unit"), _col("city", "Mailing city"),
    _col("state", "Mailing state"), _col("zip", "Mailing ZIP"), _col("full", "Full mailing address"),
    _col("why", "Why on this list"), _col("level", "Contact level"), _col("in_buybox", "In buy box"),
    _col("rank", "Rank", "int"),
    _col("property_address", "Property address"), _col("property_city", "Property city"),
    _col("property_state", "Property state"), _col("property_zip", "Property ZIP"), _col("county", "County"),
    _col("property_id", "Property ID", "int"), _col("contact_id", "Contact ID", "int"), _col("source", "Source"),
]

SUMMARY_COLUMNS: list[Col] = [_col("section", "Section"), _col("item", "Item"), _col("value", "Value")]

# --- the round trip: download, fix in Excel, import back (ingest/roundtrip.py) ---------------------------

ROW_AS_OF = "Row as of"
WORKBOOK_CREATOR = "Blue Moon Deal Finder"       # ingest/crexi.py BLUE_MOON_CREATOR: our own file, coming back
REVIEW_FOLDER = "review"
REVIEW_FIRST = ("Address", "City")               # which property each row is, then the columns you can fix
DECISION_CHOICES = ("Add to buy box", "Set aside", "Follow the rules")   # ingest/roundtrip.py DECISION_WORDS
HOW_TO_EDIT: tuple[tuple[str, str], ...] = (
    ("What this is", "The properties in one set-aside group, so you can check them and fix what is wrong."),
    ("What you can change", "Units, Building sq ft, Year built, Property type, County, ZIP, Lot acres, and "
                            "Buy box decision. Every other column is for reading; changes to it are ignored."),
    ("Buy box decision", "Type Add to buy box, Set aside or Follow the rules. Leave it as it is to change nothing."),
    ("An empty cell", "Clears that value (for example a year built you know is wrong)."),
    ("Don't change", "Property ID and Row as of: they tell the app which property each row is and how old it is."),
    ("When you're done", "Save the file (as .xlsx or .csv) and drop it on the Import screen. The preview shows "
                         "what will change before anything is saved."),
    ("Safe", "Only properties already in the app are changed; nothing is added or deleted. A row that changed "
             "in the app after you downloaded this file is left alone. Every change is logged with your name."),
)


# --- the data ------------------------------------------------------------------------------------

@dataclass
class MarketData:
    """Everything the export files are made from, loaded once."""
    settings: Settings
    market: dict
    asset: AssetClass
    properties: list[dict]                      # export rows, ranked first
    raw_headers: list[str]
    summaries: dict[int, dict]
    handed_off: set[int] = field(default_factory=set)

    @property
    def slug(self) -> str:
        return self.market["slug"]

    def columns(self) -> list[Col]:
        ours = property_columns(self.asset)
        return ours + raw_columns(self.raw_headers, (c.header for c in ours))


def load_market(conn: sqlite3.Connection, market_slug: str) -> MarketData:
    settings = get_settings()
    market = market_by_slug(conn, market_slug)
    ac = settings.asset_class(market["asset_class"])
    scores = {r["property_id"]: r for r in rows(conn, "SELECT s.* FROM scores s JOIN properties p ON p.id = "
                                                       "s.property_id WHERE p.market_id = ?", (market["id"],))}
    summaries = engine.summaries(conn, market_slug)
    decisions = overrides.load(conn, [market["id"]])
    spec = buckets.group_spec(market, ac, settings)
    decided = _last_decided(conn, market["id"])
    out, headers = [], {}
    for prop, enrich in load_scoring_rows(conn, [market["id"]]):
        row = _property_row(prop, enrich, scores.get(prop["id"]), summaries.get(prop["id"]), ac, market, settings,
                            decisions.get(prop["id"]) or {}, spec)
        row["row_as_of"] = max(str(prop.get("updated_at") or ""), str(decided.get(prop["id"]) or "")) or None
        headers.update(dict.fromkeys(row["_raw"]))
        out.append(row)
    out.sort(key=_list_order)
    handed = {r["property_id"] for r in rows(conn, "SELECT d.property_id FROM drill_state d JOIN properties p ON "
                                                   "p.id = d.property_id WHERE p.market_id = ? AND d.handed_off_at "
                                                   "IS NOT NULL", (market["id"],))}
    return MarketData(settings, market, ac, out, list(headers), summaries, handed)


def _last_decided(conn: sqlite3.Connection, market_id: int) -> dict[int, str]:
    """{property id: when its buy-box decision last changed} (for 'Row as of')."""
    return {int(r["property_id"]): r["at"] for r in rows(
        conn, "SELECT l.property_id, MAX(l.created_at) AS at FROM buybox_override_log l JOIN properties p ON "
              "p.id = l.property_id WHERE p.market_id = ? GROUP BY l.property_id", (market_id,))}


def _list_order(r: dict) -> tuple:
    """Ranked first (by rank), then the rest by score, then never-scored ones."""
    if r["rank"] is not None:
        return (0, r["rank"], 0.0, r["property_id"])
    return (1 if r["_score"] is not None else 2, 0, -(r["_score"] or 0.0), r["property_id"])


def _property_row(prop: dict, enrich: dict, score: dict | None, summary: dict | None, ac: AssetClass,
                  market: dict, settings: Settings, found: dict | None = None,
                  spec: buckets.GroupSpec | None = None) -> dict:
    found = found or {}
    reasons = loads(score.get("exclusion_reasons"), []) if score else []
    rule_reasons = loads(score.get("rule_reasons"), None) if score else []
    rule_reasons = reasons if rule_reasons is None else (rule_reasons or [])
    details: dict = {}
    if reasons or rule_reasons or found:
        details = apply_overrides(evaluate(prop, enrich, ac, market, settings), found).get("exclusion_details", {})
    row = {
        "property_id": prop["id"], "_score": (score or {}).get("score"), "_reasons": reasons, "_details": details,
        "_rule_reasons": rule_reasons, "_raw": loads(prop.get("raw_json"), {}) or {}, "_prop": prop,
        "rank": (score or {}).get("rank"), "score_pct": _pct((score or {}).get("score"), 2),
        "in_buybox": "Not scored yet" if score is None else _yes_no(score.get("in_buybox")),
        "why_set_aside": "; ".join(_reason_text(r, details) for r in reasons),
        **_decision_fields(score, rule_reasons, found, prop, spec),
        **{k: prop.get(k) for k in ("address", "unit", "city", "state", "zip", "county", "property_type", "units",
                                    "building_sqft", "year_built", "lot_acres", "owner_name")},
        "flags": ", ".join(loads(prop.get("flags_json"), []) or []),
    }
    row.update(_score_fields(score))
    row.update(_layer_fields(enrich))
    row.update(_drill_fields(summary))
    row.update(_link_fields(prop))
    return row


def _decision_fields(score: dict | None, rule_reasons: list[str], found: dict, prop: dict,
                     spec: buckets.GroupSpec | None) -> dict:
    """'Buy box decision', 'Rules said', 'Decided by', 'Decided on' and 'Set-aside groups'."""
    if score is None:
        return {}

    def labels(keys: list[str]) -> str:
        return ", ".join(EXCLUSION_LABELS.get(k, k).lower() for k in keys)

    whole = (found.get("*") or {}).get("decision")
    waived = [r for r in rule_reasons if (found.get(r) or {}).get("decision") == "include"]
    kept = [r for r in rule_reasons if (found.get(r) or {}).get("decision") == "keep"]
    if whole == "exclude":
        decision = "Set aside by hand"
    elif whole == "include":
        decision = "Added by hand"
    elif waived:
        decision = "Rule waived: " + labels(waived)
    elif kept:
        decision = "Kept set aside: " + labels(kept)
    else:
        decision = "Rules"
    latest = max(found.values(), key=lambda r: str(r.get("decided_at") or ""), default=None)
    groups = loads(score.get("aside_buckets"), []) or []
    group_text = "; ".join(f"{EXCLUSION_LABELS.get(g.split(':', 1)[0], g)}: "
                           f"{buckets.label_for(g, spec, prop.get('property_type')) if spec else g}" for g in groups)
    return {"decision": decision,
            "rules_said": "; ".join(EXCLUSION_LABELS.get(r, r) for r in rule_reasons) or "Fits the buy box",
            "decided_by": (latest or {}).get("decided_by"),
            "decided_on": str((latest or {}).get("decided_at") or "")[:10] or None,
            "groups": group_text or None}


def _score_fields(score: dict | None) -> dict:
    s = score or {}
    have, total = s.get("coverage_have"), s.get("coverage_total")
    return {"why_score": _why_score(loads(s.get("signals_json"), []) or []),
            "size_value": _round(s.get("size_value"), 1),
            "size_basis": SIZE_BASIS_LABELS.get(s.get("size_basis") or "", s.get("size_basis")),
            "size_color": s.get("size_color"), "photo_color": s.get("visual_color"), "rent_color": s.get("rent_color"),
            "signals_known": f"{have} of {total}" if total else None}


def _why_score(signals: list[dict], top: int = 4) -> str | None:
    """The strongest known score signals, biggest effect first: 'Debt Likely free and clear x1.58; ...'.
    Signals with no data or no effect (x1) are left out."""
    known = [g for g in signals if isinstance(g, dict) and g.get("multiplier") is not None
             and abs(float(g["multiplier"]) - 1) > 1e-4]
    known.sort(key=lambda g: -abs(math.log(max(float(g["multiplier"]), 1e-6))))
    parts = [" ".join(str(x) for x in (g.get("label"), g.get("value")) if x not in (None, ""))
             + f" x{fmt_mult(float(g['multiplier']))}" for g in known[:top]]
    return "; ".join(parts) or None


def _photo_reasons(visual: dict) -> str | None:
    """What the photo check said: its reasons, the cues it saw, the image quality and your review note."""
    cues = loads(visual.get("cues_json"), []) or []
    parts = (visual.get("reasons"),
             ("Cues: " + ", ".join(str(c) for c in cues)) if isinstance(cues, list) and cues else None,
             f"Image quality: {visual['image_quality']}" if visual.get("image_quality") else None,
             f"Your note: {visual['reviewed_note']}" if visual.get("reviewed_note") else None)
    return " | ".join(str(p) for p in parts if p) or None


def _layer_fields(enrich: dict) -> dict:
    drive_row, rent_row = enrich.get("drive") or {}, enrich.get("rent") or {}
    visual, owner = enrich.get("visual") or {}, enrich.get("owner") or {}
    checked = bool(visual.get("ai_label") or visual.get("reviewed_label"))
    return {
        "photo_reasons": _photo_reasons(visual),
        "drive_minutes": _round(drive_row.get("minutes"), 1),
        "drive_method": DRIVE_LABELS.get(drive_row.get("method") or "", drive_row.get("method")),
        "photo_label": visual.get("reviewed_label") or visual.get("ai_label"),
        "photo_p": _round(visual.get("final_p"), 0),
        "photo_review": ("Yes" if visual.get("final_p") is None else "No") if checked else None,
        "rent_fit": RENT_FIT_LABELS.get(rent_row.get("fit") or "", rent_row.get("fit")),
        "rent_detail": rent_row.get("detail"),
        "owner_type": OWNER_TYPE_LABELS.get(owner.get("owner_type") or "", owner.get("owner_type")),
        "years_owned": _round(owner.get("years_owned"), 1),
        "absentee": ABSENTEE_LABELS.get(owner.get("absentee") or ""),
        "debt": DEBT_LABELS.get(owner.get("debt_status") or ""),
        "portfolio_count": owner.get("portfolio_count"),
    }


def _drill_fields(summary: dict | None) -> dict:
    if not summary:
        return {}
    channels = summary.get("channels") or {}
    step = summary.get("next_step") or {}
    return {
        "level": f"{summary['level']} {summary['level_name']}", "drill_status": summary.get("status"),
        "next_step": summary.get("next_step_text"), "next_step_key": step.get("stage_key") or step.get("kind"),
        "best_guess_person": summary.get("best_guess_person"), "confirmed_person": summary.get("confirmed_person"),
        "contact_info": ", ".join(f"{CHANNEL_HEADERS[c].lower()} ({s})" for c, s in channels.items()
                                  if s != "none" and c in CHANNEL_HEADERS),
        "credit_used": _yes_no(summary.get("credit_used")),
        **{f"channel_{c}": channels.get(c, "none") for c in CHANNELS},
    }


def _link_fields(prop: dict) -> dict:
    found = {link["source_key"]: link["url"] for link in links.links_for(prop, parse_owner=False)}
    return {"crexi_url": prop.get("crexi_url") or found.get("crexi"),
            "maps_url": found.get("google_maps_listing") or found.get("google_maps"),
            "street_view_url": found.get("street_view"), "auditor_url": found.get("county_auditor")}


def _reason_text(reason: str, details: dict) -> str:
    label = EXCLUSION_LABELS.get(reason, reason)
    detail = details.get(reason)
    return f"{label} ({detail})" if detail else label


# --- writers -------------------------------------------------------------------------------------

def _csv_value(value: Any, kind: str) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return int(value) if value.is_integer() and kind == "int" else round(value, 4)
    if (isinstance(value, str) and value.lstrip(" ").startswith(_FORMULA_START)
            and not _PLAIN_NUMBER.fullmatch(value)):
        return "'" + value  # never let a spreadsheet run a cell as a formula
    return value


def write_csv(path: Path, columns: list[Col], items: Iterable[dict]) -> Path:
    def write(tmp: Path) -> None:
        with tmp.open("w", newline="", encoding=CSV_ENCODING) as f:
            out = csv.writer(f)
            out.writerow([c.header for c in columns])
            for item in items:
                out.writerow([_csv_value(c.get(item), c.kind) for c in columns])
    return _atomic(path, write)


def write_dict_csv(path: Path, header: list[str], items: list[dict]) -> Path:
    return write_csv(path, [_col(h, h) for h in header], items)


@dataclass
class Sheet:
    name: str
    columns: list[Col]
    items: list[dict]
    freeze: str = "A2"          # the cell below and right of what stays put when scrolling
    choices: dict[str, tuple[str, ...]] = field(default_factory=dict)   # header -> a dropdown's suggestions


def write_xlsx(path: Path, sheets: list[Sheet]) -> Path:
    """A workbook with a bold frozen header, filters, sensible widths, colored color-columns and links.
    Its metadata names this app, so importing it back is known as a Blue Moon export (ingest/roundtrip.py)."""
    def write(tmp: Path) -> None:
        wb = Workbook(write_only=True)
        wb.properties.creator = WORKBOOK_CREATOR
        wb.properties.title = path.stem
        for sheet in sheets:
            _write_sheet(wb.create_sheet(sheet.name[:31]), sheet)
        wb.save(tmp)
    return _atomic(path, write)


def _write_sheet(ws: Any, sheet: Sheet) -> None:
    cols = sheet.columns
    for i, width in enumerate(_widths(cols, sheet.items), start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = sheet.freeze
    bold, fill = Font(bold=True), PatternFill("solid", fgColor=HEADER_FILL)
    header = []
    for c in cols:
        cell = WriteOnlyCell(ws, value=_xlsx_text(c.header))
        cell.font, cell.fill = bold, fill
        header.append(cell)
    ws.append(header)
    fills = {k: PatternFill("solid", fgColor=v) for k, v in FILLS.items()}
    for item in sheet.items:
        ws.append([_xlsx_cell(ws, c, c.get(item), fills) for c in cols])
    if cols:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(sheet.items) + 1}"
    headers = [c.header for c in cols]
    for header, options in sheet.choices.items():
        if header in headers and sheet.items:
            # Suggestions only: any other text is still allowed (the cell may show what the export wrote).
            dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=True,
                                showErrorMessage=False)
            letter = get_column_letter(headers.index(header) + 1)
            dv.add(f"{letter}2:{letter}{len(sheet.items) + 1}")
            ws.data_validations.append(dv)


def _xlsx_cell(ws: Any, col: Col, value: Any, fills: dict[str, PatternFill]) -> Any:
    if value is None or value == "":
        return None
    if col.kind == "link" and _WEB_LINK.match(str(value).strip()):
        cell = WriteOnlyCell(ws, value=col.link_text or "Open")
        cell.hyperlink = str(value).strip()
        cell.style = "Hyperlink"
        return cell   # anything else (javascript:, a \\server\share path) stays plain text below
    if isinstance(value, str):
        text = _xlsx_text(value)
        if col.kind == "color" and text in fills:
            cell = WriteOnlyCell(ws, value=text)
            cell.fill = fills[text]
            return cell
        if text.startswith("="):
            cell = WriteOnlyCell(ws, value=text)
            cell.data_type = "s"  # text, not a formula
            return cell
        return text
    return value


def _xlsx_text(value: str) -> str:
    return _ILLEGAL_XLSX.sub("", value)[:32767]


def _widths(cols: list[Col], items: list[dict]) -> list[float]:
    sample = items[:WIDTH_SAMPLE]
    out = []
    for c in cols:
        if c.kind == "link":
            longest = max(len(c.header), len(c.link_text or "Open"))
        else:
            longest = max([len(c.header)] + [len(str(v)) for v in (c.get(r) for r in sample) if v is not None])
        out.append(float(max(MIN_WIDTH, min(MAX_WIDTH, longest + 2))))
    return out


def _atomic(path: Path, write: Callable[[Path], None]) -> Path:
    """Write to a temporary file, then swap it in, so a download never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


# --- the files -----------------------------------------------------------------------------------

def _ranked(data: MarketData) -> list[dict]:
    return [r for r in data.properties if r["rank"] is not None]


def _excluded(data: MarketData, reason: str | None = None) -> list[dict]:
    return [r for r in data.properties if r["_reasons"] and (reason is None or reason in r["_reasons"])]


def write_excluded(data: MarketData, folder: Path, reason: str) -> Path:
    """excluded_<reason>.csv: every property set aside for this reason (a header-only file when none are)."""
    why = Col("Reason", lambda r, k=reason: _reason_text(k, r["_details"]))
    return write_csv(folder / f"excluded_{reason}.csv", [why] + data.columns(), _excluded(data, reason))


def worklist_rows(data: MarketData) -> list[dict]:
    """Owners still being worked (active, or confirmed but not handed off yet), best first."""
    out = []
    for r in data.properties:
        if r.get("drill_status") == "active" or (r.get("drill_status") == "confirmed"
                                                 and r["property_id"] not in data.handed_off):
            out.append(r)
    return out


def write_crm_handoff(conn: sqlite3.Connection, data: MarketData, folder: Path, stamp: bool = True) -> Path:
    """Confirmed owners for the CRM, then (with stamp) mark the new ones as handed off, only after the file
    is saved. A plain download (stamp=False) never changes anything."""
    found = stats.crm_handoff_rows(conn, data.slug)
    for r in found:
        r["new_in_this_export"] = "No" if r.get("handed_off_at") else "Yes"
    header = list(found[0]) if found else [*CRM_COLUMNS, "new_in_this_export"]
    path = write_dict_csv(folder / "crm_handoff.csv", header, found)
    if stamp:
        engine.mark_handed_off(conn, [r["property_id"] for r in found if not r.get("handed_off_at")])
    return path


def mailing_rows(conn: sqlite3.Connection, data: MarketData) -> list[dict]:
    """Where to send letters, buy box first: owners whose next DRILL step is a letter, and buy-box owners
    whose mailing address is untested. Never a do-not-contact owner or address, never an address marked
    wrong, never a confirmed owner (they're in the CRM)."""
    mail = _mail_contacts(conn, data.market["id"])
    dnc = engine._dnc_keys(conn)
    out = []
    for r in data.properties:
        summary = data.summaries.get(r["property_id"]) or {}
        if summary.get("status") in ("dnc", "confirmed"):
            continue
        contacts = mail.get(r["property_id"], [])
        usable = [c for c in contacts if c["status"] in ("untested", "verified")
                  and ("mail", engine._contact_key("mail", c["value"])) not in dnc]
        untested = [c for c in usable if c["status"] == "untested"]
        if _letters_next(summary):
            why = "Letters are the next DRILL step"
            lines = _mail_lines(r, usable, summary, why)
            if not lines and not _record_ruled_out(r["_prop"], contacts, dnc):
                lines = _mail_lines(r, [None], summary, why)
            out += lines
        elif untested and r.get("in_buybox") != "No":   # set-aside properties don't get a letter
            out += _mail_lines(r, untested, summary, "In the buy box, mailing address not tested yet")
    return out


def _record_ruled_out(prop: dict, contacts: list[dict], dnc: set[tuple[str, str]]) -> bool:
    """True when the mailing address on record is do-not-contact, or Seth marked it wrong."""
    on_record = engine.mailing_text(prop)
    if not on_record:
        return False
    key = engine._contact_key("mail", on_record)
    return ("mail", key) in dnc or any(c["status"] in ("wrong", "dnc") and engine._contact_key("mail", c["value"]) == key
                                       for c in contacts)


def _mail_contacts(conn: sqlite3.Connection, market_id: int) -> dict[int, list[dict]]:
    """Mailing contacts of every status (wrong and do-not-contact ones rule out the address on record)."""
    out: dict[int, list[dict]] = {}
    for c in rows(conn, "SELECT cp.id, cp.property_id, cp.value, cp.status, cp.source, pe.name AS person_name "
                        "FROM contact_points cp JOIN properties p ON p.id = cp.property_id "
                        "LEFT JOIN people pe ON pe.id = cp.person_id WHERE p.market_id = ? AND cp.channel = 'mail' "
                        "ORDER BY cp.id", (market_id,)):
        out.setdefault(c["property_id"], []).append(c)
    return out


def _letters_next(summary: dict) -> bool:
    step = summary.get("next_step") or {}
    return step.get("kind") == "stage" and step.get("stage_key") == "letters"


def _mail_lines(r: dict, contacts: list[dict | None], summary: dict, why: str) -> list[dict]:
    """One mailing-list row per address. None stands for the owner's mailing address on record."""
    prop = r["_prop"]
    on_record = engine.mailing_text(prop)
    out = []
    for c in contacts:
        full = c["value"] if c else on_record
        if not full:
            continue
        same_as_record = on_record is not None and _squash(full) == _squash(on_record)
        name = (c or {}).get("person_name") or summary.get("best_guess_person") or prop.get("owner_name") or "Owner"
        out.append({
            "name": name, "owner_name": prop.get("owner_name"),
            "care_of": prop.get("mailing_care_of") if same_as_record else None,
            "line1": prop.get("mailing_address") if same_as_record else full,
            "unit": prop.get("mailing_unit") if same_as_record else None,
            "city": prop.get("mailing_city") if same_as_record else None,
            "state": prop.get("mailing_state") if same_as_record else None,
            "zip": prop.get("mailing_zip") if same_as_record else None,
            "full": full, "why": why, "level": r.get("level"), "in_buybox": r.get("in_buybox"), "rank": r.get("rank"),
            "property_address": prop.get("address"), "property_city": prop.get("city"),
            "property_state": prop.get("state"), "property_zip": prop.get("zip"), "county": prop.get("county"),
            "property_id": r["property_id"], "contact_id": (c or {}).get("id"),
            "source": (c or {}).get("source") or "owner mailing address on record",
        })
    return out


def _squash(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


# --- the summary -----------------------------------------------------------------------------------

def summary_data(conn: sqlite3.Connection, data: MarketData) -> dict:
    """The numbers behind run_summary.md and the Summary sheet."""
    market_id = data.market["id"]
    in_box = _ranked(data)
    board = engine.overview(conn, data.slug)
    return {
        "market": data.slug, "name": data.market["name"], "asset_label": data.asset.label,
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "properties": len(data.properties), "in_buybox": len(in_box),
        "excluded": len(_excluded(data)), "not_scored": sum(1 for r in data.properties if r["_score"] is None),
        "exclusions": Counter(reason for r in data.properties for reason in r["_reasons"]),
        "by_hand": {"added": sum(1 for r in in_box if r["_rule_reasons"]),
                    "set_aside": sum(1 for r in data.properties if MANUAL_REASON in r["_reasons"])},
        "colors": _color_counts(in_box, data.asset),
        "layers": pipeline.layer_status(conn, data.slug),
        "steps": _step_counts(conn, market_id, data.asset),
        "drill": board, "last_import": pipeline.last_import(conn, market_id),
        "credits": credit_reach(conn, data.slug, len(in_box)),
        "api": api_usage(conn, data.settings),
    }


def _color_counts(items: list[dict], ac: AssetClass) -> dict[str, Counter]:
    axes = {"Size": "size_color"}
    if ac.visual_enabled:
        axes["Photo check"] = "photo_color"
    if ac.rent_enabled:
        axes["Rent fit"] = "rent_color"
    return {label: Counter(r.get(key) or "none" for r in items) for label, key in axes.items()}


def _step_counts(conn: sqlite3.Connection, market_id: int, ac: AssetClass) -> list[tuple[str, dict]]:
    """(step label, {value: count}) for each layer, from the database."""
    def grouped(sql: str) -> dict:
        return {str(r["k"]): int(r["n"]) for r in rows(conn, sql, (market_id,))}
    join = "FROM properties p JOIN {t} x ON x.property_id = p.id WHERE p.market_id = ? GROUP BY 1"
    out = [("Owners (by type)", {OWNER_TYPE_LABELS.get(k, k): n for k, n in grouped(
               "SELECT x.owner_type AS k, COUNT(*) AS n " + join.format(t="owners")).items()}),
           ("Drive times", {DRIVE_LABELS.get(k, k): n for k, n in grouped(
               "SELECT x.method AS k, COUNT(*) AS n " + join.format(t="drive_times")).items()})]
    if ac.rent_enabled:
        out.append(("Rent fit", grouped("SELECT x.fit AS k, COUNT(*) AS n " + join.format(t="rent_fits"))))
    if ac.visual_enabled:
        out.append(("Photos", grouped("SELECT x.status AS k, COUNT(*) AS n " + join.format(t="visual_checks"))))
        out.append(("Photo check", grouped(
            "SELECT CASE WHEN x.reviewed_label IS NOT NULL THEN 'reviewed by you' WHEN x.ai_label IS NULL THEN "
            "'not checked' WHEN x.final_p IS NULL THEN 'needs your review' ELSE 'answered by Claude' END AS k, "
            "COUNT(*) AS n " + join.format(t="visual_checks"))))
    return out


def credit_reach(conn: sqlite3.Connection, market_slug: str, ranked_total: int) -> dict:
    """This month's credits (shared by every market) and how far down this market's ranked list they reach."""
    status = drill_credits.credit_status(conn)
    info = drill_credits.market_plan(conn, market_slug, limit=status["left"]) if status["left"] else \
        {"plan": [], "planned": 0, "planned_all": 0, "extra_count": 0, "extras_all": 0, "shortfall": 0}
    plan = info["plan"]
    ranks = [p["rank"] for p in plan if p.get("rank")]
    reach = max(ranks) if ranks else None
    every = info["planned_all"] + info["extras_all"]
    if not status["left"]:
        text = status["message"]
    elif not every:
        text = (f"{status['left']:,} credits left, but no owner qualifies for one yet in any market. Import "
                f"more properties, or work the DRILL until owners have a name on file.")
    else:
        here = info["planned"] + info["extra_count"]
        text = (f"{here:,} of the {every:,} owners planned for this month's credits are in this market"
                + (f", reaching down to rank #{reach:,} of {ranked_total:,} in the buy box." if reach else "."))
        if info["extra_count"]:
            text += f" {info['extra_count']:,} of them are fallbacks outside the main plan."
        if info["shortfall"]:
            text += f" {info['shortfall']:,} credits have no target yet."
    return {**status, "planned": info["planned"], "planned_all": info["planned_all"],
            "extras": info["extra_count"], "shortfall": info["shortfall"], "reach_rank": reach,
            "ranked_total": ranked_total, "reach_text": text}


def api_usage(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Metered calls this month (all markets) and what they cost after the free monthly calls."""
    prices = settings.section("google").get("prices") or {}
    per_call_api = float(((settings.section("vision").get("anthropic_api") or {}).get("est_cost_per_call_usd")) or 0.03)
    items = []
    for r in rows(conn, "SELECT provider, sku, SUM(units) AS units FROM api_calls "
                        "WHERE strftime('%Y-%m', created_at, 'localtime') = ? "
                        "GROUP BY provider, sku ORDER BY provider, sku", (this_month(),)):
        units = int(r["units"] or 0)
        price = prices.get(r["sku"])
        if price:
            free = int(price.get("free_per_month", 0) or 0)
            billable = max(0, units - free)
            usd = round(billable * float(price.get("per_1000", 0) or 0) / 1000.0, 2)
            note = f"{min(units, free):,} of {free:,} free calls used"
        elif r["sku"] == "vision_anthropic_api":
            billable, usd, note = units, round(units * per_call_api, 2), "pay-per-use Anthropic API (estimate)"
        else:
            billable, usd, note = 0, 0.0, "Claude plan (no per-call bill)" if "claude" in r["sku"] else "free"
        items.append({"provider": r["provider"], "sku": r["sku"], "label": pipeline.SKU_LABELS.get(r["sku"], r["sku"]),
                      "units": units, "billable": billable, "usd": usd, "note": note})
    return {"month": this_month(), "items": items, "total_usd": round(sum(i["usd"] for i in items), 2)}


def summary_markdown(s: dict) -> str:
    lines = [f"# Run summary: {s['name']}", "",
             f"Exported {s['generated_at']} (market `{s['market']}`).", ""]
    lines += _md_list_section(s)
    lines += ["## Colors in the buy box", "", "| Check | Green | Yellow | Red | Not checked yet | N/A |",
              "|---|---:|---:|---:|---:|---:|"]
    for label, counts in s["colors"].items():
        waiting = counts.get("pending", 0) + counts.get("none", 0)
        lines.append(f"| {label} | {counts.get('green', 0):,} | {counts.get('yellow', 0):,} | "
                     f"{counts.get('red', 0):,} | {waiting:,} | {counts.get('na', 0):,} |")
    lines += ["", "## Steps and layers", "", "| Layer | Done | Of | Note |", "|---|---:|---:|---|"]
    lines += [f"| {x['label']} | {x['done']:,} | {x['total']:,} | {x['note'] or ''} |" for x in s["layers"]]
    lines.append("")
    for label, counts in s["steps"]:
        if counts:
            lines.append(f"- {label}: " + ", ".join(f"{k} {n:,}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])))
    lines += [""] + _md_drill_section(s) + _md_credit_section(s) + _md_api_section(s)
    lines += ["## Files", "", *[f"- {name}" for name in EXPORT_FILES], ""]
    return "\n".join(lines)


def _md_list_section(s: dict) -> list[str]:
    imp = s["last_import"]
    out = ["## The list", "", f"- Properties: {s['properties']:,}"]
    if imp:
        out.append(f"- Last import: {imp['filename']} on {str(imp['created_at'])[:10]} ({imp['rows_total'] or 0:,} rows)")
    out.append(f"- In the buy box (ranked): {s['in_buybox']:,}")
    out.append(f"- Set aside by a filter or by hand (kept, see excluded_*.csv): {s['excluded']:,}")
    for reason in SET_ASIDE_REASONS:
        if s["exclusions"].get(reason):
            out.append(f"  - {EXCLUSION_LABELS[reason]}: {s['exclusions'][reason]:,}")
    hand = s.get("by_hand") or {}
    if hand.get("added") or hand.get("set_aside"):
        out.append(f"- Decided by hand: {hand.get('added', 0):,} added to the buy box, "
                   f"{hand.get('set_aside', 0):,} set aside")
    if s["not_scored"]:
        out.append(f"- Not scored yet: {s['not_scored']:,} (run the score step)")
    return out + [""]


def _md_drill_section(s: dict) -> list[str]:
    board = s["drill"]
    names = board.get("level_names") or engine.LEVEL_NAMES_DEFAULT
    out = ["## DRILL", ""]
    out += [f"- Level {lvl} {names.get(lvl, '')}: {board['levels'].get(lvl, 0):,}" for lvl in (1, 2, 3)]
    out.append(f"- Penetration: {board['penetration']:.0%} ({board['penetration_detail']['text']})")
    statuses = ", ".join(f"{k} {n:,}" for k, n in board["statuses"].items() if n)
    if statuses:
        out.append(f"- Status: {statuses}")
    return out + [""]


def _md_credit_section(s: dict) -> list[str]:
    c = s["credits"]
    return ["## Crexi credits", "", f"- {c['message']}", f"- {c['reach_text']}", ""]


def _md_api_section(s: dict) -> list[str]:
    api = s["api"]
    out = [f"## API calls this month ({api['month']}, all markets)", ""]
    if not api["items"]:
        return out + ["No metered calls yet this month.", ""]
    out += ["| Service | Calls | Billable | Est. cost | Note |", "|---|---:|---:|---:|---|"]
    out += [f"| {i['label']} | {i['units']:,} | {i['billable']:,} | ${i['usd']:,.2f} | {i['note']} |"
            for i in api["items"]]
    return out + ["", f"Estimated cost this month: ${api['total_usd']:,.2f}", ""]


def summary_rows(s: dict) -> list[dict]:
    """The same summary as rows for the Summary sheet."""
    out: list[tuple[str, str, Any]] = [
        ("Market", "Name", s["name"]), ("Market", "Exported", s["generated_at"]),
        ("List", "Properties", s["properties"]), ("List", "In the buy box (ranked)", s["in_buybox"]),
        ("List", "Set aside by a filter or by hand", s["excluded"]), ("List", "Not scored yet", s["not_scored"]),
    ]
    out += [("Set aside", EXCLUSION_LABELS[r], s["exclusions"].get(r, 0)) for r in SET_ASIDE_REASONS]
    hand = s.get("by_hand") or {}
    out += [("By hand", "Added to the buy box", hand.get("added", 0)),
            ("By hand", "Set aside", hand.get("set_aside", 0))]
    for label, counts in s["colors"].items():
        out += [(f"Colors in buy box: {label}", k, n) for k, n in sorted(counts.items())]
    out += [("Layers", x["label"], f"{x['done']:,} of {x['total']:,}") for x in s["layers"]]
    board = s["drill"]
    names = board.get("level_names") or engine.LEVEL_NAMES_DEFAULT
    out += [("DRILL", f"Level {lvl} {names.get(lvl, '')}", board["levels"].get(lvl, 0)) for lvl in (1, 2, 3)]
    out.append(("DRILL", "Penetration", f"{board['penetration']:.0%}"))
    out += [("Crexi credits", "This month", s["credits"]["message"]), ("Crexi credits", "Reach", s["credits"]["reach_text"])]
    out += [("API this month", i["label"], f"{i['units']:,} calls, ${i['usd']:,.2f}") for i in s["api"]["items"]]
    out.append(("API this month", "Estimated cost", f"${s['api']['total_usd']:,.2f}"))
    return [{"section": a, "item": b, "value": c} for a, b, c in out]


# --- public API ----------------------------------------------------------------------------------

def export_market(conn: sqlite3.Connection, market_slug: str, out_dir: str | Path | None = None, *,
                  only: Iterable[str] | None = None, progress: Progress = None,
                  stamp_handoffs: bool = True) -> dict[str, Path]:
    """Write the export files and return {file name: path}.

    out_dir: default output/<market slug>/. only: just these files (names from EXPORT_FILES, or
    'excluded' for every excluded_<reason>.csv). crm_handoff.csv stamps newly confirmed owners as
    handed off, unless stamp_handoffs is False (a plain download).
    """
    wanted = _wanted(only)
    folder = Path(out_dir) if out_dir else get_settings().path("output") / market_slug
    _tell(progress, 0.05, "Gathering the list")
    data = load_market(conn, market_slug)
    files: dict[str, Path] = {}
    columns = data.columns()
    if "ranked_list.csv" in wanted:
        files["ranked_list.csv"] = write_csv(folder / "ranked_list.csv", columns, data.properties)
    for reason in SET_ASIDE_REASONS:
        if f"excluded_{reason}.csv" in wanted:
            files[f"excluded_{reason}.csv"] = write_excluded(data, folder, reason)
    if "drill_worklist.csv" in wanted:
        files["drill_worklist.csv"] = write_csv(folder / "drill_worklist.csv", WORKLIST_COLUMNS, worklist_rows(data))
    if "mailing_list.csv" in wanted:
        files["mailing_list.csv"] = write_csv(folder / "mailing_list.csv", MAILING_COLUMNS, mailing_rows(conn, data))
    _tell(progress, 0.3, "Writing the summary")
    summary = summary_data(conn, data) if {"run_summary.md", "ranked_list.xlsx"} & wanted else None
    if summary and "run_summary.md" in wanted:
        files["run_summary.md"] = _atomic(folder / "run_summary.md",
                                          lambda tmp: tmp.write_text(summary_markdown(summary), encoding="utf-8"))
    if "ranked_list.xlsx" in wanted:
        _tell(progress, 0.4, "Writing the spreadsheet")
        files["ranked_list.xlsx"] = write_xlsx(folder / "ranked_list.xlsx", [
            Sheet("Ranked", columns, _ranked(data)), Sheet("All properties", columns, data.properties),
            # Our columns only: the same rows are on 'All properties' and in excluded_*.csv with every
            # original column, and leaving them out here keeps the workbook quick to write.
            Sheet("Excluded", property_columns(data.asset), _excluded(data)),
            Sheet("DRILL worklist", WORKLIST_COLUMNS, worklist_rows(data)),
            Sheet("Summary", SUMMARY_COLUMNS, summary_rows(summary or {}))])
    if "crm_handoff.csv" in wanted:
        files["crm_handoff.csv"] = write_crm_handoff(conn, data, folder, stamp=stamp_handoffs)
    _tell(progress, 1.0, f"Saved {len(files)} files to {folder}")
    return {name: files[name] for name in EXPORT_FILES if name in files}


def export_zip(conn: sqlite3.Connection, market_slug: str, names: str | Iterable[str] = "excluded",
               out_dir: str | Path | None = None, zip_name: str | None = None) -> Path:
    """Write the named files and pack them in one zip (the dashboard's 'excluded.zip')."""
    files = export_market(conn, market_slug, out_dir, only=[names] if isinstance(names, str) else names)
    if not files:
        raise ValueError("Nothing to put in the zip.")
    folder = next(iter(files.values())).parent
    name = zip_name or ("excluded.zip" if names == "excluded" else "export.zip")

    def write(tmp: Path) -> None:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for fname, path in files.items():
                z.write(path, arcname=fname)
    return _atomic(folder / name, write)


def review_columns(ac: AssetClass) -> list[Col]:
    """The review file's columns: which property it is, then what you can fix, then everything else of ours
    (Property ID and Row as of last). No original Crexi columns: they can't be changed from here."""
    ours = property_columns(ac)
    by_header = {c.header: c for c in ours}
    first = [h for h in (*REVIEW_FIRST, "Buy box decision", *(e.header for e in EDITABLE)) if h in by_header]
    return [by_header[h] for h in first] + [c for c in ours if c.header not in first]


def export_review(conn: sqlite3.Connection, market_slug: str, bucket: str, out_dir: str | Path | None = None) -> Path:
    """One set-aside group as a small workbook to check and fix in Excel, then import back: a 'Review' sheet
    (the group's properties, the columns you can change first) and a 'How to edit' sheet. Written to
    output/<market>/review/review_<group>.xlsx, so the full export is never overwritten. ValueError for a
    group the app doesn't know."""
    reason, _ = buckets.parse_key(bucket)
    data = load_market(conn, market_slug)
    ids = set(overrides.group_property_ids(conn, int(data.market["id"]), bucket))
    items = [r for r in data.properties if r["property_id"] in ids]
    spec = buckets.group_spec(data.market, data.asset, data.settings)
    sample = items[0]["_prop"].get("property_type") if items else None
    group = f"{EXCLUSION_LABELS.get(reason, reason)}: {buckets.label_for(bucket, spec, sample)}"
    notes = [("Group", f"{group} ({len(items):,} {'property' if len(items) == 1 else 'properties'})"),
             ("Market", data.market["name"]),
             ("Downloaded", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")), *HOW_TO_EDIT]
    folder = Path(out_dir) if out_dir else data.settings.path("output") / market_slug / REVIEW_FOLDER
    name = "review_" + re.sub(r"[^a-z0-9_-]+", "_", bucket) + ".xlsx"
    return write_xlsx(folder / name, [
        Sheet("Review", review_columns(data.asset), items, freeze=f"{get_column_letter(len(REVIEW_FIRST) + 1)}2",
              choices={"Buy box decision": DECISION_CHOICES}),
        Sheet("How to edit", [_col("topic", "Topic"), _col("text", "What to know")],
              [{"topic": a, "text": b} for a, b in notes])])


def _wanted(only: Iterable[str] | None) -> set[str]:
    if only is None:
        return set(EXPORT_FILES)
    out: set[str] = set()
    for name in only:
        if name == "excluded":
            out.update(EXCLUDED_FILES)
        elif name in EXPORT_FILES:
            out.add(name)
        else:
            raise ValueError(f"Unknown export file '{name}'. Files: {', '.join(EXPORT_FILES)}, or 'excluded'.")
    return out


# --- small helpers -------------------------------------------------------------------------------

def _tell(progress: Progress, fraction: float, message: str) -> None:
    """Report progress. A broken callback never stops an export; a job's cancel (a BaseException) still does."""
    if progress is None:
        return
    try:
        progress(fraction, message)
    except Exception:
        log.debug("progress callback failed", exc_info=True)


def _pct(value: Any, digits: int) -> float | None:
    return None if value is None else round(float(value) * 100.0, digits)


def _round(value: Any, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def _yes_no(value: Any) -> str | None:
    if value is None:
        return None
    return "Yes" if value else "No"


__all__ = [
    "EXCLUDED_FILES", "EXPORT_FILES", "MAIN_FILES", "ROW_AS_OF", "Col", "MarketData", "Sheet", "api_usage",
    "credit_reach", "export_market", "export_review", "export_zip", "load_market", "mailing_rows",
    "property_columns", "review_columns", "summary_data", "summary_markdown", "summary_rows", "worklist_rows",
    "write_csv", "write_xlsx",
]
