"""Buy-box decisions made by hand: tables buybox_overrides (what holds now) and buybox_override_log (every
change, never deleted).

A decision is (property, reason, decision). reason '*' is the whole property, or one rule key:
- ('*', include)   in the buy box whatever the rules say ("Add to buy box");
- ('*', exclude)   set aside by hand, even when the rules pass ("Set aside");
- (rule, include)  this rule no longer counts for the property ("Add only these": a group's rule is waived,
                   so a property that also fails another rule stays out);
- (rule, keep)     looked at it, the rule stands ("Keep set aside"; no effect on the answer).
"Follow the rules" deletes the decision. Each click is one batch (batch_id), undone together.

Decisions are keyed by property id, which stays the same when the same file is imported again, so they
survive re-imports and every re-score. They take effect in rank.compute_scores (buybox.apply_overrides); the
callers here re-score the market right after writing (about a second), so lists, counts and exports agree at
once. Who decided is auth.signed_in_as() on the dashboard ('this Mac', 'team', 'open' or an email) and 'cli'
on the command line.
"""
from __future__ import annotations

import secrets
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import loads, market_by_slug, now_iso, one, rows
from . import buckets as B
from .buybox import EXCLUSION_LABELS, EXCLUSION_REASONS, MANUAL_REASON, WHOLE, evaluate

RULE_KEYS: tuple[str, ...] = EXCLUSION_REASONS
REASONS: tuple[str, ...] = (WHOLE, *RULE_KEYS)
GROUP_DECISIONS: tuple[str, ...] = ("add_only", "add_all", "keep", "clear")
CLI_ACTOR = "cli"
HISTORY_LIMIT = 20
_CHUNK = 900   # SQLite parameter limit is 999


class DecisionIn(BaseModel):
    """One decision about one property, as the drawer sends it. 'rules' = follow the rules again (every
    decision of the property, or just the one for `reason`). check_decision says whether it fits."""
    decision: Literal["include", "exclude", "keep", "rules"]
    reason: str = Field(default=WHOLE, max_length=40)
    note: str | None = Field(default=None, max_length=4000)


def check_decision(decision: str, reason: str) -> None:
    """ValueError (shown to Seth) for a decision that can't be made."""
    if reason not in REASONS:
        raise ValueError(f"'{reason}' isn't a buy-box rule. Use * or one of: {', '.join(RULE_KEYS)}.")
    if decision not in ("include", "exclude", "keep", "rules"):
        raise ValueError(f"'{decision}' isn't a buy-box decision. Use include, exclude or rules.")
    if decision == "exclude" and reason != WHOLE:
        raise ValueError("Only the whole property can be set aside by hand.")
    if decision == "keep" and reason == WHOLE:
        raise ValueError("Keep set aside needs a rule (for example drive_time).")


class GroupDecisionIn(BaseModel):
    """One decision about a whole set-aside group (the Why? panel's Decide button)."""
    bucket: str = Field(max_length=120)
    decision: Literal["add_only", "add_all", "keep", "clear"]
    note: str | None = Field(default=None, max_length=4000)


def _chunks(ids: list[int]) -> Iterable[list[int]]:
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def new_batch_id() -> str:
    """One click = one batch: 'b20261021T221500-1a2b3c'."""
    return datetime.now(timezone.utc).strftime("b%Y%m%dT%H%M%S-") + secrets.token_hex(3)


def reason_label(reason: str) -> str:
    return "The whole property" if reason == WHOLE else EXCLUSION_LABELS.get(reason, reason)


# --- reading -------------------------------------------------------------------------------------

def load(conn: sqlite3.Connection, market_ids: int | Iterable[int]) -> dict[int, dict[str, dict]]:
    """{property id: {reason: buybox_overrides row}} for these markets (one query). Learning pools every
    market of an asset class, so this takes several market ids."""
    ids = [int(market_ids)] if isinstance(market_ids, int) else [int(m) for m in market_ids]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    out: dict[int, dict[str, dict]] = {}
    for r in rows(conn, f"SELECT o.* FROM buybox_overrides o JOIN properties p ON p.id = o.property_id "
                        f"WHERE p.market_id IN ({marks}) ORDER BY o.property_id, o.reason", ids):
        out.setdefault(int(r["property_id"]), {})[r["reason"]] = r
    return out


def for_property(conn: sqlite3.Connection, property_id: int) -> dict[str, dict]:
    """{reason: buybox_overrides row} for one property."""
    return {r["reason"]: r for r in rows(conn, "SELECT * FROM buybox_overrides WHERE property_id = ? "
                                               "ORDER BY reason", (property_id,))}


def history(conn: sqlite3.Connection, property_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    """The last decisions made or cleared for one property, newest first."""
    out = rows(conn, "SELECT id, reason, decision, previous, bucket, note, batch_id, actor, created_at "
                     "FROM buybox_override_log WHERE property_id = ? ORDER BY id DESC LIMIT ?",
               (property_id, int(limit)))
    for r in out:
        r["reason_label"] = reason_label(r["reason"])
    return out


def decisions_list(found: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    """The drawer's list: [{reason, reason_label, decision, bucket, note, decided_by, decided_at,
    detail_at_decision}], the whole-property decision first."""
    keys = sorted(found, key=lambda r: (r != WHOLE, REASONS.index(r) if r in REASONS else 99, r))
    keep = ("decision", "bucket", "note", "decided_by", "decided_at", "detail_at_decision", "batch_id")
    return [{"reason": k, "reason_label": reason_label(k), **{f: found[k].get(f) for f in keep}} for k in keys]


# --- writing -------------------------------------------------------------------------------------

def _current(conn: sqlite3.Connection, ids: list[int]) -> dict[tuple[int, str], dict]:
    out: dict[tuple[int, str], dict] = {}
    for chunk in _chunks(ids):
        q = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT * FROM buybox_overrides WHERE property_id IN ({q})", chunk):
            out[(int(r["property_id"]), r["reason"])] = r
    return out


def _keys(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for chunk in _chunks(ids):
        q = ",".join("?" for _ in chunk)
        out.update({int(r["id"]): r["property_key"] for r in rows(conn, f"SELECT id, property_key FROM properties "
                                                                         f"WHERE id IN ({q})", chunk)})
    return out


_UPSERT = ("INSERT INTO buybox_overrides(property_id, reason, decision, bucket, detail_at_decision, note, batch_id, "
           "decided_by, decided_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(property_id, reason) DO UPDATE SET "
           "decision=excluded.decision, bucket=excluded.bucket, detail_at_decision=excluded.detail_at_decision, "
           "note=excluded.note, batch_id=excluded.batch_id, decided_by=excluded.decided_by, "
           "decided_at=excluded.decided_at")
_LOG = ("INSERT INTO buybox_override_log(property_id, property_key, reason, decision, previous, bucket, note, "
        "batch_id, detail, actor, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)")


def set_decision(conn: sqlite3.Connection, property_ids: Iterable[int], reason: str, decision: str, actor: str, *,
                 note: str | None = None, bucket: str | None = None, details: Mapping[int, str] | None = None,
                 batch_id: str | None = None) -> dict:
    """Save one decision for many properties in one transaction, logging each change.
    Returns {batch_id, changed, unchanged}. A property that already has this decision is left as it is."""
    if decision == "rules":
        raise ValueError("Use clear() to follow the rules again.")
    check_decision(decision, reason)
    ids = sorted({int(i) for i in property_ids})
    batch = batch_id or new_batch_id()
    who = (actor or "unknown").strip()[:320] or "unknown"
    note = (note or "").strip() or None
    now = now_iso()
    current, keys = _current(conn, ids), _keys(conn, ids)
    changed = unchanged = 0
    with conn:
        for pid in ids:
            if pid not in keys:
                continue
            prev = current.get((pid, reason))
            if prev and prev["decision"] == decision and (note is None or prev.get("note") == note):
                unchanged += 1
                continue
            detail = (details or {}).get(pid)
            conn.execute(_UPSERT, (pid, reason, decision, bucket, detail, note, batch, who, now))
            conn.execute(_LOG, (pid, keys[pid], reason, decision, prev["decision"] if prev else None, bucket, note,
                                batch, detail, who, now))
            changed += 1
    return {"batch_id": batch, "changed": changed, "unchanged": unchanged}


def clear(conn: sqlite3.Connection, property_ids: Iterable[int], actor: str, *, reasons: Iterable[str] | None = None,
          whole_from_bucket: str | None = None, whole_decision: str | None = None, batch_id: str | None = None,
          note: str | None = None) -> dict:
    """Follow the rules again: delete decisions, logging each one as 'cleared'.
    reasons: only these reasons (None = every decision of these properties). whole_from_bucket: also the
    whole-property decisions made from that group. whole_decision: only whole-property rows with that decision.
    Returns {batch_id, changed, unchanged}."""
    ids = sorted({int(i) for i in property_ids})
    wanted = None if reasons is None else set(reasons)
    batch = batch_id or new_batch_id()
    who = (actor or "unknown").strip()[:320] or "unknown"
    now = now_iso()
    current, keys = _current(conn, ids), _keys(conn, ids)
    changed = 0
    touched: set[int] = set()
    with conn:
        for (pid, reason), row in sorted(current.items()):
            hit = wanted is None or reason in wanted
            if reason == WHOLE and whole_from_bucket is not None and row.get("bucket") == whole_from_bucket:
                hit = True
            if reason == WHOLE and whole_decision is not None:
                hit = hit and row["decision"] == whole_decision
            if not hit:
                continue
            conn.execute("DELETE FROM buybox_overrides WHERE property_id = ? AND reason = ?", (pid, reason))
            conn.execute(_LOG, (pid, keys.get(pid), reason, "cleared", row["decision"], row.get("bucket"), note, batch,
                                row.get("detail_at_decision"), who, now))
            changed += 1
            touched.add(pid)
    return {"batch_id": batch, "changed": changed, "unchanged": len(ids) - len(touched)}


def undo_batch(conn: sqlite3.Connection, batch_id: str, actor: str, market_id: int | None = None) -> dict:
    """Put back what every decision of one batch replaced. The undo is a batch of its own (so it can be undone
    too). Returns {batch_id, undone_batch, changed, property_ids}. KeyError when the batch isn't found."""
    found = rows(conn, "SELECT l.*, p.market_id FROM buybox_override_log l JOIN properties p ON p.id = l.property_id "
                       "WHERE l.batch_id = ? ORDER BY l.id", (str(batch_id),))
    if not found or (market_id is not None and any(int(r["market_id"]) != int(market_id) for r in found)):
        raise KeyError("That change wasn't found, so there is nothing to undo.")
    first: dict[tuple[int, str], dict] = {}
    for r in found:
        first.setdefault((int(r["property_id"]), r["reason"]), r)
    ids = sorted({pid for pid, _ in first})
    batch = new_batch_id()
    who = (actor or "unknown").strip()[:320] or "unknown"
    now = now_iso()
    current = _current(conn, ids)
    changed = 0
    with conn:
        for (pid, reason), entry in sorted(first.items()):
            prev = entry["previous"]
            now_row = current.get((pid, reason))
            now_decision = now_row["decision"] if now_row else None
            if prev == now_decision:
                continue
            if prev is None:
                conn.execute("DELETE FROM buybox_overrides WHERE property_id = ? AND reason = ?", (pid, reason))
                logged = "cleared"
            else:
                # Who made the decision being put back, and when: its own log row.
                origin = one(conn, "SELECT * FROM buybox_override_log WHERE property_id = ? AND reason = ? "
                                   "AND decision = ? AND id < ? ORDER BY id DESC LIMIT 1",
                             (pid, reason, prev, entry["id"])) or {}
                conn.execute(_UPSERT, (pid, reason, prev, origin.get("bucket"), origin.get("detail"),
                                       origin.get("note"), origin.get("batch_id") or batch,
                                       origin.get("actor") or who, origin.get("created_at") or now))
                logged = prev
            conn.execute(_LOG, (pid, entry.get("property_key"), reason, logged, now_decision, entry.get("bucket"),
                                f"Undo of {batch_id}", batch, None, who, now))
            changed += 1
    return {"batch_id": batch, "undone_batch": str(batch_id), "changed": changed, "property_ids": ids}


# --- what the rules said when deciding ----------------------------------------------------------

def rule_details(conn: sqlite3.Connection, market: Mapping[str, Any], property_ids: Iterable[int],
                 settings: Settings | None = None) -> dict[int, dict[str, str]]:
    """{property id: {reason: detail}}: what the rules say now (stored with a decision as detail_at_decision)."""
    from .rank import load_scoring_rows  # lazy: rank imports this module
    s = settings or get_settings()
    ac = s.asset_class(market["asset_class"])
    return {int(p["id"]): dict(evaluate(p, e, ac, market, s).get("exclusion_details") or {})
            for p, e in load_scoring_rows(conn, [int(market["id"])], property_ids)}


def _detail_text(details: Mapping[str, str], reason: str) -> str | None:
    if reason != WHOLE:
        return details.get(reason)
    parts = [f"{EXCLUSION_LABELS.get(k, k)}: {v}" for k, v in details.items() if k != MANUAL_REASON]
    return "; ".join(parts) or "Fits the buy box by the rules"


# --- one property, one group ---------------------------------------------------------------------

Rescore = Callable[[], Any] | None


def _rescore(conn: sqlite3.Connection, slug: str, rescore: Rescore) -> None:
    if rescore is not None:
        rescore()
        return
    from .rank import compute_scores  # lazy: rank imports this module
    compute_scores(conn, slug)


def status_of(conn: sqlite3.Connection, property_ids: Iterable[int]) -> dict:
    """{now_in, still_out: [{reason, label, count}], out} for these properties, from the saved scores."""
    ids = sorted({int(i) for i in property_ids})
    now_in, out = 0, 0
    why: Counter[str] = Counter()
    for chunk in _chunks(ids):
        q = ",".join("?" for _ in chunk)
        for r in rows(conn, f"SELECT in_buybox, exclusion_reasons FROM scores WHERE property_id IN ({q})", chunk):
            if r["in_buybox"]:
                now_in += 1
            else:
                out += 1
                why.update(loads(r["exclusion_reasons"], []) or [])
    still = [{"reason": k, "label": EXCLUSION_LABELS.get(k, k), "count": n}
             for k, n in sorted(why.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"now_in": now_in, "out": out, "still_out": still}


def _still_out_text(still: list[dict]) -> str:
    return ", ".join(f"{x['label'].lower()} {x['count']:,}" for x in still)


def group_property_ids(conn: sqlite3.Connection, market_id: int, bucket: str) -> list[int]:
    """The properties in one set-aside group right now (the same condition as the Review list)."""
    params = {"market_id": int(market_id), **B.member_params(bucket)}
    return [int(r[0]) for r in conn.execute(
        f"SELECT p.id FROM properties p LEFT JOIN scores s ON s.property_id = p.id "
        f"WHERE p.market_id = :market_id AND {B.MEMBER_SQL} ORDER BY p.id", params).fetchall()]


def decide_group(conn: sqlite3.Connection, market_slug: str, bucket: str, decision: str, actor: str, *,
                 note: str | None = None, rescore: Rescore = None, settings: Settings | None = None) -> dict:
    """The Why? panel's Decide button for one group. The members are looked up now, on the server.
    - add_only: the group's rule no longer counts for its members (some may stay out for other reasons);
    - add_all:  every member is added to the buy box by hand, whatever the rules say;
    - keep:     the rule stands; the members are marked as looked at;
    - clear:    the decisions made for this rule (and from this group) are removed: the rules decide again.
    For the 'Set aside by hand' group, add_only and clear both take the hand set-aside off.
    Then the market is re-scored. Returns {batch_id, changed, total, now_in, still_out, message, bucket}."""
    s = settings or get_settings()
    reason, _ = B.parse_key(bucket)
    if decision not in GROUP_DECISIONS:
        raise ValueError(f"'{decision}' isn't a choice here. Use one of: {', '.join(GROUP_DECISIONS)}.")
    market = market_by_slug(conn, market_slug)
    ac = s.asset_class(market["asset_class"])
    spec = B.group_spec(market, ac, s)
    sample = None
    if reason == "property_type":
        sample = (one(conn, "SELECT p.property_type FROM properties p JOIN scores s ON s.property_id = p.id "
                            "WHERE p.market_id = ? AND EXISTS (SELECT 1 FROM json_each(s.aside_buckets) b "
                            "WHERE b.value = ?) LIMIT 1", (market["id"], bucket)) or {}).get("property_type")
    group = B.label_for(bucket, spec, sample)
    rlabel = EXCLUSION_LABELS.get(reason, reason)
    ids = group_property_ids(conn, int(market["id"]), bucket)
    if not ids:
        return {"batch_id": None, "changed": 0, "total": 0, "now_in": 0, "still_out": [], "bucket": bucket,
                "message": f"No properties are in '{group}' any more, so nothing changed."}
    manual = reason == MANUAL_REASON
    if decision == "keep" and manual:
        raise ValueError("These are already set aside by hand.")
    clearing = decision == "clear" or (manual and decision == "add_only")
    if clearing:
        res = clear(conn, ids, actor, reasons=[WHOLE] if manual else [reason], note=note,
                    whole_from_bucket=None if manual else bucket, whole_decision="exclude" if manual else None)
    else:
        target = WHOLE if decision == "add_all" else reason
        details = rule_details(conn, market, ids, s)
        res = set_decision(conn, ids, target, "keep" if decision == "keep" else "include", actor, note=note,
                           bucket=bucket, details={pid: _detail_text(d, target) or "" for pid, d in details.items()})
    _rescore(conn, market_slug, rescore)
    st = status_of(conn, ids)
    total = len(ids)
    if decision == "add_all":
        msg = f"Added all {total:,} in '{group}' to the buy box by hand, whatever the rules say."
    elif decision == "keep":
        msg = f"Kept {total:,} in '{group}' set aside. They're marked as looked at."
    elif decision == "clear" or manual:
        msg = (f"The rules decide again for the {total:,} in '{group}': {st['now_in']:,} in the buy box, "
               f"{st['out']:,} set aside.")
    else:
        msg = (f"{st['now_in']:,} of {total:,} in '{group}' are now in the buy box ("
               f"{rlabel.lower()} no longer counts for them).")
        if st["out"]:
            msg += f" {st['out']:,} stay set aside for other reasons: {_still_out_text(st['still_out'])}."
    if decision in ("add_only", "add_all") and not manual:
        msg += " Properties you import later are still checked by the rules."
    if not res["changed"]:
        msg = (f"Nothing changed: the {total:,} in '{group}' had no decisions to clear." if clearing else
               f"Nothing changed: the {total:,} in '{group}' already had that decision.")
    return {"batch_id": res["batch_id"] if res["changed"] else None, "changed": res["changed"], "total": total,
            "now_in": st["now_in"], "still_out": st["still_out"], "bucket": bucket, "message": msg}


def decide_property(conn: sqlite3.Connection, market_slug: str, property_id: int, decision: str, actor: str, *,
                    reason: str = WHOLE, note: str | None = None, rescore: Rescore = None,
                    settings: Settings | None = None) -> dict:
    """The drawer's buttons for one property: include (Add to buy box, or ignore one rule), exclude (Set aside)
    or rules (follow the rules again: every decision, or just the one for `reason`). Re-scores the market.
    Returns {batch_id, changed, in_buybox, still_out, message}."""
    s = settings or get_settings()
    reason = reason or WHOLE
    check_decision(decision, reason)                                           # ValueError -> 400
    body = DecisionIn(decision=decision, reason=reason, note=note)
    market = market_by_slug(conn, market_slug)
    if not one(conn, "SELECT id FROM properties WHERE id = ? AND market_id = ?", (property_id, market["id"])):
        raise KeyError(f"Property {property_id} is not in this market.")
    if body.decision == "rules":
        res = clear(conn, [property_id], actor, reasons=None if body.reason == WHOLE else [body.reason],
                    note=body.note)
    else:
        details = rule_details(conn, market, [property_id], s).get(int(property_id), {})
        res = set_decision(conn, [property_id], body.reason, body.decision, actor, note=body.note,
                           details={int(property_id): _detail_text(details, body.reason) or ""})
    _rescore(conn, market_slug, rescore)
    st = status_of(conn, [property_id])
    inside = bool(st["now_in"])
    why = _still_out_text(st["still_out"])
    if body.decision == "exclude":
        msg = "Set aside by hand. It stays out until you clear the decision."
    elif body.decision == "keep":
        msg = f"Kept set aside for '{reason_label(body.reason).lower()}'."
    elif body.decision == "include" and body.reason == WHOLE:
        msg = "Added to the buy box by hand."
    elif body.decision == "include":
        msg = (f"'{reason_label(body.reason)}' no longer counts for this property. "
               + ("It's in the buy box now." if inside else f"It's still set aside: {why}."))
    else:
        msg = "Back to the rules: " + ("it's in the buy box." if inside else f"set aside ({why}).")
    if not res["changed"]:
        msg = "Nothing changed: " + ("there was no decision to clear." if body.decision == "rules"
                                     else "it already had that decision.")
    return {"batch_id": res["batch_id"] if res["changed"] else None, "changed": res["changed"],
            "in_buybox": inside, "still_out": st["still_out"], "message": msg}


def undo(conn: sqlite3.Connection, market_slug: str, batch_id: str, actor: str, *, rescore: Rescore = None) -> dict:
    """Undo one click (a batch) and re-score. Returns {batch_id, changed, total, now_in, still_out, message}."""
    market = market_by_slug(conn, market_slug)
    res = undo_batch(conn, batch_id, actor, int(market["id"]))
    _rescore(conn, market_slug, rescore)
    st = status_of(conn, res["property_ids"])
    total = len(res["property_ids"])
    msg = (f"Undone. {total:,} {'property is' if total == 1 else 'properties are'} back the way they were: "
           f"{st['now_in']:,} in the buy box, {st['out']:,} set aside." if res["changed"] else
           "Nothing to undo: those decisions had already changed.")
    return {"batch_id": res["batch_id"] if res["changed"] else None, "undone_batch": res["undone_batch"],
            "changed": res["changed"], "total": total, "now_in": st["now_in"], "still_out": st["still_out"],
            "message": msg}


__all__ = [
    "CLI_ACTOR", "DecisionIn", "GROUP_DECISIONS", "GroupDecisionIn", "REASONS", "RULE_KEYS", "check_decision", "clear",
    "decide_group", "decide_property", "decisions_list", "for_property", "group_property_ids", "history", "load",
    "new_batch_id", "reason_label", "rule_details", "set_decision", "status_of", "undo", "undo_batch",
]
