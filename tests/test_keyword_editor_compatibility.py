"""Offline API/data compatibility gates for the frontend-only keyword editor."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cookies_news_cockpit.api import create_app
from cookies_news_cockpit.backup import read_backup
from cookies_news_cockpit.models import (
    SettingsUpdate,
    SourceInput,
    TopicInput,
    TopicPatch,
    normalize_keyword_list,
)
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.storage import Database


class NoKeyStore:
    def get(self) -> None:
        return None


class NoNetworkFeeds:
    async def fetch_metadata(self, _source):
        raise AssertionError("The keyword editor tests must not fetch any feed")


@pytest.fixture
def editor_home() -> Path:
    value = Path(__file__).parents[1] / ".local-data" / "backend-tests" / f"keyword-{uuid4().hex}"
    value.mkdir(parents=True)
    return value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["A,B，C、D;E；F\r\nG"], list("ABCDEFG")),
        ([" ＡＰＩ ", "API", "api"], ["API"]),
        ([" Bank\u00a0of\u2003America ", "bank of america"], ["Bank of America"]),
        (["Straße", "STRASSE", "straße"], ["Straße"]),
        (["ΟΣ", "ος", "οσ"], ["ΟΣ"]),
        (["Open-Banking", "Open Banking", "open_banking", "C++", "C#"],
         ["Open-Banking", "Open Banking", "open_banking", "C++", "C#"]),
        (["x\u0085y", "x\u200by", "\ufeffx"], ["x y", "x\u200by", "\ufeffx"]),
        ([" \t ", "，、;；\n"], []),
    ],
)
def test_editor_normalization_matches_existing_backend(raw, expected) -> None:
    assert normalize_keyword_list(raw) == expected
    assert TopicInput(name="Banking", keywords=raw).keywords == expected
    assert TopicPatch(keywords=raw).keywords == expected
    assert TopicPatch(exclusion_keywords=raw).exclusion_keywords == expected


def test_normalization_splits_before_nfkc_and_omitted_words_are_not_rewritten(
    editor_home: Path,
) -> None:
    # Compatibility punctuation can introduce a separator only AFTER the split.
    # Pin that existing behavior: the editor must not silently normalize an
    # unchanged field merely because another setting was saved.
    db = Database(resolve_paths(editor_home).database)
    topic = db.create_topic(TopicInput(name="Compatibility", keywords=["A﹐B", "C､D"]))
    assert topic["keywords"] == ["A,B", "C、D"]
    assert normalize_keyword_list(topic["keywords"]) == ["A", "B", "C", "D"]
    changed = db.update_topic(topic["id"], TopicPatch(threshold=65))
    assert changed["keywords"] == topic["keywords"]


def test_word_count_checks_apply_before_and_after_normalization_without_length_cap() -> None:
    forty = [f"term-{index}" for index in range(40)]
    assert TopicPatch(keywords=forty).keywords == forty
    assert TopicPatch(keywords=["same"] * 40).keywords == ["same"]
    with pytest.raises(ValidationError):
        TopicPatch(keywords=["same"] * 41)  # Raw-list cap precedes deduplication.
    with pytest.raises(ValidationError):
        TopicPatch(keywords=[",".join(f"term-{index}" for index in range(41))])
    long_term = "long-valid-existing-keyword-" * 100
    assert TopicPatch(keywords=[long_term]).keywords == [long_term]
    assert TopicPatch(exclusion_keywords=forty).exclusion_keywords == forty
    with pytest.raises(ValidationError):
        TopicPatch(exclusion_keywords=["same"] * 41)


@pytest.mark.parametrize(("threshold", "article_limit"), [(None, None), (73, 9)])
def test_keyword_edit_preserves_configuration_history_favorites_and_backup(
    editor_home: Path, threshold: int | None, article_limit: int | None,
) -> None:
    paths = resolve_paths(editor_home)
    db = Database(paths.database)
    db.update_settings(SettingsUpdate(scheduler_enabled=False, default_threshold=64))
    source = db.create_source(
        SourceInput(name="Local test bank feed", url="https://bank.example/feed.xml")
    )
    topic = db.create_topic(
        TopicInput(
            name="Existing banking topic", keywords=["Bank of America", "C++"],
            exclusion_keywords=["careers"], threshold=threshold, article_limit=article_limit,
            source_ids=[source["id"]], enabled=False,
        )
    )
    other = db.create_topic(TopicInput(name="Unrelated payments", keywords=["Visa"]))
    run = db.create_run([topic["id"]], "compatibility-test")
    article = db.insert_article({
        "run_id": run["id"], "topic_id": topic["id"], "source_id": source["id"],
        "title": "Existing banking report", "url": "https://bank.example/article",
        "content_hash": "keyword-editor-history",
        "matched_keywords": ["Bank of America"], "matched_fields": ["title"],
    })
    db.set_favorite(article["id"], True)
    db.update_run(run["id"], status="complete", article_count=1, finished_at=run["started_at"])
    db.set_latest_good_run(run["id"])
    before_run = db.get_run(run["id"])
    before_article = db.get_article(article["id"])
    assert before_article["favorite"] is True
    before_settings = db.get_settings()
    before_source = db.get_source(source["id"])

    app = create_app(
        paths=paths, token="keyword-editor-test", key_store=NoKeyStore(),
        feed_service=NoNetworkFeeds(), allowed_hosts={"testserver"},
    )
    headers = {"X-Cockpit-Token": "keyword-editor-test"}
    with TestClient(app) as client:
        response = client.put(
            f"/api/topics/{topic['id']}", headers=headers,
            json={"keywords": ["Bank of America", "C++", "ＡＰＩ、Open Banking"],
                  "exclusion_keywords": ["careers", "招聘\njobs"]},
        )
        assert response.status_code == 200, response.text
        updated = response.json()["topic"]
        assert updated["keywords"] == ["Bank of America", "C++", "API", "Open Banking"]
        assert updated["exclusion_keywords"] == ["careers", "招聘", "jobs"]
        for field in ("id", "name", "threshold", "article_limit", "source_ids", "enabled",
                      "archived", "created_at"):
            assert updated[field] == topic[field], field
        assert db.get_topic(other["id"]) == other
        assert db.get_run(run["id"]) == before_run
        assert db.get_article(article["id"]) == before_article
        assert db.get_source(source["id"]) == before_source
        assert db.get_settings() == before_settings
        assert db.get_latest_good_run_id() == run["id"]

        exported = client.get("/api/export", headers=headers)
        assert exported.status_code == 200
        bundle = read_backup(exported.content)

    exported_topic = next(row for row in bundle.configuration["topics"] if row["id"] == topic["id"])
    assert exported_topic["keywords"] == updated["keywords"]
    assert exported_topic["exclusion_keywords"] == updated["exclusion_keywords"]
    restored_db = Database(resolve_paths(editor_home / "fresh-restore").database)
    restored_db.import_backup({
        "configuration": bundle.configuration,
        "history": {"runs": bundle.runs, "articles": bundle.articles},
    })
    restored = next(row for row in restored_db.list_topics() if row["name"] == topic["name"])
    for field in ("keywords", "exclusion_keywords", "threshold", "article_limit", "enabled"):
        assert restored[field] == updated[field], field
    assert restored_db.get_source(restored["source_ids"][0])["url"] == source["url"]
    restored_articles = restored_db.export_articles()
    assert len(restored_articles) == 1
    assert restored_articles[0]["favorite"] is True
    assert restored_articles[0]["matched_keywords"] == ["Bank of America"]


@pytest.mark.parametrize("patch", [
    {"keywords": None},
    {"exclusion_keywords": None},
    {"keywords": ["same"] * 41},
    {"keywords": ["valid"], "unexpected_editor_field": True},
])
def test_invalid_editor_payload_reports_422_without_partial_save(editor_home: Path, patch) -> None:
    paths = resolve_paths(editor_home)
    db = Database(paths.database)
    db.update_settings(SettingsUpdate(scheduler_enabled=False))
    topic = db.create_topic(TopicInput(name="Keep original", keywords=["bank"], threshold=None))
    app = create_app(
        paths=paths, token="invalid-editor-test", key_store=NoKeyStore(),
        feed_service=NoNetworkFeeds(), allowed_hosts={"testserver"},
    )
    with TestClient(app) as client:
        response = client.put(
            f"/api/topics/{topic['id']}", headers={"X-Cockpit-Token": "invalid-editor-test"},
            json=patch,
        )
        assert response.status_code == 422
        assert response.json()["detail"]
    assert db.get_topic(topic["id"]) == topic
