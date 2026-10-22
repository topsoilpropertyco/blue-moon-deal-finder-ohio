"""Buy-box decisions made by hand and the set-aside groups of the Why? panel.

The made-up fixture export plus a few more made-up rows, so every kind of group has members: another state,
another property type, old buildings, tiny and big ones, and long drives. No network, no keys, no real data.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from bluemoon import db as dbm
from bluemoon import exports
from bluemoon.db import connect, market_by_slug, rows, scalar
from bluemoon.drill import credits as C
from bluemoon.drill import engine
from bluemoon.enrich.drive import compute_drive_times
from bluemoon.enrich.owners import compute_owners
from bluemoon.ingest import import_file
from bluemoon.scoring import MANUAL_REASON, SET_ASIDE_REASONS, apply_overrides, buckets, overrides
from bluemoon.scoring.rank import compute_scores
from bluemoon.web import queries

MARKET = "apartments-springfield-oh"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Extra made-up rows: (apn, address, city, state, county, type, units, year, lat, lng, owner)
EXTRA = [
    ("KY-0001", "1 BLUEGRASS WAY", "FLORENCE", "KY", "BOONE", "Multifamily", 20, 1985, 38.99, -84.63, "BLUEGRASS TEST LLC"),
    ("RT-0001", "2 SHOPFRONT ST", "SPRINGFIELD", "OH", "CLARK", "Retail", 20, 1985, 39.93, -83.81, "SHOP TEST LLC"),
    ("YR-1955", "3 FIFTIES LN", "SPRINGFIELD", "OH", "CLARK", "Multifamily", 20, 1955, 39.92, -83.80, "FIFTIES TEST TRUST"),
    ("YR-1930", "4 THIRTIES LN", "SPRINGFIELD", "OH", "CLARK", "Multifamily", 20, 1930, 39.92, -83.79, "THIRTIES TEST LLC"),
    ("YR-1890", "5 OLDEN LN", "SPRINGFIELD", "OH", "CLARK", "Multifamily", 20, 1890, 39.91, -83.80, "OLDEN TEST LLC"),
    ("SZ-0003", "6 TINY CT", "SPRINGFIELD", "OH", "CLARK", "Multifamily", 3, 1980, 39.93, -83.79, "TINY TEST JANE"),
    ("SZ-0090", "7 MIDRISE AVE", "SPRINGFIELD", "OH", "CLARK", "Multifamily", 90, 1980, 39.94, -83.80, "MIDRISE TEST LLC"),
    ("SZ-0150", "8 LARGE AVE", "SPRINGFIELD", "OH", "CLARK", "Multifamily", 150, 1980, 39.94, -83.81, "LARGE TEST LLC"),
    ("DR-0121", "9 FARTHEST RD", "TOLEDO", "OH", "LUCAS", "Multifamily", 20, 1985, 41.65, -83.54, "FARTHEST TEST LLC"),
    ("DR-0060", "10 EDGE OF RANGE RD", "SPRINGFIELD", "OH", "CLARK", "Multifamily", 20, 1985, 39.95, -83.82, "EDGE TEST JOHN"),
]
DRIVE_MINUTES = {"DR-0121": 140.0, "DR-0060": 60.4}    # set by hand after the free estimates, to be exact


def set_config(root: Path, dotted: str, value) -> None:
    path = root / "config.yaml"
    data = yaml.safe_load(path.read_text())
    node = data
    *parents, last = dotted.split(".")
    for key in parents:
        node = node.setdefault(key, {})
    node[last] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def extended_csv(tmp_path: Path) -> Path:
    """The fixture export plus the EXTRA rows (same columns)."""
    with (FIXTURES / "crexi_sample.csv").open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        base = list(reader)
    template = dict(base[0])
    out = list(base)
    for apn, address, city, state, county, ptype, units, year, lat, lng, owner in EXTRA:
        row = dict(template)
        row.update({"APN": apn, "Address": address, "City": city, "State": state, "County": county,
                    "Property Type": ptype, "Number of Units": str(units), "Number of Addresses": str(units),
                    "Building SqFt": str(units * 850), "Year Built": str(year), "Latitude": str(lat),
                    "Longitude": str(lng), "Owner Name": owner, "Mailing Address": address, "Mailing Address City": city,
                    "Mailing Address State": state, "Property Name": f"{address} {city} {state}",
                    "Property Link": f"https://www.crexi.com/property-records/test-{apn.lower()}"})
        out.append(row)
    path = tmp_path / "crexi_extended.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(out)
    return path


def pid_of(conn, apn: str) -> int:
    return int(scalar(conn, "SELECT id FROM properties WHERE apn = ?", (apn,)))


def build(conn, path: Path) -> None:
    import_file(conn, MARKET, path)
    compute_owners(conn, MARKET)
    compute_drive_times(conn, MARKET, provider="estimate", geocode=False)
    for apn, minutes in DRIVE_MINUTES.items():
        conn.execute("UPDATE drive_times SET minutes = ? WHERE property_id = ?", (minutes, pid_of(conn, apn)))
    conn.commit()
    compute_scores(conn, MARKET)
    engine.run_auto_stages(conn, MARKET)


@pytest.fixture
def web_root(tmp_root):
    set_config(tmp_root, "web.rescore_delay_seconds", 0)
    return tmp_root


@pytest.fixture
def extended(web_root, tmp_path) -> Path:
    return extended_csv(tmp_path)


@pytest.fixture
def built(web_root, settings, extended):
    conn = connect(settings=settings)
    build(conn, extended)
    yield conn
    conn.close()


@pytest.fixture
def client(built):
    from bluemoon.web.app import app
    with TestClient(app) as c:
        yield c


def api(c: TestClient, path: str, **params) -> dict:
    r = c.get(path, params=params)
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    return r.json()


def post(c: TestClient, path: str, body: dict, status: int = 200) -> dict:
    r = c.post(path, json=body)
    assert r.status_code == status, (path, r.status_code, r.text[:400])
    return r.json()


def set_aside_info(conn) -> dict:
    return queries.set_aside(conn, queries.market_ctx(conn, MARKET))


def groups_of(info: dict) -> dict[str, dict]:
    return {b["key"]: b for r in info["reasons"] for b in r["buckets"]}


def check_invariant(conn) -> None:
    for r in rows(conn, "SELECT property_id, in_buybox, exclusion_reasons, rank FROM scores"):
        reasons = json.loads(r["exclusion_reasons"])
        assert bool(r["in_buybox"]) == (reasons == []), r
        assert (r["rank"] is not None) == bool(r["in_buybox"]), r


# --- the pure parts ---------------------------------------------------------------------------------

def test_apply_overrides_truth_table():
    rules = {"in_buybox": False, "exclusion_reasons": ["drive_time", "size"],
             "exclusion_details": {"drive_time": "about 82 min", "size": "6 units"}}
    ev = apply_overrides(rules, {})
    assert ev["in_buybox"] is False and ev["exclusion_reasons"] == ["drive_time", "size"] and ev["manual"] is None
    ev = apply_overrides(rules, {"drive_time": "include"})
    assert ev["in_buybox"] is False and ev["exclusion_reasons"] == ["size"] and ev["waived"] == ["drive_time"]
    assert ev["rule_reasons"] == ["drive_time", "size"] and ev["manual"] == "waived"
    ev = apply_overrides(rules, {"drive_time": "include", "size": "include"})
    assert ev["in_buybox"] is True and ev["exclusion_reasons"] == []
    ev = apply_overrides(rules, {"*": {"decision": "include", "decided_by": "this Mac"}})
    assert ev["in_buybox"] is True and ev["exclusion_reasons"] == [] and ev["manual"] == "include"
    ev = apply_overrides(rules, {"drive_time": "keep"})
    assert ev["in_buybox"] is False and ev["kept"] == ["drive_time"]
    passing = {"in_buybox": True, "exclusion_reasons": [], "exclusion_details": {}}
    ev = apply_overrides(passing, {"*": {"decision": "exclude", "decided_by": "pat@example.com",
                                         "decided_at": "2026-10-21T10:00:00+00:00", "note": "roof is gone"}})
    assert ev["in_buybox"] is False and ev["exclusion_reasons"] == [MANUAL_REASON] and ev["rules_in_buybox"]
    assert ev["exclusion_details"]["manual"] == "by pat@example.com on 2026-10-21: roof is gone"
    assert apply_overrides(ev, {})["exclusion_reasons"] == []      # safe to apply again: the rules said yes


def test_group_definitions_follow_the_yaml(settings):
    ac = settings.asset_class("apartments")
    spec = buckets.group_spec({"max_drive_minutes": 60}, ac, settings)
    assert [r.id for r in spec.drive] == ["61-75", "76-90", "91-120", "121-up"]
    assert [r.label for r in spec.drive] == ["61–75 min", "76–90 min", "91–120 min", "Over 120 min"]
    assert [r.id for r in spec.size_below + spec.size_above] == ["1-4", "5-9", "81-100", "101-200", "201-up"]
    assert spec.labels["size:1-4"] == "1–4 units" and spec.labels["size:201-up"] == "201+ units"
    assert [r.id for r in spec.years] == ["1960s", "1950s", "1940s", "1900-1939", "before-1900"]
    # Edges at or under the market's limit are skipped.
    assert [r.id for r in buckets.group_spec({"max_drive_minutes": 75}, ac, settings).drive] == \
        ["76-90", "91-120", "121-up"]
    # Every asset class gets groups that cover everything outside its ok range, with keys the API accepts.
    for key, a in settings.asset_classes.items():
        sp = buckets.group_spec({"max_drive_minutes": 60}, a, settings)
        assert sp.size_below and sp.size_above, key
        assert sp.size_below[0].lo is None and sp.size_above[-1].hi is None, key
        for k in sp.labels:
            assert buckets.BUCKET_KEY.match(k), k
    ev = {"drive_minutes": 60.4, "size_value": 4.0}
    assert buckets.group_id("drive_time", {}, ev, spec) == "61-75"          # rounds to 60: the first group
    assert buckets.group_id("drive_time", {}, {"drive_minutes": 90.2}, spec) == "76-90"   # shown as "90 min"
    # Halves round up, as the screens show them: 90.5 shows as "91 min", so it is in 91-120 (not 76-90).
    assert buckets.group_id("drive_time", {}, {"drive_minutes": 90.5}, spec) == "91-120"
    assert buckets.group_id("drive_time", {}, {"drive_minutes": 120.5}, spec) == "121-up"
    assert buckets.group_id("drive_time", {}, {"drive_minutes": 90.4}, spec) == "76-90"
    assert buckets.group_id("drive_time", {}, {"drive_minutes": None}, spec) == "other"
    assert buckets.group_id("size", {}, ev, spec) == "1-4"
    assert buckets.group_id("size", {}, {"size_value": 81}, spec) == "81-100"
    assert buckets.group_id("year", {"year_built": 1969}, {}, spec) == "1960s"
    assert buckets.group_id("skip_county", {"state": "oh", "county": "Van Wert County"}, {}, spec) == "oh-van-wert"
    assert buckets.label_for("skip_county:oh-van-wert", spec) == "Van Wert County, OH"
    with pytest.raises(ValueError):
        buckets.parse_key("drive_time:76-90; DROP TABLE scores")
    with pytest.raises(ValueError):
        buckets.parse_key("nonsense:1-2")


# --- the database ------------------------------------------------------------------------------------

def test_old_database_is_migrated_once_and_rescored(web_root, settings, extended):
    conn = connect(settings=settings)
    build(conn, extended)
    # Make it look like a database from before version 2.
    conn.execute("ALTER TABLE scores DROP COLUMN rule_reasons")
    conn.execute("ALTER TABLE scores DROP COLUMN aside_buckets")
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    path = settings.path("db")
    conn = connect(path, settings=settings)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scores)")}
    assert {"rule_reasons", "aside_buckets"} <= cols
    assert scalar(conn, "SELECT value FROM meta WHERE key = 'schema_version'") == str(dbm.SCHEMA_VERSION) == "2"
    market_id = market_by_slug(conn, MARKET)["id"]
    assert dbm.pending_rescore(conn) == {market_id}
    # Old rows read as before (rule reasons = the reasons), and they count as not grouped yet.
    info = set_aside_info(conn)
    assert info["total"] > 0 and all(b["key"].endswith(":other") for r in info["reasons"] for b in r["buckets"])
    conn.close()
    conn = connect(path, settings=settings)     # connecting again changes nothing
    assert dbm.pending_rescore(conn) == {market_id}
    queries.summary(conn, MARKET)               # the summary re-scores the market once
    assert dbm.pending_rescore(conn) == set()
    assert scalar(conn, "SELECT COUNT(*) FROM scores WHERE rule_reasons IS NULL OR aside_buckets IS NULL") == 0
    assert not any(b["key"].endswith(":other") for b in groups_of(set_aside_info(conn)).values())
    conn.close()


def test_override_table_refuses_mixed_up_decisions(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO buybox_overrides(property_id, reason, decision, decided_by, decided_at) "
                     "VALUES(1, 'drive_time', 'exclude', 'cli', '2026-10-21')")


# --- the groups --------------------------------------------------------------------------------------

def test_every_reason_adds_up_to_its_groups(built):
    info = set_aside_info(built)
    keys = set(groups_of(info))
    assert {"state:ky", "property_type:retail", "drive_time:61-75", "drive_time:76-90", "drive_time:91-120",
            "drive_time:121-up", "year:1960s", "year:1950s", "year:1900-1939", "year:before-1900", "size:1-4",
            "size:5-9", "size:81-100", "size:101-200", "size:201-up", "skip_county:oh-fairfield"} <= keys
    for r in info["reasons"]:
        for f in ("count", "only", "still_aside", "added", "kept"):
            assert r[f] == sum(b[f] for b in r["buckets"]), (r["key"], f)
        assert r["explain"]
        for b in r["buckets"]:
            assert buckets.BUCKET_KEY.match(b["key"]) and b["label"] and b["label"] != buckets.OTHER_LABEL
    assert info["total"] == scalar(built, "SELECT COUNT(*) FROM scores WHERE in_buybox = 0")
    reasons = {r["key"]: r for r in info["reasons"]}
    assert reasons["drive_time"]["label"] == "More than 60 min drive"
    skip = reasons["skip_county"]["explain"]
    assert "Clermont" in skip and "Fairfield" in skip and "Dearborn" in skip
    assert "1 of these is also over the drive limit" in skip
    assert groups_of(info)["property_type:retail"]["label"] == "Retail"
    assert info["by_hand"] == {"added": 0, "set_aside": 0}


def test_review_list_total_matches_every_group(client):
    info = api(client, f"/api/m/{MARKET}/summary")["set_aside"]
    for r in info["reasons"]:
        for b in r["buckets"]:
            got = api(client, f"/api/m/{MARKET}/properties", bucket=b["key"], limit=500)
            assert got["total"] == b["count"] == len(got["items"]), b["key"]
            assert all(b["key"] in i["aside_buckets"] for i in got["items"])
            review = got["review"]
            assert review["bucket"] == b["key"] and review["count"] == b["count"] and review["reason"] == r["key"]
            assert review["label"] == b["label"] and review["explain"] == r["explain"]


def test_bad_group_is_refused(client):
    for bad in ("drive_time", "Drive_Time:76-90", "drive_time:76-90'--", "nonsense:1-2"):
        r = client.get(f"/api/m/{MARKET}/properties", params={"bucket": bad})
        assert r.status_code == 400, bad
        assert "group" in r.json()["detail"]
        r = client.post(f"/api/m/{MARKET}/buybox/decide", json={"bucket": bad, "decision": "add_only"})
        assert r.status_code == 400, bad
    r = client.post(f"/api/m/{MARKET}/buybox/decide", json={"bucket": "drive_time:61-75", "decision": "eat"})
    assert r.status_code == 400


# --- deciding a whole group ----------------------------------------------------------------------------

def test_add_only_add_all_keep_clear_and_undo(client, built):
    group = "drive_time:61-75"
    members = set(overrides.group_property_ids(built, market_by_slug(built, MARKET)["id"], group))
    # The group mixes drive-only properties with ones that also fail other rules (the made-up 240- and 6-unit ones).
    multi = {r["property_id"] for r in rows(built, "SELECT property_id, rule_reasons FROM scores")
             if r["property_id"] in members and len(json.loads(r["rule_reasons"])) > 1}
    assert members and multi and multi < members
    before = api(client, f"/api/m/{MARKET}/summary")["in_buybox"]

    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "add_only"})
    assert res["changed"] == len(members) and res["now_in"] == len(members) - len(multi)
    assert {x["reason"] for x in res["still_out"]} >= {"size"} and "stay set aside" in res["message"]
    assert res["in_buybox"] == before + len(members) - len(multi) and res["batch_id"]
    entry = groups_of(res["set_aside"])[group]
    assert entry["count"] == len(members) and entry["added"] == res["now_in"]
    assert entry["still_aside"] == len(multi)
    check_invariant(built)
    first_batch = res["batch_id"]

    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "add_all", "note": "Brad wants these"})
    assert res["now_in"] == len(members) and res["still_out"] == []
    assert groups_of(res["set_aside"])[group]["added"] == len(members)
    ranked = {r["property_id"] for r in rows(built, "SELECT property_id FROM scores WHERE rank IS NOT NULL")}
    assert members <= ranked                    # ranked by score among the buy box at once
    check_invariant(built)

    res = post(client, f"/api/m/{MARKET}/buybox/undo", {"batch_id": res["batch_id"]})
    assert res["now_in"] == len(members) - len(multi) and res["changed"] == len(members)

    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "keep"})
    entry = groups_of(res["set_aside"])[group]
    assert entry["kept"] == len(members) and entry["still_aside"] == len(members) and res["now_in"] == 0

    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "clear"})
    assert res["now_in"] == 0 and "rules decide again" in res["message"]
    assert scalar(built, "SELECT COUNT(*) FROM buybox_overrides") == 0
    assert api(client, f"/api/m/{MARKET}/summary")["in_buybox"] == before

    # The very first batch can still be undone: its decisions are cleared again (nothing to put back).
    res = post(client, f"/api/m/{MARKET}/buybox/undo", {"batch_id": first_batch})
    assert res["changed"] == 0 and "Nothing to undo" in res["message"]
    r = client.post(f"/api/m/{MARKET}/buybox/undo", json={"batch_id": "nope-123"})
    assert r.status_code == 404
    check_invariant(built)
    log = rows(built, "SELECT DISTINCT actor FROM buybox_override_log")
    assert [x["actor"] for x in log] == ["open"]     # no password set: auth.signed_in_as() says 'open'


def test_skip_county_group_add_only_keeps_the_drive_rule(client, built):
    group = "skip_county:oh-fairfield"
    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "add_only"})
    assert res["now_in"] == 0 and res["still_out"] == [{"reason": "drive_time", "label": "Too far to drive", "count": 1}]
    # The waiver shows in the Why? panel and the Review header, so it can be seen and cleared there.
    entry = groups_of(res["set_aside"])[group]
    assert entry["waived"] == 1 and entry["added"] == 0 and entry["kept"] == 0 and entry["still_aside"] == 1
    assert next(r for r in res["set_aside"]["reasons"] if r["key"] == "skip_county")["waived"] == 1
    assert api(client, f"/api/m/{MARKET}/properties", bucket=group)["review"]["waived"] == 1
    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "clear"})
    assert res["changed"] == 1 and groups_of(res["set_aside"])[group]["waived"] == 0
    assert scalar(built, "SELECT COUNT(*) FROM buybox_overrides") == 0


def test_keep_after_add_all_takes_the_group_back_out(client, built):
    group = "drive_time:61-75"
    members = set(overrides.group_property_ids(built, market_by_slug(built, MARKET)["id"], group))
    before = api(client, f"/api/m/{MARKET}/summary")["in_buybox"]
    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "add_all"})
    assert res["now_in"] == len(members)

    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "keep"})
    assert res["now_in"] == 0 and res["in_buybox"] == before
    assert res["message"].startswith(f"Kept {len(members)} in ") and "set aside" in res["message"]
    entry = groups_of(res["set_aside"])[group]
    assert entry["added"] == 0 and entry["kept"] == len(members) and entry["still_aside"] == len(members)
    assert scalar(built, "SELECT COUNT(*) FROM buybox_overrides WHERE reason = '*'") == 0
    check_invariant(built)

    # One Undo puts the 'Add all' back (both parts of the click were one batch).
    res = post(client, f"/api/m/{MARKET}/buybox/undo", {"batch_id": res["batch_id"]})
    assert res["now_in"] == len(members) and "back the way they were" in res["message"]
    assert scalar(built, "SELECT COUNT(*) FROM buybox_overrides WHERE decision = 'keep'") == 0

    # A member added by hand another way (the drawer) stays in, and the message says so.
    post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "clear"})
    one_of = min(members)
    post(client, f"/api/m/{MARKET}/properties/{one_of}/buybox", {"decision": "include"})
    res = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "keep"})
    assert res["now_in"] == 1 and "is still in the buy box because you added it by hand another way" in res["message"]
    assert overrides.for_property(built, one_of)["*"]["decision"] == "include"
    check_invariant(built)


def test_undo_leaves_a_newer_decision_alone(client, built):
    far = pid_of(built, "DR-0121")
    first = post(client, f"/api/m/{MARKET}/properties/{far}/buybox", {"decision": "exclude"})
    post(client, f"/api/m/{MARKET}/properties/{far}/buybox", {"decision": "include", "note": "Brad wants it"})
    res = post(client, f"/api/m/{MARKET}/buybox/undo", {"batch_id": first["batch_id"]})
    assert res["changed"] == 0 and "Nothing to undo" in res["message"]
    row = overrides.for_property(built, far)["*"]
    assert row["decision"] == "include" and row["note"] == "Brad wants it"
    assert scalar(built, "SELECT in_buybox FROM scores WHERE property_id = ?", (far,)) == 1

    # A group's Undo puts back the rest and says which one it left alone.
    group = "drive_time:61-75"
    members = sorted(overrides.group_property_ids(built, market_by_slug(built, MARKET)["id"], group))
    added = post(client, f"/api/m/{MARKET}/buybox/decide", {"bucket": group, "decision": "add_all"})
    post(client, f"/api/m/{MARKET}/properties/{members[0]}/buybox", {"decision": "exclude", "note": "bad roof"})
    res = post(client, f"/api/m/{MARKET}/buybox/undo", {"batch_id": added["batch_id"]})
    assert res["changed"] == len(members) - 1
    assert f"Undone for {len(members) - 1} " in res["message"] and "1 was left alone" in res["message"]
    row = overrides.for_property(built, members[0])["*"]
    assert row["decision"] == "exclude" and row["note"] == "bad roof"
    assert scalar(built, "SELECT COUNT(*) FROM buybox_overrides WHERE property_id != ?", (far,)) == 1
    check_invariant(built)


# --- one property ----------------------------------------------------------------------------------

def test_one_property_include_exclude_rules(client, built):
    inside = int(scalar(built, "SELECT property_id FROM scores WHERE rank = 1"))
    res = post(client, f"/api/m/{MARKET}/properties/{inside}/buybox", {"decision": "exclude", "note": "Seen it"})
    assert res["row"]["in_buybox"] is False and res["row"]["by_hand"] == "aside"
    assert res["row"]["exclusion_reasons"] == ["manual"] and res["row"]["rule_reasons"] == []
    assert res["row"]["rank"] is None and "manual:by-hand" in res["row"]["aside_buckets"]
    bb = res["buybox"]
    assert bb["manual"] == "exclude" and bb["decisions"][0]["decided_by"] == "open"
    assert bb["decisions"][0]["note"] == "Seen it" and bb["history"][0]["decision"] == "exclude"
    assert bb["exclusions"][0]["label"] == "Set aside by hand" and "by open on" in bb["exclusions"][0]["detail"]
    summary = api(client, f"/api/m/{MARKET}/summary")["set_aside"]
    assert summary["by_hand"]["set_aside"] == 1
    manual = next(r for r in summary["reasons"] if r["key"] == "manual")
    assert manual["count"] == 1 and manual["buckets"][0]["key"] == "manual:by-hand"
    listed = api(client, f"/api/m/{MARKET}/properties", bucket="manual:by-hand")
    assert [i["id"] for i in listed["items"]] == [inside]

    far = pid_of(built, "DR-0121")
    res = post(client, f"/api/m/{MARKET}/properties/{far}/buybox", {"decision": "include", "reason": "drive_time"})
    assert res["in_buybox"] is True and res["row"]["by_hand"] == "added" and res["row"]["rank"] is not None
    assert res["buybox"]["waived"] == ["drive_time"] and res["buybox"]["manual"] == "waived"
    assert res["buybox"]["decisions"][0]["detail_at_decision"].startswith("about 140 min")
    detail = api(client, f"/api/m/{MARKET}/properties/{far}")
    assert detail["in_buybox"] is True and detail["buybox"]["in_buybox"] is True
    assert detail["buybox"]["exclusion_reasons"] == [] and detail["buybox"]["rule_reasons"] == ["drive_time"]

    r = client.post(f"/api/m/{MARKET}/properties/{far}/buybox", json={"decision": "exclude", "reason": "size"})
    assert r.status_code == 400 and "whole property" in r.json()["detail"]

    res = post(client, f"/api/m/{MARKET}/properties/{inside}/buybox", {"decision": "rules"})
    assert res["row"]["in_buybox"] is True and res["row"]["by_hand"] is None and "Back to the rules" in res["message"]
    res = post(client, f"/api/m/{MARKET}/properties/{far}/buybox", {"decision": "rules"})
    assert res["row"]["in_buybox"] is False and res["buybox"]["decisions"] == []
    assert len(res["buybox"]["history"]) == 2       # decided, then cleared: the history stays
    check_invariant(built)


def test_decisions_survive_rescoring_and_reimporting(client, built, extended):
    far = pid_of(built, "DR-0121")
    inside = int(scalar(built, "SELECT property_id FROM scores WHERE rank = 1"))
    post(client, f"/api/m/{MARKET}/properties/{far}/buybox", {"decision": "include"})
    post(client, f"/api/m/{MARKET}/properties/{inside}/buybox", {"decision": "exclude"})
    import_file(built, MARKET, extended)          # the same file again: same property ids
    compute_scores(built, MARKET)
    got = {r["property_id"]: r for r in rows(built, "SELECT * FROM scores WHERE property_id IN (?, ?)", (far, inside))}
    assert got[far]["in_buybox"] == 1 and json.loads(got[far]["rule_reasons"]) == ["drive_time"]
    assert got[inside]["in_buybox"] == 0 and json.loads(got[inside]["exclusion_reasons"]) == ["manual"]
    assert scalar(built, "SELECT COUNT(*) FROM buybox_overrides") == 2
    check_invariant(built)


def test_a_decision_makes_scores_stale_for_the_photo_step(built):
    from bluemoon import pipeline
    assert not pipeline._scores_stale(built, MARKET)
    overrides.set_decision(built, [pid_of(built, "DR-0121")], "*", "include", overrides.CLI_ACTOR)
    built.execute("UPDATE buybox_override_log SET created_at = '2999-01-01T00:00:00+00:00'")
    built.commit()
    assert pipeline._scores_stale(built, MARKET)


# --- what the rest of the app does with them -------------------------------------------------------

def test_credits_never_go_to_a_property_set_aside_by_hand(client, built):
    def planned() -> dict[int, str]:
        return {c["ctx"].id: c["tier"] for c in C._schedule(built).plan}

    before = planned()
    inside = next(pid for pid, tier in before.items() if tier == "main")
    assert any(tier == "set_aside" for tier in before.values())    # set-aside owners get fallback credits
    post(client, f"/api/m/{MARKET}/properties/{inside}/buybox", {"decision": "exclude"})
    after = planned()
    assert inside not in after
    assert any(tier == "set_aside" for tier in after.values())


def test_learning_uses_the_buy_box_with_decisions(built, settings):
    from bluemoon.scoring import learn
    far = pid_of(built, "DR-0121")
    markets = [market_by_slug(built, MARKET)]
    ac = settings.asset_class("apartments")
    assert learn._buckets_by_property(built, markets, ac, settings, None)[far][0] is False
    overrides.set_decision(built, [far], "*", "include", overrides.CLI_ACTOR)
    assert learn._buckets_by_property(built, markets, ac, settings, None)[far][0] is True


def test_filter_funnel_ends_with_the_ones_set_aside_by_hand(client, built):
    inside = int(scalar(built, "SELECT property_id FROM scores WHERE rank = 1"))
    post(client, f"/api/m/{MARKET}/properties/{inside}/buybox", {"decision": "exclude"})
    ff = api(client, f"/api/m/{MARKET}/analytics")["filter_funnel"]
    assert ff[-1]["key"] == "manual" and ff[-1]["removed"] == 1 and ff[-1]["label"] == "Not set aside by hand"
    assert ff[-1]["count"] == api(client, f"/api/m/{MARKET}/summary")["in_buybox"]


def test_exports_show_decisions_and_the_by_hand_file(client, built, tmp_path):
    far = pid_of(built, "DR-0121")
    inside = int(scalar(built, "SELECT property_id FROM scores WHERE rank = 1"))
    post(client, f"/api/m/{MARKET}/properties/{far}/buybox", {"decision": "include", "note": "Brad asked"})
    post(client, f"/api/m/{MARKET}/properties/{inside}/buybox", {"decision": "exclude"})
    files = exports.export_market(built, MARKET, tmp_path / "out", stamp_handoffs=False)
    assert "excluded_manual.csv" in files and list(files) == list(exports.EXPORT_FILES)

    def read(name: str) -> list[dict]:
        with files[name].open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    manual = read("excluded_manual.csv")
    assert [int(r["Property ID"]) for r in manual] == [inside]
    assert manual[0]["Reason"].startswith("Set aside by hand (by open on ")
    ranked = read("ranked_list.csv")
    header = list(ranked[0])
    at = header.index("Why set aside")
    assert header[at + 1:at + 6] == ["Buy box decision", "Rules said", "Decided by", "Decided on", "Set-aside groups"]
    by_id = {int(r["Property ID"]): r for r in ranked}
    assert by_id[far]["Buy box decision"] == "Added by hand" and by_id[far]["Rules said"] == "Too far to drive"
    assert by_id[far]["In buy box"] == "Yes" and by_id[far]["Rank"] and by_id[far]["Decided by"] == "open"
    assert by_id[far]["Set-aside groups"] == "Too far to drive: Over 120 min" and by_id[far]["Decided on"]
    assert by_id[inside]["Buy box decision"] == "Set aside by hand" and by_id[inside]["In buy box"] == "No"
    assert by_id[inside]["Rules said"] == "Fits the buy box"
    assert far not in {int(r["Property ID"]) for r in read("excluded_drive_time.csv")}
    md = files["run_summary.md"].read_text(encoding="utf-8")
    assert "Decided by hand: 1 added to the buy box, 1 set aside" in md and "Set aside by hand: 1" in md
    assert set(SET_ASIDE_REASONS) == {n[len("excluded_"):-4] for n in exports.EXCLUDED_FILES}
