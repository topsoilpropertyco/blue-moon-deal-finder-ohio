"""The Crexi batch routes (/api/crexi/...): export the next N, import Crexi's reply, close a batch, history.

Credits belong to the whole Crexi account, so these routes are not under a market. Like every /api/ route they
need the sign-in cookie when a password is set (the login middleware in app.py covers them). Files are only
ever looked up by batch or import id, never by a name the browser sends, and always inside the output folder.
The app never talks to Crexi: Seth moves the files by hand.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..crexi import batches as B
from ..crexi import merge as M
from ..crexi import reply as R
from ..db import loads, one

log = logging.getLogger("bluemoon.web.crexi")
router = APIRouter()


def get_conn() -> Iterator[sqlite3.Connection]:
    from .app import open_db     # imported here: app.py includes this router
    conn = open_db()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


class ExportIn(BaseModel):
    count: int | None = Field(default=None, ge=1, le=100000)
    retry: bool = False
    note: str | None = Field(default=None, max_length=500)


class ConfirmIn(BaseModel):
    confirm: bool = False


class AlternateIn(BaseModel):
    action: str = Field(max_length=20)       # use | ignore


def _who(request) -> str:
    from .app import _actor
    return _actor(request)


@router.get("/api/crexi/status")
def crexi_status(conn: Conn) -> dict:
    return B.status(conn)


@router.get("/api/crexi/history")
def crexi_history(conn: Conn) -> dict:
    return B.history(conn)


@router.get("/api/crexi/batches/{batch_id}")
def crexi_batch(batch_id: int, conn: Conn, limit: int = 100, offset: int = 0) -> dict:
    return B.batch_items(conn, batch_id, limit=limit, offset=offset)


@router.post("/api/crexi/export/preview")
def export_preview(body: ExportIn, conn: Conn) -> dict:
    return B.preview_export(conn, body.count, retry=body.retry)


@router.post("/api/crexi/export")
def export_batch(body: ExportIn, request: Request, conn: Conn) -> dict: 
    return B.export_batch(conn, body.count, created_by=_who(request), retry=body.retry, note=body.note)


@router.get("/api/crexi/batches/{batch_id}/file")
def export_file(batch_id: int, conn: Conn) -> FileResponse:
    path = B.export_file(conn, batch_id)
    return FileResponse(path, media_type="text/csv", filename=path.name,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/crexi/batches/{batch_id}/close")
def close_batch(batch_id: int, body: ConfirmIn, request: Request, conn: Conn) -> dict: 
    return B.close_batch(conn, batch_id, confirm=body.confirm, actor=_who(request))


@router.post("/api/crexi/batches/{batch_id}/withdraw")
def withdraw_batch(batch_id: int, body: ConfirmIn, request: Request, conn: Conn) -> dict: 
    return B.withdraw_batch(conn, batch_id, confirm=body.confirm, actor=_who(request))


def _columns(text: str | None) -> dict | None:
    if not text or not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError:
        raise HTTPException(400, "The column choices could not be read.") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "The column choices could not be read.")
    return value


@router.post("/api/crexi/import/preview")
def import_preview(conn: Conn, file: Annotated[UploadFile, File()], columns: Annotated[str | None, Form()] = None,
                   adopt_hand: Annotated[bool, Form()] = False) -> dict:
    """What Crexi's reply would do (rows matched, credits, new data), written nowhere."""
    from .app import save_upload, upload_name, upload_tmp_dir
    name = upload_name(file.filename)
    folder = upload_tmp_dir() / f"crexi_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
    try:
        return R.preview(conn, save_upload(file, folder / name), name, columns=_columns(columns),
                         adopt_hand=adopt_hand)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@router.post("/api/crexi/import")
def import_reply(request: Request, conn: Conn, file: Annotated[UploadFile, File()], 
                 columns: Annotated[str | None, Form()] = None,
                 adopt_hand: Annotated[bool, Form()] = False) -> dict:
    """Import Crexi's reply (who: the signed-in person), then rank what the facts changed."""
    from .app import save_upload, upload_name, upload_tmp_dir
    name = upload_name(file.filename)
    folder = upload_tmp_dir() / f"crexi_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
    try:
        result = R.apply_reply(conn, save_upload(file, folder / name), name, columns=_columns(columns),
                               adopt_hand=adopt_hand, actor=_who(request))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    result["after_import"] = _rank_after(conn, result)
    return result


def _rank_after(conn: sqlite3.Connection, result: dict) -> dict | None:
    """The free steps after an import (owners, score, DRILL) for the markets whose facts changed."""
    slugs = list((result.get("merge") or {}).get("markets") or {})
    if not result.get("changed") or not slugs:
        return None
    try:
        from .. import pipeline
        return {slug: pipeline.after_import(conn, slug).get("message") for slug in slugs}
    except Exception:
        log.exception("Ranking after a Crexi reply failed")
        return {"error": "Imported, but ranking it failed. Run Owners, Score and DRILL from the Run screen."}


@router.get("/api/crexi/imports/{import_id}/files/{kind}")
def import_file_download(import_id: int, kind: str, conn: Conn) -> FileResponse:
    """The files an import wrote: unmatched rows, unreadable contacts, or the saved copy of Crexi's file."""
    row = one(conn, "SELECT summary_json, saved_as FROM crexi_batch_imports WHERE id = ?", (import_id,))
    if not row:
        raise KeyError(f"Import {import_id} was not found.")
    summary = loads(row["summary_json"], {}) or {}
    rel = {"unmatched": summary.get("unmatched_file"), "unreadable": summary.get("unreadable_file"),
           "reply": row["saved_as"]}.get(kind)
    if not rel:
        raise KeyError("That file was not made for this import.")
    root = B.output_dir().resolve()
    target = (root / rel).resolve()
    if root not in target.parents or not target.is_file():
        raise KeyError("That file is no longer on this computer.")
    return FileResponse(target, filename=target.name, headers={"Cache-Control": "no-store"})


@router.get("/api/m/{slug}/properties/{pid}/crexi")
def property_crexi(slug: str, pid: int, conn: Conn) -> dict:
    from .app import require_property
    require_property(conn, slug, pid)
    return {**B.property_history(conn, pid), "alternates": M.open_alternates(conn, pid)}


@router.post("/api/m/{slug}/properties/{pid}/alternates/{alternate_id}")
def decide_alternate(slug: str, pid: int, alternate_id: int, body: AlternateIn, request: Request,
                     conn: Conn) -> dict:
    """'Use this one' or 'Ignore' for a value Crexi sent that differs from what the property holds."""
    from .app import require_property
    require_property(conn, slug, pid)
    return M.decide_alternate(conn, pid, alternate_id, body.action, _who(request))
