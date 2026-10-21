#!/usr/bin/env bash
# Keep the dashboard running in the background on the Mac Mini (starts at login, restarts if it stops).
#   scripts/install_service.sh            the dashboard
#   scripts/install_service.sh --share    the dashboard + the free Cloudflare team link
#   scripts/install_service.sh --remove   stop and remove both
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DASH="com.bluemoon.dealfinder"
SHARE="com.bluemoon.share"

unload() { launchctl unload "$AGENTS/$1.plist" 2>/dev/null || true; rm -f "$AGENTS/$1.plist"; }

if [ "${1:-}" = "--remove" ]; then
  unload "$DASH"; unload "$SHARE"
  echo "Removed the background services. The dashboard and the team link are off."
  exit 0
fi

UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
CLAUDE_DIR="$(dirname "$(command -v claude 2>/dev/null || echo /usr/local/bin/claude)")"
CF_DIR="$(dirname "$(command -v cloudflared 2>/dev/null || echo "$HOME/.local/bin/cloudflared")")"
mkdir -p "$AGENTS" "$APP_DIR/output"

write_agent() {  # label, command word, log file
  cat > "$AGENTS/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV_BIN</string><string>run</string><string>run.py</string><string>$2</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$CLAUDE_DIR:$CF_DIR:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>$APP_DIR/output/$3</string>
  <key>StandardErrorPath</key><string>$APP_DIR/output/$3</string>
</dict>
</plist>
PLIST
  launchctl unload "$AGENTS/$1.plist" 2>/dev/null || true
  launchctl load "$AGENTS/$1.plist"
}

write_agent "$DASH" serve server.log
echo "The dashboard now runs in the background and starts when you log in: http://localhost:8765"

if [ "${1:-}" = "--share" ]; then
  if ! command -v cloudflared >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/cloudflared" ]; then
    echo "Cloudflare's connector isn't installed yet. Run scripts/install_cloudflared.sh, then this again."
    exit 1
  fi
  write_agent "$SHARE" share share.log
  echo "The team link now runs in the background too. In about 20 seconds, see it with:"
  echo "  uv run run.py share list"
  echo "(or on the dashboard's Settings screen). It changes when the Mac restarts; send the new one to your team."
fi

echo
echo "So it also comes back after a power cut or an update restart, turn on automatic login:"
echo "System Settings > Users & Groups > Automatically log in as > your name (hidden when FileVault is on;"
echo "then log in once at the Mac after a restart)."
