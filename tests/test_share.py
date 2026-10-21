"""Sharing the dashboard through Cloudflare's free tunnel: the people list, the command, and the door checks."""
from __future__ import annotations

import json
import os

import pytest

from bluemoon import share


def test_people_list_add_remove_and_validate(settings):
    assert share.list_emails() == []
    assert share.add_emails(["Pat@Example.com", "*@ruralopportunity.org"]) == ["pat@example.com",
                                                                               "*@ruralopportunity.org"]
    assert share.add_emails(["pat@example.com"]) == ["pat@example.com", "*@ruralopportunity.org"]  # no dupes
    assert share.remove_emails(["pat@example.com"]) == ["*@ruralopportunity.org"]
    with pytest.raises(share.ShareError):
        share.add_emails(["not an email"])
    assert share.emails_file().stat().st_mode & 0o077 == 0          # private file


def test_command_has_one_allowed_mail_per_person():
    cmd = share.build_command("/usr/local/bin/cloudflared", 8765, ["a@x.com", "*@y.org"])
    assert cmd[:5] == ["/usr/local/bin/cloudflared", "tunnel", "--no-autoupdate", "--url", "http://localhost:8765"]
    assert cmd[5:] == ["--allowed-mail", "a@x.com", "--allowed-mail", "*@y.org"]


def test_plan_refuses_an_open_link(settings, monkeypatch):
    monkeypatch.setattr(share, "find_cloudflared", lambda: "/usr/local/bin/cloudflared")
    p = share.plan()
    assert p["mode"] == "password" and "open to anyone" in p["problem"]
    monkeypatch.setenv("APP_PASSWORD", "four random words here")
    assert share.plan()["problem"] is None
    monkeypatch.delenv("APP_PASSWORD")
    share.add_emails(["pat@example.com"])
    p = share.plan()
    assert p["mode"] == "email" and p["problem"] is None and p["emails"] == ["pat@example.com"]


def test_plan_says_how_to_install_cloudflared(settings, monkeypatch):
    monkeypatch.setattr(share, "find_cloudflared", lambda: None)
    assert "install_cloudflared" in share.plan()["problem"]


def test_outside_access_rules(settings):
    assert share.outside_access_ok("localhost:8765", has_password=False)
    assert share.outside_access_ok("127.0.0.1:8765", has_password=False)
    assert not share.outside_access_ok("x-y.trycloudflare.com", has_password=False)
    assert share.outside_access_ok("x-y.trycloudflare.com", has_password=True)
    # A live email-code share lets tunnel visitors in (Cloudflare checked their code already).
    share.state_file().write_text(json.dumps({"url": "https://x-y.trycloudflare.com", "mode": "email",
                                              "pid": os.getpid(), "people": 1}))
    assert share.outside_access_ok("x-y.trycloudflare.com", has_password=False)
    assert not share.outside_access_ok("100.1.2.3:8765", has_password=False)   # but not other routes in
    # A dead share (process gone) counts as off.
    share.state_file().write_text(json.dumps({"url": "u", "mode": "email", "pid": 999999999, "people": 1}))
    assert share.current() is None
    assert not share.outside_access_ok("x-y.trycloudflare.com", has_password=False)


def test_client_ip_trusts_cloudflare_only_from_this_computer():
    assert share.client_ip("127.0.0.1", {"cf-connecting-ip": "203.0.113.9"}) == "203.0.113.9"
    assert share.client_ip("198.51.100.4", {"cf-connecting-ip": "203.0.113.9"}) == "198.51.100.4"
    assert share.client_ip("127.0.0.1", {}) == "127.0.0.1"


def test_run_saves_and_clears_the_link(settings, monkeypatch, tmp_path):
    fake = tmp_path / "cloudflared"
    fake.write_text("#!/bin/sh\necho 'INF |  https://gentle-words-here.trycloudflare.com  |'\nsleep 0.2\n")
    fake.chmod(0o755)
    monkeypatch.setattr(share, "find_cloudflared", lambda: str(fake))
    share.add_emails(["pat@example.com"])
    seen = {}

    def on_url(url):
        seen["url"] = url
        seen["state"] = share.current()

    assert share.run(on_url=on_url) == 0
    assert seen["url"] == "https://gentle-words-here.trycloudflare.com"
    assert seen["state"]["mode"] == "email" and seen["state"]["people"] == 1
    assert share.current() is None and not share.state_file().exists()     # cleared when it stops


def test_web_shows_share_status(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from bluemoon.web.app import app
    monkeypatch.setattr(share, "find_cloudflared", lambda: None)
    with TestClient(app) as c:
        st = c.get("/api/health").json()["share"]
        assert st["on"] is False and st["cloudflared"] is False and "install_cloudflared" in st["problem"]
        share.state_file().write_text(json.dumps({"url": "https://a-b.trycloudflare.com", "mode": "email",
                                                  "pid": os.getpid(), "people": 2,
                                                  "started_at": "2026-10-21T12:00:00+00:00"}))
        st = c.get("/api/settings").json()["share"]
        assert st["on"] is True and st["url"] == "https://a-b.trycloudflare.com" and st["mode"] == "email"
        # Through the tunnel with email codes: allowed in without a password.
        assert c.get("/api/markets", headers={"Host": "a-b.trycloudflare.com"}).status_code == 200


def test_people_status_tracks_restarts(settings):
    share.add_emails(["a@x.com", "b@x.com"])
    assert [p["status"] for p in share.people_status()] == ["listed", "listed"]      # sharing off
    share.state_file().write_text(json.dumps({"url": "https://q.trycloudflare.com", "mode": "email",
                                              "pid": os.getpid(), "people": 2, "emails": ["a@x.com", "c@x.com"]}))
    got = {p["email"]: p["status"] for p in share.people_status()}
    assert got == {"a@x.com": "active", "b@x.com": "waiting", "c@x.com": "removed"}


def test_settings_lists_people(settings):
    from fastapi.testclient import TestClient
    from bluemoon.web.app import app
    share.add_emails(["pat@example.com"])
    share.state_file().write_text(json.dumps({"url": "https://q.trycloudflare.com", "mode": "email",
                                              "pid": os.getpid(), "people": 1, "emails": ["pat@example.com"]}))
    with TestClient(app) as c:
        st = c.get("/api/settings").json()["share"]
    assert st["people_list"] == [{"email": "pat@example.com", "status": "active"}] and st["needs_restart"] is False
