"""Tests for bluemoon.enrich.drive (drive times) and bluemoon.enrich.geo (geocoding).

No network: Google and Census are faked with httpx.MockTransport. Addresses here are made up or
public landmarks. Live smoke checks of the free Census geocoder run only with BLUEMOON_LIVE=1.
The optional real-export test runs only with BLUEMOON_REAL_CREXI set and checks counts only.
"""
from __future__ import annotations

import email
import json
import logging
import os
import threading
import time
from urllib.parse import parse_qs

import httpx
import pytest

from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.enrich import drive, geo
from bluemoon.enrich.drive import (
    ElementRateLimiter,
    RouteMatrixElement,
    compute_drive_times,
    drive_estimate,
    estimate_minutes,
    parse_duration_seconds,
    parse_matrix_response,
)
from bluemoon.enrich.geo import (
    AddressQuery,
    CensusAddressMatch,
    geocode_estimate,
    geocode_missing,
    geocode_one,
    parse_census_batch,
    redact,
    resolve_matches,
    street_for_geocoding,
)

MARKET = "apartments-springfield-oh"
OTHER_MARKET = "self-storage-springfield-oh"
ORIGIN = (39.9242, -83.8088)          # downtown Springfield (config.yaml)
FAKE_KEY = "FAKE-test-key-0123456789"
LIVE = os.environ.get("BLUEMOON_LIVE") == "1"


# --- helpers ---------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Retries and rate limits pause through geo._sleep; record the pauses instead of waiting."""
    slept: list[float] = []
    monkeypatch.setattr(geo, "_sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def google_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", FAKE_KEY)
    return FAKE_KEY


def add_props(conn, slug: str, items: list[dict]) -> list[int]:
    """Insert made-up properties straight into the master list."""
    market_id = market_by_slug(conn, slug)["id"]
    ids = []
    for i, item in enumerate(items):
        data = {"address": f"{100 + i} TEST ST", "city": "SPRINGFIELD", "state": "OH", "zip": "45502",
                "lat": None, "lng": None, "flags_json": "[]", **item}
        if data["lat"] is not None and "geocode_quality" not in item:
            data["geocode_quality"] = "source"
        data.update(market_id=market_id, property_key=f"test:{slug}:{i}:{data['address']}",
                    created_at=now_iso(), updated_at=now_iso())
        cols = list(data)
        cur = conn.execute(f"INSERT INTO properties({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                           [data[c] for c in cols])
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


def near(i: int) -> dict:
    """A point a few miles from the origin (well inside 60 minutes)."""
    return {"lat": round(ORIGIN[0] + 0.01 * (i + 1), 6), "lng": round(ORIGIN[1] - 0.005 * (i + 1), 6)}


def fake_seconds(lat: float, lng: float) -> int:
    return 300 + int(round((abs(lat - ORIGIN[0]) + abs(lng - ORIGIN[1])) * 6000))


class FakeRoutes:
    """Pretends to be computeRouteMatrix. Thread-safe; records every request."""

    def __init__(self, not_found=(), element_errors=(), queued=None, delay=0.0):
        self.lock = threading.Lock()
        self.requests: list[httpx.Request] = []
        self.inflight = 0
        self.max_inflight = 0
        self.not_found = {(round(a, 5), round(b, 5)) for a, b in not_found}
        self.element_errors = {(round(a, 5), round(b, 5)) for a, b in element_errors}
        self.queued = list(queued or [])        # (status, json body, headers) answered first
        self.delay = delay

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            queued = self.queued.pop(0) if self.queued else None
        try:
            if self.delay:
                time.sleep(self.delay)
            if queued:
                status, body, headers = (queued + (None,))[:3]
                return httpx.Response(status, json=body, headers=headers)
            assert request.url.path.endswith("computeRouteMatrix")
            body = json.loads(request.content)
            elements = []
            for i, d in enumerate(body["destinations"]):
                ll = d["waypoint"]["location"]["latLng"]
                pt = (round(ll["latitude"], 5), round(ll["longitude"], 5))
                el: dict = {"destinationIndex": i} if i else {}   # Google leaves out zero values
                if pt in self.element_errors:
                    el["status"] = {"code": 3, "message": "bad waypoint"}
                elif pt in self.not_found:
                    el.update(status={}, condition="ROUTE_NOT_FOUND")
                else:
                    s = fake_seconds(*pt)
                    el.update(status={}, condition="ROUTE_EXISTS", duration=f"{s}s", distanceMeters=s * 20)
                elements.append(el)
            return httpx.Response(200, json=list(reversed(elements)))   # any order
        finally:
            with self.lock:
                self.inflight -= 1

    @property
    def destinations_sent(self) -> int:
        return sum(len(json.loads(r.content)["destinations"]) for r in self.requests)


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def refusing_client() -> httpx.Client:
    def handler(request):  # pragma: no cover - only runs if the code wrongly calls out
        raise AssertionError(f"unexpected HTTP call to {request.url.host}")
    return client_for(handler)


def drive_rows(conn) -> dict[int, dict]:
    return {r["property_id"]: r for r in rows(conn, "SELECT * FROM drive_times")}


def everything_stored(conn) -> str:
    """All cache rows, call records and drive times as one string (to check no key leaked)."""
    parts = [json.dumps(rows(conn, "SELECT * FROM api_cache"))]
    parts.append(json.dumps(rows(conn, "SELECT * FROM api_calls")))
    parts.append(json.dumps(rows(conn, "SELECT * FROM drive_times")))
    return "\n".join(parts)


# --- drive: pure functions -------------------------------------------------------------------

def test_estimate_minutes_uses_config(settings):
    # 1 degree of latitude is ~69.09 miles; x1.25 road factor at 48 mph = ~107.9 minutes.
    m = estimate_minutes(ORIGIN[0] + 1, ORIGIN[1], *ORIGIN)
    assert m == pytest.approx(69.09 * 1.25 / 48 * 60, abs=0.3)
    settings.raw["drive"]["estimate"] = {"road_factor": 1.0, "avg_mph": 60}
    assert estimate_minutes(ORIGIN[0] + 1, ORIGIN[1], *ORIGIN) == pytest.approx(69.09, abs=0.2)
    assert estimate_minutes(*ORIGIN, *ORIGIN) == 0


@pytest.mark.parametrize("raw, expected", [("1234s", 1234.0), ("12.5s", 12.5), ("0s", 0.0), (" 60s ", 60.0),
                                           (None, None), ("", None), ("abc", None), ("-5s", None)])
def test_parse_duration_seconds(raw, expected):
    assert parse_duration_seconds(raw) == expected


def test_parse_matrix_response_reads_google_shapes():
    payload = [
        {"destinationIndex": 2, "status": {}, "condition": "ROUTE_EXISTS", "duration": "600s", "distanceMeters": 9000},
        {"status": {}, "condition": "ROUTE_EXISTS", "duration": "120s"},            # index 0 left out
        {"destinationIndex": 1, "status": {}, "condition": "ROUTE_NOT_FOUND"},
        {"destinationIndex": 3, "status": {"code": 3, "message": "Invalid waypoint"}},
        {"originIndex": 1, "destinationIndex": 4, "condition": "ROUTE_EXISTS", "duration": "5s"},  # not our origin
        "garbage",
    ]
    els, err = parse_matrix_response(payload)
    assert err is None
    assert set(els) == {0, 1, 2, 3}
    assert els[2].answer().seconds == 600 and els[2].answer().meters == 9000
    assert els[0].answer().seconds == 120 and els[0].answer().meters == 0
    assert els[1].answer().condition == "ROUTE_NOT_FOUND" and not els[1].answer().found
    assert els[3].answer() is None                      # element error: not cached


def test_parse_matrix_response_errors():
    err_body = [{"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}}]
    els, err = parse_matrix_response(err_body)
    assert els == {} and "API key not valid" in err
    _, err = parse_matrix_response({"error": {"status": "PERMISSION_DENIED"}})
    assert "PERMISSION_DENIED" in err
    _, err = parse_matrix_response("nonsense")
    assert err


def test_element_without_condition_but_duration_counts_as_route():
    el = RouteMatrixElement.model_validate({"destinationIndex": 1, "duration": "90s"})
    assert el.answer().found and el.answer().seconds == 90


def test_rate_limiter_waits_for_the_window(monkeypatch):
    now = [0.0]

    def fake_sleep(s):
        now[0] += s

    monkeypatch.setattr(geo, "_sleep", fake_sleep)
    lim = ElementRateLimiter(10, clock=lambda: now[0])
    assert lim.acquire(6) == 0
    now[0] = 1.0
    assert lim.acquire(4) == 0
    waited = lim.acquire(5)                        # must wait until the first 6 leave the window
    assert waited == pytest.approx(59.0)
    assert now[0] == pytest.approx(60.0)
    assert lim.acquire(50) >= 0                    # bigger than the limit: capped, never stuck


def test_drive_config_caps_batch_to_google_limits(settings):
    settings.raw["drive"]["batch_size"] = 5000
    assert drive.drive_config().max_batch == 625
    settings.raw["drive"]["routing_preference"] = "TRAFFIC_AWARE_OPTIMAL"
    assert drive.drive_config().max_batch == 100
    settings.raw["drive"]["routing_preference"] = "FASTEST_PLEASE"
    with pytest.raises(ValueError, match="routing_preference"):
        drive.drive_config()


def test_unknown_provider_is_a_plain_error(conn):
    with pytest.raises(ValueError, match="Unknown drive-time provider"):
        compute_drive_times(conn, MARKET, provider="carrier_pigeon", client=refusing_client())


# --- drive: runs -----------------------------------------------------------------------------

def test_no_key_saves_estimates_without_calling_google(conn):
    ids = add_props(conn, MARKET, [near(i) for i in range(3)])
    res = compute_drive_times(conn, MARKET, client=refusing_client())
    assert res["provider"] == "estimate" and res["estimated"] == 3 and res["requests"] == 0
    assert "no Google key" in res["message"]
    got = drive_rows(conn)
    for pid in ids:
        assert got[pid]["method"] == "estimate"
        assert got[pid]["origin_key"] == "39.92420,-83.80880"
        assert got[pid]["minutes"] > 0 and got[pid]["meters"] > 0
    assert scalar(conn, "SELECT COUNT(*) FROM api_calls") == 0


def test_google_routes_batches_caches_and_records_calls(conn, settings, google_key):
    settings.raw["drive"]["batch_size"] = 2
    pts = [near(i) for i in range(5)]
    ids = add_props(conn, MARKET, pts)
    fake = FakeRoutes()
    res = compute_drive_times(conn, MARKET, client=client_for(fake))

    assert res["provider"] == "google_routes"
    assert res["google"] == 5 and res["fetched"] == 5 and res["cached"] == 0
    assert res["requests"] == 3 and res["elements"] == 5
    for r in fake.requests:
        assert r.method == "POST" and str(r.url) == drive.ROUTES_URL
        assert r.headers["X-Goog-Api-Key"] == google_key
        assert "status" in r.headers["X-Goog-FieldMask"] and "condition" in r.headers["X-Goog-FieldMask"]
        body = json.loads(r.content)
        assert len(body["destinations"]) <= 2 and len(body["origins"]) == 1
        assert body["travelMode"] == "DRIVE" and body["routingPreference"] == "TRAFFIC_UNAWARE"
        assert body["origins"][0]["waypoint"]["location"]["latLng"] == {"latitude": 39.9242, "longitude": -83.8088}
        assert google_key not in r.content.decode() and "key=" not in str(r.url)

    got = drive_rows(conn)
    for pid, pt in zip(ids, pts, strict=True):
        assert got[pid]["method"] == "google_routes"
        assert got[pid]["minutes"] == round(fake_seconds(pt["lat"], pt["lng"]) / 60, 1)
        assert got[pid]["meters"] == fake_seconds(pt["lat"], pt["lng"]) * 20
    assert scalar(conn, "SELECT SUM(units) FROM api_calls WHERE sku='route_matrix_element'") == 5
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache WHERE provider='google_routes'") == 5
    assert google_key not in everything_stored(conn) and google_key not in json.dumps(res, default=str)

    # Re-run: everything comes from the cache, nothing is sent or billed again.
    again = FakeRoutes()
    res2 = compute_drive_times(conn, MARKET, client=client_for(again))
    assert again.requests == [] and res2["cached"] == 5 and res2["google"] == 5
    assert scalar(conn, "SELECT SUM(units) FROM api_calls") == 5


def test_cache_is_shared_across_markets_with_the_same_origin(conn, google_key):
    add_props(conn, MARKET, [near(0)])
    add_props(conn, OTHER_MARKET, [near(0)])
    first = FakeRoutes()
    compute_drive_times(conn, MARKET, client=client_for(first))
    second = FakeRoutes()
    res = compute_drive_times(conn, OTHER_MARKET, client=client_for(second))
    assert first.destinations_sent == 1 and second.requests == []
    assert res["google"] == 1 and res["cached"] == 1


def test_coordinates_round_to_5_decimals_for_the_cache(conn, google_key):
    add_props(conn, MARKET, [{"lat": 39.9500001, "lng": -83.7700001}, {"lat": 39.9500004, "lng": -83.7699996}])
    fake = FakeRoutes()
    res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert fake.destinations_sent == 1          # same spot to 5 decimals: asked once
    assert res["google"] == 2 and res["elements"] == 1
    body = json.loads(fake.requests[0].content)
    assert body["destinations"][0]["waypoint"]["location"]["latLng"] == {"latitude": 39.95, "longitude": -83.77}


def test_limit_does_unfinished_properties_first(conn, google_key):
    ids = add_props(conn, MARKET, [near(i) for i in range(5)])
    fake = FakeRoutes()
    r1 = compute_drive_times(conn, MARKET, limit=2, client=client_for(fake))
    assert r1["properties"] == 2 and fake.destinations_sent == 2
    r2 = compute_drive_times(conn, MARKET, limit=2, client=client_for(fake))
    assert r2["properties"] == 2 and fake.destinations_sent == 4
    assert set(drive_rows(conn)) == set(ids[:4])
    r3 = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert fake.destinations_sent == 5 and r3["cached"] == 4 and set(drive_rows(conn)) == set(ids)


def test_route_not_found_gets_estimate_and_is_cached(conn, google_key):
    pts = [near(0), near(1)]
    ids = add_props(conn, MARKET, pts)
    fake = FakeRoutes(not_found=[(pts[1]["lat"], pts[1]["lng"])])
    res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert res["no_route"] == 1 and res["google"] == 1 and res["estimated"] == 1
    assert "no driving route" in res["message"]
    got = drive_rows(conn)
    assert got[ids[0]]["method"] == "google_routes" and got[ids[1]]["method"] == "estimate"
    again = FakeRoutes()
    compute_drive_times(conn, MARKET, client=client_for(again))
    assert again.requests == []                 # "no route" is an answer; never paid for twice


def test_element_error_is_not_cached_and_retried_next_run(conn, google_key):
    pts = [near(0), near(1)]
    ids = add_props(conn, MARKET, pts)
    fake = FakeRoutes(element_errors=[(pts[0]["lat"], pts[0]["lng"])])
    res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert res["errors"] == 1 and drive_rows(conn)[ids[0]]["method"] == "estimate"
    healthy = FakeRoutes()
    compute_drive_times(conn, MARKET, client=client_for(healthy))
    assert healthy.destinations_sent == 1
    assert drive_rows(conn)[ids[0]]["method"] == "google_routes"


def test_retries_rate_limits_and_server_errors(conn, google_key, no_waiting):
    add_props(conn, MARKET, [near(0)])
    fake = FakeRoutes(queued=[(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}, {"Retry-After": "7"}),
                              (503, {"error": {"code": 503, "status": "UNAVAILABLE"}})])
    res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert len(fake.requests) == 3 and res["google"] == 1 and res["error"] is None
    assert no_waiting[0] == 7                   # honoured Retry-After
    assert len(no_waiting) == 2


def test_gives_up_after_retries_and_falls_back_to_estimates(conn, settings, google_key):
    settings.raw["drive"]["max_retries"] = 2
    add_props(conn, MARKET, [near(0)])
    fake = FakeRoutes(queued=[(503, {"error": {}})] * 10)
    res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert len(fake.requests) == 3
    assert res["estimated"] == 1 and res["errors"] == 1
    assert any("didn't answer" in w for w in res["warnings"])
    assert scalar(conn, "SELECT COUNT(*) FROM api_calls") == 0


def test_invalid_key_stops_the_run_with_a_plain_message(conn, settings, google_key, caplog):
    settings.raw["drive"]["batch_size"] = 1
    add_props(conn, MARKET, [near(i) for i in range(4)])
    settings.raw["google"]["concurrency"] = 1
    bad = [{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                      "status": "INVALID_ARGUMENT",
                      "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}]
    fake = FakeRoutes(queued=[(400, bad)] * 10)
    with caplog.at_level(logging.DEBUG):
        res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert len(fake.requests) == 1              # stopped after the first refusal
    assert "isn't valid" in res["error"] and "GOOGLE_MAPS_API_KEY" in res["error"]
    assert res["estimated"] == 4 and res["google"] == 0
    assert scalar(conn, "SELECT COUNT(*) FROM api_calls") == 0
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache") == 0
    assert google_key not in json.dumps(res) and google_key not in caplog.text


def test_api_not_enabled_message(conn, google_key):
    add_props(conn, MARKET, [near(0)])
    denied = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Routes API has not been used",
                        "details": [{"reason": "SERVICE_DISABLED"}]}}
    res = compute_drive_times(conn, MARKET, client=client_for(FakeRoutes(queued=[(403, denied)])))
    assert "turn on the Routes API" in res["error"]


def test_far_properties_keep_the_free_estimate(conn, google_key):
    ids = add_props(conn, MARKET, [near(0), {"lat": 41.4993, "lng": -81.6944}])   # ~150 miles away
    fake = FakeRoutes()
    res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert fake.destinations_sent == 1 and res["beyond_range"] == 1
    got = drive_rows(conn)
    assert got[ids[1]]["method"] == "estimate" and got[ids[1]]["minutes"] > 120
    assert "far outside" in res["message"]


def test_far_filter_can_be_turned_off(conn, settings, google_key):
    settings.raw["drive"]["google_max_estimate_ratio"] = None
    add_props(conn, MARKET, [{"lat": 41.4993, "lng": -81.6944}])
    fake = FakeRoutes()
    compute_drive_times(conn, MARKET, client=client_for(fake))
    assert fake.destinations_sent == 1


def test_provider_estimate_overrides_the_key(conn, google_key):
    add_props(conn, MARKET, [near(0)])
    res = compute_drive_times(conn, MARKET, provider="estimate", client=refusing_client())
    assert res["provider"] == "estimate" and res["estimated"] == 1


def test_origin_change_asks_again(conn, google_key):
    pid = add_props(conn, MARKET, [near(0)])[0]
    fake = FakeRoutes()
    compute_drive_times(conn, MARKET, client=client_for(fake))
    conn.execute("UPDATE markets SET origin_lat=39.7589, origin_lng=-84.1916 WHERE slug=?", (MARKET,))
    conn.commit()
    compute_drive_times(conn, MARKET, client=client_for(fake))
    assert fake.destinations_sent == 2
    assert drive_rows(conn)[pid]["origin_key"] == "39.75890,-84.19160"


def test_concurrency_is_limited(conn, settings, google_key):
    settings.raw["drive"]["batch_size"] = 1
    settings.raw["google"]["concurrency"] = 2
    add_props(conn, MARKET, [near(i) for i in range(6)])
    fake = FakeRoutes(delay=0.05)
    res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert len(fake.requests) == 6 and res["google"] == 6
    assert 1 <= fake.max_inflight <= 2


def test_dry_run_counts_and_cost_without_calls(conn, google_key):
    add_props(conn, MARKET, [near(i) for i in range(4)] + [{"lat": 41.4993, "lng": -81.6944}, {}])
    fake = FakeRoutes()
    compute_drive_times(conn, MARKET, limit=1, geocode=False, client=client_for(fake))   # 1 cached
    est = compute_drive_times(conn, MARKET, dry_run=True, client=refusing_client())
    assert est["dry_run"] is True and est["provider"] == "google_routes"
    assert est["properties"] == 5 and est["cached"] == 1 and est["to_fetch"] == 3 and est["elements"] == 3
    assert est["beyond_range"] == 1 and est["missing_coordinates"] == 1
    assert est["cost"]["sku"] == "route_matrix_element" and est["cost"]["usd"] == 0
    assert est["needs_confirm"] is False
    assert est["geocode"]["properties"] == 1
    assert drive_estimate(conn, MARKET)["elements"] == 3
    assert len(drive_rows(conn)) == 1           # the dry run wrote nothing


def test_dry_run_cost_after_free_tier(conn, google_key):
    from bluemoon.cache import record_call
    add_props(conn, MARKET, [near(i) for i in range(3)])
    record_call(conn, "google", "route_matrix_element", 10_000)
    est = drive_estimate(conn, MARKET)
    assert est["cost"]["billable"] == 3 and est["cost"]["usd"] == pytest.approx(0.02, abs=0.01)


def test_confirm_false_waits_for_ok_on_big_runs(conn, settings, google_key):
    settings.raw["safety"]["confirm_over_properties"] = 3
    add_props(conn, MARKET, [near(i) for i in range(5)])
    fake = FakeRoutes()
    res = compute_drive_times(conn, MARKET, confirm=False, client=client_for(fake))
    assert res["needs_confirm"] is True and fake.requests == [] and drive_rows(conn) == {}
    assert "Waiting for your OK" in res["message"]
    small = compute_drive_times(conn, MARKET, limit=2, confirm=False, client=client_for(fake))
    assert small["needs_confirm"] is False and small["google"] == 2
    full = compute_drive_times(conn, MARKET, confirm=True, client=client_for(fake))
    assert full["google"] == 5


def test_progress_is_reported(conn, google_key):
    add_props(conn, MARKET, [near(i) for i in range(3)])
    seen: list[tuple[float, str]] = []
    compute_drive_times(conn, MARKET, client=client_for(FakeRoutes()), progress=lambda f, m: seen.append((f, m)))
    assert seen and seen[-1][0] == 1.0
    assert all(0 <= f <= 1 for f, _ in seen)
    assert [f for f, _ in seen] == sorted(f for f, _ in seen)


def test_broken_progress_callback_never_breaks_the_run(conn):
    add_props(conn, MARKET, [near(0)])

    def boom(f, m):
        raise RuntimeError("ui went away")

    assert compute_drive_times(conn, MARKET, progress=boom, client=refusing_client())["estimated"] == 1


def test_empty_market_message(conn):
    res = compute_drive_times(conn, MARKET, client=refusing_client())
    assert res["properties"] == 0 and "Import a Crexi export" in res["message"]


def test_sample_import_end_to_end_estimates(conn, sample_csv):
    from bluemoon.ingest import import_file
    import_file(conn, MARKET, sample_csv)
    total = scalar(conn, "SELECT COUNT(*) FROM properties")
    res = compute_drive_times(conn, MARKET, client=refusing_client())
    assert res["properties"] == total and res["estimated"] == total
    far = one(conn, "SELECT d.minutes FROM drive_times d JOIN properties p ON p.id=d.property_id "
                    "WHERE p.address='999 FARAWAY LN'")
    near_ = one(conn, "SELECT d.minutes FROM drive_times d JOIN properties p ON p.id=d.property_id "
                      "WHERE p.address='1200 SAMPLE RIDGE DR'")
    assert far["minutes"] > 60 > near_["minutes"]


# --- geo: pure functions ---------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("77-79 FAKE MEADOW CT", "77 FAKE MEADOW CT"),
    ("100 & 102 Main St", "100 Main St"),
    ("12 Oak St; 14 Oak St", "12 Oak St"),
    ("100 Main St Apt 4", "100 Main St"),
    ("100 Main St Unit B", "100 Main St"),
    ("100 Main St #12", "100 Main St"),
    ("100 Main St, Springfield", "100 Main St"),
    ("  76   E High St ", "76 E High St"),
    ("1 Suite Rd", "1 Suite Rd"),
    ("", None),
    (None, None),
])
def test_street_for_geocoding(raw, expected):
    assert street_for_geocoding(raw) == expected


def test_address_query_line():
    q = geo.query_for("76 E High St", "Springfield", "oh", "45502-1234")
    assert q.line == "76 E High St, Springfield, OH 45502"
    assert geo.query_for("  ") is None
    assert AddressQuery("1 A St").line == "1 A St"


CENSUS_BATCH_REPLY = (
    '"2","1600 Pennsylvania Ave NW, Washington, DC, 20500","Match","Exact","1600 PENNSYLVANIA AVE NW, '
    'WASHINGTON, DC, 20500","-77.035189204737,38.898702605246","76225813","L"\n'
    '"1","76 E High St, Springfield, OH, 45502","Match","Exact","76 E HIGH ST, SPRINGFIELD, OH, 45502",'
    '"-83.80968101645,39.923338763646","135009355","L"\n'
    '"3","99999 Nowhere Blvd, Springfield, OH, 45502","No_Match"\n'
    '"4","100 Main St, , OH, ","Tie"\n'
    '\n'
    '"5","x","Match","Non_Exact","X","not-a-coordinate","1","L"\n'
)


def test_parse_census_batch_real_format():
    got = parse_census_batch(CENSUS_BATCH_REPLY)
    assert set(got) == {"1", "2", "3", "4", "5"}
    assert got["1"].lat == pytest.approx(39.92334, abs=1e-5) and got["1"].lng == pytest.approx(-83.80968, abs=1e-5)
    assert got["1"].match_type == "Exact" and got["3"].match == "No_Match" and got["4"].match == "Tie"
    assert geo.answer_from_batch_row(got["1"]).quality == "exact"
    assert geo.answer_from_batch_row(got["3"]).quality == "failed"
    assert geo.answer_from_batch_row(got["5"]).quality == "failed"     # "Match" without a location


def _match(street: str, x: float, y: float, zip_: str = "45502", city: str = "SPRINGFIELD") -> CensusAddressMatch:
    return CensusAddressMatch.model_validate({
        "matchedAddress": f"{street}, {city}, OH, {zip_}", "coordinates": {"x": x, "y": y},
        "addressComponents": {"zip": zip_, "city": city}})


def test_resolve_matches_rules():
    q = AddressQuery("76 E High St", "Springfield", "OH", "45502")
    one_exact = resolve_matches([_match("76 E HIGH ST", -83.8097, 39.9233)], q, 0.5)
    assert one_exact.quality == "exact"
    one_other = resolve_matches([_match("76 E HIGH AVE", -83.8097, 39.9233)], q, 0.5)
    assert one_other.quality == "approximate"
    close = resolve_matches([_match("76 W HIGH ST", -83.8118, 39.9235), _match("76 E HIGH ST", -83.8097, 39.9233)],
                            AddressQuery("76 High St", "Springfield", "OH", "45502"), 0.5)
    assert close.quality == "approximate" and close.lat == pytest.approx(39.9234, abs=1e-4)
    far = resolve_matches([_match("100 MAIN ST", -84.70, 39.13, "45001", "ADDYSTON"),
                           _match("100 MAIN ST", -83.84, 40.70, "45812", "ALGER")],
                          AddressQuery("100 Main St", None, "OH", None), 0.5)
    assert far.quality == "failed" and "different places" in far.detail
    by_zip = resolve_matches([_match("100 MAIN ST", -84.70, 39.13, "45001", "ADDYSTON"),
                              _match("100 MAIN ST", -83.84, 40.70, "45812", "ALGER")],
                             AddressQuery("100 Main St", None, "OH", "45812"), 0.5)
    assert by_zip.quality == "exact" and by_zip.lat == 40.70
    assert resolve_matches([], q, 0.5).quality == "failed"


def test_redact_removes_keys(google_key):
    assert google_key not in redact(f"GET https://x/?address=a&key={google_key}&b=1")
    assert redact("https://x/?key=SOMETHING") == "https://x/?key=***"
    assert redact("plain words") == "plain words"


def test_httpx_log_lines_never_show_the_key(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "GET",
                                        "https://maps.googleapis.com/maps/api/geocode/json?address=x&key=SECRET123",
                                        "HTTP/1.1 200 OK")
    assert "SECRET123" not in caplog.text and "key=***" in caplog.text


# --- geo: fake Census and Google ----------------------------------------------------------------

def multipart_fields(request: httpx.Request) -> dict[str, str]:
    raw = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    msg = email.message_from_bytes(raw)
    out = {}
    for part in msg.walk():
        name = part.get_param("name", header="content-disposition")
        if name:
            out[name] = part.get_payload(decode=True).decode()
    return out


class FakeGeocoders:
    """Census batch + one-line and Google Geocoding, answering from small tables keyed by street."""

    def __init__(self, batch=None, oneline=None, google=None, census_status=200):
        self.batch = batch or {}          # street upper -> list of CSV cells after the input address
        self.oneline = oneline or {}      # street upper -> list of (street, x, y, zip, city)
        self.google = google or {}        # street upper -> google json
        self.census_status = census_status
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if host == "geocoding.geo.census.gov" and path.endswith("/addressbatch"):
            fields = multipart_fields(request)
            self.calls.append(("batch", fields))
            if self.census_status != 200:
                return httpx.Response(self.census_status, text="busy")
            lines = []
            for cells in __import__("csv").reader(fields["addressFile"].splitlines()):
                rid, street = cells[0], cells[1].upper()
                answer = self.batch.get(street)
                if answer is None:
                    continue                                 # Census left this row out
                quoted = ",".join(f'"{c}"' for c in [rid, f"{cells[1]}, {cells[2]}, {cells[3]}, {cells[4]}", *answer])
                lines.append(quoted)
            return httpx.Response(200, text="\n".join(lines) + "\n")
        if host == "geocoding.geo.census.gov" and path.endswith("/onelineaddress"):
            self.calls.append(("oneline", params))
            if self.census_status != 200:
                return httpx.Response(self.census_status, text="busy")
            street = params["address"].split(",")[0].upper()
            matches = [{"matchedAddress": f"{s}, {city}, OH, {z}", "coordinates": {"x": x, "y": y},
                        "addressComponents": {"zip": z, "city": city}}
                       for s, x, y, z, city in self.oneline.get(street, [])]
            return httpx.Response(200, json={"result": {"input": {}, "addressMatches": matches}})
        if host == "maps.googleapis.com":
            self.calls.append(("google", params))
            street = params["address"].split(",")[0].upper()
            return httpx.Response(200, json=self.google.get(street, {"status": "ZERO_RESULTS", "results": []}))
        raise AssertionError(f"unexpected call to {request.url.host}")

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)


def google_ok(lat: float, lng: float, kind: str = "ROOFTOP") -> dict:
    return {"status": "OK", "results": [{"geometry": {"location": {"lat": lat, "lng": lng}, "location_type": kind},
                                         "formatted_address": "somewhere", "partial_match": False}]}


SIX = [
    {"address": "1 EXACT ST"},
    {"address": "2 NONEXACT ST"},
    {"address": "3 NOMATCH ST"},
    {"address": "4 TIE ST"},
    {"address": "5 SILENT ST"},
    {"address": "6 EXACT2 ST Apt 3"},
]
SIX_BATCH = {
    "1 EXACT ST": ["Match", "Exact", "1 EXACT ST, SPRINGFIELD, OH, 45502", "-83.80,39.92", "1", "L"],
    "2 NONEXACT ST": ["Match", "Non_Exact", "2 NONEXACT ST, SPRINGFIELD, OH, 45502", "-83.81,39.93", "2", "L"],
    "3 NOMATCH ST": ["No_Match"],
    "4 TIE ST": ["Tie"],
    "6 EXACT2 ST": ["Match", "Exact", "6 EXACT2 ST, SPRINGFIELD, OH, 45502", "-83.82,39.94", "6", "L"],
}
SIX_ONELINE = {"4 TIE ST": [("4 TIE ST E", -83.8000, 39.9200, "45502", "SPRINGFIELD"),
                            ("4 TIE ST W", -83.8020, 39.9202, "45502", "SPRINGFIELD")]}


def test_geocode_missing_batch_flow(conn):
    ids = add_props(conn, MARKET, [dict(p, flags_json='["no_coordinates", "year_unknown"]') for p in SIX])
    keep = add_props(conn, MARKET, [{"address": "9 HAS COORDS ST", "lat": 39.9, "lng": -83.8}])[0]
    fake = FakeGeocoders(batch=SIX_BATCH, oneline=SIX_ONELINE)
    res = geocode_missing(conn, MARKET, client=client_for(fake))

    assert fake.count("batch") == 1 and fake.count("oneline") == 1 and fake.count("google") == 0
    sent = next(f for k, f in fake.calls if k == "batch")
    assert sent["benchmark"] == "Public_AR_Current"
    assert "5,6 EXACT2 ST,SPRINGFIELD,OH,45502" in sent["addressFile"].splitlines()
    assert "Apt" not in sent["addressFile"]
    assert "9 HAS COORDS ST" not in sent["addressFile"]

    props = {r["id"]: r for r in rows(conn, "SELECT * FROM properties")}
    assert props[ids[0]]["geocode_quality"] == "exact" and props[ids[0]]["lat"] == 39.92
    assert props[ids[1]]["geocode_quality"] == "approximate"
    assert props[ids[2]]["geocode_quality"] == "failed" and props[ids[2]]["lat"] is None
    assert props[ids[3]]["geocode_quality"] == "approximate" and props[ids[3]]["lng"] == pytest.approx(-83.801)
    assert props[ids[4]]["geocode_quality"] is None and props[ids[4]]["lat"] is None   # Census left it out
    assert props[ids[5]]["geocode_quality"] == "exact"
    assert json.loads(props[ids[0]]["flags_json"]) == ["year_unknown"]                    # no_coordinates gone
    assert "no_coordinates" in json.loads(props[ids[2]]["flags_json"])
    assert props[keep]["geocode_quality"] == "source" and props[keep]["lat"] == 39.9

    assert res["properties"] == 6 and res["geocoded"] == 4 and res["exact"] == 2 and res["approximate"] == 2
    assert res["failed"] == 1 and res["unresolved"] == 1 and res["provider"] == "census"
    assert "Placed 4 of 6" in res["message"] and "run this again later" in res["message"]
    assert scalar(conn, "SELECT COUNT(*) FROM api_calls") == 0      # Census is free

    # Re-run: only the address Census skipped is asked again (one-line, since it's a single address).
    again = FakeGeocoders(batch=SIX_BATCH, oneline={"5 SILENT ST": [("5 SILENT ST", -83.7, 39.8, "45502",
                                                                       "SPRINGFIELD")]})
    res2 = geocode_missing(conn, MARKET, client=client_for(again))
    assert again.count("batch") == 0 and again.count("oneline") == 1
    assert res2["properties"] == 2 and res2["cached"] == 1          # the failed one comes from the cache
    assert one(conn, "SELECT geocode_quality FROM properties WHERE id=?", (ids[4],))["geocode_quality"] == "exact"


def test_few_addresses_use_one_line_lookups(conn):
    add_props(conn, MARKET, [{"address": "76 E High St"}])
    fake = FakeGeocoders(oneline={"76 E HIGH ST": [("76 E HIGH ST", -83.80968, 39.92334, "45502", "SPRINGFIELD")]})
    res = geocode_missing(conn, MARKET, client=client_for(fake))
    assert fake.count("batch") == 0 and fake.count("oneline") == 1
    params = fake.calls[0][1]
    assert params["address"] == "76 E High St, SPRINGFIELD, OH 45502"
    assert params["benchmark"] == "Public_AR_Current" and params["format"] == "json"
    assert res["exact"] == 1


def test_duplicate_addresses_are_looked_up_once(conn):
    add_props(conn, MARKET, [{"address": "1 SAME ST"}, {"address": "1 Same St"}])
    fake = FakeGeocoders(oneline={"1 SAME ST": [("1 SAME ST", -83.8, 39.9, "45502", "SPRINGFIELD")]})
    res = geocode_missing(conn, MARKET, client=client_for(fake))
    assert fake.count("oneline") == 1 and res["geocoded"] == 2


def test_no_street_address_is_marked_failed_without_a_call(conn):
    pid = add_props(conn, MARKET, [{"address": None}])[0]
    res = geocode_missing(conn, MARKET, client=refusing_client())
    assert res["no_address"] == 1 and res["failed"] == 1
    assert one(conn, "SELECT geocode_quality FROM properties WHERE id=?", (pid,))["geocode_quality"] == "failed"


def test_nothing_to_do_makes_no_calls(conn):
    add_props(conn, MARKET, [near(0)])
    res = geocode_missing(conn, MARKET, client=refusing_client())
    assert res["properties"] == 0 and res["message"] == "Every property already has map coordinates."


def test_google_fallback_only_after_census_fails(conn, google_key):
    ids = add_props(conn, MARKET, [{"address": "3 NOMATCH ST"}, {"address": "1 EXACT ST"}])
    fake = FakeGeocoders(oneline={"1 EXACT ST": [("1 EXACT ST", -83.8, 39.92, "45502", "SPRINGFIELD")]},
                         google={"3 NOMATCH ST": google_ok(39.95, -83.75)})
    res = geocode_missing(conn, MARKET, client=client_for(fake))
    assert fake.count("google") == 1
    gparams = next(p for k, p in fake.calls if k == "google")
    assert gparams["key"] == google_key and gparams["components"] == "country:US|administrative_area:OH"
    assert one(conn, "SELECT geocode_quality, lat FROM properties WHERE id=?", (ids[0],)) == \
        {"geocode_quality": "google", "lat": 39.95}
    assert res["google"] == 1 and res["exact"] == 1 and res["provider"] == "census+google"
    assert res["cost"]["sku"] == "geocoding"
    assert scalar(conn, "SELECT SUM(units) FROM api_calls WHERE sku='geocoding'") == 1
    assert google_key not in everything_stored(conn) and google_key not in json.dumps(res)

    # Already placed: a re-run touches nothing.
    again = FakeGeocoders()
    assert geocode_missing(conn, MARKET, client=client_for(again))["properties"] == 0
    assert again.calls == []


def test_google_failures_are_cached_and_never_repaid(conn, google_key):
    pid = add_props(conn, MARKET, [{"address": "3 NOMATCH ST"}])[0]
    fake = FakeGeocoders()                     # Census: no match; Google: ZERO_RESULTS
    geocode_missing(conn, MARKET, client=client_for(fake))
    assert fake.count("google") == 1
    assert one(conn, "SELECT geocode_quality FROM properties WHERE id=?", (pid,))["geocode_quality"] == "failed"
    again = FakeGeocoders()
    res = geocode_missing(conn, MARKET, client=client_for(again))
    assert again.calls == [] and res["failed"] == 1
    assert scalar(conn, "SELECT SUM(units) FROM api_calls") == 1


def test_google_rough_area_is_not_good_enough(conn, google_key):
    pid = add_props(conn, MARKET, [{"address": "3 NOMATCH ST"}])[0]
    fake = FakeGeocoders(google={"3 NOMATCH ST": google_ok(39.92, -83.80, kind="APPROXIMATE")})
    geocode_missing(conn, MARKET, client=client_for(fake))
    assert one(conn, "SELECT geocode_quality, lat FROM properties WHERE id=?", (pid,)) == \
        {"geocode_quality": "failed", "lat": None}


def test_google_request_denied_stops_with_plain_message(conn, google_key):
    add_props(conn, MARKET, [{"address": "3 NOMATCH ST"}, {"address": "4 NOMATCH ST"}])
    denied = {"status": "REQUEST_DENIED", "results": [], "error_message": "The provided API key is invalid. "}
    fake = FakeGeocoders(google={"3 NOMATCH ST": denied, "4 NOMATCH ST": denied})
    res = geocode_missing(conn, MARKET, client=client_for(fake))
    assert fake.count("google") == 1
    assert any("GOOGLE_MAPS_API_KEY" in w for w in res["warnings"])
    assert res["failed"] == 2                  # Census said no match; Google couldn't help
    assert scalar(conn, "SELECT COUNT(*) FROM api_calls") == 0
    assert google_key not in json.dumps(res)


def test_google_over_query_limit_is_retried(conn, google_key, no_waiting):
    add_props(conn, MARKET, [{"address": "3 NOMATCH ST"}])
    replies = [{"status": "OVER_QUERY_LIMIT", "results": []}, google_ok(39.9, -83.7)]

    class Flaky(FakeGeocoders):
        def __call__(self, request):
            if request.url.host == "maps.googleapis.com":
                self.calls.append(("google", {}))
                return httpx.Response(200, json=replies.pop(0))
            return super().__call__(request)

    fake = Flaky()
    res = geocode_missing(conn, MARKET, client=client_for(fake))
    assert fake.count("google") == 2 and res["google"] == 1 and no_waiting


def test_google_cap_per_run(conn, settings, google_key):
    settings.raw["geocode"] = {"google_max_per_run": 1, "census_batch_min": 50}
    add_props(conn, MARKET, [{"address": "3 NOMATCH ST"}, {"address": "4 NOMATCH ST"}])
    fake = FakeGeocoders()
    res = geocode_missing(conn, MARKET, client=client_for(fake))
    assert fake.count("google") == 1 and res["google_capped"] == 1
    assert any("google_max_per_run" in w for w in res["warnings"])


def test_census_down_leaves_properties_for_next_run(conn, settings, no_waiting):
    settings.raw["geocode"] = {"max_retries": 2}
    ids = add_props(conn, MARKET, [{"address": f"{i} BUSY ST"} for i in range(1, 7)])
    fake = FakeGeocoders(census_status=503)
    res = geocode_missing(conn, MARKET, client=client_for(fake))
    assert fake.count("batch") == 3                       # first try + 2 retries
    assert len(no_waiting) == 2
    assert res["unresolved"] == 6 and res["geocoded"] == 0
    assert any("didn't answer" in w for w in res["warnings"])
    assert all(r["geocode_quality"] is None for r in rows(conn, "SELECT geocode_quality FROM properties"))
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache") == 0
    assert len(ids) == 6


def test_geocode_dry_run_makes_no_calls(conn, google_key):
    add_props(conn, MARKET, [{"address": "1 A ST"}, {"address": "2 B ST"}, {"address": None}, near(0)])
    est = geocode_missing(conn, MARKET, dry_run=True, client=refusing_client())
    assert est["dry_run"] is True and est["properties"] == 3 and est["no_address"] == 1
    assert est["to_lookup"] == 2 and est["cached"] == 0 and est["google_fallback"] is True
    assert est["cost"]["sku"] == "geocoding" and est["cost"]["units"] == 2
    assert geocode_estimate(conn, MARKET, limit=1)["properties"] == 1


def test_geocode_limit(conn):
    add_props(conn, MARKET, [{"address": f"{i} LIMIT ST"} for i in range(1, 4)])
    fake = FakeGeocoders(oneline={f"{i} LIMIT ST": [(f"{i} LIMIT ST", -83.8, 39.9, "45502", "SPRINGFIELD")]
                                  for i in range(1, 4)})
    res = geocode_missing(conn, MARKET, limit=2, client=client_for(fake))
    assert res["properties"] == 2 and fake.count("oneline") == 2


def test_geocode_one_uses_cache(conn):
    fake = FakeGeocoders(oneline={"76 E HIGH ST": [("76 E HIGH ST", -83.80968, 39.92334, "45502", "SPRINGFIELD")]})
    a = geocode_one(conn, "76 E High St", "Springfield", "OH", "45502", client=client_for(fake))
    b = geocode_one(conn, "76 E High St", "Springfield", "OH", "45502", client=client_for(fake))
    assert a == b and a["quality"] == "exact" and fake.count("oneline") == 1
    assert geocode_one(conn, None, client=refusing_client())["quality"] == "failed"


def test_drive_times_geocode_missing_coordinates_first(conn):
    pid = add_props(conn, MARKET, [{"address": "1 EXACT ST"}])[0]
    fake = FakeGeocoders(oneline={"1 EXACT ST": [("1 EXACT ST", -83.80, 39.95, "45502", "SPRINGFIELD")]})
    res = compute_drive_times(conn, MARKET, client=client_for(fake))
    assert res["geocode"]["geocoded"] == 1 and res["estimated"] == 1 and res["missing_coordinates"] == 0
    assert drive_rows(conn)[pid]["minutes"] > 0


def test_bad_geocode_config_is_a_plain_error(settings):
    settings.raw["geocode"] = {"census_batch_size": 50_000}
    with pytest.raises(ValueError, match="geocode.census_batch_size"):
        geo.geo_config()


# --- live and real-data checks (skipped by default) ---------------------------------------------

@pytest.mark.skipif(not LIVE, reason="set BLUEMOON_LIVE=1 to call the free Census geocoder")
def test_live_census_one_line(conn):
    ans = geocode_one(conn, "76 E High St", "Springfield", "OH", "45502", allow_google=False)
    assert ans["quality"] == "exact"
    assert ans["lat"] == pytest.approx(39.9233, abs=0.002) and ans["lng"] == pytest.approx(-83.8097, abs=0.002)


@pytest.mark.skipif(not LIVE, reason="set BLUEMOON_LIVE=1 to call the free Census geocoder")
def test_live_census_batch(conn, settings):
    settings.raw["geocode"] = {"census_batch_min": 1}
    ids = add_props(conn, MARKET, [
        {"address": "76 E High St", "city": "Springfield", "state": "OH", "zip": "45502"},
        {"address": "1600 Pennsylvania Ave NW", "city": "Washington", "state": "DC", "zip": "20500"},
        {"address": "99999 Nowhere Blvd", "city": "Springfield", "state": "OH", "zip": "45502"},
    ])
    res = geocode_missing(conn, MARKET, allow_google=False)
    assert res["census_requests"] >= 1 and res["exact"] == 2 and res["failed"] == 1
    got = one(conn, "SELECT lat, lng FROM properties WHERE id=?", (ids[1],))
    assert got["lat"] == pytest.approx(38.8987, abs=0.002)


def test_real_export_drive_estimates(conn, real_crexi):
    """Runs only with BLUEMOON_REAL_CREXI set. Counts only; never prints owner data."""
    from bluemoon.ingest import import_file
    imported = import_file(conn, MARKET, real_crexi, limit=400)
    est = compute_drive_times(conn, MARKET, dry_run=True)
    assert est["properties"] + est["missing_coordinates"] == scalar(conn, "SELECT COUNT(*) FROM properties")
    res = compute_drive_times(conn, MARKET, client=refusing_client() if est["geocode"]["properties"] == 0 else None)
    assert res["properties"] == scalar(conn, "SELECT COUNT(*) FROM drive_times")
    assert res["estimated"] == res["properties"] and imported["rows_total"] == 400
    within = scalar(conn, "SELECT COUNT(*) FROM drive_times WHERE minutes <= 60")
    assert 0 < within <= res["properties"]
