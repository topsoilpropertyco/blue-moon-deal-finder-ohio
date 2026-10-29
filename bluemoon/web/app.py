"""The dashboard: a JSON API (FastAPI) plus the no-build web app in bluemoon/web/static.

Start it with `uv run run.py serve`, or `uv run uvicorn bluemoon.web.app:app --port 8765`.
All market routes are under /api/m/{slug}; errors come back as {"detail": "plain-English message"}:
bad input 400, unknown market/property/contact 404, a missing optional module 503, anything else 500
(the traceback goes to the server log, never to the browser). The route table is in ARCHITECTURE.md.

Optional password: set APP_PASSWORD in .env (see auth.py).
"""
from __future__ import annotations

import asyncio
import importlib
import ipaddress
import logging
import math
import os
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field

from .. import __version__, db
from .. import share as sharing
from ..config import get_settings
from ..drill import credits as drill_credits
from ..drill import engine
from ..drill import research as drill_research
from ..imagery import (
    claude_cli_available,
    review_queue,
    save_review,
    scorecard,
    vision_config,
)
from ..imagery import photos_all
from ..imagery import queue_counts as review_counts
from ..imagery import streetview
from ..ingest import IngestError, import_file, preview_file
from ..scoring import overrides
from ..scoring.learn import model_summary, set_outcome
from . import auth, crexi_api, queries

log = logging.getLogger("bluemoon.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
NO_CACHE = {"Cache-Control": "no-cache"}
UPLOAD_EXTENSIONS = (".csv", ".xlsx", ".xlsm", ".tsv")
EXPORT_FILES: dict[str, str] = {
    "ranked_list.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ranked_list.csv": "text/csv",
    "crm_handoff.csv": "text/csv",
    "mailing_list.csv": "text/csv",
    "drill_worklist.csv": "text/csv",
    "excluded.zip": "application/zip",
    "run_summary.md": "text/markdown",
}
IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.(?:jpe?g|png|webp)$", re.IGNORECASE)
MAX_TEXT = 4000

# Content-Security-Policy, sent with every response (it matters on the HTML pages and scripts). Scripts only
# from this server and never inline (login.js, core.js and the views are files; no onclick= attributes), so
# injected markup can't run code. Styles allow 'unsafe-inline' because the views set bar widths and a few
# spacings with style="..." attributes (and the login page has its own small <style>); the stylesheets are
# this server's plus Google Fonts' CSS, whose font files come from fonts.gstatic.com. Pictures are this
# server's (/images, the favicon) or data:/blob: URLs; fetch() only reaches this server; no other site may
# frame the dashboard.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


# --- database ------------------------------------------------------------------------------------

_db_lock = threading.Lock()
_db_ready: set[tuple[str, int]] = set()


def open_db() -> sqlite3.Connection:
    """A connection for one request (WAL is on, so reads never block on a running job).

    The first connection to a database in this process goes through bluemoon.db.connect(), which creates the
    schema and syncs the markets from config.yaml. Later ones skip that, because it writes, and a write would
    wait for any job that is saving.
    """
    s = get_settings()
    path = s.path("db")
    key = (str(path), id(s))
    with _db_lock:
        if key not in _db_ready or not path.exists():
            conn = db.connect(settings=s)
            _db_ready.add(key)
            return conn
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = open_db()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


# --- background re-scoring -----------------------------------------------------------------------

class Rescorer:
    """Re-score a market shortly after the last photo review or outcome, off the request thread.

    Reviews come fast (one key press each), and re-scoring 3,000 properties takes about a second, so the
    work waits `web.rescore_delay_seconds` after the last change and then runs once. A delay of 0 re-scores
    right away, inside the request (tests use this).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._running: dict[str, threading.Lock] = {}

    def schedule(self, slug: str) -> str:
        delay = float(queries.web_config()["rescore_delay_seconds"] or 0)
        if delay <= 0:
            self._run(slug)
            return "done"
        with self._lock:
            old = self._timers.pop(slug, None)
            if old:
                old.cancel()
            timer = threading.Timer(delay, self._fire, args=(slug,))
            timer.daemon = True
            self._timers[slug] = timer
            timer.start()
        return "scheduled"

    def flush(self) -> None:
        """Run every waiting re-score now (on shutdown, so no change is lost)."""
        with self._lock:
            pending = list(self._timers.items())
            self._timers.clear()
        for slug, timer in pending:
            timer.cancel()
            self._run(slug)

    def _fire(self, slug: str) -> None:
        with self._lock:
            self._timers.pop(slug, None)
        self._run(slug)

    def run_now(self, slug: str, conn: sqlite3.Connection) -> None:
        """Re-score right now, inside the request, on its connection (after a buy-box decision, so the answer
        it returns is already right). Waits for a background re-score of the same market to finish first, so
        that one can't save over the decision. Errors are raised, not swallowed."""
        from ..scoring.rank import compute_scores
        with self._lock:
            running = self._running.setdefault(slug, threading.Lock())
        with running:
            compute_scores(conn, slug)

    def _run(self, slug: str) -> None:
        from ..scoring.rank import compute_scores
        with self._lock:
            running = self._running.setdefault(slug, threading.Lock())
        with running:   # never two re-scores of one market at once
            conn = open_db()
            try:
                compute_scores(conn, slug)
            except Exception:  # a failed background re-score must never break the dashboard
                log.exception("Background re-score of %s failed", slug)
            finally:
                conn.close()


rescorer = Rescorer()


# --- optional modules (written separately; the app must start without them) ----------------------

def optional_module(name: str, what: str) -> ModuleType:
    try:
        return importlib.import_module(f"bluemoon.{name}")
    except ImportError as exc:
        log.warning("bluemoon.%s could not be loaded: %s", name, exc)
        raise HTTPException(503, f"{what} isn't available: bluemoon/{name}.py is missing or didn't load. "
                                 "Update the app and restart it.") from None


def optional_function(name: str, attr: str, what: str) -> Callable[..., Any]:
    fn = getattr(optional_module(name, what), attr, None)
    if not callable(fn):
        raise HTTPException(503, f"{what} isn't available: bluemoon/{name}.py has no {attr}(). "
                                 "Update the app and restart it.")
    return fn


# --- request bodies ------------------------------------------------------------------------------

Text = Annotated[str | None, Field(max_length=MAX_TEXT)]
Name = Annotated[str | None, Field(max_length=300)]


class ContactItem(BaseModel):
    channel: str = Field(max_length=40)
    value: str = Field(max_length=500)
    notes: Text = None
    person_name: Name = None


class AttemptIn(BaseModel):
    stage_key: str = Field(max_length=80)
    outcome: str = Field(max_length=40)
    detail: Text = None
    person: Name = None
    person_role: Name = None
    contacts: list[ContactItem] | None = Field(default=None, max_length=50)


class ContactIn(BaseModel):
    channel: str = Field(max_length=40)
    value: str = Field(max_length=500)
    source: Name = None
    person_name: Name = None
    notes: Text = None


class ContactStatusIn(BaseModel):
    status: str = Field(max_length=40)
    allow_contact: bool = False     # 'Allow contact again': the only way to take a contact off do-not-contact


class FindingStatusIn(BaseModel):
    status: str = Field(max_length=20)      # wrong | kept


class OutreachIn(BaseModel):
    channel: str = Field(max_length=40)
    outcome: str = Field(max_length=80)
    contact_point_id: int | None = None
    notes: Text = None


class LevelIn(BaseModel):
    action: str = Field(max_length=40)
    person: Name = None
    note: Text = None


class PersonIn(BaseModel):
    name: str = Field(max_length=300)
    role: Name = None
    source: Name = None
    notes: Text = None


class CreditIn(BaseModel):
    result: str | None = Field(default=None, max_length=40)
    person: Name = None
    detail: Text = None
    contacts: list[ContactItem] | None = Field(default=None, max_length=50)


class ReviewIn(BaseModel):
    label: str | int | None = None      # optional with agreed=True ("Yes, the AI is right")
    note: Text = None
    agreed: bool | None = None          # True: the AI is right; False: it's wrong and `label` is the right answer


class OutcomeIn(BaseModel):
    outcome: str | None = Field(default=None, max_length=40)
    note: Text = None


class GroupDecideIn(BaseModel):
    """The Why? panel's Decide button: one set-aside group, one choice (checked in overrides.decide_group)."""
    bucket: str = Field(max_length=120)
    decision: str = Field(max_length=20)       # add_only | add_all | keep | clear
    note: Text = None


class BuyboxIn(BaseModel):
    """The drawer's buy-box buttons (checked in overrides.decide_property)."""
    decision: str = Field(max_length=20)       # include | exclude | rules
    reason: str = Field(default="*", max_length=40)
    note: Text = None


class UndoIn(BaseModel):
    batch_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class JobIn(BaseModel):
    step: str = Field(max_length=40)
    limit: int | None = Field(default=None, ge=1)
    dry_run: bool = False
    confirm: bool = False


class LoginIn(BaseModel):
    password: str = Field(max_length=500)
    email: str | None = Field(default=None, max_length=320)


def _contacts(items: list[ContactItem] | None) -> list[dict] | None:
    return [c.model_dump() for c in items] if items is not None else None


# --- helpers -------------------------------------------------------------------------------------

def require_market(conn: sqlite3.Connection, slug: str) -> dict:
    return db.market_by_slug(conn, slug)  # KeyError -> 404


def require_property(conn: sqlite3.Connection, slug: str, pid: int) -> None:
    if not queries.property_in_market(conn, slug, pid):
        raise HTTPException(404, f"Property {pid} is not in this market.")


def safe_child(base: Path, rel: str) -> Path | None:
    """`base/rel` if it is an existing file inside `base` (no '..', no hidden files), else None."""
    if not rel or "\x00" in rel or any(part.startswith(".") for part in Path(rel).parts):
        return None
    root = base.resolve()
    target = (root / rel).resolve()
    if root not in target.parents or not target.is_file():
        return None
    return target


def upload_name(filename: str | None) -> str:
    """A safe file name for an upload, or 400 with what to do instead."""
    name = Path(filename or "").name
    ext = Path(name).suffix.lower()
    if ext == ".xls":
        raise HTTPException(400, "Old .xls files can't be read. Open it in Excel, save it as .xlsx or .csv, "
                                 "and upload that.")
    if ext not in UPLOAD_EXTENSIONS:
        raise HTTPException(400, "Upload the Crexi export as a .csv, .xlsx, .xlsm or .tsv file.")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._")[:80] or "upload"
    return stem + ext


def max_upload_bytes() -> int:
    return int(float(queries.web_config()["upload_max_mb"]) * 1024 * 1024)


def save_upload(upload: UploadFile, dest: Path) -> Path:
    """Stream the upload to `dest` (atomically), enforcing the size limit."""
    limit = max_upload_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")
    written = 0
    try:
        with tmp.open("wb") as out:
            while chunk := upload.file.read(1 << 20):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"That file is over {limit // (1024 * 1024)} MB. "
                                             "Export fewer rows from Crexi at a time.")
                out.write(chunk)
        if written == 0:
            raise HTTPException(400, "That file is empty.")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def upload_tmp_dir() -> Path:
    """Private scratch space for previews (data/tmp, git-ignored)."""
    d = get_settings().path("db").parent / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- routes: health, login, markets, settings ----------------------------------------------------

router = APIRouter()


@router.get("/api/health")
def health(request: Request) -> dict:
    ok = True
    try:
        with closing(open_db()) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        log.exception("Health check: the database did not open")
        ok = False
    try:
        backend, cli = vision_config().backend, claude_cli_available()
    except Exception:  # noqa: BLE001 - a broken vision setting must not break the health check
        backend, cli = "unknown", False
    return {"ok": ok, "version": __version__, "keys": queries.key_status(), "vision_backend": backend,
            "claude_cli": bool(cli), "auth_required": auth.enabled(), "authenticated": auth.is_authenticated(request),
            "signed_in_as": auth.signed_in_as(request), "login_email": auth.asks_for_email(), "share": share_status()}


def share_status() -> dict:
    """The team link (free Cloudflare tunnel): on/off, the link, how people sign in, and who has signed in."""
    try:
        live = sharing.current()
        p = sharing.plan()
        people = sharing.people_status()
    except Exception:  # noqa: BLE001 - sharing problems must never break the dashboard
        log.debug("Could not read the share state", exc_info=True)
        return {"on": False}
    return {"on": bool(live), "url": (live or {}).get("url"), "started_at": (live or {}).get("started_at"),
            "mode": p["mode"], "people": len(p["emails"]), "people_list": people,
            "password_set": auth.enabled(), "cloudflared": sharing.find_cloudflared() is not None,
            "problem": None if live else p["problem"]}


# Login guessing: each device (IP address) gets `app.login_max_failures` wrong passwords, then has to wait a
# minute, doubling with every further wrong password up to `app.login_lock_max_minutes`. Per device only, so
# nobody on the network can lock Seth's phone out; devices already logged in keep their cookie either way.
LOGIN_DELAY_SECONDS = 0.5
DEFAULT_LOGIN_MAX_FAILURES = 5
DEFAULT_LOGIN_LOCK_MAX_MINUTES = 15
_login_lock = threading.Lock()
_login_failures: dict[str, tuple[int, float, float]] = {}   # ip -> (wrong passwords, locked until, last try)
_login_clock = time.monotonic


async def _login_pause() -> None:
    """Slow down guessing without holding a worker thread."""
    await asyncio.sleep(LOGIN_DELAY_SECONDS)


def _login_limits() -> tuple[int, float]:
    cfg = get_settings().app
    try:
        max_failures = max(1, int(cfg.get("login_max_failures") or DEFAULT_LOGIN_MAX_FAILURES))
        lock_max = max(1.0, float(cfg.get("login_lock_max_minutes") or DEFAULT_LOGIN_LOCK_MAX_MINUTES)) * 60
    except (TypeError, ValueError):
        max_failures, lock_max = DEFAULT_LOGIN_MAX_FAILURES, DEFAULT_LOGIN_LOCK_MAX_MINUTES * 60.0
    return max_failures, lock_max


def _login_locked_for(ip: str, now: float) -> float:
    """Seconds this device still has to wait (0 = it may try). Also forgets devices quiet for an hour."""
    with _login_lock:
        for key, (_, until, last) in list(_login_failures.items()):
            if now - max(until, last) > 3600:
                del _login_failures[key]
        _, until, _ = _login_failures.get(ip, (0, 0.0, 0.0))
    return max(0.0, until - now)


def _login_failed(ip: str, now: float) -> None:
    max_failures, lock_max = _login_limits()
    with _login_lock:
        failures, until, _ = _login_failures.get(ip, (0, 0.0, 0.0))
        failures += 1
        if failures >= max_failures:
            until = now + min(60.0 * 2 ** (failures - max_failures), lock_max)
        _login_failures[ip] = (failures, until, now)


@router.post("/api/login")
async def login(body: LoginIn, request: Request) -> Response:
    if not auth.enabled():
        return JSONResponse({"ok": True, "auth_required": False, "message": "No password is set."})
    ip = sharing.client_ip(request.client.host if request.client else None, request.headers)
    wait = _login_locked_for(ip, _login_clock())
    if wait > 0:   # not even checked while this device waits
        minutes = max(1, math.ceil(wait / 60))
        return JSONResponse({"detail": f"Too many wrong passwords. Try again in {minutes} "
                                       f"minute{'s' if minutes != 1 else ''}."},
                            status_code=429, headers={"Retry-After": str(math.ceil(wait))})
    if not auth.check_password(body.password):
        _login_failed(ip, _login_clock())
        await _login_pause()
        return JSONResponse({"detail": "That password isn't right."}, status_code=401)
    with _login_lock:
        _login_failures.pop(ip, None)
    email = (body.email or "").strip().lower() or None
    team = sharing.list_emails()
    if team and not email:
        return JSONResponse({"detail": "Type your email too (the one Seth added to the team list).",
                             "email": True}, status_code=400)
    if email and team and not sharing.allows(email, team):
        return JSONResponse({"detail": f"{email} isn't on the team list. Ask Seth to add it.", "email": True},
                            status_code=403)
    if email and not sharing.EMAIL_RE.match(email):
        return JSONResponse({"detail": "That email doesn't look right.", "email": True}, status_code=400)
    if email:
        sharing.record_seen(email, force=True)
    resp = JSONResponse({"ok": True, "auth_required": True, "message": "Signed in.", "email": email})
    auth.set_cookie(resp, secure=request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto", "").lower() == "https", email=email)
    return resp


@router.post("/api/logout")
def logout() -> Response:
    resp = JSONResponse({"ok": True, "message": "Logged out."})
    auth.clear_cookie(resp)
    return resp


@router.get("/api/markets")
def list_markets(conn: Conn) -> list[dict]:
    return queries.markets(conn)


@router.get("/api/settings")
def settings_view() -> dict:
    out = queries.readable_settings()
    out["share"] = share_status()
    out["updates"] = update_status()
    return out


def update_status() -> dict | None:
    """The version and the nightly update's last result (read-only). None when it can't be read."""
    try:
        from .. import autoupdate
        st = autoupdate.status()
        st["line"] = autoupdate.status_line(st)
        st.pop("log", None)   # the folder path is not needed on screen
        return st
    except Exception:  # noqa: BLE001 - update status must never break the Settings screen
        log.debug("Could not read the update status", exc_info=True)
        return None


@router.get("/api/sources")
def sources() -> dict:
    return queries.sources_catalog()


# --- routes: one market --------------------------------------------------------------------------

@router.get("/api/m/{slug}/summary")
def market_summary(slug: str, conn: Conn) -> dict:
    return queries.summary(conn, slug)


@router.get("/api/m/{slug}/properties")
def properties(slug: str, conn: Conn, view: str = "all", q: str | None = Query(None, max_length=200),
               sort: str = "rank", direction: str | None = Query(None, alias="dir"),
               limit: int | None = Query(None, ge=1), offset: int = Query(0, ge=0),
               include_excluded: bool = False, bucket: str | None = Query(None, max_length=120)) -> dict:
    return queries.list_properties(conn, slug, view=view, q=q, sort=sort, direction=direction, limit=limit,
                                   offset=offset, include_excluded=include_excluded, bucket=bucket)


# --- routes: buy-box decisions by hand -----------------------------------------------------------

def _actor(request: Request) -> str:
    """Who made a decision: an email, 'this Mac', 'team' or 'open' (auth.signed_in_as)."""
    return auth.signed_in_as(request) or "unknown"


def _decided(conn: sqlite3.Connection, slug: str, result: dict) -> dict:
    """Add the refreshed Why? panel and the buy-box count to a decision's result."""
    mc = queries.market_ctx(conn, slug)
    result["set_aside"] = queries.set_aside(conn, mc)
    result["in_buybox"] = int(db.scalar(conn, f"SELECT COUNT(*) FROM properties p LEFT JOIN scores s ON "
                                              f"s.property_id = p.id WHERE p.market_id = ? AND {queries.IN_BOX}",
                                        (mc.id,)) or 0)
    return result


@router.post("/api/m/{slug}/buybox/decide")
def buybox_decide(slug: str, body: GroupDecideIn, request: Request, conn: Conn) -> dict:
    require_market(conn, slug)
    result = overrides.decide_group(conn, slug, body.bucket, body.decision, _actor(request), note=body.note,
                                    rescore=lambda: rescorer.run_now(slug, conn))
    return _decided(conn, slug, result)


@router.post("/api/m/{slug}/buybox/undo")
def buybox_undo(slug: str, body: UndoIn, request: Request, conn: Conn) -> dict:
    require_market(conn, slug)
    result = overrides.undo(conn, slug, body.batch_id, _actor(request), rescore=lambda: rescorer.run_now(slug, conn))
    return _decided(conn, slug, result)


@router.post("/api/m/{slug}/properties/{pid}/buybox")
def property_buybox(slug: str, pid: int, body: BuyboxIn, request: Request, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    result = overrides.decide_property(conn, slug, pid, body.decision, _actor(request), reason=body.reason,
                                       note=body.note, rescore=lambda: rescorer.run_now(slug, conn))
    return {**result, "row": queries.property_row_by_id(conn, slug, pid),
            "buybox": queries.buybox_detail(conn, slug, pid)}


@router.get("/api/m/{slug}/properties/{pid}")
def property_detail(slug: str, pid: int, conn: Conn) -> dict:
    return queries.property_detail(conn, slug, pid)


@router.post("/api/m/{slug}/properties/{pid}/attempt")
def record_attempt(slug: str, pid: int, body: AttemptIn, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    return engine.record_attempt(conn, pid, body.stage_key, body.outcome, detail=body.detail, person=body.person,
                                 person_role=body.person_role, contacts=_contacts(body.contacts))


@router.post("/api/m/{slug}/properties/{pid}/contacts")
def add_contact(slug: str, pid: int, body: ContactIn, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    return engine.add_contact(conn, pid, body.channel, body.value, source=body.source,
                              person_name=body.person_name, notes=body.notes)


@router.post("/api/m/{slug}/properties/{pid}/people")
def add_person(slug: str, pid: int, body: PersonIn, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    return engine.add_person(conn, pid, body.name, role=body.role, source=body.source, notes=body.notes)


@router.patch("/api/contacts/{cid}")
def set_contact_status(cid: int, body: ContactStatusIn, conn: Conn, request: Request) -> dict:
    return engine.set_contact_status(conn, cid, body.status, allow_contact=body.allow_contact,
                                     actor=auth.signed_in_as(request))


@router.patch("/api/research/findings/{fid}")
def mark_research_finding(fid: int, body: FindingStatusIn, conn: Conn, request: Request) -> dict:
    """An AI research finding is wrong (or not after all). Who said so is kept with it."""
    return drill_research.mark_finding(conn, fid, body.status, actor=auth.signed_in_as(request))


@router.post("/api/m/{slug}/properties/{pid}/outreach")
def log_outreach(slug: str, pid: int, body: OutreachIn, conn: Conn, request: Request) -> dict:
    require_property(conn, slug, pid)
    return engine.log_outreach(conn, pid, body.channel, body.outcome, contact_point_id=body.contact_point_id,
                               notes=body.notes, actor=auth.signed_in_as(request))


@router.post("/api/m/{slug}/properties/{pid}/level")
def level_action(slug: str, pid: int, body: LevelIn, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    return engine.level_action(conn, pid, body.action, person=body.person, note=body.note)


@router.post("/api/m/{slug}/properties/{pid}/credit")
def use_credit(slug: str, pid: int, body: CreditIn, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    return drill_credits.use_credit(conn, pid, body.result, contacts=_contacts(body.contacts), person=body.person,
                                    detail=body.detail)


@router.post("/api/m/{slug}/properties/{pid}/review")
def review(slug: str, pid: int, body: ReviewIn, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    result = save_review(conn, pid, None if body.label is None else str(body.label), body.note, body.agreed)
    result["rescore"] = rescorer.schedule(slug)
    return result


@router.post("/api/m/{slug}/properties/{pid}/outcome")
def outcome(slug: str, pid: int, body: OutcomeIn, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    result = set_outcome(conn, pid, body.outcome, body.note)
    result["rescore"] = rescorer.schedule(slug)
    saved = queries.OUTCOME_LABELS.get(result.get("outcome") or "")
    result["message"] = (f"Saved: {saved}." if saved else "Outcome cleared.") + " Scores update in a moment."
    return result


DrillScope = Annotated[str, Query(pattern="^(buybox|all)$",
                                  description="buybox (default): buy-box owners only; all: every owner")]


@router.get("/api/m/{slug}/drill")
def drill_overview(slug: str, conn: Conn, scope: DrillScope = "buybox") -> dict:
    from ..crexi import batches as crexi_batches
    out = engine.overview(conn, slug, scope=scope)
    out["crexi"] = crexi_batches.drill_box(conn, slug)      # the DRILL screen's small Crexi box
    return out


@router.get("/api/m/{slug}/drill/stages")
def drill_stages(slug: str, conn: Conn) -> dict:
    return queries.drill_stages(conn, slug)


@router.get("/api/m/{slug}/drill/queue/{stage_key}")
def drill_queue(slug: str, stage_key: str, conn: Conn, limit: int = Query(20, ge=1, le=200),
                offset: int = Query(0, ge=0), scope: DrillScope = "buybox") -> dict:
    return engine.stage_queue(conn, slug, stage_key, limit=limit, offset=offset, scope=scope)


@router.get("/api/m/{slug}/credits")
def credits(slug: str, conn: Conn, limit: int = Query(25, ge=0, le=1000)) -> dict:
    return queries.credits_view(conn, slug, limit=limit)


@router.get("/api/m/{slug}/review/queue")
def review_queue_view(slug: str, conn: Conn, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0),
                      include_excluded: bool = False,
                      scope: Annotated[str | None, Query(pattern="^(everything|unsure)$")] = None) -> dict:
    """scope: everything (the AI's unsure ones first, then each call it made) or unsure (default: review.default_scope)."""
    require_market(conn, slug)
    items = review_queue(conn, slug, limit=limit, offset=offset, include_excluded=include_excluded, scope=scope)
    counts = review_counts(conn, slug, include_excluded=include_excluded, scope=scope)
    return {"total": counts["total"], "scope": counts["scope"], "counts": counts, "limit": limit, "offset": offset,
            "items": items}


@router.get("/api/m/{slug}/photos/all")
def photos_every_lead(slug: str, conn: Conn, estimate: bool = False,
                      limit: Annotated[int | None, Query(ge=1)] = None) -> dict:
    """The one photo button: "312 of 2,981 done" and, with estimate=1, what pressing it would do and cost
    (a dry run: nothing is called). limit: the estimate for a test on that many properties."""
    require_market(conn, slug)
    out = photos_all.status(conn, slug)
    if estimate and out.get("enabled"):
        est = photos_all.estimate(conn, slug, limit)
        est.pop("details", None)
        est.pop("status", None)
        out["estimate"] = est
    return out


@router.get("/api/m/{slug}/review/scorecard")
def review_scorecard(slug: str, conn: Conn) -> dict:
    return scorecard(conn, slug)


@router.get("/api/m/{slug}/funnel")
def funnel(slug: str, conn: Conn) -> dict:
    return queries.funnel(conn, slug)


@router.get("/api/m/{slug}/analytics")
def analytics(slug: str, conn: Conn) -> dict:
    return queries.analytics(conn, slug)


@router.get("/api/m/{slug}/model")
def model(slug: str, conn: Conn) -> dict:
    return model_summary(conn, slug)


# --- routes: import, pipeline, jobs, exports -----------------------------------------------------

@router.post("/api/m/{slug}/import/preview")
def import_preview(slug: str, conn: Conn, file: Annotated[UploadFile, File()]) -> dict:
    """What importing the file would do. A Blue Moon export (ours, edited in Excel) shows its changes."""
    require_market(conn, slug)
    name = upload_name(file.filename)
    folder = upload_tmp_dir() / f"preview_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
    try:
        return preview_file(save_upload(file, folder / name), market_slug=slug, conn=conn)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


MAX_PHOTO_BYTES = 12 * 1024 * 1024


@router.post("/api/m/{slug}/properties/{pid}/photo")
def add_photo(slug: str, pid: int, conn: Conn, file: Annotated[UploadFile, File()]) -> dict:
    """A photo you found yourself (a Google search, the property's website). JPEG or PNG; the photo check reads
    it first and the review card shows it. Nothing is deleted or replaced."""
    require_property(conn, slug, pid)
    data = file.file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, f"That photo is over {MAX_PHOTO_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise HTTPException(400, "That file is empty.")
    name = streetview.add_your_image(get_settings(), pid, data)
    return {"name": name, "images": streetview.images_for(pid, get_settings()),
            "message": "Photo added. It shows first, and the photo check reads it first."}


@router.post("/api/m/{slug}/import")
def import_upload(slug: str, request: Request, conn: Conn, file: Annotated[UploadFile, File()],
                  limit: Annotated[int | None, Form(ge=1)] = None) -> dict:
    """Import a Crexi (or other) export, or a Blue Moon export with Seth's edits (who: the signed-in person)."""
    require_market(conn, slug)
    crexi_dir = get_settings().path("crexi_dir")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = save_upload(file, crexi_dir / f"{stamp}_{upload_name(file.filename)}")
    try:
        result = import_file(conn, slug, path, limit=limit, actor=_actor(request))
    except IngestError:
        path.unlink(missing_ok=True)   # not a property list: don't leave it in data/crexi
        raise
    result["saved_as"] = path.name
    result["after_import"] = _after_import(conn, slug)
    result["queued"] = _queue_rent_after_import(conn, slug)
    if result["queued"] and isinstance(result["after_import"], dict):
        result["after_import"]["message"] = (
            (result["after_import"].get("message") or "").rstrip() + " The free HUD rent check is running in the "
            "background (see the Run screen); scores update when it finishes.").strip()
    return result


def _queue_rent_after_import(conn: sqlite3.Connection, slug: str) -> list[dict]:
    """Start the free HUD rent check (it re-scores when it finishes) as a background job, when the asset
    class uses rent and config.yaml rent.after_import is on. Never fails the import."""
    try:
        s = get_settings()
        market = require_market(conn, slug)
        if not s.asset_class(market["asset_class"]).rent_enabled or not (s.section("rent") or {}).get(
                "after_import", True):
            return []
        start = optional_function("jobs", "start_job", "Background jobs")
        return [{"step": "rent", "job_id": start("rent", slug, {})}]
    except Exception:
        log.exception("Could not queue the rent check after an import for %s", slug)
        return []


def _after_import(conn: sqlite3.Connection, slug: str) -> dict:
    """Owners, free drive estimates, score and DRILL right after an import. Never fails the import."""
    try:
        run = optional_function("pipeline", "after_import", "Ranking the new import")
    except HTTPException as exc:
        return {"ok": False, "message": f"Imported. {exc.detail} Then run Owners, Score and DRILL."}
    try:
        return run(conn, slug)
    except Exception as exc:
        log.exception("after_import failed for %s", slug)
        return {"ok": False, "message": f"Imported, but ranking it failed ({exc.__class__.__name__}). "
                                        "Run Owners, Score and DRILL from the Run screen."}


@router.get("/api/m/{slug}/estimate")
def estimate(slug: str, conn: Conn, limit: int | None = Query(None, ge=1)) -> dict:
    require_market(conn, slug)
    return optional_function("pipeline", "estimate", "Cost estimates")(conn, slug, limit=limit)


@router.post("/api/m/{slug}/jobs")
def start_job(slug: str, body: JobIn, conn: Conn) -> dict:
    require_market(conn, slug)
    start = optional_function("jobs", "start_job", "Background jobs")
    job_id = start(body.step, slug, {"limit": body.limit, "dry_run": body.dry_run, "confirm": body.confirm})
    job = optional_function("jobs", "get_job", "Background jobs")(job_id)
    return {"job_id": job_id, "job": job}


@router.get("/api/jobs")
def list_jobs(conn: Conn, market: str | None = None, limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    if market:
        require_market(conn, market)
    return list(optional_function("jobs", "list_jobs", "Background jobs")(market or None, limit) or [])


@router.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    job = optional_function("jobs", "get_job", "Background jobs")(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} was not found.")
    return job


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    job = optional_function("jobs", "cancel_job", "Background jobs")(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} was not found.")
    return job


def _export_response(slug: str, name: str, path: Any) -> FileResponse:
    if not path or not Path(path).is_file():
        raise HTTPException(404, f"{name} wasn't created for this market.")
    return FileResponse(path, media_type=EXPORT_FILES[name], filename=f"{slug}_{name}",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/m/{slug}/export/review.xlsx")
def export_review(slug: str, conn: Conn, bucket: str = Query(..., max_length=120)) -> FileResponse:
    """One set-aside group as a small workbook to check and fix in Excel, then import back on the Import
    screen. Registered before /export/{name}, which would otherwise take this path."""
    require_market(conn, slug)
    path = optional_function("exports", "export_review", "Exports")(conn, slug, bucket)
    if not path or not Path(path).is_file():
        raise HTTPException(404, "The review file wasn't created.")
    return FileResponse(path, media_type=EXPORT_FILES["ranked_list.xlsx"], filename=f"{slug}_{Path(path).name}",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/m/{slug}/export/{name}")
def export_file(slug: str, name: str, conn: Conn) -> FileResponse:
    """Download one export. A GET never changes anything: crm_handoff.csv is written without marking anyone
    as handed off (POST it to do that)."""
    if name not in EXPORT_FILES:
        raise HTTPException(404, f"There is no export called '{name}'. Choose one of: {', '.join(EXPORT_FILES)}.")
    require_market(conn, slug)
    if name == "excluded.zip":
        path = optional_function("exports", "export_zip", "Exports")(conn, slug)
    else:   # write just this file
        path = optional_function("exports", "export_market", "Exports")(
            conn, slug, only=[name], stamp_handoffs=False).get(name)
    return _export_response(slug, name, path)


class HandoffIn(BaseModel):
    mark: bool = True


@router.post("/api/m/{slug}/export/crm_handoff.csv")
def export_handoff(slug: str, body: HandoffIn, conn: Conn) -> FileResponse:
    """The CRM handoff list; with mark (the default) the newly confirmed owners in it are marked as handed off.
    The JSON body means another website can't send this (it would need a CORS preflight)."""
    require_market(conn, slug)
    name = "crm_handoff.csv"
    path = optional_function("exports", "export_market", "Exports")(
        conn, slug, only=[name], stamp_handoffs=body.mark).get(name)
    return _export_response(slug, name, path)


# --- routes: photos and the web app --------------------------------------------------------------

@router.get("/images/{pid}/{file}")
def image(pid: int, file: str) -> FileResponse:
    if pid < 0 or not IMAGE_NAME.match(file):
        raise HTTPException(404, "No such photo.")
    target = safe_child(get_settings().path("images") / str(int(pid)), file)
    if target is None:
        raise HTTPException(404, "No such photo.")
    return FileResponse(target, headers={"Cache-Control": "private, max-age=86400"})


PLACEHOLDER_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Blue Moon Deal Finder</title></head>
<body style="font:16px/1.5 system-ui,sans-serif;padding:24px;max-width:560px">
<h1>Blue Moon Deal Finder</h1><p>The server is running, but the web app files are missing
(bluemoon/web/static/index.html). Update the app and restart it.</p></body></html>"""


@router.get("/", include_in_schema=False)
def index(request: Request) -> Response:
    if not auth.is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        return HTMLResponse(PLACEHOLDER_PAGE, headers=NO_CACHE)
    return FileResponse(page, media_type="text/html", headers=NO_CACHE)


@router.get("/login", include_in_schema=False)
def login_page(request: Request) -> Response:
    if not auth.enabled() or auth.is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(auth.login_page(), headers={"Cache-Control": "no-store"})


@router.get("/static/{path:path}", include_in_schema=False)
def static_file(path: str) -> FileResponse:
    target = safe_child(STATIC_DIR, path)
    if target is None:
        raise HTTPException(404, "Not found.")
    cache = NO_CACHE if target.suffix.lower() in (".js", ".css", ".html", ".json") else \
        {"Cache-Control": "public, max-age=3600"}
    return FileResponse(target, headers=cache)


@router.get("/{name}", include_in_schema=False)
def top_level_static_file(name: str) -> FileResponse:
    """Files at the top of the static folder also answer at /name (favicon.ico, or styles.css linked
    relative to index.html). Registered last, so it never hides an API route."""
    return static_file(name)


# --- errors and middleware -----------------------------------------------------------------------

def _key_error_text(exc: KeyError) -> str:
    text = exc.args[0] if exc.args else ""
    return str(text) if text else "Not found."


def _validation_text(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "The request wasn't filled in right."
    first = errors[0]
    where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path", "form"))
    return f"{where or 'Request'}: {first.get('msg', 'is not valid')}."


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _validation_text(exc)}, status_code=422)

    @app.exception_handler(ValueError)          # includes IngestError; messages are written for Seth
    async def _bad_input(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc) or "That request isn't valid."}, status_code=400)

    @app.exception_handler(KeyError)            # unknown market, property, contact
    async def _not_found(request: Request, exc: KeyError) -> JSONResponse:
        log.info("404 for %s: %r", request.url.path, exc)
        return JSONResponse({"detail": _key_error_text(exc)}, status_code=404)

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_error(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return JSONResponse({"detail": "The database is busy saving a long job. Try again in a moment."},
                                status_code=503)
        log.exception("Database error on %s", request.url.path, exc_info=exc)
        return JSONResponse({"detail": "The database had a problem. Details are in the server log."},
                            status_code=500)


WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host_name(host_header: str) -> str:
    """'Mac-Mini.local:8765' -> 'mac-mini.local'; '[::1]:8765' -> '::1'."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def host_allowed(host_header: str) -> bool:
    """Is this a name the dashboard may be opened under? Raw IP addresses (DNS rebinding needs a domain name),
    localhost, this computer's own name, Tailscale names and app.allowed_hosts in config.yaml."""
    h = _host_name(host_header)
    if not h or h in ("localhost", "testserver") or h.endswith((".ts.net", ".localhost", ".trycloudflare.com", ".ngrok-free.app", ".ngrok-free.dev", ".ngrok.app", ".ngrok.dev", ".ngrok.io")):
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    bare = me.removesuffix(".local")
    if h in (me, bare, f"{bare}.local"):
        return True
    extra = get_settings().app.get("allowed_hosts") or []
    return h in {str(x).strip().lower() for x in ([extra] if isinstance(extra, str) else extra)}


def install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _login_required(request: Request, call_next: Callable) -> Response:
        if auth.protects(request.url.path):
            who = auth.signed_in_as(request)
            if who is None:
                return JSONResponse({"detail": "Please sign in.", "login": "/login", "email": auth.asks_for_email()},
                                    status_code=401)
            if "@" in who:
                sharing.record_seen(who)
        return await call_next(request)

    @app.middleware("http")                     # runs before the login check
    async def _same_site_only(request: Request, call_next: Callable) -> Response:
        """Refuse look-alike host names (DNS rebinding) and writes sent by another website. Requests with no
        Origin header (the command line, curl) still work; the dashboard's own requests send a matching one."""
        host = request.headers.get("host", "")
        if not host_allowed(host):
            return JSONResponse({"detail": "This address isn't allowed. Add it to app.allowed_hosts in "
                                           "config.yaml."}, status_code=400)
        if not sharing.outside_access_ok(host, auth.enabled()):
            return JSONResponse({"detail": "This dashboard isn't open to other devices yet. On the Mac Mini, make "
                                           "a team password with 'uv run run.py share password', then restart "
                                           "the dashboard."}, status_code=403)
        origin = request.headers.get("origin")
        if request.method in WRITE_METHODS and origin is not None and \
                urlsplit(origin).netloc.lower() != host.strip().lower():
            return JSONResponse({"detail": "Blocked a request from another website."}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def _limits_and_headers(request: Request, call_next: Callable) -> Response:
        if "transfer-encoding" in request.headers:
            # Browsers always say how big an upload is; h11 ignores Content-Length when this is present, so a
            # chunked body could get past the size limit until it was all on disk.
            return JSONResponse({"detail": "Uploads must say their size. Use the Import screen."}, status_code=411)
        size = request.headers.get("content-length")
        if size and size.isdigit() and int(size) > max_upload_bytes() + 1024 * 1024:
            return JSONResponse({"detail": "That upload is too big."}, status_code=413)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.middleware("http")                     # outermost: last line of defence
    async def _unexpected(request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            log.exception("Unexpected error on %s %s", request.method, request.url.path)
            return JSONResponse({"detail": f"Something went wrong on the server ({exc.__class__.__name__}). "
                                           "Details are in the server log."}, status_code=500)


# --- the app -------------------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        open_db().close()   # create the schema and markets once, before the first request
    except Exception:
        log.exception("The database could not be opened at startup")
    try:
        importlib.import_module("bluemoon.jobs").recover_interrupted_jobs()
    except Exception:       # no jobs module yet, or the jobs table is busy: it also checks on first use
        log.debug("Could not check for interrupted jobs at startup", exc_info=True)
    yield
    rescorer.flush()


def create_app() -> FastAPI:
    app = FastAPI(title="Blue Moon Deal Finder", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    install_error_handlers(app)
    install_middleware(app)
    app.include_router(router)
    app.include_router(crexi_api.router)      # Crexi bulk batches (bluemoon/web/crexi_api.py)
    return app


app = create_app()


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the dashboard with uvicorn (host and port default to config.yaml app:)."""
    import uvicorn
    cfg = get_settings().app
    uvicorn.run(app, host=host or cfg.get("host", "127.0.0.1"), port=int(port or cfg.get("port", 8765)),
                log_level="info")
