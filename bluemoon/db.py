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
    # the AI's first-pass building details (photo check: style, stories, buildings, condition)
    ("visual_checks", "ai_style", "TEXT"),
    ("visual_checks", "ai_stories", "INTEGER"),
    ("visual_checks", "ai_buildings", "INTEGER"),
    ("visual_checks", "ai_condition", "TEXT"),
    ("credit_ledger", "period", "TEXT"),     # Crexi credit periods (bluemoon/crexi/period.py)
    ("credit_ledger", "batch_id", "INTEGER"),
    ("property_alternates", "batch_id", "INTEGER"),   # extra values: the Crexi batch they came back in
    # what the owner or a listing asks (never the same thing as the last sale price), and whether
    # building_sqft is net rentable or gross building area ('rentable' | 'gross' | NULL = not known)
    ("properties", "asking_price", "REAL"),
    ("properties", "building_area_type", "TEXT"),
    # who last added a person or changed their role, and when
    ("people", "edited_by", "TEXT"),
    ("people", "edited_at", "TEXT"),
    # a fingerprint of the property facts a letter was filled from (address, county, units, mailing address...)
    ("offer_documents", "facts_hash", "TEXT"),
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

-- One row each time a property's photos are pulled from Google. "Test on 5" on the photo button gives the AI
-- the properties it just pulled first; the rows are also a log of every pull.
CREATE TABLE IF NOT EXISTS photo_pulls (
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  pulled_at TEXT NOT NULL          -- UTC, like every other timestamp
);
CREATE INDEX IF NOT EXISTS idx_photo_pulls_at ON photo_pulls(pulled_at);

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
  role TEXT,                       -- owner_of_record | statutory_agent | member | officer | trustee | deed_signer | manager | broker | heir | other
  source TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
);

-- Who an offer for this property is addressed to. Kept apart from the seller (the owner of record on the
-- property), because the recipient can be someone else: a broker, a manager, a family member.
CREATE TABLE IF NOT EXISTS property_recipients (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  contact_point_id INTEGER REFERENCES contact_points(id) ON DELETE SET NULL,
  note TEXT,
  chosen_by TEXT NOT NULL,
  chosen_at TEXT NOT NULL
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
  month TEXT NOT NULL,             -- YYYY-MM of the day the credit period started
  result TEXT,                     -- found | nothing
  created_at TEXT NOT NULL,
  period TEXT,                     -- credit period start 'YYYY-MM-DD' (NULL on rows made before periods existed)
  batch_id INTEGER                 -- crexi_batches.id when the credit came back in a batch
);

-- Crexi bulk batches (bluemoon/crexi): Blue Moon exports a CSV, Seth uploads it to Crexi by hand, Crexi spends
-- credits and sends a file back, Seth imports it. Credits are shared by every market, so a batch can hold
-- properties from several markets (markets = JSON list of slugs; market_id is set only when there is one).
CREATE TABLE IF NOT EXISTS crexi_batches (
  id INTEGER PRIMARY KEY,
  market_id INTEGER REFERENCES markets(id),
  markets TEXT NOT NULL DEFAULT '[]',
  period TEXT NOT NULL,            -- credit period start 'YYYY-MM-DD' the batch is counted in
  kind TEXT NOT NULL CHECK (kind IN ('first', 'topup', 'manual')),
  requested INTEGER NOT NULL,      -- how many properties it holds
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'cli',
  file_name TEXT,                  -- the export file, relative to the output folder
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'imported', 'closed')),
  note TEXT,
  column_overrides TEXT,           -- JSON: which columns hold phones/emails/the contact name, set at import
  closed_at TEXT, closed_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_crexi_batches_period ON crexi_batches(period);

-- One row per property sent in a batch. 'sent' = waiting for Crexi (an in-flight credit), 'returned' = Crexi
-- sent the row back (the credit is spent), 'not_returned' = the batch was closed without it (no credit spent),
-- 'withdrawn' = the batch was cancelled before it was uploaded.
CREATE TABLE IF NOT EXISTS crexi_batch_items (
  batch_id INTEGER NOT NULL REFERENCES crexi_batches(id),
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  sent_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'returned', 'not_returned', 'withdrawn')),
  returned_at TEXT,
  import_id INTEGER,               -- crexi_batch_imports.id that returned it
  result TEXT,                     -- found (phone or email) | owner_only (a name, no phone or email) | nothing
  phones INTEGER NOT NULL DEFAULT 0,
  emails INTEGER NOT NULL DEFAULT 0,
  is_retry INTEGER NOT NULL DEFAULT 0,       -- a second pass for a property Crexi did not return before
  not_matched_at TEXT,             -- Crexi's reply said 'Not Matched' for it (still counted until the batch closes)
  UNIQUE (batch_id, property_id)
);
CREATE INDEX IF NOT EXISTS ix_crexi_items_property ON crexi_batch_items(property_id);

-- Every Crexi reply file imported (what it held and what came of it).
CREATE TABLE IF NOT EXISTS crexi_batch_imports (
  id INTEGER PRIMARY KEY,
  batch_ids TEXT NOT NULL DEFAULT '[]',
  file_name TEXT, saved_as TEXT, file_hash TEXT,
  rows_total INTEGER NOT NULL DEFAULT 0,
  returned INTEGER NOT NULL DEFAULT 0,       -- items newly marked returned
  found INTEGER NOT NULL DEFAULT 0, owner_only INTEGER NOT NULL DEFAULT 0, no_data INTEGER NOT NULL DEFAULT 0,
  not_matched INTEGER NOT NULL DEFAULT 0,    -- rows Crexi marked 'Not Matched'
  duplicates INTEGER NOT NULL DEFAULT 0, unmatched INTEGER NOT NULL DEFAULT 0,
  contacts_added INTEGER NOT NULL DEFAULT 0, dnc_marked INTEGER NOT NULL DEFAULT 0, unreadable INTEGER NOT NULL DEFAULT 0,
  columns_json TEXT, summary_json TEXT,
  created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT 'cli'
);

-- Rent evidence (bluemoon/napkin.py). Each row is one thing somebody told us about the rent: where it came
-- from, what it means (basis) and the yearly figure it works out to. The best one is 'selected' (or the one
-- pinned by hand); the rest stay here to look at. Money is whole cents; rates and counts keep their decimals as text.
CREATE TABLE IF NOT EXISTS rent_records (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  source TEXT NOT NULL,            -- rent_roll | listing | owner | broker | manual | import
  basis TEXT NOT NULL CHECK (basis IN ('actual', 'scheduled', 'estimated_full_occupancy', 'estimated_market', 'unconfirmed')),
  total_cents INTEGER, total_period TEXT,            -- monthly | annual | t12 | single_month | partial
  rate_text TEXT, rate_period TEXT,                  -- monthly | annual
  quantity_text TEXT, quantity_kind TEXT,            -- units | rentable_sqft
  area_type TEXT,                  -- rentable | gross | NULL (for a per sq ft rate)
  authoritative TEXT,              -- total | rate: which to trust when both were entered
  state TEXT NOT NULL CHECK (state IN ('ok', 'projection', 'needs_review')),
  annual_cents INTEGER,            -- NULL when it needs review: unknown is never zero
  formula TEXT, reason TEXT, discrepancy_cents INTEGER,
  evidence_date TEXT,              -- the day the figure is from
  note TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rent_records_property ON rent_records(property_id);

-- The napkin's assumptions, only where somebody changed them: a unit count, the real bedroom mix (counts by
-- bedroom) and rents by bedroom that replace the HUD ones. NULL = use the default.
CREATE TABLE IF NOT EXISTS rent_assumptions (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  units INTEGER,
  mix_json TEXT,                   -- {"1": 8, "2": 16}
  rents_json TEXT,                 -- {"2": 150000} monthly rent in cents by bedroom count
  updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- What a person recorded about a property's history: who built it (with a source), whether the last sale was
-- part of a bundle. Kept apart from condition and management claims.
CREATE TABLE IF NOT EXISTS property_history (
  property_id INTEGER PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
  builder_state TEXT NOT NULL DEFAULT 'unknown' CHECK (builder_state IN ('supported', 'suggested', 'contradicted', 'unknown')),
  builder_source TEXT, builder_note TEXT,
  sale_bundled INTEGER NOT NULL DEFAULT 0,
  sale_source TEXT,
  updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- Offers. One row per version of an offer for a property. The price is whole cents and the rent it was based on
-- is frozen in snapshot_json (napkin.snapshot), so a later rent change never rewrites it. price_mode says whether
-- the price still follows the 4x default or was typed by hand. 'sent' only comes from an explicit record (how, when,
-- by whom); sent_via tells a hand-marked send from one a mail provider reported. Rows are never deleted.
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  intent TEXT NOT NULL DEFAULT 'loi',
  status TEXT NOT NULL DEFAULT 'internal_draft' CHECK (status IN ('internal_draft', 'loi_template_needed',
    'required_fields_missing', 'loi_generated', 'ready_for_review', 'ready_to_send', 'sent')),
  origin TEXT NOT NULL DEFAULT 'prepared' CHECK (origin IN ('prepared', 'historical')),
  seller_name TEXT,                -- the owner of record when the offer was prepared
  recipient_person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
  recipient_name TEXT,             -- who it is addressed to (can be a broker), apart from the seller
  recipient_contact_id INTEGER REFERENCES contact_points(id) ON DELETE SET NULL,
  recipient_contact TEXT,
  price_cents INTEGER NOT NULL CHECK (price_cents > 0),
  price_mode TEXT NOT NULL CHECK (price_mode IN ('default', 'override')),
  snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
  incomplete_snapshot INTEGER NOT NULL DEFAULT 0,   -- 1: entered by hand after the fact, rent basis unknown
  sent_how TEXT CHECK (sent_how IN ('email', 'in person', 'mail')),
  sent_on TEXT, sent_by TEXT,
  sent_via TEXT CHECK (sent_via IN ('manual', 'provider')),
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (property_id, version, intent)
);

-- Everything that happens to an offer, appended and never changed.
CREATE TABLE IF NOT EXISTS offer_events (
  id INTEGER PRIMARY KEY,
  offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  detail_json TEXT,
  actor TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_offer_events_offer ON offer_events(offer_id);

-- What the seller side answered. original_text is kept exactly as entered; the category can be corrected.
-- A counter keeps its range (counter_high_cents is NULL for a single figure). Nothing here accepts an offer.
CREATE TABLE IF NOT EXISTS offer_responses (
  id INTEGER PRIMARY KEY,
  offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
  classification TEXT NOT NULL CHECK (classification IN ('serious_interest', 'counter', 'info_request', 'rejection',
    'no_response', 'needs_review')),
  original_text TEXT NOT NULL,
  counter_low_cents INTEGER, counter_high_cents INTEGER,
  received_on TEXT NOT NULL,
  recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL,
  corrected_by TEXT, corrected_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_offer_responses_offer ON offer_responses(offer_id);

CREATE TABLE IF NOT EXISTS offer_actions (
  id INTEGER PRIMARY KEY,
  offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
  text TEXT NOT NULL, owner TEXT NOT NULL, due_on TEXT,
  done INTEGER NOT NULL DEFAULT 0, done_by TEXT, done_at TEXT,
  auto INTEGER NOT NULL DEFAULT 0,       -- 1: made by the app when the offer was marked sent
  created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_offer_actions_offer ON offer_actions(offer_id);

-- The letter itself (a copy of the master template). Each row is tied to the offer snapshot it was made from.
CREATE TABLE IF NOT EXISTS offer_documents (
  id INTEGER PRIMARY KEY,
  offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
  template_id TEXT NOT NULL, template_version TEXT,
  snapshot_hash TEXT NOT NULL, facts_hash TEXT,
  doc_id TEXT, doc_url TEXT, pdf_id TEXT, pdf_url TEXT,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_offer_documents_offer ON offer_documents(offer_id);

-- How a property got on the list by hand ("Add a property"), so a new listing, a duplicate and a match resolved by
-- you stay apart. Appended and never changed. The link is only saved as the source: nothing is fetched from it.
CREATE TABLE IF NOT EXISTS lead_events (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('new_listing', 'duplicate', 'same_property')),
  detail_json TEXT,
  actor TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lead_events_property ON lead_events(property_id);

-- A send attempt through a transport (the test outbox for now; nothing here emails anyone). intent_key is what makes
-- a retry safe: the same offer version to the same recipient for the same intent is one row, so it is never
-- submitted twice, while a revised version has its own key. A row never makes the offer "sent" (only Mark as sent
-- or a provider's report does). state is what the transport reported.
CREATE TABLE IF NOT EXISTS offer_sends (
  id INTEGER PRIMARY KEY,
  offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
  intent_key TEXT NOT NULL UNIQUE,
  transport TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('prepared', 'queued', 'submitted', 'failed', 'delivered')),
  recipient_name TEXT, recipient_contact TEXT,
  outbox_file TEXT, error TEXT,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_offer_sends_offer ON offer_sends(offer_id);

-- "Prepare offers for selected": one batch and one row per property. A row ends as created (a new draft), existing
-- (the property already had an unsent draft, so nothing was added), failed (can be retried) or excluded (left out
-- by you). A retry only touches failed rows, so it can never make a second draft.
CREATE TABLE IF NOT EXISTS offer_batches (
  id INTEGER PRIMARY KEY,
  market_id INTEGER NOT NULL REFERENCES markets(id),
  status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'partial')),
  letters_ready INTEGER NOT NULL DEFAULT 0,      -- 1: the LOI template was active when the batch ran
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS offer_batch_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES offer_batches(id) ON DELETE CASCADE,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  price_cents INTEGER CHECK (price_cents IS NULL OR price_cents > 0),   -- typed price; NULL follows the 4x default
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'created', 'existing', 'failed', 'excluded')),
  offer_id INTEGER REFERENCES offers(id) ON DELETE SET NULL,
  message TEXT,
  UNIQUE (batch_id, property_id)
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

-- Extra values: a later Crexi row (a second copy of a property, a reply file, a re-import) that says something
-- else than the property already holds. The first value stays the main one; each different value is KEPT here
-- automatically as "<Field> 2", "<Field> 3"... (bluemoon/crexi/merge.py), shown next to the main one and in the
-- exports. Append-only. status: kept (the normal case), used / dismissed (decided by hand before Oct 2026).
CREATE TABLE IF NOT EXISTS property_alternates (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  field TEXT NOT NULL,             -- a properties column (units, year_built, owner_name, mailing_address...)
  value TEXT,                      -- JSON: the value Crexi sent
  original TEXT,                   -- JSON: the value held when it was sent
  source TEXT NOT NULL DEFAULT 'crexi',
  import_id INTEGER,               -- imports.id of the file that held it
  batch_id INTEGER,                -- crexi_batches.id when it came in Crexi's reply to a batch
  seen_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'kept' CHECK (status IN ('kept', 'used', 'dismissed')),
  decided_by TEXT, decided_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_alternates_property ON property_alternates(property_id, status);

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
        _migrate_extra_values(conn)
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


def _migrate_extra_values(conn: sqlite3.Connection) -> None:
    """Extra values (Oct 2026): a second value Crexi sends is kept automatically, no click needed. An older
    database has property_alternates with status 'open' waiting for 'Use this one' / 'Ignore': rebuild the table
    so 'open' rows become 'kept' (same ids, nothing lost), and drop the retired 'crexi_differs' warning flag."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='property_alternates'").fetchone()
    if row and "'open'" in (row[0] or ""):
        cols = ["id", "property_id", "field", "value", "original", "source", "import_id", "batch_id", "seen_at",
                "status", "decided_by", "decided_at"]
        have = _columns(conn, "property_alternates")
        pick = ", ".join(("CASE status WHEN 'open' THEN 'kept' ELSE status END" if c == "status" else c)
                         if c in have else "NULL" for c in cols)
        conn.execute("DROP INDEX IF EXISTS ix_alternates_property")
        conn.execute("ALTER TABLE property_alternates RENAME TO property_alternates_old")
        start = SCHEMA.index("CREATE TABLE IF NOT EXISTS property_alternates")
        end = SCHEMA.index("CREATE INDEX IF NOT EXISTS ix_alternates_property")
        conn.execute(SCHEMA[start:end].strip().rstrip(";"))
        conn.execute(f"INSERT INTO property_alternates({', '.join(cols)}) SELECT {pick} FROM property_alternates_old")
        conn.execute("DROP TABLE property_alternates_old")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_alternates_property ON property_alternates(property_id, status)")
    for pid, flags in conn.execute("SELECT id, flags_json FROM properties WHERE flags_json LIKE "
                                   "'%crexi_differs%'").fetchall():
        kept = [f for f in (loads(flags, []) or []) if f != "crexi_differs"]
        conn.execute("UPDATE properties SET flags_json=? WHERE id=?", (dumps(sorted(kept)), pid))


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
