from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .dedup import DuplicateIndex, canonical_url, normalize_title
from .keystore import DeepSeekKeyStore
from .models import FeedArticle, RunRequest
from .runtime import AppPaths
from .services import DeepSeekClient, DeepSeekError, FeedService
from .storage import Database, NotFoundError, utc_now

ANALYSIS_BREAKER = 500
LOGGER = logging.getLogger(__name__)


def _content_hash(article: FeedArticle) -> str:
    identity = f"{canonical_url(article.url)}\n{normalize_title(article.title)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _keyword_score(article: FeedArticle, topic: dict[str, Any]) -> tuple[bool, int]:
    keywords = topic["keywords"] or [topic["name"]]
    title = article.title.casefold()
    body = f"{article.title} {article.excerpt}".casefold()
    hits = 0
    title_hits = 0
    for keyword in keywords:
        normalized = keyword.casefold().strip()
        if normalized and normalized in body:
            hits += 1
            if normalized in title:
                title_hits += 1
    if hits == 0:
        return False, 0
    return True, min(95, 58 + hits * 7 + title_hits * 8)


def _published_rank(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return 0.0


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
        self.db.update_run(run_id, status="running")
        source_success = 0
        source_failure = 0
        analysis_count = 0
        duplicates_skipped = 0
        nonduplicate_candidates_reviewed = 0
        warning_parts: list[str] = []
        source_errors: list[dict[str, str]] = []
        previous_good: str | None = None
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
            sources = [
                all_sources[source_id] for source_id in selected_ids if source_id in all_sources
            ]
            if not sources:
                raise ValueError("没有可用于所选主题的已启用新闻源")

            fetched: dict[str, list[FeedArticle]] = {}
            for source in sources:
                try:
                    items = await self.feed_service.fetch(
                        source,
                        extract_full_text=settings.extract_full_text,
                    )
                    fetched[source["id"]] = items
                    source_success += 1
                    self.db.set_source_health(source["id"], True)
                except Exception as exc:
                    source_failure += 1
                    message = str(exc)[:500]
                    source_errors.append({"source_id": source["id"], "error": message})
                    self.db.set_source_health(source["id"], False, message)

            if source_success == 0:
                raise RuntimeError("所有新闻源均抓取失败")

            api_key = self.key_store.get()
            ai = self.deepseek_factory(api_key) if api_key else None
            ai_failed = False
            breaker_hit = False
            stored_count = 0

            for topic in topics:
                threshold = (
                    topic["threshold"]
                    if topic["threshold"] is not None
                    else settings.default_threshold
                )
                limit = (
                    run_article_limit or topic["article_limit"] or settings.default_article_limit
                )
                topic_sources = set(topic["source_ids"] or fetched.keys())
                candidates: list[tuple[FeedArticle, int]] = []
                seen: set[str] = set()
                for source_id in topic_sources:
                    for article in fetched.get(source_id, []):
                        digest = _content_hash(article)
                        if digest in seen:
                            duplicates_skipped += 1
                            continue
                        matched, lexical_score = _keyword_score(article, topic)
                        if matched:
                            seen.add(digest)
                            candidates.append((article, lexical_score))

                # Cluster within this topic before spending AI calls. This
                # temporary index is discarded after the topic: candidates
                # that later miss the threshold cannot suppress another topic.
                candidates.sort(
                    key=lambda item: (
                        -item[1],
                        -len(item[0].full_text or item[0].excerpt),
                        -_published_rank(item[0].published_at),
                        canonical_url(item[0].url),
                    )
                )
                topic_candidates = DuplicateIndex()
                unique_candidates: list[tuple[FeedArticle, int]] = []
                for article, lexical_score in candidates:
                    if duplicate_index.is_duplicate(article.url, article.title):
                        duplicates_skipped += 1
                        continue
                    if topic_candidates.is_duplicate(article.url, article.title):
                        duplicates_skipped += 1
                        continue
                    topic_candidates.add(article.url, article.title)
                    unique_candidates.append((article, lexical_score))
                nonduplicate_candidates_reviewed += len(unique_candidates)

                ranked: list[tuple[FeedArticle, int, str, str]] = []
                for article, lexical_score in unique_candidates:
                    score = lexical_score
                    summary = article.excerpt[:1000] or article.title
                    analysis = (
                        "DeepSeek 本次未生成分析；已使用关键词规则初筛。"
                        if api_key
                        else "关键词规则初筛；配置 DeepSeek API Key 后可获得 AI 分析。"
                    )
                    if ai and analysis_count < ANALYSIS_BREAKER:
                        analysis_count += 1
                        try:
                            result = await ai.analyze(article, topic)
                            score = result.score
                            summary = result.summary
                            analysis = result.analysis
                        except DeepSeekError as exc:
                            ai_failed = True
                            # DeepSeekClient has already retried once. Stop the
                            # rest of this run so an invalid key or outage
                            # cannot multiply into hundreds of slow requests.
                            ai = None
                            warning_parts.append(
                                f"{str(exc)[:220]} 本次剩余候选已停止 AI 调用并回退规则初筛。"
                            )
                    elif ai and analysis_count >= ANALYSIS_BREAKER:
                        breaker_hit = True
                        analysis = "已达到单次 AI 分析安全上限；使用关键词规则初筛。"
                    if score >= threshold:
                        ranked.append((article, score, summary, analysis))

                ranked.sort(key=lambda item: item[1], reverse=True)
                topic_stored = 0
                for article, score, summary, analysis in ranked:
                    if topic_stored >= limit:
                        break
                    # Earlier candidates in this topic may only have entered
                    # the index after ranking. Recheck to catch them without
                    # sacrificing a lower-ranked unique replacement.
                    if duplicate_index.is_duplicate(article.url, article.title):
                        duplicates_skipped += 1
                        continue
                    self.db.insert_article(
                        {
                            "run_id": run_id,
                            "topic_id": topic["id"],
                            "source_id": article.source_id,
                            "title": article.title,
                            "url": article.url,
                            "excerpt": article.excerpt,
                            "full_text": article.full_text,
                            "summary": summary,
                            "analysis": analysis,
                            "score": score,
                            "published_at": article.published_at,
                            "content_hash": _content_hash(article),
                        }
                    )
                    stored_count += 1
                    topic_stored += 1
                    duplicate_index.add(article.url, article.title)

            if not api_key and any(fetched.values()):
                warning_parts.append("未配置 DeepSeek API Key，本次仅使用关键词规则初筛。")
            if breaker_hit:
                warning_parts.append(
                    "单次运行已达到 500 次 AI 分析安全上限，剩余候选仅做规则初筛。"
                )
            if source_failure:
                warning_parts.append(f"{source_failure} 个新闻源抓取失败。")

            # A rules-only run is intentionally usable on a fresh install. It
            # carries a warning, but source or AI failures remain degraded.
            degraded = bool(source_failure or ai_failed or breaker_hit)
            duplicate_only = (
                stored_count == 0
                and duplicates_skipped > 0
                and nonduplicate_candidates_reviewed == 0
            )
            if duplicate_only and not degraded:
                warning_parts.append(f"近 7 天内无新增；已跳过 {duplicates_skipped} 条重复候选。")
            if stored_count == 0 and previous_good and not duplicate_only:
                degraded = True
                warning_parts.append("本次没有达标文章，已保留上一次完整报告。")
            status = "degraded" if degraded else "complete"
            finished = self.db.update_run(
                run_id,
                status=status,
                finished_at=utc_now(),
                article_count=stored_count,
                source_success=source_success,
                source_failure=source_failure,
                analyses=analysis_count,
                duplicates_skipped=duplicates_skipped,
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
        except Exception as exc:
            failed = self.db.update_run(
                run_id,
                status="failed",
                finished_at=utc_now(),
                article_count=len(self.db.list_run_articles(run_id)),
                source_success=source_success,
                source_failure=source_failure,
                analyses=analysis_count,
                duplicates_skipped=duplicates_skipped,
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

    def _write_artifact(self, run: dict[str, Any], source_errors: list[dict[str, str]]) -> Path:
        articles = self.db.list_run_articles(run["id"])
        # Full extracted text is a 30-day cache and is deliberately omitted
        # from persistent report artifacts.
        clean_articles = [
            {key: value for key, value in article.items() if key != "full_text"}
            for article in articles
        ]
        payload = {
            "schema_version": 1,
            "run": run,
            "source_errors": source_errors,
            "articles": clean_articles,
        }
        destination = self.paths.runs / f"{run['id']}.json"
        if destination.exists():
            raise FileExistsError(f"运行报告已经存在，拒绝覆盖：{destination.name}")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{run['id']}-", suffix=".tmp", dir=self.paths.runs
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

    def latest_report(self) -> dict[str, Any] | None:
        run_id = self.db.get_latest_good_run_id()
        fallback = False
        if not run_id:
            run_id = self.db.get_newest_usable_run_id()
            fallback = bool(run_id)
        if not run_id:
            return None

        def report_run() -> dict[str, Any]:
            run = self.db.get_run(run_id)
            if fallback:
                run = dict(run)
                run["status"] = "degraded"
                notice = "当前显示首次可用的降级结果；尚无完整报告。"
                run["warning"] = " ".join(filter(None, [run.get("warning"), notice]))
            return run

        path = self.paths.runs / f"{run_id}.json"
        if path.exists():
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                # Favorites are mutable user metadata. Overlay current rows on
                # the immutable raw run artifact without rewriting that file.
                report["run"] = report_run()
                report["articles"] = [
                    {key: value for key, value in article.items() if key != "full_text"}
                    for article in self.db.list_run_articles(run_id)
                ]
                return report
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "schema_version": 1,
            "run": report_run(),
            "source_errors": [],
            "articles": [
                {key: value for key, value in article.items() if key != "full_text"}
                for article in self.db.list_run_articles(run_id)
            ],
        }
