"""AI web research: Claude Code searches the web for each owner, on your Claude plan (the ai_research stage).

One Claude call per owner name: the properties with that name share one answer (never the properties of other
owners at the same mailing address, even when they share a portfolio), buy-box owners first. Claude may only
search the web and read pages. People-search, background-check and data-broker sites, and Crexi, are blocked twice:
  1. in Claude Code itself: page reads have a deny rule per site (the site and every subdomain), archive,
     translation and page-reader sites can't be read at all (they would show a blocked site's pages), and every
     web search gets those sites added to its blocked_domains by a hook (websearch_guard.py), and
  2. here, after the answer comes back: every finding needs the exact link where it was seen, and a finding whose
     link (or value) is on one of those sites, carries one's address (an archive or translation copy), or isn't
     a plain web link at all, is dropped. Claude's free text (summary, reasons) is never kept when anything was
     dropped or it names a blocked site, and phone numbers and emails are taken out of it. This second check is
     the real guarantee; the prompt's own rules are only a first layer.
What passes is saved "not tested yet" (the yellow badge): people with source 'ai_research', contacts with the link
in their notes. Nothing here confirms an owner (level 3 needs a conversation), and a registered-agent company's
phone or email is never saved as a contact. Findings below `min_confidence` are kept only as leads.

    research_owners(conn, slug, dry_run=True)      # the estimate: owners, properties covered, minutes
    research_owners(conn, slug, limit=5)           # Test on 5 = 5 owners
    research_owners(conn, slug, confirm=True)      # a run covering more than 50 properties needs the OK

Answers are cached per owner (model, prompt version, owner name, state, mailing city) for
`refresh_after_days`, "nothing found" included, so re-runs don't ask again. Errors are never cached.
Settings: the `ai_research:` block in config/drill.yaml.
"""
from __future__ import annotations

import json
import logging
import math
import re
import shlex
import sqlite3
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..cache import cache_put, make_key, record_call
from ..config import Settings, get_settings
from ..db import dumps, loads, market_by_slug, now_iso, one, rows
from ..enrich.owners import OWNER_TYPE_LABELS
from ..imagery.vision import (
    _UNKNOWN_OPTION,
    ClaudeCLIBackend,
    ClaudeCLIConfig,
    VisionCallError,
    VisionError,
    VisionFatalError,
    VisionParseError,
    extract_json_object,
)
from ..util import clean_str, normalize_owner
from . import engine as E
from . import links as L

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

STAGE = "ai_research"
SOURCE = E.AI_SOURCE          # contact_points.source and people.source of everything research adds
ACTOR = "ai"                  # drill_attempts.actor of research check-offs
PROMPT_VERSION = "1"          # part of the cache key: bump it to ask every owner again
CACHE_PROVIDER = "research:claude_cli"
CONFIDENCES = ("high", "medium", "low")
FINDING_STATUSES = ("kept", "lead", "blocked", "rejected", "wrong")
KINDS = ("entity", "person", "website", "phone", "email", "social")
RETRY_NOTE = "\n\nYour last reply couldn't be read. Reply with ONLY the JSON object the schema asks for."
UPDATE_CLAUDE = ("This Claude Code is too old for AI research (it doesn't know {flag}, one of the safety settings "
                 "the research needs). Update Claude Code: run claude update in Terminal, then try again.")

# Always blocked, whatever config/drill.yaml says (it can only add more): the best-known people-search,
# background-check and data-broker sites, and Crexi (never automated).
DEFAULT_BLOCKED = (
    "crexi.com", "whitepages.com", "411.com", "anywho.com", "spokeo.com", "truepeoplesearch.com",
    "fastpeoplesearch.com", "fastbackgroundcheck.com", "beenverified.com", "peoplelooker.com", "peoplesmart.com",
    "intelius.com", "instantcheckmate.com", "truthfinder.com", "ussearch.com", "zabasearch.com", "peoplefinders.com",
    "radaris.com", "mylife.com", "usphonebook.com", "thatsthem.com", "nuwber.com", "clustrmaps.com",
    "searchpeoplefree.com", "cyberbackgroundchecks.com", "advancedbackgroundchecks.com", "familytreenow.com",
    "peekyou.com", "pipl.com", "checkpeople.com", "voterrecords.com", "officialusa.com", "zoominfo.com",
)
# Sites that show other sites' pages (archives, translators, page readers). Claude Code may not read them at all:
# a blocked site's page read through one of them would get past that site's own deny rule.
READ_THROUGH_SITES = ("archive.org", "archive.ph", "archive.today", "archive.is", "archive.li", "archive.md",
                      "translate.goog", "r.jina.ai")
WEBSEARCH_GUARD = Path(__file__).with_name("websearch_guard.py")   # the PreToolUse hook on every web search
DEFAULT_AGENT_SERVICES = (
    "CT Corporation", "Corporation Service Company", "CSC", "Northwest Registered Agent", "Registered Agents Inc",
    "InCorp Services", "Cogency Global", "National Registered Agents", "Harbor Compliance", "LegalZoom",
    "ZenBusiness", "Statutory Agent Corp",
)
# The AI's role words -> people.role. Never possible_owner or confirmed_owner: only a conversation confirms.
# An organizer often is the lawyer who filed the LLC, so it stays 'other' (it never becomes the best guess).
ROLE_MAP = {"member": "member", "manager_of_llc": "officer", "officer": "officer", "trustee": "trustee",
            "organizer": "other", "registered_agent": "agent", "property_manager": "manager", "other": "other"}
ROLE_WORDS = {"member": "Member", "manager_of_llc": "LLC manager", "officer": "Officer", "trustee": "Trustee",
              "organizer": "Organizer (filed the paperwork; may be a lawyer)", "registered_agent": "Statutory agent",
              "property_manager": "Property manager", "other": "Named in a filing"}
CHANNEL_WORDS = {"phone": "phone", "email": "email", "social": "social page"}
# The label a finding from a blocked site keeps (nothing Claude wrote about it is stored).
KIND_WORDS = {"entity": "Business filing", "person": "Person", "website": "Website", "phone": "Phone",
              "email": "Email", "social": "Social page", "contact": "Contact"}


# --- settings -----------------------------------------------------------------------------------

def normalize_domain(value: Any) -> str | None:
    """'https://www.Spokeo.com/x', '*.spokeo.com', 'spokeo.com' -> 'spokeo.com'."""
    d = (clean_str(value) or "").lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split(":")[0].strip(".")
    d = re.sub(r"^(\*\.|www\.)", "", d)
    return d if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d) else None


class ResearchConfig(BaseModel):
    """`ai_research:` in config/drill.yaml. Missing keys use these defaults."""
    model_config = ConfigDict(extra="ignore")
    backend: str = "claude_cli"            # claude_cli | mock
    command: str = "claude"                # from config.yaml vision.claude_cli.command
    model: str = "sonnet"
    timeout_seconds: float = Field(420, gt=0)
    max_turns: int = Field(20, ge=1)
    pause_seconds: float = Field(5, ge=0)
    rate_limit_wait_seconds: float = Field(60, ge=0)
    max_calls_per_run: int = Field(100, ge=0)
    max_consecutive_errors: int = Field(3, ge=1)
    min_confidence: Literal["high", "medium", "low"] = "medium"
    scope: Literal["buybox", "all"] = "buybox"
    refresh_after_days: float = Field(180, ge=0)
    est_seconds_per_owner: float = Field(120, gt=0)
    max_people: int = Field(6, ge=0)
    max_contacts: int = Field(8, ge=0)
    blocked_domains: list[str] = Field(default_factory=list, validate_default=True)   # the defaults always apply
    registered_agent_services: list[str] = Field(default_factory=lambda: list(DEFAULT_AGENT_SERVICES))
    confirm_over_properties: int = Field(50, ge=0)

    @field_validator("blocked_domains", mode="after")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        out = [d for d in (normalize_domain(x) for x in [*DEFAULT_BLOCKED, *(v or [])]) if d]
        return list(dict.fromkeys(out))

    def cli_config(self) -> ClaudeCLIConfig:
        return ClaudeCLIConfig(command=self.command, model=self.model, timeout_seconds=self.timeout_seconds,
                               pause_seconds=self.pause_seconds, max_turns=self.max_turns,
                               rate_limit_wait_seconds=self.rate_limit_wait_seconds)


def research_config(settings: Settings | None = None) -> ResearchConfig:
    s = settings or get_settings()
    raw = {k: v for k, v in dict((s.drill or {}).get("ai_research") or {}).items() if v is not None}
    raw["confirm_over_properties"] = s.section("safety").get("confirm_over_properties", 50)
    raw.setdefault("command", (s.section("vision").get("claude_cli") or {}).get("command") or "claude")
    try:
        return ResearchConfig(**raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"config/drill.yaml ai_research setting '{where}' has a value I can't use: "
                         f"{first.get('msg')}") from None


# --- blocked sites ------------------------------------------------------------------------------

def url_host(url: Any) -> str | None:
    """The host of an http(s) link ('www.example.com'), or None when it isn't one. A link whose site part a
    browser could read differently from Python (a backslash, '@', '%' or a non-ASCII letter in it) isn't one."""
    text = clean_str(url)
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not parts.netloc.isascii() or any(c in parts.netloc for c in "\\@%"):
        return None
    host = (parts.hostname or "").strip(".").lower()
    if not re.fullmatch(r"[a-z0-9_.:-]+", host):
        return None
    try:
        host.encode("idna")
    except UnicodeError:
        return None
    return host


def _as_browser_reads(text: str) -> str:
    """Text with percent codes decoded (a few times over), full-width letters made plain, and lower case: how
    a browser reads the site names in a link."""
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return unicodedata.normalize("NFKC", text).replace("\u3002", ".").lower()   # U+3002 counts as a dot


@lru_cache(maxsize=8)
def _sites_pattern(domains: tuple[str, ...]) -> re.Pattern:
    alts = "|".join(re.escape(d) for d in sorted(domains, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9-])({alts})(?![a-z0-9-])")


def named_site(text: Any, domains: list[str]) -> str | None:
    """The first blocked site whose address appears anywhere in a link or a piece of text, or None
    ('https://web.archive.org/web/2024/https://www.spokeo.com/x' -> 'spokeo.com')."""
    t = clean_str(text)
    if not t or not domains:
        return None
    m = _sites_pattern(tuple(domains)).search(_as_browser_reads(t))
    return m.group(1) if m else None


def _translated_host(host: str | None) -> str | None:
    """'www-spokeo-com.translate.goog' (a Google Translate copy of a page) -> 'www.spokeo.com'."""
    if not host or not host.endswith(".translate.goog"):
        return None
    return host.removesuffix(".translate.goog").replace("--", "\0").replace("-", ".").replace("\0", "-")


def blocked_by(url: Any, domains: list[str]) -> str | None:
    """The blocked site a link is on or carries, or None: the site itself or any subdomain, a copy of one of its
    pages on another site (an archive, translation or page-reader link with the blocked site's address in it), or
    a link written so a browser reads it as the blocked site."""
    if not clean_str(url):
        return None
    host = url_host(url)
    for h in (host, _translated_host(host)):
        hit = next((d for d in domains if h and (h == d or h.endswith("." + d))), None)
        if hit:
            return hit
    return named_site(url, domains)


def deny_rules(domains: list[str]) -> list[str]:
    """Claude Code deny rules: 'WebFetch(domain:x.com)' covers only x.com, so each site also gets '*.x.com'.
    The archive, translation and page-reader sites are always denied too."""
    sites = list(dict.fromkeys([*domains, *READ_THROUGH_SITES]))
    return [rule for d in sites for rule in (f"WebFetch(domain:{d})", f"WebFetch(domain:*.{d})")]


def websearch_settings(domains: list[str]) -> dict:
    """Claude Code settings (--settings) with the hook that adds the blocked sites to every web search."""
    command = shlex.join([sys.executable, "-I", str(WEBSEARCH_GUARD), json.dumps(list(domains))])
    return {"hooks": {"PreToolUse": [{"matcher": "WebSearch",
                                      "hooks": [{"type": "command", "command": command, "timeout": 30}]}]}}


_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_IN_TEXT = re.compile(r"(?<![\d])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")


def safe_text(text: str, cfg: ResearchConfig, blocked: int = 0) -> str:
    """Claude's free text (summary, not-found reason, match reason, label) as it may be stored and shown: nothing
    when anything in the answer came from a blocked site or the text names one, and never a phone number or
    email address (those belong in contacts, with the page they were seen on)."""
    if not text or blocked or named_site(text, cfg.blocked_domains):
        return ""
    return _PHONE_IN_TEXT.sub("[phone removed]", _EMAIL_IN_TEXT.sub("[email removed]", text))


# --- the answer ---------------------------------------------------------------------------------

def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


class _Found(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source_url: str = ""
    confidence: str = "low"
    match_reason: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _strings(cls, v: Any) -> Any:
        return _text(v) if v is None or isinstance(v, (str, int, float)) else v

    @field_validator("confidence", mode="after")
    @classmethod
    def _confidence(cls, v: str) -> str:
        v = v.lower()
        return v if v in CONFIDENCES else "low"


class FoundEntity(_Found):
    name: str = ""
    state: str = ""
    status: str = ""
    filing_id: str = ""


class FoundPerson(_Found):
    name: str = ""
    role: str = "other"


class FoundWebsite(_Found):
    url: str = ""


class FoundContact(_Found):
    channel: str = ""
    value: str = ""
    label: str = ""
    belongs_to: str = "owner"           # owner | business | property_manager | registered_agent | other
    person_name: str = ""


class ResearchAnswer(BaseModel):
    """What Claude found about one owner (validated; this is what gets cached)."""
    model_config = ConfigDict(extra="ignore")
    entities: list[FoundEntity] = Field(default_factory=list)
    people: list[FoundPerson] = Field(default_factory=list)
    websites: list[FoundWebsite] = Field(default_factory=list)
    contacts: list[FoundContact] = Field(default_factory=list)
    summary: str = ""
    not_found_reason: str = ""

    @field_validator("entities", "people", "websites", "contacts", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        if v is None:
            return []
        items = v if isinstance(v, list) else [v]
        return [x for x in items if isinstance(x, dict)]   # a stray string in a list is skipped, not fatal

    @field_validator("summary", "not_found_reason", mode="before")
    @classmethod
    def _str(cls, v: Any) -> str:
        return _text(v)

    @property
    def empty(self) -> bool:
        return not (self.entities or self.people or self.websites or self.contacts)


def research_schema() -> dict:
    """The JSON schema Claude Code checks the reply against (--json-schema)."""
    s = {"type": "string"}
    conf = {"type": "string", "enum": list(CONFIDENCES)}

    def obj(props: dict) -> dict:
        return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}

    def arr(item: dict) -> dict:
        return {"type": "array", "items": item}

    seen = {"source_url": s, "confidence": conf, "match_reason": s}
    return obj({
        "entities": arr(obj({"name": s, "state": s, "status": s, "filing_id": s, **seen})),
        "people": arr(obj({"name": s, "role": {"type": "string", "enum": list(ROLE_MAP)}, **seen})),
        "websites": arr(obj({"url": s, **seen})),
        "contacts": arr(obj({"channel": {"type": "string", "enum": list(CHANNEL_WORDS)}, "value": s, "label": s,
                             "belongs_to": {"type": "string", "enum": ["owner", "business", "property_manager",
                                                                      "registered_agent", "other"]},
                             "person_name": s, **seen})),
        "summary": s,
        "not_found_reason": s,
    })


def parse_answer(text: str) -> ResearchAnswer:
    """Claude's reply -> ResearchAnswer. VisionParseError when it isn't the JSON object we asked for."""
    obj = extract_json_object(text)
    try:
        return ResearchAnswer.model_validate(obj)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise VisionParseError(f"'{where}' {first.get('msg')}") from None


# --- owners to research -------------------------------------------------------------------------

@dataclass
class OwnerGroup:
    """One owner (one Claude call) and the properties in this run that the answer is written to."""
    key: str                   # 'name:<normalized owner name>'
    name: str                  # the owner name searched for
    members: list[E.Ctx] = field(default_factory=list)
    cache_key: str = ""
    family: set[int] = field(default_factory=set)   # members + the owner's other properties (do-not-contact check)

    @property
    def best(self) -> E.Ctx:
        return self.members[0]

    @property
    def property_ids(self) -> list[int]:
        return [c.id for c in self.members]


@dataclass
class Targets:
    groups: list[OwnerGroup]
    skipped_dnc: int = 0       # properties left out: the owner (or a sibling) is marked do-not-contact
    no_name: int = 0           # properties left out: no owner name to search for yet
    enabled: bool = True


def _group_key(name: str) -> str:
    """One group per owner name. Not per portfolio: a portfolio also joins different owners that share a mailing
    address (often a property manager's), and one owner's answer must never be written to another owner."""
    return f"name:{normalize_owner(name) or name.upper()}"


def _family_dnc(conn: sqlite3.Connection, family: set[int]) -> bool:
    """True when any property of this owner's family is marked do-not-contact (read now, not when the run began)."""
    ids = sorted(family)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        if one(conn, f"SELECT 1 AS x FROM drill_state WHERE status='dnc' AND property_id IN "
                     f"({','.join('?' for _ in chunk)}) LIMIT 1", chunk):
            return True
    return False


def _error_counts(conn: sqlite3.Connection, market_id: int) -> dict[str, int]:
    """{owner key: failed tries since that owner's last answer} in this market."""
    return {r["owner_key"]: int(r["n"]) for r in rows(
        conn, "SELECT owner_key, COUNT(*) AS n FROM research_runs r WHERE market_id = ? AND outcome = 'error' "
              "AND id > COALESCE((SELECT MAX(id) FROM research_runs g WHERE g.market_id = r.market_id "
              "AND g.owner_key = r.owner_key AND g.outcome != 'error'), 0) GROUP BY owner_key", (market_id,))}


def owner_groups(conn: sqlite3.Connection, market: dict, cfg: ResearchConfig,
                 settings: Settings | None = None) -> Targets:
    """Owners whose properties still need AI research in this pass, best first (buy box, then score). Owners
    whose last tries failed come after the rest (fewest failures first), so a few owners that keep failing can't
    stop every run before it reaches anyone else.

    Left out: owners set aside (scope buybox, once scored), confirmed or do-not-contact owners, owner types
    the stage doesn't apply to, properties already researched this pass, owners with no name, and any owner
    group where one property, in any market, is marked do-not-contact."""
    dcfg = E.drill_config(settings)
    stage = dcfg.by_key.get(STAGE)
    if stage is None or not stage.enabled:
        return Targets(groups=[], enabled=False)
    buybox = cfg.scope == "buybox" and E._has_scores(conn, market["id"])
    groups: dict[str, OwnerGroup] = {}
    no_name = 0
    for ctx in E.load_contexts(conn, market_id=market["id"], status=("active", "exhausted"), buybox_only=buybox):
        if STAGE not in {st.key for st in E._applicable(dcfg, ctx.prop, ctx.owner_type, ctx.asset_class)}:
            continue
        if STAGE in E.current_latest(ctx.attempts):
            continue
        name = E.owner_search_name(ctx)
        if not name:
            no_name += 1
            continue
        key = _group_key(name)
        groups.setdefault(key, OwnerGroup(key=key, name=name)).members.append(ctx)
    if not groups:
        return Targets(groups=[], no_name=no_name)
    index = E.build_sibling_index(conn, settings)
    dnc = {int(r["property_id"]) for r in rows(conn, "SELECT property_id FROM drill_state WHERE status='dnc'")}
    out, skipped = [], 0
    for g in groups.values():
        family = set(g.property_ids)
        for pid in g.property_ids:
            for k in index.keys_of.get(pid, ()):
                if k.startswith(("pk:", "name:")):
                    family |= index.members.get(k, set())
        if family & dnc:
            skipped += len(g.members)
            continue
        g.family = family
        g.members.sort(key=E._sort_key)
        g.cache_key = _cache_key(g, cfg)
        out.append(g)
    errors = _error_counts(conn, market["id"])
    out.sort(key=lambda g: (errors.get(g.key, 0), E._sort_key(g.best)))
    return Targets(groups=out, skipped_dnc=skipped, no_name=no_name)


def _cache_key(g: OwnerGroup, cfg: ResearchConfig) -> str:
    p = g.best.prop
    return make_key("ai_research", cfg.model, PROMPT_VERSION, normalize_owner(g.name) or g.name.upper(),
                    (clean_str(p.get("state")) or "").upper(), (clean_str(p.get("mailing_city")) or "").upper())


# --- the prompt ---------------------------------------------------------------------------------

def _place(p: dict) -> str:
    county = clean_str(p.get("county"))
    where = ", ".join(x for x in (clean_str(p.get("address")), clean_str(p.get("city")), clean_str(p.get("state")))
                      if x)
    return where + (f" ({county.title()} County)" if county else "")


def build_prompt(g: OwnerGroup, cfg: ResearchConfig, asset_label: str = "") -> str:
    """The research request for one owner. It names only public facts from the records: the owner name, what
    kind of owner it is, the mailing city and the properties' addresses (never the mailing street)."""
    ctx = g.best
    kind = OWNER_TYPE_LABELS.get(ctx.owner_type, ctx.owner_type.replace("_", " "))
    city = ", ".join(x for x in (clean_str(ctx.prop.get("mailing_city")), clean_str(ctx.prop.get("mailing_state")))
                     if x)
    places = "\n".join(f"- {_place(c.prop)}" for c in g.members[:3])
    if len(g.members) > 3:
        places += f"\n- and {len(g.members) - 3} more"
    sites = ", ".join(cfg.blocked_domains)
    return f"""Brad's small real-estate team wants to reach the owner of a property they may want to buy. Research \
this property owner on the public web so the team can contact them about buying the property.

Owner on record: {g.name}
What the records say it is: {kind}
Owner's mailing city: {city or "not on record"}
Their {asset_label.lower() or "properties"} here:
{places}

Find, only when it is public:
1. The business entity behind an LLC, corporation or trust: its exact name, state, status and filing number, from the \
state business registry (Ohio Secretary of State business search, Indiana INBiz) or OpenCorporates.
2. The statutory (registered) agent, and the organizers, officers, members or managers named in public filings.
3. The company's own website.
4. A public business phone number and a public business email address.
5. Public social media pages or profiles that clearly belong to this owner or business. Count one only when the name \
and the city or the business match, and say why in match_reason.

Rules:
- Never use or cite people-search, background-check or data-broker sites. That includes: {sites}. Never use \
crexi.com either, and never read or cite copies of those sites' pages (archive, translation or page-reader links).
- Never give home addresses, relatives, ages, or anything about health, religion, ethnicity or other personal traits.
- Never guess an email address or phone number from a pattern. Give only what you saw written on a page.
- Every item needs source_url: the exact page where you saw it. Give a confidence: high (the page names this owner \
and the detail), medium (very likely the same owner), low (unsure).
- Registered-agent companies (like CT Corporation or Northwest Registered Agent) are not the owner: list one as a \
person with role registered_agent, and never list its phone or email as a contact.
- When you are not sure something belongs to this owner, leave it out. Empty lists are a fine answer; then say why \
in not_found_reason.

Reply with only the JSON object the schema asks for."""


# --- backends -----------------------------------------------------------------------------------

class ResearchCLI(ClaudeCLIBackend):
    """Claude Code in print mode with web search and page reads only, on your Claude plan.

    Every safety flag is required and nothing is ever dropped: if this Claude Code doesn't know one, the run
    stops and asks for `claude update`. Never --bare (it ignores the plan login), never bypassPermissions, and
    no file or shell tools. It runs in a neutral folder with the API keys taken out of its environment."""
    name = "claude_cli"
    provider = "claude_cli"
    sku = "research_claude_cli"
    task_label = "AI research"
    item_word = "owner"
    model_setting = "ai_research.model in config/drill.yaml"
    pending_text = "Owners not researched yet stay pending"
    workdir_name = "bluemoon-research"

    def __init__(self, cfg: ClaudeCLIConfig | None = None, *, blocked_domains: list[str] | None = None,
                 **kw: Any):
        super().__init__(cfg or research_config().cli_config(), **kw)
        self.blocked_domains = list(blocked_domains if blocked_domains is not None
                                    else research_config().blocked_domains)

    def optional_flags(self, schema: dict | None) -> list[list[str]]:
        return []   # nothing here may be left out

    @staticmethod
    def prompt_with_files(prompt: str, image_paths: Any) -> str:
        return prompt

    def build_command(self, image_paths: Any, prompt: str, schema: dict | None = None) -> list[str]:
        if image_paths:
            raise ValueError("The AI research doesn't read files.")
        exe = self.executable() or self.cfg.command
        return [exe, "-p", prompt, "--output-format", "json", "--model", self.cfg.model,
                "--tools", "WebSearch,WebFetch",
                "--allowedTools", "WebSearch", "WebFetch",      # in print mode both need permission, or they're denied
                "--permission-mode", "dontAsk",                 # anything else that would ask is denied
                "--strict-mcp-config",                          # none of your connectors (mail, drive...)
                "--settings", json.dumps(websearch_settings(self.blocked_domains), separators=(",", ":")),
                "--disallowedTools", *deny_rules(self.blocked_domains),   # deny beats allow
                "--json-schema", json.dumps(schema or research_schema(), separators=(",", ":")),
                "--no-session-persistence",
                "--max-turns", str(self.cfg.max_turns or 20)]

    def child_env(self) -> dict[str, str]:
        """No keys at all: besides the configured list, any *_API_KEY, *_API_TOKEN or *_SECRET is taken out."""
        env = super().child_env()
        return {k: v for k, v in env.items()
                if not re.search(r"(_API_KEY|_API_TOKEN|_SECRET|PASSWORD)$", k, re.IGNORECASE)}

    def _check_required_flags(self, text: str) -> None:
        m = _UNKNOWN_OPTION.search(text or "") or _BAD_CHOICE.search(text or "")
        if m:
            raise VisionFatalError(UPDATE_CLAUDE.format(flag=m.group(1)))


_BAD_CHOICE = re.compile(r"option\s+'(--[\w-]+)[^']*'\s+argument\s+.*?\s+is invalid", re.IGNORECASE)


class MockResearchBackend:
    """Offline answers made up from the owner name (tests and demos, `ai_research.backend: mock`). One answer in
    three finds nothing; the others include a contact from a blocked people-search site, which the filter drops."""
    name = "mock"
    provider = ""
    sku = ""
    billable = False

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.last_result = None

    @property
    def model(self) -> str:
        return "mock-1"

    def check(self) -> None:
        """Always ready."""

    def ask(self, prompt: str, *, schema: dict | None = None) -> str:
        self.prompts.append(prompt)
        m = re.search(r"^Owner on record: (.+)$", prompt, re.MULTILINE)
        owner = m.group(1).strip() if m else "OWNER"
        n = sum(map(ord, owner))
        if n % 3 == 0:
            return json.dumps({"entities": [], "people": [], "websites": [], "contacts": [], "summary": "",
                               "not_found_reason": "Mock answer (offline test mode): nothing public found."})
        slug = re.sub(r"[^a-z0-9]+", "-", owner.lower()).strip("-") or "owner"
        src = f"https://www.example.org/filings/{slug}"
        return json.dumps({
            "entities": [{"name": owner, "state": "OH", "status": "Active", "filing_id": str(1000 + n),
                          "source_url": src, "confidence": "high", "match_reason": "Same name in the state filing."}],
            "people": [{"name": "Pat Example", "role": "member", "source_url": src, "confidence": "medium",
                        "match_reason": "Named as member in the filing."}],
            "websites": [{"url": f"https://{slug}.example.com", "source_url": src, "confidence": "medium",
                          "match_reason": "Linked from the filing."}],
            "contacts": [
                {"channel": "phone", "value": f"(937) 555-{n % 10000:04d}", "label": "Office line",
                 "belongs_to": "business", "person_name": "", "source_url": f"https://{slug}.example.com/contact",
                 "confidence": "medium", "match_reason": "On the company's contact page."},
                {"channel": "phone", "value": "(937) 555-0199", "label": "Home phone", "belongs_to": "owner",
                 "person_name": "Pat Example", "source_url": "https://www.truepeoplesearch.com/find/person/x",
                 "confidence": "high", "match_reason": "Mock people-search result (always dropped)."}],
            "summary": "Mock answer (offline test mode).", "not_found_reason": ""})


def get_research_backend(cfg: ResearchConfig | None = None, name: str | None = None) -> Any:
    cfg = cfg or research_config()
    key = (name or cfg.backend or "claude_cli").strip().lower()
    if key == "claude_cli":
        return ResearchCLI(cfg.cli_config(), blocked_domains=cfg.blocked_domains)
    if key == "mock":
        return MockResearchBackend()
    raise ValueError(f"Unknown AI research backend '{name or cfg.backend}'. Use claude_cli or mock.")


# --- the filter (the real guarantee) ------------------------------------------------------------

@dataclass
class Finding:
    kind: str                      # entity | person | website | phone | email | social
    value: str
    source_url: str
    confidence: str
    match_reason: str = ""
    label: str = ""
    person_name: str | None = None
    role: str | None = None        # people.role for a person (never an owner role)
    status: str = "kept"           # kept | lead | blocked | rejected
    why: str | None = None
    channel: str | None = None     # contact_points.channel for phone, email and social

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.value.lower()


def _agent_service(name: str | None, services: list[str]) -> bool:
    """True when a name is a registered-agent company ('C T CORPORATION SYSTEM', 'CSC')."""
    up = re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).strip()
    if not up:
        return False
    for svc in services:
        s = re.sub(r"[^A-Z0-9]+", " ", svc.upper()).strip()
        if s and re.search(rf"(?<![A-Z0-9]){re.escape(s)}(?![A-Z0-9])", up):
            return True
    return False


def _rank(c: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(c, 0)


def _source_problem(item: _Found, cfg: ResearchConfig) -> tuple[str, str] | None:
    """(status, why) when an item's source link can't be used."""
    site = blocked_by(item.source_url, cfg.blocked_domains)
    if site:
        return "blocked", f"Seen on {site}, a site that is never used. Nothing from it is kept."
    if url_host(item.source_url) is None:
        return "rejected", "No link to the page where it was seen."
    return None


def screen(answer: ResearchAnswer, cfg: ResearchConfig) -> list[Finding]:
    """Every item of an answer as a Finding with its status. Pure: nothing is written here.

    blocked: the source (or the value, for a link) is on a blocked site; nothing Claude wrote about it is kept.
    rejected: no usable source link, a value that isn't a real phone, email or web link, or a registered-agent
      company's contact. lead: below min_confidence, or past max_people / max_contacts. kept: everything else."""
    out: list[Finding] = []
    services = cfg.registered_agent_services
    bar = _rank(cfg.min_confidence)

    def add(f: Finding, item: _Found, value_url: str | None = None) -> None:
        problem = _source_problem(item, cfg)
        site = blocked_by(value_url, cfg.blocked_domains) if value_url else None
        if site and (not problem or problem[0] != "blocked"):
            problem = ("blocked", f"A page on {site}, a site that is never used.")
        f.match_reason = safe_text(f.match_reason, cfg)
        if problem:
            f.status, f.why = problem
            if f.status == "blocked":                  # nothing from a blocked site is kept, not even its wording
                f.value, f.person_name, f.label, f.match_reason = "", None, KIND_WORDS[f.kind], ""
        elif f.status == "kept" and _rank(f.confidence) < bar:
            f.status, f.why = "lead", f"Confidence {f.confidence or 'low'}: kept as a lead, not added as a contact."
        out.append(f)

    for e in answer.entities:
        bits = [x for x in (e.state.upper(), e.status, f"#{e.filing_id}" if e.filing_id else "") if x]
        f = Finding("entity", e.name, e.source_url, e.confidence, e.match_reason,
                    label="Business filing" + (f" ({', '.join(bits)})" if bits else ""))
        if not clean_str(e.name):
            f.status, f.why = "rejected", "No name."
        add(f, e)
    for p in answer.people:
        word = p.role if p.role in ROLE_MAP else "other"
        f = Finding("person", p.name, p.source_url, p.confidence, p.match_reason, label=ROLE_WORDS[word],
                    role=ROLE_MAP[word], person_name=p.name)
        if not clean_str(p.name):
            f.status, f.why = "rejected", "No name."
        elif _agent_service(p.name, services):
            f.status, f.why = "rejected", "A registered-agent company, not the owner."
        add(f, p)
    for w in answer.websites:
        f = Finding("website", w.url, w.source_url, w.confidence, w.match_reason, label="Website")
        if url_host(w.url) is None:
            f.status, f.why = "rejected", "Not a web link."
        add(f, w, value_url=w.url)
    for c in answer.contacts:
        ch = c.channel.lower() if c.channel.lower() in CHANNEL_WORDS else None
        f = Finding(ch or "contact", c.value, c.source_url, c.confidence, c.match_reason,
                    label=safe_text(c.label, cfg) or CHANNEL_WORDS.get(ch or "", "contact").capitalize(),
                    person_name=clean_str(c.person_name), channel=ch)
        agentish = c.belongs_to == "registered_agent" and (
            not clean_str(c.person_name) or L.looks_like_entity(c.person_name))
        if ch is None:
            f.status, f.why = "rejected", f"'{c.channel}' is not a phone, email or social page."
        elif agentish or any(_agent_service(x, services) for x in (c.person_name, c.label)):
            f.status, f.why = "rejected", "A registered-agent company's contact: it reaches the agent, not the owner."
        elif ch == "social" and url_host(c.value) is None:
            f.status, f.why = "rejected", "A social page needs its full web link."
        else:
            try:
                f.value = E.normalize_contact(ch, c.value)[0]
            except ValueError as exc:
                f.status, f.why = "rejected", str(exc)
        add(f, c, value_url=c.value if ch == "social" else None)
    _dedupe_and_cap(out, cfg)
    return out


def without_blocked(answer: ResearchAnswer, cfg: ResearchConfig) -> tuple[ResearchAnswer, int]:
    """The answer minus every item seen on (or pointing to) a blocked site, and how many were taken out. This
    is what gets cached: nothing from a people-search site is ever stored, and the free text goes through
    safe_text (none at all when something was taken out)."""
    def ok(item: _Found, value_url: str | None = None) -> bool:
        return not blocked_by(item.source_url, cfg.blocked_domains) and not (
            value_url and blocked_by(value_url, cfg.blocked_domains))

    def tidy(item: Any) -> Any:
        update = {"match_reason": safe_text(item.match_reason, cfg)}
        if isinstance(item, FoundContact):
            update["label"] = safe_text(item.label, cfg)
        return item.model_copy(update=update)

    clean = answer.model_copy(update={
        "entities": [tidy(e) for e in answer.entities if ok(e)],
        "people": [tidy(p) for p in answer.people if ok(p)],
        "websites": [tidy(w) for w in answer.websites if ok(w, w.url)],
        "contacts": [tidy(c) for c in answer.contacts if ok(c, c.value if c.channel == "social" else None)]})
    dropped = sum(len(getattr(answer, k)) - len(getattr(clean, k)) for k in ("entities", "people", "websites",
                                                                            "contacts"))
    clean.summary = safe_text(answer.summary, cfg, dropped)
    clean.not_found_reason = safe_text(answer.not_found_reason, cfg, dropped)
    return clean, dropped


def _dedupe_and_cap(found: list[Finding], cfg: ResearchConfig) -> None:
    seen: set[tuple[str, str]] = set()
    kept_people = kept_contacts = 0
    for f in found:
        if f.status != "kept":
            continue
        if f.key in seen:
            f.status, f.why = "rejected", "Listed twice."
            continue
        seen.add(f.key)
        if f.kind == "person":
            kept_people += 1
            if kept_people > cfg.max_people:
                f.status, f.why = "lead", f"More than {cfg.max_people} people found: kept as a lead."
        elif f.channel:
            kept_contacts += 1
            if kept_contacts > cfg.max_contacts:
                f.status, f.why = "lead", f"More than {cfg.max_contacts} contacts found: kept as a lead."


# --- writing ------------------------------------------------------------------------------------

def _note(f: Finding) -> str:
    return f"AI research: {f.label or f.kind}. Source: {f.source_url}"


def _short(text: str, limit: int = 200) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _detail(found: list[Finding], answer: ResearchAnswer, added: list[dict], people_new: list[str]) -> str:
    kept = [f for f in found if f.status == "kept"]
    parts = []
    ent = next((f for f in kept if f.kind == "entity"), None)
    if ent:
        parts.append(f"{ent.label}: {ent.value}.")
    people = [f for f in kept if f.kind == "person"]
    if people:
        parts.append("People: " + ", ".join(f"{f.value} ({f.label.split(' (')[0].lower()})" for f in people[:4]) + ".")
    site = next((f for f in kept if f.kind == "website"), None)
    if site:
        parts.append(f"Website: {url_host(site.value) or site.value}.")
    if added:
        kinds = sorted({CHANNEL_WORDS.get(c["channel"], c["channel"]) for c in added})
        parts.append(f"Added {len(added)} contact{'s' if len(added) != 1 else ''} ({', '.join(kinds)}), "
                     f"not tested yet.")
    elif people_new:
        parts.append(f"Added {len(people_new)} {'person' if len(people_new) == 1 else 'people'}.")
    leads = sum(1 for f in found if f.status == "lead")
    if leads:
        parts.append(f"{leads} unsure lead{'s' if leads != 1 else ''} kept for a look.")
    blocked = sum(1 for f in found if f.status == "blocked")
    if blocked:
        parts.append(f"Dropped {blocked} from blocked sites.")
    if not added and not people_new:
        why = answer.not_found_reason or answer.summary
        parts.insert(0, "Nothing new to reach the owner with." + (f" {_short(why)}" if why else ""))
    return " ".join(parts)


@dataclass
class _Applied:
    run_id: int
    found_props: int = 0
    nothing_props: int = 0
    contacts_added: int = 0
    people_added: int = 0
    blocked: int = 0
    leads: int = 0
    skipped_dnc: bool = False   # the owner was marked do-not-contact while the run was going: nothing written


def _insert_run(conn: sqlite3.Connection, market_id: int, g: OwnerGroup, cfg: ResearchConfig, backend: Any, *,
                outcome: str, from_cache: bool, answer: ResearchAnswer | None, error: str | None,
                meta: dict) -> int:
    cur = conn.execute(
        "INSERT INTO research_runs(market_id, owner_key, owner_name, cache_key, model, prompt_version, outcome, "
        "from_cache, summary, error, num_turns, seconds, property_ids, actor, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (market_id, g.key, g.name, g.cache_key, f"{backend.name}:{meta.get('model') or backend.model}",
         PROMPT_VERSION, outcome, int(from_cache), _short((answer.summary or answer.not_found_reason) if answer else "",
                                                          500) or None,
         error, meta.get("num_turns"), meta.get("seconds"), dumps(g.property_ids), ACTOR, now_iso()))
    return int(cur.lastrowid)


def apply_answer(conn: sqlite3.Connection, market_id: int, g: OwnerGroup, answer: ResearchAnswer,
                 cfg: ResearchConfig, backend: Any, *, from_cache: bool, meta: dict,
                 settings: Settings | None = None) -> _Applied:
    """Write one owner's answer to every property of the group, in one transaction: the run, its findings,
    the kept people and contacts (source ai_research, not tested yet; values on the do-not-contact list stay
    do-not-contact), one ai_research check-off per property (found | nothing, actor 'ai') and the new state.
    Nothing is written when a property of the owner's family was marked do-not-contact in the meantime."""
    dcfg = E.drill_config(settings)
    found = screen(answer, cfg)
    blocked = sum(1 for f in found if f.status == "blocked")
    answer = answer.model_copy(update={"summary": safe_text(answer.summary, cfg, blocked),   # what may be shown
                                       "not_found_reason": safe_text(answer.not_found_reason, cfg, blocked)})
    with E._tx(conn):
        if _family_dnc(conn, g.family or set(g.property_ids)):
            return _Applied(run_id=0, skipped_dnc=True)
        dnc = E._dnc_keys(conn)
        found_ids: set[int] = set()
        res = _Applied(run_id=0)
        rows_out: list[tuple] = []
        for ctx in g.members:
            pid = ctx.id
            person_ids: dict[str, int] = {}
            people_new: list[str] = []
            added: list[dict] = []
            links: dict[int, tuple[int | None, int | None]] = {}
            for i, f in enumerate(found):
                if f.status != "kept" or f.kind != "person":
                    continue
                person_id, created = E._add_person(conn, pid, f.value, role=f.role, source=SOURCE, notes=_note(f),
                                                   upgrade_role=False)
                person_ids[E._name_key(f.value)] = person_id
                links[i] = (None, person_id)
                if created and f.role != "agent" and not L.looks_like_entity(f.value):
                    people_new.append(f.value)
            for i, f in enumerate(found):
                if f.status != "kept" or not f.channel:
                    continue
                owner_of = person_ids.get(E._name_key(f.person_name)) if f.person_name else None
                row, created = E._add_contact(conn, pid, f.channel, f.value, source=SOURCE, person_id=owner_of,
                                              notes=_note(f), dnc_keys=dnc)
                links[i] = (int(row["id"]), owner_of)
                if created:
                    added.append(row)
            outcome = "found" if added or people_new else "nothing"
            if outcome == "found":
                found_ids.add(pid)
            res.contacts_added += len(added)
            res.people_added += len(people_new)
            if outcome == "found":
                res.found_props += 1
            else:
                res.nothing_props += 1
            E._insert_attempt(conn, pid, STAGE, outcome, _detail(found, answer, added, people_new), ACTOR)
            rows_out += [(pid, i, f, *links.get(i, (None, None))) for i, f in enumerate(found)]
        res.run_id = _insert_run(conn, market_id, g, cfg, backend, outcome="found" if found_ids else "nothing",
                                 from_cache=from_cache, answer=answer, error=None, meta=meta)
        now = now_iso()
        conn.executemany(
            "INSERT INTO research_findings(run_id, property_id, kind, value, label, person_name, role, source_url, "
            "confidence, match_reason, status, why, contact_point_id, person_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(res.run_id, pid, f.kind, f.value, f.label, f.person_name, f.role,
              f.source_url if f.status != "blocked" else url_host(f.source_url), f.confidence,
              _short(f.match_reason, 300) or None, f.status, f.why, cp_id, person_id, now)
             for pid, _, f, cp_id, person_id in rows_out])
        for ctx in E.load_contexts(conn, property_ids=g.property_ids):   # found: an exhausted owner reopens
            E._save_state(conn, ctx, E.evaluate(ctx, dcfg, new_info=ctx.id in found_ids))
    res.blocked = blocked
    res.leads = sum(1 for f in found if f.status == "lead")
    return res


# --- cache --------------------------------------------------------------------------------------

def _age_days(created_at: str | None) -> float:
    try:
        then = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return math.inf
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400


def cached_answer(conn: sqlite3.Connection, g: OwnerGroup, cfg: ResearchConfig) -> tuple[ResearchAnswer, dict] | None:
    """The saved answer for this owner, while it is younger than refresh_after_days."""
    row = one(conn, "SELECT status, body, created_at FROM api_cache WHERE key = ?", (g.cache_key,))
    if not row or (row["status"] or 0) >= 400 or _age_days(row["created_at"]) > cfg.refresh_after_days:
        return None
    body = loads(row["body"], {}) or {}
    try:
        return ResearchAnswer.model_validate(body.get("answer") or {}), dict(body.get("meta") or {})
    except (ValidationError, AttributeError):
        return None


def _ask(backend: Any, prompt: str) -> tuple[ResearchAnswer, int]:
    """(answer, calls made). One retry when the reply can't be read."""
    calls, last, text_prompt = 0, "", prompt
    for _ in range(2):
        try:
            text = backend.ask(text_prompt, schema=research_schema())
        except VisionError as exc:
            exc.calls += calls
            raise
        calls += 1
        try:
            return parse_answer(text), calls
        except VisionParseError as exc:
            last = str(exc)
            text_prompt = prompt + RETRY_NOTE
    raise VisionCallError(f"Claude's answer couldn't be read twice ({last}). This owner will be tried again next "
                          f"run.", calls=calls)


# --- estimate and run ---------------------------------------------------------------------------

def _plan(conn: sqlite3.Connection, groups: list[OwnerGroup], cfg: ResearchConfig) -> tuple[int, int, int, bool]:
    """(cached, calls, properties covered, capped) for these owners, with the per-run call cap."""
    cached = calls = props = 0
    capped = False
    for g in groups:
        if cached_answer(conn, g, cfg) is not None:
            cached += 1
        elif calls < cfg.max_calls_per_run:
            calls += 1
        else:
            capped = True
            continue
        props += len(g.members)
    return cached, calls, props, capped


def _estimate(conn: sqlite3.Connection, slug: str, t: Targets, groups: list[OwnerGroup], backend: Any,
              cfg: ResearchConfig) -> dict:
    cached, calls, props, capped = _plan(conn, groups, cfg)
    minutes = math.ceil(calls * cfg.est_seconds_per_owner / 60) if calls else 0
    ready, why = True, None
    try:
        backend.check()
    except VisionFatalError as exc:
        ready, why = False, exc.message
    who = "buy-box owners" if cfg.scope == "buybox" else "owners"
    on_plan = backend.name == "claude_cli"
    msg = (f"AI research with {'Claude Code' if on_plan else 'the offline test answers'} ({backend.model}): "
           f"{len(groups):,} {who} ({props:,} properties), {cached:,} already answered, {calls:,} to ask Claude.")
    if not groups:
        msg = f"AI research: nothing to research. Every {who.removesuffix('s')} with a name has been researched."
    elif on_plan:
        msg += f" No per-call bill: uses your Claude plan, about {minutes:,} min."
    if capped:
        msg += f" Capped at {cfg.max_calls_per_run:,} owners per run (ai_research.max_calls_per_run)."
    if t.skipped_dnc:
        msg += f" Left out {t.skipped_dnc:,} propert{'y' if t.skipped_dnc == 1 else 'ies'} of do-not-contact owners."
    if not ready and calls:
        msg += f" {why}"
    over = cfg.confirm_over_properties
    return {"dry_run": True, "enabled": True, "backend": backend.name, "model": backend.model, "scope": cfg.scope,
            "owners": len(groups), "properties": props, "cached": cached, "calls": calls, "to_ask": calls,
            "capped": capped, "est_minutes": minutes, "est_cost_usd": 0.0, "ready": ready, "not_ready_reason": why,
            "needs_confirm": backend.name != "mock" and (props > over or calls > over),
            "skipped_dnc": t.skipped_dnc, "no_name": t.no_name, "market": slug, "message": msg}


def research_estimate(conn: sqlite3.Connection, market_slug: str, limit: int | None = None,
                      backend: Any = None) -> dict:
    """What an AI research run would do, without asking Claude anything."""
    return research_owners(conn, market_slug, limit=limit, dry_run=True, backend=backend)


@dataclass
class _Stats:
    researched: int = 0
    cached: int = 0
    calls: int = 0
    found: int = 0               # owners with something new
    nothing: int = 0
    found_properties: int = 0
    contacts_added: int = 0
    people_added: int = 0
    blocked_dropped: int = 0
    leads: int = 0
    errors: int = 0
    skipped_dnc: int = 0         # owners marked do-not-contact while the run was going
    capped: bool = False
    fatal: str | None = None
    warnings: list[str] = field(default_factory=list)


def research_owners(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
                    backend: Any = None, progress: Progress = None, confirm: bool = False) -> dict:
    """Research every owner that still needs it (best first); see the module docstring.

    limit: at most this many owners (Test on 5 = 5 Claude calls). dry_run: the estimate only.
    backend: a backend object or name (claude_cli | mock); default ai_research.backend.
    confirm: needed when the run covers more than safety.confirm_over_properties properties (not for mock).
    A cancel (raised inside `progress`) lands between owners; what was already written stays."""
    settings = get_settings()
    cfg = research_config(settings)
    market = market_by_slug(conn, market_slug)
    t = owner_groups(conn, market, cfg, settings)
    if not t.enabled:
        return {"dry_run": dry_run, "enabled": False, "owners": 0, "properties": 0, "calls": 0, "cached": 0,
                "needs_confirm": False, "market": market_slug,
                "message": "AI research is switched off (the ai_research stage in config/drill.yaml)."}
    be = backend if backend is not None and not isinstance(backend, str) else get_research_backend(cfg, backend)
    groups = t.groups[:max(0, int(limit))] if limit is not None else t.groups
    estimate = _estimate(conn, market_slug, t, groups, be, cfg)
    if dry_run:
        return estimate
    if estimate["calls"] and not estimate["ready"]:
        return {**estimate, "dry_run": False, "error": estimate["not_ready_reason"], "researched": 0,
                "message": estimate["not_ready_reason"]}
    if estimate["needs_confirm"] and not confirm:
        return {**estimate, "dry_run": False,
                "message": f"Waiting for your OK: {estimate['message']} Run again with confirm (CLI: --yes)."}
    asset = settings.asset_class(market["asset_class"]).label
    stats = _run(conn, market, groups, be, cfg, settings, progress, asset)
    result = _result(estimate, stats, groups, t, be)
    log.info("AI research %s: %s", market_slug, result["message"])
    _tell(progress, 1.0, result["message"])
    return result


def _tell(progress: Progress, fraction: float, message: str) -> None:
    """Report progress. A broken callback never stops the run; a job's cancel (a BaseException) does."""
    if progress is None:
        return
    try:
        progress(max(0.0, min(1.0, fraction)), message)
    except Exception:
        log.debug("progress callback failed", exc_info=True)


def _record(conn: sqlite3.Connection, backend: Any, calls: int, stats: _Stats) -> None:
    stats.calls += calls
    if calls and backend.sku:
        record_call(conn, backend.provider, backend.sku, calls)


def _run(conn: sqlite3.Connection, market: dict, groups: list[OwnerGroup], be: Any, cfg: ResearchConfig,
         settings: Settings, progress: Progress, asset_label: str) -> _Stats:
    stats = _Stats()
    consecutive = 0
    n = len(groups)
    for i, g in enumerate(groups):
        _tell(progress, i / max(1, n) * 0.98, f"AI research: owner {i + 1:,} of {n:,}: {g.name}")
        if _family_dnc(conn, g.family or set(g.property_ids)):   # marked do-not-contact since the run began
            stats.skipped_dnc += 1
            continue
        hit = cached_answer(conn, g, cfg)
        from_cache = hit is not None
        if hit is not None:
            answer, meta = hit[0], {**hit[1], "seconds": None}
            stats.cached += 1
        else:
            if stats.calls >= cfg.max_calls_per_run:
                stats.capped = True
                break
            started = time.monotonic()
            try:
                answer, calls = _ask(be, build_prompt(g, cfg, asset_label))
            except VisionFatalError as exc:
                _record(conn, be, exc.calls, stats)
                stats.fatal = exc.message
                break
            except VisionCallError as exc:
                _record(conn, be, exc.calls, stats)
                stats.errors += 1
                consecutive += 1
                with E._tx(conn):
                    _insert_run(conn, market["id"], g, cfg, be, outcome="error", from_cache=False, answer=None,
                                error=exc.message, meta={})
                if exc.message not in stats.warnings:
                    stats.warnings.append(exc.message)
                if consecutive >= cfg.max_consecutive_errors:
                    stats.fatal = (f"{consecutive} owners in a row failed, so the AI research stopped. "
                                   f"Last problem: {exc.message}")
                    break
                continue
            _record(conn, be, calls, stats)
            consecutive = 0
            last = getattr(be, "last_result", None)
            clean, dropped = without_blocked(answer, cfg)
            meta = {"asked_at": now_iso(), "model": be.model, "num_turns": getattr(last, "num_turns", None),
                    "seconds": round(time.monotonic() - started, 1), "blocked": dropped}
            cache_put(conn, g.cache_key, CACHE_PROVIDER, 200, {"answer": clean.model_dump(), "meta": meta})
        done = apply_answer(conn, market["id"], g, answer, cfg, be, from_cache=from_cache, meta=meta,
                            settings=settings)
        if done.skipped_dnc:
            stats.skipped_dnc += 1
            continue
        stats.researched += 1
        stats.found += int(done.found_props > 0)
        stats.nothing += int(done.found_props == 0)
        stats.found_properties += done.found_props
        stats.contacts_added += done.contacts_added
        stats.people_added += done.people_added
        stats.blocked_dropped += done.blocked
        stats.leads += done.leads
    return stats


def _result(estimate: dict, stats: _Stats, groups: list[OwnerGroup], t: Targets, be: Any) -> dict:
    left = len(groups) - stats.researched - stats.errors - stats.skipped_dnc
    if not groups and not stats.fatal:
        msg = "AI research: nothing to research. Every owner in scope has been researched in this pass."
        if t.no_name:
            msg += f" {t.no_name:,} propert{'y has' if t.no_name == 1 else 'ies have'} no owner name to search for yet."
    else:
        who = "Claude" if be.name == "claude_cli" else "the offline test answers"
        msg = (f"AI research: {stats.researched:,} owner{'s' if stats.researched != 1 else ''} researched "
               f"({stats.cached:,} from saved answers, {stats.calls:,} calls to {who}). "
               f"Something new for {stats.found:,} ({stats.found_properties:,} properties): "
               f"{stats.contacts_added:,} contacts and {stats.people_added:,} people added, all not tested yet. "
               f"Nothing new for {stats.nothing:,}.")
        if stats.leads:
            msg += f" {stats.leads:,} unsure leads kept for a look (not added as contacts)."
        if stats.blocked_dropped:
            msg += f" Dropped {stats.blocked_dropped:,} findings from blocked sites."
    if stats.errors:
        msg += f" {stats.errors:,} had a problem and will be retried next run."
    if stats.skipped_dnc:
        msg += (f" Left out {stats.skipped_dnc:,} owner{'s' if stats.skipped_dnc != 1 else ''} marked do-not-contact "
                f"while the run was going.")
    if stats.capped:
        msg += " Hit the per-run cap (ai_research.max_calls_per_run); run it again for the rest."
    if stats.fatal:
        msg += f" Stopped early: {stats.fatal}"
    if left > 0 and (stats.fatal or stats.capped):
        msg += f" {left:,} owner{'s' if left != 1 else ''} still pending."
    return {**estimate, "dry_run": False, "needs_confirm": False, "researched": stats.researched,
            "cached": stats.cached, "calls": stats.calls, "found": stats.found, "nothing": stats.nothing,
            "found_properties": stats.found_properties, "contacts_added": stats.contacts_added,
            "people_added": stats.people_added, "blocked_dropped": stats.blocked_dropped, "leads": stats.leads,
            "errors": stats.errors, "skipped_dnc_during_run": stats.skipped_dnc, "capped": stats.capped,
            "pending": max(0, left),
            "stopped": stats.fatal is not None, "error": stats.fatal, "warnings": stats.warnings, "message": msg}


# --- reading and correcting ---------------------------------------------------------------------

FINDING_FIELDS = ("id", "run_id", "kind", "value", "label", "person_name", "role", "source_url", "confidence",
                  "match_reason", "status", "why", "contact_point_id", "person_id", "marked_by", "marked_at",
                  "created_at")


def property_research(conn: sqlite3.Connection, property_id: int, limit: int = 60) -> dict:
    """drill.research for the property drawer: {runs:[...], findings:[...]}, newest first."""
    runs = rows(conn, "SELECT id, created_at, outcome, model, actor, summary, error, from_cache FROM research_runs r "
                      "WHERE EXISTS (SELECT 1 FROM json_each(r.property_ids) j WHERE j.value = ?) "
                      "ORDER BY id DESC LIMIT 10", (int(property_id),))
    findings = rows(conn, f"SELECT {', '.join(FINDING_FIELDS)} FROM research_findings WHERE property_id = ? "
                          f"ORDER BY id DESC LIMIT ?", (int(property_id), int(limit)))
    for r in runs:
        r["from_cache"] = bool(r["from_cache"])
    return {"runs": runs, "findings": findings}


def _copies(conn: sqlite3.Connection, f: dict) -> list[dict]:
    """The same finding on the owner's other properties (one research run writes it to each of them)."""
    return rows(conn, "SELECT * FROM research_findings WHERE run_id = ? AND kind = ? AND value = ? AND id != ?",
                (f["run_id"], f["kind"], f["value"], f["id"]))


def contact_marked(conn: sqlite3.Connection, contact: dict, status: str, actor: str | None) -> set[int]:
    """engine.set_contact_status (or a logged reach-out) changed an AI-found contact, inside its transaction:
    update its finding. Marked wrong, the same contact is marked wrong on the owner's other properties (still
    untested ones only). Taken back (untested or verified again), the copies that same wrong mark reached are
    put back to untested too. Returns the other properties changed."""
    now = now_iso()
    touched: set[int] = set()
    for f in rows(conn, "SELECT * FROM research_findings WHERE contact_point_id = ?", (contact["id"],)):
        if status == "wrong":
            conn.execute("UPDATE research_findings SET status='wrong', marked_by=?, marked_at=? WHERE id=?",
                         (actor, now, f["id"]))
            for copy in _copies(conn, f):
                conn.execute("UPDATE research_findings SET status='wrong', marked_by=?, marked_at=? WHERE id=?",
                             (actor, now, copy["id"]))
                if copy.get("contact_point_id"):
                    cur = conn.execute("UPDATE contact_points SET status='wrong', last_tested_at=? "
                                       "WHERE id=? AND status='untested'", (now, copy["contact_point_id"]))
                    if cur.rowcount:
                        touched.add(int(copy["property_id"]))
        elif status in ("untested", "verified"):
            new = "kept" if f["status"] == "wrong" else f["status"]
            conn.execute("UPDATE research_findings SET status=?, marked_by=?, marked_at=? WHERE id=?",
                         (new, actor, now, f["id"]))
            if f["status"] != "wrong":
                continue
            for copy in _copies(conn, f):
                # Only the copies the same wrong mark reached (it wrote one time on all of them); a copy someone
                # marked wrong on its own property later keeps its mark.
                if copy["status"] != "wrong" or copy["marked_at"] != f["marked_at"]:
                    continue
                conn.execute("UPDATE research_findings SET status='kept', marked_by=?, marked_at=? WHERE id=?",
                             (actor, now, copy["id"]))
                if copy.get("contact_point_id"):
                    cur = conn.execute("UPDATE contact_points SET status='untested' WHERE id=? AND status='wrong'",
                                       (copy["contact_point_id"],))
                    if cur.rowcount:
                        touched.add(int(copy["property_id"]))
    return touched


def mark_finding(conn: sqlite3.Connection, finding_id: int, status: str, *, actor: str | None = None,
                 settings: Settings | None = None) -> dict:
    """A person says an AI finding is wrong (or takes that back): PATCH /api/research/findings/{id}.

    A contact goes through engine.set_contact_status (wrong spreads to the owner's other properties). A person
    becomes role 'rejected' (no longer owner data), the same on the owner's other properties. Anything else just
    records the mark. 'kept' undoes a 'wrong'; a finding that was blocked or rejected can't be kept."""
    st = (clean_str(status) or "").lower()
    if st not in ("wrong", "kept"):
        raise ValueError(f"'{status}' is not a finding status. Use wrong or kept.")
    f = one(conn, "SELECT * FROM research_findings WHERE id = ?", (int(finding_id),))
    if not f:
        raise KeyError(f"AI research finding {finding_id} was not found.")
    if st == "kept" and f["status"] in ("blocked", "rejected"):
        raise ValueError(f"This finding was dropped ({f['why'] or f['status']}), so it can't be kept.")
    if f.get("contact_point_id"):
        contact = one(conn, "SELECT status FROM contact_points WHERE id = ?", (f["contact_point_id"],)) or {}
        change = {"wrong": "wrong" if contact.get("status") in ("untested", "verified") else None,
                  "kept": "untested" if contact.get("status") == "wrong" else None}[st]
        if change:
            res = E.set_contact_status(conn, int(f["contact_point_id"]), change, actor=actor, settings=settings)
            new = st if st == "wrong" or f["status"] == "wrong" else f["status"]
            with E._tx(conn):   # also when the contact was first found by another stage (then it has no AI mark)
                conn.execute("UPDATE research_findings SET status=?, marked_by=?, marked_at=? WHERE id=? "
                             "AND status != ?", (new, actor, now_iso(), f["id"], new))
            return {**res, "finding": one(conn, "SELECT * FROM research_findings WHERE id = ?", (f["id"],))}
    cfg = E.drill_config(settings)
    now = now_iso()
    touched = {int(f["property_id"])}
    with E._tx(conn):
        for item in [f, *_copies(conn, f)]:
            new = st if st == "wrong" or item["status"] == "wrong" else item["status"]
            conn.execute("UPDATE research_findings SET status=?, marked_by=?, marked_at=? WHERE id=?",
                         (new, actor, now, item["id"]))
            if item["kind"] == "person" and item.get("person_id"):
                if st == "wrong":
                    conn.execute("UPDATE people SET role='rejected', notes=TRIM(COALESCE(notes,'') || ?) WHERE id=? "
                                 "AND source=?", (f" Marked wrong by {actor or 'you'} (was {item['role'] or 'other'}).",
                                                  item["person_id"], SOURCE))
                else:
                    conn.execute("UPDATE people SET role=? WHERE id=? AND source=? AND role='rejected'",
                                 (item["role"] or "other", item["person_id"], SOURCE))
            touched.add(int(item["property_id"]))
        for pid in touched:
            E._refresh(conn, pid, cfg)
    msg = ("Marked wrong" + (" here and on the owner's other properties." if len(touched) > 1 else ".")
           if st == "wrong" else "Kept: the wrong mark is taken back.")
    return {**E._result(conn, int(f["property_id"]), msg, settings),
            "finding": one(conn, "SELECT * FROM research_findings WHERE id = ?", (f["id"],))}


def research_quality(conn: sqlite3.Connection, market_slug: str) -> dict:
    """How AI-found contacts held up: {channel: {untested, verified, wrong, dnc}} in this market."""
    market = market_by_slug(conn, market_slug)
    out: dict[str, dict[str, int]] = {}
    for r in rows(conn, "SELECT cp.channel, cp.status, COUNT(*) AS n FROM contact_points cp JOIN properties p "
                        "ON p.id = cp.property_id WHERE p.market_id = ? AND cp.source = ? GROUP BY 1, 2",
                  (market["id"], SOURCE)):
        out.setdefault(r["channel"], {s: 0 for s in E.CONTACT_STATUSES})[r["status"]] = int(r["n"])
    return out


__all__ = ["MockResearchBackend", "ResearchAnswer", "ResearchCLI", "ResearchConfig", "apply_answer", "blocked_by",
           "build_prompt", "cached_answer", "contact_marked", "deny_rules", "get_research_backend", "mark_finding",
           "named_site", "owner_groups", "parse_answer", "property_research", "research_config", "research_estimate",
           "research_owners", "research_quality", "research_schema", "safe_text", "screen", "url_host",
           "websearch_settings"]
