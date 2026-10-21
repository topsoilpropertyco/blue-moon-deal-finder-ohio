#!/usr/bin/env bash
# Install the git pre-commit privacy check (blocks commits that contain data from your Crexi files).
set -euo pipefail
cd "$(dirname "$0")/.."
HOOK=.git/hooks/pre-commit
mkdir -p .git/hooks
cat > "$HOOK" <<'HOOK'
#!/usr/bin/env bash
exec uv run --quiet python scripts/privacy_check.py
HOOK
chmod +x "$HOOK"
echo "Privacy check installed: commits containing data from data/crexi/ will be blocked."
