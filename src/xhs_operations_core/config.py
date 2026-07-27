"""Project configuration loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import ProjectPathError, ProjectPaths, resolve_project_relative


DEFAULT_CONFIG_PATH = "config/project.example.json"
LOCAL_CONFIG_PATH = "config/project.local.json"
SUPPORTED_SCHEMA_VERSION = 1


class ConfigError(ValueError):
    """Raised when the local project configuration is invalid."""


@dataclass(frozen=True)
class ProjectConfig:
    schema_version: int
    project_name: str
    runtime: ProjectPaths
    required_runtime: str

    @classmethod
    def from_dict(cls, root: Path, payload: dict[str, Any]) -> "ProjectConfig":
        schema_version = payload.get("schema_version")
        if schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ConfigError(
                f"schema_version must be {SUPPORTED_SCHEMA_VERSION}, got {schema_version!r}"
            )

        project_name = payload.get("project_name")
        if not isinstance(project_name, str) or not project_name.strip():
            raise ConfigError("project_name must be a non-empty string")

        codex = payload.get("codex")
        if not isinstance(codex, dict):
            raise ConfigError("codex must be an object")
        required_runtime = codex.get("required_runtime")
        if required_runtime != "desktop":
            raise ConfigError("codex.required_runtime must be 'desktop'")

        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            raise ConfigError("runtime must be an object")

        allowed_runtime_keys = {
            "runtime_dir",
            "logs_dir",
            "reports_dir",
            "browser_profiles_dir",
        }
        unknown = set(runtime) - allowed_runtime_keys
        if unknown:
            raise ConfigError(f"unknown runtime fields: {', '.join(sorted(unknown))}")

        for key, value in runtime.items():
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"runtime.{key} must be a non-empty relative path")

        try:
            paths = ProjectPaths.from_root(root, **runtime)
        except ProjectPathError as exc:
            raise ConfigError(str(exc)) from exc

        return cls(
            schema_version=schema_version,
            project_name=project_name.strip(),
            runtime=paths,
            required_runtime=required_runtime,
        )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"unable to read config: {path}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: line {exc.lineno}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"config root must be an object: {path}")
    return payload


def select_config_path(root: Path, config_path: str | Path | None = None) -> Path:
    if config_path is not None:
        candidate = str(Path(config_path).expanduser())
        try:
            return resolve_project_relative(root, candidate, field_name="config_path")
        except ProjectPathError as exc:
            raise ConfigError(str(exc)) from exc

    local = root / LOCAL_CONFIG_PATH
    if local.is_file():
        return local
    return root / DEFAULT_CONFIG_PATH


def load_project_config(
    root: str | Path,
    config_path: str | Path | None = None,
) -> tuple[ProjectConfig, Path]:
    resolved_root = Path(root).expanduser().resolve()
    selected = select_config_path(resolved_root, config_path)
    if not selected.is_file():
        raise ConfigError(f"config file not found: {selected}")
    return ProjectConfig.from_dict(resolved_root, _read_json(selected)), selected
