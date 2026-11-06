"""Add a property by hand: an address, a Crexi link, or both.

- A Crexi link is saved as the source only. Nothing is fetched from it, and the new row says so (no owner, no
  unit count, no photos until the normal steps or a Crexi import fill them in).
- The new lead is checked against the properties already in the market (Crexi link, parcel-style address key,
  street address). A sure match is never added twice: it is reported as a duplicate, and only blanks (the link)
  are filled in. An unsure match (same street address, but the city or ZIP differs or is missing) is shown and
  waits for you to say "same property" or "new property". Nothing is merged without that answer, and a value
  that is already on the property is never overwritten.
- lead_events keeps what happened: a new listing, a duplicate seen, or a match you resolved. A new offer
  version is a different thing and lives in offers.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .crexi import batches as crexi_batches
from .db import dumps, loads, now_iso, one, rows
from .ingest.crexi import PropertyRecord, address_key, final_flags, source_id_from_link
from .util import clean_str, normalize_address, normalize_zip

MAX_ADDRESS = 200
MAX_CITY = 100
MAX_LINK = 500
CERTAIN, UNSURE = "same", "maybe"


def _text(value: Any, what: str, limit: int) -> str | None:
    text = clean_str(value)
    if text and len(text) > limit:
        raise ValueError(f"The {what} is longer than {limit} characters.")
    return text


def _link(value: Any) -> str | None:
    text = _text(value, "link", MAX_LINK)
    if not text:
        return None
    parts = urlsplit(text if "://" in text else f"https://{text}")
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (host == "crexi.com" or host.endswith(".crexi.com")):
        raise ValueError("That doesn't look like a Crexi link. Paste the address from the Crexi page, or type the "
                         "property's street address instead.")
    return text if "://" in text else f"https://{text}"


_UNIT_TAIL = re.compile(r"^(?P<street>.*?\S)[\s,]+(?:(?:apt|apartment|unit|suite|ste)\b\.?\s*#?|#)\s*(?P<unit>[A-Za-z0-9-]{1,10})$",
                        re.IGNORECASE)


def split_unit(address: str) -> tuple[str, str | None]:
    """'100 Main St Apt B' -> ('100 Main St', 'B'), the way Crexi rows keep the unit apart from the street."""
    m = _UNIT_TAIL.match(address.strip())
    if m and re.match(r"\d", m.group("street")):
        return m.group("street"), m.group("unit").upper()
    return address, None


def _unit_norm(unit: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(clean_str(unit) or "").upper())


def parse(fields: Mapping[str, Any]) -> dict:
    """The lead as the properties table keeps it, or a ValueError in plain English."""
    address = _text(fields.get("address"), "address", MAX_ADDRESS)
    link = _link(fields.get("link"))
    if not address and not link:
        raise ValueError("Type a street address or paste a Crexi link.")
    state = _text(fields.get("state"), "state", 20)
    if state and not re.fullmatch(r"[A-Za-z]{2}", state):
        raise ValueError("Use the two-letter state, like OH.")
    zip_ = _text(fields.get("zip"), "ZIP", 10)
    if zip_ and not re.fullmatch(r"\d{5}(-\d{4})?", zip_):
        raise ValueError("Use a five-digit ZIP, like 45502.")
    zip_ = normalize_zip(zip_)
    city = _text(fields.get("city"), "city", MAX_CITY)
    source_id = source_id_from_link(link)
    unit = None
    if address:
        address, unit = split_unit(address)
    lead = {"address": address, "unit": unit, "city": city, "state": state.upper() if state else None, "zip": zip_,
            "crexi_url": link, "source": "crexi" if link else "manual", "source_id": source_id}
    lead["property_key"] = (address_key(address, unit, city, lead["state"], zip_) if address
                            else f"src:crexi:{source_id or link.lower()}")
    return lead


def _same_street(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    street = normalize_address(a.get("address"))
    return bool(crexi_batches.has_house_number(street) and street == normalize_address(b.get("address")))


def _match_why(lead: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[str, str] | None:
    if lead["property_key"] == row["property_key"]:
        return CERTAIN, "same street address, city and ZIP"
    link = crexi_batches.link_key(lead["crexi_url"])
    if link and link == crexi_batches.link_key(row["crexi_url"]):
        return CERTAIN, "same Crexi link"
    same_unit = _unit_norm(lead.get("unit")) == _unit_norm(row.get("unit"))
    key = crexi_batches.address_key(lead)
    if key and key == crexi_batches.address_key(row):
        if not same_unit:
            return UNSURE, "same street address, city and ZIP, but the unit is different or missing"
        return CERTAIN, "same street address, city and ZIP"
    if _same_street(lead, row):
        return UNSURE, "same street address, but the city or ZIP is different or missing"
    return None


def find_matches(conn: sqlite3.Connection, market_id: int, lead: Mapping[str, Any]) -> list[dict]:
    """Properties of this market that may be the same as the lead, sure matches first."""
    found = []
    for r in rows(conn, "SELECT id, property_key, address, unit, city, state, zip, crexi_url FROM properties "
                        "WHERE market_id = ? ORDER BY id", (market_id,)):
        why = _match_why(lead, r)
        if why:
            found.append({"property_id": r["id"], "certainty": why[0], "why": why[1], "address": r["address"],
                          "city": r["city"], "state": r["state"], "zip": r["zip"], "crexi_url": r["crexi_url"]})
    return sorted(found, key=lambda m: (m["certainty"] != CERTAIN, m["property_id"]))


def _event(conn: sqlite3.Connection, pid: int, kind: str, actor: str, **detail: Any) -> None:
    conn.execute("INSERT INTO lead_events(property_id, kind, detail_json, actor, at) VALUES(?,?,?,?,?)",
                 (pid, kind, dumps(detail), actor, now_iso()))


def _same_property(conn: sqlite3.Connection, pid: int, lead: Mapping[str, Any], kind: str, actor: str) -> dict:
    """The lead is a property we already have. Fill the Crexi link only when the property has none."""
    row = one(conn, "SELECT crexi_url, source, source_id FROM properties WHERE id = ?", (pid,))
    link = lead["crexi_url"]
    linked = bool(link and not row["crexi_url"])
    if linked:
        conn.execute("UPDATE properties SET crexi_url = ?, source_id = COALESCE(source_id, ?), updated_at = ? "
                     "WHERE id = ?", (link, lead["source_id"], now_iso(), pid))
    _event(conn, pid, kind, actor, link=link, link_saved=linked, address=lead["address"])
    conn.commit()
    note = (" Its Crexi link was saved." if linked else
            " It already has a different Crexi link, so the new one was not saved."
            if link and crexi_batches.link_key(link) != crexi_batches.link_key(row["crexi_url"]) else "")
    return {"outcome": "duplicate", "property_id": pid, "linked": linked,
            "message": "That property is already on the list, so nothing was added." + note}


def add(conn: sqlite3.Connection, market: Mapping[str, Any], fields: Mapping[str, Any], actor: str, *,
        same_as: int | None = None, as_new: bool = False) -> dict:
    """Add a lead. Returns {outcome: created | duplicate | needs_resolution, property_id, matches, message}.
    same_as: the id of an unsure match you said is the same property. as_new: you said the unsure matches are not."""
    lead = parse(fields)
    matches = find_matches(conn, market["id"], lead)
    sure = [m for m in matches if m["certainty"] == CERTAIN]
    if sure:
        return _same_property(conn, sure[0]["property_id"], lead, "duplicate", actor)
    if same_as is not None:
        if same_as not in {m["property_id"] for m in matches}:
            raise ValueError("That property is not one of the matches that were shown.")
        return _same_property(conn, same_as, lead, "same_property", actor)
    if matches and not as_new:
        return {"outcome": "needs_resolution", "matches": matches, "property_id": None,
                "message": "This may be a property you already have. Say whether it is the same one."}
    record = PropertyRecord(**{k: lead[k] for k in ("address", "city", "state", "zip", "crexi_url", "source",
                                                   "source_id")})
    flags = sorted({*final_flags(record, (), False, False), "manual_lead"})
    stamp = now_iso()
    try:
        cur = conn.execute(
            "INSERT INTO properties(market_id, property_key, source, source_id, crexi_url, address, unit, city, state, "
            "zip, raw_json, flags_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (market["id"], lead["property_key"], lead["source"], lead["source_id"], lead["crexi_url"], lead["address"],
             lead["unit"], lead["city"], lead["state"], lead["zip"], "{}", dumps(flags), stamp, stamp))
    except sqlite3.IntegrityError:      # another click or program added the same key a moment ago
        conn.rollback()
        row = one(conn, "SELECT id FROM properties WHERE market_id = ? AND property_key = ?",
                  (market["id"], lead["property_key"]))
        return _same_property(conn, row["id"], lead, "duplicate", actor)
    pid = int(cur.lastrowid)
    _event(conn, pid, "new_listing", actor, link=lead["crexi_url"], address=lead["address"],
           fetched=False, after_unsure_match=bool(matches))
    conn.commit()
    return {"outcome": "created", "property_id": pid, "matches": [],
            "message": "Property added. " + ("The Crexi link is saved as its source; nothing was fetched from it, so "
                                             "the details are blank until a Crexi import or the other steps fill them."
                                             if lead["crexi_url"] else "Its details are blank until the other steps fill them.")}


def events(conn: sqlite3.Connection, pid: int) -> list[dict]:
    """How this property got on the list by hand, newest first (empty for an imported property)."""
    return [{"kind": r["kind"], "detail": loads(r["detail_json"], {}), "actor": r["actor"], "at": r["at"]}
            for r in rows(conn, "SELECT kind, detail_json, actor, at FROM lead_events WHERE property_id = ? "
                                "ORDER BY id DESC", (pid,))]
