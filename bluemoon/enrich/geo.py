"""Map coordinates for properties that came in without them.

Order of lookups (cheapest first):
1. The US Census geocoder (free, no key). Many addresses go up as one CSV file to the batch
   endpoint; a handful go one at a time to the one-line endpoint. Census "Tie" answers (two or
   more possible spots) get a second, one-line lookup and are kept when the spots sit close together.
2. Google Geocoding, only for addresses Census could not place, and only when GOOGLE_MAPS_API_KEY
   is in .env. Every Google call is recorded for the cost meter.

Only properties without coordinates are touched. `properties.geocode_quality` says how good the
spot is: `source` (came with the import, set by ingest), `exact`, `approximate`, `google`, `failed`.
Every answer is cached in SQLite, so re-runs are free and never re-pay Google.

This module also holds the small HTTP helpers (`send_with_retry`, `redact`) that `drive.py` uses.
API keys never go into cache keys, logs, results or error messages.
"""
from __future__ import annotations

import csv
import io
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Callable, Iterable, Iterator, Sequence, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..cache import cache_get, cache_put, estimate_cost, make_key, record_call
from ..config import Settings, get_settings, secret
from ..db import dumps, loads, market_by_slug, now_iso, rows
from ..util import clean_str, haversine_miles, normalize_address, normalize_zip

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None
T = TypeVar("T")

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CENSUS_ONELINE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_KEY_NAME = "GOOGLE_MAPS_API_KEY"
GEOCODING_SKU = "geocoding"                 # matches google.prices in config.yaml
CENSUS_PROVIDER = "census_geocoder"
GOOGLE_PROVIDER = "google_geocoding"
CENSUS_MAX_BATCH = 10_000                   # Census accepts up to 10,000 addresses per file
CENSUS_MAX_ADDRESS_CHARS = 100              # Census rejects longer one-line addresses
QUALITIES = ("exact", "approximate", "google", "failed")
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
USER_AGENT = "BlueMoonDealFinder/1.0"
MAX_BACKOFF_SECONDS = 60.0


# --- settings ---------------------------------------------------------------------------------

class GeoConfig(BaseModel):
    """`geocode:` section of config.yaml. Every key is optional; these are the defaults."""
    model_config = ConfigDict(extra="ignore")

    benchmark: str = "Public_AR_Current"
    census_batch_size: int = Field(1000, ge=1, le=CENSUS_MAX_BATCH)
    census_batch_min: int = Field(5, ge=1)          # fewer addresses than this: one-line lookups
    census_timeout_seconds: float = Field(300, gt=0)
    single_timeout_seconds: float = Field(30, gt=0)
    max_retries: int = Field(3, ge=0)
    retry_base_seconds: float = Field(2.0, ge=0)
    tie_max_spread_miles: float = Field(0.5, ge=0)  # Census ties closer than this count as one spot
    google_fallback: bool = True
    google_max_per_run: int = Field(1000, ge=0)
    google_location_types: list[str] = Field(
        default_factory=lambda: ["ROOFTOP", "RANGE_INTERPOLATED", "GEOMETRIC_CENTER"])


def geo_config(settings: Settings | None = None) -> GeoConfig:
    raw = (settings or get_settings()).section("geocode")
    try:
        return GeoConfig(**{k: v for k, v in raw.items() if v is not None})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"config.yaml geocode.{where} has a value I can't use: {first.get('msg')}") from None


# --- HTTP helpers (shared with drive.py) ------------------------------------------------------

_KEY_PARAM = re.compile(r"(?i)\b(key=)[^&\s\"'<>]+")


def redact(text: Any, *secrets: str | None) -> str:
    """Remove API keys from any text before it is logged or shown."""
    out = str(text)
    for s in (*secrets, secret(GOOGLE_KEY_NAME)):
        if s:
            out = out.replace(s, "***")
    return _KEY_PARAM.sub(r"\1***", out)


class _RedactKeysFilter(logging.Filter):
    """httpx logs every request URL at INFO. Google Geocoding needs `key=` in the URL, so scrub it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # never let logging break a run
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True


def _install_log_redaction() -> None:
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactKeysFilter) for f in logger.filters):
            logger.addFilter(_RedactKeysFilter())


_install_log_redaction()


def _sleep(seconds: float) -> None:
    """Pause between retries. Tests replace this to run instantly."""
    time.sleep(seconds)


@dataclass
class HttpResult:
    ok: bool
    status: int | None                  # HTTP status; None when nothing came back
    response: httpx.Response | None
    error: str | None = None            # plain English, never contains a key
    fatal: bool = False                 # retrying will not help (bad key, bad request)
    attempts: int = 0


def backoff_seconds(attempt: int, base: float, response: httpx.Response | None = None) -> float:
    """Wait before retry number `attempt` (0-based): Retry-After when the server sends one,
    else exponential backoff with a little jitter."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(MAX_BACKOFF_SECONDS, max(0.0, float(retry_after)))
            except ValueError:
                pass
    delay = base * (2 ** attempt)
    return min(MAX_BACKOFF_SECONDS, delay + random.uniform(0, base * 0.25))


def send_with_retry(client: httpx.Client, method: str, url: str, *, max_retries: int, base_delay: float,
                    retry_statuses: Iterable[int] = RETRY_STATUSES, **kwargs: Any) -> HttpResult:
    """Send one request; retry with backoff on rate limits (429), server errors (5xx) and network trouble.

    Error text is built from the status code and exception type only, so the URL (which may hold a
    key) never ends up in a message.
    """
    retry_on = frozenset(retry_statuses)
    response: httpx.Response | None = None
    error = "no request sent"
    for attempt in range(max_retries + 1):
        response = None
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            error = "the request timed out"
        except httpx.HTTPError as exc:
            error = f"a network problem ({type(exc).__name__})"
        else:
            if response.status_code < 400:
                return HttpResult(True, response.status_code, response, attempts=attempt + 1)
            error = f"HTTP {response.status_code}"
            if response.status_code not in retry_on:
                return HttpResult(False, response.status_code, response, error, fatal=True, attempts=attempt + 1)
        if attempt < max_retries:
            _sleep(backoff_seconds(attempt, base_delay, response))
    status = response.status_code if response is not None else None
    return HttpResult(False, status, response, error, attempts=max_retries + 1)


def new_client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def chunks(items: Sequence[T], size: int) -> Iterator[list[T]]:
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def plural(n: int, one: str, many: str | None = None) -> str:
    """'1 property', '2,981 properties'."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def call_progress(progress: Progress, fraction: float, message: str) -> None:
    if progress:
        try:
            progress(max(0.0, min(1.0, fraction)), message)
        except Exception:  # a broken progress callback must never break a run
            log.debug("progress callback failed", exc_info=True)


# --- parsed external data ---------------------------------------------------------------------

class GeoAnswer(BaseModel):
    """What we learned about one address. This is what gets cached."""
    quality: str                         # exact | approximate | google | failed
    lat: float | None = None
    lng: float | None = None
    matched_address: str | None = None
    source: str = "census"               # census | google | none
    detail: str | None = None

    @property
    def placed(self) -> bool:
        return self.quality != "failed" and self.lat is not None and self.lng is not None


class CensusBatchRow(BaseModel):
    """One line of the Census batch CSV reply:
    "id","input address","Match|No_Match|Tie","Exact|Non_Exact","matched address","lon,lat","tiger id","side"
    """
    id: str
    input_address: str = ""
    match: str
    match_type: str | None = None
    matched_address: str | None = None
    lng: float | None = None
    lat: float | None = None

    @classmethod
    def from_cells(cls, cells: list[str]) -> "CensusBatchRow | None":
        if len(cells) < 3 or not cells[0].strip():
            return None
        lng = lat = None
        if len(cells) > 5 and "," in cells[5]:
            x, _, y = cells[5].partition(",")
            try:
                lng, lat = float(x), float(y)
            except ValueError:
                lng = lat = None
        return cls(id=cells[0].strip(), input_address=cells[1], match=cells[2].strip(),
                   match_type=(cells[3].strip() or None) if len(cells) > 3 else None,
                   matched_address=(cells[4].strip() or None) if len(cells) > 4 else None,
                   lng=lng, lat=lat)


class CensusCoordinates(BaseModel):
    x: float                             # longitude
    y: float                             # latitude


class CensusAddressMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    matchedAddress: str | None = None
    coordinates: CensusCoordinates
    addressComponents: dict[str, Any] = Field(default_factory=dict)


class CensusOneLineResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    addressMatches: list[CensusAddressMatch] = Field(default_factory=list)


class CensusOneLineResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    result: CensusOneLineResult


class GoogleLocation(BaseModel):
    lat: float
    lng: float


class GoogleGeometry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    location: GoogleLocation
    location_type: str | None = None


class GoogleGeocodeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    geometry: GoogleGeometry
    formatted_address: str | None = None
    partial_match: bool = False


class GoogleGeocodeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    results: list[GoogleGeocodeResult] = Field(default_factory=list)
    error_message: str | None = None


def parse_census_batch(text: str) -> dict[str, CensusBatchRow]:
    """Census batch reply (CSV, no header, any order) -> {id: row}. Unreadable lines are skipped."""
    out: dict[str, CensusBatchRow] = {}
    for cells in csv.reader(io.StringIO(text)):
        try:
            row = CensusBatchRow.from_cells(cells)
        except ValidationError:
            row = None
        if row:
            out[row.id] = row
    return out


def answer_from_batch_row(row: CensusBatchRow) -> GeoAnswer:
    if row.match == "Match" and row.lat is not None and row.lng is not None:
        quality = "exact" if (row.match_type or "").lower() == "exact" else "approximate"
        return GeoAnswer(quality=quality, lat=row.lat, lng=row.lng, matched_address=row.matched_address,
                         source="census")
    if row.match == "No_Match":
        return GeoAnswer(quality="failed", source="census", detail="Census has no match for this address")
    return GeoAnswer(quality="failed", source="census", detail=f"Census answered '{row.match}' with no location")


# --- addresses --------------------------------------------------------------------------------

_UNIT_TAIL = re.compile(
    r"\s*(?:\b(?:APT|APARTMENT|UNIT|STE|SUITE|BLDG|BUILDING|LOT|SPC|SPACE|RM|ROOM|FL|FLOOR)\b\.?|#)\s*[\w-]*\s*$",
    re.IGNORECASE)
_NUMBER_RANGE = re.compile(r"^(\d+)[A-Z]?\s*(?:-|&|/|\bAND\b|\bTHRU\b|\bTO\b)\s*\d+[A-Z]?\s+(.+)$", re.IGNORECASE)


def street_for_geocoding(address: str | None) -> str | None:
    """Clean a street line so geocoders can read it.

    "77-79 Fake Meadow Ct" -> "77 Fake Meadow Ct"; "100 Main St Apt 4" -> "100 Main St";
    "12 Oak St; 14 Oak St" -> "12 Oak St".
    """
    s = clean_str(address)
    if not s:
        return None
    s = re.split(r"[;|]", s)[0]
    s = s.split(",")[0]
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(2):  # "APT 4 UNIT B" style tails
        trimmed = _UNIT_TAIL.sub("", s).strip()
        if trimmed and re.search(r"[A-Za-z]", trimmed):
            s = trimmed
    m = _NUMBER_RANGE.match(s)
    if m:
        s = f"{m.group(1)} {m.group(2)}"
    return s[:CENSUS_MAX_ADDRESS_CHARS].strip() or None


@dataclass(frozen=True)
class AddressQuery:
    street: str
    city: str | None = None
    state: str | None = None
    zip: str | None = None

    @property
    def line(self) -> str:
        """One-line form: "76 E High St, Springfield, OH 45502"."""
        tail = " ".join(p for p in (self.state, self.zip) if p)
        line = ", ".join(p for p in (self.street, self.city, tail) if p)
        return line[:CENSUS_MAX_ADDRESS_CHARS]

    @property
    def norm(self) -> str:
        return re.sub(r"\s+", " ", self.line.upper()).strip()


def query_for(street: str | None, city: str | None = None, state: str | None = None,
              zip_: Any = None) -> AddressQuery | None:
    s = street_for_geocoding(street)
    if not s:
        return None
    st = clean_str(state)
    return AddressQuery(street=s, city=clean_str(city), state=st.upper() if st else None, zip=normalize_zip(zip_))


def census_cache_key(q: AddressQuery, benchmark: str) -> str:
    return make_key(CENSUS_PROVIDER, benchmark, q.norm)


def google_cache_key(q: AddressQuery) -> str:
    return make_key(GOOGLE_PROVIDER, q.norm, _google_components(q))


def _google_components(q: AddressQuery) -> str:
    return "country:US" + (f"|administrative_area:{q.state}" if q.state else "")


# --- Census -----------------------------------------------------------------------------------

def _same_street(ours: str, matched: str | None) -> bool:
    if not matched:
        return False
    return normalize_address(ours) == normalize_address(matched.split(",")[0])


def resolve_matches(matches: list[CensusAddressMatch], q: AddressQuery, max_spread_miles: float) -> GeoAnswer:
    """Turn the one-line lookup's candidate list into one answer.

    One candidate: exact when it is our street, else approximate. Several: narrow by ZIP then city;
    if they still disagree, keep their middle point (approximate) when all sit within
    `max_spread_miles` of each other, otherwise give up (Google may do better).
    """
    if not matches:
        return GeoAnswer(quality="failed", source="census", detail="Census has no match for this address")
    pool = matches
    for attr, want in (("zip", q.zip), ("city", (q.city or "").upper() or None)):
        if want and len(pool) > 1:
            narrowed = [m for m in pool if str(m.addressComponents.get(attr, "")).upper() == want]
            pool = narrowed or pool
    if len(pool) == 1:
        m = pool[0]
        quality = "exact" if _same_street(q.street, m.matchedAddress) else "approximate"
        return GeoAnswer(quality=quality, lat=m.coordinates.y, lng=m.coordinates.x,
                         matched_address=m.matchedAddress, source="census")
    spread = max(haversine_miles(a.coordinates.y, a.coordinates.x, b.coordinates.y, b.coordinates.x)
                 for a, b in combinations(pool, 2))
    if spread <= max_spread_miles:
        lat = sum(m.coordinates.y for m in pool) / len(pool)
        lng = sum(m.coordinates.x for m in pool) / len(pool)
        return GeoAnswer(quality="approximate", lat=lat, lng=lng, matched_address=pool[0].matchedAddress,
                         source="census", detail=f"{len(pool)} close matches; used the middle point")
    return GeoAnswer(quality="failed", source="census",
                     detail=f"{len(pool)} different places match this address ({spread:.1f} miles apart)")


def census_oneline(client: httpx.Client, q: AddressQuery, cfg: GeoConfig) -> GeoAnswer | None:
    """One address through the one-line endpoint. None means Census couldn't be reached (try later)."""
    res = send_with_retry(client, "GET", CENSUS_ONELINE_URL,
                          params={"address": q.line, "benchmark": cfg.benchmark, "format": "json"},
                          timeout=cfg.single_timeout_seconds, max_retries=cfg.max_retries,
                          base_delay=cfg.retry_base_seconds)
    if not res.ok:
        if res.status == 400:
            return GeoAnswer(quality="failed", source="census", detail="Census could not read this address")
        log.warning("Census geocoder did not answer (%s)", res.error)
        return None
    try:
        parsed = CensusOneLineResponse.model_validate(res.response.json())
    except (ValueError, ValidationError):
        log.warning("Census geocoder sent a reply I couldn't read")
        return None
    return resolve_matches(parsed.result.addressMatches, q, cfg.tie_max_spread_miles)


def _batch_csv(queries: Sequence[AddressQuery]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for i, q in enumerate(queries):
        w.writerow([i, q.street.replace(",", " "), q.city or "", q.state or "", q.zip or ""])
    return buf.getvalue().encode("utf-8")


def census_batch(client: httpx.Client, queries: Sequence[AddressQuery], cfg: GeoConfig
                 ) -> tuple[dict[int, CensusBatchRow] | None, str | None]:
    """Many addresses in one upload. Returns ({position in `queries`: row}, None) or (None, error)."""
    res = send_with_retry(client, "POST", CENSUS_BATCH_URL,
                          files={"addressFile": ("addresses.csv", _batch_csv(queries), "text/csv")},
                          data={"benchmark": cfg.benchmark},
                          timeout=cfg.census_timeout_seconds, max_retries=cfg.max_retries,
                          base_delay=cfg.retry_base_seconds)
    if not res.ok:
        if res.status == 400:
            return None, (f"The Census geocoder rejected the request (HTTP 400). Check geocode.benchmark "
                          f"in config.yaml (now '{cfg.benchmark}').")
        return None, f"The Census geocoder didn't answer ({res.error}). Try again later."
    parsed = parse_census_batch(res.response.text)
    out: dict[int, CensusBatchRow] = {}
    for rid, row in parsed.items():
        if rid.isdigit() and int(rid) < len(queries):
            out[int(rid)] = row
    return out, None


# --- Google -----------------------------------------------------------------------------------

@dataclass
class GoogleReply:
    answer: GeoAnswer | None = None      # None: no usable reply (do not cache)
    billable: bool = False
    fatal: str | None = None             # stop calling Google this run


def google_geocode(client: httpx.Client, api_key: str, q: AddressQuery, cfg: GeoConfig) -> GoogleReply:
    params = {"address": q.line, "components": _google_components(q), "key": api_key}
    for attempt in range(cfg.max_retries + 1):
        res = send_with_retry(client, "GET", GOOGLE_GEOCODE_URL, params=params,
                              timeout=cfg.single_timeout_seconds, max_retries=cfg.max_retries,
                              base_delay=cfg.retry_base_seconds)
        if not res.ok:
            if res.fatal:
                return GoogleReply(fatal=f"Google Geocoding refused the request ({res.error}). Check that "
                                         f"the Geocoding API is turned on for the key in .env.")
            return GoogleReply()
        try:
            data = GoogleGeocodeResponse.model_validate(res.response.json())
        except (ValueError, ValidationError):
            return GoogleReply(billable=True)
        if data.status == "OK" and data.results:
            return GoogleReply(answer=_answer_from_google(data.results[0], cfg), billable=True)
        if data.status in ("OK", "ZERO_RESULTS"):
            return GoogleReply(answer=GeoAnswer(quality="failed", source="google",
                                                detail="Google has no match for this address"), billable=True)
        if data.status in ("OVER_QUERY_LIMIT", "UNKNOWN_ERROR") and attempt < cfg.max_retries:
            _sleep(backoff_seconds(attempt, cfg.retry_base_seconds))
            continue
        if data.status == "REQUEST_DENIED":
            why = redact(data.error_message or "no reason given", api_key)
            return GoogleReply(fatal=f"Google Geocoding said no: {why} Check GOOGLE_MAPS_API_KEY in .env and "
                                     f"that the Geocoding API is turned on.")
        if data.status == "INVALID_REQUEST":
            return GoogleReply(answer=GeoAnswer(quality="failed", source="google",
                                                detail="Google could not read this address"))
        return GoogleReply()
    return GoogleReply()


def _answer_from_google(r: GoogleGeocodeResult, cfg: GeoConfig) -> GeoAnswer:
    kind = r.geometry.location_type or "UNKNOWN"
    if kind not in cfg.google_location_types:
        return GeoAnswer(quality="failed", source="google", matched_address=r.formatted_address,
                         detail=f"Google only found a rough area ({kind.lower()}), not the property")
    return GeoAnswer(quality="google", lat=r.geometry.location.lat, lng=r.geometry.location.lng,
                     matched_address=r.formatted_address, source="google",
                     detail="partial match" if r.partial_match else None)


# --- the run ----------------------------------------------------------------------------------

@dataclass
class _Stats:
    cached: int = 0
    census_requests: int = 0
    google_requests: int = 0
    google_capped: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Plan:
    props: list[dict]                                  # properties without coordinates (after limit)
    key_by_pid: dict[int, str]                          # census cache key per property
    queries: dict[str, AddressQuery]                    # unique addresses by census cache key
    no_address: list[int]
    census_hits: dict[str, GeoAnswer]
    google_hits: dict[str, GeoAnswer]


def _missing_coordinates(conn: sqlite3.Connection, market_id: int, limit: int | None) -> list[dict]:
    sql = ("SELECT id, address, city, state, zip, geocode_quality, flags_json FROM properties "
           "WHERE market_id=? AND (lat IS NULL OR lng IS NULL) "
           "ORDER BY CASE WHEN geocode_quality IS NULL THEN 0 ELSE 1 END, id")
    params: list[Any] = [market_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    return rows(conn, sql, params)


def _cached_answer(conn: sqlite3.Connection, key: str) -> GeoAnswer | None:
    hit = cache_get(conn, key)
    if not hit or (hit["status"] or 0) >= 400 or not isinstance(hit["body"], dict):
        return None
    try:
        return GeoAnswer.model_validate(hit["body"])
    except ValidationError:
        return None


def _plan(conn: sqlite3.Connection, market_id: int, limit: int | None, cfg: GeoConfig) -> _Plan:
    props = _missing_coordinates(conn, market_id, limit)
    key_by_pid: dict[int, str] = {}
    queries: dict[str, AddressQuery] = {}
    no_address: list[int] = []
    for p in props:
        q = query_for(p["address"], p["city"], p["state"], p["zip"])
        if q is None:
            no_address.append(p["id"])
            continue
        key = census_cache_key(q, cfg.benchmark)
        key_by_pid[p["id"]] = key
        queries.setdefault(key, q)
    census_hits = {k: a for k in queries if (a := _cached_answer(conn, k)) is not None}
    google_hits = {k: a for k, q in queries.items()
                   if (k not in census_hits or not census_hits[k].placed)
                   and (a := _cached_answer(conn, google_cache_key(q))) is not None}
    return _Plan(props, key_by_pid, queries, no_address, census_hits, google_hits)


def _census_answers(conn: sqlite3.Connection, client: httpx.Client, todo: dict[str, AddressQuery],
                    cfg: GeoConfig, stats: _Stats, progress: Progress) -> dict[str, GeoAnswer]:
    """Ask Census about every address in `todo`. Answers are cached; addresses Census could not be
    reached for are simply missing from the result."""
    answers: dict[str, GeoAnswer] = {}
    items = list(todo.items())
    singles: list[tuple[str, AddressQuery]] = []
    if len(items) >= cfg.census_batch_min:
        batches = list(chunks(items, cfg.census_batch_size))
        for n, batch in enumerate(batches):
            call_progress(progress, 0.1 + 0.5 * n / len(batches),
                          f"Asking the Census geocoder about {plural(len(batch), 'address', 'addresses')}")
            found, error = census_batch(client, [q for _, q in batch], cfg)
            stats.census_requests += 1
            if found is None:
                stats.warnings.append(error or "The Census geocoder didn't answer.")
                continue
            for i, (key, q) in enumerate(batch):
                row = found.get(i)
                if row is None:
                    continue                     # Census left it out; try next run
                if row.match == "Tie":
                    singles.append((key, q))
                else:
                    answers[key] = answer_from_batch_row(row)
                    cache_put(conn, key, CENSUS_PROVIDER, 200, answers[key].model_dump())
    else:
        singles = items
    for n, (key, q) in enumerate(singles):
        call_progress(progress, 0.6 + 0.1 * n / max(1, len(singles)), "Checking addresses one at a time")
        ans = census_oneline(client, q, cfg)
        stats.census_requests += 1
        if ans is None:
            continue                             # Census didn't answer; try next run
        answers[key] = ans
        cache_put(conn, key, CENSUS_PROVIDER, 200, ans.model_dump())
    return answers


def _google_answers(conn: sqlite3.Connection, client: httpx.Client, api_key: str,
                    todo: dict[str, AddressQuery], cfg: GeoConfig, stats: _Stats,
                    progress: Progress) -> dict[str, GeoAnswer]:
    """Google fallback for addresses Census couldn't place. Cached; every billable call recorded."""
    answers: dict[str, GeoAnswer] = {}
    items = list(todo.items())
    if len(items) > cfg.google_max_per_run:
        stats.google_capped = len(items) - cfg.google_max_per_run
        stats.warnings.append(f"Only {plural(cfg.google_max_per_run, 'address', 'addresses')} go to Google per run "
                              f"(geocode.google_max_per_run); the other {stats.google_capped:,} wait for the next run.")
        items = items[:cfg.google_max_per_run]
    for n, (key, q) in enumerate(items):
        call_progress(progress, 0.7 + 0.25 * n / max(1, len(items)), "Asking Google about addresses Census missed")
        reply = google_geocode(client, api_key, q, cfg)
        stats.google_requests += 1
        if reply.billable:
            record_call(conn, "google", GEOCODING_SKU, 1)
        if reply.fatal:
            stats.warnings.append(reply.fatal)
            break
        if reply.answer is not None:
            answers[key] = reply.answer
            cache_put(conn, google_cache_key(q), GOOGLE_PROVIDER, 200, reply.answer.model_dump())
    return answers


def _final_answer(census: GeoAnswer | None, google: GeoAnswer | None) -> GeoAnswer | None:
    """Best answer for one address; None = not known yet (a service didn't answer)."""
    if census is not None and census.placed:
        return census
    if google is not None and google.placed:
        return google
    if google is not None:
        return google              # Google tried and failed too
    return census                  # Census failed (or None when Census was unreachable)


def _apply(conn: sqlite3.Connection, props: list[dict], answer_by_pid: dict[int, GeoAnswer]) -> dict[str, int]:
    counts = {q: 0 for q in QUALITIES}
    now = now_iso()
    with conn:
        for p in props:
            ans = answer_by_pid.get(p["id"])
            if ans is None:
                continue
            counts[ans.quality] = counts.get(ans.quality, 0) + 1
            if not ans.placed:
                conn.execute("UPDATE properties SET geocode_quality='failed', updated_at=? "
                             "WHERE id=? AND (lat IS NULL OR lng IS NULL)", (now, p["id"]))
                continue
            flags = [f for f in (loads(p.get("flags_json"), []) or []) if f != "no_coordinates"]
            conn.execute("UPDATE properties SET lat=?, lng=?, geocode_quality=?, flags_json=?, updated_at=? "
                         "WHERE id=? AND (lat IS NULL OR lng IS NULL)",
                         (round(ans.lat, 7), round(ans.lng, 7), ans.quality, dumps(flags), now, p["id"]))
    return counts


def _google_key(allow_google: bool, cfg: GeoConfig) -> str | None:
    return secret(GOOGLE_KEY_NAME) if (allow_google and cfg.google_fallback) else None


def geocode_estimate(conn: sqlite3.Connection, market_slug: str, limit: int | None = None,
                     allow_google: bool = True) -> dict:
    """Dry run: how many properties need coordinates, how many are already cached, and the worst-case
    Google cost (if Census could place none of them). Makes no calls."""
    settings = get_settings()
    cfg = geo_config(settings)
    market = market_by_slug(conn, market_slug)
    plan = _plan(conn, market["id"], limit, cfg)
    key = _google_key(allow_google, cfg)
    to_lookup = [k for k in plan.queries if k not in plan.census_hits]
    census_failed = [k for k, a in plan.census_hits.items() if not a.placed and k not in plan.google_hits]
    google_worst = min(len(to_lookup) + len(census_failed), cfg.google_max_per_run) if key else 0

    def settled(k: str) -> bool:
        a = plan.census_hits.get(k)
        return a is not None and (a.placed or k in plan.google_hits or not key)

    cached_props = sum(1 for k in plan.key_by_pid.values() if settled(k))
    cost = estimate_cost(conn, GEOCODING_SKU, google_worst, settings)
    msg = _estimate_message(len(plan.props), len(plan.no_address), len(to_lookup), bool(key), cost["usd"])
    return {
        "dry_run": True,
        "properties": len(plan.props),
        "addresses": len(plan.queries),
        "no_address": len(plan.no_address),
        "cached": cached_props,
        "to_lookup": len(to_lookup),
        "google_fallback": bool(key),
        "google_max": google_worst,
        "cost": cost,
        "provider": "census+google" if key else "census",
        "message": msg,
    }


def _estimate_message(props: int, no_address: int, to_lookup: int, google: bool, worst_usd: float) -> str:
    if props == 0:
        return "Every property already has map coordinates."
    msg = (f"Without map coordinates: {plural(props, 'property', 'properties')}. "
           f"To look up with the free Census geocoder: {plural(to_lookup, 'address', 'addresses')}.")
    if no_address:
        msg += f" No street address (can't be placed): {plural(no_address, 'property', 'properties')}."
    if google:
        msg += f" Addresses Census can't place go to Google (at most about ${worst_usd:.2f})."
    return msg


def geocode_missing(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
                    progress: Progress = None, *, client: httpx.Client | None = None,
                    allow_google: bool = True) -> dict:
    """Give map coordinates to every property in the market that has none.

    Census first (free); Google only for what Census can't place, and only with a key. Sets lat, lng
    and geocode_quality (exact | approximate | google | failed). Properties a service didn't answer
    for are left alone so the next run tries them again. Safe to re-run: answers are cached.
    """
    if dry_run:
        return geocode_estimate(conn, market_slug, limit, allow_google)
    settings = get_settings()
    cfg = geo_config(settings)
    market = market_by_slug(conn, market_slug)
    call_progress(progress, 0.02, "Finding properties without map coordinates")
    plan = _plan(conn, market["id"], limit, cfg)
    stats = _Stats(cached=sum(1 for k in plan.key_by_pid.values() if k in plan.census_hits))
    api_key = _google_key(allow_google, cfg)
    cost: dict | None = None

    own_client = client is None
    http = client or new_client()
    try:
        todo = {k: q for k, q in plan.queries.items() if k not in plan.census_hits}
        census = dict(plan.census_hits)
        if todo:
            census.update(_census_answers(conn, http, todo, cfg, stats, progress))
        google = dict(plan.google_hits)
        if api_key:
            need_google = {k: q for k, q in plan.queries.items()
                           if k not in google and not (k in census and census[k].placed)}
            if need_google:
                cost = estimate_cost(conn, GEOCODING_SKU, min(len(need_google), cfg.google_max_per_run), settings)
                google.update(_google_answers(conn, http, api_key, need_google, cfg, stats, progress))
    finally:
        if own_client:
            http.close()

    answer_by_pid: dict[int, GeoAnswer] = {}
    for pid in plan.no_address:
        answer_by_pid[pid] = GeoAnswer(quality="failed", source="none", detail="no street address")
    for pid, key in plan.key_by_pid.items():
        ans = _final_answer(census.get(key), google.get(key))
        if ans is not None:
            answer_by_pid[pid] = ans
    call_progress(progress, 0.97, "Saving map coordinates")
    counts = _apply(conn, plan.props, answer_by_pid)
    result = _summary(plan, counts, stats, answer_by_pid, bool(api_key), cost, settings, conn)
    log.info("Geocoding %s: %s", market_slug, result["message"])
    call_progress(progress, 1.0, result["message"])
    return result


def _summary(plan: _Plan, counts: dict[str, int], stats: _Stats, answer_by_pid: dict[int, GeoAnswer],
             google_on: bool, cost: dict | None, settings: Settings, conn: sqlite3.Connection) -> dict:
    placed = counts["exact"] + counts["approximate"] + counts["google"]
    unresolved = len(plan.props) - len(answer_by_pid)
    failed_bad_address = counts["failed"]
    parts = []
    if not plan.props:
        message = "Every property already has map coordinates."
    else:
        detail = ", ".join(f"{n:,} {label}" for n, label in (
            (counts["exact"], "exact"), (counts["approximate"], "approximate"),
            (counts["google"], "from Google")) if n)
        parts.append(f"Placed {placed:,} of {plural(len(plan.props), 'property', 'properties')} on the map"
                     + (f" ({detail})." if detail else "."))
        if failed_bad_address:
            parts.append(f"Not found: {failed_bad_address:,}; check those addresses"
                         + ("." if google_on else " (a Google key in .env may find some)."))
        if unresolved:
            parts.append(f"Not checked yet: {unresolved:,}, because a geocoder didn't answer; run this again later.")
        message = " ".join(parts)
    return {
        "dry_run": False,
        "properties": len(plan.props),
        "addresses": len(plan.queries),
        "geocoded": placed,
        "exact": counts["exact"],
        "approximate": counts["approximate"],
        "google": counts["google"],
        "failed": counts["failed"],
        "no_address": len(plan.no_address),
        "unresolved": unresolved,
        "cached": stats.cached,
        "census_requests": stats.census_requests,
        "google_requests": stats.google_requests,
        "google_capped": stats.google_capped,
        "cost": cost or estimate_cost(conn, GEOCODING_SKU, 0, settings),
        "provider": "census+google" if google_on else "census",
        "warnings": stats.warnings,
        "message": message,
    }


def geocode_one(conn: sqlite3.Connection, street: str | None, city: str | None = None, state: str | None = None,
                zip_: Any = None, *, client: httpx.Client | None = None, allow_google: bool = True) -> dict:
    """Geocode one address (e.g. typed into the dashboard). Cached like the batch run.

    Returns GeoAnswer fields: {quality, lat, lng, matched_address, source, detail}. Writes nothing
    to `properties`.
    """
    cfg = geo_config()
    q = query_for(street, city, state, zip_)
    if q is None:
        return GeoAnswer(quality="failed", source="none", detail="no street address").model_dump()
    key = census_cache_key(q, cfg.benchmark)
    census = _cached_answer(conn, key)
    own_client = client is None
    http = client or new_client()
    try:
        if census is None:
            census = census_oneline(http, q, cfg)
            if census is not None:
                cache_put(conn, key, CENSUS_PROVIDER, 200, census.model_dump())
        api_key = _google_key(allow_google, cfg)
        google = None
        if (census is None or not census.placed) and api_key:
            google = _cached_answer(conn, google_cache_key(q))
            if google is None:
                reply = google_geocode(http, api_key, q, cfg)
                if reply.billable:
                    record_call(conn, "google", GEOCODING_SKU, 1)
                if reply.answer is not None:
                    google = reply.answer
                    cache_put(conn, google_cache_key(q), GOOGLE_PROVIDER, 200, google.model_dump())
    finally:
        if own_client:
            http.close()
    ans = _final_answer(census, google)
    if ans is None:
        return GeoAnswer(quality="failed", source="none",
                         detail="The geocoder didn't answer; try again later").model_dump()
    return ans.model_dump()
