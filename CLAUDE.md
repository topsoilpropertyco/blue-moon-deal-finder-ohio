# CLAUDE.md: House Rules for Walk-Up Finder

Read `PRD.md` first. It is the source of truth. If something in a request conflicts with the PRD, ask.

## About me
- I'm Seth, not a developer. Explain what you did in plain English, briefly, after each step.
- Give me copy-paste commands to run, one block at a time.

## Hard rules
- **Never print, log, or commit API keys.** Keys live in `.env` only. Keep `.env` in `.gitignore`. Maintain `.env.example` with blank values.
- **Ask before adding any paid service or API** not listed in PRD Section 12.
- **Before any step that costs money** (Street View images, Maps Static, Routes, vision calls), run or show a dry-run estimate and wait for my OK on runs over 50 properties.
- Never automate Crexi (no scraping, no login). Input is manual CSV export only.
- Never delete data that fails a filter. Write it to `output/excluded_*.csv`.
- Cache every external API result in SQLite so re-runs don't re-pay.

## How to work
- Build in the PRD Section 17 order. Finish and test one step before starting the next.
- Test with `--limit 5` before full runs.
- Keep thresholds, weights, origin, and rent settings in `config.yaml`, not hard-coded.
- Use the exact labels: `EXTERIOR_CORRIDOR`, `DIRECT_ENTRY`, `INTERIOR_CORRIDOR`, `MIXED`, `UNKNOWN`.
- Python 3.11+, `uv`. Small modules in `walkup/`. Type hints and pydantic models for anything parsed.
- When you finish a step, update `README.md` with how to run it.
- If you're unsure about a Crexi column, show me the column names and ask.
