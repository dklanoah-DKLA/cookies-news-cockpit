from __future__ import annotations

import io
import json
import zipfile

import pytest

from cookies_news_cockpit.backup import BackupValidationError, read_backup


def make_backup(*, schema: int = 2, extra: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": schema}))
        archive.writestr(
            "configuration.json",
            json.dumps({"schema_version": schema, "settings": {}, "topics": [], "sources": []}),
        )
        archive.writestr("history.json", json.dumps({"articles": []}))
        archive.writestr("runs.json", json.dumps({"runs": []}))
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return output.getvalue()


def test_read_backup_accepts_v2_and_never_claims_to_include_key() -> None:
    bundle = read_backup(make_backup())
    assert bundle.schema_version == 2
    assert bundle.summary() == {
        "schema_version": 2,
        "topics": 0,
        "sources": 0,
        "articles": 0,
        "runs": 0,
        "reports": 0,
        "deepseek_api_key_included": False,
    }


def test_read_backup_promotes_v1_report_run() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "configuration.json",
            json.dumps({"schema_version": 1, "settings": {}, "topics": [], "sources": []}),
        )
        archive.writestr("history.json", json.dumps({"articles": []}))
        archive.writestr(
            "reports/run-a.json",
            json.dumps({"schema_version": 1, "run": {"id": "run-a"}, "articles": []}),
        )
    bundle = read_backup(output.getvalue())
    assert bundle.schema_version == 1
    assert bundle.runs == [{"id": "run-a"}]


def test_read_backup_does_not_allow_v2_report_to_bypass_runs_index() -> None:
    bundle = read_backup(
        make_backup(
            extra={
                "reports/unindexed.json": json.dumps(
                    {
                        "schema_version": 2,
                        "run": {"id": "unindexed-run"},
                        "articles": [],
                    }
                ).encode()
            }
        )
    )

    assert bundle.runs == []
    assert "unindexed.json" in bundle.reports


@pytest.mark.parametrize("name", ["../escape.json", "/absolute.json"])
def test_read_backup_rejects_unsafe_paths(name: str) -> None:
    with pytest.raises(BackupValidationError, match="不安全|不支持"):
        read_backup(make_backup(extra={name: b"{}"}))


def test_read_backup_rejects_unknown_schema_and_duplicate_members() -> None:
    with pytest.raises(BackupValidationError, match="不支持的备份版本"):
        read_backup(make_backup(schema=99))

    output = io.BytesIO()
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        archive.writestr("configuration.json", b"{}")
        archive.writestr("configuration.json", b"{}")
        archive.writestr("history.json", b'{"articles":[]}')
    with pytest.raises(BackupValidationError, match="重名文件"):
        read_backup(output.getvalue())


def test_read_backup_rejects_non_cockpit_zip() -> None:
    with pytest.raises(BackupValidationError, match="请选择"):
        read_backup(b"not a zip")


def test_read_backup_wraps_oversized_json_integer_as_validation_error() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", '{"schema_version":2}')
        archive.writestr(
            "configuration.json",
            '{"schema_version":' + ("9" * 5000) + ',"settings":{},"topics":[],"sources":[]}',
        )
        archive.writestr("history.json", '{"articles":[]}')
    with pytest.raises(BackupValidationError, match="configuration.json"):
        read_backup(output.getvalue())


def test_read_backup_accepts_partitioned_v2_history_and_runs() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", '{"schema_version":2}')
        archive.writestr(
            "configuration.json",
            '{"schema_version":2,"settings":{},"topics":[],"sources":[]}',
        )
        archive.writestr(
            "history.json", '{"count":1,"parts":["history/part-00001.json"]}'
        )
        archive.writestr(
            "history/part-00001.json", '{"articles":[{"id":"article-a"}]}'
        )
        archive.writestr("runs.json", '{"count":1,"parts":["runs/part-00001.json"]}')
        archive.writestr("runs/part-00001.json", '{"runs":[{"id":"run-a"}]}')
    bundle = read_backup(output.getvalue())
    assert bundle.articles == [{"id": "article-a"}]
    assert bundle.runs == [{"id": "run-a"}]


@pytest.mark.parametrize("missing_name", ["manifest.json", "runs.json"])
def test_read_backup_rejects_v2_missing_required_index(missing_name: str) -> None:
    output = io.BytesIO()
    members = {
        "manifest.json": '{"schema_version":2}',
        "configuration.json": (
            '{"schema_version":2,"settings":{},"topics":[],"sources":[]}'
        ),
        "history.json": '{"articles":[]}',
        "runs.json": '{"runs":[]}',
    }
    members.pop(missing_name)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    with pytest.raises(BackupValidationError, match=missing_name):
        read_backup(output.getvalue())


def test_read_backup_rejects_unindexed_v2_partition() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", '{"schema_version":2}')
        archive.writestr(
            "configuration.json",
            '{"schema_version":2,"settings":{},"topics":[],"sources":[]}',
        )
        archive.writestr("history.json", '{"count":0,"parts":[]}')
        archive.writestr("history/part-00001.json", '{"articles":[]}')
        archive.writestr("runs.json", '{"runs":[]}')

    with pytest.raises(BackupValidationError, match="分片清单.*不一致"):
        read_backup(output.getvalue())


def test_read_backup_rejects_v2_partition_without_declared_count() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", '{"schema_version":2}')
        archive.writestr(
            "configuration.json",
            '{"schema_version":2,"settings":{},"topics":[],"sources":[]}',
        )
        archive.writestr("history.json", '{"parts":[]}')
        archive.writestr("runs.json", '{"runs":[]}')

    with pytest.raises(BackupValidationError, match="缺少数量校验"):
        read_backup(output.getvalue())


@pytest.mark.parametrize(
    ("manifest", "configuration", "message"),
    [
        ("[]", '{"schema_version":2,"settings":{},"topics":[],"sources":[]}', "清单"),
        (
            '{"schema_version":true}',
            '{"schema_version":true,"settings":{},"topics":[],"sources":[]}',
            "不支持",
        ),
        (
            '{"schema_version":2}',
            '{"schema_version":1,"settings":{},"topics":[],"sources":[]}',
            "版本不一致",
        ),
    ],
)
def test_read_backup_rejects_malformed_or_mismatched_manifest(
    manifest: str, configuration: str, message: str
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("configuration.json", configuration)
        archive.writestr("history.json", '{"articles":[]}')
    with pytest.raises(BackupValidationError, match=message):
        read_backup(output.getvalue())
