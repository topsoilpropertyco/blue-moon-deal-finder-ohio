"""Tests for the rent check (bluemoon/enrich/rent.py).

HUD's services are faked with httpx.MockTransport, so these tests never touch the network.
Live checks run only with BLUEMOON_LIVE=1.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from bluemoon.db import loads, market_by_slug, now_iso, one, rows, scalar
from bluemoon.enrich import rent
from bluemoon.enrich.rent import (
    compute_rent_fits,
    current_federal_fy,
    fiscal_year_from_edit,
    fiscal_year_from_filename,
    fiscal_year_from_text,
    fmr_for_point,
    read_hud_file,
    rent_column,
    rent_fit,
)

LIVE = os.environ.get("BLUEMOON_LIVE") == "1"
FMR_ITEM = "12d2516901f947b5bb4da4e780e35f07"
SAFMR_ITEM = "6458c67bad2a4cc7aa97514ef7ba8a0e"
EDIT_2025_09_30 = 1759276162016

SPRINGFIELD = "METRO44220M44220"
COLUMBUS = "METRO18140M18140"
DAYTON = "METRO19430M19430"
CHAMPAIGN = "NCNTY39021N39021"
CLINTON = "NCNTY39027N39027"

AREAS = {
    SPRINGFIELD: ("Springfield, OH MSA", [763, 843, 1106, 1355, 1572]),
    COLUMBUS: ("Columbus, OH  HUD Metro FMR Area", [1300, 1400, 1650, 2050, 2300]),
    DAYTON: ("Dayton-Kettering-Beavercreek, OH MSA", [900, 980, 1180, 1500, 1650]),
    CHAMPAIGN: ("Champaign County, OH", [737, 818, 1046, 1333, 1470]),
    CLINTON: ("Clinton County, OH", [700, 780, 1000, 1290, 1400]),
}
SAFMR = {
    "45502": [(SPRINGFIELD, [840, 930, 1220, 1490, 1730]), (CHAMPAIGN, [840, 930, 1220, 1490, 1730])],
    "45503": [(SPRINGFIELD, [800, 880, 1160, 1420, 1650])],
    "45504": [(SPRINGFIELD, [900, 1000, 1450, 1700, 1900])],
    "43215": [(COLUMBUS, [1670, 1790, 2150, 2580, 2900])],
    "45385": [(DAYTON, [1000, 1080, 1190, 1500, 1700]), (CLINTON, [1000, 1080, 1190, 1500, 1700])],
    "43078": [(SPRINGFIELD, [760, 840, 1040, 1330, 1500]), (CHAMPAIGN, [760, 840, 1040, 1330, 1500])],
}


# --------------------------------------------------------------------------------------------
# A fake HUD (ArcGIS map service + HUD USER API)
# --------------------------------------------------------------------------------------------

def area_code_at(lat: float, lng: float) -> str | None:
    if lat < 30:
        return None  # the ocean
    if lat >= 40.05:
        return CHAMPAIGN
    if lng >= -83.3:
        return COLUMBUS
    if lng <= -83.9:
        return DAYTON
    return SPRINGFIELD


def zcta_at(lat: float, lng: float) -> str | None:
    code = area_code_at(lat, lng)
    return {SPRINGFIELD: "45502", COLUMBUS: "43215"}.get(code or "")


def safmr_attrs(zip_code: str, code: str, r: list[int]) -> dict:
    out = {"HUD_CODE": code, "FMR_NAME": AREAS[code][0], "ID": zip_code, "ZCTA_ID": None}
    for i, v in enumerate(r):
        out[f"SAFMR_{i}BR"] = float(v)
        out[f"SAFMR_{i}BR_90_Payment_Standard"] = round(v * 0.9)
    return out


class FakeHud:
    """Answers like HUD's services. `log` holds one entry per request: (kind, params)."""

    def __init__(self) -> None:
        self.log: list[tuple[str, dict]] = []
        self.fail: dict[str, int] = {}          # kind -> HTTP status to return
        self.arcgis_error: set[str] = set()     # kinds that answer 200 with an ArcGIS error body
        self.flaky: dict[str, int] = {}         # kind -> number of 503s before answering
        self.page_size = 1000
        self.fmr_layer_desc = "In FY21 the rent for a four bedroom unit ... Date of Coverage: FY2024 : Oct. 1"
        self.fmr_item_desc = "<p>Date of Coverage:&nbsp; FY2026 :&nbsp;Oct. 1 - Sept. 30</p>"
        self.fmr_edit = EDIT_2025_09_30
        self.item_missing = False
        self.areas = {k: (v[0], list(v[1])) for k, v in AREAS.items()}
        self.api_token_seen: list[str | None] = []
        self.api_status: int | None = None

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.log if k == kind)

    @property
    def total(self) -> int:
        return len(self.log)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    # --- routing --------------------------------------------------------------------------
    def kind_of(self, req: httpx.Request) -> str:
        path, p = req.url.path, req.url.params
        if "/sharing/rest/content/items/" in path:
            return "item"
        if "/hudapi/public/fmr/" in path:
            return "api_counties" if "/listCounties/" in path else "api_data"
        svc = "safmr" if "Small_Area" in path else "fmr"
        layer = re.search(r"/FeatureServer/(\d+)", path)
        layer_id = layer.group(1) if layer else "?"
        if not path.endswith("/query"):
            return f"{svc}{layer_id}_meta"
        if svc == "fmr":
            return "fmr_point" if p.get("geometry") else "fmr_code"
        return "zcta_point" if layer_id == "0" else "safmr_zip"

    def handle(self, req: httpx.Request) -> httpx.Response:
        kind = self.kind_of(req)
        self.log.append((kind, dict(req.url.params)))
        if self.flaky.get(kind):
            self.flaky[kind] -= 1
            return httpx.Response(503, json={"error": {"code": 503, "message": "busy"}})
        if kind in self.fail:
            return httpx.Response(self.fail[kind], json={"error": {"code": self.fail[kind], "message": "down"}})
        if kind in self.arcgis_error:
            return httpx.Response(200, json={"error": {"code": 400, "message": "Invalid query parameters."}})
        return httpx.Response(200, json=getattr(self, f"r_{kind}")(req))

    # --- ArcGIS responses -----------------------------------------------------------------
    def r_fmr0_meta(self, req):
        return {"name": "FAIR_MARKET_RENTS", "description": self.fmr_layer_desc, "serviceItemId": FMR_ITEM,
                "editingInfo": {"dataLastEditDate": self.fmr_edit}}

    def r_safmr1_meta(self, req):
        return {"name": "SAFMR_table", "description": "", "serviceItemId": SAFMR_ITEM,
                "editingInfo": {"dataLastEditDate": 1759280057158}}

    def r_safmr0_meta(self, req):
        return {"name": "SAFMR_Zip_Code_Tab_Areas", "description": "", "serviceItemId": SAFMR_ITEM,
                "editingInfo": {"dataLastEditDate": 1759280048932}}

    def r_item(self, req):
        if self.item_missing:
            return {"error": {"code": 400, "message": "Item does not exist or is inaccessible."}}
        if FMR_ITEM in req.url.path:
            return {"title": "Fair Market Rents", "description": self.fmr_item_desc}
        return {"title": "Small Area Fair Market Rents",
                "description": "Date of Coverage: Fiscal Year 2026 Date Update: FY2026 : Oct. 1 - Sept. 30"}

    @staticmethod
    def _point(req) -> tuple[float, float]:
        lng, lat = (float(v) for v in req.url.params["geometry"].split(","))
        return lat, lng

    def r_fmr_point(self, req):
        assert req.url.params["inSR"] == "4326"
        assert req.url.params["geometryType"] == "esriGeometryPoint"
        assert req.url.params["returnGeometry"] == "false"
        code = area_code_at(*self._point(req))
        return {"features": [{"attributes": self._fmr(code)}] if code else []}

    def _fmr(self, code: str) -> dict:
        name, r = self.areas[code]
        return {"FMR_CODE": code, "FMR_AREANAME": name, **{f"FMR_{i}BDR": v for i, v in enumerate(r)}}

    def r_zcta_point(self, req):
        z = zcta_at(*self._point(req))
        return {"features": [{"attributes": {"ZCTA5CE20": z, "GEOID": z}}] if z else []}

    def _page(self, req, items: list[dict]) -> dict:
        offset = int(req.url.params.get("resultOffset", "0"))
        chunk = items[offset:offset + self.page_size]
        return {"features": [{"attributes": a} for a in chunk],
                "exceededTransferLimit": offset + self.page_size < len(items)}

    def r_safmr_zip(self, req):
        wanted = re.findall(r"'([^']+)'", req.url.params["where"])
        items = [safmr_attrs(z, code, r) for z in wanted for code, r in SAFMR.get(z, [])]
        return self._page(req, items)

    def r_fmr_code(self, req):
        wanted = re.findall(r"'([^']+)'", req.url.params["where"])
        return self._page(req, [self._fmr(c) for c in wanted if c in self.areas])

    # --- HUD USER API ---------------------------------------------------------------------
    def r_api_counties(self, req):
        self.api_token_seen.append(req.headers.get("authorization"))
        return [
            {"state_code": "OH", "fips_code": "3902399999", "county_name": "Clark County", "town_name": "",
             "category": "County"},
            {"state_code": "OH", "fips_code": "3904999999", "county_name": "Franklin County", "town_name": "",
             "category": "County"},
        ]

    def r_api_data(self, req):
        self.api_token_seen.append(req.headers.get("authorization"))
        if req.url.path.endswith("3902399999"):
            return {"data": {"county_name": "Clark County", "metro_status": "1", "metro_name": "Springfield, OH MSA",
                             "area_name": "Springfield, OH MSA", "smallarea_status": "0", "year": "2027",
                             "basicdata": {"Efficiency": 790, "One-Bedroom": 870, "Two-Bedroom": 1140,
                                           "Three-Bedroom": 1400, "Four-Bedroom": 1620, "FMR Percentile": 40,
                                           "year": "2027"}}}
        columbus = "Columbus, OH HUD Metro FMR Area"
        return {"data": {"county_name": "Franklin County", "metro_status": "1",
                         "metro_name": columbus, "area_name": columbus,
                         "smallarea_status": 1, "year": 2027,
                         "basicdata": [
                             {"zip_code": "MSA level", "Efficiency": 1350, "One-Bedroom": 1450, "Two-Bedroom": 1700,
                              "Three-Bedroom": 2100, "Four-Bedroom": 2350, "FMR Percentile": 40},
                             {"zip_code": "43215", "Efficiency": 1700, "One-Bedroom": 1820, "Two-Bedroom": 2190,
                              "Three-Bedroom": 2620, "Four-Bedroom": 2950, "FMR Percentile": 40}]}}


# --------------------------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------------------------

@pytest.fixture
def fake() -> FakeHud:
    return FakeHud()


@pytest.fixture
def fast(settings):
    """No waiting between retries in tests."""
    settings.raw.setdefault("rent", {}).update({"retry_backoff_seconds": 0, "max_retries": 1})
    return settings


def set_rent(settings, **kw) -> None:
    settings.raw.setdefault("rent", {}).update(kw)


def add_prop(conn, slug: str, key: str, **fields) -> int:
    m = market_by_slug(conn, slug)
    now = now_iso()
    data = {"market_id": m["id"], "property_key": key, "created_at": now, "updated_at": now, **fields}
    cur = conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' * len(data))})",
                       tuple(data.values()))
    conn.commit()
    return int(cur.lastrowid)


def fit_row(conn, pid: int) -> dict:
    r = one(conn, "SELECT * FROM rent_fits WHERE property_id=?", (pid,))
    assert r is not None
    r["rents"] = loads(r["rents_json"])
    return r


@pytest.fixture
def scenario(conn, market_slug) -> dict[str, int]:
    """Ten properties covering every path through the map-service lookup."""
    a = lambda key, **f: add_prop(conn, market_slug, key, state=f.pop("state", "OH"), **f)  # noqa: E731
    return {
        "zip_single": a("p1", zip="45503", lat=39.95, lng=-83.77, county="CLARK", city="SPRINGFIELD"),
        "zip_near": a("p2", zip="45504", lat=39.93, lng=-83.76, county="CLARK"),
        "columbus": a("p3", zip="43215", lat=39.96, lng=-83.00, county="FRANKLIN"),
        "champ1": a("p4", zip="43078", lat=40.10, lng=-83.75, county="CHAMPAIGN"),
        "champ2": a("p5", zip="43078", lat=40.11, lng=-83.76, county="CHAMPAIGN"),
        "champ3": a("p6", lat=40.12, lng=-83.74, county="Champaign County"),
        "xenia": a("p7", zip="45385", lat=39.68, lng=-83.93, county="GREENE"),
        "nothing": a("p8", state=None),
        "county_only": a("p10", county="CLARK"),
        "no_zip": a("p9", lat=39.92, lng=-83.81, county="CLARK"),
    }


APARTMENTS = {"enabled": True, "scale": {0: 800, 1: 1000, 2: 1200, 3: 1500}, "focus_bedrooms": 2, "margin": 0.15}


# --------------------------------------------------------------------------------------------
# rent_fit
# --------------------------------------------------------------------------------------------

def test_rent_fit_matches_the_example_wording(settings):
    spec = settings.asset_class("apartments").rent
    fit, detail = rent_fit({"0": 760, "1": 850, "2": 1150, "3": 1450, "4": 1600}, spec, "Apartments")
    assert fit == "in"
    assert detail.startswith("Area 2BR rent $1,150 vs Brad's $1,200 (4% under)")
    assert detail.endswith("studio-3BR average 7% under")


@pytest.mark.parametrize("two_br, expected", [
    (1200, "in"), (1380, "in"), (1020, "in"),      # within 15% (both edges)
    (1381, "near"), (1560, "near"), (840, "near"),  # within 30%
    (1561, "out"), (2150, "out"), (700, "out"),
])
def test_rent_fit_bands(two_br, expected):
    assert rent_fit({"2": two_br}, APARTMENTS)[0] == expected


def test_rent_fit_disabled_asset_class(settings):
    ss = settings.asset_class("self_storage")
    assert rent_fit({"2": 1200}, ss.rent, ss.label) == ("na", "Rent check not used for Self-storage")
    assert rent_fit({"2": 1200}, None) == ("na", "Rent check not used for this asset class")


@pytest.mark.parametrize("rents", [None, {}, {"2": None, "3": 0}, {"x": 1200}, {"2": "n/a"}])
def test_rent_fit_without_rents(rents):
    fit, detail = rent_fit(rents, APARTMENTS)
    assert fit == "na" and "No HUD rent" in detail


def test_rent_fit_focus_size_missing_uses_average():
    fit, detail = rent_fit({"0": 820, "1": 1000, "3": 1440}, APARTMENTS)
    assert fit == "in"
    assert detail == "Area studio-3BR rents average about even vs Brad's scale (no 2BR figure)"


def test_rent_fit_only_sizes_outside_the_scale():
    assert rent_fit({"4": 1600}, APARTMENTS) == ("na", "HUD rents don't cover the bedroom sizes in the rent scale")


def test_rent_fit_reads_messy_yaml_and_int_keys():
    spec = {"enabled": True, "scale": {"0": "800", "2": "1,200", "bad": 5, 3: None}, "focus_bedrooms": 2,
            "margin": 0.1, "near_margin": 0.12, "scale_label": "the target"}
    assert rent_fit({2: 1300.4}, spec) == ("in", "Area 2BR rent $1,300 vs the target $1,200 (8% over)")
    assert rent_fit({2: 1330}, spec) == ("near", "Area 2BR rent $1,330 vs the target $1,200 (11% over)")
    assert rent_fit({2: 1350}, spec)[0] == "out"   # 12.5% > custom near margin
    assert rent_fit({"2": 1201}, spec)[1].endswith("(about even)")
    assert rent_fit({"2": 1200}, {"enabled": True, "scale": {}}) == (
        "na", "No rent scale set for this asset class (config/asset_classes.yaml)")


def test_rent_fit_focus_studio_label():
    spec = {**APARTMENTS, "focus_bedrooms": 0}
    assert rent_fit({"0": 800, "1": 1000}, spec)[1].startswith("Area studio rent $800 vs Brad's $800 (about even)")


# --------------------------------------------------------------------------------------------
# Small parsers
# --------------------------------------------------------------------------------------------

def test_fiscal_year_from_text():
    assert fiscal_year_from_text("In FY21 the rent was... Date of Coverage: FY2024 : Oct. 1") == 2024
    assert fiscal_year_from_text("<p>Date of Coverage:&nbsp;</p><p>FY2026&nbsp;:Oct. 1</p>") == 2026
    assert fiscal_year_from_text("Date of Coverage: Fiscal Year 2026 Date Update: FY2026") == 2026
    assert fiscal_year_from_text("Data for FY2025 and FY2026.") == 2026
    assert fiscal_year_from_text("no year here, FY21 only") is None
    assert fiscal_year_from_text(None) is None


def test_fiscal_year_from_edit_and_current_fy():
    assert fiscal_year_from_edit(EDIT_2025_09_30) == 2026      # posted before Oct 1 -> next year
    assert fiscal_year_from_edit(1768000000000) == 2026        # January 2026
    assert fiscal_year_from_edit(None) is None
    assert current_federal_fy(date(2026, 10, 21)) == 2027
    assert current_federal_fy(date(2026, 9, 30)) == 2026


@pytest.mark.parametrize("name, fy", [
    ("FY27_FMRs.xlsx", 2027), ("fy2026_safmrs_revised.xlsx", 2026), ("FY 2025 SAFMR.csv", 2025),
    ("fmr-2024.csv", 2024), ("rents.xlsx", None),
])
def test_fiscal_year_from_filename(name, fy):
    assert fiscal_year_from_filename(name) == fy


@pytest.mark.parametrize("header, br", [
    ("fmr_0", "0"), ("fmr2", "2"), ("SAFMR\n2BR", "2"), ("SAFMR 4BR", "4"), ("Two-Bedroom", "2"),
    ("Efficiency", "0"), ("fmr_4", "4"),
    ("SAFMR\n2BR -\n90%\nPayment\nStandard", None), ("FMR Percentile", None), ("rent50_2", None),
    ("pop2020", None), ("fips2010", None), ("zip_code", None), (None, None),
])
def test_rent_column(header, br):
    assert rent_column(header) == br


# --------------------------------------------------------------------------------------------
# HUD map service (ArcGIS)
# --------------------------------------------------------------------------------------------

def test_fmr_for_point_uses_small_area_rent_for_the_zip(conn, fast, fake):
    r = fmr_for_point(conn, 39.9242, -83.8088, settings=fast, client=fake.client())
    assert r["kind"] == "small_area"
    assert r["zip"] == "45502"                                   # found from the map point
    assert r["rents"] == {"0": 840, "1": 930, "2": 1220, "3": 1490, "4": 1730}
    assert r["area_rents"]["2"] == 1106
    assert r["area_name"] == "Springfield, OH MSA, ZIP 45502"
    assert r["fiscal_year"] == "FY2026"                          # item metadata beats the stale layer text
    assert r["source"] == "hud_arcgis" and r["area_code"] == SPRINGFIELD
    assert fake.count("zcta_point") == 1


def test_zip_in_one_area_needs_no_map_lookup(conn, fast, fake):
    r = fmr_for_point(conn, 39.9242, -83.8088, "45503", settings=fast, client=fake.client())
    assert r["rents"]["2"] == 1160 and fake.count("fmr_point") == 0


def test_fmr_for_point_is_cached(conn, fast, fake):
    first = fmr_for_point(conn, 39.9242, -83.8088, "45502", settings=fast, client=fake.client())
    before = fake.total
    again = fmr_for_point(conn, 39.92418, -83.80883, "45502", settings=fast, client=fake.client())  # same ~100 m cell
    assert again == first
    assert fake.total == before
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache WHERE provider='hud_arcgis'") > 0


def test_area_rent_when_small_area_is_off(conn, fast, fake):
    set_rent(fast, prefer_small_area=False)
    r = fmr_for_point(conn, 39.9242, -83.8088, "45502", settings=fast, client=fake.client())
    assert r["kind"] == "area" and r["rents"]["2"] == 1106 and r["area_name"] == "Springfield, OH MSA"
    assert fake.count("zcta_point") == 0


def test_non_metro_county_uses_county_fmr_not_small_area(conn, fast, fake):
    r = fmr_for_point(conn, 40.1084, -83.7527, "43078", settings=fast, client=fake.client())
    assert r["kind"] == "area"
    assert r["area_name"] == "Champaign County, OH" and r["rents"]["2"] == 1046


def test_point_outside_any_area_returns_none(conn, fast, fake):
    assert fmr_for_point(conn, 25.0, -60.0, settings=fast, client=fake.client()) is None


def test_arcgis_error_inside_http_200_is_not_cached(conn, fast, fake):
    fake.arcgis_error.add("fmr_point")
    assert fmr_for_point(conn, 40.1084, -83.7527, settings=fast, client=fake.client()) is None
    fake.arcgis_error.clear()
    r = fmr_for_point(conn, 40.1084, -83.7527, settings=fast, client=fake.client())
    assert r is not None and r["rents"]["2"] == 1046


def test_busy_service_is_retried(conn, fast, fake):
    fake.flaky["fmr_point"] = 1
    r = fmr_for_point(conn, 40.1084, -83.7527, settings=fast, client=fake.client())
    assert r is not None
    assert fake.count("fmr_point") == 2


def test_fiscal_year_falls_back_to_edit_date_when_item_unreadable(conn, fast, fake):
    fake.item_missing = True
    r = fmr_for_point(conn, 40.1084, -83.7527, settings=fast, client=fake.client())
    assert r["fiscal_year"] == "FY2026"   # layer text says FY2024, but the data was posted Sept 2025


def test_new_hud_data_is_fetched_again(conn, fast, fake, monkeypatch):
    first = fmr_for_point(conn, 40.1084, -83.7527, settings=fast, client=fake.client())
    assert first["rents"]["2"] == 1046
    # HUD posts new rents; a month later the app re-reads the layer details and sees the new data stamp.
    fake.fmr_edit = EDIT_2025_09_30 + 365 * 86400 * 1000
    fake.fmr_item_desc = "Date of Coverage: FY2027 : Oct. 1 - Sept. 30"
    fake.areas[CHAMPAIGN] = ("Champaign County, OH", [760, 840, 1090, 1390, 1530])
    monkeypatch.setattr(rent, "this_month", lambda: "2099-01")
    second = fmr_for_point(conn, 40.1084, -83.7527, settings=fast, client=fake.client())
    assert second["rents"]["2"] == 1090 and second["fiscal_year"] == "FY2027"


def test_safmr_paging(conn, fast, fake):
    fake.page_size = 1
    r = fmr_for_point(conn, 39.68, -83.93, "45385", settings=fast, client=fake.client())
    assert r["kind"] == "small_area" and r["rents"]["2"] == 1190
    assert fake.count("safmr_zip") == 2   # two rows for that ZIP, one per page


# --------------------------------------------------------------------------------------------
# compute_rent_fits
# --------------------------------------------------------------------------------------------

def test_compute_rent_fits_end_to_end(conn, fast, fake, market_slug, scenario):
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client(), today=date(2026, 6, 1))
    assert res["fits"] == {"in": 6, "near": 1, "out": 1, "na": 2}
    assert res["errors"] == 0 and res["properties"] == 10 and res["saved"] == 10
    assert res["fiscal_years"] == ["FY2026"] and res["warnings"] == []
    assert res["small_area"] == 5 and res["area"] == 3
    assert res["message"].startswith("Rent check done for 10 properties: 6 in range, 1 near, 1 out, 2 without")

    p1 = fit_row(conn, scenario["zip_single"])
    assert p1["fit"] == "in" and p1["source"] == "hud_arcgis" and p1["fiscal_year"] == "FY2026"
    assert p1["area_name"] == "Springfield, OH MSA, ZIP 45503"
    assert p1["rents"] == {"0": 800, "1": 880, "2": 1160, "3": 1420, "4": 1650}
    assert p1["detail"].startswith("Area 2BR rent $1,160 vs Brad's $1,200 (3% under)")
    assert fit_row(conn, scenario["zip_near"])["fit"] == "near"
    columbus = fit_row(conn, scenario["columbus"])
    assert columbus["fit"] == "out" and columbus["area_name"] == "Columbus, OH HUD Metro FMR Area, ZIP 43215"
    for key in ("champ1", "champ2", "champ3"):
        r = fit_row(conn, scenario[key])
        assert r["area_name"] == "Champaign County, OH" and r["rents"]["2"] == 1046
    assert fit_row(conn, scenario["xenia"])["area_name"].startswith("Dayton")
    assert fit_row(conn, scenario["no_zip"])["area_name"] == "Springfield, OH MSA, ZIP 45502"
    nothing = fit_row(conn, scenario["nothing"])
    assert nothing["fit"] == "na" and nothing["detail"] == "No map point, ZIP or county to look up HUD rents"
    assert nothing["rents"] is None and nothing["source"] is None
    assert fit_row(conn, scenario["county_only"])["detail"].startswith("Only a county")

    # Efficient: ZIPs in one batch; one map lookup per county that needed one; area rents in one batch.
    assert fake.count("fmr_point") == 3        # Champaign (settled by name), Greene, Clark
    assert fake.count("safmr_zip") == 2        # all ZIPs at once + the ZIP found from a map point
    assert fake.count("fmr_code") == 1
    assert res["requests"] == fake.total


def test_compute_rent_fits_rerun_is_free_and_identical(conn, fast, fake, market_slug, scenario):
    sql = "SELECT property_id, fit, detail, rents_json, area_name FROM rent_fits ORDER BY property_id"
    compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    first = rows(conn, sql)
    before = fake.total
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert fake.total == before and res["requests"] == 0 and res["cache_hits"] > 0
    assert rows(conn, sql) == first
    assert scalar(conn, "SELECT COUNT(*) FROM rent_fits") == len(scenario)


def test_zip_lookups_are_batched(conn, fast, fake, market_slug):
    set_rent(fast, zip_batch_size=50)
    for i in range(120):
        add_prop(conn, market_slug, f"z{i}", zip=f"{46000 + i}", state="OH", county="CLARK", lat=39.9, lng=-83.8)
    compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert fake.count("safmr_zip") == 3
    assert fake.count("fmr_point") == 1        # one map point shared by everyone


def test_metro_county_settled_by_two_agreeing_points(conn, fast, fake, market_slug):
    for i in range(6):  # ZIP 45385 spans two areas, so each would need its own map lookup
        add_prop(conn, market_slug, f"g{i}", zip="45385", state="OH", county="GREENE", lat=39.60 + i * 0.01, lng=-83.95)
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert fake.count("fmr_point") == 2
    assert res["fits"]["in"] == 6


def test_county_with_two_areas_looks_up_every_point(conn, fast, fake, market_slug):
    # Bad county data: the points sit in two different FMR areas, so nothing is reused.
    for i, lng in enumerate([-83.95, -83.80, -83.95, -83.80]):
        add_prop(conn, market_slug, f"m{i}", zip="45385", state="OH", county="GREENE", lat=39.6 + i * 0.01, lng=lng)
    compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert fake.count("fmr_point") == 4


def test_hud_down_keeps_earlier_results(conn, fast, fake, market_slug, scenario):
    compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    before = rows(conn, "SELECT * FROM rent_fits ORDER BY property_id")
    conn.execute("DELETE FROM api_cache")
    conn.commit()
    down = FakeHud()
    down.fail = {k: 503 for k in ("fmr0_meta", "safmr1_meta", "item", "fmr_point", "fmr_code", "safmr_zip",
                                  "zcta_point", "safmr0_meta")}
    res = compute_rent_fits(conn, market_slug, settings=fast, client=down.client())
    assert res["errors"] == 8 and res["saved"] == 2        # only the two without a usable location are re-saved
    assert any("didn't answer" in w for w in res["warnings"])
    after = rows(conn, "SELECT * FROM rent_fits ORDER BY property_id")
    assert [(r["fit"], r["rents_json"]) for r in after] == [(r["fit"], r["rents_json"]) for r in before]
    # The circuit breaker stops asking a service that isn't answering.
    assert down.total <= 8 * 2 + 2
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache") == 0   # failures are never cached


def test_small_area_service_down_falls_back_to_area_rents(conn, fast, fake, market_slug, scenario):
    fake.fail["safmr_zip"] = 500
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert res["errors"] == 0 and res["small_area"] == 0
    assert any("Small Area" in w for w in res["warnings"])
    assert fit_row(conn, scenario["zip_single"])["area_name"] == "Springfield, OH MSA"


def test_rent_disabled_asset_class_marks_na_without_calls(conn, settings, market_slug):
    slug = "self-storage-springfield-oh"
    pid = add_prop(conn, slug, "s1", zip="45503", state="OH", lat=39.9, lng=-83.8)

    def boom(req):
        raise AssertionError("no HUD calls for a class without a rent check")

    res = compute_rent_fits(conn, slug, settings=settings, client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert res["enabled"] is False and res["fits"]["na"] == 1
    r = fit_row(conn, pid)
    assert r["fit"] == "na" and r["detail"] == "Rent check not used for Self-storage"


def test_dry_run_and_limit(conn, fast, fake, market_slug, scenario):
    res = compute_rent_fits(conn, market_slug, dry_run=True, settings=fast, client=fake.client())
    assert res["dry_run"] is True and res["properties"] == 10 and res["with_location"] == 9
    assert res["cost"]["usd"] == 0 and fake.total == 0
    assert scalar(conn, "SELECT COUNT(*) FROM rent_fits") == 0
    res = compute_rent_fits(conn, market_slug, limit=2, settings=fast, client=fake.client())
    assert res["properties"] == 2 and scalar(conn, "SELECT COUNT(*) FROM rent_fits") == 2


def test_progress_and_stale_year_warning(conn, fast, fake, market_slug, scenario):
    seen: list[tuple[float, str]] = []
    res = compute_rent_fits(conn, market_slug, progress=lambda f, m: seen.append((f, m)), settings=fast,
                            client=fake.client(), today=date(2026, 10, 21))
    assert seen and seen[-1][0] == 1.0 and all(0 <= f <= 1 for f, _ in seen)
    assert [f for f, _ in seen] == sorted(f for f, _ in seen)
    assert any("FY2027 rents took effect" in w for w in res["warnings"])


def test_broken_progress_callback_does_not_stop_the_run(conn, fast, fake, market_slug, scenario):
    def bad(f, m):
        raise ValueError("oops")

    assert compute_rent_fits(conn, market_slug, progress=bad, settings=fast, client=fake.client())["errors"] == 0


def test_unknown_source_falls_back(conn, fast, fake, market_slug, scenario):
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client(), source="zillow")
    assert res["source"] == "hud_arcgis" and any("isn't one of" in w for w in res["warnings"])


def test_real_ingest_then_rent(conn, fast, fake, market_slug, sample_csv):
    crexi = pytest.importorskip("bluemoon.ingest.crexi")
    crexi.import_file(conn, market_slug, sample_csv)
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    n = scalar(conn, "SELECT COUNT(*) FROM properties WHERE market_id=?", (market_by_slug(conn, market_slug)["id"],))
    assert res["properties"] == n and sum(res["fits"].values()) == n and res["errors"] == 0


# --------------------------------------------------------------------------------------------
# hud_file: a spreadsheet in data/hud/
# --------------------------------------------------------------------------------------------

def write_county_csv(path: Path) -> None:
    head = "stusps,state,hud_area_code,countyname,county_town_name,metro,hud_area_name,fips,pop2020," \
           "fmr_0,fmr_1,fmr_2,fmr_3,fmr_4"
    body = [
        f"OH,39,{SPRINGFIELD},Clark County,Clark County,1,Springfield  OH MSA,3902399999,136001,"
        "780,860,1130,1390,1610",
        f"OH,39,{CHAMPAIGN},Champaign County,Champaign County,0,\"Champaign County, OH\",3902199999,38714,"
        "750,830,1070,1360,1500",
        "IN,18,METRO99999M99999,Clark County,Clark County,1,\"Louisville, KY-IN\",1801999999,1,1,1,1,1,1",
    ]
    path.write_text("\n".join([head, *body]) + "\n", encoding="utf-8")


def write_safmr_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["FY2027 Small Area Fair Market Rents"])  # HUD sometimes has a title row
    head = ["ZIP\nCode", "HUD Area Code", "HUD Metro Fair Market Rent Area Name"]
    for b in range(5):
        head += [f"SAFMR\n{b}BR", f"SAFMR\n{b}BR -\n90%\nPayment\nStandard", f"SAFMR\n{b}BR -\n110%\nPayment\nStandard"]
    ws.append(head)

    def row(z, code, name, rents):
        cells = [z, code, name]
        for v in rents:
            cells += [v, round(v * 0.9), round(v * 1.1)]
        ws.append(cells)

    row(45503, SPRINGFIELD, "Springfield, OH MSA", [810, 890, 1180, 1440, 1670])
    row(45502, SPRINGFIELD, "Springfield, OH MSA", [850, 940, 1240, 1510, 1750])
    row(1105, "METRO44140M44140", "Springfield, MA MSA", [1000, 1100, 1300, 1600, 1800])
    row(43078, SPRINGFIELD, "Springfield, OH MSA", [770, 850, 1050, 1340, 1510])
    wb.save(path)


@pytest.fixture
def hud_dir(fast) -> Path:
    d = fast.path("hud_dir")
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_read_hud_files(hud_dir):
    write_county_csv(hud_dir / "FY27_FMRs.csv")
    write_safmr_xlsx(hud_dir / "fy2027_safmrs.xlsx")
    county = read_hud_file(hud_dir / "FY27_FMRs.csv")
    assert county.kind == "fmr" and county.fiscal_year == 2027 and len(county.rows) == 3
    clark = county.by_county[("OH", "CLARK")][0]
    assert clark.rents == {"0": 780, "1": 860, "2": 1130, "3": 1390, "4": 1610}
    assert clark.name == "Springfield OH MSA" and clark.code == SPRINGFIELD
    small = read_hud_file(hud_dir / "fy2027_safmrs.xlsx")
    assert small.kind == "safmr" and small.fiscal_year == 2027
    assert small.by_zip["45503"][0].rents["2"] == 1180         # not the 90%/110% payment standards
    assert "01105" in small.by_zip                             # leading zero restored


def test_hud_file_source(conn, fast, fake, market_slug, hud_dir):
    write_county_csv(hud_dir / "FY27_FMRs.csv")
    write_safmr_xlsx(hud_dir / "fy2027_safmrs.xlsx")
    (hud_dir / "notes.csv").write_text("hello,world\n1,2\n")         # not a HUD file: ignored
    (hud_dir / "broken.xlsx").write_bytes(b"not really a spreadsheet")  # unreadable: ignored
    set_rent(fast, source="hud_file")
    a = lambda key, **f: add_prop(conn, market_slug, key, state="OH", **f)  # noqa: E731
    ids = {
        "zip": a("f1", zip="45503", county="CLARK"),
        "county_only": a("f2", county="Clark County"),
        "non_metro": a("f3", zip="43078", county="CHAMPAIGN"),
        "zip_only": a("f4", zip="45502"),
        "elsewhere": a("f5", zip="45385", county="GREENE", lat=39.68, lng=-83.93),
    }
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client(), today=date(2026, 10, 21))
    assert res["source"] == "hud_file" and res["errors"] == 0
    r = fit_row(conn, ids["zip"])
    assert r["source"] == "hud_file" and r["fiscal_year"] == "FY2027" and r["rents"]["2"] == 1180
    assert r["area_name"] == "Springfield, OH MSA, ZIP 45503"
    assert fit_row(conn, ids["county_only"])["rents"]["2"] == 1130
    nm = fit_row(conn, ids["non_metro"])
    assert nm["area_name"] == "Champaign County, OH" and nm["rents"]["2"] == 1070   # county FMR, not the ZIP's
    assert fit_row(conn, ids["zip_only"])["rents"]["2"] == 1240
    other = fit_row(conn, ids["elsewhere"])                           # not in the files: free map service
    assert other["source"] == "hud_arcgis" and other["fiscal_year"] == "FY2026"
    assert any("tried HUD's free map service" in w for w in res["warnings"])
    assert any("FY2026 rents" in w for w in res["warnings"])        # the map-service rent is a year behind
    assert res["sources_used"] == {"hud_file": 4, "hud_arcgis": 1}


def test_hud_file_source_without_files_uses_map_service(conn, fast, fake, market_slug, scenario):
    set_rent(fast, source="hud_file")
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert res["source"] == "hud_arcgis"
    assert any("no HUD spreadsheet" in w for w in res["warnings"])
    assert res["fits"]["in"] == 6


# --------------------------------------------------------------------------------------------
# hud_api: HUD USER API with a token
# --------------------------------------------------------------------------------------------

TOKEN = "test-token-not-real-123"


def test_hud_api_source(conn, fast, fake, market_slug, monkeypatch):
    monkeypatch.setenv("HUD_API_TOKEN", TOKEN)
    set_rent(fast, source="hud_api")
    a = lambda key, **f: add_prop(conn, market_slug, key, state="OH", **f)  # noqa: E731
    clark = a("h1", zip="45503", county="CLARK")
    fr_zip = a("h2", zip="43215", county="FRANKLIN")
    fr_other = a("h3", zip="43299", county="Franklin County")
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client(), today=date(2026, 10, 21))
    assert res["source"] == "hud_api" and res["errors"] == 0 and res["warnings"] == []
    c = fit_row(conn, clark)
    assert c["source"] == "hud_api" and c["fiscal_year"] == "FY2027"
    assert c["rents"] == {"0": 790, "1": 870, "2": 1140, "3": 1400, "4": 1620}
    z = fit_row(conn, fr_zip)
    assert z["rents"]["2"] == 2190 and z["area_name"].endswith("ZIP 43215")
    assert fit_row(conn, fr_other)["rents"]["2"] == 1700          # the "MSA level" row
    assert fake.count("api_counties") == 1 and fake.count("api_data") == 2
    assert set(fake.api_token_seen) == {f"Bearer {TOKEN}"}
    # The token never lands in the cache.
    for r in rows(conn, "SELECT key, body FROM api_cache"):
        assert TOKEN not in r["key"] and TOKEN not in (r["body"] or "")
    before = fake.total
    compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert fake.total == before


def test_hud_api_rejected_token_falls_back(conn, fast, fake, market_slug, monkeypatch):
    monkeypatch.setenv("HUD_API_TOKEN", TOKEN)
    set_rent(fast, source="hud_api")
    fake.fail["api_counties"] = 401
    pid = add_prop(conn, market_slug, "h1", zip="45503", state="OH", county="CLARK", lat=39.95, lng=-83.77)
    add_prop(conn, market_slug, "h2", zip="45504", state="OH", county="CLARK", lat=39.93, lng=-83.76)
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert res["errors"] == 0 and res["fits"]["na"] == 0
    assert fit_row(conn, pid)["source"] == "hud_arcgis"
    assert fake.count("api_counties") == 1                    # stops asking once the token is turned down
    assert any("turned down the HUD_API_TOKEN" in w for w in res["warnings"])


def test_hud_api_without_token_uses_map_service(conn, fast, fake, market_slug, scenario):
    set_rent(fast, source="hud_api")
    res = compute_rent_fits(conn, market_slug, settings=fast, client=fake.client())
    assert res["source"] == "hud_arcgis"
    assert any("HUD_API_TOKEN is blank" in w for w in res["warnings"])
    assert fake.count("api_counties") == 0


# --------------------------------------------------------------------------------------------
# Live checks (BLUEMOON_LIVE=1)
# --------------------------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="set BLUEMOON_LIVE=1 to call HUD's real map service")
def test_live_downtown_springfield(conn, settings):
    r = fmr_for_point(conn, 39.9242, -83.8088, settings=settings)
    assert r is not None, "HUD's map service returned nothing"
    assert r["source"] == "hud_arcgis" and "Springfield" in r["area_name"]
    assert int(r["fiscal_year"][2:]) >= 2026
    assert 500 < r["rents"]["2"] < 3000 and 500 < r["area_rents"]["2"] < 3000
    print(json.dumps(r))


@pytest.mark.skipif(not LIVE, reason="set BLUEMOON_LIVE=1 to call HUD's real map service")
def test_live_real_crexi_export(conn, settings, market_slug, real_crexi):
    crexi = pytest.importorskip("bluemoon.ingest.crexi")
    crexi.import_file(conn, market_slug, real_crexi)
    res = compute_rent_fits(conn, market_slug, settings=settings)
    assert res["errors"] == 0
    assert sum(res["fits"].values()) == res["properties"] > 0
    print({k: res[k] for k in ("fits", "small_area", "area", "fiscal_years", "requests")})
