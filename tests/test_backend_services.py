from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

import cookies_news_cockpit.services as services
from cookies_news_cockpit.models import FeedArticle
from cookies_news_cockpit.services import (
    DeepSeekClient,
    DeepSeekError,
    FeedError,
    FeedService,
    UnsafeUrlError,
)


class UnknownLengthStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class StalledStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes):
        self.first_chunk = first_chunk
        self.started = False
        self.closed = False

    async def __aiter__(self):
        self.started = True
        yield self.first_chunk
        await asyncio.Future()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_content", ["not-json", None, 42])
async def test_deepseek_retries_once_when_json_is_invalid(invalid_content: object) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            invalid_content
            if calls == 1
            else '{"score":88,"summary":"摘要","analysis":"分析"}'
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    transport = httpx.MockTransport(handler)
    client = DeepSeekClient(
        "sk-test-not-real",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    result = await client.analyze(
        FeedArticle(
            title="GPU",
            url="https://news.example/a",
            source_id="source",
            source_name="Source",
        ),
        {"name": "AI", "keywords": ["GPU"]},
    )
    assert calls == 2
    assert result.score == 88


@pytest.mark.asyncio
async def test_deepseek_retries_valid_json_with_invalid_analysis_schema() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            '{"score":999,"summary":"摘要","analysis":"分析"}'
            if calls == 1
            else '{"score":88,"summary":"摘要","analysis":"分析"}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = DeepSeekClient(
        "sk-test-not-real",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await client.analyze(
        FeedArticle(
            title="GPU",
            url="https://news.example/a",
            source_id="source",
            source_name="Source",
        ),
        {"name": "AI", "keywords": ["GPU"]},
    )

    assert calls == 2
    assert result.score == 88


@pytest.mark.asyncio
async def test_calibration_invalid_schema_becomes_deepseek_error_after_retry() -> None:
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
                                '{"keywords":["   "],"threshold":50,"rationale":"无关键词"}'
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

    with pytest.raises(DeepSeekError, match="返回无效结果"):
        await client.calibrate(
            {"name": "AI", "keywords": ["GPU"]},
            "优化主题",
            [],
            [],
        )

    assert calls == 2


@pytest.mark.asyncio
async def test_deepseek_retries_share_one_total_deadline() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.11)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "not-json"}}]},
            )
        await asyncio.sleep(0.11)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"score":88,"summary":"摘要","analysis":"分析"}'
                        }
                    }
                ]
            },
        )

    client = DeepSeekClient(
        "sk-test-not-real",
        total_timeout=0.2,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DeepSeekError, match="总时限"):
        await client.analyze(
            FeedArticle(
                title="GPU",
                url="https://news.example/a",
                source_id="source",
                source_name="Source",
            ),
            {"name": "AI", "keywords": ["GPU"]},
        )

    assert calls == 2


@pytest.mark.asyncio
async def test_deepseek_stalled_response_is_closed_on_total_timeout() -> None:
    stream = StalledStream(b'{"choices":[')
    client = DeepSeekClient(
        "sk-test-not-real",
        total_timeout=0.1,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    stream=stream,
                    headers={"Content-Type": "application/json"},
                )
            )
        ),
    )

    with pytest.raises(DeepSeekError, match="总时限"):
        await client.analyze(
            FeedArticle(
                title="GPU",
                url="https://news.example/a",
                source_id="source",
                source_name="Source",
            ),
            {"name": "AI", "keywords": ["GPU"]},
        )

    assert stream.started is True
    assert stream.closed is True


@pytest.mark.asyncio
async def test_feed_rejects_redirect_to_private_address(monkeypatch) -> None:
    checked: list[str] = []
    requested: list[str] = []

    def fake_validate(url: str) -> None:
        checked.append(url)
        if "127.0.0.1" in url:
            raise UnsafeUrlError("private redirect blocked")

    monkeypatch.setattr(services, "validate_public_http_url", fake_validate)

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        return httpx.Response(
            200,
            content=b"<rss><channel><item><title>x</title><link>https://x.example</link></item></channel></rss>",
            headers={"Content-Type": "application/rss+xml"},
        )

    transport = httpx.MockTransport(handler)
    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(transport=transport, follow_redirects=True),
        validate_urls=True,
    )
    with pytest.raises(UnsafeUrlError):
        await service.fetch(
            {"id": "source", "name": "Source", "url": "https://public.example/feed.xml"}
        )
    assert any("127.0.0.1" in url for url in checked)
    assert requested == ["https://public.example/feed.xml"]


@pytest.mark.asyncio
async def test_feed_resolves_relative_entry_links_and_skips_unsafe_links(monkeypatch) -> None:
    def fake_validate(url: str) -> None:
        if not url.startswith(("http://", "https://")) or "127.0.0.1" in url:
            raise UnsafeUrlError("unsafe article link")

    monkeypatch.setattr(services, "validate_public_http_url", fake_validate)
    feed = b"""<rss><channel>
      <item><title>Relative</title><link>/story/1</link></item>
      <item><title>Script</title><link>javascript:alert(1)</link></item>
      <item><title>Private</title><link>http://127.0.0.1/admin</link></item>
    </channel></rss>"""

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=feed,
            headers={"Content-Type": "application/rss+xml"},
        )
    )
    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        validate_urls=True,
    )
    articles = await service.fetch(
        {"id": "source", "name": "Source", "url": "https://public.example/feed.xml"}
    )
    assert [article.url for article in articles] == ["https://public.example/story/1"]


@pytest.mark.asyncio
async def test_feed_rejects_declared_content_length_over_limit(monkeypatch) -> None:
    monkeypatch.setattr(services, "FEED_MAX_BYTES", 8)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"x",
            headers={"Content-Length": "9", "Content-Type": "application/rss+xml"},
        )
    )
    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        validate_urls=False,
    )
    with pytest.raises(FeedError, match="安全上限"):
        await service.fetch(
            {"id": "source", "name": "Source", "url": "https://public.example/feed.xml"}
        )


@pytest.mark.asyncio
async def test_article_stream_over_actual_limit_returns_empty_text(monkeypatch) -> None:
    monkeypatch.setattr(services, "ARTICLE_MAX_BYTES", 8)
    feed = b"""<rss><channel><item><title>Story</title>
      <link>https://public.example/story</link></item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed.xml":
            return httpx.Response(
                200,
                content=feed,
                headers={"Content-Type": "application/rss+xml"},
            )
        return httpx.Response(
            200,
            stream=UnknownLengthStream(b"12345", b"67890"),
            headers={"Content-Type": "text/html"},
        )

    transport = httpx.MockTransport(handler)
    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        validate_urls=False,
    )
    articles = await service.fetch(
        {"id": "source", "name": "Source", "url": "https://public.example/feed.xml"},
        extract_full_text=True,
    )
    assert len(articles) == 1
    assert articles[0].full_text == ""


@pytest.mark.asyncio
async def test_feed_stream_has_total_wall_clock_deadline() -> None:
    stream = StalledStream(b"<rss><channel>")
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            stream=stream,
            headers={"Content-Type": "application/rss+xml"},
        )
    )
    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        validate_urls=False,
        feed_total_timeout=0.1,
    )

    with pytest.raises(FeedError, match="总时限"):
        await service.fetch(
            {"id": "source", "name": "Source", "url": "https://public.example/feed.xml"}
        )

    assert stream.started is True
    assert stream.closed is True


@pytest.mark.asyncio
async def test_feed_redirects_share_one_total_deadline() -> None:
    calls: list[str] = []
    feed = b"""<rss><channel><item><title>Story</title>
      <link>https://public.example/story</link></item></channel></rss>"""

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/feed.xml":
            await asyncio.sleep(0.11)
            return httpx.Response(302, headers={"Location": "/redirected.xml"})
        await asyncio.sleep(0.11)
        return httpx.Response(
            200,
            content=feed,
            headers={"Content-Type": "application/rss+xml"},
        )

    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        validate_urls=False,
        feed_total_timeout=0.2,
    )

    with pytest.raises(FeedError, match="总时限"):
        await service.fetch(
            {"id": "source", "name": "Source", "url": "https://public.example/feed.xml"}
        )

    assert calls == ["/feed.xml", "/redirected.xml"]


@pytest.mark.asyncio
async def test_article_total_deadline_degrades_to_feed_metadata() -> None:
    feed = b"""<rss><channel><item><title>Story</title>
      <link>https://public.example/story</link></item></channel></rss>"""
    article_stream = StalledStream(b"<html><body><article><p>partial text")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed.xml":
            return httpx.Response(
                200,
                content=feed,
                headers={"Content-Type": "application/rss+xml"},
            )
        return httpx.Response(
            200,
            stream=article_stream,
            headers={"Content-Type": "text/html"},
        )

    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        validate_urls=False,
        article_total_timeout=0.1,
    )
    articles = await service.fetch(
        {"id": "source", "name": "Source", "url": "https://public.example/feed.xml"},
        extract_full_text=True,
    )

    assert len(articles) == 1
    assert articles[0].full_text == ""
    assert article_stream.started is True
    assert article_stream.closed is True


@pytest.mark.asyncio
async def test_slow_entry_dns_is_skipped_without_blocking_later_links(monkeypatch) -> None:
    release_slow_dns = threading.Event()
    slow_dns_threads: list[threading.Thread] = []

    def fake_validate(url: str) -> None:
        if "slow.example" in url:
            slow_dns_threads.append(threading.current_thread())
            release_slow_dns.wait(timeout=5)

    monkeypatch.setattr(services, "validate_public_http_url", fake_validate)
    feed = b"""<rss><channel>
      <item><title>Slow</title><link>https://slow.example/story</link></item>
      <item><title>Good</title><link>https://good.example/story</link></item>
    </channel></rss>"""
    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    content=feed,
                    headers={"Content-Type": "application/rss+xml"},
                )
            )
        ),
        entry_url_timeout=0.1,
        entry_validation_budget=0.5,
    )

    try:
        articles = await asyncio.wait_for(
            service.fetch(
                {
                    "id": "source",
                    "name": "Source",
                    "url": "https://public.example/feed.xml",
                }
            ),
            timeout=1.0,
        )
    finally:
        release_slow_dns.set()

    assert [article.title for article in articles] == ["Good"]
    assert slow_dns_threads
    assert all(thread is not threading.main_thread() for thread in slow_dns_threads)


@pytest.mark.asyncio
async def test_entry_dns_validation_has_a_shared_source_budget(monkeypatch) -> None:
    release_slow_dns = threading.Event()
    slow_urls: list[str] = []

    def fake_validate(url: str) -> None:
        if ".slow.example" in url:
            slow_urls.append(url)
            release_slow_dns.wait(timeout=5)

    monkeypatch.setattr(services, "validate_public_http_url", fake_validate)
    slow_items = "".join(
        f"<item><title>Slow {index}</title>"
        f"<link>https://host{index}.slow.example/story</link></item>"
        for index in range(20)
    )
    feed = (
        "<rss><channel>"
        "<item><title>Good</title><link>https://good.example/story</link></item>"
        f"{slow_items}</channel></rss>"
    ).encode()
    service = FeedService(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    content=feed,
                    headers={"Content-Type": "application/rss+xml"},
                )
            )
        ),
        entry_url_timeout=0.1,
        entry_validation_budget=0.25,
    )

    try:
        articles = await asyncio.wait_for(
            service.fetch(
                {
                    "id": "source",
                    "name": "Source",
                    "url": "https://public.example/feed.xml",
                }
            ),
            timeout=1.0,
        )
    finally:
        release_slow_dns.set()

    assert [article.title for article in articles] == ["Good"]
    # A heavily loaded runner may consume the tiny synthetic budget on the
    # first worker dispatch; the contract is that validation starts but never
    # attempts all hostile hosts once the shared budget is exhausted.
    assert 0 < len(slow_urls) < 20
