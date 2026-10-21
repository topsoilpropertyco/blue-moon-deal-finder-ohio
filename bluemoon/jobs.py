"""Background jobs: run pipeline steps without making the dashboard wait.

    job_id = start_job("drive", "apartments-springfield-oh", {"limit": 5})
    get_job(job_id)        # {id, kind, label, market, status, progress, message, params, result, ...}
    list_jobs("apartments-springfield-oh")
    cancel_job(job_id)

- One job runs per market at a time; the others wait in line. Different markets run side by side.
- Each job opens its own database connection.
- Cancelling: a waiting job is dropped at once. A running job stops at its next progress update (the
  step's progress callback raises `Cancelled`); work it already saved stays saved.
- Jobs left 'running' or 'queued' by an app that is no longer running are marked as errors
  ("Interrupted (the app restarted)") the first time this process looks at the jobs table.

Progress lives in memory, so it is always current for jobs in this process, and is copied to the jobs
table when the database is free. A step can hold the database's write lock for a moment while it saves,
and a progress update must never wait for that.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import db, pipeline
from .config import Settings, get_settings, secret
from .enrich.geo import redact

log = logging.getLogger(__name__)

STATUSES = ("queued", "running", "done", "error", "cancelled")
ACTIVE = ("queued", "running")
INTERRUPTED = "Interrupted (the app restarted)"
NOT_STARTED = "Not started (the app restarted). Start it again."
CANCELLED = "Cancelled. Work saved before you cancelled is kept."
PROGRESS_EVERY_SECONDS = 0.5     # copy progress to the database at most this often
PROGRESS_LOCK_WAIT = 0.05        # seconds a progress write may wait for the database before skipping
MAX_LIST = 200
PID_KEY = "_pid"                 # stored in params_json so another process can tell whose job it is

Progress = Callable[[float, str], None]
Execute = Callable[[sqlite3.Connection, str, str, "JobParams", Progress], dict]


class Cancelled(BaseException):
    """Raised inside a job's progress callback after someone cancels it.

    A BaseException on purpose: the backend steps guard their progress calls with `except Exception`
    (a broken progress bar must never stop a run), and a cancel has to get through those guards.
    """


class JobParams(BaseModel):
    """What a job was asked to do. Unknown keys are ignored."""
    model_config = ConfigDict(extra="ignore")
    limit: int | None = Field(None, ge=1)
    dry_run: bool = False
    confirm: bool = False
    path: str | list[str] | None = None


@dataclass
class _Job:
    id: int
    kind: str
    market: str
    params: JobParams
    settings: Settings
    status: str = "queued"
    progress: float = 0.0
    message: str = ""
    result: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    last_write: float = 0.0


def run_pipeline_step(conn: sqlite3.Connection, kind: str, market_slug: str, params: JobParams,
                      progress: Progress) -> dict:
    """The default job body: one pipeline step."""
    return pipeline.run_step(conn, market_slug, kind, limit=params.limit, dry_run=params.dry_run,
                             confirm=params.confirm, progress=progress, path=params.path)


class JobRunner:
    """Runs jobs on background threads, one at a time per market. Use the module functions normally."""

    def __init__(self, execute: Execute | None = None):
        self._execute = execute or run_pipeline_step
        self._lock = threading.Lock()
        self._jobs: dict[int, _Job] = {}              # queued or running in this process
        self._queues: dict[str, deque[int]] = {}      # market slug -> waiting job ids
        self._workers: dict[str, threading.Thread] = {}
        self._checked: set[Path] = set()              # databases already checked for interrupted jobs

    # --- public ------------------------------------------------------------------------------

    def start(self, kind: str, market_slug: str, params: dict | None = None) -> int:
        """Queue a job and return its id. ValueError (plain English) for a bad step, market or parameter."""
        step = pipeline.normalize_step(kind)
        job_params = _parse_params(params)
        settings = get_settings()
        self._prepare(settings)
        with _open(settings) as conn:
            market = conn.execute("SELECT id FROM markets WHERE slug = ?", (market_slug,)).fetchone()
            if market is None:
                raise ValueError(f"Unknown market '{market_slug}'.")
            with self._lock:
                busy = market_slug in self._workers
            message = "Waiting for the job ahead of it in this market" if busy else "Starting"
            stored = {**job_params.model_dump(exclude_none=True), PID_KEY: os.getpid()}
            cur = conn.execute("INSERT INTO jobs(kind, market_id, status, progress, message, params_json, created_at) "
                               "VALUES(?, ?, 'queued', 0, ?, ?, ?)",
                               (step, market["id"], message, db.dumps(stored), db.now_iso()))
            job_id = int(cur.lastrowid)
        self._enqueue(_Job(job_id, step, market_slug, job_params, settings, message=message))
        return job_id

    def get(self, job_id: int) -> dict | None:
        settings = get_settings()
        self._check_once(settings)
        row = _fetch(settings, "WHERE j.id = ?", (int(job_id),), 1)
        return self._view(row[0], with_result=True) if row else None

    def list(self, market_slug: str | None = None, limit: int = 20, *, with_results: bool = False) -> list[dict]:
        """Newest first. Results are left out unless asked for (get() has them), to keep the list small."""
        settings = get_settings()
        self._check_once(settings)
        where, params = ("WHERE m.slug = ?", (market_slug,)) if market_slug else ("", ())
        found = _fetch(settings, where, params, max(1, min(MAX_LIST, int(limit or 20))))
        return [self._view(r, with_result=with_results) for r in found]

    def cancel(self, job_id: int) -> dict | None:
        """Drop a waiting job, or ask a running one to stop at its next progress update."""
        dropped: _Job | None = None
        with self._lock:
            job = self._jobs.get(int(job_id))
            if job is not None and job.status == "queued":
                self._queues.get(job.market, deque()).remove(job.id)
                job.status, job.message = "cancelled", CANCELLED
                dropped = job
            elif job is not None and job.status == "running":
                job.cancel.set()
                job.message = "Stopping at the next safe point..."
        if dropped is not None:
            self._finish(dropped, "cancelled", CANCELLED)
        return self.get(job_id)

    def wait(self, job_id: int, timeout: float | None = None) -> dict | None:
        """Block until the job has finished (or the timeout passes) and return it."""
        with self._lock:
            job = self._jobs.get(int(job_id))
        if job is not None:
            job.finished.wait(timeout)
        return self.get(job_id)

    def recover(self, settings: Settings | None = None) -> int:
        """Mark jobs whose app is no longer running as interrupted. Returns how many were marked."""
        s = settings or get_settings()
        if not s.path("db").exists():
            return 0
        marked = 0
        with _open(s) as conn:
            unfinished = conn.execute("SELECT id, status, params_json FROM jobs "
                                      "WHERE status IN ('queued', 'running')").fetchall()
            for row in unfinished:
                if row["id"] in self._jobs or _owner_alive(row["params_json"]):
                    continue
                message = INTERRUPTED if row["status"] == "running" else NOT_STARTED
                conn.execute("UPDATE jobs SET status = 'error', message = ?, finished_at = ? WHERE id = ?",
                             (message, db.now_iso(), row["id"]))
                marked += 1
        if marked:
            log.warning("Marked %d unfinished job(s) from an earlier run as interrupted.", marked)
        return marked

    # --- running -----------------------------------------------------------------------------

    def _enqueue(self, job: _Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            self._queues.setdefault(job.market, deque()).append(job.id)
            if job.market not in self._workers:
                worker = threading.Thread(target=self._work, args=(job.market,), daemon=True,
                                          name=f"job-{job.market}")
                self._workers[job.market] = worker
                worker.start()

    def _work(self, market: str) -> None:
        """Run this market's jobs one after another until its line is empty."""
        while True:
            with self._lock:
                queue = self._queues.get(market)
                if not queue:
                    self._queues.pop(market, None)
                    self._workers.pop(market, None)
                    return
                job = self._jobs[queue.popleft()]
                job.status, job.message = "running", "Starting"
            try:
                self._run(job)
            except Exception:  # never let one job's bookkeeping stop this market's line
                log.exception("Job %s could not be finished cleanly", job.id)
                with self._lock:
                    self._jobs.pop(job.id, None)
                job.finished.set()

    def _run(self, job: _Job) -> None:
        _write(job.settings, job.id, status="running", progress=0.0, message=job.message)
        try:
            result = self._call(job)
        except Cancelled:
            self._finish(job, "cancelled", CANCELLED)
        except Exception as exc:  # the job fails, the app carries on
            log.exception("Job %s (%s, %s) failed", job.id, job.kind, job.market)
            self._finish(job, "error", _error_text(exc))
        else:
            ok = result.get("ok", True)
            self._finish(job, "done" if ok else "error", result.get("message") or "Done.", result,
                         progress=1.0 if ok else None)

    def _call(self, job: _Job) -> dict:
        conn = db.connect(settings=job.settings)
        try:
            return self._execute(conn, job.kind, job.market, job.params, self._reporter(job))
        finally:
            conn.close()  # rolls back anything a cancelled step had not committed

    def _reporter(self, job: _Job) -> Progress:
        def report(fraction: float, message: str) -> None:
            if job.cancel.is_set():
                raise Cancelled()
            job.progress = max(0.0, min(1.0, float(fraction)))
            job.message = str(message)[:500]
            now = time.monotonic()
            if now - job.last_write >= PROGRESS_EVERY_SECONDS:
                job.last_write = now
                _write(job.settings, job.id, wait=PROGRESS_LOCK_WAIT, only_if_running=True,
                       progress=job.progress, message=job.message)
        return report

    def _finish(self, job: _Job, status: str, message: str, result: dict | None = None,
                progress: float | None = None) -> None:
        with self._lock:
            job.status, job.message, job.result = status, message, result
            if progress is not None:
                job.progress = progress
        fields: dict[str, Any] = {"status": status, "message": message, "finished_at": db.now_iso(),
                                  "progress": job.progress}
        if result is not None:
            fields["result_json"] = db.dumps(result)
        _write(job.settings, job.id, **fields)
        with self._lock:
            self._jobs.pop(job.id, None)
        job.finished.set()

    # --- reading -----------------------------------------------------------------------------

    def _view(self, row: dict, *, with_result: bool) -> dict:
        params = db.loads(row.get("params_json"), {}) or {}
        params.pop(PID_KEY, None)
        view = {
            "id": row["id"], "kind": row["kind"], "label": pipeline.step_label(row["kind"]),
            "market": row.get("market_slug"), "status": row["status"], "progress": float(row["progress"] or 0.0),
            "message": row.get("message") or "", "params": params,
            "created_at": row["created_at"], "finished_at": row.get("finished_at"),
        }
        if with_result:
            view["result"] = db.loads(row.get("result_json"))
        with self._lock:
            live = self._jobs.get(view["id"])
            if live is not None:  # this process runs it: memory is newer than the table
                view.update(status=live.status, progress=live.progress, message=live.message)
                if with_result and live.result is not None:
                    view["result"] = live.result
        view["done"] = view["status"] not in ACTIVE
        return view

    def _prepare(self, settings: Settings) -> None:
        """Make sure the database exists, then check it for interrupted jobs (once per process)."""
        if not settings.path("db").exists():
            db.connect(settings=settings).close()
        self._check_once(settings)

    def _check_once(self, settings: Settings) -> None:
        path = settings.path("db")
        with self._lock:
            if path in self._checked:
                return
            self._checked.add(path)
        try:
            self.recover(settings)
        except sqlite3.Error:
            log.warning("Could not check for interrupted jobs", exc_info=True)


# --- database helpers --------------------------------------------------------------------------

@contextmanager
def _open(settings: Settings, timeout: float = 30.0) -> Iterator[sqlite3.Connection]:
    """A short-lived plain connection for the jobs table; commits on success and always closes.
    (db.connect() is not used here because it also syncs the markets table, which writes.)"""
    conn = sqlite3.connect(settings.path("db"), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fetch(settings: Settings, where: str, params: tuple, limit: int) -> list[dict]:
    if not settings.path("db").exists():
        return []
    with _open(settings) as conn:
        found = conn.execute(f"SELECT j.*, m.slug AS market_slug FROM jobs j LEFT JOIN markets m ON m.id = j.market_id "
                             f"{where} ORDER BY j.id DESC LIMIT ?", (*params, limit)).fetchall()
    return [dict(r) for r in found]


def _write(settings: Settings, job_id: int, *, wait: float = 30.0, only_if_running: bool = False,
           **fields: Any) -> None:
    """Update a job row. A short `wait` makes it give up quietly when the database is busy."""
    sets = ", ".join(f"{k} = ?" for k in fields)
    sql = f"UPDATE jobs SET {sets} WHERE id = ?" + (" AND status = 'running'" if only_if_running else "")
    try:
        with _open(settings, timeout=wait) as conn:
            conn.execute(sql, (*fields.values(), job_id))
    except sqlite3.OperationalError:
        if wait >= 1:
            log.exception("Could not save job %s", job_id)


def _owner_alive(params_json: str | None) -> bool:
    """Is the process that started this job still running? (Not knowing counts as no.)"""
    pid = (db.loads(params_json, {}) or {}).get(PID_KEY)
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parse_params(params: dict | None) -> JobParams:
    try:
        return JobParams.model_validate(params or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "params"
        raise ValueError(f"Job setting '{where}' has a value I can't use: {first.get('msg')}") from None


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, (ValueError, KeyError)) and exc.args:
        text = str(exc.args[0])
    else:
        text = f"Something went wrong ({type(exc).__name__}: {exc}). Details are in the app's log."
    keys = [secret(name) for name in pipeline.KEY_NAMES.values()]
    return redact(text, *keys)[:1000]


# --- module-level API (one runner per process) ---------------------------------------------------

_runner = JobRunner()


def start_job(kind: str, market_slug: str, params: dict | None = None) -> int:
    return _runner.start(kind, market_slug, params)


def get_job(job_id: int) -> dict | None:
    return _runner.get(job_id)


def list_jobs(market_slug: str | None = None, limit: int = 20, *, with_results: bool = False) -> list[dict]:
    return _runner.list(market_slug, limit, with_results=with_results)


def cancel_job(job_id: int) -> dict | None:
    return _runner.cancel(job_id)


def wait_for_job(job_id: int, timeout: float | None = None) -> dict | None:
    return _runner.wait(job_id, timeout)


def recover_interrupted_jobs() -> int:
    """Call when the app starts (the module also does this by itself on first use)."""
    return _runner.recover()


__all__ = [
    "ACTIVE", "CANCELLED", "INTERRUPTED", "STATUSES", "Cancelled", "JobParams", "JobRunner", "cancel_job",
    "get_job", "list_jobs", "recover_interrupted_jobs", "run_pipeline_step", "start_job", "wait_for_job",
]
