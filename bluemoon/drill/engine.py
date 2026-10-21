"""DRILL engine (stub; the full engine replaces this file)."""
from __future__ import annotations

import sqlite3

from ..db import market_by_slug, now_iso


def ensure_states(conn: sqlite3.Connection, market_slug: str) -> int:
    """Create a drill_state row (level 1, active) for every property in the market that lacks one."""
    m = market_by_slug(conn, market_slug)
    cur = conn.execute(
        "INSERT INTO drill_state(property_id, level, status, updated_at) "
        "SELECT p.id, 1, 'active', ? FROM properties p LEFT JOIN drill_state d ON d.property_id=p.id "
        "WHERE p.market_id=? AND d.property_id IS NULL",
        (now_iso(), m["id"]),
    )
    conn.commit()
    return cur.rowcount
