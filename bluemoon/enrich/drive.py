"""Drive time from each market's origin (e.g. downtown Springfield) to every property.

With GOOGLE_MAPS_API_KEY in .env: Google Routes `computeRouteMatrix`. One origin and up to
`drive.batch_size` destinations go in each request (Google allows 625 elements per request, 100 for
TRAFFIC_AWARE_OPTIMAL), `google.concurrency` requests run at once, and a shared limiter keeps us under
Google's 3,000 elements a minute. Each answer is cached per (origin, destination) rounded to 5 decimals
(about 1 m), so re-runs, and other markets with the same origin, never pay twice.

Without a key, or with `drive.provider: estimate`: straight-line miles x `drive.estimate.road_factor`
at `drive.estimate.avg_mph`, saved with method `estimate` so the dashboard can show "approximate".
Properties far outside the market's drive limit (estimate > `drive.google_max_estimate_ratio` x
max minutes) also keep the estimate: a Google answer would only cost money and change nothing.

Missing coordinates are filled first by `geo.geocode_missing` (free US Census geocoder).
The key goes only in the X-Goog-Api-Key header: never in cache keys, logs, results or messages.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import geo
from ..cache import cache_put, estimate_cost, make_key, record_call
from ..config import Settings, get_settings, secret
from ..db import market_by_slug, now_iso, rows, upsert
from ..util import haversine_miles

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
FIELD_MASK = "originIndex,destinationIndex,status,condition,distanceMeters,duration"
PROVIDER = "google_routes"           # drive_times.method and api_cache.provider
ESTIMATE = "estimate"
SKU = "route_matrix_element"         # matches google.prices in config.yaml
KEY_NAME = "GOOGLE_MAPS_API_KEY"
MAX_ELEMENTS_PER_REQUEST = 625       # Google limit (origins x destinations), most routing modes
MAX_ELEMENTS_OPTIMAL = 100           # Google limit with TRAFFIC_AWARE_OPTIMAL
METERS_PER_MILE = 1609.344
CACHE_LOOKUP_CHUNK = 500
ROUTING_PREFERENCES = ("TRAFFIC_UNAWARE", "TRAFFIC_AWARE", "TRAFFIC_AWARE_OPTIMAL")
PROVIDER_ALIASES = {"google_routes": PROVIDER, "google": PROVIDER, "routes": PROVIDER,
                    "estimate": ESTIMATE, "straight_line": ESTIMATE}


# --- settings ---------------------------------------------------------------------------------

class DriveConfig(BaseModel):
    """`drive:` (and google.concurrency) from config.yaml. Missing keys use these defaults."""
    provider: str = PROVIDER
    road_factor: float = Field(1.25, gt=0)
    avg_mph: float = Field(48, gt=0)
    batch_size: int = Field(25, ge=1)
    routing_preference: str = "TRAFFIC_UNAWARE"   # cheapest SKU; no live traffic
    elements_per_minute: int = Field(3000, ge=1)  # Google's published rate limit
    google_max_estimate_ratio: float | None = 2.0  # None or 0 = always ask Google
    concurrency: int = Field(5, ge=1)
    timeout_seconds: float = Field(60, gt=0)
    max_retries: int = Field(4, ge=0)
    retry_base_seconds: float = Field(1.0, ge=0)

    @property
    def max_batch(self) -> int:
        cap = MAX_ELEMENTS_OPTIMAL if self.routing_preference == "TRAFFIC_AWARE_OPTIMAL" else MAX_ELEMENTS_PER_REQUEST
        return max(1, min(self.batch_size, cap))


def drive_config(settings: Settings | None = None) -> DriveConfig:
    s = settings or get_settings()
    d, g = s.section("drive"), s.section("google")
    est = d.get("estimate") or {}
    raw = {
        "provider": d.get("provider"), "road_factor": est.get("road_factor"), "avg_mph": est.get("avg_mph"),
        "batch_size": d.get("batch_size"), "routing_preference": d.get("routing_preference"),
        "elements_per_minute": d.get("elements_per_minute"), "concurrency": g.get("concurrency"),
        "timeout_seconds": d.get("timeout_seconds"), "max_retries": d.get("max_retries"),
        "retry_base_seconds": d.get("retry_base_seconds"),
    }
    data: dict[str, Any] = {k: v for k, v in raw.items() if v is not None}
    if "google_max_estimate_ratio" in d:
        data["google_max_estimate_ratio"] = d["google_max_estimate_ratio"] or None
    try:
        cfg = DriveConfig(**data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"config.yaml drive setting '{where}' has a value I can't use: {first.get('msg')}") from None
    if cfg.routing_preference not in ROUTING_PREFERENCES:
        raise ValueError(f"config.yaml drive.routing_preference must be one of {', '.join(ROUTING_PREFERENCES)}.")
    return cfg


def resolve_provider(requested: str | None, cfg: DriveConfig) -> tuple[str, str | None]:
    """(provider actually used, plain-English note or None)."""
    wanted = (requested or cfg.provider or PROVIDER).strip().lower()
    name = PROVIDER_ALIASES.get(wanted)
    if name is None:
        raise ValueError(f"Unknown drive-time provider '{requested or cfg.provider}'. Use google_routes or estimate.")
    if name == PROVIDER and not secret(KEY_NAME):
        return ESTIMATE, "There's no Google key in .env, so drive times are straight-line estimates (approximate)."
    return name, None


# --- estimates --------------------------------------------------------------------------------

def _estimate(lat: float, lng: float, olat: float, olng: float, cfg: DriveConfig) -> tuple[float, float]:
    """(minutes, road meters) from straight-line distance."""
    road_miles = haversine_miles(olat, olng, lat, lng) * cfg.road_factor
    return round(road_miles / cfg.avg_mph * 60.0, 1), round(road_miles * METERS_PER_MILE, 1)


def estimate_minutes(lat: float, lng: float, origin_lat: float, origin_lng: float,
                     settings: Settings | None = None) -> float:
    """Approximate drive minutes: straight-line miles x road_factor, driven at avg_mph."""
    return _estimate(lat, lng, origin_lat, origin_lng, drive_config(settings))[0]


# --- Google Routes: parsed data ---------------------------------------------------------------

def parse_duration_seconds(value: Any) -> float | None:
    """Google durations look like "1234s" or "12.5s"."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s.endswith("s"):
        s = s[:-1]
    try:
        out = float(s)
    except ValueError:
        return None
    return out if math.isfinite(out) and out >= 0 else None


class RouteAnswer(BaseModel):
    """One origin -> destination answer. This is what gets cached."""
    condition: str                    # ROUTE_EXISTS | ROUTE_NOT_FOUND
    seconds: float | None = None
    meters: float | None = None

    @property
    def found(self) -> bool:
        return self.condition == "ROUTE_EXISTS" and self.seconds is not None


class RouteStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: int = 0
    message: str | None = None


class RouteMatrixElement(BaseModel):
    """One element of the computeRouteMatrix reply. Zero values (index 0, 0 meters) are left out
    of Google's JSON, hence the defaults."""
    model_config = ConfigDict(extra="ignore")
    originIndex: int = 0
    destinationIndex: int = 0
    status: RouteStatus = Field(default_factory=RouteStatus)
    condition: str | None = None
    distanceMeters: float | None = None
    duration: str | None = None

    def answer(self) -> RouteAnswer | None:
        """None when Google reported an error for this element (do not cache; try again next run)."""
        if self.status.code:
            return None
        if self.condition == "ROUTE_NOT_FOUND":
            return RouteAnswer(condition="ROUTE_NOT_FOUND")
        seconds = parse_duration_seconds(self.duration)
        if self.condition == "ROUTE_EXISTS":
            return RouteAnswer(condition="ROUTE_EXISTS", seconds=seconds or 0.0, meters=self.distanceMeters or 0.0)
        if seconds is not None:
            return RouteAnswer(condition="ROUTE_EXISTS", seconds=seconds, meters=self.distanceMeters or 0.0)
        return None


def parse_matrix_response(payload: Any) -> tuple[dict[int, RouteMatrixElement], str | None]:
    """computeRouteMatrix reply (a JSON array of elements) -> ({destination index: element}, error).

    Errors come back as {"error": {...}} or as an array holding one; both give a plain message.
    """
    if isinstance(payload, dict):
        if "error" in payload:
            return {}, _error_text(payload["error"])
        payload = [payload]
    if not isinstance(payload, list):
        return {}, "Google sent a reply I couldn't read."
    out: dict[int, RouteMatrixElement] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        if "error" in item:
            return out, _error_text(item["error"])
        try:
            el = RouteMatrixElement.model_validate(item)
        except ValidationError:
            continue
        if el.originIndex == 0:
            out[el.destinationIndex] = el
    return out, None


def _error_parts(response: httpx.Response | None) -> tuple[str | None, str | None, str | None]:
    """(status, reason, message) from a Google error body, any of them None."""
    if response is None:
        return None, None, None
    try:
        data = response.json()
    except ValueError:
        return None, None, None
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict) and "error" in d), {})
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None, None, None
    reason = next((d.get("reason") for d in err.get("details") or [] if isinstance(d, dict) and d.get("reason")), None)
    return err.get("status"), reason, err.get("message")


def _error_text(err: Any) -> str:
    if isinstance(err, dict):
        return geo.redact(f"Google returned an error: {err.get('message') or err.get('status') or 'no details'}")
    return "Google returned an error."


def routes_error(res: geo.HttpResult) -> tuple[str, bool]:
    """Plain-English message for a failed Routes request, and whether to stop asking this run."""
    status, reason, message = _error_parts(res.response)
    code = res.status
    if reason == "API_KEY_INVALID" or code == 401:
        return "Google says the API key in .env isn't valid. Check GOOGLE_MAPS_API_KEY.", True
    if reason == "BILLING_DISABLED":
        return "Google needs billing turned on for this Google Cloud project before the Routes API works.", True
    if code == 403 or status == "PERMISSION_DENIED" or reason in ("SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED"):
        return ("Google refused the request. In Google Cloud Console, turn on the Routes API for this key's "
                "project and check the key's API restrictions."), True
    if code == 429:
        return "Google is limiting how fast we can ask (rate limit). The rest will be tried on the next run.", True
    if code is None or code >= 500 or code == 408:
        return (f"Google's Routes service didn't answer ({res.error}). Those properties got estimates for now; "
                f"run drive times again later."), False
    detail = geo.redact(message or f"HTTP {code}")
    return f"Google couldn't use one request ({detail}). Those properties got estimates for now.", False


# --- rate limit -------------------------------------------------------------------------------

class ElementRateLimiter:
    """Keeps all worker threads together under Google's elements-per-minute limit."""

    def __init__(self, per_minute: int, window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.per_minute = max(1, int(per_minute))
        self.window = window_seconds
        self.clock = clock
        self._sent: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def acquire(self, n: int) -> float:
        """Wait until `n` more elements fit in the last minute; returns seconds waited."""
        n = max(1, min(int(n), self.per_minute))
        waited = 0.0
        with self._lock:
            while True:
                now = self.clock()
                while self._sent and now - self._sent[0][0] >= self.window:
                    self._sent.popleft()
                if sum(c for _, c in self._sent) + n <= self.per_minute:
                    self._sent.append((now, n))
                    return waited
                wait = max(0.01, self._sent[0][0] + self.window - now)
                geo._sleep(wait)
                waited += wait


# --- plan -------------------------------------------------------------------------------------

def coord_key(lat: float, lng: float) -> str:
    return f"{lat:.5f},{lng:.5f}"


def route_cache_key(origin_key: str, dest_key: str, routing_preference: str = "TRAFFIC_UNAWARE") -> str:
    return make_key(PROVIDER, "DRIVE", routing_preference, origin_key, dest_key)


@dataclass
class _Target:
    pid: int
    lat: float
    lng: float
    est_minutes: float
    est_meters: float
    use_google: bool
    cache_key: str | None
    row_method: str | None
    row_origin: str | None


@dataclass
class _Plan:
    market: dict
    provider: str
    note: str | None
    olat: float
    olng: float
    origin_key: str
    targets: list[_Target]                     # properties to (re)write this run, after limit
    hits: dict[str, RouteAnswer]               # cached Google answers by cache key
    misses: list[_Target]                      # unique destinations to ask Google about
    missing_coordinates: int
    beyond_range: int


def _cached_routes(conn: sqlite3.Connection, keys: list[str]) -> dict[str, RouteAnswer]:
    out: dict[str, RouteAnswer] = {}
    unique = list(dict.fromkeys(keys))
    for chunk in geo.chunks(unique, CACHE_LOOKUP_CHUNK):
        marks = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT key, status, body FROM api_cache WHERE key IN ({marks})", chunk):
            if (r["status"] or 0) >= 400:
                continue
            try:
                out[r["key"]] = RouteAnswer.model_validate_json(r["body"])
            except (ValidationError, TypeError, ValueError):
                continue
    return out


def _within_google_range(est_minutes: float, max_minutes: Any, cfg: DriveConfig) -> bool:
    if not cfg.google_max_estimate_ratio or not max_minutes:
        return True
    return est_minutes <= cfg.google_max_estimate_ratio * float(max_minutes)


def _expected_method(t: _Target, hits: dict[str, RouteAnswer]) -> str | None:
    if not t.use_google:
        return ESTIMATE
    ans = hits.get(t.cache_key or "")
    if ans is None:
        return None                      # unknown until Google answers
    return PROVIDER if ans.found else ESTIMATE


def _plan(conn: sqlite3.Connection, market_slug: str, cfg: DriveConfig, provider: str | None,
          limit: int | None) -> _Plan:
    market = market_by_slug(conn, market_slug)
    if market.get("origin_lat") is None or market.get("origin_lng") is None:
        raise ValueError(f"Market '{market_slug}' has no starting point. Set its origin in config.yaml (markets).")
    used, note = resolve_provider(provider, cfg)
    olat, olng = round(float(market["origin_lat"]), 5), round(float(market["origin_lng"]), 5)
    okey = coord_key(olat, olng)
    props = rows(conn, "SELECT p.id, p.lat, p.lng, d.method AS row_method, d.origin_key AS row_origin "
                       "FROM properties p LEFT JOIN drive_times d ON d.property_id = p.id "
                       "WHERE p.market_id = ? ORDER BY p.id", (market["id"],))
    targets: list[_Target] = []
    missing = 0
    for p in props:
        if p["lat"] is None or p["lng"] is None:
            missing += 1
            continue
        lat, lng = round(float(p["lat"]), 5), round(float(p["lng"]), 5)
        minutes, meters = _estimate(lat, lng, olat, olng, cfg)
        use_google = used == PROVIDER and _within_google_range(minutes, market.get("max_drive_minutes"), cfg)
        key = route_cache_key(okey, coord_key(lat, lng), cfg.routing_preference) if use_google else None
        targets.append(_Target(p["id"], lat, lng, minutes, meters, use_google, key, p["row_method"], p["row_origin"]))
    hits = _cached_routes(conn, [t.cache_key for t in targets if t.cache_key])

    def needs_work(t: _Target) -> bool:
        expected = _expected_method(t, hits)
        return expected is None or t.row_method != expected or t.row_origin != okey

    targets.sort(key=lambda t: (not needs_work(t), t.pid))  # stable: unfinished properties first
    if limit is not None:
        targets = targets[:max(0, int(limit))]
    misses: dict[str, _Target] = {}
    for t in targets:
        if t.cache_key and t.cache_key not in hits:
            misses.setdefault(t.cache_key, t)
    beyond = sum(1 for t in targets if used == PROVIDER and not t.use_google)
    return _Plan(market, used, note, olat, olng, okey, targets, hits, list(misses.values()), missing, beyond)


def _confirm_threshold(settings: Settings) -> int:
    return int(settings.section("safety").get("confirm_over_properties", 50) or 50)


def _properties_to_fetch(plan: _Plan) -> int:
    keys = {t.cache_key for t in plan.misses}
    return sum(1 for t in plan.targets if t.cache_key in keys)


# --- dry run ----------------------------------------------------------------------------------

def drive_estimate(conn: sqlite3.Connection, market_slug: str, limit: int | None = None,
                   provider: str | None = None) -> dict:
    """What a drive-time run would do and cost, without calling anything."""
    settings = get_settings()
    cfg = drive_config(settings)
    plan = _plan(conn, market_slug, cfg, provider, limit)
    elements = len(plan.misses) if plan.provider == PROVIDER else 0
    to_fetch = _properties_to_fetch(plan)
    cost = estimate_cost(conn, SKU, elements, settings)
    cached = sum(1 for t in plan.targets if t.cache_key and t.cache_key in plan.hits)
    requests = math.ceil(elements / cfg.max_batch) if elements else 0
    needs_confirm = plan.provider == PROVIDER and to_fetch > _confirm_threshold(settings)
    return {
        "dry_run": True,
        "provider": plan.provider,
        "properties": len(plan.targets),
        "cached": cached,
        "to_fetch": to_fetch,
        "elements": elements,
        "requests": requests,
        "cost": cost,
        "beyond_range": plan.beyond_range,
        "missing_coordinates": plan.missing_coordinates,
        "needs_confirm": needs_confirm,
        "geocode": geo.geocode_estimate(conn, market_slug, limit),
        "message": _estimate_message(plan, to_fetch, elements, cost, cached),
    }


def _estimate_message(plan: _Plan, to_fetch: int, elements: int, cost: dict, cached: int) -> str:
    n = geo.plural
    if not plan.targets and not plan.missing_coordinates:
        return "There are no properties in this market yet. Import a Crexi export first."
    if plan.provider == ESTIMATE:
        msg = f"Straight-line drive estimates (free, approximate) for {n(len(plan.targets), 'property', 'properties')}."
        return f"{msg} {plan.note}" if plan.note else msg
    msg = (f"{n(len(plan.targets), 'property', 'properties')}: {cached:,} drive times already saved, "
           f"{to_fetch:,} to ask Google for ({n(elements, 'billable element')}, about ${cost['usd']:.2f} after "
           f"the free monthly calls).")
    if plan.beyond_range:
        msg += f" Far outside the drive limit (keep a free estimate): {plan.beyond_range:,}."
    if plan.missing_coordinates:
        msg += f" No map location yet (geocoded first): {plan.missing_coordinates:,}."
    return msg


# --- the run ----------------------------------------------------------------------------------

def request_body(olat: float, olng: float, batch: list[_Target], cfg: DriveConfig) -> dict:
    def waypoint(lat: float, lng: float) -> dict:
        return {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}

    return {
        "origins": [waypoint(olat, olng)],
        "destinations": [waypoint(t.lat, t.lng) for t in batch],
        "travelMode": "DRIVE",
        "routingPreference": cfg.routing_preference,
    }


@dataclass
class _BatchOutcome:
    targets: list[_Target]
    sent: bool = False
    billed: bool = False
    elements: dict[int, RouteMatrixElement] = field(default_factory=dict)
    error: str | None = None
    fatal: bool = False


def _send_batch(client: httpx.Client, api_key: str, plan: _Plan, batch: list[_Target], cfg: DriveConfig,
                limiter: ElementRateLimiter, stop: threading.Event) -> _BatchOutcome:
    out = _BatchOutcome(batch)
    try:
        if stop.is_set():
            return out
        limiter.acquire(len(batch))
        if stop.is_set():
            return out
        res = geo.send_with_retry(
            client, "POST", ROUTES_URL, json=request_body(plan.olat, plan.olng, batch, cfg),
            headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"},
            timeout=cfg.timeout_seconds, max_retries=cfg.max_retries, base_delay=cfg.retry_base_seconds)
        out.sent = True
        if not res.ok:
            out.error, out.fatal = routes_error(res)
            if out.fatal:
                stop.set()
            return out
        out.billed = True
        try:
            payload = res.response.json()
        except ValueError:
            out.error = "Google sent a reply I couldn't read. Those properties got estimates for now."
            return out
        out.elements, out.error = parse_matrix_response(payload)
    except Exception as exc:  # keep one bad batch from sinking the run
        log.error("Drive-time request failed unexpectedly (%s)", type(exc).__name__)
        out.error = f"Something unexpected went wrong asking Google ({type(exc).__name__})."
    return out


@dataclass
class _FetchStats:
    requests: int = 0
    elements: int = 0
    fetched: int = 0
    element_errors: int = 0
    fatal: str | None = None
    warnings: list[str] = field(default_factory=list)


def _absorb(conn: sqlite3.Connection, out: _BatchOutcome, stats: _FetchStats, answers: dict[str, RouteAnswer]) -> None:
    """Main thread only: record billing, cache answers, collect messages."""
    stats.requests += int(out.sent)
    if out.billed:
        record_call(conn, "google", SKU, len(out.targets))
        stats.elements += len(out.targets)
    if out.error and out.error not in stats.warnings:
        stats.warnings.append(out.error)
    if out.fatal and not stats.fatal:
        stats.fatal = out.error
    if not out.billed:
        return
    for i, t in enumerate(out.targets):
        el = out.elements.get(i)
        ans = el.answer() if el is not None else None
        if ans is None:
            stats.element_errors += 1
            continue
        cache_put(conn, t.cache_key, PROVIDER, 200, ans.model_dump())
        answers[t.cache_key] = ans
        stats.fetched += 1


def _fetch_routes(conn: sqlite3.Connection, client: httpx.Client, api_key: str, plan: _Plan, cfg: DriveConfig,
                  answers: dict[str, RouteAnswer], progress: Progress) -> _FetchStats:
    stats = _FetchStats()
    batches = list(geo.chunks(plan.misses, cfg.max_batch))
    if not batches:
        return stats
    limiter = ElementRateLimiter(cfg.elements_per_minute)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=min(cfg.concurrency, len(batches)), thread_name_prefix="routes") as pool:
        futures = [pool.submit(_send_batch, client, api_key, plan, b, cfg, limiter, stop) for b in batches]
        try:
            for done, fut in enumerate(as_completed(futures), start=1):
                _absorb(conn, fut.result(), stats, answers)
                geo.call_progress(progress, done / len(batches),
                                  f"Asked Google for {min(done * cfg.max_batch, len(plan.misses)):,} of "
                                  f"{len(plan.misses):,} drive times")
        except BaseException:
            stop.set()
            raise
    return stats


def _write(conn: sqlite3.Connection, plan: _Plan, answers: dict[str, RouteAnswer]) -> Counter:
    counts: Counter = Counter()
    now = now_iso()
    with conn:
        for t in plan.targets:
            ans = answers.get(t.cache_key) if t.cache_key else None
            if ans is not None and ans.found:
                minutes, meters, method = round(ans.seconds / 60.0, 1), ans.meters, PROVIDER
                counts["google"] += 1
                counts["cached"] += int(t.cache_key in plan.hits)
            else:
                minutes, meters, method = t.est_minutes, t.est_meters, ESTIMATE
                counts["estimated"] += 1
                if ans is not None:
                    counts["no_route"] += 1
                elif t.use_google:
                    counts["not_fetched"] += 1
            upsert(conn, "drive_times", "property_id", {
                "property_id": t.pid, "minutes": minutes, "meters": meters, "method": method,
                "origin_key": plan.origin_key, "fetched_at": now})
    return counts


def _scaled(progress: Progress, start: float, end: float) -> Progress:
    if not progress:
        return None
    return lambda f, m: geo.call_progress(progress, start + (end - start) * f, m)


def compute_drive_times(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
                        provider: str | None = None, progress: Progress = None, *, confirm: bool | None = None,
                        geocode: bool = True, client: httpx.Client | None = None) -> dict:
    """Save a drive time for every property in the market (table drive_times).

    limit: at most this many properties, unfinished ones first (use 5 to test).
    dry_run: return drive_estimate(...) and call nothing.
    provider: google_routes | estimate (default: config drive.provider; estimate when there's no key).
    confirm: pass False to stop before asking Google about more than safety.confirm_over_properties
      properties (the result then has needs_confirm=True and the estimate). None leaves that check to
      the caller (the pipeline). True goes ahead.
    geocode: first give coordinates to properties that have none (free Census geocoder).
    """
    if dry_run:
        return drive_estimate(conn, market_slug, limit, provider)
    settings = get_settings()
    cfg = drive_config(settings)
    resolve_provider(provider, cfg)  # fail fast on a typo, before any work
    own_client = client is None
    http = client or geo.new_client(cfg.timeout_seconds)
    try:
        geo_result = None
        if geocode:
            geo_result = geo.geocode_missing(conn, market_slug, limit=limit, progress=_scaled(progress, 0.0, 0.15),
                                             client=http)
        geo.call_progress(progress, 0.16, "Working out which drive times are needed")
        plan = _plan(conn, market_slug, cfg, provider, limit)
        to_fetch = _properties_to_fetch(plan)
        cost = estimate_cost(conn, SKU, len(plan.misses) if plan.provider == PROVIDER else 0, settings)
        if confirm is False and plan.provider == PROVIDER and to_fetch > _confirm_threshold(settings):
            est = drive_estimate(conn, market_slug, limit, provider)
            est.update(dry_run=False, needs_confirm=True, geocode=geo_result,
                       message=f"Waiting for your OK: {est['message']} Run again with confirm (CLI: --yes).")
            return est
        answers: dict[str, RouteAnswer] = dict(plan.hits)
        stats = _FetchStats()
        if plan.provider == PROVIDER and plan.misses:
            stats = _fetch_routes(conn, http, secret(KEY_NAME) or "", plan, cfg, answers,
                                  _scaled(progress, 0.18, 0.95))
    finally:
        if own_client:
            http.close()
    geo.call_progress(progress, 0.96, "Saving drive times")
    counts = _write(conn, plan, answers)
    result = _result(plan, counts, stats, cost, geo_result)
    log.info("Drive times %s: %s", market_slug, result["message"])
    geo.call_progress(progress, 1.0, result["message"])
    return result


def _result(plan: _Plan, counts: Counter, stats: _FetchStats, cost: dict, geo_result: dict | None) -> dict:
    warnings = ([plan.note] if plan.note else []) + stats.warnings
    if geo_result:
        warnings += geo_result.get("warnings") or []
    return {
        "dry_run": False,
        "needs_confirm": False,
        "provider": plan.provider,
        "properties": len(plan.targets),
        "google": counts["google"],
        "estimated": counts["estimated"],
        "cached": counts["cached"],
        "fetched": stats.fetched,
        "elements": stats.elements,
        "requests": stats.requests,
        "no_route": counts["no_route"],
        "errors": counts["not_fetched"],
        "beyond_range": plan.beyond_range,
        "missing_coordinates": plan.missing_coordinates,
        "cost": cost,
        "geocode": geo_result,
        "error": stats.fatal,
        "warnings": warnings,
        "message": _result_message(plan, counts, stats),
    }


def _result_message(plan: _Plan, counts: Counter, stats: _FetchStats) -> str:
    n = geo.plural
    if not plan.targets:
        if plan.missing_coordinates:
            return (f"No drive times saved: {n(plan.missing_coordinates, 'property has', 'properties have')} "
                    f"no map location and couldn't be geocoded.")
        return "There are no properties in this market yet. Import a Crexi export first."
    total = n(len(plan.targets), "property", "properties")
    if plan.provider == ESTIMATE:
        msg = f"Saved straight-line drive estimates for {total}."
        msg += f" {plan.note}" if plan.note else " These are approximate."
    else:
        new = counts["google"] - counts["cached"]
        msg = (f"Saved drive times for {total}: {counts['google']:,} from Google "
               f"({counts['cached']:,} already saved, {new:,} new)")
        extras = []
        if plan.beyond_range:
            extras.append(f"{plan.beyond_range:,} far outside the drive limit kept a free estimate")
        if counts["no_route"]:
            extras.append(f"{counts['no_route']:,} had no driving route, so got an estimate")
        if counts["not_fetched"]:
            extras.append(f"{counts['not_fetched']:,} got an estimate for now because Google didn't answer")
        msg += ("; " + "; ".join(extras) + ".") if extras else "."
        if stats.fatal:
            msg += f" {stats.fatal}"
    if plan.missing_coordinates:
        msg += f" Still no map location: {plan.missing_coordinates:,}."
    return msg
