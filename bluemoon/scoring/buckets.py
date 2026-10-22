"""Set-aside groups: the properties out of the buy box, grouped so Seth can look at one group at a time.

A group key is '<reason>:<id>' (pattern BUCKET_KEY), for example 'drive_time:76-90', 'size:1-4', 'year:1960s',
'skip_county:oh-clermont', 'state:ky', 'property_type:retail' or 'manual:by-hand'. A value that is missing, or
that no group covers, lands in '<reason>:other', so a reason's groups always add up to its count.

The keys are worked out when the scores are saved (rank.compute_scores -> scores.aside_buckets) from the same
values the rules used, and from what the rules said (rule_reasons), not from decisions by hand. The Why? panel
counts, the Review list (?bucket=) and the Decide buttons all read those stored keys, so "Add all 40" always
means the 40 the panel showed.

Edges come from YAML (code defaults below):
- drive_time: config.yaml set_aside.drive_minutes, [75, 90, 120]. Edges at or under the market's limit are
  skipped. Minutes are rounded the way the screens show them; anything over the limit that rounds to it goes in
  the first group. Ids '61-75', '76-90', '91-120', '121-up'.
- size: config/asset_classes.yaml size.set_aside_below / set_aside_above. The outer edges are size.ok, so every
  size the rules set aside has a group. Apartments: '1-4', '5-9' (under 10), '81-100', '101-200', '201-up'.
- year: config.yaml set_aside.year_step (10) for year_steps (3) groups below the asset class's year_built_min,
  then one group back to year_oldest (1900) and one before it: '1960s', '1950s', '1940s', '1900-1939',
  'before-1900'.
- skip_county: state + county ('oh-clermont'); state: the state ('ky'); property_type: the listed type
  ('retail'); manual: 'by-hand'.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..config import AssetClass, Settings, get_settings
from ..util import clean_str, normalize_county
from .buybox import EXCLUSION_LABELS, MANUAL_REASON, METRIC_UNITS, SET_ASIDE_REASONS, shown_minutes

BUCKET_KEY = re.compile(r"^[a-z_]+:[a-z0-9-]+$")
OTHER = "other"
BY_HAND = "by-hand"
OTHER_LABEL = "Not grouped yet"
INTEGER_METRICS = frozenset({"units", "building_sqft"})
MAX_SLUG = 48


class SetAsideConfig(BaseModel):
    """`set_aside:` from config.yaml. Missing keys use these defaults."""
    drive_minutes: list[float] = Field(default_factory=lambda: [75.0, 90.0, 120.0])
    year_step: int = 10
    year_steps: int = 3
    year_oldest: int = 1900


def set_aside_config(settings: Settings | None = None) -> SetAsideConfig:
    s = settings or get_settings()
    raw = {k: v for k, v in (s.section("set_aside") or {}).items() if v is not None}
    return SetAsideConfig(**raw)


def slug(text: Any) -> str:
    """'Mixed Use / Retail' -> 'mixed-use-retail'. '' when nothing is left."""
    s = re.sub(r"[^a-z0-9]+", "-", (clean_str(text) or "").lower()).strip("-")
    return s[:MAX_SLUG].strip("-")


def _num_id(v: float) -> str:
    """A number as a key part: 5 -> '5', 0.5 -> '0p5'."""
    return str(int(v)) if float(v).is_integer() else f"{v:g}".replace(".", "p")


def _num_text(v: float) -> str:
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,g}"


@dataclass(frozen=True)
class Range:
    """One group of a numeric reason: lo <= value < hi (below) or lo < value <= hi (above, drive, year)."""
    id: str
    label: str
    lo: float | None
    hi: float | None


@dataclass(frozen=True)
class GroupSpec:
    """Every group definition for one market, built once per scoring run (group_spec)."""
    max_minutes: float | None
    drive: list[Range]
    size_metric: str
    size_unit: str
    size_ok: tuple[float, float]
    size_below: list[Range]
    size_above: list[Range]
    year_min: int | None
    years: list[Range]
    labels: dict[str, str] = field(default_factory=dict)       # key -> label, for the groups known up front

    def order(self, reason: str) -> list[str]:
        """The fixed groups of a reason in display order (the others sort by size)."""
        if reason == "drive_time":
            return [f"drive_time:{r.id}" for r in self.drive]
        if reason == "size":
            return [f"size:{r.id}" for r in self.size_below + self.size_above]
        if reason == "year":
            return [f"year:{r.id}" for r in self.years]
        return []


# --- building the groups -------------------------------------------------------------------------

def _drive_ranges(max_minutes: float | None, edges: Iterable[float]) -> list[Range]:
    base = math.floor(max_minutes) if max_minutes is not None else 0
    lower = base + 1
    out: list[Range] = []
    for e in sorted({int(round(x)) for x in edges if x is not None and x > base}):
        out.append(Range(f"{lower}-{e}", f"{lower}–{e} min", lower, e))
        lower = e + 1
    out.append(Range(f"{lower}-up", f"Over {lower - 1} min", lower, None))
    return out


def _nice(v: float) -> float:
    """Round to two significant digits (default size edges)."""
    if v <= 0:
        return v
    digits = 1 - int(math.floor(math.log10(v)))
    out = round(v, digits)
    return float(int(out)) if float(out).is_integer() else out


def _size_edges(ac: AssetClass) -> tuple[list[float], list[float]]:
    lo, hi = (float(x) for x in ac.size.ok)
    below = ac.size.set_aside_below if ac.size.set_aside_below is not None else [_nice(lo / 2)]
    above = ac.size.set_aside_above if ac.size.set_aside_above is not None else [_nice(hi * 1.25), _nice(hi * 2.5)]
    return (sorted({float(e) for e in below if 0 < float(e) < lo}),
            sorted({float(e) for e in above if float(e) > hi}))


def _size_ranges(ac: AssetClass) -> tuple[list[Range], list[Range]]:
    """Groups below size.ok[0] (lo <= v < hi) and above size.ok[1] (lo < v <= hi)."""
    lo, hi = (float(x) for x in ac.size.ok)
    unit = METRIC_UNITS.get(ac.size.metric, ac.size.metric)
    integer = ac.size.metric in INTEGER_METRICS
    below_edges, above_edges = _size_edges(ac)
    below: list[Range] = []
    starts = [None, *below_edges]
    for start, end in zip(starts, [*below_edges, lo]):
        first = math.ceil(start) if start is not None else 1
        last = math.ceil(end) - 1
        if integer and (start is not None or ac.size.metric == "units") and last >= first:
            text = f"{first:,}–{last:,}" if last > first else f"{first:,}"
            below.append(Range(f"{first}-{last}", f"{text} {unit}", start, end))
        elif start is None:
            below.append(Range(f"under-{_num_id(end)}", f"Under {_num_text(end)} {unit}", None, end))
        else:
            below.append(Range(f"{_num_id(start)}-{_num_id(end)}",
                               f"{_num_text(start)} to under {_num_text(end)} {unit}", start, end))
    above: list[Range] = []
    for start, end in zip([hi, *above_edges], [*above_edges, None]):
        if integer:
            first = math.floor(start) + 1
            if end is None:
                above.append(Range(f"{first}-up", f"{first:,}+ {unit}", start, None))
            else:
                above.append(Range(f"{first}-{int(end)}", f"{first:,}–{int(end):,} {unit}", start, end))
        elif end is None:
            above.append(Range(f"over-{_num_id(start)}", f"Over {_num_text(start)} {unit}", start, None))
        else:
            above.append(Range(f"{_num_id(start)}-{_num_id(end)}",
                               f"Over {_num_text(start)} to {_num_text(end)} {unit}", start, end))
    return below, above


def _year_ranges(year_min: int | None, cfg: SetAsideConfig) -> list[Range]:
    if not year_min:
        return []
    step, oldest = max(1, int(cfg.year_step)), int(cfg.year_oldest)
    hi = int(year_min)
    out: list[Range] = []
    for _ in range(max(0, int(cfg.year_steps))):
        if hi <= oldest:
            break
        lo = max(hi - step, oldest)
        if step == 10 and lo % 10 == 0 and hi - lo == 10:
            out.append(Range(f"{lo}s", f"{lo}s", lo, hi))
        else:
            out.append(Range(f"{lo}-{hi - 1}", f"{lo}–{hi - 1}", lo, hi))
        hi = lo
    if hi > oldest:
        out.append(Range(f"{oldest}-{hi - 1}", f"{oldest}–{hi - 1}", oldest, hi))
        hi = oldest
    out.append(Range(f"before-{hi}", f"Before {hi}", None, hi))
    return out


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def group_spec(market: Any, ac: AssetClass, settings: Settings | None = None) -> GroupSpec:
    """The set-aside groups for one market (a markets row or MarketConfig) and its asset class."""
    s = settings or get_settings()
    cfg = set_aside_config(s)
    raw_max = _get(market, "max_drive_minutes")
    max_minutes = float(raw_max) if raw_max is not None else None
    drive = _drive_ranges(max_minutes, cfg.drive_minutes)
    below, above = _size_ranges(ac)
    years = _year_ranges(ac.year_built_min, cfg)
    labels = {f"drive_time:{r.id}": r.label for r in drive}
    labels.update({f"size:{r.id}": r.label for r in below + above})
    labels.update({f"year:{r.id}": r.label for r in years})
    labels[f"{MANUAL_REASON}:{BY_HAND}"] = "Set aside by hand"
    for state, counties in ((s.section("geography") or {}).get("skip_counties") or {}).items():
        for county in counties or []:
            st, co = str(state).strip().upper(), normalize_county(county) or ""
            if st and co:
                labels[f"skip_county:{slug(f'{st}-{co}')}"] = f"{co.title()} County, {st}"
    return GroupSpec(max_minutes=max_minutes, drive=drive, size_metric=ac.size.metric,
                     size_unit=METRIC_UNITS.get(ac.size.metric, ac.size.metric),
                     size_ok=(float(ac.size.ok[0]), float(ac.size.ok[1])), size_below=below, size_above=above,
                     year_min=ac.year_built_min, years=years, labels=labels)


# --- putting a property in its groups ------------------------------------------------------------

def _number(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _drive_id(minutes: Any, spec: GroupSpec) -> str:
    m = _number(minutes)
    if m is None or (spec.max_minutes is not None and m <= spec.max_minutes):
        return OTHER
    shown = shown_minutes(m)               # what the screens show ("90 min"; 90.5 shows as 91)
    first = spec.drive[0]
    if first.lo is not None and shown < first.lo:
        shown = int(first.lo)              # over the limit but rounds to it: the first group
    for r in spec.drive:
        if r.hi is None or shown <= r.hi:
            return r.id
    return OTHER


def _size_id(value: Any, spec: GroupSpec) -> str:
    v = _number(value)
    if v is None:
        return OTHER
    lo, hi = spec.size_ok
    if v < lo:
        for r in spec.size_below:
            if (r.lo is None or v >= r.lo) and (r.hi is None or v < r.hi):
                return r.id
    elif v > hi:
        for r in spec.size_above:
            if (r.lo is None or v > r.lo) and (r.hi is None or v <= r.hi):
                return r.id
    return OTHER


def _year_id(year: Any, spec: GroupSpec) -> str:
    y = _number(year)
    if y is None or not spec.year_min or y >= spec.year_min:
        return OTHER
    for r in spec.years:
        if (r.lo is None or y >= r.lo) and (r.hi is None or y < r.hi):
            return r.id
    return OTHER


def group_id(reason: str, prop: Mapping[str, Any], ev: Mapping[str, Any], spec: GroupSpec) -> str:
    """The group of one reason for one property, from the values the rules used."""
    if reason == "drive_time":
        return _drive_id(ev.get("drive_minutes"), spec)
    if reason == "size":
        return _size_id(ev.get("size_value"), spec)
    if reason == "year":
        return _year_id(prop.get("year_built"), spec)
    if reason == "skip_county":
        state, county = clean_str(prop.get("state")), normalize_county(prop.get("county"))
        return slug(f"{state}-{county}") if state and county else OTHER
    if reason == "state":
        return slug(prop.get("state")) or OTHER
    if reason == "property_type":
        return slug(prop.get("property_type")) or OTHER
    if reason == MANUAL_REASON:
        return BY_HAND
    return OTHER


def bucket_keys(rule_reasons: Iterable[str], prop: Mapping[str, Any], ev: Mapping[str, Any], spec: GroupSpec,
                set_aside_by_hand: bool = False) -> list[str]:
    """scores.aside_buckets: one group per rule reason (what the rules said), plus 'manual:by-hand'."""
    keys = [f"{r}:{group_id(r, prop, ev, spec)}" for r in rule_reasons if r in SET_ASIDE_REASONS]
    if set_aside_by_hand:
        keys.append(f"{MANUAL_REASON}:{BY_HAND}")
    return list(dict.fromkeys(keys))


# --- reading keys back ---------------------------------------------------------------------------

def parse_key(key: Any) -> tuple[str, str]:
    """('drive_time', '76-90') from 'drive_time:76-90'. ValueError (shown to Seth) for anything else."""
    text = str(key or "").strip()
    if len(text) > 120 or not BUCKET_KEY.match(text):
        raise ValueError("That set-aside group isn't one the app knows. Open the Why? panel and pick a group.")
    reason, gid = text.split(":", 1)
    if reason not in SET_ASIDE_REASONS:
        raise ValueError("That set-aside group isn't one the app knows. Open the Why? panel and pick a group.")
    return reason, gid


def label_for(key: str, spec: GroupSpec, sample: Any = None) -> str:
    """A group's label: '76–90 min', '1–4 units', 'Clermont County, OH', 'KY', 'Retail'."""
    if key in spec.labels:
        return spec.labels[key]
    reason, _, gid = key.partition(":")
    if gid == OTHER:
        return OTHER_LABEL
    if reason == "skip_county":
        state, _, county = gid.partition("-")
        return f"{county.replace('-', ' ').title()} County, {state.upper()}"
    if reason == "state":
        return gid.upper()
    text = clean_str(sample)
    if text:
        return text
    return gid.replace("-", " ").capitalize()


# --- SQL: which properties are in a group --------------------------------------------------------

# One group's members, as a WHERE condition on `scores s` with :bucket, :bucket_reason and :bucket_other.
# Rows saved before version 2 have no groups yet: they count as '<reason>:other', in the panel and here.
MEMBER_SQL = ("(EXISTS (SELECT 1 FROM json_each(s.aside_buckets) bk WHERE bk.value = :bucket) "
              "OR (:bucket_other = 1 AND s.property_id IS NOT NULL AND s.aside_buckets IS NULL AND EXISTS "
              "(SELECT 1 FROM json_each(COALESCE(s.rule_reasons, s.exclusion_reasons)) br "
              "WHERE br.value = :bucket_reason)))")


def member_params(key: str) -> dict[str, Any]:
    reason, gid = parse_key(key)
    return {"bucket": f"{reason}:{gid}", "bucket_reason": reason, "bucket_other": 1 if gid == OTHER else 0}


# --- plain-English explanations ------------------------------------------------------------------

def _names(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def skip_counties_text(settings: Settings | None = None) -> str:
    """'Clermont, Fairfield, Licking, Mercer and Morrow (OH); Fayette, ... (IN)' from config.yaml."""
    s = settings or get_settings()
    parts = []
    for state, counties in ((s.section("geography") or {}).get("skip_counties") or {}).items():
        names = [(normalize_county(c) or "").title() for c in counties or [] if normalize_county(c)]
        if names:
            parts.append(f"{_names(names)} ({str(state).upper()})")
    return "; ".join(parts)


def explain(reason: str, market: Any, ac: AssetClass, settings: Settings | None = None, *,
            also_far: int = 0) -> str:
    """What a set-aside reason means, in plain English. also_far: for skip_county, how many of them are
    also over the drive limit."""
    s = settings or get_settings()
    max_minutes = _get(market, "max_drive_minutes")
    origin = _get(market, "origin_label") or "the starting point"
    unit = METRIC_UNITS.get(ac.size.metric, ac.size.metric)
    lo, hi = ac.size.ok
    allowed = [str(x).upper() for x in ((s.section("geography") or {}).get("allowed_states") or [])]
    if reason == "drive_time":
        return (f"More than {max_minutes} minutes' drive from {origin}. Drive times marked \"about\" are "
                "straight-line estimates until Google measures the real drive.")
    if reason == "year":
        return f"Built before {ac.year_built_min}; Brad's buy box starts at {ac.year_built_min}. A blank year is kept."
    if reason == "size":
        return (f"A known size under {_num_text(float(lo))} or over {_num_text(float(hi))} {unit}. "
                "An estimated or unknown size never sets a property aside.")
    if reason == "state":
        return f"Not in a state the buy box covers ({_names(allowed)})." if allowed else "Not in a state the buy box covers."
    if reason == "skip_county":
        named = skip_counties_text(s)
        text = (f"Counties Brad said to skip, even inside the drive limit: {named}." if named
                else "Counties Brad said to skip, even inside the drive limit.")
        if also_far:
            text += f" {also_far:,} of these {'is' if also_far == 1 else 'are'} also over the drive limit."
        return text
    if reason == "property_type":
        return f"Crexi lists them as something other than {ac.label.lower()}."
    if reason == MANUAL_REASON:
        return "You or your team set these aside by hand. Clear the decision to let the rules decide again."
    return EXCLUSION_LABELS.get(reason, reason)


__all__ = [
    "BUCKET_KEY", "BY_HAND", "GroupSpec", "MEMBER_SQL", "OTHER", "OTHER_LABEL", "Range", "SetAsideConfig",
    "bucket_keys", "explain", "group_id", "group_spec", "label_for", "member_params", "parse_key",
    "set_aside_config", "skip_counties_text", "slug",
]
