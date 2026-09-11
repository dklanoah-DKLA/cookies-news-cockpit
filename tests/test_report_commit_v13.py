from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cookies_news_cockpit.api import _export_bytes, create_app
from cookies_news_cockpit.backup import BackupBundle, read_backup
from cookies_news_cockpit.models import SourceInput, TopicInput
from cookies_news_cockpit.pipeline import RunManager
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


@pytest.fixture
def report_fixture(tmp_path: Path):
    paths = resolve_paths(tmp_path)
    db = Database(paths.database)
    manager = RunManager(db, paths, key_store=NoKeyStore())
    source = db.create_source(SourceInput(name="Commit", url="https://commit.example/rss"))
    topic = db.create_topic(
        TopicInput(name="ESG", keywords=["ESG"], source_ids=[source["id"]])
    )

    def create_result(name: str, status: str, *, artifact: bool = True) -> dict:
        run = db.create_run([topic["id"]], "test")
        db.insert_article(
            {
                "run_id": run["id"],
                "topic_id": topic["id"],
                "source_id": source["id"],
                "title": name,
                "url": f"https://commit.example/{name}",
                "content_hash": name,
            }
        )
        run = db.update_run(
            run["id"], status=status, article_count=1, finished_at="2026-09-11T00:00:00+00:00"
        )
        # All fixture runs intentionally tie to expose accidental timestamp-
        # only ordering while preserving their actual insertion sequence.
        with db.connect() as connection:
            connection.execute(
                "UPDATE runs SET started_at = ? WHERE id = ?",
                ("2026-09-11T00:00:00+00:00", run["id"]),
            )
        run = db.get_run(run["id"])
        if artifact:
            manager._write_artifact(run, [])
        return run

    return db, manager, paths, create_result


def test_missing_newest_artifact_preserves_previous_committed_degraded_report(report_fixture):
    db, manager, paths, create_result = report_fixture
    complete = create_result("A", "complete")
    db.set_latest_good_run(complete["id"])
    degraded = create_result("B", "degraded")
    committed_path = paths.runs / f"{degraded['id']}.json"
    original = committed_path.read_bytes()
    assert manager.latest_report()["run"]["id"] == degraded["id"]
    interrupted_commit = create_result("C", "degraded", artifact=False)

    assert db.list_usable_run_ids() == [interrupted_commit["id"], degraded["id"], complete["id"]]
    assert manager.latest_report()["run"]["id"] == degraded["id"]
    assert db.get_latest_good_run_id() == complete["id"]
    assert committed_path.read_bytes() == original
    assert not (paths.runs / f"{interrupted_commit['id']}.json").exists()
    reopened = RunManager(Database(paths.database), paths, key_store=NoKeyStore())
    assert reopened.latest_report()["run"]["id"] == degraded["id"]


@pytest.mark.parametrize(
    "corruption",
    [
        [], None, 7, "broken", {}, {"run": []},
        {"run": {"id": "wrong", "status": "degraded"}, "articles": [], "source_errors": []},
    ],
)
def test_nonobject_or_mismatched_artifact_skips_to_committed_report(
    report_fixture, corruption
):
    db, manager, paths, create_result = report_fixture
    complete = create_result("A", "complete")
    db.set_latest_good_run(complete["id"])
    degraded = create_result("B", "degraded")
    corrupt = create_result("C", "degraded", artifact=False)
    path = paths.runs / f"{corrupt['id']}.json"
    path.write_text(json.dumps(corruption), encoding="utf-8")
    before = path.read_bytes()
    assert manager.latest_report()["run"]["id"] == degraded["id"]
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "bad_field,value", [("status", {}), ("articles", {}), ("source_errors", 0)]
)
def test_invalid_artifact_structure_does_not_promote_new_result(report_fixture, bad_field, value):
    db, manager, paths, create_result = report_fixture
    complete = create_result("A", "complete")
    db.set_latest_good_run(complete["id"])
    invalid = create_result("B", "degraded", artifact=False)
    payload = {"run": invalid, "articles": [], "source_errors": []}
    if bad_field == "status":
        payload["run"]["status"] = value
    else:
        payload[bad_field] = value
    (paths.runs / f"{invalid['id']}.json").write_text(json.dumps(payload), encoding="utf-8")
    assert manager.latest_report()["run"]["id"] == complete["id"]


def test_complete_artifact_is_visible_if_latest_good_metadata_update_failed(report_fixture):
    db, manager, _paths, create_result = report_fixture
    previous = create_result("A", "complete")
    db.set_latest_good_run(previous["id"])
    newest = create_result("B", "complete")
    visible = manager.latest_report()
    assert visible["run"]["id"] == newest["id"]
    assert visible["run"]["status"] == "complete"
    assert db.get_latest_good_run_id() == previous["id"]
    # With no prior checkpoint, a committed complete report is still complete.
    db.restore_latest_good_run(None)
    assert manager.latest_report()["run"]["status"] == "complete"


def test_failed_run_with_artifact_cannot_replace_committed_news(report_fixture):
    db, manager, _paths, create_result = report_fixture
    previous = create_result("A", "complete")
    db.set_latest_good_run(previous["id"])
    failed = create_result("B", "failed")
    assert failed["id"] not in db.list_usable_run_ids()
    assert manager.latest_report()["run"]["id"] == previous["id"]


def test_corrupt_checkpoint_uses_database_without_crashing_or_overwriting_file(report_fixture):
    db, manager, paths, create_result = report_fixture
    previous = create_result("A", "complete")
    db.set_latest_good_run(previous["id"])
    path = paths.runs / f"{previous['id']}.json"
    path.write_text("[]", encoding="utf-8")
    visible = manager.latest_report()
    assert visible["run"]["id"] == previous["id"]
    assert visible["articles"][0]["title"] == "A"
    assert path.read_text(encoding="utf-8") == "[]"


def test_first_database_only_legacy_or_import_report_remains_available(report_fixture):
    db, manager, _paths, create_result = report_fixture
    imported = create_result("Imported", "complete", artifact=False)
    visible = manager.latest_report()
    assert visible["run"]["id"] == imported["id"]
    assert visible["run"]["status"] == "degraded"
    assert visible["articles"][0]["title"] == "Imported"
    assert db.get_latest_good_run_id() is None


@pytest.mark.parametrize("local_status", ["complete", "degraded"])
def test_safe_import_keeps_local_report_with_or_without_complete_checkpoint(
    report_fixture, local_status: str
):
    db, _manager, paths, create_result = report_fixture
    local = create_result("Local", local_status, artifact=local_status == "degraded")
    if local_status == "complete":
        db.set_latest_good_run(local["id"])

    incoming = Database(paths.root / "incoming.sqlite3")
    source = incoming.create_source(
        SourceInput(name="Incoming source", url="https://incoming.example/rss")
    )
    topic = incoming.create_topic(TopicInput(name="Incoming topic", source_ids=[source["id"]]))
    run = incoming.create_run([topic["id"]], "manual")
    incoming.insert_article(
        {
            "run_id": run["id"], "topic_id": topic["id"], "source_id": source["id"],
            "title": "Incoming future report", "url": "https://incoming.example/article",
            "content_hash": "incoming-report",
        }
    )
    incoming.update_run(
        run["id"], status="complete", article_count=1,
        finished_at="2026-09-12T00:00:00+00:00", funnel={"frozen": "original-run"},
    )
    bundle = BackupBundle(
        2, {"schema_version": 2, "sources": [source], "topics": [topic]},
        incoming.export_articles(), incoming.export_runs(), {},
    )
    app = create_app(
        paths=paths, key_store=NoKeyStore(), token="test", allowed_hosts={"testserver"}
    )
    state = app.state.cockpit
    with TestClient(app) as client:
        for index in range(2):
            import_id = state.remember_import(bundle)
            applied = client.post(
                f"/api/import/{import_id}/apply", headers={"X-Cockpit-Token": "test"},
                json={"strategy": "merge_keep_local"},
            )
            assert applied.status_code == 200
            assert applied.json()["summary"]["runs_new"] == int(index == 0)
            visible = client.get(
                "/api/reports/latest", headers={"X-Cockpit-Token": "test"}
            ).json()["report"]
            assert visible["run"]["id"] == local["id"]
            current = client.get(
                "/api/runs/current", headers={"X-Cockpit-Token": "test"}
            ).json()["run"]
            assert current["id"] == local["id"]
        assert state.db.get_metadata(f"history_only_import:{run['id']}") is True
        assert state.db.get_run(run["id"])["funnel"] == {"frozen": "original-run"}
        assert state.db.get_latest_good_run_id() == (
            local["id"] if local_status == "complete" else None
        )
        exported = _export_bytes(state)

    reopened = RunManager(Database(paths.database), paths, key_store=NoKeyStore())
    assert reopened.latest_report()["run"]["id"] == local["id"]
    with zipfile.ZipFile(BytesIO(exported)) as archive:
        assert not any(b"history_only_import:" in archive.read(name) for name in archive.namelist())

    restored_bundle = read_backup(exported)
    fresh_paths = resolve_paths(paths.root / "fresh-computer")
    fresh = Database(fresh_paths.database)
    imported = fresh.apply_import(
        restored_bundle.configuration, restored_bundle.articles, restored_bundle.runs
    )
    assert imported["preserved_local_report"] is False
    fresh_manager = RunManager(fresh, fresh_paths, key_store=NoKeyStore())
    newest = imported["latest_imported_run_id"]
    fresh_manager._write_artifact(fresh.get_run(newest), [])
    assert fresh_manager.latest_report()["articles"][0]["title"] == "Incoming future report"
    assert fresh.get_current_run()["id"] == newest
