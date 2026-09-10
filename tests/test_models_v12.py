from __future__ import annotations

import pytest
from pydantic import ValidationError

from cookies_news_cockpit.models import AIAnalysis, ImportApplyInput, TopicInput


def test_topic_keywords_accept_ascii_chinese_and_line_break_separators() -> None:
    topic = TopicInput(
        name="ESG",
        keywords=["carbon, emissions，碳排放\nnet zero、净零；climate; 气候"],
    )

    assert topic.keywords == [
        "carbon",
        "emissions",
        "碳排放",
        "net zero",
        "净零",
        "climate",
        "气候",
    ]


def test_portable_settings_import_is_opt_in() -> None:
    assert ImportApplyInput().import_portable_settings is False
    assert ImportApplyInput(import_portable_settings=True).import_portable_settings is True


def test_ai_analysis_keeps_legacy_payloads_compatible() -> None:
    analysis = AIAnalysis(score=72, summary="摘要", analysis="分析")

    assert analysis.relevant is True
    assert analysis.reason == "direct_match"


def test_ai_analysis_rejects_non_standard_reason() -> None:
    with pytest.raises(ValidationError):
        AIAnalysis(
            score=72,
            summary="摘要",
            analysis="分析",
            relevant=True,
            reason="maybe",
        )
