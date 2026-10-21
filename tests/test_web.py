"""Web API tests (FastAPI TestClient) on the made-up fixture export. No network, no keys, no real data."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import stat
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from bluemoon.db import connect, now_iso, one, scalar
from bluemoon.drill import engine
from bluemoon.enrich.drive import compute_drive_times
from bluemoon.enrich.owners import compute_owners
from bluemoon.ingest import import_file
from bluemoon.scoring.rank import compute_scores

MARKET = "apartments-springfield-oh"
EMPTY_MARKET = "self-storage-springfield-oh"
LAND_MARKET = "land-springfield-oh"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

ROW_FIELDS = {
    "id", "address", "city", "state", "zip", "county", "lat", "lng", "units", "building_sqft", "lot_acres",
    "size_value", "size_basis", "size_color", "drive_minutes", "drive_method", "visual", "rent_fit", "owner_name",
    "owner_type", "years_owned", "score", "coverage_have", "coverage_total", "rank", "in_buybox",
    "exclusion_reasons", "level", "drill_status", "next_step_text", "channels", "crexi_url",
}
DETAIL_FIELDS = {"property", "raw", "signals", "visual_check", "rent", "owner", "drill", "links", "outcome", "flags"}


# --- fixtures -----------------------------------------------------------------------------------

def set_config(root: Path, dotted: str, value) -> None:
    path = root / "config.yaml"
    data = yaml.safe_load(path.read_text())
    node = data
    *parents, last = dotted.split(".")
    for key in parents:
        node = node.setdefault(key, {})
    node[last] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def web_root(tmp_root):
    """The temp project root with re-scoring done inside the request (no background timers in tests)."""
    set_config(tmp_root, "web.rescore_delay_seconds", 0)
    return tmp_root


@pytest.fixture
def built(web_root, settings, sample_csv):
    """The fixture export imported and run through owners, free drive estimates, score and DRILL."""
    conn = connect(settings=settings)
    import_file(conn, MARKET, sample_csv)
    compute_owners(conn, MARKET)
    compute_drive_times(conn, MARKET, provider="estimate", geocode=False)
    compute_scores(conn, MARKET)
    engine.run_auto_stages(conn, MARKET)
    yield conn
    conn.close()


@pytest.fixture
def client(built):
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def bare_client(web_root, settings):
    """A client on an empty database."""
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def api(c: TestClient, path: str, **params) -> dict:
    r = c.get(path, params=params)
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    return r.json()


def top_id(c: TestClient) -> int:
    return api(c, f"/api/m/{MARKET}/properties", limit=1)["items"][0]["id"]


def other_id(conn, slug: str = MARKET, in_buybox: int = 0) -> int:
    return scalar(conn, "SELECT p.id FROM properties p JOIN scores s ON s.property_id = p.id "
                        "JOIN markets m ON m.id = p.market_id WHERE m.slug = ? AND s.in_buybox = ? ORDER BY p.id",
                  (slug, in_buybox))


# --- health, markets, settings, sources ---------------------------------------------------------

def test_health_returns_booleans_only(bare_client, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-google-value-123")
    body = api(bare_client, "/api/health")
    assert body["ok"] is True and body["version"]
    assert body["keys"] == {"google": True, "anthropic": False, "hud": False, "census": False}
    assert all(isinstance(v, bool) for v in body["keys"].values())
    assert isinstance(body["claude_cli"], bool) and body["vision_backend"] == "claude_cli"
    assert body["auth_required"] is False and body["authenticated"] is True


def test_no_route_leaks_key_values(client, monkeypatch, web_root):
    secrets = {"GOOGLE_MAPS_API_KEY": "gkey-SECRET-111", "ANTHROPIC_API_KEY": "akey-SECRET-222",
               "HUD_API_TOKEN": "htok-SECRET-333"}
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    set_config(web_root, "rent.hud_api_token", "yaml-SECRET-444")   # a key pasted into config.yaml by mistake
    from bluemoon.config import get_settings
    get_settings.cache_clear()
    pid = top_id(client)
    for path in ("/api/health", "/api/settings", "/api/sources", "/api/markets", f"/api/m/{MARKET}/summary",
                 f"/api/m/{MARKET}/properties/{pid}", f"/api/m/{MARKET}/analytics"):
        text = client.get(path).text
        assert "SECRET" not in text, path
    settings_body = api(client, "/api/settings")
    assert settings_body["config"]["rent"]["hud_api_token"] == "(hidden)"
    assert settings_body["keys"]["google"] is True


def test_markets_lists_every_market_with_counts(client, settings):
    markets = api(client, "/api/markets")
    assert [m["slug"] for m in markets] == [m.slug for m in settings.markets]
    apt = next(m for m in markets if m["slug"] == MARKET)
    assert apt["properties"] == 11 and apt["in_buybox"] == 6 and apt["confirmed"] == 0
    assert apt["asset_label"] == "Apartments" and apt["geography"] == "Springfield, OH"
    assert next(m for m in markets if m["slug"] == EMPTY_MARKET)["properties"] == 0


def test_sources_catalog(client):
    body = api(client, "/api/sources")
    costs = [s.get("cost", "free") for s in body["sources"]]
    assert costs == sorted(costs, key=lambda c: {"free": 0, "credit": 1, "paid": 2}.get(c, 3))
    auditor = next(s for s in body["sources"] if s["key"] == "county_auditor")
    assert "county_auditor" in auditor["stages"]
    assert any(c["state"] == "OH" and c["county"] == "CLARK" for c in body["counties"])
    assert {s["key"] for s in body["stages"]} >= {"records_baseline", "county_auditor"}


# --- summary, list, detail ----------------------------------------------------------------------

def test_summary(client):
    s = api(client, f"/api/m/{MARKET}/summary")
    assert s["market"]["slug"] == MARKET
    ac = s["asset_class"]
    assert ac["key"] == "apartments" and ac["visual_enabled"] and ac["rent_enabled"]
    assert ac["size_metric"] == "units" and ac["size_ideal"] == [20, 50] and ac["size_ok"] == [10, 80]
    assert "EXTERIOR_CORRIDOR" in ac["labels"] and ac["visual_question"]
    assert s["properties"] == 11 and s["in_buybox"] == 6 and s["excluded"] == 5
    assert s["levels"] == {"1": 1, "2": 10, "3": 0} and s["penetration"] == 0.0
    assert s["credits"]["per_month"] == 500 and s["needs_review"] == 0
    layers = {layer["key"]: layer for layer in s["layers"]}
    assert set(layers) == {"owners", "drive", "rent", "photos", "photo_check", "score", "drill"}
    assert layers["owners"]["done"] == layers["owners"]["total"] == 11
    assert layers["photos"]["total"] == 6 and layers["rent"]["done"] == 0
    assert all({"key", "label", "done", "total"} <= set(layer) for layer in s["layers"])
    assert {v["key"] for v in s["views"]} >= {"all", "below", "excluded", "review", "everything"}


def test_list_default_is_the_buy_box_in_rank_order(client):
    body = api(client, f"/api/m/{MARKET}/properties")
    assert body["total"] == 6 and body["view"] == "all" and body["sort"] == "rank"
    assert [i["rank"] for i in body["items"]] == [1, 2, 3, 4, 5, 6]
    item = body["items"][0]
    assert ROW_FIELDS <= set(item)
    assert set(item["visual"]) >= {"label", "p", "color", "needs_review"}
    assert set(item["channels"]) == {"mail", "phone", "text", "email", "social", "vm"}
    assert item["in_buybox"] is True and item["exclusion_reasons"] == []
    assert item["next_step_text"] and item["drive_method"] == "estimate"


def test_every_view_total_matches_its_count(client):
    body = api(client, f"/api/m/{MARKET}/properties")
    counts = body["counts"]
    assert counts["all"] == 6 and counts["everything"] == 11 and counts["excluded"] == 5
    assert counts["below"] == 6 and counts["level1"] == 1 and counts["level2"] == 5
    for view, n in counts.items():
        got = api(client, f"/api/m/{MARKET}/properties", view=view, limit=500)
        assert got["total"] == n == len(got["items"]), view
    excluded = api(client, f"/api/m/{MARKET}/properties", view="excluded")["items"]
    assert all(i["in_buybox"] is False and i["exclusion_reasons"] for i in excluded)
    assert all(i["owner_type"] in ("individual", "trust", "estate", "small_llc")
               for i in api(client, f"/api/m/{MARKET}/properties", view="mom")["items"])
    assert all(i["years_owned"] >= 15 for i in api(client, f"/api/m/{MARKET}/properties", view="long")["items"])
    assert all(i["size_color"] == "green" for i in api(client, f"/api/m/{MARKET}/properties", view="sweet")["items"])


def test_include_excluded_widens_the_view(client):
    assert api(client, f"/api/m/{MARKET}/properties", view="mom", include_excluded="true")["total"] > \
        api(client, f"/api/m/{MARKET}/properties", view="mom")["total"]


def test_search(client):
    url = f"/api/m/{MARKET}/properties"
    assert api(client, url, q="xenia")["total"] == 2
    assert api(client, url, q="45503")["total"] == 1
    assert api(client, url, q="meadow 79")["total"] == 1                 # every word must match
    assert api(client, url, q="340010000100", view="everything")["total"] == 1  # APN without dashes
    assert api(client, url, q="340-01-00-001", view="everything")["total"] == 1
    assert api(client, url, q="holloway")["total"] == 1                  # owner name
    assert api(client, url, q="oxford")["total"] == 0                     # excluded, so not in the buy box
    assert api(client, url, q="oxford", view="everything")["total"] == 1
    assert api(client, url, q="%")["total"] == 0                          # LIKE wildcards are literal
    assert api(client, url, q="_")["total"] == 0
    counts = api(client, url, q="dayton")["counts"]
    assert counts["all"] == 2 and counts["everything"] == 2


def test_sort_and_paging(client):
    url = f"/api/m/{MARKET}/properties"
    everything = api(client, url, view="everything", sort="drive", limit=500)["items"]
    minutes = [i["drive_minutes"] for i in everything]
    assert minutes == sorted(minutes)
    reverse = [i["drive_minutes"] for i in api(client, url, view="everything", sort="drive", dir="desc")["items"]]
    assert reverse == sorted(minutes, reverse=True)
    scores = [i["score"] for i in api(client, url, view="everything", sort="score")["items"]]
    assert scores == sorted(scores, reverse=True)
    addresses = [i["address"] for i in api(client, url, view="everything", sort="address")["items"]]
    assert addresses == sorted(addresses, key=str.lower)
    for key in ("units", "level", "years"):
        assert api(client, url, sort=key)["total"] == 6
    page1 = api(client, url, view="everything", sort="drive", limit=4, offset=0)
    page3 = api(client, url, view="everything", sort="drive", limit=4, offset=8)
    assert page1["total"] == page3["total"] == 11 and len(page3["items"]) == 3
    assert [i["id"] for i in page1["items"] + page3["items"]] == [i["id"] for i in everything[:4] + everything[8:]]
    past = api(client, url, offset=100)
    assert past["total"] == 6 and past["items"] == []
    assert api(client, url, limit=100000)["limit"] == 500


@pytest.mark.parametrize("params", [{"view": "nope"}, {"sort": "nope"}, {"dir": "sideways"}])
def test_bad_list_parameters_are_400(client, params):
    r = client.get(f"/api/m/{MARKET}/properties", params=params)
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)


def test_unknown_market_and_property_are_404(client, built):
    assert client.get("/api/m/nowhere/summary").status_code == 404
    assert client.get("/api/m/nowhere/properties").json()["detail"] == "Unknown market 'nowhere'"
    assert client.get(f"/api/m/{MARKET}/properties/999999").status_code == 404
    pid = top_id(client)
    assert client.get(f"/api/m/{EMPTY_MARKET}/properties/{pid}").status_code == 404   # wrong market
    r = client.post(f"/api/m/{EMPTY_MARKET}/properties/{pid}/level", json={"action": "dnc"})
    assert r.status_code == 404
    assert one(built, "SELECT status FROM drill_state WHERE property_id = ?", (pid,))["status"] == "active"


def test_property_detail(client):
    pid = top_id(client)
    d = api(client, f"/api/m/{MARKET}/properties/{pid}")
    assert ROW_FIELDS <= set(d) and DETAIL_FIELDS <= set(d)
    assert d["id"] == pid and d["rank"] == 1
    assert d["raw"]["Address"] == d["address"]                    # original Crexi columns kept
    assert "raw_json" not in d["property"] and d["property"]["id"] == pid
    assert d["signals"] and {"key", "label", "multiplier", "why"} <= set(d["signals"][0])
    assert d["visual_check"]["enabled"] is True and d["visual_check"]["images"] == []
    assert d["visual_check"]["map_links"]["google_maps"].startswith("https://www.google.com/maps")
    assert d["rent"]["enabled"] is True and d["rent"]["scale"]
    assert d["owner"]["owner_type"] and "mailing" in d["owner"]
    assert d["drill"]["property_id"] == pid and d["drill"]["stages"]
    assert d["next_step_text"] == d["drill"]["next_step"]["text"]
    assert any(link["source_key"] == "crexi" for link in d["links"])
    assert d["outcome"]["value"] is None and len(d["outcome"]["options"]) == 4
    assert d["buybox"]["size_detail"]


def test_excluded_detail_explains_why(client, built):
    pid = other_id(built, in_buybox=0)
    d = api(client, f"/api/m/{MARKET}/properties/{pid}")
    assert d["in_buybox"] is False and d["buybox"]["exclusions"]
    assert all(e["label"] and e["detail"] for e in d["buybox"]["exclusions"])


# --- writes -------------------------------------------------------------------------------------

def test_drill_writes_through_to_confirmed_owner(client):
    pid = top_id(client)
    base = f"/api/m/{MARKET}/properties/{pid}"
    r = client.post(f"{base}/attempt", json={"stage_key": "county_auditor", "outcome": "found",
                                             "detail": "Owner confirmed on the parcel page", "person": "Pat Tester",
                                             "contacts": [{"channel": "phone", "value": "937-555-0100"}]})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["drill"]["contacts"]
    r = client.post(f"{base}/contacts", json={"channel": "email", "value": "pat@example.com", "source": "manual"})
    assert r.status_code == 200 and r.json()["created"] is True
    contact_id = r.json()["contact"]["id"]
    r = client.patch(f"/api/contacts/{contact_id}", json={"status": "wrong"})
    assert r.status_code == 200 and r.json()["contact"]["status"] == "wrong"
    row = api(client, f"/api/m/{MARKET}/properties", q="pat tester")["items"]
    assert [i["id"] for i in row] == [pid]                                   # best-guess person is searchable
    assert row[0]["channels"]["phone"] == "untested" and row[0]["channels"]["email"] == "wrong"
    r = client.post(f"{base}/outreach", json={"channel": "phone", "outcome": "reached_confirmed",
                                              "notes": "Talked; they own it"})
    assert r.status_code == 200 and r.json()["level"] == 3
    confirmed = api(client, f"/api/m/{MARKET}/properties", view="confirmed")
    assert [i["id"] for i in confirmed["items"]] == [pid]
    assert api(client, f"/api/m/{MARKET}/properties", view="below")["total"] == 5
    summary = api(client, f"/api/m/{MARKET}/summary")
    assert summary["levels"]["3"] == 1 and summary["penetration"] == pytest.approx(1 / 6, abs=1e-3)
    funnel = api(client, f"/api/m/{MARKET}/funnel")
    reach = {s["key"]: s["count"] for s in funnel["reach"]}
    assert reach == {"in_drill": 6, "possible": 5, "confirmed": 1}
    hits = {h["key"]: h["count"] for h in funnel["hits"]}
    assert hits["stage_hits"] == 1 and hits["outreach"] == 1 and hits["verified"] == 1


def test_people_route(client):
    pid = top_id(client)
    r = client.post(f"/api/m/{MARKET}/properties/{pid}/people", json={"name": "Lee Example", "role": "manager"})
    assert r.status_code == 200 and r.json()["created"] is True


def test_write_validation_errors(client):
    pid = top_id(client)
    base = f"/api/m/{MARKET}/properties/{pid}"
    r = client.post(f"{base}/attempt", json={"stage_key": "county_auditor", "outcome": "maybe"})
    assert r.status_code == 400 and "found, nothing or skipped" in r.json()["detail"]
    r = client.post(f"{base}/attempt", json={"stage_key": "no_such_stage", "outcome": "found"})
    assert r.status_code == 400
    r = client.post(f"{base}/attempt", json={"outcome": "found"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str) and "stage_key" in r.json()["detail"]
    assert client.post(f"{base}/contacts", json={"channel": "pigeon", "value": "x"}).status_code == 400
    assert client.patch("/api/contacts/987654", json={"status": "verified"}).status_code == 404
    assert client.post(f"{base}/level", json={"action": "promote"}).status_code == 400


def test_level_actions_including_dnc(client):
    pid = top_id(client)
    base = f"/api/m/{MARKET}/properties/{pid}"
    r = client.post(f"{base}/level", json={"action": "dnc", "note": "Asked us to stop"})
    assert r.status_code == 200 and r.json()["status"] == "dnc"
    assert [i["id"] for i in api(client, f"/api/m/{MARKET}/properties", view="dnc")["items"]] == [pid]
    r = client.post(f"{base}/outreach", json={"channel": "phone", "outcome": "no_answer"})
    assert r.status_code == 400 and "do-not-contact" in r.json()["detail"]
    assert client.post(f"{base}/level", json={"action": "confirm"}).status_code == 400
    assert client.post(f"{base}/level", json={"action": "undnc"}).json()["status"] == "active"
    assert client.post(f"{base}/level", json={"action": "exhausted"}).json()["status"] == "exhausted"
    assert client.post(f"{base}/level", json={"action": "reopen"}).json()["status"] == "active"
    r = client.post(f"{base}/level", json={"action": "confirm", "person": "Pat Tester"})
    assert r.json()["level"] == 3 and r.json()["status"] == "confirmed"
    assert client.post(f"{base}/level", json={"action": "handoff"}).status_code == 200


def test_credit_use_and_plan(client):
    body = api(client, f"/api/m/{MARKET}/credits")
    assert body["status"]["left"] == 500 and body["log"] == []
    assert body["planned"] == len(body["plan"]) <= 25 and body["message"]
    pid = body["plan"][0]["property_id"] if body["plan"] else top_id(client)
    base = f"/api/m/{MARKET}/properties/{pid}"
    r = client.post(f"{base}/credit", json={})
    assert r.status_code == 200 and r.json()["credits"]["used"] == 1
    r = client.post(f"{base}/credit", json={"result": "found", "contacts": [{"channel": "phone",
                                                                           "value": "937-555-0199"}]})
    assert r.status_code == 200 and r.json()["result"] == "found"
    after = api(client, f"/api/m/{MARKET}/credits")
    assert after["status"]["used"] == 1 and after["status"]["found"] == 1
    assert [x["property_id"] for x in after["log"]] == [pid]
    assert client.post(f"{base}/credit", json={"result": "maybe"}).status_code == 400


def _photo_property(conn, settings, pid: int, **visual) -> None:
    folder = settings.path("images") / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("sv_0.jpg", "sv_1.jpg", "sv_2.jpg", "sat.jpg"):
        (folder / name).write_bytes(JPEG)
    row = {"property_id": pid, "status": "done", "heading": 90.0, "ai_label": "MIXED", "ai_p": 55.0,
           "image_quality": "good", "property_visible": 1, "cues_json": json.dumps(["stairs"]),
           "reasons": "Looks like both kinds", "prior": 0.0, "final_p": 54.0, "model": "mock",
           "checked_at": now_iso(), **visual}
    cols = ",".join(row)
    conn.execute(f"INSERT INTO visual_checks({cols}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
    conn.commit()


def test_photo_review_flow(client, built, settings):
    pid = top_id(client)
    _photo_property(built, settings, pid)
    queue = api(client, f"/api/m/{MARKET}/review/queue")
    assert queue["total"] == 1 and queue["counts"]["unsure"] == 1
    card = queue["items"][0]
    assert card["id"] == pid and len(card["images"]) == 4 and len(card["choices"]) == 5
    img = client.get(card["images"][0]["url"])
    assert img.status_code == 200 and img.content == JPEG
    assert api(client, f"/api/m/{MARKET}/summary")["needs_review"] == 1
    review_view = api(client, f"/api/m/{MARKET}/properties", view="review")
    assert [i["id"] for i in review_view["items"]] == [pid] and review_view["items"][0]["visual"]["needs_review"]
    detail = api(client, f"/api/m/{MARKET}/properties/{pid}")
    assert detail["visual_check"]["images"] == [f"/images/{pid}/{n}" for n in ("sv_0.jpg", "sv_1.jpg", "sv_2.jpg",
                                                                                 "sat.jpg")]
    assert detail["visual_check"]["needs_review"] is True and detail["visual_check"]["prior_rules"]
    r = client.post(f"/api/m/{MARKET}/properties/{pid}/review", json={"label": 1, "note": "Open stairs"})
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "EXTERIOR_CORRIDOR" and r.json()["rescore"] == "done"
    row = next(i for i in api(client, f"/api/m/{MARKET}/properties")["items"] if i["id"] == pid)
    assert row["visual"] == {"label": "EXTERIOR_CORRIDOR", "p": 100.0, "color": "green",
                                                  "needs_review": False, "reviewed": True, "status": "done"}
    assert one(built, "SELECT visual_color FROM scores WHERE property_id = ?", (pid,))["visual_color"] == "green"
    assert api(client, f"/api/m/{MARKET}/review/queue")["total"] == 0
    assert api(client, f"/api/m/{MARKET}/properties", view="walk")["total"] == 1
    card = api(client, f"/api/m/{MARKET}/review/scorecard")
    assert card["reviewed"] == 1
    assert client.post(f"/api/m/{MARKET}/properties/{pid}/review", json={"label": "TREEHOUSE"}).status_code == 400
    find = {s["key"]: s["count"] for s in api(client, f"/api/m/{MARKET}/funnel")["find"]}
    assert find["photo_checked"] == 1 and find["target"] == 1


def test_outcome_and_model(client):
    pid = top_id(client)
    r = client.post(f"/api/m/{MARKET}/properties/{pid}/outcome", json={"outcome": "bought", "note": "Closed"})
    assert r.status_code == 200 and r.json()["outcome"] == "bought" and r.json()["rescore"] == "done"
    assert "Scores update" in r.json()["message"]
    d = api(client, f"/api/m/{MARKET}/properties/{pid}")
    assert d["outcome"]["value"] == "bought" and d["outcome"]["label"] == "Bought"
    model = api(client, f"/api/m/{MARKET}/model")
    assert model["outcomes"]["total"] == 1 and model["signals"]
    assert client.post(f"/api/m/{MARKET}/properties/{pid}/outcome", json={"outcome": "sold?"}).status_code == 400
    r = client.post(f"/api/m/{MARKET}/properties/{pid}/outcome", json={"outcome": None})
    assert r.status_code == 200 and r.json()["outcome"] is None
    assert api(client, f"/api/m/{MARKET}/properties/{pid}")["outcome"]["value"] is None


# --- DRILL board --------------------------------------------------------------------------------

def test_drill_overview_queues_and_stages(client):
    board = api(client, f"/api/m/{MARKET}/drill")
    assert board["levels"] and board["pipeline"] and board["queues"]
    waiting = next(q for q in board["queues"] if q["count"] and q["key"] != "validate")
    queue = api(client, f"/api/m/{MARKET}/drill/queue/{waiting['key']}", limit=2)
    assert queue["total"] == waiting["count"] and len(queue["items"]) == min(2, waiting["count"])
    assert queue["items"][0]["links"] is not None and queue["stage"]["outcomes"]
    validate = api(client, f"/api/m/{MARKET}/drill/queue/validate")
    assert validate["stage"]["key"] == "validate" and validate["total"] == 0
    assert client.get(f"/api/m/{MARKET}/drill/queue/not_a_stage").status_code == 400
    stages = api(client, f"/api/m/{MARKET}/drill/stages")
    keys = [s["key"] for s in stages["stages"]]
    assert keys[0] == "records_baseline" and "crexi_credit" in keys
    assert {"tried", "found", "rate", "best_for_text", "method", "hit_rate"} <= set(stages["stages"][0])
    assert stages["validate"]["outcomes"] and stages["levels"]


# --- funnel, analytics --------------------------------------------------------------------------

def test_funnel(client):
    f = api(client, f"/api/m/{MARKET}/funnel")
    find = {s["key"]: s["count"] for s in f["find"]}
    assert find["loaded"] == 11 and find["in_buybox"] == 6 and find["photo_checked"] == 0
    assert [s["label"] for s in f["find"]][3] == "Walk-up confirmed"
    assert f["find"][1]["pct_of_previous"] == pytest.approx(6 / 11, abs=1e-3)


def test_analytics(client):
    a = api(client, f"/api/m/{MARKET}/analytics")
    assert len(a["imports"]) == 1 and a["imports"][0]["rows_total"] == 12
    ff = a["filter_funnel"]
    assert ff[0] == {"key": "loaded", "label": "Loaded", "count": 11, "removed": 0, "removed_label": None}
    assert ff[-1]["count"] == 6 and sum(step["removed"] for step in ff) == 5
    assert all(ff[i]["count"] >= ff[i + 1]["count"] for i in range(len(ff) - 1))
    coverage = {c["field"]: c["pct"] for c in a["coverage"]}
    assert coverage["location"] == 100.0 and 0 <= coverage["owner_name"] <= 100
    assert sum(c["count"] for c in a["counties"]) == 11
    assert sum(b["count"] for b in a["size_dist"]) == 11
    assert [b["label"] for b in a["size_dist"]][:5] == ["Under 10", "10-19", "20-50 (sweet spot)", "51-80", "Over 80"]
    assert sum(b["count"] for b in a["score_dist"]) == 6
    assert sum(o["count"] for o in a["owner_types"]) == 11
    assert {lv["key"]: lv["count"] for lv in a["levels"]} == {"1": 1, "2": 10, "3": 0, "dnc": 0}
    assert a["api_usage"] == []


def test_analytics_api_usage_prices_after_free_calls(client, built):
    for sku, units in (("street_view_static", 10_500), ("vision_claude_cli", 12), ("vision_anthropic_api", 10)):
        built.execute("INSERT INTO api_calls(provider, sku, units, created_at) VALUES('x', ?, ?, ?)",
                      (sku, units, now_iso()))
    built.commit()
    usage = {u["sku"]: u for u in api(client, f"/api/m/{MARKET}/analytics")["api_usage"]}
    assert usage["street_view_static"]["billable"] == 500 and usage["street_view_static"]["usd"] == 3.5
    assert usage["vision_claude_cli"]["usd"] is None and usage["vision_anthropic_api"]["usd"] == 0.3


# --- empty markets and other asset classes ------------------------------------------------------

@pytest.mark.parametrize("path", ["summary", "properties", "funnel", "analytics", "model", "drill", "drill/stages",
                                  "credits", "review/queue", "review/scorecard", "drill/queue/county_auditor"])
def test_empty_market_reads_work(bare_client, path):
    api(bare_client, f"/api/m/{EMPTY_MARKET}/{path}")


def test_imported_but_not_scored_yet(bare_client, settings, sample_csv):
    """Before any enrichment, nothing counts as excluded (missing data never excludes) and nothing breaks."""
    conn = connect(settings=settings)
    import_file(conn, MARKET, sample_csv)
    conn.close()
    body = api(bare_client, f"/api/m/{MARKET}/properties")
    assert body["total"] == 11 and body["counts"]["excluded"] == 0
    item = body["items"][0]
    assert item["rank"] is None and item["score"] is None and item["in_buybox"] is None
    assert item["visual"]["color"] == "pending" and item["level"] in (1, 2) and item["next_step_text"]
    s = api(bare_client, f"/api/m/{MARKET}/summary")
    assert s["scored"] == 0 and s["in_buybox"] == 11
    assert {layer["key"]: layer["done"] for layer in s["layers"]}["owners"] == 0
    for path in ("funnel", "analytics", "drill", "credits", "model", f"properties/{item['id']}"):
        api(bare_client, f"/api/m/{MARKET}/{path}")


def test_raw_land_has_no_photo_check(client):
    s = api(client, f"/api/m/{LAND_MARKET}/summary")
    assert s["asset_class"]["visual_enabled"] is False and s["properties"] == 0
    assert [f["key"] for f in api(client, f"/api/m/{LAND_MARKET}/funnel")["find"]] == ["loaded", "in_buybox",
                                                                                         "blue_moon"]
    assert api(client, f"/api/m/{LAND_MARKET}/review/queue")["items"] == []


# --- import -------------------------------------------------------------------------------------

def test_import_preview_and_import(bare_client, sample_csv, settings):
    files = {"file": ("my export (Oct).csv", sample_csv.read_bytes(), "text/csv")}
    r = bare_client.post(f"/api/m/{MARKET}/import/preview", files=files)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["rows"] == 12 and preview["column_map"]["address"] == "Address"
    assert all("owner_name" not in row and "mailing_address" not in row for row in preview["sample"])
    assert not list((settings.path("db").parent / "tmp").glob("*"))      # preview files cleaned up
    r = bare_client.post(f"/api/m/{MARKET}/import", files=files, data={"limit": "5"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_new"] == 5 and body["saved_as"].endswith("_my_export_Oct.csv")
    assert (settings.path("crexi_dir") / body["saved_as"]).is_file()
    assert "after_import" in body and isinstance(body["after_import"], dict)
    if importlib.util.find_spec("bluemoon.pipeline"):
        assert body["after_import"]["ok"] is True
        assert api(bare_client, f"/api/m/{MARKET}/summary")["scored"] == 5
    assert api(bare_client, f"/api/m/{MARKET}/summary")["last_import"]["rows_new"] == 5


@pytest.mark.parametrize("name,content,status", [
    ("old.xls", b"whatever", 400), ("notes.txt", b"hello", 400), ("empty.csv", b"", 400),
    ("bad.csv", b"just,some,words\n1,2,3\n", 400)])
def test_import_rejects_bad_files(bare_client, settings, name, content, status):
    r = bare_client.post(f"/api/m/{MARKET}/import", files={"file": (name, content, "text/csv")})
    assert r.status_code == status and isinstance(r.json()["detail"], str)
    assert not [p for p in settings.path("crexi_dir").glob("*") if p.name != ".gitkeep"]   # nothing left behind


def test_import_size_limit(bare_client, web_root, settings):
    set_config(web_root, "web.upload_max_mb", 0.001)                    # about 1 KB
    from bluemoon.config import get_settings
    get_settings.cache_clear()
    big = b"Address,City\n" + b"1 MAIN ST,SPRINGFIELD\n" * 200
    r = bare_client.post(f"/api/m/{MARKET}/import", files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413 and "MB" in r.json()["detail"]


# --- pipeline, jobs, exports ---------------------------------------------------------------------

def test_estimate(client):
    pytest.importorskip("bluemoon.pipeline")
    est = api(client, f"/api/m/{MARKET}/estimate", limit=5)
    assert est["market"] == MARKET and est["steps"] and "total_usd" in est


def _wait(c: TestClient, job_id: int, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api(c, f"/api/jobs/{job_id}")
        if job["done"]:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_jobs_start_poll_list_cancel(client):
    pytest.importorskip("bluemoon.jobs")
    r = client.post(f"/api/m/{MARKET}/jobs", json={"step": "score"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["job"]["kind"] == "score"
    job = _wait(client, job_id)
    assert job["status"] == "done" and job["market"] == MARKET
    listed = api(client, "/api/jobs", market=MARKET)
    assert [j["id"] for j in listed] == [job_id]
    assert api(client, "/api/jobs", market=EMPTY_MARKET) == []
    cancelled = client.post(f"/api/jobs/{job_id}/cancel")                # already finished: unchanged
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "done"
    assert client.get("/api/jobs/99999").status_code == 404
    assert client.post("/api/jobs/99999/cancel").status_code == 404
    assert client.post(f"/api/m/{MARKET}/jobs", json={"step": "teleport"}).status_code == 400
    assert client.post(f"/api/m/{MARKET}/jobs", json={"step": "drive", "limit": 0}).status_code == 422
    assert client.get("/api/jobs", params={"market": "nowhere"}).status_code == 404


def test_jobs_cancel_a_waiting_job(client, monkeypatch):
    jobs = pytest.importorskip("bluemoon.jobs")
    gate = threading.Event()

    def slow(conn, kind, market, params, progress):
        gate.wait(10)
        return {"ok": True, "message": "done"}

    monkeypatch.setattr(jobs._runner, "_execute", slow)
    first = client.post(f"/api/m/{MARKET}/jobs", json={"step": "owners"}).json()["job_id"]
    second = client.post(f"/api/m/{MARKET}/jobs", json={"step": "drill"}).json()["job_id"]
    r = client.post(f"/api/jobs/{second}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    gate.set()
    assert _wait(client, first)["status"] == "done"


def test_missing_optional_module_is_503(client, monkeypatch):
    real = importlib.import_module

    def fake(name, *a, **kw):
        if name in ("bluemoon.jobs", "bluemoon.exports", "bluemoon.pipeline"):
            raise ModuleNotFoundError(name)
        return real(name, *a, **kw)

    import bluemoon.web.app as web_app
    monkeypatch.setattr(web_app.importlib, "import_module", fake)
    for method, path, body in (("post", f"/api/m/{MARKET}/jobs", {"step": "score"}), ("get", "/api/jobs", None),
                               ("get", f"/api/m/{MARKET}/estimate", None),
                               ("get", f"/api/m/{MARKET}/export/ranked_list.csv", None)):
        r = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        assert r.status_code == 503, path
        assert "isn't available" in r.json()["detail"]


EXPORTS = ("ranked_list.xlsx", "ranked_list.csv", "crm_handoff.csv", "mailing_list.csv", "drill_worklist.csv",
           "excluded.zip", "run_summary.md")


@pytest.mark.parametrize("name", EXPORTS)
def test_export_downloads(client, name):
    pytest.importorskip("bluemoon.exports")
    r = client.get(f"/api/m/{MARKET}/export/{name}")
    assert r.status_code == 200, r.text[:300]
    assert f"{MARKET}_{name}" in r.headers["content-disposition"]
    if name.endswith(".zip"):
        assert zipfile.ZipFile(BytesIO(r.content)).namelist()
    elif name.endswith(".xlsx"):
        assert r.content[:2] == b"PK"
    else:
        assert r.content


def test_unknown_export_is_404(client):
    r = client.get(f"/api/m/{MARKET}/export/passwords.txt")
    assert r.status_code == 404 and "ranked_list.xlsx" in r.json()["detail"]


# --- images and static files --------------------------------------------------------------------

def test_images_only_serve_photos_inside_the_property_folder(client, settings, web_root):
    folder = settings.path("images") / "7"
    folder.mkdir(parents=True)
    (folder / "sv_0.jpg").write_bytes(JPEG)
    (folder / ".hidden.jpg").write_bytes(JPEG)
    (settings.path("images") / "secret.jpg").write_bytes(JPEG)
    (web_root / ".env").write_text("APP_PASSWORD=\n")
    ok = client.get("/images/7/sv_0.jpg")
    assert ok.status_code == 200 and ok.content == JPEG and ok.headers["content-type"] == "image/jpeg"
    for bad in ("/images/7/..%2Fsecret.jpg", "/images/7/..%2F..%2F.env", "/images/7/%2E%2E%2Fsecret.jpg",
                "/images/7/.hidden.jpg", "/images/7/sv_0.jpg%00.png", "/images/7/config.yaml",
                "/images/7/missing.jpg", "/images/..%2F7/sv_0.jpg", "/images/abc/sv_0.jpg",
                "/images/7/..\\secret.jpg", "/images/-1/sv_0.jpg"):
        r = client.get(bad)
        assert r.status_code in (404, 422), bad
        assert r.content != JPEG


def test_static_files_and_index(client, tmp_path, monkeypatch):
    import bluemoon.web.app as web_app
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>BM</title>")
    (static / "core.js").write_text("window.BM = {};")
    (static / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "outside.js").write_text("nope")
    monkeypatch.setattr(web_app, "STATIC_DIR", static)
    r = client.get("/")
    assert r.status_code == 200 and "<title>BM</title>" in r.text and r.headers["cache-control"] == "no-cache"
    js = client.get("/static/core.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert "max-age" in client.get("/static/logo.png").headers["cache-control"]
    assert client.get("/static/..%2Foutside.js").status_code == 404
    assert client.get("/static/").status_code == 404
    top = client.get("/core.js")                                        # relative links from index.html
    assert top.status_code == 200 and top.headers["cache-control"] == "no-cache"
    assert client.get("/outside.js").status_code == 404
    assert client.get("/api/nothing-here").status_code == 404


def test_index_without_web_app_files_shows_a_placeholder(client, tmp_path, monkeypatch):
    import bluemoon.web.app as web_app
    monkeypatch.setattr(web_app, "STATIC_DIR", tmp_path / "missing")
    r = client.get("/")
    assert r.status_code == 200 and "web app files are missing" in r.text


# --- errors -------------------------------------------------------------------------------------

def test_unexpected_errors_are_short_500s(client, monkeypatch, caplog):
    import bluemoon.web.queries as q

    def boom(*a, **kw):
        raise RuntimeError("internal detail that must not reach the browser")

    monkeypatch.setattr(q, "funnel", boom)
    r = client.get(f"/api/m/{MARKET}/funnel")
    assert r.status_code == 500
    assert "RuntimeError" in r.json()["detail"] and "internal detail" not in r.json()["detail"]
    assert "internal detail" in caplog.text                              # the traceback is in the server log


def test_api_responses_are_not_cached(client):
    r = client.get(f"/api/m/{MARKET}/summary")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


# --- password mode ------------------------------------------------------------------------------

def test_password_mode(client, monkeypatch, settings):
    password = "correct horse battery"
    monkeypatch.setenv("APP_PASSWORD", password)
    for path in ("/api/markets", f"/api/m/{MARKET}/summary", "/images/1/sv_0.jpg", "/api/openapi.json"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.json() == {"detail": "Please log in.", "login": "/login"}
    health = api(client, "/api/health")
    assert health["auth_required"] is True and health["authenticated"] is False
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    page = client.get("/login")
    assert page.status_code == 200 and "/api/login" in page.text and password not in page.text
    wrong = client.post("/api/login", json={"password": "nope"})
    assert wrong.status_code == 401 and "bm_auth" not in wrong.headers.get("set-cookie", "")
    ok = client.post("/api/login", json={"password": password})
    assert ok.status_code == 200 and password not in ok.text
    cookie = ok.headers["set-cookie"].lower()
    assert "bm_auth=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "max-age=2592000" in cookie
    assert password not in ok.headers["set-cookie"]
    assert client.get("/api/markets").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303
    secret_file = settings.path("db").parent / ".secret"
    assert secret_file.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    client.cookies.set("bm_auth", "forged")
    assert client.get("/api/markets").status_code == 401
    client.post("/api/login", json={"password": password})
    monkeypatch.setenv("APP_PASSWORD", "a new password")                # changing it logs everyone out
    assert client.get("/api/markets").status_code == 401
    out = client.post("/api/logout")
    assert out.status_code == 200 and 'bm_auth=""' in out.headers["set-cookie"]


def test_login_without_a_password_set(client):
    r = client.post("/api/login", json={"password": "anything"})
    assert r.status_code == 200 and r.json()["auth_required"] is False
    assert client.get("/login", follow_redirects=False).status_code == 303


# --- optional real-data timing (BLUEMOON_REAL_CREXI) ----------------------------------------------

def test_real_export_list_and_detail_are_fast(real_crexi, web_root, settings):
    """Runs only with BLUEMOON_REAL_CREXI set. Checks counts and timings only; prints no owner data."""
    conn = connect(settings=settings)
    import_file(conn, MARKET, real_crexi)
    compute_owners(conn, MARKET)
    compute_drive_times(conn, MARKET, provider="estimate", geocode=False)
    compute_scores(conn, MARKET)
    engine.run_auto_stages(conn, MARKET)
    conn.close()
    from bluemoon.web.app import app
    with TestClient(app) as c:
        api(c, f"/api/m/{MARKET}/properties", limit=100)                 # warm up
        t = time.perf_counter()
        body = api(c, f"/api/m/{MARKET}/properties", limit=100, view="everything")
        list_ms = (time.perf_counter() - t) * 1000
        t = time.perf_counter()
        api(c, f"/api/m/{MARKET}/properties/{body['items'][0]['id']}")
        detail_ms = (time.perf_counter() - t) * 1000
    assert body["total"] > 1000 and len(body["items"]) == 100
    assert list_ms < 500 and detail_ms < 150, (list_ms, detail_ms)
