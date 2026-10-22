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

That installs the Python packages and creates `.env`, your private settings file. Save your Google Maps key into it (paste the key when asked; nothing shows as you paste, and the key is never printed):

```bash
uv run run.py key google
```

Without a Google key the app still works: drive times are free straight-line estimates and photos are off. In Google Cloud Console (APIs & Services > Credentials for the key), turn on the **Routes API**, **Geocoding API**, **Street View Static API** and **Maps Static API**. `key anthropic` and `key census` work the same way. Never paste a key into a chat, an email or GitHub.

## 2. Start the dashboard

```bash
uv run run.py serve
```

Open http://localhost:8765. To keep it running in the background (it starts whenever you log in to the Mac Mini and restarts itself if it stops):

```bash
scripts/install_service.sh
```

So it also comes back after a power cut or update restart, turn on automatic login: System Settings > Users & Groups > Automatically log in as > your name. If FileVault is on, macOS hides that option, so after a restart log in once at the Mac.

**Share it with your team (free):** Cloudflare's free link puts the dashboard on the internet as `https://<random-words>.trycloudflare.com`, with no account, no domain and nothing to install for your team. Your data stays on the Mac Mini. Install Cloudflare's connector once, make the team password, add the people who may get in, and start sharing in the background:

```bash
scripts/install_cloudflared.sh
uv run run.py share password
uv run run.py share add pat@example.com
scripts/install_service.sh --share
```

`share password` makes a team password like `bluemoon-k7q4-92xf-h3td`, saves it in `.env` (`APP_PASSWORD`) and shows it; run it again any time to see it, or with `--new` to replace it (everyone signs in again). Send each person the link and the password. They open the link and type **their own email** and the team password; only emails on the list get in (`*@yourcompany.com` lets in everyone at that company), and the app knows who is who. With nobody on the list, the password alone gets people in. Without a password the link refuses to start. Adding or removing someone works right away, no restart: `uv run run.py share add name@example.com` or `share remove name@example.com` (a removed person is signed out on their next click; if they know the password, `share password --new` when convenient). **Settings** and `uv run run.py share list` show the current link, the people and when each last signed in. On the Mac itself (http://localhost:8765) no sign-in is needed.

The link changes whenever sharing restarts (for example after the Mac restarts), so send the new one when that happens. Cloudflare's own email-code gate (`cloudflared --allowed-mail`) isn't used: its sign-in runs out a few seconds after the code is accepted, so every screen said "Can't reach the Blue Moon server" ([cloudflared issue #1752](https://github.com/cloudflare/cloudflared/issues/1752)). The dashboard itself only listens on this computer; outside visitors reach it only through the link, and only with the team password.

To restart the background service after a settings change:

```bash
launchctl kickstart -k gui/$(id -u)/com.bluemoon.dealfinder
```

## 3. Import a Crexi export

In Crexi, export **Property Records** as CSV or Excel. Then either:

- **Dashboard:** pick the market in the top bar, open **Import**, drop the file. Check the preview (columns matched, first rows), then **Import**. Use **Import the first 5 only (test)** the first time.
- **Terminal:** put the file in `data/crexi/` and run `uv run run.py import data/crexi/yourfile.xlsx --limit 5`, then without `--limit`. `uv run run.py import` on its own imports only the files in `data/crexi/` it hasn't seen before (in any market), oldest first, so the newest export's owner data wins; `--force` imports them all again.

### Fix data in Excel and bring it back

Found a wrong unit count, year built or county? Fix it in a spreadsheet:

1. Download `ranked_list.xlsx` (or `.csv`) on **Exports**, or just one set-aside group: **Exports > Fix data in a spreadsheet > Download this group** (a small `review_<group>.xlsx` with the columns you can fix first and a "How to edit" sheet).
2. In Excel, change **Units**, **Building sq ft**, **Year built**, **Property type**, **County**, **ZIP** or **Lot acres** (an empty cell clears the value). In **Buy box decision** type **Add to buy box**, **Set aside** or **Follow the rules**.
3. Save it and drop it on **Import**. The app knows it's one of its own files: the preview says what will change ("This is a Blue Moon export. 3 properties have changes: units 2, year built 1"), then **Save the changes**. Terminal: `uv run run.py import ~/Downloads/ranked_list.csv --dry-run` to preview, then without `--dry-run`.

Only those columns are read. Nothing is added or deleted, owner names, scores and the original Crexi columns are never changed, and every change is saved with who made it. A row that changed in the app after you downloaded the file (a newer Crexi import, a buy-box decision) is left alone, so an old file never brings back old data; download a fresh copy to edit it. A later Crexi import keeps your fixes and says so ("Kept 2 values you corrected by hand"). The buy-box decisions from one file can be undone on the Import screen right after.

Re-importing is safe: matching properties are updated, nothing is deleted or duplicated. Owners, free drive estimates, scores and the DRILL's automatic steps run right after every import; a dashboard import also starts the free HUD rent check in the background (`rent: after_import` in `config.yaml`). If the file's Property Type doesn't fit the market (apartments dropped into self-storage), the preview says which market it belongs in before you import. If a column is matched wrong, set it in `config.yaml` under `crexi: column_overrides` (for example `units: "Number of Units"`).

## 4. Run the steps (Run screen or Terminal)

The **Run** screen lists every step top to bottom, from importing a Crexi list to spending Crexi credits, each marked **Done**, **To do**, **Waiting** (on a step above) or **Blocked** (something to set up, like the Google key). **Do this next** at the top names the first things to do. Machine steps have **Run**, **Test on 5** and **Estimate**; the steps you do yourself (Photo review, Crexi credits) have a button to their screen. Paid steps over 50 properties stop with **Waiting for your OK** and show the calls and cost; press **Yes, spend it** to go ahead. **Run everything** runs all the free steps and stops before each big paid step. You can leave the screen while a job runs.

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

- **Deals:** the ranked list. Work from the top. The three contact levels sit at the top with how many buy-box owners are at each; click one to see those properties. Under the numbers, **Why?** shows where the rest of the list went: each reason properties were set aside (too far, wrong size, too old, skipped county), how many for that reason alone, and how many are close calls on an estimated drive time that real Google drive times may bring back. Set-aside properties are never deleted (**Excluded** and **Everything** show them). Tap a property for its panel: the score explained, photos, rent, owner, the DRILL timeline and every original Crexi column. **Drive-time rings** shows the same list as a map of drive time.
- **Buy box by hand:** the rules decide first, then you can overrule them, and nothing is ever deleted. **Why?** splits each reason into groups (for example 61–75, 76–90, 91–120 and over 120 minutes; 1–4 and 5–9 units; the 1960s, 1950s; each skipped county), and every group says how many are still set aside, added by hand, or kept. Each group has two buttons. **Review** opens the list of just that group (for example "Reviewing: 768 properties 76–90 min away"), in or out of the buy box, so nothing disappears while you look; each set-aside row there has **Add to buy box**, and rows you added say **Added by you · Undo**. **Back to all deals** takes you back. **Decide** (in the panel or above the Review list) offers: **add the ones set aside only for this** (that rule no longer counts for them; a property that also fails another rule stays out, and the answer says how many and why), **add all** (in the buy box whatever the rules say), **keep them set aside** (marked as looked at), or **undo my decisions for this group** (the rules decide again). After each choice a message says what changed, with an **Undo** button. In a property's panel, the **Buy box** section shows what each rule says, who decided and when, and a short history, with **Add to buy box**, **Set aside** and **Undo: follow the rules**. Properties you import later still follow the rules; to change a rule itself (for example the drive limit, `max_drive_minutes: 60` under the market in `config.yaml`), edit the YAML and restart the dashboard. Decisions stay through every re-score and when you import the same file again. Properties added by hand get photos, the photo check and Crexi credits like any buy-box property (photo runs still ask first over 50); a property set aside by hand never gets a Crexi credit. Group edges are in `config.yaml` (`set_aside:`) and `config/asset_classes.yaml` (`size: set_aside_below / set_aside_above`).
- **DRILL:** start with **Reach out** (owners with a phone or email to test; "Talked to owner, confirmed" moves them to level 3), then **Work this** on the first stage with owners waiting. One owner at a time, links already filled in. Keys: F found, N nothing, R matches records (County auditor), X not applicable (saved; the owner moves to the next stage), J or S pass for now (nothing saved), K previous, O open the first link. The board counts buy-box owners; switch to **Everyone** to include set-aside properties. Do-not-contact is permanent: it covers the owner's other properties (same owner name or portfolio) and the same phone, email or mailing address everywhere, and only **Allow contact again** takes a contact off the list. A stage that keeps working for an owner type moves up for that owner type once it has 10 results (`learn_order` in `config/drill.yaml`).
- **Credits:** one plan for the whole Crexi account (credits are shared by every market), with how many of the 500 are left and the daily pace to spend them all. Owners rank by score x Crexi's hit rate for their owner type; owners with an untested phone wait in Reach out first. If the possible owners can't use every credit, the rest go to buy-box owners with no name yet, then the best owners just outside the buy box (`crexi: plan_fallbacks`). How to spend one: **Open in Crexi** on the next owner in the plan, unlock the owner's contact info in Crexi (the app never logs in to Crexi), then **Used a credit** and record what Crexi showed.
- **Photo review:** the photos the AI was unsure about. Press 1-5 to answer, S to skip. **Scorecard** shows whether the AI passes the 90% check.
- **Funnel, Analytics, The score:** how the list narrows, what's in the data, and how the Blue Moon score works. Mark how deals ended ("How did it end?" in the property panel) and the score tunes itself.
- **Exports:** `ranked_list.xlsx` for Brad (Google Sheets: File > Import > Upload), plus the CSVs. The ranked list explains each score ("Why this score") and the photo check ("Photo reasons"), and after "Why set aside" shows the buy-box decision ("Rules", "Added by hand", "Rule waived: too far to drive", "Set aside by hand"), what the rules said, who decided and when, and the set-aside groups. Properties set aside by hand are in `excluded_manual.csv`. Every property list ends with "Property ID" and "Row as of" (when that row last changed), so an edited copy can come back in (see "Fix data in Excel and bring it back" above). `mailing_list.csv` holds buy-box owners whose mailing address is untested plus any owner whose next step is a letter, never a do-not-contact or wrong address. Getting `crm_handoff.csv` from the Exports screen marks those owners as handed off (a plain link to the file changes nothing).
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

- `config.yaml`: markets and starting points, Crexi credits, drive, Google prices and caps, photo check backend, rent source, safety limit (50), dashboard options (`web:`), and the set-aside groups of the Why? panel (`set_aside:`).
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
launchctl kickstart -k gui/$(id -u)/com.bluemoon.share
```
(The last two lines restart the dashboard and the team link; skip the last one if you don't share. If you run `uv run run.py serve` by hand instead, stop it with Control-C and start it again.)

## Back it up

Your data lives in the app folder: `data/` (the database and Crexi files), `images/`, `labels/` and `output/`. Time Machine backs them up while a backup disk is connected.

## If something goes wrong

| Problem | Fix |
|---|---|
| Drive times show "~" and photos are off | No Google key yet: `uv run run.py key google`, then restart the dashboard. |
| Google says "We couldn't verify it's you" when you copy the key | Sign in to console.cloud.google.com on a computer or phone you've used with that Google account before, finish any check Google asks for, then copy the key there. It can take a day after a new sign-in. |
| The team link says "Can't reach the Blue Moon server" | Update the app (below) and restart both services; the link then asks for your email and the team password. If the Mac restarted sharing, the link changed: the new one is on Settings. |
| The team link says "isn't on the team list" | Add that exact email: `uv run run.py share add name@example.com` (works right away). |
| Google refuses the key | Turn on Routes, Geocoding, Street View Static and Maps Static APIs for that key in Google Cloud Console. |
| Photo check says Claude Code isn't signed in | Run `claude` in Terminal once and sign in. |
| A run says "Waiting for your OK" | It's over 50 properties. Read the estimate, then press **Yes, spend it** (or add `--yes`). |
| The dashboard won't open | Look at `output/server.log`, or run `uv run run.py serve` to see the message. |

## For developers

```bash
uv run pytest
```

Tests use made-up data only. Never commit `.env`, real exports, owner data, photos or the database (`.gitignore` covers them). A privacy check (`scripts/privacy_check.py`, installed as a git pre-commit hook by `scripts/setup_mac.sh` or `scripts/install_hooks.sh`) blocks any commit that contains an owner name, address or parcel number from the files in `data/crexi/`.
