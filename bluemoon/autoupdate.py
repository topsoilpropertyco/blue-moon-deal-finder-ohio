"""Nightly automatic update on the Mac Mini (launchd).

`scripts/auto_update.sh` does the update (pull, quick check, roll back on failure, restart the dashboard and
the team link). This module writes and loads the LaunchAgent that runs it every night, and reads the last
result for `run.py autoupdate status` and the Settings screen. The schedule lives in config.yaml (`updates:`).
Nothing here reads or prints a key.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import REPO_ROOT, Settings, get_settings, project_root

LABEL = "com.bluemoon.autoupdate"
SCRIPT = REPO_ROOT / "scripts" / "auto_update.sh"
STANDARD_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
RESULT_WORDS = {"updated": "ok", "up_to_date": "ok, already up to date", "skipped": "skipped", "failed": "failed"}


class UpdateSettings(BaseModel):
    hour: int = 3
    minute: int = 17
    keep_log_lines: int = 500


class LastRun(BaseModel):
    """What auto_update.sh wrote in logs/auto_update_state.json."""
    when: str = ""
    result: str = ""
    message: str = ""
    commit: str = ""
    commit_date: str = ""


def update_settings(settings: Settings | None = None) -> UpdateSettings:
    raw = (settings or get_settings()).section("updates")
    return UpdateSettings(**{k: v for k, v in raw.items() if k in UpdateSettings.model_fields})


def log_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def clock(hour: int, minute: int) -> str:
    """3, 17 -> '3:17 AM'."""
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


# --- the LaunchAgent ---------------------------------------------------------------------------

def tool_path(found: dict[str, str | None] | None = None) -> str:
    """PATH for the nightly job: the folders where uv and git live on this Mac, then the usual ones."""
    found = found if found is not None else {name: shutil.which(name) for name in ("uv", "git")}
    dirs = [str(Path(exe).parent) for exe in found.values() if exe]
    dirs +=[str(Path.home() / ".local" / "bin")] + STANDARD_PATH
    out: list[str] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return ":".join(out)


def make_plist(app_dir: Path, path_env: str, hour: int, minute: int, keep_lines: int = 500) -> bytes:
    """The LaunchAgent: run scripts/auto_update.sh every night at hour:minute (local time)."""
    logs = app_dir / "logs"
    doc: dict[str, Any] = {
        "Label": LABEL,
        "WorkingDirectory": str(app_dir),
        "ProgramArguments": ["/bin/bash", str(app_dir / "scripts" / "auto_update.sh")],
        "EnvironmentVariables": {"PATH": path_env, "BLUEMOON_UPDATE_KEEP_LINES": str(keep_lines)},
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "RunAtLoad": False,
        "Umask": 63,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(logs / "auto_update.launchd.log"),
    }
    return plistlib.dumps(doc)


def launchctl(*args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("launchctl")
    if not exe:
        raise UpdateError("launchctl isn't here. Nightly updates only work on the Mac Mini.")
    return subprocess.run([exe, *args], capture_output=True, text=True, check=False)


class UpdateError(Exception):
    """A plain-English problem."""


def install(settings: Settings | None = None, app_dir: Path = REPO_ROOT) -> Path:
    """Write the LaunchAgent and load it (replacing an older one)."""
    cfg = update_settings(settings)
    if not shutil.which("uv") or not shutil.which("git"):
        raise UpdateError("Couldn't find uv and git on this Mac. Run scripts/setup_mac.sh first.")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    (app_dir / "logs").mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")   # an older copy; fine if there is none
    path.write_bytes(make_plist(app_dir, tool_path(), cfg.hour, cfg.minute, cfg.keep_log_lines))
    done = launchctl("bootstrap", domain, str(path))
    if done.returncode != 0:
        raise UpdateError(f"macOS didn't accept the nightly update job ({(done.stderr or done.stdout).strip()[:200]}).")
    return path


def uninstall() -> bool:
    """Stop and remove the LaunchAgent. True when there was one."""
    path = plist_path()
    had = path.exists()
    if shutil.which("launchctl"):
        launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    path.unlink(missing_ok=True)
    return had


def run_now(app_dir: Path = REPO_ROOT) -> int:
    """Run the update right away, in this window."""
    return subprocess.run(["/bin/bash", str(app_dir / "scripts" / "auto_update.sh")], check=False).returncode


# --- reading the result ------------------------------------------------------------------------

def current_version(app_dir: Path = REPO_ROOT) -> dict[str, str]:
    """The commit the app runs now: {'commit': 'abc1234', 'date': '2026-10-29'} (blank when git can't say)."""
    git = shutil.which("git")
    if not git:
        return {"commit": "", "date": ""}
    try:
        out = subprocess.run([git, "-C", str(app_dir), "log", "-1", "--format=%h|%cs"],
                             capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    commit, _, date = out.partition("|")
    return {"commit": commit, "date": date}


def last_run(root: Path | None = None) -> LastRun | None:
    """The last result auto_update.sh wrote, or None when it never ran (or the file is unreadable)."""
    path = log_dir(root) / "auto_update_state.json"
    try:
        return LastRun(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def when_text(stamp: str) -> str:
    """'2026-10-29T03:17:04-0400' -> ISO with a proper offset ('' when it can't be read)."""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(stamp, fmt).isoformat()
        except (TypeError, ValueError):
            continue
    return ""


def status(settings: Settings | None = None, root: Path | None = None, app_dir: Path = REPO_ROOT) -> dict[str, Any]:
    """Everything the Settings screen and `autoupdate status` show. Read-only."""
    cfg = update_settings(settings)
    last = last_run(root)
    version = current_version(app_dir)
    out: dict[str, Any] = {
        "installed": plist_path().exists(),
        "time": clock(cfg.hour, cfg.minute),
        "version": version,
        "version_text": ", ".join(x for x in (version["commit"], version["date"]) if x) or "unknown",
        "last": None,
        "log": str(log_dir(root) / "auto_update.log"),
    }
    if last:
        out["last"] = {"when": when_text(last.when), "result": last.result,
                       "result_text": RESULT_WORDS.get(last.result, last.result), "message": last.message,
                       "ok": last.result in ("updated", "up_to_date")}
    return out


def status_line(st: dict[str, Any]) -> str:
    """'Version: abc1234, 2026-10-29 · Updates itself nightly at 3:17 AM · Last update: 2026-10-29 03:17, ok'."""
    parts = [f"Version: {st['version_text']}",
             f"Updates itself nightly at {st['time']}" if st["installed"] else "Nightly updates are off"]
    last = st.get("last")
    if last:
        when = last["when"][:16].replace("T", " ") if last["when"] else "unknown time"
        parts.append(f"Last update: {when}, {last['result_text']}")
    else:
        parts.append("Last update: never")
    return " · ".join(parts)
