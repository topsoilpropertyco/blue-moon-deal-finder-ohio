"""Owner signals: who owns each property, how long they've had it, debt, and where they live.

For every property in a market this works out (table `owners`):

- **owner_type**: individual | trust | estate | small_llc | corporation | portfolio | institutional |
  government | nonprofit | unknown. The name is read with keyword rules (`classify_owner`). Then the
  owner's group is counted: a group with `owners.portfolio_min_properties` (5) or more separate
  properties becomes `portfolio`; with `owners.institutional_min_properties` (40) or more it becomes
  `institutional`. Types in `owners.portfolio_exempt_types` (institutional, government) keep their type.
- **portfolio_key / portfolio_count**: properties are grouped across ALL markets when they share a
  normalized owner name or a normalized mailing address. `portfolio_key` is the same for every property
  in a group (the DRILL uses it to carry a contact from one property to its siblings); `portfolio_count`
  is the number of separate properties in the group. Parcels closer than `owners.same_site_meters`
  (200 m) to each other count once, because one apartment complex is often split into several parcels.
- **absentee**: mailing state differs from the property's state -> outofstate; same state but a
  different city -> instate; same city -> local; not enough data -> unknown.
- **years_owned**: years since the last sale date.
- **debt_status**: no loan on record -> free_and_clear when owned `owners.free_and_clear_min_years`
  (10) or more years, else unknown. Loan maturing (or matured) within `owners.maturing_soon_months` (18)
  -> maturing_soon. A loan that matured longer ago with no newer loan on record -> free_and_clear
  (probably paid off). Any other loan -> active.

`person_from_owner(name)` turns an owner-of-record name into a best-guess person to look for
("DOE JOHN & JANE" -> "John & Jane Doe"); entities return None.

Every threshold above is read from `owners:` in config.yaml; the defaults below apply when a key is
missing. Keyword lists can be extended (never shrunk) with `owners.keywords`, and a single owner can
be forced to a type with `owners.overrides` (exact owner name -> type).
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..config import Settings, get_settings
from ..db import market_by_slug, now_iso, rows, upsert
from ..util import clean_str, haversine_miles, normalize_address, parse_date

log = logging.getLogger(__name__)

OWNER_TYPES: tuple[str, ...] = ("individual", "trust", "estate", "small_llc", "corporation", "portfolio",
                                "institutional", "government", "nonprofit", "unknown")
OWNER_TYPE_LABELS: dict[str, str] = {
    "individual": "Individual", "trust": "Trust", "estate": "Estate or heirs", "small_llc": "Small LLC",
    "corporation": "Corporation", "portfolio": "Portfolio owner", "institutional": "Institution",
    "government": "Government", "nonprofit": "Nonprofit", "unknown": "Unknown",
}
ABSENTEE_VALUES: tuple[str, ...] = ("local", "instate", "outofstate", "unknown")
DEBT_VALUES: tuple[str, ...] = ("free_and_clear", "maturing_soon", "active", "unknown")

# Defaults for `owners:` in config.yaml (used when a key is missing there).
DEFAULT_PORTFOLIO_MIN = 5            # owners.portfolio_min_properties
DEFAULT_INSTITUTIONAL_MIN = 40       # owners.institutional_min_properties
DEFAULT_SAME_SITE_METERS = 200.0     # owners.same_site_meters
DEFAULT_MAILING_MAX_NAMES = 50       # owners.mailing_group_max_names
DEFAULT_FREE_CLEAR_YEARS = 10.0      # owners.free_and_clear_min_years
DEFAULT_MATURING_MONTHS = 18         # owners.maturing_soon_months
DEFAULT_PORTFOLIO_EXEMPT = ("institutional", "government")  # owners.portfolio_exempt_types
DAYS_PER_YEAR = 365.25
DAYS_PER_MONTH = 30.4375
METERS_PER_MILE = 1609.344


# --- settings ------------------------------------------------------------------------------------

class OwnerKeywords(BaseModel):
    """Extra words or phrases (plain text, matched as whole words) added to the built-in rules."""
    government: list[str] = Field(default_factory=list)
    nonprofit: list[str] = Field(default_factory=list)
    institutional: list[str] = Field(default_factory=list)
    trust: list[str] = Field(default_factory=list)
    estate: list[str] = Field(default_factory=list)
    small_llc: list[str] = Field(default_factory=list)
    corporation: list[str] = Field(default_factory=list)


class OwnerConfig(BaseModel):
    """`owners:` from config.yaml."""
    portfolio_min_properties: int = DEFAULT_PORTFOLIO_MIN
    institutional_min_properties: int = DEFAULT_INSTITUTIONAL_MIN
    same_site_meters: float = DEFAULT_SAME_SITE_METERS
    mailing_group_max_names: int = DEFAULT_MAILING_MAX_NAMES
    free_and_clear_min_years: float = DEFAULT_FREE_CLEAR_YEARS
    maturing_soon_months: int = DEFAULT_MATURING_MONTHS
    portfolio_exempt_types: list[str] = Field(default_factory=lambda: list(DEFAULT_PORTFOLIO_EXEMPT))
    institutional_names: list[str] = Field(default_factory=list)
    keywords: OwnerKeywords = Field(default_factory=OwnerKeywords)
    overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _known_types(cls, v: dict[str, str]) -> dict[str, str]:
        bad = {k: t for k, t in v.items() if t not in OWNER_TYPES}
        if bad:
            raise ValueError(f"owners.overrides has unknown owner types: {bad}. Use one of {', '.join(OWNER_TYPES)}.")
        return v


def owner_config(settings: Settings | None = None) -> OwnerConfig:
    s = settings or get_settings()
    return OwnerConfig(**(s.section("owners") or {}))


# --- name cleaning -------------------------------------------------------------------------------

_CARE_OF = re.compile(r"(?:^|\s+)(?:C/O|C\\O|ATTN:?|ATTENTION:?|%)(?:\s+.*)?$")
_SPLIT_SUFFIXES = (
    (re.compile(r"\bL\s?L\s?L\s?P\b"), "LLLP"), (re.compile(r"\bL\s?L\s?C\b"), "LLC"),
    (re.compile(r"\bL\s?L\s?P\b"), "LLP"), (re.compile(r"\bL\s?T\s?D\b"), "LTD"),
    (re.compile(r"\bP\s?L\s?L\s?C\b"), "PLLC"), (re.compile(r"\bI\s?N\s?C\b"), "INC"),
    (re.compile(r"\bL P\b"), "LP"),
)


def clean_owner_name(name: Any, keep_apostrophes: bool = False) -> str | None:
    """Uppercase, cut off "C/O ..." and "ATTN ...", drop punctuation, rejoin split suffixes (L.L.C. -> LLC)."""
    s = clean_str(name)
    if not s:
        return None
    s = s.upper().replace("’", "'")
    s = _CARE_OF.sub("", s)
    s = re.sub(r"\bH\s*/\s*W\b", " ", s)                      # husband/wife
    s = re.sub(r"\bU\s*/\s*(?:T\s*/\s*)?(?:D\s*/\s*)?[AT]\b", " ", s)  # U/A, U/T/A, U/D/T (trust agreement)
    s = s.replace("&", " & ")
    s = re.sub(r"[.,\"`;:()\[\]{}]", " ", s)
    s = re.sub(r"[/\\]", " ", s)
    if not keep_apostrophes:
        s = s.replace("'", "")
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    for pattern, repl in _SPLIT_SUFFIXES:
        s = pattern.sub(repl, s)
    s = re.sub(r"\bCO-?(TRUSTEES?|TTEES?|TRS?)\b", r"\1", s)    # CO-TRUSTEE is a trustee, not "& Co"
    return s or None


def _words(phrases: Iterable[str]) -> re.Pattern[str]:
    """Whole-word, case-insensitive matcher for plain phrases (from YAML)."""
    parts = [re.escape(clean_owner_name(p) or "") for p in phrases if clean_owner_name(p)]
    if not parts:
        return re.compile(r"(?!x)x")
    return re.compile(r"\b(?:" + "|".join(sorted(parts, key=len, reverse=True)) + r")\b")


def _any(patterns: Iterable[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


# --- classification rules ------------------------------------------------------------------------

PLACEHOLDER_NAMES = frozenset({
    "OWNER", "OWNERS", "CURRENT OWNER", "OWNER RECORD", "OWNER OF RECORD", "RECORD OWNER", "UNKNOWN",
    "UNKNOWN OWNER", "OWNER UNKNOWN", "NOT AVAILABLE", "NOT AVAILABLE FROM THE COUNTY", "NONE", "NA", "N A",
    "TBD", "TAXPAYER", "CURRENT RESIDENT", "OCCUPANT", "RESIDENT", "NO NAME", "NAME NOT AVAILABLE",
})

_STREET_WORDS = r"(?:DR|DRIVE|ST|STREET|RD|ROAD|AVE|AVENUE|LN|LANE|CT|COURT|BLVD|WAY|PL|PLACE|PIKE|HWY|SQ|SQUARE|HILL)"

# Government no matter what else is in the name.
GOV_ALWAYS = (
    r"\bHOUSING AUTHORITY\b", r"\bMETRO(?:POLITAN)? HOUSING\b", r"\bLAND REUTILIZATION\b", r"\bLAND BANK\b",
    r"\bPORT AUTHORITY\b", r"\bTRANSIT AUTHORITY\b", r"\bCOMMUNITY AUTHORITY\b", r"\bUNITED STATES\b",
    r"\bBOARD OF EDUCATION\b", r"\bBD OF ED(?:UCATION)?\b", r"\bCOUNTY COMMISSIONERS\b",
    r"\bBOARD OF (?:COUNTY )?COMMISSIONERS\b", r"\bSCHOOL DIST(?:RICT)?\b", r"\bDEPARTMENT OF\b", r"\bDEPT OF\b",
    r"\bSECRETARY OF (?:HOUSING|VETERANS)\b", r"\bVETERANS AFFAIRS\b", r"\bPARK DISTRICT\b", r"\bMETRO ?PARKS\b",
    r"\bPUBLIC LIBRARY\b", r"\bDISTRICT LIBRARY\b", r"\bCONSERVANCY DISTRICT\b", r"\bSANITARY DISTRICT\b",
)
# Government unless the name ends in a business suffix (LLC, INC, ...).
GOV_UNLESS_SUFFIX = (
    r"\bCITY OF\b", r"\bTOWN OF\b", r"\bCOUNTY OF\b", r"^STATE OF\b", r"\bSTATE OF (?:OHIO|INDIANA)\b",
    r"\bCOMMONWEALTH OF\b", r"\bBOARD OF (?!REALTORS\b)", r"\bS ?D$", r"\b(?:CSD|LSD|EVSD|JVSD)$",
    r"\bCITY SCHOOLS?\b", r"\bLOCAL SCHOOLS?\b", r"\bMUNICIPAL\b",
)
# Government unless the name also looks like a business (an apartment complex called "Village of ...").
GOV_UNLESS_BUSINESS = (r"(?<!THE )\bVILLAGE OF\b", r"\bTOWNSHIP\b", r"\bTWP\b")
GOV_WEAK = (r"\bCOUNTY\b",)  # only when nothing in the name points to a business or nonprofit

INSTITUTIONAL = (
    r"\bREIT\b", r"\bREAL ESTATE INVESTMENT TRUST\b", r"\bINVESTMENT TRUST\b",
    r"\bFUND\b(?:.*\b(?:[IVX]{1,5}|\d{1,3})\b)", r"\b(?:[IVX]{1,5}|\d{1,3}) FUND\b", r"\bDST\b",
    r"\bNATIONAL ASSOCIATION\b", r"\bFEDERAL (?:NATIONAL|HOME LOAN) MORTGAGE\b", r"\bFANNIE MAE\b",
    r"\bFREDDIE MAC\b", r"\bSAVINGS (?:AND|&) LOAN\b", r"\bINSURANCE CO(?:MPANY)?\b", r"\bLIFE INSURANCE\b",
    r"\bPENSION\b", r"\bRETIREMENT SYSTEM\b",
)
# Banks own property after foreclosure; a bank named as TRUSTEE runs a family trust instead.
BANK = r"\bBANK\b(?!\s+" + _STREET_WORDS + r"\b)"

# Large national owners and operators (public companies). Extend with owners.institutional_names.
NATIONAL_OPERATORS = (
    "MID AMERICA APARTMENT", "MID-AMERICA APARTMENT", "MAA", "CAMDEN PROPERTY TRUST", "AVALONBAY", "AVALON BAY",
    "EQUITY RESIDENTIAL", "ESSEX PORTFOLIO", "UDR INC", "AIMCO", "APARTMENT INCOME REIT", "INDEPENDENCE REALTY",
    "NEXPOINT", "GREYSTAR", "LINCOLN PROPERTY", "BRIDGE INVESTMENT GROUP", "BLACKSTONE", "STARWOOD",
    "INVITATION HOMES", "AMERICAN HOMES 4 RENT", "AMH", "TRICON", "PROGRESS RESIDENTIAL", "FIRSTKEY HOMES",
    "MAIN STREET RENEWAL", "VINEBROOK", "PRETIUM", "CERBERUS", "HOME PARTNERS OF AMERICA",
    "PUBLIC STORAGE", "EXTRA SPACE", "CUBESMART", "LIFE STORAGE", "STORAGEMART", "STORAGE MART", "U-HAUL",
    "U HAUL", "AMERCO", "NATIONAL STORAGE AFFILIATES", "SIMPLY SELF STORAGE",
    "PROLOGIS", "LINK LOGISTICS", "DUKE REALTY", "STAG INDUSTRIAL", "FIRST INDUSTRIAL", "EASTGROUP",
    "REXFORD INDUSTRIAL", "PLYMOUTH INDUSTRIAL", "INDUSTRIAL LOGISTICS PROPERTIES", "MONMOUTH REAL ESTATE",
    "SUN COMMUNITIES", "EQUITY LIFESTYLE", "MHC PROPERTY", "UMH PROPERTIES", "YES COMMUNITIES", "HAVENPARK",
    "INSPIRE COMMUNITIES", "RHP PROPERTIES", "CAL-AM PROPERTIES", "CAL AM PROPERTIES", "THOUSAND TRAILS",
    "INLAND AMERICAN", "INLAND REAL ESTATE", "INLAND WESTERN",
)

NONPROFIT = (
    r"\bCHURCH(?:ES)?\b(?!\s+" + _STREET_WORDS + r"\b)", r"\bMINISTR(?:Y|IES)\b", r"\bFOUNDATION\b",
    r"\bHABITAT\b", r"\bCOMMUNITY DEVELOPMENT\b", r"\bCOMMUNITY ACTION\b", r"\bUNIVERSITY(?: OF\b|$)",
    r"\bCOLLEGE(?: OF\b|$)", r"\bSTATE UNIVERSITY\b", r"\bASSOCIATION\b", r"\bASSN\b", r"\bHOMEOWNERS\b",
    r"\bDIOCESE\b", r"\bARCHDIOCESE\b", r"\bCATHOLIC\b", r"\bBAPTIST\b", r"\bMETHODIST\b", r"\bLUTHERAN\b",
    r"\bPRESBYTERIAN\b", r"\bEPISCOPAL\b", r"\bPENTECOSTAL\b", r"\bAPOSTOLIC\b", r"\bEVANGELICAL\b",
    r"\bCONGREGATION\b", r"\bPARISH\b", r"\bSYNAGOGUE\b", r"\bMOSQUE\b", r"\bISLAMIC CENTER\b", r"\bTABERNACLE\b",
    r"\bSALVATION ARMY\b", r"\bVOLUNTEERS OF AMERICA\b", r"\bYMCA\b", r"\bYWCA\b", r"\bGOODWILL\b",
    r"\bCHARIT(?:Y|IES|ABLE)\b", r"\bNON ?-?PROFIT\b", r"\bSOC(?:IAL)? SERVICES\b", r"\bMASONIC\b",
    r"\bHOSPITAL\b", r"\bMEDICAL CENTER\b", r"\bHEALTH SYSTEMS?\b", r"\bHOSPICE\b", r"\bREALTORS\b",
    r"\bAMERICAN LEGION\b", r"\bVETERANS OF FOREIGN WARS\b", r"\bKNIGHTS OF COLUMBUS\b", r"\bURBAN LEAGUE\b",
)

# Suffixes that only a for-profit business uses.
LLC_SUFFIX = r"\b(?:LLC|LLLP|LLP|LP|LTD|PLLC|LIMITED|PARTNERSHIP|LIMITED LIABILITY)\b"
CORP_SUFFIX = r"\b(?:INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY)\b"

LIFE_ESTATE = r"\bLIFE EST(?:ATE)?\b"
ESTATE = (
    r"\bESTATE OF\b", r"\bEST OF\b", r"\bHEIRS?\b", r"\bDECEASED\b", r"\bDEC(?:EA)?D\b", r"\bDECEDENT\b",
    r"\bPROBATE\b", r"\bEXECUT(?:OR|RIX)\b", r"\bADMINISTRAT(?:OR|RIX)\b", r"(?<!REAL )\bESTATE$",
    r"(?<!REAL )\bEST$",
)
TRUST = (
    r"\bTRUSTS?\b", r"\S\s+TRS?\b", r"\bTRST\b", r"\bTRUSTEES?\b", r"\bTTEES?\b", r"\bREVOCABLE\b",
    r"\bIRREVOCABLE\b", r"\bIRREV\b", r"\bREV TR\b", r"\bLIV(?:ING)? TR(?:UST)?\b", r"\bAMENDED\b",
    r"\bRESTATED\b", r"\bDECLARATION OF\b", r"\bDTD\b",
)
TRUSTEE_ROLE = r"\bTRUSTEES?\b|\bTTEES?\b|\S\s+TRS?\b"

# Words only a business name uses (an LLC whose suffix was cut off, or a d/b/a name).
BUSINESS_WORDS = (
    r"\bAPART\w*", r"\bAPTS?\b", r"\bPROPERT\w*", r"\bINVEST\w*", r"\bHOLDINGS?\b", r"\bMANAG\w*", r"\bMGMT\b",
    r"\bPARTNERS?\b", r"\bENTERPRISES?\b", r"\bDEVELOP\w*", r"\bASSOCIATES\b", r"\bVENTURES?\b", r"\bREALTY\b",
    r"\bREAL ESTATE\b", r"\bRENTALS?\b", r"\bCAPITAL\b", r"\bGROUP\b", r"\bEQUIT(?:Y|IES)\b",
    r"\bACQUISITIONS?\b", r"\bLEASING\b", r"\bFUND\b", r"\bPORTFOLIO\b", r"\bASSETS?\b", r"\bOWNER\b",
    r"\bBORROWER\b", r"\bPROPCO\b", r"\bOPCO\b", r"\bHOMES\b", r"\bHOUSING\b", r"\bBUILDERS?\b",
    r"\bCONSTRUCTION\b", r"\bCONTRACTORS?\b", r"\bSERVICES\b", r"\bSOLUTIONS\b", r"\bINDUSTRIES\b",
    r"\bINTERNATIONAL\b", r"\bSTORAGE\b", r"\bLOGISTICS\b", r"\bCOMMUNIT(?:Y|IES)\b", r"\bRESIDENTIAL\b",
    r"\bRESIDENCES?\b", r"\bTOWNHOMES?\b", r"\bTOWNHOUSES?\b", r"\bCONDO\w*", r"\bLOFTS?\b", r"\bFLATS\b",
    r"\bSENIOR\b", r"\bPLAZA\b", r"\bCENTER\b", r"\bMOTEL\b", r"\bHOTEL\b", r"\bINN\b", r"\bBROS\b",
    r"\bBROTHERS\b", r"\bSONS\b", r"\bTRADING\b", r"\bLIVING\b", r"\bPHASE\b",
)
# Words in property names that can also be surnames ("HILL JOHN"): business only when not first.
PLACE_WORDS = frozenset({
    "VILLAGE", "VILLAGES", "VILLA", "VILLAS", "MANOR", "ESTATES", "COURT", "COURTS", "COMMONS", "CROSSING",
    "GARDENS", "GARDEN", "PARK", "PLACE", "POINTE", "POINT", "RIDGE", "TERRACE", "SQUARE", "PINES", "WOODS",
    "MEADOWS", "MEADOW", "HILL", "HILLS", "VIEW", "CLUB", "TOWER", "TOWERS", "HOUSE", "CREEK", "RUN", "LAKE",
    "LAKES", "GROVE", "GLEN", "TRACE", "LANDING", "STATION", "HEIGHTS", "KNOLL", "KNOLLS", "CHASE", "BROOK",
    "SPRINGS", "HOLLOW", "OAKS", "ACRES", "FARM", "FARMS", "PARKWAY", "VISTA", "HARBOR", "COVE", "ARMS", "HALL",
    "LOOKOUT", "GATE", "GATES", "WALK", "TRAIL", "TRAILS", "MILL", "MILLS", "FOREST", "ORCHARD",
    "PRESERVE", "RESERVE", "SUITES", "PLAZA",
})
PERSON_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV", "V"})
ROMAN = frozenset({"II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"})

_NUMBER_TOKEN = re.compile(r"\d")


@dataclass(frozen=True)
class OwnerRules:
    """Compiled rules (built-ins plus anything added in config.yaml `owners:`)."""
    gov_always: re.Pattern[str]
    gov_unless_suffix: re.Pattern[str]
    gov_unless_business: re.Pattern[str]
    gov_weak: re.Pattern[str]
    institutional: re.Pattern[str]
    bank: re.Pattern[str]
    operators: re.Pattern[str]
    nonprofit: re.Pattern[str]
    llc_suffix: re.Pattern[str]
    corp_suffix: re.Pattern[str]
    life_estate: re.Pattern[str]
    estate: re.Pattern[str]
    trust: re.Pattern[str]
    trustee_role: re.Pattern[str]
    business_words: re.Pattern[str]
    overrides: Mapping[str, str]


def build_rules(cfg: OwnerConfig | None = None) -> OwnerRules:
    cfg = cfg or OwnerConfig()
    kw = cfg.keywords

    def plus(base: Iterable[str], extra: list[str]) -> re.Pattern[str]:
        extra_re = _words(extra).pattern if extra else None
        return _any(list(base) + ([extra_re] if extra_re else []))

    return OwnerRules(
        gov_always=plus(GOV_ALWAYS, kw.government),
        gov_unless_suffix=_any(GOV_UNLESS_SUFFIX),
        gov_unless_business=_any(GOV_UNLESS_BUSINESS),
        gov_weak=_any(GOV_WEAK),
        institutional=plus(INSTITUTIONAL, kw.institutional),
        bank=re.compile(BANK),
        operators=_words(list(NATIONAL_OPERATORS) + list(cfg.institutional_names)),
        nonprofit=plus(NONPROFIT, kw.nonprofit),
        llc_suffix=plus([LLC_SUFFIX], kw.small_llc),
        corp_suffix=plus([CORP_SUFFIX], kw.corporation),
        life_estate=re.compile(LIFE_ESTATE),
        estate=plus(ESTATE, kw.estate),
        trust=plus(TRUST, kw.trust),
        trustee_role=re.compile(TRUSTEE_ROLE),
        business_words=_any(BUSINESS_WORDS),
        overrides={clean_owner_name(k) or "": v for k, v in cfg.overrides.items()},
    )


_RULES_CACHE: dict[int, tuple[Settings, OwnerRules]] = {}


def rules_for(settings: Settings | None = None) -> OwnerRules:
    """Rules for these settings (cached per Settings object)."""
    s = settings or get_settings()
    hit = _RULES_CACHE.get(id(s))
    if hit is not None and hit[0] is s:
        return hit[1]
    try:
        rules = build_rules(owner_config(s))
    except ValueError as exc:
        log.warning("owners: settings in config.yaml ignored (%s); using built-in rules", exc)
        rules = build_rules(OwnerConfig())
    if len(_RULES_CACHE) >= 8:
        _RULES_CACHE.clear()
    _RULES_CACHE[id(s)] = (s, rules)
    return rules


def _name_parts(s: str) -> list[list[str]]:
    """Tokens per co-owner: "SMITH JOHN & MARY" -> [["SMITH", "JOHN"], ["MARY"]]."""
    parts = re.split(r"\s+(?:&|AND)\s+|\s*&\s*|\s+AND$", s)
    return [p.split() for p in parts if p and p.strip()]


def _looks_like_person(s: str) -> bool:
    if _NUMBER_TOKEN.search(s) or s.startswith("THE "):
        return False
    parts = _name_parts(re.sub(r"\b(?:ET AL|ETAL|ET UX|ET VIR|TOD|JT|JTWROS|JT TEN|TIC)\b", " ", s))
    if not parts or len(parts) > 4:
        return False
    if sum(len(p) for p in parts) < 2:
        return False
    for tokens in parts:
        if len(tokens) > 5:
            return False
        ends_like_person = _is_initial(tokens[-1]) or tokens[-1] in PERSON_SUFFIXES
        if not ends_like_person and any(t in PLACE_WORDS and t not in FIRST_NAMES for t in tokens[1:]):
            return False
    return True


def classify_owner(name: Any, rules: OwnerRules | None = None) -> str:
    """Owner type from the owner name alone (portfolio and size-based institutional need the database).

    Returns individual | trust | estate | small_llc | corporation | institutional | government |
    nonprofit | unknown.
    """
    s = clean_owner_name(name)
    if not s or s in PLACEHOLDER_NAMES or not re.search(r"[A-Z]{2}", s):
        return "unknown"
    r = rules or rules_for()
    if s in r.overrides:
        return r.overrides[s]
    llc = bool(r.llc_suffix.search(s))
    corp = bool(r.corp_suffix.search(s))
    business = llc or corp or bool(r.business_words.search(s))
    trustee = bool(r.trustee_role.search(s))

    if r.gov_always.search(s):
        return "government"
    if r.operators.search(s) or r.institutional.search(s) or (r.bank.search(s) and not trustee):
        return "institutional"
    if not llc and r.nonprofit.search(s):
        return "nonprofit"
    if not (llc or corp) and r.gov_unless_suffix.search(s):
        return "government"
    if not business and r.gov_unless_business.search(s):
        return "government"
    if not business and r.gov_weak.search(s) and not any(t in PLACE_WORDS for t in s.split()):
        return "government"
    if llc:
        return "small_llc"
    if corp:
        return "corporation"
    s_person = r.life_estate.sub(" ", s).strip()
    if s_person != s:
        return "individual" if _looks_like_person(s_person) else "unknown"
    if r.estate.search(s):
        return "estate"
    if r.trust.search(s):
        return "trust"
    if business:
        return "small_llc"
    if len(s.split()) == 1:
        return "unknown"
    if _looks_like_person(s):
        return "individual"
    return "small_llc"


# --- best-guess person ---------------------------------------------------------------------------

# Common US first names, used only to decide name order ("JOHN SMITH" vs "SMITH JOHN").
FIRST_NAMES = frozenset(
    "AARON ADAM ALAN ALBERT ALEX ALEXANDER ALICE ALLEN AMANDA AMY ANDREA ANDREW ANGELA ANN ANNA ANNE ANTHONY ARTHUR "
    "ASHLEY BARBARA BENJAMIN BETTY BEVERLY BILL BILLY BOBBY BONNIE BRADLEY BRANDON BRENDA BRIAN BRUCE BRYAN CARL "
    "CAROL CAROLYN CATHERINE CHARLES CHERYL CHRISTINA CHRISTINE CHRISTOPHER CHRIS CINDY CRAIG CYNTHIA DANIEL DANNY "
    "DAVID DEBORAH DEBRA DENISE DENNIS DIANA DIANE DONALD DONNA DORIS DOROTHY DOUGLAS EDWARD ELIZABETH EMILY ERIC "
    "EUGENE EVELYN FRANCES FRANK FRED GARY GEORGE GERALD GLENN GLORIA GREGORY HAROLD HARRY HEATHER HELEN HENRY JACK "
    "JACQUELINE JAMES JANE JANET JANICE JASON JEAN JEFFREY JENNIFER JEREMY JERRY JESSICA JOAN JOE JOHN JONATHAN "
    "JOSEPH JOSHUA JOYCE JUAN JUDITH JUDY JULIE JUSTIN KAREN KATHERINE KATHLEEN KATHY KEITH KELLY KENNETH KEVIN "
    "KIMBERLY LARRY LAURA LAWRENCE LINDA LISA LORI LOUIS MARGARET MARIA MARIE MARILYN MARK MARTHA MARY MATTHEW "
    "MELISSA MICHAEL MICHELLE MILDRED NANCY NICHOLAS NICOLE NORMA PAMELA PATRICIA PATRICK PAUL PETER PHILLIP RALPH "
    "RANDY RAYMOND REBECCA RICHARD ROBERT ROGER RONALD ROSE ROY RUSSELL RUTH RYAN SAMUEL SANDRA SARA SARAH SCOTT "
    "SHARON SHIRLEY STEPHANIE STEPHEN STEVEN SUSAN TERESA TERRY THERESA THOMAS TIMOTHY TINA TODD VICTORIA VIRGINIA "
    "WALTER WANDA WAYNE WILLIAM ALICIA ALLISON ALVIN AMBER ANGIE ANITA ANTOINETTE APRIL AUDREY BARRY BECKY BEN "
    "BERNARD BERNICE BETH BETSY BOB BRAD BRENT BRETT BRITTANY BROOKE CALVIN CAMERON CANDACE CARLA CARLOS CARMEN "
    "CAROLINE CARRIE CASEY CATHY CHAD CHARLENE CHARLIE CHARLOTTE CHELSEA CHESTER CLARENCE CLAUDIA CLIFFORD CLINTON "
    "CODY COLLEEN CONNIE CORINNE COURTNEY CURTIS DALE DANA DARLENE DARRELL DARREN DARYL DAWN DEAN DEBBIE DELORES "
    "DERRICK DIXIE DON DONNIE DORA DUANE DUSTIN DWIGHT EARL EDDIE EDITH EDNA EILEEN ELAINE ELEANOR ELLA ELLEN ELMER "
    "ERICA ERIN ERNEST ESTHER ETHEL EVA FLOYD FRANCIS FREDERICK GAIL GENE GERALDINE GILBERT GINA GLADYS GLEN GLENDA "
    "GORDON GRACE GREG HAZEL HEIDI HERBERT HERMAN HOLLY HOWARD IDA IRENE ISAAC IVAN JACOB JAKE JAMIE JAN JANA "
    "JASMINE JAY JEANNE JEFF JENNY JEROME JESSE JILL JIM JIMMY JO JOANN JOANNE JODY JOEL JOHNNY JON JORDAN JOSE "
    "JOSEPHINE JOY JUANITA JUNE KARL KATIE KAY KELLI KEN KENT KIM KIRK KRISTEN KRISTIN KRISTY KURT KYLE LANCE LEE "
    "LEO LEON LEONARD LESLIE LESTER LEWIS LILLIAN LLOYD LOIS LONNIE LORETTA LORRAINE LUCILLE LUKE LYNN MARCIA "
    "MARCUS MARGIE MARIAN MARIO MARION MARJORIE MARLENE MARSHA MARTIN MARVIN MATT MAUREEN MAX MEGAN MELANIE MELVIN "
    "MICHELE MIKE MINDY MISTY MITCHELL MONICA NATALIE NATHAN NEIL NELSON NORMAN OLIVIA OSCAR PAT PATSY PATTY PAULA "
    "PEARL PEGGY PENNY PERRY PHILIP PHYLLIS RACHEL RANDALL RANDAL RAY REGINA RENEE RHONDA RICK RICKY RITA ROBERTA "
    "ROBIN RODNEY RON RONNIE ROSA ROSEMARY RUBY SALLY SAM SANDY SEAN SETH SHANNON SHAWN SHEILA SHELLY SHERRY SHERYL "
    "SIDNEY STACY STAN STANLEY STEVE STUART SUE SUZANNE SYLVIA TAMMY TANYA TED TERRI THELMA TIM TOM TOMMY TONI TONY "
    "TONYA TRACY TRAVIS TROY TYLER VALERIE VERA VERNON VICKI VICKIE VINCENT VIOLA VIOLET WARREN WENDY WESLEY WILLIE "
    "WILMA YVONNE ZACHARY"
    .split()
)

_PERSON_NOISE = frozenset({
    "TR", "TRS", "TRST", "TRUST", "TRUSTS", "TRUSTEE", "TRUSTEES", "TTEE", "TTEES", "REVOCABLE", "IRREVOCABLE",
    "IRREV", "REV", "LIVING", "LIV", "FAMILY", "AMENDED", "RESTATED", "DECLARATION", "OF", "THE", "DTD",
    "DATED", "AGREEMENT", "AGMT", "ESTATE", "EST", "HEIRS", "HEIR", "DECEASED", "DECD", "DECEDENT", "TOD",
    "ETAL", "JT", "JTWROS", "TIC", "LIFE", "AS", "AKA", "FBO", "SURVIVOR", "SURV", "EXECUTOR", "EXECUTRIX",
    "ADMINISTRATOR", "ADMINISTRATRIX", "PROBATE", "UNKNOWN", "SU", "SUC", "SUCC", "SUCCESSOR",
})


def _title_token(t: str) -> str:
    if t in ROMAN:
        return t
    if t in ("JR", "SR"):
        return t.title()
    out = "-".join("'".join(seg.capitalize() for seg in piece.split("'")) for piece in t.lower().split("-"))
    return re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), out)


def _strip_noise(s: str) -> str:
    s = re.sub(r"\b(?:ET AL|ET UX|ET VIR|JT TEN|TENANTS IN COMMON|TENANTS BY THE ENTIRETY|LIFE EST(?:ATE)?)\b",
               " ", s)
    s = re.sub(r"\b(?:U A|UA|UAD|UTA|U T A|UDT)\b", " ", s)
    s = re.sub(r"\bAMENDED (?:AND|&) RESTATED\b", " ", s)
    tokens = [t for t in s.split() if t not in _PERSON_NOISE and not _NUMBER_TOKEN.search(t)]
    return " ".join(tokens)


def _is_initial(t: str) -> bool:
    return len(t) == 1 and t.isalpha()


def _order_tokens(tokens: list[str], hint: str) -> list[str]:
    """Return tokens in natural order (first ... last).

    Public records list people "LAST FIRST MIDDLE"; trust names are usually written out
    ("JOHN A SMITH REVOCABLE TRUST"); "ESTATE OF ..." is followed by the natural name.
    """
    if len(tokens) < 2:
        return tokens
    if hint == "natural":
        natural = True
    elif _is_initial(tokens[-1]):
        natural = False
    elif hint == "trust_name":
        natural = not (tokens[1] in FIRST_NAMES and tokens[0] not in FIRST_NAMES)
    else:
        natural = tokens[0] in FIRST_NAMES and tokens[-1] not in FIRST_NAMES
    return tokens if natural else tokens[1:] + tokens[:1]


def _split_suffix(tokens: list[str]) -> tuple[list[str], list[str]]:
    core = [t for t in tokens if t not in PERSON_SUFFIXES or t == tokens[0]]
    suffix = [t for t in tokens[1:] if t in PERSON_SUFFIXES]
    return core, suffix


def _shares_surname(core: list[str]) -> bool:
    """A co-owner written as given names only ("JANE", "JANE A", "MARY ANN") shares the first surname."""
    if len(core) == 1:
        return True
    if len(core) == 2:
        return _is_initial(core[1]) or (core[0] in FIRST_NAMES and core[1] in FIRST_NAMES)
    return False


def _people(parts: list[list[str]], hint: str) -> list[str]:
    """Format co-owners; a co-owner given by first name only shares the first owner's surname."""
    first_core, first_sfx = _split_suffix(parts[0])
    if len(first_core) < 2:
        return []
    ordered = _order_tokens(first_core, hint)
    last = ordered[-1]
    given = ordered[:-1]
    shared_given: list[list[str]] = [given]
    others: list[str] = []
    for tokens in parts[1:]:
        core, sfx = _split_suffix(tokens)
        if not core:
            continue
        if _shares_surname(core):
            shared_given.append(core)
        else:
            o = _order_tokens(core, hint)
            others.append(" ".join(_title_token(t) for t in o + sfx))
    givens = " & ".join(" ".join(_title_token(t) for t in g) for g in shared_given)
    main = f"{givens} {_title_token(last)}".strip()
    if first_sfx and len(shared_given) == 1:
        main += " " + " ".join(_title_token(t) for t in first_sfx)
    return [main] + others


def person_from_owner(name: Any, rules: OwnerRules | None = None) -> str | None:
    """Best-guess human name behind an owner-of-record name, or None for entities.

    'DOE JOHN & JANE' -> 'John & Jane Doe'; 'SMITH JOHN A TR' -> 'John A Smith (trustee)';
    'ESTATE OF MARY SMITH' -> 'Mary Smith (estate)'; 'HOLLOWAY FAMILY TRUST' -> None.
    """
    otype = classify_owner(name, rules)
    if otype not in ("individual", "trust", "estate"):
        return None
    s = clean_owner_name(name, keep_apostrophes=True)
    if not s:
        return None
    r = rules or rules_for()
    plain = s.replace("'", "")
    if r.bank.search(plain) or r.business_words.search(plain) or r.llc_suffix.search(plain) \
            or r.corp_suffix.search(plain):
        return None  # e.g. a bank acting as trustee: no person to look for
    role: str | None = None
    hint = "records"
    if otype == "estate":
        role = "estate"
        hint = "natural" if re.match(r"^(?:THE )?(?:ESTATE|EST|HEIRS?) OF\b", plain) else "records"
    elif otype == "trust":
        role = "trustee" if r.trustee_role.search(plain) else "trust"
        if re.search(r"\bTRUST\b|\bREVOCABLE\b|\bLIVING\b|\bAMENDED\b", plain) and role == "trust":
            hint = "trust_name"
    parts = _name_parts(_strip_noise(s))
    if not parts:
        return None
    people = _people(parts, hint)
    if not people:
        return None
    out = " & ".join(people)
    return f"{out} ({role})" if role else out


# --- group keys ----------------------------------------------------------------------------------

_GROUP_DROP = re.compile(
    r"\b(?:THE|LLC|LLLP|LLP|LP|LTD|PLLC|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LIMITED|PARTNERSHIP|"
    r"ET AL|ETAL|TOD)\b")


def owner_group_name(name: Any) -> str | None:
    """Owner name for grouping: cleaned, with entity suffixes dropped ("ABC PROPERTIES, L.L.C." -> "ABC PROPERTIES")."""
    s = clean_owner_name(name)
    if not s or s in PLACEHOLDER_NAMES:
        return None
    g = re.sub(r"\s+", " ", _GROUP_DROP.sub(" ", s)).strip()
    return g or s


def mailing_key(prop: Mapping[str, Any]) -> str | None:
    """Normalized mailing address (street + unit + city + state + zip), or None when there is no street."""
    street = clean_str(prop.get("mailing_address"))
    if not street:
        return None
    unit = clean_str(prop.get("mailing_unit"))
    line = f"{street} {unit}" if unit and unit.upper() not in street.upper() else street
    return normalize_address(line, prop.get("mailing_city"), prop.get("mailing_state"), prop.get("mailing_zip"))


# --- per-property facts --------------------------------------------------------------------------

def _city(v: Any) -> str | None:
    s = clean_str(v)
    if not s:
        return None
    s = re.sub(r"[^A-Z ]", " ", s.upper())
    s = re.sub(r"\bSAINT\b", "ST", s)
    s = re.sub(r"\bMOUNT\b", "MT", s)
    s = re.sub(r"\b(?:TWP|TOWNSHIP|CITY|VILLAGE)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip() or None


def absentee_status(prop: Mapping[str, Any]) -> str:
    """local | instate | outofstate | unknown, from the mailing address vs the property address."""
    ms = (clean_str(prop.get("mailing_state")) or "").upper()
    ps = (clean_str(prop.get("state")) or "").upper()
    if not ms or not ps:
        return "unknown"
    if ms != ps:
        return "outofstate"
    mc, pc = _city(prop.get("mailing_city")), _city(prop.get("city"))
    if not mc or not pc:
        return "unknown"
    return "local" if mc == pc else "instate"


def years_owned(sale_date: Any, today: date) -> float | None:
    d = parse_date(sale_date)
    if d is None or d > today:
        return None
    return round((today - d).days / DAYS_PER_YEAR, 1)


def months_until(when: Any, today: date) -> float | None:
    d = parse_date(when)
    if d is None:
        return None
    return round((d - today).days / DAYS_PER_MONTH, 1)


def has_loan(prop: Mapping[str, Any]) -> bool:
    amount = prop.get("loan_amount")
    return bool(clean_str(prop.get("lender"))) or (isinstance(amount, (int, float)) and amount > 0) \
        or parse_date(prop.get("loan_maturity")) is not None


def debt_status(prop: Mapping[str, Any], owned_years: float | None, today: date, cfg: OwnerConfig) -> str:
    """free_and_clear | maturing_soon | active | unknown (see the module docstring for the rules)."""
    long_owned = owned_years is not None and owned_years >= cfg.free_and_clear_min_years
    if not has_loan(prop):
        return "free_and_clear" if long_owned else "unknown"
    months = months_until(prop.get("loan_maturity"), today)
    if months is None:
        return "active"
    if abs(months) <= cfg.maturing_soon_months:
        return "maturing_soon"
    if months < 0:
        return "free_and_clear"  # matured long ago and no newer loan on record: probably paid off
    return "active"


# --- portfolio grouping --------------------------------------------------------------------------

class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb, key=str)] = min(ra, rb, key=str)


@dataclass
class Group:
    key: str
    property_ids: list[int]
    parcels: int
    sites: int


def _shared_mailing_too_wide(props: list[dict], max_names: int) -> set[str]:
    """Mailing addresses used by so many different owner names that they belong to a tax service or agent."""
    names: dict[str, set[str]] = defaultdict(set)
    for p in props:
        mk, gn = p["_mail"], p["_name"]
        if mk and gn:
            names[mk].add(gn)
    return {mk for mk, ns in names.items() if len(ns) > max_names}


def count_sites(members: list[dict], same_site_meters: float) -> int:
    """Separate properties in a group: parcels within `same_site_meters` of each other count once."""
    seen_keys: dict[str, dict] = {}
    for m in members:
        seen_keys.setdefault(m.get("property_key") or f"id:{m['id']}", m)
    uniq = list(seen_keys.values())
    with_xy = [m for m in uniq if m.get("lat") is not None and m.get("lng") is not None]
    sites = len(uniq) - len(with_xy)
    if not with_xy:
        return sites
    limit_miles = max(0.0, same_site_meters) / METERS_PER_MILE
    uf = _UnionFind()
    for i, a in enumerate(with_xy):
        uf.find(i)
        for j in range(i + 1, len(with_xy)):
            b = with_xy[j]
            if haversine_miles(a["lat"], a["lng"], b["lat"], b["lng"]) <= limit_miles:
                uf.union(i, j)
    return sites + len({uf.find(i) for i in range(len(with_xy))})


def build_groups(props: list[dict], cfg: OwnerConfig) -> dict[int, Group]:
    """Group properties that share an owner name or a mailing address. Returns property id -> Group."""
    for p in props:
        p["_name"] = owner_group_name(p.get("owner_name"))
        p["_mail"] = mailing_key(p)
    too_wide = _shared_mailing_too_wide(props, cfg.mailing_group_max_names)
    uf = _UnionFind()
    for p in props:
        node = ("p", p["id"])
        uf.find(node)
        if p["_name"]:
            uf.union(node, ("n", p["_name"]))
        if p["_mail"] and p["_mail"] not in too_wide:
            uf.union(node, ("m", p["_mail"]))
    members: dict[Any, list[dict]] = defaultdict(list)
    for p in props:
        members[uf.find(("p", p["id"]))].append(p)
    out: dict[int, Group] = {}
    for ms in members.values():
        seed = min((m["_name"] or m["_mail"] or f"id:{m['id']}") for m in ms)
        key = "pf:" + hashlib.sha1(seed.encode()).hexdigest()[:12]
        parcels = len({m.get("property_key") or f"id:{m['id']}" for m in ms})
        group = Group(key=key, property_ids=[m["id"] for m in ms], parcels=parcels,
                      sites=count_sites(ms, cfg.same_site_meters))
        for m in ms:
            out[m["id"]] = group
    return out


def final_type(base_type: str, sites: int, cfg: OwnerConfig) -> str:
    """Apply group size: `institutional_min_properties` or more separate properties -> institutional,
    `portfolio_min_properties` or more -> portfolio. Types in `portfolio_exempt_types` (institutional,
    government) never change."""
    if base_type in cfg.portfolio_exempt_types:
        return base_type
    if sites >= cfg.institutional_min_properties:
        return "institutional"
    if sites >= cfg.portfolio_min_properties:
        return "portfolio"
    return base_type


# --- main entry point ----------------------------------------------------------------------------

_PROP_COLS = ("id, market_id, property_key, owner_name, mailing_address, mailing_unit, mailing_city, "
              "mailing_state, mailing_zip, city, state, lat, lng, sale_date, lender, loan_amount, loan_maturity")


def _years_bucket(y: float | None) -> str:
    """Bucket for the run summary only (the score uses the buckets in config/asset_classes.yaml)."""
    if y is None:
        return "unknown"
    if y >= 20:
        return "20+"
    if y >= 10:
        return "10-19"
    if y >= 5:
        return "5-9"
    return "0-4"


def compute_owners(conn: sqlite3.Connection, market_slug: str, today: date | None = None,
                   settings: Settings | None = None) -> dict:
    """Work out owner type, portfolio group, absentee, years owned and debt for every property in a market.

    Groups are built across every market in the database. Safe to re-run: rows are replaced.
    """
    s = settings or get_settings()
    cfg = owner_config(s)
    rules = rules_for(s)
    today = today or date.today()
    market = market_by_slug(conn, market_slug)
    every = rows(conn, f"SELECT {_PROP_COLS} FROM properties")
    groups = build_groups(every, cfg)
    mine = [p for p in every if p["market_id"] == market["id"]]

    now = now_iso()
    types: Counter[str] = Counter()
    absentee: Counter[str] = Counter()
    debt: Counter[str] = Counter()
    years: Counter[str] = Counter()
    portfolio_groups: set[str] = set()
    sized_up = 0
    for p in mine:
        base = classify_owner(p.get("owner_name"), rules)
        group = groups.get(p["id"])
        sites = group.sites if group else 1
        otype = final_type(base, sites, cfg)
        if otype != base:
            sized_up += 1
        if otype in ("portfolio", "institutional") and group and sites >= cfg.portfolio_min_properties:
            portfolio_groups.add(group.key)
        has_key = bool(p["_name"] or p["_mail"])
        yo = years_owned(p.get("sale_date"), today)
        row = {
            "property_id": p["id"],
            "owner_type": otype,
            "owner_norm": p["_name"],
            "portfolio_key": group.key if (group and has_key) else None,
            "portfolio_count": sites if (group and has_key) else None,
            "absentee": absentee_status(p),
            "years_owned": yo,
            "debt_status": debt_status(p, yo, today, cfg),
            "computed_at": now,
        }
        upsert(conn, "owners", "property_id", row)
        types[otype] += 1
        absentee[row["absentee"]] += 1
        debt[row["debt_status"]] += 1
        years[_years_bucket(yo)] += 1
    conn.commit()
    result = {
        "market": market_slug,
        "properties": len(mine),
        "owner_types": {t: types.get(t, 0) for t in OWNER_TYPES},
        "portfolio_groups": len(portfolio_groups),
        "portfolio_properties": types.get("portfolio", 0),
        "grown_by_group_size": sized_up,
        "portfolio_min": cfg.portfolio_min_properties,
        "absentee": {k: absentee.get(k, 0) for k in ABSENTEE_VALUES},
        "debt": {k: debt.get(k, 0) for k in DEBT_VALUES},
        "years_owned": {k: years.get(k, 0) for k in ("20+", "10-19", "5-9", "0-4", "unknown")},
    }
    result["message"] = _summary_message(result)
    return result


def _summary_message(r: dict) -> str:
    if not r["properties"]:
        return "No properties in this market yet. Import a Crexi export first."
    t = r["owner_types"]
    top = ", ".join(f"{n:,} {OWNER_TYPE_LABELS[k].lower()}" for k, n in
                    sorted(t.items(), key=lambda kv: -kv[1]) if n)[:400]
    return (f"Checked the owners of {r['properties']:,} properties: {top}. "
            f"{r['portfolio_groups']:,} owners hold {r['portfolio_min']} or more properties. "
            f"{r['absentee']['outofstate']:,} owners live out of state; "
            f"{r['debt']['free_and_clear']:,} properties look free and clear.")
