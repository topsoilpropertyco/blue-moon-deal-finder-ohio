#!/usr/bin/env bash
# Update the app by itself (run nightly by launchd; turn it on with: uv run run.py autoupdate install).
#
# What it does, in order:
#   0. If an earlier update was stopped halfway (power cut, restart, a step that hung), goes back to the
#      version before it first, so a version that never passed its check is never kept.
#   1. Skips quietly (and says why in the log) when the app folder is not on the main branch, or when you
#      changed a file that git tracks. It never throws away your changes. config.local.yaml and .env are
#      not tracked by git, so they never count; uv.lock is written by uv itself, so it is put back first.
#   2. Asks GitHub for anything new. Nothing new: logs "up to date" and stops.
#   3. A job is running in the app (photos, research, an import): skips tonight, so the job isn't killed.
#   4. Writes down the current version (logs/auto_update_pending), pulls the new one, installs the exact
#      packages in uv.lock (uv sync --locked) and runs a quick check: the app loads, run.py --help works,
#      and a few fast tests pass. Each step has a time limit (updates.check_timeout_minutes).
#   5. Check failed: goes back to the version it wrote down, installs packages again and logs the failure.
#      The dashboard keeps running the old version, untouched.
#   6. Check passed: restarts the dashboard and logs the new version. The team link is only restarted when
#      its own code changed, because a restart gives it a new address.
#
# The log is logs/auto_update.log (kept short) and the last result is logs/auto_update_state.json, which the
# Settings screen and `uv run run.py autoupdate status` read. Nothing here prints or reads a key.
#
# For tests: BLUEMOON_APP_DIR (the app folder), BLUEMOON_HOME (where logs/ goes), BLUEMOON_SMOKE_TESTS (the
# test files to run), BLUEMOON_UPDATE_KEEP_LINES (log length), BLUEMOON_UPDATE_TIMEOUT_MINUTES,
# BLUEMOON_RESTART_LABELS and BLUEMOON_SHARE_LABEL can be set.
set -uo pipefail

main() {
  APP_DIR="${BLUEMOON_APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
  HOME_DIR="${BLUEMOON_HOME:-$APP_DIR}"
  LOG_DIR="$HOME_DIR/logs"
  LOG="$LOG_DIR/auto_update.log"
  STATE="$LOG_DIR/auto_update_state.json"
  KEEP="${BLUEMOON_UPDATE_KEEP_LINES:-500}"
  SMOKE_TESTS="${BLUEMOON_SMOKE_TESTS:-tests/test_foundation.py tests/test_cli.py tests/test_frontend_shell.py}"
  TIMEOUT_MIN="${BLUEMOON_UPDATE_TIMEOUT_MINUTES:-20}"
  LABELS="${BLUEMOON_RESTART_LABELS:-com.bluemoon.dealfinder}"
  SHARE_LABEL="${BLUEMOON_SHARE_LABEL:-com.bluemoon.share}"
  PENDING="$LOG_DIR/auto_update_pending"                  # the version to go back to while a check runs
  RESTART_PENDING="$LOG_DIR/auto_update_restart_pending"  # new version installed, restart waits for a job
  BRANCH="main"
  # Never wait for a password prompt nobody answers, and give up on a stalled connection.
  export GIT_TERMINAL_PROMPT=0 GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=60

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

  if [ -f "$PENDING" ]; then
    local back
    back="$(tr -dc '0-9a-f' < "$PENDING")"
    if [ -n "$back" ] && git reset --hard --quiet "$back" 2>/dev/null; then
      rm -f "$PENDING"
      limit uv sync --frozen --quiet >/dev/null 2>&1 || log "Re-installing the old packages failed; run 'uv sync' by hand."
      restart_services no || true
      finish failed "An earlier update was stopped before its check finished (a power cut, a restart or a step that hung), so the app went back to $(version). It tries again at the next nightly update."
    else
      finish failed "An earlier update was stopped before its check finished, and going back didn't work. Nothing else was changed; ask for help with the app folder."
    fi
    exit 1
  fi

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  if [ "$branch" != "$BRANCH" ]; then
    finish skipped "Skipped: the app folder is on '${branch:-no branch}', not '$BRANCH'. Nothing was changed."
    exit 0
  fi
  # uv.lock is written by uv, never by hand: put it back so a re-lock doesn't block every later update.
  git checkout --quiet -- uv.lock 2>/dev/null || true
  local dirty
  dirty="$(git status --porcelain --untracked-files=no 2>/dev/null)"
  if [ -n "$dirty" ]; then
    finish skipped "Skipped: these files were changed on this Mac, and an update never overwrites your changes: $(echo "$dirty" | awk '{print $NF}' | tr '\n' ' ')"
    exit 0
  fi

  local out
  if ! step git fetch --quiet origin "$BRANCH"; then
    finish failed "Couldn't reach GitHub to check for an update (is the internet up?). Nothing was changed. $(short "$out")"
    exit 1
  fi
  local behind
  behind="$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo 0)"
  if [ "$behind" = "0" ]; then
    if [ -f "$RESTART_PENDING" ]; then
      if restart_services "$(cat "$RESTART_PENDING" 2>/dev/null)"; then
        finish up_to_date "Up to date ($(version)). The dashboard restarted to use it now that no job is running."
      else
        finish up_to_date "Up to date ($(version)). The dashboard still runs the version before because a job is running; it restarts at the next nightly update."
      fi
    else
      finish up_to_date "Up to date ($(version))."
    fi
    exit 0
  fi

  local busy
  busy="$(busy_jobs)"
  if [ "$busy" != "0" ]; then
    finish skipped "Skipped tonight: $busy job(s) are running in the app (like getting photos), and an update restarts the dashboard. Nothing was changed; it tries again at the next nightly update."
    exit 0
  fi

  local prev
  prev="$(git rev-parse HEAD)"
  log "Found $behind new change(s). Updating from $(version)."
  printf '%s\n' "$prev" > "$PENDING"     # if this run dies from here on, the next run goes back to $prev
  if ! step git pull --ff-only --quiet origin "$BRANCH"; then
    # A pull that stops leaves the files as they were (reset anyway, in case it was stopped halfway).
    git reset --hard --quiet "$prev" 2>/dev/null || true
    rm -f "$PENDING"
    finish failed "Couldn't pull the update; still on $(version). Nothing was changed. $(short "$out")"
    exit 1
  fi

  local problem=""
  # --locked / --frozen: install exactly what uv.lock says and never rewrite it (a stale uv.lock fails here).
  if ! step uv sync --locked --quiet; then
    problem="Installing the packages failed. $(short "$out")"
  elif ! step uv run --frozen python -c 'import bluemoon.web.app'; then
    problem="The new version doesn't load. $(short "$out")"
  elif ! step uv run --frozen run.py --help; then
    problem="run.py --help failed. $(short "$out")"
  elif ! step uv run --frozen pytest -q -x -o addopts= -p no:cacheprovider $SMOKE_TESTS; then
    problem="The quick tests failed. $(short "$out")"
  fi

  if [ -n "$problem" ]; then
    local new
    new="$(version)"
    # Only go back when the folder was clean before (checked above), so nothing of yours is lost.
    if [ -z "$dirty" ]; then
      git reset --hard --quiet "$prev"
      rm -f "$PENDING"
      limit uv sync --frozen --quiet >/dev/null 2>&1 || log "Re-installing the old packages also failed; run 'uv sync' by hand."
      finish failed "Update to $new failed its check, so the app went back to $(version). The dashboard kept running. $problem"
    else
      finish failed "Update to $new failed its check. $problem"
    fi
    exit 1
  fi

  rm -f "$PENDING"     # passed its check: this version stays
  # The team link only forwards to the dashboard; restart it only when its own code changed (new address).
  local share=no extra=""
  if git diff --name-only "$prev" HEAD 2>/dev/null | grep -qx 'bluemoon/share.py'; then
    share=yes
    extra=" The team link was restarted too, so it has a new address: send the new one (on Settings) to your team."
  fi
  if restart_services "$share"; then
    finish updated "Updated to $(version).$extra"
  else
    finish updated "Updated to $(version). The dashboard keeps running the version before until the running job(s) finish; it restarts at the next nightly update."
  fi
  exit 0
}

# Restart the dashboard (and the team link when $1 is yes), unless a job is running in the app: then nothing
# is restarted, the wish is written down for the next run, and it returns 1.
restart_services() {
  local n label out
  n="$(busy_jobs)"
  if [ "$n" != "0" ]; then
    printf '%s\n' "${1:-no}" > "$RESTART_PENDING"
    log "Not restarting the dashboard yet: $n job(s) are running in the app."
    return 1
  fi
  local labels="$LABELS"
  [ "${1:-no}" = "yes" ] && labels="$labels $SHARE_LABEL"
  for label in $labels; do
    if out="$(launchctl kickstart -k "gui/$(id -u)/$label" 2>&1)"; then
      log "Restarted $label."
    else
      log "Didn't restart $label (not set up on this Mac?). $(short "$out")"
    fi
  done
  rm -f "$RESTART_PENDING"
  return 0
}

# How many app jobs are queued or running (0 when it can't tell, e.g. an older version without this command).
busy_jobs() {
  local n
  n="$(limit uv run --frozen --quiet run.py autoupdate jobs 2>/dev/null | tail -n 1 | tr -d '[:space:]')"
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  echo "$n"
}

# Run a command with the time limit; a command that runs too long is stopped with everything it started
# (exit 142). macOS has no `timeout`, but it has perl.
limit() {
  if command -v perl >/dev/null 2>&1; then
    perl -e '
      my $secs = shift;
      my $pid = fork() // exit 127;
      if (!$pid) { setpgrp(0, 0); exec(@ARGV) or exit 127; }
      $SIG{ALRM} = sub { kill("KILL", -$pid); waitpid($pid, 0); exit 142; };
      alarm($secs);
      waitpid($pid, 0);
      exit(($? & 127) ? 128 + ($? & 127) : $? >> 8);
    ' "${BLUEMOON_UPDATE_TIMEOUT_SECONDS:-$((TIMEOUT_MIN * 60))}" "$@"
  else
    "$@"
  fi
}

# One step of the update: its output goes into $out; returns its exit code.
step() {
  local rc
  out="$(limit "$@" 2>&1)"; rc=$?
  [ "$rc" -eq 142 ] && out="It took longer than $TIMEOUT_MIN minutes, so it was stopped. $out"
  return "$rc"
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
