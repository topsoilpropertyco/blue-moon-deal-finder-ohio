"""Column mapping: match the headers in a property list to our field names.

Crexi exports have 70 fixed headers; other list providers (PropStream, Reonomy, CoStar, county
auditor exports) use their own names for the same things. FIELD_ALIASES lists the known names for
each field. The first alias of each field is the Crexi header when Crexi has one.

Matching order:
1. Overrides (config.yaml `crexi.column_overrides`, or passed in) win.
2. Exact alias match, ignoring case, spacing and punctuation ("Bldg Sq Ft" == "bldg sq-ft").
3. Fuzzy match (rapidfuzz) for near misses like "Yr. Built", never across different numbers
   ("Owner 2" never matches "Owner 1").

Field groups:
- PROPERTY_FIELDS are columns of the `properties` table.
- HELPER_FIELDS are read to work something out (units from addresses, lot acres from sq ft,
  preforeclosure from the PFC columns, owner name from first + last name).
- INFO_FIELDS are known columns we only keep in raw_json (every original column is kept anyway).
"""
from __future__ import annotations

import re
from typing import Mapping

from rapidfuzz import fuzz

DEFAULT_FUZZY_MIN_SCORE = 88.0  # config.yaml crexi.fuzzy_min_score overrides this

FIELD_ALIASES: dict[str, list[str]] = {
    # --- identity and links --------------------------------------------------------------
    "source_id": ["Crexi ID", "Property ID", "PropertyID", "Record ID", "Source ID", "Reonomy ID",
                  "Listing ID", "ID"],
    "crexi_url": ["Property Link", "Crexi URL", "Crexi Link", "Property URL", "Listing URL", "URL", "Link"],
    "name": ["Property Name", "Name", "Building Name", "Complex Name", "Project Name"],
    "property_type": ["Property Type", "Asset Type", "Property Use", "Land Use", "Land Use Description",
                      "Use Code Description", "Property Class", "Class Description", "Building Type",
                      "Property Subtype", "Use Description", "Type"],
    "apn": ["APN", "Parcel Number", "Parcel ID", "Parcel", "Parcel No", "PIN", "Assessor Parcel Number",
            "Tax ID", "Tax Parcel ID", "Parcel Number 1(Min)", "APN (Parcel ID)", "PARID", "Parcel Num",
            "ParcelID", "Tax Parcel Number"],
    # --- location ----------------------------------------------------------------------
    "address": ["Address", "Site Address", "Property Address", "Street Address", "Situs Address",
                "SitusAddress", "Location Address", "Location", "Address Line 1", "Address 1", "Street",
                "Property Street Address", "Site Addr", "Prop Address", "Full Address"],
    "unit": ["Unit", "Unit Number", "Suite", "Address Unit", "Site Unit", "Apt", "Address Line 2"],
    "city": ["City", "Site City", "Property City", "Situs City", "SitusCity", "Location City",
             "Municipality", "Prop City"],
    "state": ["State", "Site State", "Property State", "Situs State", "SitusState", "ST", "Prop State"],
    "zip": ["Zip Code", "Zip", "Postal Code", "Site Zip", "Property Zip", "Situs Zip", "SitusZip",
            "Zip5", "Zipcode", "Prop Zip", "Site Zip Code", "Property Zip Code"],
    "county": ["County", "County Name", "Site County", "Property County", "Situs County"],
    "lat": ["Latitude", "Lat", "Property Latitude", "Site Latitude"],
    "lng": ["Longitude", "Long", "Lng", "Lon", "Property Longitude", "Site Longitude"],
    # --- size and age ------------------------------------------------------------------
    "units": ["Number of Units", "Units", "Unit Count", "No of Units", "Total Units", "Num Units",
              "Residential Units", "Living Units", "Apartment Units", "Dwelling Units"],
    "number_of_addresses": ["Number of Addresses", "Address Count"],
    "building_sqft": ["Building SqFt", "Building Sq Ft", "Bldg Sq Ft", "Bldg SqFt", "Building Square Feet",
                      "Building Size", "Building Area", "Gross Building Area", "GBA", "RBA",
                      "Rentable Building Area", "Living Area", "Living Sq Ft", "Total Building Area",
                      "Square Feet", "Sq Ft", "SqFt", "Improvement Sq Ft", "Building Size (SF)",
                      "Total Sq Ft", "Gross Sq Ft", "Finished Sq Ft", "Building Area (SF)", "Building Sqft"],
    "lot_acres": ["Lot Size Acres", "Lot Acres", "Acres", "Acreage", "Land Area (AC)", "Lot Size (Acres)",
                  "Lot Area (Acres)", "Land Acres", "Total Acres", "Calc Acres", "Deeded Acres"],
    "lot_sqft": ["Lot Size SqFt", "Lot Size Sq Ft", "Lot Sq Ft", "Lot SqFt", "Land Area (SF)", "Land Sq Ft",
                 "Lot Size (SF)", "Lot Area (SF)", "Lot Size Sqft", "Lot Square Feet"],
    "building_count": ["Building Count", "Number of Buildings", "Buildings", "No of Buildings", "Bldg Count",
                       "Total Buildings"],
    "stories": ["Number of Stories", "Stories", "Floors", "Number of Floors", "Story Count", "No of Stories",
                "Levels"],
    "year_built": ["Year Built", "Yr Built", "Year Constructed", "Year Blt", "YearBuilt", "Yr Blt",
                   "Built Year", "Year Built (Actual)"],
    # --- sale and debt -----------------------------------------------------------------
    "sale_date": ["Sale Date", "Last Sale Date", "Last Sold Date", "Sold Date", "Transfer Date", "Deed Date",
                  "Last Transfer Date", "Date Sold", "Sale Recording Date", "Last Sale Recording Date"],
    "sale_price": ["Sold Price", "Sale Price", "Last Sale Price", "Last Sale Amount", "Sale Amount",
                   "Last Sold Price", "Sales Price", "Transfer Price"],
    "lender": ["Lender", "Lender Name", "Mortgage Lender", "Loan Lender", "Mortgage Lender Name",
               "First Mortgage Lender", "Lender 1"],
    "loan_amount": ["Loan Amount", "Mortgage Amount", "First Mortgage Amount", "Loan Amt",
                    "Original Loan Amount", "Mortgage 1 Amount"],
    "loan_type": ["Loan Type", "Mortgage Type", "First Mortgage Type", "Loan Type Description"],
    "interest_rate": ["Interest Rate", "Mortgage Rate", "Loan Rate", "Mortgage Interest Rate"],
    "loan_maturity": ["Financing Maturity Date", "Loan Maturity Date", "Maturity Date",
                      "Mortgage Maturity Date", "Loan Maturity"],
    # --- value and tax -----------------------------------------------------------------
    "improvement_value": ["Improvement Value", "Building Value", "Improvements Value",
                          "Assessed Improvement Value", "Market Improvement Value", "Bldg Value",
                          "Total Improvement Value", "Appraised Improvement Value"],
    "land_value": ["Land Value", "Assessed Land Value", "Market Land Value", "Appraised Land Value",
                   "Total Land Value"],
    "total_value": ["Total Parcel Value", "Total Value", "Assessed Value", "Total Assessed Value",
                    "Market Value", "Total Market Value", "Appraised Value", "Total Appraised Value",
                    "Assessed Total Value"],
    "value_type": ["Parcel Value Type", "Value Type", "Valuation Type"],
    "tax_year": ["Tax Year", "Assessment Year", "Tax Yr"],
    "annual_tax": ["Annual Tax Bill", "Annual Taxes", "Tax Amount", "Taxes", "Property Tax", "Annual Tax",
                   "Tax Bill", "Total Tax", "Total Taxes", "Real Estate Taxes", "Tax Amount Billed"],
    # --- distress and status -----------------------------------------------------------
    "usps_vacant": ["USPS Vacancy", "Vacant", "USPS Vacant", "Vacancy", "Vacant Flag", "Is Vacant"],
    "opportunity_zone": ["Opportunity Zone", "Opportunity Zone Flag", "In Opportunity Zone", "OZ"],
    "reo": ["REO Sale Flag", "REO", "REO Flag", "Bank Owned", "Is REO"],
    "pfc_recording_date": ["PFC Recording Date", "Foreclosure Recording Date",
                           "Pre-Foreclosure Recording Date", "Foreclosure Date", "Lis Pendens Date"],
    "pfc_indicator": ["PFC Indicator", "Pre-Foreclosure", "Pre-Foreclosure Status", "Foreclosure Status",
                      "Foreclosure", "In Foreclosure", "Foreclosure Flag", "Preforeclosure"],
    "pfc_document_type": ["PFC Document Type", "Foreclosure Document Type", "Foreclosure Doc Type",
                          "Default Document Type"],
    "shared_apn_flag": ["Shared APN Flag", "Multi APN", "Multiple Parcels"],
    # --- owner -------------------------------------------------------------------------
    "owner_name": ["Owner Name", "Owner", "Owner 1", "Owner Name 1", "Owner 1 Full Name", "Owner1",
                   "Owner 1 Name", "Reported Owner", "Recorded Owner Name", "True Owner Name",
                   "Owner Full Name", "Deeded Owner", "Owner(s)", "Taxpayer Name", "Taxpayer", "Grantee",
                   "Current Owner", "Primary Owner", "OwnerName"],
    "owner_first_name": ["Owner 1 First Name", "Owner First Name", "First Name"],
    "owner_last_name": ["Owner 1 Last Name", "Owner Last Name", "Last Name"],
    "mailing_address": ["Mailing Address", "Mail Address", "Owner Mailing Address", "Owner Address",
                        "Mailing Street", "Mailing Address 1", "Mail Address 1", "Mailing Address Line 1",
                        "Reported Owner Address", "Recorded Owner Address", "Taxpayer Address",
                        "Mail Street", "Mailing Addr", "MailAddress", "Owner Mail Address"],
    "mailing_unit": ["Mailing Address Unit", "Mailing Unit", "Mail Unit", "Mailing Address 2",
                     "Mailing Address Line 2", "Mail Address 2"],
    "mailing_city": ["Mailing Address City", "Mailing City", "Mail City", "Owner City", "Owner Mailing City",
                     "Recorded Owner City", "Taxpayer City", "MailCity"],
    "mailing_state": ["Mailing Address State", "Mailing State", "Mail State", "Owner State",
                      "Owner Mailing State", "Recorded Owner State", "Taxpayer State", "MailState"],
    "mailing_zip": ["Mailing Address Zip Code", "Mailing Zip", "Mailing Zip Code", "Mail Zip", "Mail Zip Code",
                    "Owner Zip", "Owner Mailing Zip", "Recorded Owner Zip", "Taxpayer Zip", "MailZip",
                    "Mailing Postal Code"],
    "mailing_care_of": ["Mailing Address Care Of", "Care Of", "C/O", "In Care Of", "Mailing Care Of",
                        "Attention"],
    # --- known columns we keep only in raw_json ----------------------------------------
    "source_file": ["Source File"],
    "lease_signed": ["Lease Signed"],
    "lease_rate": ["Lease Rate"],
    "zoning": ["Zoning Code", "Zoning"],
    "water": ["Water Code", "Water"],
    "sewer": ["Sewer Code", "Sewer"],
    "asking_cap_rate": ["Asking Cap Rate"],
    "closing_noi": ["Closing NOI", "NOI"],
    "closing_cap_rate": ["Closing Cap Rate", "Cap Rate"],
    "sold_price_per_sqft": ["Sold Price/ SqFt", "Price/SF", "Sale Price per SF", "Price Per Sq Ft"],
    "sold_price_per_acre": ["Sold Price/ Acre", "Price/Acre", "Price Per Acre"],
    "occupancy": ["Occupancy", "Occupancy %", "Percent Leased", "% Leased"],
    "lease_type": ["Lease Type"],
    "lease_commencement": ["Lease Commencement"],
    "lease_term": ["Lease Term"],
    "lease_term_remaining": ["Lease Term Remaining"],
    "lease_expiration": ["Lease Expiration Date", "Lease Expiration"],
    "rent_bumps": ["Rent Bumps"],
    "lease_options": ["Lease Options"],
    "tenants": ["Tenant(s)", "Tenants", "Tenant"],
    "days_on_market": ["Days on Market", "DOM"],
    "usps_vacancy_date": ["USPS Vacancy Date", "Vacancy Date", "Vacant Date"],
    "transaction_event_type": ["Transaction Event Type", "Transaction Type", "Sale Type", "Deed Type"],
    "owner_occupied": ["Owner Occupied"],
    "est_value": ["Est. Value", "Estimated Value", "AVM"],
    "est_equity": ["Est. Equity", "Estimated Equity", "Equity"],
    "open_loans": ["Total Open Loans", "Open Loans"],
    "bedrooms": ["Bedrooms", "Beds"],
    "bathrooms": ["Bathrooms", "Baths", "Total Bathrooms"],
}

PROPERTY_FIELDS: frozenset[str] = frozenset({
    "source_id", "crexi_url", "name", "property_type", "apn", "address", "unit", "city", "state", "zip",
    "county", "lat", "lng", "units", "building_sqft", "lot_acres", "building_count", "stories", "year_built",
    "sale_date", "sale_price", "lender", "loan_amount", "loan_type", "interest_rate", "loan_maturity",
    "improvement_value", "land_value", "total_value", "value_type", "tax_year", "annual_tax", "usps_vacant",
    "opportunity_zone", "reo", "owner_name", "mailing_address", "mailing_unit", "mailing_city",
    "mailing_state", "mailing_zip", "mailing_care_of",
})
HELPER_FIELDS: frozenset[str] = frozenset({
    "number_of_addresses", "lot_sqft", "pfc_recording_date", "pfc_indicator", "pfc_document_type",
    "shared_apn_flag", "owner_first_name", "owner_last_name",
})
INFO_FIELDS: frozenset[str] = frozenset(FIELD_ALIASES) - PROPERTY_FIELDS - HELPER_FIELDS

# Owner and mailing fields are private. The import preview never shows them.
OWNER_FIELDS: frozenset[str] = frozenset({
    "owner_name", "owner_first_name", "owner_last_name", "mailing_address", "mailing_unit", "mailing_city",
    "mailing_state", "mailing_zip", "mailing_care_of",
})

# Fields the import screen highlights when they are missing from a file.
KEY_FIELDS: tuple[str, ...] = ("address", "city", "state", "zip", "county", "apn", "lat", "lng", "units",
                               "building_sqft", "year_built", "owner_name", "mailing_address")

# Plain-English names for the import screen.
FIELD_LABELS: dict[str, str] = {
    "source_id": "Crexi / source ID", "crexi_url": "Crexi link", "name": "Property name",
    "property_type": "Property type", "apn": "Parcel number (APN)", "address": "Street address",
    "unit": "Unit", "city": "City", "state": "State", "zip": "ZIP code", "county": "County",
    "lat": "Latitude", "lng": "Longitude", "units": "Number of units",
    "number_of_addresses": "Number of addresses", "building_sqft": "Building sq ft",
    "lot_acres": "Lot acres", "lot_sqft": "Lot sq ft", "building_count": "Number of buildings",
    "stories": "Stories", "year_built": "Year built", "sale_date": "Last sale date",
    "sale_price": "Last sale price", "lender": "Lender", "loan_amount": "Loan amount",
    "loan_type": "Loan type", "interest_rate": "Interest rate", "loan_maturity": "Loan maturity date",
    "improvement_value": "Building value", "land_value": "Land value", "total_value": "Total value",
    "value_type": "Value type", "tax_year": "Tax year", "annual_tax": "Annual tax bill",
    "usps_vacant": "USPS vacant", "opportunity_zone": "Opportunity zone", "reo": "Bank-owned (REO)",
    "pfc_recording_date": "Pre-foreclosure date", "pfc_indicator": "Pre-foreclosure status",
    "pfc_document_type": "Pre-foreclosure document", "shared_apn_flag": "Shared parcel flag",
    "owner_name": "Owner name", "owner_first_name": "Owner first name", "owner_last_name": "Owner last name",
    "mailing_address": "Owner mailing address", "mailing_unit": "Mailing unit", "mailing_city": "Mailing city",
    "mailing_state": "Mailing state", "mailing_zip": "Mailing ZIP", "mailing_care_of": "Mailing care of",
}


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field) or field.replace("_", " ").capitalize()


def norm_header(header: object) -> str:
    """Lowercase, '#' -> 'number', '%' -> 'pct', '&' -> 'and', everything else non-alphanumeric -> space."""
    s = str(header or "").strip().lower()
    s = s.replace("#", " number ").replace("%", " pct ").replace("&", " and ")
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _digits(s: str) -> list[str]:
    return re.findall(r"\d+", s)


def _alias_index() -> dict[str, str]:
    """normalized alias -> field."""
    index: dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            index.setdefault(norm_header(alias), field)
    return index


_ALIAS_INDEX = _alias_index()
# field -> [(normalized alias, digits in it)], in alias order
_NORMED_ALIASES: dict[str, list[tuple[str, list[str]]]] = {
    field: [(norm_header(a), _digits(norm_header(a))) for a in aliases] for field, aliases in FIELD_ALIASES.items()
}


def known_header_score(headers: list[object]) -> int:
    """How many of these cells are known column names. Used to find the header row and the right sheet."""
    return sum(1 for h in headers if h is not None and norm_header(h) in _ALIAS_INDEX)


def _find_header(headers: list[str], wanted: str) -> str | None:
    """Find a real header matching `wanted`, ignoring case and punctuation."""
    if wanted in headers:
        return wanted
    target = norm_header(wanted)
    for h in headers:
        if norm_header(h) == target:
            return h
    return None


def check_overrides(headers: list[str], overrides: Mapping[str, str | None] | None) -> list[str]:
    """Plain-English warnings for overrides that can't be applied."""
    warnings: list[str] = []
    for field, header in (overrides or {}).items():
        if field not in FIELD_ALIASES:
            warnings.append(f"Column override '{field}' is not a field I know, so I ignored it.")
        elif header and _find_header(headers, str(header)) is None:
            warnings.append(f"Column override for '{field_label(field)}' points to '{header}', "
                            f"but this file has no column with that name.")
    return warnings


def map_columns(headers: list[str], overrides: Mapping[str, str | None] | None = None,
                min_score: float = DEFAULT_FUZZY_MIN_SCORE) -> tuple[dict[str, str], list[str]]:
    """Match file headers to our fields.

    Returns (column_map: field -> source header, unmapped headers in file order).
    An override value of None or "" removes a field from the map.
    """
    headers = [h for h in headers if h is not None and str(h).strip()]
    column_map: dict[str, str] = {}
    used: set[str] = set()
    blocked: set[str] = set()

    for field, header in (overrides or {}).items():
        if field not in FIELD_ALIASES:
            continue
        if not header:
            blocked.add(field)
            continue
        real = _find_header(headers, str(header))
        if real is not None and real not in used:
            column_map[field] = real
            used.add(real)

    normed = {h: norm_header(h) for h in headers}
    for field, aliases in _NORMED_ALIASES.items():
        if field in column_map or field in blocked:
            continue
        for target, _ in aliases:
            match = next((h for h in headers if h not in used and normed[h] == target), None)
            if match is not None:
                column_map[field] = match
                used.add(match)
                break

    _fuzzy_pass(headers, normed, column_map, used, blocked, min_score)
    unmapped = [h for h in headers if h not in used]
    return column_map, unmapped


def _fuzzy_pass(headers: list[str], normed: dict[str, str], column_map: dict[str, str], used: set[str],
                blocked: set[str], min_score: float) -> None:
    """Assign remaining headers to remaining fields by best fuzzy score, highest first."""
    candidates: list[tuple[float, str, str]] = []
    for h in headers:
        nh = normed[h]
        if h in used or len(nh) < 4:
            continue
        nh_digits = _digits(nh)
        for field, aliases in _NORMED_ALIASES.items():
            if field in column_map or field in blocked:
                continue
            best = 0.0
            for na, na_digits in aliases:
                if len(na) < 4 or na_digits != nh_digits:
                    continue
                best = max(best, fuzz.token_sort_ratio(nh, na))
            if best >= min_score:
                candidates.append((best, h, field))
    for _score, h, field in sorted(candidates, key=lambda c: -c[0]):
        if h in used or field in column_map:
            continue
        column_map[field] = h
        used.add(h)


# --- provider detection ---------------------------------------------------------------------

_PROVIDER_SIGNATURES: dict[str, tuple[str, ...]] = {
    "crexi": ("Property Link", "Shared APN Flag", "PFC Indicator", "REO Sale Flag", "Number of Addresses",
              "Parcel Value Type", "Mailing Address Care Of", "Financing Maturity Date"),
    "propstream": ("Owner 1 First Name", "Owner 1 Last Name", "Est. Equity", "Est. Value",
                   "Total Open Loans", "MLS Status", "Owner Occupied"),
    "reonomy": ("Reonomy ID", "Reported Owner", "Reported Owner Address"),
    "costar": ("PropertyID", "RBA", "True Owner Name", "Recorded Owner Name", "Parcel Number 1(Min)"),
}
PROVIDER_LABELS: dict[str, str] = {
    "crexi": "Crexi export", "propstream": "PropStream export", "reonomy": "Reonomy export",
    "costar": "CoStar export", "bluemoon": "Blue Moon export (your edits)", "other": "Property list",
}

# Our own export files (bluemoon/exports.py) also carry Crexi's column names, so they are checked first:
# brought back in, they go to the safe edits importer (ingest/roundtrip.py), never through the Crexi merge.
# 'Row as of' is ours alone (added for the round trip); older exports are known by >= 2 of the others.
BLUE_MOON_MARKERS: tuple[str, ...] = ("Row as of",)
BLUE_MOON_SIGNATURE: tuple[str, ...] = ("Blue Moon score %", "Why set aside", "Score signals known",
                                        "Next DRILL step", "Buy box decision", "Set-aside groups")


# Our other exports are not property lists and never come back in (crexi._provider refuses them). Each is
# known by its Property ID column plus headers only that file has: {name: (headers, how many must be there)}.
BLUE_MOON_OTHER_EXPORTS: dict[str, tuple[tuple[str, ...], int]] = {
    "DRILL worklist": (("Next step key",), 1),
    "mailing list": (("Why on this list", "Full mailing address", "Contact ID"), 2),
    "CRM handoff": (("confirmed_person", "handed_off_at", "new_in_this_export", "other_contacts_untested"), 2),
}


def looks_like_blue_moon(headers: list[str]) -> bool:
    """Our own property list's column names ('Row as of', or two of our distinctive ones), with or without
    its Property ID column."""
    have = {norm_header(h) for h in headers if h}
    if any(norm_header(m) in have for m in BLUE_MOON_MARKERS):
        return True
    return sum(1 for s in BLUE_MOON_SIGNATURE if norm_header(s) in have) >= 2


def is_blue_moon_export(headers: list[str]) -> bool:
    """True for a file this app exported (ranked list, excluded file, review file), even after Excel."""
    return "property id" in {norm_header(h) for h in headers if h} and looks_like_blue_moon(headers)


def other_blue_moon_export(headers: list[str]) -> str | None:
    """'DRILL worklist', 'mailing list' or 'CRM handoff' for those exports of ours, else None."""
    have = {norm_header(h) for h in headers if h}
    if "property id" not in have:
        return None
    for name, (marks, need) in BLUE_MOON_OTHER_EXPORTS.items():
        if sum(1 for m in marks if norm_header(m) in have) >= need:
            return name
    return None


def detect_provider(headers: list[str]) -> str:
    """Best guess of which list provider made this file: bluemoon (our own export) | crexi | propstream |
    reonomy | costar | other."""
    if is_blue_moon_export(headers):
        return "bluemoon"
    have = {norm_header(h) for h in headers if h}
    best, best_hits = "other", 0
    for provider, sig in _PROVIDER_SIGNATURES.items():
        hits = sum(1 for s in sig if norm_header(s) in have)
        need = 3 if provider in ("crexi", "propstream") else 2
        if hits >= need and hits > best_hits:
            best, best_hits = provider, hits
    return best
