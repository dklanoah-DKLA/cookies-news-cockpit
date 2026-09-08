"""Local-only fixture server for browser acceptance tests.

The fixture deliberately replaces network-facing services so the Playwright
suite cannot contact news sites or DeepSeek.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from cookies_news_cockpit.api import create_app
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
    async def validate(self, source: dict[str, object]) -> dict[str, object]:
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

    app = create_app(
        paths=resolve_paths(args.home),
        token=args.token,
        key_store=MemoryKeyStore(),
        feed_service=NoNetworkFeedService(),
        allowed_hosts={"127.0.0.1", "localhost"},
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
