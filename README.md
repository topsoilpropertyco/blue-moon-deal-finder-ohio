# Blue Moon Deal Finder

Ranks the properties in a Crexi export so the 500 monthly Crexi owner-contact credits go to the best owners, and runs the **DRILL**: free sources first, one step at a time, until every owner is confirmed by a real conversation. Confirmed owners go to the CRM.

Everything runs on the Mac Mini. Private data (Crexi exports, owner names, photos, results, the database) stays there and never goes to GitHub.

- What it does and why: [PRD.md](PRD.md)
- How to work the DRILL: [docs/DRILL.md](docs/DRILL.md)
- Every free data source, in priority order: [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)
- How the code fits together: [ARCHITECTURE.md](ARCHITECTURE.md)

## What's inside

| Screen | What it's for |
|---|---|
| **Deals** | Every property ranked by Blue Moon score (how worth chasing the owner is), with contact level, next DRILL step, size, drive time, photo check, rent fit and owner type. List or drive-time rings. |
| **DRILL** | Penetration (owners confirmed), the stage-by-stage pipeline, and work queues: one owner at a time, links already filled in, keys to check it off. |
| **Credits** | Crexi credits left this month and which owners to spend them on. |
| **Photo review** | The photos the AI wasn't sure about. Answer with number keys. |
| **Funnel, Analytics, The score** | How the list narrows, what's in the data, how the score works and learns. |
| **Import, Run, Exports, Data sources, Settings** | Add Crexi files, run steps (cost estimate first), download files for Brad and the CRM, the free source list, settings. |

The switcher in the top bar moves between markets: apartments, self-storage, warehouses, campgrounds and RV parks, mobile home parks, outdoor storage and raw land. Every market has the same screens.

## 1. First-time setup (once)

Open Terminal on the Mac Mini and paste one block at a time. First, download the app:

```bash
cd ~
git clone https://github.com/topsoilpropertyco/blue-moon-deal-finder-ohio.git
cd blue-moon-deal-finder-ohio
```

Then install it:

```bash
scripts/setup_mac.sh
```

That installs the Python packages and creates `.env`, your private settings file. Open it and paste your Google Maps key after `GOOGLE_MAPS_API_KEY=`:

```bash
open -e .env
```

Without a Google key the app still works: drive times are free straight-line estimates and photos are off. In Google Cloud Console, turn on the **Routes API**, **Geocoding API**, **Street View Static API** and **Maps Static API** for the key.

## 2. Start the dashboard

```bash
uv run run.py serve
```

Open http://localhost:8765. To keep it running in the background (it starts whenever you log in to the Mac Mini and restarts itself if it stops):

```bash
scripts/install_service.sh
```

So it also comes back after a power cut or update restart, turn on automatic login: System Settings > Users & Groups > Automatically log in as > your name. If FileVault is on, macOS hides that option, so after a restart log in once at the Mac.

**Share it with your team (free):** Cloudflare's free link puts the dashboard on the internet as `https://<random-words>.trycloudflare.com`, with no account, no domain and nothing to install for your team. Your data stays on the Mac Mini. Install Cloudflare's connector once, add the people who may get in, and start sharing in the background:

```bash
scripts/install_cloudflared.sh
uv run run.py share add pat@example.com
scripts/install_service.sh --share
```

Each person opens the link, types their email and gets a one-time code (`*@yourcompany.com` lets in everyone at that company). With nobody on the list, the link asks for the dashboard password (`APP_PASSWORD` in `.env`, four or more random words) instead. Without either it refuses to start. The current link is on the **Settings** screen and in `uv run run.py share list`. It changes whenever sharing restarts (for example after the Mac restarts), so send the new one when that happens. To add or remove someone: `uv run run.py share add name@example.com` or `share remove name@example.com`, then restart sharing with `launchctl kickstart -k gui/$(id -u)/com.bluemoon.share` (the link changes). The dashboard itself only listens on this computer; outside visitors reach it only through the link, and only with the email code or password.

To restart the background service after a settings change:

```bash
launchctl kickstart -k gui/$(id -u)/com.bluemoon.dealfinder
```

## 3. Import a Crexi export

In Crexi, export **Property Records** as CSV or Excel. Then either:

- **Dashboard:** pick the market in the top bar, open **Import**, drop the file. Check the preview (columns matched, first rows), then **Import**. Use **Import the first 5 only (test)** the first time.
- **Terminal:** put the file in `data/crexi/` and run `uv run run.py import data/crexi/yourfile.xlsx --limit 5`, then without `--limit`. `uv run run.py import` on its own imports only the files in `data/crexi/` it hasn't seen before (in any market), oldest first, so the newest export's owner data wins; `--force` imports them all again.

Re-importing is safe: matching properties are updated, nothing is deleted or duplicated. Owners, free drive estimates, scores and the DRILL's automatic steps run right after every import; a dashboard import also starts the free HUD rent check in the background (`rent: after_import` in `config.yaml`). If the file's Property Type doesn't fit the market (apartments dropped into self-storage), the preview says which market it belongs in before you import. If a column is matched wrong, set it in `config.yaml` under `crexi: column_overrides` (for example `units: "Number of Units"`).

## 4. Run the steps (Run screen or Terminal)

The **Run** screen has a card per step with **Run**, **Test on 5** and **Estimate**. Paid steps over 50 properties stop with **Waiting for your OK** and show the calls and cost; press **Yes, spend it** to go ahead. **Run everything** runs all the free steps and stops before each big paid step. You can leave the screen while a job runs.

The same from the Terminal (`--market` can be left out for apartments; `self-storage`, `warehouses` and so on pick another market):

```bash
uv run run.py estimate            # what every step would do and cost; nothing is called
uv run run.py all --limit 5       # test run
uv run run.py all                 # everything; paid steps over 50 properties ask "Go ahead? [y/N]"
uv run run.py rent                # one step: owners | drive | rent | photos | classify | score | drill
uv run run.py export              # files for Brad in output/<market>/ (--dry-run: say what it would write)
uv run run.py status              # where every market stands
uv run run.py calibrate           # re-check the photos you reviewed; updates the Scorecard (90% check)
```

| Step | What it does | Cost |
|---|---|---|
| owners | Owner type (mom-and-pop or institutional), portfolio, years owned, loan, absentee | Free |
| drive | Drive minutes from the market's starting point | Google Routes (free estimate without a key) |
| rent | HUD Fair Market Rents by ZIP compared with Brad's rent scale | Free (HUD's map service) |
| photos | 3 Street View photos and 1 satellite photo per buy-box property | Google, after the free monthly calls |
| classify | Claude reads the photos (your Claude plan through Claude Code) | No per-call bill |
| score | Buy box, colors, Blue Moon score and rank | Free |
| drill | DRILL automatic stages and contact levels | Free |
| calibrate | Re-checks every property you answered in Photo review and updates the Scorecard | Your Claude plan |

Running drive, rent, photos or classify on its own updates the buy box, scores and rank right after, so the photos are bought for the right properties. Google drive times stop at `drive: max_elements_per_run` (10,000) per run; the rest wait for the next run. Every answer from Google, HUD and Claude is saved, so re-runs don't pay twice. Exit codes: 0 done, 1 problem, 3 waiting for your OK.

## 5. Work it in the dashboard

- **Deals:** the ranked list. Work from the top. Tap a property for its panel: the score explained, photos, rent, owner, the DRILL timeline and every original Crexi column. **Drive-time rings** shows the same list as a map of drive time.
- **DRILL:** start with **Reach out** (owners with a phone or email to test; "Talked to owner, confirmed" moves them to level 3), then **Work this** on the first stage with owners waiting. One owner at a time, links already filled in. Keys: F found, N nothing, R matches records (County auditor), X not applicable (saved; the owner moves to the next stage), J or S pass for now (nothing saved), K previous, O open the first link. The board counts buy-box owners; switch to **Everyone** to include set-aside properties. Do-not-contact is permanent: it covers the owner's other properties (same owner name or portfolio) and the same phone, email or mailing address everywhere, and only **Allow contact again** takes a contact off the list. A stage that keeps working for an owner type moves up for that owner type once it has 10 results (`learn_order` in `config/drill.yaml`).
- **Credits:** one plan for the whole Crexi account (credits are shared by every market), with how many of the 500 are left and the daily pace to spend them all. Owners rank by score x Crexi's hit rate for their owner type; owners with an untested phone wait in Reach out first. If the possible owners can't use every credit, the rest go to buy-box owners with no name yet, then the best owners just outside the buy box (`crexi: plan_fallbacks`). Use one in Crexi, then press **Used a credit** and record what Crexi showed.
- **Photo review:** the photos the AI was unsure about. Press 1-5 to answer, S to skip. **Scorecard** shows whether the AI passes the 90% check.
- **Funnel, Analytics, The score:** how the list narrows, what's in the data, and how the Blue Moon score works. Mark how deals ended ("How did it end?" in the property panel) and the score tunes itself.
- **Exports:** `ranked_list.xlsx` for Brad (Google Sheets: File > Import > Upload), plus the CSVs. The ranked list explains each score ("Why this score") and the photo check ("Photo reasons"). `mailing_list.csv` holds buy-box owners whose mailing address is untested plus any owner whose next step is a letter, never a do-not-contact or wrong address. Getting `crm_handoff.csv` from the Exports screen marks those owners as handed off (a plain link to the file changes nothing).
- **Settings / Data sources:** read-only settings, which keys are set (yes or no only), and every free source by DRILL stage, with the county links.

## What it costs

| Thing | Cost |
|---|---|
| HUD rents, Census geocoding, county and state record links | Free |
| Google drive times, Street View and satellite photos | Free up to Google's monthly allowance (about 10,000 of each); a few dollars beyond. Every paid run shows the estimate first. |
| Photo check | Your Claude plan through Claude Code on the Mac Mini; no per-call bill. If the plan hits a usage limit, the run stops cleanly and resumes next time. |
| Crexi | Your existing plan (500 owner-contact credits a month) |

## 6. Markets and asset classes

A market is one asset class in one area: apartments, self-storage, warehouses, campgrounds and RV parks, mobile home parks, outdoor storage and raw land around Springfield are set up. Every market has the same screens; switch in the top bar. Add a market in `config.yaml` under `markets:`; buy boxes, photo questions and score weights per asset class are in `config/asset_classes.yaml`.

## 7. Settings

All thresholds and weights live in YAML, never in code:

- `config.yaml`: markets and starting points, Crexi credits, drive, Google prices and caps, photo check backend, rent source, safety limit (50), dashboard options (`web:`).
- `config/asset_classes.yaml`: buy box, rent scale, photo labels and score multipliers per asset class.
- `config/drill.yaml`: the DRILL stages, their order and starting hit rates, and the reach-out outcomes.
- `config/sources.yaml`: data-source links and the county table.

Restart the dashboard after editing them.

## Update the app

```bash
cd ~/blue-moon-deal-finder-ohio
git pull
uv sync
launchctl kickstart -k gui/$(id -u)/com.bluemoon.dealfinder
```
(The last line restarts the background service; if you run `uv run run.py serve` by hand instead, stop it with Control-C and start it again.)

## Back it up

Your data lives in the app folder: `data/` (the database and Crexi files), `images/`, `labels/` and `output/`. Time Machine backs them up while a backup disk is connected.

## If something goes wrong

| Problem | Fix |
|---|---|
| Drive times show "~" and photos are off | No Google key yet: add `GOOGLE_MAPS_API_KEY` to `.env` and restart. |
| Google refuses the key | Turn on Routes, Geocoding, Street View Static and Maps Static APIs for that key in Google Cloud Console. |
| Photo check says Claude Code isn't signed in | Run `claude` in Terminal once and sign in. |
| A run says "Waiting for your OK" | It's over 50 properties. Read the estimate, then press **Yes, spend it** (or add `--yes`). |
| The dashboard won't open | Look at `output/server.log`, or run `uv run run.py serve` to see the message. |

## For developers

```bash
uv run pytest
```

Tests use made-up data only. Never commit `.env`, real exports, owner data, photos or the database (`.gitignore` covers them). A privacy check (`scripts/privacy_check.py`, installed as a git pre-commit hook by `scripts/setup_mac.sh` or `scripts/install_hooks.sh`) blocks any commit that contains an owner name, address or parcel number from the files in `data/crexi/`.
