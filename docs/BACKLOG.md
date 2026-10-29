# Backlog: improvements Seth asked for

Start of every session: read this list and ask Seth which open items to work on next.
Mark an item done (with the date) when it ships; never delete it.

## Design and usability (requested 2026-10-21)

1. **Calmer top menu.** Replace the long menu row with three buttons: **Work** (or "Work to do"), **Insights** and **Setup** (or "Settings"). Clicking one opens its menu; clicking it again hides it. **Done 2026-10-24:** three buttons, the current screen's group open by default, a dot on a closed button when it holds a count, Escape closes.
2. **More space.** The layout feels tight and dense. Make it elegant and spacious: more breathing room around sections, cards, rows and text. **Done 2026-10-24:** spacing scale (`--s1`..`--s8`, `--pad`) used across all screens.
3. **Lighter feel, same depth.** Keep every deep technical capability, but make the app feel less heavy: show less at once, reveal detail on demand. **Done 2026-10-24:** folded notes on the Deals Why panel, "What this step does" on Run, folded Settings panels and Exports help.
4. **Run screen as one top-to-bottom list.** Replace the grid of boxes with one row per step, top to bottom, so nothing is missed. Make it obvious where you are now and what to do next. **Done 2026-10-21:** numbered steps from import to Crexi credits, each Done / To do / Waiting / Blocked, with "Do this next" at the top.
5. **Blue Moon branding.** Use the Find / Fund / Operate stool logo with a bright, glowing blue moon behind it (like the bright moon photo Seth shared), and bring the moon more into the design style. The logo is in `docs/brand/`. **Done 2026-10-24:** stool with a soft moon glow (dark mode) in the header, sign-in page, empty Deals and Import screens and the tab icon.

## Photos, the AI first pass and the review loop (requested 2026-10-29)

14. **Every lead gets Street View photos and an AI first pass; Seth reviews.** **Done 2026-10-29:**
    - Run screen card **Photos and AI check for this month** ("412 of 600 done this month") with **Test on 5**, **Do the next 100** and **Keep going** (photos, then the AI check, in one job; the cost shown and one click to OK over 50). Goal and batch are `photos:` in `config.yaml`, set your own in `config.local.yaml` (shown in Settings).
    - The AI also reads the building: style, estimated stories, buildings visible, condition (card, property panel, ranked export).
    - Photo review: unsure first, then every AI call (**Yes, the AI is right** / **No, it's wrong**, keys Y and N), **Everything / Unsure only**; both answers feed `labels/examples.jsonl`. Scorecard: how often the AI is right, per label, and "Is it learning?" (latest 50 vs earlier).
    - A walk-up now counts x1.8 (was x1.4) in the apartments Blue Moon score; still never required.
    - Still open: photos and AI answers made before 2026-10-29 have no building details (re-run with Calibrate, or ask for a "re-read" button); a Settings screen editor for the goal (today it is a file); a monthly reset reminder.

## Exports and the CRM (requested 2026-10-21)

6. **Exports screen wording.** "Files for Brad" isn't quite right. Rework how the exports are named and grouped. **Done 2026-10-24:** Work lists, Hand off, Fix data in a spreadsheet, Summary.
7. **An export for Crexi.** A clear export meant for Crexi (ask Seth exactly what it should contain and how he'll use it, for example a list of properties to spend credits on).
8. **Built around the end goal.** The flow is: enrich and improve the data, then put it into the CRM. Make the app's structure follow that path. Seth is thinking more about what this means. (2026-10-22: buy-box decisions, AI research and the Excel round trip are steps toward this.)

## Carried over

9. **Owner profiles and archetypes** (ROADMAP phase 5).
10. **Who did what.** With a team using the link, record which person checked off each DRILL step. Since 2026-10-21 each person signs in with their own email, so the app knows who is clicking; recording it on each DRILL step is still to do.
11. **Manage the team list from Settings** (add or remove people with buttons instead of Terminal).
12. **HUD FY2027 rents** once HUD's free map service posts them.

## Setup still open

- Google Maps key: Google's "couldn't verify it's you" check blocked copying the key on the Mac Mini. Once copied: `uv run run.py key google`, restart the dashboard, then on Run: Drive times, then **Photos and AI check for this month** with **Test on 5**.
- Delete the old `build/v1` branch on GitHub (it holds two owner names in an old commit).

## Done 2026-10-21 (asked for directly)

- Team link fixed: every screen said "Can't reach the Blue Moon server" because Cloudflare's email-code sign-in runs out seconds after login. The dashboard now signs people in itself (their email + the team password).
- Contact levels shown as a three-step ladder with counts on Deals and DRILL.
- Deals explains where the rest of the list went (set-aside reasons, close calls on estimated drive times).
- Credits screen: three-step "how to spend one credit".
- `uv run run.py key google` saves a key without opening `.env`.

## Done 2026-10-22 (asked for directly)

- Every set-aside reason broken into groups (drive 61–75 / 76–90 / 91–120 / over 120 min; units 1–4, 5–9, 81–100, 101–200, 201+; decade built; each skipped county), with a plain explanation and **Review** and **Decide** on each group. Decisions by hand with who/when, history and Undo.
- AI research step (Run screen, `uv run run.py research`): business filings, officers, website, business phone/email and social pages per owner, on the Claude plan; people-search sites blocked; results untested with source links.
- Fix data in Excel and import it back safely (ranked list or a per-group review file).
- **County records** (done; was "Free county records APIs", approved by Seth): Run screen row and `uv run run.py parcels`. Reads the owner and tax mailing address from 11 Ohio counties' free parcel maps (Franklin, Greene, Delaware, Champaign, Madison, Logan, Fayette, Clinton, Shelby, Clark and Union; Clark and Union give the owner name only), and from older statewide data (ODNR owners, OGRIP mailing addresses) for every other Ohio county. Miami is off. Settings: `config/parcels.yaml`.

## Waiting on Seth (asked 2026-10-22)

- **County records step: two small calls.** (1) The county's tax mailing address now joins the mailing list as one more "not tested yet" address wherever it differs from the Crexi one (the list already carries every untested address of a buy-box owner). Keep, or add only the one best address? (2) Miami County's layer answers but is not published as open data, so Miami is switched off (it uses the older statewide data); turn it on in `config/parcels.yaml` only if the county says it is public.
- **Size groups:** Seth wrote "90 to 120" and "100 to 200"; built as 81–100, 101–200, 201+ (change in `config/asset_classes.yaml`).
- **AI-found contacts and Crexi credits:** an owner with untested AI contacts waits in Reach out before getting a credit (the DRILL rule). Keep, or let credits go to them anyway?
- **Letters for properties set aside by hand:** they can still be on the mailing list when a letter is the owner's next step. Keep or stop?
- **AI research on individuals:** it covers individuals, trusts and estates too (public business presence and social pages only). Keep or limit to companies?

## Still open from the review (2026-10-22)

- Undo for field fixes imported from a spreadsheet (today: type the old value in a fresh download).
- A decision saved just before a re-score that fails shows an error without an Undo handle (rare: needs a 30-second database lock).
- Run AI research live with Test on 5 on the Mac: the web-search guard's long blocked list hasn't been tried against the live search service yet.
