"""The AI's calls to confirm ("Yes, the AI is right" / "No, it's wrong"), the review order, the Scorecard's
"is it learning" numbers and the richer first pass (style, stories, buildings, condition). Offline."""
from __future__ import annotations

import pytest
from test_review import MARKET, _reviewed, add, ai, set_config, visual

from bluemoon.imagery import review, vision
from bluemoon.imagery.review import (
    examples_for,
    read_examples,
    review_queue,
    save_review,
    scorecard,
)

DETAILS = {"ai_style": "garden", "ai_stories": 3, "ai_buildings": 4, "ai_condition": "fair"}


def test_calls_to_confirm_come_after_the_unsure_ones_most_valuable_first(tmp_root, conn, settings):
    set_config(tmp_root, "review.spot_check_fraction", 0)
    plain_red = add(conn, settings, rank=1, **ai("INTERIOR_CORRIDOR", 5, 4))
    walk_low = add(conn, settings, rank=8, **ai("EXTERIOR_CORRIDOR", 95, 92))
    walk_top = add(conn, settings, rank=2, **ai("DIRECT_ENTRY", 90, 88))
    outside_walk = add(conn, settings, in_buybox=0, rank=1, **ai("EXTERIOR_CORRIDOR", 95, 92))
    unsure = add(conn, settings, rank=50, **ai("MIXED", 50, 50))
    needs = add(conn, settings, rank=60, **ai("UNKNOWN", 50, None, quality="poor"))
    q = review_queue(conn, MARKET)
    # needs-a-look and unsure first; then the calls: walk-ups before not-walk-ups, best Blue Moon rank first
    assert [c["id"] for c in q] == [needs, unsure, walk_top, walk_low, plain_red]
    assert [c["ai_called"] for c in q] == [False, False, True, True, True]
    assert outside_walk in [c["id"] for c in review_queue(conn, MARKET, include_excluded=True)]
    assert [c["id"] for c in review_queue(conn, MARKET, scope="unsure")][:2] == [needs, unsure]
    assert [c["id"] for c in review_queue(conn, MARKET, scope="unsure")] == [needs, unsure]   # no AI calls at all


def test_unchecked_photos_stay_out_of_the_queue(conn, settings):
    add(conn, settings)                                          # photos saved, the AI hasn't looked
    assert review_queue(conn, MARKET) == []


def test_card_for_a_call_says_what_the_ai_said_and_shows_the_building(conn, settings):
    pid = add(conn, settings, rank=1, **ai("EXTERIOR_CORRIDOR", 90, 88), **DETAILS)
    (card,) = review_queue(conn, MARKET)
    assert card["group"] == "confirm" and card["ai_called"] and card["ai_is_target"] is True
    assert card["ai_label_name"] == "Exterior corridor" and card["target_name"] == "Walk-up"
    assert card["why"] == "The AI says: Exterior corridor, so a walk-up (90% sure). Is it right?"
    assert card["details"] == {"style": "garden", "style_name": "Garden style", "stories": 3, "buildings": 4,
                               "condition": "fair", "condition_name": "Fair condition"}
    assert review.details_text(visual(conn, pid)) == "Garden style, about 3 stories, 4 buildings, fair condition"
    red = add(conn, settings, rank=2, **ai("INTERIOR_CORRIDOR", 10, 12))
    cards = {c["id"]: c for c in review_queue(conn, MARKET)}
    assert cards[red]["why"] == "The AI says: Interior corridor, so not a walk-up (90% sure). Is it right?"
    assert cards[red]["details"] is None
    with pytest.raises(ValueError, match="isn't a review list"):
        review_queue(conn, MARKET, scope="random")


def test_yes_the_ai_is_right_saves_its_label_as_an_agreed_example(conn, settings):
    pid = add(conn, settings, **ai("DIRECT_ENTRY", 92, 90))
    res = save_review(conn, pid, agreed=True)
    assert res["label"] == "DIRECT_ENTRY" and res["agreed"] is True and res["final_p"] == 100
    assert "The AI was right" in res["message"]
    row = visual(conn, pid)
    assert row["reviewed_label"] == "DIRECT_ENTRY" and row["ai_label"] == "DIRECT_ENTRY"
    (rec,) = read_examples()
    assert rec.agreed is True and rec.label == "DIRECT_ENTRY" and rec.ai_label == "DIRECT_ENTRY"
    (ex,) = examples_for("apartments", 3)
    assert ex["agreed"] is True and ex["label"] == "DIRECT_ENTRY"
    assert review_queue(conn, MARKET) == []                      # answered cards leave the queue


def test_no_its_wrong_needs_the_right_label_and_teaches_the_ai(conn, settings):
    pid = add(conn, settings, **ai("INTERIOR_CORRIDOR", 8, 10))
    with pytest.raises(ValueError, match="what the AI said"):
        save_review(conn, pid, "interior corridor", agreed=False)
    with pytest.raises(ValueError, match="Pick an answer"):
        save_review(conn, pid, None, agreed=False)
    with pytest.raises(ValueError, match="not Mixed"):
        save_review(conn, pid, "MIXED", agreed=True)
    assert visual(conn, pid)["reviewed_label"] is None           # nothing saved by the refusals
    res = save_review(conn, pid, "EXTERIOR_CORRIDOR", "stairs on the side", agreed=False)
    assert res["agreed"] is False and res["final_p"] == 100 and "had guessed Interior corridor" in res["message"]
    (rec,) = read_examples()
    assert rec.agreed is False and rec.label == "EXTERIOR_CORRIDOR" and rec.ai_label == "INTERIOR_CORRIDOR"
    (ex,) = examples_for("apartments", 3)
    prompt = vision.build_prompt("apartments", [ex], settings=settings)
    assert "true label EXTERIOR_CORRIDOR" in prompt and "An earlier AI guess of INTERIOR_CORRIDOR was wrong" in prompt


def test_yes_with_no_ai_call_is_refused(conn, settings):
    pid = add(conn, settings)
    with pytest.raises(ValueError, match="nothing to confirm"):
        save_review(conn, pid, agreed=True)


def test_scorecard_right_by_label_and_learning_over_time(tmp_root, conn, settings):
    set_config(tmp_root, "review.learning_window", 10)
    for i in range(10):                                           # early on the AI was mostly wrong
        _reviewed(conn, settings, "EXTERIOR_CORRIDOR" if i < 3 else "DIRECT_ENTRY", "EXTERIOR_CORRIDOR", 95)
    for _ in range(10):                                           # lately it is always right
        _reviewed(conn, settings, "INTERIOR_CORRIDOR", "INTERIOR_CORRIDOR", 5)
    card = scorecard(conn, MARKET)
    assert card["ai_right"] == 13 and card["label_accuracy"] == 65.0
    labels = {b["label"]: b for b in card["by_label"]}
    assert labels["EXTERIOR_CORRIDOR"]["ai_said"] == 10 and labels["EXTERIOR_CORRIDOR"]["right"] == 3
    assert labels["EXTERIOR_CORRIDOR"]["pct"] == 30.0 and labels["EXTERIOR_CORRIDOR"]["target"] is True
    assert labels["INTERIOR_CORRIDOR"]["pct"] == 100.0 and labels["INTERIOR_CORRIDOR"]["found_pct"] == 100.0
    assert labels["DIRECT_ENTRY"]["ai_said"] == 0 and labels["DIRECT_ENTRY"]["pct"] is None
    assert labels["DIRECT_ENTRY"]["you_said"] == 7 and labels["DIRECT_ENTRY"]["found_pct"] == 0.0
    learn = card["learning"]
    assert learn["recent"] == {"n": 10, "right": 10, "pct": 100.0}
    assert learn["earlier"] == {"n": 10, "right": 3, "pct": 30.0}
    assert learn["verdict"] == "better" and learn["delta"] == 70.0 and "better" in learn["message"]
    assert len(learn["trend"]) >= 2 and learn["trend"][-1]["pct"] == 100.0


def test_learning_says_when_it_is_too_early(conn, settings):
    for _ in range(4):
        _reviewed(conn, settings, "INTERIOR_CORRIDOR", "INTERIOR_CORRIDOR", 5)
    learn = scorecard(conn, MARKET)["learning"]
    assert learn["verdict"] == "not_enough" and learn["delta"] is None and "Not enough yet" in learn["message"]


def test_empty_scorecard_has_the_new_parts(conn, settings):
    card = scorecard(conn, MARKET)
    assert card["ai_right"] == 0 and card["learning"]["verdict"] == "not_enough"
    assert all(b["ai_said"] == 0 for b in card["by_label"])


# --- the richer first pass --------------------------------------------------------------------

def reply(**extra) -> str:
    import json
    base = {"label": "EXTERIOR_CORRIDOR", "p_target": 90, "image_quality": "good", "visible_cues": ["stairs"],
            "reasons": "Open breezeways.", "property_visible": True}
    return json.dumps({**base, **extra})


def test_details_are_read_leniently():
    labels = ["EXTERIOR_CORRIDOR", "UNKNOWN"]
    res = vision.parse_result(reply(building_style="Garden-style", estimated_stories="about 3", buildings_visible=4.0,
                                    condition="Well kept"), labels)
    assert (res.building_style, res.estimated_stories, res.buildings_visible, res.condition) == ("garden", 3, 4, "good")
    odd = vision.parse_result(reply(building_style="castle", estimated_stories=0, buildings_visible="many",
                                    condition="???"), labels)
    assert (odd.building_style, odd.estimated_stories, odd.buildings_visible, odd.condition) == \
        ("other", None, None, "unknown")
    old = vision.parse_result(reply(), labels)                    # an older saved answer has no details
    assert (old.building_style, old.estimated_stories, old.buildings_visible, old.condition) == (None,) * 4
    assert vision.normalize_style("Townhomes") == "townhome-row" and vision.normalize_style("mid rise") == "mid-rise"
    assert vision.normalize_style("High-rise tower") == "high-rise" and vision.normalize_style("") is None


def test_the_apartment_prompt_and_schema_ask_for_the_details(settings):
    apt, wh = settings.asset_class("apartments"), settings.asset_class("warehouse")
    prompt = vision.build_prompt(apt, [], settings=settings)
    for text in ("building_style", "estimated_stories", "buildings_visible", "condition",
                 "garden|townhome-row|mid-rise|high-rise|converted|other", "ONE plain sentence"):
        assert text in prompt, text
    for label in ("EXTERIOR_CORRIDOR", "DIRECT_ENTRY", "INTERIOR_CORRIDOR", "MIXED", "UNKNOWN"):
        assert f"- {label}" in prompt                            # the labels are still exact
    schema = vision.result_schema(vision.review.labels_of(apt), details=vision.wants_details(apt))
    assert set(schema["required"]) >= {"building_style", "estimated_stories", "buildings_visible", "condition"}
    assert schema["properties"]["building_style"]["enum"] == list(vision.STYLES)
    assert vision.wants_details(wh) is False
    assert "building_style" not in vision.build_prompt(wh, [], settings=settings)          # other classes unchanged
    assert "building_style" not in vision.result_schema(["A", "B"])["properties"]


def test_the_check_saves_the_details_and_keeps_colors_by_confidence(conn, settings):
    pid = add(conn, settings, rank=1, status="done")
    res = vision.classify_properties(conn, MARKET, backend=vision.MockBackend())
    assert res["classified"] == 1
    row = visual(conn, pid)
    assert row["ai_style"] and row["ai_stories"] and row["ai_buildings"] and row["ai_condition"]
    assert row["ai_style"] in vision.STYLES and row["ai_condition"] in vision.CONDITIONS
