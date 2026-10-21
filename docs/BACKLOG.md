# Backlog: improvements Seth asked for

Start of every session: read this list and ask Seth which open items to work on next.
Mark an item done (with the date) when it ships; never delete it.

## Design and usability (requested 2026-10-21)

1. **Calmer top menu.** Replace the long menu row with three buttons: **Work** (or "Work to do"), **Insights** and **Setup** (or "Settings"). Clicking one opens its menu; clicking it again hides it.
2. **More space.** The layout feels tight and dense. Make it elegant and spacious: more breathing room around sections, cards, rows and text.
3. **Lighter feel, same depth.** Keep every deep technical capability, but make the app feel less heavy: show less at once, reveal detail on demand.
4. **Run screen as one top-to-bottom list.** Replace the grid of boxes with one row per step, top to bottom, so nothing is missed. Make it obvious where you are now and what to do next. **Done 2026-10-21:** numbered steps from import to Crexi credits, each Done / To do / Waiting / Blocked, with "Do this next" at the top.
5. **Blue Moon branding.** Use the Find / Fund / Operate stool logo with a bright, glowing blue moon behind it (like the bright moon photo Seth shared), and bring the moon more into the design style. Needs the logo file from Seth (put it in `docs/brand/`); the moon photo needs a license or a moon drawn in code.

## Exports and the CRM (requested 2026-10-21)

6. **Exports screen wording.** "Files for Brad" isn't quite right. Rework how the exports are named and grouped.
7. **An export for Crexi.** A clear export meant for Crexi (ask Seth exactly what it should contain and how he'll use it, for example a list of properties to spend credits on).
8. **Built around the end goal.** The flow is: enrich and improve the data, then put it into the CRM. Make the app's structure follow that path. Seth is thinking more about what this means.

## Carried over

9. **Owner profiles and archetypes** (ROADMAP phase 5).
10. **Who did what.** With a team using the link, record which person checked off each DRILL step. Since 2026-10-21 each person signs in with their own email, so the app knows who is clicking; recording it on each DRILL step is still to do.
11. **Manage the team list from Settings** (add or remove people with buttons instead of Terminal).
12. **HUD FY2027 rents** once HUD's free map service posts them.

## Setup still open

- Google Maps key: Google's "couldn't verify it's you" check blocked copying the key on the Mac Mini. Once copied: `uv run run.py key google`, restart the dashboard, then on Run: Drive times, Photos and Photo check with **Test on 5**.
- Delete the old `build/v1` branch on GitHub (it holds two owner names in an old commit).

## Done 2026-10-21 (asked for directly)

- Team link fixed: every screen said "Can't reach the Blue Moon server" because Cloudflare's email-code sign-in runs out seconds after login. The dashboard now signs people in itself (their email + the team password).
- Contact levels shown as a three-step ladder with counts on Deals and DRILL.
- Deals explains where the rest of the list went (set-aside reasons, close calls on estimated drive times).
- Credits screen: three-step "how to spend one credit".
- `uv run run.py key google` saves a key without opening `.env`.
