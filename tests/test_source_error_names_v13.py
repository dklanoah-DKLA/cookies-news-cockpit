from __future__ import annotations

from pathlib import Path

import pytest

from cookies_news_cockpit.models import SourceInput, SourcePatch, TopicInput
from cookies_news_cockpit.pipeline import RunManager
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


def test_imported_error_uses_frozen_source_name_after_source_id_remap(tmp_path: Path) -> None:
    origin = Database(tmp_path / "origin.sqlite3")
    source = origin.create_source(
        SourceInput(name="Original market feed", url="https://market.example/rss")
    )
    topic = origin.create_topic(TopicInput(name="Market", source_ids=[source["id"]]))
    run = origin.create_run([topic["id"]], "test")
    frozen_funnel = {
        "source_errors": [{"source_id": source["id"], "error": "HTTP 503"}],
        "per_source": {source["id"]: {"name": source["name"], "feed_items": 0}},
    }
    origin.update_run(run["id"], status="failed", funnel=frozen_funnel)
    configuration = {
        "schema_version": 2,
        "sources": [source],
        "topics": [topic],
    }
    paths = resolve_paths(tmp_path / "destination")
    target = Database(paths.database)
    target.apply_import(configuration, [], origin.export_runs())
    imported_source = next(item for item in target.list_sources() if item["url"] == source["url"])
    assert imported_source["id"] != source["id"]
    target.update_source(imported_source["id"], SourcePatch(name="Renamed on new computer"))
    imported_run = target.get_run(run["id"])
    manager = RunManager(target, paths, key_store=NoKeyStore())

    assert manager.source_errors(imported_run) == [
        {
            "source_id": source["id"],
            "error": "HTTP 503",
            "source_name": "Original market feed",
        }
    ]
    assert target.get_run(run["id"])["funnel"] == frozen_funnel


@pytest.mark.parametrize("snapshots", [None, {}, [], {"old-id": {"name": None}}])
def test_legacy_error_without_source_snapshot_keeps_original_fields(
    tmp_path: Path, snapshots: object
) -> None:
    paths = resolve_paths(tmp_path)
    manager = RunManager(Database(paths.database), paths, key_store=NoKeyStore())
    error = {"source_id": "old-id", "error": "Legacy timeout"}
    run = {"id": "legacy-run", "funnel": {"source_errors": [error], "per_source": snapshots}}
    assert manager.source_errors(run) == [error]
