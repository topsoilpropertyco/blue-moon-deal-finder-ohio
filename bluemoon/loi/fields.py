"""Template tokens and what fills them.

A master document holds tokens like {{seller_name}}. config/loi.yaml says what each token is filled with (a
"source" below). Seller and recipient are separate sources: the seller is the owner of record, the recipient is
whoever the offer is addressed to (it can be a broker). Internal figures (scores, screening, the last sale price,
notes) are not sources at all, so no token can pull them in. Money is whole cents until the moment it is written.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from datetime import date
from typing import Any

from .. import pricing
from ..db import loads, one
from ..util import clean_str

TOKEN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
ANY_TOKEN = re.compile(r"\{\{[^{}]*\}\}")      # anything that looks like a token, even one we can't read
MAX_VALUE = 500

SOURCES: dict[str, str] = {
    "seller_name": "Seller: owner of record",
    "seller_mailing_address": "Seller: mailing address",
    "recipient_name": "Recipient: who the offer is addressed to",
    "recipient_contact": "Recipient: email or phone chosen for the offer",
    "property_address": "Property: street address",
    "property_city": "Property: city",
    "property_state": "Property: state",
    "property_zip": "Property: ZIP",
    "property_county": "Property: county",
    "property_apn": "Property: parcel number (APN)",
    "property_full_address": "Property: full address",
    "property_units": "Property: number of units",
    "offer_price": "Offer: price",
    "offer_date": "Offer: date",
    "offer_version": "Offer: version number",
    "annual_rent": "Yearly rent the price is based on",
}

# state of one token in the field review
FILLED, EMPTY, UNMAPPED, UNUSED = "filled", "empty", "unmapped", "unused"
STATE_TEXT = {
    FILLED: "Filled", EMPTY: "No value yet", UNMAPPED: "Not mapped in config/loi.yaml",
    UNUSED: "Mapped, but not in the template",
}


def load_map(raw: Mapping[str, Any] | None) -> dict[str, str]:
    """config/loi.yaml field_map, checked: every token is a plain name and every source is one the app knows."""
    out: dict[str, str] = {}
    for token, source in ((raw or {}).get("field_map") or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", str(token)):
            raise ValueError(f"config/loi.yaml: '{token}' can't be a token name (letters, numbers, _ . - only).")
        if source not in SOURCES:
            raise ValueError(f"config/loi.yaml: '{source}' (for {{{{{token}}}}}) is not something a token can be "
                             f"filled with. Choose from: {', '.join(SOURCES)}.")
        out[str(token)] = str(source)
    return out


def find_tokens(text: str) -> dict[str, list[str]]:
    """{token name: the ways it is written in the text, e.g. '{{seller_name}}'} in the order first seen."""
    found: dict[str, list[str]] = {}
    for m in TOKEN.finditer(text):
        spellings = found.setdefault(m.group(1), [])
        if m.group(0) not in spellings:
            spellings.append(m.group(0))
    return found


def leftovers(text: str) -> list[str]:
    """Anything token-shaped still in a filled copy (what the fill missed)."""
    return sorted(set(ANY_TOKEN.findall(text)))


def _text(value: Any) -> str | None:
    s = " ".join((clean_str(value) or "").split())
    return s[:MAX_VALUE] or None


def _street(street: Any, unit: Any) -> str | None:
    return _text(" ".join(p for p in (clean_str(street), clean_str(unit)) if p))


def _place(city: Any, state: Any, zip_: Any) -> str | None:
    tail = " ".join(p for p in (clean_str(state), clean_str(zip_)) if p)
    return _text(", ".join(p for p in (clean_str(city), tail) if p))


def _join(*parts: str | None) -> str | None:
    return ", ".join(p for p in parts if p) or None


def long_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def values(conn: sqlite3.Connection, prop: Mapping[str, Any], offer: Mapping[str, Any], today: date) -> dict[str, str | None]:
    """Every source's text for this offer (None when there is nothing to write)."""
    contact = one(conn, "SELECT value FROM contact_points WHERE id=?", (offer["recipient_contact_id"],)) \
        if offer["recipient_contact_id"] else None
    annual = (loads(offer["snapshot_json"], {}) or {}).get("annual_cents")
    street = _street(prop.get("address"), prop.get("unit"))
    city, state, zip_ = (_text(prop.get(k)) for k in ("city", "state", "zip"))
    return {
        "seller_name": _text(offer["seller_name"]),
        "seller_mailing_address": _join(_street(prop.get("mailing_address"), prop.get("mailing_unit")),
                                        _place(prop.get("mailing_city"), prop.get("mailing_state"),
                                               prop.get("mailing_zip"))),
        "recipient_name": _text(offer["recipient_name"]),
        "recipient_contact": _text(contact["value"]) if contact else None,
        "property_address": street, "property_city": city, "property_state": state, "property_zip": zip_,
        "property_county": _text(prop.get("county")), "property_apn": _text(prop.get("apn")),
        "property_full_address": _join(street, _place(city, state, zip_)),
        "property_units": None if prop.get("units") in (None, "") else str(int(prop["units"])),
        "offer_price": pricing.usd(offer["price_cents"]), "offer_date": long_date(today),
        "offer_version": str(offer["version"]),
        "annual_rent": None if annual is None else pricing.usd(annual),
    }


def review(field_map: Mapping[str, str], filled: Mapping[str, str | None],
           in_template: list[str] | None = None) -> list[dict]:
    """One row per token: its source, the text that would go in, and whether that is enough. With the template's
    own tokens (in_template) the rows are that list, with the ones the map doesn't know marked unmapped; without
    them, the rows are the mapped tokens only."""
    names = list(in_template) if in_template is not None else list(field_map)
    out = [_row(n, field_map.get(n), filled) for n in names]
    if in_template is not None:
        out += [{**_row(n, s, filled), "state": UNUSED, "state_text": STATE_TEXT[UNUSED]}
                for n, s in field_map.items() if n not in in_template]
    return out


def _row(token: str, source: str | None, filled: Mapping[str, str | None]) -> dict:
    value = filled.get(source) if source else None
    state = UNMAPPED if source is None else FILLED if value else EMPTY
    return {"token": token, "source": source, "label": SOURCES.get(source or ""), "value": value, "state": state,
            "state_text": STATE_TEXT[state]}


def unresolved(rows: list[dict]) -> list[dict]:
    """The rows that stop a letter being made: a token with no mapping or no value."""
    return [r for r in rows if r["state"] in (EMPTY, UNMAPPED)]
