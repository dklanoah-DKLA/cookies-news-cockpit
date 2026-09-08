from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
import sqlite3
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .keystore import DeepSeekKeyStore, KeyStoreError
from .models import (
    CalibrationConfirm,
    CalibrationRequest,
    DeepSeekKeyInput,
    FavoriteUpdate,
    RunRequest,
    SettingsUpdate,
    SourceInput,
    SourcePatch,
    TopicInput,
    TopicPatch,
)
from .pipeline import RunConflictError, RunManager
from .runtime import AppPaths, resolve_paths
from .services import DeepSeekError, FeedService, UnsafeUrlError
from .storage import Database, NotFoundError, TopicLimitError

LOGGER = logging.getLogger(__name__)


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
            "product": {"name": "Cookies News Cockpit", "version": __version__, "mode": "local"},
            "settings": state.db.get_settings().model_dump(),
            "deepseek": {"configured": bool(state.key_store.get()), "model": "deepseek-chat"},
            "topics": state.db.list_topics(),
            "sources": state.db.list_sources(),
            "current_run": state.db.get_current_run(),
            "latest_report": state.manager.latest_report(),
        }

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "settings": state.db.get_settings().model_dump(),
            "deepseek": {"configured": bool(state.key_store.get()), "model": "deepseek-chat"},
        }

    @app.put("/api/settings")
    async def put_settings(value: SettingsUpdate) -> dict[str, Any]:
        return {"settings": state.db.update_settings(value).model_dump()}

    @app.put("/api/settings/deepseek-key")
    async def put_deepseek_key(value: DeepSeekKeyInput) -> dict[str, Any]:
        state.key_store.set(value.api_key)
        return {"configured": True, "model": "deepseek-chat"}

    @app.delete("/api/settings/deepseek-key")
    async def delete_deepseek_key() -> dict[str, Any]:
        state.key_store.delete()
        return {"configured": False, "model": "deepseek-chat"}

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

    @app.post("/api/topics/{topic_id}/calibrate")
    async def calibrate_topic(topic_id: str, value: CalibrationRequest) -> dict[str, Any]:
        topic = state.db.get_topic(topic_id)
        api_key = state.key_store.get()
        if not api_key:
            raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key")
        ai = state.manager.deepseek_factory(api_key)
        proposal = await ai.calibrate(
            topic,
            value.goal,
            value.positive_examples,
            value.negative_examples,
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

    @app.put("/api/sources/{source_id}")
    async def update_source(source_id: str, value: SourcePatch) -> dict[str, Any]:
        return {"source": state.db.update_source(source_id, value)}

    @app.delete("/api/sources/{source_id}")
    async def delete_source(source_id: str) -> dict[str, bool]:
        state.db.delete_source(source_id)
        return {"archived": True}

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
        output = io.BytesIO()
        configuration = {
            "schema_version": 1,
            "settings": state.db.get_settings().model_dump(),
            "topics": state.db.list_topics(include_archived=True),
            "sources": state.db.list_sources(include_archived=True),
            "deepseek_api_key_included": False,
        }
        history_rows = state.db.export_articles()
        history_rows = [
            {key: value for key, value in article.items() if key != "full_text"}
            for article in history_rows
        ]
        with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "configuration.json",
                json.dumps(configuration, ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "history.json",
                json.dumps({"articles": history_rows}, ensure_ascii=False, indent=2),
            )
            for path in sorted(state.paths.runs.glob("*.json")):
                archive.write(path, f"reports/{path.name}")
        output.seek(0)
        headers = {"Content-Disposition": 'attachment; filename="cookies-news-cockpit-export.zip"'}
        return StreamingResponse(output, media_type="application/zip", headers=headers)

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
