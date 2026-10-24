#!/usr/bin/env bash
# Install ngrok (free account), used for a team link that never changes. Safe to run again.
set -euo pipefail
if command -v ngrok >/dev/null 2>&1; then
  echo "ngrok is already installed: $(ngrok version)"
  exit 0
fi
if command -v brew >/dev/null 2>&1; then
  brew install ngrok/ngrok/ngrok
else
  echo "Homebrew isn't installed. Download ngrok from https://ngrok.com/download and put it in ~/.local/bin."
  exit 1
fi
echo "Installed: $(ngrok version)"
