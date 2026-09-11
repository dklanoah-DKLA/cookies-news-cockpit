from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cookies_news_cockpit.api import create_app
from cookies_news_cockpit.models import RunRequest, SettingsUpdate, SourceInput, TopicInput
from cookies_news_cockpit.pipeline import RunManager
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.services import FeedError
from cookies_news_cockpit.storage import Database
from test_backend_api import MemoryKeyStore
from test_pipeline_v12 import NoKeyStore, article


@pytest.fixture
def data_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "v13-reports" / uuid4().hex
    path.mkdir(parents=True)
    return path


class ChangeableFeeds:
    items: dict
    failed: set

    def __init__(self) -> None:
        self.items = {}
        self.failed = set()

    async def fetch_metadata(self, source: dict) -> list:
        if source["id"] in self.failed:
            raise FeedError("HTTP 503 fixture outage")
        return self.items.get(source["id"], [])


@pytest.mark.asyncio
async def test_degraded_news_stays_visible_after_source_recovery_and_dedup(data_home: Path) -> None:
    paths = resolve_paths(data_home)
    db = Database(paths.database)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    good = db.create_source(SourceInput(name="Good", url="https://good.example/rss"))
    unstable = db.create_source(SourceInput(name="Unstable", url="https://bad.example/rss"))
    db.create_topic(TopicInput(name="ESG", keywords=["ESG"], threshold=60,
                              source_ids=[good["id"], unstable["id"]]))
    feeds = ChangeableFeeds()
    mgr = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)
    feeds.items[good["id"]] = [article(good, "ESG disclosure 101", "101")]
    first = await mgr.run_now(RunRequest(), trigger="manual")
    assert first["status"] == "complete"
    assert mgr.latest_report()["run"]["id"] == first["id"]

    feeds.items[good["id"]] = [article(good, "ESG disclosure 202", "202")]
    feeds.failed.add(unstable["id"])
    second = await mgr.run_now(RunRequest(), trigger="manual")
    assert second["status"] == "degraded"
    assert second["article_count"] == 1
    visible = mgr.latest_report()
    assert visible["run"]["id"] == second["id"]
    assert visible["run"]["status"] == "degraded"
    assert "部分完成" in visible["run"]["warning"]
    assert visible["articles"][0]["title"] == "ESG disclosure 202"
    assert visible["source_errors"][0]["source_id"] == unstable["id"]
    assert db.get_latest_good_run_id() == first["id"]

    feeds.failed.clear()
    third = await mgr.run_now(RunRequest(), trigger="manual")
    assert third["status"] == "complete"
    assert third["article_count"] == 0
    assert third["funnel"]["history_duplicates"] == 1
    assert mgr.latest_report()["run"]["id"] == second["id"]
    assert mgr.source_errors(third) == []
    assert db.get_source(unstable["id"])["last_error"] is None

    with TestClient(create_app(paths=paths, token="test", key_store=MemoryKeyStore(),
                               allowed_hosts={"testserver"})) as client:
        headers = {"X-Cockpit-Token": "test"}
        current = client.get("/api/runs/current", headers=headers).json()
        assert current["run"]["id"] == third["id"]
        assert current["source_errors"] == []
        history = client.get(f"/api/runs/{second['id']}", headers=headers).json()
        assert "HTTP 503" in history["source_errors"][0]["error"]
        bootstrap = client.get("/api/bootstrap", headers=headers).json()
        assert bootstrap["current_source_errors"] == []
        assert bootstrap["latest_report"]["run"]["id"] == second["id"]


@pytest.mark.asyncio
async def test_current_failure_errors_are_not_taken_from_old_visible_report(
    data_home: Path,
) -> None:
    paths = resolve_paths(data_home)
    db = Database(paths.database)
    db.update_settings(SettingsUpdate(extract_full_text=False))
    source = db.create_source(SourceInput(name="Feed", url="https://feed.example/rss"))
    db.create_topic(TopicInput(name="ESG", keywords=["ESG"], source_ids=[source["id"]]))
    feeds = ChangeableFeeds()
    feeds.items[source["id"]] = [article(source, "ESG disclosure", "story")]
    mgr = RunManager(db, paths, key_store=NoKeyStore(), feed_service=feeds)
    first = await mgr.run_now(RunRequest(), trigger="manual")
    feeds.failed.add(source["id"])
    failed = await mgr.run_now(RunRequest(), trigger="manual")
    assert failed["status"] == "failed"
    assert mgr.latest_report()["run"]["id"] == first["id"]
    assert mgr.latest_report()["source_errors"] == []
    assert mgr.source_errors(failed) == [
        {"source_id": source["id"], "error": "HTTP 503 fixture outage", "source_name": "Feed"}
    ]
    # Old 1.2 reports can lack embedded funnel errors. Use their safe artifact.
    legacy = dict(failed, funnel={})
    assert mgr.source_errors(legacy) == mgr.source_errors(failed)
    with TestClient(create_app(paths=paths, token="test", key_store=MemoryKeyStore(),
                               allowed_hosts={"testserver"})) as client:
        data = client.get("/api/bootstrap", headers={"X-Cockpit-Token": "test"}).json()
        assert data["current_run"]["id"] == failed["id"]
        assert data["current_source_errors"] == mgr.source_errors(failed)


def test_v12_database_gets_separate_v13_snapshot_without_overwriting_old_backup(data_home: Path):
    from test_upgrade_import_v12 import _freeze_v11_database, _preserved_v11_state

    paths = resolve_paths(data_home)
    _freeze_v11_database(paths.database)
    with sqlite3.connect(paths.database) as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (
            "upgrade_checkpoint_1_2_0", json.dumps({"version": "1.2.0"}),
        ))
    backups = data_home / "backups"
    backups.mkdir()
    old = backups / "before-upgrade-1.2.0.sqlite3"
    old.write_bytes(b"old-version-backup-preserved")
    upgraded = Database(paths.database)
    before = _preserved_v11_state(upgraded)
    assert upgraded.get_metadata("upgrade_checkpoint_1_2_0") == {"version": "1.2.0"}
    assert upgraded.get_metadata("upgrade_checkpoint_1_3_0")["version"] == "1.3.0"
    snapshot = backups / "before-upgrade-1.3.0.sqlite3"
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        count = conn.execute(
            "SELECT COUNT(*) FROM metadata WHERE key='upgrade_checkpoint_1_3_0'"
        ).fetchone()[0]
        assert count == 0
    assert old.read_bytes() == b"old-version-backup-preserved"
    assert _preserved_v11_state(Database(paths.database)) == before
