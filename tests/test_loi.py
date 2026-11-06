"""The offer letter from a master Google Doc: token detection and the field map, the field review, making a letter
through the in-memory test transport (call order, title, folder, filling, PDF), what blocks a letter, a sent offer
never being touched, the real Google adapter against a mock server, Google sign-in without a browser, the routes and
the command line. The template is a made-up list of fields: no letter wording, no deal terms. Nothing is real."""
from __future__ import annotations

import itertools
import json
import threading
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

import run
from bluemoon import napkin, offers, pricing
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.drill import engine
from bluemoon.loi import auth, fields, service
from bluemoon.loi.drive import DriveError, GoogleDrive
from bluemoon.loi.fake import FakeDrive

MARKET = "apartments-springfield-oh"
TODAY = date(2026, 11, 5)
CFG = pricing.load_config({"evaluation_date": TODAY, "follow_up_days": 7})
TEMPLATE_ID, FOLDER_ID = "master-template-id-1", "letters-folder-id-1"
FIELDS_ONLY = ("FIELD TEST seller={{seller_name}} to={{recipient_name}} price={{offer_price}} "
               "where={{ property_full_address }} v={{offer_version}}")
CLIENT = {"installed": {"client_id": "client-id-1", "client_secret": "client-secret-1"}}
REFRESH = "REFRESH-TOKEN-must-never-print"
_ids = itertools.count(1)


def put_client(settings):
    auth.client_path(settings).write_text(json.dumps(CLIENT), encoding="utf-8")


def sign_in_for_real(settings):
    """The files a finished sign-in leaves behind (the Google calls themselves go to the test transport)."""
    put_client(settings)
    auth._save_token(settings, REFRESH)


def add_prop(conn, **kw) -> int:
    n = next(_ids)
    data = {"market_id": market_by_slug(conn, MARKET)["id"], "property_key": f"loi:{n}", "address": f"{n} LETTER LN",
            "city": "SPRINGFIELD", "state": "OH", "zip": "45501", "units": 32, "owner_name": "EXAMPLE HOLDINGS LLC",
            "mailing_address": "9 MAIL ST", "mailing_city": "DAYTON", "mailing_state": "OH", "mailing_zip": "45402",
            "raw_json": "{}", "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso()}
    data.update(kw)
    pid = int(conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                           tuple(data.values())).lastrowid)
    conn.commit()
    return pid


def prop_of(conn, pid):
    return one(conn, "SELECT * FROM properties WHERE id=?", (pid,))


def snap(conn, pid):
    return napkin.snapshot(conn, prop_of(conn, pid), "apartments", CFG, None)


def draft(conn, recipient=True) -> tuple[int, dict]:
    """A property with rent, a Bea Broker recipient (unless told not to) and an unsent offer."""
    pid = add_prop(conn)
    napkin.add_record(conn, pid, source="manual", basis="scheduled", actor="seth", total="34240", total_period="monthly")
    if recipient:
        from bluemoon import people
        engine.add_person(conn, pid, "Bea Broker", role="broker")
        engine.add_contact(conn, pid, "email", "bea@example.com", person_name="Bea Broker")
        people.choose_recipient(conn, pid, scalar(conn, "SELECT id FROM people WHERE property_id=?", (pid,)),
                                scalar(conn, "SELECT id FROM contact_points WHERE property_id=?", (pid,)), None, "seth")
    return pid, offers.prepare(conn, prop_of(conn, pid), snap(conn, pid), "seth")


def configure(conn, text=FIELDS_ONLY, **kw) -> FakeDrive:
    service.save_links(conn, TEMPLATE_ID, FOLDER_ID)
    return FakeDrive(TEMPLATE_ID, text, FOLDER_ID, **kw)


def make(conn, settings, adapter, pid, oid, **kw):
    return service.generate(conn, settings, adapter, oid, prop_of(conn, pid), CFG, "seth", **kw)


def events(conn, oid):
    return [r["kind"] for r in rows(conn, "SELECT kind FROM offer_events WHERE offer_id=? ORDER BY id", (oid,))]


def status_of(conn, oid):
    return scalar(conn, "SELECT status FROM offers WHERE id=?", (oid,))


# --- tokens and the field map --------------------------------------------------------------------------

def test_tokens_are_found_with_every_way_they_are_written():
    found = fields.find_tokens("{{a}} and {{ a }} and {{b.c-d}} and {{two words}} and {a}")
    assert found == {"a": ["{{a}}", "{{ a }}"], "b.c-d": ["{{b.c-d}}"]}
    assert fields.leftovers("done {{two words}} {{x}} {{x}} plain") == ["{{two words}}", "{{x}}"]


def test_the_map_in_the_yaml_is_checked(settings):
    default = fields.load_map(settings.loi)
    assert default["seller_name"] == "seller_name" and "annual_rent" not in default.values()
    assert set(default.values()) <= set(fields.SOURCES)
    with pytest.raises(ValueError, match="not something a token can be filled with"):
        fields.load_map({"field_map": {"x": "blue_moon_score"}})
    with pytest.raises(ValueError, match="can't be a token name"):
        fields.load_map({"field_map": {"two words": "seller_name"}})
    assert not {"score", "prior", "note", "rank"} & {w for k in fields.SOURCES for w in k.split("_")}   # no internal figure


def test_seller_and_recipient_are_separate_and_money_is_exact(conn):
    pid, o = draft(conn)
    v = fields.values(conn, prop_of(conn, pid), o, TODAY)
    assert (v["seller_name"], v["recipient_name"], v["recipient_contact"]) == ("EXAMPLE HOLDINGS LLC", "Bea Broker", "bea@example.com")
    assert v["offer_price"] == "$1,643,520" and v["annual_rent"] == "$410,880" and v["offer_date"] == "November 5, 2026"
    assert v["seller_mailing_address"] == "9 MAIL ST, DAYTON, OH 45402" and v["offer_version"] == "1"
    assert v["property_full_address"] == f"{prop_of(conn, pid)['address']}, SPRINGFIELD, OH 45501" and v["property_units"] == "32"
    bare = draft(conn, recipient=False)
    again = fields.values(conn, prop_of(conn, bare[0]), bare[1], TODAY)
    assert again["recipient_name"] is None and again["recipient_contact"] is None and again["seller_name"]


def test_the_review_marks_filled_empty_unmapped_and_unused(conn, settings):
    pid, o = draft(conn, recipient=False)
    fmap = fields.load_map(settings.loi)
    filled = fields.values(conn, prop_of(conn, pid), o, TODAY)
    rows_ = fields.review(fmap, filled, ["seller_name", "recipient_name", "mystery"])
    states = {r["token"]: r["state"] for r in rows_}
    assert states["seller_name"] == "filled" and states["recipient_name"] == "empty" and states["mystery"] == "unmapped"
    assert states["offer_price"] == "unused"
    assert [r["token"] for r in fields.unresolved(rows_)] == ["recipient_name", "mystery"]
    assert {r["token"] for r in fields.review(fmap, filled)} == set(fmap)               # no template read yet: the mapped ones


# --- making a letter through the test transport --------------------------------------------------------

def test_a_letter_is_a_copy_in_the_folder_with_every_field_filled_and_a_pdf(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn)
    out = make(conn, settings, drive, pid, o["id"])
    assert out["result"] == "generated"
    names = [c[0] for c in drive.calls]
    assert names == ["describe", "read_text", "copy", "replace_tokens", "read_text", "export_pdf", "upload_pdf"]
    title = f"LOI - {prop_of(conn, pid)['address']} - 2026-11-05 v1"
    assert drive.calls[2] == ("copy", TEMPLATE_ID, title, FOLDER_ID)
    assert drive.calls[3][2] == {"{{seller_name}}": "EXAMPLE HOLDINGS LLC", "{{recipient_name}}": "Bea Broker",
                                 "{{offer_price}}": "$1,643,520", "{{ property_full_address }}": f"{prop_of(conn, pid)['address']}, SPRINGFIELD, OH 45501",
                                 "{{offer_version}}": "1"}
    assert drive.calls[6][1:] == (title, FOLDER_ID)
    assert drive.texts[TEMPLATE_ID] == FIELDS_ONLY                          # the master itself is never changed
    filled = drive.texts["copy1"]
    assert "{{" not in filled and "seller=EXAMPLE HOLDINGS LLC to=Bea Broker price=$1,643,520" in filled
    doc = one(conn, "SELECT * FROM offer_documents WHERE offer_id=?", (o["id"],))
    assert (doc["template_id"], doc["template_version"], doc["snapshot_hash"]) == (TEMPLATE_ID, "1", offers.letter_hash(o))
    assert (doc["doc_id"], doc["pdf_id"]) == ("copy1", "pdf1") and doc["doc_url"].startswith("https://docs.google.com/")
    assert drive.pdfs["pdf1"].startswith(b"%PDF") and status_of(conn, o["id"]) == "loi_generated"
    assert events(conn, o["id"]) == ["prepared", "document_generated"]
    view = offers.views(conn, [one(conn, "SELECT * FROM offers WHERE id=?", (o["id"],))])[0]
    assert view["documents"][0]["current"] is True and view["readiness"]["template"]["ready"] is True


def test_the_same_numbers_do_not_make_a_second_copy_unless_asked(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn)
    make(conn, settings, drive, pid, o["id"])
    again = make(conn, settings, drive, pid, o["id"])
    assert again["result"] == "exists" and [c[0] for c in drive.calls].count("copy") == 1
    assert scalar(conn, "SELECT COUNT(*) FROM offer_documents") == 1
    assert make(conn, settings, drive, pid, o["id"], again=True)["result"] == "generated"
    assert scalar(conn, "SELECT COUNT(*) FROM offer_documents") == 2


def test_a_new_price_needs_a_new_letter_and_a_changed_master_gets_a_new_copy(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn)
    make(conn, settings, drive, pid, o["id"])
    offers.set_status(conn, o["id"], "ready_for_review", "seth")
    offers.set_status(conn, o["id"], "ready_to_send", "seth")
    offers.set_price(conn, o["id"], "1,700,000", "seth")
    assert status_of(conn, o["id"]) == "internal_draft"
    with pytest.raises(ValueError, match="no LOI made"):                     # the old letter has the old price
        offers.set_status(conn, o["id"], "ready_for_review", "seth")
    assert make(conn, settings, drive, pid, o["id"])["result"] == "generated"
    assert "$1,700,000" in drive.texts["copy2"]
    drive.version = "2"                                                       # Seth edited the master
    assert make(conn, settings, drive, pid, o["id"])["result"] == "generated"
    assert [d["template_version"] for d in rows(conn, "SELECT template_version FROM offer_documents ORDER BY id")] == ["1", "1", "2"]


def test_a_missing_value_or_unmapped_field_blocks_the_letter_before_anything_is_copied(conn, settings):
    pid, o = draft(conn, recipient=False)
    drive = configure(conn)
    out = make(conn, settings, drive, pid, o["id"])
    assert out["result"] == "fields_missing" and "recipient_name" in out["message"]
    assert [c[0] for c in drive.calls] == ["describe", "read_text"]           # nothing was copied
    assert status_of(conn, o["id"]) == "required_fields_missing"
    assert scalar(conn, "SELECT COUNT(*) FROM offer_documents") == 0
    blocked = one(conn, "SELECT detail_json FROM offer_events WHERE kind='document_blocked'")
    assert json.loads(blocked["detail_json"])["missing"] == ["recipient_name"]
    odd = configure(conn, FIELDS_ONLY + " {{mystery_field}}")
    out = make(conn, settings, odd, pid, o["id"])
    assert out["result"] == "fields_missing" and "mystery_field" in out["message"] and "config/loi.yaml" in out["message"]
    none = configure(conn, "plain words only")
    assert "no {{fields}}" in make(conn, settings, none, pid, o["id"])["message"]
    assert not [c for c in none.calls if c[0] == "copy"]


def test_without_google_or_a_template_the_offer_says_the_template_is_needed(conn, settings):
    pid, o = draft(conn)
    out = make(conn, settings, None, pid, o["id"])                            # not signed in, nothing configured
    assert out["result"] == "template_needed" and "LOI template needed" in out["message"] and "Google sign-in needed" in out["message"]
    assert status_of(conn, o["id"]) == "loi_template_needed"
    service.save_links(conn, TEMPLATE_ID, None)
    drive = FakeDrive(TEMPLATE_ID, FIELDS_ONLY, FOLDER_ID)
    out = make(conn, settings, drive, pid, o["id"])
    assert out["result"] == "template_needed" and "Drive folder" in out["message"] and drive.calls == []
    assert scalar(conn, "SELECT price_cents FROM offers WHERE id=?", (o["id"],)) == 164352000    # everything else still works


def test_a_sent_offer_is_never_overwritten(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn)
    make(conn, settings, drive, pid, o["id"])
    offers.mark_sent(conn, o["id"], "email", None, "seth", CFG)
    calls = len(drive.calls)
    with pytest.raises(ValueError, match="already sent"):
        make(conn, settings, drive, pid, o["id"], again=True)
    assert len(drive.calls) == calls and scalar(conn, "SELECT COUNT(*) FROM offer_documents") == 1
    with pytest.raises(ValueError, match="already sent"):
        offers.add_document(conn, o["id"], {"template_id": "t", "template_version": "1", "doc_id": "d", "doc_url": "u",
                                            "pdf_id": "p", "pdf_url": "u"}, "seth")
    with pytest.raises(ValueError, match="already sent"):
        offers.letter_note(conn, o["id"], "failed", "seth", status="internal_draft")
    assert status_of(conn, o["id"]) == "sent"
    revised = offers.prepare(conn, prop_of(conn, pid), snap(conn, pid), "seth")           # version 2 gets its own letter
    assert make(conn, settings, drive, pid, revised["id"])["result"] == "generated"
    assert drive.calls[-5][2].endswith("v2") and scalar(conn, "SELECT COUNT(*) FROM offer_documents WHERE offer_id=?", (o["id"],)) == 1


def test_a_google_failure_is_kept_in_the_history_and_nothing_is_recorded_as_made(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn, fail_at="export_pdf")
    with pytest.raises(DriveError, match="export_pdf"):
        make(conn, settings, drive, pid, o["id"])
    assert scalar(conn, "SELECT COUNT(*) FROM offer_documents") == 0 and status_of(conn, o["id"]) == "internal_draft"
    failed = one(conn, "SELECT detail_json FROM offer_events WHERE kind='failed'")
    assert json.loads(failed["detail_json"])["doc_id"] == "copy1"                # the half-made copy is named
    assert make(conn, settings, configure(conn), pid, o["id"])["result"] == "generated"    # a retry works


def test_a_field_the_fill_missed_fails_the_letter(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn, FIELDS_ONLY + " {{two words}}")
    with pytest.raises(DriveError, match=r"\{\{two words\}\}"):
        make(conn, settings, drive, pid, o["id"])
    assert scalar(conn, "SELECT COUNT(*) FROM offer_documents") == 0 and "export_pdf" not in [c[0] for c in drive.calls]


# --- the settings and the template check ---------------------------------------------------------------

def test_links_are_read_from_google_addresses_only(conn):
    doc = "https://docs.google.com/document/d/1AbCdEfGhIjKlMn/edit?usp=sharing"
    assert service.link_id(doc, "Doc") == "1AbCdEfGhIjKlMn" and service.link_id("1AbCdEfGhIjKlMn", "Doc") == "1AbCdEfGhIjKlMn"
    assert service.link_id("https://drive.google.com/drive/u/0/folders/1FolderIdentifier99?x=1", "folder") == "1FolderIdentifier99"
    assert service.link_id("  ", "Doc") is None
    for bad in ("https://evil.example/document/d/1AbCdEfGhIjKlMn", "http://docs.google.com/document/d/1AbCdEfGhIjKlMn", "hello there", "a/b"):
        with pytest.raises(ValueError, match="doesn't look like a link"):
            service.link_id(bad, "Doc")


def test_the_check_reads_the_template_and_lists_unmapped_fields(conn, settings):
    drive = configure(conn, FIELDS_ONLY + " {{mystery_field}}")
    first = service.status(conn, settings)
    assert first["active"] is False and first["headline"] == "Google sign-in needed" and first["check"] is None
    sign_in_for_real(settings)
    assert service.status(conn, settings)["headline"] == "Check the template so its fields can be read"
    st = service.check(conn, settings, drive)
    assert st["check"]["tokens"][-1] == "mystery_field" and st["check"]["unmapped"] == ["mystery_field"]
    assert st["check"]["template_version"] == "1" and st["check"]["folder_name"] == "Folder"
    assert [c[0] for c in drive.calls] == ["describe", "read_text", "describe"]            # looks only: no copy, no change
    assert st["active"] is False and "mystery_field" in " ".join(st["problems"])
    clean = service.check(conn, settings, configure(conn))
    assert clean["check"]["unmapped"] == [] and clean["active"] is True and clean["headline"] == "Ready to make letters"
    assert service.save_links(conn, TEMPLATE_ID, "other-folder-id-22")["check"] is None   # a new folder: check again
    assert service.save_links(conn, TEMPLATE_ID, "other-folder-id-22")["template_id"] == TEMPLATE_ID


def test_the_check_refuses_the_wrong_kind_of_file_and_no_sign_in(conn, settings):
    drive = configure(conn)
    drive.mimes[TEMPLATE_ID] = "application/pdf"
    with pytest.raises(DriveError, match="isn't a Google Doc"):
        service.check(conn, settings, drive)
    drive = configure(conn)
    drive.mimes[FOLDER_ID] = "application/pdf"
    with pytest.raises(DriveError, match="isn't a Drive folder"):
        service.check(conn, settings, drive)
    with pytest.raises(ValueError, match="Google sign-in needed"):
        service.check(conn, settings, None)


def test_the_preview_is_a_field_review_with_no_google_call(conn, settings):
    pid, o = draft(conn)
    before = service.preview(conn, settings, o, prop_of(conn, pid), CFG)
    assert before["checked"] is False and "not the letter" in before["note"]
    assert {r["token"] for r in before["rows"] if r["state"] == "empty"} == {"property_county", "property_apn"}
    service.check(conn, settings, configure(conn, FIELDS_ONLY + " {{mystery_field}}"))
    after = service.preview(conn, settings, o, prop_of(conn, pid), CFG)
    assert after["checked"] is True and after["unresolved"] == 1 and after["template_version"] == "1"
    price = next(r for r in after["rows"] if r["token"] == "offer_price")
    assert price["value"] == "$1,643,520" and price["state"] == "filled"


# --- the real Google adapter against a mock server -----------------------------------------------------

def google(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/export") and request.url.params["mimeType"] == "text/plain":
            return httpx.Response(200, text="hello {{x}}")
        if path.endswith("/export"):
            return httpx.Response(200, content=b"%PDF-1")
        if path.endswith("/copy"):
            return httpx.Response(200, json={"id": "newdoc"})
        if path.endswith(":batchUpdate"):
            return httpx.Response(200, json={})
        if path.startswith("/upload/"):
            return httpx.Response(200, json={"id": "newpdf"})
        if path.endswith("/missing"):
            return httpx.Response(404, json={"error": {"message": "File not found"}})
        if path.endswith("/locked"):
            return httpx.Response(403, json={"error": {"message": "Docs API has not been used"}})
        return httpx.Response(200, json={"id": "f1", "name": "Master", "mimeType": "application/vnd.google-apps.document",
                                         "version": "42", "modifiedTime": "2026-11-01T00:00:00Z"})
    return GoogleDrive(httpx.Client(transport=httpx.MockTransport(handler)), lambda: "ACCESS-TOKEN-XYZ")


def test_the_google_adapter_makes_the_documented_calls():
    seen: list[httpx.Request] = []
    g = google(seen)
    info = g.describe("f1")
    assert (info.name, info.version) == ("Master", "42")
    assert g.read_text("f1") == "hello {{x}}"
    copy = g.copy("f1", "LOI - 1 Fake St - 2026-11-05 v1", "folder9")
    assert copy.id == "newdoc" and copy.url == "https://docs.google.com/document/d/newdoc/edit"
    g.replace_tokens("newdoc", {"{{x}}": "Value"})
    assert g.export_pdf("newdoc") == b"%PDF-1"
    pdf = g.upload_pdf("LOI - 1 Fake St - 2026-11-05 v1", b"%PDF-1", "folder9")
    assert pdf.url == "https://drive.google.com/file/d/newpdf/view"
    assert all(r.headers["authorization"] == "Bearer ACCESS-TOKEN-XYZ" for r in seen)
    assert [r.method + " " + r.url.path for r in seen] == [
        "GET /drive/v3/files/f1", "GET /drive/v3/files/f1/export", "POST /drive/v3/files/f1/copy",
        "POST /v1/documents/newdoc:batchUpdate", "GET /drive/v3/files/newdoc/export", "POST /upload/drive/v3/files"]
    assert json.loads(seen[2].content) == {"name": "LOI - 1 Fake St - 2026-11-05 v1", "parents": ["folder9"]}
    assert json.loads(seen[3].content) == {"requests": [{"replaceAllText": {
        "containsText": {"text": "{{x}}", "matchCase": True}, "replaceText": "Value"}}]}
    body = seen[5].content
    assert b'"parents": ["folder9"]' in body and b"LOI - 1 Fake St - 2026-11-05 v1.pdf" in body and b"%PDF-1" in body
    assert seen[5].headers["content-type"].startswith("multipart/related; boundary=")


def test_google_errors_are_plain_english():
    g = google([])
    with pytest.raises(DriveError, match="couldn't find that file"):
        g.describe("missing")
    with pytest.raises(DriveError, match="Docs API has not been used") as why:
        g.describe("locked")
    assert "ACCESS-TOKEN" not in str(why.value)

    def down(request):
        raise httpx.ConnectError("no network")
    off = GoogleDrive(httpx.Client(transport=httpx.MockTransport(down)), lambda: "ACCESS-TOKEN-XYZ")
    with pytest.raises(DriveError, match="Couldn't reach Google"):
        off.describe("f1")


# --- Google sign-in ------------------------------------------------------------------------------------



def test_sign_in_trades_the_code_for_a_private_token_and_prints_no_token(settings, capsys):
    put_client(settings)
    posted = []

    def token_service(request: httpx.Request) -> httpx.Response:
        posted.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "ACCESS", "refresh_token": REFRESH, "expires_in": 3600})

    def browser(url: str) -> None:
        q = httpx.URL(url).params
        assert q["code_challenge_method"] == "S256" and "documents" in q["scope"] and q["access_type"] == "offline"
        httpx.get(q["redirect_uri"], params={"code": "bad", "state": "other"})              # an answer for another request

    http = httpx.Client(transport=httpx.MockTransport(token_service))
    with pytest.raises(auth.SignInError, match="in time"):           # the stray answer is ignored, not fatal
        auth.sign_in(settings, http=http, open_page=browser, timeout=2)
    assert not auth.signed_in(settings)

    def browser_stray_first(url: str) -> None:
        q = httpx.URL(url).params
        httpx.get(q["redirect_uri"], params={"code": "bad", "state": "other"})
        httpx.get(q["redirect_uri"], params={"code": "real-code", "state": q["state"]})

    auth.sign_in(settings, http=http, open_page=browser_stray_first, timeout=5)
    assert auth.signed_in(settings) and posted[-1]["code"] == "real-code"
    auth.token_path(settings).unlink()

    def browser_ok(url: str) -> None:
        q = httpx.URL(url).params
        httpx.get(q["redirect_uri"], params={"code": "the-code", "state": q["state"]})

    auth.sign_in(settings, http=http, open_page=browser_ok, timeout=5)
    assert auth.signed_in(settings) and posted[-1]["code"] == "the-code" and posted[-1]["code_verifier"]
    assert (auth.token_path(settings).stat().st_mode & 0o777) == 0o600
    shown = capsys.readouterr()
    assert REFRESH not in shown.out + shown.err and "ACCESS" not in shown.out.replace("access_type", "")


def test_the_access_token_is_renewed_from_the_saved_one(settings):
    put_client(settings)
    auth._save_token(settings, REFRESH)
    calls = []

    def token_service(request: httpx.Request) -> httpx.Response:
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": f"ACCESS-{len(calls)}", "expires_in": 3600})

    creds = auth.Credentials(settings, httpx.Client(transport=httpx.MockTransport(token_service)))
    assert creds.bearer() == "ACCESS-1" and creds.bearer() == "ACCESS-1"       # kept until it runs out
    assert calls[0]["grant_type"] == "refresh_token" and calls[0]["refresh_token"] == REFRESH


def test_sign_in_problems_name_what_to_do_and_never_echo_google(settings):
    with pytest.raises(auth.SignInError, match="client file is missing"):
        auth.load_client(settings)
    auth.client_path(settings).write_text("{not json", encoding="utf-8")
    with pytest.raises(auth.SignInError, match="isn't a Google client file"):
        auth.load_client(settings)
    put_client(settings)
    auth._save_token(settings, REFRESH)
    refused = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": REFRESH})))
    with pytest.raises(auth.SignInError) as why:
        auth.Credentials(settings, refused).bearer()
    assert REFRESH not in str(why.value) and "signin" in str(why.value)
    auth.token_path(settings).unlink()
    with pytest.raises(auth.SignInError, match="loi signin"):
        auth.Credentials(settings, refused).bearer()
    assert service.open_adapter(settings) is None                              # no token: no adapter


# --- routes --------------------------------------------------------------------------------------------

@pytest.fixture
def client(conn):
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def oroute(oid, tail=""):
    return f"/api/m/{MARKET}/offers/{oid}{tail}"


def test_the_settings_routes_save_links_and_read_the_template(client, conn, settings, monkeypatch):
    got = client.get("/api/loi").json()
    assert got["active"] is False and got["template_id"] is None and any(m["token"] == "seller_name" for m in got["map"])
    assert "loi" in client.get("/api/settings").json()
    assert client.put("/api/loi", json={"template": "not a link", "folder": ""}).status_code == 400
    r = client.put("/api/loi", json={"template": f"https://docs.google.com/document/d/{TEMPLATE_ID}/edit",
                                     "folder": f"https://drive.google.com/drive/folders/{FOLDER_ID}"})
    assert r.status_code == 200 and r.json()["template_id"] == TEMPLATE_ID and r.json()["folder_id"] == FOLDER_ID
    assert client.post("/api/loi/check").status_code == 400                     # not signed in to Google
    drive = FakeDrive(TEMPLATE_ID, FIELDS_ONLY, FOLDER_ID)
    sign_in_for_real(settings)
    monkeypatch.setattr(service, "open_adapter", lambda s: drive)
    st = client.post("/api/loi/check").json()
    assert st["check"]["tokens"][0] == "seller_name" and st["problems"] == [] and st["active"] is True


def test_the_offer_routes_make_the_letter_and_move_it_along(client, conn, settings, monkeypatch):
    pid, o = draft(conn)
    drive = configure(conn)
    monkeypatch.setattr(service, "open_adapter", lambda s: drive)
    pre = client.get(oroute(o["id"], "/loi")).json()
    assert pre["note"].startswith("Field review") and pre["rows"][0]["token"] == "seller_name"
    assert client.get(oroute(99999, "/loi")).status_code == 404
    r = client.post(oroute(o["id"], "/loi"), json={})
    assert r.status_code == 200 and "LOI made" in r.json()["message"]
    mine = r.json()["offers"]["offers"][0]
    assert mine["status"] == "loi_generated" and mine["documents"][0]["current"] and mine["documents"][0]["pdf_url"].startswith("https://drive.google.com/")
    assert "A letter was already made" in client.post(oroute(o["id"], "/loi"), json={}).json()["message"]
    assert client.put(oroute(o["id"], "/status"), json={"status": "sent"}).status_code == 422       # only Mark as sent does that
    assert client.put(oroute(o["id"], "/status"), json={"status": "ready_for_review"}).json()["offers"]["offers"][0]["status"] == "ready_for_review"
    assert client.put(oroute(o["id"], "/status"), json={"status": "ready_to_send"}).status_code == 200
    assert client.post(oroute(o["id"], "/sent"), json={"how": "email"}).status_code == 200
    assert client.post(oroute(o["id"], "/loi"), json={"again": True}).status_code == 400
    assert client.put(oroute(o["id"], "/status"), json={"status": "ready_for_review"}).status_code == 400
    assert client.post(oroute(o["id"], "/loi"), json={"again": "nope"}).status_code == 422


def test_a_google_failure_reaches_the_screen_as_a_plain_message(client, conn, settings, monkeypatch):
    pid, o = draft(conn)
    monkeypatch.setattr(service, "open_adapter", lambda s: configure(conn, fail_at="copy"))
    r = client.post(oroute(o["id"], "/loi"), json={})
    assert r.status_code == 400 and "turned down" in r.json()["detail"]


def test_the_routes_need_sign_in_when_a_password_is_set(client, settings, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery")
    for method, path in (("get", "/api/loi"), ("put", "/api/loi"), ("post", "/api/loi/check"),
                         ("get", oroute(1, "/loi")), ("post", oroute(1, "/loi")), ("put", oroute(1, "/status"))):
        assert getattr(client, method)(path).status_code == 401, path


def test_a_write_from_another_website_is_refused(client):
    r = client.put("/api/loi", json={"template": "", "folder": ""}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


# --- the command line ----------------------------------------------------------------------------------

def test_the_loi_command_reports_where_things_stand(settings, capsys, monkeypatch):
    assert run.main(["loi", "status"]) == 0
    said = capsys.readouterr().out
    assert "Google sign-in: needed" in said and "client file is missing" in said and "Master Doc: not set" in said
    drive = FakeDrive(TEMPLATE_ID, FIELDS_ONLY, FOLDER_ID)
    from bluemoon.db import connect
    conn = connect(settings=settings)
    service.save_links(conn, TEMPLATE_ID, FOLDER_ID)
    conn.close()
    sign_in_for_real(settings)
    monkeypatch.setattr(service, "open_adapter", lambda s: drive)
    assert run.main(["loi", "test"]) == 0
    said = capsys.readouterr().out
    assert "Read 'Master'" in said and "seller_name, recipient_name, offer_price, property_full_address, offer_version" in said
    assert "Letters can be made." in said
    monkeypatch.setattr(service, "open_adapter", lambda s: None)
    assert run.main(["loi", "test"]) == 1
    assert "Google sign-in needed" in capsys.readouterr().out


def test_the_loi_signin_command_runs_the_sign_in(settings, capsys, monkeypatch):
    seen = {}
    monkeypatch.setattr(auth, "sign_in", lambda s, say: seen.update(ran=True) or say("Opening Google's sign-in page."))
    assert run.main(["loi", "signin"]) == 0
    assert seen["ran"] and "Signed in." in capsys.readouterr().out
    monkeypatch.setattr(auth, "sign_in", lambda s, say: (_ for _ in ()).throw(auth.SignInError("Nobody finished the sign-in in time.")))
    assert run.main(["loi", "signin"]) == 1


def test_the_one_sign_in_server_thread_does_not_leak(settings):
    put_client(settings)
    before = threading.active_count()
    with pytest.raises(auth.SignInError, match="in time"):
        auth.sign_in(settings, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                     open_page=lambda url: None, timeout=0.2, say=lambda text: None)
    assert threading.active_count() <= before + 1


# --- review fixes (drive) --------------------------------------------------------------------------------------

def test_a_price_changed_while_the_letter_is_made_means_the_letter_is_not_kept(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn)
    real = drive.replace_tokens

    def edit_midway(doc_id, replacements):
        offers.set_price(conn, o["id"], "1700000", "seth")           # someone types a new price during the fill
        real(doc_id, replacements)

    drive.replace_tokens = edit_midway
    with pytest.raises(ValueError, match="changed while the letter was being made"):
        make(conn, settings, drive, pid, o["id"])
    assert scalar(conn, "SELECT COUNT(*) FROM offer_documents") == 0
    assert status_of(conn, o["id"]) == "internal_draft" and "document_stale" in events(conn, o["id"])
    with pytest.raises(ValueError, match="no LOI"):
        offers.set_status(conn, o["id"], "loi_generated", "seth")


def test_correcting_the_address_after_the_letter_makes_it_out_of_date(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn)
    make(conn, settings, drive, pid, o["id"])
    offers.set_status(conn, o["id"], "ready_for_review", "seth")
    conn.execute("UPDATE properties SET address='99 CORRECTED RD', zip='45502' WHERE id=?", (pid,))
    conn.commit()
    assert not offers.current_document(conn, offers._get(conn, o["id"]))
    with pytest.raises(ValueError, match="no LOI"):
        offers.set_status(conn, o["id"], "ready_to_send", "seth")
    assert make(conn, settings, drive, pid, o["id"])["result"] == "generated"       # not 'exists': a new copy is made
    offers.set_status(conn, o["id"], "ready_for_review", "seth")


def test_a_blocked_attempt_does_not_downgrade_an_offer_with_a_matching_letter(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn)
    make(conn, settings, drive, pid, o["id"])
    offers.set_status(conn, o["id"], "ready_for_review", "seth")
    out = make(conn, settings, None, pid, o["id"], again=True)                 # sign-in lapsed
    assert out["result"] == "template_needed"
    assert status_of(conn, o["id"]) == "ready_for_review" and "document_blocked" in events(conn, o["id"])


def test_a_second_click_waits_and_finds_the_letter_already_made(conn, settings):
    pid, o = draft(conn)
    drive = configure(conn)
    results = []
    threads = [threading.Thread(target=lambda: results.append(make(conn, settings, drive, pid, o["id"])["result"]))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["exists", "generated"]
    assert len([c for c in drive.calls if c[0] == "copy"]) == 1


def test_a_value_with_braces_is_blocked_before_any_copy(conn, settings):
    pid = add_prop(conn, owner_name="{{offer_price}} HOLDINGS")
    napkin.add_record(conn, pid, source="manual", basis="scheduled", actor="seth", total="34240", total_period="monthly")
    o = offers.prepare(conn, prop_of(conn, pid), snap(conn, pid), "seth")
    drive = configure(conn)
    out = make(conn, settings, drive, pid, o["id"])
    assert out["result"] == "fields_missing" and "seller_name" in out["message"]
    assert not [c for c in drive.calls if c[0] == "copy"]


def test_unreadable_google_answers_and_a_lapsed_sign_in_become_drive_errors():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>proxy</html>")))
    with pytest.raises(DriveError, match="wasn't readable"):
        GoogleDrive(http, lambda: "tok").copy("a", "t", "f")
    with pytest.raises(DriveError, match="wasn't readable"):
        GoogleDrive(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
                    lambda: "tok").describe("a")

    def lapsed() -> str:
        raise auth.SignInError("Google sign-in needed.")
    with pytest.raises(DriveError, match="sign-in needed"):
        GoogleDrive(http, lapsed).describe("a")
