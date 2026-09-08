from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from cookies_news_cockpit.models import (
    FeedArticle,
    RunRequest,
    SettingsUpdate,
    SourceInput,
    TopicInput,
    TopicPatch,
)
from cookies_news_cockpit.pipeline import RunManager
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.services import DeepSeekClient
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


class ConfiguredKeyStore:
    def get(self) -> str:
        return "sk-test-not-real"


class ControlledFeeds:
    async def fetch(self, source, extract_full_text=False):
        if "bad" in source["url"]:
            raise RuntimeError("simulated feed outage")
        return [
            FeedArticle(
                title="GPU 供应链出现新进展",
                url=f"https://news.example/{source['id']}",
                excerpt="GPU 与算力基础设施市场消息。",
                full_text="GPU 供应链与算力基础设施的长文本。" if extract_full_text else "",
                source_id=source["id"],
                source_name=source["name"],
            )
        ]


class TwoArticleFeeds:
    async def fetch(self, source, extract_full_text=False):
        return [
            FeedArticle(
                title=f"GPU 进展 {index}",
                url=f"https://news.example/{index}",
                excerpt="GPU 市场消息。",
                source_id=source["id"],
                source_name=source["name"],
            )
            for index in range(2)
        ]


@pytest.mark.asyncio
async def test_deepseek_outage_stops_further_calls_and_falls_back_to_rules(
    backend_home: Path,
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    db.create_topic(
        TopicInput(name="AI 芯片", keywords=["GPU"], threshold=50, source_ids=[source["id"]])
    )
    calls = 0

    def invalid_schema(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"score":999,"summary":"摘要","analysis":"分析"}'
                            )
                        }
                    }
                ]
            },
        )

    ai = DeepSeekClient(
        "sk-test-not-real",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(invalid_schema)
        ),
    )
    manager = RunManager(
        db,
        paths,
        key_store=ConfiguredKeyStore(),
        feed_service=TwoArticleFeeds(),
        deepseek_factory=lambda _key: ai,
    )

    run = await manager.run_now(RunRequest())

    assert calls == 2
    assert run["analyses"] == 1
    assert run["status"] == "degraded"
    assert run["article_count"] == 2
    assert "剩余候选已停止 AI 调用" in run["warning"]
    assert all(
        "关键词规则初筛" in article["analysis"] for article in db.list_run_articles(run["id"])
    )


@pytest.fixture
def backend_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "backend-tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


@pytest.mark.asyncio
async def test_rules_only_first_run_is_good_but_later_degraded_run_does_not_replace_it(
    backend_home: Path,
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    good = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    bad = db.create_source(
        SourceInput(name="Bad feed", url="https://bad.example/feed.xml", enabled=True)
    )
    topic = db.create_topic(
        TopicInput(name="AI 芯片", keywords=["GPU"], threshold=50, source_ids=[good["id"]])
    )
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=ControlledFeeds())

    first = await manager.run_now(RunRequest())
    assert first["status"] == "complete"
    assert first["article_count"] == 1
    assert "未配置 DeepSeek" in first["warning"]
    assert db.get_latest_good_run_id() == first["id"]
    assert manager.latest_report()["run"]["id"] == first["id"]

    db.update_topic(topic["id"], TopicPatch(source_ids=[good["id"], bad["id"]]))
    second = await manager.run_now(RunRequest())
    assert second["status"] == "degraded"
    assert second["source_failure"] == 1
    assert db.get_latest_good_run_id() == first["id"]
    assert manager.latest_report()["run"]["id"] == first["id"]
    assert (
        json.loads((paths.runs / f"{second['id']}.json").read_text("utf-8"))["run"]["status"]
        == "degraded"
    )


@pytest.mark.asyncio
async def test_archiving_topic_and_source_preserves_history_and_favorite(
    backend_home: Path,
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    topic = db.create_topic(
        TopicInput(name="AI 芯片", keywords=["GPU"], threshold=50, source_ids=[source["id"]])
    )
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=ControlledFeeds())
    run = await manager.run_now(RunRequest())
    article = db.list_run_articles(run["id"])[0]
    db.set_favorite(article["id"], True)

    db.delete_topic(topic["id"])
    db.delete_source(source["id"])

    preserved = db.get_article(article["id"])
    assert preserved["favorite"] is True
    assert preserved["topic_name"] == "AI 芯片"
    assert preserved["source_name"] == "Good feed"
    assert manager.latest_report()["run"]["id"] == run["id"]


@pytest.mark.asyncio
async def test_first_degraded_run_is_visible_without_becoming_latest_good(
    backend_home: Path,
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    good = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    bad = db.create_source(
        SourceInput(name="Bad feed", url="https://bad.example/feed.xml", enabled=True)
    )
    db.create_topic(
        TopicInput(
            name="AI 芯片",
            keywords=["GPU"],
            threshold=50,
            source_ids=[good["id"], bad["id"]],
        )
    )
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=ControlledFeeds())

    run = await manager.run_now(RunRequest())
    assert run["status"] == "degraded"
    assert run["article_count"] == 1
    assert db.get_latest_good_run_id() is None

    report = manager.latest_report()
    assert report["run"]["id"] == run["id"]
    assert report["run"]["status"] == "degraded"
    assert "尚无完整报告" in report["run"]["warning"]
    assert len(report["articles"]) == 1
    assert db.get_latest_good_run_id() is None


@pytest.mark.asyncio
async def test_explicit_null_topic_values_inherit_global_run_settings(
    backend_home: Path,
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    db.update_settings(SettingsUpdate(default_threshold=70, default_article_limit=1))
    source = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    topic = db.create_topic(
        TopicInput(
            name="AI 芯片",
            keywords=["GPU"],
            threshold=95,
            article_limit=5,
            source_ids=[source["id"]],
        )
    )
    inherited = db.update_topic(
        topic["id"],
        TopicPatch(threshold=None, article_limit=None),
    )
    assert inherited["threshold"] is None
    assert inherited["article_limit"] is None

    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=TwoArticleFeeds())
    run = await manager.run_now(RunRequest())
    # Lexical score 73 clears global threshold 70, while global limit 1 wins.
    assert run["status"] == "complete"
    assert run["article_count"] == 1
    assert db.get_latest_good_run_id() == run["id"]


def test_duplicate_article_does_not_create_orphan_fts_row(backend_home: Path) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    topic = db.create_topic(TopicInput(name="AI", keywords=["GPU"], source_ids=[source["id"]]))
    run = db.create_run([topic["id"]], "test")
    value = {
        "run_id": run["id"],
        "topic_id": topic["id"],
        "source_id": source["id"],
        "title": "GPU",
        "url": "https://news.example/a",
        "content_hash": "same",
    }
    first = db.insert_article(value)
    second = db.insert_article(value)
    assert second["id"] == first["id"]
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
        fts_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'article_fts'"
        ).fetchone()
        if fts_exists:
            assert connection.execute("SELECT COUNT(*) FROM article_fts").fetchone()[0] == 1


def test_export_articles_is_not_limited_to_search_page_size(backend_home: Path) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    topic = db.create_topic(TopicInput(name="AI", keywords=["GPU"], source_ids=[source["id"]]))
    run = db.create_run([topic["id"]], "test")
    for index in range(501):
        db.insert_article(
            {
                "run_id": run["id"],
                "topic_id": topic["id"],
                "source_id": source["id"],
                "title": f"GPU {index}",
                "url": f"https://news.example/{index}",
                "content_hash": str(index),
            }
        )
    first_page = db.search_articles(limit=200)
    second_page = db.search_articles(limit=200, offset=200)
    third_page = db.search_articles(limit=200, offset=400)
    assert [len(first_page), len(second_page), len(third_page)] == [200, 200, 101]
    assert len({item["id"] for item in first_page + second_page + third_page}) == 501
    assert db.count_articles(topic_id=topic["id"]) == 501

    for article in first_page[:7]:
        db.set_favorite(article["id"], True)
    assert db.count_articles(topic_id=topic["id"], favorite=True) == 7
    assert len(db.search_articles(topic_id=topic["id"], favorite=True)) == 7
    assert len(db.export_articles()) == 501


@pytest.mark.asyncio
async def test_artifact_failure_restores_previous_latest_good(
    backend_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    topic = db.create_topic(
        TopicInput(name="AI 芯片", keywords=["GPU"], threshold=50, source_ids=[source["id"]])
    )
    previous = db.create_run([topic["id"]], "test")
    db.insert_article(
        {
            "run_id": previous["id"],
            "topic_id": topic["id"],
            "source_id": source["id"],
            "title": "GPU 上一份完整报告",
            "url": "https://news.example/previous",
            "content_hash": "previous",
        }
    )
    db.update_run(previous["id"], status="complete", article_count=1)
    db.set_latest_good_run(previous["id"])

    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=ControlledFeeds())
    original_writer = manager._write_artifact
    attempts = 0

    def fail_first_write(run: dict, source_errors: list[dict[str, str]]) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Report promotion must not precede the atomic artifact write: a
            # force quit here should leave the old report selected.
            assert db.get_latest_good_run_id() == previous["id"]
            raise OSError("simulated disk failure")
        return original_writer(run, source_errors)

    monkeypatch.setattr(manager, "_write_artifact", fail_first_write)
    current = await manager.run_now(RunRequest())

    assert current["status"] == "failed"
    assert db.get_latest_good_run_id() == previous["id"]
    assert manager.latest_report()["run"]["id"] == previous["id"]
    failed_artifact = json.loads((paths.runs / f"{current['id']}.json").read_text("utf-8"))
    assert failed_artifact["run"]["status"] == "failed"


@pytest.mark.asyncio
async def test_retention_cleanup_failure_does_not_demote_report(
    backend_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    db.create_topic(
        TopicInput(name="AI 芯片", keywords=["GPU"], threshold=50, source_ids=[source["id"]])
    )
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=ControlledFeeds())

    def fail_cleanup(_days: int) -> int:
        raise OSError("simulated retention cleanup failure")

    monkeypatch.setattr(db, "purge_expired_full_text", fail_cleanup)
    run = await manager.run_now(RunRequest())

    assert run["status"] == "complete"
    assert db.get_latest_good_run_id() == run["id"]
    artifact = json.loads((paths.runs / f"{run['id']}.json").read_text("utf-8"))
    assert artifact["run"]["status"] == "complete"


@pytest.mark.asyncio
async def test_failed_run_still_executes_retention_cleanup(
    backend_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Bad feed", url="https://bad.example/feed.xml", enabled=True)
    )
    db.create_topic(
        TopicInput(name="AI 芯片", keywords=["GPU"], threshold=50, source_ids=[source["id"]])
    )
    cleanup_calls: list[int] = []
    original_cleanup = db.purge_expired_full_text

    def record_cleanup(days: int) -> int:
        cleanup_calls.append(days)
        return original_cleanup(days)

    monkeypatch.setattr(db, "purge_expired_full_text", record_cleanup)
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=ControlledFeeds())

    run = await manager.run_now(RunRequest())

    assert run["status"] == "failed"
    assert cleanup_calls == [30]


@pytest.mark.asyncio
async def test_canonical_run_artifact_refuses_overwrite(backend_home: Path) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Good feed", url="https://good.example/feed.xml", enabled=True)
    )
    db.create_topic(
        TopicInput(name="AI 芯片", keywords=["GPU"], threshold=50, source_ids=[source["id"]])
    )
    manager = RunManager(db, paths, key_store=NoKeyStore(), feed_service=ControlledFeeds())
    run = await manager.run_now(RunRequest())
    artifact_path = paths.runs / f"{run['id']}.json"
    original = artifact_path.read_bytes()

    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        manager._write_artifact(run, [])

    assert artifact_path.read_bytes() == original


def test_latest_good_pointer_rejects_failed_or_empty_runs(backend_home: Path) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    failed = db.create_run([], "test")
    db.update_run(failed["id"], status="failed")
    db.set_latest_good_run(failed["id"])
    assert db.get_latest_good_run_id() is None

    empty = db.create_run([], "test")
    db.update_run(empty["id"], status="complete")
    db.set_latest_good_run(empty["id"])
    assert db.get_latest_good_run_id() is None
