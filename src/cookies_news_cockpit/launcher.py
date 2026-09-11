from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from .api import create_app
from .instance import AlreadyRunningError, InstanceOwner, existing_session_url
from .runtime import resolve_paths

IDLE_EXIT_SECONDS = 300


def _configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "cockpit.log",
        maxBytes=1_500_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger("cookies_news_cockpit").addHandler(handler)
    logging.getLogger("cookies_news_cockpit").setLevel(logging.INFO)
    cutoff = time.time() - 30 * 24 * 60 * 60
    for path in log_dir.glob("*.log*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


async def _open_browser(url: str) -> None:
    await asyncio.sleep(0.35)
    webbrowser.open(url, new=1)


async def _watch_exit(server: uvicorn.Server, state: object) -> None:
    cockpit = state
    while not server.should_exit:
        if cockpit.shutdown_event.is_set():
            server.should_exit = True
            return
        idle = time.monotonic() - cockpit.last_heartbeat
        if idle >= IDLE_EXIT_SECONDS and not cockpit.manager.active:
            server.should_exit = True
            return
        await asyncio.sleep(2)


async def _serve(port: int, launch_browser: bool) -> None:
    paths = resolve_paths()
    try:
        owner = InstanceOwner(paths.root).acquire()
    except AlreadyRunningError:
        # The first launch may still be preparing its socket. Never initialize
        # a database, configure shared log rotation, or run recovery here.
        if launch_browser:
            for _ in range(30):
                url = existing_session_url(paths.root)
                if url:
                    await _open_browser(url)
                    break
                await asyncio.sleep(0.1)
        return
    sock: socket.socket | None = None
    watchers: list[asyncio.Task[None]] = []
    try:
        _configure_logging(paths.logs)
        app = create_app(paths=paths, instance_owner=owner)
        state = app.state.cockpit
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        selected_port = sock.getsockname()[1]
        # The fragment is never sent in HTTP requests or Referer headers.
        url = f"http://127.0.0.1:{selected_port}/#token={state.token}"
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=selected_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)

        async def publish_ready_endpoint() -> None:
            while not server.started and not server.should_exit:
                await asyncio.sleep(0.05)
            if not server.started:
                return
            try:
                owner.publish_url(url)
            except OSError:
                logging.getLogger(__name__).warning("Could not publish private local endpoint")
            if launch_browser:
                await _open_browser(url)

        watchers = [
            asyncio.create_task(_watch_exit(server, state), name="idle-exit"),
            asyncio.create_task(publish_ready_endpoint(), name="publish-endpoint"),
        ]
        await server.serve(sockets=[sock])
    finally:
        for task in watchers:
            task.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        if sock is not None:
            sock.close()
        owner.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch Cookies News Cockpit")
    parser.add_argument("--port", type=int, default=0, help="Local port; 0 chooses a free port")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the cockpit browser")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    asyncio.run(_serve(args.port, not args.no_browser))
