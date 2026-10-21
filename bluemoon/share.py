"""Share the dashboard with your team for free, through a Cloudflare Quick Tunnel.

`uv run run.py share` starts Cloudflare's free tunnel (no account, no domain) and prints a public
https://<words>.trycloudflare.com link to the dashboard on this computer. Two ways in:

- Email codes (when the people list has emails): Cloudflare asks each visitor for their email and sends
  them a one-time code. Only the emails on the list get in. Strangers never reach this computer.
- One password (when the list is empty): everyone with the link types APP_PASSWORD from .env.

The link changes whenever the tunnel restarts (for example after the Mac restarts). The current link is
saved to data/share.json and shown on the Settings screen and by `run.py status`.

The people list lives in data/share_emails.txt (private, never in git). Change it with
`run.py share add/remove/list`, then restart sharing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings, get_settings, has_secret

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
EMAIL_RE = re.compile(r"^(\*|[A-Za-z0-9._%+'-]+)@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
CLOUDFLARED_PLACES = ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared",
                      str(Path.home() / ".local" / "bin" / "cloudflared"))


class ShareError(RuntimeError):
    """A problem Seth can fix; the message says how."""


# --- files ----------------------------------------------------------------------------------------

def _data_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    d = s.path("db").parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def emails_file(settings: Settings | None = None) -> Path:
    return _data_dir(settings) / "share_emails.txt"


def state_file(settings: Settings | None = None) -> Path:
    return _data_dir(settings) / "share.json"


def normalize_email(value: str) -> str:
    e = (value or "").strip().lower()
    if not EMAIL_RE.match(e):
        raise ShareError(f"'{value}' doesn't look like an email address. "
                         "Use name@example.com, or *@example.com for everyone at that company.")
    return e


def list_emails(settings: Settings | None = None) -> list[str]:
    path = emails_file(settings)
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line and EMAIL_RE.match(line) and line not in out:
            out.append(line)
    return out


def _write_emails(emails: list[str], settings: Settings | None = None) -> None:
    path = emails_file(settings)
    text = "# People who can open the shared dashboard (one email per line; *@company.com = everyone there).\n"
    path.write_text(text + "".join(f"{e}\n" for e in emails), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_emails(values: list[str], settings: Settings | None = None) -> list[str]:
    current = list_emails(settings)
    for v in values:
        e = normalize_email(v)
        if e not in current:
            current.append(e)
    _write_emails(current, settings)
    return current


def remove_emails(values: list[str], settings: Settings | None = None) -> list[str]:
    drop = {(v or "").strip().lower() for v in values}
    current = [e for e in list_emails(settings) if e not in drop]
    _write_emails(current, settings)
    return current


# --- running state --------------------------------------------------------------------------------

def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def current(settings: Settings | None = None) -> dict | None:
    """The live share ({url, mode, started_at, people}) or None when sharing is off."""
    path = state_file(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _pid_alive(data.get("pid")):
        return None
    return {k: data.get(k) for k in ("url", "mode", "started_at", "people", "emails")}


def _save_state(url: str, mode: str, emails: list[str], settings: Settings | None = None) -> None:
    path = state_file(settings)
    data = {"url": url, "mode": mode, "people": len(emails), "emails": list(emails), "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _clear_state(settings: Settings | None = None) -> None:
    try:
        path = state_file(settings)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def people_status(settings: Settings | None = None) -> list[dict]:
    """Everyone on the list or let in by the running link, with where they stand:
    active   = on the list and the running link lets them in
    waiting  = added, gets in after sharing restarts
    removed  = taken off the list, still gets in until sharing restarts
    listed   = on the list, sharing is off"""
    listed = list_emails(settings)
    live = current(settings)
    active = [e.lower() for e in ((live or {}).get("emails") or [])] if live and live.get("mode") == "email" else []
    out = []
    for e in listed:
        status = "listed" if not live else ("active" if e in active else "waiting")
        out.append({"email": e, "status": status})
    for e in active:
        if e not in listed:
            out.append({"email": e, "status": "removed"})
    return out


# --- request checks used by the web app ----------------------------------------------------------

def host_name(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def is_local_host(host_header: str) -> bool:
    """localhost, 127.0.0.1, ::1 or this computer's own name (opened on this computer)."""
    h = host_name(host_header)
    if h in LOCAL_HOSTS:
        return True
    me = socket.gethostname().lower()
    bare = me.removesuffix(".local")
    return h in (me, bare, f"{bare}.local")


def outside_access_ok(host_header: str, has_password: bool, settings: Settings | None = None) -> bool:
    """A request that came from outside this computer (any name but localhost) needs a lock on the door:
    the dashboard password, or a running email-code share (Cloudflare checks the code before the request
    gets here). Without either, outside visitors are refused."""
    if is_local_host(host_header) or has_password:
        return True
    live = current(settings)
    return bool(live and live.get("mode") == "email" and host_name(host_header).endswith(".trycloudflare.com"))


def client_ip(peer: str | None, headers) -> str:
    """The visitor's address for login throttling. Behind the tunnel every request comes from this computer,
    so trust Cloudflare's CF-Connecting-IP only when the direct peer is this computer."""
    peer = peer or "unknown"
    if peer in ("127.0.0.1", "::1"):
        cf = (headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf
    return peer


# --- starting the tunnel --------------------------------------------------------------------------

def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for place in CLOUDFLARED_PLACES:
        if Path(place).is_file() and os.access(place, os.X_OK):
            return place
    return None


def build_command(binary: str, port: int, emails: list[str]) -> list[str]:
    cmd = [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{int(port)}"]
    for e in emails:
        cmd += ["--allowed-mail", e]
    return cmd


def plan(settings: Settings | None = None) -> dict:
    """What `share` would do right now: mode, people, and a plain-English problem if it can't start."""
    s = settings or get_settings()
    emails = list_emails(s)
    mode = "email" if emails else "password"
    problem = None
    if not find_cloudflared():
        problem = ("Cloudflare's free connector (cloudflared) isn't installed. "
                   "Install it with: scripts/install_cloudflared.sh")
    elif mode == "password" and not has_secret("APP_PASSWORD"):
        problem = ("Nobody is on the people list and there is no dashboard password, so the link would be open "
                   "to anyone. Add people with: uv run run.py share add name@example.com "
                   "(or put a password after APP_PASSWORD= in .env).")
    return {"mode": mode, "emails": emails, "problem": problem, "port": int(s.app.get("port", 8765))}


def run(settings: Settings | None = None, on_url: Callable[[str], None] | None = None,
        echo: Callable[[str], None] | None = None) -> int:
    """Start the tunnel and keep it running until it stops or Ctrl-C. Returns cloudflared's exit code."""
    s = settings or get_settings()
    p = plan(s)
    if p["problem"]:
        raise ShareError(p["problem"])
    cmd = build_command(find_cloudflared(), p["port"], p["emails"])
    echo = echo or (lambda line: None)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    def _stop(signum, frame):  # pragma: no cover - signal path
        proc.terminate()

    old = signal.signal(signal.SIGTERM, _stop)
    url = None
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            echo(line.rstrip())
            if url is None:
                m = URL_RE.search(line)
                if m:
                    url = m.group(0)
                    _save_state(url, p["mode"], p["emails"], s)
                    if on_url:
                        on_url(url)
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            return 1
    finally:
        signal.signal(signal.SIGTERM, old)
        _clear_state(s)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(echo=print))
