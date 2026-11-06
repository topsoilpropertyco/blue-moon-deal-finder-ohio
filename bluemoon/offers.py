"""Offers: one row per version of an offer for a property, what it was based on, and what happened to it.

- Price: whole cents. A new offer starts at exactly 4x the yearly rent (the Blue Moon price) and is tracked as
  "following the default" until someone types a price (then it is a "manual override" and shows its implied
  multiple). A counterprice from the seller is only recorded; it never changes the offer.
- Snapshot: the rent the offer was based on (napkin.snapshot) is frozen on the offer. A later rent change makes an
  unsent draft "stale" (a warning, and an explicit refresh); it never rewrites a sent version.
- Versions: a revised offer is a new version, allowed once the latest one has been sent. A sent version is final.
- Sent: only an explicit record (how, when, by whom) makes an offer sent. A draft, a generated letter or a failed
  send never counts. `sent_via` tells a hand-marked send from one a mail provider reported.
- Everything that happens is appended to offer_events. Responses, next actions and letters (offer_documents, made in
  Google Drive by bluemoon/loi) hang off the offer. Nothing here sends mail, writes legal wording or accepts an offer.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from . import pricing
from .db import dumps, loads, now_iso, one, rows
from .people import HIDDEN_ROLES, NO_RECIPIENT_STATUSES
from .pricing import MoneyError, PricingConfig
from .util import clean_str, parse_date

STATUSES = ("internal_draft", "loi_template_needed", "required_fields_missing", "loi_generated", "ready_for_review",
            "ready_to_send", "sent")
STATUS_LABELS = {
    "internal_draft": "Internal draft", "loi_template_needed": "LOI template needed",
    "required_fields_missing": "Required fields missing", "loi_generated": "LOI generated",
    "ready_for_review": "Ready for review", "ready_to_send": "Ready to send", "sent": "Sent",
}
NEEDS_DOCUMENT = STATUSES[3:6]          # these only make sense once a letter was made from the current snapshot
SENT_HOW = ("email", "in person", "mail")
SENT_VIA = ("manual", "provider")
CLASSES = ("serious_interest", "counter", "info_request", "rejection", "no_response", "needs_review")
CLASS_LABELS = {
    "serious_interest": "Serious interest", "counter": "Counteroffer", "info_request": "Asked for information",
    "rejection": "Rejected", "no_response": "No response", "needs_review": "Needs review",
}
AMOUNT_CLASSES = ("counter", "needs_review")     # the only categories that can carry a counter figure
FOLLOW_UP_TEXT = "Follow up: did they get the LOI?"
MAX_TEXT = 5000
MAX_NOTE = 500
MAX_PRICE_CENTS = 10**13                           # $100 billion: past this it is a typo


# --- small helpers -----------------------------------------------------------------------------------------

def _event(conn: sqlite3.Connection, offer_id: int, kind: str, actor: str, **detail: Any) -> None:
    conn.execute("INSERT INTO offer_events(offer_id, kind, detail_json, actor, at) VALUES(?,?,?,?,?)",
                 (offer_id, kind, dumps(detail), actor, now_iso()))


def _get(conn: sqlite3.Connection, offer_id: int) -> dict:
    o = one(conn, "SELECT * FROM offers WHERE id=?", (offer_id,))
    if not o:
        raise KeyError(f"Offer {offer_id} was not found.")
    return o


def unsent(conn: sqlite3.Connection, offer_id: int) -> dict:
    o = _get(conn, offer_id)
    if o["status"] == "sent":
        raise ValueError(f"Version {o['version']} was already sent, so it can't change. Prepare a revised offer instead.")
    return o


def letter_hash(o: Mapping[str, Any]) -> str:
    """What a letter is made from: the rent snapshot, the price, the seller and the recipient. A letter only counts
    for an offer while this still matches, so a new price or recipient needs a new letter."""
    parts = [o["snapshot_hash"], o["price_cents"], o["seller_name"], o["recipient_name"], o["recipient_contact"]]
    return hashlib.sha256(dumps(parts).encode()).hexdigest()


def parse_price(value: Any) -> int:
    try:
        cents = pricing.to_cents(value, "price")
    except MoneyError as exc:
        raise ValueError(str(exc)) from None
    if cents <= 0:
        raise ValueError("An offer price has to be more than $0.")
    if cents > MAX_PRICE_CENTS:
        raise ValueError("That price is too large to be right. Check the number of zeros.")
    return cents


def _day(value: Any, what: str, today: date) -> str:
    """A day (today when left blank). Never in the future: these record what already happened."""
    if value in (None, ""):
        return today.isoformat()
    d = parse_date(value)
    if d is None:
        raise ValueError(f"The {what} is not a date. Use a day like 2026-11-05.")
    if d > today:
        raise ValueError(f"The {what} can't be in the future.")
    return d.isoformat()


def _due(value: Any) -> str | None:
    if value in (None, ""):
        return None
    d = parse_date(value)
    if d is None:
        raise ValueError("The due date is not a date. Use a day like 2026-11-05.")
    return d.isoformat()


def _text(value: Any, what: str, limit: int) -> str:
    text = clean_str(value)
    if not text:
        raise ValueError(f"Write the {what}.")
    if len(text) > limit:
        raise ValueError(f"The {what} is longer than {limit} characters.")
    return text


def default_cents(snap: Mapping[str, Any]) -> int | None:
    """4x the yearly rent in this snapshot (the Blue Moon price), or None when there is no usable rent."""
    ref = next((s["cents"] for s in snap.get("scenarios") or [] if s["role"] == "reference"), None)
    return ref if ref else None      # None (unknown) and 0 (a confirmed zero rent) both give no default price


def _need_default(snap: Mapping[str, Any]) -> int:
    cents = default_cents(snap)
    if cents is None:
        raise ValueError("There is no Blue Moon price to start from because the yearly rent is unknown or $0. "
                         "Enter a rent figure first, or type the price you want.")
    return cents


def current_recipient(conn: sqlite3.Connection, property_id: int) -> dict:
    """The person (and contact) offers for this property are addressed to, as chosen in the people block."""
    r = one(conn, "SELECT r.person_id, r.contact_point_id, p.name, p.role, c.channel, c.value, c.status "
                  "FROM property_recipients r JOIN people p ON p.id = r.person_id "
                  "LEFT JOIN contact_points c ON c.id = r.contact_point_id WHERE r.property_id=?", (property_id,))
    if not r or r["role"] in HIDDEN_ROLES:
        return {}
    has_contact = r["channel"] is not None and r["status"] not in NO_RECIPIENT_STATUSES
    return {"recipient_person_id": r["person_id"], "recipient_name": r["name"],
            "recipient_contact_id": r["contact_point_id"] if has_contact else None,
            "recipient_contact": f"{r['channel']}: {r['value']}" if has_contact else None}


def _reset_letter(o: Mapping[str, Any]) -> str:
    """A draft whose inputs changed no longer has a letter that matches it, so it steps back to a plain draft."""
    return "internal_draft" if o["status"] in NEEDS_DOCUMENT else o["status"]


# --- making and changing a draft ---------------------------------------------------------------------------

def prepare(conn: sqlite3.Connection, prop: Mapping[str, Any], snap: Mapping[str, Any], actor: str, *,
            price: Any = None) -> dict:
    """A new offer (version 1, or the next version once the latest was sent). Without a price it starts at 4x the
    rent and follows that default; with one it is a manual override."""
    pid = int(prop["id"])
    latest = one(conn, "SELECT id, version, status FROM offers WHERE property_id=? AND intent='loi' "
                       "ORDER BY version DESC LIMIT 1", (pid,))
    if latest and latest["status"] != "sent":
        raise ValueError(f"Version {latest['version']} is still a draft. Change or refresh it, or mark it sent "
                         "before preparing a revised offer.")
    cents, mode = (parse_price(price), "override") if price not in (None, "") else (_need_default(snap), "default")
    version = (latest["version"] + 1) if latest else 1
    stamp = now_iso()
    try:
        cur = conn.execute(
            "INSERT INTO offers(property_id, version, status, origin, seller_name, recipient_person_id, recipient_name, "
            "recipient_contact_id, recipient_contact, price_cents, price_mode, snapshot_json, snapshot_hash, "
            "created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, version, "internal_draft", "prepared", clean_str(prop.get("owner_name")),
             *(current_recipient(conn, pid).get(k) for k in ("recipient_person_id", "recipient_name",
                                                      "recipient_contact_id", "recipient_contact")),
             cents, mode, dumps(snap), snap["hash"], actor, stamp, stamp))
    except sqlite3.IntegrityError:    # a second click or a second program prepared this version first
        conn.rollback()
        raise ValueError(f"Version {version} already exists for this property.") from None
    oid = int(cur.lastrowid)
    _event(conn, oid, "prepared", actor, version=version, price_cents=cents, price_mode=mode,
           revises=latest["id"] if latest else None)
    conn.commit()
    return _get(conn, oid)


def refresh(conn: sqlite3.Connection, offer_id: int, prop: Mapping[str, Any], snap: Mapping[str, Any],
            actor: str) -> dict:
    """Re-base an unsent draft on today's rent. A price that follows the default moves to the new 4x; a typed
    price stays. The seller and recipient are read again too."""
    o = unsent(conn, offer_id)
    cents = _need_default(snap) if o["price_mode"] == "default" else o["price_cents"]
    recipient = current_recipient(conn, o["property_id"])
    conn.execute(
        "UPDATE offers SET snapshot_json=?, snapshot_hash=?, price_cents=?, seller_name=?, recipient_person_id=?, "
        "recipient_name=?, recipient_contact_id=?, recipient_contact=?, status=?, updated_at=? WHERE id=?",
        (dumps(snap), snap["hash"], cents, clean_str(prop.get("owner_name")),
         *(recipient.get(k) for k in ("recipient_person_id", "recipient_name", "recipient_contact_id",
                                      "recipient_contact")),
         _reset_letter(o), now_iso(), offer_id))
    _event(conn, offer_id, "refreshed", actor, from_hash=o["snapshot_hash"], to_hash=snap["hash"],
           from_cents=o["price_cents"], to_cents=cents)
    conn.commit()
    return _get(conn, offer_id)


def set_price(conn: sqlite3.Connection, offer_id: int, price: Any, actor: str) -> dict:
    """Type a price (a manual override), or pass None to go back to following the default (4x the rent in the
    offer's own snapshot; use refresh to pick up newer rent first)."""
    o = unsent(conn, offer_id)
    if price in (None, ""):
        cents, mode = _need_default(loads(o["snapshot_json"], {}) or {}), "default"
    else:
        cents, mode = parse_price(price), "override"
    conn.execute("UPDATE offers SET price_cents=?, price_mode=?, status=?, updated_at=? WHERE id=?",
                 (cents, mode, _reset_letter(o), now_iso(), offer_id))
    _event(conn, offer_id, "price_changed", actor, from_cents=o["price_cents"], to_cents=cents, price_mode=mode)
    conn.commit()
    return _get(conn, offer_id)


def set_status(conn: sqlite3.Connection, offer_id: int, status: str, actor: str) -> dict:
    """Move an unsent offer along the chain. Letter stages need a letter made from the offer's current snapshot.
    'sent' is never set here: only mark_sent does that."""
    o = unsent(conn, offer_id)
    if status not in STATUSES or status == "sent":
        raise ValueError("That is not a stage an offer can be moved to. Use Mark as sent to record a send.")
    if status in NEEDS_DOCUMENT and not one(conn, "SELECT id FROM offer_documents WHERE offer_id=? AND snapshot_hash=?",
                                            (offer_id, letter_hash(o))):
        raise ValueError("There is no LOI made from this offer's current numbers yet.")
    conn.execute("UPDATE offers SET status=?, updated_at=? WHERE id=?", (status, now_iso(), offer_id))
    _event(conn, offer_id, "status_changed", actor, from_status=o["status"], to_status=status)
    conn.commit()
    return _get(conn, offer_id)


# --- the letter (made in Google Drive by bluemoon/loi) ----------------------------------------------------------

def add_document(conn: sqlite3.Connection, offer_id: int, doc: Mapping[str, Any], actor: str) -> dict:
    """Keep a letter made for this offer (template id and version, the ids and links of the Doc and the PDF),
    tied to what it was made from. The offer becomes 'LOI generated'. Earlier letters stay on record."""
    o = unsent(conn, offer_id)
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO offer_documents(offer_id, template_id, template_version, snapshot_hash, doc_id, doc_url, pdf_id, "
        "pdf_url, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (offer_id, doc["template_id"], doc["template_version"], letter_hash(o), doc["doc_id"], doc["doc_url"],
         doc["pdf_id"], doc["pdf_url"], actor, stamp))
    conn.execute("UPDATE offers SET status='loi_generated', updated_at=? WHERE id=?", (stamp, offer_id))
    _event(conn, offer_id, "document_generated", actor, document_id=cur.lastrowid, template_version=doc["template_version"],
           snapshot_hash=letter_hash(o))
    conn.commit()
    return _get(conn, offer_id)


def letter_note(conn: sqlite3.Connection, offer_id: int, kind: str, actor: str, *, status: str | None = None,
                **detail: Any) -> dict:
    """Write down that making the letter did not happen (kind 'document_blocked' with the offer's new stage, or
    'failed'). A sent offer is never touched."""
    o = unsent(conn, offer_id)
    if status:
        conn.execute("UPDATE offers SET status=?, updated_at=? WHERE id=?", (status, now_iso(), offer_id))
    _event(conn, offer_id, kind, actor, from_status=o["status"], **detail)
    conn.commit()
    return _get(conn, offer_id)


# --- sending -----------------------------------------------------------------------------------------------

def mark_sent(conn: sqlite3.Connection, offer_id: int, how: str, when: Any, actor: str, cfg: PricingConfig, *,
              via: str = "manual") -> dict:
    """Record that this version was sent (how: email | in person | mail; when: a day, today if blank). Adds the
    follow-up action. A second mark on the same version is refused, so a retry never double counts."""
    o = unsent(conn, offer_id)
    if how not in SENT_HOW:
        raise ValueError(f"Say how it was sent: {', '.join(SENT_HOW)}.")
    if via not in SENT_VIA:
        raise ValueError(f"'via' must be one of: {', '.join(SENT_VIA)}.")
    today = cfg.today()
    day = _day(when, "send date", today)
    conn.execute("UPDATE offers SET status='sent', sent_how=?, sent_on=?, sent_by=?, sent_via=?, updated_at=? WHERE id=?",
                 (how, day, actor, via, now_iso(), offer_id))
    _event(conn, offer_id, "marked_sent", actor, how=how, on=day, via=via)
    due = (parse_date(day) + timedelta(days=cfg.follow_up_days)).isoformat()
    _add_action(conn, offer_id, FOLLOW_UP_TEXT, actor, due_on=due, owner=actor, auto=True)
    conn.commit()
    return _get(conn, offer_id)


def _duplicate_history(conn: sqlite3.Connection, property_id: int, cents: int, how: str, day: str,
                       skip: int | None = None) -> dict | None:
    return one(conn, "SELECT id, version FROM offers WHERE property_id=? AND status='sent' AND price_cents=? AND "
                     "sent_how=? AND sent_on=? AND id != ?", (property_id, cents, how, day, skip or 0))


def record_historical(conn: sqlite3.Connection, prop: Mapping[str, Any], actor: str, cfg: PricingConfig, *,
                      price: Any, how: str, sent_on: Any, recipient_name: Any = None, note: Any = None) -> dict:
    """An offer made before this app, entered by hand. It is a sent version with an incomplete snapshot (the rent
    it was based on is unknown). The same price, day and method for a property is refused as already recorded."""
    pid = int(prop["id"])
    cents = parse_price(price)
    if how not in SENT_HOW:
        raise ValueError(f"Say how it was sent: {', '.join(SENT_HOW)}.")
    day = _day(sent_on, "send date", cfg.today())
    dup = _duplicate_history(conn, pid, cents, how, day)
    if dup:
        raise ValueError(f"That offer is already recorded as version {dup['version']}.")
    name = clean_str(recipient_name)
    note = clean_str(note)
    if note and len(note) > MAX_NOTE:
        raise ValueError(f"The note is longer than {MAX_NOTE} characters.")
    snap = {"historical": True, "hash": "historical"}
    version = int(one(conn, "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM offers WHERE property_id=? AND intent='loi'",
                      (pid,))["v"])
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO offers(property_id, version, status, origin, seller_name, recipient_name, price_cents, price_mode, "
        "snapshot_json, snapshot_hash, incomplete_snapshot, sent_how, sent_on, sent_by, sent_via, created_by, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
        (pid, version, "sent", "historical", clean_str(prop.get("owner_name")), name, cents, "override", dumps(snap),
         snap["hash"], how, day, actor, "manual", actor, stamp, stamp))
    oid = int(cur.lastrowid)
    _event(conn, oid, "historical", actor, version=version, price_cents=cents, how=how, on=day, note=note)
    conn.commit()
    return _get(conn, oid)


def correct_historical(conn: sqlite3.Connection, offer_id: int, changes: Mapping[str, Any], actor: str,
                       cfg: PricingConfig) -> dict:
    """Fix a hand-entered offer in place (price, how or day sent). It stays one record, so nothing is counted twice."""
    o = _get(conn, offer_id)
    if o["origin"] != "historical":
        raise ValueError("Only an offer entered after the fact can be corrected. A prepared offer is final once sent.")
    cents = parse_price(changes["price"]) if changes.get("price") not in (None, "") else o["price_cents"]
    how = changes.get("how") or o["sent_how"]
    if how not in SENT_HOW:
        raise ValueError(f"Say how it was sent: {', '.join(SENT_HOW)}.")
    day = _day(changes["sent_on"], "send date", cfg.today()) if changes.get("sent_on") else o["sent_on"]
    dup = _duplicate_history(conn, o["property_id"], cents, how, day, skip=offer_id)
    if dup:
        raise ValueError(f"That would match version {dup['version']}, which is already recorded.")
    conn.execute("UPDATE offers SET price_cents=?, sent_how=?, sent_on=?, updated_at=? WHERE id=?",
                 (cents, how, day, now_iso(), offer_id))
    _event(conn, offer_id, "corrected", actor, from_cents=o["price_cents"], to_cents=cents, from_how=o["sent_how"],
           to_how=how, from_on=o["sent_on"], to_on=day)
    conn.commit()
    return _get(conn, offer_id)


# --- responses ---------------------------------------------------------------------------------------------

def _amounts(classification: str, low: Any, high: Any) -> tuple[int | None, int | None]:
    has_low, has_high = low not in (None, ""), high not in (None, "")
    if not has_low and not has_high:
        if classification == "counter":
            raise ValueError("A counteroffer needs the amount they asked for.")
        return None, None
    if classification not in AMOUNT_CLASSES:
        raise ValueError("Only a counteroffer (or a reply that needs review) can carry an amount.")
    if not has_low:
        raise ValueError("Enter the lower figure of the range (or the single figure they named).")
    lo = parse_price(low)
    hi = parse_price(high) if has_high else None
    if hi is not None and hi < lo:
        raise ValueError("The top of their range is below the bottom of it.")
    return lo, hi


def _classification(value: Any) -> str:
    c = clean_str(value) or "needs_review"      # unclear replies wait for a person instead of guessing
    if c not in CLASSES:
        raise ValueError(f"The category must be one of: {', '.join(CLASSES)}.")
    return c


def add_response(conn: sqlite3.Connection, offer_id: int, text: Any, actor: str, cfg: PricingConfig, *,
                 classification: Any = None, counter_low: Any = None, counter_high: Any = None,
                 received_on: Any = None) -> dict:
    """What they said, kept word for word. A counteroffer keeps its range. The offer itself is untouched."""
    o = _get(conn, offer_id)
    if o["status"] != "sent":
        raise ValueError("Only an offer that was sent can get a response.")
    body = _text(text, "response", MAX_TEXT)
    cls = _classification(classification)
    lo, hi = _amounts(cls, counter_low, counter_high)
    day = _day(received_on, "response date", cfg.today())
    cur = conn.execute(
        "INSERT INTO offer_responses(offer_id, classification, original_text, counter_low_cents, counter_high_cents, "
        "received_on, recorded_by, recorded_at) VALUES(?,?,?,?,?,?,?,?)",
        (offer_id, cls, body, lo, hi, day, actor, now_iso()))
    _event(conn, offer_id, "response", actor, response_id=cur.lastrowid, classification=cls, counter_low_cents=lo,
           counter_high_cents=hi)
    conn.commit()
    return one(conn, "SELECT * FROM offer_responses WHERE id=?", (cur.lastrowid,)) or {}


def correct_response(conn: sqlite3.Connection, response_id: int, actor: str, *, classification: Any,
                     counter_low: Any = None, counter_high: Any = None) -> dict:
    """Fix the category or the counter range. The original text is never edited."""
    r = one(conn, "SELECT * FROM offer_responses WHERE id=?", (response_id,))
    if not r:
        raise KeyError(f"Response {response_id} was not found.")
    cls = _classification(classification)
    lo, hi = _amounts(cls, counter_low, counter_high)
    conn.execute("UPDATE offer_responses SET classification=?, counter_low_cents=?, counter_high_cents=?, "
                 "corrected_by=?, corrected_at=? WHERE id=?", (cls, lo, hi, actor, now_iso(), response_id))
    _event(conn, r["offer_id"], "response_corrected", actor, response_id=response_id, from_classification=r["classification"],
           classification=cls, counter_low_cents=lo, counter_high_cents=hi)
    conn.commit()
    return one(conn, "SELECT * FROM offer_responses WHERE id=?", (response_id,)) or {}


# --- next actions ------------------------------------------------------------------------------------------

def _add_action(conn: sqlite3.Connection, offer_id: int, text: str, actor: str, *, due_on: str | None,
                owner: str | None, auto: bool = False) -> int:
    cur = conn.execute("INSERT INTO offer_actions(offer_id, text, owner, due_on, auto, created_by, created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (offer_id, text, owner or actor, due_on, int(auto), actor, now_iso()))
    _event(conn, offer_id, "next_action", actor, action_id=cur.lastrowid, change="added", text=text, due_on=due_on)
    return int(cur.lastrowid)


def add_action(conn: sqlite3.Connection, offer_id: int, text: Any, actor: str, *, due_on: Any = None,
               owner: Any = None) -> dict:
    _get(conn, offer_id)
    aid = _add_action(conn, offer_id, _text(text, "next action", MAX_NOTE), actor, due_on=_due(due_on),
                      owner=clean_str(owner))
    conn.commit()
    return one(conn, "SELECT * FROM offer_actions WHERE id=?", (aid,)) or {}


def update_action(conn: sqlite3.Connection, action_id: int, changes: Mapping[str, Any], actor: str) -> dict:
    """Change the text, owner or due date, or tick it done (done=False reopens it). Only the keys sent change."""
    a = one(conn, "SELECT * FROM offer_actions WHERE id=?", (action_id,))
    if not a:
        raise KeyError(f"Next action {action_id} was not found.")
    new = dict(a)
    if "text" in changes:
        new["text"] = _text(changes["text"], "next action", MAX_NOTE)
    if "owner" in changes:
        new["owner"] = clean_str(changes["owner"]) or actor
    if "due_on" in changes:
        new["due_on"] = _due(changes["due_on"])
    if "done" in changes:
        new["done"] = int(bool(changes["done"]))
        new["done_by"], new["done_at"] = (actor, now_iso()) if new["done"] else (None, None)
    conn.execute("UPDATE offer_actions SET text=?, owner=?, due_on=?, done=?, done_by=?, done_at=? WHERE id=?",
                 (new["text"], new["owner"], new["due_on"], new["done"], new["done_by"], new["done_at"], action_id))
    _event(conn, a["offer_id"], "next_action", actor, action_id=action_id, change="done" if new["done"] and not a["done"]
           else "reopened" if a["done"] and not new["done"] else "changed", text=new["text"], due_on=new["due_on"])
    conn.commit()
    return one(conn, "SELECT * FROM offer_actions WHERE id=?", (action_id,)) or {}


# --- views -------------------------------------------------------------------------------------------------

def _counter(responses: list[dict], annual: int | None) -> dict | None:
    """The latest counteroffer, with what its figures imply against the offer's own yearly rent."""
    last = next((r for r in reversed(responses) if r["classification"] == "counter"), None)
    if not last:
        return None
    lo, hi = last["counter_low_cents"], last["counter_high_cents"]
    return {"low_cents": lo, "high_cents": hi, "response_id": last["id"], "received_on": last["received_on"],
            "low_multiple": _mult(lo, annual), "high_multiple": _mult(hi, annual)}


def _mult(cents: int | None, annual: int | None) -> str | None:
    m = pricing.implied_multiple(cents, annual)
    return None if m is None else format(m, "f")


def _next_action(actions: list[dict]) -> dict | None:
    open_ = [a for a in actions if not a["done"]]
    return min(open_, key=lambda a: (a["due_on"] is None, a["due_on"] or "", a["id"])) if open_ else None


def _view(o: Mapping[str, Any], responses: list[dict], actions: list[dict], docs: list[dict], events: list[dict] | None,
          current_hash: str | None) -> dict:
    snap = loads(o["snapshot_json"], {}) or {}
    annual = snap.get("annual_cents")
    sent = None if o["status"] != "sent" else {
        "how": o["sent_how"], "on": o["sent_on"], "by": o["sent_by"], "via": o["sent_via"],
        "via_label": "Marked by hand" if o["sent_via"] == "manual" else "Reported by the mail provider"}
    current = letter_hash(o)
    has_letter = any(d["snapshot_hash"] == current for d in docs)
    out = {
        "id": o["id"], "property_id": o["property_id"], "version": o["version"], "status": o["status"],
        "status_label": STATUS_LABELS[o["status"]], "origin": o["origin"], "incomplete_snapshot": bool(o["incomplete_snapshot"]),
        "seller_name": o["seller_name"],
        "recipient": {"person_id": o["recipient_person_id"], "name": o["recipient_name"], "contact": o["recipient_contact"]}
        if o["recipient_name"] else None,
        "price_cents": o["price_cents"], "price_mode": o["price_mode"], "implied_multiple": _mult(o["price_cents"], annual),
        "rent": None if o["incomplete_snapshot"] else {
            "annual_cents": annual, "basis": snap.get("basis"), "source": snap.get("source"),
            "scenarios": snap.get("scenarios") or []},
        "stale": bool(current_hash and o["status"] != "sent" and current_hash != o["snapshot_hash"]),
        "sent": sent, "counter": _counter(responses, annual),
        "responses": [{**r, "classification_label": CLASS_LABELS[r["classification"]]} for r in responses],
        "actions": actions, "next_action": _next_action(actions),
        "documents": [{"id": d["id"], "template_version": d["template_version"], "doc_url": d["doc_url"],
                       "pdf_url": d["pdf_url"], "created_by": d["created_by"], "created_at": d["created_at"],
                       "current": d["snapshot_hash"] == current} for d in reversed(docs)],
        "readiness": {"template": {"ready": has_letter, "text": "LOI made" if has_letter else "LOI template needed"},
                      "recipient": {"ready": bool(o["recipient_name"]),
                                    "text": "Recipient chosen" if o["recipient_name"] else "Recipient needed"}},
        "created_by": o["created_by"], "created_at": o["created_at"],
    }
    if events is not None:
        out["events"] = [{**e, "detail": loads(e["detail_json"], {}) or {}} for e in events]
    return out


def _children(conn: sqlite3.Connection, offers: list[dict], table: str, order: str) -> dict[int, list[dict]]:
    ids = dumps([o["id"] for o in offers])
    out: dict[int, list[dict]] = {}
    for r in rows(conn, f"SELECT * FROM {table} WHERE offer_id IN (SELECT value FROM json_each(?)) ORDER BY {order}",
                  (ids,)):
        out.setdefault(r["offer_id"], []).append(r)
    return out


def views(conn: sqlite3.Connection, offers: list[dict], current_hashes: Mapping[int, str] | None = None, *,
          events: bool = False) -> list[dict]:
    """Offer rows to the dicts the screens use, with their responses, actions, letters (and events) in a few queries."""
    resp = _children(conn, offers, "offer_responses", "id")
    acts = _children(conn, offers, "offer_actions", "id")
    docs = _children(conn, offers, "offer_documents", "id")
    evs = _children(conn, offers, "offer_events", "id DESC") if events else {}
    hashes = current_hashes or {}
    return [_view(o, resp.get(o["id"], []), acts.get(o["id"], []), docs.get(o["id"], []),
                  evs.get(o["id"], []) if events else None, hashes.get(o["property_id"])) for o in offers]


def property_block(conn: sqlite3.Connection, pid: int, snap: Mapping[str, Any]) -> dict:
    """The drawer's Offers section: every version (newest first) and what a new one would start from."""
    found = rows(conn, "SELECT * FROM offers WHERE property_id=? AND intent='loi' ORDER BY version DESC", (pid,))
    latest = found[0] if found else None
    return {
        "offers": views(conn, found, {pid: snap["hash"]}, events=True),
        "can_prepare": latest is None or latest["status"] == "sent",
        "next_version": (latest["version"] + 1) if latest else 1,
        "current": {"annual_cents": snap.get("annual_cents"), "basis": snap.get("basis"), "source": snap.get("source"),
                    "default_cents": default_cents(snap)},
        "recipient": current_recipient(conn, pid).get("recipient_name"),
        "sent_how": list(SENT_HOW), "class_choices": [[c, CLASS_LABELS[c]] for c in CLASSES],
    }


def market_list(conn: sqlite3.Connection, market_id: int, current_hashes: Mapping[int, str] | None = None) -> list[dict]:
    """Every offer of a market, newest first, for the Offers screen."""
    found = rows(conn, "SELECT o.*, p.address, p.city FROM offers o JOIN properties p ON p.id = o.property_id "
                       "WHERE p.market_id=? ORDER BY o.created_at DESC, o.id DESC", (market_id,))
    return [{**v, "address": f["address"], "city": f["city"]} for v, f in zip(views(conn, found, current_hashes), found)]
