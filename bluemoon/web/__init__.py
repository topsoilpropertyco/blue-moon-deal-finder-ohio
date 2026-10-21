"""The dashboard: the JSON API and the web app it serves.

- app.py:     FastAPI routes (ARCHITECTURE.md "Web API"), static files, errors, background re-scoring.
- queries.py: fast read models (deals list, property detail, summary, funnel, analytics).
- auth.py:    the optional password (APP_PASSWORD in .env).

Start it with `uv run run.py serve`.
"""
