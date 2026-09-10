from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "cookies_news_cockpit" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_report_is_split_into_core_and_supplement_with_snapshot_thresholds() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    assert "核心新闻" in javascript
    assert "补充阅读" in javascript
    assert 'className: "selection-label"' in javascript
    assert '"core_threshold", "threshold"' in javascript
    assert '"supplement_threshold", "extended_threshold"' in javascript
    assert "score >= coreThreshold" in javascript
    assert "score >= supplementThreshold" in javascript
    assert 'id="latest-list"' in html


def test_imported_report_tier_uses_only_a_unique_case_insensitive_topic_name_fallback() -> None:
    javascript = _read("app.js")

    assert "function topicFunnelFor(report, article)" in javascript
    assert "article?.topic_name" in javascript
    assert "snapshot.topic_name" in javascript
    assert "name.trim().toLocaleLowerCase() === topicName" in javascript
    assert "matches.length === 1 ? matches[0] : null" in javascript
    assert "topicFunnelFor(report, article)" in javascript


def test_retained_effective_report_shows_both_run_and_report_time() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    assert 'id="report-context"' in html
    assert "本次新增 0 条 · 当前为上次有效报告" in javascript
    assert "本次任务：" in javascript
    assert "当前报告：" in javascript
    assert "selected_new" not in javascript
    assert "report?.generated_at || report?.created_at" in javascript


def test_funnel_has_three_stages_canonical_fields_and_legacy_aliases() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    for label in ("发现", "处理", "结果"):
        assert f">{label}</h3>" in html
    for field in (
        "unique_candidates",
        "fulltext_fetches",
        "fulltext_success",
        "semantic_reviewed",
        "semantic_initial_reviewed",
        "semantic_additional_reviewed",
        "ai_requests",
        "ai_success",
        "ai_failure",
        "core_selected",
        "supplement_selected",
        "below_supplement",
        "semantic_budget_exhausted",
        "history_duplicates",
        "run_duplicates",
        "selected",
    ):
        assert field in javascript
    for legacy in (
        "extended_selected",
        "below_extended",
        "budget_exhausted",
        "duplicates_skipped",
    ):
        assert legacy in javascript
    assert 'id="funnel-breakdown"' in html
    assert '"per_topic", "by_topic", "topic_funnels"' in javascript
    assert '"per_source", "by_source", "source_funnels"' in javascript
    assert "处理与淘汰原因" in javascript
    assert "function duplicateCount(run)" in javascript
    assert "duplicateCount(state.currentRun)" in javascript
    assert 'const historyDuplicates = funnelNumber(run, "history_duplicates")' in javascript
    assert 'const runDuplicates = funnelNumber(run, "run_duplicates")' in javascript


def test_smart_supplement_copy_is_bounded_and_keyword_suggestions_require_confirmation() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    assert "结果不足时智能补充" in html
    assert "首批最多 20 条" in html
    assert "最多追加 15 条" in html
    assert "零命中时使用语义兜底" not in html
    assert "不会自动修改；确认后才加入草稿" in html
    assert "确认加入草稿" in html
    assert "关键词建议已加入草稿；保存主题后才会生效" in javascript
    assert 'refs.applyTopicSuggestion.addEventListener("click", applyTopicSuggestion)' in javascript


def test_no_relevant_after_ai_has_a_specific_non_threshold_empty_state() -> None:
    javascript = _read("app.js")

    assert 'no_relevant_after_ai: ["智能复核后仍没有相关新闻"' in javascript
    assert "没有足够证据与主题相关" in javascript
    assert "可以补充更具体的关键词、别名或可靠来源后再试" in javascript


def test_portable_settings_restore_is_explicit_and_opt_in() -> None:
    html = _read("index.html")
    javascript = _read("app.js")

    assert 'id="import-portable-settings" type="checkbox"' in html
    assert "恢复便携设置" in html
    assert "默认不恢复" in html
    assert "门槛、数量、时效、刷新间隔、全文与智能补充设置" in html
    assert "API Key 永远不会从备份导入" in html
    assert "DeepSeek 连接状态与首次设置状态也不会导入" in html
    assert "refs.importPortableSettings.checked = false" in javascript
    assert "import_portable_settings: refs.importPortableSettings.checked" in javascript
