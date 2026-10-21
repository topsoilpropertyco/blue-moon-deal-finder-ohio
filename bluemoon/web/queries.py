"""Read models for the dashboard: fast SQL over the master list and every enrichment layer.

The deals list is one query with LEFT JOINs over properties, scores, drive_times, rent_fits,
visual_checks, owners and drill_state. Filtering, sorting and paging happen in SQL, and only the rows on
the page get the DRILL summary (next step and channel badges) from `drill.engine.summaries`.

Thresholds used by the list views live in config.yaml under `web:` (defaults in WEB_DEFAULTS):
moon_score (the "Blue Moon 10%+" view), funnel_score (the funnel's last step), long_owned_years and
mom_owner_types.

Buy-box decisions made by hand: rows carry rule_reasons (what the rules said), by_hand ('added' | 'aside') and
aside_buckets (their set-aside groups). set_aside() is the Why? panel: each reason with its groups, counted from
the groups stored with the scores; list_properties(bucket=...) lists one group (the Review list).
"""
from __future__ import annotations

import copy
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..config import AssetClass, Settings, get_settings, has_secret
from ..db import loads, market_by_slug, one, pending_rescore, rows, scalar, this_month
from ..drill import engine
from ..drill import links as drill_links
from ..enrich.owners import OWNER_TYPE_LABELS as OWNER_TYPES
from ..imagery import explain_prior, images_for, map_links, queue_counts
from ..scoring import buckets as B
from ..scoring import overrides
from ..scoring.buybox import (
    EXCLUSION_LABELS,
    MANUAL_REASON,
    METRIC_UNITS,
    SET_ASIDE_REASONS,
    SIZE_BASIS_LABELS,
    ScoringConfig,
    apply_overrides,
    evaluate,
    scoring_config,
    visual_color,
)
from ..scoring.learn import OUTCOME_LABELS

log = logging.getLogger(__name__)

WEB_DEFAULTS: dict[str, Any] = {
    "moon_score": 0.10,              # "Blue Moon 10%+" view and the summary count
    "funnel_score": 0.05,            # last step of the "find" funnel
    "long_owned_years": 15,          # "Owned a long time" view
    "mom_owner_types": ["individual", "trust", "estate", "small_llc"],
    "page_size": 100,
    "max_page_size": 500,
    "upload_max_mb": 50,
    "rescore_delay_seconds": 3,      # re-score this long after the last photo review or outcome
    "drive_close_call_minutes": 15,  # set aside only for an estimated drive this close to the limit = "close call"
}

KEY_NAMES = {"google": "GOOGLE_MAPS_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "hud": "HUD_API_TOKEN",
             "census": "CENSUS_API_KEY"}

OWNER_TYPE_LABELS: dict[str, str] = {**OWNER_TYPES, "not_checked": "Not checked yet"}

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|passwd)", re.IGNORECASE)


def web_config(settings: Settings | None = None) -> dict[str, Any]:
    """`web:` from config.yaml merged over WEB_DEFAULTS."""
    s = settings or get_settings()
    return {**WEB_DEFAULTS, **{k: v for k, v in (s.section("web") or {}).items() if v is not None}}


def key_status() -> dict[str, bool]:
    """Which keys are set in .env (booleans only, never the values)."""
    return {name: has_secret(env) for name, env in KEY_NAMES.items()}


# --- named-parameter queries (bluemoon.db helpers take positional params only) --------------------

def _all(conn: sqlite3.Connection, sql: str, params: Mapping[str, Any]) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, dict(params)).fetchall()]


def _first(conn: sqlite3.Connection, sql: str, params: Mapping[str, Any]) -> dict | None:
    r = conn.execute(sql, dict(params)).fetchone()
    return dict(r) if r else None


# --- market context -----------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketCtx:
    market: dict
    ac: AssetClass
    cfg: ScoringConfig
    web: dict
    settings: Settings

    @property
    def id(self) -> int:
        return int(self.market["id"])

    @property
    def slug(self) -> str:
        return str(self.market["slug"])


def market_ctx(conn: sqlite3.Connection, slug: str, settings: Settings | None = None) -> MarketCtx:
    """The market row plus its asset class and thresholds. KeyError for an unknown market."""
    s = settings or get_settings()
    market = market_by_slug(conn, slug)
    return MarketCtx(market=market, ac=s.asset_class(market["asset_class"]), cfg=scoring_config(s),
                     web=web_config(s), settings=s)


def target_word(ac: AssetClass) -> str:
    """What a 'good' photo verdict is called for this asset class."""
    named = ac.visual_check.get("target_name")
    if named:
        return str(named)
    return "Walk-up" if "EXTERIOR_CORRIDOR" in (ac.visual_check.get("target_labels") or []) else "Photo target"


def asset_class_info(ac: AssetClass) -> dict:
    vc = ac.visual_check or {}
    rent = ac.rent or {}
    return {
        "key": ac.key, "label": ac.label,
        "size_metric": ac.size.metric, "size_unit": METRIC_UNITS.get(ac.size.metric, ac.size.metric),
        "size_ideal": list(ac.size.ideal), "size_ok": list(ac.size.ok),
        "year_built_min": ac.year_built_min,
        "visual_enabled": ac.visual_enabled, "visual_question": vc.get("question"),
        "labels": list(vc.get("labels") or []), "target_labels": list(vc.get("target_labels") or []),
        "label_help": dict(vc.get("label_help") or {}), "target_word": target_word(ac),
        "rent_enabled": ac.rent_enabled, "rent_scale": dict(rent.get("scale") or {}),
    }


# --- the joined row -----------------------------------------------------------------------------

BASE_FROM = """
FROM properties p
LEFT JOIN scores s ON s.property_id = p.id
LEFT JOIN drive_times dt ON dt.property_id = p.id
LEFT JOIN rent_fits rf ON rf.property_id = p.id
LEFT JOIN visual_checks v ON v.property_id = p.id
LEFT JOIN owners o ON o.property_id = p.id
LEFT JOIN drill_state ds ON ds.property_id = p.id
WHERE p.market_id = :market_id
"""

ROW_COLUMNS = """
p.id, p.name, p.address, p.unit, p.city, p.state, p.zip, p.county, p.apn, p.lat, p.lng,
p.units, p.building_sqft, p.lot_acres, p.year_built, p.owner_name, p.crexi_url,
s.in_buybox, s.exclusion_reasons, s.size_value, s.size_basis, s.size_color, s.rent_color,
s.score, s.coverage_have, s.coverage_total, s.rank,
COALESCE(s.rule_reasons, s.exclusion_reasons) AS rule_reasons, s.aside_buckets,
dt.minutes AS drive_minutes, dt.method AS drive_method,
rf.fit AS rent_fit,
v.property_id AS v_id, v.status AS v_status, v.ai_label, v.final_p, v.reviewed_label, v.checked_at AS v_checked_at,
o.owner_type, o.years_owned,
COALESCE(ds.level, 1) AS level, COALESCE(ds.status, 'active') AS drill_status,
ds.best_guess_person, ds.confirmed_person
"""

IN_BOX = "COALESCE(s.in_buybox, 1) = 1"     # not scored yet = not excluded (missing data never excludes)
LEVEL = "COALESCE(ds.level, 1)"
STATUS = "COALESCE(ds.status, 'active')"
REVIEW_SQL = ("(:visual_on = 1 AND v.property_id IS NOT NULL AND v.reviewed_label IS NULL AND ("
              "(v.final_p IS NULL AND (v.checked_at IS NOT NULL OR v.status = 'no_imagery')) "
              "OR (v.final_p > :red_max AND v.final_p < :green_min)))")

VIEWS: dict[str, str] = {
    "all": "1 = 1",
    "below": f"{LEVEL} < 3 AND {STATUS} = 'active'",
    "excluded": "s.in_buybox = 0",
    "review": REVIEW_SQL,
    "confirmed": f"{LEVEL} = 3 AND {STATUS} != 'dnc'",
    "level1": f"{LEVEL} = 1 AND {STATUS} != 'dnc'",
    "level2": f"{LEVEL} = 2 AND {STATUS} != 'dnc'",
    "dnc": f"{STATUS} = 'dnc'",
    "sweet": "s.size_color = 'green'",
    "walk": "v.final_p >= :green_min",
    "mom": "o.owner_type IN (:mom0, :mom1, :mom2, :mom3, :mom4, :mom5, :mom6, :mom7, :mom8, :mom9)",
    "long": "o.years_owned >= :long_years",
    "moon": "s.score >= :moon",
    "everything": "1 = 1",
}
UNSCOPED_VIEWS = frozenset({"excluded", "everything"})   # every other view is limited to the buy box

SORTS: dict[str, tuple[str, str]] = {
    "rank": ("s.rank", "asc"),
    "score": ("s.score", "desc"),
    "drive": ("dt.minutes", "asc"),
    "units": ("COALESCE(s.size_value, p.units)", "desc"),
    "level": (LEVEL, "desc"),
    "years": ("o.years_owned", "desc"),
    "address": ("p.address", "asc"),
}

SEARCH_FIELDS = ("p.address", "p.city", "p.zip", "p.county", "p.apn", "p.name", "p.owner_name",
                 "ds.best_guess_person", "ds.confirmed_person")
APN_COMPACT = "REPLACE(REPLACE(REPLACE(REPLACE(p.apn, '-', ''), '.', ''), ' ', ''), '/', '')"
MAX_SEARCH_TERMS = 8


def view_labels(ac: AssetClass, web: Mapping[str, Any]) -> dict[str, str]:
    """Chip labels for the deals list, in display order."""
    return {
        "all": "In the buy box", "below": "Not confirmed yet", "review": "Needs a photo look",
        "sweet": "Sweet-spot size", "walk": target_word(ac), "mom": "Mom-and-pop owners",
        "long": f"Owned {web['long_owned_years']:g}+ years", "moon": f"Blue Moon {web['moon_score']:.0%}+",
        "level1": "No owner data", "level2": "Possible owner", "confirmed": "Confirmed owner",
        "dnc": "Do not contact", "excluded": "Set aside", "everything": "Everything",
    }


def _view_where(view: str, include_excluded: bool) -> str:
    pred = VIEWS[view]
    if view in UNSCOPED_VIEWS or include_excluded:
        return f"({pred})"
    return f"({IN_BOX} AND ({pred}))"


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search(q: str | None) -> tuple[str, dict]:
    """Every word must match one of the address/city/zip/county/APN/owner/person fields."""
    terms = [t for t in re.split(r"\s+", (q or "").strip()) if t][:MAX_SEARCH_TERMS]
    clauses: list[str] = []
    params: dict[str, str] = {}
    for i, term in enumerate(terms):
        params[f"q{i}"] = f"%{_escape_like(term)}%"
        ors = [f"{f} LIKE :q{i} ESCAPE '\\'" for f in SEARCH_FIELDS]
        compact = re.sub(r"[^0-9A-Za-z]", "", term)
        if compact and any(ch.isdigit() for ch in compact):   # parcel numbers typed with or without dashes
            params[f"qc{i}"] = f"%{compact}%"
            ors.append(f"{APN_COMPACT} LIKE :qc{i}")
        clauses.append("(" + " OR ".join(ors) + ")")
    return (" AND ".join(clauses) if clauses else "1 = 1"), params


def _order_by(sort: str, direction: str | None) -> tuple[str, str]:
    expr, natural = SORTS[sort]
    d = (direction or natural).lower()
    if d not in ("asc", "desc"):
        raise ValueError(f"'{direction}' is not a sort direction. Use asc or desc.")
    collate = " COLLATE NOCASE" if sort == "address" else ""
    return (f"({expr}) IS NULL, {expr}{collate} {d.upper()}, s.score IS NULL, s.score DESC, p.id ASC", d)


MAX_MOM_TYPES = 10   # placeholders in the "mom" view


def _base_params(mc: MarketCtx) -> dict[str, Any]:
    mom = [str(t) for t in (mc.web["mom_owner_types"] or [])][:MAX_MOM_TYPES]
    mom += [None] * (MAX_MOM_TYPES - len(mom))      # unused slots match nothing
    return {"market_id": mc.id, "green_min": mc.cfg.visual_green_min, "red_max": mc.cfg.visual_red_max,
            "visual_on": 1 if mc.ac.visual_enabled else 0, "long_years": float(mc.web["long_owned_years"]),
            "moon": float(mc.web["moon_score"]), **{f"mom{i}": t for i, t in enumerate(mom)}}


def _clamp_page(limit: Any, offset: Any, web: Mapping[str, Any]) -> tuple[int, int]:
    lim = int(limit) if limit is not None else int(web["page_size"])
    return max(1, min(lim, int(web["max_page_size"]))), max(0, int(offset or 0))


def needs_review(r: Mapping[str, Any], mc: MarketCtx) -> bool:
    """Same rule as the 'review' view: a human should look at the photos."""
    if not mc.ac.visual_enabled or r.get("v_id") is None or r.get("reviewed_label"):
        return False
    p = r.get("final_p")
    if p is None:
        return r.get("v_checked_at") is not None or r.get("v_status") == "no_imagery"
    return mc.cfg.visual_red_max < p < mc.cfg.visual_green_min


def _visual(r: Mapping[str, Any], mc: MarketCtx) -> dict:
    if not mc.ac.visual_enabled:
        return {"label": None, "p": None, "color": None, "needs_review": False, "reviewed": False, "status": None}
    row = {"final_p": r.get("final_p"), "status": r.get("v_status")} if r.get("v_id") is not None else None
    return {"label": r.get("reviewed_label") or r.get("ai_label"), "p": r.get("final_p"),
            "color": visual_color(row, mc.ac, mc.cfg), "needs_review": needs_review(r, mc),
            "reviewed": bool(r.get("reviewed_label")), "status": r.get("v_status")}


def _round(v: Any, digits: int = 1) -> float | None:
    return None if v is None else round(float(v), digits)


def by_hand(in_buybox: Any, reasons: list[str], rule_reasons: list[str]) -> str | None:
    """'added' (in the buy box although a rule said no), 'aside' (set aside by hand) or None."""
    if MANUAL_REASON in reasons:
        return "aside"
    if in_buybox and rule_reasons:
        return "added"
    return None


def property_row(r: Mapping[str, Any], drill: Mapping[str, Any] | None, mc: MarketCtx) -> dict:
    """One PropertyRow (ARCHITECTURE.md) from a joined row plus its DRILL summary."""
    d = drill or {}
    in_box = r.get("in_buybox")
    reasons = loads(r.get("exclusion_reasons"), []) or []
    rule_reasons = loads(r.get("rule_reasons"), None)
    rule_reasons = reasons if rule_reasons is None else (rule_reasons or [])
    return {
        "id": r["id"], "name": r.get("name"), "address": r.get("address"), "unit": r.get("unit"),
        "city": r.get("city"), "state": r.get("state"), "zip": r.get("zip"), "county": r.get("county"),
        "apn": r.get("apn"), "lat": r.get("lat"), "lng": r.get("lng"),
        "units": r.get("units"), "building_sqft": r.get("building_sqft"), "lot_acres": r.get("lot_acres"),
        "year_built": r.get("year_built"),
        "size_value": r.get("size_value"), "size_basis": r.get("size_basis"), "size_color": r.get("size_color"),
        "drive_minutes": _round(r.get("drive_minutes")), "drive_method": r.get("drive_method"),
        "visual": _visual(r, mc),
        "rent_fit": r.get("rent_fit"), "rent_color": r.get("rent_color"),
        "owner_name": r.get("owner_name"), "owner_type": r.get("owner_type"),
        "years_owned": _round(r.get("years_owned")),
        "score": r.get("score"), "coverage_have": r.get("coverage_have"), "coverage_total": r.get("coverage_total"),
        "rank": r.get("rank"), "in_buybox": None if in_box is None else bool(in_box),
        "exclusion_reasons": reasons, "rule_reasons": rule_reasons,
        "by_hand": by_hand(in_box, reasons, rule_reasons) if in_box is not None else None,
        "aside_buckets": loads(r.get("aside_buckets"), []) or [],
        "level": d.get("level", r.get("level")), "drill_status": d.get("status", r.get("drill_status")),
        "next_step_text": d.get("next_step_text"), "next_step_kind": (d.get("next_step") or {}).get("kind"),
        "channels": d.get("channels") or {c: "none" for c in engine.CHANNELS},
        "best_guess_person": d.get("best_guess_person", r.get("best_guess_person")),
        "crexi_url": r.get("crexi_url"),
    }


# --- the deals list -----------------------------------------------------------------------------

def list_properties(conn: sqlite3.Connection, slug: str, *, view: str | None = "all", q: str | None = None,
                    sort: str | None = "rank", direction: str | None = None, limit: Any = None, offset: Any = 0,
                    include_excluded: bool = False, bucket: str | None = None,
                    settings: Settings | None = None) -> dict:
    """{total, items:[PropertyRow], counts:{view: n}, view, sort, dir, limit, offset, q, bucket, review}.

    bucket: one set-aside group ('drive_time:76-90'). The list is then that group, in or out of the buy box
    (like the 'everything' view), so rows don't disappear while you decide; `review` describes the group."""
    mc = market_ctx(conn, slug, settings)
    group = (bucket or "").strip() or None
    group_params = B.member_params(group) if group else {}     # ValueError (400) for a key it doesn't know
    view = (view or "all").strip().lower()
    if view not in VIEWS:
        raise ValueError(f"'{view}' is not a list view. Use one of: {', '.join(VIEWS)}.")
    sort = (sort or "rank").strip().lower()
    if sort not in SORTS:
        raise ValueError(f"'{sort}' is not a sort. Use one of: {', '.join(SORTS)}.")
    order, direction = _order_by(sort, direction)
    limit, offset = _clamp_page(limit, offset, mc.web)
    search_sql, search_params = _search(q)
    params = {**_base_params(mc), **search_params, **group_params, "limit": limit, "offset": offset}
    scope = f"({B.MEMBER_SQL})" if group else _view_where(view, include_excluded)
    where = f"{BASE_FROM} AND {scope} AND {search_sql}"
    found = _all(conn, f"SELECT {ROW_COLUMNS}, COUNT(*) OVER () AS _total {where} "
                       f"ORDER BY {order} LIMIT :limit OFFSET :offset", params)
    total = int(found[0]["_total"]) if found else int(
        (_first(conn, f"SELECT COUNT(*) AS n {where}", params) or {}).get("n") or 0)
    drill = engine.summaries(conn, slug, property_ids=[r["id"] for r in found]) if found else {}
    return {
        "total": total, "items": [property_row(r, drill.get(r["id"]), mc) for r in found],
        "counts": view_counts(conn, mc, search_sql, params, include_excluded),
        "view": view, "sort": sort, "dir": direction, "limit": limit, "offset": offset, "q": q or "",
        "include_excluded": bool(include_excluded), "bucket": group,
        "review": review_block(conn, mc, group) if group else None,
    }


def view_counts(conn: sqlite3.Connection, mc: MarketCtx, search_sql: str, params: Mapping[str, Any],
                include_excluded: bool) -> dict[str, int]:
    """How many rows each view would show (with the same search), in one pass."""
    parts = [f"COALESCE(SUM(CASE WHEN {_view_where(v, include_excluded)} THEN 1 ELSE 0 END), 0) AS \"{v}\""
             for v in VIEWS]
    r = _first(conn, f"SELECT {', '.join(parts)} {BASE_FROM} AND {search_sql}", params) or {}
    return {v: int(r.get(v) or 0) for v in VIEWS}


# --- one property -------------------------------------------------------------------------------

def _one_by_property(conn: sqlite3.Connection, table: str, pid: int) -> dict | None:
    return one(conn, f"SELECT * FROM {table} WHERE property_id = ?", (pid,))


def property_detail(conn: sqlite3.Connection, slug: str, pid: int, settings: Settings | None = None) -> dict:
    """PropertyDetail: the row plus every layer, the score explanation, photos, owner, DRILL and links."""
    mc = market_ctx(conn, slug, settings)
    prop = one(conn, "SELECT * FROM properties WHERE id = ? AND market_id = ?", (pid, mc.id))
    if not prop:
        raise KeyError(f"Property {pid} is not in this market.")
    layers = {t: _one_by_property(conn, t, pid) for t in
              ("scores", "drive_times", "rent_fits", "visual_checks", "owners", "outcomes")}
    joined = _first(conn, f"SELECT {ROW_COLUMNS} {BASE_FROM} AND p.id = :pid", {**_base_params(mc), "pid": pid})
    drill = engine.property_drill(conn, pid)
    summary = {"level": drill["level"], "status": drill["status"], "next_step": drill["next_step"],
               "next_step_text": drill["next_step"].get("text"), "channels": drill["channels"],
               "best_guess_person": drill["best_guess_person"]}
    enrich = {"drive": layers["drive_times"], "rent": layers["rent_fits"], "visual": layers["visual_checks"],
              "owner": layers["owners"]}
    score = layers["scores"] or {}
    return {
        **property_row(joined or prop, summary, mc),
        "property": {k: v for k, v in prop.items() if k not in ("raw_json", "flags_json")},
        "raw": loads(prop.get("raw_json"), {}) or {},
        "flags": loads(prop.get("flags_json"), []) or [],
        "signals": loads(score.get("signals_json"), []) or [],
        "score_info": {"base_rate": mc.cfg.base_rate, "computed_at": score.get("computed_at"),
                       "scored": bool(score)},
        "buybox": _buybox_block(conn, mc, prop, enrich, score),
        "drive": _drive_detail(layers["drive_times"], mc),
        "visual_check": _visual_detail(prop, layers["visual_checks"], joined or {}, mc),
        "rent": _rent_detail(layers["rent_fits"], mc),
        "owner": _owner_detail(conn, prop, layers["owners"]),
        "drill": drill,
        "links": drill_links.links_for({**prop, "asset_class": mc.ac.key}, None, drill["best_guess_person"],
                                       settings=mc.settings, parse_owner=False),
        "outcome": _outcome(layers["outcomes"]),
        "asset_class": asset_class_info(mc.ac),
    }


def _buybox_block(conn: sqlite3.Connection, mc: MarketCtx, prop: Mapping[str, Any], enrich: Mapping[str, Any],
                  score: Mapping[str, Any] | None) -> dict:
    """The drawer's buy-box section: the rules checked live, with the decisions made by hand applied (the same
    way scoring applies them, so the drawer, the list and the exports agree), who decided and the history."""
    found = overrides.for_property(conn, int(prop["id"]))
    rules = evaluate(prop, enrich, mc.ac, mc.market, mc.settings)
    ev = apply_overrides(rules, found)
    details = ev["exclusion_details"]
    spec = B.group_spec(mc.market, mc.ac, mc.settings)
    groups = loads((score or {}).get("aside_buckets"), []) or []
    decisions = overrides.decisions_list(found)
    for d in decisions:   # what the rules say now, next to what they said when it was decided
        d["detail_now"] = (details.get(d["reason"]) if d["reason"] != "*" else
                           "; ".join(f"{EXCLUSION_LABELS.get(k, k)}: {v}" for k, v in details.items()
                                     if k != MANUAL_REASON) or "Fits the buy box by the rules")
    return {
        "in_buybox": ev["in_buybox"], "rules_in_buybox": ev["rules_in_buybox"],
        "exclusion_reasons": ev["exclusion_reasons"],
        "exclusions": [{"reason": k, "label": EXCLUSION_LABELS.get(k, k), "detail": details.get(k)}
                       for k in ev["exclusion_reasons"]],
        "rule_reasons": ev["rule_reasons"],
        "rules": [{"reason": k, "label": EXCLUSION_LABELS.get(k, k), "detail": details.get(k),
                   "waived": k in ev["waived"], "kept": k in ev["kept"]} for k in ev["rule_reasons"]],
        "waived": ev["waived"], "kept": ev["kept"], "manual": ev["manual"],
        "decisions": decisions, "history": overrides.history(conn, int(prop["id"])),
        "groups": [{"key": g, "reason": g.split(":", 1)[0],
                    "label": f"{_set_aside_label(g.split(':', 1)[0], mc)}: "
                             f"{B.label_for(g, spec, prop.get('property_type'))}"} for g in groups],
        "size_detail": ev["size_detail"], "size_basis_label": SIZE_BASIS_LABELS.get(ev["size_basis"]),
        "size_estimated": ev["size_estimated"], "flags": ev["flags"],
    }


def buybox_detail(conn: sqlite3.Connection, slug: str, pid: int, settings: Settings | None = None) -> dict:
    """Only the drawer's buy-box section for one property (after a decision)."""
    mc = market_ctx(conn, slug, settings)
    prop = one(conn, "SELECT * FROM properties WHERE id = ? AND market_id = ?", (pid, mc.id))
    if not prop:
        raise KeyError(f"Property {pid} is not in this market.")
    layers = {k: _one_by_property(conn, t, pid) for k, t in
              (("drive", "drive_times"), ("rent", "rent_fits"), ("visual", "visual_checks"), ("owner", "owners"))}
    return _buybox_block(conn, mc, prop, layers, _one_by_property(conn, "scores", pid))


def property_row_by_id(conn: sqlite3.Connection, slug: str, pid: int, settings: Settings | None = None) -> dict:
    """One PropertyRow, the way the deals list shows it."""
    mc = market_ctx(conn, slug, settings)
    joined = _first(conn, f"SELECT {ROW_COLUMNS} {BASE_FROM} AND p.id = :pid", {**_base_params(mc), "pid": pid})
    if not joined:
        raise KeyError(f"Property {pid} is not in this market.")
    drill = engine.summaries(conn, slug, property_ids=[pid]).get(pid)
    return property_row(joined, drill, mc)


def _drive_detail(drive: Mapping[str, Any] | None, mc: MarketCtx) -> dict:
    m = mc.market
    base = {"origin": m.get("origin_label"), "max_minutes": m.get("max_drive_minutes")}
    if not drive:
        return {**base, "minutes": None, "method": None, "approximate": None, "miles": None, "fetched_at": None}
    meters = drive.get("meters")
    return {**base, "minutes": _round(drive.get("minutes")), "method": drive.get("method"),
            "approximate": drive.get("method") == "estimate",
            "miles": round(meters / 1609.344, 1) if meters else None, "fetched_at": drive.get("fetched_at")}


def _visual_detail(prop: Mapping[str, Any], v: Mapping[str, Any] | None, joined: Mapping[str, Any],
                   mc: MarketCtx) -> dict:
    info = asset_class_info(mc.ac)
    if not mc.ac.visual_enabled:
        return {"enabled": False, "images": [], "photos": []}
    photos = images_for(int(prop["id"]), mc.settings)
    v = dict(v or {})
    keep = ("status", "pano_id", "pano_date", "heading", "ai_label", "ai_p", "image_quality", "reasons", "prior",
            "final_p", "model", "error", "reviewed_label", "reviewed_note", "reviewed_at", "checked_at")
    visible = v.get("property_visible")
    return {
        "enabled": True, "question": info["visual_question"], "labels": info["labels"],
        "target_labels": info["target_labels"], "label_help": info["label_help"],
        **{k: v.get(k) for k in keep},
        "property_visible": None if visible is None else bool(visible),
        "cues": loads(v.get("cues_json"), []) or [],
        "color": visual_color(v or None, mc.ac, mc.cfg),
        "needs_review": needs_review(joined, mc) if joined else False,
        "images": [p["url"] for p in photos], "photos": photos,
        "map_links": map_links(prop.get("lat"), prop.get("lng"), v.get("heading")),
        "prior_rules": explain_prior(prop, mc.ac, mc.settings),
    }


def _rent_detail(rent: Mapping[str, Any] | None, mc: MarketCtx) -> dict:
    spec = mc.ac.rent or {}
    base = {"enabled": mc.ac.rent_enabled, "scale": dict(spec.get("scale") or {}),
            "focus_bedrooms": spec.get("focus_bedrooms")}
    if not rent:
        return {**base, "fit": None, "detail": None, "rents": {}, "area_name": None, "fiscal_year": None,
                "source": None, "computed_at": None}
    return {**base, "fit": rent.get("fit"), "detail": rent.get("detail"), "rents": loads(rent.get("rents_json"), {}) or {},
            "area_name": rent.get("area_name"), "fiscal_year": rent.get("fiscal_year"), "source": rent.get("source"),
            "computed_at": rent.get("computed_at")}


def _owner_detail(conn: sqlite3.Connection, prop: Mapping[str, Any], owner: Mapping[str, Any] | None) -> dict:
    o = dict(owner or {})
    otype = o.get("owner_type")
    siblings: list[dict] = []
    sibling_total = 0
    if o.get("portfolio_key"):
        sibling_total = int(scalar(conn, "SELECT COUNT(*) FROM owners WHERE portfolio_key = ? AND property_id != ?",
                                   (o["portfolio_key"], prop["id"])) or 0)
        siblings = rows(conn, "SELECT p.id, p.address, p.city, p.state, m.slug AS market FROM owners o "
                              "JOIN properties p ON p.id = o.property_id JOIN markets m ON m.id = p.market_id "
                              "WHERE o.portfolio_key = ? AND p.id != ? ORDER BY p.id LIMIT 25",
                        (o["portfolio_key"], prop["id"]))
    return {
        "owner_name": prop.get("owner_name"), "owner_type": otype,
        "owner_type_label": OWNER_TYPE_LABELS.get(otype or "not_checked", otype),
        "portfolio_count": o.get("portfolio_count"), "absentee": o.get("absentee"),
        "years_owned": _round(o.get("years_owned")), "debt_status": o.get("debt_status"),
        "computed_at": o.get("computed_at"),
        "mailing": {k: prop.get(f"mailing_{k}") for k in ("address", "unit", "city", "state", "zip", "care_of")},
        "mailing_text": engine.mailing_text(prop),
        "siblings": siblings, "sibling_total": sibling_total,
        "sale_date": prop.get("sale_date"), "sale_price": prop.get("sale_price"),
        "lender": prop.get("lender"), "loan_amount": prop.get("loan_amount"), "loan_maturity": prop.get("loan_maturity"),
    }


def _outcome(row: Mapping[str, Any] | None) -> dict:
    value = (row or {}).get("outcome")
    return {"value": value, "label": OUTCOME_LABELS.get(value) if value else None, "note": (row or {}).get("note"),
            "created_at": (row or {}).get("created_at"),
            "options": [{"key": k, "label": v} for k, v in OUTCOME_LABELS.items()]}


# --- markets, summary, funnel -------------------------------------------------------------------

def markets(conn: sqlite3.Connection, settings: Settings | None = None) -> list[dict]:
    """Every market in config.yaml with its counts."""
    s = settings or get_settings()
    counts = {r["slug"]: r for r in rows(conn, f"""
        SELECT m.slug, COUNT(p.id) AS properties,
               COALESCE(SUM(CASE WHEN p.id IS NOT NULL AND {IN_BOX} THEN 1 ELSE 0 END), 0) AS in_buybox,
               COALESCE(SUM(CASE WHEN ds.level = 3 AND ds.status != 'dnc' THEN 1 ELSE 0 END), 0) AS confirmed
        FROM markets m LEFT JOIN properties p ON p.market_id = m.id
        LEFT JOIN scores s ON s.property_id = p.id LEFT JOIN drill_state ds ON ds.property_id = p.id
        GROUP BY m.id""")}
    out = []
    for m in s.markets:
        ac = s.asset_classes.get(m.asset_class)
        c = counts.get(m.slug, {})
        out.append({"slug": m.slug, "name": f"{ac.label if ac else m.asset_class} · {m.geography}",
                    "asset_class": m.asset_class, "asset_label": ac.label if ac else m.asset_class,
                    "geography": m.geography, "properties": int(c.get("properties") or 0),
                    "in_buybox": int(c.get("in_buybox") or 0), "confirmed": int(c.get("confirmed") or 0)})
    return out


def _counts_row(conn: sqlite3.Connection, mc: MarketCtx) -> dict:
    """Most of the summary and funnel numbers in one pass over the market."""
    cols = {
        "properties": "1",
        "in_buybox": IN_BOX,
        "excluded": "s.in_buybox = 0",
        "scored": "s.property_id IS NOT NULL",
        "owners_done": "o.property_id IS NOT NULL",
        "drive_done": "dt.property_id IS NOT NULL",
        "drive_estimated": "dt.method = 'estimate'",
        "rent_done": "rf.property_id IS NOT NULL",
        "photos_done": f"{IN_BOX} AND v.status IN ('done', 'no_imagery')",
        "photo_checked": f"{IN_BOX} AND (v.checked_at IS NOT NULL OR v.reviewed_label IS NOT NULL)",
        "photo_verdict": f"{IN_BOX} AND v.final_p IS NOT NULL",
        "target": f"{IN_BOX} AND v.final_p >= :green_min",
        "target_funnel": f"{IN_BOX} AND v.final_p >= :green_min AND s.score >= :funnel",
        "funnel_score": f"{IN_BOX} AND s.score >= :funnel",
        "moon": f"{IN_BOX} AND s.score >= :moon",
        "level1": f"{LEVEL} = 1", "level2": f"{LEVEL} = 2", "level3": f"{LEVEL} = 3",
        "st_active": f"{STATUS} = 'active'", "st_confirmed": f"{STATUS} = 'confirmed'",
        "st_exhausted": f"{STATUS} = 'exhausted'", "st_dnc": f"{STATUS} = 'dnc'",
        "box_level1": f"{IN_BOX} AND {LEVEL} = 1", "box_level2": f"{IN_BOX} AND {LEVEL} = 2",
        "box_level3": f"{IN_BOX} AND {LEVEL} = 3",
        "drill_box": f"{IN_BOX} AND {STATUS} != 'dnc'",
        "reach_possible": f"{IN_BOX} AND {STATUS} != 'dnc' AND {LEVEL} >= 2",
        "reach_confirmed": f"{IN_BOX} AND {STATUS} != 'dnc' AND {LEVEL} = 3",
        # Penetration: every owner except do-not-contact ones (exhausted owners still count).
        "pen_target": f"{STATUS} != 'dnc' AND ({IN_BOX} OR :no_scores = 1)",
        "pen_confirmed": f"{STATUS} != 'dnc' AND ({IN_BOX} OR :no_scores = 1) AND {LEVEL} = 3",
    }
    params = {**_base_params(mc), "funnel": float(mc.web["funnel_score"]),
              "no_scores": 0 if scalar(conn, "SELECT 1 FROM scores s JOIN properties p ON p.id = s.property_id "
                                             "WHERE p.market_id = ? LIMIT 1", (mc.id,)) else 1}
    parts = ", ".join(f"COALESCE(SUM(CASE WHEN {expr} THEN 1 ELSE 0 END), 0) AS {key}" for key, expr in cols.items())
    r = _first(conn, f"SELECT {parts} {BASE_FROM}", params) or {}
    return {k: int(r.get(k) or 0) for k in cols}


def _layer(key: str, label: str, done: int, total: int, enabled: bool = True, note: str | None = None) -> dict:
    return {"key": key, "label": label, "done": done, "total": total, "enabled": enabled,
            "pct": round(done / total, 4) if total else None, "note": note}


def _layers(conn: sqlite3.Connection, mc: MarketCtx, c: Mapping[str, int]) -> list[dict]:
    drilled = int(scalar(conn, "SELECT COUNT(DISTINCT a.property_id) FROM drill_attempts a "
                               "JOIN properties p ON p.id = a.property_id WHERE p.market_id = ? "
                               "AND a.stage_key = ?", (mc.id, engine.RECORDS_STAGE)) or 0)
    n, box = c["properties"], c["in_buybox"]
    visual = mc.ac.visual_enabled
    return [
        _layer("owners", "Owners", c["owners_done"], n),
        _layer("drive", "Drive time", c["drive_done"], n,
               note=f"{c['drive_estimated']:,} are straight-line estimates" if c["drive_estimated"] else None),
        _layer("rent", "Rent fit", c["rent_done"], n, enabled=mc.ac.rent_enabled),
        _layer("photos", "Photos", c["photos_done"], box, enabled=visual, note="Buy-box properties only"),
        _layer("photo_check", "Photo check", c["photo_checked"], box, enabled=visual, note="Buy-box properties only"),
        _layer("score", "Score", c["scored"], n),
        _layer("drill", "DRILL auto stages", drilled, n),
    ]


def last_import(conn: sqlite3.Connection, market_id: int) -> dict | None:
    return one(conn, "SELECT id, filename, rows_total, rows_new, rows_updated, rows_skipped, created_at "
                     "FROM imports WHERE market_id = ? ORDER BY id DESC LIMIT 1", (market_id,))


def summary(conn: sqlite3.Connection, slug: str, settings: Settings | None = None) -> dict:
    """The top bar and home numbers for one market."""
    from ..drill.credits import credit_status

    mc = market_ctx(conn, slug, settings)
    if mc.id in pending_rescore(conn):
        _rescore_once(conn, slug)
    c = _counts_row(conn, mc)
    review = queue_counts(conn, slug) if mc.ac.visual_enabled else {"total": 0}
    pen = round(c["pen_confirmed"] / c["pen_target"], 4) if c["pen_target"] else 0.0
    m = mc.market
    try:  # config.yaml is the source of truth; the markets row is a copy made at startup
        origin = mc.settings.market(mc.slug).origin
        o_label, o_lat, o_lng = origin.label, origin.lat, origin.lng
    except KeyError:
        o_label, o_lat, o_lng = m.get("origin_label"), m.get("origin_lat"), m.get("origin_lng")
    waiting = engine.waiting_counts(conn, slug, settings=mc.settings) if c["properties"] else \
        {"total": 0, "validate": 0, "stages": {}, "basis": "all"}
    return {
        "market": {"slug": mc.slug, "name": m["name"], "geography": m["geography"],
                   "origin_label": o_label, "origin_lat": o_lat, "origin_lng": o_lng,
                   "max_drive_minutes": m.get("max_drive_minutes")},
        "drill_waiting": waiting["total"],
        "drill_waiting_detail": waiting,
        "asset_class": asset_class_info(mc.ac),
        "properties": c["properties"], "in_buybox": c["in_buybox"], "excluded": c["excluded"],
        "scored": c["scored"], "blue_moon_10": c["moon"],
        "needs_review": int(review.get("total") or 0), "review_counts": review,
        "penetration": pen,
        "penetration_detail": {"confirmed": c["pen_confirmed"], "of": c["pen_target"],
                               "text": f"{c['pen_confirmed']:,} of {c['pen_target']:,} owners confirmed"},
        "levels": {"1": c["level1"], "2": c["level2"], "3": c["level3"]},
        "levels_in_buybox": {"1": c["box_level1"], "2": c["box_level2"], "3": c["box_level3"]},
        "statuses": {"active": c["st_active"], "confirmed": c["st_confirmed"], "exhausted": c["st_exhausted"],
                     "dnc": c["st_dnc"]},
        "credits": credit_status(conn, settings=mc.settings),
        "set_aside": set_aside(conn, mc),
        "last_import": last_import(conn, mc.id),
        "layers": _layers(conn, mc, c),
        "views": [{"key": k, "label": v} for k, v in view_labels(mc.ac, mc.web).items()],
        "sorts": list(SORTS),
        "thresholds": {"moon_score": mc.web["moon_score"], "funnel_score": mc.web["funnel_score"],
                       "long_owned_years": mc.web["long_owned_years"], "visual_green_min": mc.cfg.visual_green_min,
                       "visual_red_max": mc.cfg.visual_red_max},
    }


def _rescore_once(conn: sqlite3.Connection, slug: str) -> None:
    """Scores saved before the version 2 update have no set-aside groups yet: re-score the market once (free,
    about a second) so the Why? panel can show them."""
    from ..scoring.rank import compute_scores
    try:
        compute_scores(conn, slug)
    except Exception:  # the summary must still load; it tries again next time
        log.exception("Could not re-score %s for its set-aside groups", slug)


def funnel(conn: sqlite3.Connection, slug: str, settings: Settings | None = None) -> dict:
    """{find:[{label,count}], reach:[{label,count}], hits:[{label,count}]}."""
    mc = market_ctx(conn, slug, settings)
    c = _counts_row(conn, mc)
    pct = f"{mc.web['funnel_score']:.0%}"
    find = [{"key": "loaded", "label": "Loaded", "count": c["properties"]},
            {"key": "in_buybox", "label": "In buy box", "count": c["in_buybox"]}]
    word = target_word(mc.ac)
    if mc.ac.visual_enabled and c["photo_verdict"]:   # the photo steps only once some photo has a verdict
        find += [{"key": "photo_checked", "label": "Photo checked", "count": c["photo_verdict"]},
                 {"key": "target", "label": f"Likely {word.lower()}", "count": c["target"]},
                 {"key": "blue_moon", "label": f"{word} and Blue Moon {pct}+", "count": c["target_funnel"]}]
    else:
        find.append({"key": "blue_moon", "label": f"Blue Moon {pct}+", "count": c["funnel_score"],
                     "hint": "No photos checked yet" if mc.ac.visual_enabled else None})
    reach = [{"key": "in_drill", "label": "In the drill", "count": c["drill_box"]},
             {"key": "possible", "label": "Possible owner", "count": c["reach_possible"]},
             {"key": "confirmed", "label": "Confirmed owner", "count": c["reach_confirmed"]}]
    return {"find": _with_moved_on(find), "reach": _with_moved_on(reach), "hits": _hits(conn, mc.id),
            "penetration": round(c["pen_confirmed"] / c["pen_target"], 4) if c["pen_target"] else 0.0}


def _with_moved_on(steps: list[dict]) -> list[dict]:
    """Add the share of the previous step that made it to this one."""
    prev = None
    for s in steps:
        s["pct_of_previous"] = round(s["count"] / prev, 4) if prev else None
        prev = s["count"]
    return steps


def _hits(conn: sqlite3.Connection, market_id: int) -> list[dict]:
    r = _first(conn, f"""
        SELECT
          (SELECT COUNT(*) FROM drill_attempts a JOIN properties p ON p.id = a.property_id
            WHERE p.market_id = :m AND a.outcome = 'found' AND a.actor != 'auto') AS stage_hits,
          (SELECT COUNT(*) FROM contact_points c JOIN properties p ON p.id = c.property_id
            WHERE p.market_id = :m AND c.channel IN ({','.join(repr(x) for x in engine.REACH_CHANNELS)})) AS contacts,
          (SELECT COUNT(*) FROM contact_points c JOIN properties p ON p.id = c.property_id
            WHERE p.market_id = :m AND c.status = 'verified') AS verified,
          (SELECT COUNT(*) FROM outreach o JOIN properties p ON p.id = o.property_id
            WHERE p.market_id = :m) AS outreach,
          (SELECT COUNT(*) FROM credit_ledger c JOIN properties p ON p.id = c.property_id
            WHERE p.market_id = :m) AS credits
        """, {"m": market_id}) or {}
    labels = [("stage_hits", "Lookups that found something"), ("contacts", "Phones, emails and socials found"),
              ("verified", "Contacts that worked"), ("outreach", "Reach-outs logged"),
              ("credits", "Crexi credits used")]
    return [{"key": k, "label": label, "count": int(r.get(k) or 0)} for k, label in labels]


# --- analytics ----------------------------------------------------------------------------------

COVERAGE_FIELDS: list[tuple[str, str, str]] = [
    ("location", "Map location", "p.lat IS NOT NULL AND p.lng IS NOT NULL"),
    ("year_built", "Year built", "p.year_built IS NOT NULL"),
    ("units", "Units", "p.units IS NOT NULL"),
    ("building_sqft", "Building sq ft", "p.building_sqft IS NOT NULL"),
    ("lot_acres", "Lot acres", "p.lot_acres IS NOT NULL"),
    ("stories", "Stories", "p.stories IS NOT NULL"),
    ("total_value", "Total parcel value", "p.total_value IS NOT NULL"),
    ("owner_name", "Owner name", "NULLIF(TRIM(p.owner_name), '') IS NOT NULL"),
    ("mailing_address", "Owner mailing address", "NULLIF(TRIM(p.mailing_address), '') IS NOT NULL"),
    ("sale_date", "Last sale", "NULLIF(TRIM(p.sale_date), '') IS NOT NULL"),
    ("loan", "Loan record", "NULLIF(TRIM(p.lender), '') IS NOT NULL OR p.loan_amount IS NOT NULL"),
    ("apn", "Parcel number", "NULLIF(TRIM(p.apn), '') IS NOT NULL"),
]

SCORE_BUCKETS: list[tuple[str, float, float]] = [
    ("Under 1%", 0.0, 0.01), ("1-2%", 0.01, 0.02), ("2-5%", 0.02, 0.05), ("5-10%", 0.05, 0.10),
    ("10-20%", 0.10, 0.20), ("20%+", 0.20, 2.0),
]


def analytics(conn: sqlite3.Connection, slug: str, settings: Settings | None = None) -> dict:
    mc = market_ctx(conn, slug, settings)
    metric = f"p.{mc.ac.size.metric}" if mc.ac.size.metric in METRIC_UNITS else "NULL"   # a known column only
    base = _all(conn, f"""SELECT p.state, p.county, s.in_buybox, s.exclusion_reasons, s.score, s.size_basis,
                                 COALESCE(s.size_value, {metric}) AS size_value,
                                 o.owner_type, {LEVEL} AS level, {STATUS} AS status
                          {BASE_FROM}""", {"market_id": mc.id})
    return {
        "market": mc.slug,
        "properties": len(base),
        "imports": rows(conn, "SELECT id, filename, rows_total, rows_new, rows_updated, rows_skipped, notes, "
                              "created_at FROM imports WHERE market_id = ? ORDER BY id DESC LIMIT 50", (mc.id,)),
        "filter_funnel": filter_funnel(base, mc),
        "coverage": coverage(conn, mc),
        "counties": counties(base),
        "size_dist": size_distribution(base, mc.ac),
        "score_dist": score_distribution(base),
        "owner_types": owner_types(base),
        "levels": levels(base, mc.settings),
        "api_usage": api_usage(conn, mc.settings),
    }


def _reason_label(reason: str, mc: MarketCtx) -> str:
    """What passing a filter means, in plain English."""
    ac, geo = mc.ac, mc.settings.section("geography")
    lo, hi = ac.size.ok
    unit = METRIC_UNITS.get(ac.size.metric, ac.size.metric)
    return {
        "state": "In " + ", ".join(str(s) for s in (geo.get("allowed_states") or [])) if geo.get("allowed_states")
        else "Any state",
        "skip_county": "Not in a skipped county",
        "drive_time": f"Within {mc.market.get('max_drive_minutes')} min drive",
        "property_type": f"Listed as {ac.label.lower()}",
        "year": f"Built {ac.year_built_min} or later" if ac.year_built_min else "Any year",
        "size": f"{lo:,.0f}-{hi:,.0f} {unit} (or unknown)",
        MANUAL_REASON: "Not set aside by hand",
    }.get(reason, EXCLUSION_LABELS.get(reason, reason))


FUNNEL_ORDER = ("state", "skip_county", "drive_time", "property_type", "year", "size")


def _set_aside_label(reason: str, mc: MarketCtx) -> str:
    """Why a property is out of the buy box, in plain English (the opposite of _reason_label)."""
    ac = mc.ac
    lo, hi = ac.size.ok
    unit = METRIC_UNITS.get(ac.size.metric, ac.size.metric)
    return {
        "drive_time": f"More than {mc.market.get('max_drive_minutes')} min drive",
        "year": f"Built before {ac.year_built_min}" if ac.year_built_min else EXCLUSION_LABELS["year"],
        "size": f"Outside {lo:,.0f}-{hi:,.0f} {unit}",
        "property_type": f"Not listed as {ac.label.lower()}",
    }.get(reason, EXCLUSION_LABELS.get(reason, reason))


_SET_ASIDE_SQL = """
SELECT s.in_buybox, s.exclusion_reasons AS reasons, COALESCE(s.rule_reasons, s.exclusion_reasons) AS rules,
       s.aside_buckets AS groups, p.property_type, dt.method, dt.minutes,
       (SELECT group_concat(o.reason) FROM buybox_overrides o
         WHERE o.property_id = p.id AND o.decision = 'keep') AS kept
FROM properties p JOIN scores s ON s.property_id = p.id
LEFT JOIN drive_times dt ON dt.property_id = p.id
WHERE p.market_id = ? AND (s.in_buybox = 0 OR COALESCE(s.rule_reasons, s.exclusion_reasons) != '[]')
"""
_TALLY = ("count", "only", "still_aside", "added", "kept", "estimated")


def set_aside(conn: sqlite3.Connection, mc: MarketCtx) -> dict:
    """The Why? panel: why properties are out of the buy box (they are never deleted).

    total = how many are set aside now. Each reason (one property can have several) has `count` (the rules said
    so, or set aside by hand), `only` (set aside for this reason alone), `still_aside`, `added` (in the buy box
    by hand anyway), `kept` (Keep set aside), a plain `explain`, and its groups (`buckets`, same numbers plus
    `estimated` drive times). A reason's groups add up to its count. Drive times that are straight-line
    estimates are counted, and so are "close calls": set aside only for an estimated drive within
    web.drive_close_call_minutes of the limit, which may come back once Google measures real drives.
    One query and one pass over the rows (the summary is asked for often)."""
    found = rows(conn, _SET_ASIDE_SQL, (mc.id,))
    spec = B.group_spec(mc.market, mc.ac, mc.settings)
    max_minutes = mc.market.get("max_drive_minutes")
    close_window = float(mc.web.get("drive_close_call_minutes") or 0)
    by_reason: dict[str, Counter[str]] = {}
    by_group: dict[str, Counter[str]] = {}
    samples: dict[str, str] = {}
    total = several = estimated = close = added = by_hand_aside = also_far = 0
    parsed: dict[Any, list[str]] = {}

    def as_list(raw: Any) -> list[str]:
        if raw not in parsed:              # a few dozen distinct lists, thousands of rows
            parsed[raw] = [str(x) for x in (loads(raw, []) or [])]
        return parsed[raw]

    for r in found:
        final = set(as_list(r.get("reasons")))
        rules = as_list(r.get("rules"))
        aside = not r.get("in_buybox")
        everything = set(rules) | (final & {MANUAL_REASON})
        groups = as_list(r["groups"]) if r.get("groups") is not None else \
            [f"{x}:{B.OTHER}" for x in sorted(everything, key=_reason_order)]
        kept = set((r.get("kept") or "").split(",")) - {""}
        drive_estimate = r.get("method") == "estimate"
        if aside:
            total += 1
            several += len(final) > 1
            if "drive_time" in final and drive_estimate:
                estimated += 1
                minutes = r.get("minutes")
                if final == {"drive_time"} and max_minutes and minutes is not None \
                        and float(minutes) <= float(max_minutes) + close_window:
                    close += 1
        else:
            added += 1 if rules else 0
        by_hand_aside += MANUAL_REASON in final
        also_far += "skip_county" in everything and "drive_time" in everything
        for key in groups:
            reason = key.split(":", 1)[0]
            tally = {"count": 1, "only": everything == {reason}, "still_aside": aside, "added": not aside,
                     "kept": reason in kept, "estimated": reason == "drive_time" and drive_estimate}
            by_group.setdefault(key, Counter()).update({k: int(v) for k, v in tally.items()})
            by_reason.setdefault(reason, Counter()).update({k: int(v) for k, v in tally.items()})
            if reason == "property_type" and key not in samples and r.get("property_type"):
                samples[key] = str(r["property_type"]).strip()
    order = sorted((k for k in by_reason if by_reason[k]["count"]), key=lambda k: (-by_reason[k]["count"], k))
    return {"total": total, "several": several,
            "reasons": [_reason_entry(k, by_reason[k], by_group, samples, spec, mc, also_far) for k in order],
            "drive_estimated": estimated, "drive_close_calls": close,
            "close_call_minutes": close_window, "max_drive_minutes": max_minutes,
            "by_hand": {"added": added, "set_aside": by_hand_aside}}


def _reason_order(reason: str) -> int:
    return SET_ASIDE_REASONS.index(reason) if reason in SET_ASIDE_REASONS else 99


def _reason_entry(reason: str, tally: Counter[str], by_group: Mapping[str, Counter[str]], samples: Mapping[str, str],
                  spec: B.GroupSpec, mc: MarketCtx, also_far: int) -> dict:
    fixed = spec.order(reason)
    keys = [k for k in by_group if k.split(":", 1)[0] == reason]
    keys.sort(key=lambda k: (k.endswith(f":{B.OTHER}"), fixed.index(k) if k in fixed else len(fixed),
                             -by_group[k]["count"], k))
    return {"key": reason, "label": _set_aside_label(reason, mc), "count": tally["count"], "only": tally["only"],
            "explain": B.explain(reason, mc.market, mc.ac, mc.settings, also_far=also_far),
            "still_aside": tally["still_aside"], "added": tally["added"], "kept": tally["kept"],
            "buckets": [{"key": k, "label": B.label_for(k, spec, samples.get(k)),
                         **{f: by_group[k][f] for f in _TALLY}} for k in keys]}


def review_block(conn: sqlite3.Connection, mc: MarketCtx, bucket: str) -> dict:
    """What the Review list shows above one group: {bucket, reason, reason_label, label, explain, count,
    still_aside, added, kept, only}."""
    reason, _ = B.parse_key(bucket)
    info = set_aside(conn, mc)
    entry = next((x for x in info["reasons"] if x["key"] == reason), None)
    group = next((g for g in (entry or {}).get("buckets", []) if g["key"] == bucket), None)
    spec = B.group_spec(mc.market, mc.ac, mc.settings)
    return {"bucket": bucket, "reason": reason, "reason_label": _set_aside_label(reason, mc),
            "label": group["label"] if group else B.label_for(bucket, spec),
            "explain": entry["explain"] if entry else B.explain(reason, mc.market, mc.ac, mc.settings),
            **{f: int((group or {}).get(f) or 0) for f in ("count", "still_aside", "added", "kept", "only")}}


def filter_funnel(base: list[dict], mc: MarketCtx) -> list[dict]:
    """Loaded, then what is left after each filter (in a sensible order), and last the ones set aside by hand.
    Nothing is deleted. The steps count why properties are out now, so a rule ignored by hand removes fewer."""
    reasons_of = [set(loads(r.get("exclusion_reasons"), []) or []) for r in base]
    order = [r for r in FUNNEL_ORDER if r in SET_ASIDE_REASONS] + [r for r in SET_ASIDE_REASONS if r not in FUNNEL_ORDER]
    out = [{"key": "loaded", "label": "Loaded", "count": len(base), "removed": 0, "removed_label": None}]
    applied: set[str] = set()
    left = len(base)
    for reason in order:
        applied.add(reason)
        now = sum(1 for rs in reasons_of if not (rs & applied))
        if now == left and not any(reason in rs for rs in reasons_of):
            continue  # this filter removed nothing; keep the chart short
        out.append({"key": reason, "label": _reason_label(reason, mc), "count": now, "removed": left - now,
                    "removed_label": EXCLUSION_LABELS.get(reason, reason)})
        left = now
    return out


def coverage(conn: sqlite3.Connection, mc: MarketCtx) -> list[dict]:
    parts = ", ".join(f"COALESCE(SUM(CASE WHEN {expr} THEN 1 ELSE 0 END), 0) AS {key}"
                      for key, _, expr in COVERAGE_FIELDS)
    r = one(conn, f"SELECT COUNT(*) AS n, {parts} FROM properties p WHERE p.market_id = ?", (mc.id,)) or {}
    n = int(r.get("n") or 0)
    return [{"field": key, "label": label, "count": int(r.get(key) or 0),
             "pct": round(100.0 * int(r.get(key) or 0) / n, 1) if n else 0.0} for key, label, _ in COVERAGE_FIELDS]


def counties(base: list[dict]) -> list[dict]:
    tally: dict[tuple[str, str], list[int]] = {}
    for r in base:
        key = ((r.get("state") or "").upper(), (r.get("county") or "Unknown").title())
        t = tally.setdefault(key, [0, 0])
        t[0] += 1
        t[1] += 1 if r.get("in_buybox") in (1, None) else 0
    return [{"state": st, "county": co, "count": n, "in_buybox": box}
            for (st, co), (n, box) in sorted(tally.items(), key=lambda kv: (-kv[1][0], kv[0]))]


def _size_label(lo: float | None, hi: float | None, metric: str, integer: bool) -> str:
    def f(v: float) -> str:
        return f"{v:,.0f}" if integer or float(v).is_integer() else f"{v:,.1f}"
    if lo is None:
        return f"Under {f(hi)}"
    if hi is None:
        return f"Over {f(lo)}"
    return f"{f(lo)}-{f(hi)}"


def size_distribution(base: list[dict], ac: AssetClass) -> list[dict]:
    """Buckets around the asset class's ok/ideal ranges, so the sweet spot is its own bar."""
    ok_lo, ok_hi = (float(x) for x in ac.size.ok)
    id_lo, id_hi = (float(x) for x in ac.size.ideal)
    metric = ac.size.metric
    integer = metric == "units"
    step = 1.0 if integer else 0.0
    buckets = [
        ("below", _size_label(None, ok_lo, metric, integer), "red", lambda v: v < ok_lo),
        ("ok_low", _size_label(ok_lo, id_lo - step, metric, integer), "yellow", lambda v: ok_lo <= v < id_lo),
        ("ideal", _size_label(id_lo, id_hi, metric, integer) + " (sweet spot)", "green", lambda v: id_lo <= v <= id_hi),
        ("ok_high", _size_label(id_hi + step, ok_hi, metric, integer), "yellow", lambda v: id_hi < v <= ok_hi),
        ("above", _size_label(ok_hi, None, metric, integer), "red", lambda v: v > ok_hi),
    ]
    counts = {key: [0, 0] for key, *_ in buckets}
    unknown = 0
    for r in base:
        v = r.get("size_value")
        if v is None or float(v) <= 0:
            unknown += 1
            continue
        for key, _, _, test in buckets:
            if test(float(v)):
                counts[key][0] += 1
                counts[key][1] += 1 if r.get("size_basis") in ("address_estimate", "sqft_estimate", "acres_estimate") else 0
                break
    out = [{"key": key, "label": label, "color": color, "count": counts[key][0], "estimated": counts[key][1]}
           for key, label, color, _ in buckets]
    out.append({"key": "unknown", "label": "Unknown", "color": "na", "count": unknown, "estimated": 0})
    unit = METRIC_UNITS.get(metric, metric)
    for item in out:
        item["unit"] = unit
    return out


def score_distribution(base: list[dict]) -> list[dict]:
    """Blue Moon scores of the buy box."""
    box = [r for r in base if r.get("in_buybox") in (1, None)]
    out = []
    for label, lo, hi in SCORE_BUCKETS:
        out.append({"label": label, "lo": lo, "hi": hi if hi <= 1 else None,
                    "count": sum(1 for r in box if r.get("score") is not None and lo <= r["score"] < hi)})
    out.append({"label": "Not scored", "lo": None, "hi": None, "count": sum(1 for r in box if r.get("score") is None)})
    return out


def owner_types(base: list[dict]) -> list[dict]:
    tally: dict[str, int] = {}
    for r in base:
        key = r.get("owner_type") or "not_checked"
        tally[key] = tally.get(key, 0) + 1
    return [{"owner_type": k, "label": OWNER_TYPE_LABELS.get(k, k.replace("_", " ").title()), "count": n}
            for k, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))]


def levels(base: list[dict], settings: Settings) -> list[dict]:
    """Every property's contact level (do-not-contact owners in their own row), each with `in_buybox`: how many
    of them are in the buy box (not scored yet counts as in, like IN_BOX), the owners the DRILL and Funnel
    count."""
    names = engine.drill_config(settings).levels

    def row(key: str, level: int | None, label: str, match: Callable[[dict], bool]) -> dict:
        hits = [r for r in base if match(r)]
        return {"key": key, "level": level, "label": label, "count": len(hits),
                "in_buybox": sum(1 for r in hits if r.get("in_buybox") is None or bool(r["in_buybox"]))}

    out = [row(str(lv), lv, f"Level {lv} · {names.get(lv, lv)}",
               lambda r, lv=lv: r["level"] == lv and r["status"] != "dnc") for lv in (1, 2, 3)]
    out.append(row("dnc", None, "Do not contact", lambda r: r["status"] == "dnc"))
    return out


def api_usage(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """Metered calls this month (all markets) by SKU, with the estimated bill after free calls."""
    prices = settings.section("google").get("prices") or {}
    vision = settings.section("vision")
    per_call = float(((vision.get("anthropic_api") or {}).get("est_cost_per_call_usd")) or 0)
    out = []
    for r in rows(conn, "SELECT provider, sku, SUM(units) AS units, COUNT(*) AS batches FROM api_calls "
                        "WHERE strftime('%Y-%m', created_at, 'localtime') = ? GROUP BY provider, sku "
                        "ORDER BY provider, sku",
                  (this_month(),)):
        units = int(r["units"] or 0)
        item = {"provider": r["provider"], "sku": r["sku"], "units": units, "free_per_month": None,
                "billable": None, "usd": None, "note": None}
        if r["sku"] in prices:
            price = prices[r["sku"]] or {}
            free = int(price.get("free_per_month") or 0)
            billable = max(0, units - free)
            item.update(free_per_month=free, billable=billable,
                        usd=round(billable * float(price.get("per_1000") or 0) / 1000.0, 2))
        elif r["sku"] == "vision_anthropic_api":
            item.update(billable=units, usd=round(units * per_call, 2), note="Estimated from config.yaml")
        elif r["sku"] in ("vision_claude_cli", "research_claude_cli"):
            item.update(note="Runs on your Claude plan (no per-call bill)")
        out.append(item)
    return out


# --- DRILL, credits, sources, settings ----------------------------------------------------------

def drill_stages(conn: sqlite3.Connection, slug: str, settings: Settings | None = None) -> dict:
    """Stage config from drill.yaml merged with real hit rates (the 'works best for' table)."""
    from ..drill import stats

    s = settings or get_settings()
    market_by_slug(conn, slug)
    cfg = engine.drill_config(s)
    report = stats.drill_report(conn, slug, settings=s)
    by_key = {r["stage_key"]: r for r in report["stages"]}
    keep = ("tried", "found", "skipped", "rate", "blended", "best_for", "best_for_text")
    stages = [{**st.public(), **{k: (by_key.get(st.key) or {}).get(k) for k in keep}} for st in cfg.stages]
    v = cfg.validate
    return {"market": slug, "stages": stages, "levels": cfg.levels,
            "validate": {"name": v.name, "channels": list(v.channels), "tries_before_moving_on": v.tries_before_moving_on,
                         "outcomes": [{"key": k, "label": o.label, "effect": o.effect} for k, o in v.outcomes.items()]},
            "advice": report["advice"], "by_owner_type": report["by_owner_type"], "outreach": report["outreach"]}


def credits_view(conn: sqlite3.Connection, slug: str, limit: int = 25, settings: Settings | None = None) -> dict:
    """{status, plan:[first `limit`], extras:[first `limit`], planned, extra_count, planned_all, extras_all,
    other_markets, others:[{slug, name, planned, extras}], shortfall, level1_open, message, log}.

    Credits belong to the whole Crexi account, so the plan is made once for every market (drill.credits);
    `plan`, `extras`, `planned` and `extra_count` are this market's share of it; planned_all, extras_all,
    shortfall and level1_open count every market."""
    from ..drill import credits

    s = settings or get_settings()
    info = credits.market_plan(conn, slug, limit=max(0, int(limit)), settings=s)
    return {**info, "log": credits.credit_log(conn, slug, limit=100)}


def sources_catalog(settings: Settings | None = None) -> dict:
    """Data sources (free first, by priority) with the stages that use them, plus the county table."""
    s = settings or get_settings()
    raw = s.sources or {}
    used_by: dict[str, list[str]] = {}
    for st in s.drill_stages:
        for key in st.get("sources") or []:
            used_by.setdefault(key, []).append(st.get("key"))
    cost_rank = {"free": 0, "credit": 1, "paid": 2}
    # `stages` are the stages that really use a source (config/drill.yaml). The `drill_stages` notes in
    # sources.yaml are ideas for wiring, not what the app does, so they are not sent.
    items = [{"key": k, **{f: x for f, x in (v or {}).items() if f != "drill_stages"}, "stages": used_by.get(k, [])}
             for k, v in (raw.get("sources") or {}).items()]
    items.sort(key=lambda x: (cost_rank.get(str(x.get("cost") or "free"), 3), x.get("priority") or 9,
                              str(x.get("name") or x["key"]).lower()))
    county_rows = [{"state": str(st), "county": str(co), **(entry or {})}
                   for st, cs in (raw.get("counties") or {}).items() for co, entry in (cs or {}).items()]
    return {"sources": items, "counties": county_rows,
            "stages": [{"key": st.get("key"), "name": st.get("name"), "sources": st.get("sources") or []}
                       for st in s.drill_stages]}


def redact(value: Any) -> Any:
    """Copy of a settings tree with anything that looks like a key, token or password removed."""
    if isinstance(value, Mapping):
        return {k: ("(hidden)" if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return copy.deepcopy(value)


def readable_settings(settings: Settings | None = None) -> dict:
    """config.yaml and the asset classes for the Settings screen. Never includes .env values."""
    s = settings or get_settings()
    return {
        "config": redact(s.raw),
        "asset_classes": {k: redact(ac.model_dump()) for k, ac in s.asset_classes.items()},
        "drill_levels": engine.drill_config(s).levels,
        "keys": key_status(),
        "web": redact(web_config(s)),
        "files": {"config": "config.yaml", "asset_classes": "config/asset_classes.yaml",
                  "drill": "config/drill.yaml", "sources": "config/sources.yaml", "secrets": ".env"},
    }


def contact_property(conn: sqlite3.Connection, contact_id: int) -> int | None:
    return scalar(conn, "SELECT property_id FROM contact_points WHERE id = ?", (contact_id,))


def property_in_market(conn: sqlite3.Connection, slug: str, pid: int) -> bool:
    m = market_by_slug(conn, slug)
    return scalar(conn, "SELECT 1 FROM properties WHERE id = ? AND market_id = ?", (pid, m["id"])) is not None


def ids_of(items: Iterable[Mapping[str, Any]]) -> list[int]:
    return [int(i["id"]) for i in items]
