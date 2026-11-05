"""The rent "back of the napkin": a rent estimate that gets sharper as evidence arrives.

Layers, from weakest to strongest:
1. Unit makeup: the unit count from the export (or one you type) split into bedroom types. Without better
   information the split is the default mix for the asset class (config/pricing.yaml); you can type the real one.
2. Rent per bedroom type: HUD's area rent for that bedroom count (table rent_fits), unless you typed a rent.
3. Total = sum of units x rent, times 12 = the yearly gross rent, shown as an estimate.
4. Better evidence (a rent roll, a listing, the owner) is saved as a rent record (table rent_records) with its
   source, date and basis. The best record is "selected" (or the one pinned by hand) and replaces the estimate;
   the others stay on file.

Unknown is not zero: a missing figure gives None and a reason, never $0. All money is whole cents and the
arithmetic lives in pricing.py. Everything here is rental income only (no parking, laundry or fees).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from . import history, pricing
from .db import dumps, loads, now_iso, one, rows
from .pricing import (
    STATE_OK,
    STATE_PROJECTION,
    STATE_REVIEW,
    Money,
    PricingConfig,
    RentInput,
    round_cents,
)
from .util import parse_date

BASES = ("actual", "scheduled", "estimated_full_occupancy", "estimated_market", "unconfirmed")
BASIS_LABELS = {
    "actual": "Actual rent collected", "scheduled": "Scheduled rent (what leases say)",
    "estimated_full_occupancy": "Estimated at full occupancy", "estimated_market": "Estimated at market rents",
    "unconfirmed": "Unconfirmed (imported, not checked)",
}
BASIS_RANK = {b: i for i, b in enumerate(BASES)}      # the better the evidence, the lower the number
SOURCES = {"rent_roll": "Rent roll", "listing": "Listing", "owner": "Owner conversation", "broker": "Broker",
           "manual": "Entered by hand", "import": "Imported"}
STATE_LABELS = {STATE_OK: "Ready", STATE_PROJECTION: "Projection", STATE_REVIEW: "Needs review"}
CONFIDENCE = {"estimate": "Estimated", "partly": "Partly known", "reported": "Reported"}
ESTIMATE_BASIS = "estimated_market"
MAX_NOTE = 2000
MAX_UNITS = 100_000
MAX_BEDROOMS = 9


def bedroom_label(br: int) -> str:
    return "Studio" if br == 0 else f"{br} BR"


# --- layer 1-3: the estimate --------------------------------------------------------------------------

def hud_rents(rent_fit: Mapping[str, Any] | None) -> dict[int, int]:
    """{bedrooms: monthly rent in cents} from the stored HUD rents."""
    out: dict[int, int] = {}
    for k, v in (loads((rent_fit or {}).get("rents_json"), {}) or {}).items():
        try:
            br, amount = int(k), pricing.to_cents(v)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            out[br] = amount
    return out


def default_shares(cfg: PricingConfig, asset_class: str) -> dict[int, Decimal] | None:
    shares = {int(br): Decimal(str(s)) for br, s in (cfg.default_unit_mix.get(asset_class) or {}).items() if s}
    return shares or None


def allocate(units: int, shares: Mapping[int, Decimal]) -> dict[int, int]:
    """Split a unit count by percentage shares into whole units that add up exactly (largest remainder)."""
    whole_total = sum(shares.values())
    exact = {br: Decimal(units) * s / whole_total for br, s in shares.items()}
    out = {br: int(x) for br, x in exact.items()}
    left = units - sum(out.values())
    for br in sorted(exact, key=lambda b: (exact[b] - out[b], -b), reverse=True)[:left]:
        out[br] += 1
    return out


def assumptions_of(conn: sqlite3.Connection, property_id: int) -> dict:
    r = one(conn, "SELECT * FROM rent_assumptions WHERE property_id = ?", (property_id,)) or {}
    return {"units": r.get("units"),
            "mix": {int(k): int(v) for k, v in (loads(r.get("mix_json"), {}) or {}).items()},
            "rents": {int(k): int(v) for k, v in (loads(r.get("rents_json"), {}) or {}).items()},
            "updated_by": r.get("updated_by"), "updated_at": r.get("updated_at")}


def estimate(units: int | None, assumed: Mapping[str, Any], hud: Mapping[int, int], shares: Mapping[int, Decimal] | None,
             currency: str = pricing.CURRENCY) -> dict:
    """The napkin: lines of (bedroom type, units, rent), the monthly and yearly totals, and what is missing.

    `assumed` holds only what a person typed (units, mix, rents). `units` is the count from the export."""
    typed_units, mix, typed_rents = assumed.get("units"), dict(assumed.get("mix") or {}), assumed.get("rents") or {}
    count = typed_units if typed_units is not None else units
    out: dict[str, Any] = {"available": False, "reason": None, "units": count,
                           "units_source": "typed" if typed_units is not None else ("export" if units else None),
                           "mix_source": None, "lines": [], "monthly_cents": None, "annual_cents": None,
                           "warnings": [], "user_inputs": bool(typed_units is not None or mix or typed_rents)}
    if mix:
        counts, out["mix_source"] = {br: n for br, n in mix.items() if n > 0}, "typed"
        if count is not None and sum(counts.values()) != count:
            out["warnings"].append(f"The typed mix adds up to {sum(counts.values())} units, but the unit count is "
                                   f"{count}. The mix is used.")
        count = sum(counts.values())
        out["units"] = count
    elif count and shares:
        counts, out["mix_source"] = allocate(count, shares), "default"
    else:
        out["reason"] = ("The unit count is not known. Type it in." if not count else
                         "There is no assumed bedroom mix for this kind of property. Type the real mix in.")
        return out
    if not counts:
        out["reason"] = "The typed mix has no units in it."
        return out
    missing = []
    for br in sorted(counts):
        rent = typed_rents.get(br) or hud.get(br)
        line = {"bedrooms": br, "label": bedroom_label(br), "units": counts[br],
                "share_pct": float(round(Decimal(counts[br]) * 100 / count, 1)) if count else None,
                "rent_cents": rent, "rent_source": "typed" if typed_rents.get(br) else ("hud" if rent else None),
                "monthly_cents": None if rent is None else Money(rent, currency).times(counts[br]).cents}
        if rent is None:
            missing.append(bedroom_label(br))
        out["lines"].append(line)
    if missing:
        out["reason"] = f"No rent for {', '.join(missing)}. HUD has none for this area: type one in."
        return out
    monthly = pricing.total([Money(line["monthly_cents"], currency) for line in out["lines"]], currency)
    out.update(available=True, monthly_cents=monthly.cents, annual_cents=monthly.times(12).cents)
    return out


# --- layer 4: rent records ----------------------------------------------------------------------------

def _ord(r: Mapping[str, Any]) -> int:
    d = parse_date(r.get("evidence_date"))
    return d.toordinal() if d else 0


def select(records: Iterable[Mapping[str, Any]]) -> dict | None:
    """The record the price uses: the one pinned by hand, else the best basis (actual first), a real year
    before a one-month projection, then the newest evidence. A record that needs review is never used."""
    usable = [dict(r) for r in records if r["state"] in (STATE_OK, STATE_PROJECTION) and r["annual_cents"] is not None]
    pinned = [r for r in usable if r.get("pinned")]
    if pinned:
        return max(pinned, key=lambda r: r["id"])
    if not usable:
        return None
    return min(usable, key=lambda r: (BASIS_RANK[r["basis"]], r["state"] == STATE_PROJECTION, -_ord(r), -r["id"]))


def records_of(conn: sqlite3.Connection, property_id: int) -> list[dict]:
    return rows(conn, "SELECT * FROM rent_records WHERE property_id = ? ORDER BY id", (property_id,))


def _decimal(value: Any, what: str) -> Decimal | None:
    return None if value is None or value == "" else pricing.to_decimal(value, what)


def add_record(conn: sqlite3.Connection, property_id: int, *, source: str, basis: str, actor: str,
               total: Any = None, total_period: str | None = None, rate: Any = None, rate_period: str | None = None,
               quantity: Any = None, quantity_kind: str | None = None, area_type: str | None = None,
               authoritative: str | None = None, evidence_date: str | None = None, note: str | None = None,
               today: date | None = None) -> dict:
    """Save one piece of rent evidence. The yearly figure is worked out here (pricing.normalize); a figure that
    can't be turned into a year honestly is saved as 'needs review' with the reason, and is never selected."""
    if source not in SOURCES:
        raise ValueError(f"The source must be one of: {', '.join(SOURCES)}.")
    if basis not in BASES:
        raise ValueError(f"The basis must be one of: {', '.join(BASES)}.")
    if area_type not in (None, *history.AREA_TYPES):
        raise ValueError(f"The area type must be one of: {', '.join(history.AREA_TYPES)}.")
    when = None
    if evidence_date:
        d = parse_date(evidence_date)
        if d is None or d > (today or date.today()):
            raise ValueError("The date the figure is from must be a real date, not in the future.")
        when = d.isoformat()
    text = " ".join(str(note).split()) if note else None
    if text and len(text) > MAX_NOTE:
        raise ValueError(f"The note is too long (most {MAX_NOTE} letters).")
    cents = None if total in (None, "") else pricing.to_cents(total, "total")
    inp = RentInput(total=None if cents is None else Decimal(cents) / 100, total_period=total_period or None,
                    rate=_decimal(rate, "rate"), rate_period=rate_period or None,
                    quantity=_decimal(quantity, "count or area"), quantity_kind=quantity_kind or None,
                    area_type=area_type, authoritative=authoritative or None)
    if inp.total_period not in (None, *pricing.TOTAL_PERIODS) or inp.rate_period not in (None, *pricing.RATE_PERIODS) \
            or inp.quantity_kind not in (None, *pricing.QUANTITY_KINDS) \
            or inp.authoritative not in (None, *pricing.AUTHORITATIVE):
        raise ValueError("One of the period, count or 'trust' choices is not one the app knows.")
    result = pricing.normalize(inp)
    reason = result.reason
    if result.annual_cents == 0:
        reason = " ".join(filter(None, ["Confirmed zero rent: kept as zero, not treated as unknown.", reason]))
    with conn:
        cur = conn.execute(
            "INSERT INTO rent_records(property_id, source, basis, total_cents, total_period, rate_text, rate_period, "
            "quantity_text, quantity_kind, area_type, authoritative, state, annual_cents, formula, reason, "
            "discrepancy_cents, evidence_date, note, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (property_id, source, basis, cents, inp.total_period, None if inp.rate is None else str(inp.rate),
             inp.rate_period, None if inp.quantity is None else str(inp.quantity), inp.quantity_kind, area_type,
             inp.authoritative, result.state, result.annual_cents, result.formula, reason,
             result.discrepancy_cents, when, text, actor, now_iso()))
    return one(conn, "SELECT * FROM rent_records WHERE id = ?", (cur.lastrowid,)) or {}


def pin_record(conn: sqlite3.Connection, property_id: int, record_id: int, pinned: bool) -> None:
    """Use this record for the price (or go back to the app's own choice). Only one record is pinned."""
    rec = one(conn, "SELECT * FROM rent_records WHERE id = ? AND property_id = ?", (record_id, property_id))
    if rec is None:
        raise KeyError(f"Rent record {record_id} is not on this property.")
    if pinned and rec["state"] == STATE_REVIEW:
        raise ValueError("That figure needs review first, so it can't be used for the price.")
    with conn:
        conn.execute("UPDATE rent_records SET pinned = 0 WHERE property_id = ?", (property_id,))
        if pinned:
            conn.execute("UPDATE rent_records SET pinned = 1 WHERE id = ?", (record_id,))


def save_assumptions(conn: sqlite3.Connection, property_id: int, changes: Mapping[str, Any], actor: str) -> dict:
    """Change the napkin's assumptions. Only the keys in `changes` are touched; null clears one.
    units: a count; mix: {bedrooms: units}; rents: {bedrooms: monthly rent in dollars}."""
    cur = assumptions_of(conn, property_id)
    units, mix, rents = cur["units"], cur["mix"], cur["rents"]
    if "units" in changes:
        units = changes["units"]
        if units is not None and not (isinstance(units, int) and not isinstance(units, bool) and 1 <= units <= MAX_UNITS):
            raise ValueError(f"The unit count must be a whole number from 1 to {MAX_UNITS:,}.")
    if "mix" in changes:
        mix = {}
        for k, v in (changes["mix"] or {}).items():
            br = _bedrooms(k)
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MAX_UNITS:
                raise ValueError("Each bedroom count in the mix must be a whole number of units.")
            if v:
                mix[br] = v
    if "rents" in changes:
        rents = {}
        for k, v in (changes["rents"] or {}).items():
            cents = pricing.to_cents(v, "rent")
            if cents <= 0:
                raise ValueError("A rent for a bedroom type must be above zero (clear it to use the HUD rent).")
            rents[_bedrooms(k)] = cents
    if units is None and not mix and not rents:
        with conn:
            conn.execute("DELETE FROM rent_assumptions WHERE property_id = ?", (property_id,))
    else:
        with conn:
            conn.execute(
                "INSERT INTO rent_assumptions(property_id, units, mix_json, rents_json, updated_by, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(property_id) DO UPDATE SET units=excluded.units, "
                "mix_json=excluded.mix_json, rents_json=excluded.rents_json, updated_by=excluded.updated_by, "
                "updated_at=excluded.updated_at",
                (property_id, units, dumps({str(k): v for k, v in mix.items()}) if mix else None,
                 dumps({str(k): v for k, v in rents.items()}) if rents else None, actor, now_iso()))
    return assumptions_of(conn, property_id)


def _bedrooms(key: Any) -> int:
    try:
        br = int(key)
    except (TypeError, ValueError):
        raise ValueError("A bedroom count must be a number from 0 (studio) to 9.") from None
    if not 0 <= br <= MAX_BEDROOMS:
        raise ValueError("A bedroom count must be a number from 0 (studio) to 9.")
    return br


# --- putting the layers together ----------------------------------------------------------------------

def resolve(prop: Mapping[str, Any], records: list[dict], assumed: Mapping[str, Any], hud: Mapping[int, int],
            cfg: PricingConfig, asset_class: str) -> dict:
    """The one yearly rent the price uses, and where it came from.

    Returns {annual_cents, monthly_cents, basis, source (record | estimate | None), record, estimate, confidence,
    needs_review (records that can't be used)}. annual_cents is None when nothing gives a rent."""
    est = estimate(prop.get("units"), assumed, hud, default_shares(cfg, asset_class))
    chosen = select(records)
    review = sum(1 for r in records if r["state"] == STATE_REVIEW)
    if chosen:
        word = "reported" if chosen["basis"] in ("actual", "scheduled") else "partly"
        annual, basis, source = chosen["annual_cents"], chosen["basis"], "record"
    elif est["available"]:
        word = "partly" if est["user_inputs"] else "estimate"
        annual, basis, source = est["annual_cents"], ESTIMATE_BASIS, "estimate"
    else:
        word, annual, basis, source = "estimate", None, None, None
    return {"annual_cents": annual,
            "monthly_cents": None if annual is None else round_cents(Decimal(annual) / 100 / 12),
            "basis": basis, "source": source, "record": chosen, "estimate": est,
            "confidence": CONFIDENCE[word], "needs_review": review}


def _record_view(r: Mapping[str, Any], selected_id: int | None) -> dict:
    out = dict(r)
    out.update(selected=r["id"] == selected_id, pinned=bool(r["pinned"]), basis_label=BASIS_LABELS[r["basis"]],
               source_label=SOURCES.get(r["source"], r["source"]), state_label=STATE_LABELS[r["state"]],
               monthly_cents=None if r["annual_cents"] is None else round_cents(Decimal(r["annual_cents"]) / 100 / 12))
    return out


def prior_of(prop: Mapping[str, Any], reference_cents: int | None, bundled: bool, cfg: PricingConfig) -> dict:
    sale = prop.get("sale_price")
    cmp = pricing.prior_purchase(None if sale is None else pricing.to_cents(sale), reference_cents,
                                 nominal_cents=cfg.nominal_cents, bundled=bundled)
    return {"state": cmp.state, "label": cmp.label, "reason": cmp.reason, "sale_cents": cmp.sale_cents,
            "reference_cents": cmp.reference_cents, "share_pct": None if cmp.share_pct is None else float(cmp.share_pct),
            "halfway_cents": cmp.halfway_cents, "gap_cents": cmp.gap_cents, "sale_date": prop.get("sale_date"),
            "bundled": bundled}


def _scenario_view(annual: int | None, cfg: PricingConfig) -> list[dict]:
    return [{"multiple": pricing.multiple_text(s.multiple), "cents": s.cents, "role": s.role}
            for s in pricing.scenarios(annual, cfg)]


def block(conn: sqlite3.Connection, prop: Mapping[str, Any], asset_class: str, cfg: PricingConfig,
          rent_fit: Mapping[str, Any] | None) -> dict:
    """Everything the drawer's 'Rent and Blue Moon price' section shows for one property."""
    pid = int(prop["id"])
    records, assumed, hud = records_of(conn, pid), assumptions_of(conn, pid), hud_rents(rent_fit)
    res = resolve(prop, records, assumed, hud, cfg, asset_class)
    annual = res["annual_cents"]
    scen = _scenario_view(annual, cfg)
    reference = scen[0]["cents"] if scen else None
    bundled = bool(history.history_row(conn, pid).get("sale_bundled"))
    rentable = prop.get("building_area_type") == "rentable" and (prop.get("building_sqft") or 0) > 0
    per_sqft = None
    if rentable and scen:
        per_sqft = [{"multiple": s["multiple"], "cents": pricing.price_per_sqft_cents(s["cents"], prop["building_sqft"])}
                    for s in scen]
    return {
        "rent": {k: res[k] for k in ("annual_cents", "monthly_cents", "basis", "source", "confidence", "needs_review")}
                | {"basis_label": BASIS_LABELS.get(res["basis"] or "", None),
                   "label": "Estimated" if res["source"] == "estimate" else None,
                   "record_id": (res["record"] or {}).get("id"),
                   "zero": annual == 0},
        "scenarios": scen, "reference_cents": reference, "per_sqft": per_sqft,
        "rentable_sqft": prop.get("building_sqft") if rentable else None,
        "estimate": res["estimate"], "assumptions": assumed,
        "hud": {"rents": hud, "area_name": (rent_fit or {}).get("area_name"),
                "fiscal_year": (rent_fit or {}).get("fiscal_year"), "source": (rent_fit or {}).get("source")},
        "records": [_record_view(r, (res["record"] or {}).get("id")) for r in records],
        "prior": prior_of(prop, reference, bundled, cfg),
        "defaults": {"mix": {str(br): float(s) for br, s in (default_shares(cfg, asset_class) or {}).items()}},
        "asking_price": prop.get("asking_price"),
    }


def snapshot(conn: sqlite3.Connection, prop: Mapping[str, Any], asset_class: str, cfg: PricingConfig,
             rent_fit: Mapping[str, Any] | None) -> dict:
    """What an offer is based on, frozen: the selected rent record (or the estimate and its assumptions), its
    basis, the yearly rent and the 4x / 5x / 6x amounts, plus a hash that changes when any input changes.
    Offers keep this snapshot so a later rent change never rewrites an offer that was already made."""
    b = block(conn, prop, asset_class, cfg, rent_fit)
    rec = next((r for r in b["records"] if r["selected"]), None)
    snap = {
        "source": b["rent"]["source"], "basis": b["rent"]["basis"], "annual_cents": b["rent"]["annual_cents"],
        "record": None if rec is None else {k: rec[k] for k in (
            "id", "source", "basis", "state", "annual_cents", "total_cents", "total_period", "rate_text", "rate_period",
            "quantity_text", "quantity_kind", "area_type", "authoritative", "evidence_date")},
        "estimate": None if b["rent"]["source"] != "estimate" else {
            k: b["estimate"][k] for k in ("units", "mix_source", "lines", "monthly_cents", "annual_cents")},
        "scenarios": b["scenarios"],
    }
    snap["hash"] = hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()
    return snap


# --- every property of a market at once (for the Deals filter and the exports) ---------------------------

def resolve_market(conn: sqlite3.Connection, market_id: int, asset_class: str, cfg: PricingConfig) -> dict[int, dict]:
    """{property id: {rent: resolve(...), prior: prior_of(...)}} for the whole market, with a few queries."""
    props = rows(conn, "SELECT id, units, sale_price, sale_date FROM properties WHERE market_id = ?", (market_id,))
    ids = "SELECT id FROM properties WHERE market_id = ?"
    by_prop: dict[int, list[dict]] = {}
    for r in rows(conn, f"SELECT * FROM rent_records WHERE property_id IN ({ids}) ORDER BY id", (market_id,)):
        by_prop.setdefault(r["property_id"], []).append(r)
    fits = {r["property_id"]: r for r in rows(
        conn, f"SELECT property_id, rents_json FROM rent_fits WHERE property_id IN ({ids})", (market_id,))}
    assumed: dict[int, dict] = {}
    for r in rows(conn, f"SELECT property_id FROM rent_assumptions WHERE property_id IN ({ids})", (market_id,)):
        assumed[r["property_id"]] = assumptions_of(conn, r["property_id"])
    bundled = {r["property_id"] for r in rows(
        conn, f"SELECT property_id FROM property_history WHERE sale_bundled = 1 AND property_id IN ({ids})", (market_id,))}
    none = {"units": None, "mix": {}, "rents": {}}
    out: dict[int, dict] = {}
    for p in props:
        pid = p["id"]
        res = resolve(p, by_prop.get(pid, []), assumed.get(pid, none), hud_rents(fits.get(pid)), cfg, asset_class)
        reference = None
        if res["annual_cents"] is not None:
            reference = pricing.scenarios(res["annual_cents"], cfg)[0].cents
        out[pid] = {"rent": res, "reference_cents": reference, "prior": prior_of(p, reference, pid in bundled, cfg)}
    return out


def prior_group_ids(resolved: Mapping[int, Mapping[str, Any]]) -> dict[str, list[int]]:
    """The Deals filter groups: below half, at or above half, unknown (unavailable stays visible)."""
    groups: dict[str, list[int]] = {"below": [], "at_above": [], "unknown": []}
    for pid, r in resolved.items():
        state = r["prior"]["state"]
        groups["below" if state == pricing.BELOW else "unknown" if state == pricing.UNAVAILABLE else "at_above"].append(pid)
    return groups
