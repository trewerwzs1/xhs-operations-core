"""Offline local-profile readiness checks for the Run Agent execution path."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path

from .config import BrowserConfig
from .login import load_calibration_status
from .profiles import BrowserProfileManager


@dataclass(frozen=True)
class BrowserReadinessReport:
    ok: bool
    stage: str
    run_agent_dependency_installed: bool
    profile_initialized: bool
    platform_access_enabled: bool
    calibration_valid: bool
    next_action: str
    findings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def check_browser_readiness(
    *,
    runtime_dir: Path,
    manager: BrowserProfileManager,
    config: BrowserConfig,
    checked_at: str,
) -> BrowserReadinessReport:
    """Check local prerequisites without opening Chrome or accessing Xiaohongshu."""

    try:
        run_agent_dependency_installed = find_spec("websockets.sync.client") is not None
    except ModuleNotFoundError:
        run_agent_dependency_installed = False
    profile_path = manager.profile_path(config)
    marker = profile_path / ".xhs-operations-core-profile.json"
    profile_initialized = profile_path.is_dir() and marker.is_file()
    platform_access_enabled = not config.fixture_only and config.allow_platform_access
    calibration_valid = load_calibration_status(
        runtime_dir,
        config,
        checked_at=checked_at,
    )

    findings: list[str] = []
    if not run_agent_dependency_installed:
        findings.append("run_agent_dependency_missing")
    if not profile_initialized:
        findings.append("profile_not_initialized")
    if not platform_access_enabled:
        findings.append("platform_access_not_enabled")
    if not calibration_valid:
        findings.append("login_calibration_missing_or_expired")

    if not run_agent_dependency_installed:
        stage = "dependency_setup"
        next_action = "install_project_dependencies"
    elif not profile_initialized:
        stage = "profile_setup"
        next_action = "profile_init"
    elif not platform_access_enabled:
        stage = "configuration"
        next_action = "create_non_fixture_local_config"
    elif not calibration_valid:
        stage = "manual_authorization"
        next_action = "run_agent_connection_check"
    else:
        stage = "readonly_ready"
        next_action = "login_check"

    return BrowserReadinessReport(
        ok=not findings,
        stage=stage,
        run_agent_dependency_installed=run_agent_dependency_installed,
        profile_initialized=profile_initialized,
        platform_access_enabled=platform_access_enabled,
        calibration_valid=calibration_valid,
        next_action=next_action,
        findings=tuple(findings),
    )
