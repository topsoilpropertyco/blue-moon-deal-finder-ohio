"""The one photo button (bluemoon/imagery/photos_all.py): photos and the AI's call for every lead, its job step,
its CLI command and its API, and Photo review after a full run.

Google is a fake httpx transport and the AI check is the offline mock backend: nothing is called or paid for.
"""
from __future__ import annotations

from itertools import count
from pathlib import Path

import httpx
import pytest
import yaml

from bluemoon import pipeline
from bluemoon.config import reload_settings
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.enrich import geo
from bluemoon.imagery import photos_all, streetview
from bluemoon.imagery.vision import MockBackend, VisionFatalError

MARKET = "apartments-springfield-oh"
KEY = "TESTKEY-every-lead-0123456789"
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
        if request.url.path.endswith("/streetview/metadata"):
            lat, lng = (float(x) for x in params["location"].split(","))
            return httpx.Response(200, json={"status": "OK", "pano_id": f"pano-{lat:.5f}-{lng:.5f}",
                                             "location": {"lat": lat - 0.0003, "lng": lng}, "date": "2023-06"})
        return httpx.Response(200, content=JPEG + params.get("heading", "0").encode() + str(len(self.requests)).encode(),
                              headers={"content-type": "image/jpeg"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def images(self) -> int:
        return sum(1 for r in self.requests if not r.url.path.endswith("/metadata"))


def add(conn, *, in_buybox: int | None = 1, rank: int | None = None, score: float | None = None,
        status: str | None = None, lat: float | None = 39.95, **visual) -> int:
    m = market_by_slug(conn, MARKET)
    n = next(_ids)
    cur = conn.execute(
        "INSERT INTO properties(market_id, property_key, address, city, state, lat, lng, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (m["id"], f"pa:{n}", f"{n} LEAD ST", "SPRINGFIELD", "OH", lat, -83.77 + n * 0.001, now_iso(), now_iso()))
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


def visual(conn, pid: int) -> dict:
    return one(conn, "SELECT * FROM visual_checks WHERE property_id=?", (pid,)) or {}


# --- status -----------------------------------------------------------------------------------

def test_status_counts_every_lead(conn, settings):
    add(conn, rank=1)                                              # needs photos, in the buy box
    add(conn, in_buybox=0, rank=2)                                 # needs photos, set aside
    add(conn, rank=3, status="done", pano_id="p", pano_lat=39.9, pano_lng=-83.7)   # photos, no AI call yet
    add(conn, rank=4, status="done", ai_label="DIRECT_ENTRY", checked_at=now_iso())   # done
    add(conn, rank=5, status="pending", reviewed_label="INTERIOR_CORRIDOR")         # you answered it: done
    add(conn, rank=6, lat=None)                                    # no map location
    st = photos_all.status(conn, MARKET, settings)
    assert (st["total"], st["done"], st["left"]) == (6, 2, 4)
    assert st["photos_left"] == {"buybox": 1, "rest": 1, "total": 2}
    assert st["ai_left"] == 1 and st["no_location"] == 1
    assert st["message"] == "2 of 6 done."
    assert st["button"] == "Get photos and the AI's walk-up call for every lead"


def test_no_photo_check_for_raw_land(conn, settings):
    assert photos_all.status(conn, "land-springfield-oh", settings)["enabled"] is False
    est = photos_all.estimate(conn, "land-springfield-oh", backend=MockBackend())
    assert est["enabled"] is False and est["needs_confirm"] is False


# --- estimate ---------------------------------------------------------------------------------

def test_estimate_covers_every_lead_and_asks_for_one_ok_over_50(conn, settings, key):
    for i in range(40):
        add(conn, rank=i + 1)
    for i in range(30):
        add(conn, in_buybox=0, rank=i + 1)                         # set-aside leads get photos too
    est = photos_all.estimate(conn, MARKET, backend=MockBackend())
    assert est["dry_run"] and est["photos"] == 70 and est["ai_checks"] == 70 and est["calls"] == 70
    assert est["needs_confirm"] is True and est["cost"]["usd"] == 0.0 and est["free"] is True
    text = " ".join(est["lines"])
    assert "Photos for 70 properties" in text and "buy box first" in text
    assert "inside Google's free monthly allowance" in text and "walk-up call on 70 properties" in text
    assert "never paid for twice" in text and not est["capped"]
    small = photos_all.estimate(conn, MARKET, 5, MockBackend())
    assert small["photos"] == 5 and small["ai_checks"] == 5 and small["needs_confirm"] is False


def test_estimate_says_when_the_google_cap_stops_the_run(tmp_root, conn, settings, key):
    set_config(tmp_root, "google.max_street_view_images_per_run", 9)        # 3 photos a property: 3 properties
    for i in range(8):
        add(conn, rank=i + 1)
    est = photos_all.estimate(conn, MARKET, backend=MockBackend())
    assert est["google_capped"] is True and est["photos"] == 3 and est["photos_needed"] == 8
    assert any("per-run Google limit" in line and "after about 3 properties" in line and "5 more need photos" in line
               and "Press the button again" in line for line in est["lines"])


def test_estimate_says_when_the_ai_cap_stops_the_run(tmp_root, conn, settings, key):
    set_config(tmp_root, "vision.max_calls_per_run", 4)
    for i in range(6):
        add(conn, rank=i + 1)
    est = photos_all.estimate(conn, MARKET, backend=MockBackend())
    assert est["ai_capped"] is True and est["calls"] == 4
    assert any("per-run AI limit (4 calls)" in line for line in est["lines"])


def test_estimate_without_a_google_key_only_offers_the_ai(conn, settings, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    have = add(conn, rank=1, status="done", pano_id="p", pano_lat=39.95, pano_lng=-83.77)
    photos_on_disk(settings, have)
    add(conn, rank=2)
    est = photos_all.estimate(conn, MARKET, backend=MockBackend())
    assert est["photos"] == 0 and est["ai_checks"] == 1 and not est["has_key"]
    assert any("No Google key yet" in line for line in est["lines"])


def test_estimate_when_there_is_nothing_left(conn, settings, key):
    pid = add(conn, rank=1, status="done", ai_label="DIRECT_ENTRY", checked_at=now_iso(), pano_id="p",
              pano_lat=39.95, pano_lng=-83.77)
    photos_on_disk(settings, pid)
    est = photos_all.estimate(conn, MARKET, backend=MockBackend())
    assert est["nothing_to_do"] is True and est["needs_confirm"] is False
    assert est["lines"][0].startswith("Nothing to do")


def test_claude_plan_estimate_says_it_can_take_hours(conn, settings, key, monkeypatch):
    class Plan(MockBackend):
        name = "claude_cli"
    for i in range(30):
        add(conn, rank=i + 1)
    est = photos_all.estimate(conn, MARKET, backend=Plan())
    text = " ".join(est["lines"])
    assert "on your Claude plan (no bill" in text and "can take hours" in text and "usage limit" in text


# --- the OK and running -----------------------------------------------------------------------

def test_a_big_run_waits_for_the_ok_and_calls_nothing(conn, settings, key):
    for i in range(60):
        add(conn, rank=i + 1)
    google = FakeGoogle()
    res = photos_all.run(conn, MARKET, backend=MockBackend(), client=google.client())
    assert res["needs_confirm"] is True and res["message"].startswith("Waiting for your OK")
    assert google.requests == [] and rows(conn, "SELECT * FROM photo_pulls") == []


def test_dry_run_calls_nothing(conn, settings, key):
    add(conn, rank=1)
    google = FakeGoogle()
    res = photos_all.run(conn, MARKET, dry_run=True, backend=MockBackend(), client=google.client())
    assert res["dry_run"] is True and google.requests == []


def test_one_ok_does_every_lead_buy_box_first(conn, settings, key):
    box = [add(conn, rank=i + 1, score=0.5 - i * 0.01) for i in range(35)]
    rest = [add(conn, in_buybox=0, rank=i + 1) for i in range(25)]
    old = add(conn, rank=99, status="done", pano_id="p-old", pano_lat=39.95, pano_lng=-83.77)   # photos already
    photos_on_disk(settings, old)
    google = FakeGoogle()
    seen: list[tuple[float, str]] = []
    res = photos_all.run(conn, MARKET, confirm=True, backend=MockBackend(), client=google.client(),
                         progress=lambda f, m: seen.append((f, m)))
    assert res["fetched"] == 60 and res["checked"] == 61 and not res["error"]
    assert (res["done"], res["total"], res["left"]) == (61, 61, 0)
    assert all(visual(conn, pid)["ai_label"] for pid in box + rest + [old])
    assert res["message"].startswith("61 of 61 done.") and "Photo review has them" in res["message"]
    # progress reads "N of M done" and only goes forward
    assert any(" of 61 done." in m for _, m in seen) and any("Getting photos" in m for _, m in seen)
    assert any("The AI has looked at" in m and "so far this run" in m for _, m in seen)
    fractions = [f for f, _ in seen]
    assert fractions == sorted(fractions) and fractions[-1] <= 1.0


def test_buy_box_first_when_a_cap_stops_the_run(tmp_root, conn, settings, key):
    set_config(tmp_root, "google.max_street_view_images_per_run", 3 * 4)   # room for 4 properties
    rest = [add(conn, in_buybox=0, rank=i + 1) for i in range(3)]
    box = [add(conn, rank=10 - i) for i in range(4)]
    res = photos_all.run(conn, MARKET, backend=MockBackend(), client=FakeGoogle().client())
    assert res["fetched"] == 4 and all(visual(conn, pid)["ai_label"] for pid in box)
    assert all(not visual(conn, pid) for pid in rest)


def test_pressing_again_continues_and_never_pays_twice(tmp_root, conn, settings, key):
    set_config(tmp_root, "google.max_street_view_images_per_run", 9)        # 3 properties per run
    for i in range(5):
        add(conn, rank=i + 1)
    google = FakeGoogle()
    first = photos_all.run(conn, MARKET, confirm=True, backend=MockBackend(), client=google.client())
    assert first["fetched"] == 3 and first["checked"] == 3 and first["capped"] is True
    assert "2 left. Press the button again" in first["message"]
    assert "google.max" not in first["message"] and "Run photos again" not in first["message"]   # no jargon
    images_after_first = google.images()
    second = photos_all.run(conn, MARKET, confirm=True, backend=MockBackend(), client=google.client())
    assert second["fetched"] == 2 and second["checked"] == 2 and second["left"] == 0
    assert google.images() - images_after_first == 2 * 4                  # only the 2 new ones: 3 SV + 1 sat each
    third = photos_all.run(conn, MARKET, confirm=True, backend=MockBackend(), client=google.client())
    assert third["nothing_to_do"] is True and google.images() - images_after_first == 8


def test_a_usage_limit_stops_it_and_pressing_again_carries_on(conn, settings, key):
    for i in range(4):
        add(conn, rank=i + 1)

    class Limited(MockBackend):
        calls = 0

        def classify(self, image_paths, prompt, *, schema=None):
            Limited.calls += 1
            if Limited.calls == 3:
                raise VisionFatalError("You've hit your Claude usage limit. It resets at 5pm.")
            return super().classify(image_paths, prompt, schema=schema)

    google = FakeGoogle()
    first = photos_all.run(conn, MARKET, confirm=True, backend=Limited(), client=google.client())
    assert first["fetched"] == 4 and first["checked"] == 2 and first["stopped"] is True
    assert "usage limit" in first["message"] and "Press the button again" in first["message"]
    sent = len(google.requests)
    second = photos_all.run(conn, MARKET, confirm=True, backend=MockBackend(), client=google.client())
    assert second["fetched"] == 0 and second["checked"] == 2 and second["left"] == 0
    assert len(google.requests) == sent                                    # no photo fetched twice


def test_test_on_5_does_5_and_the_ai_looks_at_those_first(conn, settings, key):
    old = add(conn, rank=1, status="done", pano_id="p-old", pano_lat=39.95, pano_lng=-83.77)
    photos_on_disk(settings, old)
    new = [add(conn, rank=10 + i) for i in range(8)]
    res = photos_all.run(conn, MARKET, limit=5, backend=MockBackend(), client=FakeGoogle().client())
    assert res["fetched"] == 5 and res["checked"] == 5 and res["needs_confirm"] is False
    assert all(visual(conn, pid).get("ai_label") for pid in new[:5]) and not visual(conn, old).get("ai_label")


def test_ai_only_when_there_is_no_google_key(conn, settings, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    have = add(conn, rank=1, status="done", pano_id="p-have", pano_lat=39.95, pano_lng=-83.77)
    photos_on_disk(settings, have)
    add(conn, rank=2)
    res = photos_all.run(conn, MARKET, backend=MockBackend())
    assert res["fetched"] == 0 and res["checked"] == 1 and visual(conn, have)["ai_label"]


def test_the_ai_not_set_up_is_a_plain_stop(conn, settings, key):
    have = add(conn, rank=1, status="done", pano_id="p-have", pano_lat=39.95, pano_lng=-83.77)
    photos_on_disk(settings, have)

    class NotSetUp(MockBackend):
        name = "claude_cli"

        def check(self):
            raise VisionFatalError("Claude Code isn't installed on this computer.")
    res = photos_all.run(conn, MARKET, confirm=True, backend=NotSetUp())
    assert res["error"] and "isn't installed" in res["message"] and res["checked"] == 0


# --- Photo review after a full run ------------------------------------------------------------

def test_photo_review_after_a_full_run_unsure_first_then_every_call(conn, settings, key, monkeypatch):
    from bluemoon.imagery import review

    for i in range(12):
        add(conn, rank=i + 1)
    # the mock answers by property: a spread of sure and unsure calls
    real = MockBackend.classify
    picks = iter([("EXTERIOR_CORRIDOR", 95), ("INTERIOR_CORRIDOR", 5), ("MIXED", 50)] * 4)

    def varied(self, image_paths, prompt, *, schema=None):
        label, p = next(picks)
        return ('{"label": "%s", "p_target": %d, "image_quality": "good", "property_visible": true, '
                '"visible_cues": ["stairs"], "reasons": "test"}' % (label, p))
    monkeypatch.setattr(MockBackend, "classify", varied)
    res = photos_all.run(conn, MARKET, backend=MockBackend(), client=FakeGoogle().client())
    monkeypatch.setattr(MockBackend, "classify", real)
    assert res["checked"] == 12
    queue = review.review_queue(conn, MARKET, limit=50, scope="everything")
    groups = [item["group"] for item in queue]
    assert len(queue) == 12 and groups == sorted(groups, key=lambda g: review.GROUP_ORDER[g])
    assert groups[0] == "unsure" and "confirm" in groups
    calls = [q for q in queue if q["group"] == "confirm"]
    assert all(q["ai_called"] and q["why"].startswith("The AI says:") for q in calls)
    # Yes on the first call keeps the AI's label; the next screen no longer lists it
    first = calls[0]
    review.save_review(conn, first["property_id"], agreed=True)
    assert len(review.review_queue(conn, MARKET, limit=50, scope="everything")) == 11
    assert photos_all.status(conn, MARKET)["done"] == 12


# --- the pipeline step, the job, the CLI and the API ------------------------------------------

def test_the_step_is_registered_and_the_old_names_still_work():
    assert pipeline.STEPS == ["import", "owners", "drive", "rent", "parcels", "photos", "classify", "score", "drill",
                              "research", "all"]            # the "all" plan did not change
    assert "photos_all" in pipeline.EXTRA_STEPS and "photos_all" in pipeline.PAID_STEPS
    assert "photos_all" in pipeline.RESCORE_AFTER
    assert "photo_batch" not in pipeline.EXTRA_STEPS and "photo_keep_going" not in pipeline.EXTRA_STEPS
    for old in ("photo_batch", "photo_keep_going", "photo-batch", "photos-all"):
        assert pipeline.normalize_step(old) == "photos_all"
    assert pipeline.step_label("photo_batch") == "Photos and the AI's call for every lead"


def test_no_monthly_goal_left_in_the_config(settings):
    assert settings.section("photos") == {}
    text = (Path(__file__).resolve().parent.parent / "config.yaml").read_text()
    assert "monthly_target" not in text


def test_a_big_job_waits_for_the_ok_then_runs_with_it(tmp_root, conn, settings, key, monkeypatch):
    for i in range(60):
        add(conn, rank=i + 1)
    google = FakeGoogle()
    real = streetview.fetch_imagery
    monkeypatch.setattr(streetview, "fetch_imagery", lambda *a, **k: real(*a, **{**k, "client": google.client()}))
    set_config(tmp_root, "vision.backend", "mock")
    first = pipeline.run_step(conn, MARKET, "photos_all")
    assert first["needs_confirm"] is True and google.requests == []
    assert first["estimate"]["lines"] and first["estimate"]["cost"]["usd"] == 0.0
    second = pipeline.run_step(conn, MARKET, "photos_all", confirm=True)
    assert second["ok"] and second["counts"]["fetched"] == 60 and second["counts"]["checked"] == 60
    assert second["counts"]["done"] == 60 and "Buy box, scores and rank updated" in second["message"]


def test_cli_photos_all_asks_first_then_goes_with_yes(tmp_root, settings, key, monkeypatch, capsys):
    import run as cli
    from bluemoon.db import connect

    c = connect(settings=settings)
    for i in range(55):
        add(c, rank=i + 1)
    c.close()
    google = FakeGoogle()
    real = streetview.fetch_imagery
    monkeypatch.setattr(streetview, "fetch_imagery", lambda *a, **k: real(*a, **{**k, "client": google.client()}))
    monkeypatch.setattr(cli, "interactive", lambda: False)
    set_config(tmp_root, "vision.backend", "mock")
    assert cli.main(["photos", "all", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "- Photos for 55 properties" in out and google.requests == []
    assert cli.main(["photos", "all"]) == cli.EXIT_WAITING
    out = capsys.readouterr().out
    assert "uv run run.py photos all --market apartments-springfield-oh --yes" in out and google.requests == []
    assert cli.main(["photos", "all", "--limit", "5"]) == cli.EXIT_OK        # a test on 5 needs no OK
    assert cli.main(["photos", "all", "--yes"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "55 of 55 done." in out
    c = connect(settings=settings)
    assert scalar(c, "SELECT COUNT(*) FROM visual_checks WHERE ai_label IS NOT NULL") == 55
    c.close()


@pytest.fixture
def client(tmp_root, settings):
    from fastapi.testclient import TestClient

    from bluemoon.db import connect
    from bluemoon.web.app import create_app
    connect(settings=settings).close()
    with TestClient(create_app()) as c:
        yield c


def test_api_status_and_estimate(client, conn, settings, key):
    add(conn, rank=1)
    body = client.get(f"/api/m/{MARKET}/photos/all").json()
    assert body["enabled"] and body["done"] == 0 and body["total"] == 1 and "estimate" not in body
    assert body["button"] == "Get photos and the AI's walk-up call for every lead"
    body = client.get(f"/api/m/{MARKET}/photos/all?estimate=1").json()
    est = body["estimate"]
    assert est["photos"] == 1 and est["lines"] and "details" not in est and est["needs_confirm"] is False
    assert client.get(f"/api/m/{MARKET}/photos/all?estimate=1&limit=5").json()["estimate"]["limit"] == 5
    assert client.get("/api/m/nope/photos/all").status_code == 404
    assert client.get(f"/api/m/{MARKET}/photos/month").status_code == 404     # the monthly goal is gone


def test_api_starts_the_job(client, conn, settings, key):
    add(conn, rank=1)
    res = client.post(f"/api/m/{MARKET}/jobs", json={"step": "photos_all", "dry_run": True})
    assert res.status_code == 200 and res.json()["job_id"]
