"""Pre-filled research links for the DRILL, plus a small owner-name -> person parser.

Every link comes from `config/sources.yaml`:

- `kind: link` sources have a `template` with placeholders like `{apn}` or `{owner_q}` (see `link_values`).
- `kind: county` sources come from the county table: `counties[STATE][COUNTY][slot]` where slot is one of
  auditor | rental_registration | recorder | probate | clerk | gis. A slot is either a URL string or
  `{url, template?, name?}`. The template (usually a parcel deep link) is used when the property has the
  values it needs (an APN); otherwise the county's search page `url` is used.
- `kind: auto` / `kind: manual` sources have no link and are left out.

A county that is not in the table yet gets a plain web search for its office, so a work queue is never empty.
Nothing here calls the network: these are links for a person to open by hand.
"""
from __future__ import annotations

import re
import string
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote_plus

from ..config import Settings, get_settings
from ..util import clean_str, normalize_county

# Which county-table slot each county source reads. A source can override it with `county_slot:`.
COUNTY_SLOTS: dict[str, str] = {
    "county_auditor": "auditor",
    "rental_registration": "rental_registration",
    "county_recorder": "recorder",
    "probate_court": "probate",
    "clerk_of_courts": "clerk",
    "county_gis": "gis",
}
SLOT_LABELS: dict[str, str] = {
    "auditor": "auditor",
    "rental_registration": "rental registration",
    "recorder": "recorder",
    "probate": "probate court",
    "clerk": "clerk of courts",
    "gis": "parcel map (GIS)",
}
# Used only when a county is missing from the table. sources.yaml may override it with
# `county_fallback_template:` (placeholders: {county_q}, {state}, {office_q}).
DEFAULT_COUNTY_FALLBACK = "https://www.google.com/search?q={county_q}+County+{state}+{office_q}"
FALLBACK_OFFICE_WORDS: dict[str, str] = {
    "auditor": "auditor parcel search",
    "rental_registration": "rental registration",
    "recorder": "recorder document search",
    "probate": "probate court case search",
    "clerk": "clerk of courts case search",
    "gis": "parcel GIS map",
}

# Placeholders whose value may be blank without making the link useless.
OPTIONAL_PLACEHOLDERS = frozenset({
    "city", "city_q", "state", "state_lower", "zip", "county", "county_q", "county_title", "unit",
})

PROPERTY_LINK_SOURCES = ("street_view", "google_maps_listing")


# --- owner name -> person ------------------------------------------------------------------------

_ENTITY_WORDS = re.compile(
    r"\b(?:LLC|L L C|LLLP|LLP|LP|L P|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LTD|LIMITED|PLLC|PC|"
    r"PARTNERS|PARTNERSHIP|HOLDINGS?|PROPERTIES|PROPERTY|INVESTMENTS?|INVESTORS?|GROUP|ASSOCIATES|"
    r"APARTMENTS?|APTS|MANAGEMENT|MGMT|REALTY|REAL ESTATE|RENTALS?|ENTERPRISES?|VENTURES?|CAPITAL|FUND|"
    r"REIT|EQUITIES|DEVELOPMENT|DEVELOPERS|BUILDERS|HOMES|HOUSING|COMMUNITIES|VILLAGE|VILLAS|ESTATES|"
    r"BANK|BANCORP|SAVINGS|CREDIT UNION|FINANCIAL|MORTGAGE|NATIONAL|FEDERAL|"
    r"CITY|COUNTY|STATE|TOWNSHIP|TWP|AUTHORITY|BOARD|COMMISSION|DEPARTMENT|DEPT|DISTRICT|USA|"
    r"UNITED STATES|GOVERNMENT|CHURCH|MINISTRIES|MINISTRY|ASSOCIATION|ASSN|FOUNDATION|SOCIETY|"
    r"UNIVERSITY|COLLEGE|SCHOOL|SCHOOLS|HOSPITAL|CENTER|CENTRE|SERVICES|SYSTEMS?|SOLUTIONS|"
    r"TITLE|TRUST COMPANY|TRUST CO)\b")
_CO_WORDS = re.compile(r"\bCO[\s\-]?(?:TRUSTEES?|TRS?|TTEES?)\b|\bC/O\b.*$|\bCO[\s\-]?OWNERS?\b")

_ESTATE_PREFIX = re.compile(r"^(?:THE\s+)?(?:ESTATE|EST|HEIRS?)\s+OF\s+")
_NATURAL_HINT = re.compile(r"\b(?:REVOCABLE|IRREVOCABLE|LIVING)\b|^(?:THE\s+)")
_NOISE = re.compile(
    r"\b(?:THE|REVOCABLE|IRREVOCABLE|LIVING|TRUST|TRUSTS|TRS|TR|TRUSTEE|TRUSTEES|TTEE|TTEES|CO TRUSTEES|"
    r"CO-TRUSTEES|DECLARATION|AGREEMENT|DATED|DTD|U/A|UA|UAD|UTD|ESTATE|EST|HEIRS?|DECEASED|DEC|"
    r"ET AL|ETAL|ET UX|ETUX|ET VIR|ETVIR|TOD|JTWROS|JT|TEN|TENANTS|COMMON|ENTIRETY|LIFE ESTATE|FAMILY|"
    r"SURV|SURVIVORSHIP|RIGHTS OF|H/W|HW|L/E|LIFE EST|HUSBAND|WIFE|AND WIFE|AND HUSBAND|C/O|ATTN|MR|MRS|MS|DR)\b")
_DATE_NOISE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}\b|\b\d+\b")
_SPLIT = re.compile(r"\s*(?:&|\bAND\b|;|/|\+)\s*")
_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}

# Common US first names, used only to tell "SMITH JOHN" (records order) from "JOHN SMITH".
FIRST_NAMES = frozenset("""
AARON ADAM ALAN ALBERT ALEX ALEXANDER ALICE ALLEN AMANDA AMY ANDREA ANDREW ANGELA ANITA ANN ANNA ANNE
ANTHONY ARTHUR ASHLEY BARBARA BENJAMIN BETTY BEVERLY BILL BILLY BOB BOBBY BONNIE BRADLEY BRANDON BRENDA
BRIAN BRUCE CAROL CAROLYN CATHERINE CHARLES CHERYL CHRIS CHRISTINA CHRISTINE CHRISTOPHER CINDY CRAIG CYNTHIA
DALE DANIEL DANNY DAVID DEBORAH DEBRA DENISE DENNIS DIANA DIANE DONALD DONNA DORIS DOROTHY DOUGLAS EDWARD
ELIZABETH EMILY ERIC EUGENE EVELYN FRANK GARY GEORGE GERALD GLORIA GREGORY HAROLD HEATHER HELEN HENRY
JACK JACQUELINE JAMES JANE JANET JANICE JASON JEAN JEFFREY JENNIFER JEREMY JERRY JESSICA JOAN JOE JOHN
JONATHAN JOSEPH JOSHUA JOYCE JUAN JUDITH JUDY JULIE JUSTIN KAREN KATHLEEN KATHY KEITH KELLY KENNETH KEVIN
KIMBERLY LARRY LAURA LAWRENCE LINDA LISA LORI LOUIS MARGARET MARIA MARIE MARILYN MARK MARTHA MARY MATTHEW
MELISSA MICHAEL MICHELLE MIKE NANCY NATHAN NICHOLAS NICOLE PAMELA PATRICIA PATRICK PAUL PETER PHILIP
PHYLLIS RALPH RANDY RAYMOND REBECCA RICHARD ROBERT ROGER RONALD ROSE ROY RUSSELL RUTH RYAN SAMUEL SANDRA
SARA SARAH SCOTT SEAN SHARON SHIRLEY STEPHANIE STEPHEN STEVE STEVEN SUSAN TERESA TERRY THERESA THOMAS
TIMOTHY TINA TODD TOM TONY TRACY VICTORIA VIRGINIA WALTER WAYNE WILLIAM WILLIE
""".split())


def _is_initial(token: str) -> bool:
    return len(token) == 1 and token.isalpha()


def _title(token: str) -> str:
    if token in _SUFFIXES and token != "V":
        return token if token in {"II", "III", "IV"} else token.capitalize()
    parts = re.split(r"([\-'])", token.lower())
    return "".join(p.capitalize() if p not in {"-", "'"} else p for p in parts)


def looks_like_entity(name: Any) -> bool:
    """True when a name reads like a company, trust company, bank or government body."""
    s = clean_str(name)
    if not s:
        return False
    up = re.sub(r"[.,]", " ", s.upper())
    return bool(_ENTITY_WORDS.search(re.sub(r"\s+", " ", up)))


def _natural(tokens: list[str], natural_hint: bool) -> bool:
    """True when tokens are already "FIRST [MIDDLE] LAST" rather than records-style "LAST FIRST [MIDDLE]"."""
    if len(tokens) < 2:
        return True
    if _is_initial(tokens[-1]):
        return False  # SMITH JOHN A
    if _is_initial(tokens[1]) and len(tokens) == 3:
        return True   # JOHN A SMITH
    if tokens[0] in FIRST_NAMES and tokens[-1] not in FIRST_NAMES:
        return True
    if tokens[-1] in FIRST_NAMES and tokens[0] not in FIRST_NAMES:
        return False
    return natural_hint


def _split_suffix(tokens: list[str]) -> tuple[list[str], list[str]]:
    core = [t for t in tokens if t not in _SUFFIXES]
    return (core or tokens), [t for t in tokens if t in _SUFFIXES and core]


def _shares_surname(tokens: list[str]) -> bool:
    """A co-owner written as given names only ("JANE", "JANE B", "MARY ANN") shares the first surname."""
    if len(tokens) == 1:
        return True
    if len(tokens) == 2:
        return _is_initial(tokens[1]) or (tokens[0] in FIRST_NAMES and tokens[1] in FIRST_NAMES)
    return False


def _format(given: list[str], last: str, suffix: list[str]) -> str:
    out = " ".join(_title(t) for t in given) + " " + _title(last)
    if suffix:
        out += " " + " ".join(_title(t) for t in suffix)
    return out.strip()


def person_from_owner_name(name: Any) -> str | None:
    """Best-guess person behind an owner-of-record name, or None when it is an entity.

    'DOE JOHN & JANE' -> 'John & Jane Doe'; 'SMITH JOHN A TR' -> 'John A Smith';
    'ESTATE OF MARY JONES' -> 'Mary Jones'; 'HOLLOWAY FAMILY TRUST' -> None; 'ABC APARTMENTS LLC' -> None.
    """
    s = clean_str(name)
    if not s:
        return None
    up = re.sub(r"\s+", " ", s.upper().replace(".", " ")).strip()
    up = _CO_WORDS.sub(" ", up)
    if looks_like_entity(up):
        return None
    # County records and Crexi exports are all caps; a name typed in mixed case ("Pat Example") keeps the
    # order it was typed in unless the first-name list says it is flipped ("Smith John").
    natural_hint = bool(_ESTATE_PREFIX.match(up) or _NATURAL_HINT.search(up)) or any(c.islower() for c in s)
    up = _ESTATE_PREFIX.sub("", up)
    if "," in up:
        left, _, right = up.partition(",")
        if len(left.split()) == 1 and right.strip():
            up = f"{left} {right}"  # "DOE, JOHN" is records order
            natural_hint = False
        up = up.replace(",", " ")
    words = up.split()
    if len(words) >= 3 and words[-1] == "LE":  # life estate tag; "LE THANH" or "NGUYEN LE" is a name
        up = " ".join(words[:-1])
    cleaned = _DATE_NOISE.sub(" ", _NOISE.sub(" ", up))
    cleaned = re.sub(r"[^A-Z&;/+'\- ]", " ", cleaned)
    parts = [p.split() for p in _SPLIT.split(cleaned) if p.split()]
    if not parts:
        return None
    first_core, first_sfx = _split_suffix(parts[0])
    if len(first_core) < 2:
        return None
    nat = _natural(first_core, natural_hint)
    ordered = first_core if nat else first_core[1:] + first_core[:1]
    last, given = ordered[-1], ordered[:-1]
    shared: list[list[str]] = [given]
    others: list[str] = []
    for tokens in parts[1:]:
        core, sfx = _split_suffix(tokens)
        if _shares_surname(core):
            shared.append(core)
            continue
        o = core if _natural(core, nat) else core[1:] + core[:1]
        others.append(_format(o[:-1], o[-1], sfx))
    if len(shared) == 1:
        main = _format(given, last, first_sfx)
    else:
        givens = " & ".join(" ".join(_title(t) for t in g) for g in shared)
        main = f"{givens} {_title(last)}"
    return " & ".join([main, *others])


def first_person(name: Any) -> str | None:
    """One searchable person from a best-guess name: 'John & Jane Doe' -> 'John Doe'; drops '(trustee)'."""
    s = clean_str(name)
    if not s:
        return None
    s = re.sub(r"\([^)]*\)", "", s).strip()
    if "&" not in s:
        return s or None
    head, _, tail = s.partition("&")
    head, tail = head.strip(), tail.strip()
    words = head.split()
    if not words:
        return None
    last = words[-1].upper()
    if len(words) >= 2 and not _is_initial(last) and last not in FIRST_NAMES:
        return head  # "John Doe & Mary Smith": the first person is already a full name
    surname = tail.split()[-1] if tail.split() else ""
    return f"{head} {surname}".strip() or None


# --- placeholders ---------------------------------------------------------------------------------

def _q(v: Any) -> str:
    s = clean_str(v)
    return quote_plus(s) if s else ""


def _s(v: Any) -> str:
    s = clean_str(v)
    return s or ""


def _coord(v: Any) -> str:
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def full_address(prop: Mapping[str, Any]) -> str:
    """'123 Main St, Springfield, OH 45503' (missing parts are left out)."""
    street = _s(prop.get("address"))
    unit = _s(prop.get("unit"))
    if unit and unit.upper() not in street.upper():
        street = f"{street} {unit}".strip()
    city, state, zip_ = _s(prop.get("city")), _s(prop.get("state")), _s(prop.get("zip"))
    tail = " ".join(x for x in (state, zip_) if x)
    return ", ".join(x for x in (street, city, tail) if x)


def link_values(prop: Mapping[str, Any], person: str | None = None, parse_owner: bool = True) -> dict[str, str]:
    """Every placeholder a link template can use, as strings ('' when unknown).

    Names ending in _q are URL-encoded. `person` is the best-guess person; when missing, the property's
    `best_guess_person` (if the row has one) or, with parse_owner, a person read from the owner name is used.
    """
    apn = _s(prop.get("apn"))
    owner = _s(prop.get("owner_name"))
    who = clean_str(person) or clean_str(prop.get("best_guess_person")) \
        or (person_from_owner_name(owner) if parse_owner else None)
    search_person = first_person(who) or ""
    county = normalize_county(prop.get("county")) or ""
    state = _s(prop.get("state")).upper()
    full = full_address(prop)
    return {
        "apn": apn,
        "apn_q": _q(apn),
        "apn_digits": re.sub(r"\D", "", apn),
        "apn_nodash": re.sub(r"[\s\-.]", "", apn),
        "apn_spaces": re.sub(r"\s+", " ", re.sub(r"[\-.]", " ", apn)).strip(),
        "apn_spaces_q": _q(re.sub(r"\s+", " ", re.sub(r"[\-.]", " ", apn)).strip()),
        "address": _s(prop.get("address")),
        "address_q": _q(prop.get("address")),
        "unit": _s(prop.get("unit")),
        "city": _s(prop.get("city")),
        "city_q": _q(prop.get("city")),
        "state": state,
        "state_lower": state.lower(),
        "zip": _s(prop.get("zip")),
        "county": county,
        "county_q": _q(county.title()),
        "county_title": county.title(),
        "owner": owner,
        "owner_q": _q(owner),
        "person": who or "",
        "person_q": _q(search_person),
        "person_or_owner_q": _q(search_person or owner),
        "lat": _coord(prop.get("lat")),
        "lng": _coord(prop.get("lng")),
        "crexi_url": _s(prop.get("crexi_url")),
        "full_address": full,
        "full_address_q": _q(full),
    }


def fill_template(template: str | None, values: Mapping[str, str]) -> str | None:
    """Fill a link template. None when a needed value is blank or the template names an unknown field."""
    t = clean_str(template)
    if not t:
        return None
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(t) if f is not None]
    except ValueError:
        return None
    for f in fields:
        if f not in values:
            return None
        if not values[f] and f not in OPTIONAL_PLACEHOLDERS:
            return None
    try:
        return t.format(**{f: values[f] for f in fields})
    except (KeyError, IndexError, ValueError):
        return None


# --- county table ---------------------------------------------------------------------------------

def _norm_state(v: Any) -> str:
    return (clean_str(v) or "").upper()


def county_entry(state: Any, county: Any, settings: Settings | None = None) -> dict | None:
    """The county-table entry for a property's state and county (case and 'COUNTY' suffix ignored)."""
    s = settings or get_settings()
    table = (s.sources or {}).get("counties") or {}
    st = _norm_state(state)
    cty = normalize_county(county)
    if not st or not cty or not isinstance(table, Mapping):
        return None
    for skey, counties in table.items():
        if _norm_state(skey) != st or not isinstance(counties, Mapping):
            continue
        for ckey, entry in counties.items():
            if normalize_county(str(ckey)) == cty and isinstance(entry, Mapping):
                return dict(entry)
    return None


def _slot(entry: Mapping[str, Any] | None, slot: str) -> dict | None:
    if not entry:
        return None
    raw = entry.get(slot)
    if isinstance(raw, str):
        return {"url": raw} if clean_str(raw) else None
    if isinstance(raw, Mapping):
        return dict(raw)
    return None


def _county_link(prop: Mapping[str, Any], source_key: str, source: Mapping[str, Any], values: Mapping[str, str],
                 settings: Settings) -> dict | None:
    slot = clean_str(source.get("county_slot")) or COUNTY_SLOTS.get(source_key) or source_key
    county = values.get("county") or ""
    state = values.get("state") or ""
    entry = county_entry(state, county, settings)
    spec = _slot(entry, slot)
    county_title = county.title()
    office = SLOT_LABELS.get(slot, slot.replace("_", " "))
    if spec:
        url = fill_template(spec.get("template"), values) or clean_str(spec.get("url"))
        if not url:
            return None
        deep = bool(spec.get("template")) and url != clean_str(spec.get("url"))
        label = clean_str(spec.get("name")) or f"{county_title} County {office}"
        return {"label": label, "url": url, "deep_link": deep, "fallback": False,
                "how": clean_str(spec.get("how") or spec.get("notes")) or clean_str(source.get("how"))}
    if not county or not state:
        return None
    template = clean_str((settings.sources or {}).get("county_fallback_template")) or DEFAULT_COUNTY_FALLBACK
    fallback_values = {**values, "office_q": quote_plus(FALLBACK_OFFICE_WORDS.get(slot, office))}
    url = fill_template(template, fallback_values)
    if not url:
        return None
    why = (f"No {office} link is on file for {county_title} County; this searches for it." if entry
           else f"{county_title} County is not in the county table yet; this searches for it.")
    return {"label": f"Find the {county_title} County {office} site", "url": url, "deep_link": False,
            "fallback": True, "how": why}


# --- links ---------------------------------------------------------------------------------------

def _sources(settings: Settings) -> dict[str, dict]:
    return dict((settings.sources or {}).get("sources") or {})


def _stage(settings: Settings, stage_key: str) -> dict | None:
    for st in settings.drill_stages:
        if st.get("key") == stage_key:
            return st
    return None


def _source_applies(source: Mapping[str, Any], state: str, asset_class: str | None = None) -> bool:
    """A source can be limited to some states and/or asset classes. Unknown values never hide a source."""
    states = source.get("states")
    if states and state and state not in {_norm_state(x) for x in states}:
        return False
    classes = source.get("asset_classes")
    if classes and asset_class and asset_class not in {str(c) for c in classes}:
        return False
    return True


def source_link(prop: Mapping[str, Any], source_key: str, values: Mapping[str, str] | None = None,
                settings: Settings | None = None) -> dict | None:
    """One link for one source, or None when the source has no URL for this property."""
    s = settings or get_settings()
    source = _sources(s).get(source_key)
    if not source:
        return None
    vals = values if values is not None else link_values(prop)
    # The engine's property rows carry the market's asset class as `asset_class_key`.
    if not _source_applies(source, vals.get("state", ""),
                           clean_str(prop.get("asset_class") or prop.get("asset_class_key"))):
        return None
    kind = clean_str(source.get("kind")) or "link"
    base = {"source_key": source_key, "cost": clean_str(source.get("cost")) or "free", "kind": kind,
            "priority": source.get("priority"), "finds": clean_str(source.get("finds"))}
    if kind == "county":
        link = _county_link(prop, source_key, source, vals, s)
        return {**base, **link} if link else None
    if kind != "link":
        return None
    url = fill_template(source.get("template"), vals) or clean_str(source.get("url"))
    if not url:
        return None
    return {**base, "label": clean_str(source.get("name")) or source_key, "url": url, "deep_link": False,
            "fallback": False, "how": clean_str(source.get("how"))}


def _property_links(prop: Mapping[str, Any], values: Mapping[str, str], settings: Settings) -> list[dict]:
    out: list[dict] = []
    for key in PROPERTY_LINK_SOURCES:
        link = source_link(prop, key, values, settings)
        if link:
            out.append(link)
    if not any(x["source_key"] == "street_view" for x in out) and values.get("full_address_q"):
        out.insert(0, {"source_key": "street_view", "label": "Street View (no map pin yet: opens the map)",
                       "cost": "free", "kind": "link", "priority": None, "finds": "A look at the property.",
                       "url": f"https://www.google.com/maps/search/?api=1&query={values['full_address_q']}",
                       "deep_link": False, "fallback": True,
                       "how": "Drag the yellow person onto the street in front of the property."})
    if not any(x["source_key"] == "google_maps_listing" for x in out) and values.get("full_address_q"):
        out.append({"source_key": "google_maps", "label": "Google Maps", "cost": "free", "kind": "link",
                    "url": f"https://www.google.com/maps/search/?api=1&query={values['full_address_q']}",
                    "priority": None, "finds": None, "deep_link": False, "fallback": False, "how": None})
    for link in out:
        if link["source_key"] == "google_maps_listing":
            link["label"] = "Google Maps"
    if values.get("crexi_url"):
        out.append({"source_key": "crexi", "label": "Crexi", "url": values["crexi_url"], "cost": "free",
                    "kind": "link", "priority": None, "finds": "The Crexi property record.", "deep_link": False,
                    "fallback": False, "how": None})
    for key in ("county_auditor", "county_gis"):
        source = _sources(settings).get(key) or {"kind": "county", "cost": "free", "name": key}
        link = _county_link(prop, key, source, values, settings)
        if link and not link.get("fallback"):
            out.append({"source_key": key, "cost": "free", "kind": "county", "priority": source.get("priority"),
                        "finds": clean_str(source.get("finds")), **link})
    return out


def links_for(prop: Mapping[str, Any], stage_key: str | None = None, person: str | None = None,
              settings: Settings | None = None, parse_owner: bool = True) -> list[dict]:
    """Links to open by hand: [{label, url, source_key, cost, kind, finds, how, deep_link, fallback}].

    stage_key None -> property links (Street View, Google Maps, Crexi, county auditor and parcel map).
    stage_key given -> the stage's sources in order, each filled for this property. Sources with no URL
    (automatic or manual ones, or a county link we cannot build) are left out. 'validate' has no links.
    parse_owner=False: do not guess a person from the owner name (the engine passes its own best guess).
    """
    s = settings or get_settings()
    values = link_values(prop, person, parse_owner=parse_owner)
    if stage_key is None:
        return _property_links(prop, values, s)
    if stage_key == "validate":
        return []
    stage = _stage(s, stage_key)
    if stage is None:
        raise ValueError(f"Unknown DRILL stage '{stage_key}'. Check config/drill.yaml.")
    out: list[dict] = []
    seen: set[str] = set()
    for i, key in enumerate(stage.get("sources") or []):
        link = source_link(prop, key, values, s)
        # Only a stage's main source gets a "search for the site" fallback; extra sources need a real link.
        if not link or (i > 0 and link.get("fallback")):
            continue
        if link["url"] not in seen:
            seen.add(link["url"])
            out.append(link)
    return out
