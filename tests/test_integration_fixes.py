"""Integration fixes: DRILL buy-box scope and waiting counts, faster DRILL stats, summary origin, readable
photo answers, config documentation and plain-English step messages. Made-up data only, no network."""
from __future__ import annotations

import itertools

import pytest
import yaml

from bluemoon.db import market_by_slug, now_iso
from bluemoon.drill import engine as E
from bluemoon.drill import stats as S

MARKET = "apartments-springfield-oh"
_ids = itertools.count(1)


def add_prop(conn, market=MARKET, **kw) -> int:
    n = next(_ids)
    m = market_by_slug(conn, market)
    data = {
        "market_id": m["id"], "property_key": f"fix:{n}", "address": f"{200 + n} INVENTED ST", "city": "SPRINGFIELD",
        "state": "OH", "zip": "45503", "county": "CLARK", "apn": f"340-02-00-{n:03d}-000", "lat": 39.9 + n / 1000,
        "lng": -83.8, "units": 24, "property_type": "Multifamily", "owner_name": None,
        "crexi_url": f"https://www.crexi.com/property-records/fix-{n}",
        "raw_json": "{}", "flags_json": "[]", "created_at": now_iso(), "updated_at": now_iso(),
    }
    data.update(kw)
    conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                 tuple(data.values()))
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def set_score(conn, pid, score, in_buybox=1, rank=None):
    conn.execute("INSERT OR REPLACE INTO scores(property_id, in_buybox, exclusion_reasons, score, rank, computed_at) "
                 "VALUES(?,?,?,?,?,?)", (pid, in_buybox, "[]", score, rank, now_iso()))
    conn.commit()


@pytest.fixture
def board(conn):
    """Two buy-box owners, one set-aside owner and one unscored owner, through the automatic stages."""
    box_hi = add_prop(conn, owner_name="ALDER QUINN")
    box_lo = add_prop(conn, owner_name="BIRCH ROWAN")
    out = add_prop(conn, owner_name="CEDAR SAGE")
    unscored = add_prop(conn, owner_name="DOGWOOD ELLIS")
    set_score(conn, box_hi, 0.08, 1, 1)
    set_score(conn, box_lo, 0.03, 1, 2)
    set_score(conn, out, 0.09, 0, None)
    E.run_auto_stages(conn, MARKET)
    return {"box_hi": box_hi, "box_lo": box_lo, "out": out, "unscored": unscored}


# --- DRILL scope: buy box (default on the web) or everyone ---------------------------------------

def test_stage_queue_scope_buybox_leaves_out_set_aside_owners(conn, board):
    every = E.stage_queue(conn, MARKET, "county_auditor")
    assert every["scope"] == "all" and every["total"] == 4 and every["outside_scope"] == 0
    box = E.stage_queue(conn, MARKET, "county_auditor", scope="buybox")
    ids = [i["property_id"] for i in box["items"]]
    # Unscored owners stay in (missing data never excludes); the set-aside one is left out but counted.
    assert ids == [board["box_hi"], board["box_lo"], board["unscored"]]
    assert box["total"] == 3 and box["outside_scope"] == 1
    assert "set-aside" in box["message"]
    with pytest.raises(ValueError):
        E.stage_queue(conn, MARKET, "county_auditor", scope="nearby")


def test_validate_queue_scope(conn, board):
    E.add_contact(conn, board["out"], "phone", "937-555-0101")
    E.add_contact(conn, board["box_lo"], "phone", "937-555-0102")
    assert E.validate_queue(conn, MARKET)["total"] == 2
    box = E.stage_queue(conn, MARKET, "validate", scope="buybox")
    assert [i["property_id"] for i in box["items"]] == [board["box_lo"]] and box["scope"] == "buybox"


def test_overview_scope_counts_buybox_owners_only(conn, board):
    every = E.overview(conn, MARKET)
    box = E.overview(conn, MARKET, scope="buybox")
    assert every["scope"] == "all" and box["scope"] == "buybox"
    assert every["totals"]["properties"] == 4 and every["totals"]["all_properties"] == 4
    assert box["totals"]["properties"] == 3 and box["totals"]["all_properties"] == 4
    assert box["totals"]["outside_scope"] == 1
    q_all = {q["key"]: q["count"] for q in every["queues"]}
    q_box = {q["key"]: q["count"] for q in box["queues"]}
    assert q_all["county_auditor"] == 4 and q_box["county_auditor"] == 3
    # Penetration is the same either way: it always counts buy-box owners once scores exist.
    assert box["penetration_detail"] == every["penetration_detail"]


def test_overview_scope_without_scores_counts_everyone(conn):
    for name in ("ELM HARPER", "FIR MORGAN"):
        add_prop(conn, owner_name=name)
    E.run_auto_stages(conn, MARKET)
    ov = E.overview(conn, MARKET, scope="buybox")
    assert ov["scope"] == "all" and ov["totals"]["properties"] == 2


def test_waiting_counts_buybox_hands_on_work(conn, board):
    w = E.waiting_counts(conn, MARKET)
    assert w == {"total": 3, "validate": 0, "stages": {"county_auditor": 3}, "basis": "in_buybox"}
    E.add_contact(conn, board["box_hi"], "phone", "937-555-0103")
    E.add_contact(conn, board["out"], "phone", "937-555-0104")     # set aside: not counted
    w = E.waiting_counts(conn, MARKET)
    assert w["validate"] == 1 and w["stages"] == {"county_auditor": 2} and w["total"] == 3
    E.level_action(conn, board["box_lo"], "dnc")
    assert E.waiting_counts(conn, MARKET)["total"] == 2


def test_waiting_counts_empty_market(conn):
    assert E.waiting_counts(conn, "self-storage-springfield-oh") == {
        "total": 0, "validate": 0, "stages": {}, "basis": "all"}


# --- DRILL stats: same numbers, counted with SQL ------------------------------------------------

def test_drill_report_does_not_load_every_property(conn, board, monkeypatch):
    E.record_attempt(conn, board["box_hi"], "county_auditor", "nothing")
    E.record_attempt(conn, board["box_hi"], "county_auditor", "found")      # the latest one counts
    E.record_attempt(conn, board["box_lo"], "county_auditor", "nothing")
    E.record_attempt(conn, board["out"], "county_auditor", "skipped")
    pid = board["box_lo"]
    cid = E.add_contact(conn, pid, "phone", "937-555-0105")["contact"]["id"]
    E.log_outreach(conn, pid, "phone", "no_answer", contact_point_id=cid)
    E.log_outreach(conn, pid, "phone", "reached_confirmed", contact_point_id=cid)

    def boom(*a, **k):
        raise AssertionError("drill_report must not load every property")
    monkeypatch.setattr(E, "load_contexts", boom)
    rep = S.drill_report(conn, MARKET)
    ca = {r["owner_type"]: r for r in rep["by_owner_type"] if r["stage_key"] == "county_auditor"}
    assert ca["all"]["tried"] == 2 and ca["all"]["found"] == 1 and ca["all"]["skipped"] == 1
    assert ca["unknown"]["tried"] == 2            # no owners row yet: counted as unknown owner type
    summary = {r["stage_key"]: r for r in rep["stages"]}
    assert summary["county_auditor"]["tried"] == 2 and summary["county_auditor"]["rate"] == 0.5
    phone = {r["channel"]: r for r in rep["outreach"]}["phone"]
    assert phone["logged"] == 2 and phone["confirmed"] == 1 and phone["confirm_rate"] == 0.5


def test_drill_stats_ignore_new_pass_markers(conn, board):
    pid = board["box_hi"]
    E.record_attempt(conn, pid, "county_auditor", "found")
    E.level_action(conn, pid, "exhausted")
    E.level_action(conn, pid, "reopen")          # writes a new-pass marker row
    keys = {r["stage_key"] for r in S.stage_hit_rates(conn, MARKET)}
    assert E.PASS_MARKER not in keys


# --- web: summary origin + drill_waiting, scoped DRILL routes ----------------------------------

@pytest.fixture
def client(settings, board):
    from fastapi.testclient import TestClient

    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def test_summary_has_origin_and_drill_waiting(client, settings):
    s = client.get(f"/api/m/{MARKET}/summary").json()
    origin = settings.market(MARKET).origin
    assert s["market"]["origin_lat"] == origin.lat and s["market"]["origin_lng"] == origin.lng
    assert s["market"]["origin_label"] == "Downtown Springfield, OH"      # the quoted label keeps ", OH"
    assert s["drill_waiting"] == 3 and s["drill_waiting_detail"]["stages"] == {"county_auditor": 3}
    empty = client.get("/api/m/self-storage-springfield-oh/summary").json()
    assert empty["drill_waiting"] == 0 and empty["market"]["origin_lat"] == origin.lat


def test_drill_routes_default_to_buybox_scope(client):
    board = client.get(f"/api/m/{MARKET}/drill").json()
    assert board["scope"] == "buybox" and board["totals"]["properties"] == 3
    assert client.get(f"/api/m/{MARKET}/drill", params={"scope": "all"}).json()["totals"]["properties"] == 4
    q = client.get(f"/api/m/{MARKET}/drill/queue/county_auditor").json()
    assert q["scope"] == "buybox" and q["total"] == 3
    assert client.get(f"/api/m/{MARKET}/drill/queue/county_auditor", params={"scope": "all"}).json()["total"] == 4
    assert client.get(f"/api/m/{MARKET}/drill", params={"scope": "nearby"}).status_code in (400, 422)


def test_drill_stages_route_is_fast_enough(client):
    import time
    t = time.perf_counter()
    r = client.get(f"/api/m/{MARKET}/drill/stages")
    assert r.status_code == 200 and time.perf_counter() - t < 1.0


# --- readable photo answers -----------------------------------------------------------------------

def test_save_review_message_uses_readable_label(conn, settings):
    from bluemoon.imagery.review import readable_label, save_review
    pid = add_prop(conn)
    res = save_review(conn, pid, "EXTERIOR_CORRIDOR")
    assert res["message"].startswith("Saved: Exterior corridor.") and "EXTERIOR_CORRIDOR" not in res["message"]
    assert res["label"] == "EXTERIOR_CORRIDOR" and res["label_name"] == "Exterior corridor"
    ac = settings.asset_class("warehouse")
    assert readable_label("DOCK_HIGH", ac) == "Dock high" and readable_label(None) == ""
    ac.visual_check["label_names"] = {"DOCK_HIGH": "Dock-high doors"}
    assert readable_label("DOCK_HIGH", ac) == "Dock-high doors"


# --- config documents the dashboard keys ----------------------------------------------------------

def test_config_yaml_lists_web_defaults_and_quotes_origin_labels():
    from pathlib import Path

    from bluemoon.web.queries import WEB_DEFAULTS
    raw = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
    assert raw["web"] == WEB_DEFAULTS
    for m in raw["markets"]:
        assert set(m["origin"]) == {"label", "lat", "lng"}, m["slug"]   # an unquoted ', OH' would add a key


def test_every_photo_check_has_a_target_name(settings):
    for key, ac in settings.asset_classes.items():
        if ac.visual_enabled:
            assert ac.visual_check.get("target_name"), key


# --- plain-English step messages ------------------------------------------------------------------

def test_owner_message_uses_readable_type_names(conn, board):
    from bluemoon.enrich.owners import compute_owners
    msg = compute_owners(conn, MARKET)["message"]
    assert "small llc" not in msg and "Checked the owners of 4 properties" in msg


def test_photo_check_with_nothing_to_check_says_what_to_do(conn, settings):
    from bluemoon.imagery.vision import MockBackend, classify_properties
    res = classify_properties(conn, MARKET, backend=MockBackend())
    assert res["classified"] == 0 and "nothing new to check" in res["message"] and "Photos" in res["message"]


def test_config_local_yaml_wins_over_config_yaml(tmp_path):
    import shutil
    from bluemoon.config import REPO_ROOT, Settings
    shutil.copy(REPO_ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    (tmp_path / "config.local.yaml").write_text("share:\n  provider: ngrok\n  ngrok_domain: x.ngrok-free.dev\nsafety:\n  confirm_over_properties: 7\n")
    s = Settings(root=tmp_path)
    assert s.raw["share"]["provider"] == "ngrok" and s.raw["safety"]["confirm_over_properties"] == 7
    assert s.raw["safety"]["confirm_clears_over"] == 10      # the rest of config.yaml still applies
