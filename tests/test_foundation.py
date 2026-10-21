def test_markets_created(conn, settings):
    from bluemoon.db import rows
    ms = rows(conn, "SELECT slug, asset_class FROM markets ORDER BY id")
    assert len(ms) == len(settings.markets)
    assert ms[0]["slug"] == "apartments-springfield-oh"


def test_asset_classes_merge_default_signals(settings):
    ac = settings.asset_class("raw_land")
    assert ac.signals["owner_type"]["trust"] == 2.0
    assert ac.signals["condition"]["worn"] == 1.0  # override
    assert not ac.visual_enabled


def test_cost_estimate_uses_free_tier(conn):
    from bluemoon.cache import estimate_cost, record_call
    e = estimate_cost(conn, "street_view_static", 100)
    assert e["usd"] == 0 and e["free_left"] == 10000
    record_call(conn, "google", "street_view_static", 9950)
    e = estimate_cost(conn, "street_view_static", 100)
    assert e["billable"] == 50 and e["usd"] == 0.35


def test_secret_never_blank(monkeypatch):
    from bluemoon.config import secret
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "  ")
    assert secret("GOOGLE_MAPS_API_KEY") is None
