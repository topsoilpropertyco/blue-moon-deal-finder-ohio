"""Cache for every external API call, plus usage tracking for cost estimates.

Rules:
- Cache keys never contain API keys. Build them from the meaningful request parts.
- A cache hit costs nothing and is not recorded in api_calls.
- record_call() is called once per billable call actually sent.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Callable

from .config import Settings, get_settings
from .db import now_iso, one, scalar


def make_key(provider: str, *parts: Any) -> str:
    raw = json.dumps([provider, *parts], sort_keys=True, default=str)
    return f"{provider}:{hashlib.sha256(raw.encode()).hexdigest()[:40]}"


def cache_get(conn: sqlite3.Connection, key: str) -> dict | None:
    row = one(conn, "SELECT status, body FROM api_cache WHERE key=?", (key,))
    if not row:
        return None
    try:
        body = json.loads(row["body"]) if row["body"] is not None else None
    except ValueError:
        body = row["body"]
    return {"status": row["status"], "body": body}


def cache_put(conn: sqlite3.Connection, key: str, provider: str, status: int, body: Any) -> None:
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    conn.execute(
        "INSERT INTO api_cache(key, provider, status, body, created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET status=excluded.status, body=excluded.body, created_at=excluded.created_at",
        (key, provider, status, text, now_iso()),
    )
    conn.commit()


def cached(conn: sqlite3.Connection, key: str, provider: str, fetch: Callable[[], tuple[int, Any]],
           cache_errors: bool = False) -> tuple[Any, bool]:
    """Return (body, from_cache). fetch() returns (status, body). Errors (status >= 400) are not cached
    unless cache_errors=True."""
    hit = cache_get(conn, key)
    if hit is not None and (hit["status"] or 0) < 400:
        return hit["body"], True
    status, body = fetch()
    if status < 400 or cache_errors:
        cache_put(conn, key, provider, status, body)
    return body, False


def record_call(conn: sqlite3.Connection, provider: str, sku: str, units: int = 1) -> None:
    conn.execute("INSERT INTO api_calls(provider, sku, units, created_at) VALUES(?,?,?,?)", (provider, sku, units, now_iso()))
    conn.commit()


def month_usage(conn: sqlite3.Connection, sku: str) -> int:
    month = datetime.now().strftime("%Y-%m")
    # created_at is UTC; 'localtime' puts each call in this computer's month, the same clock as `month`.
    return int(scalar(conn, "SELECT COALESCE(SUM(units),0) FROM api_calls WHERE sku=? "
                            "AND strftime('%Y-%m', created_at, 'localtime')=?", (sku, month)) or 0)


def estimate_cost(conn: sqlite3.Connection, sku: str, new_units: int, settings: Settings | None = None) -> dict:
    """Estimate the dollar cost of `new_units` more calls of a Google SKU this month, after free calls."""
    s = settings or get_settings()
    price = (s.section("google").get("prices") or {}).get(sku, {"per_1000": 0, "free_per_month": 0})
    used = month_usage(conn, sku)
    free_left = max(0, int(price.get("free_per_month", 0)) - used)
    billable = max(0, new_units - free_left)
    usd = round(billable * float(price.get("per_1000", 0)) / 1000.0, 2)
    return {"sku": sku, "units": new_units, "used_this_month": used, "free_left": free_left,
            "billable": billable, "usd": usd}
