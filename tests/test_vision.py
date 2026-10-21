"""The AI photo check (bluemoon/imagery/vision.py).

No real Claude calls: Claude Code is replaced by a fake subprocess runner, the Anthropic API by a fake
client. The one live test at the bottom runs only with BLUEMOON_LIVE=1 and `claude` signed in.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import zlib
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
import yaml

from bluemoon.cache import month_usage
from bluemoon.config import reload_settings
from bluemoon.db import market_by_slug, now_iso, one, scalar
from bluemoon.imagery import vision
from bluemoon.imagery.vision import (
    AnthropicAPIBackend,
    AnthropicAPIConfig,
    ClaudeCLIBackend,
    ClaudeCLIConfig,
    MockBackend,
    VisionBackend,
    VisionCallError,
    VisionFatalError,
    VisionParseError,
    VisualResult,
    ai_final_p,
    build_prompt,
    classify_properties,
    combine,
    explain_prior,
    extract_json_object,
    final_p_for,
    get_backend,
    parse_cli_output,
    parse_prior_rule,
    parse_result,
    result_schema,
    rules_prior,
)

MARKET = "apartments-springfield-oh"
LABELS = ["EXTERIOR_CORRIDOR", "DIRECT_ENTRY", "INTERIOR_CORRIDOR", "MIXED", "UNKNOWN"]
_ids = count(1)


# --- helpers ---------------------------------------------------------------------------------

def tiny_png(rgb=(200, 40, 40), size: int = 8) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def fake_jpeg(seed: int) -> bytes:
    return b"\xff\xd8\xff\xe0" + seed.to_bytes(4, "big") * 16


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


def add_property(conn, settings, market: str = MARKET, *, photos: bool = True, status: str = "done",
                 rank: int | None = None, **fields) -> int:
    m = market_by_slug(conn, market)
    n = next(_ids)
    data = {"market_id": m["id"], "property_key": f"vis:{n}", "address": f"{n} PHOTO RD", "city": "SPRINGFIELD",
            "state": "OH", "lat": 39.95, "lng": -83.77, "created_at": now_iso(), "updated_at": now_iso(), **fields}
    cur = conn.execute(f"INSERT INTO properties({','.join(data)}) VALUES({','.join('?' for _ in data)})",
                       tuple(data.values()))
    pid = cur.lastrowid
    conn.execute("INSERT INTO scores(property_id, in_buybox, rank, computed_at) VALUES(?,?,?,?)",
                 (pid, 1, rank, now_iso()))
    conn.execute("INSERT INTO visual_checks(property_id, status) VALUES(?,?)", (pid, status))
    conn.commit()
    if photos:
        folder = settings.path("images") / str(pid)
        folder.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(("sv_0.jpg", "sv_1.jpg", "sv_2.jpg", "sat.jpg")):
            (folder / name).write_bytes(fake_jpeg(pid * 10 + i))
    return pid


def answer(label="EXTERIOR_CORRIDOR", p=90, quality="good", visible=True, **extra) -> str:
    return json.dumps({"label": label, "p_target": p, "image_quality": quality, "visible_cues": ["stairs"],
                       "reasons": "Open breezeways.", "property_visible": visible, **extra})


class Scripted(VisionBackend):
    """Replies from a list (strings or exceptions); remembers what it was asked."""
    name = "scripted"
    provider = "test"
    sku = "vision_test"

    def __init__(self, replies):
        self.replies = list(replies)
        self.asked: list[tuple[list[Path], str, dict | None]] = []

    @property
    def model(self) -> str:
        return "script-1"

    def classify(self, image_paths, prompt, *, schema=None):
        self.asked.append((list(image_paths), prompt, schema))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def visual(conn, pid: int) -> dict:
    return one(conn, "SELECT * FROM visual_checks WHERE property_id=?", (pid,)) or {}


# --- parsing the reply -----------------------------------------------------------------------

def test_parse_plain_json():
    r = parse_result(answer(), LABELS)
    assert r.label == "EXTERIOR_CORRIDOR" and r.p_target == 90 and r.image_quality == "good"
    assert r.visible_cues == ["stairs"] and r.property_visible is True


@pytest.mark.parametrize("text", [
    "```json\n" + answer() + "\n```",
    "Here is my answer:\n" + answer() + "\nThanks!",
    '{"note": 1} then ' + answer(),
])
def test_parse_tolerates_wrapping(text):
    assert parse_result(text, LABELS).label == "EXTERIOR_CORRIDOR"


def test_parse_normalizes_values():
    text = json.dumps({"label": "direct entry", "p_exterior": "0.85", "image_quality": "Excellent",
                       "visible_cues": "doors at grade", "reasons": ["a", "b"], "property_visible": "yes"})
    r = parse_result(text, LABELS)
    assert r.label == "DIRECT_ENTRY" and r.p_target == 85.0 and r.image_quality == "good"
    assert r.visible_cues == ["doors at grade"] and r.reasons == "a; b"
    assert parse_result(answer(p="72%"), LABELS).p_target == 72.0
    assert parse_result(answer(p=1), LABELS).p_target == 1.0  # whole numbers are percents
    assert parse_result(answer(visible="no"), LABELS).property_visible is False


@pytest.mark.parametrize("text", [
    "no json here",
    answer(label="GARAGE"),
    answer(p=150),
    answer(p=-3),
    answer(quality="sparkly"),
    json.dumps({"label": "MIXED", "image_quality": "good"}),
    answer(visible="maybe"),
])
def test_parse_rejects_bad_replies(text):
    with pytest.raises(VisionParseError):
        parse_result(text, LABELS)


def test_extract_prefers_object_with_label():
    assert extract_json_object('{"a": {"b": 1}} and {"label": "MIXED"}')["label"] == "MIXED"


def test_schema_lists_exact_labels():
    s = result_schema(LABELS)
    assert s["properties"]["label"]["enum"] == LABELS
    assert set(s["required"]) == {"label", "p_target", "image_quality", "visible_cues", "reasons", "property_visible"}
    assert s["additionalProperties"] is False


# --- prompt ----------------------------------------------------------------------------------

def test_prompt_has_labels_targets_and_reply_format(settings):
    p = build_prompt("apartments", [])
    for lab in LABELS:
        assert f"- {lab}" in p
    assert "- EXTERIOR_CORRIDOR (target):" in p and "- INTERIOR_CORRIDOR:" in p
    assert "EXTERIOR_CORRIDOR or DIRECT_ENTRY" in p
    assert "Image 1: Street View photo aimed straight at the property." in p
    assert "Image 4: satellite photo" in p and "red pin" in p
    assert "turned 35 degrees to the left" in p and "turned 35 degrees to the right" in p
    assert '"p_target"' in p and "ONLY this JSON object" in p


def test_prompt_numbers_examples_before_the_property(settings, tmp_path):
    exs = [{"label": "DIRECT_ENTRY", "note": "row of front doors", "images": [tmp_path / "a", tmp_path / "b"]},
           {"label": "INTERIOR_CORRIDOR", "note": None, "images": [tmp_path / "c"]}]
    p = build_prompt("apartments", exs, ["sv_0.jpg", "sat.jpg"])
    assert '- Images 1-2: example 1, true label DIRECT_ENTRY. Why: "row of front doors"' in p
    assert "- Image 3: example 2, true label INTERIOR_CORRIDOR." in p
    assert "- Image 4: Street View photo aimed straight" in p and "- Image 5: satellite photo" in p
    assert str(tmp_path) not in p  # the prompt (and its cache key) never holds file paths


def test_prompt_for_other_asset_classes(settings):
    p = build_prompt("warehouse", [])
    assert "DOCK_HIGH (target)" in p and "BOTH (target)" in p and "DOCK_HIGH or BOTH" in p


# --- rules prior and final verdict -----------------------------------------------------------

def test_prior_rule_parser():
    (c,) = parse_prior_rule("stories_4_plus")
    assert (c.field, c.lo, c.hi) == ("stories", 4.0, None)
    a, b = parse_prior_rule("stories_3_less_built_1970_1995")
    assert (a.field, a.hi, b.field, b.lo, b.hi) == ("stories", 3.0, "year_built", 1970.0, 1995.0)
    (u,) = parse_prior_rule("units_12_or_less")
    assert (u.field, u.hi) == ("units", 12.0)
    (y,) = parse_prior_rule("built_before_1960")
    assert y.hi == 1959
    (acres,) = parse_prior_rule("lot_acres_2p5_plus")
    assert acres.lo == 2.5
    assert parse_prior_rule("vibes_good") is None


@pytest.mark.parametrize("prop,expected", [
    ({"stories": 5, "year_built": 1985, "units": 60}, -15),
    ({"stories": 2, "year_built": 1980, "units": 40}, 15),
    ({"stories": 2, "year_built": 1985, "units": 10}, 25),
    ({"stories": 3, "year_built": 2005, "units": 12}, 10),
    ({}, 0),                                            # missing data never counts
    ({"stories": None, "year_built": 1980, "units": None}, 0),
])
def test_apartment_prior(settings, prop, expected):
    assert rules_prior(prop, "apartments") == expected


def test_explain_prior_reads_like_english(settings):
    rows_ = explain_prior({"stories": 2, "year_built": 1980}, "apartments")
    texts = {r["rule"]: r for r in rows_}
    assert texts["stories_4_plus"]["applies"] is False and texts["stories_4_plus"]["text"] == "4+ stories: -15"
    assert texts["stories_3_less_built_1970_1995"]["text"] == "3 or fewer stories and built 1970-1995: +15"
    assert texts["units_12_or_less"]["applies"] is None
    assert explain_prior({"stories": 2}, "warehouse") == []


def test_unreadable_rule_is_skipped(tmp_root, settings):
    path = tmp_root / "config" / "asset_classes.yaml"
    data = yaml.safe_load(path.read_text())
    data["asset_classes"]["apartments"]["visual_check"]["prior"]["moon_phase_full"] = 50
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    reload_settings()
    assert rules_prior({"stories": 5}, "apartments") == -15
    assert any("isn't readable" in r["text"] for r in explain_prior({}, "apartments"))


def test_final_p_blend_and_no_verdict_cases():
    r = parse_result(answer(p=90), LABELS)
    assert combine(90, 15, 0.8) == 85.0
    assert final_p_for(r, 15, 0.8) == 85.0
    assert final_p_for(r, 0, 1.0) == 90.0
    assert combine(100, 60, 0.0) == 100.0 and combine(0, -80, 0.0) == 0.0  # clamped
    assert final_p_for(parse_result(answer(quality="poor"), LABELS), 0, 0.8) is None
    assert final_p_for(parse_result(answer(quality="none"), LABELS), 0, 0.8) is None
    assert final_p_for(parse_result(answer(visible=False), LABELS), 0, 0.8) is None
    assert final_p_for(parse_result(answer(label="UNKNOWN", p=50), LABELS), 0, 0.8) is None
    assert final_p_for(parse_result(answer(quality="partial"), LABELS), 0, 0.8) == 82.0


def test_ai_final_p_from_row(settings):
    row = {"ai_label": "DIRECT_ENTRY", "ai_p": 90, "prior": 15, "image_quality": "good", "property_visible": 1}
    assert ai_final_p(row) == 85.0
    assert ai_final_p({**row, "property_visible": 0}) is None
    assert ai_final_p({**row, "ai_label": None}) is None


# --- mock backend ----------------------------------------------------------------------------

def test_mock_is_deterministic_and_valid(settings, tmp_path):
    imgs = []
    for i in range(4):
        p = tmp_path / f"{i}.jpg"
        p.write_bytes(fake_jpeg(i))
        imgs.append(p)
    prompt = build_prompt("apartments", [])
    mock = MockBackend()
    a = mock.classify(imgs, prompt, schema=result_schema(LABELS))
    assert a == mock.classify(imgs, prompt)
    r = parse_result(a, LABELS)
    if r.label in ("EXTERIOR_CORRIDOR", "DIRECT_ENTRY"):
        assert r.p_target >= 50
    seen = {parse_result(mock.classify([p], prompt), LABELS).label for p in imgs}
    assert seen <= set(LABELS)


def test_get_backend(settings):
    assert isinstance(get_backend(), ClaudeCLIBackend)
    assert isinstance(get_backend("mock"), MockBackend)
    assert isinstance(get_backend("anthropic_api"), AnthropicAPIBackend)
    with pytest.raises(ValueError, match="Unknown photo-check backend"):
        get_backend("gpt")


# --- Claude Code backend ---------------------------------------------------------------------

class FakeRunner:
    """Stands in for subprocess.run. Each reply: (stdout, stderr, returncode) or an exception."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        out, err, code = reply
        return subprocess.CompletedProcess(cmd, code, out, err)


def cli_result(result: str = "", *, is_error=False, subtype="success", structured=None) -> str:
    data = {"type": "result", "subtype": subtype, "is_error": is_error, "result": result, "session_id": "s1",
            "total_cost_usd": 0.01, "num_turns": 3}
    if structured is not None:
        data["structured_output"] = structured
    return json.dumps(data)


def cli_backend(runner, *, sleeps=None, cfg=None, which=lambda c: "/usr/local/bin/claude"):
    clock = {"t": 0.0}

    def sleep(s):
        (sleeps if sleeps is not None else []).append(s)
        clock["t"] += s

    return ClaudeCLIBackend(cfg or ClaudeCLIConfig(), runner=runner, sleep=sleep, which=which,
                            clock=lambda: clock["t"])


@pytest.fixture
def photos(tmp_path) -> list[Path]:
    d = tmp_path / "images" / "12"
    d.mkdir(parents=True)
    out = []
    for i, name in enumerate(("sv_0.jpg", "sat.jpg")):
        (d / name).write_bytes(fake_jpeg(i))
        out.append(d / name)
    return out


def test_cli_command_flags_and_environment(settings, photos, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-pass")
    monkeypatch.setenv("PATH", "/usr/bin")
    runner = FakeRunner((cli_result(answer()), "", 0))
    be = cli_backend(runner)
    ex = tmp_path / "labels" / "examples" / "3"
    ex.mkdir(parents=True)
    (ex / "sv_0.jpg").write_bytes(fake_jpeg(9))
    text = be.classify([ex / "sv_0.jpg", *photos], "PROMPT", schema=result_schema(LABELS))
    assert parse_result(text, LABELS).label == "EXTERIOR_CORRIDOR"
    call = runner.calls[0]
    cmd = call["cmd"]
    assert cmd[0] == "/usr/local/bin/claude" and cmd[1] == "-p"
    assert cmd[2].startswith("PROMPT") and f"Image 1: {(ex / 'sv_0.jpg').resolve()}" in cmd[2]
    assert f"Image 3: {photos[1].resolve()}" in cmd[2] and "Read tool" in cmd[2]
    i = cmd.index
    assert cmd[i("--output-format") + 1] == "json" and cmd[i("--model") + 1] == "sonnet"
    assert cmd[i("--allowedTools") + 1] == "Read" and cmd[i("--tools") + 1] == "Read"
    assert json.loads(cmd[i("--json-schema") + 1])["properties"]["label"]["enum"] == LABELS
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    add = cmd[i("--add-dir") + 1:]
    assert add == sorted({str(ex.resolve()), str(photos[0].parent.resolve())})
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["env"]["PATH"].split(os.pathsep)[0] == "/usr/local/bin"
    assert call["timeout"] == 240 and call["stdin"] == subprocess.DEVNULL
    assert "blue-moon-deal-finder-ohio" not in call["cwd"]  # neutral folder: no project CLAUDE.md


def test_cli_prefers_structured_output(photos):
    runner = FakeRunner((cli_result("ignored text", structured=json.loads(answer(label="MIXED", p=55))), "", 0))
    assert parse_result(cli_backend(runner).classify(photos, "P"), LABELS).label == "MIXED"


def test_cli_drops_flags_an_older_version_lacks(photos):
    runner = FakeRunner(("", "error: unknown option '--json-schema'", 1), (cli_result(answer()), "", 0))
    be = cli_backend(runner)
    be.classify(photos, "P", schema=result_schema(LABELS))
    assert "--json-schema" in runner.calls[0]["cmd"] and "--json-schema" not in runner.calls[1]["cmd"]
    assert "--json-schema" in be.dropped_flags
    be.classify(photos, "P", schema=result_schema(LABELS))
    assert "--json-schema" not in runner.calls[2]["cmd"]


def test_cli_lean_flags_can_be_turned_off(photos):
    runner = FakeRunner((cli_result(answer()), "", 0))
    cli_backend(runner, cfg=ClaudeCLIConfig(lean=False, json_schema=False, max_turns=12)).classify(photos, "P")
    cmd = runner.calls[0]["cmd"]
    assert "--tools" not in cmd and "--json-schema" not in cmd and cmd[cmd.index("--max-turns") + 1] == "12"


@pytest.mark.parametrize("message,needle", [
    ("Claude AI usage limit reached|1760000000", "usage limit"),
    ("You've hit your limit · resets 3pm", "usage limit"),
    ("Invalid API key · Please run /login", "isn't signed in"),
    ("Not logged in · Please run /login", "isn't signed in"),
    ("There's an issue with the selected model (sonet). It may not exist or you may not have access to it. "
     "model not found", "doesn't accept the model"),
])
def test_cli_fatal_errors_stop_the_run(photos, message, needle):
    runner = FakeRunner((cli_result(message, is_error=True), "", 1))
    with pytest.raises(VisionFatalError) as err:
        cli_backend(runner).classify(photos, "P")
    assert needle in err.value.message


def test_cli_rate_limit_waits_once_then_stops(photos):
    sleeps: list[float] = []
    runner = FakeRunner((cli_result("API Error: Rate limit reached", is_error=True), "", 0))
    with pytest.raises(VisionFatalError, match="rate limit"):
        cli_backend(runner, sleeps=sleeps).classify(photos, "P")
    assert len(runner.calls) == 2 and 60 in sleeps


def test_cli_usage_limit_reported_as_a_normal_reply(photos):
    runner = FakeRunner((cli_result("Claude AI usage limit reached|1760000000"), "", 0))
    with pytest.raises(VisionFatalError, match="usage limit"):
        cli_backend(runner).classify(photos, "P")


def test_cli_rate_limit_recovers(photos):
    runner = FakeRunner((cli_result("API Error: Rate limit reached", is_error=True), "", 0),
                        (cli_result(answer()), "", 0))
    assert parse_result(cli_backend(runner).classify(photos, "P"), LABELS).label == "EXTERIOR_CORRIDOR"


def test_cli_other_errors_skip_one_property(photos):
    runner = FakeRunner((cli_result("", is_error=True, subtype="error_max_turns"), "", 1))
    with pytest.raises(VisionCallError):
        cli_backend(runner).classify(photos, "P")
    runner = FakeRunner(("", "Segmentation fault", 139))
    with pytest.raises(VisionCallError, match="Segmentation fault"):
        cli_backend(runner).classify(photos, "P")


def test_cli_timeout_is_a_property_error(photos):
    runner = FakeRunner(subprocess.TimeoutExpired(["claude"], 240))
    with pytest.raises(VisionCallError, match="longer than 240 seconds"):
        cli_backend(runner).classify(photos, "P")


def test_cli_not_installed(photos, monkeypatch):
    monkeypatch.setattr(vision, "CLAUDE_FALLBACK_PATHS", ())
    be = cli_backend(FakeRunner(FileNotFoundError()), which=lambda c: None)
    with pytest.raises(VisionFatalError, match="isn't installed"):
        be.check()
    with pytest.raises(VisionFatalError):
        be.classify(photos, "P")


def test_cli_pauses_between_calls(photos):
    sleeps: list[float] = []
    runner = FakeRunner((cli_result(answer()), "", 0))
    be = cli_backend(runner, sleeps=sleeps, cfg=ClaudeCLIConfig(pause_seconds=2))
    be.classify(photos, "P")
    be.classify(photos, "P")
    assert sleeps == [2]


def test_cli_missing_photo(tmp_path):
    with pytest.raises(VisionCallError, match="missing"):
        cli_backend(FakeRunner((cli_result(answer()), "", 0))).classify([tmp_path / "nope.jpg"], "P")


def test_parse_cli_output_shapes():
    assert parse_cli_output(cli_result("x")).result == "x"
    stream = "\n".join([json.dumps({"type": "system"}), "garbage", cli_result("y")])
    assert parse_cli_output(stream).result == "y"
    arr = json.dumps([{"type": "system"}, {"type": "assistant"}, json.loads(cli_result("z"))])
    assert parse_cli_output(arr).result == "z"
    assert parse_cli_output("") is None and parse_cli_output("not json") is None


# --- Anthropic API backend -------------------------------------------------------------------

class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def api_message(text: str, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


def fake_client(*replies):
    msgs = FakeMessages(replies)
    return SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs)), msgs


def _resp(code: int):
    return httpx2.Response(code, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_api_sends_images_schema_and_fallback(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(tiny_png())
    jpg = tmp_path / "b.jpg"
    jpg.write_bytes(fake_jpeg(1))
    client, msgs = fake_client(api_message(answer()))
    be = AnthropicAPIBackend(AnthropicAPIConfig(), client=client)
    text = be.classify([png, jpg], "PROMPT", schema=result_schema(LABELS))
    assert parse_result(text, LABELS).p_target == 90
    kw = msgs.kwargs[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    content = kw["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Image 1:"}
    assert content[1]["source"]["media_type"] == "image/png" and content[3]["source"]["media_type"] == "image/jpeg"
    assert content[-1] == {"type": "text", "text": "PROMPT"}
    assert kw["output_config"]["format"]["schema"]["properties"]["label"]["enum"] == LABELS
    assert kw["output_config"]["effort"] == "low"


def test_api_retries_with_more_room_on_max_tokens(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(fake_jpeg(2))
    client, msgs = fake_client(api_message("{", stop="max_tokens"), api_message(answer()))
    AnthropicAPIBackend(AnthropicAPIConfig(max_tokens=800), client=client).classify([p], "P")
    assert [k["max_tokens"] for k in msgs.kwargs] == [800, 3200]


def test_api_refusal_and_errors(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(fake_jpeg(3))
    client, _ = fake_client(api_message("", stop="refusal"))
    with pytest.raises(VisionCallError, match="declined"):
        AnthropicAPIBackend(client=client).classify([p], "P")
    cases = [
        (anthropic.AuthenticationError("bad", response=_resp(401), body=None), VisionFatalError, "isn't valid"),
        (anthropic.NotFoundError("no model", response=_resp(404), body=None), VisionFatalError, "doesn't know"),
        (anthropic.RateLimitError("slow", response=_resp(429), body=None), VisionFatalError, "rate limit"),
        (anthropic.BadRequestError("Your credit balance is too low", response=_resp(400), body=None),
         VisionFatalError, "out of credit"),
        (anthropic.InternalServerError("oops", response=_resp(500), body=None), VisionCallError, "HTTP 500"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), VisionCallError, "network"),
    ]
    for exc, kind, needle in cases:
        client, _ = fake_client(exc)
        with pytest.raises(kind, match=needle):
            AnthropicAPIBackend(AnthropicAPIConfig(refusal_fallback=False), client=client).classify([p], "P")


def test_api_turns_fallback_off_when_rejected(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(fake_jpeg(4))
    client, msgs = fake_client(
        anthropic.BadRequestError("fallbacks: not supported for this model", response=_resp(400), body=None),
        api_message(answer()))
    AnthropicAPIBackend(client=client).classify([p], "P")
    assert "fallbacks" in msgs.kwargs[0] and "fallbacks" not in msgs.kwargs[1]


def test_api_needs_a_key(settings):
    with pytest.raises(VisionFatalError, match="ANTHROPIC_API_KEY"):
        AnthropicAPIBackend().check()


# --- the run ---------------------------------------------------------------------------------

def test_classify_with_mock_writes_verdicts(conn, settings):
    a = add_property(conn, settings, stories=2, year_built=1980, units=24, rank=1)
    b = add_property(conn, settings, rank=2)
    add_property(conn, settings, photos=False)  # no photos: nothing to ask
    res = classify_properties(conn, MARKET, backend="mock")
    assert res["classified"] == 2 and res["calls"] == 2 and res["error"] is None
    assert res["colors"]["green"] + res["colors"]["yellow"] + res["colors"]["red"] + res["colors"]["na"] == 2
    row = visual(conn, a)
    assert row["ai_label"] in LABELS and row["checked_at"] and row["model"] == "mock:mock-1"
    assert row["prior"] == 15
    expected = final_p_for(VisualResult(label=row["ai_label"], p_target=row["ai_p"], image_quality=row["image_quality"],
                                        property_visible=bool(row["property_visible"])), 15, 0.8)
    assert row["final_p"] == expected
    assert json.loads(row["cues_json"])
    assert visual(conn, b)["ai_label"]
    again = classify_properties(conn, MARKET, backend="mock")
    assert again["properties"] == 0 and again["calls"] == 0
    redo = classify_properties(conn, MARKET, backend="mock", reclassify=True)
    assert redo["classified"] == 2 and redo["calls"] == 0 and redo["cached"] == 2  # cached answers reused


def test_dry_run_counts_without_asking(conn, settings):
    add_property(conn, settings)
    be = Scripted([answer()])
    est = classify_properties(conn, MARKET, dry_run=True, backend=be)
    assert est["dry_run"] and est["calls"] == 1 and est["cached"] == 0 and be.asked == []
    assert vision.classify_estimate(conn, MARKET, backend=be)["calls"] == 1


def test_claude_cli_estimate_says_plan_and_readiness(conn, settings, monkeypatch):
    monkeypatch.setattr(vision, "CLAUDE_FALLBACK_PATHS", ())
    add_property(conn, settings)
    be = ClaudeCLIBackend(ClaudeCLIConfig(command="definitely-not-claude"))
    est = classify_properties(conn, MARKET, dry_run=True, backend=be)
    assert "Claude plan" in est["message"] and est["est_cost_usd"] == 0 and est["ready"] is False
    res = classify_properties(conn, MARKET, backend=be)
    assert res["error"] and "isn't installed" in res["error"]


def test_api_estimate_has_dollars(conn, settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    for _ in range(3):
        add_property(conn, settings)
    est = classify_properties(conn, MARKET, dry_run=True, backend="anthropic_api")
    assert est["est_cost_usd"] == pytest.approx(0.09) and est["ready"] is True


def test_retry_once_when_reply_unreadable(conn, settings):
    pid = add_property(conn, settings)
    be = Scripted(["I think it's a walk-up!", answer(label="DIRECT_ENTRY", p=80)])
    res = classify_properties(conn, MARKET, backend=be)
    assert res["classified"] == 1 and res["calls"] == 2
    assert "could not be read" in be.asked[1][1]
    assert visual(conn, pid)["ai_label"] == "DIRECT_ENTRY"
    assert month_usage(conn, "vision_test") == 2


def test_two_unreadable_replies_skip_the_property(conn, settings):
    bad = add_property(conn, settings, rank=1)
    good = add_property(conn, settings, rank=2)
    be = Scripted(["nope", "still nope", answer()])
    res = classify_properties(conn, MARKET, backend=be)
    assert res["errors"] == 1 and res["classified"] == 1
    assert visual(conn, bad)["ai_label"] is None and "couldn't be read" in visual(conn, bad)["error"]
    assert visual(conn, good)["ai_label"] == "EXTERIOR_CORRIDOR"
    assert month_usage(conn, "vision_test") == 3
    assert classify_properties(conn, MARKET, backend=Scripted([answer()]))["classified"] == 1  # retried next run


def test_fatal_error_stops_and_leaves_the_rest_pending(conn, settings):
    first = add_property(conn, settings, rank=1)
    second = add_property(conn, settings, rank=2)
    third = add_property(conn, settings, rank=3)
    be = Scripted([answer(), VisionFatalError("Your Claude plan's usage limit was reached."), answer()])
    res = classify_properties(conn, MARKET, backend=be)
    assert res["classified"] == 1 and res["stopped"] is True and "usage limit" in res["error"]
    assert res["pending"] == 2 and "still pending" in res["message"]
    assert visual(conn, first)["ai_label"] and not visual(conn, second)["ai_label"]
    assert not visual(conn, third)["ai_label"]


def test_many_failures_in_a_row_stop_the_run(conn, settings):
    for i in range(5):
        add_property(conn, settings, rank=i + 1)
    be = Scripted([VisionCallError("timeout")])
    res = classify_properties(conn, MARKET, backend=be)
    assert res["errors"] == 3 and res["stopped"] and "in a row" in res["error"]


def test_call_cap(tmp_root, conn, settings):
    set_config(tmp_root, "vision.max_calls_per_run", 2)
    for _ in range(3):
        add_property(conn, settings)
    res = classify_properties(conn, MARKET, backend="mock")
    assert res["classified"] == 2 and res["capped"] and "cap" in res["message"]


def test_confirm_needed_for_big_runs(tmp_root, conn, settings):
    set_config(tmp_root, "safety.confirm_over_properties", 1)
    for _ in range(2):
        add_property(conn, settings)
    be = Scripted([answer()])
    res = classify_properties(conn, MARKET, backend=be)
    assert res["needs_confirm"] and be.asked == [] and "Waiting for your OK" in res["message"]
    assert classify_properties(conn, MARKET, backend=be, confirm=True)["classified"] == 2
    for _ in range(2):
        add_property(conn, settings)
    assert classify_properties(conn, MARKET, backend="mock")["classified"] == 2  # mock never needs an OK


def test_limit_and_order(conn, settings):
    low = add_property(conn, settings, rank=5)
    top = add_property(conn, settings, rank=1)
    be = Scripted([answer()])
    classify_properties(conn, MARKET, backend=be, limit=1)
    assert visual(conn, top)["ai_label"] and not visual(conn, low)["ai_label"]


def test_reviewed_properties_are_left_alone(conn, settings):
    pid = add_property(conn, settings)
    conn.execute("UPDATE visual_checks SET reviewed_label='INTERIOR_CORRIDOR', final_p=0 WHERE property_id=?", (pid,))
    conn.commit()
    res = classify_properties(conn, MARKET, backend="mock")
    assert res["properties"] == 0 and visual(conn, pid)["ai_label"] is None


def test_calibrate_mode_keeps_your_answer_and_hides_its_own_example(conn, settings):
    from bluemoon.imagery.review import save_review
    pid = add_property(conn, settings)
    other = add_property(conn, settings)
    save_review(conn, pid, "DIRECT_ENTRY", "doors at grade")
    save_review(conn, other, "INTERIOR_CORRIDOR")
    be = Scripted([answer(label="EXTERIOR_CORRIDOR", p=70)])
    res = classify_properties(conn, MARKET, backend=be, reviewed_only=True)
    assert res["classified"] == 2
    row = visual(conn, pid)
    assert row["reviewed_label"] == "DIRECT_ENTRY" and row["final_p"] == 100 and row["ai_label"] == "EXTERIOR_CORRIDOR"
    for images, _prompt, _ in be.asked:
        own = [p for p in images if f"/examples/{pid}/" in str(p)]
        mine = [p for p in images if f"/images/{pid}/" in str(p)]
        assert not (own and mine), "a property must never see its own labeled example"
    assert any("true label" in prompt for _, prompt, _ in be.asked)


def test_examples_go_first_and_change_the_cache_key(conn, settings):
    from bluemoon.imagery.review import save_review
    ex = add_property(conn, settings, rank=9)
    save_review(conn, ex, "DIRECT_ENTRY", "front doors at grade")
    target = add_property(conn, settings, rank=1)
    be = Scripted([answer()])
    classify_properties(conn, MARKET, backend=be)
    images, prompt, schema = be.asked[0]
    assert len(images) == 2 + 4  # vision.images_per_example from the example, then the 4 photos
    assert f"/examples/{ex}/" in str(images[0]) and f"/images/{target}/" in str(images[-1])
    assert '- Images 1-2: example 1, true label DIRECT_ENTRY. Why: "front doors at grade"' in prompt
    assert schema["properties"]["label"]["enum"] == LABELS


def test_non_visual_market_is_skipped(conn, settings):
    res = classify_properties(conn, "land-springfield-oh", backend="mock")
    assert res["enabled"] is False and "no photo check" in res["message"]


def test_progress_reports(conn, settings):
    add_property(conn, settings)
    seen: list[tuple[float, str]] = []
    classify_properties(conn, MARKET, backend="mock", progress=lambda f, m: seen.append((f, m)))
    assert seen[-1][0] == 1.0 and "Photo check" in seen[-1][1]


def test_claude_cli_backend_through_the_run(conn, settings, monkeypatch):
    """The real ClaudeCLIBackend class end to end, with the subprocess faked."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    pid = add_property(conn, settings)
    runner = FakeRunner((cli_result(answer(label="DIRECT_ENTRY", p=88)), "", 0))
    be = ClaudeCLIBackend(ClaudeCLIConfig(pause_seconds=0), runner=runner, which=lambda c: "/bin/claude")
    res = classify_properties(conn, MARKET, backend=be)
    assert res["classified"] == 1 and visual(conn, pid)["ai_label"] == "DIRECT_ENTRY"
    assert month_usage(conn, "vision_claude_cli") == 1
    assert "ANTHROPIC_API_KEY" not in runner.calls[0]["env"]
    assert scalar(conn, "SELECT COUNT(*) FROM api_cache WHERE provider='vision:claude_cli'") == 1


# --- live smoke test (opt-in) ----------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("BLUEMOON_LIVE") != "1" or not shutil.which("claude"),
                    reason="live Claude Code test: set BLUEMOON_LIVE=1 with `claude` installed and signed in")
def test_live_claude_cli_reads_a_png(tmp_path):  # pragma: no cover - uses your Claude plan
    img = tmp_path / "images" / "1" / "sv_0.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(tiny_png((220, 30, 30), 16))
    labels = ["RED", "GREEN", "BLUE", "UNKNOWN"]
    prompt = ("This is a test of an image pipeline. Image 1 is a small square of one solid color.\n"
              "Labels: RED, GREEN, BLUE, UNKNOWN. Target label: RED.\n"
              'Reply with ONLY this JSON object: {"label": "<label>", "p_target": <0-100 chance it is RED>, '
              '"image_quality": "good|partial|poor|none", "visible_cues": ["..."], "reasons": "...", '
              '"property_visible": true}')
    be = ClaudeCLIBackend(ClaudeCLIConfig(timeout_seconds=300, pause_seconds=0))
    text = be.classify([img], prompt, schema=result_schema(labels))
    print("LIVE CLI REPLY:", text)
    res = parse_result(text, labels)
    assert res.label == "RED"
