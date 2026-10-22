"""The web-search guard for AI research: a Claude Code PreToolUse hook (research.ResearchCLI adds it).

Claude Code runs it before every web search the research makes, with the search on stdin. It adds the blocked
people-search, background-check and data-broker sites (and Crexi) to the search's blocked_domains, so their
results never reach Claude. When the search names allowed_domains instead (Claude Code refuses both lists in one
search), the blocked sites are taken out of that list, and a search left with none is refused. Anything
unexpected refuses the search (exit code 2): it never lets a search through unguarded.

Standard library only, so it runs on its own: python -I websearch_guard.py '<blocked sites as a JSON list>'
"""
from __future__ import annotations

import json
import sys
from typing import Any


def _site(value: Any) -> str:
    """'https://www.Spokeo.com/x', '*.spokeo.com' -> 'spokeo.com'."""
    d = str(value or "").strip().lower()
    d = d.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].strip(".")
    return d.removeprefix("*.").removeprefix("www.")


def _blocked(site: str, blocked: list[str]) -> bool:
    return any(site == b or site.endswith("." + b) for b in blocked)


def guard(tool_input: dict, blocked: list[str]) -> dict:
    """The hook's answer (hookSpecificOutput) for one WebSearch call."""
    out = dict(tool_input)
    allowed = [d for d in (out.get("allowed_domains") or []) if str(d or "").strip()]
    if allowed:
        keep = [d for d in allowed if not _blocked(_site(d), blocked)]
        if not keep:
            return {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "Those sites are never used for owner research."}
        out["allowed_domains"] = keep
        out.pop("blocked_domains", None)
    else:
        out.pop("allowed_domains", None)
        out["blocked_domains"] = list(dict.fromkeys([*(out.get("blocked_domains") or []), *blocked]))
    return {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": out}


def main(argv: list[str]) -> int:
    try:
        blocked = [_site(d) for d in json.loads(argv[1])]
        blocked = [d for d in blocked if d]
        event = json.load(sys.stdin)
        tool_input = event.get("tool_input") if isinstance(event, dict) else None
        if not blocked or not isinstance(tool_input, dict):
            raise ValueError("no blocked sites or no search to check")
        print(json.dumps({"hookSpecificOutput": guard(tool_input, blocked)}))
        return 0
    except Exception as exc:  # any problem refuses the search
        print(f"Web search stopped: the people-search block couldn't be added ({exc}).", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
