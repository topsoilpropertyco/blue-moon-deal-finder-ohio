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


def test_command_is_a_plain_tunnel():
    """Cloudflare's --allowed-mail gate drops its session seconds after login (cloudflared #1752), so the
    dashboard signs people in itself and the tunnel is plain."""
    cmd = share.build_command("/usr/local/bin/cloudflared", 8765)
    assert cmd == ["/usr/local/bin/cloudflared", "tunnel", "--no-autoupdate", "--url", "http://localhost:8765"]


def test_plan_refuses_an_open_link(settings, monkeypatch):
    monkeypatch.setattr(share, "find_cloudflared", lambda: "/usr/local/bin/cloudflared")
    p = share.plan()
    assert p["mode"] == "password" and "share password" in p["problem"]
    share.add_emails(["pat@example.com"])
    p = share.plan()                      # people on the list still need the team password
    assert p["mode"] == "team" and "share password" in p["problem"]
    monkeypatch.setenv("APP_PASSWORD", "four random words here")
    p = share.plan()
    assert p["mode"] == "team" and p["problem"] is None and p["emails"] == ["pat@example.com"]


def test_plan_says_how_to_install_cloudflared(settings, monkeypatch):
    monkeypatch.setattr(share, "find_cloudflared", lambda: None)
    assert "install_cloudflared" in share.plan()["problem"]


def test_outside_access_rules(settings):
    assert share.outside_access_ok("localhost:8765", has_password=False)
    assert share.outside_access_ok("127.0.0.1:8765", has_password=False)
    assert not share.outside_access_ok("x-y.trycloudflare.com", has_password=False)
    assert share.outside_access_ok("x-y.trycloudflare.com", has_password=True)
    # A running share no longer opens the door by itself: the team password does.
    share.state_file().write_text(json.dumps({"url": "https://x-y.trycloudflare.com", "mode": "team",
                                              "pid": os.getpid(), "people": 1}))
    assert not share.outside_access_ok("x-y.trycloudflare.com", has_password=False)
    share.state_file().write_text(json.dumps({"url": "u", "mode": "team", "pid": 999999999, "people": 1}))
    assert share.current() is None                                       # a dead share counts as off


def test_on_this_computer_only_without_cloudflare():
    assert share.on_this_computer("127.0.0.1", "localhost:8765", {})
    assert share.on_this_computer("::1", "127.0.0.1:8765", {})
    # Through the tunnel the peer is this computer too, but the name and Cloudflare's headers give it away.
    assert not share.on_this_computer("127.0.0.1", "x-y.trycloudflare.com", {"cf-connecting-ip": "203.0.113.9"})
    assert not share.on_this_computer("127.0.0.1", "localhost:8765", {"cf-ray": "abc"})
    assert not share.on_this_computer("127.0.0.1", "x-y.trycloudflare.com", {})
    assert not share.on_this_computer("192.168.1.20", "localhost:8765", {})
    assert not share.on_this_computer("testclient", "testserver", {})


def test_allows_exact_and_company_wide_entries(settings):
    share.add_emails(["pat@example.com", "*@ruralopportunity.org"])
    assert share.allows("Pat@Example.com ")
    assert share.allows("seth@ruralopportunity.org")
    assert not share.allows("someone@else.com")
    assert not share.allows("*@ruralopportunity.org") and not share.allows("") and not share.allows(None)


def test_team_password_is_made_and_saved_privately(settings, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "")      # blank = not set; restored after the test
    env = share.env_file()
    env.write_text("GOOGLE_MAPS_API_KEY=\nAPP_PASSWORD=\nCENSUS_API_KEY=\n")
    pw = share.make_password()
    assert pw.startswith("bluemoon-") and len(pw) == len("bluemoon-xxxx-xxxx-xxxx")
    assert share.make_password() != pw
    share.save_password(pw)
    text = env.read_text()
    assert f"APP_PASSWORD={pw}\n" in text and text.count("APP_PASSWORD=") == 1
    assert "GOOGLE_MAPS_API_KEY=\n" in text and "CENSUS_API_KEY=\n" in text      # other lines kept
    assert os.environ["APP_PASSWORD"] == pw
    assert env.stat().st_mode & 0o077 == 0


def test_client_ip_trusts_cloudflare_only_from_this_computer():
    assert share.client_ip("127.0.0.1", {"cf-connecting-ip": "203.0.113.9"}) == "203.0.113.9"
    assert share.client_ip("198.51.100.4", {"cf-connecting-ip": "203.0.113.9"}) == "198.51.100.4"
    assert share.client_ip("127.0.0.1", {}) == "127.0.0.1"


def test_run_saves_and_clears_the_link(settings, monkeypatch, tmp_path):
    fake = tmp_path / "cloudflared"
    fake.write_text("#!/bin/sh\necho \"$@\" > " + str(tmp_path / "args") +
                    "\necho 'INF |  https://gentle-words-here.trycloudflare.com  |'\nsleep 0.2\n")
    fake.chmod(0o755)
    monkeypatch.setattr(share, "find_cloudflared", lambda: str(fake))
    monkeypatch.setenv("APP_PASSWORD", "four random words here")
    share.add_emails(["pat@example.com"])
    seen = {}

    def on_url(url):
        seen["url"] = url
        seen["state"] = share.current()

    assert share.run(on_url=on_url) == 0
    assert seen["url"] == "https://gentle-words-here.trycloudflare.com"
    assert seen["state"]["mode"] == "team" and seen["state"]["people"] == 1
    assert "allowed-mail" not in (tmp_path / "args").read_text()
    assert share.current() is None and not share.state_file().exists()     # cleared when it stops


def test_web_shows_share_status(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from bluemoon.web.app import app
    monkeypatch.setattr(share, "find_cloudflared", lambda: None)
    with TestClient(app) as c:
        st = c.get("/api/health").json()["share"]
        assert st["on"] is False and st["cloudflared"] is False and "install_cloudflared" in st["problem"]
        share.state_file().write_text(json.dumps({"url": "https://a-b.trycloudflare.com", "mode": "team",
                                                  "pid": os.getpid(), "people": 2,
                                                  "started_at": "2026-10-21T12:00:00+00:00"}))
        st = c.get("/api/settings").json()["share"]
        assert st["on"] is True and st["url"] == "https://a-b.trycloudflare.com"
        assert st["password_set"] is False
        # No team password: tunnel visitors are refused, never let in.
        assert c.get("/api/markets", headers={"Host": "a-b.trycloudflare.com"}).status_code == 403


def test_people_status_shows_who_signed_in(settings):
    share.add_emails(["a@x.com", "b@x.com", "*@team.org"])
    assert [p["status"] for p in share.people_status()] == ["not_yet", "not_yet", "everyone"]
    share.record_seen("a@x.com", force=True)
    share.record_seen("kim@team.org", force=True)
    got = {p["email"]: p for p in share.people_status()}
    assert got["a@x.com"]["status"] == "signed_in" and got["a@x.com"]["last_seen"]
    assert got["b@x.com"]["status"] == "not_yet"
    assert got["kim@team.org"]["status"] == "signed_in" and got["kim@team.org"]["via"] == "*@team.org"
    assert share.seen_file().stat().st_mode & 0o077 == 0


def test_team_sign_in_through_the_link(settings, monkeypatch):
    """The whole door, as a teammate on the Cloudflare link sees it."""
    from fastapi.testclient import TestClient
    import bluemoon.web.app as web_app
    from bluemoon.web.app import app

    async def no_pause():
        return None

    monkeypatch.setattr(web_app, "_login_pause", no_pause)
    web_app._login_failures.clear()
    monkeypatch.setenv("APP_PASSWORD", "team words here ok")
    share.add_emails(["pat@example.com"])
    tunnel = {"Host": "a-b.trycloudflare.com", "cf-connecting-ip": "203.0.113.9"}
    with TestClient(app, base_url="https://a-b.trycloudflare.com") as c:
        r = c.get("/api/markets", headers=tunnel)
        assert r.status_code == 401 and r.json()["email"] is True
        page = c.get("/login", headers=tunnel)
        assert 'id="em"' in page.text and "team password" in page.text
        assert c.post("/api/login", json={"password": "team words here ok"}, headers=tunnel).status_code == 400
        assert c.post("/api/login", json={"password": "wrong", "email": "pat@example.com"},
                      headers=tunnel).status_code == 401
        r = c.post("/api/login", json={"password": "team words here ok", "email": "stranger@example.com"},
                   headers=tunnel)
        assert r.status_code == 403 and "isn't on the team list" in r.json()["detail"]
        r = c.post("/api/login", json={"password": "team words here ok", "email": "Pat@Example.com"},
                   headers=tunnel)
        assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
        assert c.get("/api/markets", headers=tunnel).status_code == 200
        h = c.get("/api/health", headers=tunnel).json()
        assert h["signed_in_as"] == "pat@example.com" and h["login_email"] is True
        assert share.people_status()[0]["status"] == "signed_in"
        share.remove_emails(["pat@example.com"])                  # removed: out on the next click
        share.add_emails(["someone@example.com"])
        assert c.get("/api/markets", headers=tunnel).status_code == 401
        share.add_emails(["pat@example.com"])                     # back on the list: the cookie works again
        assert c.get("/api/markets", headers=tunnel).status_code == 200
        monkeypatch.setenv("APP_PASSWORD", "a brand new password")   # new password: everyone signs in again
        assert c.get("/api/markets", headers=tunnel).status_code == 401


def test_password_only_cookie_needs_an_email_once_people_are_listed(settings, monkeypatch):
    from fastapi.testclient import TestClient
    import bluemoon.web.app as web_app
    from bluemoon.web.app import app

    async def no_pause():
        return None

    monkeypatch.setattr(web_app, "_login_pause", no_pause)
    web_app._login_failures.clear()
    monkeypatch.setenv("APP_PASSWORD", "team words here ok")
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "team words here ok"}).status_code == 200
        assert c.get("/api/markets").status_code == 200
        assert c.get("/api/health").json()["signed_in_as"] == "team"
        share.add_emails(["pat@example.com"])
        r = c.get("/api/markets")
        assert r.status_code == 401 and r.json()["email"] is True


def test_ngrok_gives_a_permanent_address_and_needs_its_setup(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PASSWORD", "four random words here")
    monkeypatch.setenv("NGROK_AUTHTOKEN", "")
    monkeypatch.setattr(share, "find_cloudflared", lambda: "/x/cloudflared")
    settings.raw["share"] = {"provider": "ngrok", "ngrok_domain": ""}
    assert "isn't set" in share.plan(settings)["problem"]
    settings.raw["share"] = {"provider": "ngrok", "ngrok_domain": "https://Deals-Example.ngrok-free.app/"}
    assert share.share_settings(settings)["ngrok_domain"] == "deals-example.ngrok-free.app"
    monkeypatch.setattr(share, "find_ngrok", lambda: None)
    assert "install_ngrok" in share.plan(settings)["problem"]
    monkeypatch.setattr(share, "find_ngrok", lambda: "/x/ngrok")
    assert "key ngrok" in share.plan(settings)["problem"]
    monkeypatch.setenv("NGROK_AUTHTOKEN", "tok" * 10)
    assert share.plan(settings)["problem"] is None
    assert share.ngrok_command("/x/ngrok", 8765, "deals-example.ngrok-free.app")[:3] == [
        "/x/ngrok", "http", "8765"]


def test_run_with_ngrok_reports_the_fixed_address(settings, monkeypatch, tmp_path):
    fake = tmp_path / "ngrok"
    fake.write_text('#!/bin/sh\necho \'t=1 lvl=info msg="started tunnel" obj=tunnels url=https://deals-example.ngrok-free.app\'\nsleep 0.2\n')
    fake.chmod(0o755)
    monkeypatch.setenv("APP_PASSWORD", "four random words here")
    monkeypatch.setenv("NGROK_AUTHTOKEN", "tok" * 10)
    monkeypatch.setattr(share, "find_ngrok", lambda: str(fake))
    settings.raw["share"] = {"provider": "ngrok", "ngrok_domain": "deals-example.ngrok-free.app"}
    seen = {}
    assert share.run(settings, on_url=lambda u: seen.setdefault("url", u)) == 0
    assert seen["url"] == "https://deals-example.ngrok-free.app"
    assert share.client_ip("127.0.0.1", {"x-forwarded-for": "9.9.9.9, 203.0.113.7"}) == "203.0.113.7"
