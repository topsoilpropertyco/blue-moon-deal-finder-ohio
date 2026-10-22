"""County records: the owner of record and the tax mailing address for each property, from the county's own free
public parcel map service (ArcGIS REST). No key, no cost.

For each property with a map point, one point-in-parcel query is sent to the county's layer:

    <layer>/query?geometry=<lng>,<lat>&geometryType=esriGeometryPoint&inSR=4326
                 &spatialRel=esriSpatialRelIntersects&outFields=*&returnGeometry=false&f=json

Which counties, which layer URL and which fields hold the owner and the tax mailing address is all in
`config/parcels.yaml`. An Ohio county with no live source of its own (or one that is switched off) uses the two
statewide services instead; that data is about 2023-24, so everything taken from it says "older statewide data".
Indiana has no source and is left alone.

What is written (see `_save`), all through the DRILL engine's own helpers:
- the owner name(s) on the tax record -> people, role `owner_of_record`, source `county_gis`; the taxpayer or
  contact names -> people, role `other`;
- the tax mailing address -> a `mail` contact, "not tested yet" (do-not-contact addresses come in as `dnc`);
- from a live county layer with an owner: the DRILL stage `county_auditor` is checked off as found by `auto`,
  naming the county and the owner on the tax record. When the county's owner is not the Crexi owner, that is said
  in the same note, in the owner's DRILL notes, and the property gets the flag `county_owner_differs`.
  `properties.owner_name` is never changed and no new DRILL pass starts;
- when nothing is found, nothing is checked off: the stage stays open so you can still look by hand;
- statewide data adds the people and the address but does not check the stage off (it is older; confirm it by
  hand on the auditor page).
Level 3 is never set here: only a conversation confirms an owner.

Every answer from a county (including "no parcel at that point") is saved in `api_cache` (provider `county_gis`)
under the layer and the rounded point, so a re-run asks nobody again. Errors are never saved. `parcel_lookups`
remembers which properties were looked up, so they are not asked again and the progress bar can count them.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..cache import cache_get, cache_put, make_key, record_call
from ..config import Settings, get_settings
from ..db import dumps, loads, market_by_slug, now_iso, one, rows, scalar
from ..drill import engine as E
from ..util import clean_str, normalize_county
from . import geo

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

PROVIDER = "county_gis"           # api_cache provider, api_calls provider and sku, people/contact source
SOURCE = "county_gis"             # sources.yaml key
STAGE = "county_auditor"          # the DRILL stage a live county answer checks off
FLAG = "county_owner_differs"     # properties.flags_json: the county's owner is not the Crexi owner
USER_AGENT = "BlueMoonDealFinder/1.0 (county records; free public parcel services)"
STATEWIDE = "statewide"
STATUSES = ("found", "older", "no_parcel", "no_data", "ambiguous")   # parcel_lookups.status
MAX_NAME = 160


# --------------------------------------------------------------------------------------------
# Settings (config/parcels.yaml)
# --------------------------------------------------------------------------------------------

class MailChoice(BaseModel):
    """One way to build the tax mailing address. `lines` are fields joined with ', '; a line written as a list
    joins its fields with a space ([STATE, ZIP])."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None                       # the addressee field (to spot a tax-bill service)
    lines: list[str | list[str]] = Field(min_length=1)


class Layer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    enabled: bool = True
    url: str
    parcel_id: str | None = None
    as_of: str | None = None                      # a date field saying how old the data is
    owners: list[str] = Field(default_factory=list)
    owner_fallback: list[str] = Field(default_factory=list)
    taxpayers: list[str] = Field(default_factory=list)
    mailing: list[MailChoice] = Field(default_factory=list)
    note: str | None = None


class ParcelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = "all"                            # all = every property, buy box first | buybox = buy box only
    concurrency: int = Field(5, ge=1, le=10)
    pause_seconds: float = Field(0.1, ge=0)
    timeout_seconds: float = Field(25.0, gt=0)
    max_retries: int = Field(2, ge=0)
    retry_backoff_seconds: float = Field(1.0, ge=0)
    stop_after_errors: int = Field(5, ge=1)
    chunk_size: int = Field(25, ge=1)
    coordinate_decimals: int = Field(5, ge=3, le=7)
    statewide_fallback: bool = True
    tax_service_names: list[str] = Field(default_factory=list)
    ignore_values: list[str] = Field(default_factory=list)


class StatewideConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    label: str = "older statewide data"
    mailing_layer: Layer | None = None
    owner_layer: Layer | None = None


class ParcelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: ParcelSettings = Field(default_factory=ParcelSettings)
    counties: dict[str, dict[str, Layer]] = Field(default_factory=dict)
    statewide: StatewideConfig = Field(default_factory=StatewideConfig)

    def source_for(self, state: str | None, county: str | None) -> Source | None:
        """Where to look this property up: its county's own layer, else the statewide data (Ohio only), else None."""
        st = (clean_str(state) or "").upper()
        if st != "OH" and st not in self.counties:
            return None
        cnty = normalize_county(county) or ""
        layer = (self.counties.get(st) or {}).get(cnty)
        if layer is not None and layer.enabled:
            return Source(kind="county", key=f"{st}:{cnty}", label=f"{layer.name or cnty.title()} County",
                          layers=(layer,))
        sw = self.statewide
        if st == "OH" and self.settings.statewide_fallback and sw.enabled:
            layers = tuple(x for x in (sw.owner_layer, sw.mailing_layer) if x is not None and x.enabled)
            if layers:
                return Source(kind=STATEWIDE, key=f"{st}:{STATEWIDE}", label=sw.label, layers=layers)
        return None


@dataclass(frozen=True)
class Source:
    kind: str                    # county | statewide
    key: str                     # OH:GREENE | OH:statewide
    label: str                   # 'Greene County' | 'older statewide data'
    layers: tuple[Layer, ...]

    @property
    def statewide(self) -> bool:
        return self.kind == STATEWIDE

    @property
    def records(self) -> str:
        """How the notes name the data: 'Greene County records' / 'older statewide data'."""
        return self.label if self.statewide else f"{self.label} records"

    @property
    def short(self) -> str:
        return "Statewide (older)" if self.statewide else self.label.removesuffix(" County")


def load_config(settings: Settings | None = None) -> ParcelsConfig:
    """config/parcels.yaml as a ParcelsConfig. ValueError (plain English) when it can't be read."""
    s = settings or get_settings()
    raw = getattr(s, "parcels", None) or {}
    try:
        return ParcelsConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"config/parcels.yaml: {first.get('msg', 'a value is wrong')} (at {where}).") from None


# --------------------------------------------------------------------------------------------
# Reading a county's answer
# --------------------------------------------------------------------------------------------

class Parcel(BaseModel):
    """What one parcel record says."""
    parcel_id: str | None = None
    owners: list[str] = Field(default_factory=list)
    taxpayers: list[str] = Field(default_factory=list)
    mailing: str | None = None
    mailing_to: str | None = None                  # who the tax mailing address is addressed to
    as_of: str | None = None                       # 'YYYY-MM' when the layer says how old the data is

    @property
    def has_data(self) -> bool:
        return bool(self.owners or self.taxpayers or self.mailing)


class _Reader:
    """Cleans values from one record's attributes (field names are matched ignoring case)."""

    def __init__(self, attrs: dict[str, Any], ps: ParcelSettings):
        self.attrs = {str(k).lower(): v for k, v in (attrs or {}).items()}
        self.ignore = {v.upper() for v in ps.ignore_values}
        self.tax_services = [v.upper() for v in ps.tax_service_names]

    def get(self, field_name: str | None, *, lines: bool = False) -> str | None:
        if not field_name:
            return None
        v = self.attrs.get(field_name.lower())
        if v is None or isinstance(v, (dict, list)):
            return None
        text = str(v)
        text = re.sub(r"[\r\n]+", ", " if lines else " ", text)
        text = re.sub(r"\s+", " ", text).strip(" ,")
        text = re.sub(r"(\s*,\s*)+", ", ", text)
        if not text or text.upper() in self.ignore:
            return None
        return text

    def names(self, fields: Sequence[str]) -> list[str]:
        out: list[str] = []
        for f in fields:
            n = self.get(f)
            if not n or len(n) > MAX_NAME or E._clean_owner(n) is None:
                continue
            # 'CITY OF MASON' and 'CITY OF MASON, OHIO' are one owner written twice
            if any(E._name_key(x) == E._name_key(n) or E._same_owner_written_differently([x], n) for x in out):
                continue
            out.append(n)
        return out

    def is_tax_service(self, name: str | None) -> bool:
        up = (name or "").upper()
        return bool(up) and any(t in up for t in self.tax_services)

    def address(self, choice: MailChoice) -> tuple[str | None, str | None]:
        """(address, addressee) for one choice, or (None, None) when it has no street or goes to a tax service."""
        to = self.get(choice.name)
        if self.is_tax_service(to):
            return None, None
        parts: list[str] = []
        for line in choice.lines:
            if isinstance(line, list):
                text = " ".join(x for x in (self.get(f, lines=True) for f in line) if x)
            else:
                text = self.get(line, lines=True) or ""
            parts.append(text)
        if not parts[0]:
            return None, None
        return ", ".join(p for p in parts if p), to


def _as_of(value: Any) -> str | None:
    """'2023-10' from an ArcGIS date (milliseconds since 1970)."""
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, UTC).strftime("%Y-%m")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_features(layer: Layer, features: Sequence[dict], ps: ParcelSettings) -> list[Parcel]:
    """The parcels in a county's answer. Right-of-way records ('ROW') and empty ones are dropped."""
    out: list[Parcel] = []
    for feature in features or []:
        rd = _Reader((feature or {}).get("attributes") or {}, ps)
        pid = rd.get(layer.parcel_id)
        if pid and pid.upper() == "ROW":
            continue
        owners = rd.names(layer.owners) or rd.names(layer.owner_fallback)
        owner_keys = {E._name_key(o) for o in owners}
        taxpayers = [t for t in rd.names(layer.taxpayers)
                     if E._name_key(t) not in owner_keys and not rd.is_tax_service(t)]
        mailing = mailing_to = None
        for choice in layer.mailing:
            mailing, mailing_to = rd.address(choice)
            if mailing:
                break
        raw_date = rd.attrs.get((layer.as_of or "").lower())
        out.append(Parcel(parcel_id=pid, owners=owners, taxpayers=taxpayers, mailing=mailing, mailing_to=mailing_to,
                          as_of=_as_of(raw_date) if raw_date is not None else None))
    return out


def names_match(crexi_owner: str | None, county_owners: Sequence[str]) -> bool:
    """Is the Crexi owner one of the county's owner names (same name, or written differently, like 'DOE JOHN'
    and 'John A Doe')?"""
    mine = E._clean_owner(crexi_owner)
    if not mine:
        return True
    key = E._name_key(mine)
    for name in county_owners:
        if E._name_key(name) == key or E._same_owner_written_differently([mine], name):
            return True
    return False


def pick_parcel(parcels: Sequence[Parcel], crexi_owner: str | None) -> tuple[Parcel | None, bool]:
    """(the parcel to use, ambiguous). Several parcels at one point (a condo building) with the same owners are
    one answer; with different owners the one that matches the Crexi owner wins; otherwise it is ambiguous and
    nothing is saved."""
    usable = [p for p in parcels if p.has_data]
    if not usable:
        return None, False
    groups: dict[frozenset[str], list[Parcel]] = defaultdict(list)
    for p in usable:
        groups[frozenset(E._name_key(o) for o in p.owners)].append(p)
    if len(groups) == 1:
        return next((p for p in usable if p.mailing), usable[0]), False
    for p in usable:
        if p.owners and names_match(crexi_owner, p.owners) and E._clean_owner(crexi_owner):
            return p, False
    return None, True


# --------------------------------------------------------------------------------------------
# Asking a county
# --------------------------------------------------------------------------------------------

def make_client(cfg: ParcelSettings) -> httpx.Client:
    """The HTTP client for county services (tests replace this)."""
    return httpx.Client(timeout=cfg.timeout_seconds, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def point(lat: float, lng: float, ps: ParcelSettings) -> tuple[float, float]:
    return round(float(lat), ps.coordinate_decimals), round(float(lng), ps.coordinate_decimals)


def cache_key(layer: Layer, lat: float, lng: float, ps: ParcelSettings) -> str:
    plat, plng = point(lat, lng, ps)
    return make_key(PROVIDER, layer.url, plat, plng)


def query_params(lat: float, lng: float) -> dict[str, str]:
    return {"geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "returnGeometry": "false", "f": "json"}


@dataclass
class Answer:
    ok: bool
    body: dict | None = None
    error: str | None = None
    skipped: bool = False            # not asked: the layer already failed too often in this run


class _Asker:
    """Sends the point queries (a few at a time), with retries and a stop for a layer that keeps failing."""

    def __init__(self, client: httpx.Client, ps: ParcelSettings):
        self.client, self.ps = client, ps
        self.lock = threading.Lock()
        self.failures: Counter[str] = Counter()          # consecutive failures per layer url
        self.tripped: set[str] = set()

    def ask(self, layer: Layer, lat: float, lng: float) -> Answer:
        with self.lock:
            if layer.url in self.tripped:
                return Answer(False, error="this service stopped answering in this run", skipped=True)
        plat, plng = point(lat, lng, self.ps)
        answer = self._send(layer, plat, plng)
        with self.lock:
            if answer.ok:
                self.failures[layer.url] = 0
            else:
                self.failures[layer.url] += 1
                if self.failures[layer.url] >= self.ps.stop_after_errors:
                    self.tripped.add(layer.url)
        if self.ps.pause_seconds:
            time.sleep(self.ps.pause_seconds)
        return answer

    def _send(self, layer: Layer, lat: float, lng: float) -> Answer:
        url = layer.url.rstrip("/") + "/query"
        error = "no answer"
        for attempt in range(self.ps.max_retries + 1):
            res = geo.send_with_retry(self.client, "GET", url, max_retries=0, base_delay=0, params=query_params(lat, lng))
            if not res.ok:
                error = res.error or "no answer"
                if res.fatal:          # a 4xx other than 429: asking again will not help
                    return Answer(False, error=error)
            else:
                try:
                    body = res.response.json()  # type: ignore[union-attr]
                except ValueError:
                    return Answer(False, error="the answer was not readable")
                if isinstance(body, dict) and isinstance(body.get("features"), list) and "error" not in body:
                    return Answer(True, body=body)
                err = body.get("error") if isinstance(body, dict) else None
                code = err.get("code") if isinstance(err, dict) else None
                error = f"the service said: {clean_str((err or {}).get('message')) or 'an error'}" if err else \
                    "the answer had no parcel list"
                if not (isinstance(code, int) and code >= 500):
                    return Answer(False, error=error)
            if attempt < self.ps.max_retries:
                geo._sleep(geo.backoff_seconds(attempt, self.ps.retry_backoff_seconds, res.response))
        return Answer(False, error=error)


# --------------------------------------------------------------------------------------------
# Which properties
# --------------------------------------------------------------------------------------------

@dataclass
class Target:
    id: int
    lat: float
    lng: float
    owner_name: str | None
    in_buybox: bool
    source: Source


@dataclass
class Plan:
    targets: list[Target]
    total: int = 0
    done: int = 0                 # already looked up
    checked: int = 0              # the county auditor stage was already checked off (by hand or by this step)
    no_coordinates: int = 0
    no_source: int = 0            # Indiana, or no county service and no statewide data
    skipped_status: int = 0       # confirmed or do-not-contact owners
    more: int = 0                 # candidates left out by the limit


def _chunks(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), max(1, size)):
        yield list(items[i:i + max(1, size)])


def plan_targets(conn: sqlite3.Connection, market: dict, cfg: ParcelsConfig, limit: int | None = None) -> Plan:
    """The properties still to look up, buy box first (best score first), then the rest."""
    plan = Plan(targets=[])
    checked = {int(r["property_id"]) for r in rows(
        conn, "SELECT DISTINCT a.property_id FROM drill_attempts a JOIN properties p ON p.id = a.property_id "
              "WHERE p.market_id = ? AND a.stage_key = ?", (market["id"], STAGE))}
    found: list[Target] = []
    sql = """
        SELECT p.id, p.lat, p.lng, p.state, p.county, p.owner_name, s.in_buybox AS in_box, s.score AS score,
               s.property_id AS scored, d.status AS status, l.property_id AS looked_up
        FROM properties p
        LEFT JOIN scores s ON s.property_id = p.id
        LEFT JOIN drill_state d ON d.property_id = p.id
        LEFT JOIN parcel_lookups l ON l.property_id = p.id
        WHERE p.market_id = ?
        ORDER BY COALESCE(s.in_buybox, 0) DESC, COALESCE(s.score, -1) DESC, p.id"""
    for r in rows(conn, sql, (market["id"],)):
        plan.total += 1
        if r["looked_up"] is not None:
            plan.done += 1
            continue
        if r["lat"] is None or r["lng"] is None:
            plan.no_coordinates += 1
            continue
        src = cfg.source_for(r["state"], r["county"])
        if src is None:
            plan.no_source += 1
            continue
        if cfg.settings.scope == "buybox" and r["scored"] is not None and not r["in_box"]:
            continue
        if int(r["id"]) in checked:
            plan.checked += 1
            continue
        if (r["status"] or "active") in ("dnc", "confirmed"):
            plan.skipped_status += 1
            continue
        found.append(Target(int(r["id"]), float(r["lat"]), float(r["lng"]), r["owner_name"], bool(r["in_box"]), src))
    if limit is not None and len(found) > limit:
        plan.more = len(found) - limit
        found = found[:limit]
    plan.targets = found
    return plan


def _requests(targets: Sequence[Target], ps: ParcelSettings) -> dict[str, tuple[Layer, float, float]]:
    """{cache key: (layer, lat, lng)} of every distinct question these properties need answered."""
    out: dict[str, tuple[Layer, float, float]] = {}
    for t in targets:
        for layer in t.source.layers:
            out.setdefault(cache_key(layer, t.lat, t.lng, ps), (layer, t.lat, t.lng))
    return out


def _cached_keys(conn: sqlite3.Connection, keys: Sequence[str]) -> set[str]:
    have: set[str] = set()
    for chunk in _chunks(list(keys), 500):
        marks = ",".join("?" for _ in chunk)
        have |= {r["key"] for r in rows(conn, f"SELECT key FROM api_cache WHERE status < 400 AND key IN ({marks})", chunk)}
    return have


# --------------------------------------------------------------------------------------------
# Estimate
# --------------------------------------------------------------------------------------------

def parcel_estimate(conn: sqlite3.Connection, market_slug: str, limit: int | None = None,
                    settings: Settings | None = None) -> dict:
    """What a run would do, without asking anyone: properties with a map point per county, and how many answers
    are already saved."""
    cfg = load_config(settings)
    market = market_by_slug(conn, market_slug)
    plan = plan_targets(conn, market, cfg, limit)
    reqs = _requests(plan.targets, cfg.settings)
    have = _cached_keys(conn, list(reqs))
    per: dict[str, dict] = {}
    for t in plan.targets:
        row = per.setdefault(t.source.short, {"properties": 0, "requests": 0, "cached": 0, "older": t.source.statewide})
        row["properties"] += 1
        for layer in t.source.layers:
            row["requests"] += 1
            row["cached"] += cache_key(layer, t.lat, t.lng, cfg.settings) in have
    n = len(plan.targets)
    to_fetch = len(reqs) - len(have)
    out = {"market": market_slug, "limit": limit, "dry_run": True, "needs_confirm": False, "properties": plan.total,
           "with_coordinates": plan.total - plan.no_coordinates, "to_look_up": n, "already_done": plan.done,
           "checked": plan.checked, "no_coordinates": plan.no_coordinates, "no_source": plan.no_source,
           "skipped_status": plan.skipped_status, "left_for_later": plan.more, "requests": len(reqs),
           "cached": len(have), "to_fetch": to_fetch, "by_county": per,
           "cost": {"usd": 0.0, "units": 0}}
    out["message"] = _estimate_message(out)
    return out


def _estimate_message(e: dict) -> str:
    n = int(e["to_look_up"])
    if not n:
        why = []
        if e["already_done"]:
            why.append(f"{e['already_done']:,} already looked up")
        if e["no_coordinates"]:
            why.append(f"{e['no_coordinates']:,} with no map location yet (the drive step places them)")
        if e["no_source"]:
            why.append(f"{e['no_source']:,} with no county service (Indiana or an unlisted county)")
        return "Free. Nothing to look up" + (": " + "; ".join(why) if why else " yet") + "."
    parts = [f"{c} {v['properties']:,}" for c, v in sorted(e["by_county"].items(), key=lambda kv: -kv[1]["properties"])[:8]]
    text = (f"Free (county websites, no key). {n:,} propert{'y' if n == 1 else 'ies'} with a map point to look up "
            f"({', '.join(parts)}). {e['cached']:,} of {e['requests']:,} answers are already saved; "
            f"{e['to_fetch']:,} would be asked.")
    if e["left_for_later"]:
        text += f" {e['left_for_later']:,} more wait for the next run."
    if e["no_coordinates"]:
        text += f" {e['no_coordinates']:,} have no map location yet."
    return text


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------

@dataclass
class Tally:
    counts: Counter = field(default_factory=Counter)
    by_source: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)      # error text -> times
    fetched: int = 0
    cached: int = 0
    people_added: int = 0
    contacts_added: int = 0


def lookup_parcels(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
                   progress: Progress = None, *, client: httpx.Client | None = None,
                   settings: Settings | None = None) -> dict:
    """Look up the county records for the market's properties (buy box first). See the module docstring.

    limit: at most this many properties this run. dry_run: count only, ask nobody, save nothing.
    Result: {properties, found, older, no_parcel, no_data, ambiguous, errors, skipped_dnc, owner_differs, fetched,
    cached, people_added, contacts_added, by_source, message, warnings}."""
    if dry_run:
        return parcel_estimate(conn, market_slug, limit, settings)
    cfg = load_config(settings)
    dcfg = E.drill_config(settings)
    market = market_by_slug(conn, market_slug)
    E.ensure_states(conn, market_slug)
    plan = plan_targets(conn, market, cfg, limit)
    tally = Tally()
    targets = plan.targets
    if not targets:
        return _result(market_slug, plan, tally, 0, cfg)
    own = client is None
    http = client or make_client(cfg.settings)
    asker = _Asker(http, cfg.settings)
    index = E.build_sibling_index(conn, settings)
    done = 0
    try:
        _tell(progress, 0.0, f"County records for {len(targets):,} propert{'y' if len(targets) == 1 else 'ies'}")
        for chunk in _chunks(targets, cfg.settings.chunk_size):
            dnc_props = {int(r["property_id"]) for r in rows(conn, "SELECT property_id FROM drill_state WHERE status='dnc'")}
            work = []
            for t in chunk:
                if _family(index, t.id) & dnc_props:
                    tally.counts["skipped_dnc"] += 1
                else:
                    work.append(t)
            done += len(chunk) - len(work)
            answers = _fetch(conn, asker, work, cfg.settings, tally)
            dnc = E._dnc_keys(conn)
            for t in work:
                _handle(conn, dcfg, cfg, index, t, answers, dnc, tally)
                done += 1
                _tell(progress, done / len(targets), f"County records {done:,} of {len(targets):,}")
    finally:
        if own:
            http.close()
    _tell(progress, 1.0, "County records done")
    return _result(market_slug, plan, tally, len(targets), cfg)


def _tell(progress: Progress, fraction: float, message: str) -> None:
    if progress is None:
        return
    try:
        progress(max(0.0, min(1.0, fraction)), message)
    except Exception:  # a broken progress bar must never stop a run; a cancel (BaseException) still does
        log.debug("progress callback failed", exc_info=True)


def _family(index: E.SiblingIndex, property_id: int) -> set[int]:
    """The property and the owner's other properties (same portfolio or same owner name), in any market."""
    fam = {property_id}
    for k in index.keys_of.get(property_id, ()):
        if k.startswith(("pk:", "name:")):
            fam |= index.members.get(k, set())
    return fam


def _fetch(conn: sqlite3.Connection, asker: _Asker, work: Sequence[Target], ps: ParcelSettings,
           tally: Tally) -> dict[str, Answer]:
    """The answer for every question these properties need: from the saved answers, else asked (a few at a
    time) and saved. Errors are never saved."""
    answers: dict[str, Answer] = {}
    todo: dict[str, tuple[Layer, float, float]] = {}
    for key, (layer, lat, lng) in _requests(work, ps).items():
        hit = cache_get(conn, key)
        if hit is not None and (hit["status"] or 0) < 400 and isinstance(hit["body"], dict):
            answers[key] = Answer(True, body=hit["body"])
            tally.cached += 1
        else:
            todo[key] = (layer, lat, lng)
    if not todo:
        return answers
    pool = ThreadPoolExecutor(max_workers=ps.concurrency)
    try:
        futures = {key: pool.submit(asker.ask, layer, lat, lng) for key, (layer, lat, lng) in todo.items()}
        for key, fut in futures.items():
            try:
                answer = fut.result()
            except Exception as exc:   # one odd answer must not stop the run
                log.warning("County lookup failed: %s", type(exc).__name__)
                answer = Answer(False, error=f"an unexpected problem ({type(exc).__name__})")
            answers[key] = answer
            if answer.ok:
                cache_put(conn, key, PROVIDER, 200, answer.body)
                record_call(conn, PROVIDER, PROVIDER)
                tally.fetched += 1
            elif not answer.skipped:
                tally.errors[f"{_host(todo[key][0])}: {answer.error}"] += 1
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return answers


def _host(layer: Layer) -> str:
    return layer.name or re.sub(r"^https?://([^/]+).*$", r"\1", layer.url)


@dataclass
class Hit:
    """What the lookup of one property found."""
    source: Source
    owner: Parcel | None = None          # the parcel read for the owner name(s)
    mail: Parcel | None = None           # the parcel read for the mailing address
    ambiguous: bool = False
    parcel_seen: bool = False            # a parcel with data was in the answer


def _read(source: Source, answers: dict[str, Answer], t: Target, ps: ParcelSettings) -> Hit | None:
    """Read the answers for one property. None when an answer is missing (an error: ask again next run)."""
    hit = Hit(source=source)
    picks: dict[str, Parcel | None] = {}
    for layer in source.layers:
        answer = answers.get(cache_key(layer, t.lat, t.lng, ps))
        if answer is None or not answer.ok or answer.body is None:
            return None
        parcels = parse_features(layer, answer.body.get("features") or [], ps)
        hit.parcel_seen = hit.parcel_seen or any(p.has_data for p in parcels)
        picked, amb = pick_parcel(parcels, t.owner_name)
        hit.ambiguous = hit.ambiguous or amb
        picks[layer.url] = picked
    if source.statewide:
        for layer in source.layers:
            if layer.owners:
                hit.owner = picks.get(layer.url)
            if layer.mailing:
                hit.mail = picks.get(layer.url)
    else:
        hit.owner = hit.mail = picks.get(source.layers[0].url)
    return hit


def _handle(conn: sqlite3.Connection, dcfg: E.DrillConfig, cfg: ParcelsConfig, index: E.SiblingIndex, t: Target,
            answers: dict[str, Answer], dnc: set[tuple[str, str]], tally: Tally) -> None:
    hit = _read(t.source, answers, t, cfg.settings)
    if hit is None:
        tally.counts["errors"] += 1
        return
    tally.by_source[t.source.short] += 1
    with E._tx(conn):
        if _family(index, t.id) & {int(r["property_id"]) for r in rows(
                conn, "SELECT property_id FROM drill_state WHERE status='dnc'")}:   # marked since the run began
            tally.counts["skipped_dnc"] += 1
            return
        status = _save(conn, dcfg, t, hit, dnc, tally)
    tally.counts[status] += 1


def _save(conn: sqlite3.Connection, dcfg: E.DrillConfig, t: Target, hit: Hit, dnc: set[tuple[str, str]],
          tally: Tally) -> str:
    """Write what the lookup found (inside the caller's transaction). Returns the parcel_lookups status."""
    owner, mail = hit.owner, hit.mail
    label = hit.source.label
    names = owner.owners if owner else []
    if hit.ambiguous or not (owner or mail) or not hit.parcel_seen:
        status = "ambiguous" if hit.ambiguous else "no_parcel"
        _remember(conn, t, hit, status, None, False)
        return status
    E._ensure_state_row(conn, t.id)
    ctx = E._ctx(conn, t.id)
    crexi = E._clean_owner(t.owner_name)
    differs = bool(names) and bool(crexi) and not names_match(crexi, names)
    when = (owner or mail).as_of if (owner or mail) else None
    older = f" (older data, as of {when})" if hit.source.statewide and when else ""
    added_people = added_contacts = 0
    first_pid: int | None = None
    known = ([crexi] if crexi else []) + [p["name"] for p in ctx.people if p.get("role") in E.OWNER_NAME_ROLES]
    for name in names:
        if hit.source.statewide:
            if any(names_match(k, [name]) for k in known):
                continue       # older data that agrees with what we have adds nothing
            role, note = ("possible_owner" if not known else "other"), f"Owner in the {label}{older}."
        else:
            role, note = "owner_of_record", f"Owner on the {label} tax record."
        pid, created = E._add_person(conn, t.id, name, role=role, source=SOURCE, notes=note)
        first_pid = first_pid or pid
        added_people += created
    owner_keys = {E._name_key(n) for n in names}
    for name in (owner.taxpayers if owner else []) + ((mail.taxpayers if mail and mail is not owner else [])):
        if E._name_key(name) in owner_keys:
            continue
        _, created = E._add_person(conn, t.id, name, role="other", source=SOURCE,
                                   notes=f"Taxpayer or contact in the {hit.source.records}{older}.")
        added_people += created
    if mail and mail.mailing:
        to = f" addressed to {mail.mailing_to}" if mail.mailing_to else ""
        _, made = E._add_contact(conn, t.id, "mail", mail.mailing, source=SOURCE, person_id=first_pid, dnc_keys=dnc,
                                 notes=f"Tax mailing address in the {hit.source.records}{to}{older}.")
        added_contacts += int(made)
    tally.people_added += added_people
    tally.contacts_added += added_contacts
    live_owner = bool(names) and not hit.source.statewide
    if live_owner:
        pid_text = f" (parcel {owner.parcel_id})" if owner and owner.parcel_id else ""
        detail = f"{label} tax record{pid_text}: owner {', '.join(names)}."
        if mail and mail.mailing:
            detail += f" Tax mailing address: {mail.mailing}."
        else:
            detail += " No tax mailing address in that record."
        if differs:
            detail += f" This is not the owner in the Crexi export ({crexi})."
        E._insert_attempt(conn, t.id, STAGE, "found", detail, "auto")
    if differs and not hit.source.statewide:
        _flag_differs(conn, t.id)
        E._append_note(conn, t.id, f"County records show a different owner than the Crexi export: {', '.join(names)} "
                                   f"({label}); Crexi says {crexi}. Check the county auditor page.")
        tally.counts["owner_differs"] += 1
    status = "found" if live_owner else ("older" if hit.source.statewide and (names or (mail and mail.mailing)) else "no_data")
    _remember(conn, t, hit, status, (detail if live_owner else None), differs and not hit.source.statewide)
    E._refresh(conn, t.id, dcfg, new_info=bool(live_owner or added_people or added_contacts))
    return status


def _flag_differs(conn: sqlite3.Connection, property_id: int) -> None:
    """Add the flag to flags_json (properties.updated_at is not touched: this is not a change to the property)."""
    old = loads(scalar(conn, "SELECT flags_json FROM properties WHERE id=?", (property_id,)), []) or []
    if FLAG not in old:
        conn.execute("UPDATE properties SET flags_json=? WHERE id=?", (dumps(sorted([*old, FLAG])), property_id))


def _remember(conn: sqlite3.Connection, t: Target, hit: Hit, status: str, detail: str | None, differs: bool) -> None:
    conn.execute("INSERT OR REPLACE INTO parcel_lookups(property_id, source, status, owner_differs, detail, checked_at) "
                 "VALUES(?,?,?,?,?,?)", (t.id, t.source.key, status, int(differs), detail, now_iso()))


def _result(slug: str, plan: Plan, tally: Tally, n: int, cfg: ParcelsConfig) -> dict:
    c = tally.counts
    out = {"market": slug, "dry_run": False, "properties": n, "found": c["found"], "older": c["older"],
           "no_parcel": c["no_parcel"], "no_data": c["no_data"], "ambiguous": c["ambiguous"], "errors": c["errors"],
           "skipped_dnc": c["skipped_dnc"], "owner_differs": c["owner_differs"], "fetched": tally.fetched,
           "cached": tally.cached, "people_added": tally.people_added, "contacts_added": tally.contacts_added,
           "by_source": dict(tally.by_source), "left_for_later": plan.more, "no_coordinates": plan.no_coordinates,
           "no_source": plan.no_source}
    out["warnings"] = [f"{count}x {text}" for text, count in tally.errors.most_common(5)]
    out["message"] = _message(out)
    return out


def _message(r: dict) -> str:
    if not r["properties"]:
        return _estimate_message({"to_look_up": 0, "already_done": 0, "no_coordinates": r["no_coordinates"],
                                  "no_source": r["no_source"]}).replace("Free. Nothing", "Nothing")
    parts = [f"Looked up {r['properties']:,} propert{'y' if r['properties'] == 1 else 'ies'} in the county records "
             f"(free): {r['found']:,} with the owner on the tax record"]
    if r["older"]:
        parts.append(f"{r['older']:,} from older statewide data (2023-24; check the auditor page)")
    if r["no_parcel"] + r["no_data"]:
        parts.append(f"{r['no_parcel'] + r['no_data']:,} with no usable record (check those by hand)")
    if r["ambiguous"]:
        parts.append(f"{r['ambiguous']:,} on a parcel shared by different owners (left for you)")
    text = ", ".join(parts) + "."
    if r["owner_differs"]:
        text += (f" {r['owner_differs']:,} show a different owner than Crexi: look for the flag "
                 f"\"County owner differs\".")
    if r["skipped_dnc"]:
        text += f" Left out {r['skipped_dnc']:,} of do-not-contact owners."
    if r["errors"]:
        text += f" {r['errors']:,} lookups failed and will be tried again next run."
    text += f" Added {r['people_added']:,} people and {r['contacts_added']:,} mailing addresses (not tested yet)."
    text += f" {r['cached']:,} answers were already saved; {r['fetched']:,} asked."
    if r["left_for_later"]:
        text += f" {r['left_for_later']:,} more wait for the next run."
    return text


def layer_counts(conn: sqlite3.Connection, market_id: int) -> tuple[int, int]:
    """(looked up, Ohio properties with a map point) for the progress bar."""
    r = one(conn, "SELECT COUNT(*) AS total, COUNT(l.property_id) AS done FROM properties p "
                  "LEFT JOIN parcel_lookups l ON l.property_id = p.id "
                  "WHERE p.market_id = ? AND UPPER(COALESCE(p.state, '')) = 'OH' AND p.lat IS NOT NULL "
                  "AND p.lng IS NOT NULL", (market_id,)) or {}
    return int(r.get("done") or 0), int(r.get("total") or 0)


__all__ = ["Layer", "Parcel", "ParcelsConfig", "load_config", "lookup_parcels", "parcel_estimate", "parse_features",
           "pick_parcel", "layer_counts", "names_match", "PROVIDER", "STAGE", "FLAG"]
