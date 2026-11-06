"""Sending an offer's letter through a transport. This build has one: the test outbox, which writes what would
have been sent to a file under data/outbox/ and emails nobody.

- A send attempt is its own record (offer_sends), never the offer's "sent" status. Only "Mark as sent" (or, later,
  a mail provider's own report) makes an offer sent, so submitting to the outbox leaves the offer unsent.
- Send-intent key: offer id + version + recipient + intent. The same key is one record, so a retry or a second
  click never submits twice; only a failed attempt is tried again. A revised version is a new offer with its own
  key, so it can be sent again.
- The state is whatever the transport reports: prepared (we built the message), queued, submitted, failed or
  delivered. The test outbox reports submitted.
- The message names the recipient and links the letter that was made. It carries no wording of its own: the app
  never writes letter or email text and there is no priced-email-first path.
- A batch reports every record on its own; one failure never stops the others.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from . import offers
from .config import Settings
from .db import dumps, now_iso, one
from .people import HIDDEN_ROLES, NO_RECIPIENT_STATUSES

INTENT = "loi"
REPORTED_STATES = ("queued", "submitted", "delivered")      # what a transport may answer with


class TransportError(Exception):
    """The transport could not take the message. The text is shown to the user."""


class Transport(Protocol):
    name: str

    def submit(self, key: str, message: Mapping[str, Any]) -> tuple[str, str | None]:
        """Take the message. Returns (state, reference such as a file name). Raises TransportError."""


class OutboxTransport:
    """The test transport: one JSON file per send-intent key, so the same key never writes a second file."""
    name = "test outbox"

    def __init__(self, folder: Path):
        self.folder = folder

    def submit(self, key: str, message: Mapping[str, Any]) -> tuple[str, str | None]:
        path = self.folder / f"{key[:16]}.json"
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(message, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            raise TransportError(f"The test outbox could not be written ({exc.strerror or exc}).") from exc
        return "submitted", path.name


def outbox_transport(settings: Settings) -> OutboxTransport:
    return OutboxTransport(settings.path("db").parent / "outbox")


def intent_key(o: Mapping[str, Any], intent: str = INTENT) -> str:
    # The letter hash (rent snapshot, price, seller, recipient) is part of the key: a corrected letter is a new send.
    parts = [o["id"], o["version"], o["recipient_name"], o["recipient_contact"], intent, offers.letter_hash(o)]
    return hashlib.sha256(dumps(parts).encode()).hexdigest()


def _check_ready(conn: sqlite3.Connection, o: Mapping[str, Any]) -> dict:
    """The letter made from the offer's current numbers (its newest one); ValueError says what is missing."""
    if o["status"] != "ready_to_send":
        raise ValueError(f"Version {o['version']} is '{offers.STATUS_LABELS[o['status']]}'. It can only be sent once it "
                         "is Ready to send.")
    if not o["recipient_contact"] or not o["recipient_contact"].startswith("email:"):
        raise ValueError("The recipient needs an email address before this can be sent by email.")
    doc = offers.current_document(conn, o)
    if not doc:
        raise ValueError("There is no letter made from this offer's current numbers. Make the LOI first.")
    # The recipient is re-read: a contact or name marked wrong or do-not-contact after the draft was made is not used.
    if o.get("recipient_contact_id"):
        c = one(conn, "SELECT status FROM contact_points WHERE id = ?", (o["recipient_contact_id"],))
        if not c or c["status"] in NO_RECIPIENT_STATUSES:
            raise ValueError("That email address was marked wrong or do-not-contact. Choose a new recipient and "
                             "refresh the offer.")
    if o.get("recipient_person_id"):
        p = one(conn, "SELECT role FROM people WHERE id = ?", (o["recipient_person_id"],))
        if not p or p["role"] in HIDDEN_ROLES:
            raise ValueError("That person was marked wrong. Choose a new recipient and refresh the offer.")
    return doc


def _result(conn: sqlite3.Connection, send_id: int, message: str, *, duplicate: bool = False) -> dict:
    s = one(conn, "SELECT * FROM offer_sends WHERE id = ?", (send_id,)) or {}
    return {"offer_id": s["offer_id"], "send_id": send_id, "state": s["state"],
            "state_label": offers.SEND_STATE_LABELS[s["state"]], "ok": s["state"] != "failed",
            "duplicate": duplicate, "message": message}


def send_offer(conn: sqlite3.Connection, offer_id: int, transport: Transport, actor: str, *, intent: str = INTENT) -> dict:
    """Submit one offer's letter. ValueError when it is not ready (nothing is recorded); a transport failure is
    recorded on the send and returned with ok False."""
    o = offers.unsent(conn, offer_id)
    doc = _check_ready(conn, o)
    key = intent_key(o, intent)
    duplicate = ("Already sent this way for this version and recipient, so nothing was submitted again.")
    previous = one(conn, "SELECT * FROM offer_sends WHERE intent_key = ?", (key,))
    if previous and previous["state"] != "failed":
        return _result(conn, previous["id"], duplicate, duplicate=True)
    stamp = now_iso()
    # Claim the key and commit the claim before submitting, so a second caller loses the claim and submits nothing.
    if previous:
        send_id = previous["id"]
        claim = conn.execute("UPDATE offer_sends SET state = 'prepared', error = NULL, updated_at = ? "
                             "WHERE id = ? AND state = 'failed'", (stamp, send_id))
        won = claim.rowcount == 1
    else:
        try:
            send_id = int(conn.execute(
                "INSERT INTO offer_sends(offer_id, intent_key, transport, state, recipient_name, recipient_contact, "
                "created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (offer_id, key, transport.name, "prepared", o["recipient_name"], o["recipient_contact"], actor, stamp,
                 stamp)).lastrowid)
            won = True
        except sqlite3.IntegrityError:
            won = False
    if not won:
        conn.rollback()
        taken = one(conn, "SELECT id FROM offer_sends WHERE intent_key = ?", (key,))
        return _result(conn, taken["id"], duplicate, duplicate=True)
    conn.commit()
    message = {"intent": intent, "offer_id": offer_id, "version": o["version"], "property_id": o["property_id"],
               "to_name": o["recipient_name"], "to": o["recipient_contact"].removeprefix("email:").strip(),
               "letter_pdf": doc["pdf_url"], "letter_doc": doc["doc_url"], "send_key": key}
    try:
        state, ref = transport.submit(key, message)
        if state not in REPORTED_STATES:
            raise TransportError(f"The transport answered with an unknown state '{state}'.")
        error = None
    except TransportError as exc:
        state, ref, error = "failed", None, str(exc)
    conn.execute("UPDATE offer_sends SET state = ?, outbox_file = ?, error = ?, updated_at = ? WHERE id = ?",
                 (state, ref, error, now_iso(), send_id))
    offers._event(conn, offer_id, "send_" + state, actor, send_id=send_id, transport=transport.name, error=error)
    conn.commit()
    text = f"{error} Nothing was sent." if error else f"{offers.SEND_STATE_LABELS[state]}. The offer is still not marked as sent."
    return _result(conn, send_id, text)


def send_many(conn: sqlite3.Connection, offer_ids: Iterable[int], transport: Transport, actor: str) -> dict:
    """Send each offer on its own. Every record gets its own line in the report; a failure never stops the rest."""
    results = []
    for oid in dict.fromkeys(offer_ids):
        try:
            results.append(send_offer(conn, oid, transport, actor))
        except (KeyError, ValueError) as exc:
            conn.rollback()
            results.append({"offer_id": oid, "send_id": None, "state": None, "state_label": "Not sent", "ok": False,
                            "duplicate": False, "message": str(exc.args[0])})
    done = sum(1 for r in results if r["ok"] and not r["duplicate"])
    dup = sum(1 for r in results if r["duplicate"])
    bad = sum(1 for r in results if not r["ok"])
    parts = [f"{done} submitted to the test outbox"] + ([f"{dup} already done"] if dup else []) \
        + ([f"{bad} failed"] if bad else [])
    return {"results": results, "text": ", ".join(parts) + ". No offer was marked as sent."}

