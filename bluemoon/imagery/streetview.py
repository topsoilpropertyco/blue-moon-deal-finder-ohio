"""Street View + satellite photos for the photo check.

For each property in the buy box (only asset classes whose photo check is on):

1. Street View metadata (free, no quota): is there an outdoor panorama within
   `google.street_view.radius_m` meters of the property? No panorama -> status `no_imagery`
   (the satellite photo is still saved, because some asset classes are judged mostly from above).
2. Three Street View photos from that panorama, aimed at the property: the compass bearing from the
   panorama to the property, plus that bearing turned by each of `side_offsets_deg` (-35 and +35).
3. One satellite photo (Maps Static, zoom 19) centered on the property, with a tiny red pin on it.

Photos are saved as images/{property_id}/sv_0.jpg, sv_1.jpg, sv_2.jpg and sat.jpg. A photo that is
already on disk is never asked for again, so re-runs never re-pay. Metadata answers are cached in
SQLite (api_cache). Every photo Google bills is recorded with record_call for the cost meter.

Google needs the API key as a `key=` URL parameter here. It is never put into cache keys, messages,
results or logs (httpx's request log lines are scrubbed by `geo.redact`).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..cache import cache_get, cache_put, estimate_cost, make_key, month_usage, record_call
from ..config import AssetClass, Settings, get_settings, secret
from ..db import dumps, market_by_slug, now_iso, rows, upsert
from ..enrich import geo
from ..util import bearing_deg, haversine_miles

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
STREET_VIEW_URL = "https://maps.googleapis.com/maps/api/streetview"
STATIC_MAP_URL = "https://maps.googleapis.com/maps/api/staticmap"
KEY_NAME = "GOOGLE_MAPS_API_KEY"
METADATA_PROVIDER = "google_streetview_metadata"
SKU_STREET_VIEW = "street_view_static"     # matches google.prices in config.yaml
SKU_SATELLITE = "static_maps"
SKU_METADATA = "street_view_metadata"
SAT_FILE = "sat.jpg"
YOURS_PREFIX = "you_"      # photos you add by hand (you_1.jpg, you_2.png ...); the photo check reads them first
MAX_YOURS = 8
METERS_PER_MILE = 1609.344
TOO_CLOSE_METERS = 4.0                     # panorama on top of the point: bearing means nothing
FALLBACK_HEADINGS = (0.0, 120.0, 240.0)    # then sweep around instead
CACHEABLE_METADATA = ("OK", "ZERO_RESULTS", "NOT_FOUND")
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SIZE = re.compile(r"^\d{1,4}x\d{1,4}$")


# --- settings ---------------------------------------------------------------------------------

class StreetViewSpec(BaseModel):
    """`google.street_view` in config.yaml."""
    model_config = ConfigDict(extra="ignore")
    fov: float = Field(80, gt=0, le=120)
    pitch: float = Field(5, ge=-90, le=90)
    size: str = "640x640"
    side_offsets_deg: list[float] = Field(default_factory=lambda: [-35.0, 35.0])
    radius_m: int = Field(50, ge=0)
    source: str = "outdoor"                # outdoor | default

    @field_validator("size")
    @classmethod
    def _size(cls, v: str) -> str:
        if not _SIZE.match(str(v)):
            raise ValueError("size must look like 640x640")
        return str(v)


class SatelliteSpec(BaseModel):
    """`google.satellite` in config.yaml."""
    model_config = ConfigDict(extra="ignore")
    zoom: int = Field(19, ge=0, le=22)
    size: str = "640x640"
    maptype: str = "satellite"
    scale: int = Field(1, ge=1, le=2)
    marker: bool = True                    # tiny red pin on the property's map point

    @field_validator("size")
    @classmethod
    def _size(cls, v: str) -> str:
        if not _SIZE.match(str(v)):
            raise ValueError("size must look like 640x640")
        return str(v)


class ImageryConfig(BaseModel):
    """`google:` (plus safety.confirm_over_properties) from config.yaml. Missing keys use these defaults."""
    model_config = ConfigDict(extra="ignore")
    concurrency: int = Field(5, ge=1, le=32)
    street_view: StreetViewSpec = Field(default_factory=StreetViewSpec)
    satellite: SatelliteSpec = Field(default_factory=SatelliteSpec)
    max_street_view_images_per_run: int = Field(3000, ge=0)
    max_satellite_images_per_run: int = Field(1000, ge=0)
    timeout_seconds: float = Field(30, gt=0)
    max_retries: int = Field(3, ge=0)
    retry_base_seconds: float = Field(1.0, ge=0)
    confirm_over_properties: int = Field(50, ge=0)

    @property
    def street_view_files(self) -> list[str]:
        return [f"sv_{i}.jpg" for i in range(1 + len(self.street_view.side_offsets_deg))]


def imagery_config(settings: Settings | None = None) -> ImageryConfig:
    s = settings or get_settings()
    g = dict(s.section("google"))
    g["confirm_over_properties"] = s.section("safety").get("confirm_over_properties", 50)
    data = {k: v for k, v in g.items() if v is not None}
    try:
        return ImageryConfig(**data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"config.yaml google setting '{where}' has a value I can't use: {first.get('msg')}") from None


# --- parsed Google data -----------------------------------------------------------------------

class PanoLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lat: float
    lng: float


class StreetViewMetadata(BaseModel):
    """Street View metadata reply. This is what gets cached."""
    model_config = ConfigDict(extra="ignore")
    status: str
    pano_id: str | None = None
    location: PanoLocation | None = None
    date: str | None = None
    copyright: str | None = None

    @property
    def has_pano(self) -> bool:
        return self.status == "OK" and bool(self.pano_id) and self.location is not None

    @property
    def cacheable(self) -> bool:
        return self.status in CACHEABLE_METADATA


def parse_metadata(payload: Any) -> StreetViewMetadata | None:
    if not isinstance(payload, dict):
        return None
    try:
        return StreetViewMetadata.model_validate(payload)
    except ValidationError:
        return None


# --- geometry ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Shot:
    name: str          # sv_0.jpg
    heading: float     # compass degrees, 0-360


def street_view_shots(pano_lat: float, pano_lng: float, lat: float, lng: float,
                      cfg: ImageryConfig) -> tuple[float, list[Shot]]:
    """(base heading, shots): one aimed at the property, then one per side offset."""
    names = cfg.street_view_files
    meters = haversine_miles(pano_lat, pano_lng, lat, lng) * METERS_PER_MILE
    if meters < TOO_CLOSE_METERS:
        headings = [FALLBACK_HEADINGS[i % len(FALLBACK_HEADINGS)] for i in range(len(names))]
        return headings[0], [Shot(n, h) for n, h in zip(names, headings, strict=True)]
    base = round(bearing_deg(pano_lat, pano_lng, lat, lng), 1)
    offsets = [0.0, *cfg.street_view.side_offsets_deg]
    return base, [Shot(n, round((base + off) % 360.0, 1)) for n, off in zip(names, offsets, strict=True)]


def coord(lat: float, lng: float) -> str:
    return f"{lat:.6f},{lng:.6f}"


def metadata_cache_key(lat: float, lng: float, cfg: ImageryConfig) -> str:
    return make_key(METADATA_PROVIDER, coord(lat, lng), cfg.street_view.radius_m, cfg.street_view.source)


# --- files ------------------------------------------------------------------------------------

def property_dir(settings: Settings, property_id: int) -> Path:
    return settings.path("images") / str(int(property_id))


def existing_images(settings: Settings, property_id: int) -> list[str]:
    """Photo file names already saved for a property, Street View first, satellite last."""
    d = property_dir(settings, property_id)
    if not d.is_dir():
        return []
    sv = sorted(p.name for p in d.glob("sv_*.jpg") if p.is_file() and p.stat().st_size > 0)
    sat = [SAT_FILE] if (d / SAT_FILE).is_file() and (d / SAT_FILE).stat().st_size > 0 else []
    return your_images(settings, property_id) + sv + sat


def your_images(settings: Settings, property_id: int) -> list[str]:
    """Photos you added by hand, oldest first."""
    d = property_dir(settings, property_id)
    if not d.is_dir():
        return []
    mine = [p for p in d.iterdir() if p.is_file() and p.name.startswith(YOURS_PREFIX) and p.stat().st_size > 0
            and p.suffix.lower() in (".jpg", ".png")]
    return sorted((p.name for p in mine), key=lambda n: (len(n), n))


def add_your_image(settings: Settings, property_id: int, data: bytes) -> str:
    """Save a photo you found yourself. JPEG or PNG only. Returns its file name."""
    if not looks_like_image(data):
        raise ValueError("That isn't a JPEG or PNG photo. Save the picture as a .jpg or .png and try again.")
    have = your_images(settings, property_id)
    if len(have) >= MAX_YOURS:
        raise ValueError(f"This property already has {MAX_YOURS} of your photos. Delete one first.")
    n = 1 + max([int(re.sub(r"\D", "", Path(h).stem) or 0) for h in have] + [0])
    name = f"{YOURS_PREFIX}{n}.{'png' if data.startswith(PNG_MAGIC) else 'jpg'}"
    save_image(property_dir(settings, property_id), name, data)
    return name


def image_paths(settings: Settings, property_id: int) -> list[Path]:
    d = property_dir(settings, property_id)
    return [d / name for name in existing_images(settings, property_id)]


def looks_like_image(data: bytes) -> bool:
    return data.startswith(JPEG_MAGIC) or data.startswith(PNG_MAGIC)


def save_image(folder: Path, name: str, data: bytes) -> None:
    """Write atomically, so a half-written file never counts as saved."""
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{name}.part"
    tmp.write_bytes(data)
    os.replace(tmp, folder / name)


# --- plan -------------------------------------------------------------------------------------

@dataclass
class _Job:
    pid: int
    lat: float
    lng: float
    folder: Path
    pano: StreetViewMetadata | None        # known panorama answer (row or cache), None = ask Google
    meta_key: str
    have: set[str]                         # files already on disk
    sv_needed: int                         # Street View photos still to fetch (upper bound if pano unknown)
    sat_needed: int


@dataclass
class _Plan:
    market: dict
    asset_class: AssetClass
    candidates: int                        # properties eligible for photos (after the buy-box filter)
    jobs: list[_Job]                       # properties to work on this run (after limit)
    complete: int                          # properties whose photos were already all saved
    excluded: int
    missing_coordinates: int
    reviewed: int
    no_imagery_known: int


def _visual_enabled(ac: AssetClass) -> bool:
    return ac.visual_enabled


def _buybox_from_evaluate(prop: dict, drive: dict | None, ac: AssetClass, market: dict,
                          settings: Settings, conn: sqlite3.Connection | None = None) -> bool:
    """Ask the buy box (scoring) when the property hasn't been scored yet, with any decision made by hand.
    Missing data never excludes."""
    try:
        from ..scoring.buybox import apply_overrides, evaluate
        from ..scoring.overrides import for_property
    except ImportError:
        return True
    try:
        res = evaluate(prop, {"drive": drive, "rent": None, "visual": None, "owner": None}, ac, market, settings)
        found = for_property(conn, int(prop["id"])) if conn is not None else {}
        if found:
            res = apply_overrides(res, found)
    except Exception:  # the buy box is another module; a bug there must not block photos
        log.debug("buy-box check failed for property %s", prop.get("id"), exc_info=True)
        return True
    return bool(res.get("in_buybox", True))


def _row_pano(v_status: str | None, v: Mapping[str, Any]) -> StreetViewMetadata | None:
    if v.get("v_pano_id") and v.get("pano_lat") is not None and v.get("pano_lng") is not None:
        return StreetViewMetadata(status="OK", pano_id=v["v_pano_id"],
                                  location=PanoLocation(lat=v["pano_lat"], lng=v["pano_lng"]),
                                  date=v.get("pano_date"))
    if v_status == "no_imagery":
        return StreetViewMetadata(status="ZERO_RESULTS")
    return None


def _cached_pano(conn: sqlite3.Connection, key: str) -> StreetViewMetadata | None:
    hit = cache_get(conn, key)
    if hit is None or (hit.get("status") or 0) >= 400:
        return None
    meta = parse_metadata(hit.get("body"))
    return meta if meta is not None and meta.cacheable else None


def _needs(pano: StreetViewMetadata | None, have: set[str], cfg: ImageryConfig) -> tuple[int, int]:
    sat = 0 if SAT_FILE in have else 1
    if pano is not None and not pano.has_pano:
        return 0, sat
    return sum(1 for n in cfg.street_view_files if n not in have), sat


_CANDIDATE_SQL = """
SELECT p.*, s.in_buybox AS s_in_buybox, s.rank AS s_rank, s.score AS s_score,
       d.minutes AS d_minutes, d.meters AS d_meters, d.method AS d_method, d.origin_key AS d_origin_key,
       d.fetched_at AS d_fetched_at,
       v.status AS v_status, v.pano_id AS v_pano_id, v.pano_lat, v.pano_lng, v.pano_date,
       v.reviewed_label AS v_reviewed_label
FROM properties p
LEFT JOIN scores s ON s.property_id = p.id
LEFT JOIN drive_times d ON d.property_id = p.id
LEFT JOIN visual_checks v ON v.property_id = p.id
WHERE p.market_id = ?
"""


def _drive_row(r: Mapping[str, Any]) -> dict | None:
    if r.get("d_method") is None:
        return None
    return {"property_id": r["id"], "minutes": r["d_minutes"], "meters": r["d_meters"], "method": r["d_method"],
            "origin_key": r["d_origin_key"], "fetched_at": r["d_fetched_at"]}


def _priority(r: Mapping[str, Any]) -> tuple:
    """Buy-box properties first (best Blue Moon rank first), then the rest, ranked the same way."""
    rank = r.get("s_rank")
    score = r.get("s_score")
    minutes = r.get("d_minutes")
    return (r.get("s_in_buybox") == 0, rank is None, rank or 0, -(score or 0.0),
            minutes if minutes is not None else 1e9, r["id"])


def _plan(conn: sqlite3.Connection, market_slug: str, cfg: ImageryConfig, settings: Settings,
          limit: int | None, include_excluded: bool) -> _Plan:
    market = market_by_slug(conn, market_slug)
    ac = settings.asset_class(market["asset_class"])
    plan = _Plan(market, ac, 0, [], 0, 0, 0, 0, 0)
    if not _visual_enabled(ac):
        return plan
    pending: list[tuple[tuple, _Job]] = []
    for r in rows(conn, _CANDIDATE_SQL, (market["id"],)):
        if r.get("v_reviewed_label"):
            plan.reviewed += 1
            continue
        in_box = r["s_in_buybox"]
        if in_box is None:
            in_box = _buybox_from_evaluate(r, _drive_row(r), ac, market, settings, conn)
        if not in_box and not include_excluded:
            plan.excluded += 1
            continue
        if r.get("lat") is None or r.get("lng") is None:
            plan.missing_coordinates += 1
            continue
        plan.candidates += 1
        lat, lng = float(r["lat"]), float(r["lng"])
        key = metadata_cache_key(lat, lng, cfg)
        pano = _row_pano(r.get("v_status"), r) or _cached_pano(conn, key)
        have = set(existing_images(settings, r["id"]))
        sv, sat = _needs(pano, have, cfg)
        if pano is not None and not pano.has_pano:
            plan.no_imagery_known += 1
        if pano is not None and sv == 0 and sat == 0 and r.get("v_status") in ("done", "no_imagery"):
            plan.complete += 1
            continue
        job = _Job(r["id"], lat, lng, property_dir(settings, r["id"]), pano, key, have, sv, sat)
        pending.append((_priority(r), job))
    pending.sort(key=lambda t: t[0])
    jobs = [j for _, j in pending]
    if limit is not None:
        jobs = jobs[:max(0, int(limit))]
    plan.jobs = jobs
    return plan


def _charged(job: _Job) -> bool:
    return job.sv_needed > 0 or job.sat_needed > 0


# --- dry run ----------------------------------------------------------------------------------

def _costs(conn: sqlite3.Connection, sv: int, sat: int, settings: Settings) -> dict:
    sv_cost = estimate_cost(conn, SKU_STREET_VIEW, sv, settings)
    sat_cost = estimate_cost(conn, SKU_SATELLITE, sat, settings)
    return {"street_view": sv_cost, "satellite": sat_cost, "usd": round(sv_cost["usd"] + sat_cost["usd"], 2)}


def imagery_estimate(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, *,
                     include_excluded: bool = False) -> dict:
    """What a photo run would fetch and cost, without calling anything."""
    settings = get_settings()
    cfg = imagery_config(settings)
    plan = _plan(conn, market_slug, cfg, settings, limit, include_excluded)
    return _estimate_from_plan(conn, plan, cfg, settings)


def _estimate_from_plan(conn: sqlite3.Connection, plan: _Plan, cfg: ImageryConfig, settings: Settings) -> dict:
    ac = plan.asset_class
    if not _visual_enabled(ac):
        return {"dry_run": True, "enabled": False, "asset_class": ac.key, "properties": 0, "to_fetch": 0,
                "street_view_images": 0, "satellite_images": 0, "cost": _costs(conn, 0, 0, settings),
                "needs_confirm": False,
                "message": f"{ac.label} has no photo check, so no photos are needed."}
    sv = sum(j.sv_needed for j in plan.jobs)
    sat = sum(j.sat_needed for j in plan.jobs)
    sv_capped, sat_capped = min(sv, cfg.max_street_view_images_per_run), min(sat, cfg.max_satellite_images_per_run)
    to_fetch = sum(1 for j in plan.jobs if _charged(j))
    metadata = sum(1 for j in plan.jobs if j.pano is None)
    cost = _costs(conn, sv_capped, sat_capped, settings)
    message = _estimate_message(plan, to_fetch, sv_capped, sat_capped, metadata, cost)
    if sv > sv_capped or sat > sat_capped:
        message += (f" This run stops at the per-run caps ({cfg.max_street_view_images_per_run:,} Street View and "
                    f"{cfg.max_satellite_images_per_run:,} satellite photos, google.max_*_per_run in config.yaml); "
                    f"run photos again for the rest.")
    return {
        "dry_run": True,
        "enabled": True,
        "asset_class": ac.key,
        "properties": len(plan.jobs),
        "candidates": plan.candidates,
        "cached": plan.complete,
        "to_fetch": to_fetch,
        "metadata_calls": metadata,
        "street_view_images": sv_capped,
        "satellite_images": sat_capped,
        "capped": sv > sv_capped or sat > sat_capped,
        "excluded": plan.excluded,
        "reviewed": plan.reviewed,
        "missing_coordinates": plan.missing_coordinates,
        "no_imagery_known": plan.no_imagery_known,
        "cost": cost,
        "has_key": bool(secret(KEY_NAME)),
        "needs_confirm": to_fetch > cfg.confirm_over_properties,
        "message": message,
    }


def _estimate_message(plan: _Plan, to_fetch: int, sv: int, sat: int, metadata: int, cost: dict) -> str:
    n = geo.plural
    if not plan.candidates and not plan.complete:
        if plan.excluded or plan.missing_coordinates:
            return (f"No properties need photos: {n(plan.excluded, 'is', 'are')} outside the buy box and "
                    f"{n(plan.missing_coordinates, 'has', 'have')} no map location.")
        return "There are no properties in this market yet. Import a Crexi export first."
    msg = (f"Photos for {n(len(plan.jobs), 'property', 'properties')}: {plan.complete:,} already saved, "
           f"{to_fetch:,} to fetch (up to {n(sv, 'Street View photo')} and {n(sat, 'satellite photo')}, "
           f"about ${cost['usd']:.2f} after Google's free monthly calls).")
    if metadata:
        msg += f" {n(metadata, 'free Street View lookup')} first; places without Street View cost less."
    if plan.excluded:
        msg += f" Skipped {n(plan.excluded, 'property', 'properties')} outside the buy box."
    if plan.missing_coordinates:
        msg += f" No map location yet: {plan.missing_coordinates:,}."
    if not secret(KEY_NAME):
        msg += " Add GOOGLE_MAPS_API_KEY to .env before fetching."
    return msg


# --- one property (worker thread: HTTP and files only, no database) --------------------------

@dataclass
class _Outcome:
    job: _Job
    attempted: bool = False
    meta: StreetViewMetadata | None = None
    meta_fetched: bool = False             # a metadata request was answered this run
    base_heading: float | None = None
    saved: list[str] = field(default_factory=list)
    sv_billed: int = 0
    sat_billed: int = 0
    error: str | None = None
    fatal: bool = False


def google_error(res: geo.HttpResult, what: str) -> tuple[str, bool]:
    """Plain-English message for a failed photo request, and whether to stop the whole run."""
    code = res.status
    if code in (401, 403):
        return ("Google refused the photo request. In Google Cloud Console, turn on the Street View Static API "
                "and the Maps Static API for this key's project, check that billing is on, and check the key's "
                "API restrictions."), True
    if code == 429:
        return "Google is limiting how fast we can ask (rate limit). The rest will be fetched on the next run.", True
    if code == 404:
        return f"Google has no {what} here.", False
    if code == 400:
        return f"Google couldn't use the {what} request for this property.", False
    return f"Google's {what} service didn't answer ({geo.redact(res.error)}). It will be tried again next run.", False


def _metadata(client: httpx.Client, key: str, job: _Job,
              cfg: ImageryConfig) -> tuple[StreetViewMetadata | None, str | None, bool]:
    params = {"location": coord(job.lat, job.lng), "radius": cfg.street_view.radius_m,
              "source": cfg.street_view.source, "key": key}
    res = geo.send_with_retry(client, "GET", METADATA_URL, params=params, max_retries=cfg.max_retries,
                              base_delay=cfg.retry_base_seconds, timeout=cfg.timeout_seconds)
    if not res.ok:
        msg, fatal = google_error(res, "Street View lookup")
        return None, msg, fatal
    try:
        meta = parse_metadata(res.response.json())
    except ValueError:
        meta = None
    if meta is None:
        return None, "Google sent a Street View lookup reply I couldn't read.", False
    if meta.status == "REQUEST_DENIED":
        return meta, ("Google refused the Street View lookup. Turn on the Street View Static API for this key's "
                      "project and check the key's API restrictions."), True
    if meta.status == "OVER_QUERY_LIMIT":
        return meta, "Google says we're over the Street View quota for now. The rest will be fetched later.", True
    if not meta.cacheable:
        return meta, f"Google's Street View lookup answered {meta.status}; it will be tried again next run.", False
    return meta, None, False


def _download(client: httpx.Client, url: str, params: dict, cfg: ImageryConfig,
              what: str) -> tuple[bytes | None, str | None, bool]:
    res = geo.send_with_retry(client, "GET", url, params=params, max_retries=cfg.max_retries,
                              base_delay=cfg.retry_base_seconds, timeout=cfg.timeout_seconds)
    if not res.ok:
        msg, fatal = google_error(res, what)
        return None, msg, fatal
    data = res.response.content
    if not looks_like_image(data):
        return None, f"Google sent something that isn't a photo for the {what}.", False
    return data, None, False


def street_view_params(pano_id: str, heading: float, cfg: ImageryConfig, key: str) -> dict:
    sv = cfg.street_view
    return {"pano": pano_id, "heading": heading, "fov": sv.fov, "pitch": sv.pitch, "size": sv.size,
            "return_error_code": "true", "key": key}


def satellite_params(lat: float, lng: float, cfg: ImageryConfig, key: str) -> dict:
    sat = cfg.satellite
    params: dict[str, Any] = {"center": coord(lat, lng), "zoom": sat.zoom, "size": sat.size,
                              "maptype": sat.maptype, "scale": sat.scale, "format": "jpg", "key": key}
    if sat.marker:
        params["markers"] = f"size:tiny|color:red|{coord(lat, lng)}"
    return params


def _work(client: httpx.Client, key: str, job: _Job, cfg: ImageryConfig, stop: threading.Event) -> _Outcome:
    out = _Outcome(job)
    try:
        if stop.is_set():
            return out
        out.attempted = True
        meta = job.pano
        if meta is None:
            meta, err, fatal = _metadata(client, key, job, cfg)
            out.meta, out.meta_fetched = meta, meta is not None and meta.cacheable
            if err:
                out.error, out.fatal = err, fatal
                if fatal:
                    stop.set()
                    return out
                if meta is None or not meta.cacheable:
                    meta = None
        else:
            out.meta = meta
        if meta is not None and meta.has_pano:
            _street_view(client, key, job, meta, cfg, stop, out)
        if SAT_FILE not in job.have and not stop.is_set() and not out.fatal:
            data, err, fatal = _download(client, STATIC_MAP_URL, satellite_params(job.lat, job.lng, cfg, key),
                                         cfg, "satellite photo")
            if data is not None:
                save_image(job.folder, SAT_FILE, data)
                out.saved.append(SAT_FILE)
                out.sat_billed += 1
            elif err:
                out.error, out.fatal = out.error or err, fatal
                if fatal:
                    stop.set()
    except Exception as exc:  # one bad property must not sink the run
        log.error("Photo fetch failed unexpectedly for property %s (%s)", job.pid, type(exc).__name__)
        out.error = f"Something unexpected went wrong fetching photos ({type(exc).__name__})."
    return out


def _street_view(client: httpx.Client, key: str, job: _Job, meta: StreetViewMetadata, cfg: ImageryConfig,
                 stop: threading.Event, out: _Outcome) -> None:
    base, shots = street_view_shots(meta.location.lat, meta.location.lng, job.lat, job.lng, cfg)
    out.base_heading = base
    for shot in shots:
        if shot.name in job.have:
            continue
        if stop.is_set():
            return
        data, err, fatal = _download(client, STREET_VIEW_URL, street_view_params(meta.pano_id, shot.heading, cfg, key),
                                     cfg, "Street View photo")
        if data is not None:
            save_image(job.folder, shot.name, data)
            out.saved.append(shot.name)
            out.sv_billed += 1
            continue
        out.error, out.fatal = out.error or err, fatal
        if fatal:
            stop.set()
            return


# --- the run ----------------------------------------------------------------------------------

@dataclass
class _Stats:
    attempted: int = 0
    fetched: int = 0
    no_imagery: int = 0
    errors: int = 0
    sv_images: int = 0
    sat_images: int = 0
    metadata_calls: int = 0
    fatal: str | None = None
    warnings: list[str] = field(default_factory=list)


def _status_after(out: _Outcome, cfg: ImageryConfig, files: list[str]) -> tuple[str, str | None]:
    meta = out.meta
    if meta is None or not meta.cacheable:
        return ("error" if out.error and not out.fatal else "pending"), out.error
    expected = ([*cfg.street_view_files] if meta.has_pano else []) + [SAT_FILE]
    missing = [n for n in expected if n not in files]
    if missing:
        return ("error" if out.error and not out.fatal else "pending"), out.error or "Some photos are still missing."
    return ("done" if meta.has_pano else "no_imagery"), None


def _absorb(conn: sqlite3.Connection, out: _Outcome, settings: Settings, cfg: ImageryConfig, stats: _Stats) -> None:
    """Main thread only: cache, bill, save the row."""
    job = out.job
    if out.meta_fetched and out.meta is not None:
        cache_put(conn, job.meta_key, METADATA_PROVIDER, 200, out.meta.model_dump())
        record_call(conn, "google", SKU_METADATA, 1)
        stats.metadata_calls += 1
    if out.sv_billed:
        record_call(conn, "google", SKU_STREET_VIEW, out.sv_billed)
        stats.sv_images += out.sv_billed
    if out.sat_billed:
        record_call(conn, "google", SKU_SATELLITE, out.sat_billed)
        stats.sat_images += out.sat_billed
    if out.error and out.error not in stats.warnings:
        stats.warnings.append(out.error)
    if out.fatal and not stats.fatal:
        stats.fatal = out.error
    if not out.attempted:
        return
    stats.attempted += 1
    if out.saved:
        record_pull(conn, job.pid)          # counts toward this month's photo goal (Run screen)
    files = existing_images(settings, job.pid)
    status, error = _status_after(out, cfg, files)
    stats.fetched += int(bool(out.saved))
    stats.no_imagery += int(status == "no_imagery")
    stats.errors += int(status == "error")
    meta = out.meta if out.meta is not None and out.meta.has_pano else None
    row: dict[str, Any] = {
        "property_id": job.pid, "status": status,
        "images_json": dumps([f"{job.pid}/{name}" for name in files]), "error": error,
    }
    if meta is not None:
        row.update(pano_id=meta.pano_id, pano_lat=meta.location.lat, pano_lng=meta.location.lng,
                   pano_date=meta.date)
        if out.base_heading is not None:
            row["heading"] = out.base_heading
    elif out.meta is not None and out.meta.cacheable:
        row.update(pano_id=None, pano_lat=None, pano_lng=None, pano_date=None, heading=None)
    with conn:
        upsert(conn, "visual_checks", "property_id", row)


def _repair_rows(conn: sqlite3.Connection, jobs: list[_Job], settings: Settings, cfg: ImageryConfig) -> int:
    """Properties whose photos are all on disk but whose row doesn't say so (e.g. a run that was
    interrupted): mark them done without asking Google anything."""
    fixed = 0
    with conn:
        for job in jobs:
            files = existing_images(settings, job.pid)
            has_sv = all(n in files for n in cfg.street_view_files)
            if job.pano is not None and not job.pano.has_pano:
                status = "no_imagery"
            elif has_sv:
                status = "done"
            else:
                continue
            row: dict[str, Any] = {"property_id": job.pid, "status": status, "error": None,
                                   "images_json": dumps([f"{job.pid}/{n}" for n in files])}
            if job.pano is not None and job.pano.has_pano:
                row.update(pano_id=job.pano.pano_id, pano_lat=job.pano.location.lat,
                           pano_lng=job.pano.location.lng, pano_date=job.pano.date)
            upsert(conn, "visual_checks", "property_id", row)
            fixed += 1
    return fixed


def _ensure_rows(conn: sqlite3.Connection, jobs: list[_Job]) -> None:
    with conn:
        conn.executemany("INSERT OR IGNORE INTO visual_checks(property_id, status) VALUES(?, 'pending')",
                         [(j.pid,) for j in jobs])


def _within_caps(jobs: list[_Job], cfg: ImageryConfig) -> tuple[list[_Job], bool]:
    """Keep the jobs whose worst case still fits under the per-run caps."""
    sv_left, sat_left = cfg.max_street_view_images_per_run, cfg.max_satellite_images_per_run
    kept: list[_Job] = []
    for job in jobs:
        if job.sv_needed > sv_left or job.sat_needed > sat_left:
            return kept, True
        sv_left -= job.sv_needed
        sat_left -= job.sat_needed
        kept.append(job)
    return kept, False


def fetch_imagery(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
                  progress: Progress = None, confirm: bool = False, *, include_excluded: bool = False,
                  client: httpx.Client | None = None) -> dict:
    """Save Street View + satellite photos for the market's buy-box properties.

    limit: at most this many properties, the ones still missing photos first, best-ranked first.
    dry_run: return imagery_estimate(...) and call nothing.
    confirm: needed when more than safety.confirm_over_properties properties would be charged;
      without it the result has needs_confirm=True and the estimate, and nothing is fetched.
    include_excluded: also fetch photos for properties outside the buy box.
    """
    settings = get_settings()
    cfg = imagery_config(settings)
    plan = _plan(conn, market_slug, cfg, settings, limit, include_excluded)
    estimate = _estimate_from_plan(conn, plan, cfg, settings)
    if dry_run or not estimate["enabled"]:
        return estimate
    key = secret(KEY_NAME)
    if not key:
        return _stopped(estimate, "There's no Google key in .env, so no photos were fetched. "
                                  "Add GOOGLE_MAPS_API_KEY to .env and run photos again.")
    if estimate["needs_confirm"] and not confirm:
        estimate.update(dry_run=False, message=f"Waiting for your OK: {estimate['message']} "
                                               f"Run again with confirm (CLI: --yes).")
        return estimate
    _repair_rows(conn, [j for j in plan.jobs if not _charged(j)], settings, cfg)
    jobs, capped = _within_caps([j for j in plan.jobs if _charged(j)], cfg)
    _ensure_rows(conn, jobs)
    used_before = {sku: month_usage(conn, sku) for sku in (SKU_STREET_VIEW, SKU_SATELLITE)}
    own_client = client is None
    http = client or geo.new_client(cfg.timeout_seconds)
    stats = _Stats()
    try:
        _run_jobs(conn, http, key, jobs, cfg, settings, stats, progress)
    finally:
        if own_client:
            http.close()
    if capped:
        stats.warnings.append("Hit the per-run photo cap in config.yaml (google.max_*_per_run). "
                              "Run photos again to fetch the rest.")
    result = _result(plan, stats, capped, _spent(stats, used_before, settings))
    log.info("Photos %s: %s", market_slug, result["message"])
    geo.call_progress(progress, 1.0, result["message"])
    return result


def _stopped(estimate: dict, message: str) -> dict:
    out = dict(estimate)
    out.update(dry_run=False, needs_confirm=False, error=message, message=message, fetched=0)
    return out


def _run_jobs(conn: sqlite3.Connection, http: httpx.Client, key: str, jobs: list[_Job], cfg: ImageryConfig,
              settings: Settings, stats: _Stats, progress: Progress) -> None:
    if not jobs:
        return
    stop = threading.Event()
    workers = min(cfg.concurrency, len(jobs))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="photos") as pool:
        futures = [pool.submit(_work, http, key, j, cfg, stop) for j in jobs]
        try:
            for done, fut in enumerate(as_completed(futures), start=1):
                _absorb(conn, fut.result(), settings, cfg, stats)
                geo.call_progress(progress, done / len(jobs) * 0.98,
                                  f"Fetched photos for {done:,} of {len(jobs):,} properties")
        except BaseException:
            stop.set()
            raise


def _billed(settings: Settings, sku: str, units: int, used_before: int) -> dict:
    """Dollar cost of `units` calls made this run, given how many were used earlier this month."""
    price = (settings.section("google").get("prices") or {}).get(sku, {}) or {}
    free_left = max(0, int(price.get("free_per_month", 0) or 0) - used_before)
    billable = max(0, units - free_left)
    return {"sku": sku, "units": units, "billable": billable,
            "usd": round(billable * float(price.get("per_1000", 0) or 0) / 1000.0, 2)}


def _spent(stats: _Stats, used_before: dict[str, int], settings: Settings) -> dict:
    sv = _billed(settings, SKU_STREET_VIEW, stats.sv_images, used_before[SKU_STREET_VIEW])
    sat = _billed(settings, SKU_SATELLITE, stats.sat_images, used_before[SKU_SATELLITE])
    return {"street_view": sv, "satellite": sat, "usd": round(sv["usd"] + sat["usd"], 2)}


def _result(plan: _Plan, stats: _Stats, capped: bool, cost: dict) -> dict:
    skipped = len([j for j in plan.jobs if _charged(j)]) - stats.attempted
    return {
        "dry_run": False,
        "needs_confirm": False,
        "enabled": True,
        "asset_class": plan.asset_class.key,
        "properties": len(plan.jobs),
        "cached": plan.complete + sum(1 for j in plan.jobs if not _charged(j)),
        "attempted": stats.attempted,
        "fetched": stats.fetched,
        "no_imagery": stats.no_imagery,
        "errors": stats.errors,
        "not_attempted": max(0, skipped),
        "street_view_images": stats.sv_images,
        "satellite_images": stats.sat_images,
        "metadata_calls": stats.metadata_calls,
        "capped": capped,
        "excluded": plan.excluded,
        "missing_coordinates": plan.missing_coordinates,
        "cost": cost,
        "error": stats.fatal,
        "warnings": stats.warnings,
        "message": _result_message(plan, stats, max(0, skipped), cost),
    }


def _result_message(plan: _Plan, stats: _Stats, skipped: int, cost: dict) -> str:
    n = geo.plural
    if not plan.jobs and not plan.complete:
        return _estimate_message(plan, 0, 0, 0, 0, {"usd": 0.0})
    msg = (f"Photos: saved new photos for {n(stats.fetched, 'property', 'properties')} "
           f"({n(stats.sv_images, 'Street View photo')}, {n(stats.sat_images, 'satellite photo')}, "
           f"about ${cost['usd']:.2f} after Google's free monthly calls); "
           f"{plan.complete:,} already had photos.")
    if stats.no_imagery:
        msg += (f" {n(stats.no_imagery, 'property has', 'properties have')} no Street View nearby "
                f"(satellite only).")
    if stats.errors:
        msg += f" {n(stats.errors, 'property', 'properties')} hit a problem and will be retried next run."
    if skipped:
        msg += f" {n(skipped, 'property', 'properties')} not reached this run."
    if stats.fatal:
        msg += f" Stopped early: {stats.fatal}"
    return msg


# --- helpers for other modules ----------------------------------------------------------------

_VISUAL_COLUMNS = frozenset({
    "status", "pano_id", "pano_lat", "pano_lng", "pano_date", "heading", "images_json", "ai_label", "ai_p",
    "image_quality", "property_visible", "cues_json", "reasons", "prior", "final_p", "model", "error",
    "reviewed_label", "reviewed_note", "reviewed_at", "checked_at", "ai_style", "ai_stories", "ai_buildings",
    "ai_condition"})


def record_pull(conn: sqlite3.Connection, property_id: int) -> None:
    """Note that this property's photos were pulled from Google just now (one row per pull; commits)."""
    with conn:
        conn.execute("INSERT INTO photo_pulls(property_id, pulled_at) VALUES(?, ?)", (int(property_id), now_iso()))


def update_visual(conn: sqlite3.Connection, property_id: int, data: Mapping[str, Any]) -> None:
    """Set some visual_checks columns for one property (the row is created as pending if missing).
    Doesn't commit; call inside `with conn:`."""
    bad = set(data) - _VISUAL_COLUMNS
    if bad:
        raise KeyError(f"Not visual_checks columns: {', '.join(sorted(bad))}")
    conn.execute("INSERT OR IGNORE INTO visual_checks(property_id, status) VALUES(?, 'pending')", (int(property_id),))
    if data:
        cols = list(data)
        conn.execute(f"UPDATE visual_checks SET {', '.join(f'{c} = ?' for c in cols)} WHERE property_id = ?",
                     (*[data[c] for c in cols], int(property_id)))


def images_for(property_id: int, settings: Settings | None = None) -> list[dict]:
    """Saved photos for one property as [{name, url, kind}] for the dashboard."""
    s = settings or get_settings()
    out = []
    for name in existing_images(s, property_id):
        kind = "satellite" if name == SAT_FILE else "yours" if name.startswith(YOURS_PREFIX) else "street_view"
        out.append({"name": name, "url": f"/images/{int(property_id)}/{name}", "kind": kind})
    return out


def map_links(lat: float | None, lng: float | None, heading: float | None = None) -> dict:
    """Free Google Maps links (no key) for the review card and detail panel."""
    if lat is None or lng is None:
        return {}
    c = f"{float(lat):.6f},{float(lng):.6f}"
    sv = f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={c}"
    if heading is not None:
        sv += f"&heading={float(heading):.0f}"
    return {
        "google_maps": f"https://www.google.com/maps/search/?api=1&query={c}",
        "street_view": sv,
        "satellite": f"https://www.google.com/maps/@?api=1&map_action=map&center={c}&zoom=19&basemap=satellite",
    }


__all__ = ["add_your_image", "your_images", "ImageryConfig", "StreetViewMetadata", "existing_images", "fetch_imagery", "image_paths",
           "imagery_config", "imagery_estimate", "images_for", "map_links", "record_pull", "street_view_shots",
           "update_visual"]
