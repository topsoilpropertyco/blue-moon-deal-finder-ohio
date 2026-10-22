"""AI web research (bluemoon/drill/research.py): the ai_research DRILL stage on the Claude plan.

No real Claude calls: Claude Code is replaced by a fake subprocess runner, and the research itself by a scripted
backend. Owner names, people, phones and emails are made up (555 numbers, example.com sites).
"""
from __future__ import annotations

import itertools
import json
import re
import subprocess
import threading
import time

import pytest
import yaml

from bluemoon import jobs, pipeline
from bluemoon.db import market_by_slug, now_iso, one, rows, scalar
from bluemoon.drill import engine as E
from bluemoon.drill import research as R
from bluemoon.imagery.vision import VisionFatalError

MARKET = "apartments-springfield-oh"
OTHER = "self-storage-springfield-oh"
SRC = "https://www.example.org/oh-sos/filing/4455"
SITE = "https://maplerow.example.com"
_ids = itertools.count(1)


# --- helpers ---------------------------------------------------------------------------------

def add_prop(conn, market=MARKET, **kw) -> int:
    n = next(_ids)
    m = market_by_slug(conn, market)
    data = {
        "market_id": m["id"], "property_key": f"research:{n}", "address": f"{200 + n} SAMPLE AVE",
        "city": "SPRINGFIELD", "state": "OH", "zip": "45503", "county": "CLARK", "apn": f"340-02-00-{n:03d}-000",
        "lat": 39.9 + n / 1000, "lng": -83.8, "units": 24, "property_type": "Multifamily", "owner_name": None,
        "mailing_address": "12 MADE UP RD", "mailing_city": "SPRINGFIELD", "mailing_state": "OH",
        "mailing_zip": "45504", "raw_json": "{}", "flags_json": "[]", "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    data.update(kw)
    cols = ",".join(data)
    cur = conn.execute(f"INSERT INTO properties({cols}) VALUES({','.join('?' for _ in data)})", tuple(data.values()))
    conn.commit()
    return int(cur.lastrowid)


def owner_prop(conn, owner: str, *, market=MARKET, owner_type="small_llc", portfolio_key=None, score=0.5,
               in_buybox=1, **kw) -> int:
    pid = add_prop(conn, market, owner_name=owner, **kw)
    conn.execute("INSERT OR REPLACE INTO owners(property_id, owner_type, owner_norm, portfolio_key, computed_at) "
                 "VALUES(?,?,?,?,?)", (pid, owner_type, owner.upper(), portfolio_key, now_iso()))
    conn.execute("INSERT OR REPLACE INTO scores(property_id, in_buybox, exclusion_reasons, score, rank, computed_at) "
                 "VALUES(?,?,?,?,?,?)", (pid, in_buybox, "[]" if in_buybox else '["drive_time"]', score, None,
                                         now_iso()))
    conn.commit()
    return pid


def answer(**over) -> dict:
    base = {
        "entities": [{"name": "MAPLE ROW HOLDINGS LLC", "state": "OH", "status": "Active", "filing_id": "4455",
                      "source_url": SRC, "confidence": "high", "match_reason": "Same name in the Ohio filing."}],
        "people": [
            {"name": "Robin Sample", "role": "member", "source_url": SRC, "confidence": "high",
             "match_reason": "Named as member in the filing."},
            {"name": "C T Corporation System", "role": "registered_agent", "source_url": SRC, "confidence": "high",
             "match_reason": "Statutory agent on the filing."}],
        "websites": [{"url": SITE, "source_url": SRC, "confidence": "medium",
                      "match_reason": "Linked from the filing."}],
        "contacts": [
            {"channel": "phone", "value": "937.555.0142", "label": "Office line", "belongs_to": "business",
             "person_name": "", "source_url": f"{SITE}/contact", "confidence": "high",
             "match_reason": "On the company's contact page."},
            {"channel": "email", "value": "Leasing@MapleRow.example.com", "label": "Office email",
             "belongs_to": "business", "person_name": "", "source_url": f"{SITE}/contact", "confidence": "medium",
             "match_reason": "On the company's contact page."},
            {"channel": "phone", "value": "(937) 555-0199", "label": "Cell", "belongs_to": "owner",
             "person_name": "Robin Sample", "source_url": "https://www.truepeoplesearch.com/find/person/abc",
             "confidence": "high", "match_reason": "People-search listing."},
            {"channel": "phone", "value": "(800) 555-0100", "label": "Agent line", "belongs_to": "registered_agent",
             "person_name": "C T Corporation System", "source_url": SRC, "confidence": "high", "match_reason": ""},
            {"channel": "email", "value": "maybe@maplerow.example.com", "label": "Guess", "belongs_to": "owner",
             "person_name": "", "source_url": "", "confidence": "high", "match_reason": ""},
            {"channel": "social", "value": "https://www.facebook.com/maplerowapts", "label": "Facebook page",
             "belongs_to": "business", "person_name": "", "source_url": "https://www.facebook.com/maplerowapts",
             "confidence": "low", "match_reason": "Same name, city not shown."}],
        "summary": "Found the Ohio filing and the company website.",
        "not_found_reason": "",
    }
    base.update(over)
    return base


NOTHING = {"entities": [], "people": [], "websites": [], "contacts": [], "summary": "",
           "not_found_reason": "No public filing or business page for this owner."}


class Scripted:
    """A research backend with set answers per owner name. Counts every ask."""
    name = "scripted"
    model = "scripted-1"
    provider = ""
    sku = ""

    def __init__(self, answers: dict | None = None, default: dict | BaseException | None = None):
        self.answers = answers or {}
        self.default = NOTHING if default is None else default
        self.prompts: list[str] = []
        self.last_result = None

    def check(self) -> None:
        pass

    def ask(self, prompt: str, *, schema=None) -> str:
        self.prompts.append(prompt)
        owner = re.search(r"^Owner on record: (.+)$", prompt, re.MULTILINE).group(1)
        reply = self.answers.get(owner, self.default)
        if isinstance(reply, BaseException):
            raise reply
        return json.dumps(reply)

    @property
    def owners(self) -> list[str]:
        return [re.search(r"^Owner on record: (.+)$", p, re.MULTILINE).group(1) for p in self.prompts]


class FakeRunner:
    """Stands in for subprocess.run. Each reply: (stdout, stderr, returncode)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        out, err, code = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return subprocess.CompletedProcess(cmd, code, out, err)


def cli_result(structured: dict | None = None, *, subtype="success", is_error=False, result="") -> str:
    data = {"type": "result", "subtype": subtype, "is_error": is_error, "result": result, "num_turns": 7,
            "total_cost_usd": 0.05, "session_id": "s1"}
    if structured is not None:
        data["structured_output"] = structured
    return json.dumps(data)


def research_cli(runner, settings=None) -> R.ResearchCLI:
    cfg = R.research_config(settings)
    return R.ResearchCLI(cfg.cli_config(), blocked_domains=cfg.blocked_domains, runner=runner,
                         sleep=lambda s: None, which=lambda c: "/usr/local/bin/claude", clock=lambda: 0.0)


def set_research(root, **values) -> None:
    path = root / "config" / "drill.yaml"
    data = yaml.safe_load(path.read_text())
    data.setdefault("ai_research", {}).update(values)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    from bluemoon.config import get_settings
    get_settings.cache_clear()


def contacts_of(conn, pid) -> list[dict]:
    return rows(conn, "SELECT * FROM contact_points WHERE property_id=? AND source='ai_research' ORDER BY id", (pid,))


# --- config and the stage ----------------------------------------------------------------------

def test_stage_and_settings_come_from_drill_yaml(settings):
    cfg = E.drill_config(settings)
    st = cfg.by_key["ai_research"]
    assert st.background and st.automation == "auto" and st.cost == "plan" and st.goal == "reach"
    assert "government" not in st.applies_to and "institutional" not in st.applies_to
    keys = [s.key for s in cfg.stages]
    assert keys.index("portfolio_match") < keys.index("ai_research") < keys.index("crexi_credit")
    rc = R.research_config(settings)
    assert rc.model == "sonnet" and rc.timeout_seconds == 420 and rc.max_turns == 20 and rc.pause_seconds == 5
    assert rc.max_calls_per_run == 100 and rc.min_confidence == "medium" and rc.scope == "buybox"
    assert rc.refresh_after_days == 180 and rc.confirm_over_properties == 50
    for d in ("truepeoplesearch.com", "spokeo.com", "whitepages.com", "beenverified.com", "zoominfo.com",
              "crexi.com"):
        assert d in rc.blocked_domains
    assert len(rc.blocked_domains) > 100 and "CT Corporation" in rc.registered_agent_services


def test_built_in_blocklist_always_applies(tmp_root, settings):
    set_research(tmp_root, blocked_domains=[])
    rc = R.research_config()
    assert "crexi.com" in rc.blocked_domains and "truepeoplesearch.com" in rc.blocked_domains
    set_research(tmp_root, blocked_domains=None)                 # a missing or empty key still blocks them
    assert "truepeoplesearch.com" in R.research_config().blocked_domains
    assert "crexi.com" in R.ResearchConfig().blocked_domains and R.deny_rules(R.ResearchConfig().blocked_domains)


def test_blocked_sites_match_subdomains_only():
    domains = ["truepeoplesearch.com", "crexi.com"]
    assert R.blocked_by("https://www.truepeoplesearch.com/find/x", domains) == "truepeoplesearch.com"
    assert R.blocked_by("http://TruePeopleSearch.com", domains) == "truepeoplesearch.com"
    assert R.blocked_by("https://www.crexi.com/properties/1", domains) == "crexi.com"
    assert R.blocked_by("https://nottruepeoplesearch.com", domains) is None
    assert R.blocked_by("https://not-truepeoplesearch.com/x", domains) is None
    assert R.blocked_by("https://www.example.org/oh-sos/filing/4455", domains) is None
    assert R.url_host("ftp://example.org/x") is None and R.url_host("javascript:alert(1)") is None
    assert R.normalize_domain("https://www.Spokeo.com/x") == "spokeo.com" and R.normalize_domain("*.a.com") == "a.com"


@pytest.mark.parametrize("url", [
    "https://web.archive.org/web/2024/https://www.truepeoplesearch.com/find/person/x",   # an archive copy
    "https://archive.ph/www.truepeoplesearch.com/find/x",
    "https://web.archive.org/web/2024/https://WWW.TruePeopleSearch.COM/find/x",           # any letter case
    "https://r.jina.ai/https://www.truepeoplesearch.com/find/x",                         # a page reader
    "https://www-truepeoplesearch-com.translate.goog/find/x",                            # a translated copy
    "https://www.google.com/url?q=https%253A%252F%252Fwww.truepeoplesearch.com%252Fx",   # a redirect, encoded twice
    "https://www.example.org/truepeoplesearch.com",                                      # carries the address
    "https://truepeoplesearch.com\\@example.org/x",                                     # browsers open the first site
    "https://%74ruepeoplesearch.com/x",                                                  # percent-coded letter
    "https://\uff54\uff52\uff55\uff45peoplesearch.com/x",                                  # full-width letters
])
def test_copies_and_disguised_links_of_blocked_sites_are_blocked(url):
    assert R.blocked_by(url, ["truepeoplesearch.com", "crexi.com"]) == "truepeoplesearch.com"


def test_links_a_browser_could_read_differently_are_not_web_links():
    for url in ("https://example.org\\@example.com/x", "https://user@example.org/x", "https://%65xample.org/x",
                "https://\uff45xample.org/x", "https://exa mple.org/x"):
        assert R.url_host(url) is None, url
    assert R.url_host("https://Maplerow.Example.com:443/contact") == "maplerow.example.com"


def test_screen_blocks_findings_seen_on_a_copy_of_a_blocked_site(settings):
    wrapped = "https://web.archive.org/web/2024/https://www.whitepages.com/name/Robin-Sample/Springfield-OH"
    raw = answer(contacts=[{"channel": "phone", "value": "(937) 555-0199", "label": "Cell", "belongs_to": "owner",
                            "person_name": "Robin Sample", "source_url": wrapped, "confidence": "high",
                            "match_reason": ""}])
    found = [f for f in R.screen(R.ResearchAnswer.model_validate(raw), R.research_config()) if f.kind == "phone"]
    assert [(f.status, f.value) for f in found] == [("blocked", "")]
    clean, dropped = R.without_blocked(R.ResearchAnswer.model_validate(raw), R.research_config())
    assert dropped == 1 and "0199" not in clean.model_dump_json()


# --- the Claude Code command ---------------------------------------------------------------------

def test_cli_argv_has_every_safety_flag_and_no_keys(settings, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GOOGLE_MAPS_API_KEY", "HUD_API_TOKEN", "APP_PASSWORD", "OTHER_SERVICE_API_KEY"):
        monkeypatch.setenv(k, "should-not-pass")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "plan-login-stays")
    runner = FakeRunner((cli_result(answer()), "", 0))
    be = research_cli(runner)
    text = be.ask("PROMPT", schema=R.research_schema())
    assert R.parse_answer(text).entities[0].name == "MAPLE ROW HOLDINGS LLC"
    call = runner.calls[0]
    cmd = call["cmd"]
    assert cmd[:3] == ["/usr/local/bin/claude", "-p", "PROMPT"]
    i = cmd.index
    assert cmd[i("--output-format") + 1] == "json" and cmd[i("--model") + 1] == "sonnet"
    assert cmd[i("--tools") + 1] == "WebSearch,WebFetch"
    assert cmd[i("--allowedTools") + 1:i("--allowedTools") + 3] == ["WebSearch", "WebFetch"]
    assert cmd[i("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[i("--max-turns") + 1] == "20"
    assert json.loads(cmd[i("--json-schema") + 1])["required"] == ["entities", "people", "websites", "contacts",
                                                                   "summary", "not_found_reason"]
    deny = cmd[i("--disallowedTools") + 1:i("--json-schema")]
    for rule in ("WebFetch(domain:truepeoplesearch.com)", "WebFetch(domain:*.truepeoplesearch.com)",
                 "WebFetch(domain:crexi.com)", "WebFetch(domain:*.crexi.com)", "WebFetch(domain:spokeo.com)",
                 "WebFetch(domain:*.archive.org)", "WebFetch(domain:*.translate.goog)", "WebFetch(domain:r.jina.ai)"):
        assert rule in deny
    assert all(r.startswith("WebFetch(domain:") for r in deny)
    hook = json.loads(cmd[i("--settings") + 1])["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "WebSearch" and "websearch_guard.py" in hook["hooks"][0]["command"]
    joined = " ".join(cmd[3:])
    for bad in ("--bare", "bypassPermissions", "--dangerously-skip-permissions", "--add-dir", "Bash", "Read",
                "Write", "Edit"):
        assert bad not in joined, bad
    env = call["env"]
    for k in ("ANTHROPIC_API_KEY", "GOOGLE_MAPS_API_KEY", "HUD_API_TOKEN", "APP_PASSWORD", "OTHER_SERVICE_API_KEY"):
        assert k not in env, k
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "plan-login-stays"
    assert call["cwd"].endswith("bluemoon-research") and "blue-moon-deal-finder" not in call["cwd"]
    assert call["timeout"] == 420 and call["stdin"] == subprocess.DEVNULL
    assert be.last_result.num_turns == 7


def test_every_web_search_gets_the_blocked_sites(settings):
    """The --settings hook really runs (through the shell, as Claude Code runs it) and adds the blocked list."""
    from bluemoon.drill import websearch_guard as G
    cfg = R.research_config()
    command = R.websearch_settings(cfg.blocked_domains)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    def run_hook(tool_input) -> subprocess.CompletedProcess:
        event = {"hook_event_name": "PreToolUse", "tool_name": "WebSearch", "tool_input": tool_input}
        return subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True,
                              timeout=30)

    done = run_hook({"query": "Maple Row Holdings LLC Springfield OH", "mode": "extended"})
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow" and out["hookEventName"] == "PreToolUse"
    new = out["updatedInput"]
    assert new["query"] == "Maple Row Holdings LLC Springfield OH" and new["mode"] == "extended"
    assert "truepeoplesearch.com" in new["blocked_domains"] and "crexi.com" in new["blocked_domains"]
    assert "allowed_domains" not in new
    # A search limited to some sites: blocked ones are taken out of that list (both lists can't be sent).
    out = G.guard({"query": "x", "allowed_domains": ["www.spokeo.com", "ohiosos.gov"], "blocked_domains": ["a.com"]},
                  cfg.blocked_domains)
    assert out["updatedInput"]["allowed_domains"] == ["ohiosos.gov"] and "blocked_domains" not in out["updatedInput"]
    assert G.guard({"query": "x", "allowed_domains": ["spokeo.com"]}, cfg.blocked_domains)["permissionDecision"] == \
        "deny"
    # Anything unexpected refuses the search instead of letting it through unguarded.
    bad = subprocess.run(command, shell=True, input="not json", capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2 and "Web search stopped" in bad.stderr


@pytest.mark.parametrize("stderr", ["error: unknown option '--allowedTools'",
                                    "error: unknown option '--permission-mode'",
                                    "error: option '--permission-mode <mode>' argument 'dontAsk' is invalid. "
                                    "Allowed choices are default, plan."])
def test_cli_never_drops_a_safety_flag(settings, stderr):
    runner = FakeRunner(("", stderr, 1))
    be = research_cli(runner)
    with pytest.raises(VisionFatalError, match="claude update"):
        be.ask("PROMPT", schema=R.research_schema())
    assert len(runner.calls) == 1 and not be.dropped_flags


def test_cli_plan_limit_stops_the_research(settings):
    runner = FakeRunner((cli_result(is_error=True, subtype="error", result="Claude AI usage limit reached"), "", 1))
    with pytest.raises(VisionFatalError, match="AI research stopped"):
        research_cli(runner).ask("PROMPT", schema=R.research_schema())


def test_prompt_names_public_facts_only(conn, settings):
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    t = R.owner_groups(conn, market_by_slug(conn, MARKET), R.research_config(), settings)
    prompt = R.build_prompt(t.groups[0], R.research_config(), "Apartments")
    assert "Owner on record: MAPLE ROW HOLDINGS LLC" in prompt and "SPRINGFIELD, OH" in prompt
    assert "12 MADE UP RD" not in prompt                         # never the mailing street
    assert f"{200 + pid} SAMPLE AVE, SPRINGFIELD, OH (Clark County)" in prompt
    for text in ("truepeoplesearch.com", "crexi.com", "Never give home addresses", "Never guess an email",
                 "source_url", "match_reason", "Ohio Secretary of State", "INBiz", "OpenCorporates"):
        assert text in prompt, text


# --- the filter --------------------------------------------------------------------------------

def test_screen_drops_blocked_unsourced_and_agent_findings(settings):
    found = {(f.kind, f.label): f for f in R.screen(R.ResearchAnswer.model_validate(answer()), R.research_config())}
    assert found[("entity", "Business filing (OH, Active, #4455)")].status == "kept"
    member = found[("person", "Member")]
    assert member.status == "kept" and member.role == "member"
    assert found[("person", "Statutory agent")].status == "rejected"          # an agent company is not the owner
    assert found[("website", "Website")].status == "kept"
    office = found[("phone", "Office line")]
    assert office.status == "kept" and office.value == "(937) 555-0142"
    assert found[("email", "Office email")].value == "leasing@maplerow.example.com"
    cell = found[("phone", "Phone")]                                # its own label ("Cell") isn't kept either
    assert cell.status == "blocked" and cell.value == "" and cell.person_name is None and cell.match_reason == ""
    assert found[("phone", "Agent line")].status == "rejected"
    assert found[("email", "Guess")].status == "rejected" and "No link" in found[("email", "Guess")].why
    assert found[("social", "Facebook page")].status == "lead"


def test_screen_rules_for_values_and_roles(settings):
    cfg = R.research_config()
    raw = answer(people=[{"name": "Jordan Example", "role": "registered_agent", "source_url": SRC,
                          "confidence": "high"},
                         {"name": "Casey Example", "role": "organizer", "source_url": SRC, "confidence": "high"},
                         {"name": "Sam Example", "role": "possible_owner", "source_url": SRC, "confidence": "high"}],
                 websites=[{"url": "https://www.spokeo.com/Maple-Row", "source_url": SRC, "confidence": "high"}],
                 contacts=[{"channel": "phone", "value": "555-01", "source_url": SRC, "confidence": "high"},
                           {"channel": "social", "value": "@maplerow", "source_url": SRC, "confidence": "high"},
                           {"channel": "fax", "value": "937 555 0111", "source_url": SRC, "confidence": "high"},
                           {"channel": "email", "value": "a@b.example.com", "source_url": "ftp://x.example.com/a",
                            "confidence": "high"}])
    found = R.screen(R.ResearchAnswer.model_validate(raw), cfg)
    roles = {f.value: f.role for f in found if f.kind == "person"}
    assert roles == {"Jordan Example": "agent", "Casey Example": "other", "Sam Example": "other"}
    assert all(f.role not in ("possible_owner", "confirmed_owner", "owner_of_record") for f in found)
    by_value = {f.value: f for f in found if f.kind != "person"}
    assert by_value[""].status == "blocked"                              # the spokeo page itself
    assert {f.status for f in found if f.channel or f.kind == "contact"} == {"rejected"}


def test_cached_answers_never_hold_blocked_findings(settings):
    clean, dropped = R.without_blocked(R.ResearchAnswer.model_validate(answer()), R.research_config())
    assert dropped == 1 and "truepeoplesearch" not in clean.model_dump_json() and "0199" not in clean.model_dump_json()


def test_nothing_claude_wrote_about_a_blocked_site_is_stored_or_shown(conn, settings):
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    raw = answer(
        people=[{"name": "Robin Sample", "role": "member", "source_url": "https://www.spokeo.com/Robin-Sample",
                 "confidence": "high", "match_reason": "Spokeo: age 71, lives at 12 Elm St, relatives Ann and Bob"}],
        contacts=[{"channel": "phone", "value": "(937) 555-0199", "label": "Home phone 937-555-0199 (Whitepages)",
                   "belongs_to": "owner", "person_name": "Robin Sample",
                   "source_url": "https://www.whitepages.com/name/Robin-Sample", "confidence": "high",
                   "match_reason": "Whitepages lists 937-555-0199 at 12 Elm St"}],
        summary="Whitepages shows the owner's cell 937-555-0199, age 71.")
    res = R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": raw}))
    assert res["blocked_dropped"] == 2
    for table in ("research_findings", "research_runs", "api_cache", "drill_attempts", "people", "contact_points"):
        dump = json.dumps(rows(conn, f"SELECT * FROM {table}"))
        for bit in ("0199", "Elm St", "age 71", "Ann and Bob", "Whitepages", "Spokeo"):
            assert bit not in dump, (table, bit)
    blocked = rows(conn, "SELECT label, match_reason FROM research_findings WHERE status='blocked'")
    assert sorted(b["label"] for b in blocked) == ["Person", "Phone"] and not any(b["match_reason"] for b in blocked)
    shown = json.dumps(E.property_drill(conn, pid)["research"])
    assert "0199" not in shown and "Elm St" not in shown


def test_free_text_never_keeps_a_phone_or_email(conn, settings):
    owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    raw = answer(contacts=[], summary="Office line is 937-555-0142; write to leasing@maplerow.example.com.",
                 entities=[{"name": "MAPLE ROW HOLDINGS LLC", "state": "OH", "status": "Active", "filing_id": "4455",
                            "source_url": SRC, "confidence": "high",
                            "match_reason": "Same name; a search result also showed (937) 555-0177."}])
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": raw}))
    run = one(conn, "SELECT summary FROM research_runs")
    assert run["summary"] == "Office line is [phone removed]; write to [email removed]."
    for table in ("research_findings", "research_runs", "api_cache"):
        dump = json.dumps(rows(conn, f"SELECT * FROM {table}"))
        assert "0142" not in dump and "0177" not in dump and "leasing@" not in dump, table


# --- a run -------------------------------------------------------------------------------------

def test_run_saves_untested_level_2_contacts_with_their_source(conn, settings):
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    E.run_auto_stages(conn, MARKET)
    be = Scripted({"MAPLE ROW HOLDINGS LLC": answer()})
    res = R.research_owners(conn, MARKET, backend=be)
    assert res["researched"] == 1 and res["found"] == 1 and res["contacts_added"] == 2 and res["people_added"] == 1
    assert res["blocked_dropped"] == 1 and res["leads"] == 1 and "not tested yet" in res["message"]
    cps = contacts_of(conn, pid)
    assert [(c["channel"], c["value"], c["status"]) for c in cps] == [
        ("phone", "(937) 555-0142", "untested"), ("email", "leasing@maplerow.example.com", "untested")]
    assert cps[0]["notes"] == f"AI research: Office line. Source: {SITE}/contact"
    people = rows(conn, "SELECT name, role, source, notes FROM people WHERE property_id=? AND source='ai_research'",
                  (pid,))
    assert [(p["name"], p["role"]) for p in people] == [("Robin Sample", "member")]
    assert people[0]["notes"] == f"AI research: Member. Source: {SRC}"
    st = one(conn, "SELECT * FROM drill_state WHERE property_id=?", (pid,))
    assert st["level"] == 2 and st["confirmed_at"] is None and st["status"] == "active"
    attempt = one(conn, "SELECT * FROM drill_attempts WHERE property_id=? AND stage_key='ai_research'", (pid,))
    assert attempt["outcome"] == "found" and attempt["actor"] == "ai" and "MAPLE ROW HOLDINGS LLC" in attempt["detail"]
    d = E.property_drill(conn, pid)
    assert d["level"] == 2 and d["next_step"]["kind"] == "validate" and d["channels"]["phone"] == "untested"
    # Nothing from the people-search site is stored anywhere.
    for table in ("contact_points", "research_findings", "people", "api_cache"):
        dump = json.dumps(rows(conn, f"SELECT * FROM {table}"))
        assert "0199" not in dump and "truepeoplesearch.com/find" not in dump, table
    run = one(conn, "SELECT * FROM research_runs")
    assert run["outcome"] == "found" and json.loads(run["property_ids"]) == [pid] and run["actor"] == "ai"
    statuses = sorted(f["status"] for f in rows(conn, "SELECT status FROM research_findings"))
    assert statuses.count("kept") == 5 and statuses.count("blocked") == 1 and statuses.count("lead") == 1
    research = d["research"]
    assert research["runs"][0]["outcome"] == "found"
    phone = next(f for f in research["findings"] if f["contact_point_id"] == cps[0]["id"])
    assert phone["source_url"] == f"{SITE}/contact" and phone["confidence"] == "high"


def test_one_call_per_owner_written_to_every_property(conn, settings):
    a = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple", score=0.6)
    b = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple", score=0.2)
    be = Scripted({"MAPLE ROW HOLDINGS LLC": answer()})
    res = R.research_owners(conn, MARKET, backend=be)
    assert len(be.prompts) == 1 and res["owners"] == 1 and res["properties"] == 2 and res["found_properties"] == 2
    assert len(contacts_of(conn, a)) == len(contacts_of(conn, b)) == 2
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE stage_key='ai_research' AND actor='ai'") == 2


def test_owners_sharing_a_portfolio_are_each_searched_for_themselves(conn, settings):
    # One mailing address (a manager's) joins two different owners into one portfolio key: each owner gets its own
    # search, and one owner's findings never land on the other's property.
    a = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-shared", score=0.6)
    b = owner_prop(conn, "OAK LANE PROPERTIES LLC", portfolio_key="pk-shared", score=0.5)
    be = Scripted({"MAPLE ROW HOLDINGS LLC": answer()})
    res = R.research_owners(conn, MARKET, backend=be)
    assert sorted(be.owners) == ["MAPLE ROW HOLDINGS LLC", "OAK LANE PROPERTIES LLC"] and res["owners"] == 2
    assert len(contacts_of(conn, a)) == 2 and contacts_of(conn, b) == []
    assert not rows(conn, "SELECT * FROM people WHERE property_id=? AND source='ai_research'", (b,))
    outcomes = {r["property_id"]: r["outcome"] for r in rows(
        conn, "SELECT property_id, outcome FROM drill_attempts WHERE stage_key='ai_research'")}
    assert outcomes == {a: "found", b: "nothing"}
    runs = {r["owner_name"]: json.loads(r["property_ids"]) for r in rows(conn, "SELECT * FROM research_runs")}
    assert runs == {"MAPLE ROW HOLDINGS LLC": [a], "OAK LANE PROPERTIES LLC": [b]}


def test_nothing_found_is_a_nothing_check_off(conn, settings):
    pid = owner_prop(conn, "QUIET OWNER LLC")
    res = R.research_owners(conn, MARKET, backend=Scripted())
    assert res["nothing"] == 1 and res["found"] == 0
    a = one(conn, "SELECT outcome, detail FROM drill_attempts WHERE property_id=? AND stage_key='ai_research'", (pid,))
    assert a["outcome"] == "nothing" and "No public filing" in a["detail"]


def test_do_not_contact_owner_groups_are_skipped(conn, settings):
    owner_prop(conn, "OAK LANE PROPERTIES LLC")
    sibling = owner_prop(conn, "OAK LANE PROPERTIES LLC", market=OTHER)
    E.ensure_states(conn, OTHER)
    conn.execute("UPDATE drill_state SET status='dnc' WHERE property_id=?", (sibling,))
    conn.commit()
    owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    be = Scripted({"MAPLE ROW HOLDINGS LLC": answer()})
    res = R.research_owners(conn, MARKET, backend=be)
    assert be.owners == ["MAPLE ROW HOLDINGS LLC"] and res["skipped_dnc"] == 1
    assert "do-not-contact" in R.research_owners(conn, MARKET, backend=be, dry_run=True)["message"]


def test_an_owner_marked_do_not_contact_during_a_run_is_left_alone(conn, settings):
    maple = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", score=0.9)
    oak = owner_prop(conn, "OAK LANE PROPERTIES LLC", score=0.8)
    elm = owner_prop(conn, "ELM STREET RENTALS LLC", score=0.7)
    E.ensure_states(conn, MARKET)

    class MarksWhileAsking(Scripted):
        def ask(self, prompt, *, schema=None):
            reply = super().ask(prompt, schema=schema)
            if "MAPLE ROW" in prompt:            # marked do-not-contact while Claude was searching
                E.level_action(conn, maple, "dnc")
            return reply

    def progress(fraction, message):
        if "owner 1 of" in message:                # marked before its turn came
            E.level_action(conn, oak, "dnc")

    be = MarksWhileAsking({"MAPLE ROW HOLDINGS LLC": answer(), "ELM STREET RENTALS LLC": answer()})
    res = R.research_owners(conn, MARKET, backend=be, progress=progress)
    assert be.owners == ["MAPLE ROW HOLDINGS LLC", "ELM STREET RENTALS LLC"]       # Oak Lane is never searched
    assert res["researched"] == 1 and res["skipped_dnc_during_run"] == 2 and "do-not-contact" in res["message"]
    for pid in (maple, oak):
        assert scalar(conn, "SELECT status FROM drill_state WHERE property_id=?", (pid,)) == "dnc"
        assert not rows(conn, "SELECT * FROM drill_attempts WHERE property_id=? AND stage_key='ai_research'", (pid,))
        assert not rows(conn, "SELECT * FROM people WHERE property_id=? AND source='ai_research'", (pid,))
        assert not rows(conn, "SELECT * FROM research_findings WHERE property_id=?", (pid,))
        assert contacts_of(conn, pid) == []
    assert [json.loads(r["property_ids"]) for r in rows(conn, "SELECT property_ids FROM research_runs")] == [[elm]]


def test_owners_with_no_name_or_set_aside_are_left_out(conn, settings):
    add_prop(conn, owner_name=None)
    owner_prop(conn, "FAR AWAY HOLDINGS LLC", in_buybox=0)
    be = Scripted()
    res = R.research_owners(conn, MARKET, backend=be)
    assert be.prompts == [] and res["researched"] == 0 and "nothing to research" in res["message"]


def test_cache_prevents_a_second_ask_until_it_is_old(conn, settings):
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    be = Scripted({"MAPLE ROW HOLDINGS LLC": answer()})
    R.research_owners(conn, MARKET, backend=be)
    assert len(be.prompts) == 1
    # A new pass opens the stage again; the saved answer is reused, so Claude isn't asked again.
    conn.execute("DELETE FROM drill_attempts WHERE stage_key='ai_research'")
    conn.commit()
    est = R.research_owners(conn, MARKET, backend=be, dry_run=True)
    assert est["cached"] == 1 and est["calls"] == 0
    res = R.research_owners(conn, MARKET, backend=be)
    assert len(be.prompts) == 1 and res["cached"] == 1 and res["calls"] == 0
    assert one(conn, "SELECT from_cache FROM research_runs ORDER BY id DESC LIMIT 1")["from_cache"] == 1
    assert len(contacts_of(conn, pid)) == 2                         # nothing added twice
    # After refresh_after_days the owner is asked again.
    conn.execute("DELETE FROM drill_attempts WHERE stage_key='ai_research'")
    conn.execute("UPDATE api_cache SET created_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    R.research_owners(conn, MARKET, backend=be)
    assert len(be.prompts) == 2


def test_errors_are_not_cached_and_stop_after_three_in_a_row(conn, settings):
    from bluemoon.imagery.vision import VisionCallError
    for i in range(4):
        owner_prop(conn, f"BROKEN OWNER {i} LLC", score=0.9 - i / 10)
    be = Scripted(default=VisionCallError("Claude Code couldn't research this owner (timeout).", calls=1))
    res = R.research_owners(conn, MARKET, backend=be)
    assert len(be.prompts) == 3 and res["errors"] == 3 and res["stopped"] and "in a row" in res["message"]
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache") == 0
    assert scalar(conn, "SELECT COUNT(*) FROM drill_attempts WHERE stage_key='ai_research'") == 0
    assert scalar(conn, "SELECT COUNT(*) FROM research_runs WHERE outcome='error'") == 3


def test_owners_that_keep_failing_go_behind_the_others(conn, settings):
    from bluemoon.imagery.vision import VisionCallError
    timeout = VisionCallError("Claude took longer than 420 seconds for this owner.", calls=1)
    hard = [f"HARD OWNER {i} LLC" for i in range(3)]
    good = [f"GOOD OWNER {i} LLC" for i in range(2)]
    for i, name in enumerate(hard + good):
        owner_prop(conn, name, score=0.9 - i / 20)
    be = Scripted({name: timeout for name in hard})
    first = R.research_owners(conn, MARKET, backend=be)
    assert be.owners == hard and first["stopped"] and first["researched"] == 0
    # The next run starts with the owners not tried yet, so the three that failed can't block it.
    be.prompts.clear()
    second = R.research_owners(conn, MARKET, backend=be, limit=3)
    assert be.owners == [*good, hard[0]] and second["researched"] == 2 and second["errors"] == 1

    def queue() -> list[R.OwnerGroup]:
        return R.owner_groups(conn, market_by_slug(conn, MARKET), R.research_config(settings), settings).groups

    assert [g.name for g in queue()] == [hard[1], hard[2], hard[0]]     # fewest failures first, then best score
    # An owner answered after failing starts again from zero failures.
    del be.answers[hard[1]]
    key = queue()[0].key
    R.research_owners(conn, MARKET, backend=be, limit=1)
    assert key not in R._error_counts(conn, market_by_slug(conn, MARKET)["id"])


def test_test_on_5_makes_5_calls(conn, settings):
    for i in range(8):
        owner_prop(conn, f"SAMPLE OWNER {i} LLC", score=0.9 - i / 20)
    be = Scripted()
    est = R.research_owners(conn, MARKET, limit=5, backend=be, dry_run=True)
    assert est["owners"] == 5 and est["calls"] == 5 and be.prompts == []
    res = R.research_owners(conn, MARKET, limit=5, backend=be)
    assert len(be.prompts) == 5 and res["researched"] == 5
    assert be.owners == [f"SAMPLE OWNER {i} LLC" for i in range(5)]       # best score first


def test_needs_your_ok_over_50_properties(conn, settings):
    ids = [owner_prop(conn, "BIG PORTFOLIO LLC", portfolio_key="pk-big") for _ in range(51)]
    runner = FakeRunner((cli_result(answer()), "", 0))
    be = research_cli(runner)
    est = R.research_owners(conn, MARKET, backend=be, dry_run=True)
    assert est["owners"] == 1 and est["properties"] == 51 and est["needs_confirm"] and est["ready"]
    assert "No per-call bill: uses your Claude plan" in est["message"]
    waiting = R.research_owners(conn, MARKET, backend=be)
    assert waiting["message"].startswith("Waiting for your OK") and runner.calls == []
    done = R.research_owners(conn, MARKET, backend=be, confirm=True)
    assert len(runner.calls) == 1 and done["found_properties"] == 51
    assert all(len(contacts_of(conn, pid)) == 2 for pid in ids)
    assert scalar(conn, "SELECT SUM(units) FROM api_calls WHERE sku='research_claude_cli'") == 1
    small = [owner_prop(conn, "SMALL PORTFOLIO LLC", portfolio_key="pk-small") for _ in range(50)]
    assert small and not R.research_owners(conn, MARKET, backend=be, dry_run=True)["needs_confirm"]


def test_mock_backend_runs_offline(conn, settings):
    for name in ("ELM STREET RENTALS LLC", "BIRCH COURT LLC", "CEDAR WAY HOMES LLC"):
        owner_prop(conn, name)
    res = R.research_owners(conn, MARKET, backend="mock")
    assert res["researched"] == 3 and res["backend"] == "mock" and not res["needs_confirm"]
    # Each mock answer with findings has one phone from a people-search site, which is always dropped.
    assert res["found"] >= 1 and res["blocked_dropped"] == res["found"]
    assert "0199" not in json.dumps(rows(conn, "SELECT value FROM contact_points"))


def test_test_answers_are_never_saved_or_served_as_claudes(conn, settings):
    for name in ("ELM STREET RENTALS LLC", "BIRCH COURT LLC", "CEDAR WAY HOMES LLC"):
        owner_prop(conn, name)
    res = R.research_owners(conn, MARKET, backend="mock")
    assert res["researched"] == 3 and res["contacts_added"] >= 1
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache") == 0                  # made-up answers are never saved
    notes = [r["notes"] for r in rows(conn, "SELECT notes FROM contact_points WHERE source='ai_research'")]
    assert notes and all("offline test answer" in n for n in notes)             # easy to tell apart
    # The owners come up again (a new pass): Claude is asked, never handed the made-up answers.
    conn.execute("DELETE FROM drill_attempts WHERE stage_key='ai_research'")
    conn.commit()
    runner = FakeRunner((cli_result(answer()), "", 0))
    est = R.research_owners(conn, MARKET, backend=research_cli(runner), dry_run=True)
    assert est["cached"] == 0 and est["calls"] == 3
    # A made-up answer an older version saved under Claude's key isn't served either; Claude's own answer is.
    from bluemoon.cache import cache_put
    g = R.owner_groups(conn, market_by_slug(conn, MARKET), R.research_config(settings), settings).groups[0]
    saved = R.ResearchAnswer.model_validate(answer()).model_dump()
    cache_put(conn, g.cache_key, R.CACHE_PROVIDER, 200, {"answer": saved, "meta": {"model": R.MOCK_MODEL}})
    assert R.cached_answer(conn, g, R.research_config(settings)) is None
    cache_put(conn, g.cache_key, R.CACHE_PROVIDER, 200, {"answer": saved, "meta": {"model": "sonnet"}})
    assert R.cached_answer(conn, g, R.research_config(settings)) is not None
    # Another backend's answers are kept under their own key.
    assert R._cache_key(g, R.research_config(settings), "scripted") != g.cache_key


def test_test_answers_also_wait_for_your_ok_over_50_properties(conn, settings):
    ids = [owner_prop(conn, "BIG TEST PORTFOLIO LLC") for _ in range(51)]
    est = R.research_owners(conn, MARKET, backend="mock", dry_run=True)
    assert est["needs_confirm"] and "made up" in est["message"] and "555" in est["message"]
    waiting = R.research_owners(conn, MARKET, backend="mock")
    assert waiting["message"].startswith("Waiting for your OK") and not any(contacts_of(conn, i) for i in ids)
    assert R.research_owners(conn, MARKET, backend="mock", confirm=True)["researched"] == 1


def test_estimate_names_every_owners_properties_and_what_this_run_covers(conn, settings, tmp_root):
    set_research(tmp_root, max_calls_per_run=2)
    for i in range(4):                       # owner i has i + 1 properties: 10 in all
        for _ in range(i + 1):
            owner_prop(conn, f"CAPPED OWNER {i} LLC", score=0.9 - i / 20)
    est = R.research_owners(conn, MARKET, backend=Scripted(), dry_run=True)
    assert est["owners"] == 4 and est["properties_total"] == 10 and est["capped"]
    assert est["calls"] == 2 and est["properties"] == 3                 # this run: the first 2 owners
    assert "4 buy-box owners (10 properties) to research" in est["message"]
    assert "this run covers 2 owners (3 properties); run it again for the rest" in est["message"]


# --- the DRILL around it -------------------------------------------------------------------------

def walk(conn, pid, stop_at=None) -> dict:
    for _ in range(40):
        d = E.property_drill(conn, pid, with_links=False)
        nxt = d["next_step"]
        if nxt["kind"] != "stage" or nxt["stage_key"] == stop_at:
            return d
        E.record_attempt(conn, pid, nxt["stage_key"], "nothing")
    raise AssertionError("the drill never stopped")


def test_background_stage_never_takes_the_next_slot(conn, settings):
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    E.run_auto_stages(conn, MARKET)
    d = E.property_drill(conn, pid)
    st = {s["key"]: s for s in d["stages"]}
    assert d["next_step"]["stage_key"] == "county_auditor"
    assert st["ai_research"]["status"] == "pending" and st["ai_research"]["background"] is True
    assert "ai_research" not in E.waiting_counts(conn, MARKET)["stages"]
    # Only when every other stage is tried does it become the next step; the owner is not exhausted.
    d = walk(conn, pid, stop_at="ai_research")
    assert d["next_step"]["stage_key"] == "ai_research" and d["status"] == "active"
    assert "Run screen" in d["next_step"]["text"]
    R.research_owners(conn, MARKET, backend=Scripted())
    d = E.property_drill(conn, pid)
    assert d["next_step"]["kind"] == "exhausted" and d["status"] == "exhausted"


def test_an_owner_with_no_name_can_still_run_out_of_stages(conn, settings):
    pid = add_prop(conn, owner_name=None)
    E.run_auto_stages(conn, MARKET)
    d = walk(conn, pid)
    assert d["status"] == "exhausted"
    assert {s["key"]: s["status"] for s in d["stages"]}["ai_research"] == "pending"


def test_a_set_aside_owner_can_run_out_of_stages(conn, settings, tmp_root):
    # Research (scope buybox) never reaches a set-aside owner, so it can't hold that owner open.
    pid = owner_prop(conn, "FAR AWAY HOLDINGS LLC", in_buybox=0)
    E.run_auto_stages(conn, MARKET)
    d = walk(conn, pid)
    assert d["status"] == "exhausted" and d["next_step"]["kind"] == "exhausted"
    assert {s["key"]: s["status"] for s in d["stages"]}["ai_research"] == "pending"
    # With scope all it would be in a run: then it waits for the research like a buy-box owner.
    set_research(tmp_root, scope="all")
    other = owner_prop(conn, "DISTANT HOLDINGS LLC", in_buybox=0)
    E.run_auto_stages(conn, MARKET)
    d = walk(conn, other, stop_at="ai_research")
    assert d["next_step"]["stage_key"] == "ai_research" and d["status"] == "active"


def test_found_research_reopens_an_exhausted_owner(conn, settings):
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    E.run_auto_stages(conn, MARKET)
    E.level_action(conn, pid, "exhausted")
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    d = E.property_drill(conn, pid)
    assert d["status"] == "active" and d["next_step"]["kind"] == "validate"


def test_portfolio_match_never_copies_ai_contacts(conn, settings):
    a = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    b = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", in_buybox=0)
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    assert contacts_of(conn, a) and not contacts_of(conn, b)       # b is set aside: not in this run
    E.run_auto_stages(conn, MARKET)
    assert not rows(conn, "SELECT * FROM contact_points WHERE property_id=? AND channel!='mail'", (b,))


def test_a_verified_ai_contact_carries_to_the_owners_other_properties(conn, settings):
    a = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    b = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", in_buybox=0)     # set aside: research never writes to it
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    phone_a = contacts_of(conn, a)[0]
    E.set_contact_status(conn, phone_a["id"], "verified")
    carried = rows(conn, "SELECT * FROM contact_points WHERE property_id=? AND channel!='mail'", (b,))
    assert [(c["value"], c["source"], c["status"]) for c in carried] == [
        (phone_a["value"], "portfolio_match", "untested")]                # the untested AI email stays put
    assert "verified there" in carried[0]["notes"]
    assert not rows(conn, "SELECT * FROM people WHERE property_id=? AND name='Robin Sample'", (b,))


def test_marking_an_ai_contact_wrong_updates_its_finding_everywhere(conn, settings):
    a = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple")
    b = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple")
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    phone_a = contacts_of(conn, a)[0]
    res = E.set_contact_status(conn, phone_a["id"], "wrong", actor="seth@example.org")
    assert "other property of this owner" in res["message"]
    f = one(conn, "SELECT * FROM research_findings WHERE contact_point_id=?", (phone_a["id"],))
    assert f["status"] == "wrong" and f["marked_by"] == "seth@example.org" and f["marked_at"]
    phone_b = contacts_of(conn, b)[0]
    assert phone_b["status"] == "wrong"
    copy = one(conn, "SELECT status FROM research_findings WHERE contact_point_id=?", (phone_b["id"],))
    assert copy["status"] == "wrong"
    # Taking it back on one property keeps the finding again, and puts back every copy the wrong mark reached.
    res = E.set_contact_status(conn, phone_a["id"], "untested", actor="seth@example.org")
    assert "1 other property" in res["message"]
    assert one(conn, "SELECT status FROM research_findings WHERE id=?", (f["id"],))["status"] == "kept"
    assert one(conn, "SELECT status FROM contact_points WHERE id=?", (phone_b["id"],))["status"] == "untested"
    assert one(conn, "SELECT status FROM research_findings WHERE contact_point_id=?",
               (phone_b["id"],))["status"] == "kept"
    assert E.property_drill(conn, b)["next_step"]["kind"] == "validate"
    # The same through the finding's own Wrong / Keep buttons.
    R.mark_finding(conn, f["id"], "wrong", actor="team")
    assert one(conn, "SELECT status FROM contact_points WHERE id=?", (phone_b["id"],))["status"] == "wrong"
    R.mark_finding(conn, f["id"], "kept", actor="team")
    assert [one(conn, "SELECT status FROM contact_points WHERE id=?", (c["id"],))["status"]
            for c in (phone_a, phone_b)] == ["untested", "untested"]
    assert {r["status"] for r in rows(conn, "SELECT status FROM research_findings WHERE kind='phone' AND "
                                            "status IN ('kept','wrong')")} == {"kept"}


def test_a_copy_marked_wrong_on_its_own_keeps_its_mark_after_an_undo_elsewhere(conn, settings):
    a = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple")
    b = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple")
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    phone_a, phone_b = contacts_of(conn, a)[0], contacts_of(conn, b)[0]
    E.set_contact_status(conn, phone_a["id"], "wrong")
    # A later wrong mark on b's own copy (a different time) is b's own: undoing a's mark leaves it.
    conn.execute("UPDATE research_findings SET marked_at='2020-01-01T00:00:00+00:00' WHERE contact_point_id=?",
                 (phone_b["id"],))
    conn.commit()
    E.set_contact_status(conn, phone_a["id"], "untested")
    assert one(conn, "SELECT status FROM contact_points WHERE id=?", (phone_b["id"],))["status"] == "wrong"


def test_logging_bounced_on_an_ai_contact_marks_it_wrong_everywhere(conn, settings):
    a = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple")
    b = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple")
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    phone_a, phone_b = contacts_of(conn, a)[0], contacts_of(conn, b)[0]
    res = E.log_outreach(conn, a, "phone", "bounced", contact_point_id=phone_a["id"], actor="seth@example.org")
    assert "1 other property" in res["message"]
    for c in (phone_a, phone_b):
        assert one(conn, "SELECT status FROM contact_points WHERE id=?", (c["id"],))["status"] == "wrong"
        f = one(conn, "SELECT * FROM research_findings WHERE contact_point_id=?", (c["id"],))
        assert f["status"] == "wrong" and f["marked_by"] == "seth@example.org"
    assert phone_b["id"] not in E.property_drill(conn, b)["next_step"].get("contact_ids", [])   # not asked again
    assert R.research_quality(conn, MARKET)["phone"]["wrong"] == 2
    # Talking to the owner on it after all keeps the finding again and puts the copy back.
    E.log_outreach(conn, a, "phone", "reached_confirmed", contact_point_id=phone_a["id"])
    assert one(conn, "SELECT status FROM research_findings WHERE contact_point_id=?",
               (phone_a["id"],))["status"] == "kept"
    assert one(conn, "SELECT status FROM contact_points WHERE id=?", (phone_b["id"],))["status"] == "untested"


def test_marking_a_person_wrong_rejects_them(conn, settings):
    a = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple")
    b = owner_prop(conn, "MAPLE ROW HOLDINGS LLC", portfolio_key="pk-maple")
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    f = one(conn, "SELECT * FROM research_findings WHERE property_id=? AND kind='person' AND status='kept'", (a,))
    res = R.mark_finding(conn, f["id"], "wrong", actor="team")
    assert res["finding"]["status"] == "wrong" and res["finding"]["marked_by"] == "team" and res["drill"]
    roles = {r["property_id"]: r["role"] for r in rows(conn, "SELECT property_id, role FROM people WHERE "
                                                             "name='Robin Sample'")}
    assert roles == {a: "rejected", b: "rejected"}
    assert "rejected" in E.NOT_OWNER_ROLES
    R.mark_finding(conn, f["id"], "kept", actor="team")
    assert one(conn, "SELECT role FROM people WHERE property_id=? AND name='Robin Sample'", (a,))["role"] == "member"
    blocked = one(conn, "SELECT id FROM research_findings WHERE status='blocked'")
    with pytest.raises(ValueError, match="dropped"):
        R.mark_finding(conn, blocked["id"], "kept")
    with pytest.raises(ValueError, match="wrong or kept"):
        R.mark_finding(conn, f["id"], "verified")


@pytest.mark.parametrize("role", ["previous_owner", "other"])
def test_research_never_changes_the_role_of_someone_it_did_not_add(conn, settings, role):
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    E.run_auto_stages(conn, MARKET)
    E.add_person(conn, pid, "Robin Sample", role=role, source="you")

    def guess() -> str | None:
        return E.best_guess_person(E.load_contexts(conn, property_ids=[pid])[0])

    before = guess()
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    person = one(conn, "SELECT role, source FROM people WHERE property_id=? AND name='Robin Sample'", (pid,))
    assert (person["role"], person["source"]) == (role, "you")
    assert scalar(conn, "SELECT COUNT(*) FROM people WHERE name='Robin Sample'") == 1 and guess() == before
    f = one(conn, "SELECT * FROM research_findings WHERE kind='person' AND status='kept'")
    R.mark_finding(conn, f["id"], "wrong", actor="team")
    assert one(conn, "SELECT role FROM people WHERE property_id=? AND name='Robin Sample'", (pid,))["role"] == role
    assert guess() == before
    # A person research added itself still gets the better role.
    pid2 = owner_prop(conn, "OAK LANE PROPERTIES LLC")
    E._add_person(conn, pid2, "Casey Example", role="other", source="ai_research")
    E._add_person(conn, pid2, "Casey Example", role="member", source="ai_research", upgrade_role=False)
    assert one(conn, "SELECT role FROM people WHERE property_id=?", (pid2,))["role"] == "member"


def test_findings_route_records_who(tmp_root, settings, conn):
    from fastapi.testclient import TestClient

    from bluemoon.web.app import app
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    f = one(conn, "SELECT id FROM research_findings WHERE property_id=? AND kind='website'", (pid,))
    with TestClient(app) as c:
        r = c.patch(f"/api/research/findings/{f['id']}", json={"status": "wrong"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["finding"]["status"] == "wrong" and body["finding"]["marked_by"]
        assert body["drill"]["research"]["findings"]
        detail = c.get(f"/api/m/{MARKET}/properties/{pid}").json()
        assert any(x["kind"] == "website" for x in detail["drill"]["research"]["findings"])
        assert c.patch("/api/research/findings/999999", json={"status": "wrong"}).status_code == 404
        cid = contacts_of(conn, pid)[0]["id"]
        assert c.patch(f"/api/contacts/{cid}", json={"status": "wrong"}).status_code == 200
    assert one(conn, "SELECT marked_by FROM research_findings WHERE contact_point_id=?", (cid,))["marked_by"]


def test_outreach_route_records_who_marked_an_ai_contact(tmp_root, settings, conn):
    from fastapi.testclient import TestClient

    from bluemoon.web.app import app
    pid = owner_prop(conn, "MAPLE ROW HOLDINGS LLC")
    R.research_owners(conn, MARKET, backend=Scripted({"MAPLE ROW HOLDINGS LLC": answer()}))
    cid = contacts_of(conn, pid)[0]["id"]
    with TestClient(app) as c:
        r = c.post(f"/api/m/{MARKET}/properties/{pid}/outreach",
                   json={"channel": "phone", "outcome": "wrong_person", "contact_point_id": cid})
        assert r.status_code == 200, r.text
    f = one(conn, "SELECT status, marked_by FROM research_findings WHERE contact_point_id=?", (cid,))
    assert f["status"] == "wrong" and f["marked_by"]


# --- pipeline, CLI and jobs ----------------------------------------------------------------------

def test_pipeline_step_and_its_gate(conn, settings, tmp_root):
    assert pipeline.normalize_step("ai_research") == pipeline.normalize_step("web_research") == "research"
    assert "research" in pipeline.STEPS and "research" in pipeline.PAID_STEPS
    assert "research" not in {k for k, _ in pipeline.ALL_PLAN}
    assert pipeline.step_label("research") == "AI research"
    set_research(tmp_root, backend="mock")
    for i in range(6):
        owner_prop(conn, f"PIPELINE OWNER {i} LLC")
    dry = pipeline.run_step(conn, MARKET, "research", limit=5, dry_run=True)
    assert dry["ok"] and dry["dry_run"] and dry["counts"]["owners"] == 5
    res = pipeline.run_step(conn, MARKET, "research", limit=5)
    assert res["ok"] and res["counts"]["researched"] == 5 and res["counts"]["calls"] == 5
    line = next(s for s in pipeline.estimate(conn, MARKET)["steps"] if s["step"] == "research")
    assert line["to_do"] == 1 and line["owners"] == 1 and line["usd"] == 0


def test_cli_research_command_exists():
    import run
    assert "research" in run.STEP_HELP
    args = run.build_parser().parse_args(["research", "--limit", "5", "--dry-run"])
    assert args.command == "research" and args.limit == 5 and args.dry_run


def test_research_jobs_have_their_own_lane(conn):
    started = {"research": threading.Event(), "score": threading.Event()}
    release = threading.Event()

    def body(c, kind, market, params, progress):
        started[kind].set()
        while not release.wait(0.01):
            progress(0.5, "Working")
        return {"ok": True, "message": f"{kind} done"}

    runner = jobs.JobRunner(body)
    research = runner.start("research", MARKET)
    score = runner.start("score", MARKET)          # same market: it must not wait for the research
    assert started["research"].wait(5) and started["score"].wait(5)
    second = runner.start("research", OTHER)       # but research jobs run one at a time on this computer
    time.sleep(0.05)
    assert runner.get(second)["status"] == "queued" and "AI research" in runner.get(second)["message"]
    release.set()
    for job_id in (research, score, second):
        assert runner.wait(job_id, 5)["status"] == "done"
