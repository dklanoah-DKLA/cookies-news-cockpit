from __future__ import annotations

import argparse
import calendar
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx

from cookies_news_cockpit.presets import SOURCE_PRESETS_BY_URL, SourcePreset
from cookies_news_cockpit.services import FEED_MAX_BYTES, USER_AGENT

CATALOG_V2_SOURCE_IDS = (
    "preset-sec-press-releases",
    "preset-sec-speeches-statements",
    "preset-eu-environment-news",
    "preset-eu-trade-news",
    "preset-bis-media-releases",
    "preset-nasa-news-releases",
    "preset-nasa-technology",
    "preset-techcrunch",
)

# The IMF's official RSS directory and tested news/blog feeds returned HTTP 403
# to the application's client on 2026-09-10.  BIS is an explicit same-category
# fallback, not an IMF-branded source.
REPLACEMENTS = {
    "preset-bis-media-releases": "IMF News/Blog (403 to app client)",
}


def _timestamp(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), UTC)
    return None


def inspect_feed(
    payload: bytes,
    *,
    feed_url: str,
    content_type: str = "",
    checked_at: datetime | None = None,
    max_age_days: int = 60,
) -> dict[str, Any]:
    """Inspect a bounded RSS/Atom document without performing network I/O."""

    checked_at = checked_at or datetime.now(UTC)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    checked_at = checked_at.astimezone(UTC)
    parsed = feedparser.parse(payload)
    entries = list(parsed.entries)
    sample = entries[:20]
    timestamps = [value for value in (_timestamp(entry) for entry in sample) if value]
    titles = sum(bool(str(entry.get("title", "")).strip()) for entry in sample)
    links = sum(
        urlsplit(str(entry.get("link", ""))).scheme in {"http", "https"} for entry in sample
    )
    latest = max(timestamps) if timestamps else None
    errors: list[str] = []
    feed_format: str | None = None
    try:
        root_name = ET.fromstring(payload).tag.rsplit("}", 1)[-1].casefold()
        if root_name in {"rss", "rdf"}:
            feed_format = "rss"
        elif root_name == "feed":
            feed_format = "atom"
    except ET.ParseError as exc:
        errors.append(f"invalid XML: {exc}")
    if not feed_format or not str(getattr(parsed, "version", "")).startswith(("rss", "atom")):
        errors.append("document is not a recognized RSS/Atom feed")
    normalized_type = content_type.partition(";")[0].strip().casefold()
    if normalized_type and not any(token in normalized_type for token in ("rss", "atom", "xml")):
        errors.append(f"content type is not RSS/Atom/XML: {normalized_type}")
    if len(payload) > FEED_MAX_BYTES:
        errors.append(f"document exceeds {FEED_MAX_BYTES} bytes")
    if not entries:
        errors.append("feed has no entries")
    if sample and titles != len(sample):
        errors.append("sample contains an entry without a title")
    if sample and links != len(sample):
        errors.append("sample contains an entry without an http(s) link")
    if sample and len(timestamps) != len(sample):
        errors.append("sample contains an entry without a published/updated date")
    if latest is None and entries:
        errors.append("feed has no dated entries in the sample")
    elif latest and latest < checked_at - timedelta(days=max_age_days):
        errors.append(f"latest entry is older than {max_age_days} days")
    elif latest and latest > checked_at + timedelta(days=2):
        errors.append("latest entry is more than two days in the future")
    if getattr(parsed, "bozo", False) and not entries:
        errors.append(f"feed parse error: {parsed.bozo_exception}")

    return {
        "ok": not errors,
        "url": feed_url,
        "content_type": content_type,
        "bytes": len(payload),
        "within_app_size_limit": len(payload) <= FEED_MAX_BYTES,
        "format": feed_format,
        "feedparser_version": str(getattr(parsed, "version", "")),
        "feed_title": str(parsed.feed.get("title", "")).strip(),
        "entries": len(entries),
        "sample_size": len(sample),
        "sample_titles": titles,
        "sample_http_links": links,
        "sample_dates": len(timestamps),
        "latest_published": latest.isoformat() if latest else None,
        "checked_at": checked_at.isoformat(),
        "freshness_limit_days": max_age_days,
        "errors": errors,
    }


def probe_browser_link(url: str, *, timeout: float) -> dict[str, Any]:
    """Record a human-facing metadata link without making it a feed gate."""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
        )
    }
    try:
        with (
            httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client,
            client.stream("GET", url) as response,
        ):
            return {
                "status_code": response.status_code,
                "url": str(response.url),
                "purpose": "human browser reference; not fetched by the news pipeline",
            }
    except httpx.HTTPError as exc:
        return {
            "status_code": None,
            "url": url,
            "purpose": "human browser reference; not fetched by the news pipeline",
            "error": str(exc),
        }


def fetch_bounded_feed(preset: SourcePreset, *, timeout: float) -> dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    with (
        httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client,
        client.stream("GET", preset.url) as response,
    ):
        response.raise_for_status()
        parts: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > FEED_MAX_BYTES:
                raise ValueError(f"response exceeds {FEED_MAX_BYTES} bytes")
            parts.append(chunk)
        effective_url = str(response.url)
        if urlsplit(effective_url).scheme != "https":
            raise ValueError(f"redirected to a non-HTTPS URL: {effective_url}")
        result = inspect_feed(
            b"".join(parts),
            feed_url=effective_url,
            content_type=response.headers.get("content-type", ""),
        )
        result.update(
            {
                "id": preset.id,
                "name": preset.name,
                "homepage": preset.homepage,
                "terms": preset.terms,
                "replacement_for": REPLACEMENTS.get(preset.id),
                "status_code": response.status_code,
                "authentication": "not required (HTTP 200)",
            }
        )
        return result


def _catalog_v2_presets() -> list[SourcePreset]:
    by_id = {preset.id: preset for preset in SOURCE_PRESETS_BY_URL.values()}
    return [by_id[source_id] for source_id in CATALOG_V2_SOURCE_IDS]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only health audit for Cookies News Cockpit catalog v2 feeds."
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--fixture-url", default="https://fixture.example/feed.xml")
    args = parser.parse_args()

    if args.fixture:
        print(
            json.dumps(
                inspect_feed(args.fixture.read_bytes(), feed_url=args.fixture_url),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    selected = _catalog_v2_presets()
    if args.source_id:
        wanted = set(args.source_id)
        selected = [preset for preset in selected if preset.id in wanted]
        missing = wanted.difference(preset.id for preset in selected)
        if missing:
            parser.error(f"unknown catalog v2 source id(s): {', '.join(sorted(missing))}")

    results: list[dict[str, Any]] = []
    link_cache: dict[str, dict[str, Any]] = {}
    for preset in selected:
        try:
            result = fetch_bounded_feed(preset, timeout=args.timeout)
        except (httpx.HTTPError, ValueError) as exc:
            result = {
                "ok": False,
                "id": preset.id,
                "name": preset.name,
                "url": preset.url,
                "homepage": preset.homepage,
                "terms": preset.terms,
                "replacement_for": REPLACEMENTS.get(preset.id),
                "errors": [str(exc)],
            }
        for label, url in (("homepage", preset.homepage), ("terms", preset.terms)):
            if url not in link_cache:
                link_cache[url] = probe_browser_link(url, timeout=args.timeout)
            result[f"{label}_check"] = link_cache[url]
        results.append(result)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
