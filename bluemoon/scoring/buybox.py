"""Brad's buy box: does a property pass every filter, and what colors does it get?

`evaluate(prop, enrich, asset_class, market, settings)` returns
`{in_buybox, exclusion_reasons, size_value, size_basis, size_color, visual_color, rent_color, ...}`.

Filters (a property failing any of them is set aside, never deleted):
- drive_time: drive minutes are known and over the market's `max_drive_minutes`.
- year: year built is known and before the asset class's `year_built_min` (a blank year is kept).
- size: the KNOWN size is outside the asset class's `ok` range. Estimates never exclude
  (Brad: an unknown size must not be ruled out).
- state: the property's state is not in `geography.allowed_states`.
- skip_county: the county is in `geography.skip_counties` for its state.
- property_type: the Crexi property type doesn't mention any of the asset class's `crexi_property_types`
  (matched at the start of a word, case-insensitive; a blank type is kept).
Missing data never excludes.

Size evidence, best first: units + sq ft > units > estimate (units = building sq ft / sqft_per_unit) >
unknown. `size_basis` records which one was used.

Colors: green | yellow | red | na (needs a human look / no data) | pending (not checked yet).
None means the axis doesn't apply to this asset class (e.g. no photo check for raw land).
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from ..config import AssetClass, Settings, get_settings
from ..db import loads
from ..util import clean_str, normalize_county

EXCLUSION_REASONS: tuple[str, ...] = ("drive_time", "year", "size", "state", "skip_county", "property_type")
EXCLUSION_LABELS: dict[str, str] = {
    "drive_time": "Too far to drive",
    "year": "Built too early",
    "size": "Wrong size",
    "state": "State not in the buy box",
    "skip_county": "County on the skip list",
    "property_type": "Not this kind of property",
}
SIZE_BASES: tuple[str, ...] = ("units+sqft", "units", "address_estimate", "sqft_estimate", "acres_estimate",
                               "sqft", "acres", "unknown")
SIZE_BASIS_LABELS: dict[str, str] = {
    "units+sqft": "Unit count and building size",
    "units": "Unit count",
    "address_estimate": "Estimated from the number of addresses",
    "sqft_estimate": "Estimated from building sq ft",
    "acres_estimate": "Estimated from lot acres",
    "sqft": "Building sq ft",
    "acres": "Lot acres",
    "unknown": "Unknown",
}
ESTIMATED_BASES = frozenset({"address_estimate", "sqft_estimate", "acres_estimate"})
SIZE_BUCKET_COLORS = {"ideal": "green", "ok": "yellow", "outside": "red", "unknown": "na"}
RENT_FIT_COLORS = {"in": "green", "near": "yellow", "out": "red"}
METRIC_UNITS = {"units": "units", "building_sqft": "sq ft", "lot_acres": "acres"}
SQFT_PER_UNIT_SANE = (250.0, 4000.0)     # outside this, units vs sq ft disagree: flag size_check


# --- settings ------------------------------------------------------------------------------------

class ScoringConfig(BaseModel):
    """`scoring:` from config.yaml. Missing keys use these defaults."""
    base_rate: float = 0.015
    learn_prior_strength: float = 30.0
    worn_improvement_ratio: float = 0.35     # buildings worth < 35% of the parcel = worn
    visual_green_min: float = 80.0           # final_p >= 80 green
    visual_red_max: float = 20.0             # final_p <= 20 red
    visual_yellow_split: float = 50.0        # yellow_high at or above, yellow_low below
    estimate_weight: float = 0.5             # an estimated size moves the score half as much (in log odds)
    positive_outcomes: list[str] = Field(default_factory=lambda: ["blue_moon", "bought"])
    learn_min_multiplier: float = 0.02
    learn_max_multiplier: float = 20.0


def scoring_config(settings: Settings | None = None) -> ScoringConfig:
    s = settings or get_settings()
    return ScoringConfig(**(s.section("scoring") or {}))


# --- size ----------------------------------------------------------------------------------------

class SizeInfo(BaseModel):
    metric: str
    value: float | None
    basis: str
    bucket: str            # ideal | ok | outside | unknown
    color: str             # green | yellow | red | na
    estimated: bool
    detail: str            # "34 units", "about 28 units (estimated from 24,000 sq ft at 850 sq ft per unit)"
    short: str             # "34 units", "about 28 units"


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return None if math.isnan(v) else float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _flags(prop: Mapping[str, Any]) -> set[str]:
    raw = prop.get("flags_json")
    if isinstance(raw, (list, tuple, set)):
        return set(raw)
    return set(loads(raw, []) or [])


def _fmt_size(v: float, metric: str) -> str:
    if metric == "lot_acres":
        return f"{v:,.1f} acres" if v < 100 else f"{v:,.0f} acres"
    return f"{v:,.0f} {METRIC_UNITS.get(metric, metric)}"


def size_bucket(value: float | None, asset_class: AssetClass) -> str:
    if value is None:
        return "unknown"
    lo, hi = asset_class.size.ideal
    if lo <= value <= hi:
        return "ideal"
    lo, hi = asset_class.size.ok
    if lo <= value <= hi:
        return "ok"
    return "outside"


def _units_value(prop: Mapping[str, Any], spec: Any) -> tuple[float | None, str, str | None]:
    units, sqft, acres = _num(prop.get("units")), _num(prop.get("building_sqft")), _num(prop.get("lot_acres"))
    if units is not None and units > 0:
        if "units_from_addresses" in _flags(prop):
            return units, "address_estimate", f"about {units:,.0f} units (counted from the number of addresses)"
        return units, ("units+sqft" if sqft else "units"), None
    if spec.fallback == "building_sqft" and sqft and spec.sqft_per_unit:
        est = max(1.0, round(sqft / spec.sqft_per_unit))
        return est, "sqft_estimate", (f"about {est:,.0f} units (estimated from {sqft:,.0f} sq ft at "
                                      f"{spec.sqft_per_unit:,.0f} sq ft per unit)")
    if spec.fallback == "lot_acres" and acres and spec.acres_per_unit:
        est = max(1.0, round(acres / spec.acres_per_unit))
        return est, "acres_estimate", (f"about {est:,.0f} units (estimated from {acres:,.1f} acres at "
                                       f"{spec.acres_per_unit:g} acres per unit)")
    return None, "unknown", None


def size_info(prop: Mapping[str, Any], asset_class: AssetClass) -> SizeInfo:
    """The property's size in the asset class's metric, with the evidence it came from."""
    spec = asset_class.size
    metric = spec.metric
    detail: str | None = None
    if metric == "units":
        value, basis, detail = _units_value(prop, spec)
    elif metric == "building_sqft":
        value = _num(prop.get("building_sqft"))
        basis = "sqft" if value else "unknown"
    elif metric == "lot_acres":
        value = _num(prop.get("lot_acres"))
        basis = "acres" if value else "unknown"
    else:
        value, basis = _num(prop.get(metric)), (metric if prop.get(metric) else "unknown")
    if value is not None and value <= 0:
        value, basis = None, "unknown"
    bucket = size_bucket(value, asset_class)
    short = _fmt_size(value, metric) if value is not None else f"{METRIC_UNITS.get(metric, metric)} unknown"
    estimated = basis in ESTIMATED_BASES
    if estimated:
        short = f"about {short}"
    return SizeInfo(metric=metric, value=value, basis=basis, bucket=bucket, color=SIZE_BUCKET_COLORS[bucket],
                    estimated=estimated, detail=detail or short, short=short)


# --- colors --------------------------------------------------------------------------------------

def visual_bucket(final_p: float | None, cfg: ScoringConfig) -> str | None:
    """green | yellow_high | yellow_low | red, or None when there is no photo verdict."""
    if final_p is None:
        return None
    if final_p >= cfg.visual_green_min:
        return "green"
    if final_p <= cfg.visual_red_max:
        return "red"
    return "yellow_high" if final_p >= cfg.visual_yellow_split else "yellow_low"


def visual_color(visual: Mapping[str, Any] | None, asset_class: AssetClass, cfg: ScoringConfig) -> str | None:
    """green | yellow | red from final_p; na = checked but needs a human look; pending = not checked yet."""
    if not asset_class.visual_enabled:
        return None
    if not visual:
        return "pending"
    bucket = visual_bucket(_num(visual.get("final_p")), cfg)
    if bucket is not None:
        return "yellow" if bucket.startswith("yellow") else bucket
    if visual.get("status") in ("done", "no_imagery"):
        return "na"
    return "pending"


def rent_color(rent: Mapping[str, Any] | None, asset_class: AssetClass) -> str | None:
    """green (in) | yellow (near) | red (out) | na (no HUD rents) | pending (not checked yet)."""
    if not asset_class.rent_enabled:
        return None
    if not rent:
        return "pending"
    return RENT_FIT_COLORS.get(str(rent.get("fit") or "").lower(), "na")


# --- filters -------------------------------------------------------------------------------------

def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _type_patterns(types: list[str]) -> re.Pattern[str] | None:
    words = [re.sub(r"[\s\-_/]+", " ", t.strip().lower()) for t in types if t and t.strip()]
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in words) + ")")


def property_type_matches(ptype: Any, asset_class: AssetClass) -> bool | None:
    """True/False, or None when the type is blank or the asset class lists no types."""
    text = clean_str(ptype)
    pattern = _type_patterns(asset_class.crexi_property_types)
    if not text or pattern is None:
        return None
    return bool(pattern.search(re.sub(r"[\s\-_/]+", " ", text.lower())))


def _geo(settings: Settings) -> tuple[set[str], dict[str, set[str]]]:
    g = settings.section("geography")
    allowed = {str(x).strip().upper() for x in (g.get("allowed_states") or []) if str(x).strip()}
    skip = {str(st).strip().upper(): {normalize_county(c) or "" for c in (cs or [])}
            for st, cs in (g.get("skip_counties") or {}).items()}
    return allowed, skip


def evaluate(prop: Mapping[str, Any], enrich: Mapping[str, Any] | None, asset_class: AssetClass,
             market: Any, settings: Settings | None = None) -> dict:
    """Check one property against the buy box.

    `prop` is a `properties` row. `enrich` holds the property's enrichment rows (each may be None):
    `{"drive": drive_times row, "rent": rent_fits row, "visual": visual_checks row, "owner": owners row}`.
    `market` is a `markets` row (or MarketConfig) with `max_drive_minutes`.
    """
    s = settings or get_settings()
    cfg = scoring_config(s)
    enrich = enrich or {}
    reasons: list[str] = []
    details: dict[str, str] = {}
    flags: list[str] = []

    def exclude(reason: str, text: str) -> None:
        reasons.append(reason)
        details[reason] = text

    drive = enrich.get("drive") or {}
    minutes = _num(drive.get("minutes"))
    max_minutes = _num(_get(market, "max_drive_minutes"))
    if minutes is None:
        flags.append("drive_unknown")
    else:
        if drive.get("method") == "estimate":
            flags.append("drive_estimated")
        if max_minutes is not None and minutes > max_minutes:
            approx = "about " if drive.get("method") == "estimate" else ""
            exclude("drive_time", f"{approx}{minutes:.0f} min drive; the limit is {max_minutes:.0f} min")

    year = _num(prop.get("year_built"))
    if year is None:
        flags.append("year_unknown")
    elif asset_class.year_built_min and year < asset_class.year_built_min:
        exclude("year", f"built in {year:.0f}; the buy box starts at {asset_class.year_built_min}")

    size = size_info(prop, asset_class)
    if size.basis == "unknown":
        flags.append("size_unknown")
    elif size.estimated:
        flags.append("size_estimated")
    if size.bucket == "outside" and not size.estimated:
        lo, hi = asset_class.size.ok
        unit = METRIC_UNITS.get(size.metric, size.metric)
        exclude("size", f"{size.detail}; the buy box is {lo:,.0f}-{hi:,.0f} {unit}")
    units, sqft = _num(prop.get("units")), _num(prop.get("building_sqft"))
    if size.basis == "units+sqft" and units and sqft:
        per_unit = sqft / units
        if not SQFT_PER_UNIT_SANE[0] <= per_unit <= SQFT_PER_UNIT_SANE[1]:
            flags.append("size_check")

    allowed, skip = _geo(s)
    state = (clean_str(prop.get("state")) or "").upper()
    county = normalize_county(prop.get("county"))
    if not state:
        flags.append("state_unknown")
    elif allowed and state not in allowed:
        exclude("state", f"in {state}; the buy box covers {', '.join(sorted(allowed))}")
    if state and county and county in skip.get(state, set()):
        exclude("skip_county", f"{county.title()} County, {state} is on the skip list")

    type_ok = property_type_matches(prop.get("property_type"), asset_class)
    if type_ok is None:
        flags.append("type_unknown")
    elif not type_ok:
        exclude("property_type", f"listed as {clean_str(prop.get('property_type'))}, not {asset_class.label.lower()}")

    return {
        "in_buybox": not reasons,
        "exclusion_reasons": reasons,
        "exclusion_details": details,
        "size_value": size.value,
        "size_basis": size.basis,
        "size_bucket": size.bucket,
        "size_color": size.color,
        "size_detail": size.detail,
        "size_estimated": size.estimated,
        "visual_color": visual_color(enrich.get("visual"), asset_class, cfg),
        "rent_color": rent_color(enrich.get("rent"), asset_class),
        "drive_minutes": minutes,
        "flags": flags,
    }
