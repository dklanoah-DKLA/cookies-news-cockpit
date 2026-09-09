from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "cookies_news_cockpit" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_first_run_wizard_has_all_four_skippable_steps() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    assert 'id="onboarding-dialog"' in html
    assert html.count("data-wizard-step=") == 4
    for label in ("来源", "主题", "DeepSeek", "运行"):
        assert f">{label}</li>" in html
    assert 'id="skip-onboarding"' in html
    assert 'id="wizard-skip-step"' in html
    assert "onboarding_completed" in javascript


def test_topic_draft_supports_exclusions_chips_and_ai_preview() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    for control in (
        "topic-keyword-count",
        "topic-keyword-chips",
        "topic-excludes",
        "topic-exclude-count",
        "topic-exclude-chips",
        "suggest-topic",
        "topic-suggestion",
        "apply-topic-suggestion",
    ):
        assert f'id="{control}"' in html
    assert "exclusion_keywords" in javascript
    assert "/api/topics/suggest" in javascript
    assert "[，,、;；\\r\\n]" in javascript
    assert "请至少填写一个包含关键词" in javascript


def test_source_draft_uses_distinct_homepage_feed_and_metadata_fields() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    for control in (
        "source-homepage",
        "source-url",
        "source-category",
        "source-language",
        "source-preset",
        "source-terms",
        "validate-source-draft",
    ):
        assert f'id="{control}"' in html
    assert 'api("/api/sources/validate"' in javascript
    assert '<option value="world">国际</option>' in html
    assert '<option value="science">科学</option>' in html
    assert '<option value="other">其他</option>' in html
    assert 'querySelectorAll(\'input[type="checkbox"]:checked:not(:disabled)\')' in javascript
    assert "（已停用）" in javascript


def test_live_run_contract_has_funnel_cancel_and_race_guards() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    for control in (
        "cancel-run",
        "run-funnel",
        "funnel-fetched",
        "funnel-fresh",
        "funnel-matched",
        "funnel-fulltext",
        "funnel-duplicates",
        "funnel-ai",
        "funnel-threshold",
        "funnel-kept",
    ):
        assert f'id="{control}"' in html
    assert "/cancel`" in javascript
    assert "runStarting" in javascript
    assert "runCancelling" in javascript
    assert 'filtering: "正在筛选时效与关键词"' in javascript
    assert "run?.analyses ?? run?.analysis_count" in javascript
    assert "analysis_mode" in javascript
    assert "matched_keywords" in javascript
    assert '/topics/${encodeURIComponent(topic.id)}/restore`' in javascript
    assert "all_sources_failed" in javascript
    assert "no_fresh_articles" in javascript
    assert "no_keyword_match" in javascript
    assert "below_threshold" in javascript


def test_settings_expose_freshness_and_bounded_semantic_fallback() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    for control in ("setting-freshness", "setting-semantic", "setting-semantic-limit"):
        assert f'id="{control}"' in html
    assert "freshness_days" in javascript
    assert "semantic_fallback_enabled" in javascript
    assert "semantic_fallback_limit" in javascript


def test_import_is_preview_then_explicit_safe_merge() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    assert 'id="import-file"' in html
    assert 'accept=".zip,application/zip"' in html
    assert "包含配置、全部文章历史与任务记录，不含 API Key" in html
    assert 'id="import-dialog"' in html
    assert 'id="apply-import"' in html
    assert "/api/import/preview" in javascript
    assert "/apply`" in javascript
    assert '["新增任务记录", summary.runs_new' in javascript
    assert 'strategy: "merge_keep_local"' in javascript
    assert "API Key 永远不会从备份导入" in html


def test_deepseek_four_states_and_shutdown_fallback_are_explicit() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    for status in ("unconfigured", "saved_unverified", "connected", "error"):
        assert status in javascript
    assert "/api/settings/deepseek/test" in javascript
    assert 'result?.status === "error"' in javascript
    assert "failurePayload.persisted_status" in javascript
    assert 'id="deepseek-test-result"' in html
    assert "单次运行最多分析 500 条候选" in html
    assert "500 次调用" not in html
    assert 'id="shutdown-state"' in html
    assert 'id="shutdown-copy"' in html
    assert "浏览器没有允许自动关闭" in javascript
    assert "100 * 1024 * 1024" in javascript
    assert "disabledAfter: true" in javascript
