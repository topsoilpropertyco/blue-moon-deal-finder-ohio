# CLAUDE.md: House Rules for Blue Moon Deal Finder

Read `PRD.md` first. It is the source of truth. If something in a request conflicts with the PRD, ask.
`ARCHITECTURE.md` describes the modules and the web API. `ROADMAP.md` lists what comes next.

## About me
- I'm Seth, not a developer. Explain what you did in plain English, briefly, after each step.
- Give me copy-paste commands to run, one block at a time.

## My setup
- A Mac Mini that is always on runs everything. Claude Code is installed and signed in on my nonprofit premium seat.
- I build with Claude Code + GitHub (account `topsoilpropertyco`), from the Mac and my phone.
- Private data (Crexi exports, owner names, photos, results) stays on the Mac Mini, never GitHub.
- The photo check uses my Claude plan through Claude Code (`vision.backend: claude_cli`), not a paid API key.
- I have Google Cloud keys. No Anthropic API key, no HUD account; HUD rents come from HUD's free map service.
- I only pay for Crexi (500 owner-contact credits a month, no roll-over). Brad (Springfield, OH) is the investor.

## Hard rules
- **Never print, log, or commit API keys.** Keys live in `.env` only. Keep `.env` in `.gitignore`. Maintain `.env.example` with blank values.
- **Ask before adding any paid service or API** not listed in PRD Section 12.
- **Before any step that costs money** (Street View images, Maps Static, Routes, vision calls), run or show a dry-run estimate and wait for my OK on runs over 50 properties.
- Never automate Crexi (no scraping, no login). Input is manual CSV/XLSX export only. Never automate people-search sites.
- Never delete data that fails a filter. Write it to `output/<market>/excluded_*.csv`.
- Cache every external API result in SQLite so re-runs don't re-pay.
- Never commit real Crexi exports, owner data, photos or the database (`.gitignore` covers them).

## How to work
- Test with `--limit 5` before full runs.
- Keep thresholds, weights, origin, and rent settings in YAML (`config.yaml`, `config/*.yaml`), not hard-coded.
- Use the exact labels: `EXTERIOR_CORRIDOR`, `DIRECT_ENTRY`, `INTERIOR_CORRIDOR`, `MIXED`, `UNKNOWN` (other asset classes: their labels in `config/asset_classes.yaml`).
- Contact levels are exactly: 1 No owner data, 2 Possible owner, 3 Confirmed owner (only a conversation confirms).
- Python 3.11+, `uv`. Small modules in `bluemoon/`. Type hints and pydantic models for anything parsed.
- Run the tests with `uv run pytest` before pushing.
- When you finish a step, update `README.md` with how to run it.
- If you're unsure about a Crexi column, show me the column names and ask.
