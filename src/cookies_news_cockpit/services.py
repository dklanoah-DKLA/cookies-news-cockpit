from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from .models import AIAnalysis, CalibrationProposal, FeedArticle

USER_AGENT = "CookiesNewsCockpit/0.1 (+local personal reader)"
FEED_MAX_BYTES = 5 * 1024 * 1024
ARTICLE_MAX_BYTES = 8 * 1024 * 1024
FEED_TOTAL_TIMEOUT_SECONDS = 45.0
ARTICLE_TOTAL_TIMEOUT_SECONDS = 30.0
ENTRY_URL_TIMEOUT_SECONDS = 0.5
ENTRY_URL_VALIDATION_BUDGET_SECONDS = 5.0
DEEPSEEK_TOTAL_TIMEOUT_SECONDS = 45.0


class UnsafeUrlError(ValueError):
    pass


class FeedError(RuntimeError):
    pass


class DeepSeekError(RuntimeError):
    pass


def validate_public_http_url(url: str) -> None:
    """Reject non-web and local-network targets before server-side fetching."""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("只支持公开的 http/https 地址")
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".local"):
        raise UnsafeUrlError("不允许访问本机或局域网地址")
    try:
        addresses = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror as exc:
        raise UnsafeUrlError("无法解析该地址") from exc
    for record in addresses:
        raw = record[4][0].split("%", 1)[0]
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise UnsafeUrlError("不允许访问本机或局域网地址")


def _plain_text(raw: str, limit: int = 4000) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(html.unescape(raw), "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())[:limit]


class FeedService:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        feed_total_timeout: float = FEED_TOTAL_TIMEOUT_SECONDS,
        article_total_timeout: float = ARTICLE_TOTAL_TIMEOUT_SECONDS,
        entry_url_timeout: float = ENTRY_URL_TIMEOUT_SECONDS,
        entry_validation_budget: float = ENTRY_URL_VALIDATION_BUDGET_SECONDS,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        validate_urls: bool = True,
    ):
        self.timeout = timeout
        self.feed_total_timeout = feed_total_timeout
        self.article_total_timeout = article_total_timeout
        self.entry_url_timeout = entry_url_timeout
        self.entry_validation_budget = entry_validation_budget
        self.client_factory = client_factory
        self.validate_urls = validate_urls

    def _client(self) -> httpx.AsyncClient:
        if self.client_factory:
            return self.client_factory()
        return httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
                ),
            },
        )

    async def _get_public(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int,
        total_timeout: float,
    ) -> httpx.Response:
        try:
            async with asyncio.timeout(total_timeout):
                return await self._get_public_within_deadline(
                    client,
                    url,
                    headers=headers,
                    max_bytes=max_bytes,
                )
        except TimeoutError as exc:
            raise FeedError(f"请求超过 {total_timeout:g} 秒总时限") from exc

    async def _get_public_within_deadline(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str] | None,
        max_bytes: int,
    ) -> httpx.Response:
        current = url
        for _hop in range(6):
            if self.validate_urls:
                # DNS resolution is blocking on some platforms. Run it outside
                # the event-loop thread so the wall-clock deadline can expire.
                await asyncio.to_thread(validate_public_http_url, current)
            request = client.build_request("GET", current, headers=headers)
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                if self.validate_urls:
                    await asyncio.to_thread(validate_public_http_url, str(response.url))
                if not response.is_redirect:
                    return await self._bounded_response(response, max_bytes)

                location = response.headers.get("location")
                if not location:
                    raise FeedError("新闻源返回了无目标的重定向")
                current = urljoin(str(response.url), location)
                if self.validate_urls:
                    # Validate before issuing the redirected request. This is
                    # inside the same deadline as every other redirect hop.
                    await asyncio.to_thread(validate_public_http_url, current)
            finally:
                await response.aclose()
        raise FeedError("新闻源重定向次数过多")

    @staticmethod
    async def _bounded_response(response: httpx.Response, max_bytes: int) -> httpx.Response:
        declared = response.headers.get("content-length")
        if declared:
            try:
                if int(declared) > max_bytes:
                    await response.aclose()
                    raise FeedError(f"响应超过 {max_bytes // (1024 * 1024)} MiB 安全上限")
            except ValueError:
                pass

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise FeedError(f"响应超过 {max_bytes // (1024 * 1024)} MiB 安全上限")
            body.extend(chunk)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
            extensions=response.extensions,
        )

    @staticmethod
    async def _entry_url_is_safe(url: str, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                await asyncio.to_thread(validate_public_http_url, url)
        except (TimeoutError, UnsafeUrlError, ValueError):
            return False
        return True

    @staticmethod
    def _entry_url_cache_key(url: str) -> tuple[str, str, int | None] | None:
        try:
            parsed = urlsplit(url)
            if not parsed.hostname:
                return None
            return parsed.scheme.casefold(), parsed.hostname.casefold(), parsed.port
        except ValueError:
            return None

    async def fetch(
        self, source: dict[str, Any], extract_full_text: bool = False
    ) -> list[FeedArticle]:
        url = str(source["url"])
        try:
            async with self._client() as client:
                response = await self._get_public(
                    client,
                    url,
                    max_bytes=FEED_MAX_BYTES,
                    total_timeout=self.feed_total_timeout,
                )
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                if getattr(parsed, "bozo", False) and not parsed.entries:
                    raise FeedError(str(getattr(parsed, "bozo_exception", "Feed 解析失败")))
                articles: list[FeedArticle] = []
                entry_validation_remaining = self.entry_validation_budget
                entry_validation_cache: dict[tuple[str, str, int | None], bool] = {}
                for entry in parsed.entries[:100]:
                    title = _plain_text(str(entry.get("title", "")), 500)
                    link = urljoin(str(response.url), str(entry.get("link", "")).strip())
                    if not title or not link:
                        continue
                    if self.validate_urls:
                        cache_key = self._entry_url_cache_key(link)
                        if cache_key is None:
                            continue
                        safe = entry_validation_cache.get(cache_key)
                        if safe is None:
                            if entry_validation_remaining <= 0:
                                break
                            started = asyncio.get_running_loop().time()
                            safe = await self._entry_url_is_safe(
                                link,
                                min(self.entry_url_timeout, entry_validation_remaining),
                            )
                            entry_validation_remaining -= (
                                asyncio.get_running_loop().time() - started
                            )
                            entry_validation_cache[cache_key] = safe
                        if not safe:
                            # Feed content is untrusted and becomes a clickable
                            # link in the cockpit, even when extraction is off.
                            continue
                    summary = _plain_text(
                        str(entry.get("summary", entry.get("description", ""))), 4000
                    )
                    published = entry.get("published") or entry.get("updated")
                    full_text = ""
                    if extract_full_text:
                        full_text = await self._extract(client, link)
                    articles.append(
                        FeedArticle(
                            title=title,
                            url=link,
                            excerpt=summary,
                            published_at=str(published) if published else None,
                            source_id=source["id"],
                            source_name=source["name"],
                            full_text=full_text,
                        )
                    )
                if not articles:
                    raise FeedError("Feed 中没有可用文章")
                return articles
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, UnsafeUrlError):
                raise
            raise FeedError(str(exc)) from exc

    async def _extract(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            response = await self._get_public(
                client,
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                max_bytes=ARTICLE_MAX_BYTES,
                total_timeout=self.article_total_timeout,
            )
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", ""):
                return ""
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "aside"]):
                tag.decompose()
            root = soup.find("article") or soup.find("main") or soup.body
            if root is None:
                return ""
            paragraphs = [
                " ".join(item.get_text(" ", strip=True).split()) for item in root.find_all("p")
            ]
            return "\n\n".join(item for item in paragraphs if len(item) >= 20)[:24000]
        except (FeedError, httpx.HTTPError, ValueError, UnsafeUrlError):
            # Extraction is optional; feed metadata remains usable.
            return ""

    async def validate(self, source: dict[str, Any]) -> dict[str, Any]:
        articles = await self.fetch(source, extract_full_text=False)
        return {
            "ok": True,
            "entry_count": len(articles),
            "sample_title": articles[0].title,
        }


def _json_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError("expected JSON text")
    raw = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


class DeepSeekClient:
    base_url = "https://api.deepseek.com/chat/completions"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 45.0,
        total_timeout: float = DEEPSEEK_TOTAL_TIMEOUT_SECONDS,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.api_key = api_key
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.client_factory = client_factory

    def _client(self) -> httpx.AsyncClient:
        if self.client_factory:
            return self.client_factory()
        return httpx.AsyncClient(timeout=self.timeout)

    async def _complete_json[ValidatedJson](
        self,
        system: str,
        user: str,
        validator: Callable[[dict[str, Any]], ValidatedJson],
    ) -> ValidatedJson:
        last_error: Exception | None = None
        try:
            async with asyncio.timeout(self.total_timeout):
                for _attempt in range(2):
                    try:
                        async with self._client() as client:
                            response = await client.post(
                                self.base_url,
                                headers={
                                    "Authorization": f"Bearer {self.api_key}",
                                    "Content-Type": "application/json",
                                },
                                json={
                                    "model": "deepseek-chat",
                                    "messages": [
                                        {"role": "system", "content": system},
                                        {"role": "user", "content": user},
                                    ],
                                    "response_format": {"type": "json_object"},
                                    "temperature": 0.1,
                                    "stream": False,
                                },
                            )
                            response.raise_for_status()
                            payload = response.json()
                            content = payload["choices"][0]["message"]["content"]
                            # Schema validation belongs inside the retry
                            # boundary. Valid JSON with missing/out-of-range
                            # fields is still an invalid model response.
                            return validator(_json_object(content))
                    except (
                        httpx.HTTPError,
                        KeyError,
                        IndexError,
                        TypeError,
                        json.JSONDecodeError,
                        ValueError,
                    ) as exc:
                        last_error = exc
        except TimeoutError as exc:
            raise DeepSeekError(
                f"DeepSeek 请求超过 {self.total_timeout:g} 秒总时限"
            ) from exc
        raise DeepSeekError(f"DeepSeek 返回无效结果：{last_error}")

    async def analyze(self, article: FeedArticle, topic: dict[str, Any]) -> AIAnalysis:
        system = (
            "你是私人新闻驾驶舱的严谨编辑。只返回 JSON 对象，字段为："
            "score(0-100整数)、summary(简短中文摘要)、analysis(为何与主题相关及影响)。"
            "不要编造输入中没有的事实。"
        )
        text = (article.full_text or article.excerpt)[:12000]
        user = json.dumps(
            {
                "topic": topic["name"],
                "keywords": topic["keywords"],
                "title": article.title,
                "source": article.source_name,
                "content": text,
            },
            ensure_ascii=False,
        )
        return await self._complete_json(system, user, AIAnalysis.model_validate)

    async def calibrate(
        self,
        topic: dict[str, Any],
        goal: str,
        positive_examples: list[str],
        negative_examples: list[str],
    ) -> CalibrationProposal:
        system = (
            "你帮助用户校准新闻主题。只返回 JSON 对象，字段为："
            "keywords(1-40个短语)、threshold(0-100整数)、rationale(中文说明)。"
            "建议必须保守、可由用户确认，不能直接改变配置。"
        )
        user = json.dumps(
            {
                "topic": topic,
                "goal": goal,
                "positive_examples": positive_examples,
                "negative_examples": negative_examples,
            },
            ensure_ascii=False,
        )
        return await self._complete_json(system, user, CalibrationProposal.model_validate)
