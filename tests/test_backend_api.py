from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cookies_news_cockpit.api import create_app
from cookies_news_cockpit.models import SourceInput, TopicInput
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.storage import Database


class MemoryKeyStore:
    def __init__(self, value: str | None = None):
        self.value = value

    def get(self) -> str | None:
        return self.value

    def set(self, api_key: str) -> None:
        self.value = api_key

    def delete(self) -> None:
        self.value = None


class EmptyFeedService:
    async def validate(self, source):
        return {"ok": True, "entry_count": 1, "sample_title": source["name"]}


@pytest.fixture
def backend_home() -> Path:
    path = Path(__file__).parents[1] / ".local-data" / "backend-tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


def make_client(tmp_path: Path) -> tuple[TestClient, str]:
    token = "local-test-session"
    app = create_app(
        paths=resolve_paths(tmp_path),
        token=token,
        key_store=MemoryKeyStore(),
        feed_service=EmptyFeedService(),
        allowed_hosts={"testserver"},
    )
    return TestClient(app), token


def test_api_requires_session_token_and_bootstrap_contract(backend_home: Path) -> None:
    client, token = make_client(backend_home)
    with client:
        assert client.get("/api/bootstrap").status_code == 401
        response = client.get("/api/bootstrap", headers={"X-Cockpit-Token": token})
    assert response.status_code == 200
    payload = response.json()
    # Frontend bootstrap contract: these fields are stable and are sufficient
    # to render settings, source/topic editors, current status and last report.
    assert set(payload) == {
        "product",
        "settings",
        "deepseek",
        "topics",
        "sources",
        "archived_sources",
        "source_scope_warning",
        "current_run",
        "current_source_errors",
        "latest_report",
    }
    assert payload["product"]["name"] == "Cookies News Cockpit"
    assert payload["deepseek"] == {
        "configured": False,
        "status": "unconfigured",
        "model": "deepseek-v4-flash",
        "last_tested_at": None,
        "error": None,
    }
    assert any(source["id"] == "preset-chinanews-scroll" for source in payload["sources"])
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_settings_key_and_topic_crud(backend_home: Path) -> None:
    client, token = make_client(backend_home)
    headers = {"X-Cockpit-Token": token}
    with client:
        updated = client.put(
            "/api/settings",
            headers=headers,
            json={"default_threshold": 72, "default_article_limit": 12},
        )
        assert updated.status_code == 200
        assert updated.json()["settings"]["default_threshold"] == 72

        assert (
            client.put(
                "/api/settings/deepseek-key",
                headers=headers,
                json={"api_key": "sk-test-only-not-real"},
            ).json()["configured"]
            is True
        )
        settings = client.get("/api/settings", headers=headers).json()
        assert "api_key" not in str(settings)
        assert settings["deepseek"]["configured"] is True
        assert (
            client.delete("/api/settings/deepseek-key", headers=headers).json()["configured"]
            is False
        )

        created = client.post(
            "/api/topics",
            headers=headers,
            json={"name": "AI 芯片", "keywords": ["GPU", " GPU ", "算力"]},
        )
        assert created.status_code == 201
        topic = created.json()["topic"]
        assert topic["keywords"] == ["GPU", "算力"]
        changed = client.put(
            f"/api/topics/{topic['id']}",
            headers=headers,
            json={"threshold": 80, "article_limit": 30},
        ).json()["topic"]
        assert changed["threshold"] == 80
        inherited = client.put(
            f"/api/topics/{topic['id']}",
            headers=headers,
            json={"threshold": None, "article_limit": None},
        )
        assert inherited.status_code == 200
        assert inherited.json()["topic"]["threshold"] is None
        assert inherited.json()["topic"]["article_limit"] is None
        assert client.app.state.cockpit.db.get_topic(topic["id"])["threshold"] is None
        for required_field in ("name", "keywords", "source_ids", "enabled"):
            invalid = client.put(
                f"/api/topics/{topic['id']}",
                headers=headers,
                json={required_field: None},
            )
            assert invalid.status_code == 422, required_field
        assert client.delete(f"/api/topics/{topic['id']}", headers=headers).json() == {
            "archived": True
        }
        assert client.get("/api/topics", headers=headers).json()["topics"] == []


def test_topic_limit_counts_only_active_topics(backend_home: Path) -> None:
    db = Database(resolve_paths(backend_home).database)
    topic_ids = []
    for index in range(10):
        topic_ids.append(db.create_topic(TopicInput(name=f"主题 {index}"))["id"])
    try:
        db.create_topic(TopicInput(name="第十一个"))
    except ValueError as exc:
        assert "10" in str(exc)
    else:
        raise AssertionError("topic limit was not enforced")
    db.delete_topic(topic_ids[0])
    assert db.create_topic(TopicInput(name="替补主题"))["name"] == "替补主题"


def test_source_delete_is_soft_archive(backend_home: Path) -> None:
    db = Database(resolve_paths(backend_home).database)
    source = db.create_source(
        SourceInput(name="Example", url="https://example.com/feed.xml", enabled=True)
    )
    db.delete_source(source["id"])
    assert source["id"] not in {item["id"] for item in db.list_sources()}
    assert db.get_source(source["id"])["archived"] is True


def test_history_api_returns_explicit_pagination_metadata(backend_home: Path) -> None:
    client, token = make_client(backend_home)
    headers = {"X-Cockpit-Token": token}
    db = client.app.state.cockpit.db
    source = db.create_source(
        SourceInput(name="Example", url="https://example.com/feed.xml", enabled=True)
    )
    topic = db.create_topic(TopicInput(name="AI", keywords=["GPU"], source_ids=[source["id"]]))
    run = db.create_run([topic["id"]], "test")
    for index in range(3):
        db.insert_article(
            {
                "run_id": run["id"],
                "topic_id": topic["id"],
                "source_id": source["id"],
                "title": f"GPU {index}",
                "url": f"https://news.example/{index}",
                "content_hash": str(index),
            }
        )

    with client:
        first = client.get("/api/history?limit=2&offset=0", headers=headers)
        second = client.get("/api/history?limit=2&offset=2", headers=headers)
        invalid = client.get("/api/history?limit=201", headers=headers)
    assert first.status_code == 200
    assert first.json()["total"] == 3
    assert first.json()["next_offset"] == 2
    assert second.json()["offset"] == 2
    assert len(second.json()["articles"]) == 1
    assert second.json()["next_offset"] is None
    assert invalid.status_code == 422


def test_run_api_exposes_duplicates_skipped_metric(backend_home: Path) -> None:
    client, token = make_client(backend_home)
    headers = {"X-Cockpit-Token": token}
    db = client.app.state.cockpit.db
    run = db.create_run([], "test")
    db.update_run(run["id"], status="degraded", duplicates_skipped=4)
    with client:
        response = client.get(f"/api/runs/{run['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["run"]["duplicates_skipped"] == 4


def test_startup_purges_full_text_older_than_30_days(backend_home: Path) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    source = db.create_source(
        SourceInput(name="Example", url="https://example.com/feed.xml", enabled=True)
    )
    topic = db.create_topic(
        TopicInput(name="AI", keywords=["GPU"], source_ids=[source["id"]])
    )
    run = db.create_run([topic["id"]], "test")
    article = db.insert_article(
        {
            "run_id": run["id"],
            "topic_id": topic["id"],
            "source_id": source["id"],
            "title": "GPU 旧闻",
            "url": "https://example.com/old",
            "full_text": "应在启动时清理的正文",
            "content_hash": "old-full-text",
        }
    )
    with db.connect() as connection:
        connection.execute(
            "UPDATE articles SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", article["id"]),
        )

    app = create_app(
        paths=paths,
        token="startup-retention-test",
        key_store=MemoryKeyStore(),
        feed_service=EmptyFeedService(),
        allowed_hosts={"testserver"},
    )

    assert app.state.cockpit.db.get_article(article["id"])["full_text"] == ""


def test_startup_recovers_interrupted_runs_and_preserves_latest_good(
    backend_home: Path,
) -> None:
    paths = resolve_paths(backend_home)
    db = Database(paths.database)
    topic = db.create_topic(
        TopicInput(
            name="AI",
            keywords=["GPU"],
            source_ids=["preset-chinanews-scroll"],
        )
    )
    complete = db.create_run([topic["id"]], "test")
    db.insert_article(
        {
            "run_id": complete["id"],
            "topic_id": topic["id"],
            "source_id": "preset-chinanews-scroll",
            "title": "GPU 新品发布",
            "url": "https://news.example/gpu",
            "content_hash": "good-report",
        }
    )
    db.update_run(complete["id"], status="complete", finished_at="2026-09-08T00:00:00+00:00")
    db.set_latest_good_run(complete["id"])
    queued = db.create_run([topic["id"]], "manual")
    running = db.create_run([topic["id"]], "scheduler")
    db.update_run(running["id"], status="running")

    client, token = make_client(backend_home)
    recovered_db = client.app.state.cockpit.db
    recovered = [recovered_db.get_run(run_id) for run_id in (queued["id"], running["id"])]

    assert {run["status"] for run in recovered} == {"failed"}
    assert all(run["finished_at"] for run in recovered)
    assert {run["finished_at"] for run in recovered} == {recovered[0]["finished_at"]}
    assert all("标记为中断" in run["error"] for run in recovered)
    assert recovered_db.get_run(complete["id"])["status"] == "complete"
    assert recovered_db.get_latest_good_run_id() == complete["id"]
    assert recovered_db.recover_interrupted_runs() == 0

    with client:
        bootstrap = client.get("/api/bootstrap", headers={"X-Cockpit-Token": token}).json()
    assert bootstrap["latest_report"]["run"]["id"] == complete["id"]


def test_shutdown_refuses_an_active_run_without_signaling_exit(backend_home: Path) -> None:
    class NeverDoneTask:
        @staticmethod
        def done() -> bool:
            return False

    client, token = make_client(backend_home)
    headers = {"X-Cockpit-Token": token}
    state = client.app.state.cockpit
    with client:
        state.manager.tasks["active-test-run"] = NeverDoneTask()
        refused = client.post("/api/shutdown", headers=headers)

        assert refused.status_code == 409
        assert "正在运行" in refused.json()["detail"]
        assert state.shutdown_event.is_set() is False

        state.manager.tasks.pop("active-test-run")
        accepted = client.post("/api/shutdown", headers=headers)
        assert accepted.status_code == 200
        assert accepted.json() == {"ok": True}
        assert state.shutdown_event.is_set() is True
