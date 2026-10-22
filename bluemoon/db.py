"""SQLite storage: one local file holds the master list and every enrichment layer.

Every external API result is cached (table api_cache) so re-runs never re-pay.
Rows that fail a filter are never deleted; they are marked in `scores` and exported.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings, get_settings

SCHEMA_VERSION = 2

# Columns added after a database was first made. CREATE TABLE IF NOT EXISTS never changes an old table, so
# init_db adds each missing one with ALTER TABLE (safe to run again: it checks first). (table, column, type)
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("scores", "rule_reasons", "TEXT"),      # version 2: buy-box decisions by hand
    ("scores", "aside_buckets", "TEXT"),
)
# meta key: markets whose scores were saved before version 2. The dashboard re-scores each one once, so the
# set-aside groups (scores.aside_buckets) exist. JSON list of market ids.
RESCORE_PENDING_KEY = "rescore_for_groups"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- A market is one portal: an asset class + a geography.
CREATE TABLE IF NOT EXISTS markets (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  asset_class TEXT NOT NULL,
  geography TEXT NOT NULL,
  origin_label TEXT, origin_lat REAL, origin_lng REAL,
  max_drive_minutes INTEGER,
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY,
  market_id INTEGER NOT NULL REFERENCES markets(id),
  filename TEXT NOT NULL,
  file_hash TEXT,
  rows_total INTEGER, rows_new INTEGER, rows_updated INTEGER, rows_skipped INTEGER,
  column_map_json TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
);

-- The master list. raw_json keeps every original column untouched.
CREATE TABLE IF NOT EXISTS properties (
  id INTEGER PRIMARY KEY,
  market_id INTEGER NOT NULL REFERENCES markets(id),
  property_key TEXT NOT NULL,
  source TEXT, source_id TEXT, crexi_url TEXT,
  name TEXT, property_type TEXT,
  address TEXT, unit TEXT, city TEXT, state TEXT, zip TEXT, county TEXT, apn TEXT,
  lat REAL, lng REAL, geocode_quality TEXT,
  units INTEGER, building_sqft REAL, lot_acres REAL, building_count INTEGER, stories INTEGER, year_built INTEGER,
  sale_date TEXT, sale_price REAL,
  lender TEXT, loan_amount REAL, loan_type TEXT, interest_rate REAL, loan_maturity TEXT,
  improvement_value REAL, land_value REAL, total_value REAL, value_type TEXT, tax_year INTEGER, annual_tax REAL,
  usps_vacant INTEGER, opportunity_zone INTEGER, preforeclosure INTEGER, reo INTEGER,
  owner_name TEXT,
  mailing_address TEXT, mailing_unit TEXT, mailing_city TEXT, mailing_state TEXT, mailing_zip TEXT, mailing_care_of TEXT,
  raw_json TEXT NOT NULL DEFAULT '{}',
  flags_json TEXT NOT NULL DEFAULT '[]',
  first_import_id INTEGER, last_import_id INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(market_id, property_key)
);
CREATE INDEX IF NOT EXISTS ix_properties_market ON properties(market_id);
CREATE INDEX IF NOT EXISTS ix_properties_owner ON properties(owner_name);

-- Enrichment layers (one row per property).
CREATE TABLE IF NOT EXISTS drive_times (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  minutes REAL, meters REAL,
  method TEXT NOT NULL,            -- google_routes | estimate
  origin_key TEXT,                 -- lat,lng of the origin used
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rent_fits (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  source TEXT, area_name TEXT, fiscal_year TEXT,
  rents_json TEXT,                 -- {"0": 812, "1": 905, "2": 1150, "3": 1480, "4": 1600}
  fit TEXT,                        -- in | near | out | na
  detail TEXT,
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visual_checks (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  status TEXT NOT NULL,            -- pending | no_imagery | done | error
  pano_id TEXT, pano_lat REAL, pano_lng REAL, pano_date TEXT, heading REAL,
  images_json TEXT,                -- relative paths under images/
  ai_label TEXT, ai_p REAL, image_quality TEXT, property_visible INTEGER,
  cues_json TEXT, reasons TEXT,
  prior REAL, final_p REAL,        -- final_p: 0-100; NULL = needs human review
  model TEXT, error TEXT,
  reviewed_label TEXT, reviewed_note TEXT, reviewed_at TEXT,
  checked_at TEXT
);

CREATE TABLE IF NOT EXISTS owners (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  owner_type TEXT,                 -- individual | trust | estate | small_llc | corporation | portfolio | institutional | government | nonprofit | unknown
  owner_norm TEXT,
  portfolio_key TEXT,
  portfolio_count INTEGER,
  absentee TEXT,                   -- local | instate | outofstate | unknown
  years_owned REAL,
  debt_status TEXT,                -- free_and_clear | maturing_soon | active | unknown
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  in_buybox INTEGER NOT NULL,      -- 1 = in the buy box: the rules plus any decision made by hand
  exclusion_reasons TEXT NOT NULL DEFAULT '[]',   -- why it is out now: rule reasons not waived, + 'manual'
  rule_reasons TEXT,               -- JSON: what the rules alone say (NULL on rows scored before version 2)
  aside_buckets TEXT,              -- JSON: set-aside groups, e.g. ["drive_time:76-90", "size:1-4"]
  size_value REAL, size_basis TEXT, size_color TEXT,
  visual_color TEXT, rent_color TEXT,
  score REAL,                      -- Blue Moon probability 0..1
  signals_json TEXT,               -- [{key,label,value,multiplier|null,why}]
  coverage_have INTEGER, coverage_total INTEGER,
  rank INTEGER,
  computed_at TEXT NOT NULL
);

-- DRILL
CREATE TABLE IF NOT EXISTS drill_state (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  level INTEGER NOT NULL DEFAULT 1,      -- 1 no owner data, 2 possible owner, 3 confirmed owner
  status TEXT NOT NULL DEFAULT 'active', -- active | confirmed | exhausted | dnc
  best_guess_person TEXT,
  confirmed_person TEXT,
  confirmed_at TEXT,
  handed_off_at TEXT,
  notes TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drill_attempts (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  stage_key TEXT NOT NULL,
  outcome TEXT NOT NULL,           -- found | nothing | skipped
  detail TEXT,
  actor TEXT NOT NULL DEFAULT 'you',  -- auto | you
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_property ON drill_attempts(property_id);
CREATE INDEX IF NOT EXISTS ix_attempts_stage ON drill_attempts(stage_key);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  role TEXT,                       -- owner_of_record | statutory_agent | member | officer | trustee | deed_signer | manager | heir | other
  source TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_points (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
  channel TEXT NOT NULL,           -- mail | phone | text | email | social | vm
  value TEXT NOT NULL,
  source TEXT,                     -- stage key or source key
  status TEXT NOT NULL DEFAULT 'untested',  -- untested | verified | wrong | dnc
  notes TEXT,
  last_tested_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contacts_property ON contact_points(property_id);

CREATE TABLE IF NOT EXISTS outreach (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  contact_point_id INTEGER REFERENCES contact_points(id) ON DELETE SET NULL,
  channel TEXT NOT NULL,
  outcome TEXT NOT NULL,           -- see config/drill.yaml validate.outcomes
  notes TEXT,
  created_at TEXT NOT NULL
);

-- AI web research (the ai_research DRILL stage): one run per owner, the findings per property.
CREATE TABLE IF NOT EXISTS research_runs (
  id INTEGER PRIMARY KEY,
  market_id INTEGER REFERENCES markets(id),
  owner_key TEXT NOT NULL,         -- 'name:<normalized owner name>' (older runs may hold 'pk:<portfolio key>')
  owner_name TEXT NOT NULL,
  cache_key TEXT,                  -- api_cache.key of the answer
  model TEXT, prompt_version TEXT,
  outcome TEXT NOT NULL,           -- found | nothing | error
  from_cache INTEGER NOT NULL DEFAULT 0,
  summary TEXT, error TEXT, num_turns INTEGER, seconds REAL,
  property_ids TEXT NOT NULL DEFAULT '[]',
  actor TEXT NOT NULL DEFAULT 'ai',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_research_runs_market ON research_runs(market_id);

CREATE TABLE IF NOT EXISTS research_findings (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,              -- entity | person | website | phone | email | social
  value TEXT NOT NULL,             -- blank for a finding from a blocked site (nothing from it is kept)
  label TEXT, person_name TEXT, role TEXT,
  source_url TEXT,
  confidence TEXT,                 -- high | medium | low
  match_reason TEXT,
  status TEXT NOT NULL DEFAULT 'kept',  -- kept | lead | blocked | rejected | wrong
  why TEXT,                        -- why it is only a lead, or was blocked or rejected
  contact_point_id INTEGER REFERENCES contact_points(id) ON DELETE SET NULL,
  person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
  marked_by TEXT, marked_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_findings_property ON research_findings(property_id);
CREATE INDEX IF NOT EXISTS ix_findings_contact ON research_findings(contact_point_id);
CREATE INDEX IF NOT EXISTS ix_findings_run ON research_findings(run_id);

-- County records (enrich/parcels.py): which properties were looked up in the county's parcel service, and what
-- came of it. An error is not recorded (it is tried again). status: found | older | no_parcel | no_data | ambiguous.
CREATE TABLE IF NOT EXISTS parcel_lookups (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  source TEXT NOT NULL,            -- OH:GREENE | OH:statewide
  status TEXT NOT NULL,
  owner_differs INTEGER NOT NULL DEFAULT 0,
  detail TEXT,
  checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_ledger (
  id INTEGER PRIMARY KEY,
  property_id INTEGER REFERENCES properties(id) ON DELETE SET NULL,
  month TEXT NOT NULL,             -- YYYY-MM
  result TEXT,                     -- found | nothing
  created_at TEXT NOT NULL
);

-- Deals that worked out (or not), entered by you. Used to tune the score.
CREATE TABLE IF NOT EXISTS outcomes (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  outcome TEXT NOT NULL,           -- blue_moon | bought | passed | lost
  note TEXT,
  created_at TEXT NOT NULL
);

-- Buy-box decisions made by hand. reason '*' = the whole property, or one rule key (drive_time, year, size,
-- state, skip_county, property_type). include = in the buy box anyway ('*') or ignore this rule for it (a
-- rule key); exclude = set aside by hand ('*' only); keep = looked at it, the rule stands (a rule key only).
-- "Follow the rules" deletes the row; buybox_override_log keeps the history.
CREATE TABLE IF NOT EXISTS buybox_overrides (
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  reason TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('include', 'exclude', 'keep')
                                AND (decision <> 'exclude' OR reason = '*')
                                AND (decision <> 'keep' OR reason <> '*')),
  bucket TEXT,                     -- the set-aside group it was decided from, e.g. 'drive_time:76-90'
  detail_at_decision TEXT,         -- what the rules said then, e.g. 'about 82 min drive; the limit is 60 min'
  note TEXT,
  batch_id TEXT,                   -- one click = one batch (undone together)
  decided_by TEXT NOT NULL,        -- email | this Mac | team | open | cli
  decided_at TEXT NOT NULL,
  PRIMARY KEY (property_id, reason)
);
CREATE INDEX IF NOT EXISTS ix_overrides_batch ON buybox_overrides(batch_id);

-- Every buy-box decision ever made or cleared (append-only; never deleted).
CREATE TABLE IF NOT EXISTS buybox_override_log (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  property_key TEXT,               -- copy, in case a re-import ever splits the property
  reason TEXT NOT NULL,
  decision TEXT NOT NULL,          -- include | exclude | keep | cleared
  previous TEXT,                   -- the decision before this one (NULL = the rules)
  bucket TEXT, note TEXT, batch_id TEXT,
  detail TEXT,                     -- what the rules said then (so an undo can put it back)
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_override_log_property ON buybox_override_log(property_id);
CREATE INDEX IF NOT EXISTS ix_override_log_batch ON buybox_override_log(batch_id);
CREATE INDEX IF NOT EXISTS ix_override_log_created ON buybox_override_log(created_at);

-- Corrections made by hand to a property's facts (units, year built, county...), for now by editing a Blue
-- Moon export in Excel and importing it back (ingest/roundtrip.py). Append-only: the newest row per
-- (property, field) is the correction in force, and a later Crexi import keeps it. Values are JSON.
CREATE TABLE IF NOT EXISTS property_edits (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  field TEXT NOT NULL,             -- a properties column from ingest/roundtrip.py EDITABLE
  old_value TEXT,                  -- JSON: what it was before (usually what Crexi said)
  new_value TEXT,                  -- JSON: the correction (null = cleared)
  via TEXT,                        -- 'import:<imports.id>'
  actor TEXT NOT NULL,             -- email | this Mac | team | open | cli
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_property_edits_property ON property_edits(property_id, field);

-- Cache of every external API response, so re-runs never re-pay.
CREATE TABLE IF NOT EXISTS api_cache (
  key TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  status INTEGER,
  body TEXT,
  created_at TEXT NOT NULL
);

-- Every paid or metered call actually made (not cache hits).
CREATE TABLE IF NOT EXISTS api_calls (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  sku TEXT NOT NULL,
  units INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  market_id INTEGER,
  status TEXT NOT NULL,            -- queued | running | done | error | cancelled
  progress REAL NOT NULL DEFAULT 0,
  message TEXT,
  params_json TEXT,
  result_json TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def this_month() -> str:
    return datetime.now().strftime("%Y-%m")


_lock = threading.Lock()


def connect(path: Path | str | None = None, settings: Settings | None = None) -> sqlite3.Connection:
    """Open the database (creating it and the schema if needed)."""
    s = settings or get_settings()
    p = Path(path) if path else s.path("db")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    init_db(conn, s)
    return conn


def init_db(conn: sqlite3.Connection, settings: Settings | None = None) -> None:
    with _lock:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version' AND CAST(value AS INTEGER) < ?",
                     (str(SCHEMA_VERSION), SCHEMA_VERSION))
        conn.commit()
    sync_markets(conn, settings or get_settings())


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add the columns in MIGRATIONS that an older database is missing. Runs on every connect (under _lock),
    so it only changes what is missing. When scores gains its set-aside columns on a database that already has
    scores, those markets are noted in meta, so the dashboard re-scores them once (pending_rescore)."""
    had_scores = False
    for table, column, kind in MIGRATIONS:
        if column in _columns(conn, table):
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError as exc:   # another program (the CLI, the dashboard) just added it
            if "duplicate column" not in str(exc).lower():
                raise
            continue
        if table == "scores":
            had_scores = True
    if had_scores:
        ids = [r[0] for r in conn.execute("SELECT DISTINCT p.market_id FROM scores s "
                                          "JOIN properties p ON p.id = s.property_id ORDER BY 1").fetchall()]
        if ids:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (RESCORE_PENDING_KEY, dumps(ids)))


def pending_rescore(conn: sqlite3.Connection) -> set[int]:
    """Markets scored before version 2 that still need one re-score for their set-aside groups."""
    raw = scalar(conn, "SELECT value FROM meta WHERE key = ?", (RESCORE_PENDING_KEY,))
    return {int(x) for x in (loads(raw, []) or []) if isinstance(x, int)}


def rescore_done(conn: sqlite3.Connection, market_id: int) -> None:
    """This market was re-scored: take it off the pending list."""
    left = sorted(pending_rescore(conn) - {int(market_id)})
    if left:
        conn.execute("UPDATE meta SET value = ? WHERE key = ?", (dumps(left), RESCORE_PENDING_KEY))
    else:
        conn.execute("DELETE FROM meta WHERE key = ?", (RESCORE_PENDING_KEY,))
    conn.commit()


def sync_markets(conn: sqlite3.Connection, settings: Settings) -> None:
    """Make sure every market in config.yaml exists as a row (updates name/origin, keeps id)."""
    for m in settings.markets:
        ac = settings.asset_classes.get(m.asset_class)
        label = ac.label if ac else m.asset_class
        name = f"{label} · {m.geography}"
        row = conn.execute("SELECT id FROM markets WHERE slug=?", (m.slug,)).fetchone()
        if row:
            conn.execute(
                "UPDATE markets SET name=?, asset_class=?, geography=?, origin_label=?, origin_lat=?, origin_lng=?, max_drive_minutes=? WHERE id=?",
                (name, m.asset_class, m.geography, m.origin.label, m.origin.lat, m.origin.lng, m.max_drive_minutes, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO markets(slug, name, asset_class, geography, origin_label, origin_lat, origin_lng, max_drive_minutes, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (m.slug, name, m.asset_class, m.geography, m.origin.label, m.origin.lat, m.origin.lng, m.max_drive_minutes, now_iso()),
            )
    conn.commit()


# --- small query helpers -------------------------------------------------

def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Any:
    r = conn.execute(sql, tuple(params)).fetchone()
    return r[0] if r else None


def market_by_slug(conn: sqlite3.Connection, slug: str) -> dict:
    m = one(conn, "SELECT * FROM markets WHERE slug=?", (slug,))
    if not m:
        raise KeyError(f"Unknown market '{slug}'")
    m["settings"] = json.loads(m.get("settings_json") or "{}")
    return m


def upsert(conn: sqlite3.Connection, table: str, key_col: str, data: dict) -> None:
    """INSERT ... ON CONFLICT(key_col) DO UPDATE for one-row-per-property tables."""
    cols = list(data)
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != key_col)
    sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders}) ON CONFLICT({key_col}) DO UPDATE SET {updates}"
    conn.execute(sql, tuple(data[c] for c in cols))


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def loads(v: str | None, default: Any = None) -> Any:
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default
