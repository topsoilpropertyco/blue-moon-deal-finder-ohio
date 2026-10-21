#!/usr/bin/env bash
# Install Cloudflare's free connector (cloudflared), used for the team link. Safe to run again.
set -euo pipefail
if command -v cloudflared >/dev/null 2>&1; then
  echo "Cloudflare's connector is already installed: $(cloudflared --version)"
  exit 0
fi
if command -v brew >/dev/null 2>&1; then
  brew install cloudflared
else
  case "$(uname -m)" in
    arm64) ARCH=arm64 ;;
    *) ARCH=amd64 ;;
  esac
  mkdir -p "$HOME/.local/bin"
  TMP="$(mktemp -d)"
  curl -fsSL -o "$TMP/cloudflared.tgz" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-$ARCH.tgz"
  tar xzf "$TMP/cloudflared.tgz" -C "$TMP"
  mv "$TMP/cloudflared" "$HOME/.local/bin/cloudflared"
  chmod +x "$HOME/.local/bin/cloudflared"
  rm -rf "$TMP"
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "Installed: $(cloudflared --version)"
