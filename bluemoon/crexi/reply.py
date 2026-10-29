"""Crexi's reply file: read it, match every row to the properties we sent, show a preview, apply it.

What Crexi sends back (an .xlsx or .xlsm; the sheet is 'Ownership Information'): a note row such as
'395 properties found, 93 not matched, 5 duplicates', then the header row (Property Link, Property Type,
Address, City, State, Zip Code, County, APN, Owner Name, Mailing Address..., Contact Name, Phone 1-5,
Email 1-5), then one row per property. A row Crexi MATCHED has a Property Link and (usually) an owner; a row it
could not match has Property Type 'Not Matched', the address we sent, and nothing else. Our own ids do not come
back, so every row is matched to our batches by Property Link, then parcel number (APN), then street + city +
5-digit ZIP, across every market and every batch that still has rows waiting (the newest batch first), so
one file may hold several batches' rows.

Credit rule: a matched row = one credit spent (even with no phone or email); a 'Not Matched' row costs
nothing. Importing the reply FINISHES every batch the file touches at once (batches.finish_batches): rows that
came back matched are returned; rows Crexi answered 'Not Matched' or left out of the file are missing ("Crexi
didn't have it"): their credits are free again right away and the DRILL moves those owners on to its other
sources. A batch with no row in the file is not touched. A later file that brings back a missing row records it
as returned then (the credit is charged then and the DRILL result upgraded).

Each matched row ends in one of three results: found (a phone or email), owner_only (an owner or contact name
but no phone or email) or nothing. Contacts (up to 5 phones, 5 emails and the Contact Name, the phones and
emails belong to that person) are added as untested, source 'crexi', respecting do-not-contact. Property facts
(owner, mailing address, sizes...) are merged into the property the row matched, with the keep-the-original rule
in crexi/merge.py (never a new property; a different value is kept as "Units 2", "Owner 2"..., tagged with the
batch). Importing the same rows again changes nothing.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import re
import shutil
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..db import dumps, loads, now_iso, one, rows, scalar
from ..drill import credits as drill_credits
from ..drill import engine as E
from ..ingest import crexi as ing
from ..ingest.columns import map_columns, norm_header
from ..ingest.edits import latest_edits
from ..util import clean_str, normalize_address, normalize_county
from . import batches as B
from . import merge as M
from . import period as P

ALLOWED_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm")
SPLIT = re.compile(r"[;,|\n\r]+")
PHONE_HEADER = re.compile(r"\b(phone|phones|telephone|mobile|cell|cellphone|tel)\b")
EMAIL_HEADER = re.compile(r"\b(e mail|email|emails)\b")
NAME_HEADER = re.compile(r"^(owner )?contact( person)?( name)?$|^contact name$")
CONTACT_WORD = re.compile(r"contact")
NOT_MATCHED = "not matched"
PHONE_CAP, EMAIL_CAP = 10, 10     # more values than this in one row are ignored (a spreadsheet gone wrong)

WHY = {
    "none": "Does not match any property in your lists (Crexi link, parcel number or address).",
    "no_batch": "One of your properties, but it was not in any batch.",
    "duplicate": "A second row for a property already in this file. Its data was added to that property, not saved as "
                 "a new one.",
    "not_matched": "Crexi could not match this one (no credit is spent on it).",
}


log = logging.getLogger("bluemoon.crexi.reply")


class ReplyError(ValueError):
    """A problem with the file; the message is plain English."""


# --- reading ----------------------------------------------------------------------------------------------

def read_reply(path: Path) -> ing.Table:
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ReplyError("Upload Crexi's file as .xlsx, .xlsm or .csv.")
    try:
        return ing._read(path)
    except ing.IngestError as exc:
        raise ReplyError(str(exc)) from None


# --- which columns hold contact data ---------------------------------------------------------------------

def _segments(cell: Any) -> list[str]:
    s = clean_str(cell)
    if not s:
        return []
    return [x.strip() for x in SPLIT.split(str(s)) if x.strip()]


def _phone(value: str) -> tuple[str, str] | None:
    try:
        return E.normalize_contact("phone", value)
    except ValueError:
        return None


def _email(value: str) -> tuple[str, str] | None:
    try:
        return E.normalize_contact("email", value)
    except ValueError:
        return None


def detect_columns(headers: list[str], data: list[dict], overrides: dict | None = None,
                   skip: set[str] | None = None) -> dict:
    """Which columns hold phones, emails and the contact name. Found by the words in the header (phone, mobile,
    cell, email, e-mail, contact); a 'contact' column is read by what is in it. `overrides`
    ({'phone': [headers], 'email': [headers], 'name': header or None}) replaces the guess for that kind. Returns
    {phone: [..], email: [..], name: header|None, columns: [{header, kind, values, unreadable}], notes: [..]}."""
    skip = skip or set()
    found: dict[str, list[str]] = {"phone": [], "email": []}
    name: str | None = None
    for h in headers:
        if h in skip:
            continue
        n = norm_header(h)
        if NAME_HEADER.match(n):
            name = name or h
        elif EMAIL_HEADER.search(n):
            found["email"].append(h)
        elif PHONE_HEADER.search(n):
            found["phone"].append(h)
        elif CONTACT_WORD.search(n):
            sample = [s for r in data[:200] for s in _segments(r.get(h))]
            if any("@" in s for s in sample):
                found["email"].append(h)
            elif sum(1 for s in sample if _phone(s)) > len(sample) / 2 and sample:
                found["phone"].append(h)
    notes = []
    for kind in ("phone", "email"):
        if overrides and kind in overrides and overrides[kind] is not None:
            wanted = overrides[kind] if isinstance(overrides[kind], list) else [overrides[kind]]
            real = [h for h in wanted if h in headers]
            for h in wanted:
                if h not in headers:
                    notes.append(f"There is no column called '{h}' in this file, so it was ignored.")
            found[kind] = real
    if overrides and "name" in overrides:
        v = overrides["name"]
        name = v if (v in headers) else (None if not v else name)
        if v and v not in headers:
            notes.append(f"There is no column called '{v}' in this file, so it was ignored.")
    cols = []
    for kind, hs in (("phone", found["phone"]), ("email", found["email"])):
        for h in hs:
            ok = bad = 0
            for r in data:
                for s in _segments(r.get(h)):
                    good = (_phone(s) if kind == "phone" else _email(s)) is not None
                    ok += good
                    bad += not good
            cols.append({"header": h, "kind": kind, "values": ok, "unreadable": bad})
    if name:
        cols.append({"header": name, "kind": "name", "values": sum(1 for r in data if clean_str(r.get(name))),
                     "unreadable": 0})
    return {"phone": found["phone"], "email": found["email"], "name": name, "columns": cols, "notes": notes}


# --- rows -------------------------------------------------------------------------------------------------

@dataclass
class Row:
    i: int
    raw: dict
    link_key: str | None = None
    apn: str | None = None
    state: str | None = None
    county: str | None = None
    street: str | None = None
    city: str = ""
    zip5: str = ""
    owner_name: str | None = None
    contact_name: str | None = None
    phones: list[tuple[str, str]] = field(default_factory=list)
    emails: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    not_matched: bool = False
    kind: str = "none"
    pid: int | None = None
    batch_id: int | None = None

    @property
    def result(self) -> str:
        if self.phones or self.emails:
            return "found"
        if self.contact_name or self.owner_name:
            return "owner_only"
        return "nothing"


def _parse_rows(table: ing.Table, column_map: dict[str, str], cols: dict) -> list[Row]:
    out = []
    g = lambda r, f: r.get(column_map[f]) if f in column_map else None  # noqa: E731
    lines = table.row_numbers
    for i, raw in enumerate(table.rows, start=1):
        # the row's own line in Crexi's file (what Seth sees in Excel), so 'File row' in the lists points at it
        row = Row(i=lines[i - 1] if len(lines) == len(table.rows) else i, raw=raw)
        row.link_key = B.link_key(ing._text(g(raw, "crexi_url")))
        apn = re.sub(r"[^a-z0-9]", "", (ing._text(g(raw, "apn")) or "").lower())
        row.apn = apn or None
        row.state = ing._state(g(raw, "state"))
        row.county = normalize_county(ing._text(g(raw, "county")))
        addr = ing._text(g(raw, "address"))
        row.street = normalize_address(addr)
        row.city = (clean_str(g(raw, "city")) or "").upper()
        row.zip5 = B._zip5(g(raw, "zip"))
        ptype = (ing._text(g(raw, "property_type")) or "").lower()
        row.owner_name = ing._text(g(raw, "owner_name"))
        row.contact_name = ing._text(raw.get(cols["name"])) if cols.get("name") else None
        row.not_matched = ptype == NOT_MATCHED or (not row.link_key and not row.owner_name and not row.contact_name
                                                   and ptype in ("", NOT_MATCHED))
        seen_p, seen_e = set(), set()
        for h in cols["phone"]:
            for s in _segments(raw.get(h)):
                v = _phone(s)
                if v is None:
                    row.unreadable.append((h, s))
                elif v[1] not in seen_p and len(seen_p) < PHONE_CAP:
                    seen_p.add(v[1])
                    row.phones.append(v)
        for h in cols["email"]:
            for s in _segments(raw.get(h)):
                v = _email(s)
                if v is None:
                    row.unreadable.append((h, s))
                elif v[1] not in seen_e and len(seen_e) < EMAIL_CAP:
                    seen_e.add(v[1])
                    row.emails.append(v)
        out.append(row)
    return out


# --- matching ---------------------------------------------------------------------------------------------

@dataclass
class Index:
    props: dict[int, dict]
    link: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    apn: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    addr: dict[tuple, list[int]] = field(default_factory=lambda: defaultdict(list))
    addr_no_city: dict[tuple, list[int]] = field(default_factory=lambda: defaultdict(list))


def _build_index(conn: sqlite3.Connection) -> Index:
    props = {r["id"]: r for r in rows(conn, "SELECT id, market_id, crexi_url, source_id, apn, state, county, address, "
                                            "unit, city, zip FROM properties ORDER BY id")}
    ix = Index(props)
    for pid, p in props.items():
        for k in {B.link_key(p["crexi_url"]), (clean_str(p["source_id"]) or "").lower() or None} - {None}:
            ix.link[k].append(pid)
        apn = re.sub(r"[^a-z0-9]", "", (clean_str(p["apn"]) or "").lower())
        if apn:
            ix.apn[apn].append(pid)
        street = normalize_address(p["address"])
        if street:
            ix.addr[(street, (clean_str(p["city"]) or "").upper(), B._zip5(p["zip"]))].append(pid)
            ix.addr_no_city[(street, B._zip5(p["zip"]))].append(pid)
    return ix


def _candidates(row: Row, ix: Index) -> tuple[str, list[int]]:
    """The properties this row could be: by Crexi link, else parcel number, else street + city + ZIP."""
    if row.link_key and ix.link.get(row.link_key):
        return "link", list(ix.link[row.link_key])
    if row.apn and ix.apn.get(row.apn):
        keep = []
        for pid in ix.apn[row.apn]:
            p = ix.props[pid]
            if row.state and p["state"] and row.state != p["state"]:
                continue
            if row.county and p["county"] and row.county != normalize_county(p["county"]):
                continue
            keep.append(pid)
        if len(keep) > 1 and row.street:
            # one parcel number, several of our properties: the row's own street (and ZIP) picks the right one
            same = [pid for pid in keep if normalize_address(ix.props[pid]["address"]) == row.street
                    and (not row.zip5 or not ix.props[pid]["zip"] or B._zip5(ix.props[pid]["zip"]) == row.zip5)]
            if len(same) == 1:
                keep = same
        if keep:
            return "apn", keep
    if row.street:
        found = ix.addr.get((row.street, row.city, row.zip5))
        if not found and not row.city:
            found = ix.addr_no_city.get((row.street, row.zip5))
        if not found and not row.zip5 and row.city:
            found = [pid for (st, city, _z), pids in ix.addr.items() if st == row.street and city == row.city
                     for pid in pids]
        if found and not B.has_house_number(row.street) and len(set(found)) > 1:
            found = None      # a street with no house number is not one building: several properties share it
        if found:
            return "address", list(found)
    return "", []


@dataclass
class Analysis:
    rows: list[Row]
    items: dict[tuple[int, int], dict]               # (batch id, property id) -> item + batch info
    by_prop: dict[int, list[dict]]
    ix: Index
    claimed: dict[tuple[int, int], Row] = field(default_factory=dict)         # matched rows that return an item
    flagged: dict[tuple[int, int], Row] = field(default_factory=dict)         # 'Not Matched' rows for an item
    extras: dict[tuple[int, int], list[Row]] = field(default_factory=lambda: defaultdict(list))
    methods: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def _load_items(conn: sqlite3.Connection) -> tuple[dict, dict]:
    items, by_prop = {}, defaultdict(list)
    for r in rows(conn, """
            SELECT i.*, b.status AS batch_status, b.period, b.kind AS batch_kind FROM crexi_batch_items i
            JOIN crexi_batches b ON b.id = i.batch_id WHERE i.status != 'withdrawn' ORDER BY i.batch_id DESC"""):
        items[(r["batch_id"], r["property_id"])] = r
        by_prop[r["property_id"]].append(r)
    return items, by_prop


def _anchors(parsed: list[Row], found: dict, by_prop: dict) -> Counter:
    """{batch id: rows of this file that can only belong to that batch} (their property was in one batch only)."""
    out: Counter = Counter()
    for row in parsed:
        bids = {it["batch_id"] for pid in found[id(row)][1] for it in by_prop.get(pid, [])}
        if len(bids) == 1:
            out[bids.pop()] += 1
    return out


def _answering(items: list[dict], anchors: Counter) -> list[dict]:
    """A property sent more than once (a second pass): the row belongs to the batch this file clearly answers,
    the one with the most rows that can only be its own. An old reply opened and saved again (or downloaded
    again) has hundreds of rows only the old batch had, so it answers the old batch and never charges or
    finishes the second pass still waiting at Crexi. With no clear answer, the newest batch first (as before)."""
    if len(items) < 2:
        return items
    best = max(items, key=lambda it: (anchors[it["batch_id"]], it["batch_id"]))
    if anchors[best["batch_id"]] > max(anchors[it["batch_id"]] for it in items if it is not best):
        return [best]
    return items


def first_imported(conn: sqlite3.Connection, path: Path) -> str | None:
    """When this exact file was first imported (None if never)."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return scalar(conn, "SELECT MIN(created_at) FROM crexi_batch_imports WHERE file_hash = ?", (digest,))


def analyze(conn: sqlite3.Connection, table: ing.Table, column_map: dict[str, str], cols: dict, *,
            adopt_hand: bool = False, first_seen: str | None = None) -> Analysis:
    """Match every row. Matched rows first (so a 'Not Matched' row never undoes a match), then the rest.
    With adopt_hand, a row for one of our properties that is in no batch is counted as a hand batch (the
    apply step creates it). `first_seen`: when this same file was first imported; its rows never claim or flag a
    property sent after that (a second pass Crexi has not answered yet), so importing an old file again never
    charges or finishes a newer batch. A property sent twice (a second pass) goes to the batch this file clearly
    answers (_answering), so a re-saved copy of an old reply never finishes the second pass either."""
    parsed = _parse_rows(table, column_map, cols)
    items, by_prop = _load_items(conn)
    an = Analysis(parsed, items, by_prop, _build_index(conn))
    ix = an.ix
    adopted: set[int] = set()
    handled: set[int] = set()     # properties a row of this file already claimed or flagged: a second row is a duplicate
    found = {id(row): _candidates(row, ix) for row in parsed}
    anchors = _anchors(parsed, found, by_prop)
    for matched_pass in (True, False):
        for row in parsed:
            if row.not_matched == matched_pass:
                continue
            how, cands = found[id(row)]
            if how:
                an.methods[how] += 1
            claim = None
            for pid in cands:
                if pid in handled:
                    continue
                for it in _answering(by_prop.get(pid, []), anchors):
                    key = (it["batch_id"], pid)
                    if it["status"] in ("sent", "not_returned") and key not in an.claimed and key not in an.flagged:
                        if first_seen and (it["sent_at"] or "") >= first_seen:
                            # this file was first imported before that item was sent: it can't be Crexi's answer
                            # to it, so it never charges or finishes a newer batch (a second pass still waiting)
                            continue
                        if not row.not_matched or not it["not_matched_at"]:
                            claim = (pid, it)
                            break
                if claim:
                    break
            if claim:
                pid, it = claim
                key = (it["batch_id"], pid)
                row.pid, row.batch_id = pid, it["batch_id"]
                handled.add(pid)
                if row.not_matched:
                    row.kind = "not_matched"
                    an.flagged[key] = row
                else:
                    row.kind = "new"
                    an.claimed[key] = row
                continue
            row.pid = cands[0] if cands else None
            if not cands:
                row.kind = "none"
                continue
            # a candidate already handled in this file, or returned earlier, or never sent
            if any((it["batch_id"], pid) in an.claimed or (it["batch_id"], pid) in an.flagged
                   for pid in cands for it in by_prop.get(pid, [])):
                hit = next((pid, it) for pid in cands for it in by_prop.get(pid, [])
                           if (it["batch_id"], pid) in an.claimed or (it["batch_id"], pid) in an.flagged)
                row.pid, row.batch_id = hit[0], hit[1]["batch_id"]
                row.kind = "duplicate"
                an.extras[(row.batch_id, row.pid)].append(row)
            elif any(it["status"] == "returned" or it["not_matched_at"] for pid in cands for it in by_prop.get(pid, [])):
                hit = next((pid, it) for pid in cands for it in by_prop.get(pid, [])
                           if it["status"] == "returned" or it["not_matched_at"])
                row.pid, row.batch_id = hit[0], hit[1]["batch_id"]
                row.kind = "already"
            elif adopt_hand and not row.not_matched:
                row.kind = "hand"
                adopted.add(row.pid)
            else:
                row.kind = "no_batch"
    # a second row for a property adopted by hand is a duplicate of the first
    first_hand: dict[int, Row] = {}
    for row in parsed:
        if row.kind == "hand":
            if row.pid in first_hand:
                row.kind = "duplicate"
                an.extras[(0, row.pid)].append(row)
            else:
                first_hand[row.pid] = row
    return an


# --- planning what a file would add (the enrichment report) -------------------------------------------------

def _known(conn: sqlite3.Connection, pids: list[int]) -> tuple[dict, dict]:
    """({(pid, family): {keys}} of contacts we hold, {pid: {name keys}} of people)."""
    contacts: dict[tuple[int, str], set[str]] = defaultdict(set)
    people: dict[int, set[str]] = defaultdict(set)
    for i in range(0, len(pids), 500):
        chunk = pids[i:i + 500]
        mark = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT property_id, channel, value FROM contact_points WHERE property_id IN ({mark})", chunk):
            contacts[(r["property_id"], E._contact_family(r["channel"]))].add(E._contact_key(r["channel"], r["value"]))
        for r in rows(conn, f"SELECT property_id, name FROM people WHERE property_id IN ({mark})", chunk):
            people[r["property_id"]].add(E._name_key(r["name"]))
    return contacts, people


def plan_enrichment(conn: sqlite3.Connection, an: Analysis, table: ing.Table, column_map: dict[str, str],
                    settings: Settings) -> M.Report:
    """What this file adds that we do not already have (the report shown before applying): new phones, emails,
    names, mailing addresses, blanks filled, differing values kept as alternates, properties gaining nothing."""
    rep = M.Report()
    rep.add("rows_read", len(an.rows))
    # 'matched' = the rows this import returns now (the same number as the headline's "returned"); rows already
    # imported and duplicates are counted apart, so the two lines of the preview agree
    rep.add("matched", sum(1 for r in an.rows if not r.not_matched and r.kind in ("new", "hand")))
    rep.add("duplicates", sum(1 for r in an.rows if r.kind == "duplicate"))
    groups: dict[int, list[Row]] = defaultdict(list)
    for r in an.rows:
        if r.kind in ("new", "hand") or (r.kind == "duplicate" and not r.not_matched):
            if r.pid is not None:
                groups[r.pid].append(r)
    pids = sorted(groups)
    if not pids:
        return rep
    contacts, people = _known(conn, pids)
    props = {}
    for i in range(0, len(pids), 500):
        chunk = pids[i:i + 500]
        for p in rows(conn, f"SELECT * FROM properties WHERE id IN ({','.join('?' for _ in chunk)})", chunk):
            props[p["id"]] = p
    edits_by_market: dict[int, dict] = {}
    opts_by_market: dict[int, ing.ImportOptions] = {}
    mnames = {m["id"]: m["asset_class"] for m in rows(conn, "SELECT id, asset_class FROM markets")}
    for pid in pids:
        prop = props[pid]
        mid = prop["market_id"]
        if mid not in edits_by_market:
            edits_by_market[mid] = latest_edits(conn, mid, [q for q in pids if props[q]["market_id"] == mid])
            opts_by_market[mid] = ing._options(settings, mnames.get(mid), "crexi")
        gained = 0
        known_alt = rows(conn, "SELECT field, value FROM property_alternates WHERE property_id = ?", (pid,))
        seen_alt = {(a["field"], str(loads(a["value"]))) for a in known_alt}
        seen_phone = set(contacts[(pid, "phone")])
        seen_email = set(contacts[(pid, "email")])
        seen_name = set(people[pid])
        seen_mail = set(contacts[(pid, "mail")])
        first = True
        for r in groups[pid]:
            try:
                p = ing.parse_row(r.raw, column_map, opts_by_market[mid])
                plan, _, _ = ing.plan_update(prop, p, edits_by_market[mid].get(pid))
            except Exception:
                plan = M.FactsPlan()
            if first:
                n = M.filled_count(plan)
                rep.add("blanks_filled", n)
                gained += n
                first = False
            for f, (new, _old) in plan.alternates.items():
                k = (f, str(new))
                if k not in seen_alt and not any(
                        f == a["field"] and M._same(f, loads(a["value"]), new) for a in known_alt):
                    seen_alt.add(k)
                    rep.add("alternates")
                    gained += 1
            if plan.new_owner and E._name_key(plan.new_owner) not in seen_name:
                seen_name.add(E._name_key(plan.new_owner))
                rep.add("new_names")
                gained += 1
            if plan.new_mailing and E._contact_key("mail", plan.new_mailing) not in seen_mail:
                seen_mail.add(E._contact_key("mail", plan.new_mailing))
                rep.add("new_mailing")
                gained += 1
            if r.contact_name and E._name_key(r.contact_name) not in seen_name:
                seen_name.add(E._name_key(r.contact_name))
                rep.add("new_names")
                gained += 1
            for _disp, key in r.phones:
                if key not in seen_phone:
                    seen_phone.add(key)
                    rep.add("new_phones")
                    gained += 1
            for _disp, key in r.emails:
                if key not in seen_email:
                    seen_email.add(key)
                    rep.add("new_emails")
                    gained += 1
        rep.add("properties")
        if not gained:
            rep.add("gained_nothing")
    return rep


# --- the preview -------------------------------------------------------------------------------------------

def _clean_columns(columns: dict | None) -> dict | None:
    if not columns:
        return None
    out: dict[str, Any] = {}
    for k in ("phone", "email"):
        if k in columns and columns[k] is not None:
            v = columns[k]
            out[k] = [str(x) for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
    if "name" in columns:
        out["name"] = clean_str(columns["name"])
    return out or None


def _summary(an: Analysis, conn: sqlite3.Connection) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for r in an.rows:
        counts[r.kind] += 1
    per: dict[int, dict] = {}
    for key, row in list(an.claimed.items()) + list(an.flagged.items()):
        b = per.setdefault(key[0], {"batch_id": key[0], "new": 0, "found": 0, "owner_only": 0, "no_data": 0,
                                    "not_matched": 0, "already": 0})
        if row.kind == "new":
            b["new"] += 1
            b[{"found": "found", "owner_only": "owner_only", "nothing": "no_data"}[row.result]] += 1
        else:
            b["not_matched"] += 1
    for r in an.rows:
        if r.kind == "already" and r.batch_id is not None:
            per.setdefault(r.batch_id, {"batch_id": r.batch_id, "new": 0, "found": 0, "owner_only": 0, "no_data": 0,
                                        "not_matched": 0, "already": 0})["already"] += 1
    out = []
    for bid in sorted(per, reverse=True):
        b = B.get_batch(conn, bid)
        c = B.batch_counts(conn, bid)
        returned = c["returned"] + per[bid]["new"]
        out.append({**per[bid], "sent": c["sent"], "status": b["status"], "closed": b["status"] == "closed",
                    "kind": b["kind"], "returned": returned, "missing": max(0, c["sent"] - returned),
                    "not_in_file": max(0, c["waiting"] - per[bid]["new"] - per[bid]["not_matched"]),
                    "matched_pct": round(returned / c["sent"], 4) if c["sent"] else None})
    return {"counts": dict(counts), "batches": out}


def _totals(batches: list[dict]) -> dict:
    t = {k: sum(int(b.get(k) or 0) for b in batches) for k in ("sent", "returned", "missing", "not_matched",
                                                                 "not_in_file", "new")}
    t["matched_pct"] = round(t["returned"] / t["sent"], 4) if t["sent"] else None
    return t


def _message(an: Analysis, summ: dict) -> str:
    c = summ["counts"]
    cant = c.get("none", 0) + c.get("no_batch", 0)
    bs = summ["batches"]
    t = _totals(bs)
    why = []
    if t["not_matched"]:
        why.append(f"{t['not_matched']:,} 'Not Matched'")
    if t["not_in_file"]:
        why.append(f"{t['not_in_file']:,} not in the file")
    tail = (f" ({', '.join(why)})" if why else "") + (f"; {cant:,} rows can't be matched to your properties"
                                                       if cant else "")
    if len(bs) == 1:
        b = bs[0]
        head = (f"This looks like Crexi's reply to batch #{b['batch_id']} ({b['sent']:,} sent): Crexi returned "
                f"{t['returned']:,} of {t['sent']:,}, {t['missing']:,} missing{tail}.")
    elif bs:
        names = ", ".join(f"#{b['batch_id']}" for b in bs)
        head = (f"This file holds rows for batches {names}: Crexi returned {t['returned']:,} of {t['sent']:,}, "
                f"{t['missing']:,} missing{tail}.")
    else:
        head = (f"None of the {len(an.rows):,} rows match a property waiting at Crexi: {cant:,} can't be matched"
                + (f", {c.get('already', 0):,} were already recorded" if c.get("already") else "") + ".")
    extra = []
    if bs and t["missing"]:
        head += f" The {t['missing']:,} missing cost no credit and go to the DRILL's other sources."
    if c.get("already"):
        extra.append(f"{c['already']:,} rows were already imported (nothing changes for them)")
    if c.get("duplicate"):
        extra.append(f"{c['duplicate']:,} duplicate row{'s' if c['duplicate'] != 1 else ''} (their data is added to the "
                     f"property, not saved as a new one)")
    if c.get("no_batch"):
        extra.append(f"{c['no_batch']:,} rows are your properties that were not in any batch (tick 'record these as a "
                     f"hand batch' to count them)")
    if c.get("hand"):
        extra.append(f"{c['hand']:,} rows will be recorded as a hand batch")
    return head + (" " + "; ".join(extra) + "." if extra else "")


def preview(conn: sqlite3.Connection, path: Path, filename: str | None = None, *, columns: dict | None = None,
            adopt_hand: bool = False, settings: Settings | None = None) -> dict:
    """What importing the file would do, written nowhere."""
    s = settings or get_settings()
    table = read_reply(path)
    column_map, _unmapped = map_columns(table.headers, ing._merged_overrides(s, None))
    cols = detect_columns(table.headers, table.rows, _clean_columns(columns), skip=set(column_map.values()))
    an = analyze(conn, table, column_map, cols, adopt_hand=adopt_hand, first_seen=first_imported(conn, path))
    summ = _summary(an, conn)
    rep = plan_enrichment(conn, an, table, column_map, s)
    warnings = list(cols["notes"])
    closed = [b["batch_id"] for b in summ["batches"] if b["closed"] and (b["new"] or b["not_matched"])]
    for bid in closed:
        warnings.append(f"Batch #{bid} was already finished. Rows for it that come back now are recorded (a late "
                        f"reply): each one Crexi matched is charged a credit now.")
    bad = sum(len(r.unreadable) for r in an.rows)
    if bad:
        warnings.append(f"{bad:,} phone or email value{'s' if bad != 1 else ''} could not be read and will be listed "
                        f"in a file instead of saved.")
    if cols["name"] is None:
        warnings.append("I could not find a Contact Name column, so phones and emails are saved without a person.")
    if not cols["phone"] and not cols["email"]:
        warnings.append("I could not find any phone or email columns. If this file has them, pick the columns below.")
    over = int(drill_credits.credit_status(conn, settings=s)["per_month"])
    rep_d = rep.to_dict()
    return {"filename": filename or path.name, "sheet": table.sheet, "rows": len(an.rows),
            "header_row": table.header_row, "columns": cols, "headers": table.headers,
            "summary": summ, "message": _message(an, summ), "enrichment": rep_d,
            "enrichment_text": M.report_text(rep_d), "warnings": warnings, "matched_by": dict(an.methods),
            "unreadable": bad, "credits_per_period": over,
            "can_apply": bool(summ["counts"].get("new") or summ["counts"].get("not_matched")
                              or summ["counts"].get("hand") or summ["counts"].get("duplicate")),
            "changes": bool(summ["counts"].get("new") or summ["counts"].get("not_matched")
                            or summ["counts"].get("hand"))}


# --- applying ---------------------------------------------------------------------------------------------

def _write_unmatched(conn: sqlite3.Connection, an: Analysis, headers: list[str], import_id: int,
                     settings: Settings) -> str | None:
    """output/crexi/<period>/unmatched-<import>.csv: every row that was not applied as a new return (never dropped)."""
    left = [r for r in an.rows if r.kind in ("none", "no_batch", "duplicate", "not_matched")]
    if not left:
        return None
    rel = f"crexi/{P.period_key(None, settings)}/unmatched-{import_id}.csv"
    path = B.output_dir(settings) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=B.CSV_ENCODING) as f:
        w = csv.writer(f)
        w.writerow(["Why", "File row", *[B.safe_cell(h) for h in headers]])
        for r in left:
            w.writerow([B.safe_cell(WHY[r.kind]), r.i + 0, *[B.safe_cell(r.raw.get(h)) for h in headers]])
    return rel


def _write_unreadable(an: Analysis, import_id: int, settings: Settings) -> str | None:
    bad = [(r, h, v) for r in an.rows for h, v in r.unreadable if r.kind in ("new", "duplicate", "hand")]
    if not bad:
        return None
    rel = f"crexi/{P.period_key(None, settings)}/unreadable-contacts-{import_id}.csv"
    path = B.output_dir(settings) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=B.CSV_ENCODING) as f:
        w = csv.writer(f)
        w.writerow(["File row", "Property ID", "Column", "Value that could not be read"])
        for r, h, v in bad:
            w.writerow([r.i, r.pid or "", B.safe_cell(h), B.safe_cell(v)])
    return rel


def _merge_facts(conn: sqlite3.Connection, an: Analysis, table: ing.Table, column_map: dict[str, str],
                 file_hash: str, warnings: list[str], settings: Settings, skip: set[str] | None = None) -> dict:
    """Property facts (owner, mailing address, sizes...) of every matched row, merged straight into the property
    the row was matched to (by link, parcel number or address), with the keep-the-original rule of the normal
    Crexi import (fill blanks, keep a different value as "Units 2", "Owner 2"..., tagged with the batch). One
    import record per market. Never adds a property and never matches the row a second time. `skip`: the phone,
    email and contact-name columns; they are saved as contacts, so they are left out of the property's original
    columns (the exports number contacts themselves: Phone 1..N, Email 1..N)."""
    skip = skip or set()
    by_market: dict[int, list[Row]] = defaultdict(list)
    for r in an.rows:
        if r.kind in ("new", "duplicate", "hand") and not r.not_matched and r.pid is not None:
            by_market[an.ix.props[r.pid]["market_id"]].append(r)
    markets = {m["id"]: m for m in rows(conn, "SELECT id, slug, asset_class FROM markets")}
    out: dict[str, Any] = {"markets": {}, "new_rows": 0}
    for mid, rs in by_market.items():
        m = markets[mid]
        opts = ing._options(settings, m["asset_class"], "crexi")
        edits = latest_edits(conn, mid, sorted({r.pid for r in rs if r.pid is not None}))
        report, kept = M.Report(), Counter()
        updated: set[int] = set()
        bad = 0
        now = now_iso()
        with conn:
            import_id = ing._start_import(conn, mid, f"Crexi reply: {table.filename}", file_hash, column_map)
            for r in sorted(rs, key=lambda x: (x.kind == "duplicate", x.i)):
                existing = one(conn, "SELECT * FROM properties WHERE id=?", (r.pid,))
                if existing is None:
                    continue
                try:
                    p = ing.parse_row({h: v for h, v in r.raw.items() if h not in skip}, column_map, opts)
                except ValueError:
                    bad += 1
                    continue
                ing._update(conn, existing, p, import_id, now, opts, edits.get(r.pid), kept, report)
                updated.add(int(r.pid))
                if r.batch_id:
                    # tag the extra values this reply kept (Units 2, Owner 2...) with the batch they came back in
                    conn.execute("UPDATE property_alternates SET batch_id=? WHERE property_id=? AND import_id=? "
                                 "AND batch_id IS NULL", (r.batch_id, r.pid, import_id))
            conn.execute("UPDATE imports SET rows_total=?, rows_new=0, rows_updated=?, rows_skipped=? WHERE id=?",
                         (len(rs), len(updated), bad, import_id))
        out["markets"][m["slug"]] = {"import_id": import_id, "rows_new": 0, "rows_updated": len(updated),
                                     "enrichment": report.to_dict()}
        if bad:
            warnings.append(f"{bad:,} row(s) in {m['slug']} could not be read, so their property facts were not "
                            f"merged. Their contacts were still added.")
    return out


def _apply_item(conn: sqlite3.Connection, an: Analysis, key: tuple[int, int], row: Row, batch: dict, import_id: int,
                dnc: set, cfg: E.DrillConfig, totals: dict) -> None:
    """One returned property: contacts, the ledger row, the item, the DRILL check-off. One transaction."""
    bid, pid = key
    stamp = now_iso()
    # Claim the item first: if another import already recorded it, stop here so no credit is counted twice.
    cur = conn.execute("UPDATE crexi_batch_items SET status='returned', returned_at=?, import_id=? "
                       "WHERE batch_id=? AND property_id=? AND status IN ('sent', 'not_returned')",
                       (stamp, import_id, bid, pid))
    if cur.rowcount != 1:
        return
    note = f"Crexi credit, batch {bid}"
    group = [row, *an.extras.get(key, [])]
    phones: dict[str, str] = {}
    emails: dict[str, str] = {}
    created = {"phone": 0, "email": 0, "name": 0}
    for r in group:
        person_id = None
        if r.contact_name:
            person_id, was_new = E._add_person(conn, pid, r.contact_name, role="other", source="crexi", notes=note)
            created["name"] += int(was_new)
        for disp, k in r.phones:
            phones.setdefault(k, disp)
            c, new = E._add_contact(conn, pid, "phone", disp, source="crexi", person_id=person_id, notes=note,
                                    dnc_keys=dnc)
            created["phone"] += int(new)
            totals["dnc"] += int(new and c.get("status") == "dnc")
        for disp, k in r.emails:
            emails.setdefault(k, disp)
            c, new = E._add_contact(conn, pid, "email", disp, source="crexi", person_id=person_id, notes=note,
                                    dnc_keys=dnc)
            created["email"] += int(new)
            totals["dnc"] += int(new and c.get("status") == "dnc")
    result = "found" if (phones or emails) else ("owner_only" if any(r.contact_name or r.owner_name for r in group)
                                                 else "nothing")
    led = one(conn, "SELECT id, result FROM credit_ledger WHERE property_id=? AND batch_id=? ORDER BY id DESC LIMIT 1",
              (pid, bid))
    if led is None:
        # a credit marked used by hand while the property waited at Crexi is this same credit: adopt it
        hand = one(conn, "SELECT id FROM credit_ledger WHERE property_id=? AND batch_id IS NULL AND result IS NULL "
                         "ORDER BY id DESC LIMIT 1", (pid,))
        if hand:
            conn.execute("UPDATE credit_ledger SET batch_id=?, period=?, month=? WHERE id=?",
                         (bid, batch["period"], batch["period"][:7], hand["id"]))
        else:
            conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at, period, batch_id) "
                         "VALUES(?,?,?,?,?,?)", (pid, batch["period"][:7], None, stamp, batch["period"], bid))
    elif led["result"] is not None:
        conn.execute("UPDATE credit_ledger SET result=NULL WHERE id=?", (led["id"],))   # refilled just below
    conn.execute("UPDATE crexi_batch_items SET result=?, phones=?, emails=?, not_matched_at=NULL "
                 "WHERE batch_id=? AND property_id=?", (result, len(phones), len(emails), bid, pid))
    # The Crexi credit stage's goal is "reach" (a phone or email). A name-only row still spent the credit (the item
    # is returned), but for the DRILL and the hit rate it found no way to reach the owner: 'nothing'.
    outcome = "found" if result == "found" else "nothing"
    if result == "found":
        detail = (f"Crexi batch #{bid}: found {len(phones)} phone{'s' if len(phones) != 1 else ''} and "
                  f"{len(emails)} email{'s' if len(emails) != 1 else ''} (untested).")
    elif result == "owner_only":
        detail = f"Crexi batch #{bid}: matched, owner or contact name only; no phone or email."
    else:
        detail = f"Crexi batch #{bid}: matched, but Crexi had no owner or contact information."
    if E.CREDIT_STAGE in cfg.by_key:
        E.record_attempt(conn, pid, E.CREDIT_STAGE, outcome, detail=detail, actor="you")   # fills the ledger row
    else:
        conn.execute("UPDATE credit_ledger SET result=? WHERE property_id=? AND batch_id=? AND result IS NULL",
                     (outcome, pid, bid))
        conn.commit()
    totals[result] += 1
    totals["phones"] += created["phone"]
    totals["emails"] += created["email"]
    totals["names"] += created["name"]


def _make_hand_batch(conn: sqlite3.Connection, an: Analysis, actor: str, settings: Settings) -> int | None:
    pids = [r.pid for r in an.rows if r.kind == "hand" and r.pid is not None]
    pids = list(dict.fromkeys(pids))
    if not pids:
        return None
    stamp = now_iso()
    slugs = sorted({m["slug"] for m in rows(conn, "SELECT slug FROM markets WHERE id IN (%s)" % ",".join(
        str(an.ix.props[p]["market_id"]) for p in pids))})
    cur = conn.execute(
        "INSERT INTO crexi_batches(market_id, markets, period, kind, requested, created_at, created_by, status, note) "
        "VALUES(NULL, ?, ?, 'manual', ?, ?, ?, 'open', 'Recorded from a Crexi reply: not exported by this app.')",
        (dumps(slugs), P.period_key(None, settings), len(pids), stamp, (actor or "cli")[:320]))
    bid = int(cur.lastrowid)
    conn.executemany("INSERT INTO crexi_batch_items(batch_id, property_id, position, sent_at, status) "
                     "VALUES(?,?,?,?, 'sent')", [(bid, p, i, stamp) for i, p in enumerate(pids, start=1)])
    conn.commit()
    return bid


def rank_after(conn: sqlite3.Connection, result: dict) -> dict | None:
    """The free steps after an import (owners, score, DRILL) for the markets whose facts the reply changed, so an
    owner Crexi filled in gets its owner type, portfolio and score at once. Used by the web route and the CLI.
    A ranking failure never undoes the import: it says what to run instead."""
    slugs = list((result.get("merge") or {}).get("markets") or {})
    if not result.get("changed") or not slugs:
        return None
    try:
        from .. import pipeline
        return {slug: pipeline.after_import(conn, slug).get("message") for slug in slugs}
    except Exception:
        log.exception("Ranking after a Crexi reply failed")
        return {"error": "Imported, but ranking it failed. Run Owners, Score and DRILL from the Run screen."}


_import_lock = threading.Lock()   # one reply import at a time in this program (web requests run in threads)


def apply_reply(conn: sqlite3.Connection, path: Path, filename: str | None = None, *, columns: dict | None = None,
                adopt_hand: bool = False, actor: str = "cli", settings: Settings | None = None) -> dict:
    """Import Crexi's reply: mark matched rows returned (a credit spent each), add the contacts, merge the property
    facts, then finish every batch the file touches (the rest of its rows are missing: no credit, the DRILL moves
    on), write the unmatched and missing files and the history row. Safe to run twice: rows already recorded are
    skipped, so nothing is added the second time. Two imports at once run one after the other, and each item is
    claimed only if it is still waiting, so no row is ever charged twice."""
    with _import_lock:
        return _apply_reply(conn, path, filename, columns=columns, adopt_hand=adopt_hand, actor=actor,
                            settings=settings)


def _apply_reply(conn: sqlite3.Connection, path: Path, filename: str | None, *, columns: dict | None,
                 adopt_hand: bool, actor: str, settings: Settings | None) -> dict:
    s = settings or get_settings()
    table = read_reply(path)
    column_map, _ = map_columns(table.headers, ing._merged_overrides(s, None))
    overrides = _clean_columns(columns)
    cols = detect_columns(table.headers, table.rows, overrides, skip=set(column_map.values()))
    seen_at = first_imported(conn, path)
    an = analyze(conn, table, column_map, cols, adopt_hand=adopt_hand, first_seen=seen_at)
    warnings = list(cols["notes"])
    if adopt_hand and any(r.kind == "hand" for r in an.rows):
        _make_hand_batch(conn, an, actor, s)
        an = analyze(conn, table, column_map, cols, first_seen=seen_at)
    plan = plan_enrichment(conn, an, table, column_map, s)
    summ = _summary(an, conn)
    changes = bool(an.claimed or an.flagged)
    c = summ["counts"]
    if not changes and not c.get("duplicate"):
        return {"ok": True, "changed": False, "import_id": None, "summary": summ, "enrichment": plan.to_dict(),
                "enrichment_text": plan.text(), "warnings": warnings,
                "message": "Nothing new: every row in this file was already recorded (or matches none of your "
                           "properties). Nothing was added."}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    batch_ids = sorted({k[0] for k in an.claimed} | {k[0] for k in an.flagged})
    stamp = now_iso()
    cur = conn.execute("INSERT INTO crexi_batch_imports(batch_ids, file_name, file_hash, rows_total, columns_json, "
                       "created_at, created_by) VALUES(?,?,?,?,?,?,?)",
                       (dumps(batch_ids), filename or path.name, digest, len(an.rows),
                        dumps({k: cols[k] for k in ("phone", "email", "name")}), stamp, (actor or "cli")[:320]))
    import_id = int(cur.lastrowid)
    conn.commit()
    saved = f"crexi/{P.period_key(None, s)}/reply-{import_id}-{re.sub(r'[^A-Za-z0-9._-]+', '_', Path(filename or path.name).stem)[:60]}{path.suffix.lower()}"
    dest = B.output_dir(s) / saved
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dest)

    merged = _merge_facts(conn, an, table, column_map, digest, warnings, s,
                          skip={*cols["phone"], *cols["email"], *([cols["name"]] if cols["name"] else [])})
    cfg = E.drill_config(s)
    dnc = E._dnc_keys(conn)
    totals: dict[str, int] = defaultdict(int)
    batches_cache = {bid: B.get_batch(conn, bid) for bid in batch_ids}
    for key, row in sorted(an.claimed.items(), key=lambda kv: kv[1].i):
        _apply_item(conn, an, key, row, batches_cache[key[0]], import_id, dnc, cfg, totals)
    for key, row in an.flagged.items():
        conn.execute("UPDATE crexi_batch_items SET not_matched_at=? WHERE batch_id=? AND property_id=? "
                     "AND status IN ('sent', 'not_returned')", (stamp, key[0], key[1]))
    # Every batch this file touches is finished now: what did not come back matched is missing (no credit; the
    # DRILL moves on). A batch with no row in the file is not touched.
    finished = B.finish_batches(conn, batch_ids, settings=s, actor="auto")
    for bid in batch_ids:
        if overrides:
            conn.execute("UPDATE crexi_batches SET column_overrides=? WHERE id=?", (dumps(overrides), bid))
    conn.commit()

    unmatched = _write_unmatched(conn, an, table.headers, import_id, s)
    unreadable = _write_unreadable(an, import_id, s)
    missing_file = B.write_missing(conn, batch_ids, f"crexi/{P.period_key(None, s)}/missing-{import_id}.csv", s)
    bad = sum(len(r.unreadable) for r in an.rows)
    rep = plan.to_dict()
    rep["rows_read"] = len(an.rows)
    per_batch = [{"batch_id": bid, **{k: c2[k] for k in ("sent", "returned", "missing", "found", "owner_only",
                                                           "no_data", "matched_pct")}}
                 for bid, c2 in sorted(finished.items())]
    t = {k: sum(b[k] for b in per_batch) for k in ("sent", "returned", "missing")}
    t["matched_pct"] = round(t["returned"] / t["sent"], 4) if t["sent"] else None
    headline = B.returned_text({**t, "waiting": 0}) if per_batch else ""
    summary = {"enrichment": rep, "enrichment_text": M.report_text(rep), "batches": summ["batches"],
               "counts": summ["counts"], "unmatched_file": unmatched, "unreadable_file": unreadable,
               "missing_file": missing_file, "finished": per_batch, "totals": t, "headline": headline,
               "saved_as": saved, "merge": merged, "dnc_marked": totals["dnc"]}
    conn.execute("UPDATE crexi_batch_imports SET saved_as=?, returned=?, found=?, owner_only=?, no_data=?, "
                 "not_matched=?, duplicates=?, unmatched=?, contacts_added=?, dnc_marked=?, unreadable=?, "
                 "summary_json=? WHERE id=?",
                 (saved, len(an.claimed), totals["found"], totals["owner_only"], totals["nothing"], len(an.flagged),
                  c.get("duplicate", 0), c.get("none", 0) + c.get("no_batch", 0),
                  totals["phones"] + totals["emails"], totals["dnc"], bad, dumps(summary), import_id))
    conn.commit()
    status = drill_credits.credit_status(conn, settings=s)
    after = B.topup_suggestion(conn, status)
    credits = B.credits_left_text(status) + (f" Export the next {status['left']:,}." if status["left"] else "")
    missing_text = (f"The {t['missing']:,} missing go to the DRILL's other sources (county records, AI research...)."
                    if t["missing"] else "")
    text = " ".join(x for x in (
        headline,
        f"Of the {len(an.claimed):,} returned now: {totals['found']:,} with a phone or email, "
        f"{totals['owner_only']:,} with a name only, {totals['nothing']:,} with nothing." if an.claimed else "",
        missing_text, credits) if x)
    return {"ok": True, "changed": True, "import_id": import_id, "batch_ids": batch_ids, "summary": summ,
            "headline": headline, "totals": t, "finished": per_batch, "missing": t["missing"],
            "missing_file": missing_file, "missing_text": missing_text, "credits_text": credits,
            "returned": len(an.claimed), "found": totals["found"], "owner_only": totals["owner_only"],
            "no_data": totals["nothing"], "not_matched": len(an.flagged), "duplicates": c.get("duplicate", 0),
            "unmatched": c.get("none", 0) + c.get("no_batch", 0), "contacts_added": totals["phones"] + totals["emails"],
            "new_phones": totals["phones"], "new_emails": totals["emails"], "dnc_marked": totals["dnc"],
            "enrichment": rep, "enrichment_text": M.report_text(rep), "unmatched_file": unmatched,
            "unreadable_file": unreadable, "saved_as": saved, "merge": merged, "warnings": warnings,
            "status": status, "topup": after, "message": text}
