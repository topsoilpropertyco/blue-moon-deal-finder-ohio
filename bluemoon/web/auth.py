"""Dashboard sign-in: the team password, plus each person's email when the team list has people.

Set APP_PASSWORD in .env (`uv run run.py share password` makes one) before the dashboard is opened to
other devices (run.py serve refuses 0.0.0.0 without it, and the team link refuses to start without it).
Left blank, the dashboard is open to anyone who can reach it, so it then runs on this computer only.
With it set, every /api and /images request needs the `bm_auth` cookie, except /api/login, /api/logout
and /api/health, and except on the Mac itself (http://localhost:8765 opened on this computer, see
share.on_this_computer). Wrong passwords are throttled per device (app.py login).

How it works:
- POST /api/login {email, password} checks the password (constant-time). When the team list
  (data/share_emails.txt, share.py) has people, the email must be on it.
- The cookie is `v2.<email in base64>.<HMAC-SHA256 of the email and password with a random per-install
  secret kept in data/.secret>`, or just the HMAC of the password when the team list is empty. HttpOnly,
  SameSite=Lax, 30 days. Changing APP_PASSWORD (or deleting data/.secret) signs every device out; taking
  an email off the team list signs that person out on their next click.
- GET / without a valid cookie redirects to GET /login, a small sign-in page. Its script is the file
  /static/login.js (static files are served before login), never an inline <script>: the
  Content-Security-Policy (app.py CSP) only runs scripts from this server's files. API calls without it get
  401 {"detail": "...", "login": "/login", "email": <ask for an email?>}; the web app then shows a sign-in box.
- The password is never logged or returned.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response

from .. import share as sharing
from ..config import get_settings, secret

COOKIE_NAME = "bm_auth"
COOKIE_VERSION = "v2"
ON_THIS_MAC = "this Mac"
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


def person_token_for(email: str, pw: str, key: bytes) -> str:
    return hmac.new(key, f"person|{email}|{pw}".encode("utf-8"), hashlib.sha256).hexdigest()


def expected_token() -> str | None:
    pw = password()
    return token_for(pw, install_secret()) if pw else None


def cookie_value(email: str | None = None) -> str | None:
    """The sign-in cookie for this person (or the plain team cookie when email is None)."""
    pw = password()
    if not pw:
        return None
    key = install_secret()
    if not email:
        return token_for(pw, key)
    e = email.strip().lower()
    tag = base64.urlsafe_b64encode(e.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{COOKIE_VERSION}.{tag}.{person_token_for(e, pw, key)}"


def check_password(candidate: str | None) -> bool:
    """Constant-time comparison with APP_PASSWORD."""
    pw = password()
    if not pw or candidate is None:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), pw.encode("utf-8"))


def _cookie_person(cookie: str, pw: str, key: bytes) -> str | None:
    """The email in a valid person cookie, or None."""
    parts = cookie.split(".")
    if len(parts) != 3 or parts[0] != COOKIE_VERSION:
        return None
    try:
        email = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    good = person_token_for(email, pw, key)
    return email if hmac.compare_digest(parts[2].encode("utf-8"), good.encode("utf-8")) else None


def signed_in_as(request: Request) -> str | None:
    """Who this request is: the person's email, "team" (password only, no team list), ON_THIS_MAC (opened
    on the Mac itself), "open" (no password set), or None (not signed in)."""
    pw = password()
    if not pw:
        return "open"
    peer = request.client.host if request.client else None
    if sharing.on_this_computer(peer, request.headers.get("host", ""), request.headers):
        return ON_THIS_MAC
    cookie = request.cookies.get(COOKIE_NAME) or ""
    if not cookie:
        return None
    key = install_secret()
    team = sharing.list_emails()
    if cookie.startswith(COOKIE_VERSION + "."):
        email = _cookie_person(cookie, pw, key)
        if email is None:
            return None
        return email if (not team or sharing.allows(email, team)) else None
    if team:            # a password-only cookie from before the team list: sign in again with an email
        return None
    return "team" if hmac.compare_digest(cookie.encode("utf-8"), token_for(pw, key).encode("utf-8")) else None


def is_authenticated(request: Request) -> bool:
    """True when no password is set, on the Mac itself, or with a valid sign-in cookie."""
    return signed_in_as(request) is not None


def asks_for_email() -> bool:
    """Does signing in need an email (the team list has people)?"""
    try:
        return bool(sharing.list_emails())
    except OSError:
        return False


def set_cookie(response: Response, secure: bool = False, email: str | None = None) -> None:
    token = cookie_value(email)
    if token:
        # `secure` only over https: over Tailscale the dashboard is plain http on a private network.
        response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/",
                            secure=secure)


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")


def login_page() -> str:
    """The sign-in page; it asks for an email too when the team list has people."""
    email = ('<input id="em" type="email" autocomplete="email" aria-label="Your email" '
             'placeholder="Your email" required autofocus>') if asks_for_email() else ""
    pw_focus = "" if email else " autofocus"
    hint = "Type your email and the team password." if email else "Type the team password."
    return (LOGIN_PAGE.replace("<!--EMAIL-->", email).replace("<!--HINT-->", hint)
            .replace("<!--PWFOCUS-->", pw_focus))


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blue Moon Deal Finder · Sign in</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6470;--line:#d9dde3;--accent:#2f5bd3;--bad:#b42318}
@media (prefers-color-scheme: dark){:root{--bg:#111318;--card:#1a1d24;--ink:#eceef2;--muted:#9aa1ad;--line:#2c313b;--accent:#7c9cff;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 16px;color:var(--muted);font-size:14px}
input{width:100%;margin-top:8px;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:transparent;color:inherit}
button{width:100%;margin-top:12px;font:inherit;font-weight:600;padding:10px;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}
.err{color:var(--bad);font-size:14px;min-height:21px;margin:8px 0 0}
</style></head><body>
<form id="f"><h1>Blue Moon Deal Finder</h1><p><!--HINT--></p>
<!--EMAIL-->
<input id="pw" type="password" autocomplete="current-password" aria-label="Team password" placeholder="Team password" required<!--PWFOCUS-->>
<button type="submit">Sign in</button><div class="err" id="err" role="alert"></div></form>
<script src="/static/login.js"></script></body></html>
"""
