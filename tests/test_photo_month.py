"""The monthly photo goal (bluemoon/imagery/monthly.py), its job steps and its API.

Google is a fake httpx transport and the AI check is the offline mock backend: nothing is called or paid for.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import httpx
import pytest
import yaml

from bluemoon import pipeline
from bluemoon.config import reload_settings
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.enrich import geo
from bluemoon.imagery import monthly, streetview
from bluemoon.imagery.vision import MockBackend

MARKET = "apartments-springfield-oh"
KEY = "TESTKEY-monthly-0123456789"
JPEG = b"\xff\xd8\xff\xe0" + b"\x10JFIF" + bytes(range(64))
_ids = count(1)


# --- helpers ---------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(geo, "_sleep", lambda s: None)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", KEY)
    return KEY


def set_config(root: Path, dotted: str, value) -> None:
    path = root / "config.yaml"
    data = yaml.safe_load(path.read_text())
    node = data
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    reload_settings()


class FakeGoogle:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(request.url.params)
        path = request.url.path
        if path.endswith("/streetview/metadata"):
            lat, lng = (float(x) for x in params["location"].split(","))
            return httpx.Response(200, json={"status": "OK", "pano_id": f"pano-{lat:.5f}",
                                             "location": {"lat": lat - 0.0003, "lng": lng}, "date": "2023-06"})
        return httpx.Response(200, content=JPEG + params.get("heading", "0").encode() + str(len(self.requests)).encode(),
                              headers={"content-type": "image/jpeg"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def count(self, suffix: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(suffix))


def add(conn, *, in_buybox: int | None = 1, rank: int | None = None, score: float | None = None,
        status: str | None = None, lat: float | None = 39.95, **visual) -> int:
    m = market_by_slug(conn, MARKET)
    n = next(_ids)
    cur = conn.execute(
        "INSERT INTO properties(market_id, property_key, address, city, state, lat, lng, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (m["id"], f"mo:{n}", f"{n} MONTH ST", "SPRINGFIELD", "OH", lat, -83.77 + n * 0.001, now_iso(), now_iso()))
    pid = cur.lastrowid
    if in_buybox is not None:
        conn.execute("INSERT INTO scores(property_id, in_buybox, rank, score, computed_at) VALUES(?,?,?,?,?)",
                     (pid, in_buybox, rank, score, now_iso()))
    if status is not None:
        cols = {"property_id": pid, "status": status, **visual}
        conn.execute(f"INSERT INTO visual_checks({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                     tuple(cols.values()))
    conn.commit()
    return pid


def photos_on_disk(settings, pid: int) -> None:
    folder = settings.path("images") / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(("sv_0.jpg", "sv_1.jpg", "sv_2.jpg", "sat.jpg")):
        (folder / name).write_bytes(JPEG + bytes([pid % 256, i]))


def pull_at(conn, pid: int, when: datetime) -> None:
    conn.execute("INSERT INTO photo_pulls(property_id, pulled_at) VALUES(?, ?)",
                 (pid, when.replace(microsecond=0).isoformat()))
    conn.commit()


def visual(conn, pid: int) -> dict:
    return one(conn, "SELECT * FROM visual_checks WHERE property_id=?", (pid,)) or {}


# --- settings --------------------------------------------------------------------------------

def test_defaults_are_600_and_100(settings):
    cfg = monthly.photos_config(settings)
    assert (cfg.monthly_target, cfg.batch) == (600, 100)


def test_config_local_yaml_changes_the_goal(tmp_root, settings):
    (tmp_root / "config.local.yaml").write_text("photos:\n  monthly_target: 800\n  batch: 150\n", encoding="utf-8")
    cfg = monthly.photos_config(reload_settings())
    assert (cfg.monthly_target, cfg.batch) == (800, 150)


def test_bad_number_is_a_plain_message(tmp_root, settings):
    set_config(tmp_root, "photos.monthly_target", 0)
    with pytest.raises(ValueError, match="photos setting 'monthly_target'"):
        monthly.photos_config()


# --- the pool and the count ------------------------------------------------------------------

def test_photo_pool_is_buy_box_first_by_rank_then_the_rest(conn, settings):
    rest_top = add(conn, in_buybox=0, rank=1)
    box_low = add(conn, rank=9)
    box_top = add(conn, rank=2)
    box_unranked = add(conn, rank=None)
    rest_low = add(conn, in_buybox=0, rank=7)
    add(conn, rank=3, status="done")                    # already has photos
    add(conn, rank=4, status="no_imagery")              # Google has none: nothing to pull
    add(conn, rank=5, status="pending", reviewed_label="DIRECT_ENTRY")   # you already answered it
    add(conn, rank=6, lat=None)                         # no map location
    pool = monthly.photo_pool(conn, MARKET)
    assert [r["id"] for r in pool] == [box_top, box_low, box_unranked, rest_top, rest_low]
    assert [r["group"] for r in pool] == ["buybox"] * 3 + ["rest"] * 2
    assert [r["id"] for r in monthly.photo_pool(conn, MARKET, limit=2)] == [box_top, box_low]


def test_month_count_uses_this_calendar_month_only(conn, settings):
    now = datetime.now(timezone.utc)
    a, b, c, d = (add(conn, status="done") for _ in range(4))
    pull_at(conn, a, now)
    pull_at(conn, a, now)                               # pulled twice: one property
    pull_at(conn, b, now - timedelta(days=62))          # an old month
    pull_at(conn, c, now)
    monthly.backfill(conn, MARKET, settings)            # d has photos but no file and no pull: nothing to add
    assert monthly.done_this_month(conn, MARKET) == 2
    st = monthly.status(conn, MARKET, settings)
    assert (st["done"], st["target"], st["remaining"], st["reached"]) == (2, 600, 598, False)
    assert "2 of 600 done" in st["message"]
    assert d


def test_backfill_counts_existing_photos_once_by_file_date(conn, settings):
    pid = add(conn, status="done")
    photos_on_disk(settings, pid)
    assert monthly.status(conn, MARKET, settings)["done"] == 1      # photos from before the table existed
    assert monthly.status(conn, MARKET, settings)["done"] == 1      # and only counted once
    assert scalar(conn, "SELECT COUNT(*) FROM photo_pulls WHERE property_id = ?", (pid,)) == 1


def test_status_reports_the_pool_and_waiting_ai(conn, settings):
    add(conn, rank=1)
    add(conn, rank=2, in_buybox=0)
    add(conn, status="done")
    st = monthly.status(conn, MARKET, settings)
    assert st["pool"] == {"buybox": 1, "rest": 1, "total": 2} and st["ai_waiting"] == 1
    assert st["batch"] == 100 and st["keep_going_to"] == 600


def test_no_goal_for_raw_land(conn, settings):
    assert monthly.status(conn, "land-springfield-oh", settings)["enabled"] is False


# --- estimate and the OK ---------------------------------------------------------------------

def test_estimate_asks_for_an_ok_over_50_and_shows_the_cost(tmp_root, conn, settings, key):
    for i in range(60):
        add(conn, rank=i + 1)
    est = monthly.estimate_batch(conn, MARKET, 60, MockBackend())
    assert est["photos"] == 60 and est["ai_checks"] == 60 and est["needs_confirm"] is True
    assert est["cost"]["usd"] >= 0 and "$" in est["message"] and "Next 60" in est["message"]
    small = monthly.estimate_batch(conn, MARKET, 5, MockBackend())
    assert small["needs_confirm"] is False and small["photos"] == 5


def test_big_batch_waits_for_the_ok_and_calls_nothing(tmp_root, conn, settings, key):
    for i in range(60):
        add(conn, rank=i + 1)
    google = FakeGoogle()
    res = monthly.run_batch(conn, MARKET, 60, backend=MockBackend(), client=google.client())
    assert res["needs_confirm"] is True and res["message"].startswith("Waiting for your OK")
    assert google.requests == [] and rows(conn, "SELECT * FROM photo_pulls") == []


def test_dry_run_calls_nothing(conn, settings, key):
    add(conn, rank=1)
    google = FakeGoogle()
    res = monthly.run_batch(conn, MARKET, 5, dry_run=True, backend=MockBackend(), client=google.client())
    assert res["dry_run"] is True and google.requests == []


# --- running ---------------------------------------------------------------------------------

def test_batch_pulls_photos_then_the_ai_looks_at_them(conn, settings, key):
    top = add(conn, rank=1, score=0.2)
    second = add(conn, rank=2, score=0.1)
    outside = add(conn, in_buybox=0, rank=1)
    google = FakeGoogle()
    res = monthly.run_batch(conn, MARKET, 2, backend=MockBackend(), client=google.client())
    assert res["fetched"] == 2 and res["checked"] == 2 and res["done_this_month"] == 2
    assert visual(conn, top)["status"] == "done" and visual(conn, second)["ai_label"]
    assert visual(conn, outside) == {}                       # the buy box goes first, the rest waits
    assert "2 of 600 done" in res["message"]
    assert scalar(conn, "SELECT COUNT(*) FROM photo_pulls") == 2
    detail = visual(conn, top)
    assert detail["ai_style"] and detail["ai_stories"] and detail["ai_buildings"] and detail["ai_condition"]


def test_when_the_buy_box_is_done_the_rest_get_photos(conn, settings, key):
    box = add(conn, rank=1)
    outside = add(conn, in_buybox=0, rank=1)
    google = FakeGoogle()
    res = monthly.run_batch(conn, MARKET, 5, backend=MockBackend(), client=google.client())
    assert res["fetched"] == 2 and visual(conn, box)["status"] == "done" and visual(conn, outside)["status"] == "done"


def test_the_ai_checks_the_new_photos_first(conn, settings, key):
    old = add(conn, rank=1, status="done", pano_id="p-old", pano_lat=39.95, pano_lng=-83.77)   # pulled before
    photos_on_disk(settings, old)
    new = add(conn, rank=50)
    google = FakeGoogle()
    res = monthly.run_batch(conn, MARKET, 1, backend=MockBackend(), client=google.client())
    assert res["fetched"] == 1 and res["checked"] == 1
    assert visual(conn, new)["ai_label"] and not visual(conn, old).get("ai_label")


def test_ai_only_when_there_is_no_google_key(conn, settings, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    have = add(conn, rank=1, status="done", pano_id="p-have", pano_lat=39.95, pano_lng=-83.77)
    photos_on_disk(settings, have)
    add(conn, rank=2)
    res = monthly.run_batch(conn, MARKET, 5, backend=MockBackend())
    assert res["fetched"] == 0 and res["checked"] == 1 and visual(conn, have)["ai_label"]
    assert any("Google key" in w for w in res["warnings"])


def test_keep_going_runs_batches_until_the_goal(tmp_root, conn, settings, key):
    set_config(tmp_root, "photos.monthly_target", 5)
    set_config(tmp_root, "photos.batch", 2)
    for i in range(8):
        add(conn, rank=i + 1)
    google = FakeGoogle()
    res = monthly.run_keep_going(conn, MARKET, backend=MockBackend(), client=google.client())
    assert res["fetched"] == 5 and res["checked"] == 5 and res["batches"] == 3 and res["done_this_month"] == 5
    assert monthly.status(conn, MARKET)["reached"] is True


def test_keep_going_after_the_goal_does_one_more_batch(tmp_root, conn, settings, key):
    set_config(tmp_root, "photos.monthly_target", 1)
    set_config(tmp_root, "photos.batch", 2)
    seen = add(conn, status="done")
    pull_at(conn, seen, datetime.now(timezone.utc))
    for i in range(5):
        add(conn, rank=i + 1)
    est = monthly.run_keep_going(conn, MARKET, dry_run=True, backend=MockBackend())
    assert est["properties"] == 2 and est["goal"]["reached"] is True
    res = monthly.run_keep_going(conn, MARKET, backend=MockBackend(), client=FakeGoogle().client())
    assert res["fetched"] == 2 and res["batches"] == 1


def test_keep_going_stops_at_the_google_cap(tmp_root, conn, settings, key):
    set_config(tmp_root, "photos.monthly_target", 6)
    set_config(tmp_root, "photos.batch", 2)
    set_config(tmp_root, "google.max_street_view_images_per_run", 9)     # 3 photos a property: 3 properties
    for i in range(8):
        add(conn, rank=i + 1)
    res = monthly.run_keep_going(conn, MARKET, backend=MockBackend(), client=FakeGoogle().client())
    assert res["capped"] is True and res["fetched"] == 2 and res["batches"] == 1
    assert any("per-run limit" in w for w in res["warnings"])


def test_keep_going_stops_when_nothing_is_left(tmp_root, conn, settings, key):
    set_config(tmp_root, "photos.monthly_target", 10)
    set_config(tmp_root, "photos.batch", 2)
    add(conn, rank=1)
    res = monthly.run_keep_going(conn, MARKET, backend=MockBackend(), client=FakeGoogle().client())
    assert res["fetched"] == 1 and res["batches"] <= 2


# --- the pipeline steps and jobs -------------------------------------------------------------

def test_new_steps_are_registered_deliberately():
    assert pipeline.STEPS == ["import", "owners", "drive", "rent", "parcels", "photos", "classify", "score", "drill",
                              "research", "all"]            # the "all" plan did not change
    for step in ("photo_batch", "photo_keep_going"):
        assert step in pipeline.EXTRA_STEPS and step in pipeline.PAID_STEPS and step in pipeline.RESCORE_AFTER
        assert pipeline.step_label(step).startswith("Photos and AI check")
        assert pipeline.normalize_step(step) == step
    assert pipeline.EXTRA_STEPS[:4] == ["calibrate", "export", "estimate", "after_import"]


def test_step_dry_run_through_the_pipeline(conn, settings, key):
    add(conn, rank=1)
    res = pipeline.run_step(conn, MARKET, "photo_batch", limit=5, dry_run=True)
    assert res["ok"] and res["dry_run"] and res["step"] == "photo_batch" and "Next 5" in res["message"]
    res = pipeline.run_step(conn, MARKET, "photo_keep_going", dry_run=True)
    assert res["ok"] and res["dry_run"] and "Next 600" in res["message"]


def test_a_big_job_waits_for_the_ok_then_runs_with_it(tmp_root, conn, settings, key, monkeypatch):
    for i in range(60):
        add(conn, rank=i + 1)
    google = FakeGoogle()
    real = streetview.fetch_imagery
    monkeypatch.setattr(streetview, "fetch_imagery", lambda *a, **k: real(*a, **{**k, "client": google.client()}))
    set_config(tmp_root, "vision.backend", "mock")
    first = pipeline.run_step(conn, MARKET, "photo_batch", limit=60)
    assert first["needs_confirm"] is True and google.requests == []
    assert first["estimate"]["cost"]["usd"] >= 0
    second = pipeline.run_step(conn, MARKET, "photo_batch", limit=60, confirm=True)
    assert second["ok"] and second["counts"]["fetched"] == 60 and second["counts"]["checked"] == 60
    assert "Buy box, scores and rank updated" in second["message"]


# --- the web API -----------------------------------------------------------------------------

@pytest.fixture
def client(tmp_root, settings):
    from fastapi.testclient import TestClient

    from bluemoon.db import connect
    from bluemoon.web.app import create_app
    connect(settings=settings).close()
    with TestClient(create_app()) as c:
        yield c


def test_api_month_status_and_estimate(client, conn, settings, key):
    add(conn, rank=1)
    res = client.get(f"/api/m/{MARKET}/photos/month")
    body = res.json()
    assert res.status_code == 200 and body["enabled"] and body["done"] == 0 and body["target"] == 600
    assert "estimate" not in body
    body = client.get(f"/api/m/{MARKET}/photos/month?estimate=1").json()
    assert set(body["estimate"]) == {"test", "batch", "keep_going"}
    assert body["estimate"]["test"]["photos"] == 1 and "usd" in body["estimate"]["batch"]
    assert client.get("/api/m/nope/photos/month").status_code == 404


def test_api_starts_the_new_steps_as_jobs(client, conn, settings, key):
    add(conn, rank=1)
    res = client.post(f"/api/m/{MARKET}/jobs", json={"step": "photo_batch", "limit": 5, "dry_run": True})
    assert res.status_code == 200 and res.json()["job_id"]
    assert client.post(f"/api/m/{MARKET}/jobs", json={"step": "photo_keep_going", "dry_run": True}).status_code == 200


def test_job_kinds_are_the_step_names(settings):
    assert pipeline.normalize_step("photo-batch") == "photo_batch"
