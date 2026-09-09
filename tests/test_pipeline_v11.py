from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from cookies_news_cockpit.keystore import KeyStoreError
from cookies_news_cockpit.models import (
    AIAnalysis,
    FeedArticle,
    RunRequest,
    SettingsUpdate,
    SourceInput,
    TopicInput,
)
from cookies_news_cockpit.pipeline import RunManager, _artifact_path_for_run
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.services import DeepSeekError
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


class ConfiguredKeyStore:
    def get(self) -> str:
        return "sk-test-not-real"


class BrokenKeyStore:
    def get(self) -> str:
        raise KeyStoreError("test keychain failure")


class StagedFeeds:
    def __init__(
        self,
        items: dict[str, list[FeedArticle]],
        bodies: dict[str, str] | None = None,
    ) -> None:
        self.items = items
        self.bodies = bodies or {}
        self.feed_calls: list[str] = []
        self.enrich_calls: list[str] = []

    async def fetch_metadata(self, source: dict) -> list[FeedArticle]:
        self.feed_calls.append(source["id"])
        return self.items[source["id"]]

    async def enrich_full_text(self, article: FeedArticle) -> FeedArticle:
        self.enrich_calls.append(article.url)
        return article.model_copy(update={"full_text": self.bodies.get(article.url, "")})


class CountingAI:
    def __init__(self, score: int = 80) -> None:
        self.score = score
        self.calls = 0
        self.http_request_count = 0
        self.source_ids: list[str] = []

    async def analyze(self, article: FeedArticle, _topic: dict) -> AIAnalysis:
        self.calls += 1
        self.http_request_count += 1
        self.source_ids.append(article.source_id)
        return AIAnalysis(
            score=self.score,
            summary=f"AI: {article.title}",
            analysis="DeepSeek semantic analysis",
        )


class FailingAI:
    def __init__(self) -> None:
        self.http_request_count = 0

    async def analyze(self, _article: FeedArticle, _topic: dict) -> AIAnalysis:
        self.http_request_count += 1
        raise DeepSeekError(
            "DeepSeek API Key 鉴权失败（HTTP 401）",
            status_code=401,
            http_requests=1,
        )


class BlockingFeeds:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def fetch_metadata(self, _source: dict) -> list[FeedArticle]:
        self.started.set()
        await asyncio.Future()
        return []


class FailedFeeds:
    async def fetch_metadata(self, _source: dict) -> list[FeedArticle]:
        raise RuntimeError("feed unavailable")


def _now(offset: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + offset).isoformat(timespec="seconds")


def _article(
    source: dict,
    title: str,
    suffix: str,
    *,
    published_at: str | None = None,
    excerpt: str = "",
    full_text: str = "",
) -> FeedArticle:
    return FeedArticle(
        title=title,
        url=f"https://news.example/{suffix}",
        excerpt=excerpt,
        full_text=full_text,
        published_at=published_at,
        source_id=source["id"],
        source_name=source["name"],
    )


@pytest.fixture
def backend_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "backend-tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


def _workspace(backend_home: Path) -> tuple[Database, object]:
    paths = resolve_paths(backend_home)
    return Database(paths.database), paths


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "..\\escape", "/tmp/absolute", "C:\\Temp\\drive-path"],
)
def test_artifact_path_rejects_relative_absolute_and_windows_escape(
    backend_home: Path,
    run_id: str,
) -> None:
    runs = backend_home / "runs"
    runs.mkdir()

    with pytest.raises(ValueError, match="安全的文件名|超出应用数据目录"):
        _artifact_path_for_run(runs, run_id)


def test_artifact_path_accepts_legacy_safe_id_inside_runs(backend_home: Path) -> None:
    runs = backend_home / "runs"
    runs.mkdir()

    path = _artifact_path_for_run(runs, "run-2026_09.legacy")

    assert path == runs.resolve() / "run-2026_09.legacy.json"


def test_artifact_write_and_latest_read_refuse_corrupted_unsafe_run_id(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    topic = db.create_topic(TopicInput(name="GPU", source_ids=[source["id"]]))
    run = db.create_run([topic["id"]], "test")
    with db.connect() as connection:
        connection.execute(
            """UPDATE runs SET id = '../escape', status = 'complete',
            phase = 'finished', outcome = 'complete' WHERE id = ?""",
            (run["id"],),
        )
    db.insert_article(
        {
            "run_id": "../escape",
            "topic_id": topic["id"],
            "source_id": source["id"],
            "title": "Database fallback",
            "url": "https://feed.example/database-fallback",
            "content_hash": "database-fallback",
        }
    )
    db.set_latest_good_run("../escape")
    outside = paths.runs.parent / "escape.json"
    outside.write_text('{"marker": "outside"}', encoding="utf-8")
    manager = RunManager(db, paths, key_store=NoKeyStore())

    with pytest.raises(ValueError, match="安全的文件名|超出应用数据目录"):
        manager._write_artifact(db.get_run("../escape"), [])
    report = manager.latest_report()

    assert report is not None
    assert report["run"]["id"] == "../escape"
    assert "marker" not in report
    assert outside.read_text(encoding="utf-8") == '{"marker": "outside"}'


@pytest.mark.asyncio
async def test_finite_freshness_rejects_missing_invalid_old_and_future_dates(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(name="GPU", keywords=["GPU"], threshold=50, source_ids=[source["id"]])
    )
    items = [
        _article(source, "GPU fresh", "fresh", published_at=_now()),
        _article(source, "GPU old", "old", published_at=_now(timedelta(days=-8))),
        _article(source, "GPU missing", "missing"),
        _article(source, "GPU invalid", "invalid", published_at="not-a-date"),
        _article(source, "GPU future", "future", published_at=_now(timedelta(days=2))),
    ]
    manager = RunManager(
        db,
        paths,
        key_store=NoKeyStore(),
        feed_service=StagedFeeds({source["id"]: items}),
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["status"] == "complete"
    assert run["outcome"] == "complete"
    assert run["article_count"] == 1
    assert run["phase"] == "finished"
    assert run["funnel"]["feed_items"] == 5
    assert run["funnel"]["fresh"] == 1
    assert run["funnel"]["date_rejected"] == 4
    assert run["funnel"]["date_stale"] == 1
    assert run["funnel"]["date_missing"] == 1
    assert run["funnel"]["date_invalid"] == 1
    assert run["funnel"]["date_future"] == 1
    topic_funnel = next(iter(run["funnel"]["per_topic"].values()))
    assert topic_funnel["date_rejected"] == 4
    assert topic_funnel["date_stale"] == 1
    assert topic_funnel["date_missing"] == 1
    assert topic_funnel["date_invalid"] == 1
    assert topic_funnel["date_future"] == 1


@pytest.mark.asyncio
async def test_unlimited_freshness_keeps_undated_and_old_articles(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(
        SettingsUpdate(freshness_days=None, extract_full_text=False, default_article_limit=10)
    )
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(TopicInput(name="GPU", keywords=["GPU"], source_ids=[source["id"]]))
    items = [
        _article(source, "GPU old", "old", published_at=_now(timedelta(days=-90))),
        _article(source, "GPU missing", "missing"),
        _article(source, "GPU invalid", "invalid", published_at="not-a-date"),
    ]
    manager = RunManager(
        db,
        paths,
        key_store=NoKeyStore(),
        feed_service=StagedFeeds({source["id"]: items}),
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["article_count"] == 3
    assert run["funnel"]["fresh"] == 3
    assert run["funnel"]["date_rejected"] == 0


@pytest.mark.asyncio
async def test_keyword_evidence_covers_feed_body_and_exclusion_terms(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(
            name="Market",
            keywords=["AI", "air conditioning", "供应链"],
            exclusion_keywords=["广告"],
            threshold=50,
            article_limit=10,
            source_ids=[source["id"]],
        )
    )
    items = [
        _article(source, "Retail outlook", "retail", published_at=_now()),
        _article(source, "Air-conditioning launch", "air", published_at=_now()),
        _article(
            source,
            "Industry bulletin",
            "body",
            published_at=_now(),
            full_text="这篇 feed 内嵌正文讨论全球供应链。",
        ),
        _article(
            source,
            "AI promotion",
            "ad",
            published_at=_now(),
            excerpt="广告内容",
        ),
    ]
    manager = RunManager(
        db,
        paths,
        key_store=NoKeyStore(),
        feed_service=StagedFeeds({source["id"]: items}),
    )

    run = await manager.run_now(RunRequest(), trigger="manual")
    articles = db.list_run_articles(run["id"])

    assert {item["title"] for item in articles} == {
        "Air-conditioning launch",
        "Industry bulletin",
    }
    evidence = {item["title"]: item for item in articles}
    assert evidence["Air-conditioning launch"]["matched_keywords"] == ["air conditioning"]
    assert evidence["Air-conditioning launch"]["matched_fields"] == ["title"]
    assert evidence["Industry bulletin"]["matched_fields"] == ["full_text"]
    assert run["funnel"]["keyword_hits"] == 2
    assert run["funnel"]["exclusion_rejected"] == 1


@pytest.mark.asyncio
async def test_zero_hit_fulltext_review_is_source_round_robin_and_can_recover_match(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    first = db.create_source(SourceInput(name="First", url="https://first.example/rss"))
    second = db.create_source(SourceInput(name="Second", url="https://second.example/rss"))
    db.create_topic(
        TopicInput(
            name="Quantum",
            keywords=["量子材料"],
            threshold=50,
            source_ids=[first["id"], second["id"]],
        )
    )
    items = {
        first["id"]: [
            _article(first, "First newest", "a-new", published_at=_now()),
            _article(first, "First older", "a-old", published_at=_now(timedelta(minutes=-4))),
        ],
        second["id"]: [
            _article(second, "Second newest", "b-new", published_at=_now(timedelta(minutes=-1))),
            _article(second, "Second older", "b-old", published_at=_now(timedelta(minutes=-5))),
        ],
    }
    feeds = StagedFeeds(
        items,
        bodies={"https://news.example/b-new": "正文确认这是量子材料产业进展。"},
    )
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)

    run = await manager.run_now(RunRequest(), trigger="manual")
    articles = db.list_run_articles(run["id"])

    assert feeds.enrich_calls == [
        "https://news.example/a-new",
        "https://news.example/b-new",
        "https://news.example/a-old",
        "https://news.example/b-old",
    ]
    assert [item["title"] for item in articles] == ["Second newest"]
    assert articles[0]["matched_fields"] == ["full_text"]
    assert run["funnel"]["fulltext_fetches"] == 4
    assert run["funnel"]["keyword_hits"] == 1
    assert run["funnel"]["semantic_fallback"] == 0


@pytest.mark.asyncio
async def test_semantic_fallback_is_capped_at_twenty_for_entire_run(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    first = db.create_source(SourceInput(name="First", url="https://first.example/rss"))
    second = db.create_source(SourceInput(name="Second", url="https://second.example/rss"))
    db.create_topic(
        TopicInput(
            name="Semantic",
            keywords=["literal-never-present"],
            threshold=50,
            article_limit=30,
            source_ids=[first["id"], second["id"]],
        )
    )
    items = {first["id"]: [], second["id"]: []}
    for index in range(25):
        source = first if index % 2 == 0 else second
        items[source["id"]].append(
            _article(
                source,
                f"Brief {index:02d}",
                f"brief-{index}",
                published_at=_now(timedelta(minutes=-index)),
                # Deliberately bias one source toward much longer excerpts.
                # Source rotation must survive candidate deduplication instead
                # of being replaced by content-length ranking.
                excerpt=("A" * 200 if source["id"] == first["id"] else "B"),
            )
        )
    ai = CountingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=StagedFeeds(items),
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")
    articles = db.list_run_articles(run["id"])

    assert ai.calls == 20
    assert ai.source_ids[:4] == [first["id"], second["id"], first["id"], second["id"]]
    assert run["article_count"] == 20
    assert run["analyses"] == 20
    assert run["ai_requests"] == 20
    assert run["ai_success"] == 20
    assert run["funnel"]["semantic_fallback"] == 20
    assert all(item["analysis_mode"] == "ai_semantic" for item in articles)
    assert all(item["matched_keywords"] == [] for item in articles)


@pytest.mark.asyncio
async def test_fulltext_review_recovers_unmatched_articles_even_with_metadata_hit(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    source = db.create_source(SourceInput(name="Research", url="https://research.example/rss"))
    db.create_topic(
        TopicInput(
            name="Quantum materials",
            keywords=["量子材料"],
            threshold=50,
            source_ids=[source["id"]],
        )
    )
    metadata_hit = _article(
        source,
        "Metadata match",
        "metadata-hit",
        published_at=_now(),
        excerpt="量子材料行业动态。",
    )
    body_only_hit = _article(
        source,
        "Research bulletin",
        "body-only-hit",
        published_at=_now(timedelta(minutes=-1)),
        excerpt="A general research update.",
    )
    feeds = StagedFeeds(
        {source["id"]: [metadata_hit, body_only_hit]},
        bodies={body_only_hit.url: "正文确认这项研究涉及量子材料产业。"},
    )
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)

    run = await manager.run_now(RunRequest(), trigger="manual")
    articles = db.list_run_articles(run["id"])

    assert {item["title"] for item in articles} == {
        "Metadata match",
        "Research bulletin",
    }
    recovered = next(item for item in articles if item["title"] == "Research bulletin")
    assert recovered["matched_fields"] == ["full_text"]
    assert feeds.enrich_calls == [metadata_hit.url, body_only_hit.url]
    assert run["funnel"]["keyword_hits"] == 2
    assert run["funnel"]["fulltext_fetches"] == 2


@pytest.mark.asyncio
async def test_ai_cache_avoids_second_network_request_after_dedup_window(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(TopicInput(name="GPU", keywords=["GPU"], source_ids=[source["id"]]))
    item = _article(source, "GPU capacity", "gpu", published_at=_now())
    ai = CountingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=StagedFeeds({source["id"]: [item]}),
        deepseek_factory=lambda _key: ai,
    )

    first = await manager.run_now(RunRequest(), trigger="manual")
    old = _now(timedelta(days=-8))
    with db.connect() as connection:
        connection.execute(
            "UPDATE articles SET created_at = ? WHERE run_id = ?", (old, first["id"])
        )

    second = await manager.run_now(RunRequest(), trigger="manual")
    article = db.list_run_articles(second["id"])[0]

    assert ai.calls == 1
    assert second["analyses"] == 0
    assert second["ai_requests"] == 0
    assert second["ai_success"] == 1
    assert second["funnel"]["ai_cache_hits"] == 1
    assert article["analysis_mode"] == "ai_cache"
    assert article["model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_real_analysis_success_promotes_saved_key_to_connected(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(
        SettingsUpdate(
            extract_full_text=False,
            deepseek_status="saved_unverified",
            deepseek_last_error="旧错误",
        )
    )
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(TopicInput(name="GPU", keywords=["GPU"], source_ids=[source["id"]]))
    feeds = StagedFeeds(
        {source["id"]: [_article(source, "GPU news", "status-ok", published_at=_now())]}
    )
    ai = CountingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=feeds,
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    settings = db.get_settings()
    assert run["ai_requests"] == 1
    assert settings.deepseek_status == "connected"
    assert settings.deepseek_last_tested_at is not None
    assert settings.deepseek_last_error is None


@pytest.mark.asyncio
async def test_real_analysis_failure_marks_saved_key_error_and_retains_reason(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(
        SettingsUpdate(extract_full_text=False, deepseek_status="connected")
    )
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(TopicInput(name="GPU", keywords=["GPU"], source_ids=[source["id"]]))
    feeds = StagedFeeds(
        {source["id"]: [_article(source, "GPU news", "status-error", published_at=_now())]}
    )
    ai = FailingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=feeds,
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    settings = db.get_settings()
    assert run["status"] == "degraded"
    assert run["ai_requests"] == 1
    assert run["ai_failure"] == 1
    assert settings.deepseek_status == "error"
    assert settings.deepseek_last_tested_at is not None
    assert settings.deepseek_last_error == "DeepSeek API Key 鉴权失败（HTTP 401）"


@pytest.mark.asyncio
async def test_keychain_failure_degrades_to_explicit_rules_mode(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(TopicInput(name="GPU", keywords=["GPU"], source_ids=[source["id"]]))
    feeds = StagedFeeds(
        {source["id"]: [_article(source, "GPU news", "gpu", published_at=_now())]}
    )
    manager = RunManager(db, paths, key_store=BrokenKeyStore(), feed_service=feeds)

    run = await manager.run_now(RunRequest(), trigger="manual")
    article = db.list_run_articles(run["id"])[0]

    assert run["status"] == "degraded"
    assert "无法读取系统钥匙串" in run["warning"]
    assert run["ai_requests"] == 0
    assert article["analysis_mode"] == "rules"


@pytest.mark.asyncio
async def test_cancelled_run_writes_no_articles_and_keeps_previous_report(
    backend_home: Path,
) -> None:
    db, paths = _workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    topic = db.create_topic(
        TopicInput(name="GPU", keywords=["GPU"], source_ids=[source["id"]])
    )
    previous = db.create_run([topic["id"]], "test")
    db.insert_article(
        {
            "run_id": previous["id"],
            "topic_id": topic["id"],
            "source_id": source["id"],
            "title": "Previous GPU report",
            "url": "https://news.example/previous",
            "content_hash": "previous",
        }
    )
    db.update_run(
        previous["id"], status="complete", phase="finished", outcome="complete", article_count=1
    )
    db.set_latest_good_run(previous["id"])
    feeds = BlockingFeeds()
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)

    started = await manager.start(RunRequest(), trigger="manual")
    await asyncio.wait_for(feeds.started.wait(), timeout=1)
    cancelled = await manager.cancel(started["id"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["phase"] == "finished"
    assert cancelled["outcome"] == "cancelled"
    assert cancelled["article_count"] == 0
    assert db.list_run_articles(cancelled["id"]) == []
    assert db.get_latest_good_run_id() == previous["id"]
    assert manager.latest_report()["run"]["id"] == previous["id"]


@pytest.mark.asyncio
async def test_all_source_failures_have_explicit_outcome(backend_home: Path) -> None:
    db, paths = _workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(TopicInput(name="GPU", keywords=["GPU"], source_ids=[source["id"]]))
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=FailedFeeds())

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["status"] == "failed"
    assert run["phase"] == "finished"
    assert run["outcome"] == "source_failure"
    assert run["source_failure"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("stale", "no_fresh_articles"),
        ("no_keyword", "no_keyword_match"),
        ("threshold", "below_threshold"),
        ("duplicate", "no_new_after_dedup"),
    ],
)
async def test_empty_runs_report_precise_outcome(
    backend_home: Path, scenario: str, expected: str
) -> None:
    db, paths = _workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    threshold = 99 if scenario == "threshold" else 50
    db.create_topic(
        TopicInput(
            name="GPU",
            keywords=["GPU"],
            threshold=threshold,
            source_ids=[source["id"]],
        )
    )
    title = "Different subject" if scenario == "no_keyword" else "GPU update"
    published = _now(timedelta(days=-8)) if scenario == "stale" else _now()
    feeds = StagedFeeds(
        {source["id"]: [_article(source, title, scenario, published_at=published)]}
    )
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)
    if scenario == "duplicate":
        first = await manager.run_now(RunRequest(), trigger="manual")
        assert first["article_count"] == 1

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["status"] == "complete"
    assert run["article_count"] == 0
    assert run["outcome"] == expected


@pytest.mark.asyncio
async def test_fulltext_enrichment_is_limited_to_shortlist(backend_home: Path) -> None:
    db, paths = _workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(
            name="AI",
            keywords=["AI"],
            threshold=50,
            article_limit=1,
            source_ids=[source["id"]],
        )
    )
    items = [
        _article(source, f"AI brief {index:02d}", f"ai-{index}", published_at=_now())
        for index in range(30)
    ]
    feeds = StagedFeeds({source["id"]: items})
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert feeds.feed_calls == [source["id"]]
    assert len(feeds.enrich_calls) == 20
    assert run["funnel"]["fulltext_fetches"] == 20
    assert run["article_count"] == 1
