"""Strict project-local browser configuration."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from xhs_operations_core.paths import ProjectPathError, resolve_project_relative


class BrowserConfigError(ValueError):
    pass


def _strings(name: str, values: Any, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise BrowserConfigError(f"{name} must be an array")
    result = tuple(item.strip() for item in values if isinstance(item, str) and item.strip())
    if len(result) != len(values):
        raise BrowserConfigError(f"{name} must contain non-empty strings")
    if not allow_empty and not result:
        raise BrowserConfigError(f"{name} cannot be empty")
    if len(set(result)) != len(result):
        raise BrowserConfigError(f"{name} cannot contain duplicates")
    return result


@dataclass(frozen=True)
class LoginSignature:
    positive_selectors: tuple[str, ...]
    positive_texts: tuple[str, ...]
    negative_selectors: tuple[str, ...]
    negative_texts: tuple[str, ...]
    risk_texts: tuple[str, ...]
    calibration_status: str

    def fingerprint(self) -> str:
        payload = {
            "positive_selectors": self.positive_selectors,
            "positive_texts": self.positive_texts,
            "negative_selectors": self.negative_selectors,
            "negative_texts": self.negative_texts,
            "risk_texts": self.risk_texts,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LoginSignature":
        allowed = {
            "positive_selectors",
            "positive_texts",
            "negative_selectors",
            "negative_texts",
            "risk_texts",
            "calibration_status",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise BrowserConfigError(f"unknown login signature fields: {sorted(unknown)}")
        calibration = payload.get("calibration_status")
        if calibration not in {"donor_verified_needs_recheck", "locally_verified"}:
            raise BrowserConfigError("unsupported login signature calibration_status")
        positive_selectors = _strings(
            "positive_selectors", payload.get("positive_selectors", []), allow_empty=True
        )
        positive_texts = _strings(
            "positive_texts", payload.get("positive_texts", []), allow_empty=True
        )
        if not positive_selectors and not positive_texts:
            raise BrowserConfigError("login signature requires positive evidence")
        return cls(
            positive_selectors=positive_selectors,
            positive_texts=positive_texts,
            negative_selectors=_strings(
                "negative_selectors", payload.get("negative_selectors", []), allow_empty=True
            ),
            negative_texts=_strings(
                "negative_texts", payload.get("negative_texts", []), allow_empty=True
            ),
            risk_texts=_strings("risk_texts", payload.get("risk_texts", [])),
            calibration_status=calibration,
        )


@dataclass(frozen=True)
class BrowserConfig:
    schema_version: int
    account_id: str
    profile_name: str
    channel: str
    headless: bool
    homepage_url: str
    timeout_seconds: int
    slow_mo_ms: int
    fixture_only: bool
    allow_platform_access: bool
    login_signature: LoginSignature

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise BrowserConfigError("browser schema_version must be 1")
        for name, value in (("account_id", self.account_id), ("profile_name", self.profile_name)):
            if not isinstance(value, str) or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value
            ) is None:
                raise BrowserConfigError(f"{name} must be a safe 1-64 character id")
        if self.channel not in {"chrome", "chromium"}:
            raise BrowserConfigError("browser channel must be chrome or chromium")
        if self.headless is not False:
            raise BrowserConfigError("headless browser is not allowed")
        parsed = urlparse(self.homepage_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise BrowserConfigError("homepage_url must be a query-free Xiaohongshu HTTPS URL")
        if type(self.timeout_seconds) is not int or not 3 <= self.timeout_seconds <= 120:
            raise BrowserConfigError("timeout_seconds must be an integer between 3 and 120")
        if type(self.slow_mo_ms) is not int or not 0 <= self.slow_mo_ms <= 5000:
            raise BrowserConfigError("slow_mo_ms must be an integer between 0 and 5000")
        if type(self.fixture_only) is not bool or type(self.allow_platform_access) is not bool:
            raise BrowserConfigError("fixture_only and allow_platform_access must be booleans")
        if self.fixture_only and self.allow_platform_access:
            raise BrowserConfigError("fixture config cannot allow platform access")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BrowserConfig":
        allowed = {
            "schema_version",
            "account_id",
            "profile_name",
            "channel",
            "headless",
            "homepage_url",
            "timeout_seconds",
            "slow_mo_ms",
            "fixture_only",
            "allow_platform_access",
            "login_signature",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise BrowserConfigError(f"unknown browser config fields: {sorted(unknown)}")
        try:
            values = dict(payload)
            values["login_signature"] = LoginSignature.from_dict(values["login_signature"])
            return cls(**values)
        except (KeyError, TypeError) as exc:
            raise BrowserConfigError(f"invalid browser config: {exc}") from exc

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "profile_name": self.profile_name,
            "channel": self.channel,
            "headless": self.headless,
            "homepage_url": self.homepage_url,
            "timeout_seconds": self.timeout_seconds,
            "slow_mo_ms": self.slow_mo_ms,
            "fixture_only": self.fixture_only,
            "allow_platform_access": self.allow_platform_access,
            "calibration_status": self.login_signature.calibration_status,
        }


def load_browser_config(root: Path, relative_path: str | Path) -> tuple[BrowserConfig, Path]:
    try:
        path = resolve_project_relative(root, str(relative_path), field_name="browser_config")
    except ProjectPathError as exc:
        raise BrowserConfigError(str(exc)) from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BrowserConfigError(f"browser config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BrowserConfigError(f"invalid browser config JSON at line {exc.lineno}") from exc
    if not isinstance(payload, dict):
        raise BrowserConfigError("browser config root must be an object")
    return BrowserConfig.from_dict(payload), path
