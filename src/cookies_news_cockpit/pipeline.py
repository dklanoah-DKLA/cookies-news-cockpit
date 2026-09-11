from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .dedup import DuplicateIndex, canonical_url, normalize_title
from .keystore import DeepSeekKeyStore, KeyStoreError
from .matching import KeywordMatch, match_article
from .models import AIAnalysis, FeedArticle, RunRequest, SettingsUpdate
from .runtime import AppPaths
from .services import DEEPSEEK_MODEL, DeepSeekClient, DeepSeekError, FeedService
from .storage import Database, NotFoundError, utc_now, validate_run_id_token

ANALYSIS_BREAKER = 500
FULLTEXT_SHORTLIST_MAX = 100
ZERO_HIT_REVIEW_MAX = 20
SEMANTIC_EXPANSION_MAX = 15
FUTURE_TOLERANCE = timedelta(days=1)
LOGGER = logging.getLogger(__name__)

FUNNEL_COUNTERS = (
    # 1.2 stage totals.  The 1.1 keys below remain intact for old clients and
    # imported run artifacts.
    "discovered",
    "processed",
    "results",
    "unique_candidates",
    "feed_items",
    "fresh",
    "date_rejected",
    "date_missing",
    "date_invalid",
    "date_stale",
    "date_future",
    "keyword_hits",
    "exclusion_rejected",
    "fulltext_fetches",
    "fulltext_success",
    "fulltext_failure",
    "fulltext_keyword_hits",
    "fulltext_recovery_budget_exhausted",
    "duplicates_skipped",
    "history_duplicates",
    "run_duplicates",
    "semantic_candidates",
    "semantic_reviewed",
    "semantic_initial_reviewed",
    "semantic_additional_reviewed",
    "semantic_fallback",
    "semantic_expansion",
    "semantic_budget_exhausted",
    "budget_exhausted",
    "ai_candidates",
    "ai_requests",
    "ai_success",
    "ai_failure",
    "ai_cache_hits",
    "ai_irrelevant",
    "reason_direct_match",
    "reason_contextual_match",
    "reason_background_context",
    "reason_off_topic",
    "reason_exclusion_match",
    "reason_insufficient_evidence",
    "reason_other",
    "threshold_rejected",
    "below_supplement",
    "below_extended",
    "core_selected",
    "supplement_selected",
    "extended_selected",
    "selected",
)

RELEVANCE_REASON_COUNTERS = {
    "direct_match": "reason_direct_match",
    "contextual_match": "reason_contextual_match",
    "background_context": "reason_background_context",
    "off_topic": "reason_off_topic",
    "exclusion_match": "reason_exclusion_match",
    "insufficient_evidence": "reason_insufficient_evidence",
}


def _content_hash(article: FeedArticle) -> str:
    identity = f"{canonical_url(article.url)}\n{normalize_title(article.title)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(FUNNEL_COUNTERS, 0)


def _new_funnel(
    topics: list[dict[str, Any]], sources: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        **_empty_counts(),
        "per_topic": {
            topic["id"]: {"name": topic["name"], **_empty_counts()} for topic in topics
        },
        "per_source": {
            source["id"]: {"name": source["name"], **_empty_counts()} for source in sources
        },
    }


def _bump(
    funnel: dict[str, Any],
    key: str,
    amount: int = 1,
    *,
    topic_id: str | None = None,
    source_id: str | None = None,
    total: bool = True,
) -> None:
    if total:
        funnel[key] = int(funnel.get(key, 0)) + amount
    if topic_id and topic_id in funnel.get("per_topic", {}):
        counter = funnel["per_topic"][topic_id]
        counter[key] = int(counter.get(key, 0)) + amount
    if source_id and source_id in funnel.get("per_source", {}):
        counter = funnel["per_source"][source_id]
        counter[key] = int(counter.get(key, 0)) + amount


def _keyword_score(evidence: KeywordMatch) -> int:
    title_hits = sum(
        "title" in fields for fields in evidence.keyword_fields.values()
    )
    return min(95, 58 + len(evidence.matched_keywords) * 7 + title_hits * 8)


def _published_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _freshness_result(
    value: str | None,
    days: int | None,
    now: datetime,
    *,
    allow_undated_fixture: bool = False,
) -> tuple[bool, str | None]:
    if days is None:
        return True, None
    if not value:
        return (True, None) if allow_undated_fixture else (False, "date_missing")
    published = _published_datetime(value)
    if published is None:
        return (True, None) if allow_undated_fixture else (False, "date_invalid")
    if published > now + FUTURE_TOLERANCE:
        return False, "date_future"
    if published < now - timedelta(days=days):
        return False, "date_stale"
    return True, None


def _published_rank(value: str | None) -> float:
    parsed = _published_datetime(value)
    if parsed is None:
        return 0.0
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return 0.0


def _analysis_fingerprint(article: FeedArticle) -> str:
    material = "\0".join(
        (
            canonical_url(article.url),
            normalize_title(article.title),
            article.excerpt,
            article.full_text,
            article.published_at or "",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _artifact_path_for_run(runs_directory: Path, run_id: str) -> Path:
    """Resolve one immutable report path without allowing directory escape."""

    safe_id = validate_run_id_token(run_id)
    root = runs_directory.resolve()
    destination = (root / f"{safe_id}.json").resolve()
    if destination.parent != root:
        raise ValueError("运行报告路径超出应用数据目录")
    return destination


@dataclass(slots=True)
class _Candidate:
    article: FeedArticle
    evidence: KeywordMatch
    lexical_score: int
    semantic: bool = False


@dataclass(slots=True)
class _Ranked:
    candidate: _Candidate
    score: int
    summary: str
    analysis: str
    analysis_mode: str
    model: str | None
    selection_tier: str
    relevance_reason: str


@dataclass(slots=True)
class _TopicWork:
    topic: dict[str, Any]
    threshold: int
    supplement_threshold: int
    limit: int
    source_order: list[str]
    direct_candidates: list[_Candidate]
    semantic_candidates: list[_Candidate]
    prepared_semantic_candidates: list[_Candidate]
    ranked: list[_Ranked]
    direct_position: int = 0
    semantic_position: int = 0
    semantic_review_position: int = 0


class RunConflictError(RuntimeError):
    pass


class RunManager:
    def __init__(
        self,
        db: Database,
        paths: AppPaths,
        *,
        key_store: DeepSeekKeyStore | None = None,
        feed_service: FeedService | None = None,
        deepseek_factory: Callable[[str], DeepSeekClient] | None = None,
    ):
        self.db = db
        self.paths = paths
        self.key_store = key_store or DeepSeekKeyStore()
        self.feed_service = feed_service or FeedService()
        self.deepseek_factory = deepseek_factory or DeepSeekClient
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return any(not task.done() for task in self.tasks.values())

    def _record_deepseek_status(
        self,
        status: str,
        *,
        error: str | None,
        tested: bool,
    ) -> None:
        """Keep the global four-state indicator aligned with real run traffic."""

        try:
            values: dict[str, Any] = {
                "deepseek_status": status,
                "deepseek_last_error": error[:500] if error else None,
            }
            if tested:
                values["deepseek_last_tested_at"] = utc_now()
            self.db.update_settings(SettingsUpdate(**values))
        except Exception:
            # Losing a cosmetic health update must not discard an otherwise
            # valid news run or cause another paid request.
            LOGGER.exception("Could not persist DeepSeek connection status")

    async def cancel(self, run_id: str) -> dict[str, Any]:
        """Cancel an active run without publishing a partial report."""

        run = self.db.get_run(run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        task = self.tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        run = self.db.get_run(run_id)
        if run["status"] in {"queued", "running"}:
            run = self.db.update_run(
                run_id,
                status="cancelled",
                phase="finished",
                outcome="cancelled",
                finished_at=utc_now(),
                warning="任务已取消；未生成或替换新闻报告。",
            )
            try:
                self._write_artifact(run, [])
            except (FileExistsError, OSError):
                LOGGER.exception("Could not persist cancellation artifact for run %s", run_id)
        return run

    async def _fetch_metadata(self, source: dict[str, Any]) -> list[FeedArticle]:
        fetch_metadata = getattr(self.feed_service, "fetch_metadata", None)
        if callable(fetch_metadata):
            return await fetch_metadata(source)
        # Compatibility for injected 0.1 feed doubles. Production FeedService
        # always takes the metadata-first branch.
        return await self.feed_service.fetch(source, extract_full_text=False)

    async def _enrich_full_text(
        self, article: FeedArticle
    ) -> tuple[FeedArticle, bool]:
        if article.full_text:
            return article, False
        enrich = getattr(self.feed_service, "enrich_full_text", None)
        if not callable(enrich):
            return article, False
        try:
            return await enrich(article), True
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Optional article extraction failed for %s", article.url)
            return article, True

    @staticmethod
    def _round_robin_latest(
        articles: list[FeedArticle],
        source_order: list[str],
        limit: int,
    ) -> list[FeedArticle]:
        grouped: dict[str, list[FeedArticle]] = {source_id: [] for source_id in source_order}
        for article in articles:
            grouped.setdefault(article.source_id, []).append(article)
        for values in grouped.values():
            values.sort(
                key=lambda item: (
                    -_published_rank(item.published_at),
                    canonical_url(item.url),
                )
            )
        positions = dict.fromkeys(grouped, 0)
        selected: list[FeedArticle] = []
        while len(selected) < limit:
            added = False
            for source_id in source_order:
                values = grouped.get(source_id, [])
                position = positions.get(source_id, 0)
                if position >= len(values):
                    continue
                selected.append(values[position])
                positions[source_id] = position + 1
                added = True
                if len(selected) >= limit:
                    break
            if not added:
                break
        return selected

    @classmethod
    def _round_robin_candidates(
        cls,
        candidates: list[_Candidate],
        source_order: list[str],
    ) -> list[_Candidate]:
        """Keep every candidate while alternating its configured sources."""

        by_identity: dict[tuple[str, str], list[_Candidate]] = {}
        articles: list[FeedArticle] = []
        for candidate in candidates:
            identity = (canonical_url(candidate.article.url), candidate.article.title)
            by_identity.setdefault(identity, []).append(candidate)
            articles.append(candidate.article)
        ordered_articles = cls._round_robin_latest(articles, source_order, len(articles))
        ordered: list[_Candidate] = []
        for article in ordered_articles:
            identity = (canonical_url(article.url), article.title)
            matches = by_identity.get(identity)
            if matches:
                ordered.append(matches.pop(0))
        return ordered

    @staticmethod
    def _deduplicate_candidates(
        candidates: list[_Candidate],
        duplicate_index: DuplicateIndex,
        funnel: dict[str, Any],
        topic_id: str,
        *,
        preserve_order: bool = False,
    ) -> list[_Candidate]:
        ordered = candidates
        if not preserve_order:
            ordered = sorted(
                candidates,
                key=lambda item: (
                    -item.lexical_score,
                    -len(item.article.full_text or item.article.excerpt),
                    -_published_rank(item.article.published_at),
                    canonical_url(item.article.url),
                ),
            )
        topic_index = DuplicateIndex()
        seen_hashes: set[str] = set()
        unique: list[_Candidate] = []
        for candidate in ordered:
            article = candidate.article
            digest = _content_hash(article)
            history_duplicate = duplicate_index.is_duplicate(article.url, article.title)
            run_duplicate = digest in seen_hashes or topic_index.is_duplicate(
                article.url, article.title
            )
            if history_duplicate or run_duplicate:
                _bump(
                    funnel,
                    "duplicates_skipped",
                    topic_id=topic_id,
                    source_id=article.source_id,
                )
                _bump(
                    funnel,
                    "history_duplicates" if history_duplicate else "run_duplicates",
                    topic_id=topic_id,
                    source_id=article.source_id,
                )
                continue
            seen_hashes.add(digest)
            topic_index.add(article.url, article.title)
            unique.append(candidate)
        return unique

    async def start(self, request: RunRequest, trigger: str = "manual") -> dict[str, Any]:
        async with self._lock:
            if self.active:
                raise RunConflictError("已有新闻任务正在运行")
            topics = self.db.list_topics(enabled_only=True)
            if request.topic_ids is not None:
                requested = set(request.topic_ids)
                topics = [topic for topic in topics if topic["id"] in requested]
                missing = requested - {topic["id"] for topic in topics}
                if missing:
                    raise NotFoundError("请求中包含不存在或已停用的主题")
            if not topics:
                raise ValueError("请先创建并启用至少一个主题")
            run = self.db.create_run([topic["id"] for topic in topics], trigger)
            task = asyncio.create_task(
                self._execute(run["id"], topics, request.article_limit),
                name=f"news-run-{run['id']}",
            )
            self.tasks[run["id"]] = task
            task.add_done_callback(lambda _task, run_id=run["id"]: self.tasks.pop(run_id, None))
            return run

    async def run_now(self, request: RunRequest, trigger: str = "test") -> dict[str, Any]:
        """Execute inline; useful for deterministic CLI/tests."""

        topics = self.db.list_topics(enabled_only=True)
        if request.topic_ids is not None:
            requested = set(request.topic_ids)
            topics = [topic for topic in topics if topic["id"] in requested]
        if not topics:
            raise ValueError("请先创建并启用至少一个主题")
        run = self.db.create_run([topic["id"] for topic in topics], trigger)
        await self._execute(run["id"], topics, request.article_limit)
        return self.db.get_run(run["id"])

    async def _execute(
        self,
        run_id: str,
        topics: list[dict[str, Any]],
        run_article_limit: int | None,
    ) -> None:
        self.db.update_run(run_id, status="running", phase="fetching")
        source_success = 0
        source_failure = 0
        analysis_count = 0
        ai_success = 0
        ai_failure = 0
        warning_parts: list[str] = []
        source_errors: list[dict[str, str]] = []
        funnel: dict[str, Any] = {**_empty_counts(), "per_topic": {}, "per_source": {}}
        pending_articles: list[dict[str, Any]] = []
        post_dedup_candidates = 0
        previous_good: str | None = None
        ai_failed = False
        key_store_failed = False
        breaker_hit = False
        try:
            settings = self.db.get_settings()
            previous_good = self.db.get_latest_good_run_id()
            duplicate_index = DuplicateIndex(self.db.recent_article_fingerprints(days=7))
            all_sources = {
                source["id"]: source for source in self.db.list_sources(enabled_only=True)
            }
            selected_ids: set[str] = set()
            for topic in topics:
                selected_ids.update(topic["source_ids"] or all_sources.keys())
            sources = [source for source in all_sources.values() if source["id"] in selected_ids]
            if not sources:
                raise ValueError("没有可用于所选主题的已启用新闻源")
            funnel = _new_funnel(topics, sources)
            # Keep errors on the current run too: the visible report may be an
            # older usable result and must not be used to explain this attempt.
            funnel["source_errors"] = source_errors
            self.db.update_run(run_id, phase="fetching", funnel=funnel)

            fetched: dict[str, list[FeedArticle]] = {}
            fresh_by_source: dict[str, list[FeedArticle]] = {}
            date_rejections_by_source: dict[str, dict[str, int]] = {}
            now = datetime.now(UTC)
            # Existing unit fixtures intentionally omit publication dates. A
            # real manual/scheduled run is always strict under finite windows.
            allow_undated_fixture = self.db.get_run(run_id)["trigger"] == "test"
            for source in sources:
                try:
                    items = await self._fetch_metadata(source)
                    fetched[source["id"]] = items
                    _bump(funnel, "feed_items", len(items), source_id=source["id"])
                    _bump(funnel, "discovered", len(items), source_id=source["id"])
                    fresh_items: list[FeedArticle] = []
                    source_date_rejections = {
                        "date_missing": 0,
                        "date_invalid": 0,
                        "date_stale": 0,
                        "date_future": 0,
                    }
                    for article in items:
                        is_fresh, reason = _freshness_result(
                            article.published_at,
                            settings.freshness_days,
                            now,
                            allow_undated_fixture=allow_undated_fixture,
                        )
                        if is_fresh:
                            fresh_items.append(article)
                            _bump(funnel, "fresh", source_id=source["id"])
                            continue
                        _bump(funnel, "date_rejected", source_id=source["id"])
                        if reason:
                            _bump(funnel, reason, source_id=source["id"])
                            source_date_rejections[reason] += 1
                    fresh_by_source[source["id"]] = fresh_items
                    date_rejections_by_source[source["id"]] = source_date_rejections
                    source_success += 1
                    self.db.set_source_health(source["id"], True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    source_failure += 1
                    message = str(exc)[:500]
                    source_errors.append({"source_id": source["id"], "error": message})
                    self.db.set_source_health(source["id"], False, message)
                self.db.update_run(
                    run_id,
                    phase="fetching",
                    source_success=source_success,
                    source_failure=source_failure,
                    funnel=funnel,
                )

            if source_success == 0:
                raise RuntimeError("所有新闻源均抓取失败")

            try:
                api_key = self.key_store.get()
            except KeyStoreError as exc:
                api_key = None
                key_store_failed = True
                self._record_deepseek_status(
                    "error",
                    error=str(exc),
                    tested=False,
                )
                warning_parts.append(
                    f"无法读取系统钥匙串中的 DeepSeek API Key：{str(exc)[:220]} 本次使用规则模式。"
                )
            if not api_key and not key_store_failed:
                self._record_deepseek_status(
                    "unconfigured",
                    error=None,
                    tested=False,
                )
            ai_client = self.deepseek_factory(api_key) if api_key else None
            ai_live = ai_client is not None
            ai_connection_confirmed = False
            semantic_initial_budget = min(
                ZERO_HIT_REVIEW_MAX, settings.semantic_fallback_limit
            )
            self.db.update_run(run_id, phase="filtering", funnel=funnel)

            topic_work: list[_TopicWork] = []
            for topic in topics:
                threshold = (
                    topic["threshold"]
                    if topic["threshold"] is not None
                    else settings.default_threshold
                )
                supplement_threshold = min(threshold, max(40, threshold - 15))
                limit = (
                    run_article_limit or topic["article_limit"] or settings.default_article_limit
                )
                configured_sources = topic["source_ids"] or list(fetched)
                source_order = [
                    source["id"]
                    for source in sources
                    if source["id"] in configured_sources and source["id"] in fetched
                ]
                topic_feed_items = sum(len(fetched.get(item, [])) for item in source_order)
                topic_fresh_items = sum(len(fresh_by_source.get(item, [])) for item in source_order)
                _bump(
                    funnel,
                    "feed_items",
                    topic_feed_items,
                    topic_id=topic["id"],
                    total=False,
                )
                _bump(
                    funnel,
                    "discovered",
                    topic_feed_items,
                    topic_id=topic["id"],
                    total=False,
                )
                _bump(
                    funnel,
                    "fresh",
                    topic_fresh_items,
                    topic_id=topic["id"],
                    total=False,
                )
                topic_date_rejected = topic_feed_items - topic_fresh_items
                _bump(
                    funnel,
                    "date_rejected",
                    topic_date_rejected,
                    topic_id=topic["id"],
                    total=False,
                )
                for reason in (
                    "date_missing",
                    "date_invalid",
                    "date_stale",
                    "date_future",
                ):
                    _bump(
                        funnel,
                        reason,
                        sum(
                            date_rejections_by_source.get(source_id, {}).get(reason, 0)
                            for source_id in source_order
                        ),
                        topic_id=topic["id"],
                        total=False,
                    )
                topic_funnel = funnel["per_topic"][topic["id"]]
                topic_funnel["core_threshold"] = threshold
                topic_funnel["supplement_threshold"] = supplement_threshold
                topic_funnel["target_count"] = limit
                keywords = topic["keywords"] or [topic["name"]]
                exclusions = topic.get("exclusion_keywords", [])
                initial_candidates: list[_Candidate] = []
                unmatched: list[FeedArticle] = []
                for source_id in source_order:
                    for article in fresh_by_source.get(source_id, []):
                        evidence = match_article(article, keywords, exclusions)
                        if evidence.excluded_keywords:
                            _bump(
                                funnel,
                                "exclusion_rejected",
                                topic_id=topic["id"],
                                source_id=article.source_id,
                            )
                            continue
                        if evidence.matched:
                            _bump(
                                funnel,
                                "keyword_hits",
                                topic_id=topic["id"],
                                source_id=article.source_id,
                            )
                            initial_candidates.append(
                                _Candidate(article, evidence, _keyword_score(evidence))
                            )
                        else:
                            unmatched.append(article)
                direct_candidates = self._deduplicate_candidates(
                    initial_candidates, duplicate_index, funnel, topic["id"]
                )
                review_order = self._round_robin_latest(
                    unmatched,
                    source_order,
                    len(unmatched),
                )
                semantic_candidates = self._deduplicate_candidates(
                    [
                        _Candidate(
                            article,
                            match_article(article, keywords, exclusions),
                            0,
                            True,
                        )
                        for article in review_order
                    ],
                    duplicate_index,
                    funnel,
                    topic["id"],
                    preserve_order=True,
                )
                unique_count = len(direct_candidates) + len(semantic_candidates)
                post_dedup_candidates += unique_count
                for candidate in (*direct_candidates, *semantic_candidates):
                    _bump(
                        funnel,
                        "unique_candidates",
                        topic_id=topic["id"],
                        source_id=candidate.article.source_id,
                    )
                direct_review_limit = min(FULLTEXT_SHORTLIST_MAX, max(20, limit * 3))
                topic_work.append(
                    _TopicWork(
                        topic=topic,
                        threshold=threshold,
                        supplement_threshold=supplement_threshold,
                        limit=limit,
                        source_order=source_order,
                        direct_candidates=self._round_robin_candidates(
                            direct_candidates, source_order
                        )[:direct_review_limit],
                        semantic_candidates=semantic_candidates,
                        prepared_semantic_candidates=[],
                        ranked=[],
                    )
                )

            fulltext_attempt_count = 0
            # A fetched article can be bound to several topics.  Cache even an
            # empty/failed extraction so one URL consumes at most one real
            # network attempt and one slot from the run-wide safety cap.
            fulltext_by_url: dict[str, tuple[str, bool]] = {}

            async def prepare_candidate(
                work: _TopicWork, candidate: _Candidate
            ) -> _Candidate | None:
                nonlocal fulltext_attempt_count

                article = candidate.article
                _bump(
                    funnel,
                    "processed",
                    topic_id=work.topic["id"],
                    source_id=article.source_id,
                )
                if settings.extract_full_text and not article.full_text:
                    fulltext_key = canonical_url(article.url) or article.url
                    cached_fulltext = fulltext_by_url.get(fulltext_key)
                    if cached_fulltext is not None:
                        full_text, attempted = cached_fulltext
                        article = article.model_copy(update={"full_text": full_text})
                        if attempted:
                            # Global/source totals describe real network work;
                            # the topic still records that full text was
                            # available (or unavailable) for its own matching.
                            _bump(
                                funnel,
                                "fulltext_success" if full_text.strip() else "fulltext_failure",
                                topic_id=work.topic["id"],
                                total=False,
                            )
                    elif fulltext_attempt_count < FULLTEXT_SHORTLIST_MAX:
                        article, attempted = await self._enrich_full_text(article)
                        fulltext_by_url[fulltext_key] = (article.full_text, attempted)
                        if attempted:
                            fulltext_attempt_count += 1
                            _bump(
                                funnel,
                                "fulltext_fetches",
                                topic_id=work.topic["id"],
                                source_id=article.source_id,
                            )
                            _bump(
                                funnel,
                                (
                                    "fulltext_success"
                                    if article.full_text.strip()
                                    else "fulltext_failure"
                                ),
                                topic_id=work.topic["id"],
                                source_id=article.source_id,
                            )
                    else:
                        fulltext_by_url[fulltext_key] = (article.full_text, False)
                        _bump(
                            funnel,
                            "fulltext_recovery_budget_exhausted",
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                keywords = work.topic["keywords"] or [work.topic["name"]]
                exclusions = work.topic.get("exclusion_keywords", [])
                evidence = match_article(article, keywords, exclusions)
                if evidence.excluded_keywords:
                    _bump(
                        funnel,
                        "exclusion_rejected",
                        topic_id=work.topic["id"],
                        source_id=article.source_id,
                    )
                    _bump(
                        funnel,
                        "reason_exclusion_match",
                        topic_id=work.topic["id"],
                        source_id=article.source_id,
                    )
                    return None
                if evidence.matched and not candidate.evidence.matched:
                    _bump(
                        funnel,
                        "keyword_hits",
                        topic_id=work.topic["id"],
                        source_id=article.source_id,
                    )
                    _bump(
                        funnel,
                        "fulltext_keyword_hits",
                        topic_id=work.topic["id"],
                        source_id=article.source_id,
                    )
                return _Candidate(
                    article,
                    evidence,
                    _keyword_score(evidence) if evidence.matched else 0,
                    not evidence.matched,
                )

            def bump_reason(work: _TopicWork, article: FeedArticle, reason: str) -> str:
                normalized = str(reason or "").strip().casefold()
                counter = RELEVANCE_REASON_COUNTERS.get(normalized, "reason_other")
                _bump(
                    funnel,
                    counter,
                    topic_id=work.topic["id"],
                    source_id=article.source_id,
                )
                return normalized or "direct_match"

            async def analyze_candidate(
                work: _TopicWork,
                candidate: _Candidate,
                *,
                semantic_stage: str | None = None,
            ) -> bool:
                nonlocal analysis_count, ai_connection_confirmed, ai_failed
                nonlocal ai_failure, ai_live, ai_success, breaker_hit

                article = candidate.article
                if candidate.semantic and (
                    not api_key or not settings.semantic_fallback_enabled
                ):
                    return False
                score = candidate.lexical_score
                summary = article.excerpt[:1000] or article.title
                analysis = (
                    "DeepSeek 本次未生成分析；已使用关键词规则初筛。"
                    if api_key
                    else "关键词规则初筛；未配置 DeepSeek API Key，本次未调用 AI。"
                )
                analysis_mode = "rules_fallback" if api_key else "rules"
                model: str | None = None
                ai_result: AIAnalysis | None = None
                semantic_counted = False

                def mark_semantic_review() -> None:
                    nonlocal semantic_counted
                    if semantic_counted or not candidate.semantic:
                        return
                    semantic_counted = True
                    for key in ("semantic_reviewed", "semantic_fallback"):
                        _bump(
                            funnel,
                            key,
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                    if semantic_stage == "additional":
                        for key in ("semantic_additional_reviewed", "semantic_expansion"):
                            _bump(
                                funnel,
                                key,
                                topic_id=work.topic["id"],
                                source_id=article.source_id,
                            )
                    else:
                        _bump(
                            funnel,
                            "semantic_initial_reviewed",
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )

                semantic_config = {
                    "name": work.topic["name"],
                    "keywords": work.topic["keywords"] or [work.topic["name"]],
                    "exclusion_keywords": work.topic.get("exclusion_keywords", []),
                    "scoring_policy_version": 2,
                }
                fingerprint = _analysis_fingerprint(article)
                cached: dict[str, Any] | None = None
                if api_key and hasattr(self.db, "get_ai_cache"):
                    try:
                        cached = self.db.get_ai_cache(
                            fingerprint, semantic_config, DEEPSEEK_MODEL
                        )
                    except Exception:
                        LOGGER.exception("Could not read DeepSeek analysis cache")
                if cached is not None:
                    try:
                        ai_result = AIAnalysis.model_validate(cached)
                    except ValueError:
                        ai_result = None
                    else:
                        mark_semantic_review()
                        _bump(
                            funnel,
                            "ai_candidates",
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                        _bump(
                            funnel,
                            "ai_success",
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                        _bump(
                            funnel,
                            "ai_cache_hits",
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                        ai_success += 1
                        analysis_mode = (
                            "ai_cache_semantic" if candidate.semantic else "ai_cache"
                        )
                        model = DEEPSEEK_MODEL
                if ai_result is None and ai_live and analysis_count < ANALYSIS_BREAKER:
                    mark_semantic_review()
                    _bump(
                        funnel,
                        "ai_candidates",
                        topic_id=work.topic["id"],
                        source_id=article.source_id,
                    )
                    analysis_count += 1
                    before_requests = getattr(ai_client, "http_request_count", None)
                    request_fallback = 1
                    try:
                        raw_result = await ai_client.analyze(article, work.topic)
                        ai_result = AIAnalysis.model_validate(raw_result)
                        if not ai_connection_confirmed:
                            self._record_deepseek_status(
                                "connected",
                                error=None,
                                tested=True,
                            )
                            ai_connection_confirmed = True
                        _bump(
                            funnel,
                            "ai_success",
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                        ai_success += 1
                        analysis_mode = "ai_semantic" if candidate.semantic else "ai"
                        model = DEEPSEEK_MODEL
                        if hasattr(self.db, "put_ai_cache"):
                            try:
                                self.db.put_ai_cache(
                                    fingerprint,
                                    semantic_config,
                                    DEEPSEEK_MODEL,
                                    ai_result.model_dump(mode="json"),
                                    ttl_days=7,
                                )
                            except Exception:
                                LOGGER.exception("Could not write DeepSeek analysis cache")
                    except DeepSeekError as exc:
                        ai_failed = True
                        ai_live = False
                        ai_failure += 1
                        self._record_deepseek_status(
                            "error",
                            error=str(exc),
                            tested=True,
                        )
                        request_fallback = max(1, exc.http_requests)
                        _bump(
                            funnel,
                            "ai_failure",
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                        warning_parts.append(
                            f"{str(exc)[:220]} 本次剩余候选已停止 AI 调用并回退规则初筛。"
                        )
                    finally:
                        after_requests = getattr(ai_client, "http_request_count", None)
                        if isinstance(before_requests, int) and isinstance(after_requests, int):
                            request_count = max(0, after_requests - before_requests)
                        else:
                            request_count = request_fallback
                        _bump(
                            funnel,
                            "ai_requests",
                            request_count,
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                elif ai_result is None and ai_live and analysis_count >= ANALYSIS_BREAKER:
                    breaker_hit = True
                    if not candidate.semantic:
                        analysis = "已达到单次 AI 分析安全上限；使用关键词规则初筛。"

                relevance_reason = "direct_match"
                if ai_result is not None:
                    score = ai_result.score
                    summary = ai_result.summary
                    analysis = ai_result.analysis
                    relevance_reason = bump_reason(
                        work, article, getattr(ai_result, "reason", "direct_match")
                    )
                    if not bool(getattr(ai_result, "relevant", True)):
                        _bump(
                            funnel,
                            "ai_irrelevant",
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                        return semantic_counted
                elif candidate.semantic:
                    # A non-literal candidate is never promoted by a fabricated
                    # lexical score when AI is absent, disabled, or unhealthy.
                    return semantic_counted
                else:
                    bump_reason(work, article, relevance_reason)

                if score < work.supplement_threshold:
                    for key in (
                        "threshold_rejected",
                        "below_supplement",
                        "below_extended",
                    ):
                        _bump(
                            funnel,
                            key,
                            topic_id=work.topic["id"],
                            source_id=article.source_id,
                        )
                    return semantic_counted
                selection_tier = "core" if score >= work.threshold else "supplement"
                work.ranked.append(
                    _Ranked(
                        candidate,
                        score,
                        summary,
                        analysis,
                        analysis_mode,
                        model,
                        selection_tier,
                        relevance_reason,
                    )
                )
                return semantic_counted

            def build_selection_plan(
                *, record_run_duplicates: bool = False
            ) -> tuple[list[tuple[_TopicWork, _Ranked]], dict[str, int]]:
                ranked_by_topic = {
                    work.topic["id"]: sorted(
                        work.ranked,
                        key=lambda item: (
                            -item.score,
                            -_published_rank(item.candidate.article.published_at),
                        ),
                    )
                    for work in topic_work
                }
                positions = {work.topic["id"]: 0 for work in topic_work}
                counts = {work.topic["id"]: 0 for work in topic_work}
                run_index = DuplicateIndex()
                plan: list[tuple[_TopicWork, _Ranked]] = []
                while True:
                    progressed = False
                    for work in topic_work:
                        topic_id = work.topic["id"]
                        if counts[topic_id] >= work.limit:
                            continue
                        ranked = ranked_by_topic[topic_id]
                        position = positions[topic_id]
                        while position < len(ranked):
                            item = ranked[position]
                            position += 1
                            positions[topic_id] = position
                            article = item.candidate.article
                            if run_index.is_duplicate(article.url, article.title):
                                if record_run_duplicates:
                                    _bump(
                                        funnel,
                                        "duplicates_skipped",
                                        topic_id=topic_id,
                                        source_id=article.source_id,
                                    )
                                    _bump(
                                        funnel,
                                        "run_duplicates",
                                        topic_id=topic_id,
                                        source_id=article.source_id,
                                    )
                                continue
                            plan.append((work, item))
                            counts[topic_id] += 1
                            run_index.add(article.url, article.title)
                            progressed = True
                            break
                    if not progressed:
                        break
                return plan, counts

            async def analyze_direct_fairly() -> None:
                while True:
                    progressed = False
                    for work in topic_work:
                        _plan, provisional_counts = build_selection_plan()
                        if provisional_counts[work.topic["id"]] >= work.limit:
                            continue
                        if work.direct_position >= len(work.direct_candidates):
                            continue
                        candidate = work.direct_candidates[work.direct_position]
                        work.direct_position += 1
                        progressed = True
                        prepared = await prepare_candidate(work, candidate)
                        if prepared is not None:
                            await analyze_candidate(work, prepared)
                    if not progressed:
                        return

            async def prepare_unmatched_fairly() -> None:
                """Build the semantic pool after bounded, fair full-text recovery.

                Full-text attempts have their own global cap in
                ``prepare_candidate``.  Reaching that cap does not stop this
                pass: the remaining metadata-only candidates are still added
                to the semantic pool, so the 20+15 AI budget remains usable.
                """

                while True:
                    progressed = False
                    for work in topic_work:
                        _plan, provisional_counts = build_selection_plan()
                        if provisional_counts[work.topic["id"]] >= work.limit:
                            continue
                        if work.semantic_position >= len(work.semantic_candidates):
                            continue
                        candidate = work.semantic_candidates[work.semantic_position]
                        work.semantic_position += 1
                        progressed = True
                        prepared = await prepare_candidate(work, candidate)
                        if prepared is None:
                            continue
                        if prepared.semantic:
                            _bump(
                                funnel,
                                "semantic_candidates",
                                topic_id=work.topic["id"],
                                source_id=prepared.article.source_id,
                            )
                            work.prepared_semantic_candidates.append(prepared)
                        else:
                            await analyze_candidate(work, prepared)
                    if not progressed:
                        return

            async def analyze_semantic_fairly(
                budget: int, *, stage: str
            ) -> int:
                used = 0
                while used < budget:
                    progressed = False
                    for work in topic_work:
                        if used >= budget:
                            break
                        _plan, provisional_counts = build_selection_plan()
                        if provisional_counts[work.topic["id"]] >= work.limit:
                            continue
                        if work.semantic_review_position >= len(
                            work.prepared_semantic_candidates
                        ):
                            continue
                        candidate = work.prepared_semantic_candidates[
                            work.semantic_review_position
                        ]
                        work.semantic_review_position += 1
                        progressed = True
                        reviewed = await analyze_candidate(
                            work,
                            candidate,
                            semantic_stage=stage,
                        )
                        if reviewed:
                            used += 1
                    if not progressed:
                        break
                return used

            self.db.update_run(
                run_id,
                phase="analyzing",
                funnel=funnel,
                duplicates_skipped=funnel["duplicates_skipped"],
            )
            await analyze_direct_fairly()
            semantic_pass_enabled = bool(
                settings.extract_full_text
                or (api_key and settings.semantic_fallback_enabled)
            )
            if semantic_pass_enabled:
                await prepare_unmatched_fairly()
            semantic_initial_used = 0
            semantic_additional_used = 0
            if api_key and settings.semantic_fallback_enabled:
                semantic_initial_used = await analyze_semantic_fairly(
                    semantic_initial_budget, stage="initial"
                )
            _plan, provisional_counts = build_selection_plan()
            still_short = api_key and settings.semantic_fallback_enabled and any(
                provisional_counts[work.topic["id"]] < work.limit
                and work.semantic_review_position
                < len(work.prepared_semantic_candidates)
                for work in topic_work
            )
            if still_short:
                semantic_additional_used = await analyze_semantic_fairly(
                    SEMANTIC_EXPANSION_MAX,
                    stage="additional",
                )

            _plan, provisional_counts = build_selection_plan()
            for work in topic_work:
                remaining = (
                    len(work.prepared_semantic_candidates)
                    - work.semantic_review_position
                )
                budget_was_consumed = (
                    semantic_initial_used + semantic_additional_used
                    >= semantic_initial_budget + SEMANTIC_EXPANSION_MAX
                )
                if (
                    api_key
                    and settings.semantic_fallback_enabled
                    and budget_was_consumed
                    and remaining > 0
                    and provisional_counts[work.topic["id"]] < work.limit
                ):
                    for key in ("semantic_budget_exhausted", "budget_exhausted"):
                        _bump(
                            funnel,
                            key,
                            remaining,
                            topic_id=work.topic["id"],
                        )
                work.ranked.sort(
                    key=lambda item: (
                        -item.score,
                        -_published_rank(item.candidate.article.published_at),
                    )
                )

            selection_plan, _selected_counts = build_selection_plan(
                record_run_duplicates=True
            )
            for work, item in selection_plan:
                topic_id = work.topic["id"]
                article = item.candidate.article
                pending_articles.append(
                    {
                        "run_id": run_id,
                        "topic_id": topic_id,
                        "source_id": article.source_id,
                        "title": article.title,
                        "url": article.url,
                        "excerpt": article.excerpt,
                        "full_text": article.full_text,
                        "summary": item.summary,
                        "analysis": item.analysis,
                        "score": item.score,
                        "published_at": article.published_at,
                        "content_hash": _content_hash(article),
                        "analysis_mode": item.analysis_mode,
                        "model": item.model,
                        "matched_keywords": list(
                            item.candidate.evidence.matched_keywords
                        ),
                        "matched_fields": list(item.candidate.evidence.matched_fields),
                        "selection_tier": item.selection_tier,
                        "relevance_reason": item.relevance_reason,
                    }
                )
                for key in ("selected", "results"):
                    _bump(
                        funnel,
                        key,
                        topic_id=topic_id,
                        source_id=article.source_id,
                    )
                tier_keys = (
                    ("core_selected",)
                    if item.selection_tier == "core"
                    else ("supplement_selected", "extended_selected")
                )
                for key in tier_keys:
                    _bump(
                        funnel,
                        key,
                        topic_id=topic_id,
                        source_id=article.source_id,
                    )

            self.db.update_run(
                run_id,
                phase="analyzing",
                funnel=funnel,
                analyses=analysis_count,
                ai_requests=funnel["ai_requests"],
                ai_success=ai_success,
                ai_failure=ai_failure,
                duplicates_skipped=funnel["duplicates_skipped"],
            )

            if not api_key and any(fetched.values()) and not key_store_failed:
                warning_parts.append(
                    "未配置 DeepSeek API Key，本次为规则模式；没有向 DeepSeek 发送内容。"
                )
            if breaker_hit:
                warning_parts.append(
                    "单次运行已达到 500 次 AI 分析安全上限，剩余候选仅做规则初筛。"
                )
            if funnel["semantic_budget_exhausted"] > 0:
                warning_parts.append(
                    "语义复核已达到本次预算；仍有候选未分析，结果不会通过自动降门槛硬凑。"
                )
            if funnel["fulltext_recovery_budget_exhausted"] > 0:
                warning_parts.append(
                    "全文提取已达到单次 100 次安全上限；其余候选仅使用标题与摘要继续复核。"
                )
            if source_failure:
                warning_parts.append(f"{source_failure} 个新闻源抓取失败。")

            stored_count = 0
            self.db.update_run(run_id, phase="persisting", funnel=funnel)
            # One synchronous SQLite transaction commits the whole buffered
            # selection (including FTS rows), so cancellation or persistence
            # failure cannot expose a half-written report.
            stored_count = len(self.db.insert_articles(pending_articles))

            if stored_count > 0:
                outcome = "complete"
            elif funnel["feed_items"] == 0 or funnel["fresh"] == 0:
                outcome = "no_fresh_articles"
            elif (
                funnel["keyword_hits"] > 0
                and funnel["ai_success"] > 0
                and funnel["ai_irrelevant"] == funnel["ai_success"]
                and funnel["threshold_rejected"] == 0
            ):
                outcome = "no_relevant_after_ai"
            elif funnel["keyword_hits"] == 0 and funnel["semantic_fallback"] == 0:
                outcome = "no_keyword_match"
            elif post_dedup_candidates == 0 and funnel["duplicates_skipped"] > 0:
                outcome = "no_new_after_dedup"
            elif funnel["threshold_rejected"] > 0:
                outcome = "below_threshold"
            elif funnel["duplicates_skipped"] > 0:
                outcome = "no_new_after_dedup"
            else:
                outcome = "no_keyword_match"

            if outcome == "no_fresh_articles":
                window = f"最近 {settings.freshness_days} 天" if settings.freshness_days else "当前"
                warning_parts.append(f"{window}没有可用文章；请检查来源日期或调整新鲜度。")
            elif outcome == "no_keyword_match":
                warning_parts.append("近期文章未命中主题关键词，语义兜底也未产生可展示结果。")
            elif outcome == "no_relevant_after_ai":
                warning_parts.append(
                    "关键词候选已找到，但通过 AI 相关性复核后没有可展示结果；"
                    "可检查主题范围、关键词和排除词。"
                )
            elif outcome == "below_threshold":
                warning_parts.append("候选文章存在，但均低于当前门槛分数。")
            elif outcome == "no_new_after_dedup":
                warning_parts.append(
                    f"近 7 天内无新增；已跳过 {funnel['duplicates_skipped']} 条重复候选。"
                )
            if stored_count == 0 and previous_good:
                warning_parts.append("本次没有新的可展示文章，已保留上一次完整报告。")

            # Rules-only is a supported mode. Only operational failures make a
            # usable result degraded.
            degraded = bool(source_failure or ai_failed or breaker_hit or key_store_failed)
            status = "degraded" if degraded else "complete"
            finished = self.db.update_run(
                run_id,
                status=status,
                phase="finished",
                outcome=outcome,
                finished_at=utc_now(),
                article_count=stored_count,
                source_success=source_success,
                source_failure=source_failure,
                analyses=analysis_count,
                duplicates_skipped=funnel["duplicates_skipped"],
                funnel=funnel,
                ai_requests=funnel["ai_requests"],
                ai_success=ai_success,
                ai_failure=ai_failure,
                warning=" ".join(dict.fromkeys(warning_parts)) or None,
            )
            self._write_artifact(finished, source_errors)
            if status == "complete" and stored_count > 0:
                # Publish only after the immutable report exists. A force quit
                # between DB finalization and artifact replacement must leave
                # the previous known-good report selected.
                try:
                    self.db.set_latest_good_run(run_id)
                except Exception:
                    # The completed artifact remains available in history (and
                    # as the first-run fallback). Promotion can safely fail
                    # without corrupting or demoting the run itself.
                    LOGGER.exception("Could not promote completed run %s", run_id)
        except asyncio.CancelledError:
            # Selections are buffered until the final synchronous commit, so
            # there are no run articles to expose or promote here.
            for key in (
                "selected",
                "results",
                "core_selected",
                "supplement_selected",
                "extended_selected",
            ):
                funnel[key] = 0
            for values in funnel.get("per_topic", {}).values():
                for key in (
                    "selected",
                    "results",
                    "core_selected",
                    "supplement_selected",
                    "extended_selected",
                ):
                    values[key] = 0
            for values in funnel.get("per_source", {}).values():
                for key in (
                    "selected",
                    "results",
                    "core_selected",
                    "supplement_selected",
                    "extended_selected",
                ):
                    values[key] = 0
            cancelled = self.db.update_run(
                run_id,
                status="cancelled",
                phase="finished",
                outcome="cancelled",
                finished_at=utc_now(),
                article_count=0,
                source_success=source_success,
                source_failure=source_failure,
                analyses=analysis_count,
                duplicates_skipped=funnel.get("duplicates_skipped", 0),
                funnel=funnel,
                ai_requests=funnel.get("ai_requests", 0),
                ai_success=ai_success,
                ai_failure=ai_failure,
                warning="任务已取消；未生成或替换新闻报告。",
                error=None,
            )
            try:
                self._write_artifact(cancelled, source_errors)
            except Exception:
                LOGGER.exception("Could not persist cancellation artifact for run %s", run_id)
        except Exception as exc:
            failed = self.db.update_run(
                run_id,
                status="failed",
                phase="finished",
                outcome="source_failure" if source_success == 0 else "error",
                finished_at=utc_now(),
                article_count=len(self.db.list_run_articles(run_id)),
                source_success=source_success,
                source_failure=source_failure,
                analyses=analysis_count,
                duplicates_skipped=funnel.get("duplicates_skipped", 0),
                funnel=funnel,
                ai_requests=funnel.get("ai_requests", 0),
                ai_success=ai_success,
                ai_failure=ai_failure,
                error=str(exc)[:1000],
            )
            try:
                self._write_artifact(failed, source_errors)
            except Exception:
                LOGGER.exception("Could not persist failure artifact for run %s", run_id)
        finally:
            # Retention is a privacy boundary, not a success-path nicety. Run
            # it after complete, degraded, and failed executions alike so a
            # prolonged upstream outage cannot retain extracted text forever.
            try:
                self.db.purge_expired_full_text(30)
            except Exception:
                LOGGER.exception("Full-text retention cleanup failed after run %s", run_id)
            try:
                if hasattr(self.db, "purge_ai_cache"):
                    self.db.purge_ai_cache()
            except Exception:
                LOGGER.exception("DeepSeek cache cleanup failed after run %s", run_id)

    def _write_artifact(self, run: dict[str, Any], source_errors: list[dict[str, str]]) -> Path:
        articles = self.db.list_run_articles(run["id"])
        # Full extracted text is a 30-day cache and is deliberately omitted
        # from persistent report artifacts.
        clean_articles = [
            {key: value for key, value in article.items() if key != "full_text"}
            for article in articles
        ]
        payload = {
            "schema_version": (
                self.db.get_schema_version() if hasattr(self.db, "get_schema_version") else 2
            ),
            "run": run,
            "source_errors": source_errors,
            "articles": clean_articles,
        }
        destination = _artifact_path_for_run(self.paths.runs, str(run["id"]))
        if destination.exists():
            raise FileExistsError(f"运行报告已经存在，拒绝覆盖：{destination.name}")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.stem}-", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary).replace(destination)
        finally:
            temporary_path = Path(temporary)
            if temporary_path.exists():
                temporary_path.unlink()
        return destination

    def source_errors(self, run: dict[str, Any] | None) -> list[dict[str, str]]:
        if not run:
            return []
        funnel = run.get("funnel")
        funnel = funnel if isinstance(funnel, dict) else {}
        snapshots = funnel.get("per_source")
        snapshots = snapshots if isinstance(snapshots, dict) else {}
        errors = funnel.get("source_errors")
        if errors is None:
            try:
                path = _artifact_path_for_run(self.paths.runs, str(run["id"]))
                payload = json.loads(path.read_text(encoding="utf-8"))
                errors = payload.get("source_errors", []) if isinstance(payload, dict) else []
                artifact_run = payload.get("run") if isinstance(payload, dict) else None
                artifact_funnel = (
                    artifact_run.get("funnel") if isinstance(artifact_run, dict) else None
                )
                artifact_sources = (
                    artifact_funnel.get("per_source")
                    if isinstance(artifact_funnel, dict) else None
                )
                if not snapshots and isinstance(artifact_sources, dict):
                    snapshots = artifact_sources
            except (OSError, ValueError, KeyError):
                errors = []
        if not isinstance(errors, list):
            return []
        result = []
        for item in errors:
            if not isinstance(item, dict) or not item.get("error"):
                continue
            source_id = str(item.get("source_id", ""))
            error = {"source_id": source_id, "error": str(item["error"])[:500]}
            snapshot = snapshots.get(source_id)
            name = snapshot.get("name") if isinstance(snapshot, dict) else None
            if isinstance(name, str) and name.strip():
                # Imports can remap source IDs, while frozen statistics retain
                # the original IDs and labels. Keep the historical identity
                # understandable without rewriting those stored snapshots.
                error["source_name"] = name.strip()[:200]
            result.append(error)
        return result

    def latest_report(self) -> dict[str, Any] | None:
        checkpoint = self.db.get_latest_good_run_id()
        candidates = self.db.list_usable_run_ids()
        run_id: str | None = None
        committed: dict[str, Any] | None = None
        for candidate_id in candidates:
            # A process may stop after finalizing its DB rows but before the
            # atomic report-file write. Skip that candidate rather than hiding
            # an earlier committed degraded report behind the old checkpoint.
            try:
                artifact = _artifact_path_for_run(self.paths.runs, candidate_id)
                payload = json.loads(artifact.read_text(encoding="utf-8"))
                if (
                    isinstance(payload, dict)
                    and isinstance(payload.get("run"), dict)
                    and payload["run"].get("id") == candidate_id
                    and payload["run"].get("status") in ("complete", "degraded")
                    and isinstance(payload.get("articles"), list)
                    and isinstance(payload.get("source_errors"), list)
                ):
                    run_id, committed = candidate_id, payload
                    break
            except (OSError, ValueError):
                pass
            if candidate_id == checkpoint:
                # A confirmed complete checkpoint can still be reconstructed
                # from its DB rows if its old report file was lost or damaged.
                run_id = checkpoint
                break
        if not run_id:
            run_id = checkpoint or (candidates[0] if candidates else None)
        if not run_id:
            return None

        run = self.db.get_run(run_id)
        if run["status"] == "degraded":
            notice = "当前展示部分完成的可用新结果；失败来源或 AI 降级情况见对应任务。"
            run["warning"] = " ".join(filter(None, [run.get("warning"), notice]))
        if not checkpoint and (committed is None or run["status"] == "degraded"):
            # Preserve database-only legacy/import recovery for the first
            # usable result without describing it as a confirmed full report.
            run["status"] = "degraded"
            notice = "当前显示首次可用的降级结果；尚无完整报告。"
            run["warning"] = " ".join(filter(None, [run.get("warning"), notice]))

        # Favorites are mutable user metadata. Rebuild the public report from
        # current rows without rewriting its immutable commitment artifact.
        return {
            "schema_version": (
                self.db.get_schema_version() if hasattr(self.db, "get_schema_version") else 2
            ),
            "run": run,
            "source_errors": self.source_errors(run),
            "articles": [
                {key: value for key, value in article.items() if key != "full_text"}
                for article in self.db.list_run_articles(run_id)
            ],
        }
