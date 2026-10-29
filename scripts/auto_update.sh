#!/usr/bin/env bash
# Update the app by itself (run nightly by launchd; turn it on with: uv run run.py autoupdate install).
#
# What it does, in order:
#   1. Skips quietly (and says why in the log) when the app folder is not on the main branch, or when you
#      changed a file that git tracks. It never throws away your changes. config.local.yaml and .env are
#      not tracked by git, so they never count.
#   2. Asks GitHub for anything new. Nothing new: logs "up to date" and stops.
#   3. Remembers the current version, pulls the new one, installs packages (uv sync) and runs a quick check:
#      the app loads, run.py --help works, and a few fast tests pass.
#   4. Check failed: goes back to the version it remembered, installs packages again and logs the failure.
#      The dashboard keeps running the old version, untouched.
#   5. Check passed: restarts the dashboard and the team link, and logs the new version.
#
# The log is logs/auto_update.log (kept short) and the last result is logs/auto_update_state.json, which the
# Settings screen and `uv run run.py autoupdate status` read. Nothing here prints or reads a key.
#
# For tests: BLUEMOON_APP_DIR (the app folder), BLUEMOON_HOME (where logs/ goes), BLUEMOON_SMOKE_TESTS (the
# test files to run), BLUEMOON_UPDATE_KEEP_LINES (log length) and BLUEMOON_RESTART_LABELS can be set.
set -uo pipefail

main() {
  APP_DIR="${BLUEMOON_APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
  HOME_DIR="${BLUEMOON_HOME:-$APP_DIR}"
  LOG_DIR="$HOME_DIR/logs"
  LOG="$LOG_DIR/auto_update.log"
  STATE="$LOG_DIR/auto_update_state.json"
  KEEP="${BLUEMOON_UPDATE_KEEP_LINES:-500}"
  SMOKE_TESTS="${BLUEMOON_SMOKE_TESTS:-tests/test_foundation.py tests/test_cli.py tests/test_frontend_shell.py}"
  LABELS="${BLUEMOON_RESTART_LABELS:-com.bluemoon.dealfinder com.bluemoon.share}"
  BRANCH="main"

  umask 077
  mkdir -p "$LOG_DIR"
  cd "$APP_DIR" || { log "Can't open the app folder $APP_DIR."; exit 1; }

  # Only one update at a time (the nightly one and "autoupdate now" could meet).
  LOCK="$LOG_DIR/auto_update.lock"
  if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
      rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || true
    else
      log "Another update is running right now; skipped this one."
      exit 0
    fi
  fi
  trap 'rm -rf "$LOCK"; trim' EXIT

  log "Checking for an update."

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  if [ "$branch" != "$BRANCH" ]; then
    finish skipped "Skipped: the app folder is on '${branch:-no branch}', not '$BRANCH'. Nothing was changed."
    exit 0
  fi
  local dirty
  dirty="$(git status --porcelain --untracked-files=no 2>/dev/null)"
  if [ -n "$dirty" ]; then
    finish skipped "Skipped: these files were changed on this Mac, and an update never overwrites your changes: $(echo "$dirty" | awk '{print $NF}' | tr '\n' ' ')"
    exit 0
  fi

  local out
  if ! out="$(git fetch --quiet origin "$BRANCH" 2>&1)"; then
    finish failed "Couldn't reach GitHub to check for an update (is the internet up?). Nothing was changed. $(short "$out")"
    exit 1
  fi
  local behind
  behind="$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo 0)"
  if [ "$behind" = "0" ]; then
    finish up_to_date "Up to date ($(version))."
    exit 0
  fi

  local prev
  prev="$(git rev-parse HEAD)"
  log "Found $behind new change(s). Updating from $(version)."
  if ! out="$(git pull --ff-only --quiet origin "$BRANCH" 2>&1)"; then
    # A pull that stops leaves the files as they were.
    finish failed "Couldn't pull the update; still on $(version). Nothing was changed. $(short "$out")"
    exit 1
  fi

  local problem=""
  if ! out="$(uv sync --quiet 2>&1)"; then
    problem="Installing the packages failed. $(short "$out")"
  elif ! out="$(uv run python -c 'import bluemoon.web.app' 2>&1)"; then
    problem="The new version doesn't load. $(short "$out")"
  elif ! out="$(uv run run.py --help 2>&1)"; then
    problem="run.py --help failed. $(short "$out")"
  elif ! out="$(uv run pytest -q -x -o addopts= -p no:cacheprovider $SMOKE_TESTS 2>&1)"; then
    problem="The quick tests failed. $(short "$out")"
  fi

  if [ -n "$problem" ]; then
    local new
    new="$(version)"
    # Only go back when the folder was clean before (checked above), so nothing of yours is lost.
    if [ -z "$dirty" ]; then
      git reset --hard --quiet "$prev"
      uv sync --quiet >/dev/null 2>&1 || log "Re-installing the old packages also failed; run 'uv sync' by hand."
      finish failed "Update to $new failed its check, so the app went back to $(version). The dashboard kept running. $problem"
    else
      finish failed "Update to $new failed its check. $problem"
    fi
    exit 1
  fi

  local label
  for label in $LABELS; do
    if out="$(launchctl kickstart -k "gui/$(id -u)/$label" 2>&1)"; then
      log "Restarted $label."
    else
      log "Didn't restart $label (not set up on this Mac?). $(short "$out")"
    fi
  done
  finish updated "Updated to $(version)."
  exit 0
}

stamp() { date +%Y-%m-%dT%H:%M:%S%z; }

log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S')  $*"
  echo "$line"
  [ -n "${LOG:-}" ] && echo "$line" >> "$LOG"
  return 0
}

# The last 3 lines of a command's output, on one line, so the log stays short.
short() { printf '%s' "$1" | tail -n 3 | tr '\n' ' ' | cut -c1-400; }

version() { git log -1 --format='%h, %cs' 2>/dev/null || echo "unknown"; }

json_str() {  # a JSON string: escape backslashes and quotes, drop control characters
  printf '"%s"' "$(printf '%s' "$1" | tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}

finish() {  # result, message: log it and write the state file the Settings screen reads
  local result="$1" message="$2"
  log "$message"
  local commit date
  commit="$(git log -1 --format=%h 2>/dev/null || echo "")"
  date="$(git log -1 --format=%cs 2>/dev/null || echo "")"
  {
    printf '{"when": %s, "result": %s, "message": %s, "commit": %s, "commit_date": %s}\n' \
      "$(json_str "$(stamp)")" "$(json_str "$result")" "$(json_str "$message")" \
      "$(json_str "$commit")" "$(json_str "$date")"
  } > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
}

trim() {  # keep only the last $KEEP lines of the log (and of launchd's own error file)
  local f
  for f in "$LOG" "$LOG_DIR/auto_update.launchd.log"; do
    if [ -f "$f" ] && [ "$(wc -l < "$f")" -gt "$KEEP" ]; then
      tail -n "$KEEP" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    fi
  done
}

# Everything above is read before anything runs, so pulling a new copy of this file mid-run is safe.
main "$@"
