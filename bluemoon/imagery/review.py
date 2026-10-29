"""Human review of the photo check: the queue, your answers, the example library and the scorecard.

- `review_queue`: what to look at next. Photos the AI couldn't judge (needs review) and unsure
  (yellow, 21-79) results first, then photos the AI hasn't checked yet, then places with no photos, then
  every call the AI made (green and red) for you to confirm or correct, most valuable first: buy-box places,
  the ones the AI says are targets (walk-ups), then the best Blue Moon rank. `scope='unsure'` ("Unsure only")
  leaves out the AI's calls (unless `review.spot_check_fraction` asks for a small, stable random sample).
- `save_review`: your answer wins. It sets `reviewed_label` and final_p (target 100, mixed 60, other 0),
  copies the photos into `labels/examples/{property_id}/`, appends a line to `labels/examples.jsonl`
  (with `agreed`: did you agree with the AI?), and updates `labels/scorecard.json`.
  "Yes, the AI is right" is `agreed=True` (the AI's own label is saved); "No, it's wrong" is `agreed=False`
  with the label you pick.
- `examples_for`: labeled examples the AI sees with each new property (no model training: Claude just
  reads your past answers). Most recent first, mixed across labels, capped.
- `scorecard`: how often the AI is right: overall, per label, a trend over time (your latest 50 reviews vs the
  earlier ones), confusion matrix, accuracy per color, and the acceptance check (green >= 90% truly target,
  red >= 90% truly not).
- `calibrate`: re-run the AI on everything you reviewed (without touching your answers) and score it.

`labels/` is private (git-ignored): it holds photos of real properties.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import AssetClass, Settings, get_settings
from ..db import loads, market_by_slug, now_iso, one, rows
from . import streetview

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

EXAMPLES_FILE = "examples.jsonl"
EXAMPLES_DIR = "examples"
SCORECARD_FILE = "scorecard.json"
UNKNOWN = "UNKNOWN"
EXAMPLE_IMAGE_ORDER = ("sv_0.jpg", streetview.SAT_FILE, "sv_1.jpg", "sv_2.jpg")


# --- settings ---------------------------------------------------------------------------------

class ReviewConfig(BaseModel):
    """`review:` in config.yaml (colors come from `scoring:` so every screen agrees)."""
    model_config = ConfigDict(extra="ignore")
    green_min: float = 80.0                # scoring.visual_green_min
    red_max: float = 20.0                  # scoring.visual_red_max
    spot_check_fraction: float = Field(0.0, ge=0, le=1)     # random share of the AI's calls in 'Unsure only'
    acceptance_min: float = Field(0.90, gt=0, le=1)
    acceptance_min_reviews: int = Field(10, ge=1)
    reviewed_p_target: float = Field(100.0, ge=0, le=100)
    reviewed_p_mixed: float = Field(60.0, ge=0, le=100)
    reviewed_p_other: float = Field(0.0, ge=0, le=100)
    mixed_labels: list[str] = Field(default_factory=lambda: ["MIXED"])
    include_unchecked: bool = False        # also list properties whose photos the AI hasn't looked at yet
    default_scope: str = "everything"      # everything | unsure: what the review list starts as
    learning_window: int = Field(50, ge=5)  # Scorecard "learning": your latest this-many reviews vs earlier ones


def review_config(settings: Settings | None = None) -> ReviewConfig:
    s = settings or get_settings()
    raw = dict(s.section("review"))
    scoring = s.section("scoring")
    if scoring.get("visual_green_min") is not None:
        raw.setdefault("green_min", scoring["visual_green_min"])
    if scoring.get("visual_red_max") is not None:
        raw.setdefault("red_max", scoring["visual_red_max"])
    try:
        return ReviewConfig(**{k: v for k, v in raw.items() if v is not None})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"config.yaml review setting '{where}' has a value I can't use: {first.get('msg')}") from None


# --- colors and labels ------------------------------------------------------------------------

def visual_color(final_p: float | None, cfg: ReviewConfig | None = None) -> str:
    """green | yellow | red, or na when nobody (AI or human) has a verdict yet."""
    c = cfg or review_config()
    if final_p is None:
        return "na"
    if final_p >= c.green_min:
        return "green"
    if final_p <= c.red_max:
        return "red"
    return "yellow"


def labels_of(ac: AssetClass) -> list[str]:
    return [str(x).strip().upper() for x in (ac.visual_check.get("labels") or [])]


def readable_label(label: str | None, ac: AssetClass | None = None) -> str:
    """'EXTERIOR_CORRIDOR' -> 'Exterior corridor' (or the asset class's visual_check.label_names entry)."""
    code = str(label or "").strip()
    names = ((ac.visual_check.get("label_names") or {}) if ac is not None else {}) or {}
    if code in names:
        return str(names[code])
    text = code.replace("_", " ").strip().lower()
    return text[:1].upper() + text[1:] if text else ""


def target_labels_of(ac: AssetClass) -> list[str]:
    return [str(x).strip().upper() for x in (ac.visual_check.get("target_labels") or [])]


def normalize_label(label: Any, ac: AssetClass) -> str:
    """Exact label from the asset class; also accepts its number in the list ("1" = first label)."""
    labels = labels_of(ac)
    text = str(label or "").strip().upper().replace(" ", "_").replace("-", "_")
    if text.isdigit() and 1 <= int(text) <= len(labels):
        return labels[int(text) - 1]
    if text not in labels:
        raise ValueError(f"'{label}' isn't one of the answers for {ac.label}: {', '.join(labels)}.")
    return text


def reviewed_final_p(label: str, ac: AssetClass, cfg: ReviewConfig | None = None) -> float | None:
    """Your answer as a probability: target 100, mixed 60, anything else 0; UNKNOWN stays unknown."""
    c = cfg or review_config()
    if label == UNKNOWN:
        return None
    if label in target_labels_of(ac):
        return c.reviewed_p_target
    if label in [m.upper() for m in c.mixed_labels]:
        return c.reviewed_p_mixed
    return c.reviewed_p_other


def truth_of(label: str | None, ac: AssetClass) -> bool | None:
    """Is the reviewed label a target? None when the reviewer couldn't tell."""
    if not label or label == UNKNOWN:
        return None
    return label in target_labels_of(ac)


def _visual_or_raise(ac: AssetClass) -> None:
    if not ac.visual_enabled:
        raise ValueError(f"{ac.label} has no photo check.")


# --- the queue --------------------------------------------------------------------------------

_QUEUE_SQL = """
SELECT p.id, p.address, p.city, p.state, p.zip, p.county, p.lat, p.lng, p.units, p.stories, p.year_built,
       p.building_sqft, p.lot_acres, p.name, p.crexi_url,
       v.status, v.pano_id, v.pano_date, v.heading, v.images_json, v.ai_label, v.ai_p, v.image_quality,
       v.property_visible, v.cues_json, v.reasons, v.prior, v.final_p, v.model, v.error, v.checked_at,
       v.ai_style, v.ai_stories, v.ai_buildings, v.ai_condition,
       s.in_buybox, s.score, s.rank
FROM visual_checks v
JOIN properties p ON p.id = v.property_id
LEFT JOIN scores s ON s.property_id = p.id
WHERE p.market_id = ? AND v.reviewed_label IS NULL
"""

GROUP_ORDER = {"needs_review": 0, "unsure": 1, "not_checked": 2, "no_photos": 3, "confirm": 4, "spot_check": 4}
GROUP_WHY = {
    "needs_review": "The AI couldn't judge these photos (poor photos, or the building isn't visible).",
    "unsure": "The AI is unsure.",
    "not_checked": "Photos are saved but the AI hasn't looked yet.",
    "no_photos": "No usable photos. Use the Google Maps links to look.",
    "confirm": "The AI made a call. Is it right?",
    "spot_check": "Spot check: the AI was confident. Is it right?",
}
SCOPES = ("everything", "unsure")
AI_CALLED = ("confirm", "spot_check")     # groups where the AI made a call and you confirm or correct it


def spot_check_pick(property_id: int, salt: str, fraction: float) -> bool:
    """Stable 'random' pick, so the same spot checks show until you answer them."""
    if fraction <= 0:
        return False
    h = int(hashlib.sha256(f"{salt}:{int(property_id)}".encode()).hexdigest()[:8], 16)
    return (h % 10_000) < fraction * 10_000


def normalize_scope(scope: str | None, cfg: ReviewConfig) -> str:
    """'everything' (the AI's unsure ones first, then every call to confirm) or 'unsure' (only the unsure ones,
    plus the small random spot check)."""
    text = str(scope or cfg.default_scope or "everything").strip().lower()
    if text in ("all", "everything"):
        return "everything"
    if text in ("unsure", "unsure_only"):
        return "unsure"
    raise ValueError(f"'{scope}' isn't a review list. Choose: everything or unsure.")


def _group(r: Mapping[str, Any], has_images: bool, cfg: ReviewConfig, salt: str, scope: str = "everything") -> str | None:
    p = r.get("final_p")
    classified = r.get("ai_label") is not None and r.get("checked_at") is not None
    if p is None:
        if classified:
            return "needs_review"
        if has_images:
            return "not_checked" if cfg.include_unchecked else None   # the AI looks first (Run: Photo check)
        return "no_photos" if r.get("status") == "no_imagery" else None
    color = visual_color(p, cfg)
    if color == "yellow":
        return "unsure"
    if scope == "unsure":
        return "spot_check" if spot_check_pick(r["id"], salt, cfg.spot_check_fraction) else None
    return "confirm"


def _sort_key(group: str, r: Mapping[str, Any], targets: frozenset[str] = frozenset()) -> tuple:
    """Unsure first. Among the AI's calls, the most valuable first: places in the buy box, the ones the AI says
    are targets (walk-ups), then the best Blue Moon rank."""
    in_box = r.get("in_buybox")
    rank = r.get("rank")
    not_target_call = group in AI_CALLED and str(r.get("ai_label") or "") not in targets
    return (GROUP_ORDER[group], in_box == 0, not_target_call, rank is None, rank or 0, -(r.get("score") or 0.0), r["id"])


def _label_choices(ac: AssetClass) -> list[dict]:
    helps = ac.visual_check.get("label_help") or {}
    targets = set(target_labels_of(ac))
    return [{"key": i + 1, "label": lab, "help": helps.get(lab, ""), "target": lab in targets}
            for i, lab in enumerate(labels_of(ac))]


def _target_name(ac: AssetClass) -> str:
    return str(ac.visual_check.get("target_name") or "target")


def _why(group: str, r: Mapping[str, Any], ac: AssetClass) -> str:
    p = r.get("final_p")
    if group == "unsure" and p is not None:
        return f"The AI is unsure ({p:.0f}% likely a {_target_name(ac).lower()})."
    if group in AI_CALLED and r.get("ai_label"):
        is_target = str(r["ai_label"]) in target_labels_of(ac)
        name = _target_name(ac).lower()
        verdict = f"a {name}" if is_target else f"not a {name}"
        sure = ""
        if r.get("ai_p") is not None:
            sure = f" ({float(r['ai_p']) if is_target else 100 - float(r['ai_p']):.0f}% sure)"
        return f"The AI says: {readable_label(r['ai_label'], ac)}, so {verdict}{sure}. Is it right?"
    return GROUP_WHY[group]


def queue_item(r: Mapping[str, Any], ac: AssetClass, settings: Settings, group: str,
               cfg: ReviewConfig) -> dict:
    """One review card."""
    p = r.get("final_p")
    ai_called = group in AI_CALLED
    ai_label = r.get("ai_label")
    return {
        "id": r["id"], "property_id": r["id"],
        "address": r.get("address"), "city": r.get("city"), "state": r.get("state"), "zip": r.get("zip"),
        "county": r.get("county"), "name": r.get("name"), "lat": r.get("lat"), "lng": r.get("lng"),
        "units": r.get("units"), "stories": r.get("stories"), "year_built": r.get("year_built"),
        "crexi_url": r.get("crexi_url"),
        "group": group, "why": _why(group, r, ac),
        "ai_called": ai_called,
        "ai_is_target": bool(ai_label) and str(ai_label) in target_labels_of(ac),
        "ai_label_name": readable_label(ai_label, ac) if ai_label else None,
        "target_name": _target_name(ac),
        "status": r.get("status"),
        "ai_label": ai_label, "ai_p": r.get("ai_p"), "final_p": p, "prior": r.get("prior"),
        "color": visual_color(p, cfg), "needs_review": p is None,
        "image_quality": r.get("image_quality"),
        "property_visible": None if r.get("property_visible") is None else bool(r.get("property_visible")),
        "cues": loads(r.get("cues_json"), []) or [], "reasons": r.get("reasons"), "model": r.get("model"),
        "details": details_of(r),
        "pano_date": r.get("pano_date"),
        "images": streetview.images_for(r["id"], settings),
        "links": streetview.map_links(r.get("lat"), r.get("lng"), r.get("heading")),
        "score": r.get("score"), "rank": r.get("rank"), "in_buybox": r.get("in_buybox"),
        "question": ac.visual_check.get("question"),
        "choices": _label_choices(ac),
    }


STYLE_NAMES = {"garden": "Garden style", "townhome-row": "Townhome row", "mid-rise": "Mid-rise",
               "high-rise": "High-rise", "converted": "Converted building", "other": "Other style"}
CONDITION_NAMES = {"good": "Good condition", "fair": "Fair condition", "poor": "Poor condition",
                   "unknown": "Condition unclear"}


def details_of(r: Mapping[str, Any]) -> dict | None:
    """The AI's first-pass building details from a visual_checks row, or None when it gave none."""
    style, stories = r.get("ai_style"), r.get("ai_stories")
    buildings, condition = r.get("ai_buildings"), r.get("ai_condition")
    if style is None and stories is None and buildings is None and condition is None:
        return None
    return {"style": style, "style_name": STYLE_NAMES.get(str(style), style),
            "stories": stories, "buildings": buildings,
            "condition": condition, "condition_name": CONDITION_NAMES.get(str(condition), condition)}


def details_text(r: Mapping[str, Any]) -> str | None:
    """'Garden style, about 3 stories, 4 buildings, fair condition' for lists and exports."""
    d = details_of(r)
    if not d:
        return None
    parts = [d["style_name"],
             f"about {d['stories']} {'story' if d['stories'] == 1 else 'stories'}" if d["stories"] else None,
             f"{d['buildings']} building{'' if d['buildings'] == 1 else 's'}" if d["buildings"] else None,
             (d["condition_name"] or "").lower() or None]
    return ", ".join(p for p in parts if p) or None


def _set_aside(r: Mapping[str, Any], include_excluded: bool) -> bool:
    """Leave a property outside the buy box off the list? Never once the AI has made its call: the photo button
    runs the AI on every lead, so every call it made reaches the list (after the buy-box ones, see _sort_key).
    Only outside-the-buy-box photos the AI hasn't judged yet wait for include_excluded."""
    if r.get("in_buybox") != 0 or include_excluded:
        return False
    return r.get("ai_label") is None or r.get("checked_at") is None


def _picked(conn: sqlite3.Connection, market: dict, ac: AssetClass, settings: Settings, cfg: ReviewConfig,
            slug: str, scope: str, include_excluded: bool) -> list[tuple[tuple, str, dict]]:
    targets = frozenset(target_labels_of(ac))
    picked: list[tuple[tuple, str, dict]] = []
    for r in rows(conn, _QUEUE_SQL, (market["id"],)):
        if _set_aside(r, include_excluded):
            continue
        has_images = bool(streetview.existing_images(settings, r["id"]))
        group = _group(r, has_images, cfg, slug, scope)
        if group is not None:
            picked.append((_sort_key(group, r, targets), group, r))
    picked.sort(key=lambda t: t[0])
    return picked


def review_queue(conn: sqlite3.Connection, market_slug: str, limit: int = 50, offset: int = 0, *,
                 include_excluded: bool = False, scope: str | None = None) -> list[dict]:
    """Properties to review, most useful first (see the module docstring for the order).
    scope: 'everything' (default: unsure first, then every call the AI made) or 'unsure' (only the unsure ones)."""
    settings = get_settings()
    cfg = review_config(settings)
    market = market_by_slug(conn, market_slug)
    ac = settings.asset_class(market["asset_class"])
    if not ac.visual_enabled:
        return []
    picked = _picked(conn, market, ac, settings, cfg, market_slug, normalize_scope(scope, cfg), include_excluded)
    window = picked[max(0, int(offset)): max(0, int(offset)) + max(0, int(limit))]
    return [queue_item(r, ac, settings, group, cfg) for _, group, r in window]


def queue_counts(conn: sqlite3.Connection, market_slug: str, *, include_excluded: bool = False,
                 scope: str | None = None) -> dict:
    """How many cards wait in each part of the queue, plus how many you've reviewed.
    `scopes` has the size of each list (everything / unsure) so the screen can offer both."""
    settings = get_settings()
    cfg = review_config(settings)
    market = market_by_slug(conn, market_slug)
    ac = settings.asset_class(market["asset_class"])
    chosen = normalize_scope(scope, cfg)
    counts: Counter = Counter()
    sizes = {"everything": 0, "unsure": 0}
    if ac.visual_enabled:
        for r in rows(conn, _QUEUE_SQL, (market["id"],)):
            if _set_aside(r, include_excluded):
                continue
            has_images = bool(streetview.existing_images(settings, r["id"]))
            for name in SCOPES:
                group = _group(r, has_images, cfg, market_slug, name)
                if group:
                    sizes[name] += 1
                    if name == chosen:
                        counts[group] += 1
    reviewed = one(conn, "SELECT COUNT(*) AS n FROM visual_checks v JOIN properties p ON p.id = v.property_id "
                         "WHERE p.market_id = ? AND v.reviewed_label IS NOT NULL", (market["id"],))
    return {"total": sum(counts.values()), "scope": chosen, "scopes": sizes,
            **{g: counts.get(g, 0) for g in GROUP_ORDER},
            "reviewed": int((reviewed or {}).get("n") or 0)}


# --- your answers -----------------------------------------------------------------------------

class ExampleRecord(BaseModel):
    """One line of labels/examples.jsonl."""
    model_config = ConfigDict(extra="ignore")
    property_id: int
    market: str | None = None
    asset_class: str
    label: str
    note: str | None = None
    images: list[str] = Field(default_factory=list)   # relative to labels/
    ai_label: str | None = None
    ai_p: float | None = None
    agreed: bool | None = None            # True: you confirmed the AI's call; False: you corrected it
    reviewed_at: str


def labels_dir(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).path("labels")


def _copy_examples(settings: Settings, property_id: int) -> list[str]:
    """Copy the property's photos into labels/examples/{id}/ (best photos first). Returns paths
    relative to labels/."""
    src_names = streetview.existing_images(settings, property_id)
    ordered = [n for n in EXAMPLE_IMAGE_ORDER if n in src_names] + \
              [n for n in src_names if n not in EXAMPLE_IMAGE_ORDER]
    dest = labels_dir(settings) / EXAMPLES_DIR / str(int(property_id))
    if dest.exists():
        shutil.rmtree(dest)
    if not ordered:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    src = streetview.property_dir(settings, property_id)
    out = []
    for name in ordered:
        shutil.copy2(src / name, dest / name)
        out.append(f"{EXAMPLES_DIR}/{int(property_id)}/{name}")
    return out


def _append_example(settings: Settings, rec: ExampleRecord) -> None:
    path = labels_dir(settings) / EXAMPLES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(rec.model_dump_json() + "\n")


def _property_context(conn: sqlite3.Connection, property_id: int) -> dict:
    prop = one(conn, "SELECT p.id, p.lat, p.lng, m.slug AS market_slug, m.asset_class "
                     "FROM properties p JOIN markets m ON m.id = p.market_id WHERE p.id = ?", (int(property_id),))
    if not prop:
        raise KeyError(f"Property {property_id} wasn't found.")
    return prop


def save_review(conn: sqlite3.Connection, property_id: int, label: str | int | None = None,
                note: str | None = None, agreed: bool | None = None) -> dict:
    """Save your answer for one property. Your answer always wins over the AI.

    agreed=True ("Yes, the AI is right"): the AI's own label is your answer.
    agreed=False ("No, it's wrong"): you must pick the right label, and it must differ from the AI's.
    agreed=None: just your label, as before (agreed is then worked out from what the AI said).
    """
    settings = get_settings()
    cfg = review_config(settings)
    prop = _property_context(conn, property_id)
    ac = settings.asset_class(prop["asset_class"])
    _visual_or_raise(ac)
    existing = one(conn, "SELECT ai_label, ai_p FROM visual_checks WHERE property_id = ?", (prop["id"],)) or {}
    ai_label = existing.get("ai_label")
    if agreed is True and (label is None or str(label).strip() == ""):
        if not ai_label or ai_label == UNKNOWN:
            raise ValueError("The AI didn't make a call here, so there is nothing to confirm. Pick the answer yourself.")
        label = ai_label
    if label is None or str(label).strip() == "":
        raise ValueError("Pick an answer first.")
    lab = normalize_label(label, ac)
    if agreed is True and ai_label and lab != ai_label:
        raise ValueError(f"The AI said {readable_label(ai_label, ac)}, not {readable_label(lab, ac)}. "
                         "Use 'No, it's wrong' to give your answer.")
    if agreed is False and ai_label and lab == ai_label:
        raise ValueError(f"That is what the AI said ({readable_label(lab, ac)}). "
                         "Pick a different answer, or use 'Yes, the AI is right'.")
    did_agree = None if not ai_label else bool(ai_label == lab)
    note = (note or "").strip() or None
    final_p = reviewed_final_p(lab, ac, cfg)
    when = now_iso()
    with conn:
        streetview.update_visual(conn, prop["id"], {"reviewed_label": lab, "reviewed_note": note,
                                                    "reviewed_at": when, "final_p": final_p})
    images = _copy_examples(settings, prop["id"])
    _append_example(settings, ExampleRecord(
        property_id=prop["id"], market=prop["market_slug"], asset_class=ac.key, label=lab, note=note,
        images=images, ai_label=ai_label, ai_p=existing.get("ai_p"), agreed=did_agree, reviewed_at=when))
    card = scorecard(conn, prop["market_slug"])
    write_scorecard(card, settings)
    saved = readable_label(lab, ac)
    if did_agree is True:
        message = f"Saved: {saved}. The AI was right; this is now a good example for it."
    elif did_agree is False:
        message = f"Saved: {saved}. The AI had guessed {readable_label(ai_label, ac)}; your answer teaches it."
    else:
        message = f"Saved: {saved}."
    return {"property_id": prop["id"], "label": lab, "note": note, "final_p": final_p,
            "color": visual_color(final_p, cfg), "examples_saved": len(images),
            "agreed_with_ai": did_agree, "agreed": did_agree,
            "scorecard": _scorecard_brief(card),
            "label_name": saved,
            "message": message + ("" if images else " (No photos to add to the example library.)")}


# --- the example library ----------------------------------------------------------------------

def read_examples(settings: Settings | None = None) -> list[ExampleRecord]:
    """Every saved answer, oldest first. Bad lines are skipped."""
    path = labels_dir(settings) / EXAMPLES_FILE
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(ExampleRecord.model_validate_json(line))
            except ValidationError:
                log.warning("Skipping a damaged line in %s", EXAMPLES_FILE)
    return out


def _latest_per_property(records: Iterable[ExampleRecord]) -> list[ExampleRecord]:
    latest: dict[int, ExampleRecord] = {}
    for rec in records:  # later lines win
        latest[rec.property_id] = rec
    return sorted(latest.values(), key=lambda r: r.reviewed_at, reverse=True)


def _round_robin(records: list[ExampleRecord], k: int) -> list[ExampleRecord]:
    """Most recent first, but mixed across labels so the AI sees every kind of answer."""
    by_label: dict[str, list[ExampleRecord]] = defaultdict(list)
    for rec in records:
        by_label[rec.label].append(rec)
    order = sorted(by_label, key=lambda lab: by_label[lab][0].reviewed_at, reverse=True)
    picked: list[ExampleRecord] = []
    while len(picked) < k and any(by_label[lab] for lab in order):
        for lab in order:
            if by_label[lab] and len(picked) < k:
                picked.append(by_label[lab].pop(0))
    return picked


def examples_for(asset_class: str | AssetClass, k: int, *, exclude_property_ids: Iterable[int] = (),
                 settings: Settings | None = None) -> list[dict]:
    """Up to k labeled examples with photos for this asset class:
    [{property_id, label, note, images: [absolute Path], reviewed_at, asset_class}]."""
    s = settings or get_settings()
    key = asset_class.key if isinstance(asset_class, AssetClass) else str(asset_class)
    if k <= 0:
        return []
    skip = {int(x) for x in exclude_property_ids}
    base = labels_dir(s)
    usable: list[ExampleRecord] = []
    for rec in _latest_per_property(read_examples(s)):
        if rec.asset_class != key or rec.property_id in skip:
            continue
        paths = [base / rel for rel in rec.images if (base / rel).is_file()]
        if paths:
            usable.append(rec.model_copy(update={"images": [str(p) for p in paths]}))
    return [{"property_id": r.property_id, "label": r.label, "note": r.note, "images": [Path(p) for p in r.images],
             "reviewed_at": r.reviewed_at, "asset_class": r.asset_class, "ai_label": r.ai_label,
             "agreed": r.agreed} for r in _round_robin(usable, k)]


# --- the scorecard ----------------------------------------------------------------------------

_REVIEWED_SQL = """
SELECT v.property_id, v.reviewed_label, v.ai_label, v.ai_p, v.prior, v.image_quality, v.property_visible,
       v.checked_at, v.reviewed_at
FROM visual_checks v JOIN properties p ON p.id = v.property_id
WHERE p.market_id = ? AND v.reviewed_label IS NOT NULL
"""


def _pct(a: int, b: int) -> float | None:
    return round(100.0 * a / b, 1) if b else None


def _ai_color(r: Mapping[str, Any], ac: AssetClass, cfg: ReviewConfig, settings: Settings) -> str:
    from .vision import ai_final_p  # local import: vision imports this module
    return visual_color(ai_final_p(r, settings), cfg)


def _acceptance_line(color: str, n: int, right: int, cfg: ReviewConfig) -> dict:
    goal = cfg.acceptance_min * 100
    pct = _pct(right, n)
    passed = None if n < cfg.acceptance_min_reviews else (pct is not None and pct >= goal)
    what = "really a target" if color == "green" else "really not a target"
    if n == 0:
        text = f"{color.title()}: no reviewed {color}s yet."
    else:
        text = f"{color.title()}: {right} of {n} were {what} ({pct:.0f}%). Goal: {goal:.0f}%."
        if n < cfg.acceptance_min_reviews:
            text += f" Need at least {cfg.acceptance_min_reviews} reviewed {color}s to judge."
    return {"n": n, "correct": right, "pct": pct, "goal_pct": goal, "passed": passed, "text": text}


def scorecard(conn: sqlite3.Connection, market_slug: str) -> dict:
    """How the AI did on the properties you reviewed."""
    settings = get_settings()
    cfg = review_config(settings)
    market = market_by_slug(conn, market_slug)
    ac = settings.asset_class(market["asset_class"])
    labels = labels_of(ac)
    reviewed = rows(conn, _REVIEWED_SQL, (market["id"],))
    with_ai = [r for r in reviewed if r.get("ai_label") and r.get("checked_at")]
    confusion = {t: {a: 0 for a in labels} for t in labels}
    agree = 0
    binary_right = binary_n = 0
    buckets: dict[str, dict] = {c: {"color": c, "n": 0, "target": 0, "not_target": 0, "unknown": 0}
                                for c in ("green", "yellow", "red", "na")}
    for r in with_ai:
        truth_label, ai_label = r["reviewed_label"], r["ai_label"]
        if truth_label in confusion and ai_label in confusion[truth_label]:
            confusion[truth_label][ai_label] += 1
        agree += int(truth_label == ai_label)
        truth = truth_of(truth_label, ac)
        b = buckets[_ai_color(r, ac, cfg, settings)]
        b["n"] += 1
        b["unknown" if truth is None else ("target" if truth else "not_target")] += 1
        guess = truth_of(ai_label, ac)
        if truth is not None and guess is not None:
            binary_n += 1
            binary_right += int(truth == guess)
    by_color = []
    for c, b in buckets.items():
        judged = b["target"] + b["not_target"]
        correct = b["target"] if c == "green" else b["not_target"] if c == "red" else None
        b.update(judged=judged, correct=correct,
                 accuracy=_pct(correct, judged) if correct is not None else None,
                 target_pct=_pct(b["target"], judged))
        by_color.append(b)
    green = _acceptance_line("green", buckets["green"]["judged"], buckets["green"]["target"], cfg)
    red = _acceptance_line("red", buckets["red"]["judged"], buckets["red"]["not_target"], cfg)
    passed = None if green["passed"] is None or red["passed"] is None else bool(green["passed"] and red["passed"])
    card = {
        "market": market_slug, "asset_class": ac.key, "labels": labels, "target_labels": target_labels_of(ac),
        "target_name": _target_name(ac),
        "reviewed": len(reviewed), "with_ai": len(with_ai), "ai_right": agree,
        "label_accuracy": _pct(agree, len(with_ai)),
        "by_label": _by_label(with_ai, labels, ac),
        "learning": _learning(with_ai, cfg.learning_window),
        "target_accuracy": _pct(binary_right, binary_n),
        "confusion": {"labels": labels, "rows": confusion,
                      "matrix": [[confusion[t][a] for a in labels] for t in labels]},
        "by_color": by_color,
        "acceptance": {"green": green, "red": red, "passed": passed},
        "updated_at": now_iso(),
    }
    card["message"] = _scorecard_message(card)
    return card


def _by_label(with_ai: list[dict], labels: list[str], ac: AssetClass) -> list[dict]:
    """For each label: how often the AI was right when it said it (`pct`), and how often it found the ones you
    say are that label (`found_pct`)."""
    targets = set(target_labels_of(ac))
    out = []
    for lab in labels:
        said = [r for r in with_ai if r["ai_label"] == lab]
        truly = [r for r in with_ai if r["reviewed_label"] == lab]
        right = sum(1 for r in said if r["reviewed_label"] == lab)
        out.append({"label": lab, "name": readable_label(lab, ac), "target": lab in targets,
                    "ai_said": len(said), "right": right, "pct": _pct(right, len(said)),
                    "you_said": len(truly), "found": right, "found_pct": _pct(right, len(truly))})
    return out


def _chrono(with_ai: list[dict]) -> list[dict]:
    return sorted(with_ai, key=lambda r: (str(r.get("reviewed_at") or ""), int(r["property_id"])))


def _learning(with_ai: list[dict], window: int) -> dict:
    """Is the AI getting better? Your latest `window` reviews against all the earlier ones, plus a short trend
    (the share it got right in each run of window/2 reviews, oldest first)."""
    ordered = _chrono(with_ai)
    hits = [int(r["ai_label"] == r["reviewed_label"]) for r in ordered]
    recent, earlier = hits[-window:], hits[:-window] if len(hits) > window else []
    need = max(5, window // 5)

    def part(h: list[int]) -> dict:
        return {"n": len(h), "right": sum(h), "pct": _pct(sum(h), len(h))}
    rec, ear = part(recent), part(earlier)
    step = max(5, window // 2)
    trend = [{"from": i + 1, "to": min(i + step, len(hits)), "n": len(hits[i:i + step]),
              "right": sum(hits[i:i + step]), "pct": _pct(sum(hits[i:i + step]), len(hits[i:i + step])),
              "date": str(ordered[min(i + step, len(hits)) - 1].get("reviewed_at") or "")[:10] or None}
             for i in range(0, len(hits), step)]
    delta = None
    verdict = "not_enough"
    if len(earlier) >= need and len(recent) >= need:
        delta = round(rec["pct"] - ear["pct"], 1)
        verdict = "better" if delta >= 3 else "worse" if delta <= -3 else "steady"
    if verdict == "not_enough":
        text = (f"Not enough yet to say. Once you have more than {window} reviews, this compares your latest "
                f"{window} with the earlier ones.")
    else:
        word = {"better": "better", "worse": "worse", "steady": "about the same"}[verdict]
        text = (f"Your latest {rec['n']} reviews: the AI was right {rec['pct']:.0f}% of the time. Earlier: "
                f"{ear['pct']:.0f}% ({ear['n']} reviews). It is doing {word}.")
    return {"window": window, "recent": rec, "earlier": ear, "delta": delta, "verdict": verdict, "message": text,
            "trend": trend}


def _scorecard_message(card: dict) -> str:
    if not card["reviewed"]:
        return "No reviews yet. Answer a few photo cards and the scorecard fills in."
    if not card["with_ai"]:
        return (f"You reviewed {card['reviewed']:,}, but the AI hasn't checked those yet. "
                "Use Calibrate on the Run screen to compare.")
    acc = card["acceptance"]
    msg = (f"The AI matched your answer on {card['label_accuracy']:.0f}% of {card['with_ai']:,} reviewed "
           f"properties. {acc['green']['text']} {acc['red']['text']}")
    if acc["passed"] is True:
        msg += " Acceptance check passed."
    elif acc["passed"] is False:
        msg += " Acceptance check not passed yet: keep reviewing so the AI gets better examples."
    return msg


def _scorecard_brief(card: dict) -> dict:
    return {"reviewed": card["reviewed"], "with_ai": card["with_ai"], "label_accuracy": card["label_accuracy"],
            "passed": card["acceptance"]["passed"], "message": card["message"]}


def write_scorecard(card: dict, settings: Settings | None = None) -> Path:
    """Save the market's scorecard into labels/scorecard.json (one entry per market)."""
    s = settings or get_settings()
    path = labels_dir(s) / SCORECARD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except ValueError:
            data = {}
    data.setdefault("markets", {})[card["market"]] = card
    data["updated_at"] = now_iso()
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


# --- calibrate --------------------------------------------------------------------------------

def calibrate(conn: sqlite3.Connection, market_slug: str, backend: Any = None, *, limit: int | None = None,
              dry_run: bool = False, confirm: bool = False, progress: Progress = None) -> dict:
    """Re-run the AI on every property you reviewed (your answers stay), then score it.

    Each property is checked without its own example, so the AI can't copy your answer.
    """
    from . import vision  # local import: vision imports this module
    settings = get_settings()
    market = market_by_slug(conn, market_slug)
    ac = settings.asset_class(market["asset_class"])
    _visual_or_raise(ac)
    run = vision.classify_properties(conn, market_slug, limit=limit, dry_run=dry_run, backend=backend,
                                     progress=progress, confirm=confirm, reviewed_only=True)
    if dry_run or run.get("needs_confirm"):
        return run
    card = scorecard(conn, market_slug)
    card["calibrated_at"] = now_iso()
    write_scorecard(card, settings)
    msg = f"Calibrate: checked {run.get('classified', 0):,} reviewed properties again. {card['message']}"
    if run.get("error"):
        msg += f" Stopped early: {run['error']}"
    return {**run, "scorecard": card, "message": msg}


__all__ = ["ExampleRecord", "ReviewConfig", "calibrate", "details_of", "details_text", "examples_for", "labels_of",
           "normalize_label", "normalize_scope", "queue_counts", "read_examples", "review_config", "review_queue", "reviewed_final_p", "save_review",
           "scorecard", "target_labels_of", "visual_color", "write_scorecard"]
