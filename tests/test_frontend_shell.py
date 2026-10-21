"""The web app shell: index.html, styles.css, core.js, deals.js and property.js.

No browser here. These tests check the contract other screens rely on (load order, design tokens,
shared classes, the window.BM API) and run the pure helpers in core.js under Node when Node is installed
(skipped otherwise; the dashboard itself never needs Node).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "bluemoon" / "web" / "static"
SHELL_JS = ("core.js", "deals.js", "property.js")

CSS_ORDER = ["styles.css", "drill.css", "views.css"]
JS_ORDER = ["core.js", "property.js", "deals.js", "drill.js", "credits.js", "review.js", "funnel.js", "analytics.js",
            "score.js", "sources.js", "importer.js", "run.js", "exports.js", "settings.js"]
TOKENS = ["--bg", "--surface", "--sunk", "--ink", "--muted", "--faint", "--line", "--accent", "--accent-ink",
          "--accent-soft", "--good", "--warn", "--bad", "--na", "--moon-dim", "--shadow", "--f-display", "--f-ui",
          "--f-mono"]
SHARED_CLASSES = ["panel", "btn", "chip", "pill", "eyebrow", "lines", "stat", "strip", "hbars", "hb", "wt", "dot", "lv",
                  "pips", "ch", "kv", "note", "big", "tablewrap", "cards", "card", "bar", "spacer", "search", "seg",
                  "toast", "grid2"]


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_index_loads_files_in_the_agreed_order():
    html = read("index.html")
    assert "<title>Blue Moon Deal Finder</title>" in html
    assert 'name="viewport"' in html
    css = re.findall(r'<link rel="stylesheet" href="/static/([\w.-]+)"', html)
    assert css == CSS_ORDER
    js = re.findall(r'<script src="/static/([\w.-]+)"></script>', html)
    assert js == JS_ORDER
    assert 'href="/static/favicon.svg"' in html
    assert "fonts.googleapis.com" in html and 'media="print"' in html   # fonts never block the page


def test_styles_define_both_themes_and_the_shared_classes():
    css = read("styles.css")
    root, dark = css.split("@media (prefers-color-scheme: dark)", 1)
    for token in TOKENS:
        assert f"{token}:" in root, token
    for token in TOKENS[:16]:                 # colors change in dark mode; fonts do not
        assert f"{token}:" in dark, token
    for cls in SHARED_CLASSES:
        assert re.search(r"\." + re.escape(cls) + r"[\s{,.:\[]", css), cls


def test_no_literal_colors_outside_the_tokens():
    """Rules after the token blocks use var(--...) so dark mode works (shadows and the scrim are the exceptions)."""
    css = read("styles.css")
    body = css.split("/* ---------- base ---------- */", 1)[1]
    hexes = re.findall(r"#[0-9A-Fa-f]{3,8}\b", body)
    assert hexes == []


def test_core_exposes_the_bm_contract():
    js = read("core.js")
    for name in ("api", "state", "registerView", "go", "openProperty", "closeDrawer", "toast", "refreshSummary",
                 "rerender", "esc", "fmt", "ui"):
        assert re.search(r"\b" + name + r"\b", js.split("const BM = {", 1)[1]), name
    for helper in ("moon", "levelPips", "channelBadges", "bars", "stat", "empty"):
        assert helper in js.split("ui: {", 1)[1].split("}", 1)[0], helper
    assert "localStorage" in js and "'bm.market'" in js
    assert "/api/login" in js and "/api/health" in js and "/api/markets" in js


def test_views_register_themselves():
    assert "BM.registerView('deals', {label: 'Deals', group: 'work', order: 1" in read("deals.js")
    assert "BM.renderProperty = " in read("property.js")


@pytest.mark.parametrize("name", SHELL_JS)
def test_javascript_parses(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    result = subprocess.run([node, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


HARNESS = r"""
const listeners = {};
global.window = {location: {hash: ''}, localStorage: {getItem: () => null, setItem: () => {}}};
global.document = {readyState: 'loading', addEventListener: (k, f) => { listeners[k] = f; },
  dispatchEvent: () => {}, querySelector: () => null};
global.history = {replaceState: () => {}};
require(process.argv[2]);
const BM = window.BM;
const f = BM.fmt, ui = BM.ui;
const out = {
  pct: [f.pct(0.126432), f.pct(0.051), f.pct(0), f.pct(null), f.pct(0.0004)],
  num: [f.num(2981), f.num(12.34), f.num(null)],
  money: [f.money(1250000), f.money(10000000), f.money(300000), f.money(1080), f.money(null)],
  minutes: [f.minutes(27.8, 'estimate'), f.minutes(27.2, 'google_routes'), f.minutes(null)],
  date: [f.date('1989-11-30'), f.date(null)],
  title: [f.title('77 FAKE MEADOW CT'), f.title('1200 SAMPLE RIDGE DR NE'), f.title('Already Mixed')],
  esc: BM.esc('<b a="1">&\''),
  pips: [ui.levelPips(1), ui.levelPips(3), ui.levelPips(2, 'dnc')],
  badges: ui.channelBadges({mail: 'untested', phone: 'verified', email: 'wrong', text: 'dnc'}),
  moonFull: ui.moon(0.3, 22), moonNone: ui.moon(null),
  bars: ui.bars([{label: 'Loaded', value: 200}, {label: 'In buy box', value: 50, cls: 'close', hint: 'est.'}], {drops: true}),
  stat: ui.stat(392, 'Properties in the buy box'),
  empty: ui.empty('No properties', 'Import <a file>', '<button>Go</button>'),
  booted: typeof listeners.DOMContentLoaded,
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def helpers(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path_factory.mktemp("node") / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(script), str(STATIC / "core.js")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_formatters(helpers):
    assert helpers["pct"] == ["13%", "5.1%", "0%", "—", "<0.1%"]
    assert helpers["num"] == ["2,981", "12.3", "—"]
    assert helpers["money"] == ["$1.25M", "$10M", "$300K", "$1,080", "—"]
    assert helpers["minutes"] == ["~28 min", "27 min", "—"]
    assert helpers["date"] == ["Nov 30, 1989", "—"]
    assert helpers["title"] == ["77 Fake Meadow Ct", "1200 Sample Ridge Dr NE", "Already Mixed"]
    assert helpers["esc"] == "&lt;b a=&quot;1&quot;&gt;&amp;&#39;"


def test_ui_helpers(helpers):
    one, three, dnc = helpers["pips"]
    assert 'class="lv l1"' in one and "No owner data" in one
    assert 'class="lv l3"' in three and "Confirmed owner" in three
    assert "dnc" in dnc and "Do not contact" in dnc
    badges = helpers["badges"]
    for cls, label in (("u", "Mail"), ("v", "Call"), ("w", "Email"), ("w dnc", "Text"), ("none", "Social"), ("none", "VM")):
        assert f'class="ch {cls}" title="{label}' in badges, (cls, label)
    assert "clipPath" in helpers["moonFull"] and 'fill="var(--accent)"' in helpers["moonFull"]
    assert 'fill="var(--accent)"' not in helpers["moonNone"]   # no score: an unlit moon
    bars = helpers["bars"]
    assert bars.count('class="hb"') == 2 and "25% move on" in bars and "<small>est.</small>" in bars
    assert 'width:100.0%' in bars and 'class="close"' in bars
    assert '<div class="v num">392</div>' in helpers["stat"]
    assert "Import &lt;a file&gt;" in helpers["empty"] and "<button>Go</button>" in helpers["empty"]
    assert helpers["booted"] == "function"     # boots on DOMContentLoaded, after every view registered
