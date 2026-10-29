"""Tests for bluemoon.ingest (Crexi import, column mapping, dedupe and merge).

All data here is made up. The optional real-data test runs only when BLUEMOON_REAL_CREXI is set and
asserts counts only (it never prints owner data).
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from bluemoon.db import one, rows, scalar
from bluemoon.ingest import IngestError, import_file, map_columns, preview_file, read_table
from bluemoon.ingest.columns import (
    FIELD_ALIASES,
    HELPER_FIELDS,
    INFO_FIELDS,
    OWNER_FIELDS,
    PROPERTY_FIELDS,
    check_overrides,
    detect_provider,
    norm_header,
)
from bluemoon.ingest.crexi import (
    RECORD_COLUMNS,
    ImportOptions,
    apn_compact,
    final_flags,
    parse_row,
    source_id_from_link,
)

MARKET = "apartments-springfield-oh"
STORAGE = "self-storage-springfield-oh"


# --- helpers ---------------------------------------------------------------------------------

def fixture_rows(sample_csv: Path) -> tuple[list[str], list[dict]]:
    with sample_csv.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), [dict(r) for r in reader]


def write_csv(path: Path, headers: list[str], data: list[dict], delimiter: str = ",",
              encoding: str = "utf-8") -> Path:
    with path.open("w", newline="", encoding=encoding) as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter=delimiter, extrasaction="ignore")
        w.writeheader()
        for r in data:
            w.writerow(r)
    return path


def crexi_row(headers: list[str], **values: str) -> dict:
    """A blank Crexi row with the given values (keys are Crexi headers, underscores for spaces)."""
    row = {h: "" for h in headers}
    for k, v in values.items():
        row[k.replace("_", " ")] = v
    return row


def opts(**kw) -> ImportOptions:
    base = {"today_year": date.today().year, "units_metric": True}
    base.update(kw)
    return ImportOptions(**base)


def crexi_map(sample_csv: Path) -> dict[str, str]:
    headers, _ = fixture_rows(sample_csv)
    cmap, _ = map_columns(headers)
    return cmap


def prop(conn, **where) -> dict:
    (k, v), = where.items()
    r = one(conn, f"SELECT * FROM properties WHERE {k}=?", (v,))
    assert r is not None, f"no property with {k}={v}"
    return r


def prop_alternates(conn, pid: int, field: str) -> list:
    """The values Crexi sent that differ from the stored property (crexi/merge.py), oldest first."""
    return [json.loads(r["value"]) for r in conn.execute(
        "SELECT value FROM property_alternates WHERE property_id=? AND field=? ORDER BY id", (pid, field))]


# --- column mapping --------------------------------------------------------------------------

def test_every_crexi_header_maps(sample_csv):
    headers, _ = fixture_rows(sample_csv)
    assert len(headers) == 70
    cmap, unmapped = map_columns(headers)
    assert unmapped == []
    assert len(cmap) == 70
    assert cmap["units"] == "Number of Units"
    assert cmap["number_of_addresses"] == "Number of Addresses"
    assert cmap["lot_acres"] == "Lot Size Acres"
    assert cmap["lot_sqft"] == "Lot Size SqFt"
    assert cmap["crexi_url"] == "Property Link"
    assert cmap["total_value"] == "Total Parcel Value"
    assert cmap["mailing_zip"] == "Mailing Address Zip Code"
    assert cmap["unit"] == "Unit"


def test_aliases_are_unambiguous():
    seen: dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            n = norm_header(alias)
            assert seen.get(n, field) == field, f"'{alias}' is an alias of both {seen[n]} and {field}"
            seen[n] = field
    assert PROPERTY_FIELDS | HELPER_FIELDS | INFO_FIELDS == set(FIELD_ALIASES)
    assert not PROPERTY_FIELDS & HELPER_FIELDS


def test_property_fields_are_db_columns(conn):
    cols = {r["name"] for r in rows(conn, "PRAGMA table_info(properties)")}
    assert set(RECORD_COLUMNS) <= cols
    assert PROPERTY_FIELDS <= set(RECORD_COLUMNS)


def test_other_provider_headers_map():
    headers = ["Parcel Number", "Site Address", "Site City", "Site State", "Site Zip", "Owner 1", "Units",
               "Year Built", "Bldg Sq Ft", "Lat", "Long", "Acreage", "Mail Address", "Mail City", "Mail State",
               "Mail Zip", "Last Sale Date", "Last Sale Amount", "# of Units Extra", "Random Notes"]
    cmap, unmapped = map_columns(headers)
    assert cmap["apn"] == "Parcel Number"
    assert cmap["address"] == "Site Address"
    assert cmap["city"] == "Site City"
    assert cmap["owner_name"] == "Owner 1"
    assert cmap["units"] == "Units"
    assert cmap["year_built"] == "Year Built"
    assert cmap["building_sqft"] == "Bldg Sq Ft"
    assert cmap["lat"] == "Lat" and cmap["lng"] == "Long"
    assert cmap["lot_acres"] == "Acreage"
    assert cmap["mailing_address"] == "Mail Address"
    assert cmap["sale_price"] == "Last Sale Amount"
    assert "Random Notes" in unmapped


def test_header_matching_ignores_case_and_punctuation():
    cmap, _ = map_columns(["  number of UNITS ", "YEAR-BUILT", "# of Stories", "Parcel #", "zip_code"])
    assert cmap["units"] == "  number of UNITS "
    assert cmap["year_built"] == "YEAR-BUILT"
    assert cmap["stories"] == "# of Stories"
    assert cmap["apn"] == "Parcel #"
    assert cmap["zip"] == "zip_code"


def test_fuzzy_match_and_digit_guard():
    cmap, unmapped = map_columns(["Yr. Builtt", "Owner 2 Name", "Mailing Address Zip", "Lot Size"])
    assert cmap["year_built"] == "Yr. Builtt"
    assert cmap["mailing_zip"] == "Mailing Address Zip"
    assert "owner_name" not in cmap  # "Owner 2" is never the primary owner
    assert "Owner 2 Name" in unmapped
    assert "Lot Size" in unmapped  # acres or sq ft? too ambiguous to guess
    cmap, _ = map_columns(["Yr. Builtt"], min_score=99)
    assert "year_built" not in cmap


def test_overrides_win_and_can_unmap():
    headers = ["Address", "Doors", "Number of Units", "Owner Name"]
    cmap, unmapped = map_columns(headers, overrides={"units": "doors", "owner_name": None})
    assert cmap["units"] == "Doors"
    assert "owner_name" not in cmap
    assert "Number of Units" in unmapped and "Owner Name" in unmapped
    # an override can take a header another field would have matched
    cmap, _ = map_columns(["Number of Addresses"], overrides={"units": "Number of Addresses"})
    assert cmap == {"units": "Number of Addresses"}


def test_check_overrides_reports_problems():
    warnings = check_overrides(["Address"], {"units": "Doors", "nonsense": "Address", "address": "address"})
    assert len(warnings) == 2
    assert any("Doors" in w for w in warnings)
    assert any("nonsense" in w for w in warnings)


def test_detect_provider(sample_csv):
    headers, _ = fixture_rows(sample_csv)
    assert detect_provider(headers) == "crexi"
    assert detect_provider(["Owner 1 First Name", "Owner 1 Last Name", "Est. Equity", "Address"]) == "propstream"
    assert detect_provider(["PropertyID", "RBA", "True Owner Name"]) == "costar"
    assert detect_provider(["Parcel Number", "Site Address"]) == "other"


# --- reading files ---------------------------------------------------------------------------

def test_read_csv_fixture(sample_csv):
    data = read_table(sample_csv)
    assert len(data) == 12
    assert data[0]["Address"] == "1200 SAMPLE RIDGE DR"
    assert data[0]["Unit"] is None  # empty cells are None


def test_read_csv_semicolon_cp1252_and_excel_text(tmp_path):
    p = tmp_path / "list.csv"
    p.write_bytes('APN;Address;City;Owner Name\n="00123";12 Caf\xe9 St;Dayton;Ren\xe9e Smith\n'
                  .encode("cp1252"))
    data = read_table(p)
    assert data == [{"APN": "00123", "Address": "12 Café St", "City": "Dayton", "Owner Name": "Renée Smith"}]


def test_read_csv_finds_header_below_title_rows_and_dedupes_headers(tmp_path):
    p = tmp_path / "list.csv"
    p.write_text("My county export,,,\nRun on 2026-01-01,,,\nParcel Number,Site Address,Owner,Owner\n"
                 "1-2-3,5 Main St,A,B\n,,,\n", encoding="utf-8")
    from bluemoon.ingest.crexi import _read
    table = _read(p)
    assert table.header_row == 3
    assert table.headers == ["Parcel Number", "Site Address", "Owner", "Owner (2)"]
    assert table.rows == [{"Parcel Number": "1-2-3", "Site Address": "5 Main St", "Owner": "A", "Owner (2)": "B"}]


def _make_xlsx(path: Path, headers: list[str], data: list[list]) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    key = wb.active
    key.title = "Key"
    key.append(["Color Key: which columns matter"])
    key.append(["Address", "where it is"])
    key.append(["Owner Name", "who owns it"])
    ws = wb.create_sheet("Properties")
    ws.append(headers + ["Avg SqFt per Unit (calc)", None])
    for i, r in enumerate(data, start=2):
        ws.append(r + [f"=IF(AA{i}>0,Z{i}/AA{i},\"\")", None])
    wb.save(path)
    return path


def test_read_xlsx_picks_properties_sheet_and_skips_formulas(tmp_path):
    headers = ["Property Link", "APN", "Address", "City", "State", "Zip Code", "County", "Sale Date",
               "Number of Units", "Year Built", "Owner Name"]
    p = _make_xlsx(tmp_path / "export.xlsx", headers, [
        ["https://www.crexi.com/property-records/x/abc123", "12-34", "5 Elm St", "Xenia", "OH", 45385, "GREENE",
         datetime(2001, 5, 2), 12, 1980, "TEST OWNER LLC"],
        ["https://www.crexi.com/property-records/y/def456", 1234567, "6 Elm St", "Xenia", "OH", 45385, "GREENE",
         datetime(2003, 6, 7, 13, 30), 14, 9999, None],
    ])
    from bluemoon.ingest.crexi import _read
    table = _read(p)
    assert table.sheet == "Properties"
    assert table.skipped_sheets == ["Key"]
    assert table.skipped_columns == ["Avg SqFt per Unit (calc)"]
    assert table.headers == headers
    assert table.rows[0]["Sale Date"] == "2001-05-02"
    assert table.rows[1]["Sale Date"] == "2003-06-07T13:30:00"
    assert table.rows[1]["APN"] == 1234567


def test_import_xlsx(conn, tmp_path):
    headers = ["Property Link", "APN", "Address", "City", "State", "Zip Code", "County", "Number of Units",
               "Year Built", "Latitude", "Longitude", "Owner Name"]
    p = _make_xlsx(tmp_path / "export.xlsx", headers, [
        ["https://www.crexi.com/property-records/x/abc123", "12-34", "5 Elm St", "Xenia", "OH", 45385,
         "Greene County", 12, 1980, 39.68, -83.93, "TEST OWNER LLC"],
    ])
    res = import_file(conn, MARKET, p)
    assert res["rows_new"] == 1 and res["sheet"] == "Properties"
    assert res["skipped_columns"] == ["Avg SqFt per Unit (calc)"]
    p1 = prop(conn, source_id="abc123")
    assert p1["county"] == "GREENE" and p1["zip"] == "45385" and p1["units"] == 12
    raw = json.loads(p1["raw_json"])
    assert "Avg SqFt per Unit (calc)" not in raw and raw["Zip Code"] == 45385


def test_unreadable_files(tmp_path):
    with pytest.raises(IngestError, match="can't find"):
        read_table(tmp_path / "missing.csv")
    (tmp_path / "old.xls").write_bytes(b"x")
    with pytest.raises(IngestError, match="xlsx or .csv"):
        read_table(tmp_path / "old.xls")
    (tmp_path / "doc.pdf").write_bytes(b"x")
    with pytest.raises(IngestError, match="isn't a CSV"):
        read_table(tmp_path / "doc.pdf")
    (tmp_path / "empty.csv").write_text("", encoding="utf-8")
    with pytest.raises(IngestError, match="empty"):
        read_table(tmp_path / "empty.csv")
    (tmp_path / "bad.xlsx").write_bytes(b"not a zip")
    with pytest.raises(IngestError, match="couldn't open"):
        read_table(tmp_path / "bad.xlsx")


# --- parsing rules ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,year,flag", [
    ("1978", 1978, None), ("0", None, "year_invalid"), ("9999", None, "year_invalid"),
    (str(date.today().year + 1), None, "year_invalid"), ("", None, "year_unknown"),
    ("19xx", None, "year_invalid"), ("1978.0", 1978, None), ("85", None, "year_invalid"),
])
def test_year_rules(sample_csv, value, year, flag):
    headers, _ = fixture_rows(sample_csv)
    p = parse_row(crexi_row(headers, Address="1 A St", Year_Built=value), crexi_map(sample_csv), opts())
    assert p.record.year_built == year
    flags = final_flags(p.record, p.notes, True)
    if flag:
        assert flag in flags
    else:
        assert "year_invalid" not in flags and "year_unknown" not in flags


def test_units_fallback_to_addresses(sample_csv):
    headers, _ = fixture_rows(sample_csv)
    cmap = crexi_map(sample_csv)
    p = parse_row(crexi_row(headers, Address="1 A St", Number_of_Addresses="8"), cmap, opts())
    assert p.record.units == 8 and "units_from_addresses" in p.notes
    p = parse_row(crexi_row(headers, Address="1 A St", Number_of_Addresses="1"), cmap, opts())
    assert p.record.units is None  # one address says nothing about the unit count
    assert "units_unknown" in final_flags(p.record, p.notes, True)
    assert "units_unknown" not in final_flags(p.record, p.notes, False)
    p = parse_row(crexi_row(headers, Address="1 A St", Number_of_Units="24", Number_of_Addresses="2"), cmap, opts())
    assert p.record.units == 24 and "units_from_addresses" not in p.notes
    p = parse_row(crexi_row(headers, Address="1 A St", Number_of_Units="0"), cmap, opts())
    assert p.record.units is None


def test_lot_acres_from_sqft(sample_csv):
    headers, _ = fixture_rows(sample_csv)
    cmap = crexi_map(sample_csv)
    p = parse_row(crexi_row(headers, Address="1 A St", Lot_Size_SqFt="87,120"), cmap, opts())
    assert p.record.lot_acres == 2.0
    p = parse_row(crexi_row(headers, Address="1 A St", Lot_Size_Acres="1.5", Lot_Size_SqFt="87120"), cmap, opts())
    assert p.record.lot_acres == 1.5


def test_distress_flags(sample_csv):
    headers, _ = fixture_rows(sample_csv)
    cmap = crexi_map(sample_csv)
    p = parse_row(crexi_row(headers, Address="1 A St"), cmap, opts())
    assert p.record.preforeclosure == 0 and p.record.reo == 0 and p.record.usps_vacant is None
    p = parse_row(crexi_row(headers, Address="1 A St", PFC_Document_Type="Notice of Lis Pendens",
                            REO_Sale_Flag="Yes", USPS_Vacancy="Yes", Opportunity_Zone="No"), cmap, opts())
    assert p.record.preforeclosure == 1 and p.record.reo == 1
    assert p.record.usps_vacant == 1 and p.record.opportunity_zone == 0
    p = parse_row(crexi_row(headers, Address="1 A St", PFC_Recording_Date="2025-02-03"), cmap, opts())
    assert p.record.preforeclosure == 1
    # A provider without PFC/REO columns: unknown, not "no".
    cmap2, _ = map_columns(["Address", "Pre-Foreclosure"])
    p = parse_row({"Address": "1 A St", "Pre-Foreclosure": "No"}, cmap2, opts())
    assert p.record.preforeclosure == 0 and p.record.reo is None
    p = parse_row({"Address": "1 A St"}, map_columns(["Address"])[0], opts())
    assert p.record.preforeclosure is None


def test_location_normalization(sample_csv):
    headers, _ = fixture_rows(sample_csv)
    cmap = crexi_map(sample_csv)
    p = parse_row(crexi_row(headers, Address="  12   Main  St ", County="Clark County", Zip_Code="45503-1234",
                            State="Ohio", Mailing_Address_Zip_Code="2134", Latitude="0", Longitude="0",
                            APN="  340 01-00 "), cmap, opts())
    r = p.record
    assert r.address == "12 Main St"
    assert r.county == "CLARK" and r.zip == "45503" and r.state == "OH" and r.mailing_zip == "02134"
    assert r.lat is None and r.lng is None and r.geocode_quality is None
    assert "no_coordinates" in final_flags(r, p.notes, True)
    assert r.apn == "340 01-00" and apn_compact(r.apn) == "3400100"
    assert p.apn_base == "apn:OH:CLARK:3400100"
    p = parse_row(crexi_row(headers, Address="1 A St", Latitude="39.9", Longitude="-83.8"), cmap, opts())
    assert (p.record.lat, p.record.lng, p.record.geocode_quality) == (39.9, -83.8, "source")
    p = parse_row(crexi_row(headers, Address="1 A St", Latitude="139.9", Longitude="-83.8"), cmap, opts())
    assert p.record.lat is None


def test_values_and_dates(sample_csv):
    headers, _ = fixture_rows(sample_csv)
    cmap = crexi_map(sample_csv)
    p = parse_row(crexi_row(headers, Address="1 A St", Parcel_Value_Type='[\n  "APPRAISED"\n]',
                            Sold_Price="$1,250,000", Building_SqFt="24,000 SF", Sale_Date="3/15/2021",
                            Financing_Maturity_Date="2027-03-01T00:00:00", Interest_Rate="4.5%",
                            Owner_Name="N/A"), cmap, opts())
    r = p.record
    assert r.value_type == "APPRAISED"
    assert r.sale_price == 1250000 and r.building_sqft == 24000 and r.interest_rate == 4.5
    assert r.sale_date == "2021-03-15" and r.loan_maturity == "2027-03-01"
    assert r.owner_name is None and "no_owner_name" in final_flags(r, p.notes, True)


@pytest.mark.parametrize("value,expected", [
    ("20210315", "2021-03-15"), ("44270", "2021-03-15"), (44270, "2021-03-15"), ("2021-03-15", "2021-03-15"),
    ("03/15/2021", "2021-03-15"), ("garbage", None), ("12", None), ("20211399", None),
])
def test_date_forms(value, expected):
    from bluemoon.ingest.crexi import _date
    assert _date(value) == expected


def test_count_delimiter_fallback():
    from bluemoon.ingest.crexi import _count_delimiter
    assert _count_delimiter("a;b;c\n1;2;3\n", ".csv") == ";"
    assert _count_delimiter("a|b\n1|2\n", ".csv") == "|"
    assert _count_delimiter("single\nvalue\n", ".tsv") == "\t"
    assert _count_delimiter("single\nvalue\n", ".csv") == ","


def test_owner_from_first_and_last_name():
    cmap, _ = map_columns(["Address", "Owner 1 First Name", "Owner 1 Last Name"])
    p = parse_row({"Address": "1 A St", "Owner 1 First Name": "Jane", "Owner 1 Last Name": "Doe"}, cmap, opts())
    assert p.record.owner_name == "Jane Doe"


@pytest.mark.parametrize("url,expected", [
    ("https://www.crexi.com/property-records/123-SPRINGFIELD-OH-45503/0a1b2c3d4e5f60718293a4b5c6d7e8f901234567",
     "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"),
    ("https://www.crexi.com/search?drawerPosition=end&selectedId=12-34;56.78;901&isMultiApn=true", "12-34;56.78;901"),
    ("https://www.crexi.com/properties/987654/ohio-sample-apartments", "987654"),
    ("https://www.crexi.com/property-records/sample-3", "sample-3"),
    ("www.crexi.com/property-records/a/b", "b"),
    ("not a link", None), (None, None), ("https://www.crexi.com/", None),
])
def test_source_id_from_link(url, expected):
    assert source_id_from_link(url) == expected


# --- import ----------------------------------------------------------------------------------

def test_import_fixture(conn, sample_csv):
    res = import_file(conn, MARKET, sample_csv)
    for k in ("import_id", "filename", "rows_total", "rows_new", "rows_updated", "rows_skipped", "column_map",
              "unmapped_columns", "warnings"):
        assert k in res
    assert res["rows_total"] == 12
    assert res["rows_new"] == 11 and res["rows_updated"] == 0 and res["rows_skipped"] == 1
    assert res["duplicates_collapsed"] == 1 and res["unmapped_columns"] == []
    assert res["provider"] == "crexi"
    assert scalar(conn, "SELECT count(*) FROM properties") == 11

    imp = one(conn, "SELECT * FROM imports WHERE id=?", (res["import_id"],))
    assert (imp["rows_total"], imp["rows_new"], imp["rows_updated"], imp["rows_skipped"]) == (12, 11, 0, 1)
    assert json.loads(imp["column_map_json"])["units"] == "Number of Units"
    assert imp["file_hash"]

    # every property got a DRILL state (level 1, active)
    assert scalar(conn, "SELECT count(*) FROM drill_state WHERE level=1 AND status='active'") == 11

    p1 = prop(conn, source_id="sample-1")
    assert p1["property_key"] == "apn:OH:CLARK:3400100002000"
    assert p1["county"] == "CLARK" and p1["source"] == "crexi"
    assert p1["crexi_url"] == "https://www.crexi.com/property-records/sample-1"
    assert p1["loan_maturity"] == "2027-03-01" and p1["lender"] == "FIRST TEST BANK"
    assert p1["first_import_id"] == p1["last_import_id"] == res["import_id"]
    raw = json.loads(p1["raw_json"])
    assert len(raw) == 70 and raw["County"] == "CLARK COUNTY"

    p3 = prop(conn, source_id="sample-3")
    assert p3["units"] == 8
    assert set(json.loads(p3["flags_json"])) == {"no_owner_name", "units_from_addresses"}

    dup = prop(conn, apn="A01-0001-0003")
    assert "shared_apn" in json.loads(dup["flags_json"])
    assert res["flag_counts"]["shared_apn"] == 1


def test_reimport_merges_and_keeps_ids(conn, sample_csv):
    first = import_file(conn, MARKET, sample_csv)
    before = {r["property_key"]: r["id"] for r in rows(conn, "SELECT id, property_key FROM properties")}
    second = import_file(conn, MARKET, sample_csv)
    assert second["rows_new"] == 0 and second["rows_updated"] == 11 and second["rows_skipped"] == 1
    after = {r["property_key"]: r["id"] for r in rows(conn, "SELECT id, property_key FROM properties")}
    assert before == after
    p = prop(conn, source_id="sample-0")
    assert p["first_import_id"] == first["import_id"] and p["last_import_id"] == second["import_id"]
    assert any("imported before" in w for w in second["warnings"])
    assert scalar(conn, "SELECT count(*) FROM drill_state") == 11


def test_reimport_updates_values_but_never_wipes(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    import_file(conn, MARKET, sample_csv)
    pid = prop(conn, source_id="sample-0")["id"]
    conn.execute("UPDATE properties SET flags_json=? WHERE id=?", (json.dumps(["checked_by_hand"]), pid))
    conn.commit()
    row = dict(data[0])
    row.update({"Owner Name": "", "Sold Price": "450000", "Latitude": "", "Longitude": "", "Number of Units": ""})
    import_file(conn, MARKET, write_csv(tmp_path / "update.csv", headers, [row]))
    p = prop(conn, id=pid)
    assert p["owner_name"] == "HOLLOWAY FAMILY TRUST"  # blank cell did not wipe it
    assert p["sale_price"] == 410000                    # the original value stays (keep-the-original rule) ...
    alt = conn.execute("SELECT value FROM property_alternates WHERE property_id=? AND field='sale_price'",
                       (pid,)).fetchone()
    assert alt is not None and json.loads(alt[0]) == 450000      # ... and the new one waits as an alternate
    assert p["lat"] == 39.9501                          # coordinates kept
    assert p["units"] == 28
    flags = json.loads(p["flags_json"])
    assert "checked_by_hand" in flags                   # another module's flag survives
    assert "no_owner_name" not in flags and "no_coordinates" not in flags


def test_address_count_never_replaces_real_units(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    import_file(conn, MARKET, sample_csv)
    row = dict(data[0])
    row.update({"Number of Units": "", "Number of Addresses": "3"})
    import_file(conn, MARKET, write_csv(tmp_path / "u.csv", headers, [row]))
    p = prop(conn, source_id="sample-0")
    assert p["units"] == 28 and "units_from_addresses" not in json.loads(p["flags_json"])


def test_geocoded_coordinates_survive_reimport(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    row = dict(data[0])
    row.update({"Latitude": "", "Longitude": ""})
    f = write_csv(tmp_path / "nocoords.csv", headers, [row])
    import_file(conn, MARKET, f)
    p = prop(conn, source_id="sample-0")
    assert p["lat"] is None and "no_coordinates" in json.loads(p["flags_json"])
    conn.execute("UPDATE properties SET lat=39.95, lng=-83.77, geocode_quality='census' WHERE id=?", (p["id"],))
    conn.commit()
    import_file(conn, MARKET, f)
    p = prop(conn, id=p["id"])
    assert (p["lat"], p["geocode_quality"]) == (39.95, "census")
    assert "no_coordinates" not in json.loads(p["flags_json"])


def test_same_apn_two_addresses_keeps_both(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    a, b = dict(data[0]), dict(data[0])
    b.update({"Address": "1202 SAMPLE RIDGE DR", "Property Link": "https://www.crexi.com/property-records/x/other"})
    res = import_file(conn, MARKET, write_csv(tmp_path / "shared.csv", headers, [a, b]))
    assert res["rows_new"] == 2 and res["rows_skipped"] == 0
    got = rows(conn, "SELECT property_key, flags_json FROM properties ORDER BY id")
    assert [g["property_key"] for g in got] == ["apn:OH:CLARK:3400100001000|1200 SAMPLE RIDGE DR",
                                                "apn:OH:CLARK:3400100001000|1202 SAMPLE RIDGE DR"]
    assert all("shared_apn" in json.loads(g["flags_json"]) for g in got)
    assert ("1 parcel number was listed with more than one street address. Each address was kept as its own "
            "property (2 in all) and flagged so you can check them.") in res["warnings"]
    # re-import: still two, no new rows, nothing changed
    res2 = import_file(conn, MARKET, tmp_path / "shared.csv")
    assert res2["rows_new"] == 0 and res2["rows_updated"] == 2 and res2["rows_unchanged"] == 2


def test_shared_parcel_note_counts_parcels_not_address_keys(conn, sample_csv, tmp_path):
    """Address keys ('addr:STREET|CITY|ST|ZIP') contain '|' too; they are not parcels shared by two addresses."""
    headers, data = fixture_rows(sample_csv)
    no_apn = [dict(r, APN="") for r in data[:3]]
    res = import_file(conn, MARKET, write_csv(tmp_path / "noapn.csv", headers, no_apn))
    assert res["rows_new"] == 3
    keys = [r["property_key"] for r in rows(conn, "SELECT property_key FROM properties")]
    assert all(k.startswith("addr:") and "|" in k for k in keys)
    assert not any("parcel number" in w for w in res["warnings"])
    a, b, c = dict(data[0]), dict(data[0]), dict(data[0])
    b.update({"Address": "1202 SAMPLE RIDGE DR", "Property Link": "https://www.crexi.com/property-records/x/b"})
    c.update({"Address": "1204 SAMPLE RIDGE DR", "Property Link": "https://www.crexi.com/property-records/x/c"})
    res = import_file(conn, MARKET, write_csv(tmp_path / "three.csv", headers, [a, b, c, *no_apn]))
    assert [w for w in res["warnings"] if "parcel number" in w] == [
        "1 parcel number was listed with more than one street address. Each address was kept as its own property "
        "(3 in all) and flagged so you can check them."]


def test_reimport_counts_rows_whose_values_did_not_change(conn, sample_csv, tmp_path):
    first = import_file(conn, MARKET, sample_csv)
    assert first["rows_unchanged"] == 0
    again = import_file(conn, MARKET, sample_csv)
    assert again["rows_updated"] == 11 and again["rows_unchanged"] == 11       # same file: nothing changed
    headers, data = fixture_rows(sample_csv)
    edited = [dict(r) for r in data]
    conn.execute("UPDATE properties SET sale_price = NULL WHERE source_id = 'sample-0'")    # a blank the file fills
    conn.commit()
    edited[0]["Sold Price"] = "451000"
    res = import_file(conn, MARKET, write_csv(tmp_path / "edited.csv", headers, edited))
    assert res["rows_updated"] == 11 and res["rows_unchanged"] == 10
    assert prop(conn, source_id="sample-0")["sale_price"] == 451000
    # a new column in the export changes the original data kept for every row
    extra = [dict(r, **{"Brand New Column": "x"}) for r in edited]
    res = import_file(conn, MARKET, write_csv(tmp_path / "wider.csv", [*headers, "Brand New Column"], extra))
    assert res["rows_updated"] == 11 and res["rows_unchanged"] == 0


def test_shared_apn_later_import_upgrades_existing_key(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    a = dict(data[0])
    import_file(conn, MARKET, write_csv(tmp_path / "one.csv", headers, [a]))
    pid = prop(conn, source_id="sample-0")["id"]
    b = dict(a)
    b.update({"Address": "1202 SAMPLE RIDGE DR", "Property Link": "https://www.crexi.com/property-records/x/other"})
    res = import_file(conn, MARKET, write_csv(tmp_path / "two.csv", headers, [a, b]))
    assert res["rows_new"] == 1 and res["rows_updated"] == 1
    p = prop(conn, id=pid)
    assert p["property_key"] == "apn:OH:CLARK:3400100001000|1200 SAMPLE RIDGE DR"


def test_apn_punctuation_variants_collapse(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    a, b = dict(data[6]), dict(data[6])
    a.update({"APN": "A01-0001 0001", "Number of Units": "1"})
    b.update({"APN": "A0100010001", "Number of Units": "44", "Property Link": "https://www.crexi.com/property-records/z/zz"})
    res = import_file(conn, MARKET, write_csv(tmp_path / "v.csv", headers, [a, b]))
    assert res["rows_new"] == 1 and res["duplicates_collapsed"] == 1
    p = prop(conn, property_key="apn:OH:MONTGOMERY:A0100010001")
    assert p["units"] == 1   # the first row is the original; the second row's 44 is kept as an alternate
    assert prop_alternates(conn, p["id"], "units") == [44]
    assert p["apn"] == "A01-0001 0001"
    assert "shared_apn" in json.loads(p["flags_json"])


def test_rows_without_apn_use_address_and_upgrade_later(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    row = dict(data[1])
    row["APN"] = ""
    import_file(conn, MARKET, write_csv(tmp_path / "noapn.csv", headers, [row]))
    p = prop(conn, source_id="sample-1")
    assert p["property_key"] == "addr:455 EXAMPLE AVE|SPRINGFIELD|OH|45505"
    res = import_file(conn, MARKET, write_csv(tmp_path / "apn.csv", headers, [data[1]]))
    assert res["rows_new"] == 0 and res["rows_updated"] == 1
    p2 = prop(conn, id=p["id"])
    assert p2["property_key"] == "apn:OH:CLARK:3400100002000" and p2["apn"] == "340-01-00-002-000"


def test_link_only_row_upgrades_to_address_key(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    link_only = crexi_row(headers, Property_Link=data[0]["Property Link"])
    import_file(conn, MARKET, write_csv(tmp_path / "l.csv", headers, [link_only]))
    pid = prop(conn, source_id="sample-0")["id"]
    row = dict(data[0])
    row["APN"] = ""
    res = import_file(conn, MARKET, write_csv(tmp_path / "m.csv", headers, [row]))
    assert res["rows_new"] == 0 and res["rows_updated"] == 1
    assert prop(conn, id=pid)["property_key"].startswith("addr:")


def test_different_apn_same_address_stays_separate(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    import_file(conn, MARKET, write_csv(tmp_path / "a.csv", headers, [data[0]]))
    row = dict(data[0])
    row.update({"APN": "999-99", "Property Link": "https://www.crexi.com/property-records/q/new"})
    res = import_file(conn, MARKET, write_csv(tmp_path / "b.csv", headers, [row]))
    assert res["rows_new"] == 1
    assert scalar(conn, "SELECT count(*) FROM properties") == 2


def test_rows_with_no_identity_are_skipped(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    blank = crexi_row(headers, City="SPRINGFIELD", Number_of_Units="12")
    link_only = crexi_row(headers, Property_Link="https://www.crexi.com/property-records/x/only-link")
    res = import_file(conn, MARKET, write_csv(tmp_path / "x.csv", headers, [data[0], blank, link_only]))
    assert res["rows_total"] == 3 and res["rows_new"] == 2 and res["rows_skipped"] == 1
    assert res["rows_without_id"] == 1
    assert prop(conn, source_id="only-link")["property_key"] == "src:crexi:only-link"
    assert any("no parcel number, address or Crexi link" in w for w in res["warnings"])


def test_limit(conn, sample_csv):
    res = import_file(conn, MARKET, sample_csv, limit=5)
    assert res["rows_total"] == 5 and res["rows_new"] == 5
    assert scalar(conn, "SELECT count(*) FROM properties") == 5
    assert import_file(conn, MARKET, sample_csv, limit=-3)["rows_total"] == 0


def test_override_argument_and_config_overrides(conn, tmp_path, settings):
    p = write_csv(tmp_path / "odd.csv", ["Site Address", "Doors", "Built In"],
                  [{"Site Address": "5 Elm St", "Doors": "16", "Built In": "1984"}])
    res = import_file(conn, MARKET, p, overrides={"units": "Doors"})
    assert res["column_map"]["units"] == "Doors"
    assert prop(conn, address="5 Elm St")["units"] == 16

    import yaml
    from bluemoon.config import reload_settings
    cfg_path = settings.root / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["crexi"]["column_overrides"] = {"year_built": "Built In"}
    cfg_path.write_text(yaml.safe_dump(cfg))
    reload_settings()
    res = import_file(conn, MARKET, p)
    assert res["column_map"]["year_built"] == "Built In"
    assert prop(conn, address="5 Elm St")["year_built"] == 1984
    pv = preview_file(p)
    assert pv["column_map"]["year_built"] == "Built In"


def test_units_unknown_only_for_unit_based_asset_classes(conn, sample_csv, tmp_path):
    headers, data = fixture_rows(sample_csv)
    row = dict(data[0])
    row.update({"Number of Units": "", "Number of Addresses": ""})
    f = write_csv(tmp_path / "s.csv", headers, [row])
    import_file(conn, MARKET, f)
    import_file(conn, STORAGE, f)
    flags = {r["slug"]: json.loads(r["flags_json"]) for r in rows(
        conn, "SELECT m.slug, p.flags_json FROM properties p JOIN markets m ON m.id=p.market_id")}
    assert "units_unknown" in flags[MARKET]
    assert "units_unknown" not in flags[STORAGE]


def test_markets_are_separate(conn, sample_csv):
    import_file(conn, MARKET, sample_csv)
    res = import_file(conn, STORAGE, sample_csv)
    assert res["rows_new"] == 11
    assert scalar(conn, "SELECT count(*) FROM properties") == 22


def test_plain_errors(conn, tmp_path):
    with pytest.raises(IngestError, match="no market called"):
        import_file(conn, "nope", tmp_path / "x.csv")
    p = write_csv(tmp_path / "notes.csv", ["Color", "Mood"], [{"Color": "red", "Mood": "ok"}])
    with pytest.raises(IngestError, match="couldn't find an address or parcel number"):
        import_file(conn, MARKET, p)
    assert scalar(conn, "SELECT count(*) FROM imports") == 0


def test_other_provider_file(conn, tmp_path):
    headers = ["Parcel Number", "Site Address", "City", "State", "Zip", "County", "Owner 1", "Units",
               "Year Built", "Bldg Sq Ft", "Lat", "Long"]
    p = write_csv(tmp_path / "county.csv", headers, [{
        "Parcel Number": "123-45", "Site Address": "9 Oak St", "City": "Urbana", "State": "OH", "Zip": "43078",
        "County": "Champaign", "Owner 1": "OAK RENTALS LLC", "Units": "20", "Year Built": "1975",
        "Bldg Sq Ft": "16000", "Lat": "40.1", "Long": "-83.75"}])
    res = import_file(conn, MARKET, p)
    assert res["rows_new"] == 1 and res["provider"] == "other"
    r = prop(conn, apn="123-45")
    assert r["source"] == "list" and r["county"] == "CHAMPAIGN" and r["units"] == 20
    assert r["property_key"] == "apn:OH:CHAMPAIGN:12345"


def test_progress_callback_and_broken_callback(conn, sample_csv):
    seen: list[tuple[float, str]] = []
    import_file(conn, MARKET, sample_csv, progress=lambda f, m: seen.append((f, m)))
    assert seen[0][0] < seen[-1][0] == 1.0
    assert all(isinstance(m, str) for _, m in seen)

    def broken(_f, _m):
        raise RuntimeError("boom")
    assert import_file(conn, MARKET, sample_csv, progress=broken)["rows_updated"] == 11


def test_drill_state_failure_does_not_lose_the_import(conn, sample_csv, monkeypatch):
    import bluemoon.drill.engine as engine

    def fail(*_a, **_k):
        raise RuntimeError("engine not ready")
    monkeypatch.setattr(engine, "ensure_states", fail)
    res = import_file(conn, MARKET, sample_csv)
    assert res["rows_new"] == 11
    assert any("DRILL levels" in w for w in res["warnings"])


# --- preview ---------------------------------------------------------------------------------

def test_preview_fixture(settings, sample_csv):
    pv = preview_file(sample_csv, market_slug=MARKET)
    for k in ("filename", "sheet", "headers", "column_map", "unmapped_columns", "rows", "sample"):
        assert k in pv
    assert pv["rows"] == 12 and pv["sheet"] is None and len(pv["headers"]) == 70
    assert pv["provider"] == "crexi" and pv["missing_fields"] == []
    assert len(pv["sample"]) == 5
    for item in pv["sample"]:
        assert not OWNER_FIELDS & set(item)
        assert "raw" not in item
    assert pv["sample"][0]["units"] == 28 and pv["sample"][0]["county"] == "CLARK"
    assert pv["sample"][3]["flags"] == ["no_owner_name", "units_from_addresses"]


def test_preview_sparse_file(settings, tmp_path):
    p = write_csv(tmp_path / "sparse.csv", ["Address", "Owner Name", "Weird"],
                  [{"Address": "1 A St", "Owner Name": "SECRET OWNER", "Weird": "x"}])
    pv = preview_file(p, overrides={"units": "Nope"})
    missing = {m["field"] for m in pv["missing_fields"]}
    assert {"apn", "units", "lat", "year_built"} <= missing and "address" not in missing
    assert pv["unmapped_columns"] == ["Weird"]
    assert "SECRET OWNER" not in json.dumps(pv["sample"])
    assert any("Nope" in w for w in pv["warnings"])


# --- review fixes ------------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["APN", "County"])
def test_one_building_with_and_without_its_apn_in_one_file(conn, sample_csv, tmp_path, blank):
    headers, data = fixture_rows(sample_csv)
    import_file(conn, MARKET, write_csv(tmp_path / "first.csv", headers, [data[0]]))
    partial = dict(data[0])
    partial[blank] = ""
    partial["Property Link"] = "https://www.crexi.com/property-records/sample-0-other"
    res = import_file(conn, MARKET, write_csv(tmp_path / "both.csv", headers, [partial, data[0]]))
    assert res["rows_new"] == 0 and res["rows_updated"] == 1
    assert scalar(conn, "SELECT count(*) FROM properties") == 1
    assert prop(conn, address="1200 SAMPLE RIDGE DR")["apn"] == "340-01-00-001-000"


def _land_csv(tmp_path: Path, sample_csv: Path, *, acres: bool = True) -> Path:
    headers, data = fixture_rows(sample_csv)
    out = []
    for r in data:
        r = dict(r)
        r.update({"Property Type": "Land", "Number of Units": "", "Year Built": "", "Building SqFt": ""})
        out.append(r)
    keep = [h for h in headers if acres or h not in ("Lot Size Acres", "Lot Size SqFt")]
    return write_csv(tmp_path / "land.csv", keep, out)


def test_land_import_has_no_apartment_flags(conn, sample_csv, tmp_path):
    res = import_file(conn, "land-springfield-oh", _land_csv(tmp_path, sample_csv))
    flags = [f for r in rows(conn, "SELECT flags_json FROM properties") for f in json.loads(r["flags_json"])]
    assert "units_from_addresses" not in flags and "year_unknown" not in flags
    assert not any("number of addresses" in w for w in res["warnings"])
    assert all(r["units"] is None for r in rows(conn, "SELECT units FROM properties"))


def test_preview_checks_the_asset_classes_own_size_measure(settings, sample_csv, tmp_path):
    pv = preview_file(_land_csv(tmp_path, sample_csv, acres=False), market_slug="land-springfield-oh")
    missing = {m["label"] for m in pv["missing_fields"]}
    assert "Lot acres" in missing and "Number of units" not in missing and "Year built" not in missing
    assert pv["size_metric"] == "lot_acres" and "year_built" not in pv["key_fields"]
    apt = preview_file(sample_csv, market_slug=MARKET)
    assert apt["size_metric"] == "units" and "year_built" in apt["key_fields"]


def test_importing_into_the_wrong_market_warns_first(conn, settings, sample_csv, tmp_path):
    pv = preview_file(sample_csv, market_slug=STORAGE)
    mm = pv["type_mismatch"]
    assert mm and mm["best_market"] == MARKET and mm["label"] == "Self-storage"
    assert pv["warnings"][0] == mm["message"] and "Switch to the Apartments market" in mm["message"]
    assert preview_file(sample_csv, market_slug=MARKET)["type_mismatch"] is None
    headers, data = fixture_rows(sample_csv)
    untyped = write_csv(tmp_path / "untyped.csv", [h for h in headers if h != "Property Type"], data)
    assert preview_file(untyped, market_slug=STORAGE)["type_mismatch"] is None
    res = import_file(conn, STORAGE, sample_csv)
    assert any("They look like Apartments" in w for w in res["warnings"])


# --- optional: the real Crexi export (counts only, never printed) ----------------------------

def test_real_crexi_export(conn, real_crexi):
    pv = preview_file(real_crexi, market_slug=MARKET)
    assert pv["unmapped_columns"] == []
    assert not any(OWNER_FIELDS & set(s) for s in pv["sample"])
    res = import_file(conn, MARKET, real_crexi)
    assert res["rows_total"] == res["rows_new"] + res["rows_updated"] + res["rows_skipped"]
    assert res["rows_updated"] == 0 and res["rows_new"] > 0
    assert "Avg SqFt per Unit (calc)" in res["skipped_columns"]
    n = scalar(conn, "SELECT count(*) FROM properties")
    assert n == res["rows_new"]
    assert scalar(conn, "SELECT count(*) FROM properties WHERE raw_json LIKE '%=IF(%'") == 0
    assert scalar(conn, "SELECT count(*) FROM drill_state") == n
    again = import_file(conn, MARKET, real_crexi)
    assert again["rows_new"] == 0 and again["rows_updated"] == n
