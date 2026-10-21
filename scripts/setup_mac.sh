#!/usr/bin/env bash
# One-time setup on the Mac Mini. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "1/4  Checking uv (the Python tool)..."
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "2/4  Installing the app's Python packages..."
uv sync

echo "3/4  Creating your private settings file (.env) if it doesn't exist..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "     Created .env. Open it and paste your Google Maps key after GOOGLE_MAPS_API_KEY="
else
  echo "     .env already exists; leaving it alone."
fi
mkdir -p data/crexi data/hud images labels output

echo "4/4  Checking Claude Code (used for the photo check on your Claude plan)..."
if command -v claude >/dev/null 2>&1; then
  echo "     Found Claude Code at $(command -v claude)"
else
  echo "     Claude Code not found on PATH. The photo check needs it; everything else works without it."
fi

echo
echo "Done. Start the dashboard with:"
echo "  uv run run.py serve"
echo "Then open http://localhost:8765"
