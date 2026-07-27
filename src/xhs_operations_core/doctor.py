"""Local environment diagnostics for XHS Operations Core."""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ConfigError, load_project_config
from .paths import ProjectPathError, find_project_root
from .storage import StorageError, read_json, write_json_atomic


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    project_root: str | None
    config_path: str | None
    checks: tuple[DoctorCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "project_root": self.project_root,
            "config_path": self.config_path,
            "checks": [asdict(check) for check in self.checks],
        }


def _is_writable_or_creatable(path: Path) -> bool:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return existing.is_dir() and os.access(existing, os.W_OK)


def run_doctor(
    *,
    start: str | Path | None = None,
    config_path: str | Path | None = None,
    init_runtime: bool = False,
) -> DoctorReport:
    checks: list[DoctorCheck] = []
    python_ok = sys.version_info >= (3, 11)
    checks.append(
        DoctorCheck(
            "python_version",
            python_ok,
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )

    try:
        root = find_project_root(start)
    except ProjectPathError as exc:
        checks.append(DoctorCheck("project_root", False, str(exc)))
        return DoctorReport(False, None, None, tuple(checks))
    checks.append(DoctorCheck("project_root", True, str(root)))

    try:
        config, selected = load_project_config(root, config_path)
    except ConfigError as exc:
        checks.append(DoctorCheck("project_config", False, str(exc)))
        return DoctorReport(False, str(root), None, tuple(checks))
    checks.append(DoctorCheck("project_config", True, str(selected)))

    runtime_paths = (
        ("runtime_dir", config.runtime.runtime_dir),
        ("logs_dir", config.runtime.logs_dir),
        ("reports_dir", config.runtime.reports_dir),
    )
    if init_runtime:
        try:
            config.runtime.ensure_runtime_dirs()
        except OSError as exc:
            checks.append(DoctorCheck("runtime_init", False, str(exc)))
        else:
            checks.append(DoctorCheck("runtime_init", True, "runtime directories ready"))

        storage_probe = config.runtime.runtime_dir / ".doctor-storage-probe.json"
        try:
            write_json_atomic(storage_probe, {"ok": True})
            probe_value = read_json(storage_probe)
            storage_probe.unlink(missing_ok=True)
            if probe_value != {"ok": True}:
                raise StorageError("storage probe readback mismatch")
        except (OSError, StorageError) as exc:
            checks.append(DoctorCheck("atomic_storage", False, str(exc)))
        else:
            checks.append(DoctorCheck("atomic_storage", True, "atomic JSON read/write ready"))

    for name, path in runtime_paths:
        ok = path.is_dir() if init_runtime else _is_writable_or_creatable(path)
        checks.append(DoctorCheck(f"writable:{name}", ok, str(path)))

    checks.append(
        DoctorCheck(
            "browser_profiles_path",
            _is_writable_or_creatable(config.runtime.browser_profiles_dir),
            str(config.runtime.browser_profiles_dir),
        )
    )

    return DoctorReport(
        ok=all(check.ok for check in checks),
        project_root=str(root),
        config_path=str(selected),
        checks=tuple(checks),
    )
