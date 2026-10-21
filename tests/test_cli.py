"""The command-line tool (run.py), run in-process with main(argv) in a temporary BLUEMOON_HOME, plus one
real subprocess run. No network: real HTTP is refused and HUD is faked."""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import run
from bluemoon import pipeline
from bluemoon.db import connect, scalar
from bluemoon.enrich import rent

REPO = Path(__file__).resolve().parent.parent
SECRET = "TESTKEY-cli-must-never-print-0123456789"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(self, request):
        raise AssertionError(f"Tried to reach the network: {request.url.host}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture
def fake_hud(monkeypatch, settings):
    settings.raw.setdefault("rent", {}).update(retry_backoff_seconds=0, max_retries=0)

    @contextlib.contextmanager
    def scope(client, cfg):
        yield httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"features": []})))

    monkeypatch.setattr(rent, "_client_scope", scope)


@pytest.fixture
def cli(settings, capsys):
    """Run the CLI; returns (exit code, printed text)."""
    def call(*argv: str) -> tuple[int, str]:
        code = run.main([str(a) for a in argv])
        captured = capsys.readouterr()
        return code, captured.out + captured.err
    return call


@pytest.fixture
def imported(cli, sample_csv):
    code, text = cli("import", sample_csv)
    assert code == 0, text
    return text


def count(settings, sql: str) -> int:
    conn = connect(settings=settings)
    try:
        return int(scalar(conn, sql) or 0)
    finally:
        conn.close()


def test_import_ranks_and_reports(imported, settings):
    assert "Imported 1 file: 11 new" in imported and "fit the buy box" in imported
    assert count(settings, "SELECT COUNT(*) FROM scores") == 11


def test_import_a_folder_or_the_default_crexi_folder(cli, settings, sample_csv):
    shutil.copy(sample_csv, settings.path("crexi_dir") / "export.csv")
    code, text = cli("import")
    assert code == 0 and "Imported 1 file" in text
    code, text = cli("import", settings.path("crexi_dir"), "--dry-run")
    assert code == 0 and "Preview only" in text


def _owner_file(sample_csv: Path, dest: Path, owner: str, sale: str, mtime: float) -> Path:
    lines = sample_csv.read_text(encoding="utf-8").splitlines()
    head = lines[0].split(",")
    row = lines[1].split(",")
    row[head.index("Owner Name")] = owner
    row[head.index("Sale Date")] = sale
    dest.write_text("\n".join([lines[0], ",".join(row)]) + "\n", encoding="utf-8")
    os.utime(dest, (mtime, mtime))
    return dest


def _owner_of(settings) -> str:
    conn = connect(settings=settings)
    try:
        return scalar(conn, "SELECT owner_name FROM properties WHERE address = '1200 SAMPLE RIDGE DR'")
    finally:
        conn.close()


def test_folder_import_keeps_the_newest_owner_and_skips_old_files(cli, settings, sample_csv):
    folder = settings.path("crexi_dir")
    older = _owner_file(sample_csv, folder / "crexi_october.csv", "OLD OWNER TEST LLC", "1998-05-01", 1_700_000_000)
    newer = _owner_file(sample_csv, folder / "20261101-090000_crexi_november.csv", "NEW BUYER TEST LLC",
                        "2026-09-15", 1_800_000_000)
    assert cli("import", older)[0] == 0 and cli("import", newer)[0] == 0
    assert _owner_of(settings) == "NEW BUYER TEST LLC"
    code, text = cli("import")
    assert code == 0 and "Skipped 2" in text and "Nothing new in data/crexi/" in text
    assert _owner_of(settings) == "NEW BUYER TEST LLC"
    code, text = cli("import", "--force")                     # oldest first, so the newest still wins
    assert code == 0 and "Imported 2 files" in text
    assert _owner_of(settings) == "NEW BUYER TEST LLC"


def test_folder_import_skips_another_markets_upload(cli, settings, sample_csv):
    upload = settings.path("crexi_dir") / "20261101-090000_storage.csv"
    shutil.copy(sample_csv, upload)
    assert cli("import", upload, "--market", "self-storage")[0] == 0
    code, text = cli("import")
    assert code == 0 and "Skipped 1" in text
    assert count(settings, "SELECT COUNT(*) FROM properties p JOIN markets m ON m.id = p.market_id "
                           "WHERE m.slug = 'apartments-springfield-oh'") == 0


def test_import_nothing_found(cli, settings):
    code, text = cli("import")
    assert code == 1 and "No Crexi export found" in text


def test_import_a_bad_file(cli, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    code, text = cli("import", bad)
    assert code == 1 and "bad.csv" in text


def test_status_shows_keys_as_yes_no_only(cli, imported, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", SECRET)
    code, text = cli("status")
    assert code == 0
    assert "Google yes" in text and "Anthropic no" in text and SECRET not in text
    assert "Apartments · Springfield, OH" in text and "11 properties" in text
    assert "Contact levels:" in text and "Penetration" in text and "Layers:" in text
    assert "No properties yet:" in text and "self-storage-springfield-oh" in text
    assert "Crexi credits" in text and "Last import: crexi_sample.csv" in text


def test_status_for_one_market(cli, imported):
    code, text = cli("status", "--market", "apartments")
    assert code == 0 and "apartments-springfield-oh" in text and "self-storage" not in text


@pytest.mark.parametrize("argv", [("--market", "apartments", "score"), ("score", "--market", "apartments")])
def test_options_work_before_or_after_the_command(cli, imported, argv):
    code, text = cli(*argv)
    assert code == 0 and text.startswith("Scores · apartments-springfield-oh") and "Done in" in text


def test_free_steps(cli, imported, fake_hud):
    for step in ("owners", "drive", "rent", "score", "drill"):
        code, text = cli(step, "--limit", "5")
        assert code == 0, (step, text)


def test_unknown_market_and_bad_limit(cli):
    code, text = cli("score", "--market", "atlantis")
    assert code == 1 and "is not a market" in text and "apartments-springfield-oh" in text
    with pytest.raises(SystemExit) as stop:
        cli("score", "--limit", "0")
    assert stop.value.code == 2


def test_estimate_and_dry_run(cli, imported):
    code, text = cli("estimate", "--limit", "5")
    assert code == 0 and "Nothing was called" in text and "Photos:" in text and "About $" in text
    code, text = cli("photos", "--dry-run")
    assert code == 0 and "(dry run)" in text and "to fetch" in text
    code, text = cli("all", "--dry-run")
    assert code == 0 and "Drive times:" in text


def test_paid_step_without_a_key_fails_plainly(cli, imported):
    code, text = cli("photos", "--limit", "2")
    assert code == 1 and "GOOGLE_MAPS_API_KEY" in text


def test_big_paid_run_stops_without_yes_when_nobody_can_answer(cli, imported, settings, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", SECRET)
    settings.raw.setdefault("safety", {})["confirm_over_properties"] = 2
    monkeypatch.setattr(run, "interactive", lambda: False)
    code, text = cli("photos")
    assert code == run.EXIT_WAITING
    assert "Waiting for your OK" in text and "Stopped before spending anything" in text
    assert "uv run run.py photos --market apartments-springfield-oh --yes" in text
    assert SECRET not in text
    assert count(settings, "SELECT COUNT(*) FROM api_calls") == 0


class FakeSteps:
    """Stands in for pipeline.run_step: asks for an OK until confirm=True."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def __call__(self, conn, market, step, limit=None, dry_run=False, confirm=False, progress=None, **kw):
        self.calls.append(confirm)
        if confirm:
            return {"step": step, "label": "Photos", "ok": True, "dry_run": False, "needs_confirm": False,
                    "message": "Fetched photos.", "warnings": [], "counts": {}}
        return {"step": step, "label": "Photos", "ok": True, "dry_run": False, "needs_confirm": True,
                "message": "Waiting for your OK: 60 properties, about $0.00.", "warnings": [], "counts": {},
                "estimate": {"message": "Waiting for your OK: 60 properties, about $0.00."}}


@pytest.mark.parametrize("answer, code, runs", [("y", 0, [False, True]), ("n", 3, [False]), ("", 3, [False])])
def test_terminal_asks_go_ahead(cli, monkeypatch, answer, code, runs):
    fake = FakeSteps()
    monkeypatch.setattr(pipeline, "run_step", fake)
    monkeypatch.setattr(run, "interactive", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or answer)
    got, text = cli("photos")
    assert got == code and fake.calls == runs
    assert asked == ["Go ahead? [y/N] "]
    assert ("Fetched photos." in text) == (answer == "y")


def test_yes_skips_the_question(cli, monkeypatch):
    fake = FakeSteps()
    monkeypatch.setattr(pipeline, "run_step", fake)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("should not ask"))
    code, _ = cli("photos", "--yes")
    assert code == 0 and fake.calls == [True]


def test_all_command(cli, imported, fake_hud):
    code, text = cli("all", "--limit", "5")
    assert code == 0
    assert "- Owners:" in text and "! Photos:" in text and "All steps done." in text


def test_all_waits_and_shows_the_full_estimate(cli, imported, fake_hud, settings, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", SECRET)
    settings.raw.setdefault("safety", {})["confirm_over_properties"] = 2
    monkeypatch.setattr(run, "interactive", lambda: False)
    code, text = cli("all")
    assert code == run.EXIT_WAITING
    assert "Before spending money:" in text and "- Photos:" in text and "--yes" in text
    assert SECRET not in text and count(settings, "SELECT COUNT(*) FROM api_calls") == 0


def test_export_command(cli, imported, settings):
    code, text = cli("export")
    assert code == 0 and "Saved 13 files to" in text and "ranked_list.xlsx" in text
    assert (settings.path("output") / "apartments-springfield-oh" / "run_summary.md").is_file()
    assert "handed off" not in text   # nobody is confirmed yet


def test_export_dry_run_writes_nothing(cli, imported, settings):
    code, text = cli("export", "--dry-run")
    assert code == 0 and "Preview only, nothing saved" in text and "Saved" not in text
    assert not (settings.path("output") / "apartments-springfield-oh").exists()


def test_calibrate_without_reviews(cli, imported, settings):
    settings.raw.setdefault("vision", {})["backend"] = "mock"
    code, text = cli("calibrate")
    assert code == 0 and "Calibrate" in text


def test_calibrate_for_a_market_without_photo_check(cli):
    code, text = cli("calibrate", "--market", "land-springfield-oh")
    assert code == 1 and "no photo check" in text


def test_serve_prints_the_address_and_starts_the_app(cli, monkeypatch):
    import uvicorn
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    monkeypatch.setattr(run.importlib.util, "find_spec", lambda name: object())
    code, text = cli("serve")
    assert code == 0 and "http://localhost:8765" in text
    assert started["app"] == "bluemoon.web.app:app" and started["host"] == "127.0.0.1" and started["port"] == 8765
    monkeypatch.setenv("APP_PASSWORD", "x")
    code, text = cli("serve", "--host", "0.0.0.0", "--port", "9000")
    assert started["host"] == "0.0.0.0" and started["port"] == 9000
    assert "From your phone over Tailscale" in text and "APP_PASSWORD is short" in text


def test_serve_without_a_password_stays_on_this_computer(cli, monkeypatch):
    import uvicorn
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    monkeypatch.setattr(run.importlib.util, "find_spec", lambda name: object())
    code, text = cli("serve", "--host", "0.0.0.0")
    assert code == 0 and started["host"] == "127.0.0.1"
    assert "APP_PASSWORD" in text and "From your phone over Tailscale" not in text
    monkeypatch.setenv("APP_PASSWORD", "four random words here")
    code, text = cli("serve", "--host", "0.0.0.0")
    assert started["host"] == "0.0.0.0" and "short" not in text


def test_serve_without_the_dashboard(cli, monkeypatch):
    monkeypatch.setattr(run.importlib.util, "find_spec", lambda name: None)
    code, text = cli("serve")
    assert code == 1 and "isn't installed" in text


def test_unexpected_errors_are_short_unless_debugging(cli, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("gremlins")
    monkeypatch.setattr(pipeline, "run_step", broken)
    code, text = cli("score")
    assert code == 1 and "Something went wrong (RuntimeError: gremlins)" in text and "--debug" in text
    with pytest.raises(RuntimeError):
        cli("score", "--debug")


def test_real_subprocess(tmp_root, sample_csv):
    env = {**os.environ, "BLUEMOON_HOME": str(tmp_root)}
    env.pop("GOOGLE_MAPS_API_KEY", None)
    done = subprocess.run([sys.executable, str(REPO / "run.py"), "import", str(sample_csv)], env=env, cwd=REPO,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Imported 1 file" in done.stdout
    done = subprocess.run([sys.executable, str(REPO / "run.py"), "status"], env=env, cwd=REPO,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0 and "11 properties" in done.stdout
    assert (tmp_root / "data" / "bluemoon.sqlite").is_file()


def test_share_password_and_people_commands(cli, settings, monkeypatch):
    """`share password` makes the team password once, then shows it; add/remove need no restart."""
    from bluemoon import share
    monkeypatch.setenv("APP_PASSWORD", "")      # blank = not set; restored after the test
    env = share.env_file()
    env.write_text("GOOGLE_MAPS_API_KEY=\nAPP_PASSWORD=\n")
    code, text = cli("share", "password")
    assert code == 0 and "Team password: bluemoon-" in text and "kickstart" in text
    made = text.split("Team password: ", 1)[1].split()[0]
    assert f"APP_PASSWORD={made}" in env.read_text()
    code, text = cli("share", "password")                       # shows the same one again
    assert code == 0 and made in text and "--new" in text
    code, text = cli("share", "password", "--new")
    assert code == 0 and made not in text and "sign in again" in text
    code, text = cli("share", "add", "pat@example.com")
    assert code == 0 and "right away" in text and "kickstart" not in text
    code, text = cli("share", "list")
    assert code == 0 and "pat@example.com  (hasn't signed in yet)" in text and "Team password: set" in text
    code, text = cli("share", "remove", "pat@example.com")
    assert code == 0 and "signed out on their next click" in text


def test_key_command_saves_hidden_and_never_prints(cli, settings, monkeypatch):
    import getpass
    from bluemoon import share
    fake = "AIza" + "x" * 35
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")                    # restored after the test
    share.env_file().write_text("GOOGLE_MAPS_API_KEY=\nAPP_PASSWORD=\n")
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": fake)
    code, text = cli("key", "google")
    assert code == 0 and "Saved GOOGLE_MAPS_API_KEY" in text and fake not in text
    env = share.env_file().read_text()
    assert f"GOOGLE_MAPS_API_KEY={fake}\n" in env and env.count("GOOGLE_MAPS_API_KEY=") == 1
    assert share.env_file().stat().st_mode & 0o077 == 0
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "not a key, two words")
    code, text = cli("key", "google")
    assert code != 0 and "Nothing saved" in text
    assert f"GOOGLE_MAPS_API_KEY={fake}\n" in share.env_file().read_text()     # the good key is kept
