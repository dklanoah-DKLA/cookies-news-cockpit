"""Validated backup bundles for cross-Mac migration.

Backups are data, never instructions.  This module deliberately accepts only
the small JSON surface emitted by Cookies News Cockpit and rejects archive
features that could write files or exhaust local storage.
"""

from __future__ import annotations

import io
import json
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 256
MAX_COMPRESSION_RATIO = 200
MAX_TOP_LEVEL_ROWS = 50_000


class BackupValidationError(ValueError):
    """The supplied backup is unsafe, corrupt, or unsupported."""


@dataclass(frozen=True, slots=True)
class BackupBundle:
    schema_version: int
    configuration: dict[str, Any]
    articles: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    reports: dict[str, dict[str, Any]]

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "topics": len(self.configuration["topics"]),
            "sources": len(self.configuration["sources"]),
            "articles": len(self.articles),
            "runs": len(self.runs),
            "reports": len(self.reports),
            "deepseek_api_key_included": False,
        }


def _reject_constant(value: str) -> None:
    raise BackupValidationError(f"JSON 包含无效数字：{value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BackupValidationError(f"JSON 包含重复字段：{key}")
        value[key] = item
    return value


def _read_json(archive: zipfile.ZipFile, name: str) -> Any:
    try:
        payload = archive.read(name)
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except BackupValidationError:
        raise
    except (
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        raise BackupValidationError(f"无法读取备份中的 {name}") from exc


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BackupValidationError("备份包含不安全的文件路径")
    if info.flag_bits & 0x1:
        raise BackupValidationError("不支持加密 ZIP")
    unix_mode = info.external_attr >> 16
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise BackupValidationError("备份不能包含符号链接")
    if info.is_dir():
        return
    allowed = name in {
        "manifest.json",
        "configuration.json",
        "history.json",
        "runs.json",
    } or (
        name.startswith(("reports/", "history/", "runs/"))
        and name.endswith(".json")
        and len(path.parts) == 2
    )
    if not allowed:
        raise BackupValidationError(f"备份包含不支持的文件：{name}")
    if info.file_size > MAX_MEMBER_BYTES:
        raise BackupValidationError(f"备份文件过大：{name}")
    if info.file_size and (
        info.compress_size == 0 or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
    ):
        raise BackupValidationError(f"备份压缩比例异常：{name}")


def _dict_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_TOP_LEVEL_ROWS:
        raise BackupValidationError(f"{field} 必须是有效列表")
    if not all(isinstance(item, dict) for item in value):
        raise BackupValidationError(f"{field} 包含无效记录")
    return value


def _read_parts(
    archive: zipfile.ZipFile,
    names: set[str],
    index: Any,
    *,
    field: str,
    folder: str,
    strict_index: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(index, dict):
        raise BackupValidationError(f"{field} 索引结构无效")
    available_parts = {
        name
        for name in names
        if name.startswith(f"{folder}/")
        and name.endswith(".json")
        and len(PurePosixPath(name).parts) == 2
    }
    direct = index.get(field)
    if direct is not None:
        if strict_index and ("parts" in index or available_parts):
            raise BackupValidationError(f"{field} 索引同时包含内嵌记录与分片")
        rows = _dict_list(direct, field=field)
        declared = index.get("count")
        if declared is not None and (type(declared) is not int or declared != len(rows)):
            raise BackupValidationError(f"{field} 数量校验失败")
        return rows
    if strict_index and "parts" not in index:
        raise BackupValidationError(f"{field} 索引缺少记录或分片清单")
    parts = index.get("parts", [])
    if not isinstance(parts, list) or not all(isinstance(item, str) for item in parts):
        raise BackupValidationError(f"{field} 分片索引无效")
    if strict_index and "count" not in index:
        raise BackupValidationError(f"{field} 分片索引缺少数量校验")
    if len(parts) != len(set(parts)):
        raise BackupValidationError(f"{field} 分片索引包含重复项")
    if strict_index and set(parts) != available_parts:
        raise BackupValidationError(f"{field} 分片清单与 ZIP 内容不一致")
    rows: list[dict[str, Any]] = []
    for name in parts:
        path = PurePosixPath(name)
        if (
            name not in names
            or not name.startswith(f"{folder}/")
            or not name.endswith(".json")
            or len(path.parts) != 2
        ):
            raise BackupValidationError(f"{field} 分片引用无效：{name}")
        payload = _read_json(archive, name)
        if not isinstance(payload, dict):
            raise BackupValidationError(f"{field} 分片结构无效：{name}")
        rows.extend(_dict_list(payload.get(field, []), field=field))
        if len(rows) > MAX_TOP_LEVEL_ROWS:
            raise BackupValidationError(f"{field} 记录数量超过安全上限")
    declared = index.get("count")
    if declared is not None and (type(declared) is not int or declared != len(rows)):
        raise BackupValidationError(f"{field} 分片数量校验失败")
    return rows


def read_backup(data: bytes) -> BackupBundle:
    """Parse and validate a schema-v1 or schema-v2 backup from raw ZIP bytes."""

    if not data or len(data) > MAX_ARCHIVE_BYTES:
        raise BackupValidationError("备份为空或超过 100 MiB 上限")
    stream = io.BytesIO(data)
    if not zipfile.is_zipfile(stream):
        raise BackupValidationError("请选择 Cookies News Cockpit 导出的 ZIP 备份")
    stream.seek(0)
    try:
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_MEMBERS:
                raise BackupValidationError("备份文件数量异常")
            names: set[str] = set()
            total_size = 0
            for info in infos:
                _validate_member(info)
                if info.filename in names:
                    raise BackupValidationError(f"备份包含重名文件：{info.filename}")
                names.add(info.filename)
                total_size += info.file_size
                if total_size > MAX_UNCOMPRESSED_BYTES:
                    raise BackupValidationError("备份解压后超过 100 MiB 上限")
            if not {"configuration.json", "history.json"} <= names:
                raise BackupValidationError("备份缺少 configuration.json 或 history.json")

            configuration = _read_json(archive, "configuration.json")
            history = _read_json(archive, "history.json")
            manifest = _read_json(archive, "manifest.json") if "manifest.json" in names else {}
            runs_payload = _read_json(archive, "runs.json") if "runs.json" in names else {}
            if not isinstance(configuration, dict) or not isinstance(history, dict):
                raise BackupValidationError("备份配置或历史结构无效")
            if not isinstance(manifest, dict) or not isinstance(runs_payload, dict):
                raise BackupValidationError("备份清单或运行记录结构无效")
            schema_version = manifest.get(
                "schema_version", configuration.get("schema_version", 1)
            )
            if type(schema_version) is not int or schema_version not in {1, 2}:
                raise BackupValidationError(f"不支持的备份版本：{schema_version}")
            configuration_schema = configuration.get("schema_version", schema_version)
            if type(configuration_schema) is not int or configuration_schema != schema_version:
                raise BackupValidationError("manifest 与 configuration 的版本不一致")
            if schema_version == 2:
                missing = {"manifest.json", "runs.json"} - names
                if missing:
                    raise BackupValidationError(
                        "v2 备份缺少 " + " 或 ".join(sorted(missing))
                    )
            settings = configuration.get("settings", {})
            if not isinstance(settings, dict):
                raise BackupValidationError("settings 必须是对象")
            configuration = {
                **configuration,
                "settings": settings,
                "topics": _dict_list(configuration.get("topics", []), field="topics"),
                "sources": _dict_list(configuration.get("sources", []), field="sources"),
            }
            articles = _read_parts(
                archive,
                names,
                history,
                field="articles",
                folder="history",
                strict_index=schema_version == 2,
            )
            runs = _read_parts(
                archive,
                names,
                runs_payload,
                field="runs",
                folder="runs",
                strict_index=schema_version == 2,
            )
            reports: dict[str, dict[str, Any]] = {}
            for name in sorted(names):
                if not name.startswith("reports/") or not name.endswith(".json"):
                    continue
                report = _read_json(archive, name)
                if not isinstance(report, dict):
                    raise BackupValidationError(f"报告结构无效：{name}")
                reports[PurePosixPath(name).name] = report
    except BackupValidationError:
        raise
    except (
        zipfile.BadZipFile,
        RuntimeError,
        OSError,
        RecursionError,
        TypeError,
        AttributeError,
    ) as exc:
        raise BackupValidationError("ZIP 备份已损坏") from exc

    # V1 did not have runs.json.  Its immutable reports contain the missing
    # run records, so promote those into the same in-memory representation.
    # V2's runs.json is authoritative and must never be bypassed by an
    # unindexed report member.
    if schema_version == 1:
        known_run_ids = {str(item.get("id", "")) for item in runs}
        for report in reports.values():
            run = report.get("run")
            if isinstance(run, dict) and str(run.get("id", "")) not in known_run_ids:
                runs.append(run)
                known_run_ids.add(str(run.get("id", "")))
    return BackupBundle(schema_version, configuration, articles, runs, reports)
