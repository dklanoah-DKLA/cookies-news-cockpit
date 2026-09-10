from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from cookies_news_cockpit.models import FeedArticle, RunRequest, SourceInput, TopicInput
from cookies_news_cockpit.pipeline import RunManager
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


class TwoArticleFeed:
    async def fetch_metadata(self, source: dict) -> list[FeedArticle]:
        published_at = datetime.now(UTC).isoformat()
        return [
            FeedArticle(
                title="GPU first",
                url="https://atomic.example/1",
                source_id=source["id"],
                source_name=source["name"],
                published_at=published_at,
                full_text="GPU report one",
            ),
            FeedArticle(
                title="GPU second",
                url="https://atomic.example/2",
                source_id=source["id"],
                source_name=source["name"],
                published_at=published_at,
                full_text="GPU report two",
            ),
        ]


@pytest.fixture
def atomic_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "atomic-v12-tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


@pytest.mark.asyncio
async def test_persistence_failure_rolls_back_whole_report_and_fts(
    atomic_home: Path,
) -> None:
    paths = resolve_paths(atomic_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Atomic feed", url="https://atomic.example/feed.xml")
    )
    db.create_topic(
        TopicInput(
            name="GPU",
            keywords=["GPU"],
            article_limit=2,
            source_ids=[source["id"]],
        )
    )
    with db.connect() as connection:
        connection.executescript(
            """
            CREATE TRIGGER fail_second_report_article
            BEFORE INSERT ON articles
            WHEN NEW.title = 'GPU second'
            BEGIN
                SELECT RAISE(ABORT, 'simulated report persistence failure');
            END;
            """
        )
    manager = RunManager(
        db,
        paths,
        key_store=NoKeyStore(),
        feed_service=TwoArticleFeed(),
    )

    run = await manager.run_now(RunRequest(), trigger="manual")

    assert run["status"] == "failed"
    assert run["article_count"] == 0
    assert db.list_run_articles(run["id"]) == []
    assert db.count_articles() == 0
    with db.connect() as connection:
        fts_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'article_fts'"
        ).fetchone()
        if fts_exists:
            assert connection.execute("SELECT COUNT(*) FROM article_fts").fetchone()[0] == 0
