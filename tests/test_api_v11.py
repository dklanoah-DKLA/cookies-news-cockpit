from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from cookies_news_cockpit.api import create_app
from cookies_news_cockpit.keystore import KeyStoreError
from cookies_news_cockpit.models import CalibrationProposal, SettingsUpdate, SourceInput, TopicInput
from cookies_news_cockpit.runtime import resolve_paths
from cookies_news_cockpit.services import DeepSeekError
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


class RecordingFeeds:
    def __init__(self) -> None:
        self.seen: list[dict[str, object]] = []

    async def validate(self, source):
        self.seen.append(source)
        return {"ok": True, "entry_count": 2, "sample_title": "draft works"}


class SuccessfulDeepSeek:
    async def test_connection(self):
        return {"ok": True, "model": "deepseek-v4-flash", "http_requests": 1}

    async def calibrate(self, topic, goal, positive_examples, negative_examples):
        assert topic["name"] == "机器人"
        assert goal == "关注商业落地"
        assert positive_examples == []
        assert negative_examples == []
        return CalibrationProposal(
            keywords=["机器人", "robotics", "具身智能"],
            threshold=68,
            rationale="覆盖中英文别名，并保持较稳健门槛。",
        )


class FailedDeepSeek:
    async def test_connection(self):
        raise DeepSeekError(
            "DeepSeek API Key 鉴权失败（HTTP 401）",
            status_code=401,
            http_requests=1,
        )

    async def calibrate(self, _topic, _goal, _positive_examples, _negative_examples):
        raise DeepSeekError(
            "DeepSeek API Key 鉴权失败（HTTP 401）",
            status_code=401,
            http_requests=1,
        )


class BrokenKeyStore:
    def get(self) -> str | None:
        raise KeyStoreError("钥匙串读取失败")

    def set(self, _api_key: str) -> None:
        raise KeyStoreError("钥匙串写入失败")

    def delete(self) -> None:
        raise KeyStoreError("钥匙串删除失败")


def fresh_home(label: str) -> Path:
    value = Path(__file__).parents[1] / ".local-data" / "backend-tests" / f"{label}-{uuid4().hex}"
    value.mkdir(parents=True)
    return value


def test_deepseek_states_suggestion_and_draft_source_validation() -> None:
    home = fresh_home("v11-contract")
    key_store = MemoryKeyStore()
    feeds = RecordingFeeds()
    token = "v11-local-token"
    app = create_app(
        paths=resolve_paths(home),
        token=token,
        key_store=key_store,
        feed_service=feeds,
        deepseek_factory=lambda _key: SuccessfulDeepSeek(),
        allowed_hosts={"testserver"},
    )
    headers = {"X-Cockpit-Token": token}
    with TestClient(app) as client:
        saved = client.put(
            "/api/settings/deepseek-key",
            headers=headers,
            json={"api_key": "sk-v11-test-key"},
        )
        assert saved.status_code == 200
        assert saved.json()["status"] == "saved_unverified"

        tested = client.post("/api/settings/deepseek/test", headers=headers, json={})
        assert tested.status_code == 200
        assert tested.json()["status"] == "connected"
        assert tested.json()["http_requests"] == 1

        suggestion = client.post(
            "/api/topics/suggest",
            headers=headers,
            json={
                "name": "机器人",
                "keywords": ["机器人"],
                "exclusion_keywords": ["玩具"],
                "goal": "关注商业落地",
            },
        )
        assert suggestion.status_code == 200
        assert suggestion.json()["suggestion"] == {
            "keywords": ["机器人", "robotics", "具身智能"],
            "exclusion_keywords": ["玩具"],
            "threshold": 68,
            "rationale": "覆盖中英文别名，并保持较稳健门槛。",
        }

        validated = client.post(
            "/api/sources/validate",
            headers=headers,
            json={
                "name": "新地址",
                "url": "https://new.example/feed.xml",
                "homepage": "https://new.example/",
                "category": "综合",
                "language": "zh",
                "terms": None,
                "preset_id": None,
            },
        )
    assert validated.status_code == 200
    assert validated.json()["sample_title"] == "draft works"
    assert feeds.seen[-1]["url"] == "https://new.example/feed.xml"


def test_bootstrap_surfaces_keychain_read_failure_without_taking_app_offline() -> None:
    home = fresh_home("v11-keychain-error")
    token = "keychain-error-token"
    app = create_app(
        paths=resolve_paths(home),
        token=token,
        key_store=BrokenKeyStore(),
        feed_service=RecordingFeeds(),
        allowed_hosts={"testserver"},
    )
    with TestClient(app) as client:
        response = client.get("/api/bootstrap", headers={"X-Cockpit-Token": token})
    assert response.status_code == 200
    assert response.json()["deepseek"]["status"] == "error"
    assert "钥匙串读取失败" in response.json()["deepseek"]["error"]


def test_failed_temporary_deepseek_key_does_not_pollute_persisted_state() -> None:
    home = fresh_home("v11-temporary-key-failure")
    token = "temporary-key-token"
    key_store = MemoryKeyStore()
    app = create_app(
        paths=resolve_paths(home),
        token=token,
        key_store=key_store,
        feed_service=RecordingFeeds(),
        deepseek_factory=lambda _key: FailedDeepSeek(),
        allowed_hosts={"testserver"},
    )
    headers = {"X-Cockpit-Token": token}

    with TestClient(app) as client:
        response = client.post(
            "/api/settings/deepseek/test",
            headers=headers,
            json={"api_key": "sk-temporary-not-real"},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "error"
    assert payload["persisted_status"] == "unconfigured"
    assert payload["configured"] is False
    assert payload["http_requests"] == 1
    assert "鉴权失败" in payload["error"]
    assert key_store.value is None
    settings = app.state.cockpit.db.get_settings()
    assert settings.deepseek_status == "unconfigured"
    assert settings.deepseek_last_error is None


def test_saved_deepseek_key_failure_updates_and_retains_global_error() -> None:
    home = fresh_home("v11-saved-key-failure")
    token = "saved-key-token"
    key_store = MemoryKeyStore("sk-saved-not-real")
    app = create_app(
        paths=resolve_paths(home),
        token=token,
        key_store=key_store,
        feed_service=RecordingFeeds(),
        deepseek_factory=lambda _key: FailedDeepSeek(),
        allowed_hosts={"testserver"},
    )
    db = app.state.cockpit.db
    db.update_settings(
        SettingsUpdate(
            deepseek_status="connected",
            deepseek_last_tested_at="2026-09-01T00:00:00+00:00",
            deepseek_last_error=None,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/settings/deepseek/test",
            headers={"X-Cockpit-Token": token},
            json={},
        )

    payload = response.json()
    assert payload["status"] == "error"
    assert payload["configured"] is True
    assert payload["http_requests"] == 1
    assert "鉴权失败" in payload["error"]
    settings = db.get_settings()
    assert settings.deepseek_status == "error"
    assert settings.deepseek_last_tested_at != "2026-09-01T00:00:00+00:00"
    assert settings.deepseek_last_error == "DeepSeek API Key 鉴权失败（HTTP 401）"


def test_legacy_calibration_route_updates_deepseek_success_and_failure_states() -> None:
    success_home = fresh_home("v11-calibration-success")
    success_token = "calibration-success-token"
    success_app = create_app(
        paths=resolve_paths(success_home),
        token=success_token,
        key_store=MemoryKeyStore("sk-saved-not-real"),
        feed_service=RecordingFeeds(),
        deepseek_factory=lambda _key: SuccessfulDeepSeek(),
        allowed_hosts={"testserver"},
    )
    success_db = success_app.state.cockpit.db
    topic = success_db.create_topic(TopicInput(name="机器人", keywords=["机器人"]))
    with TestClient(success_app) as client:
        response = client.post(
            f"/api/topics/{topic['id']}/calibrate",
            headers={"X-Cockpit-Token": success_token},
            json={"goal": "关注商业落地", "positive_examples": [], "negative_examples": []},
        )
    assert response.status_code == 200
    success_settings = success_db.get_settings()
    assert success_settings.deepseek_status == "connected"
    assert success_settings.deepseek_last_error is None

    failure_home = fresh_home("v11-calibration-failure")
    failure_token = "calibration-failure-token"
    failure_app = create_app(
        paths=resolve_paths(failure_home),
        token=failure_token,
        key_store=MemoryKeyStore("sk-saved-not-real"),
        feed_service=RecordingFeeds(),
        deepseek_factory=lambda _key: FailedDeepSeek(),
        allowed_hosts={"testserver"},
    )
    failure_db = failure_app.state.cockpit.db
    failure_topic = failure_db.create_topic(TopicInput(name="机器人", keywords=["机器人"]))
    with TestClient(failure_app) as client:
        response = client.post(
            f"/api/topics/{failure_topic['id']}/calibrate",
            headers={"X-Cockpit-Token": failure_token},
            json={"goal": "关注商业落地", "positive_examples": [], "negative_examples": []},
        )
    assert response.status_code == 502
    failure_settings = failure_db.get_settings()
    assert failure_settings.deepseek_status == "error"
    assert failure_settings.deepseek_last_error == "DeepSeek API Key 鉴权失败（HTTP 401）"


def test_v2_export_preview_and_transactional_import_keep_local_settings() -> None:
    source_home = fresh_home("v11-export")
    source_paths = resolve_paths(source_home)
    source_db = Database(source_paths.database)
    source_db.update_settings(SettingsUpdate(default_threshold=88))
    source = source_db.create_source(
        SourceInput(name="Example feed", url="https://example.com/feed.xml")
    )
    topic = source_db.create_topic(
        TopicInput(name="跨机迁移", keywords=["robot"], source_ids=[source["id"]])
    )
    run = source_db.create_run([topic["id"]], "test")
    source_db.insert_article(
        {
            "run_id": run["id"],
            "topic_id": topic["id"],
            "source_id": source["id"],
            "title": "Robot market update",
            "url": "https://example.com/article",
            "content_hash": "portable-fingerprint",
            "favorite": True,
            "analysis_mode": "rules",
            "matched_keywords": ["robot"],
            "matched_fields": ["title"],
        }
    )
    source_db.update_run(
        run["id"],
        status="complete",
        phase="finished",
        outcome="complete",
        article_count=1,
        finished_at=run["started_at"],
    )

    source_token = "source-token"
    source_app = create_app(
        paths=source_paths,
        token=source_token,
        key_store=MemoryKeyStore("sk-secret-never-export"),
        feed_service=RecordingFeeds(),
        allowed_hosts={"testserver"},
    )
    with TestClient(source_app) as client:
        exported = client.get("/api/export", headers={"X-Cockpit-Token": source_token})
    assert exported.status_code == 200
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        assert {"manifest.json", "configuration.json", "history.json", "runs.json"} <= set(
            archive.namelist()
        )
        assert json.loads(archive.read("manifest.json"))["schema_version"] == 2
        assert all(
            b"sk-secret-never-export" not in archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/")
        )

    target_home = fresh_home("v11-import")
    target_paths = resolve_paths(target_home)
    target_db = Database(target_paths.database)
    target_db.update_settings(SettingsUpdate(default_threshold=61))
    target_token = "target-token"
    target_app = create_app(
        paths=target_paths,
        token=target_token,
        key_store=MemoryKeyStore(),
        feed_service=RecordingFeeds(),
        allowed_hosts={"testserver"},
    )
    headers = {
        "X-Cockpit-Token": target_token,
        "Content-Type": "application/zip",
        "X-Import-Filename": "cookies-news-cockpit-export.zip",
    }
    with TestClient(target_app) as client:
        preview = client.post("/api/import/preview", headers=headers, content=exported.content)
        assert preview.status_code == 200
        preview_payload = preview.json()
        assert preview_payload["summary"]["topics_new"] == 1
        assert preview_payload["summary"]["articles_new"] == 1
        assert preview_payload["summary"]["runs_new"] == 1
        applied = client.post(
            f"/api/import/{preview_payload['import_id']}/apply",
            headers={"X-Cockpit-Token": target_token},
            json={"strategy": "merge_keep_local"},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["applied"] is True
        assert applied.json()["summary"]["articles_new"] == 1
        assert applied.json()["summary"]["runs_new"] == 1
        latest = client.get("/api/reports/latest", headers={"X-Cockpit-Token": target_token})
        assert latest.json()["report"]["articles"][0]["title"] == "Robot market update"

        second_preview = client.post(
            "/api/import/preview", headers=headers, content=exported.content
        ).json()
        assert second_preview["summary"]["articles_new"] == 0
        second_apply = client.post(
            f"/api/import/{second_preview['import_id']}/apply",
            headers={"X-Cockpit-Token": target_token},
            json={"strategy": "merge_keep_local"},
        )
        assert second_apply.status_code == 200
        assert second_apply.json()["summary"]["articles_matched"] == 1
        assert second_apply.json()["summary"]["runs_new"] == 0
        assert second_apply.json()["summary"]["runs_matched"] == 1

    imported_db = target_app.state.cockpit.db
    assert imported_db.get_settings().default_threshold == 61
    assert imported_db.count_articles() == 1
    assert list((target_paths.root / "backups").glob("before-import-*.zip"))

    existing_home = fresh_home("v11-import-keep-home")
    existing_paths = resolve_paths(existing_home)
    existing_db = Database(existing_paths.database)
    local_source = existing_db.create_source(
        SourceInput(name="Local feed", url="https://local.example/feed.xml")
    )
    local_topic = existing_db.create_topic(
        TopicInput(name="本机日报", keywords=["local"], source_ids=[local_source["id"]])
    )
    local_run = existing_db.create_run([local_topic["id"]], "test")
    existing_db.insert_article(
        {
            "run_id": local_run["id"],
            "topic_id": local_topic["id"],
            "source_id": local_source["id"],
            "title": "Current local report",
            "url": "https://local.example/current",
            "content_hash": "current-local",
        }
    )
    existing_db.update_run(
        local_run["id"],
        status="complete",
        phase="finished",
        outcome="complete",
        article_count=1,
    )
    existing_db.set_latest_good_run(local_run["id"])
    existing_token = "existing-token"
    existing_app = create_app(
        paths=existing_paths,
        token=existing_token,
        key_store=MemoryKeyStore(),
        feed_service=RecordingFeeds(),
        allowed_hosts={"testserver"},
    )
    existing_headers = {
        "X-Cockpit-Token": existing_token,
        "Content-Type": "application/zip",
    }
    with TestClient(existing_app) as client:
        preview = client.post(
            "/api/import/preview", headers=existing_headers, content=exported.content
        ).json()
        applied = client.post(
            f"/api/import/{preview['import_id']}/apply",
            headers={"X-Cockpit-Token": existing_token},
            json={"strategy": "merge_keep_local"},
        )
        assert applied.status_code == 200
        latest = client.get(
            "/api/reports/latest", headers={"X-Cockpit-Token": existing_token}
        ).json()["report"]
    assert latest["run"]["id"] == local_run["id"]
    assert latest["articles"][0]["title"] == "Current local report"


def test_source_restore_endpoint_recovers_topic_binding() -> None:
    home = fresh_home("v11-source-restore")
    token = "restore-token"
    app = create_app(
        paths=resolve_paths(home),
        token=token,
        key_store=MemoryKeyStore(),
        feed_service=RecordingFeeds(),
        allowed_hosts={"testserver"},
    )
    db = app.state.cockpit.db
    source = db.create_source(
        SourceInput(name="Undo feed", url="https://undo.example/feed.xml")
    )
    topic = db.create_topic(
        TopicInput(name="撤销测试", keywords=["undo"], source_ids=[source["id"]])
    )
    headers = {"X-Cockpit-Token": token}
    with TestClient(app) as client:
        assert client.delete(f"/api/sources/{source['id']}", headers=headers).status_code == 200
        restored = client.post(f"/api/sources/{source['id']}/restore", headers=headers)
    assert restored.status_code == 200
    assert source["id"] in db.get_topic(topic["id"])["source_ids"]
