"""The people behind a property, for the drawer's header block.

The seller is the owner of record on the property (usually a company). The people are the humans tied to it,
each with a role and where that came from. A person is only "confirmed" when a conversation confirmed them;
a company with nobody confirmed says so. Who an offer is addressed to (the recipient) is chosen by hand and
stored apart from the seller: it can be a broker, a manager or anyone else on the list.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from .config import Settings
from .crexi import merge as crexi_merge
from .db import now_iso, one, rows
from .drill import links as drill_links
from .drill.engine import SETTABLE_ROLES, drill_config
from .util import clean_str

MAX_NOTE = 500
# Order is the order people are listed in: owners first, then the people around them.
ROLE_LABELS = {
    "confirmed_owner": "Owner (confirmed)", "possible_owner": "Possible owner", "owner_of_record": "Owner of record",
    "portfolio_owner": "Owner (same-owner match)", "member": "Member", "officer": "Officer", "trustee": "Trustee",
    "deed_signer": "Signed the deed", "heir": "Heir", "manager": "Manager", "broker": "Broker",
    "statutory_agent": "Registered contact (statutory agent)", "agent": "Agent", "other": "Unconfirmed association",
    "previous_owner": "Previous owner",
}
HIDDEN_ROLES = ("rejected",)      # names marked wrong
NO_RECIPIENT_STATUSES = ("wrong", "dnc")
SOURCE_LABELS = {"manual": "you", "you": "you", "conversation": "a conversation", "crexi": "Crexi"}


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(role or "other") or str(role).replace("_", " ").capitalize()


def _rank(person: Mapping[str, Any]) -> tuple[int, int]:
    order = list(ROLE_LABELS)
    role = person.get("role") or "other"
    return (order.index(role) if role in order else len(order), int(person["id"]))


def _contact_view(c: Mapping[str, Any]) -> dict:
    return {"id": c["id"], "channel": c["channel"], "value": c["value"], "status": c["status"],
            "can_receive": c["status"] not in NO_RECIPIENT_STATUSES}


def block(conn: sqlite3.Connection, prop: Mapping[str, Any], settings: Settings | None = None) -> dict:
    """{property, seller, confirmed, people, loose_contacts, recipient, conflicts, role_choices}."""
    pid = int(prop["id"])
    stages = {k: s.name for k, s in drill_config(settings).by_key.items()}
    contacts = rows(conn, "SELECT * FROM contact_points WHERE property_id=? ORDER BY id", (pid,))
    chosen = one(conn, "SELECT * FROM property_recipients WHERE property_id=?", (pid,))
    state = one(conn, "SELECT confirmed_person, confirmed_at FROM drill_state WHERE property_id=?", (pid,)) or {}
    people = []
    for p in sorted((x for x in rows(conn, "SELECT * FROM people WHERE property_id=?", (pid,))
                     if x["role"] not in HIDDEN_ROLES), key=_rank):
        src = p.get("source")
        people.append({
            "id": p["id"], "name": p["name"], "role": p["role"] or "other", "role_label": role_label(p["role"]),
            "can_change_role": p["role"] in (None, *SETTABLE_ROLES), "notes": p.get("notes"),
            "source": src, "source_label": stages.get(src) or SOURCE_LABELS.get(src) or (src or "").replace("_", " ") or None,
            "edited_by": p.get("edited_by"), "edited_at": p.get("edited_at"),
            "is_recipient": bool(chosen and chosen["person_id"] == p["id"]),
            "contacts": [_contact_view(c) for c in contacts if c["person_id"] == p["id"]],
        })
    known = {p["id"] for p in people}
    confirmed = [p["name"] for p in people if p["role"] == "confirmed_owner"]
    if state.get("confirmed_person") and state["confirmed_person"] not in confirmed:
        confirmed.append(state["confirmed_person"])
    owner = clean_str(prop.get("owner_name"))
    return {
        "property": {"id": pid, "address": prop.get("address"), "city": prop.get("city")},
        "seller": {"name": owner, "is_entity": drill_links.looks_like_entity(owner)},
        "confirmed": confirmed,
        "people": people,
        "loose_contacts": [_contact_view(c) for c in contacts if c["person_id"] not in known],
        "recipient": _recipient_view(conn, chosen, people),
        "conflicts": {"owner_names": crexi_merge.extra_values(conn, pid, prop).get("owner_name", []),
                      "county_differs": "county_owner_differs" in (prop.get("flags_json") or "")},
        "role_choices": [[r, role_label(r)] for r in SETTABLE_ROLES],
    }


def _recipient_view(conn: sqlite3.Connection, chosen: Mapping[str, Any] | None, people: list[dict]) -> dict | None:
    if not chosen:
        return None
    person = next((p for p in people if p["id"] == chosen["person_id"]), None)
    if not person:        # the person was since marked wrong: no recipient until a new one is chosen
        return None
    contact = next((c for c in person["contacts"] if c["id"] == chosen["contact_point_id"]), None)
    return {"person_id": person["id"], "name": person["name"], "role_label": person["role_label"],
            "contact_id": contact["id"] if contact else None,
            "contact_text": f"{contact['channel']}: {contact['value']}" if contact else None,
            "note": chosen["note"], "chosen_by": chosen["chosen_by"], "chosen_at": chosen["chosen_at"]}


def choose_recipient(conn: sqlite3.Connection, property_id: int, person_id: int, contact_id: int | None,
                     note: str | None, actor: str) -> None:
    """Address offers for this property to one person (and, optionally, one of their contacts). The seller
    is not touched. Replaces any earlier choice."""
    person = one(conn, "SELECT * FROM people WHERE id=? AND property_id=?", (person_id, property_id))
    if not person or person["role"] in HIDDEN_ROLES:
        raise KeyError(f"Person {person_id} is not on property {property_id}.")
    if contact_id is not None:
        contact = one(conn, "SELECT * FROM contact_points WHERE id=? AND property_id=?", (contact_id, property_id))
        if not contact:
            raise KeyError(f"Contact {contact_id} is not on property {property_id}.")
        if contact["person_id"] != person_id:
            raise ValueError(f"That contact is not recorded for {person['name']}.")
        if contact["status"] in NO_RECIPIENT_STATUSES:
            raise ValueError("That contact is marked wrong or do-not-contact, so offers can't go there.")
    note = clean_str(note)
    if note and len(note) > MAX_NOTE:
        raise ValueError(f"The note is longer than {MAX_NOTE} characters.")
    conn.execute(
        "INSERT INTO property_recipients(property_id, person_id, contact_point_id, note, chosen_by, chosen_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(property_id) DO UPDATE SET person_id=excluded.person_id, "
        "contact_point_id=excluded.contact_point_id, note=excluded.note, chosen_by=excluded.chosen_by, "
        "chosen_at=excluded.chosen_at",
        (property_id, person_id, contact_id, note, actor, now_iso()))
    conn.commit()


def clear_recipient(conn: sqlite3.Connection, property_id: int) -> None:
    conn.execute("DELETE FROM property_recipients WHERE property_id=?", (property_id,))
    conn.commit()
