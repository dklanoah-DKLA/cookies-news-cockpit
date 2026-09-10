from __future__ import annotations

from cookies_news_cockpit.dedup import DuplicateIndex


def test_policy_stage_change_is_not_treated_as_a_repeat() -> None:
    index = DuplicateIndex()
    index.add(
        "https://policy.example/measure",
        "欧盟碳边境政策草案计划于十月开始征求意见",
    )

    assert not index.is_duplicate(
        "https://policy.example/measure-update",
        "欧盟碳边境政策已通过并将于十月正式生效",
    )


def test_english_execution_stage_change_is_not_treated_as_a_repeat() -> None:
    index = DuplicateIndex()
    index.add(
        "https://trade.example/tariff",
        "New appliance tariff proposed for October",
    )

    assert not index.is_duplicate(
        "https://trade.example/tariff-update",
        "New appliance tariff effective in October",
    )


def test_changed_deadline_is_not_treated_as_a_repeat() -> None:
    index = DuplicateIndex()
    index.add("https://news.example/rule", "新能效规则将于2026年10月1日执行")

    assert not index.is_duplicate(
        "https://news.example/rule-update",
        "新能效规则推迟至2027年1月1日执行",
    )
