"""Block a git commit that contains owner names, addresses or parcel numbers from your Crexi files.

Runs as a git pre-commit hook (scripts/install_hooks.sh sets it up; setup_mac.sh runs that for you).
It reads the Owner Name, Address, Mailing Address and APN columns of every file in data/crexi/,
then looks for those values in the lines being added by this commit. Nothing leaves your computer.

Run it by hand:  uv run python scripts/privacy_check.py
Skip it once (only if you are sure):  git commit --no-verify
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COLUMNS = ("owner_name", "address", "mailing_address", "apn")
MIN_LEN = 8  # shorter values ("PO BOX 1", "MAIN ST") are too generic to flag


def private_values() -> set[str]:
    from bluemoon.ingest.columns import map_columns
    from bluemoon.ingest.crexi import read_table

    values: set[str] = set()
    folder = ROOT / "data" / "crexi"
    for path in sorted(folder.glob("*")):
        if path.suffix.lower() not in (".csv", ".tsv", ".xlsx", ".xlsm"):
            continue
        try:
            rows = read_table(path)
        except Exception as exc:  # a broken file should not block every commit
            print(f"privacy check: skipped {path.name} ({exc})", file=sys.stderr)
            continue
        if not rows:
            continue
        mapping, _ = map_columns(list(rows[0].keys()))
        for field in COLUMNS:
            header = mapping.get(field)
            if not header:
                continue
            for row in rows:
                v = str(row.get(header) or "").strip().upper()
                if len(v) >= MIN_LEN:
                    values.add(v)
    return values


def added_lines() -> list[tuple[str, str]]:
    diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color"], cwd=ROOT,
                          capture_output=True, text=True, check=False).stdout
    out, current = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((current, line[1:].upper()))
    return out


def main() -> int:
    values = private_values()
    if not values:
        return 0
    hits = []
    for path, text in added_lines():
        for v in values:
            if v in text:
                hits.append(path)
                break
    if hits:
        files = sorted(set(hits))
        print("Commit blocked: these files contain names, addresses or parcel numbers from your Crexi files:",
              file=sys.stderr)
        for f in files:
            print(f"  {f}", file=sys.stderr)
        print("Remove them, or use made-up examples, then commit again.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
