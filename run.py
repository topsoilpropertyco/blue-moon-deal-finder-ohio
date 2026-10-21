#!/usr/bin/env python3
"""Blue Moon Deal Finder from the command line.

    uv run run.py status
    uv run run.py import data/crexi/export.xlsx --limit 5
    uv run run.py all --limit 5
    uv run run.py estimate
    uv run run.py photos --limit 5 --dry-run
    uv run run.py export
    uv run run.py serve

Every command takes --market SLUG (default: the first market in config.yaml). The pipeline commands
take --limit N (test with 5 first), --dry-run (count and price only, nothing is called) and --yes (OK to
spend on runs bigger than safety.confirm_over_properties).

Exit codes: 0 done, 1 something went wrong, 2 the command was typed wrong, 3 stopped before spending
money and waiting for your OK.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import socket
import sqlite3
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path

from bluemoon import pipeline
from bluemoon.config import Settings, get_settings, has_secret, secret
from bluemoon.db import connect, scalar
from bluemoon.drill.credits import credit_status

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_WAITING = 0, 1, 2, 3
IMPORT_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm")
MIN_PASSWORD_LENGTH = 12   # shorter dashboard passwords get a warning when the server starts
WIDTH = 100
STEP_HELP = {
    "owners": "Owner type, portfolio, years owned, debt, absentee (free).",
    "drive": "Drive times from the market's origin (Google with a key, else a free estimate).",
    "rent": "HUD rents compared with the rent scale (free).",
    "photos": "Street View + satellite photos for buy-box properties (Google, paid).",
    "classify": "Claude looks at the photos (your Claude plan).",
    "score": "Buy box, colors, Blue Moon score and rank (free).",
    "drill": "DRILL automatic stages and contact levels (free).",
    "all": "Every step in order: owners, drive, rent, score, photos, classify, score, drill.",
    "calibrate": "Re-check the photos you reviewed and report the photo check's accuracy.",
}

log = logging.getLogger("bluemoon.cli")


# --- the parser --------------------------------------------------------------------------------

def positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a whole number") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _add_common(p: argparse.ArgumentParser, *, suppress: bool) -> None:
    """The shared options. Sub-commands suppress the defaults, so the options work before or after the command."""
    def default(value: object) -> object:
        return argparse.SUPPRESS if suppress else value

    p.add_argument("--market", default=default(None),
                   help="market slug from config.yaml (default: the first one); a unique start like 'apartments' works")
    p.add_argument("--limit", type=positive_int, default=default(None), help="at most N properties (test with 5 first)")
    p.add_argument("--dry-run", action="store_true", default=default(False),
                   help="count and price only; nothing is called or saved")
    p.add_argument("--yes", "-y", action="store_true", default=default(False),
                   help="OK to spend on big paid runs without asking")
    p.add_argument("--debug", action="store_true", default=default(False), help="show full error details")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py", description="Blue Moon Deal Finder: rank properties, run the DRILL, reach every owner.",
        epilog="Example: uv run run.py all --limit 5")
    _add_common(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", metavar="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        _add_common(p, suppress=True)
        return p

    imp = add("import", "Import Crexi exports (CSV or XLSX), then rank them right away (free).")
    imp.add_argument("files", nargs="*", help="files or folders (default: data/crexi/)")
    imp.add_argument("--force", action="store_true",
                     help="re-import files in the folder that were imported before")
    for name, help_text in STEP_HELP.items():
        add(name, help_text)
    add("estimate", "What every step would do and cost, without calling anything.")
    add("export", "Write ranked_list.xlsx/.csv, excluded_*.csv, crm_handoff.csv, mailing_list.csv, "
                  "drill_worklist.csv and run_summary.md to output/<market>/.")
    serve = add("serve", "Start the dashboard.")
    serve.add_argument("--host", help="default: app.host in config.yaml (0.0.0.0 to reach it from your phone)")
    serve.add_argument("--port", type=positive_int, help="default: app.port in config.yaml")
    add("status", "Where every market stands (keys are shown as yes/no, never their values).")
    return parser


# --- small output helpers ----------------------------------------------------------------------

def say(text: str = "", indent: str = "") -> None:
    if not text:
        print()
        return
    print(textwrap.fill(text, WIDTH, initial_indent=indent, subsequent_indent=indent + "  ",
                        break_long_words=False, break_on_hyphens=False))


def show_result(result: dict, indent: str = "") -> None:
    say(result["message"], indent)
    for w in result.get("warnings") or []:
        say(f"Note: {w}", indent)


def show_steps(steps: list[dict]) -> None:
    for r in steps:
        mark = "?" if r["needs_confirm"] else ("-" if r["ok"] else "!")
        say(f"{mark} {r['label']}: {r['message']}", "  ")


def show_estimate(est: dict) -> None:
    for line in est["steps"]:
        say(f"- {line['label']}: {line['message']}", "  ")
    say(est["message"])


def elapsed(started: float) -> str:
    seconds = time.monotonic() - started
    return f"{seconds:.1f} s" if seconds < 90 else f"{seconds / 60:.1f} min"


def interactive() -> bool:
    """Can we ask a question? (Tests replace this.)"""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def ask_ok() -> bool:
    try:
        return input("Go ahead? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


class ConsoleProgress:
    """A one-line progress display on a terminal; silent otherwise."""

    def __init__(self) -> None:
        self.on = sys.stderr.isatty()
        self.last = 0.0
        self.shown = False

    def __call__(self, fraction: float, message: str) -> None:
        now = time.monotonic()
        if not self.on or now - self.last < 0.25:
            return
        self.last = now
        line = f"{fraction:4.0%}  {message}"[: WIDTH - 1]
        sys.stderr.write("\r" + line.ljust(WIDTH - 1))
        sys.stderr.flush()
        self.shown = True

    def clear(self) -> None:
        if self.shown:
            sys.stderr.write("\r" + " " * (WIDTH - 1) + "\r")
            sys.stderr.flush()
            self.shown = False


# --- markets -----------------------------------------------------------------------------------

def resolve_market(wanted: str | None, settings: Settings) -> str:
    """The market slug: exact, or a unique start of one (e.g. 'apartments'). Default: the first market."""
    slugs = [m.slug for m in settings.markets]
    if not slugs:
        raise ValueError("config.yaml has no markets. Add one under 'markets:'.")
    if not wanted:
        return slugs[0]
    if wanted in slugs:
        return wanted
    matches = [s for s in slugs if s.startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    hint = "matches several" if matches else "is not a market"
    raise ValueError(f"'{wanted}' {hint}. Markets: {', '.join(matches or slugs)}.")


def rerun_command(args: argparse.Namespace, market: str) -> str:
    """The command to copy and paste to go ahead with a paid run."""
    parts = ["uv run run.py", args.command, "--market", market]
    if args.limit:
        parts += ["--limit", str(args.limit)]
    return " ".join(parts + ["--yes"])


# --- commands ----------------------------------------------------------------------------------

def run_with_ok(args: argparse.Namespace, market: str, run: Callable[[bool], dict]) -> tuple[dict | None, int]:
    """Run a step; when it stops to ask before spending, ask (on a terminal) or explain how to go ahead."""
    result = run(bool(args.yes))
    if not result["needs_confirm"]:
        return result, EXIT_OK
    if result["step"] == "all":
        if result.get("steps"):
            show_steps(result["steps"])
        say()
        say("Before spending money:")
        show_estimate(result["estimate"])
    else:
        say((result.get("estimate") or {}).get("message") or result["message"])
    if interactive() and ask_ok():
        return run(True), EXIT_OK
    say("Stopped before spending anything. To go ahead, run:")
    say(rerun_command(args, market), "  ")
    return None, EXIT_WAITING


def cmd_step(args: argparse.Namespace, conn: sqlite3.Connection, market: str) -> int:
    say(f"{pipeline.step_label(args.command)} · {market}" + (" (dry run)" if args.dry_run else ""))
    started = time.monotonic()
    progress = ConsoleProgress()

    def run(confirm: bool) -> dict:
        try:
            return pipeline.run_step(conn, market, args.command, args.limit, args.dry_run, confirm, progress)
        finally:
            progress.clear()

    result, code = run_with_ok(args, market, run)
    if result is None:
        return code
    if result.get("steps") and result["step"] == "all":
        show_steps(result["steps"])
        say()
    if result["dry_run"] and result.get("estimate", {}).get("steps"):
        show_estimate(result["estimate"])
    else:
        show_result(result)
    say(f"Done in {elapsed(started)}.")
    return EXIT_OK if result["ok"] else EXIT_ERROR


def import_files(names: list[str], settings: Settings) -> list[Path]:
    """Files named on the command line; folders give every CSV/XLSX inside. Default: data/crexi/."""
    return [p for p, _ in _import_targets(names, settings)]


def _import_targets(names: list[str], settings: Settings) -> list[tuple[Path, bool]]:
    """(file, came from a folder). Files named on the command line keep their order. A folder's files go
    oldest first (by when they were saved, then by name), so the newest export is merged last and its owner
    data wins: 'export (2).xlsx' and dashboard uploads named by time sort right."""
    targets = [Path(n) for n in names] or [settings.path("crexi_dir")]
    out: list[tuple[Path, bool]] = []
    for t in targets:
        if t.is_dir():
            found = [p for p in t.iterdir() if p.suffix.lower() in IMPORT_SUFFIXES and not p.name.startswith(".")]
            out += [(p, True) for p in sorted(found, key=lambda p: (p.stat().st_mtime, p.name))]
        else:
            out.append((t, False))
    return out


def _already_imported(conn: sqlite3.Connection, path: Path) -> bool:
    """Was this exact file imported before, into any market?"""
    from bluemoon.ingest.crexi import _file_hash
    try:
        digest = _file_hash(path)
    except OSError:
        return False
    return scalar(conn, "SELECT 1 FROM imports WHERE file_hash=? LIMIT 1", (digest,)) is not None


def cmd_import(args: argparse.Namespace, conn: sqlite3.Connection, market: str) -> int:
    targets = _import_targets(args.files, get_settings())
    skipped: list[Path] = []
    if not args.dry_run and not getattr(args, "force", False):
        # A folder holds every export ever dropped in, from every market: only bring in the new ones.
        skipped = [p for p, from_folder in targets if from_folder and _already_imported(conn, p)]
    files = [p for p, _ in targets if p not in skipped]
    if skipped:
        names = ", ".join(p.name for p in skipped[:5]) + (" ..." if len(skipped) > 5 else "")
        say(f"Skipped {len(skipped)} file(s) already imported: {names}. Use --force to import them again.")
    if not files and skipped:
        where = "the folder" if args.files else "data/crexi/"
        say(f"Nothing new in {where} ({len(skipped)} file{'s' if len(skipped) != 1 else ''} already imported).")
        return EXIT_OK
    if not files:
        say("No Crexi export found. Put the CSV or XLSX in data/crexi/ or give its path, for example:")
        say("uv run run.py import ~/Downloads/crexi-export.xlsx --limit 5", "  ")
        return EXIT_ERROR
    say(f"Import · {market}" + (" (preview only)" if args.dry_run else ""))
    started = time.monotonic()
    progress = ConsoleProgress()
    try:
        result = pipeline.run_step(conn, market, "import", args.limit, args.dry_run, progress=progress, path=files)
    finally:
        progress.clear()
    show_result(result)
    say(f"Done in {elapsed(started)}.")
    return EXIT_OK if result["ok"] else EXIT_ERROR


def cmd_estimate(args: argparse.Namespace, conn: sqlite3.Connection, market: str) -> int:
    est = pipeline.estimate(conn, market, args.limit)
    limit = f", limit {args.limit}" if args.limit else ""
    say(f"Cost estimate · {est['name']} ({est['properties']:,} properties{limit}). Nothing was called.")
    show_estimate(est)
    return EXIT_OK


def cmd_export(args: argparse.Namespace, conn: sqlite3.Connection, market: str) -> int:
    started = time.monotonic()
    result = pipeline.run_step(conn, market, "export", dry_run=args.dry_run)
    if not result["ok"]:
        show_result(result)
        return EXIT_ERROR
    if result["dry_run"]:
        say(result["message"])
        say(f"Done in {elapsed(started)}.")
        return EXIT_OK
    say(f"Saved {len(result['files'])} files to {result['folder']}:")
    for name in result["files"]:
        say(name, "  ")
    handed = result["counts"].get("handed_off", 0)
    if handed:
        say(f"{handed:,} newly confirmed owner{'s are' if handed != 1 else ' is'} in crm_handoff.csv and now "
            f"marked as handed off to the CRM.")
    say(f"Done in {elapsed(started)}.")
    return EXIT_OK


def cmd_status(args: argparse.Namespace, conn: sqlite3.Connection, markets: list[str]) -> int:
    keys = pipeline.keys_present()
    yes_no = {True: "yes", False: "no"}
    say("Blue Moon Deal Finder status")
    say("Keys in .env (values are never shown): " + ", ".join(
        f"{label} {yes_no[keys[k]]}" for k, label in (("google", "Google"), ("anthropic", "Anthropic"),
                                                      ("hud", "HUD"), ("census", "Census"),
                                                      ("app_password", "dashboard password"))))
    say(f"Crexi credits: {credit_status(conn)['message']}")
    empty = []
    for slug in markets:
        status = pipeline.market_status(conn, slug)
        if status["properties"]:
            say()
            show_market_status(status)
        else:
            empty.append(slug)
    if empty:
        say()
        say(f"No properties yet: {', '.join(empty)}. Import an export with: "
            f"uv run run.py import <file> --market <market>")
    return EXIT_OK


def show_market_status(s: dict) -> None:
    say(f"{s['name']}  ({s['market']})")
    imp = s["last_import"]
    last = f" Last import: {imp['filename']} on {str(imp['created_at'])[:10]}." if imp else ""
    box = f"{s['in_buybox']:,} in the buy box" if s["scored"] else "not scored yet"
    say(f"{s['properties']:,} properties, {box}.{last}", "  ")
    names = s["level_names"]
    levels = " · ".join(f"{lvl} {names.get(lvl, '')} {s['levels'].get(lvl, 0):,}" for lvl in (1, 2, 3))
    say(f"Contact levels: {levels}. Penetration {s['penetration']:.0%} ({s['penetration_text']}).", "  ")
    layers = " · ".join(f"{x['label']} {x['done']:,}/{x['total']:,}" + (f" ({x['note']})" if x["note"] else "")
                        for x in s["layers"])
    say(f"Layers: {layers}", "  ")


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    host = args.host or str(settings.app.get("host") or "127.0.0.1")
    port = int(args.port or settings.app.get("port") or 8765)
    if importlib.util.find_spec("bluemoon.web.app") is None:
        say("The dashboard isn't installed yet (bluemoon/web/app.py is missing).")
        return EXIT_ERROR
    import uvicorn

    if host in ("0.0.0.0", "::", "") and not has_secret("APP_PASSWORD"):
        # Every network the Mac is on (home or office Wi-Fi, not just Tailscale) would see the owner list.
        # Run on this computer only instead of refusing to start (a refusal would loop under the service).
        say("Not opening the dashboard to other devices: APP_PASSWORD is blank in .env. Set APP_PASSWORD in "
            ".env (a long passphrase) and restart to use it from your phone. Running on this computer only "
            "for now.")
        host = "127.0.0.1"
    password = secret("APP_PASSWORD") or ""
    if password and len(password) < MIN_PASSWORD_LENGTH:
        say(f"Warning: APP_PASSWORD is short. Use a long passphrase (4 or more random words, "
            f"{MIN_PASSWORD_LENGTH}+ characters) so nobody can guess it.")
    local = "localhost" if host in ("0.0.0.0", "::", "127.0.0.1") else host
    say(f"Blue Moon Deal Finder is running: open http://{local}:{port} in your browser. Press Ctrl+C to stop.")
    if host in ("0.0.0.0", "::"):
        say(f"From your phone over Tailscale: http://{socket.gethostname()}:{port}")
    uvicorn.run("bluemoon.web.app:app", host=host, port=port, log_level="warning")
    return EXIT_OK


# --- main --------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        settings = get_settings()
        if args.command == "serve":
            return cmd_serve(args, settings)
        markets = ([resolve_market(args.market, settings)] if args.market or args.command != "status"
                   else [m.slug for m in settings.markets])
        conn = connect(settings=settings)
        try:
            return dispatch(args, conn, markets)
        finally:
            conn.close()
    except KeyboardInterrupt:
        say("Stopped. Work saved before you stopped is kept.")
        return EXIT_ERROR
    except ValueError as exc:  # plain-English problems Seth can fix
        say(str(exc))
        return EXIT_ERROR
    except Exception as exc:
        if args.debug:
            raise
        log.debug("Unexpected error", exc_info=True)
        say(f"Something went wrong ({type(exc).__name__}: {exc}). Run the same command with --debug for details.")
        return EXIT_ERROR


def dispatch(args: argparse.Namespace, conn: sqlite3.Connection, markets: list[str]) -> int:
    if args.command == "status":
        return cmd_status(args, conn, markets)
    market = markets[0]
    if args.command == "import":
        return cmd_import(args, conn, market)
    if args.command == "estimate":
        return cmd_estimate(args, conn, market)
    if args.command == "export":
        return cmd_export(args, conn, market)
    return cmd_step(args, conn, market)


if __name__ == "__main__":
    os.umask(0o077)   # the database, photos, exports and logs it creates are readable by this user only
    sys.exit(main())
