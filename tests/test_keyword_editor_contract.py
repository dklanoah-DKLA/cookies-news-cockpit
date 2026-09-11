from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

STATIC = Path(__file__).resolve().parents[1] / "src" / "cookies_news_cockpit" / "static"


def _markup() -> BeautifulSoup:
    return BeautifulSoup((STATIC / "index.html").read_text(encoding="utf-8"), "html.parser")


@pytest.mark.parametrize("canonical_id", ["topic-keywords", "topic-excludes"])
def test_canonical_keyword_values_are_hidden_and_cannot_block_native_form_validation(
    canonical_id: str,
):
    field = _markup().find(id=canonical_id)
    assert field is not None
    assert field.name == "textarea"
    assert field.has_attr("hidden")
    assert not field.has_attr("required")


@pytest.mark.parametrize("prefix", ["topic-keyword", "topic-exclude"])
def test_both_keyword_groups_have_editor_mount_and_separate_counts(prefix: str):
    markup = _markup()
    assert markup.find(id=f"{prefix}-editor") is not None
    assert markup.find(id=f"{prefix}-chips") is not None
    assert markup.find(id=f"{prefix}-count") is not None


def test_save_error_is_inline_accessible_and_hidden_initially():
    error = _markup().find(id="topic-save-error")
    assert error is not None
    assert error.get("role") == "alert"
    assert error.has_attr("hidden")


def test_keyword_component_is_loaded_before_cockpit_initialization():
    script_sources = [script.get("src", "") for script in _markup().find_all("script")]
    editor = next(
        index for index, src in enumerate(script_sources) if src.endswith("keyword-editor.js")
    )
    cockpit = next(index for index, src in enumerate(script_sources) if src.endswith("app.js"))
    assert editor < cockpit


def test_keyword_component_keeps_safe_draft_boundaries_and_accessible_controls():
    component = (STATIC / "keyword-editor.js").read_text(encoding="utf-8")
    for suffix in ("-entry", "-add", "-cancel-edit", "-bulk", "-bulk-add", "-message"):
        assert suffix in component
    assert 'role: "status"' in component
    assert '"aria-invalid"' in component
    assert '"aria-label": `编辑${this.label}：${word}`' in component
    assert '"aria-label": `删除${this.label}：${word}`' in component
    assert "event.isComposing" in component
    assert "event.keyCode !== 229" in component
    assert "next.length > 40" in component
    assert "slice(0, 40)" not in component
    for write_boundary in ("fetch(", "localStorage", "sessionStorage", "indexedDB"):
        assert write_boundary not in component
