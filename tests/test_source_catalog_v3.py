from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from scripts.audit_source_presets import select_presets

from cookies_news_cockpit import storage
from cookies_news_cockpit.models import SettingsUpdate, SourceInput, SourcePatch, TopicInput
from cookies_news_cockpit.presets import (
    BANKING_CATEGORIES,
    PRESET_CATALOG_VERSION,
    SOURCE_PRESETS,
    SOURCE_PRESETS_BY_URL,
)
from cookies_news_cockpit.storage import Database


def _banking_presets():
    return tuple(preset for preset in SOURCE_PRESETS if preset.category in BANKING_CATEGORIES)


def _legacy_presets():
    return tuple(preset for preset in SOURCE_PRESETS if preset.category not in BANKING_CATEGORIES)


def _enabled_ids(db: Database) -> set[str]:
    return {source["id"] for source in db.list_sources(enabled_only=True)}


def _source_configuration(source: dict) -> dict:
    # An untouched preset may receive refreshed catalog metadata and timestamps.
    # These are the user-controlled settings which must never be overwritten.
    keys = (
        "id",
        "name",
        "url",
        "homepage",
        "category",
        "language",
        "terms",
        "enabled",
        "archived",
        "user_modified",
    )
    return {key: source[key] for key in keys}


def test_catalog_v3_banking_feeds_are_explicit_safe_and_opt_in():
    assert PRESET_CATALOG_VERSION == 3
    assert (
        frozenset(
            {
                "central_bank",
                "bank_regulation",
                "banking",
                "fintech",
            }
        )
        == BANKING_CATEGORIES
    )
    banking = _banking_presets()
    assert len(banking) >= 25
    assert {preset.category for preset in banking} == BANKING_CATEGORIES
    for preset in banking:
        assert preset.id.startswith("preset-")
        assert preset.name.strip()
        assert preset.language.strip()
        assert preset.default_enabled is False
        for url in (preset.url, preset.homepage, preset.terms):
            parsed = urlsplit(url)
            assert parsed.scheme == "https", (preset.id, url)
            assert parsed.hostname, (preset.id, url)
            assert parsed.username is None and parsed.password is None


def test_catalog_ids_and_urls_are_unique_and_lookup_is_complete():
    assert len({preset.id for preset in SOURCE_PRESETS}) == len(SOURCE_PRESETS)
    assert len({preset.url for preset in SOURCE_PRESETS}) == len(SOURCE_PRESETS)
    assert {preset.url: preset for preset in SOURCE_PRESETS} == SOURCE_PRESETS_BY_URL


def test_banking_catalog_has_matching_official_discovery_and_audit_manifest():
    path = Path(__file__).parents[1] / "docs" / "source-catalog-v3.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in manifest["presets"]}
    assert manifest["catalog_version"] == PRESET_CATALOG_VERSION
    assert set(records) == {preset.id for preset in _banking_presets()}
    for preset in _banking_presets():
        record = records[preset.id]
        for field in ("name", "url", "homepage", "category", "language", "terms"):
            assert record[field] == getattr(preset, field)
        assert record["directory_url"].startswith("https://")
        assert record["http_status"] == 200
        assert record["format"] in {"rss", "atom"}
        assert 0 < record["bytes"] <= 5 * 1024 * 1024
        assert record["sample_dates"] == record["sample_size"] > 0
        assert record["app_articles"] > 0
        assert record["default_enabled"] is False


def test_fresh_install_has_full_catalog_without_enabling_banking_sources(tmp_path: Path):
    db = Database(tmp_path / "fresh" / "cockpit.sqlite3")
    sources = {source["id"]: source for source in db.list_sources(include_archived=True)}
    assert set(sources) == {preset.id for preset in SOURCE_PRESETS}
    assert _enabled_ids(db) == {preset.id for preset in SOURCE_PRESETS if preset.default_enabled}
    for preset in _banking_presets():
        source = sources[preset.id]
        assert source["enabled"] is False
        assert source["archived"] is False
        assert source["preset_version"] == 3
        assert source["user_modified"] is False
    assert db.get_schema_version() == 2
    assert db.list_topics() == []


def test_catalog_upgrade_preserves_user_configuration_and_existing_topic_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    legacy_presets = _legacy_presets()
    assert len(legacy_presets) >= 4
    with monkeypatch.context() as legacy:
        legacy.setattr(storage, "SOURCE_PRESETS", legacy_presets)
        legacy.setattr(storage, "PRESET_CATALOG_VERSION", 2)
        db = Database(tmp_path / "upgrade.sqlite3")
        modified, disabled, archived, enabled = legacy_presets[:4]
        db.update_source(
            modified.id,
            SourcePatch(
                name="我的银行原始订阅",
                url="https://reader.example/bank.xml",
                homepage="https://reader.example/",
                category="my-credit-research",
                language="zh-Hant",
                terms="https://reader.example/terms",
                enabled=True,
            ),
        )
        db.update_source(disabled.id, SourcePatch(enabled=False))
        db.update_source(enabled.id, SourcePatch(enabled=True))
        db.create_topic(
            TopicInput(
                name="银行与支付",
                keywords=["银行，资本充足率、payments\nAML"],
                exclusion_keywords=["赞助，广告"],
                threshold=73,
                article_limit=17,
                source_ids=[modified.id, disabled.id, archived.id],
            )
        )
        db.create_topic(TopicInput(name="所有已启用来源", source_ids=[]))
        db.delete_source(archived.id)
        db.update_settings(
            SettingsUpdate(
                default_threshold=71,
                default_article_limit=23,
                freshness_days=14,
                refresh_minutes=120,
                scheduler_enabled=False,
                extract_full_text=False,
                semantic_fallback_enabled=False,
                semantic_fallback_limit=11,
                onboarding_completed=True,
                deepseek_status="connected",
                deepseek_last_tested_at="2026-09-10T08:00:00+00:00",
            )
        )
        before_sources = {
            source["id"]: _source_configuration(source)
            for source in db.list_sources(include_archived=True)
        }
        before_enabled = _enabled_ids(db)
        before_topics = db.list_topics(include_archived=True)
        before_settings = db.get_settings().model_dump()

    # Simulate installing the new catalog over an existing schema-v2 data file.
    # No source content, application keychain, network or real user data is used.
    for _ in range(3):
        upgraded = Database(db.path)
        all_sources = upgraded.list_sources(include_archived=True)
        assert len(all_sources) == len(SOURCE_PRESETS)
        assert len({source["url"] for source in all_sources}) == len(SOURCE_PRESETS)
        assert upgraded.get_schema_version() == 2
        assert _enabled_ids(upgraded) == before_enabled
        assert upgraded.list_topics(include_archived=True) == before_topics
        assert upgraded.get_settings().model_dump() == before_settings
        for source_id, expected in before_sources.items():
            assert _source_configuration(upgraded.get_source(source_id)) == expected
        for preset in _banking_presets():
            actual = upgraded.get_source(preset.id)
            assert actual["enabled"] is False
            assert actual["archived"] is False
            assert actual["preset_version"] == 3
        assert upgraded.get_source(archived.id)["archived"] is True
        assert modified.url not in {source["url"] for source in all_sources}


@pytest.mark.parametrize("archived", [False, True])
def test_upgrade_reuses_preexisting_user_feed_with_new_catalog_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archived: bool
):
    preset = _banking_presets()[0]
    with monkeypatch.context() as legacy:
        legacy.setattr(storage, "SOURCE_PRESETS", _legacy_presets())
        legacy.setattr(storage, "PRESET_CATALOG_VERSION", 2)
        db = Database(tmp_path / "custom-before-catalog.sqlite3")
        custom = db.create_source(
            SourceInput(
                name="已手动添加的银行来源",
                url=preset.url,
                homepage="https://manual.example/",
                terms="https://manual.example/terms",
                category="custom-bank-scope",
                language="zh",
                enabled=True,
            )
        )
        topic = db.create_topic(TopicInput(name="保留原绑定", source_ids=[custom["id"]]))
        if archived:
            db.delete_source(custom["id"])
        expected_source = _source_configuration(db.get_source(custom["id"]))
        expected_topic = db.get_topic(topic["id"])

    for _ in range(3):
        upgraded = Database(db.path)
        sources = upgraded.list_sources(include_archived=True)
        matching = [source for source in sources if source["url"] == preset.url]
        assert len(sources) == len(SOURCE_PRESETS)
        assert len(matching) == 1
        assert _source_configuration(matching[0]) == expected_source
        assert matching[0]["preset_id"] == preset.id
        assert matching[0]["preset_version"] == 3
        assert matching[0]["id"] == custom["id"]
        assert upgraded.get_topic(topic["id"]) == expected_topic


def test_new_banking_source_user_edits_and_archive_survive_repeated_launches(tmp_path: Path):
    db = Database(tmp_path / "catalog-after-upgrade.sqlite3")
    preset = _banking_presets()[0]
    db.update_source(
        preset.id,
        SourcePatch(
            name="我自己的银行订阅",
            url="https://new-address.example/banking.xml",
            category="private-finance-category",
            enabled=True,
        ),
    )
    topic = db.create_topic(TopicInput(name="银行观察", source_ids=[preset.id]))
    db.delete_source(preset.id)
    expected = _source_configuration(db.get_source(preset.id))
    for _ in range(3):
        db = Database(db.path)
        assert len(db.list_sources(include_archived=True)) == len(SOURCE_PRESETS)
        assert _source_configuration(db.get_source(preset.id)) == expected
        assert db.get_topic(topic["id"])["source_ids"] == [preset.id]
        assert preset.id not in _enabled_ids(db)
        assert preset.url not in {s["url"] for s in db.list_sources(include_archived=True)}


def test_audit_selector_defaults_to_whole_catalog_not_old_v2_subset():
    assert select_presets([]) == list(SOURCE_PRESETS)


def test_audit_selector_banking_all_matches_only_four_banking_categories():
    assert select_presets([], "banking_all") == list(_banking_presets())


@pytest.mark.parametrize("category", sorted(BANKING_CATEGORIES))
def test_audit_selector_accepts_each_bank_category(category: str):
    expected = [preset for preset in SOURCE_PRESETS if preset.category == category]
    assert expected
    assert select_presets([], category) == expected


def test_audit_selector_source_ids_preserve_catalog_order_and_deduplicate_requests():
    first, second = _banking_presets()[:2]
    assert select_presets([second.id, first.id, second.id]) == [first, second]
    assert select_presets([second.id, first.id], "banking_all") == [first, second]


def test_audit_selector_rejects_unknown_id_even_with_other_valid_ids():
    with pytest.raises(ValueError, match="unknown source id"):
        select_presets([SOURCE_PRESETS[0].id, "preset-does-not-exist"], "banking_all")


def test_audit_selector_rejects_unknown_category():
    with pytest.raises(ValueError, match="no sources match"):
        select_presets([], "bankng")


def test_audit_selector_rejects_conflicting_id_and_category_filters():
    with pytest.raises(ValueError, match="no sources match"):
        select_presets([_legacy_presets()[0].id], "banking_all")
