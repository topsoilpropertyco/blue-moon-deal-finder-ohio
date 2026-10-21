"""Owner signals: owner type, best-guess person, portfolio groups, absentee, years owned, debt.

All names and addresses here are made up.
"""
from __future__ import annotations

from datetime import date
from itertools import count

import pytest

from bluemoon.db import market_by_slug, now_iso, one, rows
from bluemoon.enrich import owners as ow
from bluemoon.enrich.owners import (
    OWNER_TYPES,
    OwnerConfig,
    absentee_status,
    build_rules,
    classify_owner,
    clean_owner_name,
    compute_owners,
    count_sites,
    debt_status,
    final_type,
    mailing_key,
    owner_group_name,
    person_from_owner,
    years_owned,
)

TODAY = date(2026, 10, 21)
RULES = build_rules()
_ids = count(1)


# --- helpers -------------------------------------------------------------------------------------

def add_property(conn, market_slug: str, **fields) -> int:
    """Insert one made-up property row and return its id."""
    m = market_by_slug(conn, market_slug)
    n = next(_ids)
    data = {"market_id": m["id"], "property_key": f"test:{n}", "state": "OH", "city": "SPRINGFIELD",
            "raw_json": "{}", "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso()}
    data.update(fields)
    cols = ",".join(data)
    cur = conn.execute(f"INSERT INTO properties({cols}) VALUES({','.join('?' for _ in data)})", tuple(data.values()))
    conn.commit()
    return cur.lastrowid


def owner_row(conn, pid: int) -> dict:
    return one(conn, "SELECT * FROM owners WHERE property_id=?", (pid,))


# Points about 1 km apart (well over 200 m) around downtown Springfield.
def spread(i: int) -> dict:
    return {"lat": 39.90 + 0.01 * i, "lng": -83.80}


# --- classify_owner ------------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("DOE JOHN & JANE", "individual"),
    ("SMITH ROBERT", "individual"),
    ("SMITH JOHN A JR", "individual"),
    ("SMITH JOHN TOD", "individual"),
    ("SMITH JOHN ET AL", "individual"),
    ("JONES MARY LIFE ESTATE", "individual"),
    ("HILL JOHN", "individual"),            # place word as a surname
    ("ROBBINS GLEN A", "individual"),       # place word as a first name, ends in an initial
    ("HOLLOWAY FAMILY TRUST", "trust"),
    ("SMITH JOHN A TR", "trust"),
    ("SMITH JOHN A TRUSTEE", "trust"),
    ("SMITH JOHN CO-TRUSTEE", "trust"),
    ("JOHN A SMITH REVOCABLE LIVING TRUST", "trust"),
    ("SMITH JOHN TTEE U/A DTD 01/02/2003", "trust"),
    ("FIRST TEST NATIONAL BANK OF OHIO TRUSTEE", "trust"),  # a bank running a family trust
    ("ESTATE OF MARY SMITH", "estate"),
    ("SMITH MARY ESTATE", "estate"),
    ("HEIRS OF JOHN DOE", "estate"),
    ("SMITH JOHN DECEASED", "estate"),
    ("ABC PROPERTIES, L.L.C.", "small_llc"),
    ("ABC PROPERTIES L L C", "small_llc"),
    ("GEM CITY PROPERTIES LP", "small_llc"),
    ("FOX RUN DELAWARE L P", "small_llc"),
    ("MAPLE COURT LTD", "small_llc"),
    ("SAMPLE ASSOCIATES LIMITED PARTNERSHIP", "small_llc"),
    ("ABC REAL ESTATE", "small_llc"),       # REAL ESTATE is not an estate
    ("NORTHSIDE RENTALS", "small_llc"),     # business word without a suffix
    ("LINDEN GROVE", "small_llc"),          # property name without a suffix
    ("THE LOFTS AT MAIN", "small_llc"),
    ("123 MAIN STREET", "small_llc"),
    ("CHURCH STREET APARTMENTS LLC", "small_llc"),
    ("UNIVERSITY VILLAGE APARTMENTS", "small_llc"),
    ("XYZ HOLDINGS INC", "corporation"),
    ("ACME & CO", "corporation"),
    ("SAMPLE REALTY COMPANY", "corporation"),
    ("MIDWEST MULTIFAMILY INCOME REIT", "institutional"),
    ("MAPLE FUND III LP", "institutional"),
    ("EXAMPLE PORTFOLIO 20 DST", "institutional"),
    ("PUBLIC STORAGE PARTNERS", "institutional"),
    ("FIRST TEST BANK", "institutional"),
    ("US BANK NATIONAL ASSOCIATION", "institutional"),
    ("FEDERAL NATIONAL MORTGAGE ASSOCIATION", "institutional"),
    ("CITY OF SPRINGFIELD", "government"),
    ("SPRINGFIELD CITY OF", "government"),
    ("CITY OF COLUMBUS OHIO", "government"),
    ("STATE OF OHIO", "government"),
    ("VILLAGE OF YELLOW SPRINGS", "government"),
    ("WALNUT TOWNSHIP TRUSTEES", "government"),
    ("TRUSTEES OF SAMPLE TWP", "government"),
    ("CLARK COUNTY", "government"),
    ("COUNTY OF GREENE", "government"),
    ("SPRINGFIELD METROPOLITAN HOUSING AUTHORITY", "government"),
    ("CLARK COUNTY LAND REUTILIZATION CORP", "government"),
    ("SAMPLE CITY BOARD OF EDUCATION", "government"),
    ("SAMPLE VILLAGE EXEMPT S D", "government"),
    ("FIRST BAPTIST CHURCH OF SAMPLETOWN", "nonprofit"),
    ("GRACE MINISTRIES INC", "nonprofit"),
    ("SAMPLE FOUNDATION", "nonprofit"),
    ("HABITAT FOR HUMANITY OF SAMPLE COUNTY INC", "nonprofit"),
    ("CLARK COUNTY COMMUNITY ACTION", "nonprofit"),
    ("SAMPLE COMMUNITY DEVELOPMENT CORP", "nonprofit"),
    ("UNIVERSITY OF SAMPLE", "nonprofit"),
    ("SAMPLE STATE COMMUNITY COLLEGE", "nonprofit"),
    ("MEADOWS CONDO ASSOCIATION", "nonprofit"),
    ("SAMPLE BOARD OF REALTORS", "nonprofit"),
    ("THE VILLAGE OF MEADOWS", "small_llc"),  # an apartment name, not a village government
    ("OWNER RECORD", "unknown"),
    ("UNKNOWN", "unknown"),
    ("C/O SOMEONE", "unknown"),
    ("SMITH", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
    ("   ", "unknown"),
    ("12345", "unknown"),
])
def test_classify_owner(name, expected):
    assert classify_owner(name, RULES) == expected


def test_classify_owner_never_returns_portfolio_and_always_a_known_type():
    names = ["DOE JOHN", "ABC LLC", "CITY OF X", None, "ZZZ", "A B C D E F G H", "&&&", "TRUST", "LLC"]
    for n in names:
        t = classify_owner(n, RULES)
        assert t in OWNER_TYPES and t != "portfolio"


def test_classify_owner_uses_settings_when_no_rules_given(settings):
    assert classify_owner("HOLLOWAY FAMILY TRUST") == "trust"


def test_clean_owner_name():
    assert clean_owner_name("  Abc Properties, L.L.C. ") == "ABC PROPERTIES LLC"
    assert clean_owner_name("SMITH JOHN C/O ACME TAX SERVICE") == "SMITH JOHN"
    assert clean_owner_name("SMITH JOHN ATTN: BOOKKEEPING") == "SMITH JOHN"
    assert clean_owner_name("O'BRIEN PAT") == "OBRIEN PAT"
    assert clean_owner_name("O'BRIEN PAT", keep_apostrophes=True) == "O'BRIEN PAT"
    assert clean_owner_name(None) is None
    assert clean_owner_name("") is None


# --- person_from_owner ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("DOE JOHN & JANE", "John & Jane Doe"),
    ("HOLLOWAY FAMILY TRUST", None),
    ("ESTATE OF MARY SMITH", "Mary Smith (estate)"),
    ("SMITH MARY ESTATE", "Mary Smith (estate)"),
    ("HEIRS OF JOHN DOE", "John Doe (estate)"),
    ("SMITH ROBERT", "Robert Smith"),
    ("JOHN SMITH", "John Smith"),
    ("SMITH JOHN A", "John A Smith"),
    ("SMITH JOHN A JR", "John A Smith Jr"),
    ("SMITH JOHN A & MARY B", "John A & Mary B Smith"),
    ("SMITH JOHN & MARY ANN", "John & Mary Ann Smith"),
    ("SMITH JOHN & JONES MARY", "John Smith & Mary Jones"),
    ("SMITH JOHN TOD", "John Smith"),
    ("SMITH JOHN ET AL", "John Smith"),
    ("MCDONALD RONALD", "Ronald McDonald"),
    ("O'BRIEN PATRICK", "Patrick O'Brien"),
    ("SMITH-JONES ANNA M", "Anna M Smith-Jones"),
    ("SMITH JOHN A TR", "John A Smith (trustee)"),
    ("SMITH JOHN TTEE", "John Smith (trustee)"),
    ("SMITH JOHN A REVOCABLE TRUST", "John A Smith (trust)"),
    ("JOHN A SMITH REVOCABLE TRUST", "John A Smith (trust)"),
    ("REVOCABLE TRUST OF CARLA L SAMPLE", "Carla L Sample (trust)"),
    ("FIRST TEST NATIONAL BANK OF OHIO TRUSTEE", None),
    ("ABC PROPERTIES LLC", None),
    ("CITY OF SPRINGFIELD", None),
    ("MIDWEST MULTIFAMILY INCOME REIT", None),
    ("OWNER RECORD", None),
    (None, None),
    ("SMITH", None),
])
def test_person_from_owner(name, expected):
    assert person_from_owner(name, RULES) == expected


# --- grouping keys -------------------------------------------------------------------------------

def test_owner_group_name_drops_entity_suffixes():
    assert owner_group_name("ABC PROPERTIES, L.L.C.") == owner_group_name("ABC Properties LLC") == "ABC PROPERTIES"
    assert owner_group_name("THE ABC COMPANY") == "ABC"
    assert owner_group_name("OWNER RECORD") is None
    assert owner_group_name(None) is None


def test_mailing_key_normalizes_and_includes_unit():
    a = mailing_key({"mailing_address": "100 Main Street", "mailing_city": "Dayton", "mailing_state": "OH",
                     "mailing_zip": "45402-1234"})
    b = mailing_key({"mailing_address": "100 MAIN ST", "mailing_city": "DAYTON", "mailing_state": "OH",
                     "mailing_zip": "45402"})
    assert a == b
    c = mailing_key({"mailing_address": "100 MAIN ST", "mailing_unit": "STE 200", "mailing_city": "DAYTON",
                     "mailing_state": "OH", "mailing_zip": "45402"})
    assert c != a
    assert mailing_key({"mailing_city": "DAYTON"}) is None


# --- absentee / years / debt ---------------------------------------------------------------------

@pytest.mark.parametrize("prop, expected", [
    ({"state": "OH", "city": "SPRINGFIELD", "mailing_state": "FL", "mailing_city": "MIAMI"}, "outofstate"),
    ({"state": "OH", "city": "SPRINGFIELD", "mailing_state": "OH", "mailing_city": "COLUMBUS"}, "instate"),
    ({"state": "OH", "city": "SPRINGFIELD", "mailing_state": "OH", "mailing_city": "Springfield"}, "local"),
    ({"state": "OH", "city": "SPRINGFIELD TWP", "mailing_state": "oh", "mailing_city": "SPRINGFIELD"}, "local"),
    ({"state": "OH", "city": "SPRINGFIELD", "mailing_state": None, "mailing_city": "SPRINGFIELD"}, "unknown"),
    ({"state": None, "city": "SPRINGFIELD", "mailing_state": "OH", "mailing_city": "SPRINGFIELD"}, "unknown"),
    ({"state": "OH", "city": None, "mailing_state": "OH", "mailing_city": "DAYTON"}, "unknown"),
    ({"state": "OH", "city": "XENIA", "mailing_state": "IN", "mailing_city": None}, "outofstate"),
])
def test_absentee_status(prop, expected):
    assert absentee_status(prop) == expected


def test_years_owned():
    assert years_owned("2006-10-21", TODAY) == 20.0
    assert years_owned("10/21/2016", TODAY) == 10.0
    assert years_owned("2030-01-01", TODAY) is None      # future date: bad data
    assert years_owned(None, TODAY) is None
    assert years_owned("not a date", TODAY) is None


@pytest.mark.parametrize("prop, owned, expected", [
    ({}, 25.0, "free_and_clear"),
    ({}, 10.0, "free_and_clear"),
    ({}, 9.9, "unknown"),
    ({}, None, "unknown"),
    ({"lender": "FIRST TEST BANK", "loan_maturity": "2027-06-01"}, 5.0, "maturing_soon"),
    ({"lender": "FIRST TEST BANK", "loan_maturity": "2028-04-01"}, 5.0, "maturing_soon"),
    ({"lender": "FIRST TEST BANK", "loan_maturity": "2026-01-01"}, 5.0, "maturing_soon"),   # just matured
    ({"lender": "FIRST TEST BANK", "loan_maturity": "2015-01-01"}, 12.0, "free_and_clear"),  # long ago: paid off
    ({"lender": "FIRST TEST BANK", "loan_maturity": "2035-01-01"}, 5.0, "active"),
    ({"lender": "FIRST TEST BANK"}, 30.0, "active"),
    ({"loan_amount": 250000.0}, 30.0, "active"),
    ({"loan_amount": 0}, 30.0, "free_and_clear"),
])
def test_debt_status(prop, owned, expected):
    assert debt_status(prop, owned, TODAY, OwnerConfig()) == expected


def test_debt_status_window_comes_from_config():
    prop = {"lender": "FIRST TEST BANK", "loan_maturity": "2027-12-01"}  # about 13 months out
    assert debt_status(prop, 5.0, TODAY, OwnerConfig(maturing_soon_months=6)) == "active"
    assert debt_status(prop, 5.0, TODAY, OwnerConfig(maturing_soon_months=18)) == "maturing_soon"


# --- sites and final type ------------------------------------------------------------------------

def test_count_sites_merges_nearby_parcels():
    near = [{"id": i, "property_key": f"k{i}", "lat": 39.9000 + 0.0003 * i, "lng": -83.8} for i in range(5)]
    assert count_sites(near, 200) == 1          # 5 parcels of one complex
    far = [{"id": i, "property_key": f"k{i}", **spread(i)} for i in range(5)]
    assert count_sites(far, 200) == 5
    no_xy = [{"id": 1, "property_key": "a", "lat": None, "lng": None},
             {"id": 2, "property_key": "b", "lat": None, "lng": None}]
    assert count_sites(no_xy, 200) == 2
    same_parcel_two_markets = [{"id": 1, "property_key": "apn:OH:CLARK:1", **spread(0)},
                               {"id": 2, "property_key": "apn:OH:CLARK:1", **spread(0)}]
    assert count_sites(same_parcel_two_markets, 0) == 1


def test_final_type_rules():
    cfg = OwnerConfig()
    assert final_type("small_llc", 4, cfg) == "small_llc"
    assert final_type("small_llc", 5, cfg) == "portfolio"
    assert final_type("individual", 7, cfg) == "portfolio"
    assert final_type("nonprofit", 6, cfg) == "portfolio"
    assert final_type("government", 60, cfg) == "government"
    assert final_type("institutional", 1, cfg) == "institutional"
    assert final_type("small_llc", 40, cfg) == "institutional"
    assert final_type("unknown", 1, cfg) == "unknown"


# --- compute_owners (database) -------------------------------------------------------------------

def test_compute_owners_portfolio_by_name_and_mailing(conn, settings, market_slug):
    # Same owner name, five separate sites -> portfolio.
    by_name = [add_property(conn, market_slug, owner_name=f"GEM CITY PROPERTIES {'LLC' if i % 2 else 'L.L.C.'}",
                            **spread(i)) for i in range(5)]
    # Five different LLC names sharing one mailing address -> one owner, portfolio.
    by_mail = [add_property(conn, market_slug, owner_name=f"MAPLE {i} LLC", mailing_address="9 SAMPLE PLAZA",
                            mailing_city="DAYTON", mailing_state="OH", mailing_zip="45402", **spread(10 + i))
               for i in range(5)]
    # Five parcels of ONE complex (within 200 m) -> one site, not a portfolio.
    complex_ = [add_property(conn, market_slug, owner_name="ONE COMPLEX LLC", lat=39.95 + 0.0002 * i, lng=-83.7)
                for i in range(5)]
    # A government owner with many sites stays government.
    gov = [add_property(conn, market_slug, owner_name="CITY OF SPRINGFIELD", **spread(20 + i)) for i in range(6)]
    loner = add_property(conn, market_slug, owner_name="DOE JOHN & JANE", **spread(40))
    blank = add_property(conn, market_slug, owner_name=None, **spread(41))
    placeholder = [add_property(conn, market_slug, owner_name="OWNER RECORD", **spread(50 + i)) for i in range(6)]

    res = compute_owners(conn, market_slug, today=TODAY)

    assert {owner_row(conn, p)["owner_type"] for p in by_name} == {"portfolio"}
    assert len({owner_row(conn, p)["portfolio_key"] for p in by_name}) == 1
    assert owner_row(conn, by_name[0])["portfolio_count"] == 5
    assert {owner_row(conn, p)["owner_type"] for p in by_mail} == {"portfolio"}
    assert len({owner_row(conn, p)["portfolio_key"] for p in by_mail}) == 1
    assert {owner_row(conn, p)["owner_type"] for p in complex_} == {"small_llc"}
    assert owner_row(conn, complex_[0])["portfolio_count"] == 1
    assert len({owner_row(conn, p)["portfolio_key"] for p in complex_}) == 1   # still siblings for the DRILL
    assert {owner_row(conn, p)["owner_type"] for p in gov} == {"government"}
    assert owner_row(conn, loner)["owner_type"] == "individual"
    assert owner_row(conn, loner)["portfolio_count"] == 1
    assert owner_row(conn, blank)["owner_type"] == "unknown"
    assert owner_row(conn, blank)["portfolio_key"] is None
    # Placeholder names never group anything together.
    assert {owner_row(conn, p)["owner_type"] for p in placeholder} == {"unknown"}
    assert len({owner_row(conn, p)["portfolio_key"] for p in placeholder}) <= 1
    assert all(owner_row(conn, p)["portfolio_key"] is None for p in placeholder)

    assert res["properties"] == 5 + 5 + 5 + 6 + 1 + 1 + 6
    assert sum(res["owner_types"].values()) == res["properties"]
    assert res["portfolio_properties"] == 10
    assert res["portfolio_groups"] == 2
    assert "Checked the owners" in res["message"]


def test_compute_owners_writes_absentee_years_debt(conn, settings, market_slug):
    pid = add_property(conn, market_slug, owner_name="ESTATE OF MARY TESTPERSON", city="OXFORD", state="OH",
                       mailing_address="1 ELSEWHERE RD", mailing_city="MIAMI", mailing_state="FL",
                       sale_date="2000-01-01", **spread(0))
    loan = add_property(conn, market_slug, owner_name="EXAMPLE AVE APARTMENTS LLC", city="SPRINGFIELD",
                        mailing_address="PO BOX 100", mailing_city="COLUMBUS", mailing_state="OH",
                        sale_date="2021-03-15", lender="FIRST TEST BANK", loan_maturity="2027-03-01", **spread(1))
    compute_owners(conn, market_slug, today=TODAY)
    r = owner_row(conn, pid)
    assert (r["owner_type"], r["absentee"], r["debt_status"]) == ("estate", "outofstate", "free_and_clear")
    assert r["years_owned"] == pytest.approx(26.8, abs=0.1)
    assert r["owner_norm"] == "ESTATE OF MARY TESTPERSON"
    r = owner_row(conn, loan)
    assert (r["owner_type"], r["absentee"], r["debt_status"]) == ("small_llc", "instate", "maturing_soon")
    assert r["computed_at"]


def test_compute_owners_groups_across_markets(conn, settings, market_slug):
    other = "self-storage-springfield-oh"
    mine = [add_property(conn, market_slug, owner_name="RIVERBEND HOLDINGS LLC", **spread(i)) for i in range(3)]
    for i in range(3):
        add_property(conn, other, owner_name="RIVERBEND HOLDINGS LLC", **spread(10 + i))
    res = compute_owners(conn, market_slug, today=TODAY)
    assert res["properties"] == 3                                 # only this market is written
    assert owner_row(conn, mine[0])["portfolio_count"] == 6
    assert owner_row(conn, mine[0])["owner_type"] == "portfolio"
    assert rows(conn, "SELECT o.* FROM owners o JOIN properties p ON p.id=o.property_id "
                      "JOIN markets m ON m.id=p.market_id WHERE m.slug=?", (other,)) == []


def test_compute_owners_is_idempotent(conn, settings, market_slug):
    for i in range(6):
        add_property(conn, market_slug, owner_name="SAME OWNER LLC", sale_date="2001-01-01", **spread(i))
    first = compute_owners(conn, market_slug, today=TODAY)
    snapshot = rows(conn, "SELECT property_id, owner_type, portfolio_key, portfolio_count, absentee, years_owned, "
                          "debt_status FROM owners ORDER BY property_id")
    second = compute_owners(conn, market_slug, today=TODAY)
    again = rows(conn, "SELECT property_id, owner_type, portfolio_key, portfolio_count, absentee, years_owned, "
                       "debt_status FROM owners ORDER BY property_id")
    assert snapshot == again
    assert {k: v for k, v in first.items() if k != "message"} == {k: v for k, v in second.items() if k != "message"}


def test_compute_owners_empty_market(conn, settings, market_slug):
    res = compute_owners(conn, market_slug, today=TODAY)
    assert res["properties"] == 0
    assert "No properties" in res["message"]


def test_compute_owners_unknown_market(conn, settings):
    with pytest.raises(KeyError):
        compute_owners(conn, "no-such-market")


# --- config --------------------------------------------------------------------------------------

def _with_config(tmp_root, text: str):
    from bluemoon.config import get_settings
    with (tmp_root / "config.yaml").open("a", encoding="utf-8") as f:
        f.write("\n" + text + "\n")
    get_settings.cache_clear()
    return get_settings()


def test_thresholds_come_from_config(tmp_root, conn, settings, market_slug):
    pids = [add_property(conn, market_slug, owner_name="TRIO RENTALS LLC", **spread(i)) for i in range(3)]
    compute_owners(conn, market_slug, today=TODAY)
    assert owner_row(conn, pids[0])["owner_type"] == "small_llc"
    s2 = _with_config(tmp_root, "owners:\n  portfolio_min_properties: 3\n")
    compute_owners(conn, market_slug, today=TODAY, settings=s2)
    assert owner_row(conn, pids[0])["owner_type"] == "portfolio"


def test_overrides_and_extra_keywords_from_config(tmp_root, settings):
    assert classify_owner("GREATER SAMPLE PREMIER MANAGEMENT") == "small_llc"
    s2 = _with_config(tmp_root, "owners:\n"
                                "  keywords:\n    government: [GREATER SAMPLE PREMIER MANAGEMENT]\n"
                                "  overrides:\n    'COFORGE SAMPLE': corporation\n"
                                "  institutional_names: [BIGCO RESIDENTIAL]\n")
    rules = ow.rules_for(s2)
    assert classify_owner("GREATER SAMPLE PREMIER MANAGEMENT", rules) == "government"
    assert classify_owner("Coforge Sample", rules) == "corporation"
    assert classify_owner("BIGCO RESIDENTIAL OHIO LLC", rules) == "institutional"


def test_bad_override_type_falls_back_to_built_in_rules(tmp_root, settings):
    s2 = _with_config(tmp_root, "owners:\n  overrides:\n    'SOMEBODY': not_a_type\n")
    rules = ow.rules_for(s2)
    assert classify_owner("SOMEBODY SAMPLE LLC", rules) == "small_llc"


def test_mailing_address_used_by_too_many_names_is_ignored(tmp_root, conn, settings, market_slug):
    s2 = _with_config(tmp_root, "owners:\n  mailing_group_max_names: 3\n")
    pids = [add_property(conn, market_slug, owner_name=f"OWNER {i} LLC", mailing_address="PO BOX 1",
                         mailing_city="TAXVILLE", mailing_state="TX", **spread(i)) for i in range(5)]
    compute_owners(conn, market_slug, today=TODAY, settings=s2)
    assert len({owner_row(conn, p)["portfolio_key"] for p in pids}) == 5
    assert {owner_row(conn, p)["owner_type"] for p in pids} == {"small_llc"}


# --- real export (local only) --------------------------------------------------------------------

def test_real_export_owner_types(conn, settings, market_slug, real_crexi):
    from bluemoon.ingest.crexi import import_file
    import_file(conn, market_slug, real_crexi)
    res = compute_owners(conn, market_slug)
    assert res["properties"] > 0
    assert sum(res["owner_types"].values()) == res["properties"]
    found = {r["owner_type"] for r in rows(conn, "SELECT DISTINCT owner_type FROM owners")}
    assert found <= set(OWNER_TYPES)
