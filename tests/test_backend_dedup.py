from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from cookies_news_cockpit.dedup import DuplicateIndex, canonical_url
from cookies_news_cockpit.models import AIAnalysis, FeedArticle, RunRequest, SourceInput, TopicInput
from cookies_news_cockpit.pipeline import RunManager
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


class MutableFeeds:
    def __init__(self):
        self.items: dict[str, list[FeedArticle]] = {}

    async def fetch(self, source, extract_full_text=False):
        return self.items[source["id"]]


class StaticKeyStore:
    def get(self) -> str:
        return "sk-test-not-real"


class CountingAI:
    def __init__(self):
        self.calls = 0
        self.urls: list[str] = []

    async def analyze(self, article, topic):
        self.calls += 1
        self.urls.append(article.url)
        return AIAnalysis(score=80, summary=article.title, analysis="测试分析")


@pytest.fixture
def backend_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "backend-tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


def make_article(source: dict, title: str, url: str) -> FeedArticle:
    return FeedArticle(
        title=title,
        url=url,
        excerpt=f"{title} 的新闻摘要",
        source_id=source["id"],
        source_name=source["name"],
    )


def setup_one_topic(backend_home: Path):
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Feed", url="https://feed.example/rss.xml", enabled=True)
    )
    db.create_topic(
        TopicInput(
            name="公司动态", keywords=["公司", "英伟达"], threshold=50, source_ids=[source["id"]]
        )
    )
    feeds = MutableFeeds()
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)
    return paths, db, source, feeds, manager


@pytest.mark.asyncio
async def test_exact_duplicate_is_skipped_across_runs_and_counted(backend_home: Path) -> None:
    paths, db, source, feeds, manager = setup_one_topic(backend_home)
    title = "英伟达发布新一代人工智能芯片平台"
    feeds.items[source["id"]] = [make_article(source, title, "https://news.example/story")]
    first = await manager.run_now(RunRequest())
    assert first["article_count"] == 1

    second = await manager.run_now(RunRequest())
    assert second["article_count"] == 0
    assert second["duplicates_skipped"] == 1
    assert second["status"] == "complete"
    assert "无新增" in second["warning"]
    artifact = json.loads((paths.runs / f"{second['id']}.json").read_text("utf-8"))
    assert artifact["run"]["duplicates_skipped"] == 1
    assert db.get_latest_good_run_id() == first["id"]


@pytest.mark.asyncio
async def test_similar_title_on_different_url_is_skipped(backend_home: Path) -> None:
    _paths, _db, source, feeds, manager = setup_one_topic(backend_home)
    base = "英伟达发布新一代人工智能芯片平台"
    feeds.items[source["id"]] = [make_article(source, base, "https://news.example/original")]
    await manager.run_now(RunRequest())

    feeds.items[source["id"]] = [
        make_article(source, f"据悉{base}", "https://another.example/reprint")
    ]
    repeated = await manager.run_now(RunRequest())
    assert repeated["article_count"] == 0
    assert repeated["duplicates_skipped"] == 1


@pytest.mark.asyncio
async def test_same_url_with_substantive_numeric_update_can_enter_again(
    backend_home: Path,
) -> None:
    _paths, _db, source, feeds, manager = setup_one_topic(backend_home)
    url = "https://news.example/live-story"
    feeds.items[source["id"]] = [make_article(source, "公司预计2026年营收增长10%并扩大产能", url)]
    await manager.run_now(RunRequest())

    feeds.items[source["id"]] = [make_article(source, "公司预计2026年营收增长25%并扩大产能", url)]
    updated = await manager.run_now(RunRequest())
    assert updated["article_count"] == 1
    assert updated["duplicates_skipped"] == 0


@pytest.mark.asyncio
async def test_duplicate_older_than_seven_days_is_allowed(backend_home: Path) -> None:
    _paths, db, source, feeds, manager = setup_one_topic(backend_home)
    title = "英伟达发布新一代人工智能芯片平台"
    feeds.items[source["id"]] = [make_article(source, title, "https://news.example/story")]
    first = await manager.run_now(RunRequest())
    cutoff = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    with db.connect() as connection:
        connection.execute(
            "UPDATE articles SET created_at = ? WHERE run_id = ?", (cutoff, first["id"])
        )

    repeated = await manager.run_now(RunRequest())
    assert repeated["article_count"] == 1
    assert repeated["duplicates_skipped"] == 0


@pytest.mark.asyncio
async def test_current_run_deduplicates_across_sources_and_topics(backend_home: Path) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    first_source = db.create_source(
        SourceInput(name="Feed A", url="https://a.example/rss.xml", enabled=True)
    )
    second_source = db.create_source(
        SourceInput(name="Feed B", url="https://b.example/rss.xml", enabled=True)
    )
    for name in ("AI 芯片", "公司动态"):
        db.create_topic(
            TopicInput(
                name=name,
                keywords=["英伟达"],
                threshold=50,
                source_ids=[first_source["id"], second_source["id"]],
            )
        )
    title = "英伟达发布新一代人工智能芯片平台"
    feeds = MutableFeeds()
    feeds.items[first_source["id"]] = [make_article(first_source, title, "https://a.example/story")]
    feeds.items[second_source["id"]] = [
        make_article(second_source, title, "https://b.example/reprint")
    ]
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)

    run = await manager.run_now(RunRequest())
    assert run["article_count"] == 1
    assert run["duplicates_skipped"] >= 1


@pytest.mark.asyncio
async def test_short_titles_on_different_urls_are_not_false_positives(backend_home: Path) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    first_source = db.create_source(
        SourceInput(name="Feed A", url="https://a.example/rss.xml", enabled=True)
    )
    second_source = db.create_source(
        SourceInput(name="Feed B", url="https://b.example/rss.xml", enabled=True)
    )
    db.create_topic(
        TopicInput(
            name="AI",
            keywords=["AI"],
            threshold=50,
            source_ids=[first_source["id"], second_source["id"]],
        )
    )
    feeds = MutableFeeds()
    feeds.items[first_source["id"]] = [
        make_article(first_source, "AI快讯", "https://a.example/story")
    ]
    feeds.items[second_source["id"]] = [
        make_article(second_source, "AI快讯", "https://b.example/other")
    ]
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)

    run = await manager.run_now(RunRequest())
    assert run["article_count"] == 2
    assert run["duplicates_skipped"] == 0


@pytest.mark.asyncio
async def test_topic_candidates_are_clustered_before_deepseek_call(backend_home: Path) -> None:
    paths, db, source, feeds, _manager = setup_one_topic(backend_home)
    base = "英伟达发布新一代人工智能芯片平台"
    preferred = make_article(source, base, "https://news.example/original")
    preferred.excerpt = f"{base}，这是信息更完整的正文摘要，包含更多可供分析的上下文。"
    reprint = make_article(source, f"据悉{base}", "https://other.example/reprint")
    feeds.items[source["id"]] = [reprint, preferred]
    ai = CountingAI()
    manager = RunManager(
        db,
        paths,
        key_store=StaticKeyStore(),
        feed_service=feeds,
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest())
    assert ai.calls == 1
    assert ai.urls == [preferred.url]
    assert run["analyses"] == 1
    assert run["duplicates_skipped"] == 1
    assert run["article_count"] == 1


@pytest.mark.asyncio
async def test_same_url_material_updates_are_not_blocked_by_seen_hash(backend_home: Path) -> None:
    _paths, _db, source, feeds, manager = setup_one_topic(backend_home)
    url = "https://news.example/live-story"
    feeds.items[source["id"]] = [
        make_article(source, "公司预计2026年营收增长10%并扩大产能", url),
        make_article(source, "公司预计2026年营收增长25%并扩大产能", url),
    ]
    run = await manager.run_now(RunRequest())
    assert run["article_count"] == 2
    assert run["duplicates_skipped"] == 0


def test_same_url_near_title_and_material_change_rules() -> None:
    index = DuplicateIndex()
    url = "https://news.example/live"
    index.add(url, "公司将于明日发布新一代人工智能芯片平台")
    assert index.is_duplicate(url, "公司将于明日发布新一代人工智能芯片平台最新消息")
    assert not index.is_duplicate(url, "公司已于今日发布新一代人工智能芯片平台")


def test_common_syndication_rewrites_and_outlet_suffixes_are_duplicates() -> None:
    index = DuplicateIndex()
    index.add(
        "https://news.example/original",
        "英伟达发布新一代人工智能芯片平台",
    )

    assert index.is_duplicate(
        "https://mirror.example/rewrite",
        "英伟达推出新一代人工智能芯片平台",
    )
    assert index.is_duplicate(
        "https://mirror.example/syndicated",
        "英伟达发布新一代人工智能芯片平台 - 新浪财经",
    )


def test_cross_url_numeric_or_status_update_is_not_suppressed() -> None:
    index = DuplicateIndex()
    index.add("https://news.example/forecast", "公司拟将收入目标上调至10亿元")

    assert not index.is_duplicate(
        "https://news.example/forecast-update",
        "公司已将收入目标上调至12亿元",
    )


def test_canonical_url_falls_back_for_malformed_port() -> None:
    malformed = "https://Example.com:not-a-port/story#fragment"
    assert canonical_url(malformed) == "raw:https://example.com:not-a-port/story"
    index = DuplicateIndex([{"url": malformed, "title": "英伟达发布新一代人工智能芯片平台"}])
    assert index.is_duplicate(malformed, "英伟达发布新一代人工智能芯片平台")


def test_history_index_includes_degraded_but_excludes_failed_results(backend_home: Path) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Feed", url="https://feed.example/rss.xml", enabled=True)
    )
    topic = db.create_topic(TopicInput(name="公司", keywords=["公司"], source_ids=[source["id"]]))

    def insert_for(status: str, title: str) -> str:
        run = db.create_run([topic["id"]], "test")
        db.insert_article(
            {
                "run_id": run["id"],
                "topic_id": topic["id"],
                "source_id": source["id"],
                "title": title,
                "url": f"https://news.example/{run['id']}",
                "content_hash": run["id"],
            }
        )
        db.update_run(run["id"], status=status)
        return run["id"]

    failed_id = insert_for("failed", "公司失败结果不应去重")
    degraded_id = insert_for("degraded", "公司降级结果也应参与去重")
    complete_id = insert_for("complete", "公司完整报告应参与去重")
    db.set_latest_good_run(complete_id)

    indexed = {item["title"] for item in db.recent_article_fingerprints()}
    assert indexed == {"公司完整报告应参与去重", "公司降级结果也应参与去重"}
    assert failed_id != degraded_id


def test_first_usable_degraded_fallback_participates_when_no_good_exists(
    backend_home: Path,
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Feed", url="https://feed.example/rss.xml", enabled=True)
    )
    topic = db.create_topic(TopicInput(name="公司", keywords=["公司"], source_ids=[source["id"]]))
    run = db.create_run([topic["id"]], "test")
    db.insert_article(
        {
            "run_id": run["id"],
            "topic_id": topic["id"],
            "source_id": source["id"],
            "title": "公司首次降级但可见的报告",
            "url": "https://news.example/fallback",
            "content_hash": "fallback",
        }
    )
    db.update_run(run["id"], status="degraded")

    assert db.get_latest_good_run_id() is None
    assert db.recent_article_fingerprints() == [
        {
            "url": "https://news.example/fallback",
            "title": "公司首次降级但可见的报告",
        }
    ]


def test_legacy_runs_table_migrates_duplicates_metric_safely(backend_home: Path) -> None:
    database_path = backend_home / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                trigger TEXT NOT NULL,
                requested_topics_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                article_count INTEGER NOT NULL DEFAULT 0,
                source_success INTEGER NOT NULL DEFAULT 0,
                source_failure INTEGER NOT NULL DEFAULT 0,
                analyses INTEGER NOT NULL DEFAULT 0,
                warning TEXT,
                error TEXT
            )"""
        )
    db = Database(database_path)
    with db.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    assert "duplicates_skipped" in columns
    run = db.create_run([], "migration-test")
    assert run["duplicates_skipped"] == 0
