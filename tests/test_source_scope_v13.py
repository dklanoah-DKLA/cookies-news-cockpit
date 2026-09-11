from __future__ import annotations

import json
from pathlib import Path

import pytest

from cookies_news_cockpit.models import SourceInput, SourcePatch, TopicInput, TopicPatch
from cookies_news_cockpit.storage import Database


def _configuration(db: Database) -> dict:
    return {
        "schema_version": 2,
        "sources": db.list_sources(include_archived=True),
        "topics": db.list_topics(include_archived=True),
    }


def _custom(db: Database, name: str = "Selected") -> dict:
    return db.create_source(
        SourceInput(name=name, url=f"https://{name.lower()}.example/feed.xml")
    )


def _effective_source_ids(db: Database, topic_id: str) -> set[str]:
    scope = db.get_topic(topic_id)["source_ids"]
    return {
        source["id"]
        for source in db.list_sources(enabled_only=True)
        if not scope or source["id"] in scope
    }


def test_archive_last_binding_does_not_expand_scope_and_restore_preserves_it(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    source = _custom(db)
    unselected = _custom(db, "Unselected")
    topic = db.create_topic(TopicInput(name="Explicit", source_ids=[source["id"]]))
    untouched = db.create_topic(TopicInput(name="Unrelated"))
    db.delete_source(source["id"])

    assert db.get_topic(topic["id"])["source_ids"] == [source["id"]]
    assert _effective_source_ids(db, topic["id"]) == set()
    assert db.get_topic(untouched["id"]) == untouched
    assert db.get_source(source["id"])["user_modified"] is True
    restored = db.restore_source(source["id"])
    assert restored["enabled"] is True
    assert restored["user_modified"] is True
    assert _effective_source_ids(db, topic["id"]) == {source["id"]}
    assert unselected["id"] not in _effective_source_ids(db, topic["id"])


def test_edit_can_keep_disabled_archived_binding_but_cannot_add_new_archived_source(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    source = _custom(db)
    topic = db.create_topic(TopicInput(name="Before", source_ids=[source["id"]]))
    db.update_source(source["id"], SourcePatch(enabled=False))
    db.update_topic(topic["id"], TopicPatch(name="While disabled", source_ids=[source["id"]]))
    db.delete_source(source["id"])
    updated = db.update_topic(
        topic["id"], TopicPatch(name="While archived", source_ids=[source["id"]])
    )
    assert updated["source_ids"] == [source["id"]]
    with pytest.raises(ValueError, match="已移除"):
        db.create_topic(TopicInput(name="New archived", source_ids=[source["id"]]))
    other = db.create_topic(TopicInput(name="Other"))
    with pytest.raises(ValueError, match="已移除"):
        db.update_topic(other["id"], TopicPatch(source_ids=[source["id"]]))
    with pytest.raises(ValueError, match="不存在"):
        db.update_topic(topic["id"], TopicPatch(source_ids=["missing"]))
    assert db.get_topic(topic["id"])["source_ids"] == [source["id"]]


@pytest.mark.parametrize("restore_by_create", [False, True])
def test_restore_does_not_rebind_after_explicit_user_scope_edit(
    tmp_path: Path, restore_by_create: bool
):
    db = Database(tmp_path / "db.sqlite3")
    source = _custom(db)
    replacement = _custom(db, "Replacement")
    topic = db.create_topic(TopicInput(name="Edit scope", source_ids=[source["id"]]))
    db.delete_source(source["id"])
    db.update_topic(topic["id"], TopicPatch(source_ids=[replacement["id"]]))
    if restore_by_create:
        db.create_source(SourceInput(name=source["name"], url=source["url"]))
    else:
        db.restore_source(source["id"])
    assert db.get_topic(topic["id"])["source_ids"] == [replacement["id"]]


@pytest.mark.parametrize("legacy_modified_flag", [False, True])
def test_safe_import_preserves_local_archive_even_with_legacy_flag(
    tmp_path: Path, legacy_modified_flag: bool
):
    db = Database(tmp_path / "db.sqlite3")
    preset = db.get_source("preset-chinanews-scroll")
    configuration = {
        "schema_version": 2,
        "sources": [preset | {"id": "backup-source", "enabled": True}],
        "topics": [{"id": "backup-topic", "name": "Imported", "source_ids": ["backup-source"]}],
    }
    db.delete_source(preset["id"])
    with db.connect() as connection:
        connection.execute(
            "UPDATE sources SET user_modified = ? WHERE id = ?",
            (int(legacy_modified_flag), preset["id"]),
        )
    preview = db.preview_import(configuration, [])
    assert preview["sources"]["new"] == 0
    assert any("保持移除" in warning for warning in preview["warnings"])
    result = db.apply_import(configuration, [])
    assert result["sources_added"] == 0
    assert result["archived_sources_preserved"] == 1
    assert db.get_source(preset["id"])["archived"] is True
    assert db.get_source(preset["id"])["enabled"] is False
    imported_topic = next(topic for topic in db.list_topics() if topic["name"] == "Imported")
    assert imported_topic["source_ids"] == [preset["id"]]
    assert _effective_source_ids(db, imported_topic["id"]) == set()


def test_tombstone_scope_survives_export_import_and_explicit_all_stays_all(tmp_path: Path):
    source_db = Database(tmp_path / "source.sqlite3")
    source = _custom(source_db)
    source_db.create_topic(TopicInput(name="Scoped", source_ids=[source["id"]]))
    source_db.create_topic(TopicInput(name="All enabled", source_ids=[]))
    source_db.delete_source(source["id"])
    target = Database(tmp_path / "target.sqlite3")
    configuration = _configuration(source_db)
    result = target.apply_import(configuration, [])
    assert result["topics_added"] == 2
    topics = {topic["name"]: topic for topic in target.list_topics()}
    assert len(topics["Scoped"]["source_ids"]) == 1
    assert _effective_source_ids(target, topics["Scoped"]["id"]) == set()
    assert topics["All enabled"]["source_ids"] == []
    archived_source = target.get_source(topics["Scoped"]["source_ids"][0])
    assert archived_source["user_modified"] is True
    target.restore_source(archived_source["id"])
    assert _effective_source_ids(target, topics["Scoped"]["id"]) == {archived_source["id"]}


def test_import_uses_stable_preset_identity_after_local_url_name_and_status_edit(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    preset = db.get_source("preset-chinanews-scroll")
    db.update_source(
        preset["id"],
        SourcePatch(name="My feed", url="https://edited.example/feed.xml", enabled=False),
    )
    configuration = {
        "schema_version": 2,
        "sources": [preset | {"id": "backup-preset"}],
        "topics": [{"id": "t", "name": "Imported", "source_ids": ["backup-preset"]}],
    }
    before_count = len(db.list_sources(include_archived=True))
    db = Database(db.path)
    assert len(db.list_sources(include_archived=True)) == before_count
    preview = db.preview_import(configuration, [])
    result = db.apply_import(configuration, [])
    assert preview["sources"] == {"incoming": 1, "new": 0, "matched": 1}
    assert result["sources_added"] == 0
    assert len(db.list_sources(include_archived=True)) == before_count
    kept = db.get_source(preset["id"])
    assert (kept["name"], kept["url"], kept["enabled"]) == (
        "My feed", "https://edited.example/feed.xml", False
    )
    assert db.list_topics()[0]["source_ids"] == [preset["id"]]
    reopened = Database(db.path)
    assert len(reopened.list_sources(include_archived=True)) == before_count
    assert reopened.get_source(preset["id"])["url"] == "https://edited.example/feed.xml"


def test_import_rejects_conflicting_url_and_preset_identity_in_preview_and_apply(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    preset = db.get_source("preset-chinanews-scroll")
    other = _custom(db)
    configuration = {"sources": [preset | {"url": other["url"]}], "topics": []}
    for operation in (db.preview_import, db.apply_import):
        with pytest.raises(ValueError, match="不同本机来源"):
            operation(configuration, [])
    assert db.get_source(preset["id"])["url"] == preset["url"]


def test_fresh_install_restores_modified_preset_address_without_duplicate(tmp_path: Path):
    source = Database(tmp_path / "source.sqlite3")
    preset_id = "preset-chinanews-scroll"
    source.update_source(
        preset_id,
        SourcePatch(name="My source", url="https://my-source.example/feed", enabled=False),
    )
    source.create_topic(TopicInput(name="Modified preset", source_ids=[preset_id]))
    target = Database(tmp_path / "target.sqlite3")
    configuration = _configuration(source)
    count = len(target.list_sources(include_archived=True))
    preview = target.preview_import(configuration, [])
    result = target.apply_import(configuration, [])
    assert preview["sources"]["new"] == result["sources_added"] == 0
    assert len(target.list_sources(include_archived=True)) == count
    restored = target.get_source(preset_id)
    assert (restored["name"], restored["url"], restored["enabled"]) == (
        "My source", "https://my-source.example/feed", False
    )
    assert target.list_topics()[0]["source_ids"] == [preset_id]
    reopened = Database(target.path)
    assert len(reopened.list_sources(include_archived=True)) == count
    assert reopened.get_source(preset_id)["url"] == restored["url"]


def test_upgrade_repairs_known_empty_scopes_and_warns_on_ambiguous_edits(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    source = _custom(db)
    replacement = _custom(db, "Replacement")
    same_time = db.create_topic(TopicInput(name="Known unchanged"))
    empty_later = db.create_topic(TopicInput(name="Ambiguous empty"))
    nonempty_later = db.create_topic(
        TopicInput(name="Edited bounded", source_ids=[replacement["id"]])
    )
    stamp = "2026-09-10T00:00:00+00:00"
    with db.connect() as connection:
        connection.execute(
            "UPDATE sources SET archived = 1, enabled = 0, user_modified = 0 WHERE id = ?",
            (source["id"],),
        )
        connection.execute(
            "UPDATE topics SET updated_at = ? WHERE id = ?", (stamp, same_time["id"])
        )
        connection.execute(
            "INSERT INTO source_archive_snapshots VALUES (?, ?, ?)",
            (
                source["id"],
                json.dumps([same_time["id"], empty_later["id"], nonempty_later["id"]]),
                stamp,
            ),
        )
    upgraded = Database(db.path)
    assert upgraded.get_source(source["id"])["user_modified"] is True
    assert upgraded.get_topic(same_time["id"])["source_ids"] == [source["id"]]
    assert upgraded.get_topic(empty_later["id"])["source_ids"] == [source["id"]]
    assert upgraded.get_topic(nonempty_later["id"])["source_ids"] == [replacement["id"]]
    warning = upgraded.source_scope_warning()
    assert warning["recovered_topic_ids"] == [empty_later["id"]]
    assert warning["unresolved_topic_ids"] == [nonempty_later["id"]]
    reopened = Database(db.path)
    assert reopened.source_scope_warning() == warning
    assert reopened.get_topic(empty_later["id"])["source_ids"] == [source["id"]]
    reopened.update_topic(empty_later["id"], TopicPatch(source_ids=[]))
    reopened.update_topic(nonempty_later["id"], TopicPatch(source_ids=[replacement["id"]]))
    assert reopened.source_scope_warning() is None
    reopened.restore_source(source["id"])
    assert reopened.get_topic(empty_later["id"])["source_ids"] == []
    assert reopened.get_topic(nonempty_later["id"])["source_ids"] == [replacement["id"]]
