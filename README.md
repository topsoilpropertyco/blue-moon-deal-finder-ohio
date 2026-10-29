# Blue Moon Deal Finder

Ranks the properties in a Crexi export so the 500 monthly Crexi owner-contact credits go to the best owners, and runs the **DRILL**: free sources first, one step at a time, until every owner is confirmed by a real conversation. Confirmed owners go to the CRM.

Everything runs on the Mac Mini. Private data (Crexi exports, owner names, photos, results, the database) stays there and never goes to GitHub.

- What it does and why: [PRD.md](PRD.md)
- How to work the DRILL: [docs/DRILL.md](docs/DRILL.md)
- Every free data source, in priority order: [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)
- How the code fits together: [ARCHITECTURE.md](ARCHITECTURE.md)

## What's inside

The top bar has three buttons: **Work**, **Insights** and **Setup**. Click one to open its screens; click it again (or press Escape) to hide them. The group you are in is open by itself, and a dot on a closed button means something inside has a count waiting (hover it to see which).

| Screen | What it's for |
|---|---|
| **Deals** | Every property ranked by Blue Moon score (how worth chasing the owner is), with contact level, next DRILL step, size, drive time, photo check, rent fit and owner type. List or drive-time rings. |
| **DRILL** | Penetration (owners confirmed), the stage-by-stage pipeline, and work queues: one owner at a time, links already filled in, keys to check it off. |
| **Credits** | Crexi credits in bulk: export the next N owners as a file, drop Crexi's reply back, history of every batch. |
| **Photo review** | The AI looks first; you check its work. Its unsure ones come first, then every call it made: **Yes, the AI is right** or **No, it's wrong**. The Scorecard shows how often it is right and whether it is learning. |
| **Funnel, Analytics, The score** | How the list narrows, what's in the data, how the score works and learns. |
| **Import, Run, Exports, Data sources, Settings** | Add Crexi files, run steps (cost estimate first), download files (below), the free source list, settings. |

**Exports** groups the downloads by what you are about to do: **Work lists** (ranked list, DRILL worklist, mailing list), **Hand off** (CRM handoff, set-aside zip), **Fix data in a spreadsheet** (download, fix in Excel, drop it back on Import) and **Summary** (run summary). Each one says what it is and when to use it.

**Logo:** the Find / Fund / Operate stool is in `docs/brand/` (original) and, with the white background removed, in `bluemoon/web/static/brand/` (header, sign-in page, empty screens, browser tab icon). `python3 scripts/make_logo.py` rebuilds those files from the original.

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

To pick your own simpler password: `uv run run.py share password --set maple-river-blue7` (8+ characters; it's guarded by a 5-wrong-tries-then-wait lock per device, but anyone with the link can try, so avoid a single common word). `share password` makes a team password like `bluemoon-k7q4-92xf-h3td`, saves it in `.env` (`APP_PASSWORD`) and shows it; run it again any time to see it, or with `--new` to replace it (everyone signs in again). Send each person the link and the password. They open the link and type **their own email** and the team password; only emails on the list get in (`*@yourcompany.com` lets in everyone at that company), and the app knows who is who. With nobody on the list, the password alone gets people in. Without a password the link refuses to start. Adding or removing someone works right away, no restart: `uv run run.py share add name@example.com` or `share remove name@example.com` (a removed person is signed out on their next click; if they know the password, `share password --new` when convenient). **Settings** and `uv run run.py share list` show the current link, the people and when each last signed in. On the Mac itself (http://localhost:8765) no sign-in is needed.

The link changes whenever sharing restarts (for example after the Mac restarts), so send the new one when that happens. Cloudflare's own email-code gate (`cloudflared --allowed-mail`) isn't used: its sign-in runs out a few seconds after the code is accepted, so every screen said "Can't reach the Blue Moon server" ([cloudflared issue #1752](https://github.com/cloudflare/cloudflared/issues/1752)). The dashboard itself only listens on this computer; outside visitors reach it only through the link, and only with the team password.

**A link that never changes (free):** the free Cloudflare link gets a new name whenever sharing restarts. ngrok's free plan gives one permanent address instead, with no domain and no website to pay for. Your data still stays on the Mac Mini; ngrok only passes visitors through, and they still sign in with their email and the team password. Free ngrok links show each visitor a one-time "you are about to visit" button the first time, and free bandwidth is limited (check ngrok's current limits).

1. Make a free account at ngrok.com. In its dashboard open **Domains** and create your free domain (it looks like `something.ngrok-free.app`). Copy your **Authtoken** from the dashboard too.
2. Install ngrok and save the token (paste it at the hidden prompt, nowhere else):
```bash
scripts/install_ngrok.sh
uv run run.py key ngrok
```
3. Put your own settings in `config.local.yaml` (copy `config.local.yaml.example`; it is never in git and wins over `config.yaml`, so updates never clash with it): `share:` with `provider: ngrok` and `ngrok_domain: something.ngrok-free.dev`. Then restart sharing: `launchctl kickstart -k gui/$(id -u)/com.bluemoon.share`. Settings then shows the same address every time. (Don't edit `config.yaml` itself: `git pull` stops when you and an update both changed it.)

To go back to the temporary Cloudflare link, set `provider: cloudflare`.

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

Only those columns are read. Nothing is added or deleted, owner names, scores and the original Crexi columns are never changed, and every change is saved with who made it. A row that changed in the app after you downloaded the file (a newer Crexi import, a buy-box decision) is left alone, so an old file never brings back old data; download a fresh copy to edit it. Keep the **Property ID** and **Row as of** columns: a file without Property ID is refused. Without Row as of, an `.xlsx` the app made goes by when it was downloaded (saved inside the workbook); a CSV, or a workbook too old to carry that, changes nothing. A later Crexi import keeps your fixes and says so ("Kept 2 values you corrected by hand"). The buy-box decisions from one file can be undone on the Import screen right after.

A cell the app can't read (text in a number column, a minus sign, a formula Excel never worked out) is left as it was and listed in the preview; a formula Excel saved counts as its result (`=36+2` is 38). Empty cells that would erase values are counted on their own in the preview, and when there are more than `safety: confirm_clears_over` (10) the Import screen asks "Erase N values?" before saving. The DRILL worklist, mailing list and CRM handoff can't be imported (they aren't property lists); bring fixes back with `ranked_list` or a review file.

Re-importing is safe: matching properties are updated, nothing is deleted or duplicated. Owners, free drive estimates, scores and the DRILL's automatic steps run right after every import; a dashboard import also starts the free HUD rent check in the background (`rent: after_import` in `config.yaml`). If the file's Property Type doesn't fit the market (apartments dropped into self-storage), the preview says which market it belongs in before you import. If a column is matched wrong, set it in `config.yaml` under `crexi: column_overrides` (for example `units: "Number of Units"`).

## 4. Run the steps (Run screen or Terminal)

The **Run** screen lists every step top to bottom, from importing a Crexi list to spending Crexi credits, each marked **Done**, **To do**, **Waiting** (on a step above) or **Blocked** (something to set up, like the Google key). **Do this next** at the top names the first things to do. Machine steps have **Run**, **Test on 5** and **Estimate**; the steps you do yourself (Photo review, Crexi credits) have a button to their screen. Paid steps over 50 properties stop with **Waiting for your OK** and show the calls and cost; press **Yes, spend it** to go ahead. **Run everything** runs all the free steps and stops before each big paid step. You can leave the screen while a job runs.

**Photos and AI check for this month** (a card between "Do this next" and the numbered list) is the one to use every day. It says how many properties had their Street View photos pulled this calendar month ("412 of 600 done this month") and has three buttons:

- **Test on 5**: photos, then the AI check, for 5 properties. Try it first.
- **Do the next 100**: the same for the next batch of 100 (the batch size is a setting).
- **Keep going**: batch after batch, until this month's goal is reached (when it already is, one more batch). One click, one OK: it stops at the Google and Claude caps per run, at a problem, or when nothing is left, and what it did is saved.

Each button runs **Photos** and then **Photo check** in one background job. Properties go in priority order: buy-box properties with no photos first, best Blue Moon score first, then the rest of the list. Under the buttons the card shows what the next batch will cost (a dry run, nothing is called). Runs over 50 properties stop with **Waiting for your OK**, the cost, and one button (**Yes, go ahead (about $X)**). Every answer is saved, so nothing is paid for twice. Photos pulled before this feature existed are counted once, from the date of their files.

**Change the monthly goal or the batch size** (Settings > **Monthly photo goal** shows the current numbers): put your own in `config.local.yaml`, which updates never touch. Make it once, then add the lines:

```bash
cd ~/blue-moon-deal-finder-ohio && cp -n config.local.yaml.example config.local.yaml && open -e config.local.yaml
```

```yaml
photos:
  monthly_target: 800   # properties that should have photos and an AI first look each month (default 600)
  batch: 150            # what "Do the next ..." does (default 100)
```

Restart the dashboard afterwards. In the Terminal: `uv run run.py photo_batch --limit 5` (add `--dry-run` to see the cost), `uv run run.py photo_batch`, `uv run run.py photo_keep_going`.

The same from the Terminal (`--market` can be left out for apartments; `self-storage`, `warehouses` and so on pick another market):

```bash
uv run run.py estimate            # what every step would do and cost; nothing is called
uv run run.py all --limit 5       # test run
uv run run.py all                 # everything; paid steps over 50 properties ask "Go ahead? [y/N]"
uv run run.py rent                # one step: owners | drive | rent | parcels | photos | classify | score | drill | research
uv run run.py parcels --limit 5 --dry-run   # County records: what it would look up (free; nothing is asked)
uv run run.py parcels --limit 5   # County records for 5 properties; then without --limit
uv run run.py research --limit 5 --dry-run   # AI research: what it would do (owners, properties, minutes)
uv run run.py research --limit 5  # AI research on 5 owners; then without --limit (asks first over 50 properties)
uv run run.py export              # the export files in output/<market>/ (--dry-run: say what it would write)
uv run run.py status              # where every market stands
uv run run.py calibrate           # re-check the photos you reviewed; updates the Scorecard (90% check)
```

| Step | What it does | Cost |
|---|---|---|
| owners | Owner type (mom-and-pop or institutional), portfolio, years owned, loan, absentee | Free |
| drive | Drive minutes from the market's starting point | Google Routes (free estimate without a key) |
| rent | HUD Fair Market Rents by ZIP compared with Brad's rent scale | Free (HUD's map service) |
| parcels | County records: the owner of record and the tax mailing address from each Ohio county's own free public parcel map (see below) | Free |
| photos | 3 Street View photos and 1 satellite photo per buy-box property | Google, after the free monthly calls |
| classify | Claude reads the photos (your Claude plan through Claude Code): is it a walk-up, plus its first read of the building (style, stories, buildings, condition) | No per-call bill |
| photo_batch / photo_keep_going | The Run screen's **Photos and AI check for this month** buttons: photos, then the AI check, for the next batch or until the monthly goal | Google, after the free monthly calls; your Claude plan |
| score | Buy box, colors, Blue Moon score and rank | Free |
| drill | DRILL automatic stages and contact levels | Free |
| calibrate | Re-checks every property you answered in Photo review and updates the Scorecard | Your Claude plan |
| research | AI research: Claude searches the web for each buy-box owner's business filing, officers and agent, website, business phone and email (web search and page reads only; people-search sites and Crexi are blocked). Everything it finds is saved "not tested yet" (level 2 at most) with the link where it was seen | Your Claude plan, no per-call bill; asks first over 50 properties |

**County records** (Run screen: **County records**, after Score; `uv run run.py parcels`) asks each county's free public parcel map who owns the parcel under each property's map point. Test it with `uv run run.py parcels --limit 5 --dry-run`, then `--limit 5`, then the whole market. It is free, so it needs no OK, and it is part of `all`. It looks up buy-box properties first (best score first), then the rest, and only properties with a map location (the drive step places them). What it fills in, per property:

- the **owner(s) on the county tax record** (people, role "owner of record") and the taxpayer or contact name (role "other");
- the **tax mailing address** as a mailing contact, "not tested yet" (an address on your do-not-contact list comes in as do-not-contact; one already on the property is not added twice);
- the DRILL's **County auditor** stage is checked off as found, by "auto", with a note naming the county and the owner on the tax record. If that owner is not the Crexi owner the note says so, the owner's DRILL notes get a line, and the property gets the flag **County owner differs** (shown on the property). Crexi's owner name is never overwritten and no new DRILL pass starts. Contact level 3 is never set here; only a conversation confirms an owner.

When a county finds nothing, nothing is checked off, so that owner still waits in the County auditor queue and you can look it up by hand with the same links as before. A county owner on a shared parcel (a condo building with several owners) is left alone for you to check. Do-not-contact owners are skipped.

Counties with their own live service: Franklin, Greene, Delaware, Champaign, Madison, Logan, Fayette, Clinton, Shelby, Clark and Union (Clark and Union give the owner name only; Clark's and Union's layers have no usable mailing address). Any other Ohio county (Montgomery, Warren, Preble, Darke, Butler, Hamilton...) uses **older statewide data**: ODNR's owner names and the state's mailing addresses, mostly from 2023-24 (ODNR has no owners for Montgomery). Those results are saved the same way but marked "older statewide data" and do **not** check the County auditor stage off, so you still confirm the owner on the auditor page. Miami's layer is switched off (the county has not published it as open data), so Miami uses the statewide data too. Indiana properties are skipped (no source). Every answer is saved in the database, so a re-run asks nobody again; a county that fails is reported and tried again next run. Counties, web addresses, field names and the polite pace (5 at a time) are in `config/parcels.yaml`.

AI research is not part of `all` or **Run everything**: run it on its own (Run screen: **AI research (owners)**, or `uv run run.py research`). It asks up to `ai_research: max_calls_per_run` (100) owners per run, about 2 minutes each, and its estimate says how many owners and properties are left and how many this run covers. Each owner's answer is saved for `refresh_after_days` (180), so a re-run doesn't ask again. Settings are in `config/drill.yaml` under `ai_research:`; `backend: mock` gives made-up offline test answers (example names, 555 numbers) that are written into the market like real ones and never saved as Claude's, so use it only on a test copy.

Running drive, rent, photos or classify on its own updates the buy box, scores and rank right after, so the photos are bought for the right properties. Google drive times stop at `drive: max_elements_per_run` (10,000) per run; the rest wait for the next run. Every answer from Google, HUD and Claude is saved, so re-runs don't pay twice. Exit codes: 0 done, 1 problem, 3 waiting for your OK.

## 5. Work it in the dashboard

- **Deals:** the ranked list. Work from the top. The three contact levels sit at the top with how many buy-box owners are at each; click one to see those properties. Under the numbers, **Why?** shows where the rest of the list went: each reason properties were set aside (too far, wrong size, too old, skipped county), how many for that reason alone, and how many are close calls on an estimated drive time that real Google drive times may bring back. Set-aside properties are never deleted (**Excluded** and **Everything** show them). Tap a property for its panel: the score explained, photos, rent, owner, the DRILL timeline and every original Crexi column. **Drive-time rings** shows the same list as a map of drive time.
- **Buy box by hand:** the rules decide first, then you can overrule them, and nothing is ever deleted. **Why?** splits each reason into groups (for example 61–75, 76–90, 91–120 and over 120 minutes; 1–4 and 5–9 units; the 1960s, 1950s; each skipped county), and every group says how many are still set aside, added by hand, or kept. Each group has two buttons. **Review** opens the list of just that group (for example "Reviewing: 768 properties 76–90 min away"), in or out of the buy box, so nothing disappears while you look; each set-aside row there has **Add to buy box**, and rows you added say **Added by you · Undo**. **Back to all deals** takes you back. **Decide** (in the panel or above the Review list) offers: **add the ones set aside only for this** (that rule no longer counts for them; a property that also fails another rule stays out, and the answer says how many and why), **add all** (in the buy box whatever the rules say), **keep them set aside** (marked as looked at; what you added from that group leaves the buy box again, and the message says how many), or **undo my decisions for this group** (the rules decide again, also for ones you added one at a time from a row, the drawer or a spreadsheet; shown only when the group holds a decision it can undo, including a rule you stopped counting, which the group shows as "this rule no longer counts"). The numbers count the properties still set aside: ones already in the buy box are not offered again, and "only for this reason" leaves out a rule you already stopped counting. After each choice a message says what changed, with an **Undo** button. Undo never wipes a newer decision: if someone decided one of those properties again since, that one is left alone and the message says so. In a property's panel, the **Buy box** section shows what each rule says, who decided and when, and a short history, with **Add to buy box**, **Set aside** and **Undo: follow the rules**. Properties you import later still follow the rules; to change a rule itself (for example the drive limit, `max_drive_minutes: 60` under the market in `config.yaml`), edit the YAML and restart the dashboard. Decisions stay through every re-score and when you import the same file again. Properties added by hand get photos, the photo check and Crexi credits like any buy-box property (photo runs still ask first over 50); a property set aside by hand never gets a Crexi credit. Group edges are in `config.yaml` (`set_aside:`) and `config/asset_classes.yaml` (`size: set_aside_below / set_aside_above`).
- **DRILL:** start with **Reach out** (owners with a phone or email to test; "Talked to owner, confirmed" moves them to level 3), then **Work this** on the first stage with owners waiting. One owner at a time, links already filled in. Keys: F found, N nothing, R matches records (County auditor), X not applicable (saved; the owner moves to the next stage), J or S pass for now (nothing saved), K previous, O open the first link. The board counts buy-box owners; switch to **Everyone** to include set-aside properties. Do-not-contact is permanent: it covers the owner's other properties (same owner name or portfolio) and the same phone, email or mailing address everywhere, and only **Allow contact again** takes a contact off the list. A stage that keeps working for an owner type moves up for that owner type once it has 10 results (`learn_order` in `config/drill.yaml`). After **AI research (owners)** runs (Run screen), what Claude found shows on the property panel with an **AI** tag and its source link, "not tested yet"; press **Wrong** on one that isn't the owner's or isn't real (the same contact is marked wrong on the owner's other properties).
- **Credits (Crexi, in bulk):** you never open properties in Crexi one at a time. The Credits screen does this: **1. Export the next N for Crexi** (N starts as every free credit; type any number, for example 50). **Show who is in it** first if you like: it lists the owners by reason and how many duplicates were left out. The app writes `output/crexi/<period>/batch-<id>-<N>.csv` (never in GitHub) with Crexi's own column names first (Property Link, Property Type, Address, City, State, Zip Code, County, APN) and ours after them. **2. Upload that file in Crexi's bulk tool yourself** (the app never logs in to Crexi or opens it; Crexi matches the columns by name and spends one credit for each row it matches; a "Not Matched" row costs nothing). **3. Drop the file Crexi sends back** (Excel or CSV, the sheet called Ownership Information) on the Credits screen. It shows a preview before anything is saved: for example "This looks like Crexi's reply to batch #3 (500 sent): 447 match, 31 look like they weren't returned yet, 22 can't be matched", the columns it read as phones, emails and contact name (you can change them), and what the file would add (new phones, emails, owner or contact names, mailing addresses, blanks filled, different values kept as alternates). Rows are matched by Crexi link, then parcel number, then address + city + ZIP, across every market and batch, so one file may hold several batches. Phones and emails are saved as **not tested yet** (source Crexi, note "Crexi credit, batch N"), they belong to Crexi's contact person, and do-not-contact is respected. Rows it can't match are never dropped: they are in `output/crexi/<period>/unmatched-<import>.csv`. Importing the same file twice adds nothing. **4. Close the batch** when Crexi has sent everything it will: the rows that never came back are marked not found (the DRILL's Crexi stage records "nothing") and their credits go back to the free pool, so the screen suggests "42 of 50 came back: export the next 8". Closing always asks first. **Cancel** is for a batch you never uploaded. **Credit period:** credits reset on `crexi: reset_day` in `config.yaml` (29: a new period starts on the 29th, Oct 29 to Nov 28). **Returned** + **in flight** (sent, reply not imported yet) count against the 500, so you can't overspend. **History** lists every period and batch (when, who, sent, returned, not returned, matched %, status, download links) with what each import found. In Deals, the chips **Has Crexi data**, **No Crexi data** and **Sent to Crexi, waiting** show where each property stands; a property's panel has a Crexi section ("sent in batch 3 on Oct 29, returned Oct 31, found 2 phones"). If a later Crexi file disagrees with what you hold (units, year built, owner name...), the original is kept, the property gets a "Crexi sent different data" flag, and its Crexi section offers **Use this one** (undoable through the edit history) or **Ignore**. The old one-at-a-time way is folded under **Do one by hand**. From Terminal: `uv run run.py crexi status | export --count 50 [--dry-run] [--retry] | import <file> [--yes] | close <batch> --yes | cancel <batch> --yes | history`.
- **Photo review:** you check the AI's first pass (the AI looks first: run **Photos and AI check** on the Run screen; photos it hasn't looked at yet stay out of the list). The list is in this order: the ones the AI couldn't call or is unsure about, then every call it made, most valuable first (buy-box places, the ones it says are walk-ups, the best Blue Moon score). **Everything** and **Unsure only** at the top switch between the full list and just the unsure ones (`review: default_scope` in `config.yaml`; `spot_check_fraction` can sprinkle a share of the AI's confident calls into Unsure only; it is 0, off). Each card shows the full address, the AI's call in words ("Exterior corridor, so a walk-up"), how sure it is, and its read of the building (garden style, about 3 stories, 4 buildings, fair condition: estimates from the photos). On a call the AI made, press **Yes, the AI is right** (key **Y**) or **No, it's wrong** (key **N**, then pick what it really is). Yes saves the AI's own label as your answer; No saves yours; both go into the example library the AI reads next time (`labels/examples.jsonl`, with whether you agreed) and update the Blue Moon score. Numbers 1-5 still answer directly, S skips, K goes back. Cards where the AI is unsure show the label choices straight away. If the pictures don't help, find one yourself (Google, the property's website) and use **Add a photo** (or copy it and press Ctrl/Cmd+V on the page): it shows first and the photo check reads it first. JPEG or PNG only.
- **Scorecard** (Photo review > Scorecard): how often the AI is right overall and **label by label** (when it says "Exterior corridor", how often you agreed; how many of the ones you call that it found), **Is it learning?** (your latest 50 reviews against the earlier ones, with a bar for each run of reviews; `review: learning_window`), the 90% acceptance check (greens 90% truly a target, reds 90% truly not), the table of your answers against its guesses, and accuracy per color.
- **Walk-ups and the score:** a walk-up (exterior corridor or direct entry) raises the Blue Moon score and is never required. For apartments a green walk-up photo verdict multiplies the odds by 1.8 (a red "not a walk-up" by 0.4, no verdict by 1); change `signals: visual: green` under apartments in `config/asset_classes.yaml`.
- **Funnel, Analytics, The score:** how the list narrows, what's in the data, and how the Blue Moon score works. Mark how deals ended ("How did it end?" in the property panel) and the score tunes itself.
- **Exports:** `ranked_list.xlsx` (Google Sheets: File > Import > Upload), plus the CSVs. The ranked list explains each score ("Why this score") and the photo check ("Photo reasons", then the AI's "Building style (AI)", "Stories (AI estimate)", "Buildings seen (AI)" and "Condition (AI)", and "Photo answer from": AI, You, or You agreed with / corrected the AI), and after "Why set aside" shows the buy-box decision ("Rules", "Added by hand", "Rule waived: too far to drive", "Set aside by hand"), what the rules said, who decided and when, and the set-aside groups. Properties set aside by hand are in `excluded_manual.csv`. Every property list ends with "Property ID" and "Row as of" (when that row last changed), so an edited copy can come back in (see "Fix data in Excel and bring it back" above). `mailing_list.csv` holds buy-box owners whose mailing address is untested plus any owner whose next step is a letter, never a do-not-contact or wrong address. Getting `crm_handoff.csv` from the Exports screen marks those owners as handed off (a plain link to the file changes nothing).
- **Settings / Data sources:** read-only settings, which keys are set (yes or no only), and every free source by DRILL stage, with the county links.

## What it costs

| Thing | Cost |
|---|---|
| HUD rents, Census geocoding, county and state record links, county parcel lookups (County records step) | Free |
| Google drive times, Street View and satellite photos | Free up to Google's monthly allowance (about 10,000 of each); a few dollars beyond. Every paid run shows the estimate first. |
| Photo check | Your Claude plan through Claude Code on the Mac Mini; no per-call bill. If the plan hits a usage limit, the run stops cleanly and resumes next time. |
| AI research | Your Claude plan through Claude Code; no per-call bill. About 2 minutes of plan time per owner; runs over 50 properties ask first. |
| Crexi | Your existing plan (500 owner-contact credits a month) |

## 6. Markets and asset classes

A market is one asset class in one area: apartments, self-storage, warehouses, campgrounds and RV parks, mobile home parks, outdoor storage and raw land around Springfield are set up. Every market has the same screens; switch in the top bar. Add a market in `config.yaml` under `markets:`; buy boxes, photo questions and score weights per asset class are in `config/asset_classes.yaml`.

## 7. Settings

All thresholds and weights live in YAML, never in code:

- `config.yaml`: markets and starting points, Crexi credits, drive, Google prices and caps, photo check backend, the monthly photo goal (`photos:`), rent source, safety limit (50), dashboard options (`web:`), and the set-aside groups of the Why? panel (`set_aside:`).
- `config/asset_classes.yaml`: buy box, rent scale, photo labels and score multipliers per asset class.
- `config.local.yaml` (yours; make it from `config.local.yaml.example`): any setting from `config.yaml` that you want to change, for example `photos:`. It wins over `config.yaml` and updates never touch it.
- `config/drill.yaml`: the DRILL stages, their order and starting hit rates, and the reach-out outcomes.
- `config/sources.yaml`: data-source links and the county table.

Restart the dashboard after editing them.

## Updates

**Turn on nightly updates (once):** the app then updates itself every night at 3:17 AM. It checks GitHub, takes the new version, runs a quick check, and restarts the dashboard and the team link. If the check fails, it goes back to the version before by itself, so the dashboard keeps working.

```bash
cd ~/blue-moon-deal-finder-ohio
uv run run.py autoupdate install
```

**See how it went:** Settings shows a line like *Version: 3d75b81, 2026-10-29 · Updates itself nightly at 3:17 AM · Last update: Oct 29, 3:17 AM, ok*. Or in Terminal:

```bash
uv run run.py autoupdate status
```

**Update right now** (the same steps as the nightly one):

```bash
uv run run.py autoupdate now
```

Or the old way, by hand:

```bash
cd ~/blue-moon-deal-finder-ohio
git pull
uv sync
launchctl kickstart -k gui/$(id -u)/com.bluemoon.dealfinder
launchctl kickstart -k gui/$(id -u)/com.bluemoon.share
```
(The last two lines restart the dashboard and the team link; skip the last one if you don't share. If you run `uv run run.py serve` by hand instead, stop it with Control-C and start it again.)

Updating by hand never hurts anything: your data, `.env` and `config.local.yaml` are never touched, and the nightly update then simply finds nothing new.

Good to know:
- The nightly update skips itself (and says why on Settings) if you edited a file that came with the app, such as `config.yaml`. It never overwrites your edits. Put your own settings in `config.local.yaml` instead (see Settings above).
- The Mac must be on at 3:17 AM. If it was asleep, macOS runs the update when it wakes up.
- Turn it off: `uv run run.py autoupdate uninstall`. Change the time: `updates:` in `config.yaml`, then run `autoupdate install` again.
- The log is `logs/auto_update.log` (kept short).

## Back it up

Your data lives in the app folder: `data/` (the database and Crexi files), `images/`, `labels/` and `output/`. Time Machine backs them up while a backup disk is connected.

## If something goes wrong

| Problem | Fix |
|---|---|
| Drive times show "~" and photos are off | No Google key yet: `uv run run.py key google`, then restart the dashboard. |
| Google says "We couldn't verify it's you" when you copy the key | Sign in to console.cloud.google.com on a computer or phone you've used with that Google account before, finish any check Google asks for, then copy the key there. It can take a day after a new sign-in. |
| The team link says "Can't reach the Blue Moon server" | Update the app (see Updates above) and restart both services; the link then asks for your email and the team password. If the Mac restarted sharing, the link changed: the new one is on Settings. |
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
