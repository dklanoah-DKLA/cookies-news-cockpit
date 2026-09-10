from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from cookies_news_cockpit.models import (
    AIAnalysis,
    FeedArticle,
    RunRequest,
    SettingsUpdate,
    SourceInput,
    TopicInput,
)
from cookies_news_cockpit.pipeline import RunManager
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.services import DeepSeekError
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


class ConfiguredKeyStore:
    def get(self) -> str:
        return "sk-test-not-real"


class Feeds:
    def __init__(
        self,
        items: dict[str, list[FeedArticle]],
        *,
        bodies: dict[str, str] | None = None,
    ) -> None:
        self.items = items
        self.bodies = bodies or {}
        self.enrich_calls: list[str] = []

    async def fetch_metadata(self, source: dict) -> list[FeedArticle]:
        return self.items[source["id"]]

    async def enrich_full_text(self, article: FeedArticle) -> FeedArticle:
        self.enrich_calls.append(article.url)
        return article.model_copy(update={"full_text": self.bodies.get(article.url, "")})


class RecordingAI:
    def __init__(
        self,
        *,
        score: int = 80,
        relevant: bool = True,
        reason: str = "contextual_match",
    ) -> None:
        self.score = score
        self.relevant = relevant
        self.reason = reason
        self.http_request_count = 0
        self.calls: list[tuple[str, str]] = []

    async def analyze(self, article: FeedArticle, topic: dict) -> AIAnalysis:
        self.http_request_count += 1
        self.calls.append((topic["name"], article.source_id))
        return AIAnalysis(
            score=self.score,
            summary=article.title,
            analysis="bounded semantic review",
            relevant=self.relevant,
            reason=self.reason,
        )


class FailingAI:
    def __init__(self) -> None:
        self.http_request_count = 0

    async def analyze(self, _article: FeedArticle, _topic: dict) -> AIAnalysis:
        self.http_request_count += 1
        raise DeepSeekError("test failure", http_requests=1)


class RejectRecoveredAI(RecordingAI):
    async def analyze(self, article: FeedArticle, topic: dict) -> AIAnalysis:
        self.http_request_count += 1
        self.calls.append((topic["name"], article.source_id))
        return AIAnalysis(
            score=30 if article.full_text else 80,
            summary=article.title,
            analysis="reject literal recoveries; accept semantic candidates",
            relevant=True,
            reason="direct_match" if article.full_text else "contextual_match",
        )


@pytest.fixture
def backend_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "backend-tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


def workspace(path: Path) -> tuple[Database, object]:
    paths = resolve_paths(path)
    return Database(paths.database), paths


def now(offset: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + offset).isoformat(timespec="seconds")


def article(
    source: dict,
    title: str,
    suffix: str,
    *,
    published_at: str | None = None,
) -> FeedArticle:
    return FeedArticle(
        title=title,
        url=f"https://news.example/{suffix}",
        published_at=published_at or now(),
        source_id=source["id"],
        source_name=source["name"],
    )


@pytest.mark.asyncio
async def test_one_literal_hit_still_triggers_shortfall_semantic_review(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(
            name="ESG",
            keywords=["ESG"],
            threshold=60,
            article_limit=5,
            source_ids=[source["id"]],
        )
    )
    items = [article(source, "ESG disclosure", "fresh-0")]
    items.extend(
        article(source, f"Sustainability context {index}", f"fresh-{index}")
        for index in range(1, 170)
    )
    items.extend(
        article(
            source,
            f"Old context {index}",
            f"old-{index}",
            published_at=now(timedelta(days=-8, minutes=-index)),
        )
        for index in range(11)
    )
    ai = RecordingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=Feeds({source["id"]: items}),
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["funnel"]["feed_items"] == 181
    assert run["funnel"]["fresh"] == 170
    assert run["funnel"]["keyword_hits"] == 1
    assert run["funnel"]["semantic_reviewed"] == 4
    assert run["funnel"]["semantic_additional_reviewed"] == 0
    assert run["funnel"]["threshold_rejected"] == 0
    assert run["funnel"]["core_selected"] == 5
    assert run["funnel"]["selected"] == 5
    assert run["article_count"] == 5
    assert "降低门槛" not in (run["warning"] or "")


@pytest.mark.asyncio
async def test_fulltext_recovery_does_not_consume_a_semantic_ai_slot(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(
            name="ESG",
            keywords=["ESG"],
            threshold=60,
            article_limit=100,
            source_ids=[source["id"]],
        )
    )
    items = [
        article(
            source,
            f"Context {index:02d}",
            f"context-{index}",
            published_at=now(timedelta(minutes=-index)),
        )
        for index in range(55)
    ]
    feeds = Feeds(
        {source["id"]: items},
        bodies={
            item.url: "The full article directly discusses ESG disclosure."
            for item in items[:20]
        },
    )
    ai = RecordingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=feeds,
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["funnel"]["fulltext_keyword_hits"] == 20
    assert run["funnel"]["semantic_initial_reviewed"] == 20
    assert run["funnel"]["semantic_additional_reviewed"] == 15
    assert run["funnel"]["semantic_reviewed"] == 35
    assert len(ai.calls) == 55
    assert run["article_count"] == 55


@pytest.mark.asyncio
async def test_fulltext_cap_is_independent_from_all_35_semantic_slots(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(
            name="ESG",
            keywords=["ESG"],
            threshold=60,
            article_limit=100,
            source_ids=[source["id"]],
        )
    )
    items = [
        article(
            source,
            f"Context {index:03d}",
            f"bounded-{index}",
            published_at=now(timedelta(minutes=-index)),
        )
        for index in range(105)
    ]
    feeds = Feeds(
        {source["id"]: items},
        bodies={
            item.url: "The full article directly discusses ESG disclosure."
            for item in items[:70]
        },
    )
    ai = RejectRecoveredAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=feeds,
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["funnel"]["fulltext_fetches"] == 100
    assert run["funnel"]["fulltext_success"] == 70
    assert run["funnel"]["fulltext_keyword_hits"] == 70
    assert run["funnel"]["fulltext_recovery_budget_exhausted"] == 5
    assert run["funnel"]["semantic_candidates"] == 35
    assert run["funnel"]["semantic_initial_reviewed"] == 20
    assert run["funnel"]["semantic_additional_reviewed"] == 15
    assert run["funnel"]["semantic_reviewed"] == 35
    assert run["funnel"]["semantic_budget_exhausted"] == 0
    assert run["article_count"] == 35


@pytest.mark.asyncio
async def test_body_hit_after_first_35_semantic_candidates_is_still_recovered(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(
            name="ESG",
            keywords=["ESG"],
            threshold=60,
            article_limit=36,
            source_ids=[source["id"]],
        )
    )
    items = [
        article(
            source,
            f"Context {index:02d}",
            f"late-body-{index}",
            published_at=now(timedelta(minutes=-index)),
        )
        for index in range(36)
    ]
    feeds = Feeds(
        {source["id"]: items},
        bodies={items[35].url: "Late in the pool, this body contains ESG evidence."},
    )
    ai = RecordingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=feeds,
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["funnel"]["fulltext_fetches"] == 36
    assert run["funnel"]["fulltext_keyword_hits"] == 1
    assert run["funnel"]["semantic_reviewed"] == 35
    assert run["article_count"] == 36


@pytest.mark.asyncio
async def test_fulltext_result_is_reused_for_same_url_across_topics(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    first = db.create_topic(
        TopicInput(name="Alpha", keywords=["Alpha"], source_ids=[source["id"]])
    )
    second = db.create_topic(
        TopicInput(name="Beta", keywords=["Beta"], source_ids=[source["id"]])
    )
    shared = article(source, "Context only", "shared-fulltext")
    feeds = Feeds(
        {source["id"]: [shared]},
        bodies={shared.url: "The article contains both Alpha and Beta evidence."},
    )
    manager = RunManager(
        db,
        paths,
        key_store=NoKeyStore(),
        feed_service=feeds,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert feeds.enrich_calls == [shared.url]
    assert run["funnel"]["fulltext_fetches"] == 1
    assert run["funnel"]["fulltext_success"] == 1
    assert run["funnel"]["fulltext_keyword_hits"] == 2
    assert run["funnel"]["per_topic"][first["id"]]["fulltext_success"] == 1
    assert run["funnel"]["per_topic"][second["id"]]["fulltext_success"] == 1


@pytest.mark.asyncio
async def test_fulltext_cap_counts_unique_urls_not_topic_copies(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    for name in ("Alpha", "Beta"):
        db.create_topic(
            TopicInput(
                name=name,
                keywords=[f"missing-{name}"],
                article_limit=100,
                source_ids=[source["id"]],
            )
        )
    items = [
        article(
            source,
            f"Context {index:03d}",
            f"shared-cap-{index}",
            published_at=now(timedelta(minutes=-index)),
        )
        for index in range(105)
    ]
    feeds = Feeds({source["id"]: items})
    manager = RunManager(
        db,
        paths,
        key_store=NoKeyStore(),
        feed_service=feeds,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert len(feeds.enrich_calls) == 100
    assert len(set(feeds.enrich_calls)) == 100
    assert run["funnel"]["fulltext_fetches"] == 100
    assert run["funnel"]["fulltext_recovery_budget_exhausted"] == 5


@pytest.mark.asyncio
async def test_cross_topic_duplicate_shortfall_still_uses_semantic_candidate(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    first = db.create_topic(
        TopicInput(
            name="Alpha",
            keywords=["Alpha"],
            article_limit=1,
            source_ids=[source["id"]],
        )
    )
    second = db.create_topic(
        TopicInput(
            name="Beta",
            keywords=["Beta"],
            article_limit=1,
            source_ids=[source["id"]],
        )
    )
    items = [
        article(source, "Alpha Beta shared event", "shared"),
        article(source, "Contextual follow-up", "context"),
    ]
    ai = RecordingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=Feeds({source["id"]: items}),
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["article_count"] == 2
    assert run["funnel"]["per_topic"][first["id"]]["selected"] == 1
    assert run["funnel"]["per_topic"][second["id"]]["selected"] == 1
    assert run["funnel"]["semantic_reviewed"] == 1
    assert ai.calls[-1] == ("Beta", source["id"])


@pytest.mark.asyncio
async def test_semantic_review_round_robins_topics_and_sources(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    sources = [
        db.create_source(SourceInput(name=f"S{index}", url=f"https://s{index}.example/rss"))
        for index in range(4)
    ]
    db.create_topic(
        TopicInput(
            name="Topic A",
            keywords=["missing-a"],
            threshold=60,
            article_limit=4,
            source_ids=[sources[0]["id"], sources[1]["id"]],
        )
    )
    db.create_topic(
        TopicInput(
            name="Topic B",
            keywords=["missing-b"],
            threshold=60,
            article_limit=4,
            source_ids=[sources[2]["id"], sources[3]["id"]],
        )
    )
    items = {
        source["id"]: [
            article(source, f"Context {source['name']} {index}", f"{source['name']}-{index}")
            for index in range(3)
        ]
        for source in sources
    }
    ai = RecordingAI()
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=Feeds(items),
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    first_four = ai.calls[:4]
    assert [name for name, _source_id in first_four] == [
        "Topic A",
        "Topic B",
        "Topic A",
        "Topic B",
    ]
    assert [source_id for _name, source_id in first_four] == [
        sources[0]["id"],
        sources[2]["id"],
        sources[1]["id"],
        sources[3]["id"],
    ]
    assert run["funnel"]["selected"] == 8
    assert all(
        values["selected"] == 4 for values in run["funnel"]["per_topic"].values()
    )


@pytest.mark.asyncio
async def test_ai_relevance_veto_wins_over_high_score(backend_home: Path) -> None:
    db, paths = workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(name="AI", keywords=["AI"], source_ids=[source["id"]])
    )
    ai = RecordingAI(score=99, relevant=False, reason="off_topic")
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=Feeds({source["id"]: [article(source, "AI advertisement", "ad")]}),
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["article_count"] == 0
    assert run["outcome"] == "no_relevant_after_ai"
    assert "AI 相关性复核后没有可展示结果" in run["warning"]
    assert run["funnel"]["ai_irrelevant"] == 1
    assert run["funnel"]["reason_off_topic"] == 1
    assert run["funnel"]["threshold_rejected"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("score", "expected_selected", "expected_below"),
    [(50, 1, 0), (44, 0, 1)],
)
async def test_core_threshold_has_bounded_supplement_band(
    backend_home: Path,
    score: int,
    expected_selected: int,
    expected_below: int,
) -> None:
    db, paths = workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    topic = db.create_topic(
        TopicInput(
            name="AI",
            keywords=["AI"],
            threshold=60,
            article_limit=1,
            source_ids=[source["id"]],
        )
    )
    ai = RecordingAI(score=score, reason="direct_match")
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=Feeds({source["id"]: [article(source, "AI release", "release")]}),
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["article_count"] == expected_selected
    assert run["funnel"]["supplement_selected"] == expected_selected
    assert run["funnel"]["below_supplement"] == expected_below
    assert run["funnel"]["per_topic"][topic["id"]]["core_threshold"] == 60
    assert run["funnel"]["per_topic"][topic["id"]]["supplement_threshold"] == 45
    assert run["funnel"]["per_topic"][topic["id"]]["target_count"] == 1


@pytest.mark.asyncio
async def test_no_key_never_promotes_pure_semantic_candidate(backend_home: Path) -> None:
    db, paths = workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(name="ESG", keywords=["absent"], source_ids=[source["id"]])
    )
    manager = RunManager(
        db,
        paths,
        key_store=NoKeyStore(),
        feed_service=Feeds({source["id"]: [article(source, "Context", "context")]}),
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["article_count"] == 0
    assert run["analyses"] == 0
    assert run["funnel"]["semantic_reviewed"] == 0


@pytest.mark.asyncio
async def test_ai_failure_never_promotes_remaining_semantic_candidates(
    backend_home: Path,
) -> None:
    db, paths = workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(name="ESG", keywords=["absent"], source_ids=[source["id"]])
    )
    items = [article(source, f"Context {index}", f"context-{index}") for index in range(3)]
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=Feeds({source["id"]: items}),
        deepseek_factory=lambda _key: FailingAI(),
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["status"] == "degraded"
    assert run["article_count"] == 0
    assert run["ai_failure"] == 1
    assert run["funnel"]["semantic_reviewed"] == 1
    assert run["funnel"]["semantic_budget_exhausted"] == 0


@pytest.mark.asyncio
async def test_ai_cache_identity_includes_scoring_policy_v2(backend_home: Path) -> None:
    db, paths = workspace(backend_home)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(
        TopicInput(name="AI", keywords=["AI"], source_ids=[source["id"]])
    )
    seen_configs: list[dict] = []
    original_get = db.get_ai_cache
    original_put = db.put_ai_cache

    def get_cache(fingerprint: str, config: dict, model: str):
        seen_configs.append(config)
        return original_get(fingerprint, config, model)

    def put_cache(
        fingerprint: str,
        config: dict,
        model: str,
        analysis: dict,
        *,
        ttl_days: int,
    ) -> None:
        seen_configs.append(config)
        original_put(
            fingerprint,
            config,
            model,
            analysis,
            ttl_days=ttl_days,
        )

    db.get_ai_cache = get_cache  # type: ignore[method-assign]
    db.put_ai_cache = put_cache  # type: ignore[method-assign]
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=Feeds({source["id"]: [article(source, "AI release", "release")]}),
        deepseek_factory=lambda _key: RecordingAI(reason="direct_match"),
    )

    await manager.run_now(RunRequest(), trigger="manual")

    assert seen_configs
    assert all(config["scoring_policy_version"] == 2 for config in seen_configs)
