"""Share the dashboard with your team for free, through a Cloudflare Quick Tunnel.

`uv run run.py share` starts Cloudflare's free tunnel (no account, no domain) and prints a public
https://<words>.trycloudflare.com link to the dashboard on this computer. The dashboard itself guards the
door: every visitor signs in with the team password (APP_PASSWORD in .env). When the people list has
emails, they also type their email, and only emails on the list get in, so the app knows who is who.
Adding or removing someone works at once (no restart); a removed person is signed out on their next click.

Cloudflare's own email-code gate (`cloudflared --allowed-mail`) is not used: its sign-in runs out a few
seconds after the code is accepted, so the dashboard's requests bounce to Cloudflare's login page and fail
(cloudflare/cloudflared issue #1752, open as of October 2026).

The link changes whenever the tunnel restarts (for example after the Mac restarts). The current link is
saved to data/share.json and shown on the Settings screen and by `run.py status`.

The people list lives in data/share_emails.txt and the last sign-in times in data/share_seen.json
(private, never in git). Change the list with `run.py share add/remove/list`; make the team password with
`run.py share password`.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings, get_settings, has_secret, save_secret

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


def seen_file(settings: Settings | None = None) -> Path:
    return _data_dir(settings) / "share_seen.json"


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


def allows(email: str | None, emails: list[str] | None = None) -> bool:
    """Is this email on the people list (exactly, or through a *@company.com entry)?"""
    e = (email or "").strip().lower()
    if not e or "@" not in e or e.startswith("*"):
        return False
    listed = list_emails() if emails is None else emails
    return e in listed or f"*@{e.rsplit('@', 1)[1]}" in listed


# --- who signed in, and when ----------------------------------------------------------------------

_seen_lock = threading.Lock()
_seen_written: dict[str, float] = {}     # email -> when this process last wrote it (to write rarely)
SEEN_EVERY_SECONDS = 300


def last_seen(settings: Settings | None = None) -> dict[str, str]:
    try:
        data = json.loads(seen_file(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def record_seen(email: str, settings: Settings | None = None, force: bool = False) -> None:
    """Note that this person used the dashboard now (at most every few minutes per person)."""
    e = (email or "").strip().lower()
    if not e:
        return
    now = time.monotonic()
    with _seen_lock:
        if not force and now - _seen_written.get(e, -1e9) < SEEN_EVERY_SECONDS:
            return
        _seen_written[e] = now
        data = last_seen(settings)
        data[e] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        path = seen_file(settings)
        try:
            path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:
            pass


# --- the team password ----------------------------------------------------------------------------

PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"     # no 0/o, 1/l/i: easy to read out or text


def make_password() -> str:
    """A random team password that is easy to text: bluemoon-xxxx-xxxx-xxxx (about 59 bits)."""
    groups = ["".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "bluemoon-" + "-".join(groups)


def env_file(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).root / ".env"


def save_password(value: str, settings: Settings | None = None) -> Path:
    """Write APP_PASSWORD=value into .env (replacing the old line or adding one). Private file."""
    return save_secret("APP_PASSWORD", value, settings)


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
    """Everyone on the list, with when they last used the dashboard:
    signed_in = has signed in (last_seen says when)
    not_yet   = on the list, hasn't signed in yet
    everyone  = a *@company.com entry (each person there signs in with their own email)
    People who signed in through a *@company.com entry are listed too, after it."""
    listed = list_emails(settings)
    seen = last_seen(settings)
    out = []
    for e in listed:
        if e.startswith("*@"):
            out.append({"email": e, "status": "everyone", "last_seen": None})
            domain = e[2:]
            for who in sorted(x for x in seen if x.endswith("@" + domain) and x not in listed):
                out.append({"email": who, "status": "signed_in", "last_seen": seen[who], "via": e})
        else:
            out.append({"email": e, "status": "signed_in" if e in seen else "not_yet", "last_seen": seen.get(e)})
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


def outside_access_ok(host_header: str, has_password: bool) -> bool:
    """A request that came from outside this computer (any name but localhost) needs a lock on the door:
    the team password. Without it, outside visitors are refused."""
    return is_local_host(host_header) or has_password


def on_this_computer(peer: str | None, host_header: str, headers) -> bool:
    """Opened on the Mac itself (http://localhost:8765): the direct peer is this computer, the address is
    localhost, and the request did not come through Cloudflare (which adds CF-Connecting-IP / CF-Ray and
    keeps the trycloudflare.com name). No sign-in is needed there."""
    if (peer or "") not in ("127.0.0.1", "::1"):
        return False
    if headers.get("cf-connecting-ip") or headers.get("cf-ray"):
        return False
    return is_local_host(host_header)


def client_ip(peer: str | None, headers) -> str:  # noqa: D401
    """The visitor's address for login throttling. Behind the tunnel every request comes from this computer,
    so trust Cloudflare's CF-Connecting-IP only when the direct peer is this computer."""
    peer = peer or "unknown"
    if peer in ("127.0.0.1", "::1"):
        cf = (headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf
        fwd = (headers.get("x-forwarded-for") or "").split(",")[-1].strip()   # ngrok adds the visitor last
        if fwd:
            return fwd
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


NGROK_PLACES = ("/opt/homebrew/bin/ngrok", "/usr/local/bin/ngrok", str(Path.home() / ".local" / "bin" / "ngrok"))
NGROK_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)+$")


def find_ngrok() -> str | None:
    found = shutil.which("ngrok")
    if found:
        return found
    for place in NGROK_PLACES:
        if Path(place).is_file() and os.access(place, os.X_OK):
            return place
    return None


def share_settings(settings: Settings | None = None) -> dict:
    """`share:` from config.yaml: provider (cloudflare | ngrok) and ngrok_domain (your free permanent address)."""
    raw = ((settings or get_settings()).raw.get("share") or {})
    domain = str(raw.get("ngrok_domain") or "").strip().lower().removeprefix("https://").strip("/")
    provider = str(raw.get("provider") or "cloudflare").strip().lower()
    return {"provider": provider if provider in ("cloudflare", "ngrok") else "cloudflare", "ngrok_domain": domain}


def ngrok_command(binary: str, port: int, domain: str) -> list[str]:
    return [binary, "http", str(int(port)), f"--url=https://{domain}", "--log=stdout", "--log-format=logfmt"]


def build_command(binary: str, port: int) -> list[str]:
    return [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{int(port)}"]


def plan(settings: Settings | None = None) -> dict:
    """What `share` would do right now: mode, people, and a plain-English problem if it can't start.
    mode "team" = each person types their email and the team password; "password" = just the password."""
    s = settings or get_settings()
    emails = list_emails(s)
    mode = "team" if emails else "password"
    problem = None
    cfg = share_settings(s)
    if cfg["provider"] == "ngrok":
        if not cfg["ngrok_domain"] or not NGROK_DOMAIN_RE.match(cfg["ngrok_domain"]):
            problem = ("ngrok is chosen but your free permanent address isn't set. "
                       "Set share: ngrok_domain: in config.yaml (see the README, 'A link that never changes').")
        elif not find_ngrok():
            problem = "ngrok isn't installed. Install it with: scripts/install_ngrok.sh"
        elif not has_secret("NGROK_AUTHTOKEN"):
            problem = "ngrok needs your free account token. Save it with: uv run run.py key ngrok"
    elif not find_cloudflared():
        problem = ("Cloudflare's free connector (cloudflared) isn't installed. "
                   "Install it with: scripts/install_cloudflared.sh")
    elif not has_secret("APP_PASSWORD"):
        problem = ("There is no team password yet, so the link would be open to anyone. "
                   "Make one with: uv run run.py share password")
    return {"mode": mode, "emails": emails, "problem": problem, "port": int(s.app.get("port", 8765))}


def run(settings: Settings | None = None, on_url: Callable[[str], None] | None = None,
        echo: Callable[[str], None] | None = None) -> int:
    """Start the tunnel and keep it running until it stops or Ctrl-C. Returns cloudflared's exit code."""
    s = settings or get_settings()
    p = plan(s)
    if p["problem"]:
        raise ShareError(p["problem"])
    cfg = share_settings(s)
    env = None
    if cfg["provider"] == "ngrok":
        cmd = ngrok_command(find_ngrok(), p["port"], cfg["ngrok_domain"])
        env = {**os.environ, "NGROK_AUTHTOKEN": os.environ.get("NGROK_AUTHTOKEN", "")}
    else:
        cmd = build_command(find_cloudflared(), p["port"])
    echo = echo or (lambda line: None)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)

    def _stop(signum, frame):  # pragma: no cover - signal path
        proc.terminate()

    old = signal.signal(signal.SIGTERM, _stop)
    url = None
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            echo(line.rstrip())
            if url is None:
                if cfg["provider"] == "ngrok":
                    # the address is fixed; wait until ngrok says the tunnel is up
                    m = re.search(r"msg=\"?started tunnel", line) or re.search(r"url=https://\S+", line)
                    url = f"https://{cfg['ngrok_domain']}" if m else None
                else:
                    m = URL_RE.search(line)
                    url = m.group(0) if m else None
                if url:
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
