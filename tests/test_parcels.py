"""County records (bluemoon/enrich/parcels.py): the owner and tax mailing address from free county parcel services.

No real network: every county is a fake ArcGIS service behind httpx.MockTransport. Owner names, addresses and
parcel numbers are made up.
"""
from __future__ import annotations

import itertools
import json

import httpx
import pytest

from bluemoon import pipeline
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.drill import engine as E
from bluemoon.drill import stats
from bluemoon.enrich import geo, parcels

MARKET = "apartments-springfield-oh"
_ids = itertools.count(1)

GREENE = "gis.greenecountyohio.gov"
CLINTON = "services1.arcgis.com/tAhcHWpOD9ygNPbJ"
CHAMPAIGN = "services5.arcgis.com/HBIN2hfRscrws7eM"
FRANKLIN = "gis.franklincountyohio.gov"
MIAMI = "services3.arcgis.com/wCWf4EGMg4PzHwzA"
ODNR = "gis.ohiodnr.gov"
OGRIP = "services2.arcgis.com/MlJ0G8iWUyC7jAmu"


# --- a fake county ---------------------------------------------------------------------------------

class Fake:
    """Fake ArcGIS services. `answers` maps a piece of the layer URL to a function (lat, lng) -> features, a list
    of features, or an httpx.Response. Counts every request."""

    def __init__(self, **answers):
        self.answers: dict[str, object] = {}
        self.calls: list[tuple[str, float, float, dict]] = []
        for key, value in answers.items():
            self.answers[key] = value

    def set(self, key, value):
        self.answers[key] = value

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        q = dict(request.url.params)
        lng, lat = (float(x) for x in q["geometry"].split(","))
        self.calls.append((url, lat, lng, q))
        for key, value in self.answers.items():
            if key in url:
                if callable(value):
                    value = value(lat, lng)
                if isinstance(value, httpx.Response):
                    return value
                return httpx.Response(200, json={"features": [{"attributes": a} for a in value]})
        return httpx.Response(200, json={"features": []})

    def count(self, key: str = "") -> int:
        return sum(1 for c in self.calls if key in c[0])


@pytest.fixture
def fake(monkeypatch, settings):
    f = Fake()
    monkeypatch.setattr(parcels, "make_client", lambda cfg: httpx.Client(transport=httpx.MockTransport(f)))
    monkeypatch.setattr(geo, "_sleep", lambda seconds: None)
    settings.parcels["settings"].update(pause_seconds=0, retry_backoff_seconds=0)
    return f


# --- helpers ---------------------------------------------------------------------------------------

def add_prop(conn, *, county="GREENE", state="OH", lat=39.69, lng=-83.93, owner="ACME PORTFOLIO LLC", score=0.5,
             in_buybox=1, mailing="12 MADE UP RD", **kw) -> int:
    n = next(_ids)
    m = market_by_slug(conn, MARKET)
    data = {
        "market_id": m["id"], "property_key": f"parcels:{n}", "address": f"{300 + n} SAMPLE AVE", "city": "XENIA",
        "state": state, "zip": "45385", "county": county, "apn": f"P{n:04d}", "lat": lat, "lng": lng, "units": 24,
        "property_type": "Multifamily", "owner_name": owner, "mailing_address": mailing, "mailing_city": "DUBLIN",
        "mailing_state": "OH", "mailing_zip": "43017", "raw_json": "{}", "flags_json": "[]", "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    data.update(kw)
    cur = conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                       tuple(data.values()))
    pid = int(cur.lastrowid)
    if owner:
        conn.execute("INSERT OR REPLACE INTO owners(property_id, owner_type, owner_norm, computed_at) VALUES(?,?,?,?)",
                     (pid, "small_llc", owner.upper(), now_iso()))
    if score is not None:
        conn.execute("INSERT OR REPLACE INTO scores(property_id, in_buybox, exclusion_reasons, score, rank, computed_at) "
                     "VALUES(?,?,?,?,?,?)", (pid, in_buybox, "[]" if in_buybox else '["drive_time"]', score, None,
                                             now_iso()))
    conn.commit()
    E.ensure_states(conn, MARKET)
    return pid


def greene_parcel(owner="ACME PORTFOLIO LLC", taxpayer=None, street="6791 CHESLEY RD", csz="DUBLIN OH 43017", pid="M40-1"):
    return {"Parcel_Number": pid, "Owner_Name": owner, "Taxpayer_Contact": taxpayer or owner,
            "Tax_Mailing_Address": street, "Tax_Mailing_City_St_Zip": csz}


def clinton_parcel(owner="MADE UP CITY", street="69 N SOUTH ST", city="WILMINGTON", zip_="45177"):
    return {"Parcel_Number": "010-03-03", "Listed_Name": owner, "Owner_Contact_Name": owner,
            "Owner_Contact_Address": "1 OLD OWNER ST", "Owner_Contact_City": "OLDTOWN", "Owner_Contact_State": "OH",
            "Owner_Contact_ZipCode": "45000", "Taxpayer_Contact_Name": owner, "Taxpayer_Contact_Address": street,
            "Taxpayer_Contact_City": city, "Taxpayer_Contact_State": "OH", "Taxpayer_Contact_ZipCode": zip_}


def drill_of(conn, pid):
    return E.property_drill(conn, pid)


def stage(drill, key):
    return next(s for s in drill["stages"] if s["key"] == key)


def run(conn, **kw):
    return parcels.lookup_parcels(conn, MARKET, **kw)


def people(conn, pid):
    return rows(conn, "SELECT name, role, source, notes FROM people WHERE property_id=? ORDER BY id", (pid,))


def mail(conn, pid):
    return rows(conn, "SELECT value, status, source, notes FROM contact_points WHERE property_id=? AND channel='mail' "
                      "AND source='county_gis'", (pid,))


# --- config ----------------------------------------------------------------------------------------

def test_the_real_config_loads_and_every_county_has_what_it_needs(settings):
    cfg = parcels.load_config(settings)
    oh = cfg.counties["OH"]
    assert {"FRANKLIN", "GREENE", "DELAWARE", "CHAMPAIGN", "MADISON", "LOGAN", "FAYETTE", "CLINTON", "SHELBY",
            "CLARK", "UNION", "MIAMI"} <= set(oh)
    for key, layer in oh.items():
        assert layer.url.startswith("https://") and layer.owners, key
        assert "/MapServer/" in layer.url or "/FeatureServer/" in layer.url, key
    assert oh["MIAMI"].enabled is False and "not published" in (oh["MIAMI"].note or "")
    assert all(layer.enabled for k, layer in oh.items() if k != "MIAMI")
    assert cfg.statewide.owner_layer and cfg.statewide.mailing_layer
    assert cfg.source_for("IN", "ALLEN") is None and cfg.source_for("OH", "MONTGOMERY").statewide


def test_a_wrong_key_in_the_config_is_explained(settings):
    settings.parcels["counties"]["OH"]["GREENE"]["oweners"] = ["x"]
    with pytest.raises(ValueError, match="parcels.yaml"):
        parcels.load_config(settings)


# --- reading a county's answer ---------------------------------------------------------------------

def test_two_counties_with_different_field_maps_give_the_same_picture(conn, fake):
    g = add_prop(conn, county="GREENE", lat=39.69, lng=-83.93, owner="ACME PORTFOLIO LLC")
    c = add_prop(conn, county="CLINTON", lat=39.44, lng=-83.82, owner="MADE UP CITY")
    fake.set(GREENE, [greene_parcel(owner="ACME PORTFOLIO LLC", taxpayer="ACME OPERATIONS LLC")])
    fake.set(CLINTON, [clinton_parcel()])
    res = run(conn)
    assert res["properties"] == 2 and res["found"] == 2 and res["errors"] == 0
    assert mail(conn, g)[0]["value"] == "6791 CHESLEY RD, DUBLIN OH 43017"
    assert mail(conn, c)[0]["value"] == "69 N SOUTH ST, WILMINGTON, OH 45177"   # city and state/zip joined
    assert {p["name"]: p["role"] for p in people(conn, g)} == {"ACME PORTFOLIO LLC": "owner_of_record",
                                                              "ACME OPERATIONS LLC": "other"}
    assert all(p["source"] == "county_gis" for p in people(conn, g))
    # the real question was asked the way ArcGIS wants it
    url, lat, lng, q = fake.calls[0]
    assert url.startswith(f"https://{GREENE}/webgis2/rest/services/OpenData/OpenData/MapServer/1/query")
    assert q == {"geometry": "-83.93,39.69", "geometryType": "esriGeometryPoint", "inSR": "4326",
                 "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "returnGeometry": "false", "f": "json"}


def test_the_stage_is_checked_off_by_auto_naming_the_county_and_the_owner(conn, fake):
    pid = add_prop(conn)
    fake.set(GREENE, [greene_parcel(pid="M40-0001-0009")])
    run(conn)
    a = rows(conn, "SELECT * FROM drill_attempts WHERE property_id=? AND stage_key='county_auditor'", (pid,))
    assert len(a) == 1 and a[0]["outcome"] == "found" and a[0]["actor"] == "auto"
    assert "Greene County tax record" in a[0]["detail"] and "ACME PORTFOLIO LLC" in a[0]["detail"]
    assert "M40-0001-0009" in a[0]["detail"] and "6791 CHESLEY RD" in a[0]["detail"]
    d = drill_of(conn, pid)
    assert stage(d, "county_auditor")["status"] == "done_found" and d["level"] == 2
    assert d["next_step"]["stage_key"] != "county_auditor"


def test_the_tax_mailing_address_is_untested_and_never_level_3(conn, fake):
    pid = add_prop(conn)
    fake.set(GREENE, [greene_parcel(street="55 OTHER BLVD", csz="COLUMBUS OH 43215")])
    run(conn)
    (m,) = mail(conn, pid)
    assert m["status"] == "untested" and m["source"] == "county_gis" and "Greene County" in m["notes"]
    st = one(conn, "SELECT * FROM drill_state WHERE property_id=?", (pid,))
    assert st["level"] == 2 and st["confirmed_at"] is None


def test_the_same_address_as_crexi_is_not_added_twice(conn, fake):
    pid = add_prop(conn, mailing="6791 Chesley Rd")
    conn.execute("UPDATE properties SET mailing_city='DUBLIN', mailing_state='OH', mailing_zip='43017' WHERE id=?", (pid,))
    E._add_contact(conn, pid, "mail", "6791 CHESLEY RD, DUBLIN, OH 43017", source="records_baseline")
    conn.commit()
    fake.set(GREENE, [greene_parcel(street="6791 CHESLEY RD", csz="DUBLIN OH 43017")])
    res = run(conn)
    assert res["contacts_added"] == 0 and mail(conn, pid) == []


def test_a_tax_service_address_is_passed_over_for_the_owners_own(conn, fake):
    pid = add_prop(conn, county="FRANKLIN")
    fake.set(FRANKLIN, [{"PARCELID": "010-1", "OWNERNME1": "ACME PORTFOLIO LLC", "PSTLNME1": "CORELOGIC",
                         "PSTLADDRES": "PO BOX 9202 COPPELL TX 75019", "MAILNME1": "C/O RYAN LLC, 200 E BROWARD BLVD",
                         "MAILNME2": "FT. LAUDERDALE, FL 33301"}])
    run(conn)
    assert [m["value"] for m in mail(conn, pid)] == ["C/O RYAN LLC, 200 E BROWARD BLVD, FT. LAUDERDALE, FL 33301"]
    assert "CORELOGIC" not in " ".join(p["name"] for p in people(conn, pid))


def test_placeholders_and_not_on_file_are_ignored(conn, fake):
    pid = add_prop(conn, county="DELAWARE")
    fake.set("services2.arcgis.com/ziXVKVy3BiopMCCU", [{"OWNER1": "ACME PORTFOLIO LLC", "OWNER2": "0",
                                                         "MAILNAME1": "ACME PORTFOLIO LLC", "MAILADDR1": "NOT ON FILE",
                                                         "MAILADDR2": "COLUMBUS, OH 43215"}])
    run(conn)
    assert [p["name"] for p in people(conn, pid) if p["source"] == "county_gis"] == ["ACME PORTFOLIO LLC"]
    assert mail(conn, pid) == []


def test_champaign_single_line_addresses(conn, fake):
    pid = add_prop(conn, county="CHAMPAIGN")
    fake.set(CHAMPAIGN, [{"Parcel": "K48-1", "PPOwner": "CITY OF TESTVILLE", "PPTaxPayer": "CITY OF TESTVILLE A CORP",
                          "PPTaxPayerAddress": "DUGAN ST URBANA OH 43078", "PPOwnerAddress": "OTHER ST URBANA OH 43078"}])
    run(conn)
    assert mail(conn, pid)[0]["value"] == "DUGAN ST URBANA OH 43078"
    assert {p["role"] for p in people(conn, pid) if p["name"] == "CITY OF TESTVILLE A CORP"} == {"other"}


# --- the owner on the tax record is not the Crexi owner --------------------------------------------

def test_a_different_owner_is_said_flagged_and_noted_but_not_overwritten(conn, fake):
    pid = add_prop(conn, owner="OLD OWNER LLC")
    fake.set(GREENE, [greene_parcel(owner="NEW BUYER HOLDINGS LLC")])
    res = run(conn)
    assert res["owner_differs"] == 1
    detail = scalar(conn, "SELECT detail FROM drill_attempts WHERE property_id=? AND stage_key='county_auditor'", (pid,))
    assert "NEW BUYER HOLDINGS LLC" in detail and "not the owner in the Crexi export (OLD OWNER LLC)" in detail
    assert "county_owner_differs" in json.loads(scalar(conn, "SELECT flags_json FROM properties WHERE id=?", (pid,)))
    assert scalar(conn, "SELECT owner_name FROM properties WHERE id=?", (pid,)) == "OLD OWNER LLC"
    assert "different owner" in scalar(conn, "SELECT notes FROM drill_state WHERE property_id=?", (pid,))
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE property_id=? AND stage_key='_new_pass'", (pid,)) == 0
    assert scalar(conn, "SELECT updated_at FROM properties WHERE id=?", (pid,)) is not None


def test_the_same_owner_written_differently_is_not_a_difference(conn, fake):
    pid = add_prop(conn, owner="Doe John")
    fake.set(GREENE, [greene_parcel(owner="DOE JOHN A", taxpayer="DOE JOHN A")])
    res = run(conn)
    assert res["found"] == 1 and res["owner_differs"] == 0
    assert "county_owner_differs" not in json.loads(scalar(conn, "SELECT flags_json FROM properties WHERE id=?", (pid,)))


# --- saving answers; nothing is asked twice --------------------------------------------------------

def test_a_second_run_makes_no_calls_and_even_a_cleared_list_uses_the_saved_answers(conn, fake):
    add_prop(conn, county="GREENE")
    add_prop(conn, county="CLINTON", lat=39.4, lng=-83.8)
    fake.set(GREENE, [greene_parcel()])
    fake.set(CLINTON, [clinton_parcel()])
    first = run(conn)
    assert first["fetched"] == 2 and len(fake.calls) == 2
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache WHERE provider='county_gis'") == 2
    assert scalar(conn, "SELECT COUNT(*) FROM api_calls WHERE provider='county_gis'") == 2
    again = run(conn)
    assert again["properties"] == 0 and len(fake.calls) == 2
    conn.execute("DELETE FROM parcel_lookups")           # forget who was looked up: the saved answers still stand
    conn.execute("DELETE FROM drill_attempts WHERE stage_key='county_auditor'")
    conn.commit()
    third = run(conn)
    assert third["properties"] == 2 and third["cached"] == 2 and third["fetched"] == 0 and len(fake.calls) == 2
    assert scalar(conn, "SELECT COUNT(*) FROM api_calls WHERE provider='county_gis'") == 2   # a cache hit is no call


def test_an_error_is_retried_then_reported_and_never_saved(conn, fake):
    pid = add_prop(conn)
    fake.set(GREENE, httpx.Response(503, text="busy"))
    res = run(conn)
    assert res["errors"] == 1 and res["found"] == 0 and res["warnings"] and "HTTP 503" in res["warnings"][0]
    assert fake.count(GREENE) == 3                                   # first try + max_retries (2)
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache WHERE provider='county_gis'") == 0
    assert scalar(conn, "SELECT COUNT(*) FROM parcel_lookups") == 0
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE property_id=? AND stage_key='county_auditor'",
                  (pid,)) == 0
    fake.set(GREENE, [greene_parcel()])                              # the county is back: the next run finds it
    assert run(conn)["found"] == 1


def test_an_arcgis_error_inside_a_200_is_an_error_too(conn, fake):
    add_prop(conn)
    fake.set(GREENE, httpx.Response(200, json={"error": {"code": 400, "message": "Invalid query"}}))
    res = run(conn)
    assert res["errors"] == 1 and fake.count(GREENE) == 1            # a 400 is not retried
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache WHERE provider='county_gis'") == 0
    fake.set(GREENE, httpx.Response(200, json={"error": {"code": 500, "message": "Server busy"}}))
    assert run(conn)["errors"] == 1 and fake.count(GREENE) == 4      # a 500 is retried


def test_a_service_that_keeps_failing_is_left_alone_for_the_rest_of_the_run(conn, fake, settings):
    settings.parcels["settings"].update(stop_after_errors=2, concurrency=1, max_retries=0, chunk_size=3)
    for i in range(6):
        add_prop(conn, lat=39.6 + i / 100)
    fake.set(GREENE, httpx.Response(500, text="down"))
    res = run(conn)
    assert res["errors"] == 6 and fake.count(GREENE) == 2


def test_no_parcel_is_saved_too_and_left_open_for_the_hands_on_check(conn, fake):
    pid = add_prop(conn)
    res = run(conn)                                                  # the fake county has nothing at that point
    assert res["no_parcel"] == 1 and res["found"] == 0
    assert scalar(conn, "SELECT status FROM parcel_lookups WHERE property_id=?", (pid,)) == "no_parcel"
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache WHERE provider='county_gis'") == 1
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE property_id=? AND stage_key='county_auditor'",
                  (pid,)) == 0
    E.run_auto_stages(conn, MARKET)
    assert drill_of(conn, pid)["next_step"]["stage_key"] == "county_auditor"      # Seth can still check by hand


def test_right_of_way_records_are_no_parcel(conn, fake):
    add_prop(conn)
    fake.set(GREENE, [{"Parcel_Number": "ROW", "Owner_Name": "SOME ROAD AUTHORITY"}])
    assert run(conn)["no_parcel"] == 1


# --- who is looked up ------------------------------------------------------------------------------

def test_no_coordinates_and_other_states_are_skipped(conn, fake):
    add_prop(conn, lat=None, lng=None)
    add_prop(conn, state="IN", county="ALLEN")
    ok = add_prop(conn)
    fake.set(GREENE, [greene_parcel()])
    res = run(conn)
    assert res["properties"] == 1 and res["no_coordinates"] == 1 and res["no_source"] == 1
    assert len(fake.calls) == 1 and scalar(conn, "SELECT property_id FROM parcel_lookups") == ok


def test_a_property_you_checked_by_hand_or_a_confirmed_one_is_left_alone(conn, fake):
    hand = add_prop(conn)
    E.record_attempt(conn, hand, "county_auditor", "nothing", "looked, nothing")
    done = add_prop(conn)
    E.level_action(conn, done, "owner_found", person="Robin Sample")
    E.level_action(conn, done, "confirm", person="Robin Sample")
    fake.set(GREENE, [greene_parcel()])
    res = run(conn)
    assert res["properties"] == 0 and fake.calls == []
    assert one(conn, "SELECT level FROM drill_state WHERE property_id=?", (done,))["level"] == 3


def test_a_disabled_county_is_never_asked_and_uses_the_statewide_data(conn, fake):
    pid = add_prop(conn, county="MIAMI", lat=39.96, lng=-84.15)
    fake.set(MIAMI, [{"PARCEL": "A47-1", "PPOwner": "SHOULD NEVER BE READ"}])
    fake.set(ODNR, [{"PIN": "A47-1", "OWNER1": "ACME PORTFOLIO LLC", "CurrentTo": 1698796800000}])
    fake.set(OGRIP, [{"LocalParcelID": "A47-1", "MailAddressAll": "9  STATE ROAD   TIPP CITY 45371",
                      "CurrentTo": 1698814800000}])
    res = run(conn)
    assert fake.count(MIAMI) == 0 and fake.count(ODNR) == 1 and fake.count(OGRIP) == 1
    assert res["older"] == 1 and scalar(conn, "SELECT source FROM parcel_lookups WHERE property_id=?", (pid,)) == "OH:statewide"


def test_with_the_statewide_data_off_a_disabled_county_has_no_source(conn, fake, settings):
    settings.parcels["settings"]["statewide_fallback"] = False
    add_prop(conn, county="MIAMI")
    add_prop(conn, county="MONTGOMERY")
    res = run(conn)
    assert res["properties"] == 0 and res["no_source"] == 2 and fake.calls == []


def test_statewide_data_is_marked_older_and_leaves_the_stage_open(conn, fake):
    pid = add_prop(conn, county="MONTGOMERY", owner="CURRENT OWNER LLC")
    fake.set(ODNR, [{"PIN": "R72 1", "OWNER1": "FORMER OWNER LLC", "OWNER2": "", "CurrentTo": 1698796800000}])
    fake.set(OGRIP, [{"LocalParcelID": "R72 1", "MailAddressAll": "118  N JEFFERSON ST   DAYTON 45402",
                      "CurrentTo": 1698814800000}])
    res = run(conn, limit=5)
    assert res["older"] == 1 and res["found"] == 0 and res["owner_differs"] == 0
    (p,) = [x for x in people(conn, pid) if x["source"] == "county_gis"]
    assert p["name"] == "FORMER OWNER LLC" and p["role"] == "other" and "older statewide data" in p["notes"]
    (m,) = mail(conn, pid)
    assert m["value"] == "118 N JEFFERSON ST DAYTON 45402" and m["status"] == "untested"
    assert "older statewide data" in m["notes"] and "2023-1" in m["notes"]
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE property_id=? AND stage_key='county_auditor'", (pid,)) == 0
    E.run_auto_stages(conn, MARKET)
    assert drill_of(conn, pid)["next_step"]["stage_key"] == "county_auditor"      # the old data still needs a look
    assert "county_owner_differs" not in json.loads(scalar(conn, "SELECT flags_json FROM properties WHERE id=?", (pid,)))


def test_statewide_owner_for_a_property_with_no_owner_name_is_a_possible_owner(conn, fake):
    pid = add_prop(conn, county="WARREN", owner=None)
    fake.set(ODNR, [{"PIN": "1", "OWNER1": "ROBIN SAMPLE", "CurrentTo": 1698796800000}])
    run(conn)
    assert [(p["name"], p["role"]) for p in people(conn, pid)] == [("ROBIN SAMPLE", "possible_owner")]
    assert drill_of(conn, pid)["level"] == 2


def test_ogrip_with_no_owner_adds_only_the_address(conn, fake):
    pid = add_prop(conn, county="WARREN")
    fake.set(OGRIP, [{"LocalParcelID": "9", "MailAddressAll": "77  SOMEWHERE LN   MASON 45040"}])
    res = run(conn)
    assert res["older"] == 1 and mail(conn, pid)[0]["value"] == "77 SOMEWHERE LN MASON 45040"
    assert people(conn, pid) == []


# --- do not contact --------------------------------------------------------------------------------

def test_do_not_contact_owners_are_left_out_and_nobody_is_asked(conn, fake):
    a = add_prop(conn, owner="ROBIN SAMPLE HOLDINGS LLC")
    b = add_prop(conn, owner="ROBIN SAMPLE HOLDINGS LLC", lat=39.7)       # the same owner's other property
    c = add_prop(conn, owner="SOMEONE ELSE LLC", lat=39.8)
    E.level_action(conn, a, "dnc")
    fake.set(GREENE, [greene_parcel(owner="SOMEONE ELSE LLC")])
    res = run(conn)
    assert res["skipped_dnc"] == 0 and res["properties"] == 1 and len(fake.calls) == 1   # a and b never reach the run
    assert scalar(conn, "SELECT status FROM drill_state WHERE property_id=?", (b,)) == "dnc"
    assert scalar(conn, "SELECT COUNT(*) FROM parcel_lookups WHERE property_id IN (?,?)", (a, b)) == 0
    assert scalar(conn, "SELECT property_id FROM parcel_lookups") == c


def test_an_owner_marked_do_not_contact_in_another_market_group_is_skipped_by_family(conn, fake):
    a = add_prop(conn, owner="FAMILY HOLDINGS LLC")
    b = add_prop(conn, owner="FAMILY HOLDINGS LLC", lat=39.71)
    # b is not itself marked, but a (same owner name) is: the run must not touch b
    conn.execute("UPDATE drill_state SET status='dnc' WHERE property_id=?", (a,))
    conn.commit()
    fake.set(GREENE, [greene_parcel(owner="FAMILY HOLDINGS LLC")])
    res = run(conn)
    assert res["skipped_dnc"] == 1 and fake.calls == [] and scalar(conn, "SELECT COUNT(*) FROM parcel_lookups") == 0
    assert b


def test_an_address_on_the_do_not_contact_list_comes_in_as_dnc(conn, fake):
    other = add_prop(conn, owner="ZED LLC", lat=39.9)
    E._add_contact(conn, other, "mail", "6791 CHESLEY RD, DUBLIN OH 43017", source="manual", status="dnc")
    conn.commit()
    pid = add_prop(conn)
    fake.set(GREENE, [greene_parcel(street="6791 Chesley Rd", csz="DUBLIN OH 43017")])
    run(conn)
    assert [m["status"] for m in mail(conn, pid)] == ["dnc"]


# --- several parcels at one point ------------------------------------------------------------------

def test_a_condo_building_with_one_owner_is_one_answer(conn, fake):
    pid = add_prop(conn)
    fake.set(GREENE, [greene_parcel(pid="U1"), greene_parcel(pid="U2")])
    res = run(conn)
    assert res["found"] == 1 and len([p for p in people(conn, pid) if p["role"] == "owner_of_record"]) == 1


def test_parcels_with_different_owners_pick_the_crexi_owner_or_leave_it_to_you(conn, fake):
    pid = add_prop(conn, owner="ACME PORTFOLIO LLC")
    fake.set(GREENE, [greene_parcel(owner="UNIT ONE PERSON"), greene_parcel(owner="ACME PORTFOLIO LLC")])
    assert run(conn)["found"] == 1
    amb = add_prop(conn, owner="NOBODY LISTED LLC", lat=39.75)
    fake.set(GREENE, [greene_parcel(owner="UNIT ONE PERSON"), greene_parcel(owner="UNIT TWO PERSON")])
    res = run(conn)
    assert res["ambiguous"] == 1 and people(conn, amb) == [] and mail(conn, amb) == []
    assert scalar(conn, "SELECT status FROM parcel_lookups WHERE property_id=?", (amb,)) == "ambiguous"
    assert pid


# --- the limit, the order, the estimate ------------------------------------------------------------

def test_limit_looks_up_the_best_buy_box_properties_first(conn, fake):
    box = [add_prop(conn, lat=39.60 + i / 100, score=0.9 - i / 100) for i in range(3)]
    out = [add_prop(conn, lat=39.70 + i / 100, score=0.99, in_buybox=0) for i in range(4)]
    fake.set(GREENE, [greene_parcel()])
    res = run(conn, limit=5)
    assert res["properties"] == 5 and res["left_for_later"] == 2
    done = {r["property_id"] for r in rows(conn, "SELECT property_id FROM parcel_lookups")}
    assert set(box) <= done and len(done & set(out)) == 2
    assert run(conn)["properties"] == 2                       # the rest on the next run


def test_buy_box_only_setting(conn, fake, settings):
    settings.parcels["settings"]["scope"] = "buybox"
    add_prop(conn)
    add_prop(conn, in_buybox=0)
    fake.set(GREENE, [greene_parcel()])
    assert run(conn)["properties"] == 1


def test_estimate_counts_properties_per_county_and_what_is_saved(conn, fake):
    add_prop(conn, county="GREENE")
    add_prop(conn, county="GREENE", lat=39.7)
    add_prop(conn, county="MONTGOMERY", lat=39.76, lng=-84.19)
    add_prop(conn, lat=None, lng=None)
    add_prop(conn, state="IN", county="ALLEN")
    fake.set(GREENE, [greene_parcel()])
    est = run(conn, dry_run=True)
    assert fake.calls == [] and est["dry_run"] and est["to_look_up"] == 3 and est["with_coordinates"] == 4
    assert est["by_county"]["Greene"] == {"properties": 2, "requests": 2, "cached": 0, "older": False}
    assert est["by_county"]["Statewide (older)"]["requests"] == 2
    assert est["requests"] == 4 and est["cached"] == 0 and est["to_fetch"] == 4 and est["cost"]["usd"] == 0
    assert est["no_coordinates"] == 1 and est["no_source"] == 1
    assert "Free" in est["message"] and "Greene 2" in est["message"]
    run(conn, limit=1)
    after = run(conn, dry_run=True)
    assert after["to_look_up"] == 2 and after["already_done"] == 1 and fake.count() == 1
    conn.execute("DELETE FROM parcel_lookups")
    conn.commit()
    conn.execute("DELETE FROM drill_attempts WHERE stage_key='county_auditor'")   # (a 'found' check-off also skips it)
    conn.commit()
    assert run(conn, dry_run=True)["cached"] == 1


def test_progress_is_reported_and_a_cancel_gets_through(conn, fake):
    for i in range(3):
        add_prop(conn, lat=39.6 + i / 100)
    fake.set(GREENE, [greene_parcel()])
    seen = []
    run(conn, progress=lambda f, m: seen.append(f))
    assert seen[0] == 0.0 and seen[-1] == 1.0 and seen == sorted(seen)

    class Cancelled(BaseException):
        pass

    def cancel(fraction, message):
        raise Cancelled()
    add_prop(conn, lat=39.9)
    with pytest.raises(Cancelled):
        run(conn, progress=cancel)


# --- the DRILL stays honest ------------------------------------------------------------------------

def test_automatic_county_checks_do_not_count_as_hand_check_results(conn, fake):
    pid = add_prop(conn)
    fake.set(GREENE, [greene_parcel()])
    run(conn)
    market = market_by_slug(conn, MARKET)
    counts = stats.counts_by_market(conn, [market["id"]])
    assert counts.get((market["id"], "county_auditor", "all"), {"tried": 0})["tried"] == 0
    other = add_prop(conn, lat=39.7)
    E.record_attempt(conn, other, "county_auditor", "found", "by hand")
    assert stats.counts_by_market(conn, [market["id"]])[(market["id"], "county_auditor", "all")]["tried"] == 1
    assert pid


def test_the_drill_records_baseline_still_works_after_county_records(conn, fake, market_slug):
    pid = add_prop(conn, owner="ACME PORTFOLIO LLC")
    fake.set(GREENE, [greene_parcel(owner="OTHER NAME LLC")])
    run(conn)
    E.run_auto_stages(conn, market_slug)
    d = drill_of(conn, pid)
    assert stage(d, "records_baseline")["status"] == "done_found" and d["level"] == 2
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE property_id=? AND stage_key='_new_pass'", (pid,)) == 0
    assert {p["name"] for p in d["people"]} >= {"ACME PORTFOLIO LLC", "OTHER NAME LLC"}


# --- the step in the pipeline ----------------------------------------------------------------------

def test_the_pipeline_step_and_its_alias(conn, fake):
    add_prop(conn)
    fake.set(GREENE, [greene_parcel()])
    assert "parcels" in pipeline.STEPS and pipeline.STEP_LABELS["parcels"] == "County records"
    assert pipeline.normalize_step("county-records") == "parcels"
    dry = pipeline.run_step(conn, MARKET, "parcels", limit=5, dry_run=True)
    assert dry["ok"] and dry["dry_run"] and dry["estimate"]["to_look_up"] == 1 and fake.calls == []
    res = pipeline.run_step(conn, MARKET, "parcels", limit=5)
    assert res["ok"] and res["label"] == "County records" and not res["needs_confirm"]
    assert res["counts"]["found"] == 1 and res["counts"]["contacts_added"] == 1
    assert "owner on the tax record" in res["message"]


def test_the_step_is_free_part_of_all_and_estimated(conn, fake):
    assert "parcels" not in pipeline.PAID_STEPS
    plan = [k for k, _ in pipeline.ALL_PLAN]
    assert plan.index("score") < plan.index("parcels") < plan.index("photos")
    assert round(sum(share for _, share in pipeline.ALL_PLAN), 6) == 1.0
    add_prop(conn)
    est = pipeline.estimate(conn, MARKET)
    line = next(s for s in est["steps"] if s["step"] == "parcels")
    assert line["paid"] is False and line["usd"] == 0 and line["to_do"] == 1 and not line["needs_confirm"]
    assert est["total_usd"] == round(sum(s["usd"] for s in est["steps"]), 2)


def test_an_error_in_the_county_step_never_stops_all(conn, fake, monkeypatch):
    add_prop(conn)
    fake.set(GREENE, httpx.Response(500, text="down"))
    res = pipeline.run_all(conn, MARKET)
    by_step = {s["step"]: s for s in res["steps"]}
    assert by_step["parcels"]["ok"] and "failed" in by_step["parcels"]["message"]
    assert by_step["parcels"]["warnings"] and by_step["drill"]["ok"]


def test_the_progress_layer_counts_ohio_properties_looked_up(conn, fake):
    market = market_by_slug(conn, MARKET)
    add_prop(conn)
    add_prop(conn, lat=39.7)
    add_prop(conn, state="IN", county="ALLEN")
    assert parcels.layer_counts(conn, market["id"]) == (0, 2)
    fake.set(GREENE, [greene_parcel()])
    run(conn, limit=1)
    assert parcels.layer_counts(conn, market["id"]) == (1, 2)
    layers = {x["key"]: x for x in pipeline.layer_status(conn, MARKET)}
    assert layers["parcels"]["done"] == 1 and layers["parcels"]["total"] == 2


def test_the_run_screen_and_the_command_line_know_the_step():
    import re
    from pathlib import Path

    import run

    js = (Path(__file__).resolve().parent.parent / "bluemoon" / "web" / "static" / "run.js").read_text(encoding="utf-8")
    keys = re.findall(r"^    \{key: '([a-z_]+)'", js, re.M)
    assert "parcels" in keys and keys.index("score") < keys.index("parcels") < keys.index("photos")
    assert "title: 'County records'" in js and "layers: ['parcels']" in js
    assert "parcels" in run.STEP_HELP and "County records" in run.STEP_HELP["parcels"]
    assert "parcels" in run.build_parser().format_help() if hasattr(run, "build_parser") else True
