"""The LOI settings (which master Doc, which Drive folder), the template check, the field review and making a
letter for an offer.

Making a letter: copy the master into the folder as "LOI - <address> - <date> v<n>", fill its {{tokens}}, check
nothing token-shaped is left, export a PDF, keep both links on the offer. If something needed is missing (Google
sign-in, the template, a token's value) no copy is made and the offer says what is missing instead. A letter is
tied to what it was made from (offers.letter_hash) and to the template's version. A sent offer is never touched.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import offers
from ..config import Settings
from ..db import dumps, loads, now_iso, one
from ..pricing import PricingConfig
from . import auth, fields
from .drive import DOC_MIME, FOLDER_MIME, DriveAdapter, DriveError, FileInfo, GoogleDrive, StoredFile, doc_url

META_KEY = "loi_settings"
ID = re.compile(r"[A-Za-z0-9_\-]{10,200}")
LINK = re.compile(r"/(?:d|folders)/([A-Za-z0-9_\-]{10,200})")
SIGN_IN_COMMAND = "uv run run.py loi signin"
REVIEW_NOTE = "Field review: the text that would fill your template's fields. This is not the letter."


# --- settings ----------------------------------------------------------------------------------------------

def link_id(text: Any, what: str) -> str | None:
    """The id in a Google link (or an id pasted as it is). Blank gives None, anything else is refused."""
    t = str(text or "").strip()
    if not t:
        return None
    if ID.fullmatch(t):
        return t
    parts = urlsplit(t)
    m = LINK.search(parts.path)
    if parts.scheme == "https" and parts.netloc in ("docs.google.com", "drive.google.com") and m:
        return m.group(1)
    raise ValueError(f"That doesn't look like a link to a Google {what}. Open it in Google Drive and copy the "
                     "address from the browser's address bar.")


def load(conn: sqlite3.Connection) -> dict:
    row = one(conn, "SELECT value FROM meta WHERE key=?", (META_KEY,))
    return {"template_id": None, "folder_id": None, "check": None, **(loads(row["value"], {}) if row else {})}


def _save(conn: sqlite3.Connection, state: Mapping[str, Any]) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (META_KEY, dumps(dict(state))))
    conn.commit()


def save_links(conn: sqlite3.Connection, template: Any, folder: Any) -> dict:
    """Keep the master Doc and the folder. Changing either clears the last template check."""
    old = load(conn)
    new = {"template_id": link_id(template, "Doc"), "folder_id": link_id(folder, "Drive folder")}
    changed = (new["template_id"], new["folder_id"]) != (old["template_id"], old["folder_id"])
    _save(conn, {**old, **new, "check": None if changed else old["check"]})
    return load(conn)


_http: httpx.Client | None = None


def open_adapter(settings: Settings) -> DriveAdapter | None:
    """The real Google adapter, or None until Google sign-in has been done on this computer."""
    global _http
    if not auth.signed_in(settings):
        return None
    _http = _http or httpx.Client(timeout=60)
    return GoogleDrive(_http, auth.Credentials(settings, _http).bearer)


def blockers(conn: sqlite3.Connection, settings: Settings, signed_in: bool) -> list[str]:
    """What has to be done before any letter can be made (each a sentence for Seth)."""
    out = []
    if not signed_in:
        out.append(f"Google sign-in needed: run '{SIGN_IN_COMMAND}' on the Mac Mini." if auth.client_path(settings).exists()
                   else f"Google sign-in needed: the Google client file is missing (data/{auth.CLIENT_FILE}).")
    state = load(conn)
    if not state["template_id"]:
        out.append("LOI template needed: paste the link to your master Google Doc in Settings.")
    if not state["folder_id"]:
        out.append("LOI template needed: paste the link to the Drive folder for the letters in Settings.")
    return out


def _problems(tokens: list[str], fmap: Mapping[str, str]) -> list[str]:
    if not tokens:
        return ["The master document has no {{fields}} to fill, so a copy would be the master unchanged."]
    unmapped = [t for t in tokens if t not in fmap]
    return [f"These fields in the master aren't mapped in config/loi.yaml: {', '.join(unmapped)}."] if unmapped else []


def status(conn: sqlite3.Connection, settings: Settings) -> dict:
    """The Settings panel: sign-in, template, folder, the last check and what each token is filled with."""
    state = load(conn)
    fmap = fields.load_map(settings.loi)
    signed = auth.signed_in(settings)
    check = state["check"]
    problems = blockers(conn, settings, signed)
    if not problems:
        problems = _problems(check["tokens"], fmap) if check else ["Check the template so its fields can be read."]
    return {
        "signed_in": signed, "client_file": auth.client_path(settings).exists(), "sign_in_command": SIGN_IN_COMMAND,
        "template_id": state["template_id"], "template_url": doc_url(state["template_id"]) if state["template_id"] else None,
        "folder_id": state["folder_id"],
        "folder_url": f"https://drive.google.com/drive/folders/{state['folder_id']}" if state["folder_id"] else None,
        "check": check, "problems": problems, "active": not problems,
        "headline": "Ready to make letters" if not problems else problems[0].split(":")[0].rstrip("."),
        "map": [{"token": t, "source": s, "label": fields.SOURCES[s]} for t, s in fmap.items()],
    }


# --- the template ------------------------------------------------------------------------------------------

def _read_template(adapter: DriveAdapter, template_id: str) -> tuple[FileInfo, dict[str, list[str]]]:
    info = adapter.describe(template_id)
    if info.mime != DOC_MIME:
        raise DriveError("That link isn't a Google Doc. Paste the link to your master letter document.")
    return info, fields.find_tokens(adapter.read_text(template_id))


def check(conn: sqlite3.Connection, settings: Settings, adapter: DriveAdapter | None) -> dict:
    """Read the master and the folder (changes nothing in Drive) and keep which fields the template holds."""
    problems = blockers(conn, settings, adapter is not None)
    if adapter is None or problems:
        raise ValueError(problems[0])
    state = load(conn)
    info, tokens = _read_template(adapter, state["template_id"])
    folder = adapter.describe(state["folder_id"])
    if folder.mime != FOLDER_MIME:
        raise DriveError("The folder link isn't a Drive folder. Open the folder in Google Drive and copy its address.")
    fmap = fields.load_map(settings.loi)
    result = {"template_name": info.name, "template_version": info.version, "folder_name": folder.name,
              "tokens": list(tokens), "unmapped": [t for t in tokens if t not in fmap], "checked_at": now_iso()}
    _save(conn, {**state, "check": result})
    return status(conn, settings)


# --- one offer ---------------------------------------------------------------------------------------------

def preview(conn: sqlite3.Connection, settings: Settings, offer: Mapping[str, Any], prop: Mapping[str, Any],
            cfg: PricingConfig) -> dict:
    """What would fill each field for this offer. Uses the fields found by the last template check (all mapped
    fields when there was none). A review of values only: no letter, no Google call."""
    check_ = load(conn)["check"]
    rows = fields.review(fields.load_map(settings.loi), fields.values(conn, prop, offer, cfg.today()),
                         check_["tokens"] if check_ else None)
    return {"rows": rows, "unresolved": len(fields.unresolved(rows)), "checked": check_ is not None,
            "template_version": check_["template_version"] if check_ else None, "note": REVIEW_NOTE}


def _title(address: str | None, offer: Mapping[str, Any], cfg: PricingConfig) -> str:
    return f"LOI - {address or 'address unknown'} - {cfg.today().isoformat()} v{offer['version']}"[:200]


def generate(conn: sqlite3.Connection, settings: Settings, adapter: DriveAdapter | None, offer_id: int,
             prop: Mapping[str, Any], cfg: PricingConfig, actor: str, *, again: bool = False) -> dict:
    """Make the letter for an unsent offer. Returns {result, message}: generated, exists (already made from these
    numbers, so no second copy unless `again`), template_needed or fields_missing (nothing was made in Drive).
    A Google failure is written to the offer's history and raised."""
    o = offers.unsent(conn, offer_id)
    reasons = blockers(conn, settings, adapter is not None)
    if adapter is None or reasons:
        offers.letter_note(conn, offer_id, "document_blocked", actor, status="loi_template_needed", reasons=reasons)
        return {"result": "template_needed", "message": "LOI template needed. " + " ".join(reasons)}
    h = offers.letter_hash(o)          # what this letter is made from; checked again when it is saved
    state = load(conn)
    fmap = fields.load_map(settings.loi)
    made: dict[str, str] = {}
    try:
        info, tokens = _read_template(adapter, state["template_id"])
        if not again and one(conn, "SELECT id FROM offer_documents WHERE offer_id=? AND snapshot_hash=? AND "
                                   "template_version=?", (offer_id, offers.letter_hash(o), info.version)):
            if o["status"] not in offers.NEEDS_DOCUMENT:
                offers.set_status(conn, offer_id, "loi_generated", actor)
            return {"result": "exists", "message": "A letter was already made from these numbers. Nothing new was "
                                                   "made. Use 'Make a new copy' if you changed the master or other details."}
        filled = fields.values(conn, prop, o, cfg.today())
        rows = fields.review(fmap, filled, list(tokens))
        problems = _problems(list(tokens), fmap)
        empty = [r for r in rows if r["state"] == fields.EMPTY]
        if empty:
            problems.append("No value yet for: " + ", ".join(r["token"] for r in empty) + ".")
        if problems:
            offers.letter_note(conn, offer_id, "document_blocked", actor, status="required_fields_missing",
                               missing=[r["token"] for r in fields.unresolved(rows)], reasons=problems)
            return {"result": "fields_missing", "message": "Required fields missing. " + " ".join(problems)}
        title = _title(filled["property_address"], o, cfg)
        copy = adapter.copy(state["template_id"], title, state["folder_id"])
        made["doc_id"] = copy.id
        by_token = {r["token"]: r["value"] or "" for r in rows}
        adapter.replace_tokens(copy.id, {raw: by_token[name] for name, raws in tokens.items() for raw in raws})
        left = fields.leftovers(adapter.read_text(copy.id))
        if left:
            raise DriveError(f"Some fields were left unfilled in the copy ({', '.join(left)}). The copy is in your "
                             "Drive folder; check the master's spelling of those fields.")
        pdf: StoredFile = adapter.upload_pdf(title, adapter.export_pdf(copy.id), state["folder_id"])
    except DriveError as exc:
        offers.letter_note(conn, offer_id, "failed", actor, message=str(exc), **made)
        raise
    offers.add_document(conn, offer_id, {"template_id": state["template_id"], "template_version": info.version,
                                         "doc_id": copy.id, "doc_url": copy.url, "pdf_id": pdf.id, "pdf_url": pdf.url},
                        actor, expected_hash=h)
    return {"result": "generated", "message": "LOI made. The Doc and the PDF are in your Drive folder. Read it through "
                                              "before you mark it ready."}
