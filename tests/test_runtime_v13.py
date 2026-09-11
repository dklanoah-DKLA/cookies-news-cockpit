from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cookies_news_cockpit.api import create_app
from cookies_news_cockpit.instance import (
    AlreadyRunningError,
    InstanceOwner,
    existing_session_url,
)
from cookies_news_cockpit.models import RunRequest, SourceInput, TopicInput
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


class WaitingFeed:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def fetch_metadata(self, _source: dict) -> list:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _process_owner(root: str, connection) -> None:
    paths = resolve_paths(root)
    owner = InstanceOwner(paths.root).acquire()
    db = Database(paths.database)
    run = db.create_run([], "manual")
    db.update_run(run["id"], status="running")
    connection.send(run["id"])
    connection.recv()
    owner.close()


def _compete_for_owner(root: str, start, release, results) -> None:
    start.wait()
    owner = InstanceOwner(Path(root))
    try:
        owner.acquire()
    except AlreadyRunningError:
        results.put("busy")
        return
    results.put("owner")
    release.wait()
    owner.close()


def test_second_app_cannot_recover_or_cancel_first_apps_run(tmp_path: Path) -> None:
    paths = resolve_paths(tmp_path)
    first = create_app(paths=paths, key_store=NoKeyStore())
    owner = first.state.cockpit.instance_owner
    try:
        run = first.state.cockpit.db.create_run([], "manual")
        first.state.cockpit.db.update_run(run["id"], status="running")
        with pytest.raises(AlreadyRunningError):
            create_app(paths=paths, key_store=NoKeyStore())
        assert first.state.cockpit.db.get_run(run["id"])["status"] == "running"
        assert owner.held
    finally:
        owner.close()


def test_crashed_process_releases_lock_before_startup_recovery(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_process_owner, args=(str(tmp_path), child))
    process.start()
    try:
        assert parent.poll(20), "owner child did not become ready"
        run_id = parent.recv()
        with pytest.raises(AlreadyRunningError):
            create_app(paths=resolve_paths(tmp_path), key_store=NoKeyStore())
        assert Database(resolve_paths(tmp_path).database).get_run(run_id)["status"] == "running"
        process.terminate()
        process.join(timeout=10)
        assert not process.is_alive()
        app = create_app(paths=resolve_paths(tmp_path), key_store=NoKeyStore())
        try:
            recovered = app.state.cockpit.db.get_run(run_id)
            assert recovered["status"] == "failed"
            assert "标记为中断" in recovered["error"]
            assert app.state.cockpit.instance_owner.held
        finally:
            app.state.cockpit.instance_owner.close()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        parent.close()
        child.close()


def test_simultaneous_first_launch_has_exactly_one_owner(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start, release = context.Event(), context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_compete_for_owner, args=(str(tmp_path), start, release, results)
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        assert sorted(results.get(timeout=20) for _ in processes) == ["busy", "owner"]
    finally:
        release.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()
    assert all(process.exitcode == 0 for process in processes)
    owner = InstanceOwner(tmp_path).acquire()
    owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_persistence_failure", [False, True])
async def test_shutdown_finishes_active_task_before_releasing_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_persistence_failure: bool
) -> None:
    feed = WaitingFeed()
    paths = resolve_paths(tmp_path)
    app = create_app(paths=paths, key_store=NoKeyStore(), feed_service=feed)
    state = app.state.cockpit
    source = state.db.create_source(
        SourceInput(name="Waiting", url="https://waiting.example/feed.xml")
    )
    topic = state.db.create_topic(
        TopicInput(name="GPU", keywords=["GPU"], source_ids=[source["id"]])
    )
    if cancel_persistence_failure:
        async def failed_cancel(_run_id: str) -> None:
            raise OSError("simulated cancel persistence failure")

        monkeypatch.setattr(state.manager, "cancel", failed_cancel)
    async with app.router.lifespan_context(app):
        run = await state.manager.start(RunRequest(topic_ids=[topic["id"]]))
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        assert state.instance_owner.held
    assert feed.cancelled
    assert not state.manager.active
    assert state.db.get_run(run["id"])["status"] == "cancelled"
    assert not state.instance_owner.held
    next_app = create_app(paths=paths, key_store=NoKeyStore())
    assert next_app.state.cockpit.db.get_run(run["id"])["status"] == "cancelled"
    next_app.state.cockpit.instance_owner.close()


def test_lock_file_persists_and_stale_metadata_does_not_block_restart(tmp_path: Path) -> None:
    owner = InstanceOwner(tmp_path).acquire()
    lock_path = owner.directory / "instance.lock"
    owner.close()
    assert lock_path.exists()
    owner.metadata.write_text('{"url":"http://malicious.example/#token=stale"}')
    second = InstanceOwner(tmp_path).acquire()
    try:
        assert not owner.metadata.exists()
        assert lock_path.exists()
    finally:
        second.close()


def test_endpoint_token_is_private_or_not_persisted(tmp_path: Path) -> None:
    owner = InstanceOwner(tmp_path).acquire()
    url = "http://127.0.0.1:12345/#token=test-session-token-123456789"
    try:
        owner.publish_url(url)
        if os.name == "nt":
            assert not owner.metadata.exists()
            assert existing_session_url(tmp_path) is None
        else:
            assert owner.directory.stat().st_mode & 0o077 == 0
            assert owner.metadata.stat().st_mode & 0o077 == 0
            assert existing_session_url(tmp_path) == url
            owner.metadata.chmod(0o644)
            assert existing_session_url(tmp_path) is None
    finally:
        owner.close()
    assert not owner.metadata.exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:12345/#token=test-session-token-123456789",
        "http://evil.example:12345/#token=test-session-token-123456789",
        "http://127.0.0.1:12345/?token=test-session-token-123456789",
        "http://127.0.0.1:12345/#token=short",
    ],
)
def test_owner_rejects_non_local_or_unsafe_session_url(tmp_path: Path, url: str) -> None:
    owner = InstanceOwner(tmp_path).acquire()
    try:
        with pytest.raises(ValueError):
            owner.publish_url(url)
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_duplicate_launcher_only_reopens_existing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cookies_news_cockpit.launcher as launcher

    paths = resolve_paths(tmp_path)
    owner = InstanceOwner(tmp_path).acquire()
    opened: list[str] = []
    url = "http://127.0.0.1:12345/#token=test-session-token-123456789"

    async def record_open(value: str) -> None:
        opened.append(value)

    def forbidden_create(**_kwargs):
        raise AssertionError("duplicate launch must not create a second app")

    monkeypatch.setattr(launcher, "resolve_paths", lambda: paths)
    monkeypatch.setattr(launcher, "existing_session_url", lambda _root: url)
    monkeypatch.setattr(launcher, "_open_browser", record_open)
    monkeypatch.setattr(launcher, "create_app", forbidden_create)
    try:
        await launcher._serve(0, True)
        assert opened == [url]
        assert owner.held
        await launcher._serve(0, False)
        assert opened == [url]
    finally:
        owner.close()


def test_initialization_failure_releases_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cookies_news_cockpit.api as api

    def fail_database(_path: Path) -> None:
        raise OSError("simulated startup failure")

    monkeypatch.setattr(api, "Database", fail_database)
    with pytest.raises(OSError, match="startup failure"):
        create_app(paths=resolve_paths(tmp_path), key_store=NoKeyStore())
    owner = InstanceOwner(tmp_path).acquire()
    owner.close()


@pytest.mark.asyncio
async def test_launcher_server_failure_releases_socket_and_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cookies_news_cockpit.launcher as launcher

    paths = resolve_paths(tmp_path)
    closed: list[bool] = []

    class FakeSocket:
        def setsockopt(self, *_args) -> None:
            pass

        def bind(self, _address) -> None:
            pass

        def listen(self, _backlog) -> None:
            pass

        def getsockname(self) -> tuple[str, int]:
            return "127.0.0.1", 12345

        def close(self) -> None:
            closed.append(True)

    class FailedServer:
        def __init__(self, _config) -> None:
            self.started = False
            self.should_exit = False

        async def serve(self, **_kwargs) -> None:
            raise RuntimeError("simulated server startup failure")

    monkeypatch.setattr(launcher, "resolve_paths", lambda: paths)
    monkeypatch.setattr(launcher, "_configure_logging", lambda _logs: None)
    monkeypatch.setattr(
        launcher,
        "socket",
        SimpleNamespace(
            socket=lambda *_args: FakeSocket(),
            AF_INET=launcher.socket.AF_INET,
            SOCK_STREAM=launcher.socket.SOCK_STREAM,
            SOL_SOCKET=launcher.socket.SOL_SOCKET,
            SO_REUSEADDR=launcher.socket.SO_REUSEADDR,
        ),
    )
    monkeypatch.setattr(launcher.uvicorn, "Server", FailedServer)
    with pytest.raises(RuntimeError, match="server startup failure"):
        await launcher._serve(0, False)
    assert closed == [True]
    owner = InstanceOwner(tmp_path).acquire()
    owner.close()
