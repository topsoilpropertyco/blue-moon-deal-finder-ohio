"""The Google Drive and Docs calls the LOI needs, over plain HTTPS: look at a file, read a Doc as text, copy it
into a folder, fill {{tokens}}, export a PDF and store it. DriveAdapter is the shape fake.py repeats in memory.
Nothing here decides what the letter says; it only moves the master's own words around.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx

from .auth import SignInError

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHARED = {"supportsAllDrives": "true"}


class DriveError(ValueError):
    """A Google call that didn't work, in plain English."""


@dataclass(frozen=True)
class FileInfo:
    id: str
    name: str
    mime: str
    version: str          # Drive's counter: it goes up every time the file changes
    modified: str


@dataclass(frozen=True)
class StoredFile:
    id: str
    url: str


class DriveAdapter(Protocol):
    def describe(self, file_id: str) -> FileInfo: ...
    def read_text(self, doc_id: str) -> str: ...
    def copy(self, file_id: str, title: str, folder_id: str) -> StoredFile: ...
    def replace_tokens(self, doc_id: str, replacements: Mapping[str, str]) -> None: ...
    def export_pdf(self, doc_id: str) -> bytes: ...
    def upload_pdf(self, title: str, data: bytes, folder_id: str) -> StoredFile: ...


def doc_url(file_id: str) -> str:
    return f"https://docs.google.com/document/d/{file_id}/edit"


def pdf_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


class GoogleDrive:
    """DriveAdapter over Google's REST API. `bearer` gives the access token for each call."""

    def __init__(self, http: httpx.Client, bearer: Callable[[], str]):
        self.http, self.bearer = http, bearer

    def _call(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            headers = {"Authorization": f"Bearer {self.bearer()}", **kw.pop("headers", {})}
            res = self.http.request(method, url, headers=headers, **kw)
        except httpx.HTTPError:
            raise DriveError("Couldn't reach Google. Check the internet connection and try again.") from None
        except SignInError as exc:
            raise DriveError(str(exc)) from None
        if res.status_code >= 400:
            raise DriveError(self._why(res))
        return res

    def _json(self, res: httpx.Response) -> dict:
        try:
            j = res.json()
            if not isinstance(j, dict) or not j.get("id"):
                raise ValueError
            return j
        except ValueError:
            raise DriveError("Google's answer wasn't readable. Try again.") from None

    @staticmethod
    def _why(res: httpx.Response) -> str:
        try:
            said = str((res.json().get("error") or {}).get("message") or "")[:200]
        except (ValueError, AttributeError):
            said = ""
        if res.status_code == 404:
            return "Google couldn't find that file or folder. Check the link, and that the signed-in Google account can open it."
        if res.status_code in (401, 403):
            return ("Google said no. Check that the Google Drive API and Google Docs API are turned on in your Google "
                    "Cloud project, that the signed-in account can edit the file, then run 'uv run run.py loi signin' "
                    f"again. Google's words: {said or res.status_code}")
        return f"Google answered with an error ({res.status_code}). {said}".strip()

    def describe(self, file_id: str) -> FileInfo:
        j = self._call("GET", f"{DRIVE}/files/{file_id}",
                       params={"fields": "id,name,mimeType,version,modifiedTime", **SHARED})
        j = self._json(j)
        return FileInfo(j["id"], j.get("name") or "", j.get("mimeType") or "", str(j.get("version") or ""),
                        j.get("modifiedTime") or "")

    def read_text(self, doc_id: str) -> str:
        return self._call("GET", f"{DRIVE}/files/{doc_id}/export", params={"mimeType": "text/plain"}).text

    def copy(self, file_id: str, title: str, folder_id: str) -> StoredFile:
        j = self._call("POST", f"{DRIVE}/files/{file_id}/copy", params={"fields": "id", **SHARED},
                       json={"name": title, "parents": [folder_id]})
        j = self._json(j)
        return StoredFile(j["id"], doc_url(j["id"]))

    def replace_tokens(self, doc_id: str, replacements: Mapping[str, str]) -> None:
        """Replace every written-out token (key, e.g. '{{seller_name}}') with its text, in one batch."""
        requests = [{"replaceAllText": {"containsText": {"text": token, "matchCase": True}, "replaceText": text}}
                    for token, text in replacements.items()]
        self._call("POST", f"{DOCS}/{doc_id}:batchUpdate", json={"requests": requests})

    def export_pdf(self, doc_id: str) -> bytes:
        return self._call("GET", f"{DRIVE}/files/{doc_id}/export", params={"mimeType": "application/pdf"}).content

    def upload_pdf(self, title: str, data: bytes, folder_id: str) -> StoredFile:
        boundary = "bluemoon-pdf-boundary"
        meta = json.dumps({"name": title if title.endswith(".pdf") else title + ".pdf", "parents": [folder_id],
                           "mimeType": "application/pdf"})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/pdf\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
        j = self._call("POST", UPLOAD, params={"uploadType": "multipart", "fields": "id", **SHARED}, content=body,
                       headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        j = self._json(j)
        return StoredFile(j["id"], pdf_url(j["id"]))
