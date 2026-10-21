# Backlog: improvements Seth asked for (not started)

Start of every session: read this list and ask Seth which open items to work on next.
Mark an item done (with the date) when it ships; never delete it.

## Design and usability (requested 2026-10-21)

1. **Calmer top menu.** Replace the long menu row with three buttons: **Work** (or "Work to do"), **Insights** and **Setup** (or "Settings"). Clicking one opens its menu; clicking it again hides it.
2. **More space.** The layout feels tight and dense. Make it elegant and spacious: more breathing room around sections, cards, rows and text.
3. **Lighter feel, same depth.** Keep every deep technical capability, but make the app feel less heavy: show less at once, reveal detail on demand.
4. **Run screen as one top-to-bottom list.** Replace the grid of boxes with one row per step, top to bottom, so nothing is missed. Make it obvious where you are now and what to do next.
5. **Blue Moon branding.** Use the Find / Fund / Operate stool logo with a bright, glowing blue moon behind it (like the bright moon photo Seth shared), and bring the moon more into the design style. Needs the logo file from Seth (put it in `docs/brand/`); the moon photo needs a license or a moon drawn in code.

## Exports and the CRM (requested 2026-10-21)

6. **Exports screen wording.** "Files for Brad" isn't quite right. Rework how the exports are named and grouped.
7. **An export for Crexi.** A clear export meant for Crexi (ask Seth exactly what it should contain and how he'll use it, for example a list of properties to spend credits on).
8. **Built around the end goal.** The flow is: enrich and improve the data, then put it into the CRM. Make the app's structure follow that path. Seth is thinking more about what this means.

## Carried over

9. **Owner profiles and archetypes** (ROADMAP phase 5).
10. **Who did what.** With a team using the link, record which person checked off each DRILL step (Cloudflare tells the app each person's email).
11. **Manage the team list from Settings** (add or remove people with buttons instead of Terminal).
12. **HUD FY2027 rents** once HUD's free map service posts them.

## Setup still open

- Google Maps key: Google's "couldn't verify it's you" check blocked copying the key on the Mac Mini. Add it to `.env`, restart the dashboard, then Run: Drive times, Photos and Photo check with **Test on 5**.
- Delete the old `build/v1` branch on GitHub (it holds two owner names in an old commit).
