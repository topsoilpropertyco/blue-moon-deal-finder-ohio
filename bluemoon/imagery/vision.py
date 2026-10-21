"""The AI photo check: Claude looks at the photos and answers the asset class's question.

Backends (pick with `vision.backend` in config.yaml):
- `claude_cli` (default): Claude Code on this computer, on your Claude plan (`claude -p ...`).
  ANTHROPIC_API_KEY is removed from its environment so it never bills the pay-per-use API.
- `anthropic_api`: the pay-per-use Anthropic API (needs ANTHROPIC_API_KEY in .env).
- `mock`: offline and free; answers are made up from the photo bytes (tests and demos).

Each property gets one call: your labeled examples first (from the review screen), then its
3 Street View photos and 1 satellite photo. Claude replies with one JSON object
{label, p_target, image_quality, visible_cues, reasons, property_visible}; it is validated with
pydantic (one retry when the reply can't be read) and cached per (photos + prompt + backend + model),
so re-runs never ask twice.

The final verdict blends the photo answer with simple rules about the building (the "prior"):
    final_p = weight_vision * p_target + (1 - weight_vision) * (50 + prior)
final_p stays empty (needs a human look) when the photos are poor or empty, the property isn't
visible, or the label is UNKNOWN. Your own review always wins and is never overwritten here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, NoReturn, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..cache import cache_get, cache_put, make_key, record_call
from ..config import AssetClass, Settings, get_settings, secret
from ..db import dumps, market_by_slug, now_iso, rows
from ..enrich import geo
from . import review, streetview

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None] | None

UNKNOWN = "UNKNOWN"
QUALITIES = ("good", "partial", "poor", "none")
NO_VERDICT_QUALITIES = ("poor", "none")
CACHE_PROVIDER = "vision"
API_KEY_NAME = "ANTHROPIC_API_KEY"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_LABELS = ["EXTERIOR_CORRIDOR", "DIRECT_ENTRY", "INTERIOR_CORRIDOR", "MIXED", "UNKNOWN"]
CLAUDE_FALLBACK_PATHS = ("~/.claude/local/claude", "~/.local/bin/claude", "/opt/homebrew/bin/claude",
                         "/usr/local/bin/claude")


# --- settings ---------------------------------------------------------------------------------

class ClaudeCLIConfig(BaseModel):
    """`vision.claude_cli` in config.yaml."""
    model_config = ConfigDict(extra="ignore")
    command: str = "claude"
    model: str = "sonnet"
    timeout_seconds: float = Field(240, gt=0)
    pause_seconds: float = Field(2, ge=0)
    json_schema: bool = True               # --json-schema: Claude Code checks the reply's shape
    lean: bool = True                      # only the Read tool, no MCP servers, no saved session
    max_turns: int | None = Field(None, ge=1)
    rate_limit_wait_seconds: float = Field(60, ge=0)
    workdir: str | None = None             # where `claude` runs (default: a folder in the temp dir)
    strip_env: list[str] = Field(default_factory=lambda: [API_KEY_NAME])


class AnthropicAPIConfig(BaseModel):
    """`vision.anthropic_api` in config.yaml."""
    model_config = ConfigDict(extra="ignore")
    model: str = "claude-sonnet-5-5"
    max_tokens: int = Field(800, ge=64)
    est_cost_per_call_usd: float = Field(0.03, ge=0)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    refusal_fallback: bool = True
    timeout_seconds: float = Field(120, gt=0)
    max_retries: int = Field(2, ge=0)


class VisionConfig(BaseModel):
    """`vision:` in config.yaml. Missing keys use these defaults."""
    model_config = ConfigDict(extra="ignore")
    backend: str = "claude_cli"
    claude_cli: ClaudeCLIConfig = Field(default_factory=ClaudeCLIConfig)
    anthropic_api: AnthropicAPIConfig = Field(default_factory=AnthropicAPIConfig)
    max_calls_per_run: int = Field(400, ge=0)
    examples_per_call: int = Field(6, ge=0)
    images_per_example: int = Field(2, ge=1, le=4)
    weight_vision: float = Field(0.8, ge=0, le=1)
    max_consecutive_errors: int = Field(3, ge=1)
    confirm_over_properties: int = Field(50, ge=0)


def vision_config(settings: Settings | None = None) -> VisionConfig:
    s = settings or get_settings()
    raw = {k: v for k, v in dict(s.section("vision")).items() if v is not None}
    raw["confirm_over_properties"] = s.section("safety").get("confirm_over_properties", 50)
    try:
        return VisionConfig(**raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"config.yaml vision setting '{where}' has a value I can't use: {first.get('msg')}") from None


# --- errors -----------------------------------------------------------------------------------

class VisionError(Exception):
    """A photo-check problem with a plain-English message."""

    def __init__(self, message: str, *, calls: int = 0):
        super().__init__(message)
        self.message = message
        self.calls = calls                 # calls that reached Claude (for the usage meter)


class VisionFatalError(VisionError):
    """Stop the whole run (not signed in, usage limit reached, bad model, no key...)."""


class VisionCallError(VisionError):
    """This property failed; the run goes on and it is tried again next run."""


class VisionParseError(ValueError):
    """Claude's reply wasn't the JSON object we asked for."""


# --- the answer -------------------------------------------------------------------------------

_QUALITY_SYNONYMS = {"excellent": "good", "great": "good", "clear": "good", "fair": "partial", "ok": "partial",
                     "okay": "partial", "limited": "partial", "bad": "poor", "low": "poor", "blurry": "poor",
                     "unusable": "none", "missing": "none", "no": "none", "n/a": "none"}
_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0"}
_P_ALIASES = ("p_target", "p_exterior", "probability", "p", "confidence", "score")


def _to_number(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError("p_target must be a number")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("%").strip()
    return float(s)


class VisualResult(BaseModel):
    """Claude's answer about one property's photos."""
    model_config = ConfigDict(extra="ignore")
    label: str
    p_target: float = Field(ge=0, le=100)
    image_quality: Literal["good", "partial", "poor", "none"]
    visible_cues: list[str] = Field(default_factory=list)
    reasons: str = ""
    property_visible: bool = True

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict) and "p_target" not in data:
            for alias in _P_ALIASES[1:]:
                if alias in data:
                    data = {**data, "p_target": data[alias]}
                    break
        return data

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, v: Any) -> str:
        return re.sub(r"[\s\-]+", "_", str(v or "").strip()).upper()

    @field_validator("p_target", mode="before")
    @classmethod
    def _p(cls, v: Any) -> float:
        p = _to_number(v)
        if 0 < p < 1 and not float(p).is_integer():
            p *= 100.0  # a fraction like 0.85 means 85%
        return round(p, 1)

    @field_validator("image_quality", mode="before")
    @classmethod
    def _quality(cls, v: Any) -> str:
        s = str(v or "").strip().lower()
        return _QUALITY_SYNONYMS.get(s, s)

    @field_validator("visible_cues", mode="before")
    @classmethod
    def _cues(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
        return [str(x).strip() for x in v if str(x).strip()]

    @field_validator("reasons", mode="before")
    @classmethod
    def _reasons(cls, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return "; ".join(str(x).strip() for x in v if str(x).strip())
        return str(v).strip()

    @field_validator("property_visible", mode="before")
    @classmethod
    def _visible(cls, v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError("property_visible must be true or false")


def result_schema(labels: Sequence[str]) -> dict:
    """JSON Schema for the reply (Claude Code --json-schema and API structured outputs)."""
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": list(labels)},
            "p_target": {"type": "number"},
            "image_quality": {"type": "string", "enum": list(QUALITIES)},
            "visible_cues": {"type": "array", "items": {"type": "string"}},
            "reasons": {"type": "string"},
            "property_visible": {"type": "boolean"},
        },
        "required": ["label", "p_target", "image_quality", "visible_cues", "reasons", "property_visible"],
        "additionalProperties": False,
    }


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def extract_json_object(text: str) -> dict:
    """The JSON object in Claude's reply (tolerates code fences or a sentence around it)."""
    t = _strip_fences(text or "")
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    found: list[dict] = []
    for i, ch in enumerate(t):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(t, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            if "label" in obj:
                return obj
            found.append(obj)
    if found:
        return found[0]
    raise VisionParseError("The reply had no JSON object in it.")


def parse_result(text: str, labels: Sequence[str]) -> VisualResult:
    """Validate Claude's reply. Raises VisionParseError when it isn't usable."""
    data = extract_json_object(text)
    try:
        res = VisualResult.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise VisionParseError(f"The reply's '{where}' wasn't usable ({first.get('msg')}).") from None
    allowed = [str(x).upper() for x in labels]
    if res.label not in allowed:
        raise VisionParseError(f"'{res.label}' isn't one of the allowed labels ({', '.join(allowed)}).")
    return res


# --- the prompt -------------------------------------------------------------------------------

REPLY_INSTRUCTIONS = """Reply with ONLY this JSON object and nothing else (no code fences, no other words):
{{"label": "<one label from the list>", "p_target": <number 0-100>, "image_quality": "good|partial|poor|none", "visible_cues": ["<short cue>", "..."], "reasons": "<one or two plain sentences>", "property_visible": true}}
- p_target: your probability, 0 to 100, that the property is {targets}. Keep it consistent with the label: high for a target label, low for the others, near 50 when torn.
- image_quality: good = the property's buildings are clearly visible; partial = partly visible or partly blocked; poor = blurry, far away or mostly blocked; none = no usable photo of the property.
- property_visible: false when you can't tell which building is the property, or it is hidden.{unknown_rule}
- visible_cues: the things you actually saw that drove your answer."""


def _asset_class(asset_class: str | AssetClass, settings: Settings | None = None) -> AssetClass:
    if isinstance(asset_class, AssetClass):
        return asset_class
    return (settings or get_settings()).asset_class(str(asset_class))


def _photo_role(name: str, settings: Settings) -> str:
    cfg = streetview.imagery_config(settings)
    if name == streetview.SAT_FILE:
        pin = " A tiny red pin marks the property's map point." if cfg.satellite.marker else ""
        return f"satellite photo from directly above, centered on the property.{pin}"
    m = re.match(r"sv_(\d+)\.", name)
    i = int(m.group(1)) if m else 0
    if i == 0:
        return "Street View photo aimed straight at the property."
    offsets = cfg.street_view.side_offsets_deg
    off = offsets[i - 1] if 0 < i <= len(offsets) else 0
    side = "left" if off < 0 else "right"
    return f"Street View photo from the same spot, turned {abs(off):.0f} degrees to the {side}."


def build_prompt(asset_class: str | AssetClass, examples: Sequence[Mapping[str, Any]],
                 property_images: Sequence[str | Path] | None = None, *, settings: Settings | None = None) -> str:
    """The instructions for one property. Images are numbered in the order they are sent:
    each example's images first, then the property's photos (default: sv_0, sv_1, sv_2, sat)."""
    s = settings or get_settings()
    ac = _asset_class(asset_class, s)
    vc = ac.visual_check
    labels, targets = review.labels_of(ac), review.target_labels_of(ac)
    helps = vc.get("label_help") or {}
    lines = [str(vc.get("prompt") or "").strip(), ""]
    if vc.get("question"):
        lines += [f"Question: {vc['question']}", ""]
    lines.append("Labels (answer with exactly one):")
    for lab in labels:
        tag = " (target)" if lab in targets else ""
        lines.append(f"- {lab}{tag}: {helps.get(lab, '')}".rstrip(": "))
    n = 1
    if examples:
        lines += ["", "Labeled examples from the investor's own reviews. Their labels are correct. "
                      "Learn from them, but do not classify them:"]
        for i, ex in enumerate(examples, start=1):
            k = len(ex.get("images") or [])
            if not k:
                continue
            where = f"Image {n}" if k == 1 else f"Images {n}-{n + k - 1}"
            note = f' Why: "{ex["note"]}"' if ex.get("note") else ""
            lines.append(f"- {where}: example {i}, true label {ex['label']}.{note}")
            n += k
    names = [Path(p).name for p in property_images] if property_images else \
        [*streetview.imagery_config(s).street_view_files, streetview.SAT_FILE]
    lines += ["", "The property to classify:"]
    for name in names:
        lines.append(f"- Image {n}: {_photo_role(name, s)}")
        n += 1
    unknown_rule = " Then use the label UNKNOWN." if UNKNOWN in labels else ""
    target_text = " or ".join(targets) if targets else "the kind the investor wants"
    lines += ["", REPLY_INSTRUCTIONS.format(targets=target_text, unknown_rule=unknown_rule)]
    return "\n".join(lines).strip() + "\n"


RETRY_NOTE = ("\n\nIMPORTANT: your last reply could not be read. Reply with ONLY the JSON object described above, "
              "starting with { and ending with }.")


# --- backends ---------------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_hash_memo: dict[tuple[str, int, int], str] = {}


def file_sha256(path: Path) -> str:
    """Content hash of a photo (memoized by path, size and modified time)."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _hash_memo:
        _hash_memo[key] = _sha256_bytes(path.read_bytes())
    return _hash_memo[key]


def media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _short(text: Any, limit: int = 240) -> str:
    """A tidy, key-free excerpt of an error message."""
    s = re.sub(r"sk-ant-[\w\-]+", "***", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


class VisionBackend:
    """A way to ask Claude about photos. `classify` returns Claude's raw reply text."""
    name: str = "base"
    provider: str = ""                     # api_calls.provider
    sku: str = ""                          # api_calls.sku ("" = don't record)
    billable: bool = False                 # charges money per call

    @property
    def model(self) -> str:
        return ""

    def check(self) -> None:
        """Raise VisionFatalError when this backend can't run here."""

    def classify(self, image_paths: list[Path], prompt: str, *, schema: dict | None = None) -> str:
        raise NotImplementedError


class MockBackend(VisionBackend):
    """Offline answers made up from the photo bytes. Same photos, same answer."""
    name = "mock"

    @property
    def model(self) -> str:
        return "mock-1"

    def classify(self, image_paths: list[Path], prompt: str, *, schema: dict | None = None) -> str:
        digest = hashlib.sha256()
        for p in image_paths:
            digest.update(Path(p).read_bytes())
        h = int(digest.hexdigest(), 16)
        labels, targets = _labels_from(prompt, schema)
        p = h % 101
        quality = ("good",) * 7 + ("partial",) * 2 + ("poor",)
        q = quality[(h >> 8) % len(quality)]
        visible = (h >> 16) % 12 != 0
        if not visible or q == "poor":
            label = UNKNOWN if UNKNOWN in labels else labels[-1]
        else:
            pool = [x for x in labels if (x in targets) == (p >= 50) and x != UNKNOWN] or \
                   [x for x in labels if x != UNKNOWN] or labels
            label = pool[(h >> 24) % len(pool)]
        return json.dumps({"label": label, "p_target": p, "image_quality": q,
                           "visible_cues": [f"mock cue {(h >> 32) % 7}"], "reasons": "Mock answer (offline test mode).",
                           "property_visible": visible})


def _labels_from(prompt: str, schema: dict | None) -> tuple[list[str], set[str]]:
    labels: list[str] = []
    targets: set[str] = set()
    for m in re.finditer(r"^- ([A-Z][A-Z0-9_]+)( \(target\))?(?::|$)", prompt or "", re.MULTILINE):
        labels.append(m.group(1))
        if m.group(2):
            targets.add(m.group(1))
    if schema:
        enum = (((schema.get("properties") or {}).get("label") or {}).get("enum")) or []
        labels = [str(x) for x in enum] or labels
    return (labels or list(DEFAULT_LABELS)), targets


# --- Claude Code (your Claude plan) ---

class CLIResult(BaseModel):
    """`claude -p --output-format json` reply."""
    model_config = ConfigDict(extra="ignore")
    type: str | None = None
    subtype: str | None = None
    is_error: bool = False
    result: str | None = None
    structured_output: Any = None
    total_cost_usd: float | None = None
    session_id: str | None = None
    num_turns: int | None = None


def parse_cli_output(stdout: str) -> CLIResult | None:
    """The result message from Claude Code's JSON output (or None if there isn't one)."""
    text = (stdout or "").strip()
    if not text:
        return None
    candidates: list[Any] = []
    try:
        candidates.append(json.loads(text))
    except ValueError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    candidates.append(json.loads(line))
                except ValueError:
                    continue
    for obj in candidates:
        items = obj if isinstance(obj, list) else [obj]
        for item in reversed(items):
            if isinstance(item, dict) and item.get("type") == "result":
                try:
                    return CLIResult.model_validate(item)
                except ValidationError:
                    return None
    return None


_UNKNOWN_OPTION = re.compile(r"unknown option\s+'?(--[\w-]+)", re.IGNORECASE)
_RATE = re.compile(r"rate.?limit|too many requests|\b429\b|overloaded|\b529\b", re.IGNORECASE)
_USAGE = re.compile(r"usage limit|limit reached|hit your (?:usage )?limit|out of (?:extra )?usage|"
                    r"weekly limit|5-hour limit|credit balance|resets? (?:at|in)\b|quota", re.IGNORECASE)
_AUTH = re.compile(r"not logged in|/login|log ?in|sign ?in|invalid api key|authenticat|unauthori[sz]ed|\b401\b|"
                   r"oauth|token (?:has )?expired|forbidden|\b403\b", re.IGNORECASE)
_MODEL = re.compile(r"model.{0,40}(?:not (?:found|available|exist|supported)|invalid|unknown)|"
                    r"(?:invalid|unknown) model|not_found_error", re.IGNORECASE)


def classify_cli_error(text: str) -> str:
    """rate | usage | auth | model | other."""
    if _RATE.search(text or ""):
        return "rate"
    if _USAGE.search(text or ""):
        return "usage"
    if _AUTH.search(text or ""):
        return "auth"
    if _MODEL.search(text or ""):
        return "model"
    return "other"


def find_claude(command: str, which: Callable[[str], str | None] = shutil.which) -> str | None:
    """Full path of the `claude` command: config value, then PATH, then the usual install spots
    (a background service often has a short PATH)."""
    p = Path(command).expanduser()
    if p.is_absolute():
        return str(p) if p.exists() else None
    found = which(command)
    if found:
        return found
    if command == "claude":
        for spot in CLAUDE_FALLBACK_PATHS:
            q = Path(spot).expanduser()
            if q.exists():
                return str(q)
    return None


class ClaudeCLIBackend(VisionBackend):
    """Claude Code in print mode on this computer, billed to your Claude plan."""
    name = "claude_cli"
    provider = "claude_cli"
    sku = "vision_claude_cli"

    def __init__(self, cfg: ClaudeCLIConfig | None = None, *, runner: Callable[..., Any] = subprocess.run,
                 sleep: Callable[[float], None] = time.sleep, which: Callable[[str], str | None] = shutil.which,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg or vision_config().claude_cli
        self.runner, self.sleep, self.which, self.clock = runner, sleep, which, clock
        self.dropped_flags: set[str] = set()
        self._last_end: float | None = None

    @property
    def model(self) -> str:
        return self.cfg.model

    def executable(self) -> str | None:
        return find_claude(self.cfg.command, self.which)

    def check(self) -> None:
        if not self.executable():
            raise VisionFatalError(
                "Claude Code isn't installed on this computer (the `claude` command wasn't found), so the photo "
                "check can't run. Install it from https://claude.com/claude-code and sign in, or set "
                "vision.claude_cli.command in config.yaml to its full path.")

    # building the command

    def optional_flags(self, schema: dict | None) -> list[list[str]]:
        groups: list[list[str]] = []
        if self.cfg.json_schema and schema:
            groups.append(["--json-schema", json.dumps(schema, separators=(",", ":"))])
        if self.cfg.lean:
            groups += [["--tools", "Read"], ["--no-session-persistence"], ["--strict-mcp-config"]]
        if self.cfg.max_turns:
            groups.append(["--max-turns", str(self.cfg.max_turns)])
        return [g for g in groups if g[0] not in self.dropped_flags]

    @staticmethod
    def prompt_with_files(prompt: str, image_paths: Sequence[Path]) -> str:
        files = "\n".join(f"Image {i}: {Path(p).resolve()}" for i, p in enumerate(image_paths, start=1))
        return (f"{prompt.rstrip()}\n\nThe image files, in order. Open every one with the Read tool before "
                f"answering:\n{files}\n\nAfter reading them all, reply with ONLY the JSON object.")

    def build_command(self, image_paths: Sequence[Path], prompt: str, schema: dict | None = None) -> list[str]:
        exe = self.executable() or self.cfg.command
        dirs = sorted({str(Path(p).resolve().parent) for p in image_paths})
        cmd = [exe, "-p", self.prompt_with_files(prompt, image_paths), "--output-format", "json",
               "--model", self.cfg.model, "--allowedTools", "Read"]
        for group in self.optional_flags(schema):
            cmd += group
        if dirs:
            cmd += ["--add-dir", *dirs]
        return cmd

    def child_env(self) -> dict[str, str]:
        """This process's environment minus the API key, so Claude Code uses your Claude plan."""
        strip = set(self.cfg.strip_env)
        env = {k: v for k, v in os.environ.items() if k not in strip}
        exe = self.executable()
        if exe:
            bin_dir = str(Path(exe).parent)
            parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
            if bin_dir not in parts:
                env["PATH"] = os.pathsep.join([bin_dir, *parts])
        return env

    def workdir(self) -> str:
        """A neutral folder, so the project's own CLAUDE.md and settings don't get loaded."""
        d = Path(self.cfg.workdir).expanduser() if self.cfg.workdir else Path(tempfile.gettempdir()) / "bluemoon-vision"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    # running it

    def _pause(self) -> None:
        if self._last_end is not None and self.cfg.pause_seconds > 0:
            wait = self.cfg.pause_seconds - (self.clock() - self._last_end)
            if wait > 0:
                self.sleep(wait)

    def _run(self, cmd: list[str]) -> Any:
        self._pause()
        try:
            return self.runner(cmd, capture_output=True, text=True, timeout=self.cfg.timeout_seconds,
                               env=self.child_env(), cwd=self.workdir(), stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            self.check()
            raise VisionFatalError("Claude Code couldn't be started. Check vision.claude_cli.command in "
                                   "config.yaml.") from None
        except subprocess.TimeoutExpired:
            raise VisionCallError(f"Claude took longer than {self.cfg.timeout_seconds:.0f} seconds on this property; "
                                  f"it will be tried again next run.", calls=1) from None
        finally:
            self._last_end = self.clock()

    def classify(self, image_paths: list[Path], prompt: str, *, schema: dict | None = None) -> str:
        missing = [str(p) for p in image_paths if not Path(p).is_file()]
        if missing:
            raise VisionCallError("Some photos for this property are missing on disk; fetch photos again.")
        rate_retries = 1
        for _ in range(8):  # a few rounds: dropping flags this version doesn't know, one rate-limit wait
            proc = self._run(self.build_command(image_paths, prompt, schema))
            res = parse_cli_output(proc.stdout or "")
            if res is None:
                text = f"{proc.stderr or ''} {proc.stdout or ''}"
                flag = self._unknown_flag(text, schema)
                if flag:
                    log.info("This Claude Code version doesn't know %s; running without it.", flag)
                    self.dropped_flags.add(flag)
                    continue
                kind = classify_cli_error(text)
                if kind == "rate" and rate_retries:
                    rate_retries -= 1
                    self.sleep(self.cfg.rate_limit_wait_seconds)
                    continue
                self._raise_for(kind, text, proc.returncode)
            if res.is_error or (res.subtype and res.subtype != "success"):
                text = res.result or res.subtype or ""
                kind = classify_cli_error(text)
                if kind == "rate" and rate_retries:
                    rate_retries -= 1
                    self.sleep(self.cfg.rate_limit_wait_seconds)
                    continue
                self._raise_for(kind, text, proc.returncode, calls=1)
            if isinstance(res.structured_output, (dict, list)):
                return json.dumps(res.structured_output)
            text = res.result or ""
            if "{" not in text and _USAGE.search(text):  # some versions report the plan limit as a normal reply
                self._raise_for("usage", text, proc.returncode, calls=1)
            return text
        raise VisionCallError("Claude Code kept failing on this property; it will be tried again next run.")

    def _unknown_flag(self, text: str, schema: dict | None) -> str | None:
        m = _UNKNOWN_OPTION.search(text or "")
        if not m:
            return None
        flag = m.group(1)
        known = {g[0] for g in self.optional_flags(schema)}
        return flag if flag in known else None

    def _raise_for(self, kind: str, text: str, returncode: int | None, calls: int = 0) -> NoReturn:
        said = _short(text)
        if kind == "rate":
            raise VisionFatalError(f"Claude is busy or limiting how fast we can ask right now (rate limit), so the "
                                   f"photo check stopped. The rest stay pending; run it again later. "
                                   f"(Claude said: {said})", calls=calls)
        if kind == "usage":
            raise VisionFatalError(f"Your Claude plan's usage limit was reached, so the photo check stopped. "
                                   f"Properties not checked yet stay pending; run it again after the limit resets. "
                                   f"(Claude said: {said})", calls=calls)
        if kind == "auth":
            raise VisionFatalError("Claude Code isn't signed in on this computer, so the photo check can't run. Open "
                                   "Terminal, type `claude`, sign in (type /login), then run the photo check again.",
                                   calls=calls)
        if kind == "model":
            raise VisionFatalError(f"Claude Code doesn't accept the model '{self.cfg.model}'. Change "
                                   f"vision.claude_cli.model in config.yaml (for example: sonnet). "
                                   f"(Claude said: {said})", calls=calls)
        detail = said or f"exit code {returncode}"
        raise VisionCallError(f"Claude Code couldn't check this property ({detail}). It will be tried again next run.",
                              calls=calls)


# --- Anthropic API (pay per use) ---

class AnthropicAPIBackend(VisionBackend):
    """The pay-per-use Anthropic API. Needs ANTHROPIC_API_KEY in .env."""
    name = "anthropic_api"
    provider = "anthropic"
    sku = "vision_anthropic_api"
    billable = True

    def __init__(self, cfg: AnthropicAPIConfig | None = None, *, client: Any = None):
        self.cfg = cfg or vision_config().anthropic_api
        self._client = client
        self._fallback = self.cfg.refusal_fallback

    @property
    def model(self) -> str:
        return self.cfg.model

    def check(self) -> None:
        if self._client is None and not secret(API_KEY_NAME):
            raise VisionFatalError("vision.backend is anthropic_api but there's no ANTHROPIC_API_KEY in .env. Add the "
                                   "key, or switch vision.backend back to claude_cli to use your Claude plan.")

    def client(self) -> Any:
        if self._client is None:
            self.check()
            import anthropic
            self._client = anthropic.Anthropic(api_key=secret(API_KEY_NAME), max_retries=self.cfg.max_retries,
                                               timeout=self.cfg.timeout_seconds)
        return self._client

    @staticmethod
    def content_blocks(image_paths: Sequence[Path], prompt: str) -> list[dict]:
        blocks: list[dict] = []
        for i, p in enumerate(image_paths, start=1):
            data = Path(p).read_bytes()
            blocks.append({"type": "text", "text": f"Image {i}:"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type(data),
                                                       "data": base64.standard_b64encode(data).decode("ascii")}})
        blocks.append({"type": "text", "text": prompt})
        return blocks

    def request(self, image_paths: Sequence[Path], prompt: str, schema: dict | None, max_tokens: int) -> dict:
        kwargs: dict[str, Any] = {"model": self.cfg.model, "max_tokens": max_tokens,
                                  "messages": [{"role": "user", "content": self.content_blocks(image_paths, prompt)}]}
        output_config: dict[str, Any] = {}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if self.cfg.effort:
            output_config["effort"] = self.cfg.effort
        if output_config:
            kwargs["output_config"] = output_config
        return kwargs

    def _send(self, kwargs: dict) -> Any:
        import anthropic
        c = self.client()
        try:
            if self._fallback:
                return c.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
            return c.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise VisionFatalError("The Anthropic API says the ANTHROPIC_API_KEY in .env isn't valid.") from None
        except anthropic.PermissionDeniedError as exc:
            raise VisionFatalError(f"The Anthropic API refused this key ({_short(exc.message)}).") from None
        except anthropic.NotFoundError:
            raise VisionFatalError(f"The Anthropic API doesn't know the model '{self.cfg.model}'. Change "
                                   f"vision.anthropic_api.model in config.yaml.") from None
        except anthropic.RateLimitError:
            raise VisionFatalError("The Anthropic API is limiting how fast we can ask (rate limit), so the photo check "
                                   "stopped. The rest stay pending; run it again later.") from None
        except anthropic.BadRequestError as exc:
            text = str(exc.message)
            if "credit balance" in text.lower():
                raise VisionFatalError("The Anthropic account is out of credit, so the photo check stopped. "
                                       "Add credit in the Claude Console, then run it again.") from None
            if self._fallback and "fallback" in text.lower():
                self._fallback = False
                return self._send(kwargs)
            raise VisionCallError(f"The Anthropic API couldn't use this request ({_short(text)}).") from None
        except anthropic.APIStatusError as exc:
            raise VisionCallError(f"The Anthropic API had a problem (HTTP {exc.status_code}); this property will be "
                                  f"tried again next run.") from None
        except anthropic.APIConnectionError:
            raise VisionCallError("Couldn't reach the Anthropic API (network). This property will be tried again "
                                  "next run.") from None

    def classify(self, image_paths: list[Path], prompt: str, *, schema: dict | None = None) -> str:
        msg = self._send(self.request(image_paths, prompt, schema, self.cfg.max_tokens))
        if getattr(msg, "stop_reason", None) == "max_tokens":
            bigger = min(16000, self.cfg.max_tokens * 4)
            msg = self._send(self.request(image_paths, prompt, schema, bigger))
        if getattr(msg, "stop_reason", None) == "refusal":
            raise VisionCallError("Claude declined to judge these photos. Review this one by hand.", calls=1)
        return "".join(getattr(b, "text", "") for b in (getattr(msg, "content", None) or [])
                       if getattr(b, "type", None) == "text")


BACKENDS = {"claude_cli": ClaudeCLIBackend, "anthropic_api": AnthropicAPIBackend, "mock": MockBackend}


def get_backend(name: str | None = None, settings: Settings | None = None) -> VisionBackend:
    """The configured backend (vision.backend), or the one named."""
    cfg = vision_config(settings)
    key = (name or cfg.backend or "claude_cli").strip().lower()
    if key == "claude_cli":
        return ClaudeCLIBackend(cfg.claude_cli)
    if key == "anthropic_api":
        return AnthropicAPIBackend(cfg.anthropic_api)
    if key == "mock":
        return MockBackend()
    raise ValueError(f"Unknown photo-check backend '{name or cfg.backend}'. Use claude_cli, anthropic_api or mock.")


def claude_cli_available(settings: Settings | None = None) -> bool:
    """Is Claude Code installed here? (Doesn't check that it's signed in.)"""
    return find_claude(vision_config(settings).claude_cli.command) is not None


# --- rules prior ------------------------------------------------------------------------------

_FIELDS = ("stories", "units", "building_count", "building_sqft", "lot_acres", "year_built")
_FIELD_WORDS = {"stories": "stories", "units": "units", "building_count": "buildings",
                "building_sqft": "sq ft", "lot_acres": "acres", "year_built": "year built"}
_CLAUSE = re.compile(
    r"(?P<field>" + "|".join(_FIELDS) + r")_(?P<n>\d+(?:p\d+)?)_(?:or_)?(?P<cmp>plus|more|less|fewer)"
    r"|built_before_(?P<yb>\d{4})"
    r"|built_(?P<ya>\d{4})_(?:or_)?(?:after|later|plus)"
    r"|built_(?P<y1>\d{4})_(?P<y2>\d{4})")


@dataclass(frozen=True)
class _Clause:
    field: str
    lo: float | None
    hi: float | None

    def test(self, prop: Mapping[str, Any]) -> bool | None:
        v = prop.get(self.field)
        try:
            x = float(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            x = None
        if x is None:
            return None
        return (self.lo is None or x >= self.lo) and (self.hi is None or x <= self.hi)

    def text(self) -> str:
        word = _FIELD_WORDS.get(self.field, self.field)

        def num(v: float) -> str:
            return f"{v:g}"
        if self.field == "year_built":
            if self.lo is not None and self.hi is not None:
                return f"built {num(self.lo)}-{num(self.hi)}"
            return f"built {num(self.lo)} or later" if self.lo is not None else f"built before {num(self.hi + 1)}"
        if self.lo is not None:
            return f"{num(self.lo)}+ {word}"
        return f"{num(self.hi)} or fewer {word}"


@lru_cache(maxsize=256)
def parse_prior_rule(key: str) -> tuple[_Clause, ...] | None:
    """'stories_3_less_built_1970_1995' -> (stories <= 3, 1970 <= year_built <= 1995). None if unreadable."""
    s, pos, out = str(key).strip().lower(), 0, []
    while pos < len(s):
        m = _CLAUSE.match(s, pos)
        if not m:
            return None
        if m.group("field"):
            n = float(m.group("n").replace("p", "."))
            up = m.group("cmp") in ("plus", "more")
            out.append(_Clause(m.group("field"), n if up else None, None if up else n))
        elif m.group("yb"):
            out.append(_Clause("year_built", None, float(m.group("yb")) - 1))
        elif m.group("ya"):
            out.append(_Clause("year_built", float(m.group("ya")), None))
        else:
            out.append(_Clause("year_built", float(m.group("y1")), float(m.group("y2"))))
        pos = m.end()
        if pos < len(s):
            if s[pos] != "_":
                return None
            pos += 1
            if s.startswith("and_", pos):
                pos += 4
    return tuple(out) or None


def explain_prior(prop: Mapping[str, Any], asset_class: str | AssetClass,
                  settings: Settings | None = None) -> list[dict]:
    """Each rule from visual_check.prior: {rule, points, applies (True/False/None = data missing), text}."""
    ac = _asset_class(asset_class, settings)
    out = []
    for key, points in (ac.visual_check.get("prior") or {}).items():
        clauses = parse_prior_rule(key)
        try:
            pts = float(points)
        except (TypeError, ValueError):
            pts = None
        if clauses is None or pts is None:
            out.append({"rule": key, "points": points, "applies": None,
                        "text": f"Rule '{key}' in asset_classes.yaml isn't readable; skipped."})
            continue
        results = [c.test(prop) for c in clauses]
        applies = None if any(r is None for r in results) else all(results)
        out.append({"rule": key, "points": pts, "applies": applies,
                    "text": " and ".join(c.text() for c in clauses) + f": {pts:+g}"})
    return out


def rules_prior(prop: Mapping[str, Any], asset_class: str | AssetClass, settings: Settings | None = None) -> float:
    """Sum of the rule points that apply. Missing data never counts for or against."""
    return float(sum(r["points"] for r in explain_prior(prop, asset_class, settings) if r["applies"] is True))


# --- final verdict ----------------------------------------------------------------------------

def combine(ai_p: float, prior: float, weight_vision: float) -> float:
    p = weight_vision * float(ai_p) + (1.0 - weight_vision) * (50.0 + float(prior))
    return round(min(100.0, max(0.0, p)), 1)


def final_p_for(result: VisualResult, prior: float, weight_vision: float) -> float | None:
    """None = needs a human look (poor or no photos, property not visible, or UNKNOWN)."""
    if result.image_quality in NO_VERDICT_QUALITIES or not result.property_visible or result.label == UNKNOWN:
        return None
    return combine(result.p_target, prior, weight_vision)


def ai_final_p(row: Mapping[str, Any], settings: Settings | None = None) -> float | None:
    """The AI's own final_p from a visual_checks row (even after a human review replaced final_p)."""
    if not row.get("ai_label") or row.get("ai_p") is None:
        return None
    if row.get("image_quality") in NO_VERDICT_QUALITIES or row.get("property_visible") in (0, False):
        return None
    if row.get("ai_label") == UNKNOWN:
        return None
    return combine(float(row["ai_p"]), float(row.get("prior") or 0.0), vision_config(settings).weight_vision)


# --- the run ----------------------------------------------------------------------------------

@dataclass
class _Target:
    pid: int
    prop: dict
    images: list[Path]
    classified: bool


_TARGET_SQL = """
SELECT p.id, p.stories, p.units, p.year_built, p.building_count, p.building_sqft, p.lot_acres,
       v.status, v.ai_label, v.checked_at, v.reviewed_label, s.rank, s.score, s.in_buybox
FROM visual_checks v
JOIN properties p ON p.id = v.property_id
LEFT JOIN scores s ON s.property_id = p.id
WHERE p.market_id = ?
"""


def _targets(conn: sqlite3.Connection, market: dict, settings: Settings, *, reviewed_only: bool,
             reclassify: bool, limit: int | None) -> list[_Target]:
    found: list[tuple[tuple, _Target]] = []
    for r in rows(conn, _TARGET_SQL, (market["id"],)):
        reviewed = bool(r.get("reviewed_label"))
        if reviewed != reviewed_only:
            continue
        classified = bool(r.get("ai_label") and r.get("checked_at"))
        if classified and not reclassify and not reviewed_only:
            continue
        images = streetview.image_paths(settings, r["id"])
        if not images:
            continue
        rank = r.get("rank")
        order = (classified, r.get("in_buybox") == 0, rank is None, rank or 0, -(r.get("score") or 0.0), r["id"])
        found.append((order, _Target(r["id"], r, images, classified)))
    found.sort(key=lambda t: t[0])
    out = [t for _, t in found]
    return out[:max(0, int(limit))] if limit is not None else out


@dataclass
class _Prepared:
    prompt: str
    images: list[Path]
    key: str
    examples: int


class _Asker:
    """Builds each property's prompt and cache key (examples are read once per run)."""

    def __init__(self, ac: AssetClass, backend: VisionBackend, cfg: VisionConfig, settings: Settings):
        self.ac, self.backend, self.cfg, self.settings = ac, backend, cfg, settings
        self.labels = review.labels_of(ac)
        self.schema = result_schema(self.labels)
        k = cfg.examples_per_call
        self.pool = review.examples_for(ac, k + 1, settings=settings) if k else []

    def prepare(self, t: _Target) -> _Prepared:
        exs = [dict(e) for e in self.pool if e["property_id"] != t.pid][:self.cfg.examples_per_call]
        for e in exs:
            e["images"] = list(e["images"])[:self.cfg.images_per_example]
        images = [p for e in exs for p in e["images"]] + list(t.images)
        prompt = build_prompt(self.ac, exs, [p.name for p in t.images], settings=self.settings)
        key = make_key(CACHE_PROVIDER, self.backend.name, self.backend.model, _sha256_bytes(prompt.encode()),
                       [file_sha256(p) for p in images])
        return _Prepared(prompt, images, key, len(exs))

    def cached(self, conn: sqlite3.Connection, prep: _Prepared) -> VisualResult | None:
        hit = cache_get(conn, prep.key)
        if not hit or (hit.get("status") or 0) >= 400 or not isinstance(hit.get("body"), dict):
            return None
        try:
            res = VisualResult.model_validate(hit["body"])
        except ValidationError:
            return None
        return res if res.label in self.labels else None

    def ask(self, prep: _Prepared) -> tuple[VisualResult, int]:
        """(answer, calls made). One retry when the reply can't be read."""
        calls = 0
        prompt = prep.prompt
        last = ""
        for _ in range(2):
            try:
                text = self.backend.classify(prep.images, prompt, schema=self.schema)
            except VisionError as exc:
                exc.calls += calls
                raise
            calls += 1
            try:
                return parse_result(text, self.labels), calls
            except VisionParseError as exc:
                last = str(exc)
                prompt = prep.prompt + RETRY_NOTE
        raise VisionCallError(f"Claude's answer couldn't be read twice ({last}). This property will be tried again "
                              f"next run.", calls=calls)


@dataclass
class _RunStats:
    classified: int = 0
    cached: int = 0
    calls: int = 0
    errors: int = 0
    needs_review: int = 0
    colors: dict[str, int] = field(default_factory=lambda: {"green": 0, "yellow": 0, "red": 0, "na": 0})
    capped: bool = False
    fatal: str | None = None
    warnings: list[str] = field(default_factory=list)


def _record(conn: sqlite3.Connection, backend: VisionBackend, calls: int, stats: _RunStats) -> None:
    stats.calls += calls
    if calls and backend.sku:
        record_call(conn, backend.provider, backend.sku, calls)


def _save(conn: sqlite3.Connection, t: _Target, res: VisualResult, ac: AssetClass, backend: VisionBackend,
          cfg: VisionConfig, settings: Settings, keep_final: bool) -> float | None:
    prior = rules_prior(t.prop, ac, settings)
    fp = final_p_for(res, prior, cfg.weight_vision)
    row: dict[str, Any] = {
        "ai_label": res.label, "ai_p": res.p_target, "image_quality": res.image_quality,
        "property_visible": int(res.property_visible), "cues_json": dumps(res.visible_cues), "reasons": res.reasons,
        "prior": prior, "model": f"{backend.name}:{backend.model}", "error": None, "checked_at": now_iso(),
    }
    if not keep_final:
        row["final_p"] = fp
    with conn:
        streetview.update_visual(conn, t.pid, row)
    return fp


def _save_error(conn: sqlite3.Connection, pid: int, message: str) -> None:
    with conn:
        conn.execute("UPDATE visual_checks SET error = ? WHERE property_id = ?", (message, pid))


def _count_calls(conn: sqlite3.Connection, asker: _Asker, targets: list[_Target]) -> tuple[int, int]:
    """(cached, calls needed) for these properties."""
    cached = sum(1 for t in targets if asker.cached(conn, asker.prepare(t)) is not None)
    return cached, len(targets) - cached


def _cost_note(backend: VisionBackend, cfg: VisionConfig, calls: int) -> tuple[float, str]:
    if backend.name == "anthropic_api":
        usd = round(calls * cfg.anthropic_api.est_cost_per_call_usd, 2)
        return usd, f"about ${usd:.2f} on the pay-per-use Anthropic API"
    if backend.name == "claude_cli":
        return 0.0, "no per-call bill: it uses your Claude plan's usage"
    return 0.0, "free (offline test mode)"


def _estimate(conn: sqlite3.Connection, slug: str, ac: AssetClass, targets: list[_Target], asker: _Asker,
              backend: VisionBackend, cfg: VisionConfig, reviewed_only: bool) -> dict:
    cached, calls = _count_calls(conn, asker, targets)
    planned = min(calls, cfg.max_calls_per_run)
    usd, note = _cost_note(backend, cfg, planned)
    ready, why = True, None
    try:
        backend.check()
    except VisionFatalError as exc:
        ready, why = False, exc.message
    what = "reviewed properties to re-check" if reviewed_only else "properties with photos to check"
    msg = (f"Photo check with {backend.name} ({backend.model}): {len(targets):,} {what}, {cached:,} already "
           f"answered, {planned:,} to ask Claude ({note}).")
    if calls > planned:
        msg += f" Capped at {cfg.max_calls_per_run:,} calls per run (vision.max_calls_per_run)."
    if not ready:
        msg += f" {why}"
    return {"dry_run": True, "enabled": True, "asset_class": ac.key, "backend": backend.name, "model": backend.model,
            "properties": len(targets), "cached": cached, "calls": planned, "capped": calls > planned,
            "est_cost_usd": usd, "ready": ready, "not_ready_reason": why, "examples": len(asker.pool),
            "needs_confirm": backend.name != "mock" and planned > cfg.confirm_over_properties,
            "reviewed_only": reviewed_only, "market": slug, "message": msg}


def classify_estimate(conn: sqlite3.Connection, market_slug: str, limit: int | None = None,
                      backend: VisionBackend | str | None = None, *, reviewed_only: bool = False,
                      reclassify: bool = False) -> dict:
    """What a photo-check run would do, without asking Claude anything."""
    return classify_properties(conn, market_slug, limit=limit, dry_run=True, backend=backend,
                               reviewed_only=reviewed_only, reclassify=reclassify)


def classify_properties(conn: sqlite3.Connection, market_slug: str, limit: int | None = None, dry_run: bool = False,
                        backend: VisionBackend | str | None = None, progress: Progress = None, confirm: bool = False,
                        *, reviewed_only: bool = False, reclassify: bool = False) -> dict:
    """Ask Claude about every property whose photos are saved but not yet checked.

    limit: at most this many properties (best-ranked first).
    dry_run: count and estimate only.
    backend: a VisionBackend, a backend name, or None for vision.backend in config.yaml.
    confirm: needed when more than safety.confirm_over_properties calls would be made (not for mock).
    reviewed_only: re-check the properties you reviewed (calibrate); your answers are kept.
    reclassify: also re-check properties the AI already answered (cached answers are reused).
    """
    settings = get_settings()
    cfg = vision_config(settings)
    market = market_by_slug(conn, market_slug)
    ac = settings.asset_class(market["asset_class"])
    if not ac.visual_enabled:
        return {"dry_run": dry_run, "enabled": False, "asset_class": ac.key, "properties": 0, "calls": 0,
                "needs_confirm": False, "message": f"{ac.label} has no photo check."}
    be = backend if isinstance(backend, VisionBackend) else get_backend(backend, settings)
    targets = _targets(conn, market, settings, reviewed_only=reviewed_only, reclassify=reclassify, limit=limit)
    asker = _Asker(ac, be, cfg, settings)
    estimate = _estimate(conn, market_slug, ac, targets, asker, be, cfg, reviewed_only)
    if dry_run:
        return estimate
    if estimate["calls"] and not estimate["ready"]:
        return {**estimate, "dry_run": False, "error": estimate["not_ready_reason"], "classified": 0,
                "message": estimate["not_ready_reason"]}
    if estimate["needs_confirm"] and not confirm:
        return {**estimate, "dry_run": False,
                "message": f"Waiting for your OK: {estimate['message']} Run again with confirm (CLI: --yes)."}
    stats = _run(conn, targets, asker, ac, be, cfg, settings, progress, keep_final=reviewed_only)
    result = _result(estimate, stats, targets, be)
    log.info("Photo check %s: %s", market_slug, result["message"])
    geo.call_progress(progress, 1.0, result["message"])
    return result


def _run(conn: sqlite3.Connection, targets: list[_Target], asker: _Asker, ac: AssetClass, be: VisionBackend,
         cfg: VisionConfig, settings: Settings, progress: Progress, keep_final: bool) -> _RunStats:
    stats = _RunStats()
    rcfg = review.review_config(settings)
    consecutive = 0
    for i, t in enumerate(targets, start=1):
        prep = asker.prepare(t)
        res = asker.cached(conn, prep)
        if res is not None:
            stats.cached += 1
        else:
            if stats.calls >= cfg.max_calls_per_run:
                stats.capped = True
                break
            try:
                res, calls = asker.ask(prep)
                _record(conn, be, calls, stats)
                cache_put(conn, prep.key, f"{CACHE_PROVIDER}:{be.name}", 200, res.model_dump())
                consecutive = 0
            except VisionFatalError as exc:
                _record(conn, be, exc.calls, stats)
                stats.fatal = exc.message
                break
            except VisionCallError as exc:
                _record(conn, be, exc.calls, stats)
                stats.errors += 1
                consecutive += 1
                _save_error(conn, t.pid, exc.message)
                if exc.message not in stats.warnings:
                    stats.warnings.append(exc.message)
                if consecutive >= cfg.max_consecutive_errors:
                    stats.fatal = (f"{consecutive} properties in a row failed, so the photo check stopped. "
                                   f"Last problem: {exc.message}")
                    break
                continue
        fp = _save(conn, t, res, ac, be, cfg, settings, keep_final)
        stats.classified += 1
        stats.colors[review.visual_color(fp, rcfg)] += 1
        stats.needs_review += int(fp is None)
        geo.call_progress(progress, i / max(1, len(targets)) * 0.98,
                                     f"Photo check: {i:,} of {len(targets):,} properties")
    return stats


def _result(estimate: dict, stats: _RunStats, targets: list[_Target], be: VisionBackend) -> dict:
    left = len(targets) - stats.classified - stats.errors
    msg = (f"Photo check: {stats.classified:,} properties checked ({stats.cached:,} from saved answers, "
           f"{stats.calls:,} calls to Claude). Green {stats.colors['green']:,}, yellow {stats.colors['yellow']:,}, "
           f"red {stats.colors['red']:,}, needs your review {stats.colors['na']:,}.")
    if stats.errors:
        msg += f" {stats.errors:,} had a problem and will be retried next run."
    if stats.capped:
        msg += " Hit the per-run call cap (vision.max_calls_per_run); run it again for the rest."
    if stats.fatal:
        msg += f" Stopped early: {stats.fatal}"
    if left > 0 and (stats.fatal or stats.capped):
        msg += f" {left:,} still pending."
    return {**estimate, "dry_run": False, "needs_confirm": False, "classified": stats.classified,
            "cached": stats.cached, "calls": stats.calls, "errors": stats.errors, "needs_review": stats.needs_review,
            "colors": stats.colors, "capped": stats.capped, "pending": max(0, left), "stopped": stats.fatal is not None,
            "error": stats.fatal, "warnings": stats.warnings,
            "est_cost_usd": _cost_note(be, vision_config(), stats.calls)[0], "message": msg}


__all__ = ["AnthropicAPIBackend", "ClaudeCLIBackend", "MockBackend", "VisionBackend", "VisionCallError",
           "VisionConfig", "VisionError", "VisionFatalError", "VisionParseError", "VisualResult", "ai_final_p",
           "build_prompt", "claude_cli_available", "classify_estimate", "classify_properties", "combine",
           "explain_prior", "extract_json_object", "final_p_for", "get_backend", "parse_cli_output",
           "parse_prior_rule", "parse_result", "result_schema", "rules_prior", "vision_config"]
