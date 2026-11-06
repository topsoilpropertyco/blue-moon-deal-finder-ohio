"""Google sign-in for the LOI, the "installed app" way: a browser window on this computer, Google sends a one-time
code back to a page this program opens for a moment, and the program trades it for a long-lived token.

Two files in data/ (never in git): google_oauth_client.json (the client Seth creates in his own Google Cloud) and
google_token.json (made by `uv run run.py loi signin`; readable by this user only). The token is never printed or
logged, and it is only sent to Google.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..config import Settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Full Drive access is needed because the master is a file Seth made himself: the narrower drive.file scope only
# reaches files this app created. Docs is for filling the {{tokens}}.
SCOPES = ("https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/documents")
CLIENT_FILE = "google_oauth_client.json"
TOKEN_FILE = "google_token.json"
EARLY = 60            # seconds before expiry that a token is renewed


class SignInError(ValueError):
    """Something Seth can fix, in plain English."""


def _data_dir(settings: Settings) -> Path:
    return settings.path("db").parent


def client_path(settings: Settings) -> Path:
    return _data_dir(settings) / CLIENT_FILE


def token_path(settings: Settings) -> Path:
    return _data_dir(settings) / TOKEN_FILE


def load_client(settings: Settings) -> dict[str, str]:
    path = client_path(settings)
    if not path.exists():
        raise SignInError(f"The Google client file is missing. Save it as data/{CLIENT_FILE} (README: 'Letters from "
                          "your Google Doc', step 3).")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        info = raw.get("installed") or raw.get("web") or {}
        return {"client_id": info["client_id"], "client_secret": info["client_secret"]}
    except (OSError, ValueError, KeyError, AttributeError):
        raise SignInError(f"data/{CLIENT_FILE} isn't a Google client file. Download it again from Google Cloud "
                          "(Credentials, your Desktop app client, Download JSON).") from None


def _refresh_token(settings: Settings) -> str | None:
    try:
        return json.loads(token_path(settings).read_text(encoding="utf-8")).get("refresh_token") or None
    except (OSError, ValueError, AttributeError):
        return None


def signed_in(settings: Settings) -> bool:
    return _refresh_token(settings) is not None


def _save_token(settings: Settings, refresh_token: str) -> None:
    path = token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"refresh_token": refresh_token, "scopes": list(SCOPES)}, f)
    os.chmod(path, 0o600)


def _token_call(http: httpx.Client, data: dict[str, str]) -> dict:
    """Ask Google's token service. The answer's body is never put in an error: it can hold the token."""
    try:
        res = http.post(TOKEN_URL, data=data)
    except httpx.HTTPError:
        raise SignInError("Couldn't reach Google. Check the internet connection and try again.") from None
    if res.status_code != 200:
        raise SignInError("Google turned the sign-in down. Run 'uv run run.py loi signin' again.")
    try:
        return res.json()
    except ValueError:
        raise SignInError("Google's answer wasn't readable. Try 'uv run run.py loi signin' again.") from None


class Credentials:
    """The access token for Drive and Docs calls, renewed from the saved refresh token when it runs out."""

    def __init__(self, settings: Settings, http: httpx.Client):
        self.settings, self.http = settings, http
        self._token: str | None = None
        self._expires = 0.0

    def bearer(self) -> str:
        if self._token and time.time() < self._expires - EARLY:
            return self._token
        refresh = _refresh_token(self.settings)
        if not refresh:
            raise SignInError("Google sign-in needed. Run 'uv run run.py loi signin' on the Mac Mini.")
        client = load_client(self.settings)
        got = _token_call(self.http, {"grant_type": "refresh_token", "refresh_token": refresh, **client})
        if not got.get("access_token"):
            raise SignInError("Google sign-in needed. Run 'uv run run.py loi signin' on the Mac Mini.")
        self._token = str(got["access_token"])
        self._expires = time.time() + float(got.get("expires_in") or 0)
        return self._token


def _wait_for_code(server: HTTPServer, state: str, timeout: float) -> str:
    """Serve one page at a time until Google's redirect arrives with the code (or the wait runs out)."""
    server.timeout = 1.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        server.handle_request()
        got = getattr(server, "answer", None)
        if got is not None:
            if got.get("state") != [state]:
                server.answer = None                         # not ours: ignore it and keep waiting
                continue
            if "error" in got or "code" not in got:
                raise SignInError("Google sign-in was cancelled or turned down. Run it again when ready.")
            return got["code"][0]
    raise SignInError("Nobody finished the sign-in in time. Run 'uv run run.py loi signin' again.")


class _Landing(BaseHTTPRequestHandler):
    def do_GET(self) -> None:   # the method name http.server looks for
        query = parse_qs(urlsplit(self.path).query)
        if "code" in query or "error" in query:
            self.server.answer = query                      # type: ignore[attr-defined]
        body = b"<p>You can close this tab and go back to the Terminal.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:           # the address holds the one-time code: keep it quiet
        pass


def sign_in(settings: Settings, *, http: httpx.Client | None = None,
            open_page: Callable[[str], object] = webbrowser.open, say: Callable[[str], None] = print,
            timeout: float = 300) -> None:
    """Sign in to Google in the browser and save the token. open_page gets the address to visit."""
    client = load_client(settings)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), _Landing)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    say("Opening Google's sign-in page. If nothing opens, copy this address into a browser on this computer:")
    say(url)
    own = http is None
    http = http or httpx.Client(timeout=30)
    try:
        threading.Thread(target=open_page, args=(url,), daemon=True).start()
        code = _wait_for_code(server, state, timeout)
        got = _token_call(http, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                 "code_verifier": verifier, **client})
        if not got.get("refresh_token"):
            raise SignInError("Google didn't give a lasting sign-in. Remove this app at "
                              "myaccount.google.com/permissions and run the sign-in again.")
        _save_token(settings, str(got["refresh_token"]))
    finally:
        server.server_close()
        if own:
            http.close()
