from __future__ import annotations

import errno
import json
import os
import re
import stat
import weakref
from pathlib import Path
from urllib.parse import urlsplit


class AlreadyRunningError(RuntimeError):
    """Another process owns this data directory's runtime lock."""


def _private_runtime_directory(root: Path) -> Path:
    directory = root.resolve() / ".runtime"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError("The runtime directory must be a local directory, not a symlink")
    if os.name != "nt":
        if directory.stat().st_uid != os.getuid():
            raise PermissionError("The runtime directory belongs to another user")
        directory.chmod(0o700)
    return directory


def _lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceOwner:
    """Lifetime OS lock, acquired before any startup database mutation.

    The lock file is never removed: unlinking it could allow two processes to
    lock different inodes. The OS releases ownership after a crash; PID files
    and stale timestamps never determine whether recovery may run.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = _private_runtime_directory(self.root)
        self.metadata = self.directory / "endpoint.json"
        self._fd: int | None = None
        self._finalizer: weakref.finalize | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> InstanceOwner:
        if self.held:
            raise RuntimeError("This runtime owner already holds its lock")
        lock_path = self.directory / "instance.lock"
        if lock_path.is_symlink():
            raise OSError("The runtime lock must not be a symlink")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            try:
                _lock(fd)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise AlreadyRunningError("Cookies News Cockpit 已在运行") from None
                raise
            # Windows supports locking beyond EOF. Initialize only after
            # ownership, so two simultaneous first launches cannot race a
            # write into the other process's already-locked first byte.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            self._fd = fd
            # Metadata from a crashed process can only be cleared by the next
            # proven owner. A competing launch cannot modify the active URL.
            self.metadata.unlink(missing_ok=True)
            self._finalizer = weakref.finalize(self, os.close, fd)
        except BaseException:
            self._fd = None
            os.close(fd)
            raise
        return self

    def publish_url(self, url: str) -> None:
        if not self.held:
            raise RuntimeError("Cannot publish an endpoint without runtime ownership")
        if not _valid_session_url(url):
            raise ValueError("Invalid local cockpit endpoint")
        if os.name == "nt":
            # chmod is not a Windows ACL. The Mac product can reopen its private
            # URL; Windows development launches safely decline a second owner
            # instead of persisting a session token with uncertain permissions.
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.metadata, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"url": url}, output)

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None
        try:
            self.metadata.unlink(missing_ok=True)
        finally:
            try:
                _unlock(fd)
            finally:
                os.close(fd)


def _valid_session_url(url: object) -> bool:
    if not isinstance(url, str) or len(url) > 1024:
        return False
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/"
            and not parsed.query
            and re.fullmatch(r"token=[A-Za-z0-9_-]{16,128}", parsed.fragment)
        )
    except ValueError:
        return False


def existing_session_url(root: Path) -> str | None:
    """Read only owner-private metadata after an acquisition was refused."""

    if os.name == "nt":
        return None
    directory = root.resolve() / ".runtime"
    metadata = directory / "endpoint.json"
    try:
        directory_stat = directory.lstat()
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.getuid()
            or directory_stat.st_mode & 0o077
        ):
            return None
        fd = os.open(metadata, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 4096
            ):
                return None
            payload = json.load(source)
        url = payload.get("url") if isinstance(payload, dict) else None
        return url if _valid_session_url(url) else None
    except (OSError, ValueError, UnicodeError):
        return None
