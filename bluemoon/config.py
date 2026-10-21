"""Load settings from config.yaml, config/*.yaml and .env.

Everything tunable (thresholds, weights, origin, rent scale, prices) lives in YAML.
Secrets live only in .env and are read through `secret()`; never print them.
"""
from __future__ import annotations

import copy
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """The folder holding config.yaml. BLUEMOON_HOME overrides it (tests use this)."""
    return Path(os.environ.get("BLUEMOON_HOME", REPO_ROOT))


class Origin(BaseModel):
    label: str
    lat: float
    lng: float


class MarketConfig(BaseModel):
    slug: str
    asset_class: str
    geography: str
    origin: Origin
    max_drive_minutes: int = 60


class SizeSpec(BaseModel):
    metric: str  # units | building_sqft | lot_acres
    ideal: list[float]
    ok: list[float]
    fallback: str | None = None
    sqft_per_unit: float | None = None
    acres_per_unit: float | None = None


class AssetClass(BaseModel):
    key: str
    label: str
    crexi_property_types: list[str] = Field(default_factory=list)
    size: SizeSpec
    year_built_min: int | None = None
    max_stories_hint: int | None = None
    rent: dict[str, Any] = Field(default_factory=dict)
    visual_check: dict[str, Any] = Field(default_factory=dict)
    signals: dict[str, Any] = Field(default_factory=dict)  # merged default_signals + overrides

    @property
    def rent_enabled(self) -> bool:
        return bool(self.rent.get("enabled"))

    @property
    def visual_enabled(self) -> bool:
        return bool(self.visual_check.get("enabled"))


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Settings:
    """All settings. Use get_settings(); call reload_settings() after editing YAML."""

    def __init__(self, root: Path | None = None, config_path: Path | None = None):
        self.root = Path(root) if root else project_root()
        load_dotenv(self.root / ".env", override=False)
        self.raw: dict = _load_yaml(Path(config_path) if config_path else self.root / "config.yaml")
        ac_raw = _load_yaml(self.root / "config" / "asset_classes.yaml")
        self.drill: dict = _load_yaml(self.root / "config" / "drill.yaml")
        self.sources: dict = _load_yaml(self.root / "config" / "sources.yaml")

        defaults = ac_raw.get("default_signals", {})
        self.asset_classes: dict[str, AssetClass] = {}
        for key, spec in (ac_raw.get("asset_classes") or {}).items():
            spec = dict(spec)
            spec["signals"] = _deep_merge(defaults, spec.get("signals") or {})
            self.asset_classes[key] = AssetClass(key=key, **spec)

        self.markets: list[MarketConfig] = [MarketConfig(**m) for m in self.raw.get("markets", [])]

    # --- sections -------------------------------------------------------
    def section(self, name: str) -> dict:
        return self.raw.get(name, {}) or {}

    @property
    def app(self) -> dict:
        return self.section("app")

    def path(self, name: str) -> Path:
        """Absolute path for a key under `paths:` (db, crexi_dir, images, labels, output, hud_dir)."""
        rel = self.section("paths").get(name)
        if rel is None:
            raise KeyError(f"paths.{name} missing from config.yaml")
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def asset_class(self, key: str) -> AssetClass:
        if key not in self.asset_classes:
            raise KeyError(f"Unknown asset class '{key}'. Known: {', '.join(self.asset_classes)}")
        return self.asset_classes[key]

    def market(self, slug: str) -> MarketConfig:
        for m in self.markets:
            if m.slug == slug:
                return m
        raise KeyError(f"Unknown market '{slug}'")

    @property
    def default_market(self) -> MarketConfig:
        return self.markets[0]

    @property
    def drill_stages(self) -> list[dict]:
        return [s for s in self.drill.get("stages", [])]


def secret(name: str) -> str | None:
    """Read a secret from the environment (.env). Returns None when blank. Never log the value."""
    v = os.environ.get(name, "").strip()
    return v or None


def has_secret(name: str) -> bool:
    return secret(name) is not None


def save_secret(name: str, value: str, settings: "Settings | None" = None) -> Path:
    """Write NAME=value into .env (replacing the old line, or adding one) and use it in this process too.
    The file stays private (owner-only). Never log the value."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or "\n" in value or "\r" in value:
        raise ValueError("That name or value can't go in .env.")
    path = (settings or get_settings()).root / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if re.match(rf"^\s*(export\s+)?{name}\s*=", line):
            if not done:
                out.append(f"{name}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{name}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    os.environ[name] = value
    return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
