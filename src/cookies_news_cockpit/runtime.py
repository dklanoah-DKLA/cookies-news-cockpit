from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path, user_log_path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    runs: Path
    logs: Path

    def ensure(self) -> AppPaths:
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        return self


def resolve_paths(home: str | Path | None = None) -> AppPaths:
    """Resolve writable runtime locations.

    ``COOKIES_NEWS_COCKPIT_HOME`` is intentionally supported for portable
    development and tests. Packaged macOS builds use platformdirs, resulting
    in ``~/Library/Application Support/Cookies News Cockpit`` and
    ``~/Library/Logs/Cookies News Cockpit``.
    """

    override = home or os.getenv("COOKIES_NEWS_COCKPIT_HOME")
    if override:
        root = Path(override).expanduser().resolve()
        logs = root / "logs"
    else:
        root = Path(user_data_path("Cookies News Cockpit", appauthor=False))
        logs = Path(user_log_path("Cookies News Cockpit", appauthor=False))
    return AppPaths(
        root=root,
        database=root / "cockpit.sqlite3",
        runs=root / "runs",
        logs=logs,
    ).ensure()
