# PRD: Blue Moon Deal Finder (Crexi → enrich → rank → DRILL → verified owner contact)

Owner: Seth · Investor: Brad (Springfield, OH) · Status: v1.1 (supersedes the "Walk-Up Finder" v1 draft) · Decisions log: Section 18

---

## 1. Why we're building this

The tool exists to **reach property owners**. Two jobs:

1. **Spend the 500 monthly Crexi owner-contact credits well.** Rank properties so credits go to the owners most worth chasing. Credits do not roll over, so the plan spends all of them every month.
2. **Run the DRILL.** Take every property through sequential, increasingly targeted data processes, free sources first, until every owner on the list is confirmed by a real conversation. No record is abandoned until every source is exhausted.

Brad's first portal is apartments near Springfield, Ohio, ideally **outdoor walk-ups** (every unit's front door opens to the outside). The same app holds other asset classes and geographies (Section 3).

Success looks like: a ranked list Seth works top-down; a DRILL board that shows exactly what to do next for each owner; penetration (share of owners confirmed) climbing toward 100%; and a human only reviewing the photos the AI is unsure about.

Out of scope here: conversations after contact (the separate CRM), pricing and the Blue Moon strike price (a later, separate buildout from Brad's spreadsheet).

## 2. Brad's buy box (apartments, confirmed)

| Criterion | Value | Notes |
|---|---|---|
| Location | ≤ 60 min drive from downtown Springfield, OH | Configurable origin + minutes per market |
| Asset | Multifamily / apartments | |
| Units | **10–80 OK, 20–50 ideal** | Unknown unit count is never ruled out |
| Size evidence | units + sq ft > units > sq ft estimate > unknown | Sq ft estimate = building sq ft / 850 |
| Year built | 1970 or later | Blank year kept and flagged |
| Entry type | Outdoor access matters most | Section 4 |
| Rent | Sweet spot studio $800, 1BR ~$1,000, 2BR ~$1,200, 3BR $1,500 | 2BR favored; compared with HUD area rents |

## 3. Markets and asset classes

A **market** is one portal: an asset class plus a geography (`config.yaml: markets`). Every market has the same screens; a switcher in the top bar moves between them. Asset classes (`config/asset_classes.yaml`) set the size metric and ranges, the rent scale (if any), the photo-check question and labels, and score multipliers:

apartments · self-storage · warehouses · campgrounds & RV parks · mobile home parks · outdoor storage · raw land (more can be added in YAML).

## 4. Photo check (apartments: walk-up classification)

Use these exact labels in code and output.

| Label | Definition | Visual cues |
|---|---|---|
| `EXTERIOR_CORRIDOR` | Garden/motel style. Doors open onto open-air breezeways, exterior stairs, outdoor walkways. **Target.** | Exterior stair towers, open breezeway gaps, rows of doors along a railing, 2–3 stories, no lobby |
| `DIRECT_ENTRY` | Townhome/row style. Each unit has its own ground-level door. **Target.** | Repeating doors at grade, stoops, often 2 stories |
| `INTERIOR_CORRIDOR` | Shared lobby, enclosed hallways, doors inside. **Not wanted.** | Few central entrances, canopy, no upper-floor exterior doors, 4+ stories, elevator overrun |
| `MIXED` | Both types on site | Varies |
| `UNKNOWN` | Imagery missing or building not visible | Trees, fences, set-back complex, only leasing office visible |

Other asset classes define their own labels (self-storage: drive-up vs interior; warehouses: dock-high vs grade-level; RV and mobile home parks: operating park; outdoor storage: paved/gravel yard). Raw land has no photo check.

## 5. Scoring

**Colors per axis:** walk-up (green ≥ 80, yellow 21–79, red ≤ 20, N/A = human review), rent fit (in / near / out of Brad's scale), size (ideal / OK / outside / unknown).

**Blue Moon score** answers one question: *is this owner worth chasing?* It works like a forecast: start from a rare base rate (1.5%), then each signal multiplies the odds (owner type, years owned, debt, absentee owner, vacancy, condition, preforeclosure, size, walk-up, rent fit, drive time). **Missing data is skipped, never counted against a property**; a coverage meter shows how many signals were known. Multipliers live in YAML and are tuned from real outcomes (empirical-Bayes blend with the starting estimates).

**Ranking:** in-buy-box properties by Blue Moon score, then walk-up probability, then drive time. Rows that fail a filter are never deleted; they are marked with the reason and exported to `output/<market>/excluded_*.csv`.

**Decisions by hand:** the rules decide first; Seth (or a teammate) can add a set-aside property to the buy box, set one aside, or stop one rule counting for a whole group (Deals > Why? > Review / Decide). Every decision records who and when, survives re-scores and re-imports, and can be undone. Set-aside groups (drive minutes, size, decade built, skipped county, state, type) are defined in YAML.

## 6. Pipeline

Free steps run first; paid steps run last, only on what survives. Every external result is cached in SQLite so re-runs never re-pay.

1. **Import.** Seth exports Crexi Property Records (CSV or XLSX) and drops them in the app (or `data/crexi/`). Columns auto-map (fuzzy, overridable in `config.yaml`), addresses normalize, rows dedupe by parcel/APN else normalized address, and re-imports merge. Every original column is kept.
2. **Owners.** Owner type (individual, trust, estate, small LLC, corporation, portfolio, institutional, government, nonprofit), portfolio grouping by owner name and mailing address, years owned, debt status, absentee owner.
3. **Drive time.** Google Routes `computeRouteMatrix` from the market origin (approximate estimate when no key). Missing coordinates geocode via the free US Census geocoder first.
4. **Rent.** HUD Fair Market Rents (free HUD map service, no key; Small Area FMR by ZIP where available), compared with Brad's scale.
5. **Photos.** Street View metadata (free) → 3 Street View images aimed at the property + 1 satellite image. **First paid step.** Dry-run estimate first; explicit OK above 50 properties.
6. **Photo check.** Claude reads the photos (on Seth's Claude plan through Claude Code on the Mac Mini; the pay-per-use API is an optional switch). Rules prior 20%, photos 80%. Unsure results go to review.
7. **Review.** Fast review screen with one-key answers. Answers become labeled examples the AI reads next time; a scorecard tracks accuracy (`calibrate`).
8. **Score and rank.**
9. **DRILL** (Section 7), credit plan, CRM handoff.

## 7. The DRILL

DRILL = Data Resolution through Incremental List Liquidation. Industry: buy a list, skip trace once, connect with ~3%, abandon the rest. DRILL: route every failure to the next process until every owner is reached.

**Contact levels** (per property): **1 No owner data · 2 Possible owner** (owner data from records, not yet talked to) **· 3 Confirmed owner** (we talked; they confirmed). Level 3 hands the property to the CRM. Records alone never confirm an owner. Side states: wrong info (contact marked wrong, drill continues), exhausted (every stage tried), do not contact.

**Stages** (`config/drill.yaml`, in order, free first): records baseline (auto) → portfolio match (auto) → county auditor → rental registration (Ohio ORC 5323.02 includes an owner/agent phone) → LLC and trust lookup (Secretary of State) → recorder documents (deed/mortgage signers) → on-site and manager contacts (occupant and operator lookup for warehouses and outdoor storage) → AI web research (Claude Code on Seth's plan searches the web for the owner's business filing, officers and agent, website, business phone, email and social pages; runs as its own step, never as part of `all`) → web and social search → Crexi credit → court and probate records → letters → neighbors and local network → paid skip trace (off) → deep research. Each stage lists its sources, method, cost and a starting hit-rate guess that real results replace.

**Validation:** whenever a property has untested phone, text, email or social contact info, its next step is to reach out on it and log the outcome (no answer, left message, replied but not confirmed yet, wrong person or number, bounced, talked to owner and confirmed, do not contact). Do not contact is permanent and covers the owner's other properties.

**Work it:** a DRILL board with live penetration and a pipeline table (input, resolved, unresolved per stage), and a work queue per stage that shows one owner at a time with the right links pre-filled and buttons to check it off (found / nothing / skip). Hit rates by stage and owner type feed back into the order (free hands-on stages trade places by blended hit rate once they have enough results) and the credit plan (highest expected value: score x the credit hit rate for the owner type; one plan for the whole account across markets).

**Credits:** 500 per credit period (`crexi.reset_day`, 29: Oct 29 to Nov 28; no roll-over), spent in bulk: the app exports the next N best owners as a CSV, Seth uploads it to Crexi by hand, Crexi sends a file back, Seth imports it (preview first). Crexi charges one credit per row it matches (even with no phone or email) and nothing for 'Not Matched'. Credits used = returned + in flight (sent, waiting), so the 500 cannot be overspent; closing a batch (confirmed) gives the unreturned ones back and records Crexi: nothing in the DRILL. Replies are matched by Crexi link, APN, then address, never by our ids; contacts come in as not tested; incoming data never overwrites: the first record is kept, blanks are filled, contacts are unioned and differing values become alternates to use or ignore. History keeps every period, batch and import. The plan's order (highest expected value, buy box first, free stages first, fallbacks) picks the N.

**Compliance:** never automate Crexi or people-search sites (AI research blocks them three ways: WebFetch deny rules, a guard that adds them to every web search's blocked list, and a filter that drops any finding sourced from them). Everything AI research finds stays untested (level 2 at most) with the link where it was seen. Calls and texts to cell phones fall under telemarketing rules (TCPA, Do Not Call); the app flags DNC and keeps "do not contact" permanent.

## 8. Rent data

1. HUD Fair Market Rents / Small Area FMRs via HUD's free map service (no key). Baseline.
2. A downloaded HUD spreadsheet or the HUD USER API (free token) as alternatives.
3. Census ACS rent by bedroom (free key) and Zillow ZORI (free CSV) as optional context.
4. RentCast or other paid sources: **ask Seth before enabling.**

## 9. Review screen and feedback loop

- Queue: photo N/A and yellow results first, then a small random sample of greens/reds for spot checks.
- Card: the 4 images, AI label, confidence, reasons, Google Maps + Street View + Crexi links. Keys 1–5 answer.
- **No model training.** Corrections save to `labels/`: an example library (images + true label + one-line why) included in later AI calls (capped, most recent first), and a scorecard (confusion matrix, accuracy per color).
- `run.py calibrate` re-runs reviewed properties and reports accuracy. **Acceptance:** green ≥ 90% truly target; red ≥ 90% truly not.

## 10. Outputs

- `ranked_list.xlsx` + `.csv` for Brad (Google Sheets), with original columns plus drive minutes, labels, colors, score, rank, reasons, flags, links, owner, contact level, next DRILL step.
- `crm_handoff.csv` (confirmed owners), `mailing_list.csv` (letters), `drill_worklist.csv`, `excluded_*.csv` per filter, `run_summary.md` (counts per step and color, API calls, estimated cost, credit plan reach).

## 11. Cost and safety controls

- Dry-run estimate for every paid step; `--limit N` for tests (always test with `--limit 5` first).
- Hard caps per run in config. Backoff on rate limits; ~5 concurrent.
- Google free monthly usage is tracked per SKU; estimates show what would be billed.
- Keys in `.env` only, never printed or logged. Optional dashboard password.
- Ask before adding any paid service not listed in Section 12.

## 12. Tech stack and structure

- Python 3.11+, `uv`. `pandas`, `openpyxl`, `httpx`, `pydantic`, `python-dotenv`, `pyyaml`, `rapidfuzz`, `fastapi`, `uvicorn`, `anthropic` (optional backend), stdlib `sqlite3`.
- Dashboard: FastAPI backend + no-build vanilla-JS single-page app (replaces Streamlit; works on phone and computer). Team access through Cloudflare's free Quick Tunnel (`run.py share`): a public https link; each listed person signs in with their own email and the team password (the dashboard checks both). No account or domain; the link changes when sharing restarts.
- Photo check and AI owner research: Claude Code on the Mac Mini (`claude -p`, Seth's plan; research sends owner names, cities and property addresses to Anthropic, never mailing streets). Optional: Anthropic API (`claude-sonnet-5-5`).
- Google APIs (key `GOOGLE_MAPS_API_KEY`): Geocoding, Routes, Street View Static, Maps Static.
- Free: HUD map service, US Census geocoder, county and state public-record sites (links), and the counties' free public parcel map services (ArcGIS REST, no key), which the County records step now uses automatically (`config/parcels.yaml`): Franklin, Greene, Delaware, Champaign, Madison, Logan, Fayette, Clinton, Shelby, Clark and Union; any other Ohio county uses the older (2023-24) statewide map data (ODNR owners, OGRIP mailing addresses). Miami's service is switched off (not published as open data). Indiana has no source. Never automated: the auditor web pages themselves (links only).
- CLI: `uv run run.py import | owners | drive | rent | parcels | photos | classify | score | drill | all | estimate | export | calibrate | serve | status` with `--market`, `--limit`, `--dry-run`, `--yes`.
- Layout: see `ARCHITECTURE.md`. Package `bluemoon/`. Data in `data/` (SQLite `data/bluemoon.sqlite`), photos in `images/`, labels in `labels/`, outputs in `output/` — all git-ignored.

## 13. Open questions

Answered by Brad: units 10–80 (20–50 ideal), rent scale studio $800 to 3BR $1,500, walk-up matters most.
Still open: minimum check size; edge counties and Wayne County, IN; instant no's; what exactly "10k NRSF" should mean when units are unknown (now: sq ft / 850 as an estimate that never excludes).

## 14. Geography

Default: the 60-minute drive filter from Springfield does the real work; counties guide the Crexi export.

- **Ohio Core:** Clark, Champaign, Greene, Madison, Montgomery, Miami, Logan, Union, Clinton, Fayette, Shelby, Franklin, Warren, Preble, Auglaize
- **Ohio Edge (partial):** Darke, Delaware, Pickaway, Butler, Hardin, Highland, Hamilton
- **Ohio Mostly over:** Allen, Marion, Ross
- **Indiana:** Wayne (Core); Union, Randolph (Mostly over)
- **Skip:** OH Clermont, Fairfield, Licking, Mercer, Morrow; IN Fayette, Franklin, Henry, Jay, Dearborn

## 15. Design

Calm, minimal, effective: one accent color, semantic green/yellow/red only where it means something, big numbers only where they are the point. Deals list with moon glyph scores and drive-time rings; a detail panel with the score explanation, photos, owner and DRILL timeline; a DRILL board with work queues; a fast review queue. Reference: `docs/mockup.html`.

## 16. Out of scope (v1)

Crexi scraping or automated login; automating people-search sites; automated outreach or CRM sync; underwriting and pricing; a hosted cloud app.

## 17. Build order

1. Foundation: config, database, markets and asset classes
2. Import + column mapping + dedupe/merge
3. Owners, drive time, rent
4. Scoring, buy box, ranking
5. DRILL engine, links, credits, stats
6. Photos + photo check + review + calibrate
7. Pipeline, jobs, exports, CLI
8. Dashboard API + web app
9. Data-source catalog (docs/DATA_SOURCES.md, config/sources.yaml)
10. README and Mac Mini setup for a non-developer

## 18. Decisions log

| Date | Decision |
|---|---|
| 2026-10 | Goal is owner contact (credits + DRILL); pricing is a later, separate buildout |
| 2026-10 | Three contact levels; only a conversation confirms an owner |
| 2026-10 | One app, many markets (asset class + geography), same screens |
| 2026-10 | Custom web dashboard instead of Streamlit |
| 2026-10 | Team access through Cloudflare's free Quick Tunnel with email codes (Tailscale's free plan is non-commercial only; a domain for a permanent link is an optional ~$10/year upgrade) |
| 2026-10 | Team sign-in moved into the dashboard (email on the team list + one team password). Cloudflare's email-code gate logs people out seconds after sign-in (cloudflared issue #1752), which broke every screen on the team link |
| 2026-10 | Photo check runs on Seth's Claude plan via Claude Code; API optional |
| 2026-10 | HUD rents from HUD's free map service (HUD's download site blocks scripts) |
| 2026-10 | Package renamed `walkup/` → `bluemoon/` because the scope grew past walk-ups |
| 2026-10 | Private data never goes to GitHub; it lives on the Mac Mini |
| 2026-10 | Buy-box decisions by hand (add, set aside, stop a rule counting for a group) with Review/Decide on every set-aside group; the rules stay the default |
| 2026-10 | AI web research as a DRILL stage on Seth's Claude plan (business filings, officers, website, business phone/email, social pages), people-search sites blocked, results untested |
| 2026-10 | Blue Moon exports can be fixed in Excel and imported back (whitelisted columns only, stale rows skipped, corrections survive later Crexi imports) |
