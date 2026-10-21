"""Optional dashboard password.

Always set APP_PASSWORD in .env when app.host is 0.0.0.0 (run.py serve refuses to open the dashboard to
other devices without it). Left blank, the dashboard is open to anyone who can reach it, so it then runs
on this computer only. Set it and every /api and /images request needs the `bm_auth` cookie, except
/api/login and /api/health. Wrong passwords are throttled per device (app.py login).

How it works:
- POST /api/login {password} checks the password (constant-time) and sets `bm_auth` = HMAC-SHA256 of the
  password with a random per-install secret kept in data/.secret. The cookie is HttpOnly, SameSite=Lax and
  lasts 30 days. Changing APP_PASSWORD (or deleting data/.secret) logs every device out.
- GET / without a valid cookie redirects to GET /login, a small login page. Its script is the file
  /static/login.js (static files are served before login), never an inline <script>: the
  Content-Security-Policy (app.py CSP) only runs scripts from this server's files. API calls without it get
  401 {"detail": "...", "login": "/login"}; the web app can send the browser to /login on a 401.
- The password is never logged or returned.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response

from ..config import get_settings, secret

COOKIE_NAME = "bm_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600
PASSWORD_ENV = "APP_PASSWORD"
OPEN_PATHS = frozenset({"/api/login", "/api/logout", "/api/health"})
PROTECTED_PREFIXES = ("/api/", "/images/")

_lock = threading.Lock()
_secrets: dict[str, bytes] = {}


def password() -> str | None:
    """APP_PASSWORD from .env (loading settings first so .env is read). None when blank."""
    get_settings()
    return secret(PASSWORD_ENV)


def enabled() -> bool:
    return password() is not None


def protects(path: str) -> bool:
    """Does this URL path need the login cookie (when a password is set)?"""
    return path not in OPEN_PATHS and path.startswith(PROTECTED_PREFIXES)


def secret_path() -> Path:
    """data/.secret, next to the database."""
    return get_settings().path("db").parent / ".secret"


def install_secret(path: Path | None = None) -> bytes:
    """The per-install signing secret. Created once (random, readable only by this user)."""
    p = path or secret_path()
    key = str(p)
    with _lock:
        if key in _secrets:
            return _secrets[key]
        value = _read_secret(p) or _create_secret(p)
        _secrets[key] = value
        return value


def _read_secret(p: Path) -> bytes | None:
    try:
        text = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return text.encode() if len(text) >= 32 else None


def _create_secret(p: Path) -> bytes:
    p.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    tmp = p.with_name(f".secret.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.replace(tmp, p)
    return value.encode()


def token_for(pw: str, key: bytes) -> str:
    return hmac.new(key, pw.encode("utf-8"), hashlib.sha256).hexdigest()


def expected_token() -> str | None:
    pw = password()
    return token_for(pw, install_secret()) if pw else None


def check_password(candidate: str | None) -> bool:
    """Constant-time comparison with APP_PASSWORD."""
    pw = password()
    if not pw or candidate is None:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), pw.encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    """True when no password is set, or the request carries a valid cookie."""
    expected = expected_token()
    if expected is None:
        return True
    cookie = request.cookies.get(COOKIE_NAME) or ""
    return hmac.compare_digest(cookie.encode("utf-8"), expected.encode("utf-8"))


def set_cookie(response: Response, secure: bool = False) -> None:
    token = expected_token()
    if token:
        # `secure` only over https: over Tailscale the dashboard is plain http on a private network.
        response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/",
                            secure=secure)


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blue Moon Deal Finder · Log in</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6470;--line:#d9dde3;--accent:#2f5bd3;--bad:#b42318}
@media (prefers-color-scheme: dark){:root{--bg:#111318;--card:#1a1d24;--ink:#eceef2;--muted:#9aa1ad;--line:#2c313b;--accent:#7c9cff;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 16px;color:var(--muted);font-size:14px}
input{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:transparent;color:inherit}
button{width:100%;margin-top:12px;font:inherit;font-weight:600;padding:10px;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}
.err{color:var(--bad);font-size:14px;min-height:21px;margin:8px 0 0}
</style></head><body>
<form id="f"><h1>Blue Moon Deal Finder</h1><p>Enter the dashboard password.</p>
<input id="pw" type="password" autocomplete="current-password" aria-label="Password" required autofocus>
<button type="submit">Log in</button><div class="err" id="err" role="alert"></div></form>
<script src="/static/login.js"></script></body></html>
"""
