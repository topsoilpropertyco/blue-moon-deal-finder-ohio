#!/usr/bin/env bash
# Keep the dashboard running in the background on the Mac Mini (starts at login, restarts if it stops).
# Undo with: scripts/install_service.sh --remove
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
LABEL="com.bluemoon.dealfinder"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "--remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed the background service."
  exit 0
fi

UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
CLAUDE_DIR="$(dirname "$(command -v claude 2>/dev/null || echo /usr/local/bin/claude)")"
mkdir -p "$HOME/Library/LaunchAgents" "$APP_DIR/output"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV_BIN</string><string>run</string><string>run.py</string><string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$CLAUDE_DIR:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/output/server.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/output/server.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "The dashboard now runs in the background and starts when you log in."
echo "Open http://localhost:8765"
