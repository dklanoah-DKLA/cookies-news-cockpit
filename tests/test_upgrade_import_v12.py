from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cookies_news_cockpit.api import create_app
from cookies_news_cockpit.models import SettingsUpdate, SourceInput, TopicInput
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.storage import (
    UPGRADE_CHECKPOINT,
    UPGRADE_SNAPSHOT_NAME,
    Database,
    TopicLimitError,
)

V11_SOURCE_ENABLED_ID = "v11-source-enabled"
V11_SOURCE_DISABLED_ID = "v11-source-disabled"
V11_TOPIC_ID = "v11-topic"
V11_RUN_ID = "v11-run"
V11_ARTICLE_ID = "v11-article"
V11_CONTENT_HASH = "a" * 64


class MemoryKeyStore:
    def __init__(self, value: str | None = None):
        self.value = value

    def get(self) -> str | None:
        return self.value

    def set(self, value: str) -> None:
        self.value = value

    def delete(self) -> None:
        self.value = None


@pytest.fixture
def v12_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "upgrade-v12-tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


def _freeze_v11_database(path: Path) -> None:
    """Create a frozen 1.1 schema-v2 database without running 1.2 code."""

    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {
        "default_threshold": 71,
        "default_article_limit": 13,
        "freshness_days": 14,
        "refresh_minutes": 120,
        "scheduler_enabled": False,
        "extract_full_text": True,
        "semantic_fallback_enabled": False,
        "semantic_fallback_limit": 6,
        "onboarding_completed": True,
        "deepseek_status": "connected",
        "deepseek_last_tested_at": "2026-09-01T00:00:00+00:00",
        "deepseek_last_error": None,
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE topics (
                id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                keywords_json TEXT NOT NULL,
                exclusion_keywords_json TEXT NOT NULL DEFAULT '[]',
                threshold INTEGER, article_limit INTEGER,
                source_ids_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE sources (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                homepage TEXT, category TEXT NOT NULL DEFAULT 'custom',
                language TEXT NOT NULL DEFAULT 'other', terms TEXT, preset_id TEXT,
                preset_version INTEGER, user_modified INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0, last_status TEXT,
                last_checked_at TEXT, last_error TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'queued', outcome TEXT,
                trigger TEXT NOT NULL, requested_topics_json TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT,
                article_count INTEGER NOT NULL DEFAULT 0,
                source_success INTEGER NOT NULL DEFAULT 0,
                source_failure INTEGER NOT NULL DEFAULT 0,
                analyses INTEGER NOT NULL DEFAULT 0,
                duplicates_skipped INTEGER NOT NULL DEFAULT 0,
                funnel_json TEXT NOT NULL DEFAULT '{}',
                ai_requests INTEGER NOT NULL DEFAULT 0,
                ai_success INTEGER NOT NULL DEFAULT 0,
                ai_failure INTEGER NOT NULL DEFAULT 0, warning TEXT, error TEXT
            );
            CREATE TABLE articles (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                title TEXT NOT NULL, url TEXT NOT NULL,
                excerpt TEXT NOT NULL DEFAULT '', full_text TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '', analysis TEXT NOT NULL DEFAULT '',
                score INTEGER, published_at TEXT, created_at TEXT NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL,
                analysis_mode TEXT NOT NULL DEFAULT 'rules', model TEXT,
                matched_keywords_json TEXT NOT NULL DEFAULT '[]',
                matched_fields_json TEXT NOT NULL DEFAULT '[]',
                UNIQUE(run_id, topic_id, content_hash)
            );
            CREATE INDEX idx_articles_run ON articles(run_id);
            CREATE INDEX idx_articles_topic ON articles(topic_id);
            CREATE INDEX idx_articles_created ON articles(created_at DESC);
            CREATE TABLE calibrations (
                id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                proposal_json TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, confirmed_at TEXT
            );
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE source_archive_snapshots (
                source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                topic_ids_json TEXT NOT NULL, deleted_at TEXT NOT NULL
            );
            CREATE TABLE ai_cache (
                cache_key TEXT PRIMARY KEY, content_fingerprint TEXT NOT NULL,
                topic_fingerprint TEXT NOT NULL, model TEXT NOT NULL,
                analysis_json TEXT NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX idx_ai_cache_expiry ON ai_cache(expires_at);
            PRAGMA user_version = 2;
            """
        )
        connection.execute(
            "INSERT INTO settings(id, payload, updated_at) VALUES(1, ?, ?)",
            (json.dumps(settings, ensure_ascii=False), "2026-09-01T00:00:00+00:00"),
        )
        connection.executemany(
            """INSERT INTO sources
            (id, name, url, homepage, category, language, terms, preset_id,
             preset_version, user_modified, enabled, archived, last_status,
             last_checked_at, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    V11_SOURCE_ENABLED_ID,
                    "用户自定义来源",
                    "https://upgrade.example/feed.xml",
                    "https://upgrade.example/",
                    "industry",
                    "zh",
                    "测试条款",
                    None,
                    None,
                    1,
                    1,
                    0,
                    "ok",
                    "2026-09-01T00:00:00+00:00",
                    None,
                    "2026-08-31T00:00:00+00:00",
                    "2026-09-01T00:00:00+00:00",
                ),
                (
                    V11_SOURCE_DISABLED_ID,
                    "暂停来源",
                    "https://disabled.example/feed.xml",
                    None,
                    "custom",
                    "en",
                    None,
                    None,
                    None,
                    1,
                    0,
                    0,
                    "error",
                    "2026-09-01T00:00:00+00:00",
                    "temporary failure",
                    "2026-08-31T00:00:00+00:00",
                    "2026-09-01T00:00:00+00:00",
                ),
            ],
        )
        connection.execute(
            """INSERT INTO topics
            (id, name, keywords_json, exclusion_keywords_json, threshold,
             article_limit, source_ids_json, enabled, archived, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
            (
                V11_TOPIC_ID,
                "保留主题",
                json.dumps(["ESG", "可持续披露"], ensure_ascii=False),
                json.dumps(["招聘", "广告"], ensure_ascii=False),
                73,
                9,
                json.dumps([V11_SOURCE_ENABLED_ID, V11_SOURCE_DISABLED_ID]),
                "2026-08-31T00:00:00+00:00",
                "2026-09-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """INSERT INTO runs
            (id, status, phase, outcome, trigger, requested_topics_json,
             started_at, finished_at, article_count, source_success,
             source_failure, analyses, duplicates_skipped, funnel_json,
             ai_requests, ai_success, ai_failure, warning, error)
            VALUES (?, 'complete', 'finished', 'complete', 'manual', ?, ?, ?,
                    1, 1, 0, 1, 0, ?, 1, 1, 0, NULL, NULL)""",
            (
                V11_RUN_ID,
                json.dumps([V11_TOPIC_ID]),
                "2026-09-01T00:00:00+00:00",
                "2026-09-01T00:01:00+00:00",
                json.dumps(
                    {
                        "feed_items": 1,
                        "keyword_hits": 1,
                        "selected": 1,
                        "per_topic": {V11_TOPIC_ID: {"name": "保留主题", "selected": 1}},
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.execute(
            """INSERT INTO articles
            (id, run_id, topic_id, source_id, title, url, excerpt, full_text,
             summary, analysis, score, published_at, created_at, favorite,
             content_hash, analysis_mode, model, matched_keywords_json,
             matched_fields_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (
                V11_ARTICLE_ID,
                V11_RUN_ID,
                V11_TOPIC_ID,
                V11_SOURCE_ENABLED_ID,
                "冻结的 1.1 历史新闻",
                "https://upgrade.example/news/1",
                "历史摘要",
                "历史全文",
                "AI 摘要",
                "AI 分析",
                88,
                "2026-09-01T00:00:00+00:00",
                "2026-09-01T00:01:00+00:00",
                V11_CONTENT_HASH,
                "ai",
                "deepseek-v4-flash",
                json.dumps(["ESG"], ensure_ascii=False),
                json.dumps(["title"], ensure_ascii=False),
            ),
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            [("schema_version", "2"), ("latest_good_run_id", V11_RUN_ID)],
        )


def _preserved_v11_state(db: Database) -> dict[str, object]:
    topic = db.get_topic(V11_TOPIC_ID)
    enabled_source = db.get_source(V11_SOURCE_ENABLED_ID)
    disabled_source = db.get_source(V11_SOURCE_DISABLED_ID)
    article = db.get_article(V11_ARTICLE_ID)
    run = db.get_run(V11_RUN_ID)
    settings = db.get_settings()
    return {
        "settings": {
            "default_threshold": settings.default_threshold,
            "default_article_limit": settings.default_article_limit,
            "freshness_days": settings.freshness_days,
            "refresh_minutes": settings.refresh_minutes,
            "scheduler_enabled": settings.scheduler_enabled,
            "extract_full_text": settings.extract_full_text,
            "semantic_fallback_enabled": settings.semantic_fallback_enabled,
            "semantic_fallback_limit": settings.semantic_fallback_limit,
            "onboarding_completed": settings.onboarding_completed,
            "deepseek_status": settings.deepseek_status,
        },
        "topic": {
            "keywords": topic["keywords"],
            "exclusion_keywords": topic["exclusion_keywords"],
            "threshold": topic["threshold"],
            "article_limit": topic["article_limit"],
            "source_ids": topic["source_ids"],
            "enabled": topic["enabled"],
        },
        "sources": {
            enabled_source["id"]: {
                "name": enabled_source["name"],
                "enabled": enabled_source["enabled"],
                "url": enabled_source["url"],
            },
            disabled_source["id"]: {
                "name": disabled_source["name"],
                "enabled": disabled_source["enabled"],
                "url": disabled_source["url"],
            },
        },
        "run": {
            "status": run["status"],
            "topic_ids": run["topic_ids"],
            "article_count": run["article_count"],
            "funnel": run["funnel"],
        },
        "article": {
            "run_id": article["run_id"],
            "topic_id": article["topic_id"],
            "source_id": article["source_id"],
            "title": article["title"],
            "full_text": article["full_text"],
            "favorite": article["favorite"],
            "content_hash": article["content_hash"],
            "analysis_mode": article["analysis_mode"],
            "matched_keywords": article["matched_keywords"],
            "matched_fields": article["matched_fields"],
        },
        "latest_good": db.get_latest_good_run_id(),
    }


def test_first_v12_open_snapshots_v11_data_once_and_preserves_configuration(
    v12_home: Path,
) -> None:
    path = v12_home / "cockpit.sqlite3"
    _freeze_v11_database(path)

    upgraded = Database(path)
    snapshot = v12_home / "backups" / UPGRADE_SNAPSHOT_NAME
    assert snapshot.is_file()
    first_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    settings = upgraded.get_settings()
    assert settings.default_threshold == 71
    assert settings.default_article_limit == 13
    assert settings.freshness_days == 14
    assert settings.semantic_fallback_enabled is False
    topic = upgraded.get_topic(V11_TOPIC_ID)
    assert topic["keywords"] == ["ESG", "可持续披露"]
    assert topic["exclusion_keywords"] == ["招聘", "广告"]
    assert topic["threshold"] == 73
    assert topic["article_limit"] == 9
    assert topic["source_ids"] == [V11_SOURCE_ENABLED_ID, V11_SOURCE_DISABLED_ID]
    assert upgraded.get_source(V11_SOURCE_ENABLED_ID)["enabled"] is True
    assert upgraded.get_source(V11_SOURCE_DISABLED_ID)["enabled"] is False
    assert upgraded.get_source(V11_SOURCE_ENABLED_ID)["name"] == "用户自定义来源"
    articles = upgraded.list_run_articles(V11_RUN_ID)
    assert len(articles) == 1
    assert articles[0]["id"] == V11_ARTICLE_ID
    assert articles[0]["favorite"] is True
    assert articles[0]["full_text"] == "历史全文"
    assert articles[0]["content_hash"] == V11_CONTENT_HASH
    assert upgraded.get_latest_good_run_id() == V11_RUN_ID

    checkpoint = upgraded.get_metadata(UPGRADE_CHECKPOINT)
    assert checkpoint["version"] == "1.2.0"
    assert checkpoint["snapshot"] == UPGRADE_SNAPSHOT_NAME
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT COUNT(*) FROM metadata WHERE key = ?", (UPGRADE_CHECKPOINT,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM topics WHERE name = '保留主题'"
        ).fetchone()[0] == 1
        assert json.loads(
            connection.execute(
                "SELECT exclusion_keywords_json FROM topics WHERE id = ?", (V11_TOPIC_ID,)
            ).fetchone()[0]
        ) == ["招聘", "广告"]
        assert json.loads(
            connection.execute(
                "SELECT source_ids_json FROM topics WHERE id = ?", (V11_TOPIC_ID,)
            ).fetchone()[0]
        ) == [V11_SOURCE_ENABLED_ID, V11_SOURCE_DISABLED_ID]
        assert connection.execute(
            "SELECT enabled FROM sources WHERE id = ?", (V11_SOURCE_DISABLED_ID,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT favorite FROM articles WHERE id = ?", (V11_ARTICLE_ID,)
        ).fetchone()[0] == 1

    preserved = _preserved_v11_state(upgraded)
    checkpoint_before_restart = upgraded.get_metadata(UPGRADE_CHECKPOINT)
    restarted_once = Database(path)
    restarted_twice = Database(path)
    assert _preserved_v11_state(restarted_once) == preserved
    assert _preserved_v11_state(restarted_twice) == preserved
    assert restarted_twice.get_metadata(UPGRADE_CHECKPOINT) == checkpoint_before_restart

    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == first_hash
    assert list((v12_home / "backups").glob("before-upgrade-1.2.0.sqlite3")) == [snapshot]


def test_upgrade_checkpoint_failure_rolls_back_live_database_and_keeps_snapshot(
    v12_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = v12_home / "cockpit.sqlite3"
    _freeze_v11_database(path)

    def fail_checkpoint(_db: sqlite3.Connection, _snapshot: str | None) -> None:
        raise RuntimeError("simulated checkpoint failure")

    monkeypatch.setattr(
        Database, "_record_upgrade_checkpoint", staticmethod(fail_checkpoint)
    )
    with pytest.raises(RuntimeError, match="simulated checkpoint failure"):
        Database(path)

    snapshot = v12_home / "backups" / UPGRADE_SNAPSHOT_NAME
    assert snapshot.is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT COUNT(*) FROM metadata WHERE key = ?", (UPGRADE_CHECKPOINT,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM topics WHERE name = '保留主题'"
        ).fetchone()[0] == 1


def test_corrupt_existing_upgrade_snapshot_is_rebuilt_before_checkpoint(
    v12_home: Path,
) -> None:
    path = v12_home / "cockpit.sqlite3"
    _freeze_v11_database(path)
    snapshot = v12_home / "backups" / UPGRADE_SNAPSHOT_NAME
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"not a sqlite database")

    upgraded = Database(path)

    assert upgraded.get_metadata(UPGRADE_CHECKPOINT)["snapshot"] == UPGRADE_SNAPSHOT_NAME
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT COUNT(*) FROM metadata WHERE key = ?", (UPGRADE_CHECKPOINT,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM articles WHERE id = ?", (V11_ARTICLE_ID,)
        ).fetchone()[0] == 1


def test_concurrent_first_open_serializes_snapshot_and_rechecks_checkpoint(
    v12_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = v12_home / "cockpit.sqlite3"
    _freeze_v11_database(path)
    original_snapshot = Database._ensure_upgrade_snapshot
    original_process_lock = Database._upgrade_process_lock
    first_snapshot_started = Event()
    release_first_snapshot = Event()
    second_lock_attempted = Event()
    count_lock = Lock()
    snapshot_calls = 0
    lock_attempts = 0

    def slow_first_snapshot(self: Database, connection: sqlite3.Connection) -> str:
        nonlocal snapshot_calls
        with count_lock:
            snapshot_calls += 1
            call_number = snapshot_calls
        if call_number == 1:
            first_snapshot_started.set()
            assert release_first_snapshot.wait(timeout=5)
        return original_snapshot(self, connection)

    @contextmanager
    def observed_process_lock(self: Database) -> Iterator[None]:
        nonlocal lock_attempts
        with count_lock:
            lock_attempts += 1
            attempt_number = lock_attempts
        if attempt_number == 2:
            second_lock_attempted.set()
        with original_process_lock(self):
            yield

    monkeypatch.setattr(Database, "_ensure_upgrade_snapshot", slow_first_snapshot)
    monkeypatch.setattr(Database, "_upgrade_process_lock", observed_process_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(Database, path)
        assert first_snapshot_started.wait(timeout=5)
        second = executor.submit(Database, path)
        assert second_lock_attempted.wait(timeout=5)
        release_first_snapshot.set()
        first_db = first.result(timeout=10)
        second_db = second.result(timeout=10)

    assert snapshot_calls == 1
    assert first_db.get_metadata(UPGRADE_CHECKPOINT)["snapshot"] == UPGRADE_SNAPSHOT_NAME
    assert second_db.get_metadata(UPGRADE_CHECKPOINT)["snapshot"] == UPGRADE_SNAPSHOT_NAME
    assert _preserved_v11_state(second_db) == _preserved_v11_state(first_db)


def test_portable_settings_are_opt_in_transactional_and_exclude_device_state(
    v12_home: Path,
) -> None:
    path = v12_home / "cockpit.sqlite3"
    db = Database(path)
    db.update_settings(
        SettingsUpdate(
            default_threshold=82,
            default_article_limit=7,
            freshness_days=3,
            refresh_minutes=120,
            scheduler_enabled=False,
            extract_full_text=False,
            semantic_fallback_enabled=False,
            semantic_fallback_limit=6,
            onboarding_completed=True,
            deepseek_status="error",
            deepseek_last_tested_at="2026-09-10T00:00:00+00:00",
            deepseek_last_error="local-only",
        )
    )
    incoming_settings = {
        "default_threshold": 54,
        "default_article_limit": 16,
        "freshness_days": None,
        "refresh_minutes": 30,
        "scheduler_enabled": True,
        "extract_full_text": True,
        "semantic_fallback_enabled": True,
        "semantic_fallback_limit": 19,
        "onboarding_completed": False,
        "deepseek_status": "connected",
        "deepseek_last_tested_at": "2030-01-01T00:00:00+00:00",
        "deepseek_last_error": "incoming-device-state",
        "deepseek_api_key": "sk-must-never-be-restored",
    }
    configuration = {
        "schema_version": 2,
        "settings": incoming_settings,
        "sources": [],
        "topics": [],
    }

    preview = db.preview_import(configuration, [], [])
    assert preview["portable_settings"]["available"] is True
    assert preview["portable_settings"]["default_action"] == "keep_local"
    assert "deepseek_api_key" in preview["portable_settings"]["excluded_fields"]

    kept = db.apply_import(configuration, [], [])
    assert kept["portable_settings_requested"] is False
    assert kept["portable_settings_applied"] is False
    assert db.get_settings().default_threshold == 82

    restored = db.apply_import(
        configuration, [], [], import_portable_settings=True
    )
    assert restored["portable_settings_requested"] is True
    assert restored["portable_settings_applied"] is True
    settings = db.get_settings()
    for field in (
        "default_threshold",
        "default_article_limit",
        "freshness_days",
        "refresh_minutes",
        "scheduler_enabled",
        "extract_full_text",
        "semantic_fallback_enabled",
        "semantic_fallback_limit",
    ):
        assert getattr(settings, field) == incoming_settings[field]
    assert settings.onboarding_completed is True
    assert settings.deepseek_status == "error"
    assert settings.deepseek_last_tested_at == "2026-09-10T00:00:00+00:00"
    assert settings.deepseek_last_error == "local-only"


def test_portable_settings_and_content_merge_share_one_transaction(v12_home: Path) -> None:
    db = Database(v12_home / "cockpit.sqlite3")
    db.update_settings(SettingsUpdate(default_threshold=83))
    for index in range(10):
        db.create_topic(TopicInput(name=f"本机主题 {index}"))
    configuration = {
        "schema_version": 2,
        "settings": {"default_threshold": 41},
        "sources": [
            {
                "id": "incoming-source",
                "name": "Incoming source",
                "url": "https://transaction.example/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "incoming-topic",
                "name": "Incoming topic",
                "keywords": ["signal"],
                "source_ids": ["incoming-source"],
                "enabled": True,
            }
        ],
    }

    with pytest.raises(TopicLimitError):
        db.apply_import(
            configuration, [], [], import_portable_settings=True
        )
    assert db.get_settings().default_threshold == 83
    assert not any(
        item["url"] == "https://transaction.example/feed.xml"
        for item in db.list_sources(include_archived=True)
    )


def test_v2_import_preserves_same_article_in_two_runs_and_is_idempotent(
    v12_home: Path,
) -> None:
    db = Database(v12_home / "cockpit.sqlite3")
    configuration = {
        "schema_version": 2,
        "sources": [
            {
                "id": "source-1",
                "name": "Historical feed",
                "url": "https://history.example/feed.xml",
                "enabled": True,
            }
        ],
        "topics": [
            {
                "id": "topic-1",
                "name": "Historical topic",
                "keywords": ["signal"],
                "source_ids": ["source-1"],
                "enabled": True,
            }
        ],
    }
    runs = [
        {
            "id": f"historical-run-{index}",
            "status": "complete",
            "phase": "finished",
            "outcome": "complete",
            "trigger": "manual",
            "topic_ids": ["topic-1"],
            "started_at": f"2026-08-{index:02d}T00:00:00+00:00",
            "finished_at": f"2026-08-{index:02d}T00:01:00+00:00",
            "article_count": 1,
            "source_success": 1,
            "funnel": {"selected": 1},
        }
        for index in (1, 15)
    ]
    articles = [
        {
            "id": f"historical-article-{index}",
            "run_id": f"historical-run-{index}",
            "topic_id": "topic-1",
            "source_id": "source-1",
            "title": "The same story appeared again",
            "url": "https://history.example/story",
            "content_hash": "c" * 64,
            "favorite": index == 15,
            "created_at": f"2026-08-{index:02d}T00:01:00+00:00",
        }
        for index in (1, 15)
    ]

    preview = db.preview_import(configuration, articles, runs)
    assert preview["articles"] == {"incoming": 2, "new": 2, "matched": 0}
    first = db.apply_import(configuration, articles, runs)
    assert first["articles_added"] == 2
    assert first["articles_matched"] == 0
    for index in (1, 15):
        restored = db.list_run_articles(f"historical-run-{index}")
        assert len(restored) == 1
        assert restored[0]["id"] == f"historical-article-{index}"
        assert restored[0]["favorite"] is (index == 15)
        assert db.get_run(f"historical-run-{index}")["article_count"] == 1

    second_preview = db.preview_import(configuration, articles, runs)
    assert second_preview["articles"] == {"incoming": 2, "new": 0, "matched": 2}
    second = db.apply_import(configuration, articles, runs)
    assert second["articles_added"] == 0
    assert second["articles_matched"] == 2
    assert db.count_articles() == 2


def test_api_backup_never_contains_key_and_import_is_safe_and_idempotent(
    v12_home: Path,
) -> None:
    source_paths = resolve_paths(v12_home / "source")
    source_key = "sk-source-secret-never-export"
    source_store = MemoryKeyStore(source_key)
    source_app = create_app(
        paths=source_paths,
        token="source-token",
        key_store=source_store,
        allowed_hosts={"testserver"},
    )
    source = source_app.state.cockpit.db.create_source(
        SourceInput(name="Portable feed", url="https://portable.example/feed.xml")
    )
    source_topic = source_app.state.cockpit.db.create_topic(
        TopicInput(
            name="Portable topic",
            keywords=["portable"],
            exclusion_keywords=["advertisement"],
            threshold=60,
            article_limit=5,
            source_ids=[source["id"]],
        )
    )
    source_app.state.cockpit.db.update_settings(
        SettingsUpdate(
            default_threshold=52,
            freshness_days=30,
            onboarding_completed=False,
            deepseek_status="connected",
        )
    )
    source_run = source_app.state.cockpit.db.create_run(
        [source_topic["id"]], "manual"
    )
    source_article = source_app.state.cockpit.db.insert_article(
        {
            "id": "portable-article",
            "run_id": source_run["id"],
            "topic_id": source_topic["id"],
            "source_id": source["id"],
            "title": "Portable historical signal",
            "url": "https://portable.example/news/1",
            "excerpt": "Portable excerpt",
            "summary": "Portable summary",
            "analysis": "Portable analysis",
            "score": 52,
            "published_at": "2026-09-09T00:00:00+00:00",
            "content_hash": "b" * 64,
            "analysis_mode": "ai",
            "model": "deepseek-v4-flash",
            "matched_keywords": ["portable"],
            "matched_fields": ["title"],
        }
    )
    source_app.state.cockpit.db.set_favorite(source_article["id"], True)
    source_run = source_app.state.cockpit.db.update_run(
        source_run["id"],
        status="complete",
        phase="finished",
        outcome="complete",
        finished_at="2026-09-09T00:01:00+00:00",
        article_count=1,
        source_success=1,
        analyses=1,
        funnel={
            "feed_items": 1,
            "selected": 1,
            "core_selected": 0,
            "supplement_selected": 1,
            "per_topic": {
                source_topic["id"]: {
                    "name": source_topic["name"],
                    "core_threshold": 60,
                    "supplement_threshold": 45,
                    "selected": 1,
                }
            },
        },
        ai_requests=1,
        ai_success=1,
    )
    source_app.state.cockpit.db.set_latest_good_run(source_run["id"])
    with TestClient(source_app) as client:
        exported = client.get("/api/export", headers={"X-Cockpit-Token": "source-token"})
    assert exported.status_code == 200
    assert source_key.encode() not in exported.content
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        configuration = json.loads(archive.read("configuration.json"))
    assert manifest["schema_version"] == 2
    assert manifest["deepseek_api_key_included"] is False
    assert configuration["deepseek_api_key_included"] is False
    assert source_key not in json.dumps(configuration)
    assert configuration["topics"][0]["exclusion_keywords"] == ["advertisement"]

    target_paths = resolve_paths(v12_home / "target")
    target_store = MemoryKeyStore("sk-local-key-must-remain")
    target_app = create_app(
        paths=target_paths,
        token="target-token",
        key_store=target_store,
        allowed_hosts={"testserver"},
    )
    target_app.state.cockpit.db.update_settings(
        SettingsUpdate(
            default_threshold=91,
            freshness_days=1,
            onboarding_completed=True,
            deepseek_status="error",
            deepseek_last_error="local-status",
        )
    )
    local_source = target_app.state.cockpit.db.create_source(
        SourceInput(name="Local source name", url="https://portable.example/feed.xml")
    )
    local_topic = target_app.state.cockpit.db.create_topic(
        TopicInput(
            name="Portable topic",
            keywords=["local-only"],
            exclusion_keywords=["keep-local-exclusion"],
            threshold=88,
            article_limit=3,
            source_ids=[local_source["id"]],
        )
    )
    headers = {
        "X-Cockpit-Token": "target-token",
        "X-Import-Filename": "cookies-news-cockpit-export.zip",
    }
    with TestClient(target_app) as client:
        preview = client.post(
            "/api/import/preview", headers=headers, content=exported.content
        ).json()
        assert preview["summary"]["sources_new"] == 0
        assert preview["summary"]["topics_new"] == 0
        first = client.post(
            f"/api/import/{preview['import_id']}/apply",
            headers={"X-Cockpit-Token": "target-token"},
            json={},
        ).json()
        assert first["portable_settings"]["requested"] is False
        assert first["summary"]["sources_new"] == 0
        assert first["summary"]["sources_matched"] >= 1
        assert first["summary"]["topics_new"] == 0
        assert first["summary"]["topics_matched"] == 1
        assert first["summary"]["articles_new"] == 1
        assert first["summary"]["runs_new"] == 1
        assert target_app.state.cockpit.db.get_settings().default_threshold == 91
        run_detail = client.get(
            f"/api/runs/{source_run['id']}",
            headers={"X-Cockpit-Token": "target-token"},
        )
        assert run_detail.status_code == 200
        run_articles = run_detail.json()["articles"]
        assert len(run_articles) == 1
        assert run_articles[0]["selection_tier"] == "supplement"
        assert run_articles[0]["favorite"] is True

        second_preview = client.post(
            "/api/import/preview", headers=headers, content=exported.content
        ).json()
        second = client.post(
            f"/api/import/{second_preview['import_id']}/apply",
            headers={"X-Cockpit-Token": "target-token"},
            json={"import_portable_settings": True},
        ).json()

    assert second["portable_settings"]["requested"] is True
    assert second["portable_settings"]["applied"] is True
    assert second["summary"]["sources_new"] == 0
    assert second["summary"]["topics_new"] == 0
    assert second["summary"]["articles_new"] == 0
    assert second["summary"]["articles_matched"] == 1
    assert second["summary"]["runs_new"] == 0
    assert second["summary"]["runs_matched"] == 1
    target_settings = target_app.state.cockpit.db.get_settings()
    assert target_settings.default_threshold == 52
    assert target_settings.freshness_days == 30
    assert target_settings.onboarding_completed is True
    assert target_settings.deepseek_status == "error"
    assert target_settings.deepseek_last_error == "local-status"
    assert target_store.get() == "sk-local-key-must-remain"
    preserved_topic = target_app.state.cockpit.db.get_topic(local_topic["id"])
    assert preserved_topic["keywords"] == ["local-only"]
    assert preserved_topic["exclusion_keywords"] == ["keep-local-exclusion"]
    assert preserved_topic["threshold"] == 88
    assert preserved_topic["article_limit"] == 3
    assert preserved_topic["source_ids"] == [local_source["id"]]
    assert target_app.state.cockpit.db.get_source(local_source["id"])["name"] == (
        "Local source name"
    )
    imported_articles = target_app.state.cockpit.db.list_run_articles(source_run["id"])
    assert len(imported_articles) == 1
    assert "selection_tier" not in imported_articles[0]
    assert imported_articles[0]["topic_id"] == local_topic["id"]
    assert imported_articles[0]["source_id"] == local_source["id"]
    assert imported_articles[0]["favorite"] is True
    assert imported_articles[0]["content_hash"] == "b" * 64
    assert list((target_paths.root / "backups").glob("before-import-*.zip"))
