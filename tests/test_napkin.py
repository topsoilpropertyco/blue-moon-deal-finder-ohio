"""The rent napkin, rent records, the Blue Moon price block, prior-purchase groups, age and history, and their
routes. Made-up properties only."""
from __future__ import annotations

import itertools
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from bluemoon import history, napkin, pricing
from bluemoon.db import dumps, market_by_slug, now_iso, one, rows

MARKET = "apartments-springfield-oh"
HUD = {"0": 800, "1": 1000, "2": 1650, "3": 1900, "4": 2100}
CFG = pricing.load_config({"evaluation_date": date(2026, 11, 5), "default_unit_mix": {"apartments": {0: 10, 1: 30, 2: 50, 3: 10}}})
_ids = itertools.count(1)


def add_prop(conn, market=MARKET, rents=HUD, **kw) -> int:
    n = next(_ids)
    data = {"market_id": market_by_slug(conn, market)["id"], "property_key": f"napkin:{n}", "address": f"{n} NAPKIN AVE",
            "city": "SPRINGFIELD", "state": "OH", "units": 32, "raw_json": "{}", "flags_json": "[]",
            "created_at": now_iso(), "updated_at": now_iso()}
    data.update(kw)
    pid = int(conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                           tuple(data.values())).lastrowid)
    if rents:
        conn.execute("INSERT INTO rent_fits(property_id, source, area_name, fiscal_year, rents_json, fit, detail, computed_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (pid, "hud_arcgis", "Clark County", "FY2026", dumps(rents), "near",
                                                  "Area 2BR rent $1,650 vs Brad's $1,200 (38% over)", now_iso()))
    conn.commit()
    return pid


def prop_of(conn, pid):
    return one(conn, "SELECT * FROM properties WHERE id = ?", (pid,))


def block(conn, pid):
    return napkin.block(conn, prop_of(conn, pid), "apartments", CFG, one(conn, "SELECT * FROM rent_fits WHERE property_id = ?", (pid,)))


# --- layers 1 to 3 -------------------------------------------------------------------------------------

def test_default_mix_gives_whole_units_that_add_up():
    shares = napkin.default_shares(CFG, "apartments")
    assert napkin.allocate(32, shares) == {0: 3, 1: 10, 2: 16, 3: 3}
    assert sum(napkin.allocate(7, shares).values()) == 7 and sum(napkin.allocate(1, shares).values()) == 1


def test_estimate_from_units_default_mix_and_hud_rents(conn):
    pid = add_prop(conn)
    b = block(conn, pid)
    e = b["estimate"]
    assert [(ln["label"], ln["units"], ln["rent_cents"], ln["rent_source"]) for ln in e["lines"]] == [
        ("Studio", 3, 80000, "hud"), ("1 BR", 10, 100000, "hud"), ("2 BR", 16, 165000, "hud"), ("3 BR", 3, 190000, "hud")]
    assert (e["monthly_cents"], e["annual_cents"], e["mix_source"]) == (4450000, 53400000, "default")
    assert (b["rent"]["source"], b["rent"]["label"], b["rent"]["confidence"], b["rent"]["basis"]) == (
        "estimate", "Estimated", "Estimated", "estimated_market")
    assert [s["cents"] for s in b["scenarios"]] == [213600000, 267000000, 320400000]
    assert b["reference_cents"] == 213600000


def test_typed_inputs_make_it_partly_known_and_rerun_the_price(conn):
    pid = add_prop(conn)
    napkin.save_assumptions(conn, pid, {"mix": {"1": 12, "2": 20}, "rents": {"2": "1,500"}}, "seth")
    b = block(conn, pid)
    assert b["estimate"]["monthly_cents"] == 12 * 100000 + 20 * 150000
    assert (b["estimate"]["mix_source"], b["rent"]["confidence"]) == ("typed", "Partly known")
    assert [ln["rent_source"] for ln in b["estimate"]["lines"]] == ["hud", "typed"]
    napkin.save_assumptions(conn, pid, {"mix": None, "rents": None}, "seth")
    assert block(conn, pid)["rent"]["confidence"] == "Estimated"
    assert one(conn, "SELECT * FROM rent_assumptions WHERE property_id = ?", (pid,)) is None


def test_typed_mix_that_disagrees_with_the_unit_count_warns_but_is_used(conn):
    pid = add_prop(conn, units=10)
    napkin.save_assumptions(conn, pid, {"mix": {"2": 8}}, "seth")
    e = block(conn, pid)["estimate"]
    assert e["units"] == 8 and e["available"] and "adds up to 8" in e["warnings"][0]


def test_missing_information_is_unknown_never_zero(conn):
    no_hud = add_prop(conn, rents=None)
    b = block(conn, no_hud)
    assert b["rent"]["annual_cents"] is None and b["scenarios"] == [] and b["reference_cents"] is None
    assert "No rent for" in b["estimate"]["reason"]
    no_units = add_prop(conn, units=None)
    assert "unit count is not known" in block(conn, no_units)["estimate"]["reason"]
    napkin.save_assumptions(conn, no_units, {"units": 20}, "seth")
    assert block(conn, no_units)["estimate"]["available"]
    no_mix = napkin.estimate(10, {}, {2: 100000}, None)
    assert not no_mix["available"] and "no assumed bedroom mix" in no_mix["reason"]
    assert block(conn, no_hud)["prior"]["state"] == "unavailable"


def test_assumption_inputs_are_checked(conn):
    pid = add_prop(conn)
    for bad in ({"units": 0}, {"units": True}, {"mix": {"x": 1}}, {"mix": {"10": 1}}, {"mix": {"1": -1}},
                {"rents": {"2": 0}}, {"rents": {"2": "abc"}}):
        with pytest.raises(ValueError):
            napkin.save_assumptions(conn, pid, bad, "seth")


# --- layer 4: records ------------------------------------------------------------------------------------

def record(conn, pid, basis="actual", **kw):
    args = dict(source="rent_roll", basis=basis, actor="seth", total="34240", total_period="monthly")
    args.update(kw)
    return napkin.add_record(conn, pid, **args)


def test_a_record_replaces_the_estimate_and_the_best_basis_is_selected(conn):
    pid = add_prop(conn)
    sched = record(conn, pid, "scheduled", evidence_date="2026-09-01")
    assert sched["annual_cents"] == 41088000 and sched["state"] == "ok"
    b = block(conn, pid)
    assert (b["rent"]["source"], b["rent"]["confidence"], b["rent"]["basis"]) == ("record", "Reported", "scheduled")
    unconfirmed = record(conn, pid, "unconfirmed", source="import", total="500000", total_period="annual")
    assert block(conn, pid)["rent"]["record_id"] == sched["id"]          # better evidence still wins
    actual = record(conn, pid, "actual", total="400000", total_period="t12")
    b = block(conn, pid)
    assert b["rent"]["record_id"] == actual["id"] and b["rent"]["annual_cents"] == 40000000
    assert {r["id"]: r["selected"] for r in b["records"]} == {sched["id"]: False, unconfirmed["id"]: False, actual["id"]: True}
    assert b["estimate"]["available"]                                    # the estimate stays visible beside it


def test_unconfirmed_and_estimated_records_are_only_partly_known(conn):
    pid = add_prop(conn)
    record(conn, pid, "unconfirmed", source="listing")
    assert block(conn, pid)["rent"]["confidence"] == "Partly known"


def test_pinning_overrides_the_choice_and_needs_review_cannot_be_pinned(conn):
    pid = add_prop(conn)
    actual = record(conn, pid, "actual")
    market = record(conn, pid, "estimated_market", total="300000", total_period="annual")
    napkin.pin_record(conn, pid, market["id"], True)
    assert block(conn, pid)["rent"]["record_id"] == market["id"]
    napkin.pin_record(conn, pid, market["id"], False)
    assert block(conn, pid)["rent"]["record_id"] == actual["id"]
    review = record(conn, pid, "actual", total="5000", total_period="partial")
    assert (review["state"], review["annual_cents"]) == ("needs_review", None)
    with pytest.raises(ValueError):
        napkin.pin_record(conn, pid, review["id"], True)
    assert block(conn, pid)["rent"]["needs_review"] == 1
    other = add_prop(conn)
    with pytest.raises(KeyError):
        napkin.pin_record(conn, other, actual["id"], True)


def test_projection_ranks_below_a_full_year_of_the_same_basis(conn):
    pid = add_prop(conn)
    one_month = record(conn, pid, "actual", total="34240", total_period="single_month")
    t12 = record(conn, pid, "actual", total="400000", total_period="t12")
    assert napkin.select([one_month, t12])["id"] == t12["id"]
    assert napkin.select([one_month])["state"] == "projection" and napkin.select([]) is None


def test_confirmed_zero_rent_is_kept_with_a_note(conn):
    pid = add_prop(conn)
    r = record(conn, pid, "actual", total="0", total_period="annual")
    b = block(conn, pid)
    assert r["annual_cents"] == 0 and "Confirmed zero" in r["reason"]
    assert b["rent"]["zero"] and b["rent"]["annual_cents"] == 0 and b["reference_cents"] == 0
    assert b["prior"]["state"] == "unavailable"


def test_record_inputs_are_validated(conn):
    pid = add_prop(conn)
    for bad in ({"source": "rumor"}, {"basis": "hopeful"}, {"evidence_date": "2999-01-01"}, {"evidence_date": "nope"},
                {"total": "-5"}, {"total": "abc"}, {"total_period": "weekly"}, {"area_type": "huge"},
                {"note": "x" * 2001}):
        with pytest.raises(ValueError):
            record(conn, pid, **bad)
    assert rows(conn, "SELECT id FROM rent_records") == []


def test_sqft_record_keeps_the_area_type_and_the_discrepancy(conn):
    pid = add_prop(conn)
    r = record(conn, pid, total=None, total_period=None, rate="1", rate_period="monthly", quantity="35000",
               quantity_kind="rentable_sqft", area_type="rentable")
    assert r["annual_cents"] == 42000000 and r["area_type"] == "rentable"
    both = record(conn, pid, total="34000", total_period="monthly", rate="1070", rate_period="monthly", quantity="32",
                  quantity_kind="units", authoritative="rate")
    assert both["annual_cents"] == 41088000 and both["discrepancy_cents"] == 288000 and "differs" in both["reason"]


def test_snapshot_is_frozen_and_changes_with_its_inputs(conn):
    pid = add_prop(conn)
    p, fit = prop_of(conn, pid), one(conn, "SELECT * FROM rent_fits WHERE property_id = ?", (pid,))
    first = napkin.snapshot(conn, p, "apartments", CFG, fit)
    assert first == napkin.snapshot(conn, p, "apartments", CFG, fit) and first["source"] == "estimate"
    assert first["scenarios"][0] == {"multiple": "4", "cents": 213600000, "role": "reference"}
    napkin.save_assumptions(conn, pid, {"rents": {"2": 1700}}, "seth")
    assert napkin.snapshot(conn, p, "apartments", CFG, fit)["hash"] != first["hash"]
    rec = record(conn, pid)
    snap = napkin.snapshot(conn, p, "apartments", CFG, fit)
    assert snap["source"] == "record" and snap["record"]["id"] == rec["id"] and snap["estimate"] is None
    assert snap["annual_cents"] == 41088000 and snap["basis"] == "actual"


# --- the price block --------------------------------------------------------------------------------------

def test_per_sqft_price_only_with_a_confirmed_rentable_area(conn):
    pid = add_prop(conn, building_sqft=35000)
    record(conn, pid)
    assert block(conn, pid)["per_sqft"] is None                       # area type unknown: not guessed
    conn.execute("UPDATE properties SET building_area_type = 'gross' WHERE id = ?", (pid,))
    assert block(conn, pid)["per_sqft"] is None
    conn.execute("UPDATE properties SET building_area_type = 'rentable' WHERE id = ?", (pid,))
    b = block(conn, pid)
    assert [(x["multiple"], x["cents"]) for x in b["per_sqft"]] == [("4", 4696), ("5", 5870), ("6", 7044)]


def test_prior_purchase_in_the_block_uses_the_reference_not_an_offer(conn):
    pid = add_prop(conn, sale_price=527000, sale_date="2019-05-01")
    record(conn, pid, total="275000", total_period="annual")
    pr = block(conn, pid)["prior"]
    assert (pr["state"], pr["share_pct"], pr["gap_cents"], pr["reference_cents"]) == ("below", 47.9, 2300000, 110000000)
    assert pr["sale_date"] == "2019-05-01" and pr["bundled"] is False
    history.save_history(conn, pid, {"sale_bundled": True}, "seth")
    pr = block(conn, pid)["prior"]
    assert pr["state"] == "unavailable" and pr["bundled"] and "bundled" in pr["reason"]


def test_market_groups_below_at_above_and_unknown_stay_visible(conn):
    ids = {}
    for name, sale in (("below", 527000), ("at", 550000), ("above", 550001), ("nominal", 500), ("unknown", None)):
        ids[name] = add_prop(conn, sale_price=sale)
        record(conn, ids[name], total="275000", total_period="annual")
    ids["no_rent"] = add_prop(conn, sale_price=527000, rents=None)
    market_id = market_by_slug(conn, MARKET)["id"]
    groups = napkin.prior_group_ids(napkin.resolve_market(conn, market_id, "apartments", CFG))
    assert set(groups["below"]) == {ids["below"]}
    assert set(groups["at_above"]) == {ids["at"], ids["above"]}
    assert set(groups["unknown"]) == {ids["nominal"], ids["unknown"], ids["no_rent"]}


# --- age and history --------------------------------------------------------------------------------------

def test_age_is_approximate_and_odd_years_are_flagged():
    assert history.age_info(1978, CFG) == {"year": 1978, "age": 48, "approximate": True, "flag": None,
                                           "text": "About 48 years old (built 1978; the year only, so the age is approximate)"}
    assert history.age_info(None, CFG)["age"] is None and history.age_info("", CFG)["text"] == "Age unknown"
    assert history.age_info(2030, CFG)["flag"] == "future" and history.age_info(2030, CFG)["age"] is None
    assert history.age_info(1066, CFG)["flag"] == "impossible"


def test_years_owned_shows_its_precision():
    exact = history.ownership_info("2014-03-01", CFG)
    assert (exact["precision"], exact["years"], exact["since"]) == ("exact", 12.7, "2014-03-01")
    year = history.ownership_info("2014", CFG)
    assert (year["precision"], year["years"]) == ("year", 12) and "approximate" in year["text"]
    for unknown in (None, "", "sometime", "13/45/2020"):
        assert history.ownership_info(unknown, CFG)["precision"] is None
    for future in ("2027-01-01", "2030"):
        r = history.ownership_info(future, CFG)
        assert (r["flag"], r["years"]) == ("future", None)


def test_builder_evidence_is_kept_apart_and_never_inferred(conn):
    pid = add_prop(conn, year_built=1978, sale_date="1978-06-01")
    assert history.history_block(conn, prop_of(conn, pid), CFG)["builder"]["state"] == "unknown"   # matching years prove nothing
    with pytest.raises(ValueError):
        history.save_history(conn, pid, {"builder_state": "supported"}, "seth")                   # a claim needs a source
    with pytest.raises(ValueError):
        history.save_history(conn, pid, {"builder_state": "certain"}, "seth")
    history.save_history(conn, pid, {"builder_state": "supported", "builder_source": "County deed 1978"}, "seth")
    history.save_history(conn, pid, {"builder_note": "Seller said so"}, "kim")
    h = history.history_block(conn, prop_of(conn, pid), CFG)
    assert (h["builder"]["state"], h["builder"]["source"], h["builder"]["note"], h["updated_by"]) == (
        "supported", "County deed 1978", "Seller said so", "kim")
    assert not any("single" in k or "local" in k for k in h)


def test_area_type_is_set_with_the_value(conn):
    pid = add_prop(conn, building_sqft=35000)
    history.save_history(conn, pid, {"building_area_type": "rentable"}, "seth")
    assert prop_of(conn, pid)["building_area_type"] == "rentable"
    history.save_history(conn, pid, {"building_area_type": None}, "seth")
    assert prop_of(conn, pid)["building_area_type"] is None
    with pytest.raises(ValueError):
        history.save_history(conn, pid, {"building_area_type": "total"}, "seth")


# --- the routes -------------------------------------------------------------------------------------------

@pytest.fixture
def client(conn):
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def url(pid, tail=""):
    return f"/api/m/{MARKET}/properties/{pid}{tail}"


def test_routes_read_write_and_return_the_fresh_block(client, conn):
    pid = add_prop(conn, sale_price=527000, asking_price=1900000)
    got = client.get(url(pid, "/pricing")).json()
    assert got["pricing"]["rent"]["confidence"] == "Estimated" and got["history"]["age"]["age"] is None
    detail = client.get(url(pid)).json()
    assert detail["pricing"]["asking_price"] == 1900000 and detail["property"]["asking_price"] == 1900000
    assert detail["history"]["sale"]["bundled"] is False and detail["rent_detail"].startswith("Area 2BR")

    r = client.put(url(pid, "/rent/assumptions"), json={"units": 40, "rents": {"2": 1500}})
    assert r.status_code == 200 and r.json()["pricing"]["assumptions"] == {
        "units": 40, "mix": {}, "rents": {"2": 150000}, "updated_by": "open", "updated_at": r.json()["pricing"]["assumptions"]["updated_at"]}
    r = client.put(url(pid, "/rent/assumptions"), json={"units": None})        # only what is sent changes
    assert r.json()["pricing"]["assumptions"]["units"] is None and r.json()["pricing"]["assumptions"]["rents"] == {"2": 150000}

    r = client.post(url(pid, "/rent/records"), json={"source": "owner", "basis": "scheduled", "total": "34,240",
                                                     "total_period": "monthly", "evidence_date": "2026-10-01"})
    assert r.status_code == 200
    b = r.json()["pricing"]
    assert b["rent"]["annual_cents"] == 41088000 and b["records"][0]["created_by"] == "open"
    rid = b["records"][0]["id"]
    assert client.post(url(pid, f"/rent/records/{rid}/pin"), json={"pinned": True}).json()["pricing"]["records"][0]["pinned"]
    assert client.post(url(pid, "/rent/records/9999/pin"), json={}).status_code == 404

    r = client.put(url(pid, "/history"), json={"builder_state": "suggested", "sale_bundled": True,
                                               "building_area_type": "rentable"})
    assert r.json()["history"]["builder"]["state"] == "suggested" and r.json()["history"]["sale"]["bundled"]
    assert r.json()["property"]["building_area_type"] == "rentable"


def test_routes_reject_bad_input_and_foreign_properties(client, conn):
    pid = add_prop(conn)
    for body in ({"source": "owner", "basis": "scheduled", "total": "abc", "total_period": "monthly"},
                 {"source": "owner", "basis": "guess", "total": "5", "total_period": "monthly"},
                 {"source": "owner", "basis": "actual", "total": "5", "total_period": "monthly", "note": "x" * 5000}):
        assert client.post(url(pid, "/rent/records"), json=body).status_code in (400, 422)
    assert client.put(url(pid, "/rent/assumptions"), json={"mix": {"1": 1.5}}).status_code == 422
    assert client.put(url(pid, "/rent/assumptions"), json={"mix": {"99": 1}}).status_code == 400
    assert client.put(url(pid, "/history"), json={"builder_state": "supported"}).status_code == 400
    assert client.get(f"/api/m/self-storage-springfield-oh/properties/{pid}/pricing").status_code == 404
    assert client.put(f"/api/m/self-storage-springfield-oh/properties/{pid}/history", json={}).status_code == 404
    assert rows(conn, "SELECT id FROM rent_records") == []


def test_deals_filter_groups_and_counts(client, conn):
    below = add_prop(conn, sale_price=527000)
    above = add_prop(conn, sale_price=600000)
    unknown = add_prop(conn)
    for pid in (below, above, unknown):
        record(conn, pid, total="275000", total_period="annual")

    def ids(view):
        r = client.get(f"/api/m/{MARKET}/properties", params={"view": view}).json()
        return {x["id"] for x in r["items"]}, r["counts"]

    got, counts = ids("prior_below")
    assert got == {below} and counts["prior_below"] == 1
    assert ids("prior_at_above")[0] == {above} and ids("prior_unknown")[0] == {unknown}
    assert counts["prior_at_above"] == 1 and counts["prior_unknown"] == 1
    assert {"prior_below", "prior_at_above", "prior_unknown"} <= {v["key"] for v in client.get(f"/api/m/{MARKET}/summary").json()["views"]}


def test_a_new_database_has_the_tables_and_an_old_one_gains_the_columns(settings):
    import sqlite3
    from bluemoon.db import connect
    path = settings.path("db")
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE properties(id INTEGER PRIMARY KEY, market_id INTEGER, property_key TEXT, "
                      "owner_name TEXT, sale_price REAL, flags_json TEXT); INSERT INTO properties VALUES(1, 1, 'k', 'X', 5, '[]');")
    old.close()
    c = connect(path, settings)
    cols = {r[1] for r in c.execute("PRAGMA table_info(properties)")}
    assert {"asking_price", "building_area_type"} <= cols
    assert c.execute("SELECT sale_price, asking_price, building_area_type FROM properties").fetchone()[:] == (5, None, None)
    assert {"rent_records", "rent_assumptions", "property_history"} <= {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    c.close()


# --- imports and exports ----------------------------------------------------------------------------------

def import_csv(conn, tmp_path, header: str, row: str) -> dict:
    from bluemoon.ingest import import_file
    path = tmp_path / "list.csv"
    path.write_text(f"Address,City,State,Zip Code,County,Number of Units,Sold Price,{header}\n{row}\n", encoding="utf-8")
    import_file(conn, MARKET, path)
    return one(conn, "SELECT * FROM properties WHERE address LIKE '1 TEST%'")


def test_import_keeps_asking_price_apart_and_reads_the_area_type_from_the_header(conn, tmp_path):
    p = import_csv(conn, tmp_path, "Asking Price,RBA", "1 Test St,Springfield,OH,45503,Clark,30,527000,1900000,31000")
    assert (p["asking_price"], p["sale_price"], p["building_sqft"], p["building_area_type"]) == (1900000, 527000, 31000, "rentable")


@pytest.mark.parametrize("header,kind", [("Building SqFt", None), ("GBA", "gross"), ("Rentable Building Area", "rentable")])
def test_only_a_labelled_building_size_gets_an_area_type(conn, tmp_path, header, kind):
    p = import_csv(conn, tmp_path, f"Asking Price,{header}", "1 Test St,Springfield,OH,45503,Clark,30,527000,,31000")
    assert (p["asking_price"], p["building_area_type"]) == (None, kind)


def test_the_export_carries_rent_and_price_and_never_writes_unknown_as_zero(conn, tmp_path):
    from bluemoon import exports
    with_rent = add_prop(conn, sale_price=527000, asking_price=1900000)
    record(conn, with_rent, total="275000", total_period="annual")
    without = add_prop(conn, rents=None)
    data = exports.load_market(conn, MARKET)
    by_id = {r["property_id"]: r for r in data.properties}
    a, b = by_id[with_rent], by_id[without]
    assert (a["rent_annual"], a["blue_moon_price"], a["price_multiple"], a["asking_price"]) == (275000, 1100000, 4, 1900000)
    assert a["rent_confidence"] == "Reported" and a["rent_basis"] == "Actual rent collected"
    assert a["prior_check"] == "Below half (47.9% of the price)"
    assert (b["rent_annual"], b["blue_moon_price"], b["rent_confidence"]) == (None, None, None)
    assert b["prior_check"].startswith("Unavailable")
    headers = [c.header for c in data.our_columns()]
    assert {"Yearly gross rent", "Blue Moon price", "Price multiple", "Area rent check"} <= set(headers)


# --- review fixes (math) ------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"source": "owner", "basis": "actual", "total": "1e20", "total_period": "monthly"},
    {"source": "owner", "basis": "actual", "total": "1e30", "total_period": "monthly"},
    {"source": "owner", "basis": "actual", "total": "92233720368547758", "total_period": "annual"},
    {"source": "owner", "basis": "actual", "rate": "1e20", "rate_period": "monthly", "quantity": "1e10",
     "quantity_kind": "units"}])
def test_huge_money_is_a_400_not_a_500(client, conn, body):
    pid = add_prop(conn)
    assert client.post(url(pid, "/rent/records"), json=body).status_code == 400
    assert client.put(url(pid, "/rent/assumptions"), json={"rents": {"2": "1e30"}}).status_code == 400
    assert rows(conn, "SELECT id FROM rent_records") == []


def test_offer_default_price_past_the_cap_is_refused():
    from bluemoon import offers
    snap = {"scenarios": [{"role": "reference", "cents": offers.MAX_PRICE_CENTS + 1}]}
    with pytest.raises(ValueError, match="too large"):
        offers._need_default(snap)


def test_zero_unit_bedroom_types_do_not_need_a_rent():
    shares = {0: Decimal(10), 1: Decimal(30), 2: Decimal(50), 3: Decimal(10)}
    est = napkin.estimate(1, {}, {2: 120000}, shares)
    assert est["available"] and [line["bedrooms"] for line in est["lines"]] == [2] and est["monthly_cents"] == 120000


def test_one_month_projection_is_only_partly_known(conn):
    pid = add_prop(conn)
    record(conn, pid, "actual", total="34240", total_period="single_month")
    res = napkin.resolve(prop_of(conn, pid), napkin.records_of(conn, pid), {}, {}, CFG, "apartments")
    assert res["record"]["state"] == "projection" and res["confidence"] == napkin.CONFIDENCE["partly"]


@pytest.mark.parametrize("sale", ["0000", "1000", "0001-01-01"])
def test_impossible_sale_dates_are_flagged(sale):
    got = history.ownership_info(sale, CFG)
    assert got["flag"] == "impossible" and got["years"] is None and "can't be right" in got["text"]
