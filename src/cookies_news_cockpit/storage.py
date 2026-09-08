from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Settings, SettingsUpdate, SourceInput, SourcePatch, TopicInput, TopicPatch


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class NotFoundError(LookupError):
    pass


class TopicLimitError(ValueError):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialized = False
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self.connect() as db:
                db.executescript(
                    """
                    PRAGMA journal_mode = WAL;
                    CREATE TABLE IF NOT EXISTS settings (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS topics (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        keywords_json TEXT NOT NULL,
                        threshold INTEGER,
                        article_limit INTEGER,
                        source_ids_json TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        archived INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS sources (
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
                    CREATE TABLE IF NOT EXISTS runs (
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
                        duplicates_skipped INTEGER NOT NULL DEFAULT 0,
                        warning TEXT,
                        error TEXT
                    );
                    CREATE TABLE IF NOT EXISTS articles (
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
                    );
                    CREATE INDEX IF NOT EXISTS idx_articles_run ON articles(run_id);
                    CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic_id);
                    CREATE INDEX IF NOT EXISTS idx_articles_created ON articles(created_at DESC);
                    CREATE TABLE IF NOT EXISTS calibrations (
                        id TEXT PRIMARY KEY,
                        topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                        proposal_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        confirmed_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                topic_columns = {row[1] for row in db.execute("PRAGMA table_info(topics)")}
                if "archived" not in topic_columns:
                    db.execute("ALTER TABLE topics ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
                source_columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
                if "archived" not in source_columns:
                    db.execute("ALTER TABLE sources ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
                run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
                if "duplicates_skipped" not in run_columns:
                    db.execute(
                        "ALTER TABLE runs ADD COLUMN duplicates_skipped INTEGER NOT NULL DEFAULT 0"
                    )
                # LIKE search remains available on Python builds without FTS5.
                with suppress(sqlite3.OperationalError):
                    db.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS article_fts USING fts5("
                        "id UNINDEXED, title, excerpt, summary, analysis, tokenize='unicode61')"
                    )
                defaults = Settings().model_dump_json()
                db.execute(
                    "INSERT OR IGNORE INTO settings(id, payload, updated_at) VALUES(1, ?, ?)",
                    (defaults, utc_now()),
                )
                # Small, source-bounded starter catalog. Only the broad official
                # China News feed is enabled; the others require explicit user
                # validation/enabling in the cockpit.
                now = utc_now()
                presets = (
                    (
                        "preset-chinanews-scroll",
                        "中新网 · 即时新闻",
                        "https://www.chinanews.com.cn/rss/scroll-news.xml",
                        1,
                    ),
                    (
                        "preset-chinanews-world",
                        "中新网 · 国际",
                        "https://www.chinanews.com.cn/rss/world.xml",
                        0,
                    ),
                    (
                        "preset-chinaorg-top",
                        "中国网 · Top News",
                        "http://www.china.org.cn/rss/1185842.xml",
                        0,
                    ),
                    ("preset-solidot", "Solidot", "https://www.solidot.org/index.rss", 0),
                )
                db.executemany(
                    """INSERT OR IGNORE INTO sources
                    (id, name, url, enabled, archived, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?)""",
                    [
                        (item_id, name, url, enabled, now, now)
                        for item_id, name, url, enabled in presets
                    ],
                )
            self._initialized = True

    def get_settings(self) -> Settings:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
        return Settings.model_validate_json(row["payload"] if row else Settings().model_dump_json())

    def update_settings(self, patch: SettingsUpdate) -> Settings:
        current = self.get_settings()
        values = current.model_dump()
        values.update(patch.model_dump(exclude_none=True))
        updated = Settings.model_validate(values)
        with self.connect() as db:
            db.execute(
                "UPDATE settings SET payload = ?, updated_at = ? WHERE id = 1",
                (updated.model_dump_json(), utc_now()),
            )
        return updated

    @staticmethod
    def _topic(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "keywords": _loads(row["keywords_json"], []),
            "threshold": row["threshold"],
            "article_limit": row["article_limit"],
            "source_ids": _loads(row["source_ids_json"], []),
            "enabled": bool(row["enabled"]),
            "archived": bool(row["archived"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_topics(
        self, enabled_only: bool = False, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        if include_archived:
            where = "WHERE enabled = 1" if enabled_only else ""
        else:
            where = "WHERE archived = 0 AND enabled = 1" if enabled_only else "WHERE archived = 0"
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM topics {where} ORDER BY created_at, name").fetchall()
        return [self._topic(row) for row in rows]

    def get_topic(self, topic_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if row is None:
            raise NotFoundError("主题不存在")
        return self._topic(row)

    def create_topic(self, value: TopicInput) -> dict[str, Any]:
        now = utc_now()
        topic_id = uuid.uuid4().hex
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM topics WHERE archived = 0").fetchone()[0]
            if count >= 10:
                raise TopicLimitError("最多只能创建 10 个主题")
            archived = db.execute(
                "SELECT id FROM topics WHERE name = ? COLLATE NOCASE AND archived = 1",
                (value.name,),
            ).fetchone()
            if archived:
                topic_id = archived["id"]
                db.execute(
                    """UPDATE topics SET keywords_json = ?, threshold = ?, article_limit = ?,
                    source_ids_json = ?, enabled = ?, archived = 0, updated_at = ? WHERE id = ?""",
                    (
                        json.dumps(value.keywords, ensure_ascii=False),
                        value.threshold,
                        value.article_limit,
                        json.dumps(value.source_ids),
                        int(value.enabled),
                        now,
                        topic_id,
                    ),
                )
            else:
                db.execute(
                    """INSERT INTO topics
                    (id, name, keywords_json, threshold, article_limit, source_ids_json,
                     enabled, archived, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        topic_id,
                        value.name,
                        json.dumps(value.keywords, ensure_ascii=False),
                        value.threshold,
                        value.article_limit,
                        json.dumps(value.source_ids),
                        int(value.enabled),
                        now,
                        now,
                    ),
                )
        return self.get_topic(topic_id)

    def update_topic(self, topic_id: str, patch: TopicPatch) -> dict[str, Any]:
        current = self.get_topic(topic_id)
        # ``exclude_unset`` distinguishes inheritance requests such as
        # threshold=null from fields the client simply did not send.
        values = current | patch.model_dump(exclude_unset=True)
        with self.connect() as db:
            db.execute(
                """UPDATE topics SET name = ?, keywords_json = ?, threshold = ?,
                article_limit = ?, source_ids_json = ?, enabled = ?, updated_at = ?
                WHERE id = ?""",
                (
                    values["name"],
                    json.dumps(values["keywords"], ensure_ascii=False),
                    values["threshold"],
                    values["article_limit"],
                    json.dumps(values["source_ids"]),
                    int(values["enabled"]),
                    utc_now(),
                    topic_id,
                ),
            )
        return self.get_topic(topic_id)

    def delete_topic(self, topic_id: str) -> None:
        with self.connect() as db:
            result = db.execute(
                "UPDATE topics SET archived = 1, enabled = 0, updated_at = ? "
                "WHERE id = ? AND archived = 0",
                (utc_now(), topic_id),
            )
        if result.rowcount == 0:
            raise NotFoundError("主题不存在")

    @staticmethod
    def _source(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "enabled": bool(row["enabled"]),
            "archived": bool(row["archived"]),
            "last_status": row["last_status"],
            "last_checked_at": row["last_checked_at"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_sources(
        self, enabled_only: bool = False, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        if include_archived:
            where = "WHERE enabled = 1" if enabled_only else ""
        else:
            where = "WHERE archived = 0 AND enabled = 1" if enabled_only else "WHERE archived = 0"
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM sources {where} ORDER BY created_at, name").fetchall()
        return [self._source(row) for row in rows]

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise NotFoundError("新闻源不存在")
        return self._source(row)

    def create_source(self, value: SourceInput) -> dict[str, Any]:
        now = utc_now()
        source_id = uuid.uuid4().hex
        with self.connect() as db:
            archived = db.execute(
                "SELECT id FROM sources WHERE url = ? AND archived = 1",
                (str(value.url),),
            ).fetchone()
            if archived:
                source_id = archived["id"]
                db.execute(
                    """UPDATE sources SET name = ?, enabled = ?, archived = 0,
                    last_status = NULL, last_checked_at = NULL, last_error = NULL,
                    updated_at = ? WHERE id = ?""",
                    (value.name, int(value.enabled), now, source_id),
                )
            else:
                db.execute(
                    """INSERT INTO sources
                    (id, name, url, enabled, archived, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?)""",
                    (source_id, value.name, str(value.url), int(value.enabled), now, now),
                )
        return self.get_source(source_id)

    def update_source(self, source_id: str, patch: SourcePatch) -> dict[str, Any]:
        current = self.get_source(source_id)
        values = current | patch.model_dump(exclude_none=True, mode="json")
        with self.connect() as db:
            db.execute(
                "UPDATE sources SET name = ?, url = ?, enabled = ?, updated_at = ? WHERE id = ?",
                (values["name"], str(values["url"]), int(values["enabled"]), utc_now(), source_id),
            )
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> None:
        with self.connect() as db:
            result = db.execute(
                "UPDATE sources SET archived = 1, enabled = 0, updated_at = ? "
                "WHERE id = ? AND archived = 0",
                (utc_now(), source_id),
            )
            # Remove stale selections from topics without relying on JSON1.
            rows = db.execute("SELECT id, source_ids_json FROM topics").fetchall()
            for row in rows:
                ids = [item for item in _loads(row["source_ids_json"], []) if item != source_id]
                db.execute(
                    "UPDATE topics SET source_ids_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(ids), utc_now(), row["id"]),
                )
        if result.rowcount == 0:
            raise NotFoundError("新闻源不存在")

    def set_source_health(self, source_id: str, ok: bool, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE sources SET last_status = ?, last_checked_at = ?, last_error = ?,
                updated_at = ? WHERE id = ?""",
                ("ok" if ok else "error", utc_now(), error, utc_now(), source_id),
            )

    def create_run(self, topic_ids: Sequence[str], trigger: str) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                """INSERT INTO runs
                (id, status, trigger, requested_topics_json, started_at)
                VALUES (?, 'queued', ?, ?, ?)""",
                (run_id, trigger, json.dumps(list(topic_ids)), utc_now()),
            )
        return self.get_run(run_id)

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "status": row["status"],
            "trigger": row["trigger"],
            "topic_ids": _loads(row["requested_topics_json"], []),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "article_count": row["article_count"],
            "source_success": row["source_success"],
            "source_failure": row["source_failure"],
            "analyses": row["analyses"],
            "duplicates_skipped": row["duplicates_skipped"],
            "warning": row["warning"],
            "error": row["error"],
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError("运行记录不存在")
        return self._run(row)

    def get_current_run(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return self._run(row) if row else None

    def recover_interrupted_runs(self) -> int:
        """Fail runs left active by an earlier process without changing report promotion."""

        finished_at = utc_now()
        with self.connect() as db:
            result = db.execute(
                """UPDATE runs
                SET status = 'failed', finished_at = ?,
                    error = '应用上次在任务完成前退出；该任务已标记为中断。'
                WHERE status IN ('queued', 'running')""",
                (finished_at,),
            )
        return result.rowcount

    def update_run(self, run_id: str, **values: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "finished_at",
            "article_count",
            "source_success",
            "source_failure",
            "analyses",
            "duplicates_skipped",
            "warning",
            "error",
        }
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return self.get_run(run_id)
        clause = ", ".join(f"{key} = ?" for key in fields)
        with self.connect() as db:
            db.execute(
                f"UPDATE runs SET {clause} WHERE id = ?",
                (*fields.values(), run_id),
            )
        return self.get_run(run_id)

    def insert_article(self, article: dict[str, Any]) -> dict[str, Any]:
        article_id = article.get("id") or uuid.uuid4().hex
        with self.connect() as db:
            try:
                db.execute(
                    """INSERT INTO articles
                (id, run_id, topic_id, source_id, title, url, excerpt, full_text,
                 summary, analysis, score, published_at, created_at, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        article_id,
                        article["run_id"],
                        article["topic_id"],
                        article["source_id"],
                        article["title"],
                        article["url"],
                        article.get("excerpt", ""),
                        article.get("full_text", ""),
                        article.get("summary", ""),
                        article.get("analysis", ""),
                        article.get("score"),
                        article.get("published_at"),
                        utc_now(),
                        article["content_hash"],
                    ),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT id FROM articles "
                    "WHERE run_id = ? AND topic_id = ? AND content_hash = ?",
                    (article["run_id"], article["topic_id"], article["content_hash"]),
                ).fetchone()
                if existing is None:
                    raise
                article_id = existing["id"]
            else:
                with suppress(sqlite3.OperationalError):
                    db.execute(
                        "INSERT INTO article_fts(id, title, excerpt, summary, analysis) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            article_id,
                            article["title"],
                            article.get("excerpt", ""),
                            article.get("summary", ""),
                            article.get("analysis", ""),
                        ),
                    )
        return self.get_article(article_id)

    @staticmethod
    def _article(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        value = {key: row[key] for key in keys}
        if "favorite" in value:
            value["favorite"] = bool(value["favorite"])
        return value

    def get_article(self, article_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """SELECT a.*, t.name AS topic_name, s.name AS source_name
                FROM articles a JOIN topics t ON t.id = a.topic_id
                JOIN sources s ON s.id = a.source_id WHERE a.id = ?""",
                (article_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("文章不存在")
        return self._article(row)

    def list_run_articles(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT a.*, t.name AS topic_name, s.name AS source_name
                FROM articles a JOIN topics t ON t.id = a.topic_id
                JOIN sources s ON s.id = a.source_id
                WHERE a.run_id = ? ORDER BY a.score DESC, a.created_at DESC""",
                (run_id,),
            ).fetchall()
        return [self._article(row) for row in rows]

    def search_articles(
        self,
        query: str = "",
        topic_id: str | None = None,
        favorite: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._article_filter(query, topic_id, favorite)
        params.extend([max(1, min(limit, 200)), max(0, offset)])
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT a.*, t.name AS topic_name, s.name AS source_name
                FROM articles a JOIN topics t ON t.id = a.topic_id
                JOIN sources s ON s.id = a.source_id
                {where} ORDER BY a.created_at DESC, a.score DESC LIMIT ? OFFSET ?""",
                params,
            ).fetchall()
        return [self._article(row) for row in rows]

    @staticmethod
    def _article_filter(
        query: str,
        topic_id: str | None,
        favorite: bool | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if query.strip():
            pattern = f"%{query.strip()}%"
            conditions.append(
                "(a.title LIKE ? OR a.excerpt LIKE ? OR a.summary LIKE ? OR a.analysis LIKE ?)"
            )
            params.extend([pattern] * 4)
        if topic_id:
            conditions.append("a.topic_id = ?")
            params.append(topic_id)
        if favorite is not None:
            conditions.append("a.favorite = ?")
            params.append(int(favorite))
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        return where, params

    def count_articles(
        self,
        query: str = "",
        topic_id: str | None = None,
        favorite: bool | None = None,
    ) -> int:
        where, params = self._article_filter(query, topic_id, favorite)
        with self.connect() as db:
            row = db.execute(
                f"""SELECT COUNT(*) AS total
                FROM articles a JOIN topics t ON t.id = a.topic_id
                JOIN sources s ON s.id = a.source_id
                {where}""",
                params,
            ).fetchone()
        return int(row["total"])

    def export_articles(self) -> list[dict[str, Any]]:
        """Return every retained article for a full, key-free user backup."""

        with self.connect() as db:
            rows = db.execute(
                """SELECT a.*, t.name AS topic_name, s.name AS source_name
                FROM articles a JOIN topics t ON t.id = a.topic_id
                JOIN sources s ON s.id = a.source_id
                ORDER BY a.created_at, a.id"""
            ).fetchall()
        return [self._article(row) for row in rows]

    def recent_article_fingerprints(self, days: int = 7) -> list[dict[str, str]]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connect() as db:
            rows = db.execute(
                """SELECT a.url, a.title FROM articles a
                JOIN runs r ON r.id = a.run_id
                WHERE a.created_at >= ?
                  AND (
                    r.status = 'complete'
                    OR (
                        r.status = 'degraded'
                        AND NOT EXISTS (
                            SELECT 1 FROM metadata m WHERE m.key = 'latest_good_run_id'
                        )
                    )
                  )
                ORDER BY a.created_at DESC""",
                (cutoff,),
            ).fetchall()
        return [{"url": row["url"], "title": row["title"]} for row in rows]

    def set_favorite(self, article_id: str, favorite: bool) -> dict[str, Any]:
        with self.connect() as db:
            result = db.execute(
                "UPDATE articles SET favorite = ? WHERE id = ?",
                (int(favorite), article_id),
            )
        if result.rowcount == 0:
            raise NotFoundError("文章不存在")
        return self.get_article(article_id)

    def save_calibration(self, topic_id: str, proposal: dict[str, Any]) -> dict[str, Any]:
        calibration_id = uuid.uuid4().hex
        created_at = utc_now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO calibrations
                (id, topic_id, proposal_json, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)""",
                (calibration_id, topic_id, json.dumps(proposal, ensure_ascii=False), created_at),
            )
        return {
            "id": calibration_id,
            "topic_id": topic_id,
            "status": "pending",
            "proposal": proposal,
            "created_at": created_at,
        }

    def confirm_calibration(self, calibration_id: str, topic_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM calibrations WHERE id = ? AND topic_id = ?",
                (calibration_id, topic_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("校准草案不存在")
            if row["status"] != "pending":
                raise ValueError("该校准草案已经处理")
            db.execute(
                "UPDATE calibrations SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
                (utc_now(), calibration_id),
            )
        return _loads(row["proposal_json"], {})

    def set_latest_good_run(self, run_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO metadata(key, value) VALUES('latest_good_run_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (run_id,),
            )

    def restore_latest_good_run(self, run_id: str | None) -> None:
        """Restore report promotion metadata after a failed artifact write."""

        if run_id is not None:
            self.set_latest_good_run(run_id)
            return
        with self.connect() as db:
            db.execute("DELETE FROM metadata WHERE key = 'latest_good_run_id'")

    def get_latest_good_run_id(self) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT m.value FROM metadata m
                JOIN runs r ON r.id = m.value
                WHERE m.key = 'latest_good_run_id'
                  AND r.status = 'complete'
                  AND EXISTS (SELECT 1 FROM articles a WHERE a.run_id = r.id)"""
            ).fetchone()
        return row["value"] if row else None

    def get_newest_usable_run_id(self) -> str | None:
        """Find a displayable run without promoting it to latest-good metadata."""

        with self.connect() as db:
            row = db.execute(
                """SELECT r.id FROM runs r
                WHERE r.status IN ('complete', 'degraded')
                  AND EXISTS (SELECT 1 FROM articles a WHERE a.run_id = r.id)
                ORDER BY COALESCE(r.finished_at, r.started_at) DESC, r.started_at DESC
                LIMIT 1"""
            ).fetchone()
        return row["id"] if row else None

    def purge_expired_full_text(self, days: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connect() as db:
            result = db.execute(
                "UPDATE articles SET full_text = '' WHERE created_at < ? AND full_text != ''",
                (cutoff,),
            )
        return result.rowcount

    def rebuild_search_index(self) -> bool:
        with self.connect() as db:
            try:
                db.execute("DELETE FROM article_fts")
                db.execute(
                    """INSERT INTO article_fts(id, title, excerpt, summary, analysis)
                    SELECT id, title, excerpt, summary, analysis FROM articles"""
                )
            except sqlite3.OperationalError:
                return False
        return True
