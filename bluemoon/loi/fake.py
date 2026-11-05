"""DriveAdapter in memory: the "test transport". Tests (and nobody else) use it with a made-up template, so no
Google call is made and no letter wording is needed. `calls` lists every call in order; `fail_at` makes one step
fail the way Google could."""
from __future__ import annotations

from collections.abc import Mapping

from .drive import DOC_MIME, FOLDER_MIME, DriveError, FileInfo, StoredFile, doc_url, pdf_url


class FakeDrive:
    def __init__(self, master_id: str, master_text: str, folder_id: str, *, version: str = "1", fail_at: str | None = None):
        self.texts = {master_id: master_text}
        self.names = {master_id: "Master", folder_id: "Folder"}
        self.mimes = {master_id: DOC_MIME, folder_id: FOLDER_MIME}
        self.version = version
        self.fail_at = fail_at
        self.calls: list[tuple] = []
        self.pdfs: dict[str, bytes] = {}
        self.parents: dict[str, str] = {}

    def _step(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))
        if self.fail_at == name:
            raise DriveError(f"Google turned down the {name} step (test).")

    def describe(self, file_id: str) -> FileInfo:
        self._step("describe", file_id)
        if file_id not in self.mimes:
            raise DriveError("Google couldn't find that file or folder.")
        return FileInfo(file_id, self.names[file_id], self.mimes[file_id], self.version, "2026-11-01T00:00:00Z")

    def read_text(self, doc_id: str) -> str:
        self._step("read_text", doc_id)
        return self.texts[doc_id]

    def copy(self, file_id: str, title: str, folder_id: str) -> StoredFile:
        self._step("copy", file_id, title, folder_id)
        new = f"copy{len([c for c in self.calls if c[0] == 'copy'])}"
        self.texts[new], self.names[new], self.mimes[new], self.parents[new] = self.texts[file_id], title, DOC_MIME, folder_id
        return StoredFile(new, doc_url(new))

    def replace_tokens(self, doc_id: str, replacements: Mapping[str, str]) -> None:
        self._step("replace_tokens", doc_id, dict(replacements))
        for token, text in replacements.items():
            self.texts[doc_id] = self.texts[doc_id].replace(token, text)

    def export_pdf(self, doc_id: str) -> bytes:
        self._step("export_pdf", doc_id)
        return b"%PDF-test " + self.texts[doc_id].encode()

    def upload_pdf(self, title: str, data: bytes, folder_id: str) -> StoredFile:
        self._step("upload_pdf", title, folder_id)
        new = f"pdf{len(self.pdfs) + 1}"
        self.pdfs[new], self.parents[new] = data, folder_id
        return StoredFile(new, pdf_url(new))
