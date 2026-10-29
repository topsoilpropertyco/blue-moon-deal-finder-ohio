# Architecture

Blue Moon Deal Finder runs on one computer (Seth's Mac Mini). A Python backend keeps one SQLite file
(`data/bluemoon.sqlite`) as the master list, enriches it layer by layer, scores it, runs the DRILL, and serves a
web dashboard (FastAPI + a no-build vanilla-JS single-page app) that works on a computer or a phone.

```
Crexi CSV/XLSX ─► ingest ─► properties (master list, every original column kept in raw_json)
                                │
     ┌──────────────┬───────────┼──────────────┬───────────────┬────────────────┐
     ▼              ▼           ▼              ▼               ▼                ▼
  drive time     rent fit     owners      visual check      scores          DRILL
 (Google Routes  (HUD FMR    (type,       (Street View +   (buy box, colors, (levels 1-3, stages,
  or estimate)    ArcGIS)    portfolio,   satellite +      Blue Moon score,  attempts, people,
                             equity)      Claude vision)   rank)             contacts, credits)
                                │
                                ▼
                 web dashboard  +  exports (xlsx/csv work lists, CRM handoff, mailing list)
```

## Concepts

- **Market** = one portal = asset class + geography (`config.yaml: markets`). Every market has the same screens.
  Properties belong to one market. All per-market functions take `market_slug`.
- **Asset class** (`config/asset_classes.yaml`): buy-box size metric and ranges, rent scale, the visual check
  question/labels/prompt, score multipliers.
- **Contact level** (`drill_state.level`): 1 No owner data, 2 Possible owner, 3 Confirmed owner (we talked and
  they confirmed; hand off to the CRM). Records alone never set level 3.
  `drill_state.status`: active | confirmed | exhausted | dnc.
- **DRILL stage** (`config/drill.yaml: stages`): ordered; each has goal, automation, cost, applies_to (owner
  types), optional asset_classes/states filters, sources, method, hit_rate (starting guess).
- **Sources** (`config/sources.yaml`): link templates and the county table.
- **County records** (`config/parcels.yaml`): the free county parcel services the County records step reads (see
  `enrich/parcels.py`): URL, field map and on/off switch per county, plus the statewide fallback.

## Rules (from CLAUDE.md)

- Never print, log or commit API keys. Read secrets only via `bluemoon.config.secret(name)`.
- Never delete rows that fail a filter: `scores.in_buybox=0` + `scores.exclusion_reasons`, exported to
  `output/<market>/excluded_<reason>.csv` (and `excluded_manual.csv` for the ones set aside by hand).
- Buy-box decisions made by hand live in `buybox_overrides` (every change in `buybox_override_log`) and are
  applied only in `rank.compute_scores`, so `scores.in_buybox` / `exclusion_reasons` are the final answer and
  `in_buybox == (exclusion_reasons == [])` holds for every row. `scores.rule_reasons` keeps what the rules said.
- Cache every external API result (`bluemoon.cache.cached`) and record every call actually sent (`record_call`; the free county parcel services are recorded too, provider `county_gis`, so a count of `api_calls` is not a count of paid calls).
- Paid steps support `dry_run=True` (count + cost, no calls) and `limit`. Runs over
  `safety.confirm_over_properties` need `confirm=True` from the caller (CLI asks; web shows the estimate first).
- Exact visual labels from the asset class config (apartments: EXTERIOR_CORRIDOR, DIRECT_ENTRY,
  INTERIOR_CORRIDOR, MIXED, UNKNOWN).
- Thresholds and weights come from YAML, not code.
- Never automate Crexi (no scraping, no login). Never automate people-search sites.
- Real Crexi exports and owner data never go into git (`.gitignore` covers data/, images/, labels/, output/,
  *.csv, *.tsv, *.xlsx, *.xlsm, *.ods, *.numbers, *.zip, *.sqlite, and screenshots: *.png, *.jpg, *.pdf).

## Shared modules (already written; use them)

- `bluemoon/config.py`: `get_settings()`, `Settings.path(name)`, `.asset_class(key)`, `.market(slug)`,
  `.markets`, `.drill`, `.drill_stages`, `.sources`, `.section(name)`; `secret(name)`, `has_secret(name)`.
- `bluemoon/db.py`: schema; `connect()`, `rows()`, `one()`, `scalar()`, `upsert(conn, table, key_col, data)`,
  `market_by_slug(conn, slug)`, `now_iso()`, `this_month()`, `dumps()`, `loads()`.
- `bluemoon/cache.py`: `make_key()`, `cached(conn, key, provider, fetch)`, `record_call()`, `month_usage()`,
  `estimate_cost(conn, sku, units)`.
- `bluemoon/util.py`: `normalize_address/county/owner/zip`, `haversine_miles`, `bearing_deg`, `parse_date`,
  `to_float`, `to_int`, `yes_no`, `clean_str`.

`progress` arguments below are optional callables `progress(fraction: float, message: str)`.
Every function that writes commits its own transaction.

## Module contracts

### ingest (`bluemoon/ingest/`)
- `columns.py`: `FIELD_ALIASES: dict[str, list[str]]`; `map_columns(headers, overrides=None) -> tuple[dict[str,str], list[str]]`
  (our field -> source header; unmapped headers). Exact alias match first (case-insensitive), then rapidfuzz.
- `crexi.py`: `read_table(path) -> list[dict]` (CSV or XLSX; for XLSX pick the sheet whose header row has
  Address/Owner-like columns, e.g. "Properties"; skip formula strings like "=IF(...)").
  `import_file(conn, market_slug, path, limit=None, overrides=None) -> dict` returns
  `{import_id, filename, rows_total, rows_new, rows_updated, rows_unchanged, rows_skipped, column_map, unmapped_columns,
  warnings}` (rows_unchanged: the part of rows_updated whose stored values did not change, e.g. a re-import).
  Property key: `apn:{STATE}:{COUNTY}:{APN}` when APN present else `addr:{normalize_address(...)}`.
  Re-import merges (updates fields, keeps id). Duplicate rows inside a file collapse. Sets `flags_json`
  (e.g. `year_unknown`, `year_invalid`, `units_unknown`, `shared_apn`, `no_owner_name`, `no_coordinates`).
  Normalizes county ("CLARK COUNTY" -> "CLARK"), zip to 5 digits, year 9999 -> NULL + flag.
  After import call `bluemoon.drill.engine.ensure_states(conn, market_slug)` (creates level rows).
  `import_file(..., actor=None)`: who is importing (the dashboard's `auth.signed_in_as`; `cli` when None).
  `updated_at` moves only when a stored value changes. After the merge, the corrections in `property_edits` are put
  back (result `edits_kept`, warning "Kept N values you corrected by hand"); raw_json still gets the file's values.
  XLSX: on a tie between sheets, our export's "All properties" wins; `read_sheets(path)` returns every list sheet.
- Round trip (our own exports coming back): `columns.detect_provider` returns `bluemoon` for a file with "Property ID"
  and "Row as of" (or >= 2 of our distinctive headers on older exports; or workbook metadata creator "Blue Moon
  Deal Finder"), checked before Crexi. `import_file`/`preview_file(..., conn=)` hand such a file to
  `roundtrip.import_blue_moon_edits` / `preview_blue_moon_edits`: rows match by "Property ID" inside the market
  (never inserts; unknown ids and other markets are reported), only `edits.EDITABLE` columns are read (Units,
  Building sq ft, Year built, Property type, County, ZIP, Lot acres; exact header first, as Crexi's own columns can
  differ only in case) plus "Buy box decision" ("Add to buy box" / "Set aside" / "Follow the rules" become
  `overrides.set_decision`/`clear` in one batch, note "From spreadsheet", bucket NULL; text still equal to what
  the export wrote changes nothing). Only real differences are written (decimals within 1e-4, text ignoring case;
  an empty cell clears), each one logged in `property_edits` (via `import:<id>`, actor). A row whose "Row as of" is
  older than `properties.updated_at` (fields) or the property's last `buybox_override_log` entry (decision) is left
  alone (`skipped_stale`). No "Row as of" anywhere: our workbook's `created` metadata (export time) stands in for
  it; a CSV (or a workbook not made by us) changes nothing (`skipped_unchecked`). A property sheet that lost the
  column while another has it is not read, and the preview names it. Sheets of one workbook that disagree about a
  value leave it alone (`conflicts`). Cells that can't be read (a minus sign, a whole number rounding to 0, a
  formula: `read_sheets` uses Excel's saved result, else keeps the formula text) are listed and left alone, never
  cleared. Never touches raw_json, source_id, owner or address; re-scores the market when anything changed. Result
  adds `{provider: 'bluemoon', properties_changed, fields_changed:{field:n}, decisions_changed:{include|exclude|rules:n},
  skipped_stale, skipped_unchecked, unknown_ids, other_market, conflicts, clears:{field:n}, confirm_clears, batch_id,
  message, changes:[{property_id, address, field, label, old, new}]}`; the preview has the same counts under `edits`
  and a `message`, and never owner data. `confirm_clears` (more clears than `safety.confirm_clears_over`) makes the
  Import screen ask "Erase N values?" before saving. `crexi._provider` refuses (IngestError) our other exports
  (`columns.other_blue_moon_export`: DRILL worklist, mailing list, CRM handoff) and our property lists without
  "Property ID" (`columns.looks_like_blue_moon` or the workbook metadata), so they never reach the Crexi merge.
- `edits.py`: `EDITABLE`, `latest_edits(conn, market_id, property_ids=None)`, `record_edit(...)`, `same_value(...)`.
  Table `property_edits(id, property_id, field, old_value, new_value (JSON), via, actor, created_at)`, append-only;
  the newest row per (property, field) is the correction in force. Owner names are not editable (a new owner name
  reads as a sale to the DRILL).

### enrich (`bluemoon/enrich/`)
- `drive.py`: `estimate_minutes(lat, lng, origin_lat, origin_lng, settings=None) -> float`;
  `drive_estimate(conn, market_slug, limit=None) -> dict` (`{properties, cached, to_fetch, elements, cost:{...}, provider}`);
  `compute_drive_times(conn, market_slug, limit=None, dry_run=False, provider=None, progress=None) -> dict`.
  Google Routes `computeRouteMatrix` (POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix,
  header X-Goog-Api-Key, X-Goog-FieldMask), batches of `drive.batch_size` destinations, cached per
  (origin, destination rounded to 5 decimals). Without a key: method `estimate`.
- `geo.py`: `geocode_missing(conn, market_slug, limit=None, dry_run=False, progress=None) -> dict`.
  US Census geocoder (free, no key) first; Google Geocoding fallback only if key. Sets `geocode_quality`.
- `rent.py`: `fmr_for_point(conn, lat, lng) -> dict | None` (`{area_name, fiscal_year, rents:{"0":..,"4":..}, source}`),
  `rent_fit(rents, rent_spec) -> tuple[str, str]` (fit in|near|out, plain-English detail),
  `compute_rent_fits(conn, market_slug, progress=None) -> dict`. Source `hud_arcgis` (free HUD map service),
  `hud_file` (spreadsheet in data/hud), `hud_api` (token). Skip (fit `na`) when asset class rent disabled.
- `owners.py`: `classify_owner(name) -> str` (individual | trust | estate | small_llc | corporation |
  institutional | government | nonprofit | unknown; `portfolio` is assigned when an owner group holds >= 5
  properties and is not institutional/government), `compute_owners(conn, market_slug, today=None) -> dict`.
  Portfolio groups across ALL markets by normalized owner name and by normalized mailing address.
  absentee: mailing state != property state -> outofstate; same state, different city -> instate; else local.
  years_owned from sale_date. debt: no lender/loan -> free_and_clear when years_owned >= 10 else unknown;
  maturity within 18 months -> maturing_soon; else active.

- `parcels.py`: **County records** (free, no key). `lookup_parcels(conn, market_slug, limit=None, dry_run=False,
  progress=None, *, client=None, settings=None) -> dict` and `parcel_estimate(conn, slug, limit=None)`;
  `load_config()` reads `config/parcels.yaml` (pydantic: per county `url`, `enabled`, `owners`, `owner_fallback`,
  `taxpayers`, `mailing` choices, `parcel_id`, `note`; `settings:` pace, retries, tax-service names, ignored values;
  `statewide:` the OGRIP mailing layer and the ODNR owner layer). `Settings.parcels` holds the raw YAML.
  Per property with a map point (buy box first, best score first; then the rest; `settings.scope: buybox` limits it)
  one point-in-parcel query goes to the county's layer (`<url>/query?geometry=<lng>,<lat>&geometryType=esriGeometryPoint
  &inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=*&returnGeometry=false&f=json`), at most
  `settings.concurrency` (5) at a time with a pause, timeouts and retries (`geo.send_with_retry`; 429/5xx and ArcGIS
  error codes >= 500 retry, other errors do not), and a layer that fails `stop_after_errors` times in a row is left
  alone for the rest of the run. Every answer (including "no parcel") is saved in `api_cache` (provider `county_gis`,
  key = layer URL + point rounded to `coordinate_decimals`) and counted in `api_calls` (provider and sku
  `county_gis`, cache hits are not); errors are never saved. An Ohio county with no enabled layer (or `enabled:
  false`, like Miami) uses the statewide layers; Indiana has no source. `parcel_lookups(property_id, source, status
  found|older|no_parcel|no_data|ambiguous, owner_differs, detail, checked_at)` remembers who was looked up (an error
  leaves no row, so it is tried again) and feeds the `parcels` progress layer (`layer_counts`).
  What it writes, in one transaction per property, with the engine's helpers (`_add_person`, `_add_contact`,
  `_insert_attempt`, `_refresh`): owner names -> people (`owner_of_record`, source `county_gis`), taxpayer or
  contact names -> people (`other`), the tax mailing address -> a `mail` contact (untested; do-not-contact
  addresses come in as `dnc`; a tax service as addressee is passed over); from a live county layer with an
  owner, the DRILL stage `county_auditor` is checked off `found` by actor `auto` (detail: county, parcel, owner,
  mailing address, and "not the owner in the Crexi export" when it differs, which also adds the property flag
  `county_owner_differs` to `flags_json` and a line in the owner's DRILL notes). `county_auditor` stays `assisted`
  (hands-on queue unchanged); a lookup that finds nothing writes no attempt so the stage stays open; statewide
  data writes people and the address but no attempt (it is 2023-24 data, marked "older statewide data").
  Owners whose family (same portfolio or owner name, any market) is do-not-contact are skipped and never asked;
  confirmed owners and properties whose `county_auditor` stage is already checked off are skipped. Never level 3.
  `stats.counts_by_market` leaves out `auto` check-offs of `county_auditor`, so the hands-on hit rate is not inflated.

### imagery (`bluemoon/imagery/`)
- `streetview.py`: `imagery_estimate(conn, market_slug, limit=None) -> dict`;
  `fetch_imagery(conn, market_slug, limit=None, dry_run=False, progress=None, confirm=False) -> dict`.
  Street View metadata first (free; no pano within radius -> status no_imagery). Bearing pano->property;
  3 images at heading, heading±35 (fov 80, pitch 5, 640x640) + 1 satellite (Maps Static zoom 19).
  Saves to `images/{property_id}/sv_0.jpg, sv_1.jpg, sv_2.jpg, sat.jpg`. Only for asset classes with
  visual_check enabled and properties in the buy box (or all with `include_excluded=True`; buy-box properties always
  go first, best Blue Moon rank first, then the rest the same way). Caps from config. Each time photos are saved
  `record_pull(conn, property_id)` adds a row to `photo_pulls(property_id, pulled_at)`.
- `vision.py`: backends with `.name` and `.classify(image_paths: list[Path], prompt: str) -> str` —
  `ClaudeCLIBackend` (runs `claude -p` with `--output-format json`, the Read tool allowed for the image dir,
  ANTHROPIC_API_KEY removed from the child env so the Claude plan is used), `AnthropicAPIBackend`, `MockBackend`.
  `get_backend(name=None)`; `build_prompt(asset_class, examples) -> str`; `parse_result(text, labels) -> VisualResult`
  (pydantic; strict JSON; one retry on parse failure);
  `classify_properties(conn, market_slug, limit=None, dry_run=False, backend=None, progress=None, confirm=False) -> dict`.
  final_p = weight_vision*ai_p + (1-weight_vision)*(50+prior); NULL when image_quality poor/none or
  property_visible false (needs human review).
  First-pass building details: when the asset class has `visual_check.details: true` (apartments) the prompt and
  the JSON schema also ask for `building_style` (garden | townhome-row | mid-rise | high-rise | converted | other),
  `estimated_stories`, `buildings_visible`, `condition` (good | fair | poor | unknown) and a one-sentence `reasons`.
  They are read leniently (`normalize_style`, `normalize_condition`, whole numbers in range) and saved in
  `visual_checks.ai_style / ai_stories / ai_buildings / ai_condition` (added by `db.MIGRATIONS`, so an older
  database gets the columns on its next connect). Answers saved before this have none. Colors and "unsure" still
  come only from final_p (the same green/yellow/red thresholds). `classify_properties(..., prefer_ids=[...])`
  checks those properties first (the batch whose photos were just pulled).
- `monthly.py`: the monthly photo goal (`photos:` in config.yaml: `monthly_target` 600, `batch` 100; your own
  numbers in config.local.yaml). `status(conn, slug)` -> `{done, target, remaining, reached, pool:{buybox, rest},
  ai_waiting, batch, keep_going_to}` ("412 of 600 done this month": distinct properties in `photo_pulls` this
  calendar month, local time; photos saved before the table existed are counted once from their files' dates,
  `backfill`); `photo_pool(conn, slug)` = properties still needing photos, buy box first by rank, then the rest;
  `estimate_batch(conn, slug, n)` = a dry run of photos + the AI check (calls nothing; `needs_confirm` over
  `safety.confirm_over_properties`); `run_batch(conn, slug, limit=None, dry_run, confirm, progress)` = photos for
  the next `limit` (default `batch`) properties with `include_excluded=True`, then the AI check, newly photographed
  properties first; `run_keep_going(...)` = batches of `batch` until the month's goal (one batch when it is already
  met), one OK for all of it, stopping at `google.max_*_per_run` and `vision.max_calls_per_run` counted over the
  whole run, at a fatal problem, or when nothing is left. Never writes anything the two steps it calls would not.
- `review.py`: `review_queue(conn, market_slug, limit=50, offset=0, include_excluded=False, scope=None) -> list[dict]`.
  Order: needs-a-look (final_p NULL) and unsure (21-79) first, then checked-nothing/no-photos, then the AI's calls
  (group `confirm`: green and red) most valuable first (in the buy box, the AI says target, best rank). `scope`
  `everything` (default, `review.default_scope`) or `unsure` (no `confirm`; `review.spot_check_fraction`, default 0,
  adds a stable random share of calls as group `spot_check`). Photos the AI hasn't looked at stay out
  (`review.include_unchecked` false). Each card has `ai_called`, `ai_is_target`, `ai_label_name`, `target_name`,
  `details {style, style_name, stories, buildings, condition, condition_name}` and a plain `why`.
  `queue_counts(...)` returns the counts per group plus `scopes {everything, unsure}`.
  `save_review(conn, property_id, label=None, note=None, agreed=None) -> dict`: `agreed=True` ("Yes, the AI is
  right") uses the AI's own label; `agreed=False` ("No, it's wrong") needs a different label; the result has
  `agreed` (None when the AI made no call). Sets reviewed_label, final_p 100/0/60 by label; copies images into
  `labels/examples/` + appends `labels/examples.jsonl` (`ExampleRecord.agreed`); updates `labels/scorecard.json`.
  `examples_for(asset_class, k)` also returns `ai_label` and `agreed`; the prompt tells the AI when its earlier guess
  for an example was wrong. `scorecard(conn, market_slug)` adds `ai_right`, `by_label` (per label: AI said, right,
  pct, you said, found_pct), `learning {recent, earlier, delta, verdict better|steady|worse|not_enough, trend[]}`
  (your latest `review.learning_window`=50 reviews vs the earlier ones) to the confusion matrix, by-color accuracy and
  acceptance check; `calibrate(conn, market_slug, backend=None) -> dict`.

### scoring (`bluemoon/scoring/`)
- `buybox.py`: `evaluate(prop, enrich, asset_class, market, settings) -> dict`
  (`{in_buybox, exclusion_reasons:[...], size_value, size_basis, size_color, visual_color, rent_color}`).
  Exclusion reasons: drive_time, year, size, state, skip_county, property_type. Missing data never excludes.
  Size basis hierarchy: units+sqft > units > sqft-estimate > unknown.
  `apply_overrides(ev, decisions) -> dict` (pure) applies the decisions made by hand: final `in_buybox` and
  `exclusion_reasons` (rule reasons not waived, plus `manual` = set aside by hand), and `rule_reasons`,
  `waived`, `kept`, `manual` ('include'|'exclude'|'waived'|None). `SET_ASIDE_REASONS` = the rules + `manual`.
- `overrides.py`: decisions by hand (tables `buybox_overrides`, keyed (property_id, reason '*' or a rule key),
  decision include|exclude ('*' only)|keep (a rule only); and the append-only `buybox_override_log`).
  `load(conn, market_ids)`, `for_property`, `history`, `set_decision(conn, ids, reason, decision, actor, ...)`,
  `clear(...)`, `undo_batch(conn, batch_id, actor)` (a decision changed again after the batch is left alone);
  and the web's `decide_group` (keep also takes back an add_all made from the same group, in the same batch, and
  its message says how many you had added left the buy box; clear also takes off whole-property adds made with
  no group: a row, the drawer or a spreadsheet; hand set-asides stay),
  `decide_property`, `undo` (they re-score the market before returning). Who: `auth.signed_in_as()` on the web, `cli` from the CLI.
- `buckets.py`: the Why? panel's set-aside groups. Keys `<reason>:<id>` (e.g. `drive_time:76-90`, `size:1-4`,
  `year:1960s`, `skip_county:oh-clermont`, `state:ky`, `property_type:retail`, `manual:by-hand`;
  `<reason>:other` = not grouped). Edges from `config.yaml set_aside` and `asset_classes.yaml size.set_aside_*`.
  Worked out at score time into `scores.aside_buckets`; `MEMBER_SQL` lists one group (json_each, bound
  parameters). `explain(reason, ...)` gives the plain-English text.
- `blue_moon.py`: `signals_for(prop, enrich, asset_class, settings) -> list[dict]` items
  `{key, label, value, multiplier (None = skipped), why}`; `probability(signals, base_rate) -> float`.
- `rank.py`: `compute_scores(conn, market_slug, progress=None) -> dict` (writes `scores`, rank 1..n among
  in-buybox by: score desc, visual final_p desc, drive minutes asc). The only writer of `scores`: applies the
  decisions by hand and stores `rule_reasons` and `aside_buckets` (JSON lists).
- `learn.py`: `learned_weights(conn, market_slug) -> dict` (empirical-Bayes blend of starting multipliers with
  outcomes; prior strength `scoring.learn_prior_strength`), `model_summary(conn, market_slug) -> dict`.

### drill (`bluemoon/drill/`)
- `engine.py`:
  `ensure_states(conn, market_slug)`; `run_auto_stages(conn, market_slug) -> dict` (records_baseline,
  portfolio_match); `applicable_stages(prop, owner_type, asset_class) -> list[dict]`;
  `property_drill(conn, property_id) -> dict` (`{level, status, best_guess_person, confirmed_person,
  stages:[{key,name,goal,automation,cost,status: done_found|done_nothing|skipped|next|pending|disabled,
  attempts:[...], links:[...]}], next_step:{kind: stage|validate|handoff|exhausted|dnc, stage_key, text},
  people:[...], contacts:[...], outreach:[...]}`);
  `record_attempt(conn, property_id, stage_key, outcome, detail=None, person=None, person_role=None,
  contacts=None) -> dict` (outcome found|nothing|skipped; contacts = [{channel, value, notes}]);
  `add_person(...)`, `add_contact(conn, property_id, channel, value, source=None, person_name=None, notes=None)`,
  `set_contact_status(conn, contact_id, status, allow_contact=False)` (do-not-contact is permanent:
  only `allow_contact=True`, "Allow contact again", takes a number, email or address off the list, on every
  property at once, and never while an owner still marked do-not-contact has it),
  `log_outreach(conn, property_id, channel, outcome, contact_point_id=None, notes=None) -> dict`
  (applies validate.outcomes effects: confirm -> level 3/status confirmed; mark_wrong; dnc),
  `level_action(conn, property_id, action, person=None, note=None) -> dict`
  (owner_found, confirm, dnc, undnc, exhausted, reopen; dnc also covers `owner_sibling_ids`, the owner's other
  properties with the same owner name or portfolio, and spreads every contact including the mailing address);
  `stage_queue(conn, market_slug, stage_key, limit=50, offset=0) -> dict` (properties whose next step is that
  stage, best Blue Moon score first, with links);
  `validate_queue(conn, market_slug, limit=50) -> dict` (properties with untested contacts);
  `stage_queue`, `validate_queue` and `overview` take `scope='all'|'buybox'` (buy box = set-aside properties left
  out once the market is scored); `waiting_counts(conn, market_slug) -> {total, validate, stages, basis}`;
  `overview(conn, market_slug) -> dict` (`{levels:{1:n,2:n,3:n}, status counts, penetration, pipeline:[{stage_key,
  name, input, resolved, unresolved, hit_rate_actual, hit_rate_hypothesis, tried}], credits:{...}}`).
  Penetration = confirmed / every buy-box owner except do-not-contact (exhausted owners still count).
  Stages are skipped in counties where the county table says their source doesn't exist
  (`DrillConfig.not_in_county`). With `learn_order` on, real hit rates reorder the free hands-on stages per
  owner type (`Ctx.stage_order`). A new owner of record (a sale) starts a new pass and takes back an old
  confirmation.
- `links.py`: `link_values(prop, person=None) -> dict`, `links_for(prop, stage_key=None, person=None) ->
  list[{label, url, source_key, cost}]` using `config/sources.yaml` templates and the county table.
- `research.py`: the ai_research stage (`research_owners`). People-search sites are blocked in Claude Code
  (WebFetch deny rules, archive/translation/reader sites denied, and `websearch_guard.py`, a PreToolUse hook that
  adds the blocked list to every WebSearch) and again after the answer (`blocked_by`, `screen`,
  `without_blocked`, `safe_text`: copies and disguised links count as blocked; free text from a blocked answer is
  never stored; phones and emails are taken out of free text). Research never changes the role of a person
  another source added. One Claude call per owner name (never per portfolio: a shared mailing address can join
  different owners); owners whose last tries failed go behind the rest; do-not-contact is checked again before
  each owner is asked and before its answer is written. A logged reach-out (`log_outreach`) on an AI contact
  updates its finding and copies like `set_contact_status`; taking a wrong mark back puts back the copies it
  reached; a verified AI contact carries to the owner's other properties (untested ones never do). A set-aside
  owner (scope buybox) is never held open waiting for research.
  `research_owners(conn, slug, limit=None, dry_run=False, backend=None, progress=None, confirm=False) -> dict`
  (CLI `run.py research`, pipeline step `research`, not part of `all`): the estimate has `owners` and
  `properties_total` (everyone left) and `properties` / `calls` / `cached` (this run, capped at
  `max_calls_per_run`); over `safety.confirm_over_properties` properties it waits for `confirm`, the offline
  `mock` backend too (its made-up answers are written into the market). Answers are saved in `api_cache` per
  owner (Claude Code: provider `research:claude_cli`; another backend has its name in the key and provider
  `research:<name>`); mock answers are never saved, and an old saved mock answer is never served.
  Tables: `research_runs(id, market_id, owner_key, owner_name, cache_key, model, prompt_version, outcome
  found|nothing|error, from_cache, summary, error, num_turns, seconds, property_ids, actor, created_at)` (one row
  per owner asked or answered from the cache) and `research_findings(id, run_id, property_id, kind
  entity|person|website|phone|email|social, value, label, person_name, role, source_url, confidence,
  match_reason, status kept|lead|blocked|rejected|wrong, why, contact_point_id, person_id, marked_by, marked_at,
  created_at)`. `mark_finding(conn, finding_id, status wrong|kept, actor)` backs the PATCH route below.
- `credits.py`: `credit_status(conn) -> {month, period, period_label, period_end, next_reset, reset_day,
  per_month, used, left, returned, in_flight, used_today, days_left, daily_pace, today_left, ...}` (credits are
  counted per credit period that resets on `crexi.reset_day`; **used = returned + in_flight**: `returned` = ledger rows
  of the period, `in_flight` = batch items still 'sent'; `left = per_month - used`) (the pace comes from the credits left when the day began, so it holds all day; the plan gives
  today `today_left` credits and every later day `daily_pace`), `use_credit(conn, property_id,
  result=None) -> dict`. One plan for the whole account (credits are shared by every market):
  `account_plan(conn)`, `market_plan(conn, market_slug, limit) -> {plan, extras, planned, planned_all,
  extra_count, extras_all, other_markets, others:[{slug, name, planned, extras}], shortfall, level1_open,
  message}` (planned/extra_count: this market; *_all, shortfall, level1_open: every market; level1_open = buy-box
  owners with no owner name who would qualify for a credit once named and are not planned yet). The plan
  evaluates only owners who could get a credit (one query per market), and the set-aside fallbacks best first
  until the credits are planned. `credit_plan(conn, market_slug, limit=25)`
  (this market's main plan: level-2, active, buy-box owners with no working or untested reach contact, by
  free stages tried, then score x the credit hit rate for the owner type) and `credit_extras(...)` (the
  `crexi.plan_fallbacks` tiers that use credits that would otherwise expire).
- `stats.py`: `stage_hit_rates(conn, market_slug) -> list[dict]` (by stage and owner type),
  `crm_handoff_rows(conn, market_slug) -> list[dict]`.

### crexi (`bluemoon/crexi/`, Crexi bulk batches)
Tables (db.py): `crexi_batches(id, market_id, markets JSON, period 'YYYY-MM-DD', kind first|topup|manual, requested,
created_at, created_by, file_name, status open|imported|closed, note, column_overrides JSON, closed_at, closed_by)`;
`crexi_batch_items(batch_id, property_id, position, sent_at, status sent|returned|not_returned|withdrawn, returned_at,
import_id, result found|owner_only|nothing, phones, emails, is_retry, not_matched_at, UNIQUE(batch_id, property_id))`;
`crexi_batch_imports(id, batch_ids, file_name, saved_as, file_hash, rows_total, returned, found, owner_only, no_data,
not_matched, duplicates, unmatched, contacts_added, dnc_marked, unreadable, columns_json, summary_json, created_at,
created_by)` (summary_json holds the enrichment report); `property_alternates(id, property_id, field, value,
original, source, import_id, seen_at, status open|used|dismissed, decided_by, decided_at)`; `credit_ledger` gained
`period` and `batch_id`. Credit rule: Crexi charges one credit per row it matches (even with no phone or email),
nothing for 'Not Matched'. A batch's rows are 'sent' (in flight) until Crexi returns them; closing marks the rest
`not_returned` (no credit, DRILL crexi_credit = nothing); `withdrawn` = cancelled before upload.
- `period.py`: `period_start/next_period_start/period_end(date, reset_day)`, `period_key`, `days_left`, `describe`,
  `label`. A month without the reset day starts its period on its last day.
- `batches.py`: `status`, `pick(conn, n, retry)` (the credit plan order, dedupe by Crexi link, APN, address; never a
  property already sent this period), `preview_export`, `export_batch` (writes output/crexi/<period>/batch-<id>-<N>.csv;
  cells starting = + - @ are quoted), `close_batch(confirm)`, `withdraw_batch(confirm)`, `history`, `batch_items`,
  `property_history`, `export_file` (by batch id, inside output/).
- `reply.py`: `preview(conn, path, columns, adopt_hand)` and `apply_reply(...)`. Reads Crexi's layout (header row found
  by scanning), detects phone/email/contact-name columns by header words (override per import, stored on the batch),
  matches rows to items across batches (newest first), unmatched/duplicate rows go to unmatched-<import>.csv,
  property facts go through `ingest.import_file` per market, contacts through the DRILL's `_add_contact`.
- `merge.py`: the keep-the-original rule (`plan_facts`, `apply_alternates`, `decide_alternate`, `Report`), used by the
  reply import and by `ingest/crexi.py` (`plan_update`, `_update`, duplicate rows in a file).
- `cli.py`: `uv run run.py crexi ...`.

### pipeline + jobs + exports
- `bluemoon/pipeline.py`: `STEPS = ["import","owners","drive","rent","parcels","photos","classify","score","drill","research","all"]`
  (`EXTRA_STEPS` also holds `photo_batch` and `photo_keep_going`: the Run screen's monthly photo goal buttons, both
  paid steps that re-score afterwards; `all` is unchanged)
  (`parcels`, label "County records", is free and not in `PAID_STEPS`; it runs inside `all` after the first `score`,
  so the buy box is known, and before the paid steps; `after_import` does not run it because it uses the network;
  aliases `county_records` and `county`)
  (old names visual_fetch/visual_classify still work as aliases);
  `run_step(conn, market_slug, step, limit=None, dry_run=False, confirm=False, progress=None) -> dict`
  (drive, rent, photos, classify and calibrate re-score right after a real run; export honours dry_run);
  `run_all(...)`; `estimate(conn, market_slug, limit=None) -> dict` (dry run of every paid step with cost).
- `bluemoon/jobs.py`: background thread runner. `start_job(kind, market_slug, params) -> int`,
  `get_job(job_id) -> dict`, `list_jobs(limit=20)`. Progress written to `jobs`.
- `bluemoon/exports.py`: `export_market(conn, market_slug, out_dir=None, only=None, stamp_handoffs=True) ->
  dict[str, Path]` (`ranked_list.xlsx`, `ranked_list.csv`, `excluded_<reason>.csv` incl. `excluded_manual.csv`,
  `crm_handoff.csv`, `mailing_list.csv`, `drill_worklist.csv`, `run_summary.md`). Property lists include "Why
  this score", "Photo reasons" (followed by "Building style (AI)", "Stories (AI estimate)", "Buildings seen (AI)", "Condition (AI)" and "Photo answer from") and, after "Why set aside": "Buy box decision", "Rules said", "Decided by",
  "Decided on", "Set-aside groups", and end with "Property ID" and "Row as of" (max of `properties.updated_at` and the
  last buy-box decision; the round trip's staleness guard). Workbooks carry metadata creator "Blue Moon Deal Finder".
  `export_review(conn, market_slug, bucket) -> Path`: one set-aside group as `output/<market>/review/review_<group>.xlsx`
  (sheet "Review": Address, City, then "Buy box decision" (a dropdown) and the editable columns, then the rest of
  ours, no Crexi columns; sheet "How to edit"). CSV cells that could run as a formula start with `'`; xlsx links
  are http(s) only.
- `run.py` CLI: `uv run run.py <command> [--market SLUG] [--limit N] [--dry-run] [--yes]` with commands
  `import <files...> [--force]` (a folder: oldest first, files already imported in any market skipped),
  `owners`, `drive`, `rent`, `parcels`, `photos`, `classify`, `score`, `drill`, `all`, `estimate`, `export`, `calibrate`,
  `serve` (a wildcard host without APP_PASSWORD runs on 127.0.0.1 only), `status`.

## Team link (`bluemoon/share.py`)

`run.py share` runs Cloudflare's free Quick Tunnel as a plain tunnel (`cloudflared tunnel --no-autoupdate --url
http://localhost:<port>`); it refuses to start without `APP_PASSWORD`. The dashboard does the sign-in itself
(`bluemoon/web/auth.py`): each visitor types their email and the team password, and the email must be on the
list in `data/share_emails.txt` (exact, or `*@company.com`). Cloudflare's `--allowed-mail` gate is not used: its
session cookie runs out seconds after login, so API calls get a cross-origin redirect to Cloudflare's login and
fetch() fails (cloudflared issue #1752). List changes apply at once (the cookie names the person and is checked
against the list on every request); last sign-ins are in `data/share_seen.json`. `run.py share password` makes and
saves the team password (`bluemoon-xxxx-xxxx-xxxx`); `run.py key google|anthropic|census` saves a key into `.env`
from a hidden prompt (`config.save_secret`). The live link is saved to `data/share.json` (with the pid) and shown by
`/api/settings` / `/api/health` (`share`), the Settings screen and `run.py status`. The web app refuses requests
from any non-local host name unless `APP_PASSWORD` is set. On the Mac itself (direct peer 127.0.0.1/::1, a
localhost Host and no `CF-Connecting-IP`/`CF-Ray`) no sign-in is needed (`share.on_this_computer`). Login throttling
uses `CF-Connecting-IP` only when the direct peer is this computer. `scripts/install_service.sh --share` runs it as
a second LaunchAgent (`com.bluemoon.share`).

## Web API (`bluemoon/web/app.py`, JSON)

All market routes are under `/api/m/{slug}`. Errors: `{"detail": "..."}` with 4xx.
Optional password: if `APP_PASSWORD` is set, every request (except on the Mac itself) needs cookie `bm_auth`
(POST `/api/login {email, password}`; the email is required and must be on the team list when the list has
people; 5 wrong passwords per device, then 429 with a growing wait). Without it: 401 `{detail, login, email}`
(`email` = the sign-in box should ask for an email). The web app fetches with `redirect: 'manual'` and shows
"Please sign in again" when a request is bounced to another site's sign-in page. Every request must use an allowed Host (IP address,
localhost, this computer's name, `*.ts.net`, `app.allowed_hosts`; else 400), writes with another website's Origin
get 403, and request bodies sent without a size (Transfer-Encoding) get 411. Every response carries a
Content-Security-Policy (`app.py CSP`): scripts only from this server's files (no inline scripts, so the login
page loads `/static/login.js`), styles from this server and Google Fonts (inline style attributes allowed), fonts
from fonts.gstatic.com, images from this server or data:/blob:, fetch() to this server only, no framing.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ok, version, keys:{google:bool, anthropic:bool, hud:bool, census:bool}, vision_backend, claude_cli:bool, auth_required, authenticated, signed_in_as, login_email, share}` |
| GET | `/api/markets` | `[{slug, name, asset_class, asset_label, geography, properties, in_buybox, confirmed}]` |
| GET | `/api/m/{slug}/summary` | `{market:{slug,name,geography,origin_label,origin_lat,origin_lng,max_drive_minutes}, asset_class:{key,label,size_metric,visual_enabled,rent_enabled,visual_question,target_word}, properties, in_buybox, blue_moon_10, needs_review, penetration, levels, levels_in_buybox, credits, set_aside:{total, several, multi, reasons:[{key,label,count,only,explain,still_aside,added,kept,waived,buckets:[{key,label,count,only,still_aside,added,kept,waived,clearable,estimated}]}], drive_estimated, drive_close_calls, close_call_minutes, max_drive_minutes, by_hand:{added,set_aside}}, last_import, layers:[{key,label,done,total,enabled,pct,note}] (owners, drive, rent, photos, photo_check, score, parcels, drill), drill_waiting, drill_waiting_detail:{total,validate,stages,basis}}` (drill_waiting = buy-box owners whose next step is reach-out or a hands-on stage) |
| GET | `/api/m/{slug}/properties?view=&q=&sort=&limit=&offset=&include_excluded=&bucket=` | `{total, items:[PropertyRow], review}`; `bucket` = one set-aside group (in or out of the buy box), `review:{bucket,reason,reason_label,label,explain,count,still_aside,added,kept,waived,clearable,only}` (waived = still set aside, but you said this rule doesn't count; only = still set aside for this reason alone, after the decisions; clearable = members with a decision the group's clear takes off) |
| POST | `/api/m/{slug}/buybox/decide` | body `{bucket, decision: add_only\|add_all\|keep\|clear, note?}` -> `{batch_id, changed, total, now_in, still_out:[{reason,label,count}], message, set_aside, in_buybox}` (re-scores first) |
| POST | `/api/m/{slug}/buybox/undo` | body `{batch_id}` -> same shape as decide |
| POST | `/api/m/{slug}/properties/{id}/buybox` | body `{decision: include\|exclude\|rules, reason?: '*'\|rule key, note?}` -> `{row: PropertyRow, buybox, message, batch_id, in_buybox}` |
| GET | `/api/m/{slug}/properties/{id}` | `PropertyDetail` |
| POST | `/api/m/{slug}/properties/{id}/attempt` | body `{stage_key, outcome, detail?, person?, person_role?, contacts?}` |
| POST | `/api/m/{slug}/properties/{id}/contacts` | body `{channel, value, source?, person_name?, notes?}` |
| PATCH | `/api/research/findings/{fid}` | body `{status: wrong\|kept}` -> mark an AI research finding wrong, or take that back (records who) |
| PATCH | `/api/contacts/{cid}` | body `{status, allow_contact?}` (taking a contact off do-not-contact needs `allow_contact: true`) |
| POST | `/api/m/{slug}/properties/{id}/outreach` | body `{channel, outcome, contact_point_id?, notes?}` |
| POST | `/api/m/{slug}/properties/{id}/level` | body `{action, person?, note?}` |
| POST | `/api/m/{slug}/properties/{id}/credit` | body `{result?}` |
| POST | `/api/m/{slug}/properties/{id}/review` | body `{label?, note?, agreed?}`: `{agreed: true}` = "Yes, the AI is right" (no label needed), `{agreed: false, label}` = "No, it's wrong"; plain `{label}` as before. Answer has `agreed`, `message` |
| POST | `/api/m/{slug}/properties/{id}/outcome` | body `{outcome, note?}` |
| GET | `/api/m/{slug}/drill?scope=buybox\|all` | drill overview (default `buybox`: owners of buy-box properties; `all`: everyone) |
| GET | `/api/m/{slug}/drill/queue/{stage_key}?limit=&offset=&scope=` | stage queue (`stage_key=validate` for reach-out queue; `scope` as above) |
| GET | `/api/m/{slug}/credits` | `{status, plan:[...], extras:[...], planned, planned_all, extra_count, extras_all, other_markets, others:[{slug,name,planned,extras}], shortfall, level1_open, message, log:[...]}` (credits are shared: *_all, shortfall and level1_open count every market) |
| GET | `/api/m/{slug}/review/queue?limit=&offset=&include_excluded=&scope=` | `{total, scope, counts:{total, scope, scopes:{everything, unsure}, needs_review, unsure, not_checked, no_photos, confirm, spot_check, reviewed}, items}`; `scope` = everything (default) or unsure |
| GET | `/api/m/{slug}/photos/month?estimate=` | the monthly photo goal: `{enabled, month, target, batch, done, remaining, reached, pct, pool:{buybox, rest, total}, ai_waiting, keep_going_to, message}`; with `estimate=1` also `estimate:{test, batch, keep_going}` each `{properties, photos, ai_checks, calls, usd, needs_confirm, ...}` (dry runs) |
| GET | `/api/m/{slug}/review/scorecard` | scorecard: confusion matrix, by-color accuracy, acceptance, plus `ai_right`, `by_label`, `learning` |
| GET | `/api/crexi/status` | `{status (credit_status), open_batches, topup:{suggested,message,close_first,batch_id}, default_count}` |
| GET | `/api/crexi/history` | `{periods:[{period,label,current,batches:[...],sent,returned,by_hand,not_returned,matched_pct,enrichment}], current}` |
| GET | `/api/crexi/batches/{id}` | batch counts and its items (`limit`, `offset`) |
| POST | `/api/crexi/export/preview` , `/api/crexi/export` | body `{count?, retry?, note?}`: who would be in the batch / write it |
| GET | `/api/crexi/batches/{id}/file` | the export CSV (looked up by id) |
| POST | `/api/crexi/import/preview` , `/api/crexi/import` | multipart `file`, `columns` (JSON), `adopt_hand` |
| POST | `/api/crexi/batches/{id}/close` , `/withdraw` | body `{confirm}`; without confirm only says what it would do |
| GET | `/api/crexi/imports/{id}/files/{unmatched\|unreadable\|reply}` | files an import wrote |
| GET | `/api/m/{slug}/properties/{id}/crexi` | that property's Crexi history and open alternates (also `crexi` in the property detail) |
| POST | `/api/m/{slug}/properties/{id}/alternates/{aid}` | body `{action: use\|ignore}` |
| GET | `/api/m/{slug}/funnel` | `{find:[{label,count}], reach:[{label,count}]}` |
| GET | `/api/m/{slug}/analytics` | `{imports:[...], filter_funnel:[...], coverage:[{field,pct}], counties:[...], size_dist:[...], score_dist:[...], owner_types:[...], levels:[{key,level,label,count,in_buybox}], api_usage:[...]}` |
| GET | `/api/m/{slug}/model` | score model summary (weights, learned, outcomes count) |
| GET | `/api/sources` | prioritized source catalog + counties |
| POST | `/api/m/{slug}/import/preview` | multipart `file` -> columns, sample, `key_fields`, `size_metric`, `type_mismatch`; a Blue Moon export: `{provider: 'bluemoon', message, edits:{properties_changed, fields_changed, decisions_changed, skipped_stale, unknown_ids, other_market, conflicts, changes}, warnings}` |
| POST | `/api/m/{slug}/import` | multipart `file` -> import result (then owners/score/drill auto-run; `queued`: the free rent check started in the background); edits from a Blue Moon export are saved with the signed-in person (see ingest round trip) |
| GET | `/api/m/{slug}/estimate?limit=` | dry-run estimate of every paid step |
| POST | `/api/m/{slug}/jobs` | body `{step, limit?, dry_run?, confirm?}` -> `{job_id}` |
| GET | `/api/jobs/{id}` / `/api/jobs` | job status |
| GET | `/api/m/{slug}/export/review.xlsx?bucket=` | one set-aside group to fix in Excel and import back (`exports.export_review`; 400 for an unknown group) |
| GET | `/api/m/{slug}/export/{name}` | file download (`ranked_list.xlsx`, `ranked_list.csv`, `crm_handoff.csv`, `mailing_list.csv`, `drill_worklist.csv`, `excluded.zip`, `run_summary.md`); a GET never changes data |
| POST | `/api/m/{slug}/export/crm_handoff.csv` | JSON body `{mark: true}`: the CRM list, marking the newly confirmed owners in it as handed off |
| GET | `/images/{property_id}/{file}` | cached image (local only) |
| GET | `/api/settings` | readable settings (no secrets) |

PropertyRow: `{id, address, city, state, zip, county, lat, lng, units, building_sqft, lot_acres, size_value,
size_basis, size_color, drive_minutes, drive_method, visual:{label, p, color, needs_review}, rent_fit,
owner_name, owner_type, years_owned, score, coverage_have, coverage_total, rank, in_buybox, exclusion_reasons,
rule_reasons, by_hand ('added'|'aside'|null), aside_buckets, level, drill_status, next_step_text, channels:{mail,phone,text,email,social,vm: none|untested|verified|wrong|dnc},
crexi_url}`.

PropertyDetail: PropertyRow + `{property:{all columns}, raw:{original columns}, signals:[...], visual_check:{...,
images:[url]}, rent:{...}, owner:{...}, drill: property_drill(...), links:[...], outcome, buybox:{in_buybox,
exclusion_reasons, exclusions, rule_reasons, rules, waived, kept, manual, decisions:[{reason, reason_label, decision,
bucket, note, decided_by, decided_at, detail_at_decision, detail_now}], history, groups, size_detail, ...}}`.

## Frontend (`bluemoon/web/static/`)

No build step. `index.html` loads `styles.css`, then `core.js`, then view modules. Design reference:
`docs/mockup.html` (tokens, type, components). `core.js` exposes:

```js
window.BM = {
  api(path, {method, body, form}) -> Promise<json>,   // prefixes nothing; throws Error(detail)
  state: {market: slug, markets: [...], summary: {...}},
  registerView(key, {label, order, nav: true|false, render: async (root, params) => void}),
  go(viewKey, params), openProperty(id), closeDrawer(), toast(msg), refreshSummary(),
  esc(str), fmt: {pct, num, money, minutes, date}, ui: {moon(p,size), levelPips(level), channelBadges(channels), bars(rows, opts), stat(v,label)}
}
```
Views: deals (list + drive-time rings), drill (overview + stage work queues with check-off), credits, funnel,
review (photo review with keyboard 1-5), analytics, score (model), sources, import, run (pipeline jobs with
estimates and confirm), exports, settings. Market switcher in the top bar. Hash routing `#view/param`.

Buy-box decisions in the browser: the Deals "Why?" panel lists `summary.set_aside.reasons[]` with their groups
(`buckets[]`); each group has Review (`#deals/review/<group key>`, which lists `GET properties?bucket=<key>`
under a `review` header) and Decide (choices add_only / add_all / keep / clear posted to `POST buybox/decide`;
the toast's Undo posts `buybox/undo` with the `batch_id`). Set-aside rows have Add to buy box, rows decided by
hand Undo (`POST properties/{id}/buybox` with include / rules); on phones these buttons sit beside the card
button, never inside it. The drawer's Buy box section (`property_detail.buybox`) shows each rule, the decisions
with who and when, the history, and Add to buy box / Set aside / Undo: follow the rules.


### Top menu and logo (2026-10-24)

The top bar is three group buttons (Work, Insights, Setup; `GROUPS` in core.js) that open one group's screens at a time (`aria-expanded`, Escape closes; the current screen's group opens on arrival). A group's badges show as a dot on its button. The logo files live in `static/brand/` (served by the existing `/static/` route; CSP `img-src 'self'` already covers them). No routes changed.
