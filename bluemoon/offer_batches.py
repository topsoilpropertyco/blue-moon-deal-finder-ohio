"""Prepare offers for several properties at once ("Prepare offers for selected" on Deals).

- preview: one line per property with its rent basis, 4x price, recipient and anything missing, so each row can
  be checked, given a typed price or left out before anything is saved.
- create: saves the batch and its rows in one write (BEGIN IMMEDIATE), then prepares each row on its own. One row
  failing never stops the others; the report says what happened to every row.
- Retry: only failed rows are tried again. A row that was created is never made twice, and a property that already
  has an unsent draft gets no second one (the row is "existing"), so a retry or a second click cannot duplicate an
  offer. The database also refuses a second version with the same number.
- Every offer is the same internal draft a single "Prepare offer" makes (offers.prepare): 4x the yearly rent unless
  a price was typed. Without an active LOI template the rows are internal records, not letters.
"""
from __future__ import annotations

import csv
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import napkin, offers
from .crexi.batches import safe_cell
from .db import now_iso, one, rows
from .pricing import PricingConfig

# property id -> (the property row, the rent snapshot an offer made now would be based on); KeyError when the
# property is not in the market
Basis = Callable[[int], tuple[Mapping[str, Any], Mapping[str, Any]]]
ROW_STATUS_LABELS = {"pending": "Waiting", "created": "Draft prepared", "existing": "Already had a draft",
                     "failed": "Failed (can retry)", "excluded": "Left out"}


def _flag(key: str, text: str, blocks: bool = False) -> dict:
    return {"key": key, "text": text, "blocks": blocks}


def assess(conn: sqlite3.Connection, pid: int, basis: Basis) -> dict:
    """One preview row: what an offer for this property would start from and what is missing."""
    try:
        prop, snap = basis(pid)
    except KeyError:
        return {"property_id": pid, "address": None, "city": None, "can_prepare": False,
                "flags": [_flag("not_found", "This property is not in this market.", True)]}
    latest = offers.open_draft(conn, pid) or one(conn, "SELECT version, status FROM offers WHERE property_id = ? "
                                                       "AND intent = 'loi' ORDER BY version DESC LIMIT 1", (pid,))
    default = offers.default_cents(snap)
    flags = []
    if default is None:
        flags.append(_flag("no_rent", "No yearly rent yet (unknown or $0), so there is no 4x price. Enter a rent "
                                      "figure first or type a price.", True))
    if not offers.current_recipient(conn, pid):
        flags.append(_flag("no_recipient", "No recipient chosen yet (choose one in the property's people block)."))
    if not (prop.get("owner_name") or "").strip():
        flags.append(_flag("no_owner", "No owner on record."))
    if latest and latest["status"] != "sent":
        flags.append(_flag("draft_exists", f"Version {latest['version']} is already a draft, so no new offer is added."))
    elif latest:
        flags.append(_flag("revision", f"Version {latest['version']} was already sent, so this prepares version "
                                       f"{latest['version'] + 1}, a revised offer."))
    return {"property_id": pid, "address": prop.get("address"), "city": prop.get("city"),
            "owner": prop.get("owner_name"), "recipient": offers.current_recipient(conn, pid).get("recipient_name"),
            "rent_basis": napkin.BASIS_LABELS.get(snap.get("basis") or "") or "Estimated",
            "annual_cents": snap.get("annual_cents"), "default_cents": default, "flags": flags,
            "can_prepare": not any(f["key"] in ("not_found", "draft_exists") for f in flags)}


def preview(conn: sqlite3.Connection, ids: Iterable[int], basis: Basis, cfg: PricingConfig,
            letters_ready: bool) -> dict:
    ids = list(dict.fromkeys(int(i) for i in ids))
    _check_size(ids, cfg)
    return {"rows": [assess(conn, pid, basis) for pid in ids], "letters_ready": letters_ready,
            "note": None if letters_ready else
            "No letter template is active yet, so these will be internal records, not LOIs."}


def _check_size(items: list, cfg: PricingConfig) -> None:
    if not items:
        raise ValueError("Pick at least one property.")
    if len(items) > cfg.batch_max_properties:
        raise ValueError(f"Prepare at most {cfg.batch_max_properties} offers at a time (batch_max_properties in "
                         "config/pricing.yaml). Pick fewer properties.")


def _set(conn: sqlite3.Connection, row_id: int, status: str, offer_id: int | None, message: str) -> None:
    conn.execute("UPDATE offer_batch_rows SET status = ?, offer_id = ?, message = ? WHERE id = ?",
                 (status, offer_id, message, row_id))
    conn.commit()


def _prepare_row(conn: sqlite3.Connection, row: Mapping[str, Any], basis: Basis, actor: str, internal: str) -> None:
    pid = row["property_id"]
    try:
        prop, snap = basis(pid)
        latest = offers.open_draft(conn, pid)
        if latest and latest["status"] != "sent":
            _set(conn, row["id"], "existing", latest["id"],
                 f"Version {latest['version']} was already a draft, so no second offer was made.")
            return
        price = None if row["price_cents"] is None else str(Decimal(row["price_cents"]) / 100)
        o = offers.prepare(conn, prop, snap, actor, price=price)
    except (KeyError, ValueError) as exc:
        conn.rollback()
        _set(conn, row["id"], "failed", None, str(exc.args[0] if isinstance(exc, KeyError) else exc))
        return
    _set(conn, row["id"], "created", o["id"], f"Version {o['version']} prepared{internal}.")


def _run(conn: sqlite3.Connection, batch_id: int, basis: Basis, actor: str) -> None:
    batch = one(conn, "SELECT letters_ready FROM offer_batches WHERE id = ?", (batch_id,))
    internal = "" if batch["letters_ready"] else " as an internal record (no letter template yet)"
    for row in rows(conn, "SELECT * FROM offer_batch_rows WHERE batch_id = ? AND status IN ('pending', 'failed') "
                          "ORDER BY position", (batch_id,)):
        _prepare_row(conn, row, basis, actor, internal)
    failed = one(conn, "SELECT COUNT(*) AS n FROM offer_batch_rows WHERE batch_id = ? AND status = 'failed'",
                 (batch_id,))["n"]
    conn.execute("UPDATE offer_batches SET status = ?, finished_at = ? WHERE id = ?",
                 ("partial" if failed else "done", now_iso(), batch_id))
    conn.commit()


def create(conn: sqlite3.Connection, market_id: int, items: list[Mapping[str, Any]], basis: Basis, cfg: PricingConfig,
           letters_ready: bool, actor: str) -> dict:
    """Save and run a batch. items: [{property_id, price (dollars, optional), exclude}]."""
    _check_size(items, cfg)
    ids = [int(i["property_id"]) for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError("A property is in the list twice.")
    known = {r["id"] for r in rows(conn, f"SELECT id FROM properties WHERE market_id = ? AND id IN "
                                         f"({','.join('?' * len(ids))})", (market_id, *ids))}
    if known != set(ids):
        raise ValueError("Some of these properties are not in this market.")
    prices = [None if i.get("exclude") or i.get("price") in (None, "") else offers.parse_price(i["price"])
              for i in items]
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_id = int(conn.execute("INSERT INTO offer_batches(market_id, letters_ready, created_by, created_at) "
                                    "VALUES(?,?,?,?)", (market_id, int(letters_ready), actor, now_iso())).lastrowid)
        conn.executemany(
            "INSERT INTO offer_batch_rows(batch_id, property_id, position, price_cents, status) VALUES(?,?,?,?,?)",
            [(batch_id, pid, pos, cents, "excluded" if item.get("exclude") else "pending")
             for pos, (pid, item, cents) in enumerate(zip(ids, items, prices), start=1)])
        conn.execute("UPDATE offer_batch_rows SET message = 'Left out by you.' WHERE batch_id = ? AND "
                     "status = 'excluded'", (batch_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _run(conn, batch_id, basis, actor)
    return view(conn, batch_id)


def retry(conn: sqlite3.Connection, batch_id: int, basis: Basis, actor: str) -> dict:
    """Try the failed rows again. Rows that were created, found existing or left out are not touched."""
    get(conn, batch_id)
    _run(conn, batch_id, basis, actor)
    return view(conn, batch_id)


def get(conn: sqlite3.Connection, batch_id: int) -> dict:
    b = one(conn, "SELECT * FROM offer_batches WHERE id = ?", (batch_id,))
    if not b:
        raise KeyError(f"Batch {batch_id} was not found.")
    return b


def _summary(counts: Mapping[str, int]) -> str:
    parts = [f"{counts['created']} offer{'s' if counts['created'] != 1 else ''} prepared"]
    for key, text in (("existing", "already had a draft"), ("failed", "failed"), ("excluded", "left out")):
        if counts[key]:
            parts.append(f"{counts[key]} {text}")
    return ", ".join(parts) + "."


def view(conn: sqlite3.Connection, batch_id: int) -> dict:
    """The batch report: what happened to every row, and counts."""
    b = get(conn, batch_id)
    found = rows(conn, "SELECT r.*, p.address, p.city, o.version FROM offer_batch_rows r JOIN properties p ON "
                       "p.id = r.property_id LEFT JOIN offers o ON o.id = r.offer_id WHERE r.batch_id = ? "
                       "ORDER BY r.position", (batch_id,))
    counts = {k: sum(1 for r in found if r["status"] == k) for k in ROW_STATUS_LABELS}
    return {**b, "letters_ready": bool(b["letters_ready"]), "counts": counts, "text": _summary(counts),
            "rows": [{"property_id": r["property_id"], "address": r["address"], "city": r["city"],
                      "status": r["status"], "status_label": ROW_STATUS_LABELS[r["status"]],
                      "offer_id": r["offer_id"], "version": r["version"], "message": r["message"],
                      "price_cents": r["price_cents"]} for r in found]}


def write_report(conn: sqlite3.Connection, batch_id: int, out_dir: Path) -> Path:
    """The batch report as a CSV (formula-safe cells) under output/offers/."""
    v = view(conn, batch_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / "offers" / f"batch-{batch_id}-{stamp}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Property ID", "Address", "City", "Result", "Offer version", "Typed price (cents)", "Note"])
        for r in v["rows"]:
            w.writerow([safe_cell(x) for x in (r["property_id"], r["address"], r["city"], r["status_label"],
                                               r["version"], r["price_cents"], r["message"])])
    return path
