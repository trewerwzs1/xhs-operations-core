"""Project-root discovery and safe local path resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_MARKERS = ("pyproject.toml", "AGENTS.md")


class ProjectPathError(ValueError):
    """Raised when a configured project path is unsafe or invalid."""


def find_project_root(start: str | Path | None = None) -> Path:
    """Find the nearest parent containing the project markers."""

    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if all((candidate / marker).is_file() for marker in PROJECT_MARKERS):
            return candidate
    markers = ", ".join(PROJECT_MARKERS)
    raise ProjectPathError(f"project root not found from {current}; expected {markers}")


def resolve_project_relative(root: Path, value: str, *, field_name: str) -> Path:
    """Resolve a configured relative path and keep it inside the project root."""

    raw = Path(value)
    if raw.is_absolute():
        raise ProjectPathError(f"{field_name} must be relative to the project root")

    resolved_root = root.resolve()
    resolved = (resolved_root / raw).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ProjectPathError(f"{field_name} escapes the project root") from exc
    return resolved


@dataclass(frozen=True)
class ProjectPaths:
    """Resolved project and local runtime paths."""

    root: Path
    config_dir: Path
    runtime_dir: Path
    logs_dir: Path
    reports_dir: Path
    browser_profiles_dir: Path

    @classmethod
    def from_root(
        cls,
        root: str | Path,
        *,
        runtime_dir: str = "data/runtime",
        logs_dir: str = "logs",
        reports_dir: str = "reports/runtime",
        browser_profiles_dir: str = "browser-profiles",
    ) -> "ProjectPaths":
        resolved_root = Path(root).expanduser().resolve()
        if not resolved_root.is_dir():
            raise ProjectPathError(f"project root does not exist: {resolved_root}")

        return cls(
            root=resolved_root,
            config_dir=resolved_root / "config",
            runtime_dir=resolve_project_relative(
                resolved_root, runtime_dir, field_name="runtime_dir"
            ),
            logs_dir=resolve_project_relative(resolved_root, logs_dir, field_name="logs_dir"),
            reports_dir=resolve_project_relative(
                resolved_root, reports_dir, field_name="reports_dir"
            ),
            browser_profiles_dir=resolve_project_relative(
                resolved_root,
                browser_profiles_dir,
                field_name="browser_profiles_dir",
            ),
        )

    def ensure_runtime_dirs(self) -> tuple[Path, ...]:
        """Create only the writable runtime directories used by the product."""

        directories = (self.runtime_dir, self.logs_dir, self.reports_dir)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        return directories
