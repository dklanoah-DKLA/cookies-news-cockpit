from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .dedup import canonical_url, normalize_title
from .models import Settings, SettingsUpdate, SourceInput, SourcePatch, TopicInput, TopicPatch
from .presets import PRESET_CATALOG_VERSION, SOURCE_PRESETS

SCHEMA_VERSION = 2
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}

LEGACY_DEEPSEEK_MODEL = "deepseek-chat"
LEGACY_RULE_ANALYSES = frozenset(
    {
        "DeepSeek 本次未生成分析；已使用关键词规则初筛。",
        "关键词规则初筛；配置 DeepSeek API Key 后可获得 AI 分析。",
        "已达到单次 AI 分析安全上限；使用关键词规则初筛。",
    }
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _legacy_analysis_provenance(analysis: str) -> tuple[str, str | None]:
    """Recover provenance omitted by 0.1 database rows and ZIP exports.

    Version 0.1 always wrote one of three fixed messages for rule-screened
    articles. A successful DeepSeek result was schema-validated as non-blank
    and replaced that message, so every other non-blank value is a legacy AI
    analysis produced by the only model used in that release.
    """

    normalized = analysis.strip()
    if not normalized or normalized in LEGACY_RULE_ANALYSES:
        return "rules", None
    return "ai_legacy", LEGACY_DEEPSEEK_MODEL


def _import_bool(value: Any, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError(f"备份字段 {field} 必须是布尔值")


def _import_int(
    value: Any,
    *,
    field: str,
    default: int = 0,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"备份字段 {field} 必须是整数")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"备份字段 {field} 超出允许范围")
    return value


def _import_id(value: Any, *, field: str, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"备份字段 {field} 必须是字符串")
    result = value.strip()
    if required and not result:
        raise ValueError(f"备份字段 {field} 不能为空")
    if len(result) > 128:
        raise ValueError(f"备份字段 {field} 过长")
    return result


def validate_run_id_token(value: Any) -> str:
    """Return a run ID that is safe to reuse as one report filename."""

    result = _import_id(value, field="runs.id")
    basename = result.split(".", 1)[0].casefold()
    if (
        value != result
        or not _SAFE_RUN_ID.fullmatch(result)
        or basename in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("备份字段 runs.id 不是安全的文件名标识")
    return result


def _import_string(
    value: Any,
    *,
    field: str,
    default: str = "",
    required: bool = False,
    maximum: int = 20_000,
) -> str:
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"备份字段 {field} 必须是字符串")
    result = value.strip() if required else value
    if required and not result:
        raise ValueError(f"备份字段 {field} 不能为空")
    if len(result) > maximum:
        raise ValueError(f"备份字段 {field} 过长")
    return result


def _import_string_list(value: Any, *, field: str, maximum: int = 100) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"备份字段 {field} 必须是有效列表")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        parsed = _import_id(item, field=field)
        if parsed in seen:
            raise ValueError(f"备份字段 {field} 包含重复 ID")
        seen.add(parsed)
        result.append(parsed)
    return result


def _import_article_fingerprint(raw: dict[str, Any], *, title: str, url: str) -> str:
    supplied = raw.get("content_hash")
    if supplied is not None:
        supplied = _import_string(
            supplied,
            field="articles.content_hash",
            maximum=256,
        ).strip()
        if supplied:
            return supplied
    material = "\0".join((canonical_url(url), normalize_title(title)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _normalized_source_url(value: str) -> str:
    """Apply the same URL normalization used by SourceInput to stored rows."""

    try:
        return str(SourceInput(name="Imported source", url=value).url)
    except (TypeError, ValueError):
        # A legacy row can predate URL validation.  It remains addressable by
        # its exact value, but cannot silently collide with a valid normalized URL.
        return value


def _unique_name_match(
    index: dict[str, set[str]],
    name: str,
    *,
    label: str,
) -> str | None:
    candidates = index.get(name.casefold(), set())
    if len(candidates) > 1:
        raise ValueError(f"{label}名称不唯一，无法安全匹配：{name}")
    return next(iter(candidates), None)


def _import_article_url(value: Any) -> str:
    """Accept only browser-safe HTTP(S) article links from an untrusted backup."""

    raw = _import_string(value, field="articles.url", required=True, maximum=5000)
    try:
        return str(SourceInput(name="Imported article", url=raw).url)
    except (TypeError, ValueError) as exc:
        raise ValueError("备份字段 articles.url 必须是有效的 http/https 地址") from exc


def _import_text_list(value: Any, *, field: str, maximum: int = 100) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"备份字段 {field} 必须是有效列表")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 500:
            raise ValueError(f"备份字段 {field} 包含无效文本")
        result.append(item)
    return result


def _validated_import_records(
    configuration: dict[str, Any],
    articles: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    if not isinstance(configuration, dict):
        raise ValueError("备份 configuration 必须是对象")
    schema_version = configuration.get("schema_version", 1)
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError(f"不支持的备份版本：{schema_version}")
    raw_sources = configuration.get("sources", [])
    raw_topics = configuration.get("topics", [])
    if not isinstance(raw_sources, list) or not all(isinstance(item, dict) for item in raw_sources):
        raise ValueError("备份 sources 必须是对象列表")
    if not isinstance(raw_topics, list) or not all(isinstance(item, dict) for item in raw_topics):
        raise ValueError("备份 topics 必须是对象列表")
    if not isinstance(articles, list) or not all(isinstance(item, dict) for item in articles):
        raise ValueError("备份 articles 必须是对象列表")
    if not isinstance(runs, list) or not all(isinstance(item, dict) for item in runs):
        raise ValueError("备份 runs 必须是对象列表")

    source_records: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    for raw in raw_sources:
        incoming_id = _import_id(raw.get("id"), field="sources.id")
        if incoming_id in source_ids:
            raise ValueError(f"备份包含重复 source id：{incoming_id}")
        source_ids.add(incoming_id)
        enabled = _import_bool(raw.get("enabled"), field="sources.enabled", default=True)
        archived = _import_bool(raw.get("archived"), field="sources.archived", default=False)
        user_modified = _import_bool(
            raw.get("user_modified"), field="sources.user_modified", default=False
        )
        value = SourceInput(
            name=raw.get("name", ""),
            url=raw.get("url", ""),
            enabled=enabled,
            homepage=raw.get("homepage"),
            category=raw.get("category") or "custom",
            language=raw.get("language") or "other",
            terms=raw.get("terms"),
            preset_id=raw.get("preset_id"),
            preset_version=raw.get("preset_version"),
            user_modified=user_modified,
        )
        source_records.append(
            {
                "id": incoming_id,
                "value": value,
                "url": str(value.url),
                "archived": archived,
                "user_modified": user_modified,
                "created_at": _import_string(
                    raw.get("created_at"), field="sources.created_at", default="", maximum=100
                ),
            }
        )

    topic_records: list[dict[str, Any]] = []
    topic_ids: set[str] = set()
    for raw in raw_topics:
        incoming_id = _import_id(raw.get("id"), field="topics.id")
        if incoming_id in topic_ids:
            raise ValueError(f"备份包含重复 topic id：{incoming_id}")
        topic_ids.add(incoming_id)
        source_refs = _import_string_list(raw.get("source_ids"), field="topics.source_ids")
        missing_sources = [source_id for source_id in source_refs if source_id not in source_ids]
        if missing_sources:
            raise ValueError(f"备份主题引用了不存在的新闻源：{missing_sources[0]}")
        enabled = _import_bool(raw.get("enabled"), field="topics.enabled", default=True)
        archived = _import_bool(raw.get("archived"), field="topics.archived", default=False)
        value = TopicInput(
            name=raw.get("name", ""),
            keywords=raw.get("keywords") or [],
            exclusion_keywords=raw.get("exclusion_keywords") or [],
            threshold=raw.get("threshold"),
            article_limit=raw.get("article_limit"),
            source_ids=source_refs,
            enabled=enabled,
        )
        topic_records.append(
            {
                "id": incoming_id,
                "value": value,
                "archived": archived,
                "created_at": _import_string(
                    raw.get("created_at"), field="topics.created_at", default="", maximum=100
                ),
            }
        )

    run_records: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    allowed_statuses = {"queued", "running", "complete", "degraded", "failed", "cancelled"}
    now = utc_now()
    for raw in runs:
        incoming_id = validate_run_id_token(raw.get("id"))
        if incoming_id in run_ids:
            raise ValueError(f"备份包含重复 run id：{incoming_id}")
        run_ids.add(incoming_id)
        topic_refs = _import_string_list(
            raw.get("topic_ids", raw.get("requested_topics", [])), field="runs.topic_ids"
        )
        missing_topics = [topic_id for topic_id in topic_refs if topic_id not in topic_ids]
        if missing_topics:
            raise ValueError(f"备份运行记录引用了不存在的主题：{missing_topics[0]}")
        status = _import_string(raw.get("status"), field="runs.status", required=True, maximum=30)
        if status not in allowed_statuses:
            raise ValueError(f"备份运行状态无效：{status}")
        phase = (
            _import_string(raw.get("phase"), field="runs.phase", default="finished", maximum=60)
            or "finished"
        )
        started_at = (
            _import_string(raw.get("started_at"), field="runs.started_at", default=now, maximum=100)
            or now
        )
        outcome_value = raw.get("outcome")
        outcome = (
            None
            if outcome_value is None
            else _import_string(outcome_value, field="runs.outcome", maximum=100)
        )
        finished_at_value = raw.get("finished_at")
        finished_at = (
            None
            if finished_at_value is None
            else _import_string(finished_at_value, field="runs.finished_at", maximum=100)
        )
        error_value = raw.get("error")
        error = (
            None
            if error_value is None
            else _import_string(error_value, field="runs.error", maximum=5000)
        )
        if status in {"queued", "running"}:
            status = "failed"
            phase = "finished"
            outcome = "interrupted"
            finished_at = finished_at or started_at
            notice = "导入时该任务仍未完成，已安全标记为中断。"
            error = " ".join(item for item in (error, notice) if item)
        funnel = raw.get("funnel")
        if funnel is None:
            funnel = {}
        if not isinstance(funnel, dict):
            raise ValueError("备份字段 runs.funnel 必须是对象")
        warning_value = raw.get("warning")
        warning = (
            None
            if warning_value is None
            else _import_string(warning_value, field="runs.warning", maximum=5000)
        )
        run_records.append(
            {
                "id": incoming_id,
                "status": status,
                "phase": phase,
                "outcome": outcome,
                "trigger": _import_string(
                    raw.get("trigger"), field="runs.trigger", default="import", maximum=60
                )
                or "import",
                "topic_ids": topic_refs,
                "started_at": started_at,
                "finished_at": finished_at,
                "article_count": _import_int(raw.get("article_count"), field="runs.article_count"),
                "source_success": _import_int(
                    raw.get("source_success"), field="runs.source_success"
                ),
                "source_failure": _import_int(
                    raw.get("source_failure"), field="runs.source_failure"
                ),
                "analyses": _import_int(raw.get("analyses"), field="runs.analyses"),
                "duplicates_skipped": _import_int(
                    raw.get("duplicates_skipped"), field="runs.duplicates_skipped"
                ),
                "funnel": funnel,
                "ai_requests": _import_int(raw.get("ai_requests"), field="runs.ai_requests"),
                "ai_success": _import_int(raw.get("ai_success"), field="runs.ai_success"),
                "ai_failure": _import_int(raw.get("ai_failure"), field="runs.ai_failure"),
                "warning": warning,
                "error": error,
            }
        )

    article_records: list[dict[str, Any]] = []
    article_ids: set[str] = set()
    for index, raw in enumerate(articles):
        raw_id = raw.get("id")
        incoming_id = (
            _import_id(raw_id, field="articles.id") if raw_id is not None else f"legacy-{index}"
        )
        if incoming_id in article_ids:
            raise ValueError(f"备份包含重复 article id：{incoming_id}")
        article_ids.add(incoming_id)
        title = _import_string(
            raw.get("title"), field="articles.title", required=True, maximum=1000
        )
        url = _import_article_url(raw.get("url"))
        topic_id = _import_id(raw.get("topic_id"), field="articles.topic_id", required=False)
        source_id = _import_id(raw.get("source_id"), field="articles.source_id", required=False)
        run_id = _import_id(raw.get("run_id"), field="articles.run_id", required=False)
        if topic_id and topic_id not in topic_ids:
            raise ValueError(f"备份文章引用了不存在的主题：{topic_id}")
        if source_id and source_id not in source_ids:
            raise ValueError(f"备份文章引用了不存在的新闻源：{source_id}")
        if schema_version == 2 and run_id not in run_ids:
            raise ValueError(
                f"v2 备份文章引用了 runs.json 中不存在的运行记录：{run_id or '(空)'}"
            )
        if schema_version == 1 and run_records and run_id not in run_ids:
            raise ValueError(f"备份文章引用了不存在的运行记录：{run_id or '(空)'}")
        favorite = _import_bool(raw.get("favorite"), field="articles.favorite", default=False)
        score_value = raw.get("score")
        score = (
            None
            if score_value is None
            else _import_int(score_value, field="articles.score", minimum=0, maximum=100)
        )
        published_value = raw.get("published_at")
        published_at = (
            None
            if published_value is None
            else _import_string(published_value, field="articles.published_at", maximum=100)
        )
        analysis = _import_string(
            raw.get("analysis"), field="articles.analysis", maximum=20_000
        )
        model_value = raw.get("model")
        model = (
            None
            if model_value is None
            else _import_string(model_value, field="articles.model", maximum=100)
        )
        analysis_mode_value = raw.get("analysis_mode")
        if schema_version == 1 and analysis_mode_value is None and model is None:
            analysis_mode, model = _legacy_analysis_provenance(analysis)
        else:
            analysis_mode = (
                _import_string(
                    analysis_mode_value,
                    field="articles.analysis_mode",
                    default="ai" if model else "rules",
                    maximum=60,
                )
                or ("ai" if model else "rules")
            )
        article_records.append(
            {
                "id": incoming_id,
                "topic_id": topic_id,
                "topic_name": _import_string(
                    raw.get("topic_name"), field="articles.topic_name", maximum=100
                ),
                "source_id": source_id,
                "source_name": _import_string(
                    raw.get("source_name"), field="articles.source_name", maximum=120
                ),
                "run_id": run_id,
                "title": title,
                "url": url,
                "excerpt": _import_string(
                    raw.get("excerpt"), field="articles.excerpt", maximum=10_000
                ),
                "summary": _import_string(
                    raw.get("summary"), field="articles.summary", maximum=10_000
                ),
                "analysis": analysis,
                "score": score,
                "published_at": published_at,
                "created_at": _import_string(
                    raw.get("created_at"), field="articles.created_at", default=now, maximum=100
                )
                or now,
                "favorite": favorite,
                "content_hash": _import_article_fingerprint(raw, title=title, url=url),
                "analysis_mode": analysis_mode,
                "model": model,
                "matched_keywords": _import_text_list(
                    raw.get("matched_keywords"), field="articles.matched_keywords"
                ),
                "matched_fields": _import_text_list(
                    raw.get("matched_fields"), field="articles.matched_fields"
                ),
            }
        )
    return source_records, topic_records, run_records, article_records


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
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS settings (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS topics (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        keywords_json TEXT NOT NULL,
                        exclusion_keywords_json TEXT NOT NULL DEFAULT '[]',
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
                        homepage TEXT,
                        category TEXT NOT NULL DEFAULT 'custom',
                        language TEXT NOT NULL DEFAULT 'other',
                        terms TEXT,
                        preset_id TEXT,
                        preset_version INTEGER,
                        user_modified INTEGER NOT NULL DEFAULT 1,
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
                        phase TEXT NOT NULL DEFAULT 'queued',
                        outcome TEXT,
                        trigger TEXT NOT NULL,
                        requested_topics_json TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        article_count INTEGER NOT NULL DEFAULT 0,
                        source_success INTEGER NOT NULL DEFAULT 0,
                        source_failure INTEGER NOT NULL DEFAULT 0,
                        analyses INTEGER NOT NULL DEFAULT 0,
                        duplicates_skipped INTEGER NOT NULL DEFAULT 0,
                        funnel_json TEXT NOT NULL DEFAULT '{}',
                        ai_requests INTEGER NOT NULL DEFAULT 0,
                        ai_success INTEGER NOT NULL DEFAULT 0,
                        ai_failure INTEGER NOT NULL DEFAULT 0,
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
                        analysis_mode TEXT NOT NULL DEFAULT 'rules',
                        model TEXT,
                        matched_keywords_json TEXT NOT NULL DEFAULT '[]',
                        matched_fields_json TEXT NOT NULL DEFAULT '[]',
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
                    CREATE TABLE IF NOT EXISTS source_archive_snapshots (
                        source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                        topic_ids_json TEXT NOT NULL,
                        deleted_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS ai_cache (
                        cache_key TEXT PRIMARY KEY,
                        content_fingerprint TEXT NOT NULL,
                        topic_fingerprint TEXT NOT NULL,
                        model TEXT NOT NULL,
                        analysis_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_ai_cache_expiry ON ai_cache(expires_at);
                    """
                )
                self._migrate_v2(db)
            self._initialized = True

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _add_column(cls, db: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split(maxsplit=1)[0]
        if name not in cls._columns(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    @classmethod
    def _migrate_v2(cls, db: sqlite3.Connection) -> None:
        """Upgrade the 0.1 database in one transaction and remain idempotent."""

        previous_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        existing_install = any(
            int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("sources", "topics", "runs", "articles")
        )
        cls._add_column(db, "topics", "archived INTEGER NOT NULL DEFAULT 0")
        cls._add_column(db, "topics", "exclusion_keywords_json TEXT NOT NULL DEFAULT '[]'")

        cls._add_column(db, "sources", "archived INTEGER NOT NULL DEFAULT 0")
        cls._add_column(db, "sources", "homepage TEXT")
        cls._add_column(db, "sources", "category TEXT NOT NULL DEFAULT 'custom'")
        cls._add_column(db, "sources", "language TEXT NOT NULL DEFAULT 'other'")
        cls._add_column(db, "sources", "terms TEXT")
        cls._add_column(db, "sources", "preset_id TEXT")
        cls._add_column(db, "sources", "preset_version INTEGER")
        cls._add_column(db, "sources", "user_modified INTEGER NOT NULL DEFAULT 0")

        cls._add_column(db, "runs", "duplicates_skipped INTEGER NOT NULL DEFAULT 0")
        cls._add_column(db, "runs", "phase TEXT NOT NULL DEFAULT 'queued'")
        cls._add_column(db, "runs", "outcome TEXT")
        cls._add_column(db, "runs", "funnel_json TEXT NOT NULL DEFAULT '{}'")
        cls._add_column(db, "runs", "ai_requests INTEGER NOT NULL DEFAULT 0")
        cls._add_column(db, "runs", "ai_success INTEGER NOT NULL DEFAULT 0")
        cls._add_column(db, "runs", "ai_failure INTEGER NOT NULL DEFAULT 0")

        cls._add_column(db, "articles", "analysis_mode TEXT NOT NULL DEFAULT 'rules'")
        cls._add_column(db, "articles", "model TEXT")
        cls._add_column(db, "articles", "matched_keywords_json TEXT NOT NULL DEFAULT '[]'")
        cls._add_column(db, "articles", "matched_fields_json TEXT NOT NULL DEFAULT '[]'")

        if previous_version < SCHEMA_VERSION:
            legacy_rows = db.execute(
                """SELECT id, analysis FROM articles
                WHERE analysis_mode = 'rules' AND model IS NULL"""
            ).fetchall()
            for legacy_row in legacy_rows:
                analysis_mode, model = _legacy_analysis_provenance(legacy_row["analysis"])
                db.execute(
                    "UPDATE articles SET analysis_mode = ?, model = ? WHERE id = ?",
                    (analysis_mode, model, legacy_row["id"]),
                )

        db.execute(
            """CREATE TABLE IF NOT EXISTS source_archive_snapshots (
                source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                topic_ids_json TEXT NOT NULL,
                deleted_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS ai_cache (
                cache_key TEXT PRIMARY KEY,
                content_fingerprint TEXT NOT NULL,
                topic_fingerprint TEXT NOT NULL,
                model TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )"""
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_cache_expiry ON ai_cache(expires_at)")

        # LIKE search remains available on Python builds without FTS5.
        with suppress(sqlite3.OperationalError):
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS article_fts USING fts5("
                "id UNINDEXED, title, excerpt, summary, analysis, tokenize='unicode61')"
            )

        now = utc_now()
        db.execute(
            "INSERT OR IGNORE INTO settings(id, payload, updated_at) VALUES(1, ?, ?)",
            (Settings().model_dump_json(), now),
        )
        row = db.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
        settings = Settings.model_validate(_loads(row["payload"] if row else None, {}))
        if previous_version < SCHEMA_VERSION and existing_install:
            settings = settings.model_copy(update={"onboarding_completed": True})
        db.execute(
            "UPDATE settings SET payload = ?, updated_at = ? WHERE id = 1",
            (settings.model_dump_json(), now),
        )

        if previous_version < SCHEMA_VERSION:
            cls._classify_legacy_sources(db)
        cls._sync_source_presets(db, now, enable_defaults=not existing_install)
        db.execute(
            """INSERT INTO metadata(key, value) VALUES('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (str(SCHEMA_VERSION),),
        )
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _classify_legacy_sources(db: sqlite3.Connection) -> None:
        legacy = {
            "preset-chinanews-scroll": (
                "中新网 · 即时新闻",
                "https://www.chinanews.com.cn/rss/scroll-news.xml",
            ),
            "preset-chinanews-world": (
                "中新网 · 国际",
                "https://www.chinanews.com.cn/rss/world.xml",
            ),
            "preset-chinaorg-top": (
                "中国网 · Top News",
                "http://www.china.org.cn/rss/1185842.xml",
            ),
            "preset-solidot": ("Solidot", "https://www.solidot.org/index.rss"),
        }
        for source_id, (name, url) in legacy.items():
            row = db.execute("SELECT name, url FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                continue
            modified = row["name"] != name or row["url"] != url
            db.execute(
                "UPDATE sources SET user_modified = ? WHERE id = ?",
                (int(modified), source_id),
            )
        placeholders = ",".join("?" for _ in legacy)
        db.execute(
            f"UPDATE sources SET user_modified = 1 WHERE id NOT IN ({placeholders})",
            tuple(legacy),
        )

        old = db.execute("SELECT * FROM sources WHERE id = 'preset-chinaorg-top'").fetchone()
        if old is None:
            return
        untouched = (
            old["name"] == legacy["preset-chinaorg-top"][0]
            and old["url"] == legacy["preset-chinaorg-top"][1]
            and not bool(old["enabled"])
            and not bool(old["user_modified"])
        )
        if untouched:
            db.execute(
                "UPDATE sources SET archived = 1, enabled = 0, updated_at = ? WHERE id = ?",
                (utc_now(), "preset-chinaorg-top"),
            )
            db.execute("DELETE FROM metadata WHERE key = 'preset_migration_warning'")
        else:
            warning = {
                "source_id": "preset-chinaorg-top",
                "message": "旧版中国网来源曾被启用或修改，已原样保留；请手动验证其 HTTP feed。",
            }
            db.execute(
                """INSERT INTO metadata(key, value) VALUES('preset_migration_warning', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (json.dumps(warning, ensure_ascii=False),),
            )

    @staticmethod
    def _sync_source_presets(db: sqlite3.Connection, now: str, *, enable_defaults: bool) -> None:
        for preset in SOURCE_PRESETS:
            row = db.execute("SELECT * FROM sources WHERE url = ?", (preset.url,)).fetchone()
            if row is not None:
                if not bool(row["user_modified"]):
                    db.execute(
                        """UPDATE sources SET name = ?, homepage = ?, category = ?, language = ?,
                        terms = ?, preset_id = ?, preset_version = ?, updated_at = ?
                        WHERE id = ?""",
                        (
                            preset.name,
                            preset.homepage,
                            preset.category,
                            preset.language,
                            preset.terms,
                            preset.id,
                            PRESET_CATALOG_VERSION,
                            now,
                            row["id"],
                        ),
                    )
                else:
                    db.execute(
                        """UPDATE sources SET preset_id = COALESCE(preset_id, ?),
                        preset_version = COALESCE(preset_version, ?) WHERE id = ?""",
                        (preset.id, PRESET_CATALOG_VERSION, row["id"]),
                    )
                continue

            source_id = preset.id
            if db.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
                source_id = f"{preset.id}-{uuid.uuid4().hex[:8]}"
            db.execute(
                """INSERT INTO sources
                (id, name, url, homepage, category, language, terms, preset_id,
                 preset_version, user_modified, enabled, archived, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)""",
                (
                    source_id,
                    preset.name,
                    preset.url,
                    preset.homepage,
                    preset.category,
                    preset.language,
                    preset.terms,
                    preset.id,
                    PRESET_CATALOG_VERSION,
                    int(preset.default_enabled and enable_defaults),
                    now,
                    now,
                ),
            )

    def get_settings(self) -> Settings:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
        return Settings.model_validate_json(row["payload"] if row else Settings().model_dump_json())

    def get_schema_version(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else SCHEMA_VERSION

    def get_metadata(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return _loads(row["value"], row["value"])

    def update_settings(self, patch: SettingsUpdate) -> Settings:
        current = self.get_settings()
        values = current.model_dump()
        values.update(patch.model_dump(exclude_unset=True))
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
            "exclusion_keywords": _loads(row["exclusion_keywords_json"], []),
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
                    """UPDATE topics SET keywords_json = ?, exclusion_keywords_json = ?,
                    threshold = ?, article_limit = ?, source_ids_json = ?, enabled = ?,
                    archived = 0, updated_at = ? WHERE id = ?""",
                    (
                        json.dumps(value.keywords, ensure_ascii=False),
                        json.dumps(value.exclusion_keywords, ensure_ascii=False),
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
                    (id, name, keywords_json, exclusion_keywords_json, threshold,
                     article_limit, source_ids_json,
                     enabled, archived, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        topic_id,
                        value.name,
                        json.dumps(value.keywords, ensure_ascii=False),
                        json.dumps(value.exclusion_keywords, ensure_ascii=False),
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
                """UPDATE topics SET name = ?, keywords_json = ?, exclusion_keywords_json = ?,
                threshold = ?, article_limit = ?, source_ids_json = ?, enabled = ?, updated_at = ?
                WHERE id = ?""",
                (
                    values["name"],
                    json.dumps(values["keywords"], ensure_ascii=False),
                    json.dumps(values["exclusion_keywords"], ensure_ascii=False),
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

    def restore_topic(self, topic_id: str) -> dict[str, Any]:
        """Restore one archived topic without bypassing the ten-topic limit."""

        with self.connect() as db:
            row = db.execute("SELECT archived FROM topics WHERE id = ?", (topic_id,)).fetchone()
            if row is None or not bool(row["archived"]):
                raise NotFoundError("已归档主题不存在")
            count = int(db.execute("SELECT COUNT(*) FROM topics WHERE archived = 0").fetchone()[0])
            if count >= 10:
                raise TopicLimitError("最多只能保留 10 个活动主题，请先归档其他主题")
            db.execute(
                "UPDATE topics SET archived = 0, enabled = 1, updated_at = ? WHERE id = ?",
                (utc_now(), topic_id),
            )
        return self.get_topic(topic_id)

    @staticmethod
    def _source(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "homepage": row["homepage"],
            "category": row["category"],
            "language": row["language"],
            "terms": row["terms"],
            "preset_id": row["preset_id"],
            "preset_version": row["preset_version"],
            "user_modified": bool(row["user_modified"]),
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
                    """UPDATE sources SET name = ?, url = ?, homepage = ?, category = ?,
                    language = ?, terms = ?, enabled = ?, archived = 0, user_modified = 1,
                    last_status = NULL, last_checked_at = NULL, last_error = NULL,
                    updated_at = ? WHERE id = ?""",
                    (
                        value.name,
                        str(value.url),
                        str(value.homepage) if value.homepage else None,
                        value.category,
                        value.language,
                        str(value.terms) if value.terms else None,
                        int(value.enabled),
                        now,
                        source_id,
                    ),
                )
                self._restore_source_relations(db, source_id)
            else:
                db.execute(
                    """INSERT INTO sources
                    (id, name, url, homepage, category, language, terms, preset_id,
                     preset_version, user_modified, enabled, archived, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        source_id,
                        value.name,
                        str(value.url),
                        str(value.homepage) if value.homepage else None,
                        value.category,
                        value.language,
                        str(value.terms) if value.terms else None,
                        value.preset_id,
                        value.preset_version,
                        int(value.user_modified),
                        int(value.enabled),
                        now,
                        now,
                    ),
                )
        return self.get_source(source_id)

    def update_source(self, source_id: str, patch: SourcePatch) -> dict[str, Any]:
        current = self.get_source(source_id)
        patch_values = patch.model_dump(exclude_unset=True, mode="json")
        for required in ("name", "url", "enabled", "category", "language"):
            if patch_values.get(required, current[required]) is None:
                patch_values.pop(required, None)
        values = current | patch_values
        url_changed = str(values["url"]) != current["url"]
        with self.connect() as db:
            db.execute(
                """UPDATE sources SET name = ?, url = ?, homepage = ?, category = ?,
                language = ?, terms = ?, enabled = ?, user_modified = 1,
                last_status = CASE WHEN ? THEN NULL ELSE last_status END,
                last_checked_at = CASE WHEN ? THEN NULL ELSE last_checked_at END,
                last_error = CASE WHEN ? THEN NULL ELSE last_error END,
                updated_at = ? WHERE id = ?""",
                (
                    values["name"],
                    str(values["url"]),
                    str(values["homepage"]) if values.get("homepage") else None,
                    values["category"],
                    values["language"],
                    str(values["terms"]) if values.get("terms") else None,
                    int(values["enabled"]),
                    int(url_changed),
                    int(url_changed),
                    int(url_changed),
                    utc_now(),
                    source_id,
                ),
            )
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> None:
        with self.connect() as db:
            source = db.execute(
                "SELECT id FROM sources WHERE id = ? AND archived = 0", (source_id,)
            ).fetchone()
            if source is None:
                raise NotFoundError("新闻源不存在")
            rows = db.execute("SELECT id, source_ids_json FROM topics").fetchall()
            linked_topic_ids = [
                row["id"] for row in rows if source_id in _loads(row["source_ids_json"], [])
            ]
            db.execute(
                """INSERT INTO source_archive_snapshots(source_id, topic_ids_json, deleted_at)
                VALUES (?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET
                topic_ids_json = excluded.topic_ids_json, deleted_at = excluded.deleted_at""",
                (source_id, json.dumps(linked_topic_ids), utc_now()),
            )
            result = db.execute(
                "UPDATE sources SET archived = 1, enabled = 0, updated_at = ? "
                "WHERE id = ? AND archived = 0",
                (utc_now(), source_id),
            )
            # Remove stale selections from topics without relying on JSON1.
            for row in rows:
                ids = [item for item in _loads(row["source_ids_json"], []) if item != source_id]
                db.execute(
                    "UPDATE topics SET source_ids_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(ids), utc_now(), row["id"]),
                )
        if result.rowcount == 0:
            raise NotFoundError("新闻源不存在")

    @staticmethod
    def _restore_source_relations(db: sqlite3.Connection, source_id: str) -> None:
        snapshot = db.execute(
            "SELECT topic_ids_json FROM source_archive_snapshots WHERE source_id = ?", (source_id,)
        ).fetchone()
        if snapshot is None:
            return
        topic_ids = set(_loads(snapshot["topic_ids_json"], []))
        if topic_ids:
            rows = db.execute("SELECT id, source_ids_json FROM topics").fetchall()
            now = utc_now()
            for row in rows:
                if row["id"] not in topic_ids:
                    continue
                source_ids = _loads(row["source_ids_json"], [])
                if source_id not in source_ids:
                    source_ids.append(source_id)
                    db.execute(
                        "UPDATE topics SET source_ids_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(source_ids), now, row["id"]),
                    )
        db.execute("DELETE FROM source_archive_snapshots WHERE source_id = ?", (source_id,))

    def restore_source(self, source_id: str) -> dict[str, Any]:
        """Undo a soft archive and restore the topic bindings captured at deletion time."""

        with self.connect() as db:
            result = db.execute(
                "UPDATE sources SET archived = 0, enabled = 1, updated_at = ? "
                "WHERE id = ? AND archived = 1",
                (utc_now(), source_id),
            )
            if result.rowcount == 0:
                raise NotFoundError("已归档新闻源不存在")
            self._restore_source_relations(db, source_id)
        return self.get_source(source_id)

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
                (id, status, phase, trigger, requested_topics_json, started_at)
                VALUES (?, 'queued', 'queued', ?, ?, ?)""",
                (run_id, trigger, json.dumps(list(topic_ids)), utc_now()),
            )
        return self.get_run(run_id)

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "status": row["status"],
            "phase": row["phase"],
            "outcome": row["outcome"],
            "trigger": row["trigger"],
            "topic_ids": _loads(row["requested_topics_json"], []),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "article_count": row["article_count"],
            "source_success": row["source_success"],
            "source_failure": row["source_failure"],
            "analyses": row["analyses"],
            "duplicates_skipped": row["duplicates_skipped"],
            "funnel": _loads(row["funnel_json"], {}),
            "ai_requests": row["ai_requests"],
            "ai_success": row["ai_success"],
            "ai_failure": row["ai_failure"],
            "warning": row["warning"],
            "error": row["error"],
        }

    @staticmethod
    def _expected_import_run(record: dict[str, Any], topic_map: dict[str, str]) -> dict[str, Any]:
        return {
            "status": record["status"],
            "phase": record["phase"],
            "outcome": record["outcome"],
            "trigger": record["trigger"],
            "topic_ids": [topic_map[topic_id] for topic_id in record["topic_ids"]],
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "article_count": record["article_count"],
            "source_success": record["source_success"],
            "source_failure": record["source_failure"],
            "analyses": record["analyses"],
            "duplicates_skipped": record["duplicates_skipped"],
            "funnel": record["funnel"],
            "ai_requests": record["ai_requests"],
            "ai_success": record["ai_success"],
            "ai_failure": record["ai_failure"],
            "warning": record["warning"],
            "error": record["error"],
        }

    @classmethod
    def _import_run_matches(cls, row: sqlite3.Row | None, expected: dict[str, Any]) -> bool:
        if row is None:
            return False
        stored = cls._run(row)
        # Imported articles can be merged into an existing topic-level copy,
        # so article_count is recalculated locally and is not part of identity.
        return all(
            stored[key] == value for key, value in expected.items() if key != "article_count"
        )

    @staticmethod
    def _remapped_import_run_id(incoming_id: str, expected: dict[str, Any]) -> str:
        identity = {key: value for key, value in expected.items() if key != "article_count"}
        fingerprint = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"cookies-news-cockpit/import-run/{incoming_id}/{fingerprint}",
        ).hex

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
                SET status = 'failed', phase = 'finished', outcome = 'interrupted', finished_at = ?,
                    error = '应用上次在任务完成前退出；该任务已标记为中断。'
                WHERE status IN ('queued', 'running')""",
                (finished_at,),
            )
        return result.rowcount

    def update_run(self, run_id: str, **values: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "phase",
            "outcome",
            "finished_at",
            "article_count",
            "source_success",
            "source_failure",
            "analyses",
            "duplicates_skipped",
            "funnel",
            "ai_requests",
            "ai_success",
            "ai_failure",
            "warning",
            "error",
        }
        fields = {key: value for key, value in values.items() if key in allowed}
        if "funnel" in fields:
            fields["funnel_json"] = json.dumps(fields.pop("funnel"), ensure_ascii=False)
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
                 summary, analysis, score, published_at, created_at, content_hash,
                 analysis_mode, model, matched_keywords_json, matched_fields_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        article.get("analysis_mode", "rules"),
                        article.get("model"),
                        json.dumps(article.get("matched_keywords", []), ensure_ascii=False),
                        json.dumps(article.get("matched_fields", []), ensure_ascii=False),
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
        value["matched_keywords"] = _loads(value.pop("matched_keywords_json", None), [])
        value["matched_fields"] = _loads(value.pop("matched_fields_json", None), [])
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

    def preview_import(
        self,
        configuration: dict[str, Any],
        articles: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Summarize a decoded backup without changing local data."""

        # Keep the original bundle-shaped convenience call for internal/tests.
        if articles is None and "configuration" in configuration:
            bundle = configuration
            configuration = bundle.get("configuration") or {}
            history = bundle.get("history") or {}
            articles = history.get("articles") or []
            runs = history.get("runs") or []
        if articles is None:
            articles = []
        if runs is None:
            runs = []
        source_records, topic_records, run_records, article_records = _validated_import_records(
            configuration, articles, runs
        )

        with self.connect() as db:
            source_rows = db.execute("SELECT id, name, url, user_modified FROM sources").fetchall()
            source_by_url: dict[str, str] = {}
            for row in source_rows:
                normalized_url = _normalized_source_url(row["url"])
                if normalized_url in source_by_url:
                    raise ValueError(f"本机存在规范化后重复的新闻源地址：{normalized_url}")
                source_by_url[normalized_url] = row["id"]
            source_names: dict[str, set[str]] = {}
            for row in source_rows:
                source_names.setdefault(str(row["name"]).casefold(), set()).add(row["id"])
            source_map: dict[str, str] = {}
            sources_new = 0
            sources_matched = 0
            local_source_by_id = {row["id"]: row for row in source_rows}
            for record in source_records:
                destination_id = source_by_url.get(record["url"])
                if destination_id is None:
                    destination_id = f"new-source:{record['id']}"
                    source_by_url[record["url"]] = destination_id
                    source_names.setdefault(record["value"].name.casefold(), set()).add(
                        destination_id
                    )
                    local_source_by_id[destination_id] = {
                        "id": destination_id,
                        "name": record["value"].name,
                        "user_modified": bool(
                            record["user_modified"] or record["value"].preset_id is None
                        ),
                    }
                    sources_new += 1
                else:
                    sources_matched += 1
                    local = local_source_by_id.get(destination_id)
                    if (
                        local is not None
                        and not bool(local["user_modified"])
                        and record["user_modified"]
                    ):
                        old_key = str(local["name"]).casefold()
                        source_names.get(old_key, set()).discard(destination_id)
                        source_names.setdefault(record["value"].name.casefold(), set()).add(
                            destination_id
                        )
                        local_source_by_id[destination_id] = {
                            "id": destination_id,
                            "name": record["value"].name,
                            "user_modified": True,
                        }
                source_map[record["id"]] = destination_id

            topic_rows = db.execute("SELECT id, name, archived FROM topics").fetchall()
            topic_names: dict[str, set[str]] = {}
            for row in topic_rows:
                topic_names.setdefault(str(row["name"]).casefold(), set()).add(row["id"])
            topic_map: dict[str, str] = {}
            topics_new = 0
            topics_matched = 0
            active_topics = int(
                db.execute("SELECT COUNT(*) FROM topics WHERE archived = 0").fetchone()[0]
            )
            projected_active = active_topics
            for record in topic_records:
                name_key = record["value"].name.casefold()
                destination_id = _unique_name_match(
                    topic_names,
                    record["value"].name,
                    label="主题",
                )
                if destination_id is None:
                    destination_id = f"new-topic:{record['id']}"
                    topic_names.setdefault(name_key, set()).add(destination_id)
                    topics_new += 1
                    projected_active += int(not record["archived"])
                else:
                    topics_matched += 1
                topic_map[record["id"]] = destination_id

            run_rows = {row["id"]: row for row in db.execute("SELECT * FROM runs ORDER BY id")}
            runs_new = 0
            runs_matched = 0
            runs_remapped = 0
            for record in run_records:
                expected_run = self._expected_import_run(record, topic_map)
                existing_run = run_rows.get(record["id"])
                if self._import_run_matches(existing_run, expected_run):
                    runs_matched += 1
                    continue
                if existing_run is None:
                    runs_new += 1
                    continue
                remapped_id = self._remapped_import_run_id(record["id"], expected_run)
                remapped_run = run_rows.get(remapped_id)
                if remapped_run is not None:
                    if not self._import_run_matches(remapped_run, expected_run):
                        raise ValueError("导入运行记录 ID 发生不可安全合并的冲突")
                    runs_matched += 1
                    continue
                runs_new += 1
                runs_remapped += 1

            seen_pairs = {
                (row["topic_id"], row["content_hash"])
                for row in db.execute("SELECT topic_id, content_hash FROM articles")
            }
            articles_new = 0
            articles_matched = 0
            for record in article_records:
                topic_id = topic_map.get(record["topic_id"])
                if topic_id is None and record["topic_name"]:
                    topic_id = _unique_name_match(
                        topic_names,
                        record["topic_name"],
                        label="主题",
                    )
                if topic_id is None:
                    raise ValueError("备份文章引用了不存在的主题")

                source_id = source_map.get(record["source_id"])
                if source_id is None and record["source_name"]:
                    source_id = _unique_name_match(
                        source_names,
                        record["source_name"],
                        label="来源",
                    )
                if source_id is None:
                    raise ValueError("备份文章引用了不存在的新闻源")

                pair = (topic_id, record["content_hash"])
                if pair in seen_pairs:
                    articles_matched += 1
                else:
                    seen_pairs.add(pair)
                    articles_new += 1

        warnings = ["本机全局设置优先；备份中的 DeepSeek API Key 不会导入。"]
        topic_limit_conflict = projected_active > 10
        if topic_limit_conflict:
            warnings.append("导入后活动主题将超过 10 个，应用前需要归档部分主题。")
        source_summary = {
            "incoming": len(source_records),
            "new": sources_new,
            "matched": sources_matched,
        }
        topic_summary = {
            "incoming": len(topic_records),
            "new": topics_new,
            "matched": topics_matched,
        }
        article_summary = {
            "incoming": len(article_records),
            "new": articles_new,
            "matched": articles_matched,
        }
        run_summary = {
            "incoming": len(run_records),
            "new": runs_new,
            "matched": runs_matched,
            "remapped": runs_remapped,
        }
        return {
            "schema_version": int(configuration.get("schema_version", 1)),
            "sources": source_summary,
            "topics": topic_summary,
            "runs": run_summary,
            "articles": article_summary,
            "summary": {
                "sources": source_summary,
                "topics": topic_summary,
                "runs": run_summary,
                "articles": article_summary,
            },
            "conflicts": {"topic_limit": topic_limit_conflict},
            "warnings": warnings,
        }

    def import_backup(self, bundle: dict[str, Any]) -> dict[str, Any]:
        """Atomically merge a validated v1/v2 backup; local global settings win."""

        if not isinstance(bundle, dict):
            raise ValueError("备份必须是对象")
        configuration = bundle.get("configuration", {})
        history = bundle.get("history", {})
        if not isinstance(configuration, dict) or not isinstance(history, dict):
            raise ValueError("备份 configuration 和 history 必须是对象")
        incoming_articles = history.get("articles", [])
        incoming_runs = history.get("runs", [])
        return self._apply_validated_import(configuration, incoming_articles, incoming_runs)

    def _apply_validated_import(
        self,
        configuration: dict[str, Any],
        articles: list[dict[str, Any]],
        runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        source_records, topic_records, run_records, article_records = _validated_import_records(
            configuration, articles, runs
        )
        result: dict[str, Any] = {
            "sources_added": 0,
            "sources_matched": 0,
            "topics_added": 0,
            "topics_matched": 0,
            "runs_added": 0,
            "runs_matched": 0,
            "runs_remapped": 0,
            "articles_added": 0,
            "articles_matched": 0,
            "favorites_merged": 0,
            "latest_imported_run_id": None,
            "run_id": None,
        }
        source_map: dict[str, str] = {}
        topic_map: dict[str, str] = {}
        run_map: dict[str, str] = {}
        imported_run_records: dict[str, dict[str, Any]] = {}
        new_article_run_ids: set[str] = set()
        now = utc_now()

        with self.connect() as db:
            source_by_url: dict[str, sqlite3.Row | dict[str, Any]] = {}
            source_names: dict[str, set[str]] = {}
            for row in db.execute("SELECT id, name, url, user_modified FROM sources"):
                normalized_url = _normalized_source_url(row["url"])
                if normalized_url in source_by_url:
                    raise ValueError(f"本机存在规范化后重复的新闻源地址：{normalized_url}")
                source_by_url[normalized_url] = row
                source_names.setdefault(str(row["name"]).casefold(), set()).add(row["id"])
            for record in source_records:
                value: SourceInput = record["value"]
                existing = source_by_url.get(record["url"])
                if existing:
                    source_id = existing["id"]
                    if not bool(existing["user_modified"]):
                        if record["user_modified"]:
                            old_name_key = str(existing["name"]).casefold()
                            db.execute(
                                """UPDATE sources SET name = ?, homepage = ?, category = ?,
                                language = ?, terms = ?, enabled = ?, archived = ?,
                                user_modified = 1, updated_at = ? WHERE id = ?""",
                                (
                                    value.name,
                                    str(value.homepage) if value.homepage else None,
                                    value.category,
                                    value.language,
                                    str(value.terms) if value.terms else None,
                                    int(value.enabled),
                                    int(record["archived"]),
                                    now,
                                    source_id,
                                ),
                            )
                            source_by_url[record["url"]] = {
                                "id": source_id,
                                "name": value.name,
                                "user_modified": True,
                            }
                            source_names.get(old_name_key, set()).discard(source_id)
                            source_names.setdefault(value.name.casefold(), set()).add(source_id)
                        else:
                            db.execute(
                                """UPDATE sources SET enabled = ?, archived = ?, updated_at = ?
                                WHERE id = ?""",
                                (
                                    int(value.enabled),
                                    int(record["archived"]),
                                    now,
                                    source_id,
                                ),
                            )
                    result["sources_matched"] += 1
                else:
                    source_id = uuid.uuid4().hex
                    inserted_user_modified = bool(
                        record["user_modified"] or value.preset_id is None
                    )
                    db.execute(
                        """INSERT INTO sources
                        (id, name, url, homepage, category, language, terms, preset_id,
                         preset_version, user_modified, enabled, archived, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            source_id,
                            value.name,
                            record["url"],
                            str(value.homepage) if value.homepage else None,
                            value.category,
                            value.language,
                            str(value.terms) if value.terms else None,
                            value.preset_id,
                            value.preset_version,
                            int(inserted_user_modified),
                            int(value.enabled),
                            int(record["archived"]),
                            record["created_at"] or now,
                            now,
                        ),
                    )
                    source_by_url[record["url"]] = {
                        "id": source_id,
                        "name": value.name,
                        "user_modified": inserted_user_modified,
                    }
                    source_names.setdefault(value.name.casefold(), set()).add(source_id)
                    result["sources_added"] += 1
                source_map[record["id"]] = source_id

            topic_names: dict[str, set[str]] = {}
            for row in db.execute("SELECT id, name FROM topics"):
                topic_names.setdefault(str(row["name"]).casefold(), set()).add(row["id"])
            active_topics = int(
                db.execute("SELECT COUNT(*) FROM topics WHERE archived = 0").fetchone()[0]
            )
            for record in topic_records:
                value: TopicInput = record["value"]
                mapped_sources = [source_map[source_id] for source_id in value.source_ids]
                topic_id = _unique_name_match(topic_names, value.name, label="主题")
                if topic_id is not None:
                    result["topics_matched"] += 1
                else:
                    if not record["archived"] and active_topics >= 10:
                        raise TopicLimitError("导入后活动主题将超过 10 个，请先归档本机主题")
                    topic_id = uuid.uuid4().hex
                    db.execute(
                        """INSERT INTO topics
                        (id, name, keywords_json, exclusion_keywords_json, threshold,
                         article_limit, source_ids_json, enabled, archived, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            topic_id,
                            value.name,
                            json.dumps(value.keywords, ensure_ascii=False),
                            json.dumps(value.exclusion_keywords, ensure_ascii=False),
                            value.threshold,
                            value.article_limit,
                            json.dumps(mapped_sources),
                            int(value.enabled and not record["archived"]),
                            int(record["archived"]),
                            record["created_at"] or now,
                            now,
                        ),
                    )
                    topic_names.setdefault(value.name.casefold(), set()).add(topic_id)
                    active_topics += int(not record["archived"])
                    result["topics_added"] += 1
                topic_map[record["id"]] = topic_id

            for record in run_records:
                expected_run = self._expected_import_run(record, topic_map)

                run_id = record["id"]
                existing_run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                if self._import_run_matches(existing_run, expected_run):
                    result["runs_matched"] += 1
                else:
                    remapped = existing_run is not None
                    if remapped:
                        run_id = self._remapped_import_run_id(record["id"], expected_run)

                    destination = db.execute(
                        "SELECT * FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    if destination is not None:
                        if not self._import_run_matches(destination, expected_run):
                            raise ValueError("导入运行记录 ID 发生不可安全合并的冲突")
                        result["runs_matched"] += 1
                    else:
                        db.execute(
                            """INSERT INTO runs
                            (id, status, phase, outcome, trigger, requested_topics_json,
                             started_at, finished_at, article_count, source_success,
                             source_failure, analyses, duplicates_skipped, funnel_json,
                             ai_requests, ai_success, ai_failure, warning, error)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                run_id,
                                record["status"],
                                record["phase"],
                                record["outcome"],
                                record["trigger"],
                                json.dumps(expected_run["topic_ids"]),
                                record["started_at"],
                                record["finished_at"],
                                record["article_count"],
                                record["source_success"],
                                record["source_failure"],
                                record["analyses"],
                                record["duplicates_skipped"],
                                json.dumps(record["funnel"], ensure_ascii=False),
                                record["ai_requests"],
                                record["ai_success"],
                                record["ai_failure"],
                                record["warning"],
                                record["error"],
                            ),
                        )
                        result["runs_added"] += 1
                        result["runs_remapped"] += int(remapped)
                run_map[record["id"]] = run_id
                imported_run_records[run_id] = record

            synthetic_run_id: str | None = None
            synthetic_topic_ids: set[str] = set()
            imported_runs_with_articles: set[str] = set()
            for record in article_records:
                topic_id = topic_map.get(record["topic_id"])
                if topic_id is None and record["topic_name"]:
                    topic_id = _unique_name_match(
                        topic_names,
                        record["topic_name"],
                        label="主题",
                    )
                if topic_id is None:
                    raise ValueError("备份文章引用了不存在的主题")

                source_id = source_map.get(record["source_id"])
                if source_id is None and record["source_name"]:
                    source_id = _unique_name_match(
                        source_names,
                        record["source_name"],
                        label="来源",
                    )
                if source_id is None:
                    raise ValueError("备份文章引用了不存在的新闻源")

                if run_records:
                    run_id = run_map[record["run_id"]]
                    imported_runs_with_articles.add(run_id)

                existing = db.execute(
                    """SELECT id, favorite FROM articles
                    WHERE topic_id = ? AND content_hash = ? ORDER BY created_at LIMIT 1""",
                    (topic_id, record["content_hash"]),
                ).fetchone()
                if existing:
                    result["articles_matched"] += 1
                    if record["favorite"] and not bool(existing["favorite"]):
                        db.execute(
                            "UPDATE articles SET favorite = 1 WHERE id = ?", (existing["id"],)
                        )
                        result["favorites_merged"] += 1
                    continue

                if not run_records:
                    if synthetic_run_id is None:
                        synthetic_run_id = uuid.uuid4().hex
                        db.execute(
                            """INSERT INTO runs
                            (id, status, phase, outcome, trigger, requested_topics_json,
                             started_at, finished_at)
                            VALUES (?, 'complete', 'finished', 'imported', 'import', '[]', ?, ?)""",
                            (synthetic_run_id, now, now),
                        )
                        imported_run_records[synthetic_run_id] = {
                            "status": "complete",
                            "started_at": now,
                            "finished_at": now,
                        }
                        result["runs_added"] += 1
                    run_id = synthetic_run_id
                    synthetic_topic_ids.add(topic_id)

                incoming_article_id = record["id"]
                article_id = incoming_article_id
                if (
                    incoming_article_id.startswith("legacy-")
                    or db.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone()
                ):
                    article_id = uuid.uuid4().hex
                db.execute(
                    """INSERT INTO articles
                    (id, run_id, topic_id, source_id, title, url, excerpt, full_text,
                     summary, analysis, score, published_at, created_at, favorite, content_hash,
                     analysis_mode, model, matched_keywords_json, matched_fields_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        article_id,
                        run_id,
                        topic_id,
                        source_id,
                        record["title"],
                        record["url"],
                        record["excerpt"],
                        record["summary"],
                        record["analysis"],
                        record["score"],
                        record["published_at"],
                        record["created_at"],
                        int(record["favorite"]),
                        record["content_hash"],
                        record["analysis_mode"],
                        record["model"],
                        json.dumps(record["matched_keywords"], ensure_ascii=False),
                        json.dumps(record["matched_fields"], ensure_ascii=False),
                    ),
                )
                with suppress(sqlite3.OperationalError):
                    db.execute(
                        """INSERT INTO article_fts(id, title, excerpt, summary, analysis)
                        VALUES (?, ?, ?, ?, ?)""",
                        (
                            article_id,
                            record["title"],
                            record["excerpt"],
                            record["summary"],
                            record["analysis"],
                        ),
                    )
                new_article_run_ids.add(run_id)
                result["articles_added"] += 1

            if synthetic_run_id is not None:
                db.execute(
                    """UPDATE runs SET requested_topics_json = ?, article_count = ?,
                    funnel_json = ? WHERE id = ?""",
                    (
                        json.dumps(sorted(synthetic_topic_ids)),
                        result["articles_added"],
                        json.dumps({"imported": result["articles_added"]}),
                        synthetic_run_id,
                    ),
                )

            for imported_run_id in imported_runs_with_articles:
                actual_count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM articles WHERE run_id = ?",
                        (imported_run_id,),
                    ).fetchone()[0]
                )
                db.execute(
                    "UPDATE runs SET article_count = ? WHERE id = ?",
                    (actual_count, imported_run_id),
                )

            candidates = [
                (run_id, record)
                for run_id, record in imported_run_records.items()
                if run_id in new_article_run_ids and record["status"] in {"complete", "degraded"}
            ]
            if candidates:
                latest_run_id, _ = max(
                    candidates,
                    key=lambda item: (
                        item[1].get("finished_at") or item[1].get("started_at") or "",
                        item[0],
                    ),
                )
                result["latest_imported_run_id"] = latest_run_id
                result["run_id"] = latest_run_id
        return result

    def apply_import(
        self,
        configuration: dict[str, Any],
        articles: list[dict[str, Any]],
        runs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compatibility facade for the API's preview/apply import contract."""

        return self.import_backup(
            {
                "configuration": configuration,
                "history": {"articles": articles, "runs": runs or []},
            }
        )

    def export_runs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY started_at, id").fetchall()
        return [self._run(row) for row in rows]

    def metadata_warning(self) -> dict[str, Any] | None:
        value = self.get_metadata("preset_migration_warning")
        return value if isinstance(value, dict) else None

    def recent_article_fingerprints(self, days: int = 7) -> list[dict[str, str]]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connect() as db:
            rows = db.execute(
                """SELECT a.url, a.title FROM articles a
                JOIN runs r ON r.id = a.run_id
                WHERE a.created_at >= ?
                  AND r.status IN ('complete', 'degraded')
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

    @staticmethod
    def _ai_cache_identity(
        content_fingerprint: str,
        topic_semantic_config: dict[str, Any] | str,
        model: str,
    ) -> tuple[str, str]:
        if isinstance(topic_semantic_config, str):
            canonical_topic = topic_semantic_config
        else:
            canonical_topic = json.dumps(
                topic_semantic_config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        topic_fingerprint = hashlib.sha256(canonical_topic.encode("utf-8")).hexdigest()
        material = "\0".join((content_fingerprint, topic_fingerprint, model))
        return hashlib.sha256(material.encode("utf-8")).hexdigest(), topic_fingerprint

    def get_ai_cache(
        self,
        content_fingerprint: str,
        topic_semantic_config: dict[str, Any] | str,
        model: str,
    ) -> dict[str, Any] | None:
        cache_key, _ = self._ai_cache_identity(content_fingerprint, topic_semantic_config, model)
        now = utc_now()
        with self.connect() as db:
            row = db.execute(
                "SELECT analysis_json, expires_at FROM ai_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is not None and row["expires_at"] <= now:
                db.execute("DELETE FROM ai_cache WHERE cache_key = ?", (cache_key,))
                row = None
        return _loads(row["analysis_json"], None) if row else None

    def put_ai_cache(
        self,
        content_fingerprint: str,
        topic_semantic_config: dict[str, Any] | str,
        model: str,
        analysis: dict[str, Any],
        ttl_days: int = 7,
    ) -> None:
        cache_key, topic_fingerprint = self._ai_cache_identity(
            content_fingerprint, topic_semantic_config, model
        )
        now_dt = datetime.now(UTC)
        created_at = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(days=max(1, ttl_days))).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                """INSERT INTO ai_cache
                (cache_key, content_fingerprint, topic_fingerprint, model, analysis_json,
                 created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET analysis_json = excluded.analysis_json,
                created_at = excluded.created_at, expires_at = excluded.expires_at""",
                (
                    cache_key,
                    content_fingerprint,
                    topic_fingerprint,
                    model,
                    json.dumps(analysis, ensure_ascii=False),
                    created_at,
                    expires_at,
                ),
            )

    def purge_ai_cache(self) -> int:
        with self.connect() as db:
            result = db.execute("DELETE FROM ai_cache WHERE expires_at <= ?", (utc_now(),))
        return result.rowcount

    def purge_expired_ai_cache(self) -> int:
        return self.purge_ai_cache()

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
