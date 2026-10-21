"""Tests for DRILL research links (bluemoon/drill/links.py) and the owner-name -> person parser."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from bluemoon.drill import links as L

PROP = {
    "id": 1, "address": "1200 SAMPLE RIDGE DR", "unit": None, "city": "SPRINGFIELD", "state": "OH", "zip": "45503",
    "county": "CLARK COUNTY", "apn": "340-01-00-001-000", "lat": 39.9501, "lng": -83.7702,
    "owner_name": "DOE JOHN & JANE", "crexi_url": "https://www.crexi.com/property-records/sample-0",
}

COUNTIES = {
    "OH": {
        "Clark": {
            "auditor": {"url": "https://auditor.example.gov/search",
                        "template": "https://auditor.example.gov/parcel/{apn_digits}"},
            "rental_registration": "https://auditor.example.gov/rental",
            "recorder": {"url": "https://recorder.example.gov/"},
            "probate": {"url": "https://probate.example.gov/", "template": "https://probate.example.gov/?q={owner_q}"},
            "clerk": {"url": "https://clerk.example.gov/"},
            "gis": {"url": "https://gis.example.gov/", "template": "https://gis.example.gov/?parcel={apn_q}"},
        },
        "GREENE COUNTY": {"auditor": {"url": "https://greene.example.gov/"}},
    },
}


@pytest.fixture
def county_settings(tmp_root, settings):
    """Settings whose sources.yaml has a small, known county table."""
    from bluemoon.config import reload_settings
    path = tmp_root / "config" / "sources.yaml"
    data = yaml.safe_load(path.read_text())
    data["counties"] = COUNTIES
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return reload_settings()


@pytest.fixture
def empty_county_settings(tmp_root, settings):
    from bluemoon.config import reload_settings
    path = tmp_root / "config" / "sources.yaml"
    data = yaml.safe_load(path.read_text())
    data["counties"] = {}
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return reload_settings()


def _by_key(links: list[dict]) -> dict[str, dict]:
    return {x["source_key"]: x for x in links}


# --- person parser --------------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("DOE JOHN & JANE", "John & Jane Doe"),
    ("DOE JOHN A & JANE B", "John A & Jane B Doe"),
    ("DOE JOHN & SMITH MARY", "John Doe & Mary Smith"),
    ("DOE, JOHN", "John Doe"),
    ("DOE JOHN JR", "John Doe Jr"),
    ("JOHNSON WILLIAM H III", "William H Johnson III"),
    ("SMITH JOHN A TR", "John A Smith"),
    ("SMITH JOHN CO-TRUSTEES", "John Smith"),
    ("JOHN A SMITH REVOCABLE TRUST", "John A Smith"),
    ("THE JANE DOE LIVING TRUST DTD 01/02/2003", "Jane Doe"),
    ("ESTATE OF MARY JONES", "Mary Jones"),
    ("SMITH JOHN EST", "John Smith"),
    ("SMITH JOHN ET AL", "John Smith"),
    ("O'BRIEN PATRICK J", "Patrick J O'Brien"),
    ("JOHN SMITH", "John Smith"),
    ("MILLER JAMES", "James Miller"),
    ("GARCIA MARIA L/E", "Maria Garcia"),
    ("SMITH JOHN LE", "John Smith"),                       # trailing LE = life estate
    ("LE THANH", "Thanh Le"),                              # ...but Le is also a surname
    ("NGUYEN LE", "Le Nguyen"),
    ("DOE JOHN & JANE LE", "John & Jane Doe"),
    ("MARY ANN B SMITH CO-TR", "Mary Ann B Smith"),
    ("SMITH JOHN A REVOCABLE FAMILY TRUST", "John A Smith"),
    ("SMITH JOHN C/O ACME MANAGEMENT", "John Smith"),
])
def test_person_from_owner_name(name, expected):
    assert L.person_from_owner_name(name) == expected


@pytest.mark.parametrize("name", [
    None, "", "   ", "MARY", "HOLLOWAY FAMILY TRUST", "EXAMPLE AVE APARTMENTS LLC", "ABC PROPERTIES L.L.C.",
    "CITY OF SPRINGFIELD", "FIRST TEST BANK", "SPRINGFIELD METROPOLITAN HOUSING AUTHORITY", "CT CORPORATION SYSTEM",
    "ACME HOLDINGS INC", "ST JOHNS CHURCH", "THE SMITH FAMILY LIVING TRUST", "REVOCABLE TRUST",
    "FIRST NATIONAL BANK OF OHIO TRUSTEE",
])
def test_person_from_owner_name_entities_and_blanks(name):
    assert L.person_from_owner_name(name) is None


def test_looks_like_entity():
    assert L.looks_like_entity("ABC Properties LLC")
    assert L.looks_like_entity("CT Corporation System")
    assert not L.looks_like_entity("John Doe")
    assert not L.looks_like_entity(None)


@pytest.mark.parametrize("name, expected", [
    ("John & Jane Doe", "John Doe"),
    ("Robert L & Mary K Jones", "Robert L Jones"),
    ("John Doe & Mary Smith", "John Doe"),
    ("John A Smith (trustee)", "John A Smith"),
    ("Jane Roe", "Jane Roe"),
    (None, None),
    ("", None),
])
def test_first_person(name, expected):
    assert L.first_person(name) == expected


# --- placeholders ---------------------------------------------------------------------------------

def test_link_values_has_every_placeholder_from_sources_yaml(settings):
    text = (settings.root / "config" / "sources.yaml").read_text()
    documented = set(re.findall(r"\{(\w+)\}", text.split("sources:")[0]))
    used = set()
    for src in settings.sources["sources"].values():
        used |= set(re.findall(r"\{(\w+)\}", str(src.get("template") or "")))
    values = L.link_values(PROP)
    missing = (documented | used | {"state_lower", "apn_digits", "full_address_q"}) - set(values)
    assert not missing, missing


def test_link_values_content(settings):
    v = L.link_values(PROP)
    assert v["apn"] == "340-01-00-001-000"
    assert v["apn_digits"] == "3400100001000"
    assert v["apn_q"] == "340-01-00-001-000"
    assert v["county"] == "CLARK"
    assert v["state_lower"] == "oh"
    assert v["owner_q"] == "DOE+JOHN+%26+JANE"
    assert v["person"] == "John & Jane Doe"           # parsed from the owner name
    assert v["person_q"] == "John+Doe"                # one searchable person
    assert v["person_or_owner_q"] == "John+Doe"
    assert v["full_address"] == "1200 SAMPLE RIDGE DR, SPRINGFIELD, OH 45503"
    assert v["full_address_q"] == "1200+SAMPLE+RIDGE+DR%2C+SPRINGFIELD%2C+OH+45503"
    assert v["lat"] == "39.9501" and v["lng"] == "-83.7702"
    assert v["crexi_url"].startswith("https://www.crexi.com/")


def test_link_values_person_override_and_entity_owner(settings):
    v = L.link_values({**PROP, "owner_name": "ACME HOLDINGS LLC"}, person="Pat Lee")
    assert v["person"] == "Pat Lee" and v["person_q"] == "Pat+Lee"
    v2 = L.link_values({**PROP, "owner_name": "ACME HOLDINGS LLC"})
    assert v2["person"] == "" and v2["person_q"] == ""
    assert v2["person_or_owner_q"] == "ACME+HOLDINGS+LLC"


def test_link_values_can_skip_owner_parsing(settings):
    v = L.link_values(PROP, parse_owner=False)
    assert v["person"] == "" and v["person_or_owner_q"] == "DOE+JOHN+%26+JANE"


def test_link_values_missing_data_is_blank(settings):
    v = L.link_values({"id": 9})
    assert all(isinstance(x, str) for x in v.values())
    assert v["apn"] == "" and v["lat"] == "" and v["full_address"] == ""


def test_fill_template_rules():
    vals = {"apn": "1-2", "city": "", "owner_q": ""}
    assert L.fill_template("https://x/{apn}", vals) == "https://x/1-2"
    assert L.fill_template("https://x/{apn}?c={city}", vals) == "https://x/1-2?c="   # city is optional
    assert L.fill_template("https://x/{owner_q}", vals) is None                     # owner is required
    assert L.fill_template("https://x/{nope}", vals) is None                        # unknown placeholder
    assert L.fill_template("https://x/{", vals) is None                             # broken template
    assert L.fill_template(None, vals) is None
    assert L.fill_template("https://static/", vals) == "https://static/"


# --- property links -------------------------------------------------------------------------------

def test_property_links_always_have_street_view_maps_and_crexi(empty_county_settings):
    links = L.links_for(PROP, settings=empty_county_settings)
    keys = [x["source_key"] for x in links]
    assert keys[:2] == ["street_view", "google_maps_listing"]
    assert "crexi" in keys
    sv = _by_key(links)["street_view"]
    assert "viewpoint=39.9501,-83.7702" in sv["url"]
    assert _by_key(links)["google_maps_listing"]["label"] == "Google Maps"
    assert _by_key(links)["crexi"]["url"] == PROP["crexi_url"]
    for x in links:
        assert {"label", "url", "source_key", "cost"} <= set(x)
        assert x["url"].startswith("http")


def test_property_links_without_coordinates_fall_back_to_map_search(empty_county_settings):
    links = _by_key(L.links_for({**PROP, "lat": None, "lng": None}, settings=empty_county_settings))
    assert links["street_view"]["fallback"] is True
    assert links["street_view"]["url"].startswith("https://www.google.com/maps/search/")


def test_property_links_include_county_auditor_and_gis(county_settings):
    links = _by_key(L.links_for(PROP, settings=county_settings))
    assert links["county_auditor"]["url"] == "https://auditor.example.gov/parcel/3400100001000"
    assert links["county_auditor"]["deep_link"] is True
    assert links["county_gis"]["url"] == "https://gis.example.gov/?parcel=340-01-00-001-000"


# --- stage links ----------------------------------------------------------------------------------

def test_county_stage_uses_template_with_apn(county_settings):
    links = L.links_for(PROP, "county_auditor", settings=county_settings)
    assert links[0]["source_key"] == "county_auditor"
    assert links[0]["url"] == "https://auditor.example.gov/parcel/3400100001000"
    assert links[0]["label"] == "Clark County auditor"
    assert links[0]["cost"] == "free"


def test_county_stage_without_apn_uses_search_page(county_settings):
    links = L.links_for({**PROP, "apn": None}, "county_auditor", settings=county_settings)
    assert links[0]["url"] == "https://auditor.example.gov/search"
    assert links[0]["deep_link"] is False


def test_county_slot_as_plain_url_and_case_insensitive_keys(county_settings):
    links = L.links_for({**PROP, "county": "clark"}, "rental_registration", settings=county_settings)
    assert [x["url"] for x in links] == ["https://auditor.example.gov/rental"]
    greene = L.links_for({**PROP, "county": "Greene"}, "county_auditor", settings=county_settings)
    assert greene[0]["url"] == "https://greene.example.gov/"


def test_county_missing_from_table_gets_search_fallback(county_settings):
    links = L.links_for({**PROP, "county": "NOWHERE"}, "county_auditor", settings=county_settings)
    assert len(links) == 1 and links[0]["fallback"] is True
    q = parse_qs(urlparse(links[0]["url"]).query)["q"][0]
    assert "Nowhere County OH" in q and "auditor" in q


def test_county_slot_missing_in_known_county_falls_back(county_settings):
    links = L.links_for({**PROP, "county": "GREENE"}, "recorder_documents", settings=county_settings)
    assert links and links[0]["fallback"] is True


def test_no_county_means_no_county_link(county_settings):
    assert L.links_for({**PROP, "county": None}, "county_auditor", settings=county_settings) == []


def test_court_records_fills_owner_template(county_settings):
    links = _by_key(L.links_for(PROP, "court_records", settings=county_settings))
    assert links["probate_court"]["url"] == "https://probate.example.gov/?q=DOE+JOHN+%26+JANE"
    assert links["clerk_of_courts"]["url"] == "https://clerk.example.gov/"


def test_entity_unmask_links_and_state_filter(empty_county_settings):
    s = empty_county_settings
    oh = _by_key(L.links_for({**PROP, "owner_name": "ACME HOLDINGS LLC"}, "entity_unmask", settings=s))
    assert "ohio_sos" in oh and "indiana_inbiz" not in oh            # INBiz is Indiana only
    assert oh["opencorporates"]["url"] == ("https://opencorporates.com/companies?q=ACME+HOLDINGS+LLC"
                                           "&jurisdiction_code=us_oh")
    ind = _by_key(L.links_for({**PROP, "state": "IN", "owner_name": "ACME HOLDINGS LLC"}, "entity_unmask",
                              settings=s))
    assert "indiana_inbiz" in ind
    assert ind["opencorporates"]["url"].endswith("us_in")


def test_web_footprint_needs_a_person_for_people_sites(empty_county_settings):
    s = empty_county_settings
    entity = _by_key(L.links_for({**PROP, "owner_name": "ACME HOLDINGS LLC"}, "web_footprint", settings=s))
    assert "google_search" in entity            # falls back to the owner name
    assert "linkedin" not in entity and "facebook" not in entity and "free_people_search" not in entity
    person = _by_key(L.links_for({**PROP, "owner_name": "ACME HOLDINGS LLC"}, "web_footprint",
                                 person="Pat Lee", settings=s))
    assert person["linkedin"]["url"].endswith("keywords=Pat+Lee")
    assert "%22Pat+Lee%22" in person["google_search"]["url"]
    nobody = L.links_for({**PROP, "owner_name": None}, "web_footprint", settings=s)
    assert nobody == []


def test_auto_and_manual_sources_have_no_links(empty_county_settings):
    auto_manual = {"crexi_export", "internal_portfolio", "mail", "local_brokers", "all_sources"}
    for stage in ("records_baseline", "letters", "neighbor_network", "deep_research"):
        keys = {x["source_key"] for x in L.links_for(PROP, stage, settings=empty_county_settings)}
        assert not keys & auto_manual
    assert L.links_for(PROP, "validate", settings=empty_county_settings) == []


def test_crexi_credit_link(empty_county_settings):
    links = L.links_for(PROP, "crexi_credit", settings=empty_county_settings)
    assert len(links) == 1 and links[0]["url"] == PROP["crexi_url"]
    assert links[0]["cost"] == "credit"
    assert L.links_for({**PROP, "crexi_url": None}, "crexi_credit", settings=empty_county_settings) == []


def test_unknown_stage_raises(settings):
    with pytest.raises(ValueError, match="Unknown DRILL stage"):
        L.links_for(PROP, "no_such_stage", settings=settings)


def test_links_are_deduplicated(tmp_root, settings):
    """Two sources with the same URL show once."""
    from bluemoon.config import reload_settings
    path = tmp_root / "config" / "drill.yaml"
    data = yaml.safe_load(path.read_text())
    for st in data["stages"]:
        if st["key"] == "property_contacts":
            st["sources"] = ["google_maps_listing", "google_maps_listing", "neighbors"]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    s = reload_settings()
    links = L.links_for(PROP, "property_contacts", settings=s)
    assert [x["source_key"] for x in links] == ["google_maps_listing", "neighbors"]


@pytest.mark.parametrize("name,expected", [
    ("Pat Example", "Pat Example"),        # typed by hand: kept in the order it was typed
    ("Brad Wilson", "Brad Wilson"),
    ("Smith John", "John Smith"),          # the first-name list still spots a flipped name
    ("Doe, Pat", "Pat Doe"),               # a comma means records order
    ("EXAMPLE PAT", "Pat Example"),        # all caps from county records: LAST FIRST
])
def test_person_from_names_typed_by_hand(name, expected):
    assert L.person_from_owner_name(name) == expected


def test_engine_rows_filter_sources_by_asset_class(settings):
    prop = {"asset_class_key": "apartments", "state": "OH", "county": "CLARK", "address": "1 TEST ST",
            "city": "SPRINGFIELD", "apn": "340-01"}
    keys = {x["source_key"] for x in L.links_for(prop, "property_contacts", settings=settings)}
    assert "fmcsa_carriers" not in keys and "ohio_self_storage_assn" not in keys
    yard = {**prop, "asset_class_key": "outdoor_storage"}
    assert "fmcsa_carriers" in {x["source_key"] for x in L.links_for(yard, "occupant_lookup", settings=settings)}
