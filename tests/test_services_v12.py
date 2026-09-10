from __future__ import annotations

import json

import httpx
import pytest

from cookies_news_cockpit.models import FeedArticle
from cookies_news_cockpit.services import USER_AGENT, DeepSeekClient, DeepSeekError


@pytest.mark.asyncio
async def test_v12_analysis_prompt_has_fixed_rubric_and_exclusions() -> None:
    request_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "relevant": True,
                                    "reason": "contextual_match",
                                    "score": 62,
                                    "summary": "摘要",
                                    "analysis": "分析",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = DeepSeekClient(
        "sk-test-not-real",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.analyze(
        FeedArticle(
            title="Sustainability report",
            url="https://news.example/esg",
            source_id="source",
            source_name="Source",
        ),
        {
            "name": "ESG",
            "keywords": ["ESG", "ISSB"],
            "exclusion_keywords": ["招聘"],
        },
    )

    messages = request_body["messages"]
    system = messages[0]["content"]
    task = json.loads(messages[1]["content"])["task"]
    assert "80-100" in system
    assert "60-79" in system
    assert "40-59" in system
    assert "0-39" in system
    assert "relevant" in system
    assert "insufficient_evidence" in system
    assert task == {
        "topic": "ESG",
        "keywords": ["ESG", "ISSB"],
        "exclusion_keywords": ["招聘"],
    }
    assert result.relevant is True
    assert result.reason == "contextual_match"


@pytest.mark.asyncio
async def test_live_analysis_missing_relevance_fields_is_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            '{"score":88,"summary":"摘要","analysis":"旧格式"}'
            if calls == 1
            else (
                '{"relevant":false,"reason":"off_topic",'
                '"score":18,"summary":"摘要","analysis":"新版格式"}'
            )
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    client = DeepSeekClient(
        "sk-test-not-real",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await client.analyze(
        FeedArticle(
            title="Context",
            url="https://news.example/context",
            source_id="source",
            source_name="Source",
        ),
        {"name": "ESG", "keywords": ["ESG"]},
    )

    assert calls == 2
    assert client.last_request_count == 2
    assert result.relevant is False
    assert result.reason == "off_topic"


@pytest.mark.asyncio
async def test_live_analysis_missing_relevance_fields_fails_after_retry() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"score":88,"summary":"摘要",'
                                '"analysis":"始终缺少必填字段"}'
                            )
                        }
                    }
                ]
            },
        )

    client = DeepSeekClient(
        "sk-test-not-real",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DeepSeekError, match="missing required analysis fields"):
        await client.analyze(
            FeedArticle(
                title="Context",
                url="https://news.example/context",
                source_id="source",
                source_name="Source",
            ),
            {"name": "ESG", "keywords": ["ESG"]},
        )

    assert calls == 2
    assert client.last_request_count == 2


def test_user_agent_reports_v12_local_personal_reader() -> None:
    assert USER_AGENT == (
        "CookiesNewsCockpit/1.2 "
        "(+https://github.com/dklanoah-DKLA/cookies-news-cockpit; local personal reader)"
    )


@pytest.mark.asyncio
async def test_calibration_requests_aliases_abbreviations_and_standard_names() -> None:
    request_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "keywords": ["环境、社会和治理", "ESG"],
                                    "threshold": 60,
                                    "rationale": "供用户预览确认",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = DeepSeekClient(
        "sk-test-not-real",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await client.calibrate(
        {"name": "ESG", "keywords": ["ESG"]},
        "追踪监管和披露动态",
        [],
        [],
    )

    system = request_body["messages"][0]["content"]
    assert "中文别名" in system
    assert "英文别名" in system
    assert "缩写" in system
    assert "标准名称" in system
    assert "用户确认" in system
    assert "不能直接改变配置" in system
