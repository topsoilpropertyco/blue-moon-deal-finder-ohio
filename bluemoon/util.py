"""Small shared helpers: text normalization, distance, dates."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

_SUFFIXES = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "BOULEVARD": "BLVD", "CIRCLE": "CIR", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "TERRACE": "TER", "TRAIL": "TRL", "WAY": "WAY", "SQUARE": "SQ", "PIKE": "PIKE",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}


def clean_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    return s or None


def normalize_address(addr: str | None, city: str | None = None, state: str | None = None, zip_: Any = None) -> str | None:
    """Uppercase, strip punctuation, standardize street suffixes. Used for dedupe keys."""
    a = clean_str(addr)
    if not a:
        return None
    a = a.upper()
    a = re.sub(r"[.,#]", " ", a)
    a = re.sub(r"\s+", " ", a).strip()
    words = [_SUFFIXES.get(w, w) for w in a.split(" ")]
    parts = [" ".join(words)]
    for extra in (city, state):
        e = clean_str(extra)
        if e:
            parts.append(re.sub(r"\s+", " ", e.upper()))
    z = clean_str(zip_)
    if z:
        parts.append(z.split("-")[0].split(".")[0].zfill(5)[:5])
    return "|".join(parts)


def normalize_county(c: str | None) -> str | None:
    s = clean_str(c)
    if not s:
        return None
    s = s.upper()
    s = re.sub(r"\s+COUNTY$", "", s).strip()
    return s


def normalize_owner(name: str | None) -> str | None:
    """Collapse an owner name for grouping: uppercase, drop punctuation and entity suffix noise."""
    s = clean_str(name)
    if not s:
        return None
    s = s.upper()
    s = re.sub(r"[.,'\"]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_zip(z: Any) -> str | None:
    s = clean_str(z)
    if not s:
        return None
    s = s.split("-")[0].split(".")[0]
    return s.zfill(5)[:5] if s.isdigit() else s


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Compass bearing from point 1 to point 2, 0-360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def parse_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


def to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).strip().replace(",", "").replace("$", "").replace("%", "")
    if not s or s.startswith("="):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(v: Any) -> int | None:
    f = to_float(v)
    return int(round(f)) if f is not None else None


def yes_no(v: Any) -> int | None:
    s = clean_str(v)
    if s is None:
        return None
    s = s.lower()
    if s in ("yes", "y", "true", "1"):
        return 1
    if s in ("no", "n", "false", "0"):
        return 0
    return None
