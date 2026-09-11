from __future__ import annotations

from pathlib import Path

from cookies_news_cockpit.models import SourceInput, TopicInput
from cookies_news_cockpit.storage import Database


def test_same_second_export_restore_preserves_current_and_latest_report_order(tmp_path: Path):
    source_db = Database(tmp_path / "source.sqlite3")
    feed = source_db.create_source(
        SourceInput(name="Order feed", url="https://order.example/feed")
    )
    topic = source_db.create_topic(TopicInput(name="Order topic", source_ids=[feed["id"]]))
    stamp = "2026-09-11T00:00:00+00:00"
    # Deliberately reverse lexicographic IDs: they cannot represent chronology.
    for index, run_id in enumerate(("z-earlier-run", "a-later-run")):
        created = source_db.create_run([topic["id"]], "manual")
        with source_db.connect() as connection:
            connection.execute(
                "UPDATE runs SET id = ?, started_at = ? WHERE id = ?",
                (run_id, stamp, created["id"]),
            )
        source_db.update_run(
            run_id, status="complete", finished_at=stamp, article_count=1, phase="finished"
        )
        source_db.insert_article(
            {
                "id": f"article-{index}",
                "run_id": run_id,
                "topic_id": topic["id"],
                "source_id": feed["id"],
                "title": f"Article {index}",
                "url": f"https://order.example/article-{index}",
                "content_hash": str(index) * 64,
            }
        )
    source_db.set_latest_good_run("a-later-run")
    assert source_db.get_current_run()["id"] == "a-later-run"
    exported = source_db.export_runs()
    assert [run["id"] for run in exported] == ["z-earlier-run", "a-later-run"]
    configuration = {
        "schema_version": 2,
        "sources": source_db.list_sources(include_archived=True),
        "topics": source_db.list_topics(include_archived=True),
    }
    articles = [
        article for run in exported for article in source_db.list_run_articles(run["id"])
    ]
    target_db = Database(tmp_path / "target.sqlite3")
    imported = target_db.apply_import(configuration, articles, exported)
    assert target_db.get_current_run()["id"] == "a-later-run"
    assert target_db.get_newest_usable_run_id() == "a-later-run"
    assert imported["latest_imported_run_id"] == "a-later-run"
    # API import promotion uses this result; its report must agree with /current.
    target_db.set_latest_good_run(imported["latest_imported_run_id"])
    assert target_db.get_latest_good_run_id() == "a-later-run"
    reopened = Database(target_db.path)
    assert reopened.get_current_run()["id"] == "a-later-run"
    assert reopened.get_latest_good_run_id() == "a-later-run"
    assert [run["id"] for run in reopened.export_runs()] == [
        "z-earlier-run", "a-later-run"
    ]
    repeat = reopened.apply_import(configuration, articles, exported)
    assert repeat["runs_added"] == repeat["articles_added"] == 0
    assert reopened.get_current_run()["id"] == "a-later-run"
    assert reopened.get_latest_good_run_id() == "a-later-run"
