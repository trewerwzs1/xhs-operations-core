"""Dedicated profile initialization and exclusive session leases."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from xhs_operations_core.storage import file_lock, read_json, write_json_atomic

from .config import BrowserConfig


class BrowserProfileError(RuntimeError):
    pass


class BrowserProfileInUseError(BrowserProfileError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class BrowserProfileManager:
    def __init__(self, browser_profiles_dir: str | Path) -> None:
        self.root = Path(browser_profiles_dir)

    def profile_path(self, config: BrowserConfig) -> Path:
        return self.root / config.profile_name

    def initialize(self, config: BrowserConfig) -> Path:
        path = self.profile_path(config)
        marker = path / ".xhs-operations-core-profile.json"
        if path.exists() and any(path.iterdir()) and not marker.is_file():
            raise BrowserProfileError(
                "existing non-empty directory is not an XHS Operations Core dedicated profile"
            )
        path.mkdir(parents=True, exist_ok=True)
        expected = {
            "schema_version": 1,
            "account_id": config.account_id,
            "profile_name": config.profile_name,
        }
        if marker.is_file():
            current = read_json(marker)
            if current != expected:
                raise BrowserProfileError("profile marker does not match browser config")
        else:
            write_json_atomic(marker, expected)
        return path

    @contextmanager
    def lease(self, config: BrowserConfig, *, run_id: str) -> Iterator[Path]:
        path = self.initialize(config)
        lease_path = path / ".xhs-operations-core-lease.json"
        guard_path = path / ".xhs-operations-core-lease-guard"
        lease_id = uuid4().hex
        payload = {"lease_id": lease_id, "pid": os.getpid(), "run_id": run_id}

        with file_lock(guard_path):
            if lease_path.exists():
                try:
                    existing = read_json(lease_path)
                except Exception as exc:
                    raise BrowserProfileInUseError("profile lease is unreadable") from exc
                pid = existing.get("pid") if isinstance(existing, dict) else None
                if type(pid) is int and _pid_alive(pid):
                    raise BrowserProfileInUseError("browser profile is already in use")
                lease_path.unlink(missing_ok=True)
            try:
                descriptor = os.open(
                    lease_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError as exc:
                raise BrowserProfileInUseError("unable to acquire browser profile lease") from exc
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

        try:
            yield path
        finally:
            with file_lock(guard_path):
                try:
                    current = read_json(lease_path)
                except Exception:
                    current = None
                if isinstance(current, dict) and current.get("lease_id") == lease_id:
                    lease_path.unlink(missing_ok=True)
