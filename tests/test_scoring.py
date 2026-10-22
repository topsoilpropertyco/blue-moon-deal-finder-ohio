"""Buy box, Blue Moon score, ranking and learning. All data here is made up."""
from __future__ import annotations

import json
import math
from datetime import date
from itertools import count

import pytest

from bluemoon.db import market_by_slug, now_iso, one, rows, upsert
from bluemoon.scoring import buybox
from bluemoon.scoring.blue_moon import NO_DATA, coverage, probability, signals_for
from bluemoon.scoring.buybox import ScoringConfig, evaluate, rent_color, size_info, visual_color
from bluemoon.scoring.learn import blend, learned_weights, model_summary, set_outcome
from bluemoon.scoring.rank import compute_scores, rank_key

TODAY = date(2026, 10, 21)
_ids = count(1)
MARKET = {"max_drive_minutes": 60}


# --- helpers -------------------------------------------------------------------------------------

def prop(**kw) -> dict:
    """A clean in-buy-box apartment property; override any field."""
    base = {"id": 1, "state": "OH", "county": "CLARK", "city": "SPRINGFIELD", "property_type": "Multifamily",
            "units": 30, "building_sqft": 25000.0, "lot_acres": 2.0, "year_built": 1985, "flags_json": "[]",
            "owner_name": "DOE JOHN", "sale_date": "1995-01-01", "mailing_state": "OH",
            "mailing_city": "SPRINGFIELD", "improvement_value": 800000.0, "total_value": 1000000.0,
            "usps_vacant": 0, "preforeclosure": 0}
    base.update(kw)
    return base


def enrich(minutes=30.0, method="google_routes", final_p=None, status="done", fit="in", owner=None) -> dict:
    return {
        "drive": None if minutes is None else {"minutes": minutes, "method": method},
        "visual": None if status is None else {"status": status, "final_p": final_p},
        "rent": None if fit is None else {"fit": fit, "detail": "2BR HUD rent is close to the scale"},
        "owner": owner,
    }


def by_key(signals: list[dict]) -> dict[str, dict]:
    return {s["key"]: s for s in signals}


def add_property(conn, market_slug: str, **fields) -> int:
    m = market_by_slug(conn, market_slug)
    n = next(_ids)
    data = {"market_id": m["id"], "property_key": f"test:{n}", "state": "OH", "county": "CLARK",
            "city": "SPRINGFIELD", "property_type": "Multifamily", "units": 30, "building_sqft": 25000.0,
            "year_built": 1985, "lat": 39.92 + 0.01 * (n % 50), "lng": -83.80,
            "raw_json": "{}", "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso()}
    data.update(fields)
    cur = conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                       tuple(data.values()))
    conn.commit()
    return cur.lastrowid


def add_drive(conn, pid: int, minutes: float, method: str = "google_routes") -> None:
    upsert(conn, "drive_times", "property_id", {"property_id": pid, "minutes": minutes, "meters": None,
                                                "method": method, "origin_key": "x", "fetched_at": now_iso()})
    conn.commit()


def add_visual(conn, pid: int, final_p: float | None, status: str = "done") -> None:
    upsert(conn, "visual_checks", "property_id", {"property_id": pid, "status": status, "final_p": final_p})
    conn.commit()


def score_row(conn, pid: int) -> dict:
    return one(conn, "SELECT * FROM scores WHERE property_id=?", (pid,))


@pytest.fixture
def ac(settings):
    return settings.asset_class("apartments")


# --- size ----------------------------------------------------------------------------------------

def test_size_hierarchy_for_apartments(ac):
    assert size_info(prop(units=30, building_sqft=25000), ac).basis == "units+sqft"
    assert size_info(prop(units=30, building_sqft=None), ac).basis == "units"
    est = size_info(prop(units=None, building_sqft=24000), ac)
    assert (est.basis, est.value, est.estimated) == ("sqft_estimate", 28.0, True)
    assert "850 sq ft per unit" in est.detail and est.short == "about 28 units"
    unknown = size_info(prop(units=None, building_sqft=None), ac)
    assert (unknown.basis, unknown.value, unknown.color, unknown.bucket) == ("unknown", None, "na", "unknown")
    addr = size_info(prop(units=12, flags_json=json.dumps(["units_from_addresses"])), ac)
    assert addr.basis == "address_estimate" and addr.estimated


@pytest.mark.parametrize("units, bucket, color", [
    (20, "ideal", "green"), (50, "ideal", "green"), (35, "ideal", "green"),
    (10, "ok", "yellow"), (19, "ok", "yellow"), (80, "ok", "yellow"),
    (9, "outside", "red"), (81, "outside", "red"), (240, "outside", "red"),
])
def test_size_buckets(ac, units, bucket, color):
    s = size_info(prop(units=units), ac)
    assert (s.bucket, s.color) == (bucket, color)


def test_size_other_metrics(settings):
    storage = settings.asset_class("self_storage")
    s = size_info(prop(units=None, building_sqft=45000), storage)
    assert (s.basis, s.value, s.bucket) == ("sqft", 45000, "ideal")
    land = settings.asset_class("raw_land")
    s = size_info(prop(lot_acres=12.5), land)
    assert (s.basis, s.bucket) == ("acres", "ideal")
    assert size_info(prop(lot_acres=None), land).basis == "unknown"
    mhp = settings.asset_class("mobile_home_park")
    s = size_info(prop(units=None, lot_acres=9.0), mhp)
    assert (s.basis, s.value, s.estimated) == ("acres_estimate", 60.0, True)


# --- buy box filters -----------------------------------------------------------------------------

def test_clean_property_is_in_the_buybox(ac, settings):
    ev = evaluate(prop(), enrich(), ac, MARKET, settings)
    assert ev["in_buybox"] is True and ev["exclusion_reasons"] == []
    assert (ev["size_value"], ev["size_basis"], ev["size_color"]) == (30, "units+sqft", "green")
    assert ev["drive_minutes"] == 30


def test_drive_time_filter(ac, settings):
    assert evaluate(prop(), enrich(minutes=61), ac, MARKET, settings)["exclusion_reasons"] == ["drive_time"]
    assert evaluate(prop(), enrich(minutes=60), ac, MARKET, settings)["in_buybox"]
    ev = evaluate(prop(), enrich(minutes=None), ac, MARKET, settings)
    assert ev["in_buybox"] and "drive_unknown" in ev["flags"]
    ev = evaluate(prop(), enrich(minutes=75, method="estimate"), ac, MARKET, settings)
    assert ev["exclusion_reasons"] == ["drive_time"] and "about 75 min" in ev["exclusion_details"]["drive_time"]
    # Halves round up, like the screens (and the set-aside groups): 90.5 reads "91 min", not "90 min".
    ev = evaluate(prop(), enrich(minutes=90.5), ac, MARKET, settings)
    assert ev["exclusion_details"]["drive_time"].startswith("91 min drive")
    # A market row from the database works as well as a config object.
    assert evaluate(prop(), enrich(minutes=61), ac, {"max_drive_minutes": 90}, settings)["in_buybox"]


def test_year_filter(ac, settings):
    assert evaluate(prop(year_built=1969), enrich(), ac, MARKET, settings)["exclusion_reasons"] == ["year"]
    assert evaluate(prop(year_built=1970), enrich(), ac, MARKET, settings)["in_buybox"]
    ev = evaluate(prop(year_built=None), enrich(), ac, MARKET, settings)
    assert ev["in_buybox"] and "year_unknown" in ev["flags"]
    storage = settings.asset_class("self_storage")      # no year limit
    old_storage = prop(year_built=1950, building_sqft=45000, property_type="Self Storage")
    assert evaluate(old_storage, enrich(), storage, MARKET, settings)["in_buybox"]


def test_size_filter_never_excludes_estimates_or_unknowns(ac, settings):
    assert evaluate(prop(units=6, building_sqft=5000), enrich(), ac, MARKET, settings)["exclusion_reasons"] == ["size"]
    assert evaluate(prop(units=240), enrich(), ac, MARKET, settings)["exclusion_reasons"] == ["size"]
    est = evaluate(prop(units=None, building_sqft=8000), enrich(), ac, MARKET, settings)  # ~9 units
    assert est["in_buybox"] and est["size_basis"] == "sqft_estimate" and est["size_color"] == "red"
    big_est = evaluate(prop(units=None, building_sqft=250000), enrich(), ac, MARKET, settings)
    assert big_est["in_buybox"] and "size_estimated" in big_est["flags"]
    unknown = evaluate(prop(units=None, building_sqft=None), enrich(), ac, MARKET, settings)
    assert unknown["in_buybox"] and unknown["size_color"] == "na" and "size_unknown" in unknown["flags"]


def test_state_county_and_type_filters(ac, settings):
    assert evaluate(prop(state="KY"), enrich(), ac, MARKET, settings)["exclusion_reasons"] == ["state"]
    assert evaluate(prop(state="IN", county="WAYNE"), enrich(), ac, MARKET, settings)["in_buybox"]
    assert evaluate(prop(county="FAIRFIELD"), enrich(), ac, MARKET, settings)["exclusion_reasons"] == ["skip_county"]
    assert evaluate(prop(county="Fairfield County"), enrich(), ac, MARKET, settings)["exclusion_reasons"] == \
        ["skip_county"]
    # FRANKLIN is skipped in Indiana only.
    assert evaluate(prop(county="FRANKLIN"), enrich(), ac, MARKET, settings)["in_buybox"]
    assert evaluate(prop(state="IN", county="FRANKLIN"), enrich(), ac, MARKET, settings)["exclusion_reasons"] == \
        ["skip_county"]
    ev = evaluate(prop(state=None), enrich(), ac, MARKET, settings)
    assert ev["in_buybox"] and "state_unknown" in ev["flags"]
    assert evaluate(prop(property_type="Retail"), enrich(), ac, MARKET, settings)["exclusion_reasons"] == \
        ["property_type"]
    assert evaluate(prop(property_type="Mixed Use Multifamily Retail"), enrich(), ac, MARKET, settings)["in_buybox"]
    assert evaluate(prop(property_type="Apartments"), enrich(), ac, MARKET, settings)["in_buybox"]
    ev = evaluate(prop(property_type=None), enrich(), ac, MARKET, settings)
    assert ev["in_buybox"] and "type_unknown" in ev["flags"]


def test_property_type_matches_at_word_start(settings):
    rv = settings.asset_class("campground_rv")
    assert buybox.property_type_matches("RV Park", rv) is True
    assert buybox.property_type_matches("Campground", rv) is True
    assert buybox.property_type_matches("Service Station", rv) is False   # "rv" inside "Service"
    storage = settings.asset_class("self_storage")
    assert buybox.property_type_matches("Self-Storage", storage) is True
    assert buybox.property_type_matches("", storage) is None


def test_several_reasons_are_all_listed(ac, settings):
    ev = evaluate(prop(units=4, building_sqft=3400, year_built=1950, county="FAIRFIELD", property_type="Office"),
                  enrich(minutes=90),
                  ac, MARKET, settings)
    assert ev["exclusion_reasons"] == ["drive_time", "year", "size", "skip_county", "property_type"]
    assert set(ev["exclusion_details"]) == set(ev["exclusion_reasons"])


def test_too_few_units_for_the_building_uses_the_sqft_estimate(ac, settings):
    ev = evaluate(prop(units=2, building_sqft=24000), enrich(), ac, MARKET, settings)
    assert ev["in_buybox"] and ev["size_basis"] == "sqft_estimate" and ev["size_value"] == 28
    assert {"size_check", "size_estimated"} <= set(ev["flags"]) and "says 2 units" in ev["size_detail"]
    tiny = evaluate(prop(units=1, building_sqft=5000), enrich(), ac, MARKET, settings)   # ~6 units: still too small
    assert tiny["exclusion_reasons"] == ["size"] and "size_check" in tiny["flags"]


def test_units_vs_sqft_mismatch_is_flagged(ac, settings):
    assert "size_check" in evaluate(prop(units=30, building_sqft=200000), enrich(), ac, MARKET, settings)["flags"]
    assert "size_check" not in evaluate(prop(), enrich(), ac, MARKET, settings)["flags"]


# --- colors --------------------------------------------------------------------------------------

@pytest.mark.parametrize("visual, expected", [
    (None, "pending"),
    ({"status": "pending", "final_p": None}, "pending"),
    ({"status": "error", "final_p": None}, "pending"),
    ({"status": "done", "final_p": None}, "na"),
    ({"status": "no_imagery", "final_p": None}, "na"),
    ({"status": "done", "final_p": 80}, "green"),
    ({"status": "done", "final_p": 79.9}, "yellow"),
    ({"status": "done", "final_p": 21}, "yellow"),
    ({"status": "done", "final_p": 20}, "red"),
    ({"status": "done", "final_p": 0}, "red"),
    ({"status": "no_imagery", "final_p": 100}, "green"),   # reviewed by hand
])
def test_visual_color(ac, visual, expected):
    assert visual_color(visual, ac, ScoringConfig()) == expected


def test_visual_and_rent_colors_off_when_not_used(settings):
    land = settings.asset_class("raw_land")
    assert visual_color({"status": "done", "final_p": 90}, land, ScoringConfig()) is None
    assert rent_color({"fit": "in"}, land) is None


@pytest.mark.parametrize("rent, expected", [
    (None, "pending"), ({"fit": "in"}, "green"), ({"fit": "near"}, "yellow"), ({"fit": "out"}, "red"),
    ({"fit": "na"}, "na"), ({"fit": None}, "na"),
])
def test_rent_color(ac, rent, expected):
    assert rent_color(rent, ac) == expected


# --- signals -------------------------------------------------------------------------------------

def test_signal_keys_follow_the_asset_class(settings):
    apt = signals_for(prop(), enrich(), settings.asset_class("apartments"), settings, today=TODAY)
    assert [s["key"] for s in apt] == ["size", "visual", "rent_fit", "drive", "owner_type", "years_owned", "debt",
                                       "absentee", "vacancy", "condition", "distress"]
    storage = signals_for(prop(building_sqft=45000), enrich(), settings.asset_class("self_storage"), settings,
                          today=TODAY)
    assert "rent_fit" not in [s["key"] for s in storage] and "visual" in [s["key"] for s in storage]
    land = signals_for(prop(lot_acres=10), enrich(), settings.asset_class("raw_land"), settings, today=TODAY)
    keys = [s["key"] for s in land]
    assert "visual" not in keys and "rent_fit" not in keys
    assert by_key(land)["condition"]["multiplier"] == 1.0          # raw_land override in YAML
    for s in apt:
        assert set(s) == {"key", "label", "value", "bucket", "multiplier", "why"}
        assert s["label"] and s["why"]


def test_missing_data_is_skipped_not_penalized(ac, settings):
    empty = {"id": 2, "flags_json": "[]"}
    sigs = signals_for(empty, {}, ac, settings, today=TODAY)
    assert all(s["multiplier"] is None for s in sigs)
    assert all(s["why"].startswith(NO_DATA) for s in sigs)
    assert coverage(sigs) == (0, len(sigs))
    assert probability(sigs, 0.015) == pytest.approx(0.015)


def test_signal_buckets_and_multipliers(ac, settings):
    p = prop(owner_name="HOLLOWAY FAMILY TRUST", sale_date="1990-01-01", mailing_state="FL", mailing_city="MIAMI",
             usps_vacant=1, improvement_value=300000.0, total_value=1000000.0, preforeclosure=1, units=35)
    sigs = by_key(signals_for(p, enrich(minutes=25, final_p=85, fit="near"), ac, settings, today=TODAY))
    expect = {"size": ("ideal", 1.3), "visual": ("green", 1.4), "rent_fit": ("near", 1.0), "drive": ("near", 1.1),
              "owner_type": ("trust", 2.0), "years_owned": ("20+", 2.0), "debt": ("free_and_clear", 1.6),
              "absentee": ("outofstate", 1.4), "vacancy": ("vacant", 1.5), "condition": ("worn", 1.3),
              "distress": ("preforeclosure", 2.0)}
    for k, (bucket, m) in expect.items():
        assert (sigs[k]["bucket"], sigs[k]["multiplier"]) == (bucket, m), k
    assert "raises the odds" in sigs["owner_type"]["why"]
    assert sigs["absentee"]["value"] == "Out of state"


@pytest.mark.parametrize("final_p, bucket, m", [(85, "green", 1.4), (60, "yellow_high", 1.1),
                                                (35, "yellow_low", 0.8), (10, "red", 0.4)])
def test_visual_buckets(ac, settings, final_p, bucket, m):
    s = by_key(signals_for(prop(), enrich(final_p=final_p), ac, settings, today=TODAY))["visual"]
    assert (s["bucket"], s["multiplier"]) == (bucket, m)


@pytest.mark.parametrize("minutes, bucket", [(10, "near"), (30, "near"), (31, "mid"), (50, "mid"), (55, "far")])
def test_drive_buckets(ac, settings, minutes, bucket):
    assert by_key(signals_for(prop(), enrich(minutes=minutes), ac, settings, today=TODAY))["drive"]["bucket"] == bucket


@pytest.mark.parametrize("sale_date, bucket", [("2024-01-01", "0-4"), ("2019-01-01", "5-9"),
                                               ("2016-10-21", "10-19"), ("2006-10-21", "20+")])
def test_years_buckets(ac, settings, sale_date, bucket):
    s = by_key(signals_for(prop(sale_date=sale_date), enrich(), ac, settings, today=TODAY))["years_owned"]
    assert s["bucket"] == bucket


def test_owner_row_wins_over_name_rules(ac, settings):
    owner = {"owner_type": "portfolio", "portfolio_count": 7, "years_owned": 3.0, "debt_status": "active",
             "absentee": "local"}
    sigs = by_key(signals_for(prop(owner_name="DOE JOHN"), enrich(owner=owner), ac, settings, today=TODAY))
    assert (sigs["owner_type"]["bucket"], sigs["owner_type"]["multiplier"]) == ("portfolio", 0.6)
    assert "7 properties" in sigs["owner_type"]["why"]
    assert sigs["years_owned"]["bucket"] == "0-4"
    assert sigs["debt"]["bucket"] == "active"
    unknown = by_key(signals_for(prop(), enrich(owner={"owner_type": "unknown", "debt_status": "unknown",
                                                         "absentee": "unknown"}), ac, settings, today=TODAY))
    assert unknown["owner_type"]["multiplier"] is None
    assert unknown["debt"]["multiplier"] is None
    assert unknown["absentee"]["multiplier"] is None


def test_estimated_size_counts_for_less(ac, settings):
    known = by_key(signals_for(prop(units=120), enrich(), ac, settings, today=TODAY))["size"]
    est = by_key(signals_for(prop(units=None, building_sqft=102000), enrich(), ac, settings, today=TODAY))["size"]
    assert known["multiplier"] == 0.5
    assert est["bucket"] == "outside" and est["multiplier"] == pytest.approx(math.sqrt(0.5), abs=1e-3)
    assert "estimate" in est["why"]


def test_condition_threshold_comes_from_config(tmp_root, settings):
    from bluemoon.config import get_settings
    p = prop(improvement_value=400000.0, total_value=1000000.0)   # 40%
    ac = settings.asset_class("apartments")
    assert by_key(signals_for(p, enrich(), ac, settings, today=TODAY))["condition"]["bucket"] == "normal"
    import re
    text = re.sub(r"worn_improvement_ratio: [0-9.]+", "worn_improvement_ratio: 0.5", (tmp_root / "config.yaml").read_text())
    if "worn_improvement_ratio" not in text:
        text = text.replace("scoring:\n", "scoring:\n  worn_improvement_ratio: 0.5\n", 1)
    (tmp_root / "config.yaml").write_text(text)
    get_settings.cache_clear()
    s2 = get_settings()
    assert by_key(signals_for(p, enrich(), s2.asset_class("apartments"), s2, today=TODAY))["condition"]["bucket"] \
        == "worn"


def test_custom_weights_replace_the_yaml_multipliers(ac, settings):
    w = json.loads(json.dumps(ac.signals))
    w["owner_type"]["individual"] = 3.0
    s = by_key(signals_for(prop(), enrich(), ac, settings, weights=w, today=TODAY))["owner_type"]
    assert s["multiplier"] == 3.0


def test_probability_math():
    assert probability([], 0.015) == pytest.approx(0.015)
    two = probability([{"multiplier": 2.0}], 0.015)
    odds = 0.015 / 0.985 * 2
    assert two == pytest.approx(odds / (1 + odds))
    assert probability([{"multiplier": None}, {"multiplier": 2.0}], 0.015) == pytest.approx(two)
    assert probability([{"multiplier": 0.5}, {"multiplier": 2.0}], 0.015) == pytest.approx(0.015)
    assert probability([{"multiplier": 1e9}], 0.015) < 1.0
    assert probability([{"multiplier": 0.0}], 0.015) == 0.0


# --- compute_scores ------------------------------------------------------------------------------

def test_compute_scores_writes_every_property_and_ranks_the_buybox(conn, settings, market_slug):
    good = add_property(conn, market_slug, owner_name="DOE JOHN", sale_date="1990-01-01")
    okay = add_property(conn, market_slug, owner_name="SAMPLE APARTMENTS LLC", sale_date="2020-01-01",
                        lender="FIRST TEST BANK", loan_maturity="2040-01-01")
    far = add_property(conn, market_slug, owner_name="DOE JOHN", sale_date="1990-01-01")
    old = add_property(conn, market_slug, year_built=1950)
    for pid in (good, okay, old):
        add_drive(conn, pid, 30)
    add_drive(conn, far, 95)

    res = compute_scores(conn, market_slug, today=TODAY)

    assert res["properties"] == 4 and res["in_buybox"] == 2 and res["excluded"] == 2
    assert res["exclusion_counts"]["drive_time"] == 1 and res["exclusion_counts"]["year"] == 1
    assert res["owners_computed"] is True                 # owner step ran because it was missing
    assert score_row(conn, good)["rank"] == 1 and score_row(conn, okay)["rank"] == 2
    for pid in (far, old):
        r = score_row(conn, pid)
        assert r["rank"] is None and r["in_buybox"] == 0 and r["score"] is not None
    assert json.loads(score_row(conn, far)["exclusion_reasons"]) == ["drive_time"]
    r = score_row(conn, good)
    sigs = json.loads(r["signals_json"])
    assert r["coverage_total"] == len(sigs) and r["coverage_have"] == sum(s["multiplier"] is not None for s in sigs)
    assert r["visual_color"] == "pending" and r["rent_color"] == "pending" and r["size_color"] == "green"
    assert res["score"]["count"] == 4 and res["score_in_buybox"]["count"] == 2
    assert res["score"]["min"] <= res["score"]["median"] <= res["score"]["p90"] <= res["score"]["max"]
    assert "nothing was deleted" in res["message"]
    assert res["weights"] == "starting"


def test_rank_ties_break_on_photo_then_drive(conn, settings, market_slug):
    same = {"owner_name": "SAMPLE APARTMENTS LLC", "sale_date": "2020-01-01"}
    a = add_property(conn, market_slug, **same)
    b = add_property(conn, market_slug, **same)
    c = add_property(conn, market_slug, **same)
    # Same score bucket for photos (both green) but different final_p; same drive bucket, different minutes.
    add_visual(conn, a, 85)
    add_visual(conn, b, 95)
    add_visual(conn, c, 95)
    add_drive(conn, a, 20)
    add_drive(conn, b, 25)
    add_drive(conn, c, 15)
    compute_scores(conn, market_slug, today=TODAY)
    assert [score_row(conn, p)["rank"] for p in (c, b, a)] == [1, 2, 3]


def test_rank_key_orders_missing_values_last():
    items = [{"property_id": 1, "score": 0.02, "visual_p": None, "drive_minutes": None},
             {"property_id": 2, "score": 0.02, "visual_p": 50, "drive_minutes": None},
             {"property_id": 3, "score": 0.02, "visual_p": 50, "drive_minutes": 10},
             {"property_id": 4, "score": 0.03, "visual_p": None, "drive_minutes": None}]
    assert [i["property_id"] for i in sorted(items, key=rank_key)] == [4, 3, 2, 1]


def test_compute_scores_is_idempotent(conn, settings, market_slug):
    pids = [add_property(conn, market_slug, owner_name=f"OWNER {i} LLC", sale_date="2001-01-01") for i in range(5)]
    for i, pid in enumerate(pids):
        add_drive(conn, pid, 20 + i)
    first = compute_scores(conn, market_slug, today=TODAY)
    snap = rows(conn, "SELECT property_id, in_buybox, score, rank, signals_json FROM scores ORDER BY property_id")
    second = compute_scores(conn, market_slug, today=TODAY)
    again = rows(conn, "SELECT property_id, in_buybox, score, rank, signals_json FROM scores ORDER BY property_id")
    assert snap == again
    assert first["in_buybox"] == second["in_buybox"] == 5
    assert second["owners_computed"] is False
    assert rows(conn, "SELECT COUNT(*) AS n FROM scores")[0]["n"] == 5


def test_compute_scores_empty_market(conn, settings, market_slug):
    res = compute_scores(conn, market_slug)
    assert res["properties"] == 0 and res["in_buybox"] == 0
    assert "Import" in res["message"]


def test_compute_scores_reports_progress(conn, settings, market_slug):
    add_property(conn, market_slug)
    seen: list[float] = []
    compute_scores(conn, market_slug, progress=lambda f, m: seen.append(f))
    assert seen[0] == 0.0 and seen[-1] == 1.0 and seen == sorted(seen)


def test_compute_scores_survives_a_broken_progress_callback(conn, settings, market_slug):
    add_property(conn, market_slug)

    def boom(f, m):
        raise RuntimeError("display went away")

    assert compute_scores(conn, market_slug, progress=boom)["properties"] == 1


# --- learning ------------------------------------------------------------------------------------

def test_blend_with_no_outcomes_returns_the_start():
    start = {"a": 2.0, "b": 1.0, "c": 0.5}
    out = blend(start, {}, {"a": 10, "b": 50, "c": 40}, 30)
    assert {k: v["learned"] for k, v in out.items()} == start


def test_blend_moves_toward_the_evidence_and_prior_strength_slows_it():
    start = {"a": 1.0, "b": 1.0}
    ref = {"a": 50, "b": 50}
    pos = {"a": 20, "b": 0}
    weak = blend(start, pos, ref, prior_strength=5)
    strong = blend(start, pos, ref, prior_strength=500)
    assert weak["a"]["learned"] > strong["a"]["learned"] > 1.0
    assert weak["b"]["learned"] < strong["b"]["learned"] < 1.0
    huge = blend(start, {"a": 100000, "b": 100000}, ref, prior_strength=30)
    assert huge["a"]["learned"] == pytest.approx(1.0, abs=0.01)   # evidence agrees with the start


def test_blend_clips_extremes_and_handles_empty_reference_buckets():
    out = blend({"a": 1.0, "b": 1.0}, {"a": 1000}, {"b": 100}, prior_strength=1, clip=(0.1, 5.0))
    assert out["a"]["learned"] == 5.0 and out["b"]["learned"] >= 0.1


def test_learned_weights_without_outcomes(conn, settings, market_slug):
    add_property(conn, market_slug)
    lw = learned_weights(conn, market_slug, settings=settings)
    ac = settings.asset_class("apartments")
    assert lw["n"] == 0 and lw["positives"] == 0 and lw["learned"] is False
    assert lw["weights"] == ac.signals
    assert "No outcomes" in lw["message"]


def test_learned_weights_with_outcomes_change_the_score(conn, settings, market_slug):
    individuals = [add_property(conn, market_slug, owner_name=f"DOE{chr(65 + i)} JOHN", sale_date="2001-01-01")
                   for i in range(12)]
    llcs = [add_property(conn, market_slug, owner_name=f"SAMPLE {i} LLC", sale_date="2001-01-01") for i in range(40)]
    for pid in individuals + llcs:
        add_drive(conn, pid, 30)
    compute_scores(conn, market_slug, today=TODAY)
    before = score_row(conn, individuals[0])["score"]

    for pid in individuals[:10]:
        set_outcome(conn, pid, "blue_moon", "made-up good deal")
    set_outcome(conn, llcs[0], "passed")
    lw = learned_weights(conn, market_slug, settings=settings, today=TODAY)
    assert lw["learned"] is True and lw["n"] == 11 and lw["positives"] == 10
    start = settings.asset_class("apartments").signals["owner_type"]
    assert lw["weights"]["owner_type"]["individual"] > start["individual"]
    assert lw["weights"]["owner_type"]["small_llc"] < start["small_llc"]
    assert lw["weights"]["drive"]["near_max"] == 30          # settings inside a signal are kept
    assert lw["changes"] and lw["changes"][0]["signal"]

    res = compute_scores(conn, market_slug, today=TODAY)
    assert res["weights"] == "learned" and res["outcomes"] == 11
    assert score_row(conn, individuals[0])["score"] > before


def test_only_negative_outcomes_keep_the_starting_weights(conn, settings, market_slug):
    pid = add_property(conn, market_slug)
    set_outcome(conn, pid, "passed")
    lw = learned_weights(conn, market_slug, settings=settings)
    assert lw["learned"] is False and lw["n"] == 1 and lw["positives"] == 0
    assert lw["weights"] == settings.asset_class("apartments").signals


def test_outcomes_pool_across_markets_of_the_same_asset_class(tmp_root, settings):
    from bluemoon.config import get_settings
    from bluemoon.db import connect
    text = (tmp_root / "config.yaml").read_text().replace(
        "markets:\n", "markets:\n  - slug: apartments-dayton-oh\n    asset_class: apartments\n    geography: Dayton, OH\n"
                      "    origin: {label: Dayton, lat: 39.76, lng: -84.19}\n    max_drive_minutes: 45\n", 1)
    (tmp_root / "config.yaml").write_text(text)
    get_settings.cache_clear()
    s2 = get_settings()
    c = connect(settings=s2)
    try:
        pid = add_property(c, "apartments-dayton-oh", owner_name="DOE JOHN")
        add_property(c, "apartments-springfield-oh", owner_name="SAMPLE LLC")
        set_outcome(c, pid, "bought")
        lw = learned_weights(c, "apartments-springfield-oh", settings=s2)
        assert lw["positives"] == 1 and lw["learned"] is True
    finally:
        c.close()


def test_set_outcome_validates_and_clears(conn, settings, market_slug):
    pid = add_property(conn, market_slug)
    assert set_outcome(conn, pid, "Bought")["outcome"] == "bought"
    assert one(conn, "SELECT outcome FROM outcomes WHERE property_id=?", (pid,))["outcome"] == "bought"
    with pytest.raises(ValueError):
        set_outcome(conn, pid, "maybe")
    with pytest.raises(KeyError):
        set_outcome(conn, 999999, "bought")
    assert set_outcome(conn, pid, None)["outcome"] is None
    assert one(conn, "SELECT * FROM outcomes WHERE property_id=?", (pid,)) is None


def test_model_summary(conn, settings, market_slug):
    add_property(conn, market_slug)
    compute_scores(conn, market_slug)
    m = model_summary(conn, market_slug, settings=settings)
    assert m["base_rate"] == 0.015 and m["learned"] is False and m["outcomes"]["total"] == 0
    keys = [s["key"] for s in m["signals"]]
    assert keys[0] == "size" and "owner_type" in keys
    owner = next(s for s in m["signals"] if s["key"] == "owner_type")
    assert {b["bucket"] for b in owner["buckets"]} >= {"trust", "individual", "portfolio"}
    assert all(b["start"] == b["learned"] for b in owner["buckets"])
    drive = next(s for s in m["signals"] if s["key"] == "drive")
    assert {b["bucket"] for b in drive["buckets"]} == {"near", "mid", "far"}   # near_max/mid_max are settings
    assert m["scored"]["properties"] == 1 and m["how_it_works"]


# --- end to end with the made-up Crexi sample ----------------------------------------------------

def test_sample_csv_end_to_end(conn, settings, market_slug, sample_csv):
    from bluemoon.enrich.owners import compute_owners
    from bluemoon.ingest.crexi import import_file
    import_file(conn, market_slug, sample_csv)
    compute_owners(conn, market_slug, today=TODAY)
    res = compute_scores(conn, market_slug, today=TODAY)

    def by_address(addr: str) -> dict:
        return one(conn, "SELECT p.address, o.owner_type, o.absentee, s.* FROM properties p "
                         "JOIN owners o ON o.property_id=p.id JOIN scores s ON s.property_id=p.id "
                         "WHERE p.address=?", (addr,))

    assert by_address("1200 SAMPLE RIDGE DR")["owner_type"] == "trust"
    assert by_address("77 FAKE MEADOW CT")["owner_type"] == "individual"
    reit = by_address("1 PLACEHOLDER TOWER")
    assert reit["owner_type"] == "institutional" and "size" in json.loads(reit["exclusion_reasons"])
    old_small = json.loads(by_address("22 OLD BUILD ST")["exclusion_reasons"])
    assert "year" in old_small and "size" in old_small
    assert json.loads(by_address("5 SKIPPED COUNTY RD")["exclusion_reasons"]) == ["skip_county"]
    assert by_address("10 HOOSIER WAY")["in_buybox"] == 1
    estimate = by_address("79 FAKE MEADOW CT")    # no unit count; 8 addresses -> about 8 units (an estimate)
    assert estimate["size_basis"] == "address_estimate" and estimate["size_color"] == "red"
    assert estimate["in_buybox"] == 1             # estimates never exclude
    estate = by_address("999 FARAWAY LN")
    assert estate["owner_type"] == "estate" and estate["absentee"] == "outofstate"
    assert res["properties"] == res["in_buybox"] + res["excluded"]
    ranks = sorted(r["rank"] for r in rows(conn, "SELECT rank FROM scores WHERE rank IS NOT NULL"))
    assert ranks == list(range(1, res["in_buybox"] + 1))


# --- real export (local only) --------------------------------------------------------------------

def test_real_export_scores(conn, settings, market_slug, real_crexi):
    from bluemoon.ingest.crexi import import_file
    import_file(conn, market_slug, real_crexi)
    res = compute_scores(conn, market_slug)
    assert res["properties"] == res["in_buybox"] + res["excluded"] > 0
    n = rows(conn, "SELECT COUNT(*) AS n FROM scores")[0]["n"]
    assert n == res["properties"]
    assert all(0 <= r["score"] <= 1 for r in rows(conn, "SELECT score FROM scores"))
