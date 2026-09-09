from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ._version import __version__
from .backup import (
    MAX_ARCHIVE_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    BackupBundle,
    BackupValidationError,
    read_backup,
)
from .keystore import DeepSeekKeyStore, KeyStoreError
from .models import (
    CalibrationConfirm,
    CalibrationRequest,
    DeepSeekKeyInput,
    DeepSeekTestInput,
    FavoriteUpdate,
    ImportApplyInput,
    RunRequest,
    SettingsUpdate,
    SourceInput,
    SourcePatch,
    TopicInput,
    TopicPatch,
    TopicSuggestionInput,
)
from .pipeline import RunConflictError, RunManager
from .runtime import AppPaths, resolve_paths
from .services import DEEPSEEK_MODEL, DeepSeekError, FeedService, UnsafeUrlError
from .storage import SCHEMA_VERSION, Database, NotFoundError, TopicLimitError, utc_now

LOGGER = logging.getLogger(__name__)
IMPORT_PREVIEW_TTL_SECONDS = 15 * 60
BACKUP_PART_BYTES = 4 * 1024 * 1024


def _deepseek_snapshot(db: Database, key_store: DeepSeekKeyStore) -> dict[str, Any]:
    settings = db.get_settings()
    try:
        configured = bool(key_store.get())
    except KeyStoreError as exc:
        return {
            "configured": False,
            "status": "error",
            "model": DEEPSEEK_MODEL,
            "last_tested_at": settings.deepseek_last_tested_at,
            "error": str(exc),
        }
    status = settings.deepseek_status if configured else "unconfigured"
    if configured and status == "unconfigured":
        # An environment-provided key, or a key restored outside the app, is
        # configured but has not yet been verified by this database.
        status = "saved_unverified"
    return {
        "configured": configured,
        "status": status,
        "model": DEEPSEEK_MODEL,
        "last_tested_at": settings.deepseek_last_tested_at,
        "error": settings.deepseek_last_error if status == "error" else None,
    }


def _write_json_parts(
    archive: zipfile.ZipFile,
    *,
    index_name: str,
    folder: str,
    field: str,
    rows: list[dict[str, Any]],
) -> int:
    parts: list[str] = []
    current: list[bytes] = []
    current_size = len(field.encode("utf-8")) + 16
    raw_size = 0

    def flush() -> None:
        nonlocal current, current_size, raw_size
        if not current:
            return
        name = f"{folder}/part-{len(parts) + 1:05d}.json"
        payload = b'{"' + field.encode("utf-8") + b'":[' + b",".join(current) + b"]}"
        archive.writestr(name, payload)
        parts.append(name)
        raw_size += len(payload)
        current = []
        current_size = len(field.encode("utf-8")) + 16

    for row in rows:
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if current and current_size + len(encoded) + 1 > BACKUP_PART_BYTES:
            flush()
        current.append(encoded)
        current_size += len(encoded) + 1
    flush()
    index = json.dumps(
        {"count": len(rows), "parts": parts},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    archive.writestr(index_name, index)
    return raw_size + len(index)


def _export_bytes(state: CockpitState) -> bytes:
    configuration = {
        "schema_version": SCHEMA_VERSION,
        "settings": state.db.get_settings().model_dump(),
        "topics": state.db.list_topics(include_archived=True),
        "sources": state.db.list_sources(include_archived=True),
        "deepseek_api_key_included": False,
    }
    history_rows = [
        {key: value for key, value in article.items() if key != "full_text"}
        for article in state.db.export_articles()
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "product": "Cookies News Cockpit",
        "app_version": __version__,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "deepseek_api_key_included": False,
    }
    output = io.BytesIO()
    raw_size = 0
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest_payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        configuration_payload = json.dumps(
            configuration, ensure_ascii=False, indent=2
        ).encode("utf-8")
        archive.writestr("manifest.json", manifest_payload)
        archive.writestr("configuration.json", configuration_payload)
        raw_size += len(manifest_payload) + len(configuration_payload)
        raw_size += _write_json_parts(
            archive,
            index_name="history.json",
            folder="history",
            field="articles",
            rows=history_rows,
        )
        raw_size += _write_json_parts(
            archive,
            index_name="runs.json",
            folder="runs",
            field="runs",
            rows=state.db.export_runs(),
        )
    payload = output.getvalue()
    if raw_size > MAX_UNCOMPRESSED_BYTES or len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("完整备份超过安全上限，请先减少长期历史记录后再导出")
    return payload


def _write_automatic_backup(state: CockpitState, payload: bytes) -> Path:
    destination_dir = state.paths.root / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = destination_dir / f"before-import-{stamp}-{secrets.token_hex(3)}.zip"
    descriptor, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".tmp", dir=destination_dir)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(destination)
    finally:
        temporary_path = Path(temporary)
        if temporary_path.exists():
            temporary_path.unlink()
    return destination


class CockpitState:
    def __init__(
        self,
        paths: AppPaths,
        db: Database,
        manager: RunManager,
        key_store: DeepSeekKeyStore,
        token: str,
    ):
        self.paths = paths
        self.db = db
        self.manager = manager
        self.key_store = key_store
        self.token = token
        self.last_heartbeat = time.monotonic()
        self.last_scheduled_run = time.monotonic()
        self.shutdown_event = asyncio.Event()
        self.scheduler_task: asyncio.Task[None] | None = None
        self.pending_imports: dict[str, tuple[float, BackupBundle]] = {}

    def remember_import(self, bundle: BackupBundle) -> str:
        now = time.monotonic()
        # Only the newest preview can be applied. Reports have already yielded
        # any v1 run metadata and are not needed during merge, so do not retain
        # their duplicate article bodies in memory on an 8 GB target Mac.
        self.pending_imports.clear()
        bundle = BackupBundle(
            bundle.schema_version,
            bundle.configuration,
            bundle.articles,
            bundle.runs,
            {},
        )
        import_id = secrets.token_urlsafe(18)
        self.pending_imports[import_id] = (now, bundle)
        return import_id

    def get_import(self, import_id: str) -> BackupBundle:
        value = self.pending_imports.get(import_id)
        if value is None or time.monotonic() - value[0] >= IMPORT_PREVIEW_TTL_SECONDS:
            self.pending_imports.pop(import_id, None)
            raise NotFoundError("导入预览已过期，请重新选择备份")
        return value[1]

    async def scheduler(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                settings = self.db.get_settings()
                now = time.monotonic()
                tab_active = now - self.last_heartbeat < 90
                due = now - self.last_scheduled_run >= settings.refresh_minutes * 60
                if settings.scheduler_enabled and tab_active and due and not self.manager.active:
                    try:
                        await self.manager.start(RunRequest(), trigger="scheduler")
                        self.last_scheduled_run = now
                    except (ValueError, RunConflictError):
                        self.last_scheduled_run = now
            except Exception:
                # Scheduling must never take down the local API.
                pass
            try:
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=15)
            except TimeoutError:
                continue


def create_app(
    *,
    paths: AppPaths | None = None,
    token: str | None = None,
    key_store: DeepSeekKeyStore | None = None,
    feed_service: FeedService | None = None,
    deepseek_factory: Any | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    paths = paths or resolve_paths()
    db = Database(paths.database)
    db.recover_interrupted_runs()
    # Enforce the 30-day full-text cache boundary even when the user only
    # reopens the app and does not start another successful refresh.
    try:
        db.purge_expired_full_text(30)
    except Exception:
        # A cleanup problem must not make the local cockpit unlaunchable. It is
        # logged and retried after every subsequent run.
        LOGGER.exception("Full-text retention cleanup failed during startup")
    key_store = key_store or DeepSeekKeyStore()
    token = token or secrets.token_urlsafe(32)
    manager = RunManager(
        db,
        paths,
        key_store=key_store,
        feed_service=feed_service,
        deepseek_factory=deepseek_factory,
    )
    state = CockpitState(paths, db, manager, key_store, token)
    hosts = allowed_hosts or {"127.0.0.1", "localhost", "[::1]"}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state.scheduler_task = asyncio.create_task(state.scheduler(), name="cockpit-scheduler")
        yield
        state.shutdown_event.set()
        if state.scheduler_task:
            state.scheduler_task.cancel()
            await asyncio.gather(state.scheduler_task, return_exceptions=True)

    app = FastAPI(
        title="Cookies News Cockpit",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.cockpit = state

    @app.middleware("http")
    async def local_session_guard(request: Request, call_next):
        def secure(response):
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self'; font-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            )
            if request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        host = request.headers.get("host", "").rsplit(":", 1)[0].casefold()
        if host not in hosts:
            return secure(JSONResponse({"detail": "Host 不被允许"}, status_code=400))
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("x-cockpit-token") or request.query_params.get("token")
            if not supplied or not secrets.compare_digest(supplied, state.token):
                return secure(
                    JSONResponse({"detail": "本地会话已失效，请重新打开应用"}, status_code=401)
                )
        return secure(await call_next(request))

    @app.exception_handler(NotFoundError)
    async def not_found(_request: Request, exc: NotFoundError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(TopicLimitError)
    async def topic_limit(_request: Request, exc: TopicLimitError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RunConflictError)
    async def run_conflict(_request: Request, exc: RunConflictError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(UnsafeUrlError)
    async def unsafe_url(_request: Request, exc: UnsafeUrlError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyStoreError)
    async def key_store_error(_request: Request, exc: KeyStoreError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(DeepSeekError)
    async def deepseek_error(_request: Request, exc: DeepSeekError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(ValueError)
    async def invalid_operation(_request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def duplicate(_request: Request, _exc: sqlite3.IntegrityError):
        return JSONResponse({"detail": "名称或地址已经存在"}, status_code=409)

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        """Initial frontend contract.

        Shape: {product, settings, deepseek, topics, sources, current_run,
        latest_report}. ``latest_report`` is either null or the canonical
        {schema_version, run, source_errors, articles} report object.
        """

        return {
            "product": {
                "name": "Cookies News Cockpit",
                "version": __version__,
                "mode": "local",
                "schema_version": SCHEMA_VERSION,
                "migration_warning": state.db.metadata_warning(),
            },
            "settings": state.db.get_settings().model_dump(),
            "deepseek": _deepseek_snapshot(state.db, state.key_store),
            "topics": state.db.list_topics(),
            "sources": state.db.list_sources(),
            "current_run": state.db.get_current_run(),
            "latest_report": state.manager.latest_report(),
        }

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "settings": state.db.get_settings().model_dump(),
            "deepseek": _deepseek_snapshot(state.db, state.key_store),
        }

    @app.put("/api/settings")
    async def put_settings(value: SettingsUpdate) -> dict[str, Any]:
        return {"settings": state.db.update_settings(value).model_dump()}

    @app.put("/api/settings/deepseek-key")
    async def put_deepseek_key(value: DeepSeekKeyInput) -> dict[str, Any]:
        state.key_store.set(value.api_key)
        state.db.update_settings(
            SettingsUpdate(
                deepseek_status="saved_unverified",
                deepseek_last_tested_at=None,
                deepseek_last_error=None,
            )
        )
        return _deepseek_snapshot(state.db, state.key_store)

    @app.delete("/api/settings/deepseek-key")
    async def delete_deepseek_key() -> dict[str, Any]:
        state.key_store.delete()
        state.db.update_settings(
            SettingsUpdate(
                deepseek_status="unconfigured",
                deepseek_last_tested_at=None,
                deepseek_last_error=None,
            )
        )
        return _deepseek_snapshot(state.db, state.key_store)

    @app.post("/api/settings/deepseek/test")
    async def test_deepseek(value: DeepSeekTestInput | None = None) -> dict[str, Any]:
        supplied_key = value.api_key if value else None
        try:
            api_key = supplied_key or state.key_store.get()
        except KeyStoreError as exc:
            state.db.update_settings(
                SettingsUpdate(deepseek_status="error", deepseek_last_error=str(exc))
            )
            snapshot = _deepseek_snapshot(state.db, state.key_store)
            snapshot["http_requests"] = 0
            return snapshot
        tested_at = utc_now()
        if not api_key:
            message = "请先输入或保存 DeepSeek API Key"
            state.db.update_settings(
                SettingsUpdate(
                    deepseek_status="unconfigured",
                    deepseek_last_tested_at=tested_at,
                    deepseek_last_error=message,
                )
            )
            return {
                "status": "error",
                "configured": False,
                "model": DEEPSEEK_MODEL,
                "last_tested_at": tested_at,
                "error": message,
                "http_requests": 0,
            }
        ai = state.manager.deepseek_factory(api_key)
        try:
            result = await ai.test_connection()
        except DeepSeekError as exc:
            message = str(exc)[:500]
            if supplied_key:
                # A typed key is only persisted after a successful test. Its
                # failure must not overwrite the health of a different key
                # already stored in the OS credential vault.
                persisted = _deepseek_snapshot(state.db, state.key_store)
                return {
                    "status": "error",
                    "configured": persisted["configured"],
                    "persisted_status": persisted["status"],
                    "model": DEEPSEEK_MODEL,
                    "last_tested_at": tested_at,
                    "error": message,
                    "http_requests": exc.http_requests,
                    "tested_temporary": True,
                }
            state.db.update_settings(
                SettingsUpdate(
                    deepseek_status="error",
                    deepseek_last_tested_at=tested_at,
                    deepseek_last_error=message,
                )
            )
            snapshot = _deepseek_snapshot(state.db, state.key_store)
            snapshot["http_requests"] = exc.http_requests
            return snapshot
        if supplied_key:
            state.key_store.set(supplied_key)
        state.db.update_settings(
            SettingsUpdate(
                deepseek_status="connected",
                deepseek_last_tested_at=tested_at,
                deepseek_last_error=None,
            )
        )
        return {
            "status": "connected",
            "configured": True,
            "model": DEEPSEEK_MODEL,
            "last_tested_at": tested_at,
            "http_requests": result.get("http_requests", 1),
        }

    @app.get("/api/topics")
    async def list_topics() -> dict[str, Any]:
        return {"topics": state.db.list_topics()}

    @app.post("/api/topics", status_code=201)
    async def create_topic(value: TopicInput) -> dict[str, Any]:
        return {"topic": state.db.create_topic(value)}

    @app.put("/api/topics/{topic_id}")
    async def update_topic(topic_id: str, value: TopicPatch) -> dict[str, Any]:
        return {"topic": state.db.update_topic(topic_id, value)}

    @app.delete("/api/topics/{topic_id}")
    async def delete_topic(topic_id: str) -> dict[str, bool]:
        state.db.delete_topic(topic_id)
        return {"archived": True}

    @app.post("/api/topics/{topic_id}/restore")
    async def restore_topic(topic_id: str) -> dict[str, Any]:
        return {"topic": state.db.restore_topic(topic_id)}

    @app.post("/api/topics/suggest")
    async def suggest_topic(value: TopicSuggestionInput) -> dict[str, Any]:
        api_key = state.key_store.get()
        if not api_key:
            raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key")
        ai = state.manager.deepseek_factory(api_key)
        topic = {
            "name": value.name,
            "keywords": value.keywords,
            "exclusion_keywords": value.exclusion_keywords,
        }
        try:
            proposal = await ai.calibrate(topic, value.goal, [], [])
        except DeepSeekError as exc:
            state.db.update_settings(
                SettingsUpdate(
                    deepseek_status="error",
                    deepseek_last_tested_at=utc_now(),
                    deepseek_last_error=str(exc)[:500],
                )
            )
            raise
        state.db.update_settings(
            SettingsUpdate(
                deepseek_status="connected",
                deepseek_last_tested_at=utc_now(),
                deepseek_last_error=None,
            )
        )
        return {
            "suggestion": {
                "keywords": proposal.keywords,
                "exclusion_keywords": value.exclusion_keywords,
                "threshold": proposal.threshold,
                "rationale": proposal.rationale,
            }
        }

    @app.post("/api/topics/{topic_id}/calibrate")
    async def calibrate_topic(topic_id: str, value: CalibrationRequest) -> dict[str, Any]:
        topic = state.db.get_topic(topic_id)
        api_key = state.key_store.get()
        if not api_key:
            raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key")
        ai = state.manager.deepseek_factory(api_key)
        tested_at = utc_now()
        try:
            proposal = await ai.calibrate(
                topic,
                value.goal,
                value.positive_examples,
                value.negative_examples,
            )
        except DeepSeekError as exc:
            state.db.update_settings(
                SettingsUpdate(
                    deepseek_status="error",
                    deepseek_last_tested_at=tested_at,
                    deepseek_last_error=str(exc)[:500],
                )
            )
            raise
        state.db.update_settings(
            SettingsUpdate(
                deepseek_status="connected",
                deepseek_last_tested_at=tested_at,
                deepseek_last_error=None,
            )
        )
        return {"calibration": state.db.save_calibration(topic_id, proposal.model_dump())}

    @app.post("/api/topics/{topic_id}/calibrate/confirm")
    async def confirm_calibration(topic_id: str, value: CalibrationConfirm) -> dict[str, Any]:
        proposal = state.db.confirm_calibration(value.calibration_id, topic_id)
        keywords = value.keywords if value.keywords is not None else proposal["keywords"]
        threshold = value.threshold if value.threshold is not None else proposal["threshold"]
        topic = state.db.update_topic(
            topic_id,
            TopicPatch(keywords=keywords, threshold=threshold),
        )
        return {"topic": topic, "calibration_id": value.calibration_id, "confirmed": True}

    @app.get("/api/sources")
    async def list_sources() -> dict[str, Any]:
        return {"sources": state.db.list_sources()}

    @app.post("/api/sources", status_code=201)
    async def create_source(value: SourceInput) -> dict[str, Any]:
        return {"source": state.db.create_source(value)}

    @app.post("/api/sources/validate")
    async def validate_source_draft(value: SourceInput) -> dict[str, Any]:
        source = value.model_dump(mode="json")
        source["id"] = "draft-source"
        try:
            return await state.manager.feed_service.validate(source)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"新闻源验证失败：{exc}") from exc

    @app.put("/api/sources/{source_id}")
    async def update_source(source_id: str, value: SourcePatch) -> dict[str, Any]:
        return {"source": state.db.update_source(source_id, value)}

    @app.delete("/api/sources/{source_id}")
    async def delete_source(source_id: str) -> dict[str, bool]:
        state.db.delete_source(source_id)
        return {"archived": True}

    @app.post("/api/sources/{source_id}/restore")
    async def restore_source(source_id: str) -> dict[str, Any]:
        return {"source": state.db.restore_source(source_id)}

    @app.post("/api/sources/{source_id}/validate")
    async def validate_source(source_id: str) -> dict[str, Any]:
        source = state.db.get_source(source_id)
        try:
            result = await state.manager.feed_service.validate(source)
            state.db.set_source_health(source_id, True)
            return result
        except Exception as exc:
            state.db.set_source_health(source_id, False, str(exc)[:500])
            raise HTTPException(status_code=422, detail=f"新闻源验证失败：{exc}") from exc

    @app.post("/api/runs", status_code=202)
    async def start_run(value: RunRequest) -> dict[str, Any]:
        return {"run": await state.manager.start(value, trigger="manual")}

    @app.get("/api/runs/current")
    async def current_run() -> dict[str, Any]:
        return {"run": state.db.get_current_run()}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = state.db.get_run(run_id)
        return {"run": run, "articles": state.db.list_run_articles(run_id)}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        return {"run": await state.manager.cancel(run_id)}

    @app.get("/api/reports/latest")
    async def latest_report() -> dict[str, Any]:
        return {"report": state.manager.latest_report()}

    @app.get("/api/history")
    async def history(
        q: str = Query(default="", max_length=200),
        topic_id: str | None = None,
        favorite: bool | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        articles = state.db.search_articles(q, topic_id, favorite, limit, offset)
        total = state.db.count_articles(q, topic_id, favorite)
        consumed = offset + len(articles)
        return {
            "articles": articles,
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": consumed if consumed < total else None,
            "query": {"q": q, "topic_id": topic_id, "favorite": favorite},
        }

    @app.put("/api/articles/{article_id}/favorite")
    async def favorite_article(article_id: str, value: FavoriteUpdate) -> dict[str, Any]:
        return {"article": state.db.set_favorite(article_id, value.favorite)}

    @app.get("/api/export")
    async def export_data() -> StreamingResponse:
        output = io.BytesIO(_export_bytes(state))
        headers = {"Content-Disposition": 'attachment; filename="cookies-news-cockpit-export.zip"'}
        return StreamingResponse(output, media_type="application/zip", headers=headers)

    @app.post("/api/import/preview")
    async def preview_import(request: Request) -> dict[str, Any]:
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > MAX_ARCHIVE_BYTES:
                    raise HTTPException(status_code=413, detail="备份超过 100 MiB 上限")
            except ValueError:
                raise HTTPException(status_code=400, detail="Content-Length 无效") from None
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_ARCHIVE_BYTES:
                raise HTTPException(status_code=413, detail="备份超过 100 MiB 上限")
            payload.extend(chunk)
        try:
            bundle = read_backup(bytes(payload))
        except BackupValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        preview = state.db.preview_import(
            bundle.configuration,
            bundle.articles,
            bundle.runs,
        )
        import_id = state.remember_import(bundle)
        conflicts: list[dict[str, str]] = []
        if preview["sources"]["matched"]:
            conflicts.append(
                {
                    "kind": "source",
                    "name": f"{preview['sources']['matched']} 个同地址来源",
                    "resolution": "本机已修改的设置优先；全新内置占位项采用备份启停状态",
                }
            )
        if preview["topics"]["matched"]:
            conflicts.append(
                {
                    "kind": "topic",
                    "name": f"{preview['topics']['matched']} 个同名主题",
                    "resolution": "保留本机主题设置",
                }
            )
        if preview["articles"]["matched"]:
            conflicts.append(
                {
                    "kind": "article",
                    "name": f"{preview['articles']['matched']} 条已有文章",
                    "resolution": "不重复导入，收藏状态取并集",
                }
            )
        if preview["runs"]["remapped"]:
            conflicts.append(
                {
                    "kind": "run",
                    "name": f"{preview['runs']['remapped']} 个同 ID 但内容不同的任务记录",
                    "resolution": "保留本机记录；导入记录使用新的内部 ID",
                }
            )
        return {
            "import_id": import_id,
            "summary": {
                "schema_version": bundle.schema_version,
                "topics_incoming": preview["topics"]["incoming"],
                "topics_new": preview["topics"]["new"],
                "sources_incoming": preview["sources"]["incoming"],
                "sources_new": preview["sources"]["new"],
                "articles_incoming": preview["articles"]["incoming"],
                "articles_new": preview["articles"]["new"],
                "runs_incoming": preview["runs"]["incoming"],
                "runs_new": preview["runs"]["new"],
                "runs_matched": preview["runs"]["matched"],
                "runs_remapped": preview["runs"]["remapped"],
                "conflicts": sum(
                    preview[key]["matched"] for key in ("sources", "topics", "articles")
                )
                + preview["runs"]["remapped"],
            },
            "conflicts": conflicts,
            "warnings": preview["warnings"],
        }

    @app.post("/api/import/{import_id}/apply")
    async def apply_import(import_id: str, _value: ImportApplyInput) -> dict[str, Any]:
        if state.manager.active:
            raise HTTPException(status_code=409, detail="新闻任务运行中，完成或取消后再导入")
        bundle = state.get_import(import_id)
        previous_good = state.db.get_latest_good_run_id()
        automatic_backup = _write_automatic_backup(state, _export_bytes(state))
        result = state.db.apply_import(
            bundle.configuration,
            bundle.articles,
            bundle.runs,
        )
        warning: str | None = None
        run_id = result.get("latest_imported_run_id") or result.get("run_id")
        if run_id:
            run = state.db.get_run(run_id)
            try:
                state.manager._write_artifact(run, [])
                if previous_good is None:
                    state.db.set_latest_good_run(run_id)
            except Exception:
                warning = "历史已安全导入，但无法生成首页报告；仍可在历史记录中查看。"
                LOGGER.exception("Could not promote imported run %s", run_id)
        state.pending_imports.pop(import_id, None)
        summary = {
            "sources_new": result["sources_added"],
            "sources_matched": result["sources_matched"],
            "topics_new": result["topics_added"],
            "topics_matched": result["topics_matched"],
            "articles_new": result["articles_added"],
            "articles_matched": result["articles_matched"],
            "favorites_merged": result["favorites_merged"],
            "runs_new": result["runs_added"],
            "runs_matched": result["runs_matched"],
            "runs_remapped": result["runs_remapped"],
        }
        return {
            "applied": True,
            "summary": summary,
            "automatic_backup": automatic_backup.name,
            "warning": warning,
        }

    @app.post("/api/heartbeat")
    async def heartbeat() -> dict[str, Any]:
        state.last_heartbeat = time.monotonic()
        return {"ok": True, "run_active": state.manager.active, "idle_timeout_seconds": 300}

    @app.post("/api/shutdown")
    async def shutdown() -> dict[str, bool]:
        if state.manager.active:
            raise HTTPException(status_code=409, detail="新闻任务正在运行，请等待完成后再退出应用")
        state.shutdown_event.set()
        return {"ok": True}

    static_dir = Path(__file__).with_name("static")
    if (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:

        @app.get("/")
        async def missing_frontend() -> dict[str, str]:
            return {"status": "backend-ready", "detail": "static/index.html not packaged"}

    return app
