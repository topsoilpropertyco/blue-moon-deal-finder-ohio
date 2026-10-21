"""The dashboard: a JSON API (FastAPI) plus the no-build web app in bluemoon/web/static.

Start it with `uv run run.py serve`, or `uv run uvicorn bluemoon.web.app:app --port 8765`.
All market routes are under /api/m/{slug}; errors come back as {"detail": "plain-English message"}:
bad input 400, unknown market/property/contact 404, a missing optional module 503, anything else 500
(the traceback goes to the server log, never to the browser). The route table is in ARCHITECTURE.md.

Optional password: set APP_PASSWORD in .env (see auth.py).
"""
from __future__ import annotations

import importlib
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any

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
from ..config import get_settings
from ..drill import credits as drill_credits
from ..drill import engine
from ..imagery import (
    claude_cli_available,
    review_queue,
    save_review,
    scorecard,
    vision_config,
)
from ..imagery import queue_counts as review_counts
from ..ingest import IngestError, import_file, preview_file
from ..scoring.learn import model_summary, set_outcome
from . import auth, queries

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
    label: str | int
    note: Text = None


class OutcomeIn(BaseModel):
    outcome: str | None = Field(default=None, max_length=40)
    note: Text = None


class JobIn(BaseModel):
    step: str = Field(max_length=40)
    limit: int | None = Field(default=None, ge=1)
    dry_run: bool = False
    confirm: bool = False


class LoginIn(BaseModel):
    password: str = Field(max_length=500)


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
            "claude_cli": bool(cli), "auth_required": auth.enabled(), "authenticated": auth.is_authenticated(request)}


@router.post("/api/login")
def login(body: LoginIn) -> Response:
    if not auth.enabled():
        return JSONResponse({"ok": True, "auth_required": False, "message": "No password is set."})
    if not auth.check_password(body.password):
        time.sleep(0.5)  # slow down guessing
        return JSONResponse({"detail": "That password isn't right."}, status_code=401)
    resp = JSONResponse({"ok": True, "auth_required": True, "message": "Logged in."})
    auth.set_cookie(resp)
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
    return queries.readable_settings()


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
               include_excluded: bool = False) -> dict:
    return queries.list_properties(conn, slug, view=view, q=q, sort=sort, direction=direction, limit=limit,
                                   offset=offset, include_excluded=include_excluded)


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
def set_contact_status(cid: int, body: ContactStatusIn, conn: Conn) -> dict:
    return engine.set_contact_status(conn, cid, body.status)


@router.post("/api/m/{slug}/properties/{pid}/outreach")
def log_outreach(slug: str, pid: int, body: OutreachIn, conn: Conn) -> dict:
    require_property(conn, slug, pid)
    return engine.log_outreach(conn, pid, body.channel, body.outcome, contact_point_id=body.contact_point_id,
                               notes=body.notes)


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
    result = save_review(conn, pid, str(body.label), body.note)
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


@router.get("/api/m/{slug}/drill")
def drill_overview(slug: str, conn: Conn) -> dict:
    return engine.overview(conn, slug)


@router.get("/api/m/{slug}/drill/stages")
def drill_stages(slug: str, conn: Conn) -> dict:
    return queries.drill_stages(conn, slug)


@router.get("/api/m/{slug}/drill/queue/{stage_key}")
def drill_queue(slug: str, stage_key: str, conn: Conn, limit: int = Query(20, ge=1, le=200),
                offset: int = Query(0, ge=0)) -> dict:
    return engine.stage_queue(conn, slug, stage_key, limit=limit, offset=offset)


@router.get("/api/m/{slug}/credits")
def credits(slug: str, conn: Conn, limit: int = Query(25, ge=0, le=1000)) -> dict:
    return queries.credits_view(conn, slug, limit=limit)


@router.get("/api/m/{slug}/review/queue")
def review_queue_view(slug: str, conn: Conn, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0),
                      include_excluded: bool = False) -> dict:
    require_market(conn, slug)
    items = review_queue(conn, slug, limit=limit, offset=offset, include_excluded=include_excluded)
    counts = review_counts(conn, slug, include_excluded=include_excluded)
    return {"total": counts["total"], "counts": counts, "limit": limit, "offset": offset, "items": items}


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
    require_market(conn, slug)
    name = upload_name(file.filename)
    folder = upload_tmp_dir() / f"preview_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
    try:
        return preview_file(save_upload(file, folder / name), market_slug=slug)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@router.post("/api/m/{slug}/import")
def import_upload(slug: str, conn: Conn, file: Annotated[UploadFile, File()],
                  limit: Annotated[int | None, Form(ge=1)] = None) -> dict:
    require_market(conn, slug)
    crexi_dir = get_settings().path("crexi_dir")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = save_upload(file, crexi_dir / f"{stamp}_{upload_name(file.filename)}")
    try:
        result = import_file(conn, slug, path, limit=limit)
    except IngestError:
        path.unlink(missing_ok=True)   # not a property list: don't leave it in data/crexi
        raise
    result["saved_as"] = path.name
    result["after_import"] = _after_import(conn, slug)
    return result


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


@router.get("/api/m/{slug}/export/{name}")
def export_file(slug: str, name: str, conn: Conn) -> FileResponse:
    if name not in EXPORT_FILES:
        raise HTTPException(404, f"There is no export called '{name}'. Choose one of: {', '.join(EXPORT_FILES)}.")
    require_market(conn, slug)
    if name == "excluded.zip":
        path = optional_function("exports", "export_zip", "Exports")(conn, slug)
    else:   # write just this file (downloading the ranked list must not stamp CRM handoffs)
        path = optional_function("exports", "export_market", "Exports")(conn, slug, only=[name]).get(name)
    if not path or not Path(path).is_file():
        raise HTTPException(404, f"{name} wasn't created for this market.")
    return FileResponse(path, media_type=EXPORT_FILES[name], filename=f"{slug}_{name}",
                        headers={"Cache-Control": "no-store"})


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
    return HTMLResponse(auth.LOGIN_PAGE, headers={"Cache-Control": "no-store"})


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


def install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _login_required(request: Request, call_next: Callable) -> Response:
        if auth.protects(request.url.path) and not auth.is_authenticated(request):
            return JSONResponse({"detail": "Please log in.", "login": "/login"}, status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def _limits_and_headers(request: Request, call_next: Callable) -> Response:
        size = request.headers.get("content-length")
        if size and size.isdigit() and int(size) > max_upload_bytes() + 1024 * 1024:
            return JSONResponse({"detail": "That upload is too big."}, status_code=413)
        response = await call_next(request)
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
    return app


app = create_app()


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the dashboard with uvicorn (host and port default to config.yaml app:)."""
    import uvicorn
    cfg = get_settings().app
    uvicorn.run(app, host=host or cfg.get("host", "127.0.0.1"), port=int(port or cfg.get("port", 8765)),
                log_level="info")
