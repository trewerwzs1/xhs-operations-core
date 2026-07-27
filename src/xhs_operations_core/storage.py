"""Durable local JSON and JSONL storage primitives."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar


T = TypeVar("T")
_MISSING = object()
_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}


class StorageError(RuntimeError):
    """Base class for durable storage failures."""


class StorageCorruptionError(StorageError):
    """Raised when persisted JSON cannot be decoded safely."""


class StorageSerializationError(StorageError):
    """Raised when a value is not JSON serializable."""


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.RLock())


@contextmanager
def _os_file_lock(lock_path: Path) -> Iterator[None]:
    """Hold a blocking cross-process lock using only the standard library."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)

        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Serialize writers across threads and processes using a sidecar lock."""

    target = Path(path)
    lock_path = target.with_name(f"{target.name}.lock")
    thread_lock = _thread_lock_for(lock_path)
    with thread_lock:
        with _os_file_lock(lock_path):
            yield


def _serialize(value: Any, *, pretty: bool) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise StorageSerializationError(f"value is not JSON serializable: {exc}") from exc


def _write_json_atomic_unlocked(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _serialize(value, pretty=True) + "\n"
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_json_atomic(path: str | Path, value: Any) -> Path:
    """Atomically replace one JSON document after durable file flush."""

    target = Path(path)
    with file_lock(target):
        _write_json_atomic_unlocked(target, value)
    return target


def read_json(path: str | Path, *, default: T | object = _MISSING) -> Any | T:
    """Read one JSON document, distinguishing missing from corrupt state."""

    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return default
    except OSError as exc:
        raise StorageError(f"unable to read JSON state: {target}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StorageCorruptionError(
            f"corrupt JSON state at {target}: line {exc.lineno}, column {exc.colno}"
        ) from exc


def update_json_object(
    path: str | Path,
    updater: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Atomically read, update, and replace a JSON object under one lock."""

    target = Path(path)
    with file_lock(target):
        current = read_json(target, default={})
        if not isinstance(current, dict):
            raise StorageCorruptionError(f"expected JSON object at {target}")
        updated = updater(dict(current))
        if not isinstance(updated, dict):
            raise StorageSerializationError("JSON object updater must return a dict")
        _write_json_atomic_unlocked(target, updated)
        return updated


def append_jsonl(path: str | Path, record: dict[str, Any]) -> Path:
    """Append one complete JSON object line with flush and writer locking."""

    if not isinstance(record, dict):
        raise StorageSerializationError("JSONL record must be a dict")
    target = Path(path)
    line = _serialize(record, pretty=False) + "\n"
    with file_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError(f"unable to append JSONL state: {target}") from exc
    return target


def read_jsonl(path: str | Path, *, missing_ok: bool = True) -> list[dict[str, Any]]:
    """Read JSONL records and fail with the exact corrupt line number."""

    target = Path(path)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        if missing_ok:
            return []
        raise
    except OSError as exc:
        raise StorageError(f"unable to read JSONL state: {target}") from exc

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StorageCorruptionError(
                f"corrupt JSONL at {target}: line {line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise StorageCorruptionError(
                f"expected JSON object at {target}: line {line_number}"
            )
        records.append(value)
    return records
