"""Idempotent local-user initialization that always starts with STOP enabled."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import re

from .config import load_project_config
from .platform.browser import BrowserProfileManager
from .platform.browser.config import BrowserConfig
from .storage import read_json, write_json_atomic


class SetupError(ValueError):
    pass


@dataclass(frozen=True)
class SetupResult:
    account_id: str
    profile_name: str
    project_config_path: str
    browser_config_path: str
    browser_profile_path: str
    stop_path: str
    platform_access_allowed: bool
    writes_allowed: bool
    platform_actions_executed: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _safe(name: str, value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value or "") is None:
        raise SetupError(f"{name} must be a safe 1-64 character ID")
    return value


def _write_idempotent(path: Path, value: dict[str, object]) -> None:
    if path.exists():
        if read_json(path) != value:
            raise SetupError(f"existing local file differs and will not be overwritten: {path}")
        return
    write_json_atomic(path, value)


def initialize_user_project(root: Path, *, account_id: str, profile_name: str) -> SetupResult:
    root = Path(root).resolve()
    account_id = _safe("account_id", account_id)
    profile_name = _safe("profile_name", profile_name)
    project_payload = json.loads((root / "config" / "project.example.json").read_text(encoding="utf-8"))
    project_path = root / "config" / "project.local.json"
    _write_idempotent(project_path, project_payload)
    example = json.loads((root / "config" / "browser.example.json").read_text(encoding="utf-8"))
    browser_payload = {
        **example,
        "account_id": account_id,
        "profile_name": profile_name,
        "fixture_only": False,
        "allow_platform_access": False,
    }
    browser_path = root / "config" / "browser.local.json"
    _write_idempotent(browser_path, browser_payload)
    project_config, _ = load_project_config(root)
    project_config.runtime.ensure_runtime_dirs()
    browser_config = BrowserConfig.from_dict(browser_payload)
    profile_path = BrowserProfileManager(project_config.runtime.browser_profiles_dir).initialize(browser_config)
    stop_path = project_config.runtime.runtime_dir / "comment_flow" / "STOP.json"
    _write_idempotent(stop_path, {
        "reason": "fresh_install_requires_login_and_explicit_write_enable",
        "writes_allowed": False,
    })
    _write_idempotent(project_config.runtime.runtime_dir / "setup" / "receipt.json", {
        "schema_version": 1,
        "account_id": account_id,
        "profile_name": profile_name,
        "project_config": str(project_path.relative_to(root)),
        "browser_config": str(browser_path.relative_to(root)),
        "stop_enabled": True,
        "platform_access_allowed": False,
        "platform_actions_executed": 0,
    })
    return SetupResult(
        account_id, profile_name, str(project_path), str(browser_path), str(profile_path),
        str(stop_path), False, False, 0,
    )


def register_existing_user_project(
    root: Path, *, account_id: str, profile_name: str
) -> dict[str, object]:
    """Create an upgrade receipt without changing existing local configuration."""
    root = Path(root).resolve()
    account_id = _safe("account_id", account_id)
    profile_name = _safe("profile_name", profile_name)
    project_path = root / "config" / "project.local.json"
    browser_path = root / "config" / "browser.local.json"
    project_payload = read_json(project_path)
    browser_payload = read_json(browser_path)
    if not isinstance(project_payload, dict) or not isinstance(browser_payload, dict):
        raise SetupError("existing project and browser local configuration are required")
    if (
        browser_payload.get("account_id") != account_id
        or browser_payload.get("profile_name") != profile_name
    ):
        raise SetupError("existing browser configuration belongs to another account or profile")
    project_config, _ = load_project_config(root)
    project_config.runtime.ensure_runtime_dirs()
    stop_path = project_config.runtime.runtime_dir / "comment_flow" / "STOP.json"
    stop_payload = read_json(stop_path)
    if not isinstance(stop_payload, dict) or stop_payload.get("writes_allowed") is not False:
        raise SetupError("existing installation requires STOP with writes_allowed=false")
    receipt_path = project_config.runtime.runtime_dir / "setup" / "receipt.json"
    receipt = {
        "schema_version": 1,
        "installation_mode": "upgrade_existing",
        "account_id": account_id,
        "profile_name": profile_name,
        "project_config": str(project_path.relative_to(root)),
        "browser_config": str(browser_path.relative_to(root)),
        "stop_enabled": True,
        "platform_access_allowed": browser_payload.get("allow_platform_access") is True,
        "platform_actions_executed": 0,
    }
    if receipt_path.exists():
        existing = read_json(receipt_path)
        if not isinstance(existing, dict):
            raise SetupError("existing setup receipt is invalid")
        if (
            existing.get("account_id") != account_id
            or existing.get("profile_name") != profile_name
            or existing.get("platform_actions_executed") != 0
        ):
            raise SetupError("existing setup receipt does not match this account/profile")
        return existing
    write_json_atomic(receipt_path, receipt)
    return receipt
