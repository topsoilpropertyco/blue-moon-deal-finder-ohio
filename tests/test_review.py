"""Review queue, your answers, the example library, the scorecard and calibrate
(bluemoon/imagery/review.py). Everything runs offline."""
from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest
import yaml

from bluemoon.config import reload_settings
from bluemoon.db import market_by_slug, now_iso, one
from bluemoon.imagery import review
from bluemoon.imagery.review import (
    ReviewConfig,
    calibrate,
    examples_for,
    normalize_label,
    queue_counts,
    read_examples,
    review_config,
    review_queue,
    reviewed_final_p,
    save_review,
    scorecard,
    spot_check_pick,
    visual_color,
)
from bluemoon.imagery.vision import MockBackend, VisionBackend

MARKET = "apartments-springfield-oh"
_ids = count(1)


# --- helpers ---------------------------------------------------------------------------------

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


def add(conn, settings, market: str = MARKET, *, photos: bool = True, status: str | None = "done",
        in_buybox: int | None = 1, rank: int | None = None, **visual) -> int:
    """A property with (optionally) saved photos and a visual_checks row."""
    m = market_by_slug(conn, market)
    n = next(_ids)
    cur = conn.execute(
        "INSERT INTO properties(market_id, property_key, address, city, state, lat, lng, crexi_url, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (m["id"], f"rev:{n}", f"{n} REVIEW AVE", "SPRINGFIELD", "OH", 39.95, -83.77,
         f"https://www.crexi.com/property-records/x-{n}", now_iso(), now_iso()))
    pid = cur.lastrowid
    if in_buybox is not None:
        conn.execute("INSERT INTO scores(property_id, in_buybox, rank, computed_at) VALUES(?,?,?,?)",
                     (pid, in_buybox, rank, now_iso()))
    if status is not None:
        cols = {"property_id": pid, "status": status, **visual}
        conn.execute(f"INSERT INTO visual_checks({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                     tuple(cols.values()))
    conn.commit()
    if photos:
        folder = settings.path("images") / str(pid)
        folder.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(("sv_0.jpg", "sv_1.jpg", "sv_2.jpg", "sat.jpg")):
            (folder / name).write_bytes(b"\xff\xd8\xff\xe0" + bytes([pid % 256, i]) * 8)
    return pid


def ai(label: str, p: float, final_p: float | None, quality: str = "good", visible: int = 1, prior: float = 0) -> dict:
    return {"ai_label": label, "ai_p": p, "final_p": final_p, "image_quality": quality, "property_visible": visible,
            "prior": prior, "checked_at": now_iso(), "model": "mock:mock-1", "reasons": "because",
            "cues_json": json.dumps(["stairs"])}


def visual(conn, pid: int) -> dict:
    return one(conn, "SELECT * FROM visual_checks WHERE property_id=?", (pid,)) or {}


# --- colors and labels -----------------------------------------------------------------------

def test_colors_follow_config(tmp_root, settings):
    cfg = review_config()
    assert (cfg.green_min, cfg.red_max, cfg.spot_check_fraction) == (80, 20, 0.05)
    assert visual_color(80, cfg) == "green" and visual_color(79.9, cfg) == "yellow"
    assert visual_color(21, cfg) == "yellow" and visual_color(20, cfg) == "red" and visual_color(None, cfg) == "na"
    set_config(tmp_root, "scoring.visual_green_min", 75)
    assert visual_color(76) == "green"


def test_normalize_label(settings):
    ac = settings.asset_class("apartments")
    assert normalize_label("exterior corridor", ac) == "EXTERIOR_CORRIDOR"
    assert normalize_label("direct-entry", ac) == "DIRECT_ENTRY"
    assert normalize_label("3", ac) == "INTERIOR_CORRIDOR" and normalize_label(5, ac) == "UNKNOWN"
    with pytest.raises(ValueError, match="isn't one of the answers"):
        normalize_label("GARAGE", ac)
    with pytest.raises(ValueError):
        normalize_label("9", ac)


def test_reviewed_final_p(settings):
    apt, wh = settings.asset_class("apartments"), settings.asset_class("warehouse")
    cfg = ReviewConfig()
    assert reviewed_final_p("EXTERIOR_CORRIDOR", apt, cfg) == 100 and reviewed_final_p("DIRECT_ENTRY", apt, cfg) == 100
    assert reviewed_final_p("MIXED", apt, cfg) == 60 and reviewed_final_p("INTERIOR_CORRIDOR", apt, cfg) == 0
    assert reviewed_final_p("UNKNOWN", apt, cfg) is None
    assert reviewed_final_p("BOTH", wh, cfg) == 100 and reviewed_final_p("GRADE_LEVEL", wh, cfg) == 0


def test_spot_check_pick_is_stable_and_about_the_right_share():
    picks = [spot_check_pick(i, "m", 0.05) for i in range(4000)]
    assert picks == [spot_check_pick(i, "m", 0.05) for i in range(4000)]
    assert 120 < sum(picks) < 280
    assert not any(spot_check_pick(i, "m", 0) for i in range(100))
    assert all(spot_check_pick(i, "m", 1) for i in range(100))


# --- the queue -------------------------------------------------------------------------------

def test_queue_order_and_card_contents(tmp_root, conn, settings):
    set_config(tmp_root, "review.spot_check_fraction", 1.0)
    green = add(conn, settings, **ai("EXTERIOR_CORRIDOR", 95, 92))
    yellow_low = add(conn, settings, rank=5, **ai("MIXED", 50, 55))
    yellow_top = add(conn, settings, rank=1, **ai("MIXED", 60, 60))
    unsure = add(conn, settings, **ai("UNKNOWN", 50, None, quality="poor"))
    not_checked = add(conn, settings)
    no_photos = add(conn, settings, photos=False, status="no_imagery")
    add(conn, settings, photos=False, status="pending")              # photos still coming: not shown
    add(conn, settings, **ai("INTERIOR_CORRIDOR", 5, 0), reviewed_label="INTERIOR_CORRIDOR")  # answered
    add(conn, settings, in_buybox=0, **ai("MIXED", 50, 50))            # outside the buy box
    q = review_queue(conn, MARKET)
    assert [c["id"] for c in q] == [unsure, yellow_top, yellow_low, not_checked, no_photos, green]
    assert [c["group"] for c in q] == ["needs_review", "unsure", "unsure", "not_checked", "no_photos", "spot_check"]
    card = q[1]
    assert card["color"] == "yellow" and card["final_p"] == 60 and "60%" in card["why"]
    assert [i["name"] for i in card["images"]] == ["sv_0.jpg", "sv_1.jpg", "sv_2.jpg", "sat.jpg"]
    assert card["images"][0]["url"] == f"/images/{yellow_top}/sv_0.jpg"
    assert card["links"]["google_maps"].startswith("https://www.google.com/maps")
    assert [c["key"] for c in card["choices"]] == [1, 2, 3, 4, 5]
    helps = settings.asset_class("apartments").visual_check["label_help"]
    assert card["choices"][0] == {"key": 1, "label": "EXTERIOR_CORRIDOR", "target": True,
                                  "help": helps["EXTERIOR_CORRIDOR"]}
    assert card["cues"] == ["stairs"] and card["crexi_url"] and card["question"]
    assert q[0]["needs_review"] is True and q[0]["color"] == "na"
    assert len(review_queue(conn, MARKET, include_excluded=True)) == 7
    assert [c["id"] for c in review_queue(conn, MARKET, limit=2, offset=1)] == [yellow_top, yellow_low]


def test_spot_checks_off(tmp_root, conn, settings):
    set_config(tmp_root, "review.spot_check_fraction", 0)
    add(conn, settings, **ai("EXTERIOR_CORRIDOR", 95, 92))
    add(conn, settings, **ai("INTERIOR_CORRIDOR", 5, 4))
    assert review_queue(conn, MARKET) == []


def test_queue_counts(tmp_root, conn, settings):
    set_config(tmp_root, "review.spot_check_fraction", 0)
    add(conn, settings, **ai("MIXED", 50, 50))
    add(conn, settings, **ai("UNKNOWN", 50, None, quality="none"))
    add(conn, settings, **ai("MIXED", 50, 50), reviewed_label="MIXED")
    c = queue_counts(conn, MARKET)
    assert c["total"] == 2 and c["unsure"] == 1 and c["needs_review"] == 1 and c["reviewed"] == 1


def test_queue_empty_for_raw_land(conn, settings):
    assert review_queue(conn, "land-springfield-oh") == []


# --- saving answers --------------------------------------------------------------------------

def test_save_review_updates_everything(conn, settings):
    pid = add(conn, settings, **ai("INTERIOR_CORRIDOR", 30, 34))
    res = save_review(conn, pid, "exterior corridor", "  stair towers on the side  ")
    assert res["label"] == "EXTERIOR_CORRIDOR" and res["final_p"] == 100 and res["color"] == "green"
    assert res["agreed_with_ai"] is False and res["examples_saved"] == 4
    row = visual(conn, pid)
    assert row["reviewed_label"] == "EXTERIOR_CORRIDOR" and row["reviewed_note"] == "stair towers on the side"
    assert row["final_p"] == 100 and row["reviewed_at"] and row["ai_label"] == "INTERIOR_CORRIDOR"
    labels = settings.path("labels")
    copied = sorted(p.name for p in (labels / "examples" / str(pid)).iterdir())
    assert copied == ["sat.jpg", "sv_0.jpg", "sv_1.jpg", "sv_2.jpg"]
    (rec,) = read_examples()
    assert rec.property_id == pid and rec.label == "EXTERIOR_CORRIDOR" and rec.asset_class == "apartments"
    assert rec.images[:2] == [f"examples/{pid}/sv_0.jpg", f"examples/{pid}/sat.jpg"]  # best photos first
    assert rec.ai_label == "INTERIOR_CORRIDOR" and rec.market == MARKET
    card = json.loads((labels / "scorecard.json").read_text())
    assert card["markets"][MARKET]["reviewed"] == 1
    assert review_queue(conn, MARKET) == []  # answered cards leave the queue


def test_review_without_photos_or_row(conn, settings):
    pid = add(conn, settings, photos=False, status=None)
    res = save_review(conn, pid, "2")
    assert res["label"] == "DIRECT_ENTRY" and res["examples_saved"] == 0 and res["agreed_with_ai"] is None
    assert visual(conn, pid)["status"] == "pending" and visual(conn, pid)["final_p"] == 100
    assert "No photos" in res["message"]


def test_unknown_review_keeps_no_verdict(conn, settings):
    pid = add(conn, settings, **ai("MIXED", 50, 50))
    assert save_review(conn, pid, "UNKNOWN")["final_p"] is None
    assert visual(conn, pid)["reviewed_label"] == "UNKNOWN"


def test_save_review_errors(conn, settings):
    land = add(conn, settings, "land-springfield-oh")
    with pytest.raises(ValueError, match="no photo check"):
        save_review(conn, land, "UNKNOWN")
    with pytest.raises(KeyError):
        save_review(conn, 999999, "MIXED")
    pid = add(conn, settings)
    with pytest.raises(ValueError):
        save_review(conn, pid, "TREEHOUSE")


def test_changing_your_answer(conn, settings):
    pid = add(conn, settings)
    save_review(conn, pid, "MIXED")
    save_review(conn, pid, "DIRECT_ENTRY", "looked again")
    assert visual(conn, pid)["reviewed_label"] == "DIRECT_ENTRY"
    assert len(read_examples()) == 2
    (ex,) = examples_for("apartments", 5)
    assert ex["label"] == "DIRECT_ENTRY" and ex["note"] == "looked again"


# --- the example library ---------------------------------------------------------------------

def test_examples_mix_labels_and_skip_what_they_must(conn, settings):
    a = add(conn, settings)
    b = add(conn, settings)
    c = add(conn, settings)
    d = add(conn, settings)
    no_pics = add(conn, settings, photos=False)
    save_review(conn, a, "DIRECT_ENTRY")
    save_review(conn, b, "DIRECT_ENTRY")
    save_review(conn, c, "DIRECT_ENTRY")
    save_review(conn, d, "INTERIOR_CORRIDOR")
    save_review(conn, no_pics, "MIXED")
    got = examples_for("apartments", 2)
    assert {e["label"] for e in got} == {"DIRECT_ENTRY", "INTERIOR_CORRIDOR"}  # mixed, not just the latest label
    assert all(isinstance(p, Path) and p.is_file() for e in got for p in e["images"])
    assert len(examples_for("apartments", 10)) == 4  # the one without photos is left out
    assert d not in {e["property_id"] for e in examples_for("apartments", 10, exclude_property_ids=[d])}
    assert examples_for("warehouse", 5) == [] and examples_for("apartments", 0) == []
    for p in (settings.path("labels") / "examples" / str(a)).iterdir():
        p.unlink()
    assert a not in {e["property_id"] for e in examples_for("apartments", 10)}


def test_damaged_library_lines_are_skipped(conn, settings):
    pid = add(conn, settings)
    save_review(conn, pid, "MIXED")
    with (settings.path("labels") / "examples.jsonl").open("a") as f:
        f.write("{not json\n\n")
    assert len(read_examples()) == 1


# --- scorecard -------------------------------------------------------------------------------

def _reviewed(conn, settings, truth: str, ai_label: str, ai_p: float, **kw) -> int:
    pid = add(conn, settings, **ai(ai_label, ai_p, ai_p, **kw))
    save_review(conn, pid, truth)
    return pid


def test_scorecard_counts_and_acceptance(conn, settings):
    for _ in range(9):
        _reviewed(conn, settings, "EXTERIOR_CORRIDOR", "EXTERIOR_CORRIDOR", 95)
    _reviewed(conn, settings, "MIXED", "EXTERIOR_CORRIDOR", 95)          # a green that was wrong
    for _ in range(10):
        _reviewed(conn, settings, "INTERIOR_CORRIDOR", "INTERIOR_CORRIDOR", 5)
    _reviewed(conn, settings, "DIRECT_ENTRY", "MIXED", 50)                # yellow
    _reviewed(conn, settings, "UNKNOWN", "UNKNOWN", 50, quality="poor")   # AI needed review
    card = scorecard(conn, MARKET)
    assert card["reviewed"] == 22 and card["with_ai"] == 22
    rows_ = card["confusion"]["rows"]
    assert rows_["EXTERIOR_CORRIDOR"]["EXTERIOR_CORRIDOR"] == 9 and rows_["MIXED"]["EXTERIOR_CORRIDOR"] == 1
    assert rows_["DIRECT_ENTRY"]["MIXED"] == 1
    assert card["confusion"]["matrix"][0][0] == 9 and card["confusion"]["labels"][0] == "EXTERIOR_CORRIDOR"
    assert card["label_accuracy"] == pytest.approx(round(100 * 20 / 22, 1))
    by = {b["color"]: b for b in card["by_color"]}
    assert by["green"]["n"] == 10 and by["green"]["target"] == 9 and by["green"]["accuracy"] == 90.0
    assert by["red"]["n"] == 10 and by["red"]["accuracy"] == 100.0
    assert by["yellow"]["n"] == 1 and by["na"]["n"] == 1 and by["na"]["unknown"] == 1
    acc = card["acceptance"]
    assert acc["green"]["passed"] is True and acc["red"]["passed"] is True and acc["passed"] is True
    assert "Acceptance check passed" in card["message"]


def test_scorecard_failing_and_too_few(conn, settings):
    for _ in range(10):
        _reviewed(conn, settings, "INTERIOR_CORRIDOR", "EXTERIOR_CORRIDOR", 95)   # confident and wrong
    _reviewed(conn, settings, "INTERIOR_CORRIDOR", "INTERIOR_CORRIDOR", 5)
    card = scorecard(conn, MARKET)
    assert card["acceptance"]["green"]["passed"] is False
    assert card["acceptance"]["red"]["passed"] is None and "Need at least 10" in card["acceptance"]["red"]["text"]
    assert card["acceptance"]["passed"] is None


def test_scorecard_uses_the_ai_answer_not_yours(conn, settings):
    """After a review final_p is yours (100); the scorecard still judges the AI's own red verdict
    (5% blended with a neutral prior: 0.8 * 5 + 0.2 * 50 = 14)."""
    pid = _reviewed(conn, settings, "DIRECT_ENTRY", "INTERIOR_CORRIDOR", 5)
    assert visual(conn, pid)["final_p"] == 100
    by = {b["color"]: b for b in scorecard(conn, MARKET)["by_color"]}
    assert by["red"]["n"] == 1 and by["red"]["accuracy"] == 0.0


def test_empty_scorecard_messages(conn, settings):
    assert "No reviews yet" in scorecard(conn, MARKET)["message"]
    pid = add(conn, settings)
    save_review(conn, pid, "MIXED")
    assert "hasn't checked" in scorecard(conn, MARKET)["message"]


# --- calibrate -------------------------------------------------------------------------------

def test_calibrate_rechecks_reviews_and_writes_the_scorecard(conn, settings):
    a = add(conn, settings)
    b = add(conn, settings)
    add(conn, settings)  # not reviewed: calibrate leaves it alone
    save_review(conn, a, "DIRECT_ENTRY")
    save_review(conn, b, "INTERIOR_CORRIDOR")
    res = calibrate(conn, MARKET, backend=MockBackend())
    assert res["classified"] == 2 and res["error"] is None
    assert res["scorecard"]["with_ai"] == 2 and res["scorecard"]["calibrated_at"]
    assert visual(conn, a)["final_p"] == 100 and visual(conn, b)["final_p"] == 0   # your answers stay
    assert visual(conn, a)["ai_label"] is not None
    saved = json.loads((settings.path("labels") / "scorecard.json").read_text())
    assert saved["markets"][MARKET]["calibrated_at"]
    assert "Calibrate" in res["message"]


def test_calibrate_dry_run_and_stop(conn, settings):
    pid = add(conn, settings)
    save_review(conn, pid, "MIXED")
    est = calibrate(conn, MARKET, backend=MockBackend(), dry_run=True)
    assert est["dry_run"] and est["calls"] == 1 and est["reviewed_only"] is True

    class Broken(VisionBackend):
        name = "broken"

        @property
        def model(self):
            return "x"

        def classify(self, image_paths, prompt, *, schema=None):
            from bluemoon.imagery.vision import VisionFatalError
            raise VisionFatalError("Claude Code isn't signed in.")

    res = calibrate(conn, MARKET, backend=Broken())
    assert res["stopped"] and "Stopped early" in res["message"]


def test_calibrate_raw_land(conn, settings):
    with pytest.raises(ValueError, match="no photo check"):
        calibrate(conn, "land-springfield-oh", backend=MockBackend())


def test_module_exports(settings):
    import bluemoon.imagery as im
    for name in ("fetch_imagery", "imagery_estimate", "classify_properties", "get_backend", "build_prompt",
                 "parse_result", "review_queue", "save_review", "examples_for", "scorecard", "calibrate"):
        assert callable(getattr(im, name))
    assert review.EXAMPLES_FILE == "examples.jsonl"
