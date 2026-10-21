"""Photo check: Street View + satellite photos, the AI's verdict, and your reviews.

    from bluemoon.imagery import fetch_imagery, classify_properties, review_queue, save_review
    fetch_imagery(conn, "apartments-springfield-oh", limit=5, dry_run=True)   # what it would cost
    fetch_imagery(conn, "apartments-springfield-oh", limit=5)                  # save photos
    classify_properties(conn, "apartments-springfield-oh", limit=5)            # Claude looks at them
    review_queue(conn, "apartments-springfield-oh")                            # what to review next
"""
from .review import (
    calibrate,
    examples_for,
    queue_counts,
    review_config,
    review_queue,
    save_review,
    scorecard,
    visual_color,
)
from .streetview import fetch_imagery, images_for, imagery_config, imagery_estimate, map_links
from .vision import (
    AnthropicAPIBackend,
    ClaudeCLIBackend,
    MockBackend,
    VisualResult,
    ai_final_p,
    build_prompt,
    claude_cli_available,
    classify_estimate,
    classify_properties,
    explain_prior,
    get_backend,
    parse_result,
    rules_prior,
    vision_config,
)

__all__ = [
    "AnthropicAPIBackend", "ClaudeCLIBackend", "MockBackend", "VisualResult", "ai_final_p", "build_prompt",
    "calibrate", "claude_cli_available", "classify_estimate", "classify_properties", "examples_for",
    "explain_prior", "fetch_imagery", "get_backend", "images_for", "imagery_config", "imagery_estimate",
    "map_links", "parse_result", "queue_counts", "review_config", "review_queue", "rules_prior", "save_review",
    "scorecard", "vision_config", "visual_color",
]
