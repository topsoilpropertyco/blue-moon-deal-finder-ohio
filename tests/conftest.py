"""Shared test fixtures.

Every test gets its own temporary project root (copies of the real config files)
and its own empty database, so tests never touch your real data.

Optional real-data tests: set BLUEMOON_REAL_CREXI to the path of a real Crexi export.
Those tests are skipped when it is not set. Never commit real exports.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def tmp_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "bm"
    root.mkdir()
    # The real config.yaml, word for word (some tests edit its text), except that tests never start the
    # background HUD rent check after an import.
    text = (REPO / "config.yaml").read_text(encoding="utf-8")
    off = re.sub(r"(?m)^(\s+after_import:\s*)true\b", r"\1false", text, count=1)
    assert off != text, "config.yaml should have rent: after_import: true"
    (root / "config.yaml").write_text(off, encoding="utf-8")
    shutil.copytree(REPO / "config", root / "config")
    for d in ("data/crexi", "images", "labels", "output"):
        (root / d).mkdir(parents=True, exist_ok=True)
    # No real keys in tests.
    for k in ("GOOGLE_MAPS_API_KEY", "ANTHROPIC_API_KEY", "HUD_API_TOKEN", "CENSUS_API_KEY", "APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BLUEMOON_HOME", str(root))
    return root


@pytest.fixture
def settings(tmp_root):
    """Settings for the temp root. get_settings() also returns these (BLUEMOON_HOME is set)."""
    from bluemoon.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.root == tmp_root
    yield s
    get_settings.cache_clear()


@pytest.fixture
def conn(settings):
    from bluemoon.db import connect
    c = connect(settings=settings)
    yield c
    c.close()


@pytest.fixture
def sample_csv() -> Path:
    return FIXTURES / "crexi_sample.csv"


@pytest.fixture
def market_slug() -> str:
    return "apartments-springfield-oh"


@pytest.fixture
def real_crexi() -> Path:
    p = os.environ.get("BLUEMOON_REAL_CREXI")
    if not p or not Path(p).exists():
        pytest.skip("BLUEMOON_REAL_CREXI not set")
    return Path(p)
