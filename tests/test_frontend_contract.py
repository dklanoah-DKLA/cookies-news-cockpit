from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "cookies_news_cockpit" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_frontend_assets_are_packaged_and_loaded_without_inline_code() -> None:
    html = _read("index.html")

    assert (STATIC / "app.css").is_file()
    assert (STATIC / "tokens.css").is_file()
    assert (STATIC / "app.js").is_file()
    assert '<link rel="stylesheet" href="./app.css">' in html
    assert '<script src="./app.js" defer></script>' in html
    assert not re.search(r"<script(?![^>]+src=)", html, flags=re.IGNORECASE)
    assert not re.search(r"\sstyle\s*=", html, flags=re.IGNORECASE)


def test_cookie_background_is_decorative_and_not_a_dense_tile() -> None:
    html = _read("index.html")

    assert '<div class="cookie-field" aria-hidden="true">' in html
    assert 8 <= html.count(">🍪</span>") <= 16


def test_browser_session_token_and_external_links_are_hardened() -> None:
    javascript = _read("app.js")

    assert "window.location.hash" in javascript
    assert 'window.history.replaceState(null, "", "/")' in javascript
    assert 'headers.set("X-Cockpit-Token", sessionToken)' in javascript
    assert '["http:", "https:"]' in javascript
    assert 'link.rel = "noopener noreferrer"' in javascript
    assert 'link.target = "_blank"' in javascript
    assert "element.innerHTML = value" not in javascript
    assert ".style." not in javascript


def test_interface_matches_the_feed_only_v1_contract() -> None:
    combined = _read("index.html") + _read("app.js")

    assert "RSS / Atom" in combined
    assert "JSON API" not in combined
    assert "retention_days" not in combined
    assert "max_analysis" not in combined
    assert "deepseek-v4-flash" in combined
    for setting in (
        "default_threshold",
        "default_article_limit",
        "refresh_minutes",
        "scheduler_enabled",
        "extract_full_text",
    ):
        assert setting in combined


def test_cross_day_duplicate_review_is_visible_by_default() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    assert '<dd id="run-dedupe">默认 · 近 7 天</dd>' in html
    assert "duplicates_skipped" in javascript
    assert "已拦截" in javascript


def test_css_uses_hallmark_tokens_and_layout_safety_rules() -> None:
    css = _read("app.css")
    first_line = css.splitlines()[0]

    assert first_line.startswith("/* Hallmark · pre-emit critique:")
    assert "macrostructure: Workbench" in first_line
    assert '@import url("./tokens.css");' in css
    assert re.search(r"html,\s*\nbody\s*\{[^}]*overflow-x:\s*clip", css, re.DOTALL)
    assert "transition: all" not in css
    assert "transition-all" not in css
    assert "100vw" not in css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(", css)
    font_lines = [line.strip() for line in css.splitlines() if "font-family:" in line]
    assert font_lines
    assert all("font-family: var(" in line for line in font_lines)
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_mobile_first_breakpoints_cover_required_viewports() -> None:
    css = _read("app.css")

    assert "@media (min-width:" in css
    assert "20rem" in css
    assert len(re.findall(r"@media \(min-width:", css)) >= 2
