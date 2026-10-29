"""`uv run run.py crexi ...`: the same batch workflow as the dashboard's Credits screen.

    uv run run.py crexi status
    uv run run.py crexi export --count 50 --dry-run     who would be in it (nothing is written)
    uv run run.py crexi export --count 50               write output/crexi/<period>/batch-<id>-<N>.csv
    uv run run.py crexi import ~/Downloads/reply.xlsx   preview; add --yes to apply
    uv run run.py crexi close 3 --yes                   give back the credits of rows Crexi never returned
    uv run run.py crexi cancel 3 --yes                  a batch you never uploaded
    uv run run.py crexi history

The app never talks to Crexi: you upload the exported file there and bring Crexi's file back.
"""
from __future__ import annotations

import argparse
import sqlite3
import textwrap
from pathlib import Path

from ..config import get_settings
from . import batches as B
from . import reply as R

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_WAITING = 0, 1, 2, 3
ACTIONS = ("status", "export", "import", "close", "cancel", "history")


def say(text: str = "", indent: str = "") -> None:
    if not text:
        print()
        return
    print(textwrap.fill(text, 100, initial_indent=indent, subsequent_indent=indent + "  ",
                        break_long_words=False, break_on_hyphens=False))


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=ACTIONS, help="what to do")
    p.add_argument("target", nargs="?", help="import: Crexi's file; close and cancel: the batch number")
    p.add_argument("--count", "-n", type=int, help="export: how many (default: every free credit)")
    p.add_argument("--retry", action="store_true",
                   help="export: put the properties Crexi did not return before back in (a second pass)")
    p.add_argument("--hand", action="store_true",
                   help="import: record rows for properties that were in no batch as a hand batch")
    p.add_argument("--phone-columns", help="import: the columns that hold phones, if I guess wrong (a, b)")
    p.add_argument("--email-columns", help="import: the columns that hold emails, if I guess wrong (a, b)")
    p.add_argument("--contact-column", help="import: the column that holds the contact's name")


def _columns(args: argparse.Namespace) -> dict | None:
    out: dict = {}
    if args.phone_columns:
        out["phone"] = [x.strip() for x in args.phone_columns.split(",") if x.strip()]
    if args.email_columns:
        out["email"] = [x.strip() for x in args.email_columns.split(",") if x.strip()]
    if args.contact_column:
        out["name"] = args.contact_column
    return out or None


def _status(conn: sqlite3.Connection) -> int:
    info = B.status(conn)
    st = info["status"]
    say(f"Crexi credit period {st['period_label']} ({st['days_left']} days left; the next period starts "
        f"{st['next_reset']}).")
    say(f"{st['per_period']:,} per period: {st['returned']:,} returned, {st['in_flight']:,} waiting at Crexi, "
        f"{st['left']:,} free.")
    for b in info["open_batches"]:
        say(f"Batch #{b['id']} ({b['created_at'][:10]}): {b['sent']:,} sent, {b['returned']:,} returned, "
            f"{b['waiting']:,} waiting.", "  ")
    say(info["topup"]["message"])
    return EXIT_OK


def _export(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    if args.dry_run:
        pv = B.preview_export(conn, args.count, retry=args.retry)
        say(pv["message"])
        for r in pv["reasons"]:
            say(f"{r['count']:,} {r['label']}", "  ")
        if pv["left_out_text"]:
            say("Left out: " + pv["left_out_text"] + ".")
        for s in pv["sample"][:10]:
            say(f"{s['address']}, {s['city']}", "  ")
        say("Nothing was written. Run again without --dry-run to make the file.")
        return EXIT_OK
    res = B.export_batch(conn, args.count, created_by="cli", retry=args.retry)
    say(res["message"])
    say(f"File: {B.output_dir() / res['file']}")
    return EXIT_OK


def _import(args: argparse.Namespace, conn: sqlite3.Connection, interactive, ask_ok) -> int:
    if not args.target:
        say("Give the path of the file Crexi sent back, for example: uv run run.py crexi import ~/Downloads/reply.xlsx")
        return EXIT_USAGE
    path = Path(args.target).expanduser()
    columns = _columns(args)
    pv = R.preview(conn, path, path.name, columns=columns, adopt_hand=args.hand)
    say(pv["message"])
    c = pv["columns"]
    say("Columns I read: phones " + (", ".join(c["phone"]) or "none") + "; emails " + (", ".join(c["email"]) or "none")
        + "; contact name " + (c["name"] or "none") + ".")
    say(pv["enrichment_text"])
    for w in pv["warnings"]:
        say(f"Note: {w}")
    if args.dry_run:
        say("Preview only. Nothing was changed.")
        return EXIT_OK
    if not pv["can_apply"]:
        say("Nothing to record from this file.")
        return EXIT_OK
    if not args.yes and not (interactive() and ask_ok()):
        say("Nothing was changed. To go ahead, add --yes to the same command.")
        return EXIT_WAITING
    res = R.apply_reply(conn, path, path.name, columns=columns, adopt_hand=args.hand, actor="cli")
    say(res["message"])
    if res.get("changed"):
        say(res["enrichment_text"])
        for w in res["warnings"]:
            say(f"Note: {w}")
        if res.get("unmatched_file"):
            say(f"Rows that could not be matched: {B.output_dir() / res['unmatched_file']}")
    return EXIT_OK


def _batch_number(args: argparse.Namespace) -> int | None:
    try:
        return int(str(args.target).lstrip("#"))
    except (TypeError, ValueError):
        say("Give the batch number, for example: uv run run.py crexi close 3")
        return None


def _close(args: argparse.Namespace, conn: sqlite3.Connection, cancel: bool) -> int:
    n = _batch_number(args)
    if n is None:
        return EXIT_USAGE
    fn = B.withdraw_batch if cancel else B.close_batch
    res = fn(conn, n, confirm=bool(args.yes), actor="cli")
    say(res["message"])
    if res.get("needs_confirm"):
        say("Nothing was changed. To go ahead, add --yes to the same command.")
        return EXIT_WAITING
    return EXIT_OK


def _history(conn: sqlite3.Connection) -> int:
    for p in B.history(conn)["periods"]:
        pct = f"{p['matched_pct']:.0%}" if p["matched_pct"] is not None else "-"
        say(f"{p['label']}: {p['sent']:,} sent, {p['returned']:,} returned ({pct}), {p['not_returned']:,} not returned, "
            f"{p['waiting']:,} waiting, {p['by_hand']:,} used by hand.")
        for b in p["batches"]:
            say(f"#{b['id']} {b['created_day']} {b['kind_label']} by {b['created_by']}: {b['sent']:,} sent, "
                f"{b['returned']:,} returned, {b['not_returned']:,} not returned, {b['waiting']:,} waiting "
                f"({b['status']})", "  ")
    return EXIT_OK


def run(args: argparse.Namespace, conn: sqlite3.Connection, interactive=lambda: False, ask_ok=lambda: False) -> int:
    get_settings()
    try:
        if args.action == "status":
            return _status(conn)
        if args.action == "export":
            return _export(args, conn)
        if args.action == "import":
            return _import(args, conn, interactive, ask_ok)
        if args.action == "close":
            return _close(args, conn, cancel=False)
        if args.action == "cancel":
            return _close(args, conn, cancel=True)
        return _history(conn)
    except (B.CrexiError, R.ReplyError, KeyError) as exc:
        say(str(exc.args[0] if isinstance(exc, KeyError) and exc.args else exc))
        return EXIT_ERROR
