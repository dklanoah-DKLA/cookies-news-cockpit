from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cookies_news_cockpit.models import DeepSeekKeyInput, SourceInput, TopicInput
from cookies_news_cockpit.storage import Database, TopicLimitError


@pytest.fixture
def storage_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "storage-v11-tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


def test_clean_database_has_v2_defaults_and_neutral_catalog(storage_home: Path) -> None:
    # The parent directory does not exist yet: this is the packaged app's true
    # first-launch shape, not merely an empty SQLite file in an existing folder.
    db = Database(storage_home / "new-install" / "cockpit.sqlite3")

    sources = db.list_sources()
    enabled_presets = {item["preset_id"] for item in sources if item["enabled"]}

    assert db.get_schema_version() == 2
    assert db.get_settings().freshness_days == 7
    assert db.get_settings().onboarding_completed is False
    assert len(sources) == 24
    assert enabled_presets == {
        "preset-chinanews-scroll",
        "preset-un-zh",
        "preset-nsf-news",
        "preset-wto-news",
    }
    assert {item["language"] for item in sources} == {"zh", "en"}
    assert all(item["homepage"] and item["terms"] for item in sources)


def _make_v1_sources_database(path: Path, *, chinaorg_enabled: bool = False) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0,
                last_status TEXT,
                last_checked_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        now = "2026-01-01T00:00:00+00:00"
        connection.execute(
            "INSERT INTO sources VALUES (?, ?, ?, 1, 0, NULL, NULL, NULL, ?, ?)",
            (
                "preset-chinanews-scroll",
                "中新网 · 即时新闻",
                "https://www.chinanews.com.cn/rss/scroll-news.xml",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?)",
            (
                "preset-chinaorg-top",
                "中国网 · Top News",
                "http://www.china.org.cn/rss/1185842.xml",
                int(chinaorg_enabled),
                now,
                now,
            ),
        )


def test_v1_upgrade_preserves_scope_and_archives_untouched_chinaorg(
    storage_home: Path,
) -> None:
    path = storage_home / "legacy.sqlite3"
    _make_v1_sources_database(path)

    db = Database(path)

    assert db.get_settings().onboarding_completed is True
    assert {item["preset_id"] for item in db.list_sources() if item["enabled"]} == {
        "preset-chinanews-scroll"
    }
    assert db.get_source("preset-chinaorg-top")["archived"] is True
    assert db.metadata_warning() is None

    # A second startup is idempotent and does not re-enable new defaults.
    reopened = Database(path)
    assert len(reopened.list_sources()) == 24
    assert sum(item["enabled"] for item in reopened.list_sources()) == 1


def test_v1_upgrade_keeps_enabled_chinaorg_with_warning(storage_home: Path) -> None:
    path = storage_home / "legacy-enabled.sqlite3"
    _make_v1_sources_database(path, chinaorg_enabled=True)

    db = Database(path)

    assert db.get_source("preset-chinaorg-top")["archived"] is False
    assert db.metadata_warning()["source_id"] == "preset-chinaorg-top"


def test_v1_upgrade_recovers_legacy_article_provenance(storage_home: Path) -> None:
    path = storage_home / "legacy-history.sqlite3"
    seed = Database(path)
    source = seed.create_source(
        SourceInput(name="Legacy feed", url="https://legacy.example/feed.xml")
    )
    topic = seed.create_topic(
        TopicInput(name="Legacy topic", keywords=["legacy"], source_ids=[source["id"]])
    )
    run = seed.create_run([topic["id"]], "manual")
    legacy_analyses = {
        "legacy-ai": "该事件与主题直接相关，并可能改变后续市场节奏。",
        "legacy-rules-no-key": "关键词规则初筛；配置 DeepSeek API Key 后可获得 AI 分析。",
        "legacy-rules-failed": "DeepSeek 本次未生成分析；已使用关键词规则初筛。",
        "legacy-rules-cap": "已达到单次 AI 分析安全上限；使用关键词规则初筛。",
        "legacy-rules-empty": "",
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE articles")
        connection.execute(
            """CREATE TABLE articles (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                excerpt TEXT NOT NULL DEFAULT '',
                full_text TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                analysis TEXT NOT NULL DEFAULT '',
                score INTEGER,
                published_at TEXT,
                created_at TEXT NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT NOT NULL,
                UNIQUE(run_id, topic_id, content_hash)
            )"""
        )
        for index, (article_id, analysis) in enumerate(legacy_analyses.items()):
            connection.execute(
                """INSERT INTO articles
                (id, run_id, topic_id, source_id, title, url, excerpt, summary,
                 analysis, score, created_at, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    article_id,
                    run["id"],
                    topic["id"],
                    source["id"],
                    f"Legacy article {index}",
                    f"https://legacy.example/{index}",
                    "Legacy excerpt",
                    "Legacy summary",
                    analysis,
                    80,
                    "2026-01-01T00:00:00+00:00",
                    f"legacy-hash-{index}",
                ),
            )
        connection.execute("PRAGMA user_version = 1")
        connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")

    upgraded = Database(path)
    saved = {item["id"]: item for item in upgraded.list_run_articles(run["id"])}

    assert saved["legacy-ai"]["analysis_mode"] == "ai_legacy"
    assert saved["legacy-ai"]["model"] == "deepseek-chat"
    for article_id in (
        "legacy-rules-no-key",
        "legacy-rules-failed",
        "legacy-rules-cap",
        "legacy-rules-empty",
    ):
        assert saved[article_id]["analysis_mode"] == "rules"
        assert saved[article_id]["model"] is None

    reopened = Database(path)
    saved_again = {item["id"]: item for item in reopened.list_run_articles(run["id"])}
    assert saved_again["legacy-ai"]["analysis_mode"] == "ai_legacy"
    assert saved_again["legacy-ai"]["model"] == "deepseek-chat"


def test_keywords_normalize_common_separators_and_key_length_after_strip() -> None:
    topic = TopicInput(
        name="通用 AI",
        keywords=["AI，人工智能\nmachine-learning; machine learning", "ＡＩ"],
        exclusion_keywords=["广告、招聘； 软文"],
    )

    assert topic.keywords == ["AI", "人工智能", "machine-learning", "machine learning"]
    assert topic.exclusion_keywords == ["广告", "招聘", "软文"]
    with pytest.raises(ValidationError):
        DeepSeekKeyInput(api_key="abc     ")


def test_source_restore_recovers_topic_bindings(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    source = db.create_source(SourceInput(name="Example", url="https://example.com/feed.xml"))
    topic = db.create_topic(TopicInput(name="Example topic", source_ids=[source["id"]]))

    db.delete_source(source["id"])
    assert source["id"] not in db.get_topic(topic["id"])["source_ids"]

    restored = db.restore_source(source["id"])
    assert restored["archived"] is False
    assert source["id"] in db.get_topic(topic["id"])["source_ids"]


def test_run_article_metadata_and_ai_cache_round_trip(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    source = db.create_source(SourceInput(name="Example", url="https://example.com/feed.xml"))
    topic = db.create_topic(TopicInput(name="Example topic", source_ids=[source["id"]]))
    run = db.create_run([topic["id"]], "test")
    run = db.update_run(
        run["id"],
        status="running",
        phase="analyzing",
        outcome="semantic_fallback",
        funnel={"fetched": 12, "fresh": 8},
        ai_requests=3,
        ai_success=2,
        ai_failure=1,
    )
    article = db.insert_article(
        {
            "run_id": run["id"],
            "topic_id": topic["id"],
            "source_id": source["id"],
            "title": "AI policy",
            "url": "https://example.com/ai",
            "content_hash": "hash-1",
            "analysis_mode": "deepseek",
            "model": "deepseek-v4-flash",
            "matched_keywords": ["AI"],
            "matched_fields": ["title"],
        }
    )

    assert run["funnel"] == {"fetched": 12, "fresh": 8}
    assert (run["ai_requests"], run["ai_success"], run["ai_failure"]) == (3, 2, 1)
    assert article["analysis_mode"] == "deepseek"
    assert article["model"] == "deepseek-v4-flash"
    assert article["matched_keywords"] == ["AI"]
    assert article["matched_fields"] == ["title"]

    semantic = {"keywords": ["AI"], "threshold": 60}
    db.put_ai_cache("hash-1", semantic, "deepseek-v4-flash", {"score": 88})
    assert db.get_ai_cache("hash-1", semantic, "deepseek-v4-flash") == {"score": 88}
    assert db.get_ai_cache("hash-1", {"keywords": ["other"]}, "deepseek-v4-flash") is None
    with db.connect() as connection:
        connection.execute("UPDATE ai_cache SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert db.purge_ai_cache() == 1


def test_apply_import_merges_by_stable_keys_and_ors_favorites(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    configuration = {
        "schema_version": 1,
        "sources": [
            {
                "id": "old-source",
                "name": "Imported feed",
                "url": "https://import.example/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "old-topic",
                "name": "Imported topic",
                "keywords": ["signal"],
                "source_ids": ["old-source"],
                "enabled": True,
            }
        ],
    }
    articles = [
        {
            "topic_id": "old-topic",
            "source_id": "old-source",
            "title": "Signal found",
            "url": "https://import.example/1",
            "content_hash": "stable-hash",
            "favorite": False,
        }
    ]

    preview = db.preview_import(configuration, articles)
    assert preview["topics"]["new"] == 1
    first = db.apply_import(configuration, articles)
    assert first["articles_added"] == 1

    articles[0]["favorite"] = True
    second = db.apply_import(configuration, articles)
    assert second["articles_added"] == 0
    assert second["articles_matched"] == 1
    assert second["favorites_merged"] == 1
    saved = db.search_articles(query="Signal")
    assert len(saved) == 1
    assert saved[0]["favorite"] is True


def test_v1_zip_import_recovers_legacy_article_provenance(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    configuration = {
        "schema_version": 1,
        "sources": [
            {
                "id": "legacy-source",
                "name": "Legacy backup feed",
                "url": "https://legacy-backup.example/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "legacy-topic",
                "name": "Legacy backup topic",
                "keywords": ["legacy"],
                "source_ids": ["legacy-source"],
                "enabled": True,
            }
        ],
    }
    articles = [
        {
            "id": "legacy-ai",
            "topic_id": "legacy-topic",
            "source_id": "legacy-source",
            "title": "Legacy AI story",
            "url": "https://legacy-backup.example/ai",
            "summary": "AI summary",
            "analysis": "该事件与主题直接相关，并可能改变后续市场节奏。",
            "score": 88,
        },
        {
            "id": "legacy-rules",
            "topic_id": "legacy-topic",
            "source_id": "legacy-source",
            "title": "Legacy rules story",
            "url": "https://legacy-backup.example/rules",
            "summary": "Rules summary",
            "analysis": "关键词规则初筛；配置 DeepSeek API Key 后可获得 AI 分析。",
            "score": 60,
        },
    ]

    imported = db.apply_import(configuration, articles)
    saved = {
        item["title"]: item for item in db.list_run_articles(imported["latest_imported_run_id"])
    }

    assert saved["Legacy AI story"]["analysis_mode"] == "ai_legacy"
    assert saved["Legacy AI story"]["model"] == "deepseek-chat"
    assert saved["Legacy rules story"]["analysis_mode"] == "rules"
    assert saved["Legacy rules story"]["model"] is None


def test_restore_topic_enables_it_and_enforces_active_limit(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    target = db.create_topic(TopicInput(name="Restore me", enabled=False))
    db.delete_topic(target["id"])

    restored = db.restore_topic(target["id"])
    assert restored["archived"] is False
    assert restored["enabled"] is True

    db.delete_topic(target["id"])
    for index in range(10):
        db.create_topic(TopicInput(name=f"Active {index}"))
    with pytest.raises(TopicLimitError):
        db.restore_topic(target["id"])
    assert db.get_topic(target["id"])["archived"] is True


def test_import_preview_matches_apply_for_normalized_url_and_topic_scoped_dedup(
    storage_home: Path,
) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    local_source = db.create_source(
        SourceInput(name="Local feed", url="https://import.example/feed.xml")
    )
    configuration = {
        "schema_version": 1,
        "sources": [
            {
                "id": "source-1",
                "name": "Backup feed",
                "url": "HTTPS://IMPORT.EXAMPLE/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "topic-1",
                "name": "Topic one",
                "keywords": ["signal"],
                "source_ids": ["source-1"],
                "enabled": True,
            },
            {
                "id": "topic-2",
                "name": "Topic two",
                "keywords": ["signal"],
                "source_ids": ["source-1"],
                "enabled": True,
            },
        ],
    }
    articles = [
        {
            "id": "article-1",
            "topic_id": "topic-1",
            "source_id": "source-1",
            "title": "Same signal",
            "url": "https://import.example/story",
        },
        {
            "id": "article-2",
            "topic_id": "topic-1",
            "source_id": "source-1",
            "title": "Same signal",
            "url": "https://import.example/story",
            "content_hash": "",
        },
        {
            "id": "article-3",
            "topic_id": "topic-2",
            "source_id": "source-1",
            "title": "Same signal",
            "url": "https://import.example/story",
        },
    ]

    preview = db.preview_import(configuration, articles)
    assert preview["sources"] == {"incoming": 1, "new": 0, "matched": 1}
    assert preview["articles"] == {"incoming": 3, "new": 2, "matched": 1}

    applied = db.apply_import(configuration, articles)
    assert applied["sources_added"] == preview["sources"]["new"]
    assert applied["sources_matched"] == preview["sources"]["matched"]
    assert applied["topics_added"] == preview["topics"]["new"]
    assert applied["topics_matched"] == preview["topics"]["matched"]
    assert applied["articles_added"] == preview["articles"]["new"]
    assert applied["articles_matched"] == preview["articles"]["matched"]
    assert applied["runs_added"] == 1
    assert applied["latest_imported_run_id"] == applied["run_id"]
    assert db.get_run(applied["run_id"])["article_count"] == 2
    assert db.count_articles() == 2
    assert db.get_source(local_source["id"])["name"] == "Local feed"

    second_preview = db.preview_import(configuration, articles)
    second = db.apply_import(configuration, articles)
    assert second_preview["articles"] == {"incoming": 3, "new": 0, "matched": 3}
    assert second["articles_added"] == 0
    assert second["articles_matched"] == 3
    assert second["runs_added"] == 0
    assert second["latest_imported_run_id"] is None


def test_import_rejects_duplicate_ids_dangling_references_and_loose_types(
    storage_home: Path,
) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    source = {
        "id": "source-1",
        "name": "Feed",
        "url": "https://backup.example/feed.xml",
        "enabled": True,
    }
    topic = {
        "id": "topic-1",
        "name": "Topic",
        "source_ids": ["source-1"],
        "enabled": True,
    }
    article = {
        "id": "article-1",
        "topic_id": "topic-1",
        "source_id": "source-1",
        "title": "Story",
        "url": "https://backup.example/story",
    }

    invalid_payloads = [
        ({"sources": [source, {**source, "url": "https://other.example/feed"}], "topics": []}, []),
        (
            {
                "sources": [source],
                "topics": [topic, {**topic, "name": "Other topic"}],
            },
            [],
        ),
        ({"sources": [source], "topics": [topic]}, [article, {**article}]),
        (
            {
                "sources": [source],
                "topics": [{**topic, "source_ids": ["missing-source"]}],
            },
            [],
        ),
        (
            {"sources": [source], "topics": [topic]},
            [{**article, "source_id": "missing-source"}],
        ),
        (
            {"sources": [{**source, "enabled": "true"}], "topics": []},
            [],
        ),
        (
            {"sources": [source], "topics": [topic]},
            [{**article, "favorite": "yes"}],
        ),
        (
            {"sources": [source], "topics": [topic]},
            [{**article, "score": True}],
        ),
        (
            {"sources": [source], "topics": [topic]},
            [{**article, "url": "javascript:alert(document.domain)"}],
        ),
    ]
    for configuration, articles in invalid_payloads:
        with pytest.raises((ValueError, ValidationError)):
            db.preview_import(configuration, articles)

    valid_configuration = {"sources": [source], "topics": [topic]}
    run = {
        "id": "run-1",
        "status": "complete",
        "topic_ids": ["topic-1"],
        "started_at": "2026-09-01T00:00:00+00:00",
        "finished_at": "2026-09-01T00:01:00+00:00",
    }
    with pytest.raises(ValueError, match="重复 run id"):
        db.preview_import(valid_configuration, [], [run, {**run}])
    with pytest.raises(ValueError, match="不存在的运行记录"):
        db.preview_import(
            valid_configuration,
            [{**article, "run_id": "missing-run"}],
            [run],
        )


@pytest.mark.parametrize(
    "run_id",
    [
        ".",
        "..",
        " run-id",
        "run-id ",
        "../escape",
        "..\\escape",
        "/tmp/absolute",
        "C:\\Temp\\drive-path",
        "folder/child",
        "CON",
    ],
)
def test_import_rejects_run_ids_that_are_unsafe_as_filenames(
    storage_home: Path,
    run_id: str,
) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    configuration = {
        "schema_version": 2,
        "sources": [],
        "topics": [],
    }
    run = {
        "id": run_id,
        "status": "complete",
        "topic_ids": [],
        "started_at": "2026-09-01T00:00:00+00:00",
        "finished_at": "2026-09-01T00:01:00+00:00",
    }

    with pytest.raises(ValueError, match="安全的文件名"):
        db.preview_import(configuration, [], [run])


def test_import_accepts_legacy_safe_run_filename_token(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    run = {
        "id": "run-2026_09.legacy",
        "status": "complete",
        "topic_ids": [],
        "started_at": "2026-09-01T00:00:00+00:00",
        "finished_at": "2026-09-01T00:01:00+00:00",
    }

    preview = db.preview_import(
        {"schema_version": 2, "sources": [], "topics": []},
        [],
        [run],
    )

    assert preview["runs"] == {"incoming": 1, "new": 1, "matched": 0, "remapped": 0}


def test_v2_articles_cannot_fall_back_to_synthetic_run(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    configuration = {
        "schema_version": 2,
        "sources": [
            {
                "id": "source-1",
                "name": "Feed",
                "url": "https://feed.example/rss",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "topic-1",
                "name": "Topic",
                "source_ids": ["source-1"],
                "enabled": True,
            }
        ],
    }
    articles = [
        {
            "id": "article-1",
            "run_id": "",
            "topic_id": "topic-1",
            "source_id": "source-1",
            "title": "Missing indexed run",
            "url": "https://feed.example/story",
        }
    ]

    with pytest.raises(ValueError, match="runs.json 中不存在"):
        db.preview_import(configuration, articles, [])
    with pytest.raises(ValueError, match="runs.json 中不存在"):
        db.apply_import(configuration, articles, [])

    assert db.count_articles() == 0
    assert db.export_runs() == []


def test_ambiguous_source_fallback_rolls_back_entire_import(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    db.create_source(SourceInput(name="Duplicate", url="https://one.example/feed"))
    db.create_source(SourceInput(name="Duplicate", url="https://two.example/feed"))
    before_sources = len(db.list_sources(include_archived=True))
    before_topics = len(db.list_topics(include_archived=True))
    configuration = {
        "schema_version": 1,
        "sources": [
            {
                "id": "new-source",
                "name": "Inserted before failure",
                "url": "https://new.example/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "new-topic",
                "name": "Inserted before failure",
                "source_ids": ["new-source"],
                "enabled": True,
            }
        ],
    }
    articles = [
        {
            "id": "article-1",
            "topic_id": "new-topic",
            "source_name": "Duplicate",
            "title": "Ambiguous",
            "url": "https://duplicate.example/story",
        }
    ]

    with pytest.raises(ValueError, match="来源名称不唯一"):
        db.apply_import(configuration, articles)

    assert len(db.list_sources(include_archived=True)) == before_sources
    assert len(db.list_topics(include_archived=True)) == before_topics
    assert db.count_articles() == 0


def test_import_restores_runs_remaps_conflicts_and_is_idempotent(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    local_source = db.create_source(SourceInput(name="Local", url="https://local.example/feed.xml"))
    local_topic = db.create_topic(TopicInput(name="Local topic", source_ids=[local_source["id"]]))
    local_run = db.create_run([local_topic["id"]], "test")
    db.insert_article(
        {
            "run_id": local_run["id"],
            "topic_id": local_topic["id"],
            "source_id": local_source["id"],
            "title": "Local story",
            "url": "https://local.example/story",
            "content_hash": "local-hash",
        }
    )
    db.update_run(
        local_run["id"],
        status="complete",
        phase="finished",
        outcome="complete",
        article_count=1,
        finished_at="2026-09-01T00:01:00+00:00",
    )
    db.set_latest_good_run(local_run["id"])

    configuration = {
        "schema_version": 2,
        "sources": [
            {
                "id": "source-1",
                "name": "Backup feed",
                "url": "https://backup.example/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "topic-1",
                "name": "Backup topic",
                "keywords": ["robot"],
                "source_ids": ["source-1"],
                "enabled": True,
            }
        ],
    }
    runs = [
        {
            "id": local_run["id"],
            "status": "degraded",
            "phase": "finished",
            "outcome": "partial",
            "trigger": "manual",
            "topic_ids": ["topic-1"],
            "started_at": "2026-09-02T00:00:00+00:00",
            "finished_at": "2026-09-02T00:01:00+00:00",
            "article_count": 1,
            "source_success": 4,
            "source_failure": 1,
            "analyses": 3,
            "duplicates_skipped": 2,
            "funnel": {"fetched": 12, "keyword_matched": 5},
            "ai_requests": 4,
            "ai_success": 3,
            "ai_failure": 1,
            "warning": "partial feed failure",
            "error": None,
        }
    ]
    articles = [
        {
            "id": "backup-article",
            "run_id": local_run["id"],
            "topic_id": "topic-1",
            "source_id": "source-1",
            "title": "Robot market",
            "url": "https://backup.example/robot",
            "content_hash": "robot-hash",
            "score": 88,
            "analysis_mode": "deepseek",
            "model": "deepseek-v4-flash",
            "matched_keywords": ["robot"],
            "matched_fields": ["title"],
        }
    ]

    first_preview = db.preview_import(configuration, articles, runs)
    assert first_preview["runs"] == {
        "incoming": 1,
        "new": 1,
        "matched": 0,
        "remapped": 1,
    }
    first = db.apply_import(configuration, articles, runs)
    imported_run_id = first["latest_imported_run_id"]
    assert first["runs_added"] == 1
    assert first["runs_remapped"] == 1
    assert imported_run_id == first["run_id"]
    assert imported_run_id != local_run["id"]
    restored_run = db.get_run(imported_run_id)
    imported_topic = next(topic for topic in db.list_topics() if topic["name"] == "Backup topic")
    assert restored_run["topic_ids"] == [imported_topic["id"]]
    assert restored_run["status"] == "degraded"
    assert restored_run["outcome"] == "partial"
    assert restored_run["source_success"] == 4
    assert restored_run["source_failure"] == 1
    assert restored_run["analyses"] == 3
    assert restored_run["duplicates_skipped"] == 2
    assert restored_run["funnel"] == {"fetched": 12, "keyword_matched": 5}
    assert (
        restored_run["ai_requests"],
        restored_run["ai_success"],
        restored_run["ai_failure"],
    ) == (4, 3, 1)
    imported_article = db.list_run_articles(imported_run_id)[0]
    assert imported_article["id"] == "backup-article"
    assert imported_article["analysis_mode"] == "deepseek"
    assert imported_article["model"] == "deepseek-v4-flash"
    assert db.get_latest_good_run_id() == local_run["id"]

    run_count = len(db.export_runs())
    second_preview = db.preview_import(configuration, articles, runs)
    assert second_preview["runs"] == {
        "incoming": 1,
        "new": 0,
        "matched": 1,
        "remapped": 0,
    }
    second = db.apply_import(configuration, articles, runs)
    assert second["runs_added"] == 0
    assert second["runs_matched"] == 1
    assert second["articles_added"] == 0
    assert len(db.export_runs()) == run_count


def test_imported_active_run_is_interrupted_and_never_promoted(storage_home: Path) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    configuration = {
        "schema_version": 2,
        "sources": [
            {
                "id": "source-1",
                "name": "Feed",
                "url": "https://active.example/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "topic-1",
                "name": "Active topic",
                "source_ids": ["source-1"],
                "enabled": True,
            }
        ],
    }
    runs = [
        {
            "id": "active-run",
            "status": "running",
            "phase": "analyzing",
            "trigger": "manual",
            "topic_ids": ["topic-1"],
            "started_at": "2026-09-03T00:00:00+00:00",
        }
    ]
    articles = [
        {
            "id": "active-article",
            "run_id": "active-run",
            "topic_id": "topic-1",
            "source_id": "source-1",
            "title": "Interrupted story",
            "url": "https://active.example/story",
            "content_hash": "active-hash",
        }
    ]

    result = db.apply_import(configuration, articles, runs)
    restored = db.get_run("active-run")
    assert restored["status"] == "failed"
    assert restored["phase"] == "finished"
    assert restored["outcome"] == "interrupted"
    assert restored["finished_at"] == restored["started_at"]
    assert "安全标记为中断" in restored["error"]
    assert result["latest_imported_run_id"] is None
    assert result["run_id"] is None
    assert db.get_latest_good_run_id() is None


def test_import_preview_and_apply_share_unicode_casefold_name_matching(
    storage_home: Path,
) -> None:
    db = Database(storage_home / "cockpit.sqlite3")
    local_source = db.create_source(
        SourceInput(name="Straße Quelle", url="https://unicode.example/feed.xml")
    )
    local_topic = db.create_topic(
        TopicInput(name="Straße", source_ids=[local_source["id"]])
    )
    source_count = len(db.list_sources(include_archived=True))
    configuration = {
        "schema_version": 1,
        "sources": [
            {
                "id": "source-1",
                "name": "STRASSE QUELLE",
                "url": "https://unicode.example/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "topic-1",
                "name": "STRASSE",
                "source_ids": ["source-1"],
                "enabled": True,
            }
        ],
    }
    articles = [
        {
            "id": "unicode-article",
            "topic_id": "",
            "topic_name": "STRASSE",
            "source_id": "",
            "source_name": "STRASSE QUELLE",
            "title": "Unicode-safe import",
            "url": "https://unicode.example/story",
            "content_hash": "unicode-hash",
        }
    ]

    preview = db.preview_import(configuration, articles)
    assert preview["topics"] == {"incoming": 1, "new": 0, "matched": 1}
    assert preview["sources"] == {"incoming": 1, "new": 0, "matched": 1}
    assert preview["articles"] == {"incoming": 1, "new": 1, "matched": 0}

    applied = db.apply_import(configuration, articles)
    assert applied["topics_added"] == 0
    assert applied["topics_matched"] == 1
    assert applied["sources_added"] == 0
    assert applied["sources_matched"] == 1
    assert len(db.list_topics(include_archived=True)) == 1
    assert len(db.list_sources(include_archived=True)) == source_count
    imported = db.search_articles(query="Unicode-safe import")
    assert len(imported) == 1
    assert imported[0]["topic_id"] == local_topic["id"]
    assert imported[0]["source_id"] == local_source["id"]
