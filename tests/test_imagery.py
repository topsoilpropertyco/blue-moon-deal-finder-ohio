"""Street View + satellite photos (bluemoon/imagery/streetview.py).

Google is never called: every request goes to an httpx.MockTransport. The live smoke test at the
bottom runs only with BLUEMOON_LIVE=1 and a real GOOGLE_MAPS_API_KEY.
"""
from __future__ import annotations

import json
import logging
import os
from itertools import count
from pathlib import Path

import httpx
import pytest
import yaml

from bluemoon.cache import month_usage
from bluemoon.config import reload_settings
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.enrich import geo
from bluemoon.imagery import streetview
from bluemoon.imagery.streetview import (
    ImageryConfig,
    fetch_imagery,
    imagery_config,
    imagery_estimate,
    map_links,
    street_view_shots,
)

MARKET = "apartments-springfield-oh"
KEY = "TESTKEY-imagery-0123456789"
JPEG = b"\xff\xd8\xff\xe0" + b"\x10JFIF" + bytes(range(64))
LIVE_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")  # read before the fixtures clear it
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


def add_property(conn, market: str = MARKET, *, lat: float | None = 39.95, lng: float | None = -83.77,
                 in_buybox: int | None = 1, rank: int | None = None, score: float | None = None, **fields) -> int:
    m = market_by_slug(conn, market)
    n = next(_ids)
    data = {"market_id": m["id"], "property_key": f"test:{n}", "address": f"{n} TEST ST", "city": "SPRINGFIELD",
            "state": "OH", "zip": "45503", "county": "CLARK", "lat": lat, "lng": lng,
            "created_at": now_iso(), "updated_at": now_iso(), **fields}
    cols = ",".join(data)
    cur = conn.execute(f"INSERT INTO properties({cols}) VALUES({','.join('?' for _ in data)})", tuple(data.values()))
    pid = cur.lastrowid
    if in_buybox is not None:
        conn.execute("INSERT INTO scores(property_id, in_buybox, rank, score, computed_at) VALUES(?,?,?,?,?)",
                     (pid, in_buybox, rank, score, now_iso()))
    conn.commit()
    return pid


class FakeGoogle:
    """Answers like Google: metadata JSON, then JPEG photos. Records every request."""

    def __init__(self, meta_status: str = "OK", image_status: int = 200, map_status: int = 200,
                 fail_headings: set[float] | None = None, body: bytes = JPEG):
        self.meta_status = meta_status
        self.image_status = image_status
        self.map_status = map_status
        self.fail_headings = fail_headings or set()
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(request.url.params)
        path = request.url.path
        if path.endswith("/streetview/metadata"):
            lat, lng = (float(x) for x in params["location"].split(","))
            if self.meta_status != "OK":
                return httpx.Response(200, json={"status": self.meta_status})
            return httpx.Response(200, json={"status": "OK", "pano_id": f"pano-{lat:.5f}",
                                             "location": {"lat": lat - 0.0003, "lng": lng},
                                             "date": "2023-06", "copyright": "(c) Google"})
        if path.endswith("/streetview"):
            if float(params["heading"]) in self.fail_headings:
                return httpx.Response(500)
            if self.image_status != 200:
                return httpx.Response(self.image_status, text="error")
            return httpx.Response(200, content=self.body, headers={"content-type": "image/jpeg"})
        if path.endswith("/staticmap"):
            if self.map_status != 200:
                return httpx.Response(self.map_status, text="The Google Maps Platform server rejected your request.")
            return httpx.Response(200, content=self.body, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    def count(self, suffix: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(suffix))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def visual(conn, pid: int) -> dict:
    return one(conn, "SELECT * FROM visual_checks WHERE property_id=?", (pid,)) or {}


def files_of(settings, pid: int) -> list[str]:
    return sorted(p.name for p in (settings.path("images") / str(pid)).glob("*"))


# --- settings and geometry -------------------------------------------------------------------

def test_config_reads_yaml(settings):
    cfg = imagery_config(settings)
    assert cfg.street_view.fov == 80 and cfg.street_view.pitch == 5 and cfg.street_view.size == "640x640"
    assert cfg.street_view.side_offsets_deg == [-35, 35] and cfg.street_view.radius_m == 50
    assert cfg.street_view.source == "outdoor"
    assert cfg.satellite.zoom == 19 and cfg.satellite.maptype == "satellite"
    assert cfg.max_street_view_images_per_run == 3000 and cfg.max_satellite_images_per_run == 1000
    assert cfg.concurrency == 5 and cfg.confirm_over_properties == 50
    assert cfg.street_view_files == ["sv_0.jpg", "sv_1.jpg", "sv_2.jpg"]


def test_bad_config_value_is_plain_english(tmp_root, settings):
    set_config(tmp_root, "google.street_view.size", "big")
    with pytest.raises(ValueError, match="google setting"):
        imagery_config()


def test_shots_aim_at_property_and_turn_both_ways():
    cfg = ImageryConfig()
    base, shots = street_view_shots(39.9497, -83.77, 39.95, -83.77, cfg)  # panorama due south of the property
    assert base == pytest.approx(0.0, abs=0.2)
    assert [s.name for s in shots] == ["sv_0.jpg", "sv_1.jpg", "sv_2.jpg"]
    assert shots[1].heading == pytest.approx(325.0, abs=0.2) and shots[2].heading == pytest.approx(35.0, abs=0.2)
    base, shots = street_view_shots(39.95, -83.7705, 39.95, -83.77, cfg)  # panorama due west
    assert base == pytest.approx(90.0, abs=0.5)


def test_shots_when_panorama_sits_on_the_point():
    _, shots = street_view_shots(39.95, -83.77, 39.95, -83.77, ImageryConfig())
    assert [s.heading for s in shots] == [0.0, 120.0, 240.0]


def test_map_links_need_no_key():
    links = map_links(39.95, -83.77, 12.0)
    assert "query=39.950000,-83.770000" in links["google_maps"]
    assert "map_action=pano" in links["street_view"] and "heading=12" in links["street_view"]
    assert "basemap=satellite" in links["satellite"]
    assert all("key=" not in v for v in links.values())
    assert map_links(None, None) == {}


# --- dry run ---------------------------------------------------------------------------------

def test_estimate_counts_and_costs_without_calling(conn, settings):
    for _ in range(3):
        add_property(conn)
    est = imagery_estimate(conn, MARKET)
    assert est["dry_run"] and est["enabled"]
    assert est["properties"] == 3 and est["to_fetch"] == 3 and est["cached"] == 0
    assert est["street_view_images"] == 9 and est["satellite_images"] == 3 and est["metadata_calls"] == 3
    assert est["cost"]["street_view"]["units"] == 9 and est["cost"]["usd"] == 0.0  # inside the free monthly calls
    assert est["needs_confirm"] is False and est["has_key"] is False
    assert "GOOGLE_MAPS_API_KEY" in est["message"]
    assert scalar(conn, "SELECT COUNT(*) FROM api_calls") == 0
    assert fetch_imagery(conn, MARKET, dry_run=True)["dry_run"] is True


def test_estimate_bills_after_free_calls(tmp_root, conn, settings):
    set_config(tmp_root, "google.prices.street_view_static", {"per_1000": 7.0, "free_per_month": 0})
    add_property(conn)
    est = imagery_estimate(conn, MARKET)
    assert est["cost"]["street_view"]["billable"] == 3
    assert est["cost"]["usd"] == pytest.approx(0.02, abs=0.01)


def test_raw_land_has_no_photo_check(conn, settings):
    add_property(conn, "land-springfield-oh")
    est = imagery_estimate(conn, "land-springfield-oh")
    assert est["enabled"] is False and est["to_fetch"] == 0
    assert "no photo check" in est["message"]
    assert fetch_imagery(conn, "land-springfield-oh")["enabled"] is False


def test_empty_market_message(conn, settings):
    assert "Import a Crexi export" in imagery_estimate(conn, MARKET)["message"]


def test_estimate_skips_excluded_reviewed_and_unmapped(conn, settings):
    add_property(conn)
    add_property(conn, in_buybox=0)
    add_property(conn, lat=None, lng=None)
    reviewed = add_property(conn)
    conn.execute("INSERT INTO visual_checks(property_id, status, reviewed_label) VALUES(?, 'pending', 'MIXED')",
                 (reviewed,))
    conn.commit()
    est = imagery_estimate(conn, MARKET)
    assert est["properties"] == 1 and est["excluded"] == 1 and est["missing_coordinates"] == 1
    assert est["reviewed"] == 1
    assert imagery_estimate(conn, MARKET, include_excluded=True)["properties"] == 2


def test_unscored_property_uses_buy_box_check(conn, settings, monkeypatch):
    import bluemoon.scoring.buybox as buybox
    keep = add_property(conn, in_buybox=None, year_built=1990)
    drop = add_property(conn, in_buybox=None, year_built=1950)
    monkeypatch.setattr(buybox, "evaluate",
                        lambda prop, enrich, ac, market, s=None: {"in_buybox": (prop.get("year_built") or 0) >= 1970})
    est = imagery_estimate(conn, MARKET)
    assert est["properties"] == 1 and est["excluded"] == 1
    assert keep != drop


def test_buy_box_check_failure_never_blocks(conn, settings, monkeypatch):
    import bluemoon.scoring.buybox as buybox

    def broken(*a, **k):
        raise RuntimeError("bug elsewhere")

    monkeypatch.setattr(buybox, "evaluate", broken)
    add_property(conn, in_buybox=None)
    assert imagery_estimate(conn, MARKET)["properties"] == 1


# --- fetching --------------------------------------------------------------------------------

def test_no_key_fetches_nothing(conn, settings):
    add_property(conn)
    res = fetch_imagery(conn, MARKET)
    assert res["error"] and "GOOGLE_MAPS_API_KEY" in res["message"]
    assert res["fetched"] == 0 and scalar(conn, "SELECT COUNT(*) FROM api_calls") == 0


def test_fetch_saves_four_photos_and_records_calls(conn, settings, key):
    pid = add_property(conn)
    g = FakeGoogle()
    progress: list[tuple[float, str]] = []
    res = fetch_imagery(conn, MARKET, client=g.client(), progress=lambda f, m: progress.append((f, m)))
    assert res["fetched"] == 1 and res["errors"] == 0 and res["error"] is None
    assert res["street_view_images"] == 3 and res["satellite_images"] == 1 and res["metadata_calls"] == 1
    assert files_of(settings, pid) == ["sat.jpg", "sv_0.jpg", "sv_1.jpg", "sv_2.jpg"]
    row = visual(conn, pid)
    assert row["status"] == "done" and row["pano_id"].startswith("pano-") and row["pano_date"] == "2023-06"
    assert row["heading"] == pytest.approx(0.0, abs=0.2)
    assert json.loads(row["images_json"]) == [f"{pid}/sv_0.jpg", f"{pid}/sv_1.jpg", f"{pid}/sv_2.jpg",
                                              f"{pid}/sat.jpg"]
    assert month_usage(conn, "street_view_static") == 3
    assert month_usage(conn, "static_maps") == 1
    assert month_usage(conn, "street_view_metadata") == 1
    assert progress and progress[-1][0] == 1.0
    # request shapes
    sv = [r for r in g.requests if r.url.path.endswith("/streetview")]
    p = dict(sv[0].url.params)
    assert p["pano"].startswith("pano-") and p["fov"] == "80.0" and p["pitch"] == "5.0"
    assert p["size"] == "640x640" and p["return_error_code"] == "true"
    meta = dict(next(r for r in g.requests if r.url.path.endswith("/metadata")).url.params)
    assert meta["radius"] == "50" and meta["source"] == "outdoor"
    sat = dict(next(r for r in g.requests if r.url.path.endswith("/staticmap")).url.params)
    assert sat["zoom"] == "19" and sat["maptype"] == "satellite" and sat["format"] == "jpg"
    assert sat["markers"].startswith("size:tiny|color:red|")


def test_key_never_leaks(conn, settings, key, caplog):
    caplog.set_level(logging.DEBUG)
    add_property(conn)
    res = fetch_imagery(conn, MARKET, client=FakeGoogle().client())
    assert KEY not in json.dumps(res, default=str)
    for table in ("api_cache", "api_calls", "visual_checks"):
        assert KEY not in json.dumps(rows(conn, f"SELECT * FROM {table}"), default=str)
    assert KEY not in caplog.text


def test_rerun_never_repays(conn, settings, key):
    add_property(conn)
    add_property(conn, lat=39.96)
    fetch_imagery(conn, MARKET, client=FakeGoogle().client())
    g2 = FakeGoogle()
    res = fetch_imagery(conn, MARKET, client=g2.client())
    assert g2.requests == [] and res["cached"] == 2 and res["fetched"] == 0
    assert month_usage(conn, "street_view_static") == 6
    assert imagery_estimate(conn, MARKET)["to_fetch"] == 0


def test_no_street_view_still_gets_satellite(conn, settings, key):
    pid = add_property(conn)
    g = FakeGoogle(meta_status="ZERO_RESULTS")
    res = fetch_imagery(conn, MARKET, client=g.client())
    assert res["no_imagery"] == 1 and g.count("/streetview") == 0 and g.count("/staticmap") == 1
    assert files_of(settings, pid) == ["sat.jpg"]
    row = visual(conn, pid)
    assert row["status"] == "no_imagery" and row["pano_id"] is None
    assert "no Street View" in res["message"]
    g2 = FakeGoogle()
    fetch_imagery(conn, MARKET, client=g2.client())
    assert g2.requests == []  # the "no panorama" answer is remembered


def test_cached_metadata_is_reused_when_photos_were_deleted(conn, settings, key):
    pid = add_property(conn)
    fetch_imagery(conn, MARKET, client=FakeGoogle().client())
    (settings.path("images") / str(pid) / "sv_1.jpg").unlink()
    conn.execute("UPDATE visual_checks SET status='pending', pano_id=NULL, pano_lat=NULL, pano_lng=NULL "
                 "WHERE property_id=?", (pid,))
    conn.commit()
    g = FakeGoogle()
    fetch_imagery(conn, MARKET, client=g.client())
    assert g.count("/metadata") == 0 and g.count("/streetview") == 1 and g.count("/staticmap") == 0
    assert visual(conn, pid)["status"] == "done"


def test_limit_takes_best_ranked_first(conn, settings, key):
    low = add_property(conn, rank=2, score=0.1)
    top = add_property(conn, rank=1, score=0.5)
    res = fetch_imagery(conn, MARKET, limit=1, client=FakeGoogle().client())
    assert res["properties"] == 1
    assert files_of(settings, top) and not files_of(settings, low)


def test_confirm_needed_over_threshold(tmp_root, conn, settings, key):
    set_config(tmp_root, "safety.confirm_over_properties", 1)
    add_property(conn)
    add_property(conn, lat=39.96)
    g = FakeGoogle()
    res = fetch_imagery(conn, MARKET, client=g.client())
    assert res["needs_confirm"] is True and g.requests == [] and "Waiting for your OK" in res["message"]
    res = fetch_imagery(conn, MARKET, client=g.client(), confirm=True)
    assert res["fetched"] == 2


def test_caps_stop_the_run(tmp_root, conn, settings, key):
    set_config(tmp_root, "google.max_street_view_images_per_run", 4)
    add_property(conn)
    add_property(conn, lat=39.96)
    est = imagery_estimate(conn, MARKET)
    assert est["capped"] is True and est["street_view_images"] == 4
    res = fetch_imagery(conn, MARKET, client=FakeGoogle().client())
    assert res["fetched"] == 1 and res["capped"] is True
    assert any("cap" in w for w in res["warnings"])
    assert imagery_estimate(conn, MARKET)["capped"] is False  # the rest fits next run


def test_refused_key_stops_with_plain_message(conn, settings, key):
    a = add_property(conn)
    b = add_property(conn, lat=39.96)
    g = FakeGoogle(image_status=403, map_status=403)
    res = fetch_imagery(conn, MARKET, client=g.client())
    assert res["error"] and "Street View Static API" in res["error"]
    assert KEY not in res["message"]
    assert {visual(conn, a)["status"], visual(conn, b)["status"]} <= {"pending"}
    assert month_usage(conn, "street_view_static") == 0


def test_request_denied_metadata_is_fatal(conn, settings, key):
    add_property(conn)
    res = fetch_imagery(conn, MARKET, client=FakeGoogle(meta_status="REQUEST_DENIED").client())
    assert res["error"] and "refused" in res["error"]
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache") == 0  # denials are never cached


def test_one_failing_photo_is_retried_next_run(conn, settings, key):
    pid = add_property(conn)
    res = fetch_imagery(conn, MARKET, client=FakeGoogle(fail_headings={35.0}).client())
    assert res["errors"] == 1
    row = visual(conn, pid)
    assert row["status"] == "error" and row["error"]
    assert "sv_2.jpg" not in files_of(settings, pid) and "sv_0.jpg" in files_of(settings, pid)
    g = FakeGoogle()
    fetch_imagery(conn, MARKET, client=g.client())
    assert g.count("/streetview") == 1 and g.count("/staticmap") == 0
    assert visual(conn, pid)["status"] == "done"


def test_non_image_reply_is_not_saved(conn, settings, key):
    pid = add_property(conn)
    res = fetch_imagery(conn, MARKET, client=FakeGoogle(body=b"<html>nope</html>").client())
    assert res["errors"] == 1 and files_of(settings, pid) == []
    assert month_usage(conn, "street_view_static") == 0


def test_interrupted_run_is_repaired_without_calls(conn, settings, key):
    pid = add_property(conn)
    folder = settings.path("images") / str(pid)
    folder.mkdir(parents=True)
    for name in ("sv_0.jpg", "sv_1.jpg", "sv_2.jpg", "sat.jpg"):
        (folder / name).write_bytes(JPEG)
    g = FakeGoogle()
    res = fetch_imagery(conn, MARKET, client=g.client())
    assert g.requests == [] and res["fetched"] == 0
    assert visual(conn, pid)["status"] == "done"


def test_partial_file_never_counts(settings):
    folder = settings.path("images") / "77"
    streetview.save_image(folder, "sv_0.jpg", JPEG)
    assert (folder / "sv_0.jpg").read_bytes() == JPEG
    assert not list(folder.glob(".*.part"))
    (folder / "sv_1.jpg").write_bytes(b"")
    assert streetview.existing_images(settings, 77) == ["sv_0.jpg"]


def test_images_for_lists_urls(conn, settings, key):
    pid = add_property(conn)
    fetch_imagery(conn, MARKET, client=FakeGoogle().client())
    imgs = streetview.images_for(pid)
    assert [i["name"] for i in imgs] == ["sv_0.jpg", "sv_1.jpg", "sv_2.jpg", "sat.jpg"]
    assert imgs[0]["url"] == f"/images/{pid}/sv_0.jpg" and imgs[-1]["kind"] == "satellite"


def test_other_asset_classes_use_the_same_flow(conn, settings, key):
    pid = add_property(conn, "self-storage-springfield-oh")
    res = fetch_imagery(conn, "self-storage-springfield-oh", client=FakeGoogle().client())
    assert res["fetched"] == 1 and visual(conn, pid)["status"] == "done"


def test_server_errors_are_retried_then_reported(conn, settings, key):
    add_property(conn)
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    res = fetch_imagery(conn, MARKET, client=httpx.Client(transport=httpx.MockTransport(flaky)))
    assert calls["n"] > 1  # retried with backoff
    assert res["errors"] == 1 and res["error"] is None


# --- live smoke test (opt-in) ----------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("BLUEMOON_LIVE") != "1" or not LIVE_KEY,
                    reason="live Google test: set BLUEMOON_LIVE=1 and GOOGLE_MAPS_API_KEY")
def test_live_google_metadata(conn, settings, monkeypatch):  # pragma: no cover - needs network and a key
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", LIVE_KEY)
    add_property(conn, lat=39.9242, lng=-83.8088)
    res = fetch_imagery(conn, MARKET, limit=1, confirm=True)
    assert res["error"] is None
