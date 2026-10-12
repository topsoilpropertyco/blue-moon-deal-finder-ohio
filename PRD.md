# PRD: Walk-Up Finder (Crexi → Filters → Street View Classifier → Ranked Owner List)

Owner: Seth · Investor: Brad (Springfield, OH) · Status: v1 build doc, settings TBD pending Brad's answers (Section 13)

---

## 1. Why we're building this

Brad wants apartment buildings near Springfield, Ohio, ideally **outdoor walk-ups**: buildings where every unit's front door opens to the outside. Crexi has plenty of property data but only **500 owner-contact credits per month**. The real job of this tool is to decide **where those 500 credits go**, and to flag the good leads Crexi has no owner data for so we can source them elsewhere.

Success looks like: a ranked list Seth can work top-down, where the top of the list really is close, the right size, in the right rent market, and walk-up, with a human only reviewing the photos the AI isn't sure about.

## 2. Buy box (as remembered; confirm with Brad)

| Criterion | Value | Notes |
|---|---|---|
| Location | ≤ 60 min drive from downtown Springfield, OH | Configurable origin + minutes |
| Asset | Multifamily / apartments | |
| Year built | 1970 or later | Keep blank year-built rows, flag `year_unknown` |
| Entry type | Exterior-entry strongly preferred | See Section 4 |
| Rent | ~$1,000 market rent | **Ambiguous:** 1BR or 2BR? Floor or ceiling? Keep configurable |
| Units / price | TBD | Config filters, default off |

## 3. Who uses it and how

- **Seth (operator):** drops Crexi CSVs in a folder, runs one command, reviews uncertain photos in a review screen, spends Crexi credits top-down on the ranked list.
- **Brad (viewer):** gets the ranked list as a spreadsheet (v1: Seth uploads the .xlsx to Google Sheets manually).

## 4. Classification definitions

Use these exact labels in code and output.

| Label | Definition | Visual cues |
|---|---|---|
| `EXTERIOR_CORRIDOR` | Garden/motel style. Doors open onto open-air breezeways, exterior stairs, outdoor walkways. **Target.** | Exterior stair towers, open breezeway gaps, rows of doors along a railing, 2–3 stories, no lobby |
| `DIRECT_ENTRY` | Townhome/row style. Each unit has its own ground-level door. **Target.** | Repeating doors at grade, stoops, often 2 stories |
| `INTERIOR_CORRIDOR` | Shared lobby, enclosed hallways, doors inside. **Not wanted.** | Few central entrances, canopy, no upper-floor exterior doors, 4+ stories, elevator overrun |
| `MIXED` | Both types on site | Varies |
| `UNKNOWN` | Imagery missing or building not visible | Trees, fences, set-back complex, only leasing office visible |

Targets together = **exterior-entry**.

## 5. Scoring: three axes, four colors

Every property gets a color on each axis.

| Axis | Green | Yellow | Red | N/A |
|---|---|---|---|---|
| **Walk-up** | `p_exterior` ≥ 80 | 21–79 | ≤ 20 | No usable photo → human review |
| **Rent market** | County/zip rent inside the configured band | Within a configurable margin of the band | Outside | No rent data |
| **Size** | Meets min units / min value | Within margin | Below | Missing data |

Also keep the finer walk-up bucket for sorting: 1 (≥80), 2 (55–79), 3 (21–54 or poor imagery), 4 (≤20).

**Ranking:** count of greens, then walk-up `p_exterior` desc, then drive time asc. Axis weights are configurable so Brad's answer to "rank walk-up vs rent vs size" can be plugged in.

## 6. Pipeline (9 steps)

Cheap/free steps run first; paid steps run last, only on what survives. Every step writes to a local SQLite cache keyed by `property_id`, so re-runs never re-pay and every step is resumable.

1. **Export (manual).** Seth exports Crexi Property Records CSVs into `data/crexi/` (see `CREXI_EXPORT_SOP.md`).
2. **Clean.** Load all CSVs, auto-map columns (fuzzy, case-insensitive, confirm/override in `config.yaml`), normalize addresses, dedupe by parcel/APN else normalized address, assign `property_id`. Pass every other column through untouched.
3. **Drive time.** Geocode rows missing lat/lng (record `geocode_quality`, flag approximate). Routes API `computeRouteMatrix` from origin. Over the limit → `excluded_drive_time.csv`, not deleted.
4. **Size.** Apply year-built filter (<1970 → `excluded_year.csv`, blanks kept and flagged) and optional min units / min value. Ohio assessed value = 35% of market, so multiply by ~2.86 for OH; Indiana assessed ≈ market. Fails → `excluded_size.csv`.
5. **Rent.** Join rent market data (Section 8). Score rent axis.
6. **Walk-up.** Rules-based prior, then Street View + satellite + vision model (Section 7). **First paid step.**
7. **Review.** Streamlit review screen for N/A and yellow walk-up results (Section 9).
8. **Rank.** Combine axes, sort, write outputs (Section 10).
9. **Owners.** Ranked enrichment list: Seth spends Crexi credits top-down; tool tracks status and flags gaps for other sources (county auditor, Ohio Secretary of State for LLC agents, skip tracing). No Crexi automation.

## 7. Walk-up classifier detail

**Rules-based prior** (mild, ±10–20 points, weights in config; imagery dominates when clear):
- stories ≥ 4 → lean `INTERIOR_CORRIDOR`
- stories ≤ 3 and built 1970–1995 → lean `EXTERIOR_CORRIDOR`
- units ≤ 12 → lean exterior-entry

**Imagery:**
- Street View **metadata** call first (free). No pano within ~50 m → `no_streetview`.
- Compute bearing from pano to property. Fetch 3 images: heading = bearing, ±35°; FOV 80; pitch 5; 640×640.
- One satellite image via Maps Static, zoom 19, 640×640.
- Save to `images/{property_id}/`.

**Vision call:** all images in one request. Include the labeled example library (Section 9) as few-shot context. Strict JSON, validate with pydantic, retry once on parse failure.

Prompt starting draft:

> You are classifying an apartment property for a real estate investor. You'll see up to 3 Street View photos aimed at the property and 1 overhead satellite image. Determine how residents reach their unit front doors.
>
> Categories: EXTERIOR_CORRIDOR (doors on open-air breezeways, exterior stairs, outdoor walkways), DIRECT_ENTRY (each unit has its own ground-level door), INTERIOR_CORRIDOR (shared entrance into enclosed hallways), MIXED, UNKNOWN.
>
> The photo may show a leasing office, a neighbor, or a commercial building. Only judge the residential buildings. If you can't tell which building is the property, say so.
>
> Return only JSON: `{"label": "...", "p_exterior": 0-100, "image_quality": "good|partial|poor|none", "visible_cues": ["..."], "reasons": "one or two sentences", "property_visible": true|false}`

**Final score:** 80% vision / 20% prior (configurable). Poor/no imagery or `property_visible: false` → walk-up = N/A.

## 8. Rent data

Crexi exports won't have rent. Sources, in order:
1. **HUD Fair Market Rents API** (free, every county, by bedroom count). Baseline for v1.
2. **Zillow Observed Rent Index** by zip (free CSV download). Optional finer layer.
3. **RentCast** per-address estimates. Free tier, then paid. **Ask Seth before enabling.**

Config: `rent.bedrooms` (1 or 2), `rent.target` (1000), `rent.mode` (floor | ceiling | band), `rent.margin`.

## 9. Review screen and feedback loop (Streamlit)

- Queue of properties needing review (walk-up N/A and yellow first, then a random sample of greens/reds for spot checks).
- Each card: the 4 images, label, confidence, reasons, Google Maps + Street View + Crexi links.
- Buttons: **Correct · Wrong (choose true label) · Can't tell**. Keyboard shortcuts.
- **No model training.** Corrections save to `labels/`:
  - A growing **example library** (images + true label + one-line why) the vision prompt reads before each guess. Cap the number of examples sent per call; pick the most similar/recent.
  - A **scorecard** of misses (confusion matrix, accuracy per bucket) updated after each review session.
- `python run.py calibrate` re-runs reviewed properties and reports accuracy.
- **Acceptance:** green walk-up ≥ 90% truly exterior-entry; red ≥ 90% truly not.

## 10. Outputs

- `output/ranked_list.xlsx` (+ .csv): original columns + `drive_minutes`, `label`, `p_exterior`, `walkup_color`, `rent_color`, `size_color`, `rank`, `reasons`, `visible_cues`, `flags`, map/Street View/Crexi links, owner name/mailing from Crexi, and **`owner_contact_status`** (blank | crexi_credit_used | not_in_crexi | sourced_elsewhere) for Seth to fill.
- `output/excluded_*.csv` for each filter.
- `output/run_summary.md`: counts per step and color, API call counts, estimated cost, credits plan (how far down the list 500 credits reach).

## 11. Cost and safety controls

- `--dry-run`: run steps 2–5, print how many paid calls steps 6+ would make and estimated cost.
- `--limit N` for testing.
- Hard caps in config on images and vision calls per run.
- Backoff on rate limits; ~5 concurrent.
- Keys in `.env` only, never printed or logged; `.env` in `.gitignore`.
- Ask before adding any paid service.

## 12. Tech stack and structure

- Python 3.11+, `uv` (or pip + requirements.txt)
- `pandas`, `openpyxl`, `httpx`, `pydantic`, `python-dotenv`, `pyyaml`, `anthropic`, `streamlit`, `rapidfuzz`, stdlib `sqlite3`
- Vision model: Anthropic API, default `claude-sonnet-5-5`, configurable
- Google APIs (Seth's existing project, key `GOOGLE_MAPS_API_KEY`): Geocoding, Routes, Street View Static, Maps Static
- CLI: `python run.py clean | drive | size | rent | walkup | rank | all | calibrate` with `--limit`, `--dry-run`, `--config`
- Review: `streamlit run review_app.py`

```
walkup-finder/
  PRD.md  CLAUDE.md  CREXI_EXPORT_SOP.md  README.md
  .env  .env.example  config.yaml
  data/crexi/        # Crexi CSVs go here
  images/            # cached imagery per property
  labels/            # example library + scorecard
  output/            # ranked list, exclusions, run summary
  cache.sqlite
  run.py  review_app.py  walkup/  (package modules)
```

## 13. Open questions (settings, not blockers)

Ask Brad:
1. Rank walk-up vs rent vs size.
2. Minimum check size.
3. Minimum units.
4. $1,000 rent: 1BR or 2BR? Floor or ceiling?
5. Edge counties and Wayne County, IN: yes/no.
6. Any instant no's.

Seth to check: which columns the Crexi export actually has (lat/long, year built, units, stories, value, APN, owner name, owner mailing) and total lead count.

## 14. Geography

Default: 60-minute drive filter from Springfield does the real work; counties guide the Crexi export.

- **Ohio Core:** Clark, Champaign, Greene, Madison, Montgomery, Miami, Logan, Union, Clinton, Fayette, Shelby, Franklin, Warren, Preble, Auglaize
- **Ohio Edge (partial):** Darke, Delaware, Pickaway, Butler, Hardin, Highland, Hamilton
- **Ohio Mostly over:** Allen, Marion, Ross
- **Indiana:** Wayne (Core); Union, Randolph (Mostly over)
- **Skip:** OH Clermont, Fairfield, Licking, Mercer, Morrow; IN Fayette, Franklin, Henry, Jay, Dearborn

## 15. Design direction (placeholder)

Pending Mobbin pull. For now: review screen modeled on fast triage queues (Linear/Airtable: big image, status chips in green/yellow/red/gray, one-key decisions); ranked list modeled on record-filtering tools (Reonomy/Crexi Intelligence) with optional map view (Zillow/Redfin map + list).

## 16. Out of scope (v1)

Crexi scraping or automated login; automated skip tracing, outreach, CRM sync; underwriting; hosted web app.

## 17. Build order

1. Clean: ingest, column mapping, dedupe (test on a sample CSV)
2. Drive time + dry-run cost estimate
3. Size + year filters
4. Rent (HUD FMR)
5. Walk-up imagery fetch with caching
6. Vision classification + JSON validation
7. Rank + outputs + run summary
8. Streamlit review screen + example library + scorecard
9. Calibrate command
10. Owners: enrichment list + status column
11. README for a non-developer
