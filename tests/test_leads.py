"""Leads and batches: adding a property by address or Crexi link, sure and unsure matches, what an import does with
asking price and advertised rent, and "Prepare offers for selected" (per-row states, partial failure, no duplicate
offers on a retry). Made-up properties only."""
from __future__ import annotations

import csv
import itertools
from datetime import date

import pytest
from fastapi.testclient import TestClient

from bluemoon import leads, napkin, offer_batches, offers, pricing
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.ingest import import_file

MARKET = "apartments-springfield-oh"
CFG = pricing.load_config({"evaluation_date": date(2026, 11, 5)})
LINK = "https://www.crexi.com/properties/1234567/ohio-example-apartments"
_ids = itertools.count(1)


def market(conn):
    return market_by_slug(conn, MARKET)


def add_prop(conn, **kw) -> int:
    n = next(_ids)
    data = {"market_id": market(conn)["id"], "property_key": f"leads:{n}", "address": f"{n} LEAD LN", "city": "SPRINGFIELD",
            "state": "OH", "zip": "45503", "units": 32, "owner_name": "EXAMPLE HOLDINGS LLC", "raw_json": "{}",
            "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso()}
    data.update(kw)
    pid = int(conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                           tuple(data.values())).lastrowid)
    conn.commit()
    return pid


def rent(conn, pid, monthly="34240") -> None:
    napkin.add_record(conn, pid, source="manual", basis="scheduled", actor="seth", total=monthly, total_period="monthly")


def prop(conn, pid):
    return one(conn, "SELECT * FROM properties WHERE id = ?", (pid,))


def basis(conn):
    def find(pid):
        p = prop(conn, pid)
        if not p or p["market_id"] != market(conn)["id"]:
            raise KeyError(f"Property {pid} is not in this market.")
        return p, napkin.snapshot(conn, p, "apartments", CFG, None)
    return find


def count(conn, table):
    return scalar(conn, f"SELECT COUNT(*) FROM {table}")


# --- V33: add a property by address or by Crexi link -------------------------------------------------------

def test_an_address_makes_a_lead_that_says_it_was_added_by_hand(conn):
    r = leads.add(conn, market(conn), {"address": "12 Maple St", "city": "Springfield", "state": "oh", "zip": "45503"}, "seth")
    p = prop(conn, r["property_id"])
    assert r["outcome"] == "created"
    assert (p["address"], p["state"], p["source"], p["crexi_url"], p["units"], p["owner_name"]) == ("12 Maple St", "OH", "manual", None, None, None)
    assert "manual_lead" in p["flags_json"] and p["property_key"].startswith("addr:")
    assert [e["kind"] for e in leads.events(conn, p["id"])] == ["new_listing"]


def test_a_crexi_link_is_saved_as_the_source_and_nothing_is_pretended_fetched(conn):
    r = leads.add(conn, market(conn), {"link": LINK}, "seth")
    p = prop(conn, r["property_id"])
    assert (p["crexi_url"], p["source"], p["source_id"], p["address"], p["units"], p["owner_name"]) == (LINK, "crexi", "1234567", None, None, None)
    assert p["property_key"] == "src:crexi:1234567" and p["raw_json"] == "{}"
    event = leads.events(conn, p["id"])[0]
    assert event["detail"]["fetched"] is False and "nothing was fetched" in r["message"]


@pytest.mark.parametrize("fields,message", [
    ({}, "street address or paste a Crexi link"),
    ({"address": "1 Main St", "link": "https://example.com/x"}, "Crexi link"),
    ({"address": "1 Main St", "link": "javascript:alert(1)"}, "Crexi link"),
    ({"address": "1 Main St", "state": "Ohio"}, "two-letter state"),
    ({"address": "1 Main St", "zip": "455031"}, "five-digit ZIP"),
    ({"address": "x" * 201}, "longer than 200"),
])
def test_bad_input_is_refused_in_plain_english(conn, fields, message):
    with pytest.raises(ValueError, match=message):
        leads.add(conn, market(conn), fields, "seth")
    assert count(conn, "properties") == 0


# --- V34: sure matches are never added twice; unsure ones wait for you ------------------------------------

def test_the_same_address_again_is_a_duplicate_not_a_second_property(conn):
    first = leads.add(conn, market(conn), {"address": "12 Maple St", "city": "Springfield", "state": "OH", "zip": "45503"}, "seth")
    again = leads.add(conn, market(conn), {"address": "12 MAPLE STREET", "city": "springfield", "state": "OH", "zip": "45503"}, "seth")
    assert (again["outcome"], again["property_id"]) == ("duplicate", first["property_id"])
    assert count(conn, "properties") == 1
    assert [e["kind"] for e in leads.events(conn, first["property_id"])] == ["duplicate", "new_listing"]


def test_a_known_crexi_link_is_the_same_listing_and_only_fills_a_blank_link(conn):
    pid = add_prop(conn)
    r = leads.add(conn, market(conn), {"address": "999 Different Rd", "link": LINK}, "seth")
    assert r["outcome"] == "created"       # no match yet: a new lead with this link
    other = add_prop(conn, address="5 OTHER ST")
    same = leads.add(conn, market(conn), {"link": LINK + "?utm=x"}, "seth")
    assert (same["outcome"], same["property_id"], same["linked"]) == ("duplicate", r["property_id"], False)
    assert prop(conn, pid)["crexi_url"] is None and prop(conn, other)["crexi_url"] is None
    # an existing property with no link gets it filled in; one that has a different link keeps its own
    p2 = add_prop(conn, address="7 SEVEN ST", city="SPRINGFIELD", zip="45503")
    filled = leads.add(conn, market(conn), {"address": "7 Seven St", "city": "Springfield", "zip": "45503", "link": "https://www.crexi.com/properties/777/seven"}, "seth")
    assert (filled["property_id"], filled["linked"]) == (p2, True) and prop(conn, p2)["crexi_url"].endswith("/777/seven")
    kept = leads.add(conn, market(conn), {"address": "7 Seven St", "city": "Springfield", "zip": "45503", "link": LINK.replace("1234567", "888")}, "seth")
    assert kept["linked"] is False and "different Crexi link" in kept["message"]
    assert prop(conn, p2)["crexi_url"].endswith("/777/seven")


def test_an_unsure_match_is_shown_and_nothing_is_merged_until_you_answer(conn):
    pid = add_prop(conn, address="40 ELM ST", city="SPRINGFIELD", zip="45503", owner_name="REVIEWED OWNER LLC")
    asked = leads.add(conn, market(conn), {"address": "40 Elm St", "city": "Dayton", "link": LINK}, "seth")
    assert asked["outcome"] == "needs_resolution" and asked["matches"][0]["property_id"] == pid
    assert asked["matches"][0]["certainty"] == "maybe" and count(conn, "properties") == 1 and count(conn, "lead_events") == 0
    # same property: the link fills the blank, the reviewed owner and address stay as they were
    same = leads.add(conn, market(conn), {"address": "40 Elm St", "city": "Dayton", "link": LINK}, "seth", same_as=pid)
    p = prop(conn, pid)
    assert same["outcome"] == "duplicate" and p["crexi_url"] == LINK
    assert (p["owner_name"], p["address"], p["city"]) == ("REVIEWED OWNER LLC", "40 ELM ST", "SPRINGFIELD")
    assert leads.events(conn, pid)[0]["kind"] == "same_property"


def test_saying_new_property_adds_it_beside_the_unsure_match(conn):
    pid = add_prop(conn, address="40 ELM ST", city="SPRINGFIELD", zip="45503")
    made = leads.add(conn, market(conn), {"address": "40 Elm St", "city": "Dayton"}, "seth", as_new=True)
    assert made["outcome"] == "created" and made["property_id"] != pid and count(conn, "properties") == 2
    assert leads.events(conn, made["property_id"])[0]["detail"]["after_unsure_match"] is True


def test_same_as_must_be_one_of_the_matches_that_were_shown(conn):
    pid = add_prop(conn)
    with pytest.raises(ValueError, match="not one of the matches"):
        leads.add(conn, market(conn), {"address": "1 Nowhere Ln"}, "seth", same_as=pid)


def test_a_street_without_a_house_number_is_never_called_the_same_building(conn):
    add_prop(conn, address="CHANNINGWAY DR", property_key="x:1")
    r = leads.add(conn, market(conn), {"address": "Channingway Dr", "city": "Elsewhere"}, "seth")
    assert r["outcome"] == "created"


def test_a_lead_matches_when_a_crexi_import_later_brings_the_same_property(conn, tmp_path):
    made = leads.add(conn, market(conn), {"address": "1 Test St", "city": "Springfield", "state": "OH", "zip": "45503"}, "seth")
    path = tmp_path / "later.csv"
    path.write_text("Address,City,State,Zip Code,County,Number of Units\n1 Test St,Springfield,OH,45503,Clark,30\n", encoding="utf-8")
    result = import_file(conn, MARKET, path)
    assert (result["rows_new"], result["rows_updated"]) == (0, 1)
    assert prop(conn, made["property_id"])["units"] == 30


# --- V35: imported prices and rents keep their meaning ----------------------------------------------------

def test_an_imported_asking_price_is_its_own_field_and_advertised_rent_makes_no_rent_record(conn, tmp_path):
    path = tmp_path / "listing.csv"
    path.write_text("Address,City,State,Zip Code,County,Number of Units,Sold Price,Asking Price,Gross Rent\n"
                    "2 Test St,Springfield,OH,45503,Clark,30,527000,1900000,410880\n", encoding="utf-8")
    import_file(conn, MARKET, path)
    p = one(conn, "SELECT * FROM properties WHERE address LIKE '2 TEST%'")
    assert (p["asking_price"], p["sale_price"]) == (1900000, 527000)
    assert count(conn, "rent_records") == 0 and count(conn, "offers") == 0
    snap = napkin.snapshot(conn, p, "apartments", CFG, None)
    assert snap["annual_cents"] is None          # the advertised 410,880 was never taken as the rent


# --- V36: prepare offers for selected ---------------------------------------------------------------------

def priced(conn, **kw):
    pid = add_prop(conn, **kw)
    rent(conn, pid)
    return pid


def run_batch(conn, items, ready=False):
    return offer_batches.create(conn, market(conn)["id"], items, basis(conn), CFG, ready, "seth")


def test_the_preview_shows_each_row_with_what_is_missing(conn):
    ok, norent, drafted = priced(conn), add_prop(conn, owner_name=None), priced(conn)
    offers.prepare(conn, prop(conn, drafted), basis(conn)(drafted)[1], "seth")
    pre = offer_batches.preview(conn, [ok, norent, drafted, 99999], basis(conn), CFG, False)
    by = {r["property_id"]: r for r in pre["rows"]}
    assert (by[ok]["default_cents"], by[ok]["annual_cents"], by[ok]["rent_basis"]) == (164352000, 41088000, napkin.BASIS_LABELS["scheduled"])
    assert {f["key"] for f in by[ok]["flags"]} == {"no_recipient"} and by[ok]["can_prepare"]
    assert {"no_rent", "no_owner"} <= {f["key"] for f in by[norent]["flags"]}
    assert not by[drafted]["can_prepare"] and not by[99999]["can_prepare"]
    assert "internal records, not LOIs" in pre["note"]


def test_a_batch_prepares_4x_defaults_and_typed_prices_and_leaves_rows_out(conn):
    a, b, c = priced(conn), priced(conn), priced(conn)
    rep = run_batch(conn, [{"property_id": a}, {"property_id": b, "price": "1,700,000"}, {"property_id": c, "exclude": True}])
    assert rep["status"] == "done" and rep["counts"] == {"pending": 0, "created": 2, "existing": 0, "failed": 0, "excluded": 1}
    got = {r["property_id"]: one(conn, "SELECT * FROM offers WHERE id = ?", (r["offer_id"],)) for r in rep["rows"] if r["offer_id"]}
    assert (got[a]["price_cents"], got[a]["price_mode"], got[a]["status"], got[a]["origin"]) == (164352000, "default", "internal_draft", "prepared")
    assert (got[b]["price_cents"], got[b]["price_mode"]) == (170000000, "override")
    assert scalar(conn, "SELECT COUNT(*) FROM offers WHERE property_id = ?", (c,)) == 0
    assert all(g["sent_on"] is None for g in got.values()) and "internal record" in rep["rows"][0]["message"]
    assert rep["text"] == "2 offers prepared, 1 left out."


def test_a_row_that_cannot_be_priced_fails_alone_and_a_retry_adds_no_duplicates(conn):
    good, bad = priced(conn), add_prop(conn)
    rep = run_batch(conn, [{"property_id": good}, {"property_id": bad}], ready=True)
    assert rep["status"] == "partial" and (rep["counts"]["created"], rep["counts"]["failed"]) == (1, 1)
    failed = next(r for r in rep["rows"] if r["status"] == "failed")
    assert "no Blue Moon price" in failed["message"] or "unknown" in failed["message"]
    assert count(conn, "offers") == 1
    again = offer_batches.retry(conn, rep["id"], basis(conn), "seth")     # nothing fixed: still failed, still one offer
    assert again["status"] == "partial" and count(conn, "offers") == 1
    rent(conn, bad)
    fixed = offer_batches.retry(conn, rep["id"], basis(conn), "seth")
    assert fixed["status"] == "done" and fixed["counts"]["created"] == 2 and count(conn, "offers") == 2
    final = offer_batches.retry(conn, rep["id"], basis(conn), "seth")
    assert count(conn, "offers") == 2 and final["counts"] == fixed["counts"]
    assert "internal record" not in fixed["rows"][0]["message"]       # a template was active: these are for letters


def test_a_second_batch_never_duplicates_an_unsent_draft_but_a_sent_one_gets_a_revision(conn):
    pid = priced(conn)
    first = run_batch(conn, [{"property_id": pid}])
    second = run_batch(conn, [{"property_id": pid}])
    assert second["rows"][0]["status"] == "existing" and second["rows"][0]["offer_id"] == first["rows"][0]["offer_id"]
    assert count(conn, "offers") == 1
    offers.mark_sent(conn, first["rows"][0]["offer_id"], "email", None, "seth", CFG)
    third = run_batch(conn, [{"property_id": pid}])
    assert (third["rows"][0]["status"], third["rows"][0]["version"]) == ("created", 2)
    assert scalar(conn, "SELECT status FROM offers WHERE version = 1") == "sent"


def test_batch_input_is_checked_before_anything_is_saved(conn):
    pid = priced(conn)
    for items, message in [([], "at least one"), ([{"property_id": pid}, {"property_id": pid}], "twice"),
                           ([{"property_id": 99999}], "not in this market"), ([{"property_id": pid, "price": "abc"}], "not a number"),
                           ([{"property_id": pid, "price": "0"}], "more than \\$0"),
                           ([{"property_id": i} for i in range(1, CFG.batch_max_properties + 2)], "at most")]:
        with pytest.raises(ValueError, match=message):
            run_batch(conn, items)
    assert count(conn, "offer_batches") == 0 and count(conn, "offers") == 0


def test_the_report_csv_is_formula_safe(conn, tmp_path):
    pid = priced(conn, address="=cmd|' /C calc'!A0")
    rep = run_batch(conn, [{"property_id": pid}])
    path = offer_batches.write_report(conn, rep["id"], tmp_path)
    cells = list(csv.reader(path.open(encoding="utf-8-sig")))[1]
    assert cells[1].startswith("'=") and cells[3] == "Draft prepared"


# --- routes -----------------------------------------------------------------------------------------------

@pytest.fixture
def client(conn):
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def api(tail=""):
    return f"/api/m/{MARKET}{tail}"


def test_the_lead_route_walks_through_new_unsure_and_duplicate(client, conn):
    r = client.post(api("/leads"), json={"address": "40 Elm St", "city": "Springfield", "zip": "45503", "link": LINK})
    assert r.status_code == 200 and r.json()["outcome"] == "created" and "after_import" in r.json()
    pid = r.json()["property_id"]
    assert client.get(api(f"/properties/{pid}")).json()["lead_events"][0]["kind"] == "new_listing"
    asked = client.post(api("/leads"), json={"address": "40 Elm St", "city": "Dayton"}).json()
    assert asked["outcome"] == "needs_resolution" and asked["matches"][0]["property_id"] == pid
    dup = client.post(api("/leads"), json={"address": "40 Elm St", "city": "Springfield", "zip": "45503"}).json()
    assert dup["outcome"] == "duplicate"
    assert client.post(api("/leads"), json={"link": "https://evil.example/x"}).status_code == 400
    assert client.post(api("/leads"), json={"address": "1 A St", "same_as": 0}).status_code == 422
    assert client.post("/api/m/nowhere/leads", json={"address": "1 A St"}).status_code == 404


def test_batch_routes_preview_create_retry_report_and_stay_in_their_market(client, conn):
    good, bad = priced(conn), add_prop(conn)
    pre = client.post(api("/offer-batches/preview"), json={"property_ids": [good, bad]}).json()
    assert [r["can_prepare"] for r in pre["rows"]] == [True, True] and pre["letters_ready"] is False
    made = client.post(api("/offer-batches"), json={"rows": [{"property_id": good}, {"property_id": bad}]}).json()
    assert made["status"] == "partial" and made["created_by"] and made["letters_ready"] is False
    rent(conn, bad)
    retried = client.post(api(f"/offer-batches/{made['id']}/retry")).json()
    assert retried["status"] == "done" and count(conn, "offers") == 2
    assert client.get(api(f"/offer-batches/{made['id']}")).json()["counts"]["created"] == 2
    csv_res = client.get(api(f"/offer-batches/{made['id']}/report.csv"))
    assert csv_res.status_code == 200 and "Draft prepared" in csv_res.text
    assert client.get(api("/offers")).json()["total"] == 2
    other = next((m for m in rows(conn, "SELECT slug FROM markets WHERE slug != ?", (MARKET,))), None)
    if other:
        assert client.get(f"/api/m/{other['slug']}/offer-batches/{made['id']}").status_code == 404
    assert client.post(api("/offer-batches"), json={"rows": []}).status_code == 422
    assert client.post(api("/offer-batches"), json={"rows": [{"property_id": 99999}]}).status_code == 400


def test_exports_carry_the_lead_flag_and_the_offer_status(conn, settings):
    from bluemoon import exports, pipeline
    pid = leads.add(conn, market(conn), {"address": "12 Maple St", "city": "Springfield"}, "seth")["property_id"]
    rent(conn, pid)
    run_batch(conn, [{"property_id": pid}])
    pipeline.after_import(conn, MARKET)
    made = exports.export_market(conn, MARKET, only=["ranked_list.csv"])
    found = list(csv.DictReader(made["ranked_list.csv"].open(encoding="utf-8-sig")))
    mine = [r for r in found if r["Address"] == "12 Maple St"]
    assert mine and "manual_lead" in mine[0]["Flags"] and mine[0]["Offer status"] == "Internal draft"
