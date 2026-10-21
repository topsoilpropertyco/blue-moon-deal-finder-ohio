"""DRILL feedback loops: which stage works for which kind of owner, and the CRM handoff list.

Every check-off is evidence. `stage_hit_rates` counts, for each stage and owner type, how many owners were
tried and how many turned something up. The `blended` rate mixes the starting guess in config/drill.yaml
with real results (the guess counts as `drill.yaml hit_rate_prior_strength` pseudo-tries, default 10), so a
handful of results nudges it and hundreds of results take over. `stage_advice` turns those numbers into
plain-English tips. They also change what happens next: the engine reorders the free hands-on stages per owner
type once they have `advice_min_tries` results (drill.yaml learn_order), and the credit plan ranks owners by
score x the Crexi credit's blended hit rate for their owner type.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from ..config import Settings, get_settings
from ..db import market_by_slug
from . import engine as E

DEFAULT_PRIOR_STRENGTH = 10.0   # drill.yaml hit_rate_prior_strength overrides this
DEFAULT_ADVICE_MIN_TRIES = 10   # drill.yaml advice_min_tries overrides this
OWNER_TYPE_LABELS = {
    "individual": "individuals", "trust": "trusts", "estate": "estates and heirs", "small_llc": "small LLCs",
    "corporation": "corporations", "portfolio": "portfolio owners", "institutional": "institutions",
    "government": "government owners", "nonprofit": "nonprofits", "unknown": "unknown owner types",
    "all": "every owner type",
}


def _prior_strength(settings: Settings | None) -> float:
    s = settings or get_settings()
    try:
        return max(0.0, float((s.drill or {}).get("hit_rate_prior_strength", DEFAULT_PRIOR_STRENGTH)))
    except (TypeError, ValueError):
        return DEFAULT_PRIOR_STRENGTH


def _min_tries(settings: Settings | None) -> int:
    s = settings or get_settings()
    try:
        return max(1, int((s.drill or {}).get("advice_min_tries", DEFAULT_ADVICE_MIN_TRIES)))
    except (TypeError, ValueError):
        return DEFAULT_ADVICE_MIN_TRIES


def blend(found: int, tried: int, hypothesis: float | None, strength: float) -> float | None:
    """Empirical-Bayes style mix of the starting guess and the real hit rate."""
    if hypothesis is None:
        return round(found / tried, 4) if tried else None
    return round((found + strength * hypothesis) / (tried + strength), 4) if (tried + strength) else hypothesis


def counts_by_market(conn: sqlite3.Connection, market_ids: list[int]) -> dict[tuple[int, str, str], dict[str, int]]:
    """{(market_id, stage_key, owner_type or 'all'): {tried, found, skipped}} from the latest check-off per
    property and stage.

    One SQL pass over drill_attempts (not a load of every property), so the DRILL screens stay fast."""
    counts: dict[tuple[int, str, str], dict[str, int]] = defaultdict(lambda: {"tried": 0, "found": 0, "skipped": 0})
    ids = [int(i) for i in market_ids]
    if not ids:
        return counts
    sql = f"""
    SELECT market_id, stage_key, owner_type, outcome, COUNT(*) AS n FROM (
      SELECT p.market_id, a.stage_key, a.outcome, COALESCE(o.owner_type, 'unknown') AS owner_type,
             ROW_NUMBER() OVER (PARTITION BY a.property_id, a.stage_key ORDER BY a.id DESC) AS rn
      FROM drill_attempts a
      JOIN properties p ON p.id = a.property_id
      LEFT JOIN owners o ON o.property_id = a.property_id
      WHERE p.market_id IN ({",".join("?" for _ in ids)}) AND a.stage_key != ?
    ) WHERE rn = 1 GROUP BY market_id, stage_key, owner_type, outcome
    """
    for mid, key, ot, outcome, n in conn.execute(sql, (*ids, E.PASS_MARKER)).fetchall():
        for t in (ot or "unknown", "all"):
            c = counts[(int(mid), key, t)]
            if outcome in ("found", "nothing"):
                c["tried"] += n
                c["found"] += n if outcome == "found" else 0
            elif outcome == "skipped":
                c["skipped"] += n
    return counts


def _counts(conn: sqlite3.Connection, market_slug: str,
            by_market: Mapping[tuple[int, str, str], Mapping[str, int]] | None = None,
            ) -> dict[tuple[str, str], dict[str, int]]:
    """{(stage_key, owner_type or 'all'): {tried, found, skipped}} for one market (see counts_by_market)."""
    market_id = int(market_by_slug(conn, market_slug)["id"])
    out: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"tried": 0, "found": 0, "skipped": 0})
    found = counts_by_market(conn, [market_id]) if by_market is None else by_market
    for (mid, key, ot), c in found.items():
        if int(mid) == market_id:
            out[(key, ot)] = dict(c)
    return out


def stage_hit_rates(conn: sqlite3.Connection, market_slug: str, *, settings: Settings | None = None,
                    counts: Mapping[tuple[int, str, str], Mapping[str, int]] | None = None) -> list[dict]:
    """[{stage_key, stage_name, owner_type, owner_label, automation, tried, found, skipped, rate, hypothesis,
    blended}].

    One 'all' row per stage (always present), then one row per owner type that has tried it, most tried first.
    `counts` (counts_by_market for this market and maybe others) can be passed in when the caller has them.
    """
    cfg = E.drill_config(settings)
    strength = _prior_strength(settings)
    counts = _counts(conn, market_slug, counts)
    out = []
    for st in cfg.stages:
        types = sorted({ot for (k, ot) in counts if k == st.key and ot != "all"},
                       key=lambda ot: (-counts[(st.key, ot)]["tried"], ot))
        for ot in ["all", *types]:
            c = counts.get((st.key, ot), {"tried": 0, "found": 0, "skipped": 0})
            out.append({"stage_key": st.key, "stage_name": st.name, "goal": st.goal, "cost": st.cost,
                        "automation": st.automation, "owner_type": ot,
                        "owner_label": OWNER_TYPE_LABELS.get(ot, ot.replace("_", " ")),
                        "tried": c["tried"], "found": c["found"], "skipped": c["skipped"],
                        "rate": round(c["found"] / c["tried"], 4) if c["tried"] else None,
                        "hypothesis": st.hit_rate, "blended": blend(c["found"], c["tried"], st.hit_rate, strength)})
    return out


def stage_summary(conn: sqlite3.Connection, market_slug: str, *, settings: Settings | None = None,
                  rates: list[dict] | None = None) -> list[dict]:
    """One row per stage for the 'hit rate by source' table: tried, found, rate and who it works best for.

    `rates` (from stage_hit_rates) can be passed in so the numbers are counted only once."""
    min_tries = _min_tries(settings)
    by_stage: dict[str, list[dict]] = defaultdict(list)
    for r in (rates if rates is not None else stage_hit_rates(conn, market_slug, settings=settings)):
        by_stage[r["stage_key"]].append(r)
    out = []
    for rs in by_stage.values():
        total = rs[0]
        typed = [r for r in rs[1:] if r["tried"] >= min_tries and r["rate"] is not None]
        best = max(typed, key=lambda r: (r["rate"], r["tried"]), default=None)
        if best is not None and not best["rate"] > 0:
            best = None   # found nothing for anyone yet: it works best for no one
        out.append({**{k: total[k] for k in ("stage_key", "stage_name", "goal", "cost", "tried", "found", "skipped",
                                            "rate", "hypothesis", "blended")},
                    "best_for": best["owner_type"] if best else None,
                    "best_for_text": (f"{_sentence_case(best['owner_label'])} ({best['rate']:.0%} of {best['tried']:,})"
                                      if best else _no_best_text(typed))})
    return out


def _sentence_case(text: str) -> str:
    """'small LLCs' -> 'Small LLCs' (str.capitalize would make it 'Small llcs')."""
    return text[:1].upper() + text[1:]


def _no_best_text(typed: list[dict]) -> str:
    return "Nothing found for any owner type yet" if typed else "Not enough results yet"


def stage_advice(conn: sqlite3.Connection, market_slug: str, *, settings: Settings | None = None,
                 rates: list[dict] | None = None) -> list[dict]:
    """Plain-English tips per owner type: which free hands-on stage has worked best so far (needs enough tries;
    automatic stages are left out)."""
    min_tries = _min_tries(settings)
    rows_by_type: dict[str, list[dict]] = defaultdict(list)
    for r in (rates if rates is not None else stage_hit_rates(conn, market_slug, settings=settings)):
        # Automatic stages run by themselves on every DRILL run, so a tip about them is nothing Seth can act on.
        if r["owner_type"] != "all" and r["cost"] == "free" and r.get("automation") != "auto" \
                and r["tried"] >= min_tries and r["rate"] is not None:
            rows_by_type[r["owner_type"]].append(r)
    tips = []
    for ot, rs in sorted(rows_by_type.items()):
        rs.sort(key=lambda r: (-(r["blended"] or 0), r["stage_name"]))
        best = rs[0]
        text = (f"For {best['owner_label']}, {best['stage_name']} has worked best so far "
                f"({best['found']:,} of {best['tried']:,}, {best['rate']:.0%}).")
        if len(rs) > 1:
            worst = rs[-1]
            text += f" {worst['stage_name']} the least ({worst['rate']:.0%})."
        tips.append({"owner_type": ot, "owner_label": best["owner_label"], "text": text,
                     "stages": [{"stage_key": r["stage_key"], "stage_name": r["stage_name"], "tried": r["tried"],
                                 "rate": r["rate"], "blended": r["blended"]} for r in rs]})
    return tips


def outreach_rates(conn: sqlite3.Connection, market_slug: str, *, settings: Settings | None = None) -> list[dict]:
    """Per channel: reach-outs logged, confirmed owners, wrong numbers or addresses, and do-not-contacts."""
    cfg = E.drill_config(settings)
    effects = {k: v.effect for k, v in cfg.validate.outcomes.items()}
    market = market_by_slug(conn, market_slug)
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"logged": 0, "confirmed": 0, "wrong": 0, "dnc": 0,
                                                            "no_reply": 0})
    sql = ("SELECT o.channel, o.outcome, COUNT(*) FROM outreach o JOIN properties p ON p.id = o.property_id "
           "WHERE p.market_id = ? GROUP BY o.channel, o.outcome")
    for channel, outcome, n in conn.execute(sql, (market["id"],)).fetchall():
        s = stats[channel]
        s["logged"] += n
        eff = effects.get(outcome, "none")
        key = {"confirm": "confirmed", "mark_wrong": "wrong", "dnc": "dnc"}.get(eff, "no_reply")
        s[key] += n
    return [{"channel": ch, **s, "confirm_rate": round(s["confirmed"] / s["logged"], 4) if s["logged"] else None}
            for ch, s in sorted(stats.items(), key=lambda kv: -kv[1]["logged"])]


def _join(values: list[str]) -> str:
    return "; ".join(v for v in values if v)


def crm_handoff_rows(conn: sqlite3.Connection, market_slug: str, *, include_handed_off: bool = True,
                     settings: Settings | None = None) -> list[dict]:
    """Confirmed owners (level 3) ready for the CRM: person, working contacts, property basics, notes.

    Owners marked do-not-contact are left out. Flat rows, ready for a CSV.
    """
    cfg = E.drill_config(settings)
    labels = {**E.RETIRED_OUTCOME_LABELS, **{k: v.label for k, v in cfg.validate.outcomes.items()}}
    market = market_by_slug(conn, market_slug)
    out = []
    for ctx in E.load_contexts(conn, market_id=market["id"], status="confirmed"):
        st = ctx.state
        if not st.get("confirmed_at") or (st.get("handed_off_at") and not include_handed_off):
            continue
        verified = [c for c in ctx.contacts if c["status"] == "verified"]
        untested = [c for c in ctx.contacts if c["status"] == "untested" and c["channel"] in E.REACH_CHANNELS]
        by_channel = {ch: _join([c["value"] for c in verified if c["channel"] == ch]) for ch in E.CHANNELS}
        mail = [c["value"] for c in ctx.contacts if c["channel"] == "mail" and c["status"] in ("verified", "untested")]
        last = ctx.outreach[-1] if ctx.outreach else None
        p = ctx.prop
        notes = [st.get("notes") or ""]
        notes += [f"{o['created_at'][:10]} {o['channel']}: {labels.get(o['outcome'], o['outcome'])}"
                  + (f" ({o['notes']})" if o.get("notes") else "") for o in ctx.outreach[-3:]]
        out.append({
            "property_id": ctx.id, "rank": (ctx.score or {}).get("rank"), "score": ctx.score_value,
            "confirmed_person": st.get("confirmed_person"), "confirmed_at": st.get("confirmed_at"),
            "handed_off_at": st.get("handed_off_at"),
            "phone": by_channel["phone"], "text": by_channel["text"], "email": by_channel["email"],
            "social": by_channel["social"], "vm": by_channel["vm"],
            "other_contacts_untested": _join([f"{c['channel']}: {c['value']}" for c in untested]),
            "mailing_address": mail[0] if mail else E.mailing_text(p),
            "owner_name": p.get("owner_name"), "owner_type": ctx.owner_type,
            "people": _join([f"{x['name']} ({(x.get('role') or 'other').replace('_', ' ')})" for x in ctx.people]),
            "address": p.get("address"), "city": p.get("city"), "state": p.get("state"), "zip": p.get("zip"),
            "county": p.get("county"), "apn": p.get("apn"), "property_type": p.get("property_type"),
            "units": p.get("units"), "building_sqft": p.get("building_sqft"), "year_built": p.get("year_built"),
            "crexi_url": p.get("crexi_url"),
            "last_outreach": f"{last['created_at'][:10]} {last['channel']}: {labels.get(last['outcome'], last['outcome'])}"
            if last else None,
            "notes": "\n".join(n for n in notes if n).strip(),
        })
    out.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["property_id"]))
    return out


def drill_report(conn: sqlite3.Connection, market_slug: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Everything the learning screen needs in one call (hit rates are counted once)."""
    rates = stage_hit_rates(conn, market_slug, settings=settings)
    return {"stages": stage_summary(conn, market_slug, settings=settings, rates=rates),
            "by_owner_type": rates,
            "advice": stage_advice(conn, market_slug, settings=settings, rates=rates),
            "outreach": outreach_rates(conn, market_slug, settings=settings)}
