"""The DRILL engine: Data Resolution through Incremental List Liquidation.

Every property runs down the stages in `config/drill.yaml`, free sources first. Each stage is checked off
as found / nothing / skipped, and every failed lookup feeds the next stage. No record is abandoned until
every stage has been tried.

Contact levels (drill_state.level):
  1 No owner data    2 Possible owner (owner data from records, not talked to yet)
  3 Confirmed owner  (we talked and they confirmed; hand off to the CRM). Records never set level 3.
Status (drill_state.status): active | confirmed | exhausted | dnc.

What the next step is, in order:
  1. do not contact      -> 'dnc' (nothing more to do)
  2. confirmed owner     -> 'handoff' (send to the CRM)
  3. exhausted           -> 'exhausted' (every stage tried; reopen when something new turns up)
  4. untested phone, text, email, social or voicemail contact -> 'validate' (reach out and log what happened).
     Mail does not count here: letters are their own stage. A contact that has been tried
     `validate.tries_before_moving_on` times with no answer stops blocking the drill (it stays untested).
  5. the first applicable stage not tried yet -> 'stage'
  6. nothing left -> 'exhausted' (set automatically)

Errors meant for Seth are ValueError (bad input) or KeyError (unknown property) with plain-English text.
Every public function that writes commits its own transaction.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..config import Settings, get_settings
from ..db import market_by_slug, now_iso, one, rows, scalar, this_month
from ..enrich.owners import classify_owner
from ..util import clean_str, normalize_address, normalize_county, normalize_owner
from . import links as L

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

STATUSES = ("active", "confirmed", "exhausted", "dnc")
OUTCOMES = ("found", "nothing", "skipped")
OUTCOME_LABELS = {"found": "Found something", "nothing": "Nothing found", "skipped": "Skipped"}
CHANNELS = ("mail", "phone", "text", "email", "social", "vm")
REACH_CHANNELS = ("phone", "text", "email", "social", "vm")
PHONE_CHANNELS = ("phone", "text", "vm")
CHANNEL_LABELS = {"mail": "mailing address", "phone": "phone", "text": "text number", "email": "email",
                  "social": "social account", "vm": "voicemail"}
CONTACT_STATUSES = ("untested", "verified", "wrong", "dnc")
LEVEL_ACTIONS = ("owner_found", "confirm", "dnc", "undnc", "exhausted", "reopen", "handoff", "note")
LEVEL_NAMES_DEFAULT = {1: "No owner data", 2: "Possible owner", 3: "Confirmed owner"}
NEXT_KINDS = ("stage", "validate", "handoff", "exhausted", "dnc")
# Outcomes taken out of drill.yaml: older reach-out logs still show a readable label.
RETIRED_OUTCOME_LABELS = {"reached_not_owner": "Reached someone who isn't the owner"}

RECORDS_STAGE = "records_baseline"
PORTFOLIO_STAGE = "portfolio_match"
CREDIT_STAGE = "crexi_credit"
COUNTY_STAGE = "county_auditor"   # the county records step (enrich/parcels.py) checks it off as 'auto'
PASS_MARKER = "_new_pass"   # an attempt row with this stage key starts a new pass through the stages
CONTACT_GOALS = ("identify", "unmask")

# Best-guess person: earlier groups win; inside a group the most recent wins.
PERSON_TIERS: tuple[tuple[str, ...], ...] = (
    ("confirmed_owner", "portfolio_owner", "possible_owner", "member", "officer", "trustee", "deed_signer", "heir"),
    ("statutory_agent",),
)
CARRY_ROLES = ("member", "officer", "trustee", "deed_signer", "heir")
# People who are never the owner: recording one does not make a property "Possible owner" (level 2).
# 'rejected' is a person an AI research finding named who was then marked wrong.
NOT_OWNER_ROLES = ("manager", "agent", "rejected")
# Contacts and people the AI research step found. It writes them itself, but only to the buy-box properties of
# the market it ran in, with the link where each was seen. So the portfolio match copies an AI contact to the
# owner's other properties only once a reach-out shows it works (verified); AI people are never copied.
AI_SOURCE = "ai_research"
# Roles that name the owner itself: when the export had no owner name, the owner type is read from them.
OWNER_NAME_ROLES = ("owner_of_record", "possible_owner", "confirmed_owner")
# Owner types whose owner-of-record name can be read as a person ("DOE JOHN" -> "John Doe").
PERSON_OWNER_TYPES = ("individual", "trust", "estate", "portfolio", "unknown")
DEFAULT_PERSON_ROLE = {
    "records_baseline": "owner_of_record", "county_auditor": "owner_of_record", "rental_registration": "other",
    "entity_unmask": "member", "recorder_documents": "deed_signer", "property_contacts": "manager",
    "court_records": "heir",
}
PLACEHOLDER_OWNERS = frozenset({
    "UNKNOWN", "UNKNOWN OWNER", "OWNER", "CURRENT OWNER", "OWNER OF RECORD", "NOT AVAILABLE", "N/A", "NA",
    "NONE", "CURRENT RESIDENT", "RESIDENT", "TBD",
})
DEFAULT_TRIES_BEFORE_MOVING_ON = 2   # drill.yaml validate.tries_before_moving_on overrides this
DEFAULT_MAILING_GROUP_MAX = 50       # config.yaml owners.mailing_group_max_names overrides this
_CHUNK = 900


# --- config models ------------------------------------------------------------------------------

class Stage(BaseModel):
    """One DRILL stage from config/drill.yaml."""
    model_config = ConfigDict(extra="allow")

    key: str
    name: str
    goal: str = "reach"
    automation: str = "assisted"
    cost: str = "free"
    applies_to: list[str] = Field(default_factory=lambda: ["all"])
    asset_classes: list[str] | None = None
    states: list[str] | None = None
    sources: list[str] = Field(default_factory=list)
    method: str = ""
    hit_rate: float | None = None
    learn: str | None = None
    enabled: bool = True
    person_role: str | None = None
    # Runs as its own job (the AI research): never takes an owner's 'next' slot while a hands-on stage is open.
    background: bool = False

    @field_validator("applies_to", "sources", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> Any:
        if v is None:
            return []
        return [v] if isinstance(v, str) else v

    def public(self) -> dict:
        return {"key": self.key, "name": self.name, "goal": self.goal, "automation": self.automation,
                "cost": self.cost, "method": self.method, "hit_rate": self.hit_rate, "learn": self.learn,
                "enabled": self.enabled, "applies_to": list(self.applies_to), "sources": list(self.sources),
                "asset_classes": self.asset_classes, "states": self.states, "background": self.background}

    def applies(self, owner_type: str | None, asset_class: str | None, state: str | None) -> bool:
        who = {x.lower() for x in self.applies_to}
        if who and "all" not in who and (owner_type or "unknown").lower() not in who:
            return False
        if self.asset_classes and (asset_class or "") not in self.asset_classes:
            return False
        return not self.states or (state or "").upper() in {s.upper() for s in self.states}


class OutcomeSpec(BaseModel):
    label: str
    effect: str = "none"   # none | mark_wrong | confirm | dnc


class ValidateConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "Reach out and confirm"
    channels: list[str] = Field(default_factory=lambda: list(CHANNELS))
    outcomes: dict[str, OutcomeSpec] = Field(default_factory=dict)
    tries_before_moving_on: int = DEFAULT_TRIES_BEFORE_MOVING_ON


@dataclass
class DrillConfig:
    stages: list[Stage]
    validate: ValidateConfig
    levels: dict[int, str]
    # stage key -> (STATE, COUNTY) pairs where the county table says the stage's source doesn't exist there
    # (no county rental registry, for example): the stage is skipped for properties in those counties.
    not_in_county: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    learn_order: bool = True   # drill.yaml learn_order: real hit rates reorder the free hands-on stages
    # drill.yaml ai_research.scope: buybox = a background stage (the AI research) only reaches buy-box owners
    research_scope: str = "buybox"

    @property
    def by_key(self) -> dict[str, Stage]:
        return {s.key: s for s in self.stages}

    def stage(self, key: str) -> Stage:
        st = self.by_key.get(key)
        if st is None:
            known = ", ".join(s.key for s in self.stages)
            raise ValueError(f"Unknown DRILL stage '{key}'. Known stages: {known}.")
        return st


def drill_config(settings: Settings | None = None) -> DrillConfig:
    """Parse config/drill.yaml (stages, validate outcomes, level names)."""
    s = settings or get_settings()
    raw = s.drill or {}
    try:
        stages = [Stage(**st) for st in raw.get("stages") or []]
        validate = ValidateConfig(**(raw.get("validate") or {}))
    except ValidationError as e:
        raise ValueError(f"config/drill.yaml has a problem: {e.errors()[0].get('msg')}") from e
    levels = dict(LEVEL_NAMES_DEFAULT)
    for k, v in (raw.get("levels") or {}).items():
        try:
            levels[int(k)] = (v or {}).get("name") or levels.get(int(k), str(k))
        except (TypeError, ValueError, AttributeError):
            continue
    learn = raw.get("learn_order", True)
    scope = str((raw.get("ai_research") or {}).get("scope") or "buybox").strip().lower()
    return DrillConfig(stages=stages, validate=validate, levels=levels, not_in_county=_dead_county_slots(stages, s),
                       learn_order=learn if isinstance(learn, bool) else str(learn).lower() not in ("false", "no", "0"),
                       research_scope=scope)


def _dead_county_slots(stages: list[Stage], s: Settings) -> dict[str, set[tuple[str, str]]]:
    """{stage key: {(STATE, COUNTY)}} for stages whose every source is a county slot that the county table
    marks `available: false` with no link (e.g. no county rental registry). Counties missing from the table,
    or with a city registry link, keep the stage."""
    sources = (s.sources or {}).get("sources") or {}
    table = (s.sources or {}).get("counties") or {}
    if not isinstance(table, Mapping):
        return {}
    out: dict[str, set[tuple[str, str]]] = {}
    for st in stages:
        srcs = [sources.get(k) or {} for k in st.sources]
        if not srcs or any(not isinstance(src, Mapping) or src.get("kind") != "county" for src in srcs):
            continue  # only stages whose every source is a county slot
        slots = {clean_str(src.get("county_slot")) or L.COUNTY_SLOTS.get(k) or k for k, src in zip(st.sources, srcs)}
        dead = {(str(state).upper(), normalize_county(str(cty)) or "")
                for state, counties in table.items() if isinstance(counties, Mapping)
                for cty, entry in counties.items() if isinstance(entry, Mapping)
                and all(isinstance(entry.get(sl), Mapping) and entry[sl].get("available") is False
                        and not clean_str(entry[sl].get("url")) and not clean_str(entry[sl].get("template"))
                        for sl in slots)}
        if dead:
            out[st.key] = dead
    return out


# --- input models -------------------------------------------------------------------------------

_CHANNEL_ALIASES = {"cell": "phone", "mobile": "phone", "telephone": "phone", "call": "phone", "sms": "text",
                    "voicemail": "vm", "e-mail": "email", "address": "mail", "letter": "mail", "facebook": "social",
                    "linkedin": "social", "instagram": "social"}


def normalize_channel(v: Any) -> str:
    """'Phone', 'cell', 'SMS', 'e-mail', 'voicemail'... -> one of CHANNELS. ValueError when unknown."""
    c = (clean_str(v) or "").lower()
    c = _CHANNEL_ALIASES.get(c, c)
    if c not in CHANNELS:
        raise ValueError(f"'{v}' is not a contact type. Use one of: {', '.join(CHANNELS)}.")
    return c


class ContactIn(BaseModel):
    """A contact point typed in by hand or found by a stage."""
    channel: str
    value: str
    notes: str | None = None
    person_name: str | None = None

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, v: Any) -> str:
        return normalize_channel(v)


def _contact_in(**kw: Any) -> ContactIn:
    try:
        return ContactIn(**kw)
    except ValidationError as e:
        msg = str(e.errors()[0].get("msg", "invalid contact")).removeprefix("Value error, ")
        raise ValueError(msg) from None


def parse_contacts(contacts: Iterable[Mapping[str, Any]] | None) -> list[ContactIn]:
    """Turn [{channel, value, notes}] into ContactIn items, skipping blank values."""
    out: list[ContactIn] = []
    for c in contacts or []:
        if not isinstance(c, Mapping) or not clean_str(c.get("value")):
            continue
        out.append(_contact_in(**{k: c.get(k) for k in ("channel", "value", "notes", "person_name")}))
    return out


# --- contact values -----------------------------------------------------------------------------

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_contact(channel: str, value: Any) -> tuple[str, str]:
    """(display value, comparison key) for a contact. Raises ValueError with a plain message when invalid."""
    v = clean_str(value)
    if not v:
        raise ValueError("The contact is blank.")
    if channel in PHONE_CHANNELS:
        parts = re.split(r"(?i)\s*(?:x|ext\.?|extension)\s*(?=\d)", v, maxsplit=1)
        main = parts[0]
        ext = re.sub(r"\D", "", parts[1]) if len(parts) > 1 else ""
        digits = re.sub(r"\D", "", main)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise ValueError(f"'{v}' doesn't look like a 10-digit US phone number.")
        display = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" + (f" x{ext}" if ext else "")
        return display, digits + (f"x{ext}" if ext else "")
    if channel == "email":
        e = v.strip().strip("<>").lower().removeprefix("mailto:")
        if not _EMAIL.match(e):
            raise ValueError(f"'{v}' doesn't look like an email address.")
        return e, e
    if channel == "social":
        key = re.sub(r"^https?://", "", v.strip().lower())
        key = re.sub(r"^(www\.|m\.)", "", key).rstrip("/")
        return v.strip(), key
    if channel == "mail":
        return v.strip(), normalize_address(v) or v.strip().upper()
    return v.strip(), v.strip().lower()


def _contact_family(channel: str) -> str:
    return "phone" if channel in PHONE_CHANNELS else channel


def _contact_key(channel: str, value: Any) -> str:
    try:
        return normalize_contact(channel, value)[1]
    except ValueError:
        return (clean_str(value) or "").lower()


def _name_key(name: Any) -> str:
    s = (clean_str(name) or "").upper()
    s = re.sub(r"\([^)]*\)", " ", s)
    return re.sub(r"[^A-Z0-9&]+", " ", s).strip()


def _clean_owner(name: Any) -> str | None:
    s = clean_str(name)
    if not s or (normalize_owner(s) or "") in PLACEHOLDER_OWNERS:
        return None
    return s


def mailing_text(prop: Mapping[str, Any]) -> str | None:
    """The owner's mailing address as one line ('88 TESTER LN, SPRINGFIELD, OH 45504'), or None."""
    street = clean_str(prop.get("mailing_address"))
    if not street:
        return None
    unit = clean_str(prop.get("mailing_unit"))
    if unit and unit.upper() not in street.upper():
        street = f"{street} {unit}"
    city, state, zip_ = (clean_str(prop.get(k)) or "" for k in ("mailing_city", "mailing_state", "mailing_zip"))
    tail = " ".join(x for x in (state, zip_) if x)
    return ", ".join(x for x in (street, city, tail) if x)


_PO_BOX = re.compile(r"^(?:P\s*O|POST\s+OFFICE)\s+BOX\b|^BOX\b")


def _mailing_key(prop: Mapping[str, Any]) -> str | None:
    """Normalized mailing address for grouping ('P.O. Box 77' and 'PO BOX 77' match)."""
    street = clean_str(prop.get("mailing_address"))
    if not street:
        return None
    unit = clean_str(prop.get("mailing_unit"))
    line = f"{street} {unit}" if unit and unit.upper() not in street.upper() else street
    key = normalize_address(line, prop.get("mailing_city"), prop.get("mailing_state"), prop.get("mailing_zip"))
    return _PO_BOX.sub("PO BOX", key) if key else None


# --- transactions and small lookups -------------------------------------------------------------

@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _chunks(ids: list[int]) -> Iterator[list[int]]:
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def _require_property(conn: sqlite3.Connection, property_id: int) -> dict:
    p = one(conn, "SELECT p.*, m.asset_class AS asset_class_key, m.slug AS market_slug FROM properties p "
                  "JOIN markets m ON m.id=p.market_id WHERE p.id=?", (property_id,))
    if not p:
        raise KeyError(f"Property {property_id} was not found.")
    return p


def _ensure_state_row(conn: sqlite3.Connection, property_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO drill_state(property_id, level, status, updated_at) VALUES(?,1,'active',?)",
                 (property_id, now_iso()))


def ensure_states(conn: sqlite3.Connection, market_slug: str) -> int:
    """Create a drill_state row (level 1, active) for every property in the market that lacks one."""
    m = market_by_slug(conn, market_slug)
    cur = conn.execute(
        "INSERT INTO drill_state(property_id, level, status, updated_at) "
        "SELECT p.id, 1, 'active', ? FROM properties p LEFT JOIN drill_state d ON d.property_id=p.id "
        "WHERE p.market_id=? AND d.property_id IS NULL",
        (now_iso(), m["id"]),
    )
    conn.commit()
    return cur.rowcount


# --- loading everything the engine needs about a property ---------------------------------------

@dataclass
class Ctx:
    """Everything the engine knows about one property."""
    prop: dict
    state: dict
    owner: dict | None
    score: dict | None
    attempts: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    outreach: list[dict] = field(default_factory=list)
    credits: list[dict] = field(default_factory=list)
    stage_order: list[str] | None = None   # learned stage order for this owner type (None: drill.yaml order)

    @property
    def id(self) -> int:
        return int(self.prop["id"])

    @cached_property
    def owner_type(self) -> str:
        """The owner step's type. When the export had no owner name, the newest owner name the DRILL
        recorded decides it ('Robin Sample' is an individual), so the stages fit the owner found."""
        known = (self.owner or {}).get("owner_type") or "unknown"
        if known != "unknown" or _clean_owner(self.prop.get("owner_name")):
            return known
        for p in reversed(self.people):          # newest owner name the drill recorded
            if p.get("role") in OWNER_NAME_ROLES:
                found = classify_owner(p.get("name"))
                if found != "unknown":
                    return found
        return "unknown"

    @property
    def asset_class(self) -> str | None:
        return self.prop.get("asset_class_key")

    @property
    def score_value(self) -> float | None:
        return (self.score or {}).get("score")

    @property
    def in_buybox(self) -> bool | None:
        v = (self.score or {}).get("in_buybox")
        return None if v is None else bool(v)


def _load_rows(conn: sqlite3.Connection, table: str, cols: str, ids: list[int], order: str = "id") -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT {cols} FROM {table} WHERE property_id IN ({marks}) ORDER BY {order}", chunk):
            out[r["property_id"]].append(r)
    return out


def load_contexts(conn: sqlite3.Connection, *, market_id: int | None = None, property_ids: Iterable[int] | None = None,
                  status: str | Iterable[str] | None = None, buybox_only: bool = False,
                  learned: Mapping[tuple[int, str], list[str]] | None = None) -> list[Ctx]:
    """Load properties with their drill state, owner, score, attempts, people, contacts, outreach, credits.

    buybox_only leaves out properties scored outside the buy box (unscored ones stay: missing data never
    excludes). `learned` is _learned_orders() worked out already (for callers that load in several batches);
    by default it is worked out here."""
    where: list[str] = []
    params: list[Any] = []
    wanted: set[int] | None = None
    if buybox_only:
        where.append("NOT EXISTS (SELECT 1 FROM scores sc WHERE sc.property_id = p.id AND sc.in_buybox = 0)")
    if market_id is not None:
        where.append("p.market_id=?")
        params.append(market_id)
    if property_ids is not None:
        ids = [int(i) for i in property_ids]
        if not ids:
            return []
        if len(ids) <= _CHUNK:
            where.append(f"p.id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        else:
            wanted = set(ids)
    if status is not None:
        sts = [status] if isinstance(status, str) else list(status)
        where.append(f"COALESCE(d.status,'active') IN ({','.join('?' for _ in sts)})")
        params.extend(sts)
    sql = ("SELECT p.*, m.asset_class AS asset_class_key, m.slug AS market_slug FROM properties p "
           "JOIN markets m ON m.id=p.market_id LEFT JOIN drill_state d ON d.property_id=p.id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    props = rows(conn, sql + " ORDER BY p.id", params)
    if wanted is not None:
        props = [p for p in props if p["id"] in wanted]
    pids = [p["id"] for p in props]
    if not pids:
        return []
    states = {r["property_id"]: r for rs in _load_rows(conn, "drill_state", "*", pids, "property_id").values() for r in rs}
    owners = {r["property_id"]: r for rs in _load_rows(conn, "owners", "*", pids, "property_id").values() for r in rs}
    scores = {r["property_id"]: r for rs in _load_rows(
        conn, "scores", "property_id, in_buybox, score, rank, exclusion_reasons", pids, "property_id").values() for r in rs}
    attempts = _load_rows(conn, "drill_attempts", "*", pids)
    people = _load_rows(conn, "people", "*", pids)
    contacts = _load_rows(conn, "contact_points", "*", pids)
    outreach = _load_rows(conn, "outreach", "*", pids)
    credits = _load_rows(conn, "credit_ledger", "*", pids)
    out: list[Ctx] = []
    for p in props:
        st = states.get(p["id"]) or {"property_id": p["id"], "level": 1, "status": "active", "best_guess_person": None,
                                      "confirmed_person": None, "confirmed_at": None, "handed_off_at": None,
                                      "notes": None, "updated_at": None, "_missing": True}
        out.append(Ctx(prop=p, state=dict(st), owner=owners.get(p["id"]), score=scores.get(p["id"]),
                       attempts=attempts.get(p["id"], []), people=people.get(p["id"], []),
                       contacts=contacts.get(p["id"], []), outreach=outreach.get(p["id"], []),
                       credits=credits.get(p["id"], [])))
    orders = learned if learned is not None else _learned_orders(conn, {int(p["market_id"]) for p in props})
    if orders:
        for ctx in out:
            ctx.stage_order = orders.get((int(ctx.prop["market_id"]), ctx.owner_type))
    return out


def _learned_orders(conn: sqlite3.Connection, market_ids: Iterable[int], settings: Settings | None = None,
                    counts: Mapping[tuple[int, str, str], Mapping[str, int]] | None = None,
                    ) -> dict[tuple[int, str], list[str]]:
    """{(market_id, owner_type): stage keys in the order that has worked best} for owner types whose results
    change the drill.yaml order (drill.yaml learn_order).

    Only free hands-on stages (assisted, before the Crexi credit) move, and only into the slot of another
    stage with the same goal (identify, unmask, reach) that also has `advice_min_tries` results for that owner
    type. They are sorted by blended hit rate (stats.blend: the drill.yaml guess mixed with real results).
    Automatic, credit, paid and manual stages never move, and nothing moves until enough results exist. `counts`
    (stats.counts_by_market for these markets) can be passed in when the caller already has them."""
    ids = sorted({int(i) for i in market_ids})
    cfg = drill_config(settings)
    if not cfg.learn_order or not ids:
        return {}
    from . import stats as S  # stats imports this module, so import it here
    credit_at = next((i for i, st in enumerate(cfg.stages) if st.key == CREDIT_STAGE), len(cfg.stages))
    movable = [i for i, st in enumerate(cfg.stages[:credit_at])
               if st.enabled and st.cost == "free" and st.automation == "assisted"]
    if len(movable) < 2:
        return {}
    if counts is None:
        counts = S.counts_by_market(conn, ids)
    strength, min_tries = S._prior_strength(settings), S._min_tries(settings)
    out: dict[tuple[int, str], list[str]] = {}
    for mid, ot in sorted({(m, ot) for (m, _, ot) in counts if ot != "all"}):
        order = [st.key for st in cfg.stages]
        rates: dict[int, float] = {}
        for i in movable:
            c = counts.get((mid, cfg.stages[i].key, ot))
            if c and c["tried"] >= min_tries:
                rates[i] = S.blend(c["found"], c["tried"], cfg.stages[i].hit_rate, strength) or 0.0
        for goal in ("identify", "unmask", "reach"):
            slots = [i for i in movable if i in rates and cfg.stages[i].goal == goal]
            for slot, src in zip(slots, sorted(slots, key=lambda i: (-rates[i], i))):
                order[slot] = cfg.stages[src].key
        if order != [st.key for st in cfg.stages]:
            out[(mid, ot)] = order
    return out


def _ctx(conn: sqlite3.Connection, property_id: int) -> Ctx:
    found = load_contexts(conn, property_ids=[property_id])
    if not found:
        raise KeyError(f"Property {property_id} was not found.")
    return found[0]


# --- pure logic (no database) -------------------------------------------------------------------

def applicable_stages(prop: Mapping[str, Any], owner_type: str | None, asset_class: Any,
                      settings: Settings | None = None) -> list[dict]:
    """Stages that fit this property's owner type, asset class, state and county, in DRILL order.

    Each item is the stage config plus `enabled`. Stages switched off in drill.yaml (enabled: false) are
    included so screens can show them, but they are never part of the path.
    """
    cfg = drill_config(settings)
    ac = getattr(asset_class, "key", asset_class) or prop.get("asset_class_key")
    return [st.public() for st in _applicable(cfg, prop, owner_type, ac)]


def _ordered(stages: list[Stage], order: list[str] | None) -> list[Stage]:
    """Stages in a learned order (stage keys), or as they are when there is none."""
    if not order:
        return list(stages)
    pos = {k: i for i, k in enumerate(order)}
    return sorted(stages, key=lambda st: pos.get(st.key, len(pos)))


def _applicable(cfg: DrillConfig, prop: Mapping[str, Any], owner_type: str | None, asset_class: str | None,
                order: list[str] | None = None) -> list[Stage]:
    where = ((clean_str(prop.get("state")) or "").upper(), normalize_county(prop.get("county")) or "")
    return _ordered([st for st in cfg.stages if st.applies(owner_type, asset_class, prop.get("state"))
                     and where not in cfg.not_in_county.get(st.key, ())], order)


def _latest_by_stage(attempts: list[dict]) -> dict[str, dict]:
    """Latest check-off per stage over all passes."""
    out: dict[str, dict] = {}
    for a in attempts:  # ordered by id, so later ones win
        if a["stage_key"] != PASS_MARKER:
            out[a["stage_key"]] = a
    return out


def pass_number(attempts: list[dict]) -> int:
    """1 for the first pass through the stages, 2 after the first reopen of an exhausted owner, ..."""
    return 1 + sum(1 for a in attempts if a["stage_key"] == PASS_MARKER)


def current_latest(attempts: list[dict]) -> dict[str, dict]:
    """Latest check-off per stage in the current pass. A 'found' always stands; 'nothing' or 'skipped'
    from an earlier pass is open again after a reopen, except on the automatic stages."""
    start = 0
    for a in attempts:
        if a["stage_key"] == PASS_MARKER:
            start = int(a["id"])
    # The automatic stages stay checked: the DRILL job re-checks them every run anyway, so reopening them
    # would only park the owner on a step nobody works by hand.
    return {k: a for k, a in _latest_by_stage(attempts).items()
            if a["outcome"] == "found" or int(a["id"]) > start or k in (RECORDS_STAGE, PORTFOLIO_STAGE)}


def _stage_status(latest: dict | None) -> str | None:
    if latest is None:
        return None
    return {"found": "done_found", "nothing": "done_nothing", "skipped": "skipped"}.get(latest["outcome"], "done_nothing")


def _contact_tries(ctx: Ctx, cfg: DrillConfig) -> dict[int, dict]:
    """Per contact: number of no-reply tries and the latest outcome."""
    silent = {k for k, v in cfg.validate.outcomes.items() if v.effect == "none"}
    info: dict[int, dict] = {}
    for o in ctx.outreach:
        cid = o.get("contact_point_id")
        if cid is None:
            continue
        d = info.setdefault(cid, {"tries": 0, "last_outcome": None, "last_at": None})
        if o["outcome"] in silent:
            d["tries"] += 1
        d["last_outcome"] = o["outcome"]
        d["last_at"] = o["created_at"]
    return info


def contacts_to_validate(ctx: Ctx, cfg: DrillConfig) -> list[dict]:
    """Untested phone/text/email/social/voicemail contacts that still need a reach-out."""
    tries = _contact_tries(ctx, cfg)
    limit = max(1, int(cfg.validate.tries_before_moving_on or 1))
    out = []
    for c in ctx.contacts:
        if c["status"] != "untested" or c["channel"] not in REACH_CHANNELS:
            continue
        t = tries.get(c["id"], {})
        if t.get("last_outcome") == "replied_unconfirmed" or t.get("tries", 0) < limit:
            out.append(c)
    return out


def _person_tier(role: str | None) -> int | None:
    for i, tier in enumerate(PERSON_TIERS):
        if (role or "") in tier:
            return i
    return None


def best_guess_person(ctx: Ctx) -> str | None:
    """Who to look for: a member/officer/trustee/deed signer/heir first, then the statutory agent, then a
    person read from the owner-of-record name, then anyone else recorded (managers never count)."""
    best: tuple[int, int, str] | None = None
    for p in ctx.people:
        tier = _person_tier(p.get("role"))
        name = clean_str(p.get("name"))
        if tier is None or not name or L.looks_like_entity(name):
            continue
        cand = (tier, -int(p["id"]), name)
        if best is None or cand < best:
            best = cand
    if best:
        return best[2]
    if ctx.owner_type in PERSON_OWNER_TYPES:  # the owner step already knows LLCs and companies
        records = [p for p in ctx.people if p.get("role") == "owner_of_record"]
        for p in reversed(records):
            guess = L.person_from_owner_name(p.get("name"))
            if guess:
                return guess
        guess = L.person_from_owner_name(_clean_owner(ctx.prop.get("owner_name")))
        if guess:
            return guess
    for p in reversed(ctx.people):
        name = clean_str(p.get("name"))
        if p.get("role") in ("other", None) and name and not L.looks_like_entity(name):
            return name
    return None


def compute_level(ctx: Ctx, cfg: DrillConfig) -> int:
    """3 only after a conversation confirmed the owner; 2 with any owner data from records; else 1.

    A property manager or agent alone does not make level 2: they are never the owner."""
    if ctx.state.get("confirmed_at"):
        return 3
    if _clean_owner(ctx.prop.get("owner_name")) or any(p.get("role") not in NOT_OWNER_ROLES for p in ctx.people):
        return 2
    goals = {s.key: s.goal for s in cfg.stages}
    if any(a["outcome"] == "found" and goals.get(a["stage_key"]) in CONTACT_GOALS for a in ctx.attempts):
        return 2
    return 1


@dataclass
class Evaluation:
    level: int
    status: str
    best_guess: str | None
    stages: list[dict]
    to_validate: list[dict]
    next_step: dict


def _first_sentence(text: str | None) -> str:
    t = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    return m.group(1) if m else t


def owner_search_name(ctx: Ctx) -> str | None:
    """The owner name a web search can start from: the owner of record, else the newest owner name the DRILL
    recorded (a possible or confirmed owner). None when there is no name to search for yet."""
    name = _clean_owner(ctx.prop.get("owner_name"))
    if name:
        return name
    for p in reversed(ctx.people):
        if p.get("role") in OWNER_NAME_ROLES and _clean_owner(p.get("name")):
            return clean_str(p.get("name"))
    return None


def _stage_rows(ctx: Ctx, cfg: DrillConfig) -> list[dict]:
    latest = current_latest(ctx.attempts)
    by_stage: dict[str, list[dict]] = defaultdict(list)
    for a in ctx.attempts:
        by_stage[a["stage_key"]].append(a)
    out, next_marked = [], False
    held: list[int] = []   # untried background stages: 'next' only when nothing else is open
    applicable = {st.key for st in _applicable(cfg, ctx.prop, ctx.owner_type, ctx.asset_class)}
    home = {st.key: i for i, st in enumerate(cfg.stages)}
    for i, st in enumerate(_ordered(cfg.stages, ctx.stage_order)):
        if st.key not in applicable and st.key not in by_stage:
            continue
        status = _stage_status(latest.get(st.key))
        if st.key not in applicable:
            status = status or "skipped"  # checked off earlier, before the owner type changed
        elif not st.enabled:
            status = "disabled"
        elif status is None and st.background:
            status = "pending"
            # With no owner name there is nothing to search for, and a set-aside owner (scope buybox) is never in
            # a run: either way the owner can still run out of stages.
            if owner_search_name(ctx) and not (cfg.research_scope == "buybox" and ctx.in_buybox is False):
                held.append(len(out))
        elif status is None:
            status = "pending" if next_marked else "next"
            next_marked = True
        out.append({**st.public(), "status": status, "attempts": list(reversed(by_stage.get(st.key, []))),
                    "moved": i != home[st.key], "moved_up": i < home[st.key]})
    if not next_marked and held:
        out[held[0]]["status"] = "next"
    return out


def _validate_text(contacts: list[dict]) -> str:
    if len(contacts) == 1:
        c = contacts[0]
        return f"Reach out on the untested {CHANNEL_LABELS.get(c['channel'], c['channel'])} {c['value']} and log what happened."
    kinds = sorted({CHANNEL_LABELS.get(c["channel"], c["channel"]) for c in contacts})
    return f"Reach out on {len(contacts)} untested contacts ({', '.join(kinds)}) and log what happened."


def _next_step(ctx: Ctx, status: str, level: int, stages: list[dict], to_validate: list[dict],
               cfg: DrillConfig) -> dict:
    if status == "dnc":
        return {"kind": "dnc", "stage_key": None,
                "text": "Do not contact. This owner asked not to be contacted; leave them alone."}
    if level == 3 or status == "confirmed":
        who = clean_str(ctx.state.get("confirmed_person"))
        handed = clean_str(ctx.state.get("handed_off_at"))
        text = (f"Confirmed owner{f' ({who})' if who else ''}. "
                + (f"Handed off to the CRM on {handed[:10]}." if handed else "Hand off to the CRM."))
        return {"kind": "handoff", "stage_key": None, "text": text}
    if status == "exhausted":
        return {"kind": "exhausted", "stage_key": None,
                "text": "Every source has been tried. Reopen it when something new turns up."}
    if to_validate:
        step = {"kind": "validate", "stage_key": "validate", "text": _validate_text(to_validate),
                "contact_ids": [c["id"] for c in to_validate]}
        if any(c["channel"] in PHONE_CHANNELS for c in to_validate):
            step["note"] = ("Calls and texts to cell phones fall under telemarketing rules (TCPA, Do Not Call). "
                            "Check the number before you call or text.")
        return step
    for st in stages:
        if st["status"] == "next":
            text = f"{st['name']}: {_first_sentence(st['method'])}".strip().rstrip(":")
            if st.get("background"):
                text = (f"{st['name']}: it hasn't run for this owner yet. Run it from the Run screen "
                        f"(AI research), or check it off here.")
            if st.get("moved_up"):
                text += " (moved up by your results)"
            return {"kind": "stage", "stage_key": st["key"], "text": text}
    return {"kind": "exhausted", "stage_key": None,
            "text": "Every source has been tried. Reopen it when something new turns up."}


def evaluate(ctx: Ctx, cfg: DrillConfig, new_info: bool = False) -> Evaluation:
    """Work out level, status, best-guess person, stage statuses and the next step for one property."""
    level = compute_level(ctx, cfg)
    status = ctx.state.get("status") or "active"
    if status not in STATUSES:
        status = "active"
    if status == "exhausted" and new_info:
        status = "active"
    if status == "active" and level == 3:
        status = "confirmed"
    if status == "confirmed" and level != 3:
        status = "active"
    stages = _stage_rows(ctx, cfg)
    to_validate = contacts_to_validate(ctx, cfg) if status == "active" else []
    nxt = _next_step(ctx, status, level, stages, to_validate, cfg)
    if status == "active" and nxt["kind"] == "exhausted":
        status = "exhausted"
    return Evaluation(level=level, status=status, best_guess=best_guess_person(ctx), stages=stages,
                      to_validate=to_validate, next_step=nxt)


def channel_summary(contacts: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Best status per channel: verified > untested > dnc > wrong > none."""
    rank = {"verified": 4, "untested": 3, "dnc": 2, "wrong": 1}
    out = {c: "none" for c in CHANNELS}
    for c in contacts:
        ch, st = c.get("channel"), c.get("status")
        if ch in out and rank.get(st, 0) > rank.get(out[ch], 0):
            out[ch] = st
    return out


# --- writing state ------------------------------------------------------------------------------

def _save_state(conn: sqlite3.Connection, ctx: Ctx, ev: Evaluation) -> bool:
    st = ctx.state
    if st.get("_missing"):
        _ensure_state_row(conn, ctx.id)
    changed = (st.get("level") != ev.level or st.get("status") != ev.status
               or st.get("best_guess_person") != ev.best_guess or st.get("_missing"))
    if changed:
        conn.execute("UPDATE drill_state SET level=?, status=?, best_guess_person=?, updated_at=? WHERE property_id=?",
                     (ev.level, ev.status, ev.best_guess, now_iso(), ctx.id))
        st.update({"level": ev.level, "status": ev.status, "best_guess_person": ev.best_guess})
        st.pop("_missing", None)
    return bool(changed)


def _refresh(conn: sqlite3.Connection, property_id: int, cfg: DrillConfig, new_info: bool = False) -> Evaluation:
    ctx = _ctx(conn, property_id)
    ev = evaluate(ctx, cfg, new_info=new_info)
    _save_state(conn, ctx, ev)
    return ev


def refresh_states(conn: sqlite3.Connection, market_slug: str, settings: Settings | None = None) -> dict:
    """Recompute level, status (auto-exhaust) and best-guess person for every property in a market."""
    cfg = drill_config(settings)
    market = market_by_slug(conn, market_slug)
    ensure_states(conn, market_slug)
    changed = 0
    with _tx(conn):
        for ctx in load_contexts(conn, market_id=market["id"]):
            if _save_state(conn, ctx, evaluate(ctx, cfg)):
                changed += 1
    return {"market": market_slug, "changed": changed}


def _insert_attempt(conn: sqlite3.Connection, property_id: int, stage_key: str, outcome: str, detail: str | None,
                    actor: str) -> int:
    cur = conn.execute("INSERT INTO drill_attempts(property_id, stage_key, outcome, detail, actor, created_at) "
                       "VALUES(?,?,?,?,?,?)", (property_id, stage_key, outcome, clean_str(detail), actor, now_iso()))
    return int(cur.lastrowid)


def _add_person(conn: sqlite3.Connection, property_id: int, name: str, role: str | None = None,
                source: str | None = None, notes: str | None = None, *,
                upgrade_role: bool = True) -> tuple[int, bool]:
    """Add a person (deduped by name). Returns (person_id, created).

    A person already there with no real role (none, 'other', 'previous_owner') takes the new role. With
    upgrade_role=False that only happens to a person added by the same source: AI research never changes the
    role of someone you or another stage added."""
    nm = clean_str(name)
    if not nm:
        raise ValueError("The person's name is blank.")
    key = _name_key(nm)
    role = clean_str(role)
    for p in rows(conn, "SELECT id, name, role, source FROM people WHERE property_id=?", (property_id,)):
        if _name_key(p["name"]) == key:
            if (role and (p["role"] in (None, "", "other", "previous_owner")) and role != "other"
                    and (upgrade_role or p["source"] == clean_str(source))):
                conn.execute("UPDATE people SET role=? WHERE id=?", (role, p["id"]))
            return int(p["id"]), False
    cur = conn.execute("INSERT INTO people(property_id, name, role, source, notes, created_at) VALUES(?,?,?,?,?,?)",
                       (property_id, nm, role or "other", clean_str(source), clean_str(notes), now_iso()))
    return int(cur.lastrowid), True


def _dnc_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    out = set()
    for r in rows(conn, "SELECT channel, value FROM contact_points WHERE status='dnc'"):
        out.add((_contact_family(r["channel"]), _contact_key(r["channel"], r["value"])))
    return out


def _add_contact(conn: sqlite3.Connection, property_id: int, channel: str, value: Any, source: str | None = None,
                 person_id: int | None = None, notes: str | None = None, status: str = "untested",
                 dnc_keys: set[tuple[str, str]] | None = None) -> tuple[dict, bool]:
    """Add a contact (deduped per property by channel + value). Returns (contact row, created)."""
    channel = normalize_channel(channel)
    display, key = normalize_contact(channel, value)
    for c in rows(conn, "SELECT * FROM contact_points WHERE property_id=? AND channel=?", (property_id, channel)):
        if _contact_key(channel, c["value"]) == key:
            return c, False
    dnc = dnc_keys if dnc_keys is not None else _dnc_keys(conn)
    if (_contact_family(channel), key) in dnc:
        status = "dnc"
    state = scalar(conn, "SELECT status FROM drill_state WHERE property_id=?", (property_id,))
    if state == "dnc":
        status = "dnc"
    cur = conn.execute(
        "INSERT INTO contact_points(property_id, person_id, channel, value, source, status, notes, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (property_id, person_id, channel, display, clean_str(source), status, clean_str(notes), now_iso()))
    return one(conn, "SELECT * FROM contact_points WHERE id=?", (cur.lastrowid,)) or {}, True


def _person_by_name(conn: sqlite3.Connection, property_id: int, name: str | None, source: str | None) -> int | None:
    if not clean_str(name):
        return None
    pid, _ = _add_person(conn, property_id, name, role="other", source=source)
    return pid


def waiting_at_crexi(conn: sqlite3.Connection, property_id: int) -> int | None:
    """The batch id this property is waiting at Crexi in (sent, no reply yet), or None."""
    r = one(conn, "SELECT batch_id FROM crexi_batch_items WHERE property_id=? AND status='sent' ORDER BY batch_id DESC "
                  "LIMIT 1", (property_id,))
    return int(r["batch_id"]) if r else None


def waiting_message(batch_id: int) -> str:
    return (f"This property is waiting at Crexi in batch #{batch_id}; its credit is counted there. Import Crexi's "
            f"reply on the Credits screen instead (or close that batch first).")


def _sync_credit_ledger(conn: sqlite3.Connection, property_id: int, outcome: str) -> None:
    """A found/nothing check-off on the Crexi credit stage means a credit was spent: keep the ledger in step."""
    pending = one(conn, "SELECT id FROM credit_ledger WHERE property_id=? AND result IS NULL ORDER BY id DESC LIMIT 1",
                  (property_id,))
    if pending:
        conn.execute("UPDATE credit_ledger SET result=? WHERE id=?", (outcome, pending["id"]))
    else:
        waiting = waiting_at_crexi(conn, property_id)
        if waiting is not None:          # a second credit for one lookup: refuse (the whole check-off rolls back)
            raise ValueError(waiting_message(waiting))
        from ..crexi.period import period_key      # the credit period it is counted in (config.yaml crexi.reset_day)
        key = period_key()
        conn.execute("INSERT INTO credit_ledger(property_id, month, result, created_at, period) VALUES(?,?,?,?,?)",
                     (property_id, key[:7], outcome, now_iso(), key))


# --- public write API ---------------------------------------------------------------------------

def _result(conn: sqlite3.Connection, property_id: int, message: str, settings: Settings | None, **extra: Any) -> dict:
    drill = property_drill(conn, property_id, settings=settings)
    return {"ok": True, "property_id": property_id, "message": message, "level": drill["level"],
            "status": drill["status"], "next_step": drill["next_step"], **extra, "drill": drill}


def record_attempt(conn: sqlite3.Connection, property_id: int, stage_key: str, outcome: str, detail: str | None = None,
                   person: str | None = None, person_role: str | None = None,
                   contacts: Iterable[Mapping[str, Any]] | None = None, *, actor: str = "you",
                   settings: Settings | None = None) -> dict:
    """Check off one stage for one property: found | nothing | skipped, with what was found.

    `person` (+ `person_role`) is a name the stage turned up; `contacts` = [{channel, value, notes}].
    Re-checking a stage is allowed; the latest check-off counts. Checking off the Crexi credit stage as
    found/nothing also writes the credit ledger (it means a credit was spent).
    """
    cfg = drill_config(settings)
    stage = cfg.stage(stage_key)
    out = (clean_str(outcome) or "").lower()
    if out not in OUTCOMES:
        raise ValueError(f"'{outcome}' is not a result. Use found, nothing or skipped.")
    items = parse_contacts(contacts)
    with _tx(conn):
        _require_property(conn, property_id)
        _ensure_state_row(conn, property_id)
        attempt_id = _insert_attempt(conn, property_id, stage.key, out, detail, actor)
        person_id, person_new = None, False
        if clean_str(person):
            role = person_role or stage.person_role or DEFAULT_PERSON_ROLE.get(stage.key, "other")
            person_id, person_new = _add_person(conn, property_id, person, role=role, source=stage.key)
        dnc = _dnc_keys(conn)
        added = []
        for c in items:
            pid = person_id or _person_by_name(conn, property_id, c.person_name, stage.key)
            row, created = _add_contact(conn, property_id, c.channel, c.value, source=stage.key, person_id=pid,
                                        notes=c.notes, dnc_keys=dnc)
            if created:
                added.append(row)
        if stage.key == CREDIT_STAGE and out in ("found", "nothing"):
            _sync_credit_ledger(conn, property_id, out)
        ev = _refresh(conn, property_id, cfg, new_info=(out == "found" or person_new or bool(added)))
    msg = f"Saved: {stage.name}: {OUTCOME_LABELS[out].lower()}."
    if added:
        msg += f" Added {len(added)} contact{'s' if len(added) != 1 else ''}."
    msg += f" Next: {ev.next_step['text']}"
    return _result(conn, property_id, msg, settings, attempt_id=attempt_id, stage_key=stage.key, outcome=out,
                   person_id=person_id, contacts_added=added)


def add_person(conn: sqlite3.Connection, property_id: int, name: str, role: str | None = None,
               source: str | None = None, notes: str | None = None, *, settings: Settings | None = None) -> dict:
    """Record a person tied to the property (owner of record, member, agent, signer, manager, heir...)."""
    cfg = drill_config(settings)
    with _tx(conn):
        _require_property(conn, property_id)
        _ensure_state_row(conn, property_id)
        pid, created = _add_person(conn, property_id, name, role=role, source=source, notes=notes)
        _refresh(conn, property_id, cfg, new_info=created)
    person = one(conn, "SELECT * FROM people WHERE id=?", (pid,))
    msg = f"Added {person['name']}." if created else f"{person['name']} was already on this property."
    return _result(conn, property_id, msg, settings, person=person, created=created)


def add_contact(conn: sqlite3.Connection, property_id: int, channel: str, value: str, source: str | None = None,
                person_name: str | None = None, notes: str | None = None, *, settings: Settings | None = None) -> dict:
    """Add a phone, text, email, social, voicemail or mailing contact. Duplicates are not added twice."""
    cfg = drill_config(settings)
    item = _contact_in(channel=channel, value=value or "", notes=notes, person_name=person_name)
    with _tx(conn):
        _require_property(conn, property_id)
        _ensure_state_row(conn, property_id)
        pid = _person_by_name(conn, property_id, item.person_name, source)
        contact, created = _add_contact(conn, property_id, item.channel, item.value, source=source or "manual",
                                        person_id=pid, notes=item.notes)
        _refresh(conn, property_id, cfg, new_info=created)
    if not created:
        msg = f"That {CHANNEL_LABELS[item.channel]} is already on this property ({contact['status']})."
    elif contact["status"] == "dnc":
        msg = f"Added {contact['value']}, marked do-not-contact (it is on your do-not-contact list)."
    else:
        msg = f"Added {CHANNEL_LABELS[item.channel]} {contact['value']}."
    return _result(conn, property_id, msg, settings, contact=contact, created=created)


DNC_PERMANENT = ("This contact is marked do not contact, and that is permanent. If it was a mistake, use "
                 "'Allow contact again'.")


def set_contact_status(conn: sqlite3.Connection, contact_id: int, status: str, *, allow_contact: bool = False,
                       actor: str | None = None, settings: Settings | None = None) -> dict:
    """Mark a contact untested | verified | wrong | dnc. Do-not-contact also applies to the same phone number,
    email or mailing address on every other property, and it is permanent: taking it back needs
    allow_contact=True ('Allow contact again'), which takes it off every property at once.

    A contact the AI research found also updates its finding (who marked it, `actor`); marked wrong, the same
    finding is marked wrong on the owner's other properties that research wrote it to."""
    cfg = drill_config(settings)
    st = (clean_str(status) or "").lower()
    if st not in CONTACT_STATUSES:
        raise ValueError(f"'{status}' is not a contact status. Use one of: {', '.join(CONTACT_STATUSES)}.")
    c = one(conn, "SELECT * FROM contact_points WHERE id=?", (contact_id,))
    if not c:
        raise KeyError(f"Contact {contact_id} was not found.")
    allowing = c["status"] == "dnc" and st != "dnc"
    if allowing and not allow_contact:
        raise ValueError(DNC_PERMANENT)
    touched = {int(c["property_id"])}
    with _tx(conn):
        if allowing:
            touched |= _unspread_dnc(conn, c, st)
        else:
            tested = now_iso() if st in ("verified", "wrong", "dnc") else c.get("last_tested_at")
            conn.execute("UPDATE contact_points SET status=?, last_tested_at=? WHERE id=?", (st, tested, contact_id))
        if st == "dnc":
            touched |= _spread_dnc(conn, c["channel"], c["value"])
        if c.get("source") == AI_SOURCE:
            from . import research   # research imports this module, so import it here
            touched |= research.contact_marked(conn, c, st, actor)
        for pid in touched:
            _refresh(conn, pid, cfg)
    if st == "verified":
        carry_to_siblings(conn, int(c["property_id"]), settings=settings)
    contact = one(conn, "SELECT * FROM contact_points WHERE id=?", (contact_id,))
    msg = {"verified": "Marked as working.", "wrong": "Marked as wrong; the drill moves on.",
           "dnc": "Marked do-not-contact everywhere it appears.", "untested": "Marked as untested."}[st]
    if allowing:
        msg = f"Allowed contact again on {c['value']}: it is off the do-not-contact list. {msg}"
    if len(touched) > 1 and (st == "dnc" or allowing):
        msg += f" ({len(touched) - 1} other propert{'ies' if len(touched) > 2 else 'y'} had it too.)"
    elif len(touched) > 1 and st == "wrong":
        msg += (f" Also marked wrong on {len(touched) - 1} other propert{'ies' if len(touched) > 2 else 'y'} of this "
                f"owner (the AI research found it for all of them).")
    elif len(touched) > 1:
        msg += (f" Also put back to not tested yet on {len(touched) - 1} other "
                f"propert{'ies' if len(touched) > 2 else 'y'} of this owner.")
    return _result(conn, int(c["property_id"]), msg, settings, contact=contact)


def _spread_dnc(conn: sqlite3.Connection, channel: str, value: str) -> set[int]:
    fam, key = _contact_family(channel), _contact_key(channel, value)
    touched: set[int] = set()
    chans = PHONE_CHANNELS if fam == "phone" else (channel,)
    marks = ",".join("?" for _ in chans)
    for r in rows(conn, f"SELECT id, property_id, channel, value FROM contact_points WHERE channel IN ({marks}) "
                        f"AND status!='dnc'", chans):
        if _contact_key(r["channel"], r["value"]) == key:
            conn.execute("UPDATE contact_points SET status='dnc', last_tested_at=? WHERE id=?", (now_iso(), r["id"]))
            touched.add(int(r["property_id"]))
    return touched


def _same_contact(conn: sqlite3.Connection, channel: str, value: str, status: str | None = None) -> list[dict]:
    """Every contact row with the same phone number (phone, text or voicemail), email, social account or
    mailing address, on any property."""
    fam, key = _contact_family(channel), _contact_key(channel, value)
    chans = PHONE_CHANNELS if fam == "phone" else (channel,)
    marks = ",".join("?" for _ in chans)
    where = " AND cp.status=?" if status else ""
    found = rows(conn, f"SELECT cp.id, cp.property_id, cp.channel, cp.value, cp.status, d.status AS owner_status "
                       f"FROM contact_points cp LEFT JOIN drill_state d ON d.property_id=cp.property_id "
                       f"WHERE cp.channel IN ({marks}){where}", (*chans, *([status] if status else [])))
    return [r for r in found if _contact_key(r["channel"], r["value"]) == key]


def _unspread_dnc(conn: sqlite3.Connection, contact: dict, status: str) -> set[int]:
    """'Allow contact again': take this number, email or address off the do-not-contact list on every property.

    Refused while an owner who is still marked do-not-contact has it (their contacts stay blocked)."""
    same = _same_contact(conn, contact["channel"], contact["value"], "dnc")
    blocked = sorted({int(r["property_id"]) for r in same if r["owner_status"] == "dnc"})
    if blocked:
        props = {r["id"]: r for r in rows(conn, f"SELECT id, address, city FROM properties WHERE id IN "
                                                f"({','.join('?' for _ in blocked)})", blocked)}
        where = "; ".join(_short_address(props.get(i)) for i in blocked[:3])
        raise ValueError(f"{contact['value']} belongs to an owner still marked do-not-contact ({where}). "
                         "Undo that owner's do-not-contact first.")
    tested = now_iso() if status in ("verified", "wrong") else None
    touched: set[int] = set()
    for r in same:
        conn.execute("UPDATE contact_points SET status=?, last_tested_at=COALESCE(?, last_tested_at) WHERE id=?",
                     (status, tested, r["id"]))
        touched.add(int(r["property_id"]))
    for pid in touched:
        _append_note(conn, pid, f"Allowed contact again on {contact['value']}.")
    return touched


def _set_property_dnc(conn: sqlite3.Connection, property_id: int) -> None:
    """The owner asked not to be contacted: the property and every contact on it, and the same phone numbers,
    emails, social accounts and mailing addresses on every other property."""
    conn.execute("UPDATE drill_state SET status='dnc', updated_at=? WHERE property_id=?", (now_iso(), property_id))
    contacts = rows(conn, "SELECT id, channel, value FROM contact_points WHERE property_id=?", (property_id,))
    conn.execute("UPDATE contact_points SET status='dnc', last_tested_at=COALESCE(last_tested_at, ?) "
                 "WHERE property_id=?", (now_iso(), property_id))
    for c in contacts:
        if c["channel"] in CHANNELS:
            _spread_dnc(conn, c["channel"], c["value"])


def owner_sibling_ids(conn: sqlite3.Connection, property_id: int) -> list[int]:
    """The owner's other properties, in any market: the same owners.portfolio_key or the same owner name (the
    'pk:' and 'name:' keys of build_sibling_index). A shared mailing address alone doesn't count here (it can
    be a property manager's)."""
    sql = "SELECT p.id, p.owner_name, o.portfolio_key FROM properties p LEFT JOIN owners o ON o.property_id=p.id"
    me = one(conn, sql + " WHERE p.id=?", (property_id,))
    if not me:
        return []
    pk = clean_str(me.get("portfolio_key"))
    owner = _clean_owner(me.get("owner_name"))
    name = normalize_owner(owner) if owner else None
    if not pk and not name:
        return []
    out = []
    for r in rows(conn, sql + " WHERE p.id!=?", (property_id,)):
        if pk and clean_str(r["portfolio_key"]) == pk:
            out.append(int(r["id"]))
        elif name:
            other = _clean_owner(r["owner_name"])
            if other and normalize_owner(other) == name:
                out.append(int(r["id"]))
    return sorted(out)


def _dnc_siblings(conn: sqlite3.Connection, property_id: int, cfg: DrillConfig) -> list[dict]:
    """Do-not-contact carries to the owner's other properties (see owner_sibling_ids). Returns the ones marked.
    confirmed_at is kept, so undoing it on a property restores a confirmed owner."""
    marked = []
    for sid in owner_sibling_ids(conn, property_id):
        if scalar(conn, "SELECT status FROM drill_state WHERE property_id=?", (sid,)) == "dnc":
            continue
        _ensure_state_row(conn, sid)
        _set_property_dnc(conn, sid)
        _refresh(conn, sid, cfg)
        marked.append(one(conn, "SELECT id, address, city FROM properties WHERE id=?", (sid,)) or {"id": sid})
    return marked


def _dnc_message(marked: list[dict]) -> str:
    if not marked:
        return "Marked do-not-contact. Every contact on this property is now off limits."
    where = "; ".join(_short_address(p) for p in marked[:5]) + ("; ..." if len(marked) > 5 else "")
    n = len(marked)
    return (f"Marked do-not-contact here and on {n} other propert{'ies' if n != 1 else 'y'} this owner has: "
            f"{where}. Every contact on them is now off limits.")


def _confirm(conn: sqlite3.Connection, property_id: int, person: str | None) -> str | None:
    st = one(conn, "SELECT best_guess_person FROM drill_state WHERE property_id=?", (property_id,)) or {}
    owner = scalar(conn, "SELECT owner_name FROM properties WHERE id=?", (property_id,))
    who = clean_str(person) or clean_str(st.get("best_guess_person")) or _clean_owner(owner)
    if clean_str(person):
        _add_person(conn, property_id, person, role="confirmed_owner", source="conversation")
    conn.execute("UPDATE drill_state SET level=3, status='confirmed', confirmed_person=?, "
                 "confirmed_at=COALESCE(confirmed_at, ?), updated_at=? WHERE property_id=?",
                 (who, now_iso(), now_iso(), property_id))
    return who


def log_outreach(conn: sqlite3.Connection, property_id: int, channel: str, outcome: str,
                 contact_point_id: int | None = None, notes: str | None = None, *,
                 actor: str | None = None, settings: Settings | None = None) -> dict:
    """Log one reach-out and apply its effect from drill.yaml validate.outcomes:
    confirm -> level 3 (confirmed); mark_wrong -> the contact is marked wrong; dnc -> do not contact.
    A contact the AI research found also updates its finding, like set_contact_status: marked wrong, the same
    contact is marked wrong on the owner's other properties research wrote it to (`actor` is who logged it)."""
    cfg = drill_config(settings)
    allowed = cfg.validate.channels or list(CHANNELS)
    try:
        ch = normalize_channel(channel)
    except ValueError:
        ch = None
    if ch not in allowed:
        raise ValueError(f"'{channel}' is not a channel. Use one of: {', '.join(allowed)}.")
    oc = (clean_str(outcome) or "").lower()
    spec = cfg.validate.outcomes.get(oc)
    if spec is None:
        raise ValueError(f"'{outcome}' is not an outcome. Use one of: {', '.join(cfg.validate.outcomes)}.")
    _require_property(conn, property_id)
    status = scalar(conn, "SELECT status FROM drill_state WHERE property_id=?", (property_id,))
    if status == "dnc":
        raise ValueError("This owner is marked do-not-contact. Undo that first if they reached out to you.")
    contact = _pick_contact(conn, property_id, ch, contact_point_id)
    if contact and spec.effect != "dnc" and (contact["status"] == "dnc" or (
            _contact_family(contact["channel"]), _contact_key(contact["channel"], contact["value"])) in _dnc_keys(conn)):
        raise ValueError("That contact is marked do not contact. Do not call, text, email or write to it.")
    with _tx(conn):
        _ensure_state_row(conn, property_id)
        cur = conn.execute("INSERT INTO outreach(property_id, contact_point_id, channel, outcome, notes, created_at) "
                           "VALUES(?,?,?,?,?,?)",
                           (property_id, contact["id"] if contact else None, ch, oc, clean_str(notes), now_iso()))
        msg, others = _apply_effect(conn, property_id, spec.effect, contact, cfg, actor)
        for pid in sorted(others - {property_id}):
            _refresh(conn, pid, cfg)
        ev = _refresh(conn, property_id, cfg)
    if spec.effect == "confirm":
        carry_to_siblings(conn, property_id, settings=settings)
    text = f"Logged: {spec.label}. {msg}".strip()
    if spec.effect != "confirm":
        text += f" Next: {ev.next_step['text']}"
    return _result(conn, property_id, text, settings, outreach_id=int(cur.lastrowid), effect=spec.effect,
                   contact_point_id=contact["id"] if contact else None)


def _pick_contact(conn: sqlite3.Connection, property_id: int, channel: str, contact_point_id: int | None) -> dict | None:
    if contact_point_id is not None:
        c = one(conn, "SELECT * FROM contact_points WHERE id=?", (contact_point_id,))
        if not c or int(c["property_id"]) != int(property_id):
            raise ValueError("That contact does not belong to this property.")
        return c
    fam = PHONE_CHANNELS if channel in PHONE_CHANNELS else (channel,)
    cands = [c for c in rows(conn, "SELECT * FROM contact_points WHERE property_id=? AND status='untested'",
                             (property_id,)) if c["channel"] in fam]
    return cands[0] if len(cands) == 1 else None


def _ai_contact_marked(conn: sqlite3.Connection, contact: dict | None, status: str, actor: str | None) -> set[int]:
    """An AI-found contact changed status from a logged reach-out: keep its finding (and its copies on the
    owner's other properties) in step. Returns the other properties changed."""
    if not contact or contact.get("source") != AI_SOURCE:
        return set()
    from . import research   # research imports this module, so import it here
    return research.contact_marked(conn, contact, status, actor)


def _apply_effect(conn: sqlite3.Connection, property_id: int, effect: str, contact: dict | None,
                  cfg: DrillConfig, actor: str | None = None) -> tuple[str, set[int]]:
    """(message, other properties changed) for one logged reach-out."""
    now = now_iso()
    # status!='dnc' below: an outcome can never take a contact off the do-not-contact list.
    if effect == "confirm":
        others: set[int] = set()
        if contact:
            conn.execute("UPDATE contact_points SET status='verified', last_tested_at=? WHERE id=? AND status!='dnc'",
                         (now, contact["id"]))
            others = _ai_contact_marked(conn, contact, "verified", actor)
        name = None
        if contact and contact.get("person_id"):
            name = scalar(conn, "SELECT name FROM people WHERE id=?", (contact["person_id"],))
            if L.looks_like_entity(name):
                name = None  # the owner of record is a company; the person we talked to is the best guess
        who = _confirm(conn, property_id, name)
        return f"Confirmed owner{f' ({who})' if who else ''}: level 3. Hand off to the CRM.", others
    if effect == "mark_wrong":
        if not contact:
            return "Pick which contact was wrong so the drill can skip it.", set()
        conn.execute("UPDATE contact_points SET status='wrong', last_tested_at=? WHERE id=? AND status!='dnc'",
                     (now, contact["id"]))
        others = _ai_contact_marked(conn, contact, "wrong", actor) - {property_id}
        msg = f"{contact['value']} is marked wrong."
        if others:
            msg += (f" Also marked wrong on {len(others)} other propert{'ies' if len(others) > 1 else 'y'} of this "
                    f"owner (the AI research found it for all of them).")
        return msg, others
    if effect == "dnc":
        _set_property_dnc(conn, property_id)
        return _dnc_message(_dnc_siblings(conn, property_id, cfg)), set()
    if contact:
        conn.execute("UPDATE contact_points SET last_tested_at=? WHERE id=?", (now, contact["id"]))
    return "", set()


def level_action(conn: sqlite3.Connection, property_id: int, action: str, person: str | None = None,
                 note: str | None = None, *, settings: Settings | None = None) -> dict:
    """Owner-level actions: owner_found, confirm, dnc, undnc, exhausted, reopen, handoff, note."""
    cfg = drill_config(settings)
    act = (clean_str(action) or "").lower()
    if act not in LEVEL_ACTIONS:
        raise ValueError(f"'{action}' is not an action. Use one of: {', '.join(LEVEL_ACTIONS)}.")
    prop = _require_property(conn, property_id)
    state = one(conn, "SELECT * FROM drill_state WHERE property_id=?", (property_id,)) or {}
    status = state.get("status") or "active"
    with _tx(conn):
        _ensure_state_row(conn, property_id)
        msg, new_info = _do_action(conn, prop, state, act, person, cfg)
        if clean_str(note):
            _append_note(conn, property_id, note)
        _refresh(conn, property_id, cfg, new_info=new_info)
    if act == "confirm" and status != "confirmed":
        carry_to_siblings(conn, property_id, settings=settings)
    return _result(conn, property_id, msg, settings, action=act)


def _do_action(conn: sqlite3.Connection, prop: dict, state: dict, act: str, person: str | None,
               cfg: DrillConfig) -> tuple[str, bool]:
    pid = int(prop["id"])
    status = state.get("status") or "active"
    now = now_iso()
    if act == "owner_found":
        if not clean_str(person) and not _clean_owner(prop.get("owner_name")):
            raise ValueError("Type the owner's name you found.")
        if clean_str(person):
            _add_person(conn, pid, person, role="possible_owner", source="you")
        return "Saved the possible owner (level 2). Only a conversation confirms them.", True
    if act == "confirm":
        if status == "dnc":
            raise ValueError("This owner is marked do-not-contact. Undo that first.")
        who = _confirm(conn, pid, person)
        return f"Confirmed owner{f' ({who})' if who else ''}: level 3. Hand off to the CRM.", False
    if act == "dnc":
        _set_property_dnc(conn, pid)
        return _dnc_message(_dnc_siblings(conn, pid, cfg)) + " This stays until you undo it.", False
    if act == "undnc":
        if status != "dnc":
            return "This owner was not marked do-not-contact.", False
        back = "confirmed" if state.get("confirmed_at") else "active"
        conn.execute("UPDATE drill_state SET status=?, updated_at=? WHERE property_id=?", (back, now, pid))
        return ("Do-not-contact removed on this property only. Its contacts stay marked do-not-contact; use "
                "'Allow contact again' on each one if that was a mistake. The owner's other properties stay "
                "do-not-contact: undo them one at a time."), False
    if act == "exhausted":
        if status in ("confirmed", "dnc"):
            raise ValueError("Only an open owner can be marked exhausted.")
        conn.execute("UPDATE drill_state SET status='exhausted', updated_at=? WHERE property_id=?", (now, pid))
        return "Marked exhausted. Reopen it when something new turns up.", False
    if act == "reopen":
        if status == "dnc":
            raise ValueError("This owner is marked do-not-contact. Use 'undo do-not-contact' instead.")
        if status == "confirmed":
            conn.execute("UPDATE drill_state SET status='active', confirmed_person=NULL, confirmed_at=NULL, "
                         "handed_off_at=NULL, updated_at=? WHERE property_id=?", (now, pid))
            return "Reopened: the confirmation was removed and the drill continues.", False
        conn.execute("UPDATE drill_state SET status='active', updated_at=? WHERE property_id=?", (now, pid))
        ctx = _ctx(conn, pid)
        if evaluate(ctx, cfg).next_step["kind"] != "exhausted":
            return "Reopened. The drill picks up where it left off.", False
        n = pass_number(ctx.attempts) + 1
        _insert_attempt(conn, pid, PASS_MARKER, "skipped", f"Started pass {n}.", "you")
        return f"Reopened for pass {n}: every hands-on stage that found nothing is open again.", False
    if act == "handoff":
        if status != "confirmed":
            raise ValueError("Only a confirmed owner can be handed off to the CRM.")
        conn.execute("UPDATE drill_state SET handed_off_at=?, updated_at=? WHERE property_id=?", (now, now, pid))
        return "Marked as handed off to the CRM.", False
    return "Note saved.", False


def _append_note(conn: sqlite3.Connection, property_id: int, note: str) -> None:
    old = scalar(conn, "SELECT notes FROM drill_state WHERE property_id=?", (property_id,)) or ""
    line = f"[{now_iso()[:10]}] {clean_str(note)}"
    conn.execute("UPDATE drill_state SET notes=?, updated_at=? WHERE property_id=?",
                 ((old + "\n" + line).strip(), now_iso(), property_id))


def mark_handed_off(conn: sqlite3.Connection, property_ids: Iterable[int]) -> int:
    """Stamp handed_off_at on confirmed owners (used by the CRM export). Returns how many were stamped."""
    ids = [int(i) for i in property_ids]
    n = 0
    with _tx(conn):
        for chunk in _chunks(ids):
            marks = ",".join("?" for _ in chunk)
            cur = conn.execute(f"UPDATE drill_state SET handed_off_at=?, updated_at=? WHERE status='confirmed' "
                               f"AND handed_off_at IS NULL AND property_id IN ({marks})", [now_iso(), now_iso(), *chunk])
            n += cur.rowcount
    return n


# --- portfolio siblings -------------------------------------------------------------------------

@dataclass
class SiblingIndex:
    """Which properties share an owner (owners.portfolio_key or normalized owner name) or a mailing address."""
    keys_of: dict[int, set[str]]
    members: dict[str, set[int]]
    props: dict[int, dict]

    def siblings(self, property_id: int) -> set[int]:
        out: set[int] = set()
        for k in self.keys_of.get(property_id, ()):
            out |= self.members.get(k, set())
        out.discard(property_id)
        return out


def build_sibling_index(conn: sqlite3.Connection, settings: Settings | None = None) -> SiblingIndex:
    s = settings or get_settings()
    cap = int((s.section("owners") or {}).get("mailing_group_max_names") or DEFAULT_MAILING_GROUP_MAX)
    props = {r["id"]: r for r in rows(conn, "SELECT p.id, p.market_id, p.address, p.city, p.owner_name, "
                                            "p.mailing_address, p.mailing_unit, p.mailing_city, p.mailing_state, "
                                            "p.mailing_zip, o.portfolio_key FROM properties p "
                                            "LEFT JOIN owners o ON o.property_id=p.id")}
    keys_of: dict[int, set[str]] = defaultdict(set)
    members: dict[str, set[int]] = defaultdict(set)
    mail_names: dict[str, set[str]] = defaultdict(set)
    for pid, p in props.items():
        owner = _clean_owner(p.get("owner_name"))
        name = normalize_owner(owner) if owner else None
        keys = set()
        if clean_str(p.get("portfolio_key")):
            keys.add(f"pk:{p['portfolio_key']}")
        if name:
            keys.add(f"name:{name}")
        mk = _mailing_key(p)
        if mk:
            keys.add(f"mail:{mk}")
            mail_names[mk].add(name or f"#{pid}")
        for k in keys:
            keys_of[pid].add(k)
            members[k].add(pid)
    for mk, names in mail_names.items():
        if len(names) > cap:  # a big management company's address: not one owner
            for pid in members.pop(f"mail:{mk}", set()):
                keys_of[pid].discard(f"mail:{mk}")
    return SiblingIndex(keys_of=dict(keys_of), members=dict(members), props=props)


@dataclass
class CarrySource:
    contacts: dict[int, list[dict]]
    people: dict[int, list[dict]]
    confirmed: dict[int, str]


def _carry_sources(conn: sqlite3.Connection) -> CarrySource:
    """Original (not already carried) contacts, unmasked people and confirmed owners of every property. An AI
    research contact counts only once verified (see AI_SOURCE)."""
    contacts: dict[int, list[dict]] = defaultdict(list)
    people: dict[int, list[dict]] = defaultdict(list)
    confirmed: dict[int, str] = {}
    for c in rows(conn, "SELECT * FROM contact_points WHERE status IN ('verified','untested') AND channel!='mail' "
                        "AND COALESCE(source,'') != ? AND NOT (COALESCE(source,'') = ? AND status != 'verified') "
                        "ORDER BY id", (PORTFOLIO_STAGE, AI_SOURCE)):
        contacts[c["property_id"]].append(c)
    roles = CARRY_ROLES + ("statutory_agent", "confirmed_owner")
    marks = ",".join("?" for _ in roles)
    for p in rows(conn, f"SELECT * FROM people WHERE role IN ({marks}) AND COALESCE(source,'') NOT IN (?, ?) "
                        f"ORDER BY id", (*roles, PORTFOLIO_STAGE, AI_SOURCE)):
        people[p["property_id"]].append(p)
    for r in rows(conn, "SELECT property_id, confirmed_person FROM drill_state WHERE confirmed_at IS NOT NULL "
                        "AND confirmed_person IS NOT NULL"):
        confirmed[r["property_id"]] = r["confirmed_person"]
    return CarrySource(contacts=contacts, people=people, confirmed=confirmed)


def _short_address(p: Mapping[str, Any] | None) -> str:
    if not p:
        return "another property"
    return ", ".join(x for x in (clean_str(p.get("address")), clean_str(p.get("city"))) if x) or f"property {p.get('id')}"


def _carry_into(conn: sqlite3.Connection, target: int, index: SiblingIndex, src: CarrySource,
                dnc: set[tuple[str, str]]) -> dict:
    """Copy contacts and people from a property's siblings. Returns what was added."""
    sibs = sorted(index.siblings(target))
    added_contacts: list[dict] = []
    added_people: list[str] = []
    from_props: set[int] = set()
    me = index.props.get(target) or {}
    my_name = normalize_owner(_clean_owner(me.get("owner_name")) or "") or None
    for sid in sibs:
        sp = index.props.get(sid)
        where = _short_address(sp)
        for c in src.contacts.get(sid, []):
            note = f"From {where} ({c['status']} there)"
            try:
                row, created = _add_contact(conn, target, c["channel"], c["value"], source=PORTFOLIO_STAGE,
                                            notes=note, dnc_keys=dnc)
            except ValueError:
                continue
            if created:
                added_contacts.append(row)
                from_props.add(sid)
        who = src.confirmed.get(sid)
        if who:
            _, created = _add_person(conn, target, who, role="portfolio_owner", source=PORTFOLIO_STAGE,
                                     notes=f"Confirmed owner of {where}")
            if created:
                added_people.append(who)
                from_props.add(sid)
        same_entity = bool(my_name) and normalize_owner(_clean_owner((sp or {}).get("owner_name")) or "") == my_name
        for p in src.people.get(sid, []):
            role = p.get("role")
            if role == "confirmed_owner":
                continue  # carried above as the confirmed person
            if role == "statutory_agent" and not same_entity:
                continue
            _, created = _add_person(conn, target, p["name"], role=role, source=PORTFOLIO_STAGE,
                                     notes=f"{(role or 'person').replace('_', ' ').capitalize()} on {where}")
            if created:
                added_people.append(p["name"])
                from_props.add(sid)
    return {"siblings": len(sibs), "contacts": added_contacts, "people": added_people, "from": sorted(from_props)}


def _carry_detail(res: dict, index: SiblingIndex) -> str:
    parts = []
    if res["contacts"]:
        parts.append(", ".join(f"{CHANNEL_LABELS.get(c['channel'], c['channel'])} {c['value']}" for c in res["contacts"][:4]))
    if res["people"]:
        parts.append("person " + ", ".join(res["people"][:3]))
    where = "; ".join(_short_address(index.props.get(i)) for i in res["from"][:3])
    n = len(res["from"])
    return (f"From {n} other propert{'ies' if n != 1 else 'y'} with the same owner or mailing address "
            f"({where}): {'; '.join(parts)}.")


def _run_portfolio(conn: sqlite3.Connection, ctx: Ctx, index: SiblingIndex, src: CarrySource,
                   dnc: set[tuple[str, str]], has_attempt: bool) -> str:
    """Carry sibling info into one property. Returns found | nothing | unchanged."""
    res = _carry_into(conn, ctx.id, index, src, dnc)
    if res["contacts"] or res["people"]:
        _insert_attempt(conn, ctx.id, PORTFOLIO_STAGE, "found", _carry_detail(res, index), "auto")
        return "found"
    if has_attempt:
        return "unchanged"
    detail = ("No other property shares this owner or mailing address." if not res["siblings"] else
              f"{res['siblings']} other propert{'ies share' if res['siblings'] != 1 else 'y shares'} this owner "
              f"or mailing address, but none has contact info yet.")
    _insert_attempt(conn, ctx.id, PORTFOLIO_STAGE, "nothing", detail, "auto")
    return "nothing"


def carry_to_siblings(conn: sqlite3.Connection, property_id: int, settings: Settings | None = None) -> dict:
    """After a contact is verified or an owner confirmed, copy that to the property's open siblings now."""
    cfg = drill_config(settings)
    stage = cfg.by_key.get(PORTFOLIO_STAGE)
    if stage is None or not stage.enabled:
        return {"siblings": 0, "updated": 0}
    index = build_sibling_index(conn, settings)
    sibs = sorted(index.siblings(property_id))
    if not sibs:
        return {"siblings": 0, "updated": 0}
    updated = 0
    with _tx(conn):
        src = _carry_sources(conn)
        dnc = _dnc_keys(conn)
        for ctx in load_contexts(conn, property_ids=sibs, status=("active", "exhausted")):
            if not stage.applies(ctx.owner_type, ctx.asset_class, ctx.prop.get("state")):
                continue
            has = PORTFOLIO_STAGE in current_latest(ctx.attempts)
            if _run_portfolio(conn, ctx, index, src, dnc, has) == "found":
                updated += 1
                _refresh(conn, ctx.id, cfg, new_info=True)
    return {"siblings": len(sibs), "updated": updated}


# --- automatic stages ---------------------------------------------------------------------------

def _same_owner_written_differently(old_names: list[str], new: str) -> bool:
    """'DOE JOHN' and 'DOE JOHN A' are one owner written two ways, not a sale: every word of the shorter name
    is in the longer one (and they share at least two words)."""
    new_words = set(_name_key(new).split())
    for old in old_names:
        old_words = set(_name_key(old).split())
        short, long_ = sorted((old_words, new_words), key=len)
        if len(short) >= 2 and short <= long_:
            return True
    return False


def _owner_changed(conn: sqlite3.Connection, ctx: Ctx, owner: str) -> None:
    """The owner of record changed (a sale): start a new pass so every stage that found nothing runs again
    for the new owner, and take back a confirmation of the old owner (level 3 means *this* owner confirmed).
    A do-not-contact property stays as it is."""
    st = ctx.state
    if st.get("status") == "dnc":
        return
    n = pass_number(ctx.attempts) + 1
    _insert_attempt(conn, ctx.id, PASS_MARKER, "skipped", f"Started pass {n}: new owner on record ({owner}).", "auto")
    marks = ",".join("?" for _ in OWNER_NAME_ROLES)   # the old owner's names stay on file, but not as the owner
    conn.execute(f"UPDATE people SET role='previous_owner' WHERE property_id=? AND role IN ({marks})",
                 (ctx.id, *OWNER_NAME_ROLES))
    if st.get("confirmed_at"):
        old = clean_str(st.get("confirmed_person")) or "the previous owner"
        conn.execute("UPDATE drill_state SET status='active', confirmed_person=NULL, confirmed_at=NULL, "
                     "handed_off_at=NULL, updated_at=? WHERE property_id=?", (now_iso(), ctx.id))
        _append_note(conn, ctx.id, f"Owner on record changed to {owner}; the confirmation of {old} was removed.")


def _run_records(conn: sqlite3.Connection, ctx: Ctx, dnc: set[tuple[str, str]]) -> str:
    """Owner name and mailing address from the Crexi export. Returns found | nothing | unchanged."""
    latest = current_latest(ctx.attempts).get(RECORDS_STAGE)
    owner = _clean_owner(ctx.prop.get("owner_name"))
    mail = mailing_text(ctx.prop)
    mail_key = _contact_key("mail", mail) if mail else None
    has_mail = mail_key is None or any(c["channel"] == "mail" and _contact_key("mail", c["value"]) == mail_key
                                       for c in ctx.contacts)
    if not owner:
        if latest is not None:
            return "unchanged"
        if mail and not has_mail:
            _add_contact(conn, ctx.id, "mail", mail, source=RECORDS_STAGE, dnc_keys=dnc,
                         notes="Mailing address on record (no owner name): write to 'Owner'.")
        _insert_attempt(conn, ctx.id, RECORDS_STAGE, "nothing", "No owner name in the Crexi export.", "auto")
        return "nothing"
    key = _name_key(owner)
    has_person = any(p.get("role") == "owner_of_record" and _name_key(p["name"]) == key for p in ctx.people)
    if latest is not None and latest["outcome"] == "found" and has_person and has_mail:
        return "unchanged"
    changed = latest is not None and latest["outcome"] == "found" and not has_person
    if changed and not _same_owner_written_differently(
            [p["name"] for p in ctx.people if p.get("role") == "owner_of_record"], owner):
        _owner_changed(conn, ctx, owner)
    person_id, _ = _add_person(conn, ctx.id, owner, role="owner_of_record", source=RECORDS_STAGE)
    if mail and not has_mail:
        care = clean_str(ctx.prop.get("mailing_care_of"))
        _add_contact(conn, ctx.id, "mail", mail, source=RECORDS_STAGE, person_id=person_id, dnc_keys=dnc,
                     notes=f"c/o {care}" if care else None)
    detail = (f"Owner on record {'changed to' if changed else 'is'} {owner}."
              + (f" Mailing address: {mail}." if mail else " No mailing address on record."))
    _insert_attempt(conn, ctx.id, RECORDS_STAGE, "found", detail, "auto")
    return "found"


def run_auto_stages(conn: sqlite3.Connection, market_slug: str, progress: Progress = None, *,
                    settings: Settings | None = None) -> dict:
    """Run the automatic stages for every property in a market: records baseline, then portfolio match.

    Safe to re-run: nothing is recorded twice unless something new turned up (a new owner name from a
    re-import, or a sibling property gained a contact or a confirmed owner).
    """
    cfg = drill_config(settings)
    market = market_by_slug(conn, market_slug)
    ensure_states(conn, market_slug)
    counts = {RECORDS_STAGE: {"found": 0, "nothing": 0, "unchanged": 0, "not_applicable": 0},
              PORTFOLIO_STAGE: {"found": 0, "nothing": 0, "unchanged": 0, "not_applicable": 0}}
    _tell(progress, 0.0, "Reading owner names from the Crexi export")
    records = cfg.by_key.get(RECORDS_STAGE)
    found_ids: set[int] = set()   # properties that learned something new (reopens exhausted ones)
    with _tx(conn):
        dnc = _dnc_keys(conn)
        ctxs = load_contexts(conn, market_id=market["id"])
        for i, ctx in enumerate(ctxs):
            if records is None or not records.enabled or not records.applies(ctx.owner_type, ctx.asset_class,
                                                                             ctx.prop.get("state")):
                counts[RECORDS_STAGE]["not_applicable"] += 1
                continue
            res = _run_records(conn, ctx, dnc)
            counts[RECORDS_STAGE][res] += 1
            if res == "found":
                found_ids.add(ctx.id)
            if i % 500 == 0:
                _tell(progress, 0.4 * i / max(1, len(ctxs)), f"Records baseline {i:,} of {len(ctxs):,}")
    _tell(progress, 0.4, "Matching properties that share an owner or mailing address")
    portfolio = cfg.by_key.get(PORTFOLIO_STAGE)
    with _tx(conn):
        index = build_sibling_index(conn, settings)
        src = _carry_sources(conn)
        dnc = _dnc_keys(conn)
        ctxs = load_contexts(conn, market_id=market["id"])
        for ctx in ctxs:
            status = ctx.state.get("status") or "active"
            if (portfolio is None or not portfolio.enabled or status in ("dnc", "confirmed")
                    or not portfolio.applies(ctx.owner_type, ctx.asset_class, ctx.prop.get("state"))):
                counts[PORTFOLIO_STAGE]["not_applicable"] += 1
                continue
            has = PORTFOLIO_STAGE in current_latest(ctx.attempts)
            res = _run_portfolio(conn, ctx, index, src, dnc, has)
            counts[PORTFOLIO_STAGE][res] += 1
            if res == "found":
                found_ids.add(ctx.id)
    _tell(progress, 0.8, "Updating contact levels")
    with _tx(conn):
        for ctx in load_contexts(conn, market_id=market["id"]):
            _save_state(conn, ctx, evaluate(ctx, cfg, new_info=ctx.id in found_ids))
    levels = _level_counts(conn, market["id"])
    _tell(progress, 1.0, "DRILL automatic steps done")
    result = {"market": market_slug, "properties": len(ctxs), "stages": counts, "levels": levels}
    result["message"] = _auto_message(result)
    return result


def _tell(progress: Progress, fraction: float, message: str) -> None:
    if progress is None:
        return
    try:
        progress(max(0.0, min(1.0, fraction)), message)
    except Exception:  # a broken progress callback must never stop the drill
        log.debug("progress callback failed", exc_info=True)


def _auto_message(r: dict) -> str:
    if not r["properties"]:
        return "No properties in this market yet. Import a Crexi export first."
    rb, pm, lv = r["stages"][RECORDS_STAGE], r["stages"][PORTFOLIO_STAGE], r["levels"]
    parts = [f"Checked {r['properties']:,} properties."]
    if rb["found"] or rb["nothing"]:
        parts.append(f"Records baseline: {rb['found']:,} owner names found, {rb['nothing']:,} with none.")
    if pm["found"]:
        parts.append(f"Portfolio match carried contacts or people to {pm['found']:,} properties.")
    parts.append(f"Levels now: {lv.get(1, 0):,} no owner data, {lv.get(2, 0):,} possible owner, "
                 f"{lv.get(3, 0):,} confirmed.")
    return " ".join(parts)


def _level_counts(conn: sqlite3.Connection, market_id: int) -> dict[int, int]:
    out = {1: 0, 2: 0, 3: 0}
    for r in rows(conn, "SELECT d.level, COUNT(*) AS n FROM drill_state d JOIN properties p ON p.id=d.property_id "
                        "WHERE p.market_id=? GROUP BY d.level", (market_id,)):
        out[int(r["level"])] = int(r["n"])
    return out


# --- reading ------------------------------------------------------------------------------------

def _outcome_options(cfg: DrillConfig) -> list[dict]:
    return [{"key": k, "label": v.label, "effect": v.effect} for k, v in cfg.validate.outcomes.items()]


def _contacts_view(ctx: Ctx, cfg: DrillConfig, to_validate: list[dict]) -> list[dict]:
    tries = _contact_tries(ctx, cfg)
    names = {p["id"]: p["name"] for p in ctx.people}
    need = {c["id"] for c in to_validate}
    out = []
    for c in ctx.contacts:
        t = tries.get(c["id"], {})
        out.append({**c, "person_name": names.get(c.get("person_id")), "tries": t.get("tries", 0),
                    "last_outcome": t.get("last_outcome"), "needs_validation": c["id"] in need})
    return out


def property_drill(conn: sqlite3.Connection, property_id: int, *, settings: Settings | None = None,
                   with_links: bool = True) -> dict:
    """Everything the DRILL knows about one property, with the next step."""
    cfg = drill_config(settings)
    ctx = _ctx(conn, property_id)
    ev = evaluate(ctx, cfg)
    if ctx.state.get("_missing") or ev.status != ctx.state.get("status") or ev.level != ctx.state.get("level") \
            or ev.best_guess != ctx.state.get("best_guess_person"):
        with _tx(conn):
            _save_state(conn, ctx, ev)
    view = _drill_view(ctx, ev, cfg, settings, with_links)
    from . import research   # research imports this module, so import it here
    view["research"] = research.property_research(conn, property_id)
    if with_links:   # the owner's other properties, which do-not-contact would also cover
        sibs = owner_sibling_ids(conn, property_id)
        view["siblings"] = len(sibs)
        view["sibling_addresses"] = [_short_address(p) for p in rows(
            conn, f"SELECT id, address, city FROM properties WHERE id IN ({','.join('?' for _ in sibs[:5])}) "
                  "ORDER BY id", sibs[:5])] if sibs else []
    return view


def _drill_view(ctx: Ctx, ev: Evaluation, cfg: DrillConfig, settings: Settings | None, with_links: bool) -> dict:
    labels = {**RETIRED_OUTCOME_LABELS, **{k: v.label for k, v in cfg.validate.outcomes.items()}}
    stages = []
    for st in ev.stages:
        item = {k: st[k] for k in ("key", "name", "goal", "automation", "cost", "method", "hit_rate", "status",
                                   "attempts", "enabled", "moved", "moved_up", "background")}
        item["links"] = (L.links_for(ctx.prop, st["key"], ev.best_guess, settings=settings, parse_owner=False)
                         if with_links and st["status"] != "disabled" else [])
        stages.append(item)
    st = ctx.state
    return {
        "property_id": ctx.id,
        "level": ev.level,
        "level_name": cfg.levels.get(ev.level, str(ev.level)),
        "status": ev.status,
        "owner_name": ctx.prop.get("owner_name"),
        "owner_type": ctx.owner_type,
        "best_guess_person": ev.best_guess,
        "confirmed_person": st.get("confirmed_person"),
        "confirmed_at": st.get("confirmed_at"),
        "handed_off_at": st.get("handed_off_at"),
        "notes": st.get("notes"),
        "stages": stages,
        "next_step": ev.next_step,
        "people": list(ctx.people),
        "contacts": _contacts_view(ctx, cfg, ev.to_validate),
        "channels": channel_summary(ctx.contacts),
        "outreach": [{**o, "outcome_label": labels.get(o["outcome"], o["outcome"])} for o in reversed(ctx.outreach)],
        "credit_used": bool(ctx.credits),
        "credits": list(ctx.credits),
        "pass": pass_number(ctx.attempts),
        "outcome_options": _outcome_options(cfg),
        "levels": cfg.levels,
    }


def summaries(conn: sqlite3.Connection, market_slug: str | None = None, property_ids: Iterable[int] | None = None,
              *, settings: Settings | None = None) -> dict[int, dict]:
    """Fast per-property DRILL summary for lists and exports:
    {id: {level, level_name, status, best_guess_person, next_step, channels, owner_type, credit_used}}."""
    cfg = drill_config(settings)
    market_id = market_by_slug(conn, market_slug)["id"] if market_slug else None
    out = {}
    for ctx in load_contexts(conn, market_id=market_id, property_ids=property_ids):
        ev = evaluate(ctx, cfg)
        out[ctx.id] = {"level": ev.level, "level_name": cfg.levels.get(ev.level, str(ev.level)), "status": ev.status,
                       "best_guess_person": ev.best_guess, "confirmed_person": ctx.state.get("confirmed_person"),
                       "next_step": ev.next_step, "next_step_text": ev.next_step["text"],
                       "channels": channel_summary(ctx.contacts), "owner_type": ctx.owner_type,
                       "credit_used": bool(ctx.credits)}
    return out


def _sort_key(ctx: Ctx) -> tuple:
    score = ctx.score_value
    group = 0 if ctx.in_buybox else (1 if score is not None else 2)
    rank = (ctx.score or {}).get("rank")
    return (group, -(score or 0.0), rank if rank is not None else float("inf"), ctx.id)


def _basics(ctx: Ctx) -> dict:
    p = ctx.prop
    keys = ("address", "unit", "city", "state", "zip", "county", "apn", "name", "property_type", "units",
            "building_sqft", "lot_acres", "year_built", "lat", "lng", "crexi_url", "owner_name", "mailing_address",
            "mailing_city", "mailing_state", "mailing_zip")
    return {"property_id": ctx.id, **{k: p.get(k) for k in keys}, "owner_type": ctx.owner_type,
            "score": ctx.score_value, "rank": (ctx.score or {}).get("rank"), "in_buybox": ctx.in_buybox}


def _queue_item(ctx: Ctx, ev: Evaluation, cfg: DrillConfig, stage_key: str, settings: Settings | None) -> dict:
    names = {s.key: s.name for s in cfg.stages}
    history = [{"stage_key": a["stage_key"], "stage_name": names.get(a["stage_key"], a["stage_key"]),
                "outcome": a["outcome"], "detail": a["detail"], "created_at": a["created_at"]}
               for a in reversed(ctx.attempts[-6:])]
    if stage_key == "validate":   # reaching out: check phone numbers against the Do Not Call list first
        links = [link for link in [L.source_link(ctx.prop, "national_dnc", settings=settings)] if link]
    else:
        links = L.links_for(ctx.prop, stage_key, ev.best_guess, settings=settings, parse_owner=False)
    return {**_basics(ctx), "level": ev.level, "level_name": cfg.levels.get(ev.level, str(ev.level)),
            "status": ev.status, "best_guess_person": ev.best_guess, "next_step": ev.next_step,
            "links": links,
            "property_links": L.links_for(ctx.prop, None, ev.best_guess, settings=settings, parse_owner=False),
            "contacts": _contacts_view(ctx, cfg, ev.to_validate), "people": list(ctx.people),
            "channels": channel_summary(ctx.contacts), "history": history}


SCOPES = ("all", "buybox")


def _check_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope '{scope}'. Use one of: {', '.join(SCOPES)}.")
    return scope


def _has_scores(conn: sqlite3.Connection, market_id: int) -> bool:
    return bool(scalar(conn, "SELECT 1 FROM scores s JOIN properties p ON p.id = s.property_id "
                             "WHERE p.market_id = ? LIMIT 1", (market_id,)))


def _queue(conn: sqlite3.Connection, market_slug: str, want: Callable[[Evaluation], bool], cfg: DrillConfig,
           scope: str = "all") -> tuple[list[tuple[Ctx, Evaluation]], int]:
    """(owners waiting in this scope, best first; how many more wait outside the scope)."""
    market = market_by_slug(conn, market_slug)
    ensure_states(conn, market_slug)
    # Buy-box scope only loads buy-box owners (fast on big lists); set-aside ones are not counted then.
    buybox = scope == "buybox" and _has_scores(conn, market["id"])
    picked = []
    for ctx in load_contexts(conn, market_id=market["id"], status="active", buybox_only=buybox):
        ev = evaluate(ctx, cfg)
        if ev.status == "active" and want(ev):
            picked.append((ctx, ev))
    picked.sort(key=lambda t: _sort_key(t[0]))
    outside = scalar(conn, "SELECT COUNT(*) FROM properties p JOIN scores sc ON sc.property_id = p.id "
                           "LEFT JOIN drill_state d ON d.property_id = p.id WHERE p.market_id = ? "
                           "AND sc.in_buybox = 0 AND COALESCE(d.status, 'active') = 'active'",
                     (market["id"],)) if buybox else 0
    return picked, int(outside or 0)


def _outside_note(outside: int) -> str:
    if not outside:
        return ""
    return (f" Owners of {outside:,} set-aside {'property are' if outside == 1 else 'properties are'} not in this "
            f"queue (switch the DRILL board to everyone to include them).")


def stage_queue(conn: sqlite3.Connection, market_slug: str, stage_key: str, limit: int = 50, offset: int = 0, *,
                settings: Settings | None = None, scope: str = "all") -> dict:
    """Open properties whose next step is this stage, best Blue Moon score first, with links filled in.

    scope 'buybox' keeps only buy-box owners (once the market is scored); 'all' keeps everyone."""
    _check_scope(scope)
    if stage_key == "validate":
        return validate_queue(conn, market_slug, limit=limit, offset=offset, settings=settings, scope=scope)
    cfg = drill_config(settings)
    stage = cfg.stage(stage_key)
    picked, outside = _queue(conn, market_slug, lambda ev: ev.next_step["kind"] == "stage"
                             and ev.next_step["stage_key"] == stage_key, cfg, scope)
    limit, offset = max(0, int(limit)), max(0, int(offset))
    items = [_queue_item(c, e, cfg, stage_key, settings) for c, e in picked[offset:offset + limit]]
    return {"market": market_slug, "stage": {**stage.public(), "outcomes": [
                {"key": k, "label": v} for k, v in OUTCOME_LABELS.items()]},
            "scope": scope, "total": len(picked), "outside_scope": outside,
            "limit": limit, "offset": offset, "items": items,
            "message": (f"{len(picked):,} owners are waiting on {stage.name}." if picked
                        else f"Nobody is waiting on {stage.name} right now.") + _outside_note(outside)}


def waiting_counts(conn: sqlite3.Connection, market_slug: str, *, settings: Settings | None = None) -> dict:
    """Hands-on DRILL work waiting for buy-box owners: {total, validate, stages:{stage_key: n}}.

    Counts open buy-box owners whose next step is to reach out on untested contact info, or a stage
    Seth works by hand (assisted or manual, switched on). Automatic stages run by themselves and are not
    counted. When nothing has been scored yet every property counts, like penetration does. Only the
    buy-box owners are evaluated, so this stays fast enough for the top bar."""
    cfg = drill_config(settings)
    market = market_by_slug(conn, market_slug)
    hands_on = {st.key for st in cfg.stages if st.enabled and st.automation != "auto"}
    scored = _has_scores(conn, market["id"])
    ctxs = load_contexts(conn, market_id=market["id"], status="active", buybox_only=scored)
    stages: dict[str, int] = defaultdict(int)
    validate = 0
    for ctx in ctxs:
        ev = evaluate(ctx, cfg)
        if ev.status != "active":
            continue
        kind = ev.next_step.get("kind")
        if kind == "validate":
            validate += 1
        elif kind == "stage" and ev.next_step.get("stage_key") in hands_on:
            stages[ev.next_step["stage_key"]] += 1
    return {"total": validate + sum(stages.values()), "validate": validate, "stages": dict(stages),
            "basis": "in_buybox" if scored else "all"}


def validate_queue(conn: sqlite3.Connection, market_slug: str, limit: int = 50, offset: int = 0, *,
                   settings: Settings | None = None, scope: str = "all") -> dict:
    """Open properties with untested phone/text/email/social/voicemail contacts, best score first."""
    _check_scope(scope)
    cfg = drill_config(settings)
    picked, outside = _queue(conn, market_slug, lambda ev: ev.next_step["kind"] == "validate", cfg, scope)
    limit, offset = max(0, int(limit)), max(0, int(offset))
    items = []
    for ctx, ev in picked[offset:offset + limit]:
        item = _queue_item(ctx, ev, cfg, "validate", settings)
        item["to_validate"] = [c for c in item["contacts"] if c["needs_validation"]]
        items.append(item)
    stage = {"key": "validate", "name": cfg.validate.name, "goal": "validate", "automation": "manual",
             "cost": "free", "method": "Reach out on the untested contact and log what happened.",
             "channels": cfg.validate.channels, "outcomes": _outcome_options(cfg),
             "tries_before_moving_on": cfg.validate.tries_before_moving_on}
    return {"market": market_slug, "stage": stage, "scope": scope, "total": len(picked), "outside_scope": outside,
            "limit": limit, "offset": offset, "items": items,
            "message": (f"{len(picked):,} owners have contact info to test." if picked
                        else "No untested contact info right now.") + _outside_note(outside)}


def _pipeline(ctxs: list[tuple[Ctx, Evaluation]], cfg: DrillConfig) -> list[dict]:
    rows_out = []
    latest_of = {ctx.id: _latest_by_stage(ctx.attempts) for ctx, _ in ctxs}
    for st in cfg.stages:
        reached = resolved = tried = skipped = queue = 0
        for ctx, ev in ctxs:
            latest = latest_of[ctx.id].get(st.key)
            is_next = ev.status == "active" and ev.next_step.get("stage_key") == st.key
            if latest is None and not is_next:
                continue
            reached += 1
            queue += int(is_next)
            if latest is not None:
                if latest["outcome"] == "found":
                    resolved += 1
                if latest["outcome"] in ("found", "nothing"):
                    tried += 1
                if latest["outcome"] == "skipped":
                    skipped += 1
        rows_out.append({"stage_key": st.key, "name": st.name, "goal": st.goal, "automation": st.automation,
                         "cost": st.cost, "enabled": st.enabled, "background": st.background,
                         "input": reached, "resolved": resolved,
                         "unresolved": reached - resolved, "tried": tried, "skipped": skipped, "queue": queue,
                         "hit_rate_actual": round(resolved / tried, 4) if tried else None,
                         "hit_rate_hypothesis": st.hit_rate})
    return rows_out


def overview(conn: sqlite3.Connection, market_slug: str, *, settings: Settings | None = None,
             scope: str = "all") -> dict:
    """The DRILL board: levels, statuses, penetration, the stage pipeline, queues and credits.

    scope 'buybox' counts only buy-box owners (once the market is scored); 'all' counts everyone."""
    from .credits import credit_status
    _check_scope(scope)
    cfg = drill_config(settings)
    market = market_by_slug(conn, market_slug)
    ensure_states(conn, market_slug)
    has_scores = _has_scores(conn, market["id"])
    buybox = scope == "buybox" and has_scores
    pairs = [(ctx, evaluate(ctx, cfg)) for ctx in load_contexts(conn, market_id=market["id"], buybox_only=buybox)]
    every_n = int(scalar(conn, "SELECT COUNT(*) FROM properties WHERE market_id = ?", (market["id"],)) or 0) \
        if buybox else len(pairs)
    levels = {1: 0, 2: 0, 3: 0}
    levels_box = {1: 0, 2: 0, 3: 0}
    statuses = {s: 0 for s in STATUSES}
    next_counts: dict[str, int] = defaultdict(int)
    for ctx, ev in pairs:
        levels[ev.level] += 1
        statuses[ev.status] += 1
        if ctx.in_buybox:
            levels_box[ev.level] += 1
        key = ev.next_step["stage_key"] if ev.next_step["kind"] == "stage" else ev.next_step["kind"]
        next_counts[key] += 1
    # Every owner counts except do-not-contact ones: marking an owner exhausted never raises penetration.
    # Not scored yet counts as in the buy box (missing data never excludes), the same rule as the summary.
    target = [(c, e) for c, e in pairs if e.status != "dnc" and c.in_buybox is not False]
    confirmed = sum(1 for _, e in target if e.level == 3)
    tried_all = sum(1 for _, e in target if e.status == "exhausted")
    pen = round(confirmed / len(target), 4) if target else 0.0
    queues = [{"key": st.key, "name": st.name, "count": next_counts.get(st.key, 0), "automation": st.automation,
               "cost": st.cost, "enabled": st.enabled} for st in cfg.stages]
    queues.insert(0, {"key": "validate", "name": cfg.validate.name, "count": next_counts.get("validate", 0),
                      "automation": "manual", "cost": "free", "enabled": True})
    contact_status = {s: 0 for s in CONTACT_STATUSES}
    for ctx, _ in pairs:
        for c in ctx.contacts:
            if c["channel"] in REACH_CHANNELS:
                contact_status[c["status"]] = contact_status.get(c["status"], 0) + 1
    totals = {"properties": len(pairs), "in_buybox": sum(1 for c, _ in pairs if c.in_buybox),
              "scored": sum(1 for c, _ in pairs if c.score is not None),
              "attempts": sum(len(c.attempts) for c, _ in pairs), "people": sum(len(c.people) for c, _ in pairs),
              "contacts": contact_status, "outreach": sum(len(c.outreach) for c, _ in pairs),
              "handoff_waiting": sum(1 for c, e in pairs if e.status == "confirmed" and not c.state.get("handed_off_at")),
              "all_properties": every_n, "outside_scope": every_n - len(pairs)}
    result = {
        "market": market_slug,
        "scope": scope if has_scores else "all",
        "levels": levels,
        "levels_in_buybox": levels_box,
        "level_names": cfg.levels,
        "statuses": statuses,
        "status_counts": statuses,
        "penetration": pen,
        "penetration_detail": {"confirmed": confirmed, "of": len(target),
                               "basis": "in_buybox" if has_scores else "all",
                               "text": (f"{confirmed:,} of {len(target):,} "
                                        f"{'buy-box ' if has_scores else ''}owners confirmed"
                                        + (f", {tried_all:,} with every source tried" if tried_all else ""))},
        "pipeline": _pipeline(pairs, cfg),
        "validate": {"waiting": next_counts.get("validate", 0), **contact_status},
        "next_steps": dict(next_counts),
        "queues": queues,
        "credits": credit_status(conn, settings=settings),
        "totals": totals,
    }
    result["message"] = (f"{pen:.0%} penetration: {result['penetration_detail']['text']}. "
                         f"{next_counts.get('validate', 0):,} have contact info to test.") if every_n else \
        "No properties in this market yet. Import a Crexi export first."
    return result
