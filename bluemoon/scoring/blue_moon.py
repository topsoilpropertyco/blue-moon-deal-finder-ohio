"""The Blue Moon score: is this owner worth chasing?

It works like a weather forecast. Start from a rare base rate (`scoring.base_rate`, 1.5%), then every
signal we know multiplies the odds by its multiplier (a likelihood ratio) from
`config/asset_classes.yaml` (`default_signals` plus the asset class's `signals` overrides):

    odds = base_rate / (1 - base_rate) x m1 x m2 x ...      score = odds / (1 + odds)

Missing data is skipped (multiplier None, "No data - not counted"), never held against a property.
The signal list is exactly the asset class's `signals` keys, in this order: size, visual (only when the
asset class has a photo check), rent_fit (only when rent is enabled), drive, owner_type, years_owned,
debt, absentee, vacancy, condition, distress.

`signals_for` returns `[{key, label, value, bucket, multiplier, why}]`: `value` is what we know in plain
words, `bucket` is the multiplier key it fell in (used by learn.py), `why` explains the effect.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import AssetClass, Settings, get_settings
from ..enrich import owners as owner_rules
from ..util import clean_str, parse_date
from .buybox import ScoringConfig, SizeInfo, scoring_config, size_info, visual_bucket

SIGNAL_ORDER: tuple[str, ...] = ("size", "visual", "rent_fit", "drive", "owner_type", "years_owned", "debt",
                                 "absentee", "vacancy", "condition", "distress")
SIGNAL_LABELS: dict[str, str] = {
    "size": "Size",
    "visual": "Photo check",
    "rent_fit": "Rent fit",
    "drive": "Drive time",
    "owner_type": "Owner type",
    "years_owned": "Years owned",
    "debt": "Mortgage",
    "absentee": "Where the owner lives",
    "vacancy": "Vacancy",
    "condition": "Condition",
    "distress": "Pre-foreclosure",
}
# Keys inside a signal's YAML that are settings, not multipliers.
PARAM_KEYS = frozenset({"near_max", "mid_max", "estimate_weight"})
BUCKET_LABELS: dict[str, str] = {
    "ideal": "Ideal size", "ok": "In the buy box", "outside": "Outside the buy box",
    "green": "Likely target (80%+)", "yellow_high": "Leaning target (50-79%)",
    "yellow_low": "Leaning not (21-49%)", "red": "Not the target (20% or less)",
    "in": "Fits the rent scale", "near": "Near the rent scale", "out": "Off the rent scale",
    "near_drive": "Close", "mid": "Middle distance", "far": "Far",
    "free_and_clear": "Free and clear", "maturing_soon": "Loan maturing soon", "active": "Active loan",
    "outofstate": "Out of state", "instate": "Elsewhere in the state", "local": "Local",
    "vacant": "Vacant", "occupied": "Occupied", "worn": "Worn (low building value)", "normal": "Normal",
    "preforeclosure": "Pre-foreclosure", "none": "None",
    "20+": "20+ years", "10-19": "10-19 years", "5-9": "5-9 years", "0-4": "0-4 years",
}
NO_DATA = "No data - not counted"


def bucket_label(signal: str, bucket: str) -> str:
    if signal == "drive" and bucket == "near":
        return BUCKET_LABELS["near_drive"]
    if signal == "owner_type":
        return owner_rules.OWNER_TYPE_LABELS.get(bucket, bucket)
    return BUCKET_LABELS.get(bucket, bucket.replace("_", " ").capitalize())


def active_signal_keys(asset_class: AssetClass) -> list[str]:
    """The asset class's signal keys, in display order; photo/rent signals only when enabled."""
    keys = [k for k in SIGNAL_ORDER if k in asset_class.signals]
    if not asset_class.visual_enabled:
        keys = [k for k in keys if k != "visual"]
    if not asset_class.rent_enabled:
        keys = [k for k in keys if k != "rent_fit"]
    return keys


def multipliers(spec: Mapping[str, Any] | None) -> dict[str, float]:
    """The bucket -> multiplier part of one signal's YAML (drops settings like near_max)."""
    out: dict[str, float] = {}
    for k, v in (spec or {}).items():
        if k in PARAM_KEYS or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[str(k)] = float(v)
    return out


# --- helpers -------------------------------------------------------------------------------------

def fmt_mult(m: float) -> str:
    return f"{m:.2f}".rstrip("0").rstrip(".")


def effect_text(m: float | None) -> str:
    if m is None:
        return NO_DATA
    if m > 1.0001:
        return f"raises the odds x{fmt_mult(m)}"
    if m < 0.9999:
        return f"lowers the odds x{fmt_mult(m)}"
    return "no effect (x1)"


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _humanize(label: str) -> str:
    return label.replace("_", " ").lower()


def _month_year(v: Any) -> str | None:
    d = parse_date(v)
    return d.strftime("%b %Y") if d else None


@dataclass
class _Ctx:
    prop: Mapping[str, Any]
    enrich: Mapping[str, Any]
    ac: AssetClass
    cfg: ScoringConfig
    ocfg: owner_rules.OwnerConfig
    today: date
    size: SizeInfo

    @property
    def owner(self) -> Mapping[str, Any]:
        return self.enrich.get("owner") or {}

    def years_owned(self) -> float | None:
        y = _num(self.owner.get("years_owned"))
        if y is None and not self.owner:
            y = owner_rules.years_owned(self.prop.get("sale_date"), self.today)
        return y


# A reading is (bucket or None, value shown to Seth, fact sentence or reason it's missing).
Reading = tuple[str | None, str | None, str]


def _read_size(c: _Ctx) -> Reading:
    s = c.size
    if s.bucket == "unknown":
        return None, None, "size unknown"
    lo, hi = c.ac.size.ok
    ilo, ihi = c.ac.size.ideal
    unit = {"units": "units", "building_sqft": "sq ft", "lot_acres": "acres"}.get(s.metric, s.metric)
    where = {"ideal": f"in the ideal range ({ilo:,.0f}-{ihi:,.0f} {unit})",
             "ok": f"in the buy box ({lo:,.0f}-{hi:,.0f}) but not the ideal {ilo:,.0f}-{ihi:,.0f}",
             "outside": f"outside the buy box ({lo:,.0f}-{hi:,.0f} {unit})"}[s.bucket]
    note = "; it's an estimate, so it counts for less" if s.estimated else ""
    return s.bucket, s.short, f"{s.detail[0].upper()}{s.detail[1:]} is {where}{note}"


def _read_visual(c: _Ctx) -> Reading:
    v = c.enrich.get("visual") or {}
    p = _num(v.get("final_p"))
    if not v:
        return None, None, "the photo check hasn't run yet"
    if p is None:
        if v.get("status") == "no_imagery":
            return None, None, "no Street View photos here; needs a human look"
        if v.get("status") == "done":
            return None, None, "the photos were unclear; waiting for your review"
        return None, None, "the photo check hasn't finished"
    targets = " or ".join(_humanize(t) for t in (c.ac.visual_check.get("target_labels") or [])) or "the target"
    label = v.get("reviewed_label") or v.get("ai_label")
    seen = f" ({'you said' if v.get('reviewed_label') else 'AI says'} {_humanize(label)})" if label else ""
    return visual_bucket(p, c.cfg), f"{p:.0f}%", f"{p:.0f}% chance it is {targets}{seen}"


def _read_rent(c: _Ctx) -> Reading:
    r = c.enrich.get("rent") or {}
    fit = str(r.get("fit") or "").lower()
    detail = clean_str(r.get("detail"))
    if fit not in ("in", "near", "out"):
        return None, None, (detail or "no HUD rent figures yet").rstrip(".")
    shown = {"in": "Fits", "near": "Near", "out": "Off"}[fit]
    return fit, shown, (detail or f"HUD area rents are {bucket_label('rent_fit', fit).lower()}").rstrip(".")


def _read_drive(c: _Ctx) -> Reading:
    d = c.enrich.get("drive") or {}
    minutes = _num(d.get("minutes"))
    if minutes is None:
        return None, None, "drive time not worked out yet"
    spec = c.ac.signals.get("drive") or {}
    near_max, mid_max = _num(spec.get("near_max")) or 30.0, _num(spec.get("mid_max")) or 50.0
    bucket = "near" if minutes <= near_max else ("mid" if minutes <= mid_max else "far")
    if d.get("method") == "estimate":
        return bucket, f"about {minutes:.0f} min", f"About a {minutes:.0f}-minute drive (straight-line estimate)"
    return bucket, f"{minutes:.0f} min", f"{minutes:.0f}-minute drive"


_OWNER_WHY = {
    "individual": "decides alone and is often open to an offer",
    "trust": "trusts often sell when a family settles its affairs",
    "estate": "heirs often want a quick, clean sale",
    "small_llc": "usually one or two investors",
    "corporation": "sells on its own schedule",
    "portfolio": "experienced owners who price like professionals",
    "institutional": "rarely sells below market",
    "government": "almost never sells off-market",
    "nonprofit": "sells rarely and slowly",
}


def _read_owner_type(c: _Ctx) -> Reading:
    t = c.owner.get("owner_type") or owner_rules.classify_owner(c.prop.get("owner_name"))
    if not t or t == "unknown":
        return None, None, "no owner name on record" if not clean_str(c.prop.get("owner_name")) \
            else "can't tell what kind of owner this is"
    label = owner_rules.OWNER_TYPE_LABELS.get(t, t)
    count = _num(c.owner.get("portfolio_count"))
    held = f" holding {count:,.0f} properties" if t in ("portfolio", "institutional") and count and count > 1 else ""
    return t, label, f"{label}{held}: {_OWNER_WHY.get(t, 'owner type')}"


def _year_bucket(years: float, keys: list[str]) -> str | None:
    whole = math.floor(years)
    for k in keys:
        try:
            if k.endswith("+") and whole >= float(k[:-1]):
                return k
            if "-" in k:
                lo, hi = (float(x) for x in k.split("-", 1))
                if lo <= whole <= hi:
                    return k
        except ValueError:
            continue
    return None


def _read_years(c: _Ctx) -> Reading:
    y = c.years_owned()
    if y is None:
        return None, None, "no sale date on record"
    bucket = _year_bucket(y, list(multipliers(c.ac.signals.get("years_owned"))))
    sold = parse_date(c.prop.get("sale_date"))
    since = f" (since {sold.year})" if sold else ""
    return bucket, f"{y:.0f} yrs", f"Owned {y:.0f} years{since}"


def _read_debt(c: _Ctx) -> Reading:
    p = c.prop
    status = c.owner.get("debt_status")
    if not status:
        status = owner_rules.debt_status(p, c.years_owned(), c.today, c.ocfg)
    lender = clean_str(p.get("lender"))
    when = _month_year(p.get("loan_maturity"))
    matured = parse_date(p.get("loan_maturity"))
    if status == "free_and_clear":
        if owner_rules.has_loan(p) and when:
            return status, "Free and clear", f"Last recorded loan matured {when} with no newer loan: probably paid off"
        y = c.years_owned()
        return status, "Free and clear", f"No loan on record after {y:.0f} years: probably owned free and clear" \
            if y is not None else "No loan on record: probably owned free and clear"
    if status == "maturing_soon":
        verb = "matured" if matured and matured < c.today else "matures"
        who = f" from {lender}" if lender else ""
        return status, "Maturing", f"Loan{who} {verb} {when}: a refinance-or-sell decision is near"
    if status == "active":
        who = f" from {lender}" if lender else ""
        due = f", due {when}" if when else ""
        return status, "Active loan", f"Has a loan{who}{due}"
    y = c.years_owned()
    if not owner_rules.has_loan(p) and y is not None:
        return None, None, f"no loan on record, but owned only {y:.0f} years, so a loan may just be missing from the data"
    if not owner_rules.has_loan(p):
        return None, None, "no loan and no sale date on record, so we can't tell"
    return None, None, "loan details unclear"


def _read_absentee(c: _Ctx) -> Reading:
    a = c.owner.get("absentee") or owner_rules.absentee_status(c.prop)
    p = c.prop
    if a == "outofstate":
        return a, "Out of state", f"Owner's mail goes to {clean_str(p.get('mailing_state')) or 'another state'}"
    if a == "instate":
        city = (clean_str(p.get("mailing_city")) or "another city").title()
        return a, "In state", f"Owner's mail goes to {city}, not the property's city"
    if a == "local":
        return a, "Local", "Owner's mail goes to the same city"
    return None, None, "no owner mailing address on record"


def _read_vacancy(c: _Ctx) -> Reading:
    v = c.prop.get("usps_vacant")
    if v is None:
        return None, None, "no USPS vacancy data"
    if int(v) == 1:
        return "vacant", "Vacant", "USPS marks the address vacant"
    return "occupied", "Occupied", "USPS shows the address occupied"


def _read_condition(c: _Ctx) -> Reading:
    imp, total = _num(c.prop.get("improvement_value")), _num(c.prop.get("total_value"))
    if not imp or not total or total <= 0:
        return None, None, "no building and parcel values on record"
    ratio = imp / total
    if ratio < c.cfg.worn_improvement_ratio:
        return "worn", "Worn", (f"Buildings are only {ratio:.0%} of the parcel's value (under "
                                f"{c.cfg.worn_improvement_ratio:.0%}): likely older or worn")
    return "normal", "Normal", f"Buildings are {ratio:.0%} of the parcel's value"


def _read_distress(c: _Ctx) -> Reading:
    v = c.prop.get("preforeclosure")
    if v is None:
        return None, None, "no pre-foreclosure data"
    if int(v) == 1:
        return "preforeclosure", "Pre-foreclosure", "A pre-foreclosure filing is on record"
    return "none", "None", "No pre-foreclosure filing on record"


READERS: dict[str, Callable[[_Ctx], Reading]] = {
    "size": _read_size, "visual": _read_visual, "rent_fit": _read_rent, "drive": _read_drive,
    "owner_type": _read_owner_type, "years_owned": _read_years, "debt": _read_debt,
    "absentee": _read_absentee, "vacancy": _read_vacancy, "condition": _read_condition,
    "distress": _read_distress,
}


def _multiplier(key: str, bucket: str | None, weights: Mapping[str, Any], c: _Ctx) -> float | None:
    if bucket is None:
        return None
    m = multipliers(weights.get(key)).get(bucket)
    if m is None:
        return None
    if key == "size" and c.size.estimated and m > 0:
        w = _num((weights.get("size") or {}).get("estimate_weight"))
        m = m ** (c.cfg.estimate_weight if w is None else w)
    return round(m, 4)


def signals_for(prop: Mapping[str, Any], enrich: Mapping[str, Any] | None, asset_class: AssetClass,
                settings: Settings | None = None, *, weights: Mapping[str, Any] | None = None,
                today: date | None = None) -> list[dict]:
    """Every Blue Moon signal for one property: `[{key, label, value, bucket, multiplier, why}]`.

    `enrich` is `{"drive", "rent", "visual", "owner"}` rows (see buybox.evaluate). `weights` replaces the
    asset class's multipliers (learn.learned_weights); same shape as `asset_class.signals`.
    """
    s = settings or get_settings()
    ctx = _Ctx(prop=prop, enrich=enrich or {}, ac=asset_class, cfg=scoring_config(s),
               ocfg=owner_rules.owner_config(s), today=today or date.today(), size=size_info(prop, asset_class))
    w = weights if weights is not None else asset_class.signals
    out: list[dict] = []
    for key in active_signal_keys(asset_class):
        reader = READERS.get(key)
        if reader is None:
            continue
        bucket, value, fact = reader(ctx)
        m = _multiplier(key, bucket, w, ctx)
        if m is None:
            why = f"{NO_DATA} ({fact})." if bucket is None else f"{fact}. {NO_DATA} (no multiplier set for '{bucket}')."
            out.append({"key": key, "label": SIGNAL_LABELS[key], "value": value if bucket else None,
                        "bucket": bucket, "multiplier": None, "why": why})
            continue
        fact = fact[:1].upper() + fact[1:]
        out.append({"key": key, "label": SIGNAL_LABELS[key], "value": value, "bucket": bucket,
                    "multiplier": m, "why": f"{fact} - {effect_text(m)}."})
    return out


def probability(signals: list[Mapping[str, Any]], base_rate: float) -> float:
    """Base-rate odds times every known multiplier, back to a probability (0..1)."""
    b = min(max(float(base_rate), 1e-9), 1 - 1e-9)
    odds = b / (1 - b)
    for sig in signals:
        m = sig.get("multiplier")
        if m is None:
            continue
        odds *= max(float(m), 0.0)
    return odds / (1 + odds)


def coverage(signals: list[Mapping[str, Any]]) -> tuple[int, int]:
    """(signals with data, signals in total)."""
    return sum(1 for s in signals if s.get("multiplier") is not None), len(signals)
