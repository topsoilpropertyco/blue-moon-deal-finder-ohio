"""Pipeline steps (bluemoon/pipeline.py) and background jobs (bluemoon/jobs.py).

No network: real HTTP is refused, HUD is faked with httpx.MockTransport, and there is no Google key
unless a test sets a fake one (and then checks that nothing was sent). Owner names are invented.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest

from bluemoon import jobs, pipeline
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.enrich import rent
from bluemoon.ingest import import_file

FAKE_KEY = "TESTKEY-pipeline-0123456789"


# --- fixtures --------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP request fails the test (httpx.MockTransport still works)."""
    def refuse(self, request):
        raise AssertionError(f"Tried to reach the network: {request.url.host}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture
def fake_hud(monkeypatch, settings):
    """HUD answers every rent question with 'no data here' (fast, no retries)."""
    settings.raw.setdefault("rent", {}).update(retry_backoff_seconds=0, max_retries=0)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"features": []})

    @contextlib.contextmanager
    def scope(client, cfg):
        yield httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(rent, "_client_scope", scope)
    return seen


@pytest.fixture
def google_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", FAKE_KEY)
    return FAKE_KEY


@pytest.fixture
def small_ok(settings):
    """Ask for an OK above 2 properties, so the 11-property fixture triggers it."""
    settings.raw.setdefault("safety", {})["confirm_over_properties"] = 2
    return 2


@pytest.fixture
def loaded(conn, market_slug, sample_csv):
    """The sample export imported (without the after-import steps)."""
    import_file(conn, market_slug, sample_csv)
    return conn


@pytest.fixture
def ranked(conn, market_slug, sample_csv):
    res = pipeline.run_step(conn, market_slug, "import", path=sample_csv)
    assert res["ok"], res["message"]
    return conn


def count(conn, table: str) -> int:
    return int(scalar(conn, f"SELECT COUNT(*) FROM {table}") or 0)


# --- import and after_import -------------------------------------------------------------------------

def test_import_ranks_the_list_right_away(conn, market_slug, sample_csv):
    res = pipeline.run_step(conn, market_slug, "import", path=sample_csv)
    assert res["ok"] and res["step"] == "import"
    assert res["counts"]["rows_new"] == 11 and res["counts"]["files"] == 1
    steps = [s["step"] for s in res["after_import"]["steps"]]
    assert steps == ["owners", "drive", "score", "drill"]
    assert count(conn, "owners") == count(conn, "scores") == count(conn, "drive_times") == 11
    assert scalar(conn, "SELECT COUNT(*) FROM scores WHERE rank IS NOT NULL") > 0
    assert {r["method"] for r in rows(conn, "SELECT method FROM drive_times")} == {"estimate"}
    assert "fit the buy box" in res["message"] and "nothing deleted" in res["message"]
    assert count(conn, "api_calls") == 0


def test_import_bad_file_is_a_plain_error(conn, market_slug, tmp_path):
    bad = tmp_path / "notes.csv"
    bad.write_text("hello,world\n1,2\n")
    res = pipeline.run_step(conn, market_slug, "import", path=bad)
    assert not res["ok"]
    assert "notes.csv" in res["message"]
    assert res["after_import"] is None
    assert count(conn, "properties") == 0


def test_import_without_a_file(conn, market_slug):
    res = pipeline.run_step(conn, market_slug, "import")
    assert not res["ok"] and "Which file" in res["message"]


def test_import_dry_run_previews_without_saving(conn, market_slug, sample_csv):
    res = pipeline.run_step(conn, market_slug, "import", dry_run=True, path=[sample_csv])
    assert res["ok"] and res["dry_run"]
    assert res["previews"][0]["rows"] == 12
    assert "nothing saved" in res["message"]
    assert count(conn, "properties") == 0


def test_after_import_never_replaces_a_google_drive_time(loaded, market_slug):
    pid = scalar(loaded, "SELECT MIN(id) FROM properties")
    loaded.execute("INSERT INTO drive_times(property_id, minutes, meters, method, origin_key, fetched_at) "
                   "VALUES(?, 12.5, 9000, 'google_routes', 'x', ?)", (pid, now_iso()))
    loaded.commit()
    res = pipeline.after_import(loaded, market_slug)
    assert res["ok"]
    kept = one(loaded, "SELECT minutes, method FROM drive_times WHERE property_id = ?", (pid,))
    assert kept == {"minutes": 12.5, "method": "google_routes"}
    assert count(loaded, "drive_times") == 11


def test_after_import_refreshes_estimates_from_an_old_origin(loaded, market_slug):
    pipeline.after_import(loaded, market_slug)
    loaded.execute("UPDATE drive_times SET origin_key = 'old', minutes = 999")
    loaded.commit()
    pipeline.after_import(loaded, market_slug)
    assert scalar(loaded, "SELECT MAX(minutes) FROM drive_times") < 999


# --- single steps ------------------------------------------------------------------------------------

def test_owners_score_and_drill_steps(loaded, market_slug):
    owners = pipeline.run_step(loaded, market_slug, "owners")
    assert owners["ok"] and owners["counts"]["properties"] == 11
    score = pipeline.run_step(loaded, market_slug, "score")
    assert score["ok"] and score["counts"]["in_buybox"] + score["counts"]["excluded"] == 11
    drill = pipeline.run_step(loaded, market_slug, "drill")
    assert drill["ok"]
    assert sum(drill["counts"][f"levels_{n}"] for n in (1, 2, 3)) == 11
    assert "details" in drill and drill["warnings"] == []


def test_drive_without_a_key_saves_free_estimates(loaded, market_slug):
    res = pipeline.run_step(loaded, market_slug, "drive", limit=5)
    assert res["ok"] and not res["needs_confirm"]
    assert res["counts"]["estimated"] == 5 and res["counts"]["cost_usd"] == 0
    assert count(loaded, "api_calls") == 0


def test_drive_with_a_key_waits_for_ok_on_big_runs(loaded, market_slug, google_key, small_ok):
    res = pipeline.run_step(loaded, market_slug, "drive")
    assert res["ok"] and res["needs_confirm"]
    assert res["estimate"]["to_fetch"] > 2
    assert "Waiting for your OK" in res["message"]
    assert count(loaded, "drive_times") == 0 and count(loaded, "api_calls") == 0


def test_drive_asks_before_sending_many_addresses_to_google(loaded, market_slug, google_key, small_ok):
    loaded.execute("UPDATE properties SET lat = NULL, lng = NULL")
    loaded.commit()
    res = pipeline.run_step(loaded, market_slug, "drive")
    assert res["needs_confirm"]
    assert "map coordinates" in res["message"]
    assert count(loaded, "api_calls") == 0


def test_drive_dry_run_prices_without_calling(loaded, market_slug, google_key):
    res = pipeline.run_step(loaded, market_slug, "drive", dry_run=True)
    assert res["dry_run"] and not res["needs_confirm"]
    assert res["estimate"]["provider"] == "google_routes"
    assert res["counts"]["cost_usd"] == 0.0
    assert count(loaded, "drive_times") == 0


def test_rent_step_with_fake_hud(loaded, market_slug, fake_hud):
    res = pipeline.run_step(loaded, market_slug, "rent")
    assert res["ok"] and res["counts"]["properties"] == 11
    assert fake_hud, "HUD was asked"
    assert count(loaded, "rent_fits") == 11


def test_photos_without_a_key_explain_what_to_do(ranked, market_slug):
    res = pipeline.run_step(ranked, market_slug, "photos")
    assert not res["ok"]
    assert "GOOGLE_MAPS_API_KEY" in res["message"]


def test_photos_wait_for_ok_and_alias_works(ranked, market_slug, google_key, small_ok):
    res = pipeline.run_step(ranked, market_slug, "visual_fetch")
    assert res["step"] == "photos" and res["needs_confirm"]
    assert res["estimate"]["to_fetch"] > 2
    assert count(ranked, "api_calls") == 0


def test_classify_with_nothing_to_check(ranked, market_slug, settings):
    settings.raw.setdefault("vision", {})["backend"] = "mock"
    res = pipeline.run_step(ranked, market_slug, "classify")
    assert res["ok"] and res["counts"]["calls"] == 0


def test_free_dry_runs_change_nothing(loaded, market_slug):
    for step in ("owners", "score", "drill"):
        res = pipeline.run_step(loaded, market_slug, step, dry_run=True)
        assert res["ok"] and res["dry_run"] and "Free" in res["message"]
    assert count(loaded, "owners") == count(loaded, "scores") == 0


def test_unknown_step_and_market(conn, market_slug):
    with pytest.raises(ValueError, match="Unknown step"):
        pipeline.run_step(conn, market_slug, "teleport")
    res = pipeline.run_step(conn, "nowhere", "score")
    assert not res["ok"] and "Unknown market 'nowhere'" in res["message"]
    res = pipeline.run_step(conn, market_slug, "score", limit=0)
    assert not res["ok"] and "1 or more" in res["message"]


def test_empty_market_steps_are_calm(conn):
    for step in ("owners", "drive", "score", "drill", "photos"):
        res = pipeline.run_step(conn, "self-storage-springfield-oh", step)
        assert res["message"], step


# --- all ------------------------------------------------------------------------------------------------

def test_all_runs_every_step_in_order(loaded, market_slug, fake_hud):
    seen: list[float] = []
    res = pipeline.run_all(loaded, market_slug, progress=lambda f, m: seen.append(f))
    assert [s["step"] for s in res["steps"]] == [k for k, _ in pipeline.ALL_PLAN]
    assert res["ok"] and not res["needs_confirm"]
    assert any("Photos" in w and "GOOGLE_MAPS_API_KEY" in w for w in res["warnings"])
    assert res["message"].startswith("All steps done.")
    assert seen and min(seen) >= 0.0 and max(seen) <= 1.0
    assert count(loaded, "scores") == 11 and count(loaded, "rent_fits") == 11


def test_all_waits_on_paid_steps_but_runs_the_free_ones(loaded, market_slug, fake_hud, google_key, small_ok):
    res = pipeline.run_all(loaded, market_slug)
    by_step = {s["step"]: s for s in res["steps"]}
    assert res["needs_confirm"] and res["ok"]
    assert by_step["drive"]["needs_confirm"]
    assert by_step["photos"]["needs_confirm"] and "Not run yet" in by_step["photos"]["message"]
    assert by_step["rent"]["ok"] and by_step["drill"]["ok"]
    assert res["estimate"]["needs_confirm"] and "steps" in res["estimate"]
    assert "Waiting for your OK before: drive times, photos, photo check" in res["message"]
    assert count(loaded, "api_calls") == 0 and count(loaded, "scores") == 11


def test_all_reports_a_failing_step_and_carries_on(loaded, market_slug, fake_hud, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("HUD exploded")
    monkeypatch.setattr(pipeline.rent, "compute_rent_fits", broken)
    res = pipeline.run_all(loaded, market_slug)
    by_step = {s["step"]: s for s in res["steps"]}
    assert not res["ok"] and not by_step["rent"]["ok"]
    assert "HUD exploded" in by_step["rent"]["message"]
    assert by_step["drill"]["ok"] and "Some steps failed" in res["message"]


def test_all_counts_a_crash_in_a_paid_step_as_a_failure(loaded, market_slug, fake_hud, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("camera fell off")
    monkeypatch.setattr(pipeline, "fetch_imagery", broken)
    res = pipeline.run_all(loaded, market_slug)
    assert not res["ok"] and "camera fell off" in res["message"]


def test_all_dry_run_is_the_estimate(loaded, market_slug):
    res = pipeline.run_step(loaded, market_slug, "all", dry_run=True)
    assert res["dry_run"] and res["estimate"]["steps"]
    assert count(loaded, "owners") == 0


# --- estimate and status --------------------------------------------------------------------------------

def test_estimate_lists_every_step_with_costs(ranked, market_slug, google_key):
    est = pipeline.estimate(ranked, market_slug)
    assert [s["step"] for s in est["steps"]] == ["owners", "drive", "rent", "photos", "classify", "score", "drill"]
    assert est["total_usd"] == round(sum(s["usd"] for s in est["steps"]), 2)
    photos = next(s for s in est["steps"] if s["step"] == "photos")
    assert photos["paid"] and {s["sku"] for s in photos["skus"]} == {"street_view_static", "static_maps"}
    assert all(s["free_left"] == 10000 for s in photos["skus"])
    assert est["has_google_key"] and est["message"].startswith("About $")
    assert count(ranked, "api_calls") == 0


def test_estimate_uses_this_months_usage_for_the_free_tier(ranked, market_slug, google_key):
    ranked.execute("INSERT INTO api_calls(provider, sku, units, created_at) VALUES('google', 'street_view_static', "
                   "10000, ?)", (now_iso(),))
    ranked.commit()
    est = pipeline.estimate(ranked, market_slug)
    photos = next(s for s in est["steps"] if s["step"] == "photos")
    sv = next(s for s in photos["skus"] if s["sku"] == "street_view_static")
    assert sv["free_left"] == 0 and sv["billable"] == sv["units"] and sv["usd"] > 0
    assert est["total_usd"] >= sv["usd"]


def test_estimate_counts_claude_plan_calls(ranked, market_slug, settings):
    settings.raw.setdefault("vision", {})["backend"] = "claude_cli"
    est = pipeline.estimate(ranked, market_slug, limit=3)
    classify = next(s for s in est["steps"] if s["step"] == "classify")
    assert classify["usd"] == 0 and classify["calls_after_photos"] == 3
    assert est["claude_plan_calls"] == 3


def test_layer_and_market_status(ranked, market_slug):
    layers = {x["key"]: x for x in pipeline.layer_status(ranked, market_slug)}
    assert layers["owners"]["done"] == layers["owners"]["total"] == 11
    assert layers["drive"]["note"] == "approximate"
    assert layers["photos"]["total"] == scalar(ranked, "SELECT COUNT(*) FROM scores WHERE in_buybox = 1")
    status = pipeline.market_status(ranked, market_slug)
    assert status["properties"] == 11 and status["last_import"]["filename"] == "crexi_sample.csv"
    assert sum(status["levels"].values()) == 11
    assert set(pipeline.keys_present()) >= {"google", "anthropic", "hud", "census"}


def test_status_of_an_empty_market(conn):
    status = pipeline.market_status(conn, "land-springfield-oh")
    assert status["properties"] == 0 and status["last_import"] is None
    assert {x["key"] for x in status["layers"]} == {"owners", "drive", "score"}


# --- jobs -------------------------------------------------------------------------------------------------

class Gate:
    """A job body that waits until the test lets it go, reporting progress while it waits."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.ran: list[tuple[str, str]] = []

    def __call__(self, conn, kind, market, params, progress) -> dict:
        self.ran.append((kind, market))
        self.started.set()
        while not self.release.wait(0.01):
            progress(0.5, "Working")
        return {"ok": True, "message": f"{kind} done", "counts": {"n": 1}}


def wait_until(check, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_job_lifecycle(conn, market_slug):
    runner = jobs.JobRunner(lambda c, kind, m, p, progress: {"ok": True, "message": "All good.", "counts": {}})
    job_id = runner.start("score", market_slug, {"limit": 5, "extra": "ignored"})
    job = runner.wait(job_id, 5)
    assert job["status"] == "done" and job["done"] and job["progress"] == 1.0
    assert job["message"] == "All good." and job["result"]["ok"]
    assert job["params"] == {"limit": 5, "dry_run": False, "confirm": False}
    assert job["market"] == market_slug and job["label"] == "Scores"
    listed = runner.list(market_slug)
    assert listed[0]["id"] == job_id and "result" not in listed[0]
    assert runner.list(market_slug, with_results=True)[0]["result"]["ok"]
    assert runner.get(999999) is None


def test_jobs_queue_per_market_and_run_side_by_side(conn, market_slug):
    gate = Gate()
    runner = jobs.JobRunner(gate)
    first = runner.start("owners", market_slug)
    second = runner.start("score", market_slug)
    other = runner.start("score", "land-springfield-oh")
    wait_until(lambda: len(gate.ran) == 2)
    assert runner.get(first)["status"] == "running"
    assert runner.get(second)["status"] == "queued"
    assert "Waiting" in runner.get(second)["message"]
    assert runner.get(other)["status"] == "running"
    gate.release.set()
    for job_id in (first, second, other):
        assert runner.wait(job_id, 5)["status"] == "done"
    assert gate.ran[-1] == ("score", market_slug)


def test_cancel_a_running_job(conn, market_slug):
    gate = Gate()
    runner = jobs.JobRunner(gate)
    job_id = runner.start("all", market_slug)
    assert gate.started.wait(5)
    runner.cancel(job_id)
    job = runner.wait(job_id, 5)
    assert job["status"] == "cancelled" and "kept" in job["message"]


def test_cancel_a_queued_job_drops_it(conn, market_slug):
    gate = Gate()
    runner = jobs.JobRunner(gate)
    first = runner.start("owners", market_slug)
    queued = runner.start("score", market_slug)
    assert gate.started.wait(5)
    assert runner.cancel(queued)["status"] == "cancelled"
    gate.release.set()
    assert runner.wait(first, 5)["status"] == "done"
    assert gate.ran == [("owners", market_slug)]


def test_failing_jobs_are_errors_with_plain_messages(conn, market_slug):
    def boom(*a):
        raise RuntimeError("disk on fire")
    runner = jobs.JobRunner(boom)
    job = runner.wait(runner.start("score", market_slug), 5)
    assert job["status"] == "error" and "disk on fire" in job["message"]
    runner = jobs.JobRunner(lambda *a: {"ok": False, "message": "No Google key."})
    job = runner.wait(runner.start("photos", market_slug), 5)
    assert job["status"] == "error" and job["message"] == "No Google key."


def test_start_job_checks_its_input(conn, market_slug):
    runner = jobs.JobRunner(lambda *a: {"ok": True, "message": ""})
    with pytest.raises(ValueError, match="Unknown step"):
        runner.start("teleport", market_slug)
    with pytest.raises(ValueError, match="Unknown market"):
        runner.start("score", "nowhere")
    with pytest.raises(ValueError, match="limit"):
        runner.start("score", market_slug, {"limit": 0})


def test_interrupted_jobs_are_marked_on_startup(conn, market_slug):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    market_id = market_by_slug(conn, market_slug)["id"]
    for status, params in (("running", f'{{"_pid": {gone.pid}}}'), ("queued", "{}"),
                           ("running", f'{{"_pid": {os.getpid()}}}'), ("done", "{}")):
        conn.execute("INSERT INTO jobs(kind, market_id, status, progress, params_json, created_at) "
                     "VALUES('score', ?, ?, 0, ?, ?)", (market_id, status, params, now_iso()))
    conn.commit()
    runner = jobs.JobRunner()
    found = {j["id"]: j for j in runner.list(market_slug)}
    statuses = [found[i]["status"] for i in sorted(found)]
    assert statuses == ["error", "error", "running", "done"]
    first, second = (found[i] for i in sorted(found)[:2])
    assert first["message"] == jobs.INTERRUPTED and "Not started" in second["message"]
    assert "_pid" not in first["params"]


def test_real_pipeline_job_through_the_module_api(ranked, market_slug):
    job_id = jobs.start_job("score", market_slug)
    job = jobs.wait_for_job(job_id, 30)
    assert job["status"] == "done" and job["result"]["step"] == "score"
    assert any(j["id"] == job_id for j in jobs.list_jobs(market_slug))
    assert jobs.cancel_job(job_id)["status"] == "done"   # too late to cancel: nothing changes


def test_a_cancel_gets_through_the_backend_progress_guards(ranked, market_slug, fake_hud):
    """The backend steps swallow ordinary errors from progress callbacks; a cancel must still stop them."""
    def cancelled(fraction, message):
        raise jobs.Cancelled()
    for step in ("score", "drill", "drive", "rent", "all"):
        with pytest.raises(jobs.Cancelled):
            pipeline.run_step(ranked, market_slug, step, progress=cancelled)


def test_cancel_stops_a_real_pipeline_job(ranked, market_slug, fake_hud):
    go = threading.Event()

    def body(conn, kind, market, params, progress):
        go.wait(5)   # the test cancels while the job waits here
        return jobs.run_pipeline_step(conn, kind, market, params, progress)

    runner = jobs.JobRunner(body)
    job_id = runner.start("all", market_slug)
    wait_until(lambda: runner.get(job_id)["status"] == "running")
    assert "Stopping" in runner.cancel(job_id)["message"]
    go.set()
    job = runner.wait(job_id, 30)
    assert job["status"] == "cancelled" and job["result"] is None
