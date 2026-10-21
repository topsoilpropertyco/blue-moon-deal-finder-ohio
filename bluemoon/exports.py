"""Files for Brad, the CRM and the mail house, written to output/<market>/.

    export_market(conn, "apartments-springfield-oh")      # -> {file name: Path}
    export_zip(conn, "apartments-springfield-oh", "excluded")

- ranked_list.xlsx   Sheets: Ranked (buy box, by rank), All properties, Excluded, DRILL worklist, Summary.
                     Bold frozen header, filters, colored green/yellow/red cells, clickable links.
                     Ranked and All properties also carry every original column.
- ranked_list.csv    Every property, ranked ones first (opens in Google Sheets).
- excluded_<reason>.csv  One file per filter with a plain-English reason. Rows are never deleted.
- crm_handoff.csv    Confirmed owners for the CRM. Exporting it stamps them as handed off.
- mailing_list.csv   Name and mailing address where letters are next or a mailing address is untested.
- drill_worklist.csv What to do next for each owner still being worked.
- run_summary.md     Counts, colors, layers, API use and cost this month, credits and how far they reach.

Our columns come first, then every original column from the import, under its original name.
This module never reads API keys.
"""
from __future__ import annotations

import csv
import logging
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

from . import pipeline
from .config import AssetClass, Settings, get_settings
from .db import loads, market_by_slug, rows, this_month
from .drill import credits as drill_credits
from .drill import engine, links, stats
from .enrich.owners import OWNER_TYPE_LABELS
from .scoring import (
    EXCLUSION_LABELS,
    EXCLUSION_REASONS,
    SIZE_BASIS_LABELS,
    evaluate,
    load_scoring_rows,
)

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

MAIN_FILES = ("ranked_list.xlsx", "ranked_list.csv", "crm_handoff.csv", "mailing_list.csv", "drill_worklist.csv",
              "run_summary.md")
EXCLUDED_FILES = tuple(f"excluded_{r}.csv" for r in EXCLUSION_REASONS)
EXPORT_FILES = MAIN_FILES[:2] + EXCLUDED_FILES + MAIN_FILES[2:]
CSV_ENCODING = "utf-8-sig"          # with a BOM so Excel reads accents correctly; Google Sheets ignores it
FILLS = {"green": "C6EFCE", "yellow": "FFEB9C", "red": "FFC7CE"}
HEADER_FILL = "E7E6E6"
MAX_WIDTH, MIN_WIDTH, WIDTH_SAMPLE = 50, 8, 300
_ILLEGAL_XLSX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FORMULA_START = ("=", "+", "@")
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
        _col("in_buybox", "In buy box"), _col("why_set_aside", "Why set aside"),
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
                 _col("photo_color", "Photo color", "color"), _col("photo_review", "Photo needs review")]
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
    out, headers = [], {}
    for prop, enrich in load_scoring_rows(conn, [market["id"]]):
        row = _property_row(prop, enrich, scores.get(prop["id"]), summaries.get(prop["id"]), ac, market, settings)
        headers.update(dict.fromkeys(row["_raw"]))
        out.append(row)
    out.sort(key=_list_order)
    handed = {r["property_id"] for r in rows(conn, "SELECT d.property_id FROM drill_state d JOIN properties p ON "
                                                   "p.id = d.property_id WHERE p.market_id = ? AND d.handed_off_at "
                                                   "IS NOT NULL", (market["id"],))}
    return MarketData(settings, market, ac, out, list(headers), summaries, handed)


def _list_order(r: dict) -> tuple:
    """Ranked first (by rank), then the rest by score, then never-scored ones."""
    if r["rank"] is not None:
        return (0, r["rank"], 0.0, r["property_id"])
    return (1 if r["_score"] is not None else 2, 0, -(r["_score"] or 0.0), r["property_id"])


def _property_row(prop: dict, enrich: dict, score: dict | None, summary: dict | None, ac: AssetClass,
                  market: dict, settings: Settings) -> dict:
    reasons = loads(score.get("exclusion_reasons"), []) if score else []
    details = evaluate(prop, enrich, ac, market, settings).get("exclusion_details", {}) if reasons else {}
    row = {
        "property_id": prop["id"], "_score": (score or {}).get("score"), "_reasons": reasons, "_details": details,
        "_raw": loads(prop.get("raw_json"), {}) or {}, "_prop": prop,
        "rank": (score or {}).get("rank"), "score_pct": _pct((score or {}).get("score"), 2),
        "in_buybox": "Not scored yet" if score is None else _yes_no(score.get("in_buybox")),
        "why_set_aside": "; ".join(_reason_text(r, details) for r in reasons),
        **{k: prop.get(k) for k in ("address", "unit", "city", "state", "zip", "county", "property_type", "units",
                                    "building_sqft", "year_built", "lot_acres", "owner_name")},
        "flags": ", ".join(loads(prop.get("flags_json"), []) or []),
    }
    row.update(_score_fields(score))
    row.update(_layer_fields(enrich))
    row.update(_drill_fields(summary))
    row.update(_link_fields(prop))
    return row


def _score_fields(score: dict | None) -> dict:
    s = score or {}
    have, total = s.get("coverage_have"), s.get("coverage_total")
    return {"size_value": _round(s.get("size_value"), 1),
            "size_basis": SIZE_BASIS_LABELS.get(s.get("size_basis") or "", s.get("size_basis")),
            "size_color": s.get("size_color"), "photo_color": s.get("visual_color"), "rent_color": s.get("rent_color"),
            "signals_known": f"{have} of {total}" if total else None}


def _layer_fields(enrich: dict) -> dict:
    drive_row, rent_row = enrich.get("drive") or {}, enrich.get("rent") or {}
    visual, owner = enrich.get("visual") or {}, enrich.get("owner") or {}
    checked = bool(visual.get("ai_label") or visual.get("reviewed_label"))
    return {
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
    if isinstance(value, str) and value.startswith(_FORMULA_START):
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


def write_xlsx(path: Path, sheets: list[Sheet]) -> Path:
    """A workbook with a bold frozen header, filters, sensible widths, colored color-columns and links."""
    def write(tmp: Path) -> None:
        wb = Workbook(write_only=True)
        for sheet in sheets:
            _write_sheet(wb.create_sheet(sheet.name[:31]), sheet)
        wb.save(tmp)
    return _atomic(path, write)


def _write_sheet(ws: Any, sheet: Sheet) -> None:
    cols = sheet.columns
    for i, width in enumerate(_widths(cols, sheet.items), start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
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


def _xlsx_cell(ws: Any, col: Col, value: Any, fills: dict[str, PatternFill]) -> Any:
    if value is None or value == "":
        return None
    if col.kind == "link":
        cell = WriteOnlyCell(ws, value=col.link_text or "Open")
        cell.hyperlink = str(value)
        cell.style = "Hyperlink"
        return cell
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


def write_crm_handoff(conn: sqlite3.Connection, data: MarketData, folder: Path) -> Path:
    """Confirmed owners for the CRM, then stamp the new ones as handed off (only after the file is saved)."""
    found = stats.crm_handoff_rows(conn, data.slug)
    for r in found:
        r["new_in_this_export"] = "No" if r.get("handed_off_at") else "Yes"
    header = list(found[0]) if found else [*CRM_COLUMNS, "new_in_this_export"]
    path = write_dict_csv(folder / "crm_handoff.csv", header, found)
    engine.mark_handed_off(conn, [r["property_id"] for r in found if not r.get("handed_off_at")])
    return path


def mailing_rows(conn: sqlite3.Connection, data: MarketData) -> list[dict]:
    """Where to send letters, buy box first: never a do-not-contact owner, never a confirmed one (they're in
    the CRM)."""
    mail = _mail_contacts(conn, data.market["id"])
    out = []
    for r in data.properties:
        summary = data.summaries.get(r["property_id"]) or {}
        if summary.get("status") in ("dnc", "confirmed"):
            continue
        contacts = mail.get(r["property_id"], [])
        untested = [c for c in contacts if c["status"] == "untested"]
        if _letters_next(summary):
            out += _mail_lines(r, contacts, summary, "Letters are the next DRILL step") or \
                   _mail_lines(r, [None], summary, "Letters are the next DRILL step")
        elif untested:
            out += _mail_lines(r, untested, summary, "Mailing address not tested yet")
    return out


def _mail_contacts(conn: sqlite3.Connection, market_id: int) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for c in rows(conn, "SELECT cp.id, cp.property_id, cp.value, cp.status, cp.source, pe.name AS person_name "
                        "FROM contact_points cp JOIN properties p ON p.id = cp.property_id "
                        "LEFT JOIN people pe ON pe.id = cp.person_id WHERE p.market_id = ? AND cp.channel = 'mail' "
                        "AND cp.status IN ('untested', 'verified') ORDER BY cp.id", (market_id,)):
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
    """This month's credits and how far down the ranked list they reach."""
    status = drill_credits.credit_status(conn)
    plan = drill_credits.credit_plan(conn, market_slug, limit=status["left"]) if status["left"] else []
    ranks = [p["rank"] for p in plan if p.get("rank")]
    reach = max(ranks) if ranks else None
    if not status["left"]:
        text = status["message"]
    elif not plan:
        text = (f"{status['left']:,} credits left, but no owner qualifies for one yet: work the free DRILL stages "
                f"to turn more owners into possible owners.")
    else:
        text = f"The {status['left']:,} credits left this month cover {len(plan):,} owners"
        text += f", reaching down to rank #{reach:,} of {ranked_total:,} in the buy box." if reach else "."
        if len(plan) < status["left"]:
            text += f" {status['left'] - len(plan):,} credits have no target yet."
    return {**status, "planned": len(plan), "reach_rank": reach, "ranked_total": ranked_total, "reach_text": text}


def api_usage(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Metered calls this month (all markets) and what they cost after the free monthly calls."""
    prices = settings.section("google").get("prices") or {}
    per_call_api = float(((settings.section("vision").get("anthropic_api") or {}).get("est_cost_per_call_usd")) or 0.03)
    items = []
    for r in rows(conn, "SELECT provider, sku, SUM(units) AS units FROM api_calls WHERE substr(created_at, 1, 7) = ? "
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
    out.append(f"- Set aside by a filter (kept, see excluded_*.csv): {s['excluded']:,}")
    for reason in EXCLUSION_REASONS:
        if s["exclusions"].get(reason):
            out.append(f"  - {EXCLUSION_LABELS[reason]}: {s['exclusions'][reason]:,}")
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
        ("List", "Set aside by a filter", s["excluded"]), ("List", "Not scored yet", s["not_scored"]),
    ]
    out += [("Set aside", EXCLUSION_LABELS[r], s["exclusions"].get(r, 0)) for r in EXCLUSION_REASONS]
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
                  only: Iterable[str] | None = None, progress: Progress = None) -> dict[str, Path]:
    """Write the export files and return {file name: path}.

    out_dir: default output/<market slug>/. only: just these files (names from EXPORT_FILES, or
    'excluded' for every excluded_<reason>.csv). crm_handoff.csv stamps newly confirmed owners as
    handed off.
    """
    wanted = _wanted(only)
    folder = Path(out_dir) if out_dir else get_settings().path("output") / market_slug
    _tell(progress, 0.05, "Gathering the list")
    data = load_market(conn, market_slug)
    files: dict[str, Path] = {}
    columns = data.columns()
    if "ranked_list.csv" in wanted:
        files["ranked_list.csv"] = write_csv(folder / "ranked_list.csv", columns, data.properties)
    for reason in EXCLUSION_REASONS:
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
        files["crm_handoff.csv"] = write_crm_handoff(conn, data, folder)
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
    "EXCLUDED_FILES", "EXPORT_FILES", "MAIN_FILES", "Col", "MarketData", "Sheet", "api_usage", "credit_reach",
    "export_market", "export_zip", "load_market", "mailing_rows", "property_columns", "summary_data",
    "summary_markdown", "summary_rows", "worklist_rows", "write_csv", "write_xlsx",
]
