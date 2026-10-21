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
                 web dashboard  +  exports (xlsx/csv for Brad, CRM handoff, mailing list)
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

## Rules (from CLAUDE.md)

- Never print, log or commit API keys. Read secrets only via `bluemoon.config.secret(name)`.
- Never delete rows that fail a filter: `scores.in_buybox=0` + `scores.exclusion_reasons`, exported to
  `output/<market>/excluded_<reason>.csv`.
- Cache every external API result (`bluemoon.cache.cached`) and record every billable call (`record_call`).
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

### imagery (`bluemoon/imagery/`)
- `streetview.py`: `imagery_estimate(conn, market_slug, limit=None) -> dict`;
  `fetch_imagery(conn, market_slug, limit=None, dry_run=False, progress=None, confirm=False) -> dict`.
  Street View metadata first (free; no pano within radius -> status no_imagery). Bearing pano->property;
  3 images at heading, heading±35 (fov 80, pitch 5, 640x640) + 1 satellite (Maps Static zoom 19).
  Saves to `images/{property_id}/sv_0.jpg, sv_1.jpg, sv_2.jpg, sat.jpg`. Only for asset classes with
  visual_check enabled and properties in the buy box (or all with `include_excluded=True`). Caps from config.
- `vision.py`: backends with `.name` and `.classify(image_paths: list[Path], prompt: str) -> str` —
  `ClaudeCLIBackend` (runs `claude -p` with `--output-format json`, the Read tool allowed for the image dir,
  ANTHROPIC_API_KEY removed from the child env so the Claude plan is used), `AnthropicAPIBackend`, `MockBackend`.
  `get_backend(name=None)`; `build_prompt(asset_class, examples) -> str`; `parse_result(text, labels) -> VisualResult`
  (pydantic; strict JSON; one retry on parse failure);
  `classify_properties(conn, market_slug, limit=None, dry_run=False, backend=None, progress=None, confirm=False) -> dict`.
  final_p = weight_vision*ai_p + (1-weight_vision)*(50+prior); NULL when image_quality poor/none or
  property_visible false (needs human review).
- `review.py`: `review_queue(conn, market_slug, limit=50) -> list[dict]` (final_p NULL or 21-79 first, then a
  random 5% spot-check of greens/reds); `save_review(conn, property_id, label, note=None) -> dict` (sets
  reviewed_label, final_p 100/0/60 by label; copies images into `labels/examples/` + appends
  `labels/examples.jsonl`; updates `labels/scorecard.json`); `examples_for(asset_class, k) -> list[dict]`;
  `scorecard(conn, market_slug) -> dict`; `calibrate(conn, market_slug, backend=None) -> dict`.

### scoring (`bluemoon/scoring/`)
- `buybox.py`: `evaluate(prop, enrich, asset_class, market, settings) -> dict`
  (`{in_buybox, exclusion_reasons:[...], size_value, size_basis, size_color, visual_color, rent_color}`).
  Exclusion reasons: drive_time, year, size, state, skip_county, property_type. Missing data never excludes.
  Size basis hierarchy: units+sqft > units > sqft-estimate > unknown.
- `blue_moon.py`: `signals_for(prop, enrich, asset_class, settings) -> list[dict]` items
  `{key, label, value, multiplier (None = skipped), why}`; `probability(signals, base_rate) -> float`.
- `rank.py`: `compute_scores(conn, market_slug, progress=None) -> dict` (writes `scores`, rank 1..n among
  in-buybox by: score desc, visual final_p desc, drive minutes asc).
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
- `credits.py`: `credit_status(conn) -> {month, per_month, used, left, used_today, days_left, daily_pace,
  today_left, ...}` (the pace comes from the credits left when the day began, so it holds all day; the plan gives
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

### pipeline + jobs + exports
- `bluemoon/pipeline.py`: `STEPS = ["import","owners","drive","rent","photos","classify","score","drill","all"]`
  (old names visual_fetch/visual_classify still work as aliases);
  `run_step(conn, market_slug, step, limit=None, dry_run=False, confirm=False, progress=None) -> dict`
  (drive, rent, photos, classify and calibrate re-score right after a real run; export honours dry_run);
  `run_all(...)`; `estimate(conn, market_slug, limit=None) -> dict` (dry run of every paid step with cost).
- `bluemoon/jobs.py`: background thread runner. `start_job(kind, market_slug, params) -> int`,
  `get_job(job_id) -> dict`, `list_jobs(limit=20)`. Progress written to `jobs`.
- `bluemoon/exports.py`: `export_market(conn, market_slug, out_dir=None, only=None, stamp_handoffs=True) ->
  dict[str, Path]` (`ranked_list.xlsx`, `ranked_list.csv`, `excluded_<reason>.csv`, `crm_handoff.csv`,
  `mailing_list.csv`, `drill_worklist.csv`, `run_summary.md`). Property lists include "Why this score" and
  "Photo reasons". CSV cells that could run as a formula start with `'`; xlsx links are http(s) only.
- `run.py` CLI: `uv run run.py <command> [--market SLUG] [--limit N] [--dry-run] [--yes]` with commands
  `import <files...> [--force]` (a folder: oldest first, files already imported in any market skipped),
  `owners`, `drive`, `rent`, `photos`, `classify`, `score`, `drill`, `all`, `estimate`, `export`, `calibrate`,
  `serve` (a wildcard host without APP_PASSWORD runs on 127.0.0.1 only), `status`.

## Web API (`bluemoon/web/app.py`, JSON)

All market routes are under `/api/m/{slug}`. Errors: `{"detail": "..."}` with 4xx.
Optional password: if `APP_PASSWORD` is set, every request needs cookie `bm_auth` (POST `/api/login {password}`;
5 wrong passwords per device, then 429 with a growing wait). Every request must use an allowed Host (IP address,
localhost, this computer's name, `*.ts.net`, `app.allowed_hosts`; else 400), writes with another website's Origin
get 403, and request bodies sent without a size (Transfer-Encoding) get 411. Every response carries a
Content-Security-Policy (`app.py CSP`): scripts only from this server's files (no inline scripts, so the login
page loads `/static/login.js`), styles from this server and Google Fonts (inline style attributes allowed), fonts
from fonts.gstatic.com, images from this server or data:/blob:, fetch() to this server only, no framing.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ok, version, keys:{google:bool, anthropic:bool, hud:bool, census:bool}, vision_backend, claude_cli:bool}` |
| GET | `/api/markets` | `[{slug, name, asset_class, asset_label, geography, properties, in_buybox, confirmed}]` |
| GET | `/api/m/{slug}/summary` | `{market:{slug,name,geography,origin_label,origin_lat,origin_lng,max_drive_minutes}, asset_class:{key,label,size_metric,visual_enabled,rent_enabled,visual_question,target_word}, properties, in_buybox, blue_moon_10, needs_review, penetration, levels, credits, last_import, layers:[{key,label,done,total}], drill_waiting, drill_waiting_detail:{total,validate,stages,basis}}` (drill_waiting = buy-box owners whose next step is reach-out or a hands-on stage) |
| GET | `/api/m/{slug}/properties?view=&q=&sort=&limit=&offset=&include_excluded=` | `{total, items:[PropertyRow]}` |
| GET | `/api/m/{slug}/properties/{id}` | `PropertyDetail` |
| POST | `/api/m/{slug}/properties/{id}/attempt` | body `{stage_key, outcome, detail?, person?, person_role?, contacts?}` |
| POST | `/api/m/{slug}/properties/{id}/contacts` | body `{channel, value, source?, person_name?, notes?}` |
| PATCH | `/api/contacts/{cid}` | body `{status, allow_contact?}` (taking a contact off do-not-contact needs `allow_contact: true`) |
| POST | `/api/m/{slug}/properties/{id}/outreach` | body `{channel, outcome, contact_point_id?, notes?}` |
| POST | `/api/m/{slug}/properties/{id}/level` | body `{action, person?, note?}` |
| POST | `/api/m/{slug}/properties/{id}/credit` | body `{result?}` |
| POST | `/api/m/{slug}/properties/{id}/review` | body `{label, note?}` |
| POST | `/api/m/{slug}/properties/{id}/outcome` | body `{outcome, note?}` |
| GET | `/api/m/{slug}/drill?scope=buybox\|all` | drill overview (default `buybox`: owners of buy-box properties; `all`: everyone) |
| GET | `/api/m/{slug}/drill/queue/{stage_key}?limit=&offset=&scope=` | stage queue (`stage_key=validate` for reach-out queue; `scope` as above) |
| GET | `/api/m/{slug}/credits` | `{status, plan:[...], extras:[...], planned, planned_all, extra_count, extras_all, other_markets, others:[{slug,name,planned,extras}], shortfall, level1_open, message, log:[...]}` (credits are shared: *_all, shortfall and level1_open count every market) |
| GET | `/api/m/{slug}/review/queue` | review queue |
| GET | `/api/m/{slug}/review/scorecard` | scorecard |
| GET | `/api/m/{slug}/funnel` | `{find:[{label,count}], reach:[{label,count}]}` |
| GET | `/api/m/{slug}/analytics` | `{imports:[...], filter_funnel:[...], coverage:[{field,pct}], counties:[...], size_dist:[...], score_dist:[...], owner_types:[...], levels:[{key,level,label,count,in_buybox}], api_usage:[...]}` |
| GET | `/api/m/{slug}/model` | score model summary (weights, learned, outcomes count) |
| GET | `/api/sources` | prioritized source catalog + counties |
| POST | `/api/m/{slug}/import/preview` | multipart `file` -> columns, sample, `key_fields`, `size_metric`, `type_mismatch` |
| POST | `/api/m/{slug}/import` | multipart `file` -> import result (then owners/score/drill auto-run; `queued`: the free rent check started in the background) |
| GET | `/api/m/{slug}/estimate?limit=` | dry-run estimate of every paid step |
| POST | `/api/m/{slug}/jobs` | body `{step, limit?, dry_run?, confirm?}` -> `{job_id}` |
| GET | `/api/jobs/{id}` / `/api/jobs` | job status |
| GET | `/api/m/{slug}/export/{name}` | file download (`ranked_list.xlsx`, `ranked_list.csv`, `crm_handoff.csv`, `mailing_list.csv`, `drill_worklist.csv`, `excluded.zip`, `run_summary.md`); a GET never changes data |
| POST | `/api/m/{slug}/export/crm_handoff.csv` | JSON body `{mark: true}`: the CRM list, marking the newly confirmed owners in it as handed off |
| GET | `/images/{property_id}/{file}` | cached image (local only) |
| GET | `/api/settings` | readable settings (no secrets) |

PropertyRow: `{id, address, city, state, zip, county, lat, lng, units, building_sqft, lot_acres, size_value,
size_basis, size_color, drive_minutes, drive_method, visual:{label, p, color, needs_review}, rent_fit,
owner_name, owner_type, years_owned, score, coverage_have, coverage_total, rank, in_buybox, exclusion_reasons,
level, drill_status, next_step_text, channels:{mail,phone,text,email,social,vm: none|untested|verified|wrong|dnc},
crexi_url}`.

PropertyDetail: PropertyRow + `{property:{all columns}, raw:{original columns}, signals:[...], visual_check:{...,
images:[url]}, rent:{...}, owner:{...}, drill: property_drill(...), links:[...], outcome}`.

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
