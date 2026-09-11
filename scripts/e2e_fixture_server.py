"""Local-only fixture server for browser acceptance tests.

The fixture deliberately replaces network-facing services so the Playwright
suite cannot contact news sites or DeepSeek.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from cookies_news_cockpit.api import create_app
from cookies_news_cockpit.models import FeedArticle
from cookies_news_cockpit.runtime import resolve_paths


class MemoryKeyStore:
    def __init__(self) -> None:
        self.value: str | None = None

    def get(self) -> str | None:
        return self.value

    def set(self, api_key: str) -> None:
        self.value = api_key

    def delete(self) -> None:
        self.value = None


class NoNetworkFeedService:
    def __init__(self) -> None:
        self.mode = "empty"
        self.good_source_id = ""
        self.bad_source_id = ""
        self.title = ""
        self.fetch_calls: list[str] = []
        self.validation_calls = 0

    def configure(self, value: dict[str, object]) -> dict[str, object]:
        mode = str(value.get("mode", "empty"))
        if mode not in {"empty", "success", "partial", "all_failed", "slow"}:
            raise ValueError("Unknown browser fixture scenario")
        self.mode = mode
        self.good_source_id = str(value.get("good_source_id", ""))
        self.bad_source_id = str(value.get("bad_source_id", ""))
        self.title = str(value.get("title", "E2E ESG 企业绿色投资增长10%"))
        self.fetch_calls.clear()
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "fetch_calls": self.fetch_calls,
            "validation_calls": self.validation_calls,
        }

    async def fetch(
        self, source: dict[str, object], extract_full_text: bool = False
    ) -> list[FeedArticle]:
        source_id = str(source["id"])
        self.fetch_calls.append(source_id)
        if self.mode == "slow":
            await asyncio.sleep(120)
        if self.mode == "all_failed" or (
            self.mode == "partial" and source_id == self.bad_source_id
        ):
            raise RuntimeError(f"E2E_CURRENT_{self.mode.upper()}_SOURCE_ERROR")
        if self.mode in {"success", "partial"} and source_id == self.good_source_id:
            return [
                FeedArticle(
                    source_id=source_id,
                    source_name=str(source["name"]),
                    title=self.title,
                    url=f"https://fixture.invalid/article/{self.title[-3:]}",
                    excerpt=f"ESG 浏览器隔离验收新闻：{self.title}",
                    published_at=datetime.now(UTC).isoformat(),
                )
            ]
        return []

    async def validate(self, source: dict[str, object]) -> dict[str, object]:
        self.validation_calls += 1
        if "slow-validation" in str(source["url"]):
            # A real asynchronous delay, beyond the former 15-second UI timeout.
            await asyncio.sleep(16)
        return {
            "ok": True,
            "entry_count": 1,
            "sample_title": f"E2E fixture: {source['name']}",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the isolated browser QA fixture")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    feed_service = NoNetworkFeedService()
    app = create_app(
        paths=resolve_paths(args.home),
        token=args.token,
        key_store=MemoryKeyStore(),
        feed_service=feed_service,
        allowed_hosts={"127.0.0.1", "localhost"},
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    )

    # These controls exist only in this local fixture script and use the same
    # /api/ session guard. The installed app never exposes them.
    @app.post("/api/e2e/configure")
    async def configure_fixture(value: dict[str, object]) -> dict[str, object]:
        return feed_service.configure(value)

    @app.get("/api/e2e/state")
    async def fixture_state() -> dict[str, object]:
        return feed_service.snapshot()

    @app.post("/api/e2e/shutdown")
    async def shutdown_fixture() -> dict[str, bool]:
        server.should_exit = True
        return {"ok": True}

    fixture_routes = [route for route in app.router.routes if route.path.startswith("/api/e2e/")]
    app.router.routes = fixture_routes + [
        route for route in app.router.routes if route not in fixture_routes
    ]

    server.run()


if __name__ == "__main__":
    main()
