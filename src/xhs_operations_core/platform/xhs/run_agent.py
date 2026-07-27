"""Pinned Ranfang Run Agent vendor boundary for Xiaohongshu execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
import hashlib
import json
import os
import re
import random
import subprocess
import sys
import time

from ...config import load_project_config
from ...authority import ActionPermit, AuthorityContractError, ExecutionMandate
from ...storage import append_jsonl, read_json, read_jsonl, write_json_atomic
from ...unresolved_targets import (
    UNRESOLVED_TARGET_RESOLUTION_CONFIRMATION,
    UnresolvedTargetRegistry,
)
from .capabilities import (
    CapabilityAccess,
    CapabilityRegistry,
    CapabilitySurface,
    XhsCapability,
    XhsCapabilityDeniedError,
)
from .gateway import XhsOperationGateway
from .method_lock import REQUIRED_XHS_CALL_METHOD, require_approved_xhs_call_method
from .write_journal import (
    WRITE_RECONCILIATION_CONFIRMATION,
    PlatformWriteJournal,
)


class RunAgentError(RuntimeError):
    def __init__(self, message: str, *, failure_code: str = "") -> None:
        super().__init__(message)
        self.failure_code = failure_code


SENSITIVE_KEYS = {
    "xsectoken", "xsec_token", "cookie", "cookies", "web_session", "a1", "xs",
    "authorization", "requestheaders", "request_headers", "responseheaders", "response_headers",
}

FORBIDDEN_EXTENSION_PERMISSIONS = {"cookies", "webRequest", "webRequestBlocking"}
FORBIDDEN_VENDOR_FILES = {
    "extension/interceptor.js",
    "extension/netlogger.js",
    "scripts/xhs/cookies.py",
    "scripts/xhs/risk_analyzer.py",
}
READONLY_UAT_CONFIRMATION = "I_CONFIRM_RISK_COOLDOWN_AND_READONLY_UAT"
READONLY_UAT_RISK_OVERRIDE_CONFIRMATION = "I_ACCEPT_BOUNDED_READONLY_UAT_BEFORE_24H"
READONLY_UAT_REVOKE_CONFIRMATION = "I_REVOKE_READONLY_UAT"
READONLY_UAT_COOLDOWN_SECONDS = 24 * 60 * 60
READONLY_UAT_MAX_DURATION_SECONDS = 2 * 60 * 60
RISK_CLASS_TECHNICAL = "technical_fault"
RISK_CLASS_PLATFORM = "platform_risk"
EXPLICIT_PLATFORM_RISK_MARKERS = (
    "验证码",
    "安全验证",
    "操作频繁",
    "请求频繁",
    "访问频繁",
    "异常登录",
    "登录异常",
    "账号存在风险",
    "账号风险",
    "功能受限",
    "禁言",
    "违反社区规范",
    "行为异常",
)
EXTENSION_ENROLL_CONFIRMATION = "I_ENROLL_CURRENT_XHS_OPERATIONS_CORE_EXTENSION_INSTANCE"
PLATFORM_ACCOUNT_ENROLLMENT_CONFIRMATION = "I_CONFIRM_VISIBLE_PLATFORM_ACCOUNT_ENROLLMENT"
BOUNDED_WRITE_UAT_CONFIRMATION = "I_CONFIRM_EXACT_BOUNDED_WRITE_UAT"
BOUNDED_WRITE_UAT_REVOKE_CONFIRMATION = "I_REVOKE_BOUNDED_WRITE_UAT"
VISIBLE_OBSERVATION_MIN_SECONDS = 10.0
VISIBLE_OBSERVATION_MAX_SECONDS = 15.0

ACTION_KIND_BY_WRITE_OPERATION = {
    "like_current_feed": frozenset({"engage_note_like"}),
    "post_comment_current": frozenset({"engage_note_comment"}),
    "like_current_comment": frozenset({"engage_comment_like"}),
    "reply_current_comment": frozenset(
        {"engage_comment_reply", "service_comment_reply"}
    ),
    "send_current_dm_message": frozenset(
        {"engage_single_dm", "service_dm_reply"}
    ),
}


def _directory_sha256(root: Path) -> str:
    """Hash relative paths and bytes so staged-extension drift is detectable."""
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return value


def sanitize_run_agent_output(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "").replace("_", "")
            if normalized in {name.replace("-", "").replace("_", "") for name in SENSITIVE_KEYS}:
                continue
            clean[key] = sanitize_run_agent_output(item)
        return clean
    if isinstance(value, list):
        return [sanitize_run_agent_output(item) for item in value]
    if isinstance(value, str):
        return _safe_url(value)
    return value


def _find_comment_identity(
    detail: dict[str, Any], comment_id: str
) -> tuple[str, str, str]:
    """Return exact visible comment user ID, nickname and normalized text."""
    rows = detail.get("comments")
    if not isinstance(rows, list):
        raise RunAgentError("current note has no valid visible comment list")

    def visit(items: list[object]) -> tuple[str, str, str] | None:
        for raw in items:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("id") or "")
            if raw_id == comment_id:
                user = raw.get("user")
                if not isinstance(user, dict):
                    user = raw.get("userInfo")
                if not isinstance(user, dict):
                    raise RunAgentError("exact comment author identity is missing")
                user_id = str(user.get("userId") or "").strip()
                nickname = str(user.get("nickname") or user.get("nickName") or "").strip()
                text = " ".join(str(raw.get("content") or "").split())
                if not user_id or not nickname or not text:
                    raise RunAgentError("exact comment context is incomplete")
                return user_id, nickname, text
            nested = raw.get("subComments")
            if isinstance(nested, list):
                found = visit(nested)
                if found is not None:
                    return found
        return None

    found = visit(rows)
    if found is None:
        raise RunAgentError("exact comment target is not present in the current visible thread")
    return found


def _comment_context_hash(
    *, note_id: str, comment_id: str, commenter: str, text: str
) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(
        f"{note_id}\n{comment_id}\n{commenter.strip()}\n{normalized}".encode("utf-8")
    ).hexdigest()


def has_explicit_platform_risk(value: object) -> bool:
    """Return true only for an explicit account/platform safety signal.

    Navigation failures, /404 pages, QR/login gates and identity mismatches are
    technical or content-availability failures. They still fail the current
    session closed, but they do not start the long platform-risk cooldown.
    """
    if isinstance(value, (list, tuple, set)):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value or "")
    return any(marker in text for marker in EXPLICIT_PLATFORM_RISK_MARKERS)


def _visible_observation_delay_seconds(
    *, fixture_mode: bool = False, fixture_delay_ms: float | None = None
) -> float:
    """Return a product-layer observation delay without consulting process env.

    Test acceleration is available only through an explicit fixture-mode
    constructor argument.  The real vendor subprocess never receives that test
    setting.
    """
    if fixture_delay_ms is not None and not fixture_mode:
        raise RunAgentError("fixture delay requires explicit fixture mode")
    if fixture_mode:
        if fixture_delay_ms is None:
            raise RunAgentError("fixture mode requires an explicit fixture delay")
        if (
            isinstance(fixture_delay_ms, bool)
            or not isinstance(fixture_delay_ms, (int, float))
            or fixture_delay_ms < 0
        ):
            raise RunAgentError("fixture delay must be a non-negative number")
        return float(fixture_delay_ms) / 1000.0
    return random.uniform(VISIBLE_OBSERVATION_MIN_SECONDS, VISIBLE_OBSERVATION_MAX_SECONDS)


@dataclass(frozen=True)
class RunAgentVendorStatus:
    source_release: str
    archive_sha256: str
    execution_enabled: bool
    hardening_status: str
    known_blockers: tuple[str, ...]
    capability_limits: tuple[str, ...]
    extension_build_id: str
    read_only_capability_enabled: bool
    bounded_write_uat_capability_enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": REQUIRED_XHS_CALL_METHOD,
            "source_release": self.source_release,
            "archive_sha256": self.archive_sha256,
            "execution_enabled": self.execution_enabled,
            "hardening_status": self.hardening_status,
            "known_blockers": list(self.known_blockers),
            "capability_limits": list(self.capability_limits),
            "extension_build_id": self.extension_build_id,
            "read_only_capability_enabled": self.read_only_capability_enabled,
            "bounded_write_uat_capability_enabled": self.bounded_write_uat_capability_enabled,
        }


class RunAgentClient:
    """Own the vendor boundary; no live invocation is allowed before hardening approval."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        fixture_mode: bool = False,
        fixture_delay_ms: float | None = None,
        mandate_id: str = "",
        action_permit_id: str = "",
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.vendor_root = self.project_root / "vendor" / "xiaohongshu-skills"
        self.manifest_path = self.vendor_root / "XHS_OPERATIONS_CORE_VENDOR_MANIFEST.json"
        self._token_by_feed: dict[str, str] = {}
        self._fixture_mode = fixture_mode
        self._fixture_delay_ms = fixture_delay_ms
        self._mandate_id = str(mandate_id or "").strip()
        self._action_permit_id = str(action_permit_id or "").strip()
        if self._mandate_id and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", self._mandate_id
        ) is None:
            raise RunAgentError("mandate_id is invalid")
        if self._action_permit_id and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", self._action_permit_id
        ) is None:
            raise RunAgentError("action_permit_id is invalid")
        # Validate fixture settings at construction, before any connection.
        if fixture_mode or fixture_delay_ms is not None:
            _visible_observation_delay_seconds(
                fixture_mode=fixture_mode, fixture_delay_ms=fixture_delay_ms
            )
        self._capability_registry = CapabilityRegistry.product()
        self._gateway = XhsOperationGateway(
            registry=self._capability_registry,
            transport=self._invoke_authorized,
        )

    def capability_audit(self) -> dict[str, Any]:
        """Return the complete machine-readable product invocation surface."""
        return {
            **self._gateway.audit(),
            "required_method": REQUIRED_XHS_CALL_METHOD,
            "legacy_playwright_exported": False,
            "test_delay_environment_forwarded": False,
            "fixture_mode": self._fixture_mode,
        }

    def status(self) -> RunAgentVendorStatus:
        if not self.manifest_path.is_file():
            raise RunAgentError("Run Agent vendor manifest is missing")
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        required = {"source_release", "source_archive_sha256", "execution_enabled", "hardening_status", "known_blockers", "extension_build_id", "read_only_capability_enabled", "bounded_write_uat_capability_enabled"}
        if not required.issubset(payload):
            raise RunAgentError("Run Agent vendor manifest is incomplete")
        return RunAgentVendorStatus(
            source_release=str(payload["source_release"]),
            archive_sha256=str(payload["source_archive_sha256"]),
            execution_enabled=payload["execution_enabled"] is True,
            hardening_status=str(payload["hardening_status"]),
            known_blockers=tuple(str(item) for item in payload["known_blockers"]),
            capability_limits=tuple(str(item) for item in payload.get("capability_limits", [])),
            extension_build_id=str(payload["extension_build_id"]),
            read_only_capability_enabled=payload["read_only_capability_enabled"] is True,
            bounded_write_uat_capability_enabled=payload["bounded_write_uat_capability_enabled"] is True,
        )

    def _load_active_mandate(self) -> ExecutionMandate | None:
        if not self._mandate_id:
            return None
        config, _ = load_project_config(self.project_root)
        value = read_json(
            config.runtime.runtime_dir
            / "authority"
            / "mandates"
            / f"{self._mandate_id}.json",
            default=None,
        )
        if not isinstance(value, dict):
            raise RunAgentError("mandate-bound read authority is missing")
        try:
            mandate = ExecutionMandate.from_dict(value)
        except AuthorityContractError as exc:
            raise RunAgentError("mandate-bound read authority is invalid") from exc
        now = datetime.now(timezone.utc)
        valid_from = datetime.fromisoformat(mandate.valid_from.replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(mandate.valid_until.replace("Z", "+00:00"))
        if not valid_from <= now < valid_until:
            raise RunAgentError("mandate-bound read authority is outside its window")
        if mandate.account_id != self._local_account_id():
            raise RunAgentError("mandate account mismatch")
        return mandate

    def _active_read_mandate(
        self, capability: XhsCapability | None
    ) -> ExecutionMandate | None:
        mandate = self._load_active_mandate()
        if mandate is None:
            return None
        if capability is None:
            raise RunAgentError("mandate-bound read requires an exact capability")
        allowed_surfaces = {
            "setup": {CapabilitySurface.SETUP_READ},
            "engage": {CapabilitySurface.SETUP_READ},
            "service": {CapabilitySurface.SETUP_READ, CapabilitySurface.SERVICE_INBOX},
            "publish": {CapabilitySurface.SETUP_READ},
            "review": set(),
        }
        if capability.surface not in allowed_surfaces.get(mandate.workflow, set()):
            raise RunAgentError("mandate workflow does not allow this read surface")
        return mandate

    def _active_action_permit(self, capability: XhsCapability) -> ActionPermit | None:
        if not self._action_permit_id:
            return None
        mandate = self._load_active_mandate()
        if mandate is None:
            raise RunAgentError("action permit requires its execution mandate")
        allowed_kinds = ACTION_KIND_BY_WRITE_OPERATION.get(capability.operation)
        if not allowed_kinds:
            raise RunAgentError("write operation has no autonomous permit mapping")
        config, _ = load_project_config(self.project_root)
        authority_root = config.runtime.runtime_dir / "authority"
        permit_value = read_json(
            authority_root / "permits" / f"{self._action_permit_id}.json",
            default=None,
        )
        if not isinstance(permit_value, dict):
            raise RunAgentError("action permit is missing")
        try:
            permit = ActionPermit.from_dict(permit_value)
        except AuthorityContractError as exc:
            raise RunAgentError("action permit is invalid") from exc
        if (
            permit.mandate_id != mandate.mandate_id
            or permit.mandate_hash != mandate.content_hash
            or permit.account_id != mandate.account_id
            or permit.action_kind not in allowed_kinds
            or permit.action_kind not in mandate.allowed_actions
        ):
            raise RunAgentError("action permit does not match mandate or capability")
        consumed = read_json(
            authority_root / "consumed" / f"{permit.permit_id}.json",
            default=None,
        )
        if not isinstance(consumed, dict) or set(consumed) != {
            "schema_version",
            "permit_id",
            "permit_hash",
            "plan_hash",
            "consumed_at",
            "max_actions_consumed",
        }:
            raise RunAgentError("action permit has not been consumed exactly once")
        if (
            consumed.get("schema_version") != 1
            or consumed.get("permit_id") != permit.permit_id
            or consumed.get("permit_hash") != permit.content_hash
            or consumed.get("plan_hash") != permit.plan_hash
            or consumed.get("max_actions_consumed") != 1
        ):
            raise RunAgentError("action permit consumption integrity failed")
        try:
            consumed_at = datetime.fromisoformat(
                str(consumed.get("consumed_at", "")).replace("Z", "+00:00")
            )
            valid_until = datetime.fromisoformat(
                permit.valid_until.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise RunAgentError("action permit timing is invalid") from exc
        now = datetime.now(timezone.utc)
        if consumed_at.tzinfo is None or valid_until.tzinfo is None:
            raise RunAgentError("action permit timing must be timezone-aware")
        if consumed_at > now or now >= valid_until:
            raise RunAgentError("action permit is outside its dispatch window")
        stop = read_json(
            config.runtime.runtime_dir / "comment_flow" / "STOP.json",
            default=None,
        )
        if isinstance(stop, dict) and stop.get("requires_manual_reconciliation") is True:
            raise RunAgentError("global STOP requires write reconciliation")
        return permit

    def require_readonly_ready(self, capability: XhsCapability | None = None) -> None:
        require_approved_xhs_call_method(REQUIRED_XHS_CALL_METHOD)
        status = self.status()
        if not status.read_only_capability_enabled:
            raise RunAgentError("Ranfang Run Agent read-only capability is disabled")
        mandate = self._active_read_mandate(capability)
        if mandate is None:
            authorization = self.readonly_uat_status()
            if not authorization["authorized"]:
                raise RunAgentError(
                    "Ranfang Run Agent read-only UAT is not authorized: "
                    + "; ".join(authorization["blockers"])
                )
        if not self.connection_status()["ready_for_login_check"]:
            raise RunAgentError("Run Agent connection/build attestation is not ready")
        self._require_vendor_integrity()

    def _readonly_uat_path(self) -> Path:
        config, _ = load_project_config(self.project_root)
        return config.runtime.runtime_dir / "run_agent" / "readonly_uat_authorization.json"

    def _risk_events_path(self) -> Path:
        config, _ = load_project_config(self.project_root)
        return config.runtime.runtime_dir / "run_agent" / "risk_events.jsonl"

    def record_readonly_risk_event(
        self,
        *,
        stage: str,
        event_code: str,
        session_id: str = "",
        occurred_at: datetime | None = None,
        risk_class: str = RISK_CLASS_TECHNICAL,
    ) -> dict[str, Any]:
        """Persist a sanitized fail-closed event without page text or credentials."""
        if re.fullmatch(r"[a-z0-9_.-]{1,64}", stage.strip()) is None:
            raise RunAgentError("risk event stage must be a safe token")
        if re.fullmatch(r"[a-z0-9_.-]{1,64}", event_code.strip()) is None:
            raise RunAgentError("risk event event_code must be a safe token")
        if session_id and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id.strip()) is None:
            raise RunAgentError("risk event session_id must be a safe token")
        if risk_class not in {RISK_CLASS_TECHNICAL, RISK_CLASS_PLATFORM}:
            raise RunAgentError("risk event risk_class is invalid")
        now = occurred_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise RunAgentError("risk event time must be timezone-aware")
        event = {
            "schema_version": 2,
            "occurred_at": now.astimezone(timezone.utc).isoformat(),
            "account_id": self._local_account_id(),
            "stage": stage.strip(),
            "event_code": event_code.strip(),
            "session_id": session_id.strip(),
            "risk_class": risk_class,
            "cooldown_required": risk_class == RISK_CLASS_PLATFORM,
            "writes_allowed": False,
            "platform_actions_executed": 0,
        }
        append_jsonl(self._risk_events_path(), event)
        return event

    def fail_closed_readonly_session(
        self,
        *,
        stage: str,
        event_code: str,
        session_id: str = "",
        occurred_at: datetime | None = None,
        risk_class: str = RISK_CLASS_TECHNICAL,
    ) -> dict[str, Any]:
        event = self.record_readonly_risk_event(
            stage=stage,
            event_code=event_code,
            session_id=session_id,
            occurred_at=occurred_at,
            risk_class=risk_class,
        )
        revocation = self.revoke_readonly_uat(
            confirmation=READONLY_UAT_REVOKE_CONFIRMATION
        )
        return {"risk_event": event, "readonly_uat": revocation}

    def _latest_platform_risk_time(self) -> datetime | None:
        moments: list[datetime] = []
        for event in read_jsonl(self._risk_events_path()):
            if (
                event.get("risk_class") != RISK_CLASS_PLATFORM
                or event.get("cooldown_required") is not True
            ):
                continue
            raw = str(event.get("occurred_at", ""))
            try:
                value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if value.tzinfo is not None:
                moments.append(value.astimezone(timezone.utc))
        return max(moments) if moments else None

    def _local_account_id(self) -> str:
        payload = read_json(self.project_root / "config" / "browser.local.json")
        if not isinstance(payload, dict) or not isinstance(payload.get("account_id"), str):
            raise RunAgentError("valid browser.local.json account_id is required")
        return payload["account_id"]

    def readonly_uat_status(self, *, checked_at: datetime | None = None) -> dict[str, Any]:
        now = checked_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise RunAgentError("read-only UAT status requires timezone-aware time")
        config, _ = load_project_config(self.project_root)
        stop_path = config.runtime.runtime_dir / "comment_flow" / "STOP.json"
        blockers: list[str] = []
        stop = read_json(stop_path)
        stop_allows_read = isinstance(stop, dict) and (
            stop.get("writes_allowed") is False
            or (
                stop.get("writes_allowed") is True
                and stop.get("reason") == "exact_bounded_write_lease_active"
                and isinstance(stop.get("active_lease_id"), str)
                and bool(stop.get("active_lease_id"))
            )
        )
        if not stop_allows_read:
            blockers.append("stop_not_enabled")
        last_risk_at = ""
        cooldown_remaining = 0
        risk_time = self._latest_platform_risk_time()
        if risk_time is not None:
            last_risk_at = risk_time.isoformat()
            cooldown_remaining = max(
                0,
                int((risk_time + timedelta(seconds=READONLY_UAT_COOLDOWN_SECONDS) - now).total_seconds()),
            )
        if cooldown_remaining > 0:
            blockers.append("risk_cooldown_not_elapsed")
        receipt = read_json(self._readonly_uat_path(), default=None)
        valid_until = ""
        if not isinstance(receipt, dict):
            blockers.append("readonly_uat_authorization_missing")
        else:
            valid_until = str(receipt.get("valid_until", ""))
            if receipt.get("risk_cooldown_overridden") is True:
                blockers = [item for item in blockers if item != "risk_cooldown_not_elapsed"]
            try:
                expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
            except ValueError:
                blockers.append("readonly_uat_authorization_invalid")
            else:
                if expiry.tzinfo is None or now > expiry:
                    blockers.append("readonly_uat_authorization_expired")
            if receipt.get("account_id") != self._local_account_id():
                blockers.append("readonly_uat_account_mismatch")
            if receipt.get("extension_build_id") != self.status().extension_build_id:
                blockers.append("readonly_uat_extension_build_mismatch")
        return {
            "authorized": not blockers,
            "blockers": blockers,
            "last_risk_at": last_risk_at,
            "cooldown_seconds": READONLY_UAT_COOLDOWN_SECONDS,
            "cooldown_remaining_seconds": cooldown_remaining,
            "valid_until": valid_until,
            "risk_cooldown_overridden": isinstance(receipt, dict)
            and receipt.get("risk_cooldown_overridden") is True,
            "platform_actions_executed": 0,
        }

    def authorize_readonly_uat(
        self,
        *,
        confirmation: str,
        risk_override_confirmation: str = "",
        authorized_at: datetime | None = None,
        duration_seconds: int = 60 * 60,
    ) -> dict[str, Any]:
        if confirmation != READONLY_UAT_CONFIRMATION:
            raise RunAgentError("exact read-only UAT confirmation is required")
        if not 1 <= duration_seconds <= READONLY_UAT_MAX_DURATION_SECONDS:
            raise RunAgentError("read-only UAT duration must be between 1 and 7200 seconds")
        now = authorized_at or datetime.now(timezone.utc)
        preflight = self.readonly_uat_status(checked_at=now)
        cooldown_overridden = (
            risk_override_confirmation == READONLY_UAT_RISK_OVERRIDE_CONFIRMATION
        )
        # A fresh exact confirmation is allowed to replace a missing, expired,
        # malformed, or prior-build read-only receipt.  Account drift and risk
        # cooldown remain hard blockers.  Without this replacement rule every
        # normal extension upgrade requires a manual revoke before preflight.
        replaceable_receipt_blockers = {
            "readonly_uat_authorization_missing",
            "readonly_uat_authorization_expired",
            "readonly_uat_authorization_invalid",
            "readonly_uat_extension_build_mismatch",
        }
        remaining = [
            item for item in preflight["blockers"]
            if item not in replaceable_receipt_blockers
            and not (item == "risk_cooldown_not_elapsed" and cooldown_overridden)
        ]
        if remaining:
            raise RunAgentError("read-only UAT preflight blocked: " + "; ".join(remaining))
        connection = self.connection_status()
        if not connection["ready_for_login_check"]:
            raise RunAgentError("Run Agent connection/build attestation is not ready")
        receipt = {
            "schema_version": 1,
            "account_id": self._local_account_id(),
            "extension_build_id": self.status().extension_build_id,
            "authorized_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=duration_seconds)).isoformat(),
            "scope": "bounded_readonly_uat",
            "risk_cooldown_overridden": cooldown_overridden,
            "writes_allowed": False,
            "platform_actions_executed": 0,
        }
        write_json_atomic(self._readonly_uat_path(), receipt)
        return receipt

    def revoke_readonly_uat(self, *, confirmation: str) -> dict[str, Any]:
        if confirmation != READONLY_UAT_REVOKE_CONFIRMATION:
            raise RunAgentError("exact read-only UAT revoke confirmation is required")
        path = self._readonly_uat_path()
        existed = path.is_file()
        if existed:
            path.unlink()
        return {"revoked": True, "receipt_existed": existed, "platform_actions_executed": 0}

    def run_readonly_uat_preflight(
        self,
        *,
        confirmation: str,
        risk_override_confirmation: str = "",
        duration_seconds: int = 30 * 60,
    ) -> dict[str, Any]:
        """Authorize, diagnose and bind one healthy XHS tab; revoke on any failure."""
        connection = self.connection_status()
        if not connection["ready_for_login_check"]:
            raise RunAgentError("Run Agent connection/build attestation is not ready")
        authorization = self.authorize_readonly_uat(
            confirmation=confirmation,
            risk_override_confirmation=risk_override_confirmation,
            duration_seconds=duration_seconds,
        )
        tabs: dict[str, Any] = {"tabs": [], "count": 0}
        observed_risks: object = ()
        try:
            # Ask the packaged Run Agent page layer to recover a healthy XHS
            # home tab before tab diagnostics.  This handles a clean Chrome
            # window whose only visible page is chrome://extensions without
            # falling back to generic Chrome automation.
            bootstrap_context = self.page_context()
            bootstrap_risks = bootstrap_context.get("riskSignals", [])
            observed_risks = bootstrap_risks
            if not isinstance(bootstrap_risks, list) or bootstrap_risks:
                raise RunAgentError("recovered XHS tab contains risk or unknown state")
            tabs = self.list_xhs_tabs()
            binding = self.bind_active_xhs_tab()
            context = self.page_context()
            risks = context.get("riskSignals", [])
            observed_risks = risks
            if not isinstance(risks, list) or risks:
                raise RunAgentError("bound XHS tab contains risk or unknown state")
            if context.get("boundTabId") != binding.get("boundTabId"):
                raise RunAgentError("bound XHS tab changed during read-only preflight")
            return {
                "connection": connection,
                "authorization": authorization,
                "bootstrap_page_context": bootstrap_context,
                "tabs": tabs,
                "binding": binding,
                "page_context": context,
                "platform_actions_executed": 0,
            }
        except Exception as exc:
            risk_class = (
                RISK_CLASS_PLATFORM
                if has_explicit_platform_risk(observed_risks)
                or has_explicit_platform_risk(exc)
                else RISK_CLASS_TECHNICAL
            )
            self.fail_closed_readonly_session(
                stage="readonly_preflight",
                event_code=(
                    "explicit_platform_risk_signal"
                    if risk_class == RISK_CLASS_PLATFORM
                    else "preflight_page_or_binding_failure"
                ),
                risk_class=risk_class,
            )
            safe_tabs = sanitize_run_agent_output(tabs)
            raise RunAgentError(
                f"read-only UAT preflight failed and authorization was revoked: {exc}; "
                f"tab_diagnostics={safe_tabs}"
            ) from exc

    def write_runtime_status(self) -> dict[str, Any]:
        """Resolve the only three product write modes without dispatching."""

        vendor = self.status()
        scoped = self.bounded_write_uat_status()
        if vendor.bounded_write_uat_capability_enabled and scoped["authorized"]:
            return {
                "mode": "scoped_uat",
                "ready": True,
                "blockers": [],
                "scoped_uat": scoped,
                "platform_actions_executed": 0,
            }
        checkout = read_json(self.project_root / "V2_CHECKOUT.json", default=None)
        release_blockers: list[str] = []
        if not isinstance(checkout, dict):
            release_blockers.append("v2_checkout_missing_or_invalid")
            checkout = {}
        if checkout.get("execution_enabled") is not True:
            release_blockers.append("v2_execution_disabled")
        if checkout.get("release_ready") is not True:
            release_blockers.append("v2_release_not_ready")
        product_blockers = checkout.get("known_blockers")
        if not isinstance(product_blockers, list):
            release_blockers.append("v2_known_blockers_invalid")
        elif product_blockers:
            release_blockers.append("v2_known_blockers_present")
        if not vendor.execution_enabled:
            release_blockers.append("vendor_execution_disabled")
        if vendor.known_blockers:
            release_blockers.append("vendor_known_blockers_present")
        if not release_blockers:
            return {
                "mode": "recipient_release",
                "ready": True,
                "blockers": [],
                "scoped_uat": scoped,
                "platform_actions_executed": 0,
            }
        return {
            "mode": "offline",
            "ready": False,
            "blockers": release_blockers,
            "scoped_uat": scoped,
            "platform_actions_executed": 0,
        }

    def require_execution_ready(
        self, capability: XhsCapability | None = None
    ) -> dict[str, Any]:
        require_approved_xhs_call_method(REQUIRED_XHS_CALL_METHOD)
        if self._action_permit_id:
            if capability is None:
                raise RunAgentError("action permit requires an exact write capability")
            permit = self._active_action_permit(capability)
            if not self.connection_status()["ready_for_login_check"]:
                raise RunAgentError("Run Agent connection/build attestation is not ready")
            self._require_vendor_integrity()
            assert permit is not None
            return {
                "mode": "autonomous_action_permit",
                "ready": True,
                "blockers": [],
                "permit_id": permit.permit_id,
                "plan_hash": permit.plan_hash,
                "max_actions": 1,
                "platform_actions_executed": 0,
            }
        runtime = self.write_runtime_status()
        if not runtime["ready"]:
            raise RunAgentError(
                "Ranfang Run Agent writes are frozen: " + "; ".join(runtime["blockers"])
            )
        if runtime["mode"] == "scoped_uat":
            if not self.connection_status()["ready_for_login_check"]:
                raise RunAgentError("Run Agent connection/build attestation is not ready")
        self._require_vendor_integrity()
        return runtime

    def _bounded_write_uat_path(self) -> Path:
        config, _ = load_project_config(self.project_root)
        return config.runtime.runtime_dir / "run_agent" / "bounded_write_uat.json"

    def bounded_write_uat_status(self, *, checked_at: datetime | None = None) -> dict[str, Any]:
        now = checked_at or datetime.now(timezone.utc)
        blockers: list[str] = []
        receipt = read_json(self._bounded_write_uat_path(), default=None)
        if not isinstance(receipt, dict):
            blockers.append("bounded_write_uat_authorization_missing")
            receipt = {}
        valid_until = str(receipt.get("valid_until", ""))
        try:
            expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        except ValueError:
            blockers.append("bounded_write_uat_authorization_invalid")
        else:
            if expiry.tzinfo is None or now > expiry:
                blockers.append("bounded_write_uat_authorization_expired")
        if receipt and receipt.get("account_id") != self._local_account_id():
            blockers.append("bounded_write_uat_account_mismatch")
        if receipt and receipt.get("extension_build_id") != self.status().extension_build_id:
            blockers.append("bounded_write_uat_extension_build_mismatch")
        if receipt and receipt.get("writes_allowed") is not True:
            blockers.append("bounded_write_uat_not_write_enabled")
        config, _ = load_project_config(self.project_root)
        stop = read_json(config.runtime.runtime_dir / "comment_flow" / "STOP.json")
        if not isinstance(stop, dict):
            blockers.append("global_stop_state_missing")
        elif stop.get("writes_allowed") is not True:
            blockers.append("global_stop_blocks_writes")
        elif stop.get("active_lease_id") != receipt.get("lease_id"):
            blockers.append("global_stop_lease_mismatch")
        max_actions = receipt.get("max_actions", 0)
        actions_used = receipt.get("actions_used", 0)
        if (
            type(max_actions) is not int or type(actions_used) is not int
            or max_actions != 1 or not 0 <= actions_used <= max_actions
        ):
            blockers.append("bounded_write_uat_action_budget_invalid")
        elif actions_used >= max_actions:
            blockers.append("bounded_write_uat_action_budget_exhausted")
        return {
            "authorized": not blockers,
            "blockers": blockers,
            "session_id": str(receipt.get("session_id", "")),
            "note_id": str(receipt.get("note_id", "")),
            "plan_hash": str(receipt.get("plan_hash", "")),
            "branch": str(receipt.get("branch", "")),
            "lease_id": str(receipt.get("lease_id", "")),
            "max_actions": max_actions,
            "actions_used": actions_used,
            "valid_until": valid_until,
            "platform_actions_executed": 0,
        }

    def authorize_bounded_write_uat(
        self,
        *,
        confirmation: str,
        account_id: str,
        session_id: str,
        note_id: str,
        plan_hash: str,
        branch: str,
        max_actions: int,
        duration_seconds: int = 15 * 60,
    ) -> dict[str, Any]:
        if confirmation != BOUNDED_WRITE_UAT_CONFIRMATION:
            raise RunAgentError("exact bounded write UAT confirmation is required")
        if not session_id or not note_id or len(plan_hash) != 64:
            raise RunAgentError("bounded write UAT requires exact session, note and plan hash")
        config, _ = load_project_config(self.project_root)
        if UnresolvedTargetRegistry(config.runtime.runtime_dir).is_unresolved(note_id):
            raise RunAgentError("bounded write UAT target has unresolved prior action state")
        if account_id != self._local_account_id():
            raise RunAgentError("bounded write UAT account does not match local browser account")
        if branch not in {
            "note_like_only",
            "note_engagement",
            "comment_like_only",
            "comment_engagement",
            "dm_message",
            "service_comment_reply",
            "service_dm_reply",
            "publish_image",
            "publish_video",
        }:
            raise RunAgentError("bounded write UAT branch is invalid")
        if max_actions != 1:
            raise RunAgentError("bounded write UAT allows exactly one action")
        if not 1 <= duration_seconds <= 15 * 60:
            raise RunAgentError("bounded write UAT duration must be between 1 and 900 seconds")
        connection = self.connection_status()
        if not connection["ready_for_login_check"]:
            raise RunAgentError("Run Agent connection/build attestation is not ready")
        stop_path = config.runtime.runtime_dir / "comment_flow" / "STOP.json"
        stop = read_json(stop_path)
        if not isinstance(stop, dict) or stop.get("writes_allowed") is not False:
            raise RunAgentError("bounded write UAT requires an explicit stopped state")
        if stop.get("requires_manual_reconciliation") is True:
            raise RunAgentError("bounded write UAT is blocked by unresolved manual reconciliation")
        now = datetime.now(timezone.utc)
        lease_id = "write_lease_" + hashlib.sha256(
            f"{account_id}|{session_id}|{note_id}|{plan_hash}|{now.isoformat()}".encode("utf-8")
        ).hexdigest()[:20]
        receipt = {
            "schema_version": 1,
            "account_id": account_id,
            "extension_build_id": self.status().extension_build_id,
            "session_id": session_id,
            "note_id": note_id,
            "plan_hash": plan_hash,
            "branch": branch,
            "lease_id": lease_id,
            "max_actions": max_actions,
            "actions_used": 0,
            "authorized_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=duration_seconds)).isoformat(),
            "writes_allowed": True,
            "platform_actions_executed": 0,
        }
        write_json_atomic(self._bounded_write_uat_path(), receipt)
        write_json_atomic(stop_path, {
            "schema_version": 2,
            "writes_allowed": True,
            "reason": "exact_bounded_write_lease_active",
            "active_lease_id": lease_id,
            "session_id": session_id,
            "target_ref_hash": hashlib.sha256(note_id.encode("utf-8")).hexdigest(),
            "plan_hash": plan_hash,
            "valid_until": receipt["valid_until"],
            "requires_manual_reconciliation": False,
        })
        return receipt

    def require_bounded_write_uat(
        self, *, session_id: str, note_id: str, plan_hash: str, branch: str
    ) -> dict[str, Any]:
        status = self.bounded_write_uat_status()
        if not status["authorized"]:
            raise RunAgentError("bounded write UAT is not authorized")
        for key, expected in (
            ("session_id", session_id), ("note_id", note_id),
            ("plan_hash", plan_hash), ("branch", branch),
        ):
            if status[key] != expected:
                raise RunAgentError(f"bounded write UAT {key} mismatch")
        return status

    def revoke_bounded_write_uat(self) -> None:
        path = self._bounded_write_uat_path()
        if path.is_file():
            path.unlink()
        config, _ = load_project_config(self.project_root)
        stop_path = config.runtime.runtime_dir / "comment_flow" / "STOP.json"
        current_stop = read_json(stop_path, default=None)
        if (
            isinstance(current_stop, dict)
            and current_stop.get("requires_manual_reconciliation") is True
        ):
            return
        write_json_atomic(stop_path, {
            "schema_version": 2,
            "writes_allowed": False,
            "reason": "bounded_write_lease_revoked",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "requires_manual_reconciliation": False,
        })

    def reconcile_unknown_write(
        self,
        *,
        attempt_id: str,
        observed_outcome: str,
        evidence_ref: str,
        reconciled_at: str,
        confirmation: str,
        note_id: str = "",
    ) -> dict[str, Any]:
        """Clear an unknown-result STOP after a separately observed manual check."""
        if confirmation != WRITE_RECONCILIATION_CONFIRMATION:
            raise RunAgentError("exact manual write reconciliation confirmation is required")
        config, _ = load_project_config(self.project_root)
        journal = PlatformWriteJournal(config.runtime.runtime_dir)
        try:
            try:
                journal_event = journal.reconcile_unknown(
                    attempt_id=attempt_id,
                    observed_outcome=observed_outcome,
                    evidence_ref=evidence_ref,
                    reconciled_at=reconciled_at,
                    confirmation=confirmation,
                )
            except ValueError as exc:
                # Resume safely when the journal append succeeded but target
                # reconciliation failed in a prior invocation.
                rows = [
                    row for row in read_jsonl(journal.path)
                    if row.get("attempt_id") == attempt_id
                ]
                prior = rows[-1] if rows else None
                if not (
                    isinstance(prior, dict)
                    and prior.get("status") == "reconciled"
                    and prior.get("observed_outcome") == observed_outcome
                    and prior.get("evidence_ref") == evidence_ref
                ):
                    raise exc
                journal_event = prior
            target_event = None
            if note_id:
                target_event = UnresolvedTargetRegistry(config.runtime.runtime_dir).resolve(
                    note_id=note_id,
                    resolution=observed_outcome,
                    evidence_ref=evidence_ref,
                    resolved_at=reconciled_at,
                    confirmation=UNRESOLVED_TARGET_RESOLUTION_CONFIRMATION,
                )
        except ValueError as exc:
            raise RunAgentError(str(exc)) from exc
        return {
            "journal_reconciliation": journal_event,
            "target_reconciliation": target_event,
            "writes_allowed": False,
            "retry_exact_target_allowed": False,
            "platform_actions_executed": 0,
        }

    def _consume_bounded_write_action_if_active(self) -> None:
        if self._action_permit_id:
            return
        path = self._bounded_write_uat_path()
        receipt = read_json(path, default=None)
        if not isinstance(receipt, dict):
            return
        max_actions = receipt.get("max_actions")
        actions_used = receipt.get("actions_used")
        if type(max_actions) is not int or type(actions_used) is not int:
            raise RunAgentError("bounded write UAT action budget is invalid")
        if actions_used >= max_actions:
            raise RunAgentError("bounded write UAT action budget is exhausted")
        config, _ = load_project_config(self.project_root)
        stop_path = config.runtime.runtime_dir / "comment_flow" / "STOP.json"
        stop = read_json(stop_path, default=None)
        if (
            not isinstance(stop, dict)
            or stop.get("writes_allowed") is not True
            or stop.get("active_lease_id") != receipt.get("lease_id")
        ):
            raise RunAgentError("global STOP or write lease changed before dispatch")
        receipt["actions_used"] = actions_used + 1
        write_json_atomic(path, receipt)
        write_json_atomic(stop_path, {
            "schema_version": 2,
            "writes_allowed": False,
            "reason": "bounded_write_action_dispatched",
            "active_lease_id": receipt.get("lease_id", ""),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "requires_manual_reconciliation": False,
        })

    def _require_vendor_integrity(self) -> None:
        for relative in ("LICENSE", "scripts/cli.py", "scripts/bridge_server.py", "extension/manifest.json"):
            if not (self.vendor_root / relative).is_file():
                raise RunAgentError(f"Run Agent vendor file is missing: {relative}")
        security_failures = self.audit_security()
        if security_failures:
            raise RunAgentError("Run Agent security audit failed: " + "; ".join(security_failures))

    def audit_security(self) -> tuple[str, ...]:
        """Statically enforce the privacy boundary of the packaged vendor fork."""
        failures: list[str] = []
        extension_manifest = self.vendor_root / "extension" / "manifest.json"
        if not extension_manifest.is_file():
            return ("extension_manifest_missing",)
        payload = json.loads(extension_manifest.read_text(encoding="utf-8"))
        permissions = set(payload.get("permissions", []))
        forbidden = sorted(permissions & FORBIDDEN_EXTENSION_PERMISSIONS)
        if forbidden:
            failures.append("forbidden_extension_permissions:" + ",".join(forbidden))
        loaded_scripts = {
            script
            for item in payload.get("content_scripts", [])
            for script in item.get("js", [])
        }
        if {"interceptor.js", "netlogger.js"} & loaded_scripts:
            failures.append("forbidden_content_script_loaded")
        for relative in sorted(FORBIDDEN_VENDOR_FILES):
            if (self.vendor_root / relative).exists():
                failures.append(f"forbidden_vendor_file:{relative}")
        cli_text = (self.vendor_root / "scripts" / "cli.py").read_text(encoding="utf-8")
        for command in ('add_parser("delete-cookies"', 'add_parser("get-netlog"', 'add_parser("risk-report"'):
            if command in cli_text:
                failures.append(f"forbidden_cli_command:{command}")
        if 'required=True, help="xsec_token"' in cli_text:
            failures.append("xsec_token_required_in_process_arguments")
        return tuple(failures)

    def setup_guide(self) -> dict[str, Any]:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", self.project_root / "work"))
        staged_extension = local_app_data / "XhsOperationsCore" / "xhs-bridge-extension"
        vendor_extension = self.vendor_root / "extension"
        vendor_extension_sha256 = _directory_sha256(vendor_extension)
        staged_extension_sha256 = _directory_sha256(staged_extension)
        browser_config = self.project_root / "config" / "browser.local.json"
        profile_path = ""
        if browser_config.is_file():
            payload = json.loads(browser_config.read_text(encoding="utf-8"))
            profile_name = str(payload.get("profile_name", ""))
            if profile_name:
                profile_path = str(self.project_root / "browser-profiles" / profile_name)
        return {
            "method": REQUIRED_XHS_CALL_METHOD,
            "one_time_per_computer_and_profile": True,
            "staged_extension_path": str(staged_extension),
            "extension_manifest_present": (staged_extension / "manifest.json").is_file(),
            "vendor_extension_sha256": vendor_extension_sha256,
            "staged_extension_sha256": staged_extension_sha256,
            "staged_extension_matches_vendor": bool(vendor_extension_sha256)
            and staged_extension_sha256 == vendor_extension_sha256,
            "chrome_profile_path": profile_path,
            "steps": [
                "run scripts/install.ps1",
                "run scripts/run-bridge.ps1 start",
                "run scripts/run-chrome.ps1 start to open only the configured XhsOperationsCore Chrome profile",
                "open chrome://extensions and enable Developer mode",
                "choose Load unpacked and select staged_extension_path",
                "confirm XHS Bridge is enabled",
                "restart the dedicated profile without --load-extension or --disable-extensions-except if the extension card disappears",
                "run run-agent enroll-extension-instance once for this dedicated profile",
                "run run-agent connection-check",
                "log in to Xiaohongshu in the same dedicated profile",
            ],
            "troubleshooting_doc": "docs/migration/20_xhs_bridge_new_computer_troubleshooting.md",
            "forbidden_chrome_flags": [
                "--load-extension",
                "--disable-extensions-except",
            ],
        }

    def connection_status(self, bridge_url: str = "ws://localhost:9333") -> dict[str, Any]:
        """Check only the local bridge handshake; never access Xiaohongshu."""
        status = self.setup_guide()
        expected_extension_build_id = self.status().extension_build_id
        bridge_connected = False
        extension_connected = False
        loaded_extension_build_id = ""
        loaded_extension_instance_id = ""
        error = ""
        try:
            import websockets.sync.client as ws_client

            with ws_client.connect(bridge_url, open_timeout=3) as ws:
                ws.send(json.dumps({"role": "cli", "method": "ping_server"}))
                response = json.loads(ws.recv(timeout=5))
            bridge_connected = "result" in response
            extension_connected = bool(response.get("result", {}).get("extension_connected"))
            loaded_extension_build_id = str(
                response.get("result", {}).get("extension_build_id", "")
            )
            loaded_extension_instance_id = str(
                response.get("result", {}).get("extension_instance_id", "")
            )
        except Exception as exc:
            error = type(exc).__name__
        staged_extension_current = status["staged_extension_matches_vendor"] is True
        loaded_extension_current = (
            bool(expected_extension_build_id)
            and loaded_extension_build_id == expected_extension_build_id
        )
        enrollment = read_json(self._extension_enrollment_path(), default=None)
        enrolled_extension_instance_id = (
            str(enrollment.get("extension_instance_id", ""))
            if isinstance(enrollment, dict) else ""
        )
        extension_instance_enrolled = bool(enrolled_extension_instance_id)
        extension_instance_matches = (
            extension_instance_enrolled
            and loaded_extension_instance_id == enrolled_extension_instance_id
        )
        ready_for_login_check = (
            bridge_connected
            and extension_connected
            and staged_extension_current
            and loaded_extension_current
            and extension_instance_matches
        )
        return {
            **status,
            "bridge_url": bridge_url,
            "bridge_server_connected": bridge_connected,
            "extension_connected": extension_connected,
            "staged_extension_current": staged_extension_current,
            "expected_extension_build_id": expected_extension_build_id,
            "loaded_extension_build_id": loaded_extension_build_id,
            "loaded_extension_current": loaded_extension_current,
            "loaded_extension_instance_id": loaded_extension_instance_id,
            "enrolled_extension_instance_id": enrolled_extension_instance_id,
            "extension_instance_enrolled": extension_instance_enrolled,
            "extension_instance_matches": extension_instance_matches,
            "ready_for_login_check": ready_for_login_check,
            "error_code": error,
            "platform_actions_executed": 0,
        }

    def _extension_enrollment_path(self) -> Path:
        config, _ = load_project_config(self.project_root)
        return config.runtime.runtime_dir / "run_agent" / "extension_instance.json"

    def enroll_current_extension_instance(self, *, confirmation: str) -> dict[str, Any]:
        if confirmation != EXTENSION_ENROLL_CONFIRMATION:
            raise RunAgentError("exact extension enrollment confirmation is required")
        connection = self.connection_status()
        if not (
            connection["bridge_server_connected"]
            and connection["extension_connected"]
            and connection["staged_extension_current"]
            and connection["loaded_extension_current"]
            and connection["loaded_extension_instance_id"]
        ):
            raise RunAgentError("current extension instance is not eligible for enrollment")
        receipt = {
            "schema_version": 1,
            "extension_instance_id": connection["loaded_extension_instance_id"],
            "extension_build_id": connection["loaded_extension_build_id"],
            "account_id": self._local_account_id(),
            "enrolled_at": datetime.now(timezone.utc).isoformat(),
            "platform_actions_executed": 0,
        }
        write_json_atomic(self._extension_enrollment_path(), receipt)
        return receipt

    def search_feeds(self, keyword: str) -> dict[str, Any]:
        raise RunAgentError(
            "legacy direct search is frozen; use one visible search session"
        )

    def search_feeds_visible(self, keyword: str) -> dict[str, Any]:
        if not keyword.strip() or "\ufffd" in keyword or set(keyword.strip()) == {"?"}:
            raise RunAgentError("search keyword failed Unicode validation")
        raw = self._invoke_raw(
            "search-feeds-visible", ["--keyword", keyword.strip()], timeout=180, read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        for feed in raw.get("feeds", []):
            feed_id = str(feed.get("id", ""))
            token = str(feed.get("xsecToken", ""))
            if feed_id and token:
                self._token_by_feed[feed_id] = token
        return sanitize_run_agent_output(raw)

    def adopt_current_search_results(self, keyword: str) -> dict[str, Any]:
        """Read the current query batch, normalizing AI search without retyping."""
        if not keyword.strip() or "\ufffd" in keyword or set(keyword.strip()) == {"?"}:
            raise RunAgentError("search keyword failed Unicode validation")
        raw = self._invoke_raw(
            "adopt-search-results", ["--keyword", keyword.strip()], timeout=180, read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        for feed in raw.get("feeds", []):
            feed_id = str(feed.get("id", ""))
            token = str(feed.get("xsecToken", ""))
            if feed_id and token:
                self._token_by_feed[feed_id] = token
        return sanitize_run_agent_output(raw)

    def get_feed_detail(self, feed_id: str, *, max_comment_items: int = 20) -> dict[str, Any]:
        raise RunAgentError(
            "legacy token detail is frozen; use current-page detail after opening one result"
        )

    def capture_own_reply_history(
        self, *, start_position: int = 0, max_notes: int = 10,
        max_comment_items: int = 200,
    ) -> dict[str, Any]:
        if type(start_position) is not int or start_position < 0:
            raise RunAgentError("start_position must be a non-negative integer")
        if type(max_notes) is not int or not 1 <= max_notes <= 30:
            raise RunAgentError("max_notes must be 1-30")
        if type(max_comment_items) is not int or not 1 <= max_comment_items <= 200:
            raise RunAgentError("max_comment_items must be 1-200")
        return sanitize_run_agent_output(self._invoke_raw(
            "capture-own-reply-history",
            [
                "--start-position", str(start_position),
                "--max-notes", str(max_notes),
                "--max-comment-items", str(max_comment_items),
            ],
            timeout=1800,
            read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        ))

    def open_own_profile(self) -> dict[str, Any]:
        """Open the visible own-profile link and bind it to the enrolled account."""
        raw = self._invoke_raw(
            "open-own-profile", [], read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        platform_user_id = raw.pop("platformUserId", None)
        if not isinstance(platform_user_id, str) or not platform_user_id:
            raise RunAgentError("own-profile navigation returned no stable platform identity")
        identity_hash = hashlib.sha256(platform_user_id.encode("utf-8")).hexdigest()
        config, _ = load_project_config(self.project_root)
        identity_path = config.runtime.runtime_dir / "setup" / "platform_identity.json"
        existing = read_json(identity_path, default=None)
        if existing is not None and (
            not isinstance(existing, dict)
            or existing.get("account_id") != self._local_account_id()
            or existing.get("platform_identity_hash") != identity_hash
        ):
            raise RunAgentError("opened Xiaohongshu profile differs from the enrolled account")
        if existing is None:
            existing = {
                "schema_version": 1,
                "account_id": self._local_account_id(),
                "platform_identity_hash": identity_hash,
                "enrolled_at": datetime.now(timezone.utc).isoformat(),
                "provider": "ranfang_run_agent_visible_own_profile",
            }
            write_json_atomic(identity_path, existing)
        safe = sanitize_run_agent_output(raw)
        safe["platform_identity_hash"] = identity_hash
        safe["identity_enrolled"] = True
        safe["platform_actions_executed"] = 0
        return safe

    def assert_current_account_identity(self) -> dict[str, Any]:
        """Fail closed unless the live visible account matches setup enrollment."""
        raw = self._invoke_raw(
            "current-account-identity", [], read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        platform_user_id = raw.pop("platformUserId", None)
        if not isinstance(platform_user_id, str) or not platform_user_id:
            raise RunAgentError("live Xiaohongshu account identity is missing")
        live_hash = hashlib.sha256(platform_user_id.encode("utf-8")).hexdigest()
        config, _ = load_project_config(self.project_root)
        enrolled = read_json(
            config.runtime.runtime_dir / "setup" / "platform_identity.json",
            default=None,
        )
        if (
            not isinstance(enrolled, dict)
            or enrolled.get("account_id") != self._local_account_id()
            or enrolled.get("platform_identity_hash") != live_hash
        ):
            raise RunAgentError("live Xiaohongshu account differs from setup enrollment")
        safe = sanitize_run_agent_output(raw)
        safe["platform_identity_hash"] = live_hash
        safe["verified"] = True
        safe["platform_actions_executed"] = 0
        return safe

    def enroll_current_account_identity(self, *, confirmation: str) -> dict[str, Any]:
        """Bind the visible 我 sidebar identity to the local account once."""
        if confirmation != PLATFORM_ACCOUNT_ENROLLMENT_CONFIRMATION:
            raise RunAgentError("exact visible platform account enrollment confirmation is required")
        raw = self._invoke_raw(
            "current-account-identity", [], read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        platform_user_id = raw.pop("platformUserId", None)
        if not isinstance(platform_user_id, str) or not platform_user_id:
            raise RunAgentError("visible Xiaohongshu account identity is missing")
        identity_hash = hashlib.sha256(platform_user_id.encode("utf-8")).hexdigest()
        config, _ = load_project_config(self.project_root)
        identity_path = config.runtime.runtime_dir / "setup" / "platform_identity.json"
        existing = read_json(identity_path, default=None)
        if existing is not None and (
            not isinstance(existing, dict)
            or existing.get("account_id") != self._local_account_id()
            or existing.get("platform_identity_hash") != identity_hash
        ):
            raise RunAgentError("visible Xiaohongshu account differs from prior enrollment")
        receipt = {
            "schema_version": 1,
            "account_id": self._local_account_id(),
            "platform_identity_hash": identity_hash,
            "enrolled_at": datetime.now(timezone.utc).isoformat(),
            "provider": "ranfang_run_agent_visible_own_sidebar",
            "platform_actions_executed": 0,
        }
        if existing is None:
            write_json_atomic(identity_path, receipt)
        else:
            receipt = {**existing, "platform_actions_executed": 0}
        safe = sanitize_run_agent_output(raw)
        safe.update(receipt)
        safe["identity_enrolled"] = True
        return safe

    def open_commenter_profile(
        self,
        *,
        feed_id: str,
        comment_id: str,
        target_context_hash: str,
    ) -> dict[str, Any]:
        """Open the exact comment author after revalidating current context."""
        for name, value in (("feed_id", feed_id), ("comment_id", comment_id)):
            if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
                raise RunAgentError(f"{name} contains unsupported characters")
        if re.fullmatch(r"[0-9a-f]{64}", target_context_hash or "") is None:
            raise RunAgentError("target_context_hash must be SHA-256 hex")
        detail = self.get_current_feed_detail(feed_id, max_comment_items=200)
        user_id, nickname, text = _find_comment_identity(detail, comment_id)
        observed_hash = _comment_context_hash(
            note_id=feed_id,
            comment_id=comment_id,
            commenter=nickname,
            text=text,
        )
        if observed_hash != target_context_hash:
            raise RunAgentError("exact comment context changed before profile navigation")
        raw = self._invoke_raw(
            "open-commenter-profile",
            [
                "--feed-id", feed_id,
                "--comment-id", comment_id,
                "--expected-user-id", user_id,
            ],
            read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        returned_id = raw.pop("profileId", None)
        if returned_id != user_id:
            raise RunAgentError("opened commenter profile identity mismatch")
        safe = sanitize_run_agent_output(raw)
        safe["peer_ref_hash"] = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        safe["target_context_hash"] = observed_hash
        safe["platform_actions_executed"] = 0
        return safe

    def page_context(self) -> dict[str, Any]:
        """Read visible page identity/risk without navigating or retaining secrets."""
        return sanitize_run_agent_output(self._invoke_raw(
            "page-context", [], read_only=True, _surface=CapabilitySurface.SETUP_READ
        ))

    def return_to_source_comment(
        self, *, feed_id: str, comment_id: str, target_context_hash: str
    ) -> dict[str, Any]:
        """Return to one source comment and verify its immutable context again."""
        for name, value in (("feed_id", feed_id), ("comment_id", comment_id)):
            if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
                raise RunAgentError(f"{name} contains unsupported characters")
        if re.fullmatch(r"[0-9a-f]{64}", target_context_hash or "") is None:
            raise RunAgentError("target_context_hash must be SHA-256 hex")
        raw = self._invoke_raw(
            "return-to-source-comment",
            ["--feed-id", feed_id, "--comment-id", comment_id],
            read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        detail = self.get_current_feed_detail(feed_id, max_comment_items=200)
        _user_id, nickname, text = _find_comment_identity(detail, comment_id)
        observed_hash = _comment_context_hash(
            note_id=feed_id,
            comment_id=comment_id,
            commenter=nickname,
            text=text,
        )
        if observed_hash != target_context_hash:
            raise RunAgentError("exact comment context changed after profile return")
        safe = sanitize_run_agent_output(raw)
        safe["target_context_hash"] = observed_hash
        safe["platform_actions_executed"] = 0
        return safe

    def open_dm_conversation(self, *, expected_peer_ref_hash: str) -> dict[str, Any]:
        """Open one visible DM conversation and bind it to the selected peer."""
        if re.fullmatch(r"[0-9a-f]{64}", expected_peer_ref_hash or "") is None:
            raise RunAgentError("expected_peer_ref_hash must be SHA-256 hex")
        raw = self._invoke_raw(
            "open-dm-conversation", [], read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        profile_id = raw.pop("profileId", None)
        if not isinstance(profile_id, str) or not profile_id:
            raise RunAgentError("DM navigation returned no stable peer identity")
        peer_hash = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()
        if peer_hash != expected_peer_ref_hash:
            raise RunAgentError("opened DM peer differs from the selected commenter")
        safe = sanitize_run_agent_output(raw)
        safe["peer_ref_hash"] = peer_hash
        safe["conversation_id"] = "xhs_dm_" + peer_hash[:24]
        safe["platform_actions_executed"] = 0
        return safe

    def capture_current_dm_snapshot(
        self,
        *,
        account_id: str,
        conversation_id: str,
        expected_peer_ref_hash: str,
        captured_at: str,
        max_messages: int = 50,
    ) -> tuple[Any, dict[str, Any]]:
        """Return a validated snapshot plus non-sensitive capture evidence."""
        from xhs_operations_core.dm import build_dm_conversation_snapshot

        if not account_id or account_id != self._local_account_id():
            raise RunAgentError("DM capture account differs from local account")
        if re.fullmatch(r"xhs_dm_[0-9a-f]{24}", conversation_id or "") is None:
            raise RunAgentError("conversation_id is not a Run Agent DM reference")
        if re.fullmatch(r"[0-9a-f]{64}", expected_peer_ref_hash or "") is None:
            raise RunAgentError("expected_peer_ref_hash must be SHA-256 hex")
        if type(max_messages) is not int or not 1 <= max_messages <= 100:
            raise RunAgentError("max_messages must be 1-100")
        raw = self._invoke_raw(
            "capture-current-dm-conversation",
            ["--max-messages", str(max_messages)],
            read_only=True,
            _surface=CapabilitySurface.SETUP_READ,
        )
        profile_id = raw.pop("profileId", None)
        if not isinstance(profile_id, str) or not profile_id:
            raise RunAgentError("DM capture returned no stable peer identity")
        observed_peer_hash = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()
        if observed_peer_hash != expected_peer_ref_hash:
            raise RunAgentError("current DM peer differs from the selected commenter")
        rows = raw.pop("messages", None)
        if not isinstance(rows, list) or len(rows) > max_messages:
            raise RunAgentError("DM capture returned an invalid message batch")
        normalized = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RunAgentError("DM capture returned an invalid message row")
            direction = row.get("direction")
            text = " ".join(str(row.get("text") or "").split())
            if direction not in {"incoming", "outgoing"} or not text:
                raise RunAgentError("DM message direction or text is invalid")
            digest = hashlib.sha256(
                f"{conversation_id}|{index}|{direction}|{text}".encode("utf-8")
            ).hexdigest()
            normalized.append({
                "message_id": "dm_visible_" + digest[:20],
                "direction": direction,
                "text": text,
                "sent_at": captured_at,
            })
        snapshot = build_dm_conversation_snapshot(
            account_id=account_id,
            captured_at=captured_at,
            capture={
                "conversation_id": conversation_id,
                "peer_ref": "sha256:" + expected_peer_ref_hash,
                "messages": normalized,
                "risk_signals": list(raw.get("riskSignals") or []),
            },
            max_messages=max_messages,
        )
        evidence = {
            "coverage": raw.get("coverage"),
            "visible_row_count": int(raw.get("visibleRowCount") or 0),
            "captured_message_count": len(normalized),
            "direction_evidence_required": True,
        }
        return snapshot, evidence

    def capture_current_dm_conversation(
        self,
        *,
        account_id: str,
        conversation_id: str,
        expected_peer_ref_hash: str,
        captured_at: str,
        max_messages: int = 50,
    ) -> dict[str, Any]:
        """Build a persistable privacy-redacted snapshot from visible rows."""
        snapshot, evidence = self.capture_current_dm_snapshot(
            account_id=account_id,
            conversation_id=conversation_id,
            expected_peer_ref_hash=expected_peer_ref_hash,
            captured_at=captured_at,
            max_messages=max_messages,
        )
        return {"snapshot": snapshot.to_dict(), "capture_evidence": evidence}

    def open_service_inbox(self, *, channel: str) -> dict[str, Any]:
        """Navigate to one visible inbound-service channel through the Gateway."""
        if channel not in {"comments", "dm"}:
            raise RunAgentError("service channel must be comments or dm")
        result = sanitize_run_agent_output(self._invoke_raw(
            "open-service-inbox",
            ["--channel", channel],
            read_only=True,
            _surface=CapabilitySurface.SERVICE_INBOX,
        ))
        if (
            result.get("verified") is not True
            or result.get("channel") != channel
            or result.get("platform_actions_executed") != 0
        ):
            raise RunAgentError("service inbox navigation was not exactly verified")
        return result

    def capture_service_inbox(
        self,
        *,
        channel: str,
        max_items: int = 20,
    ) -> dict[str, Any]:
        """Capture one bounded visible service inbox and hash platform peer IDs."""
        if channel not in {"comments", "dm"}:
            raise RunAgentError("service channel must be comments or dm")
        if type(max_items) is not int or not 1 <= max_items <= 50:
            raise RunAgentError("service max_items must be 1-50")
        raw = self._invoke_raw(
            "capture-service-inbox",
            ["--channel", channel, "--max-items", str(max_items)],
            read_only=True,
            _surface=CapabilitySurface.SERVICE_INBOX,
        )
        if (
            raw.get("channel") != channel
            or raw.get("coverage") != "bounded_visible_service_inbox"
            or raw.get("read_only") is not True
            or raw.get("platform_actions_executed") != 0
            or not isinstance(raw.get("items"), list)
            or len(raw["items"]) > max_items
        ):
            raise RunAgentError("service inbox capture contract is invalid")
        safe_items: list[dict[str, Any]] = []
        for raw_item in raw["items"]:
            if not isinstance(raw_item, dict):
                raise RunAgentError("service inbox item is invalid")
            item = dict(raw_item)
            peer_id = str(item.pop("peerProfileId", "") or "")
            if peer_id and re.fullmatch(r"[A-Za-z0-9_-]+", peer_id) is None:
                raise RunAgentError("service inbox peer identity is invalid")
            item["peer_ref_hash"] = (
                hashlib.sha256(peer_id.encode("utf-8")).hexdigest() if peer_id else ""
            )
            if re.fullmatch(r"[0-9a-f]{64}", str(item.get("itemHash") or "")) is None:
                raise RunAgentError("service inbox item hash is invalid")
            if re.fullmatch(r"[0-9a-f]{64}", str(item.get("incomingTextHash") or "")) is None:
                raise RunAgentError("service inbox incoming text hash is invalid")
            safe_items.append(sanitize_run_agent_output(item))
        return {
            "channel": channel,
            "items": safe_items,
            "captured_item_count": len(safe_items),
            "coverage": "bounded_visible_service_inbox",
            "bound_tab_id": raw.get("boundTabId"),
            "read_only": True,
            "platform_actions_executed": 0,
        }

    def open_service_item(
        self,
        *,
        channel: str,
        expected_item_hash: str,
    ) -> dict[str, Any]:
        """Open one exact saved inbox item; never fall back to a direct URL."""
        if channel not in {"comments", "dm"}:
            raise RunAgentError("service channel must be comments or dm")
        if re.fullmatch(r"[0-9a-f]{64}", expected_item_hash or "") is None:
            raise RunAgentError("service expected_item_hash must be SHA-256 hex")
        raw = self._invoke_raw(
            "open-service-item",
            ["--channel", channel, "--expected-item-hash", expected_item_hash],
            read_only=True,
            _surface=CapabilitySurface.SERVICE_INBOX,
        )
        if (
            raw.get("verified") is not True
            or raw.get("channel") != channel
            or raw.get("itemHash") != expected_item_hash
            or raw.get("platform_actions_executed") != 0
        ):
            raise RunAgentError("service inbox item navigation was not exactly verified")
        profile_id = str(raw.pop("profileId", "") or "")
        if profile_id and re.fullmatch(r"[A-Za-z0-9_-]+", profile_id) is None:
            raise RunAgentError("opened service peer identity is invalid")
        safe = sanitize_run_agent_output(raw)
        safe["peer_ref_hash"] = (
            hashlib.sha256(profile_id.encode("utf-8")).hexdigest() if profile_id else ""
        )
        return safe

    def send_current_dm_message(
        self, *, expected_peer_ref_hash: str, content: str
    ) -> dict[str, Any]:
        """Private runtime primitive; callers must enforce exact DM approval first."""
        if re.fullmatch(r"[0-9a-f]{64}", expected_peer_ref_hash or "") is None:
            raise RunAgentError("expected_peer_ref_hash must be SHA-256 hex")
        content = " ".join(str(content or "").split())
        if not content or len(content) > 240 or "\ufffd" in content or set(content) == {"?"}:
            raise RunAgentError("DM content failed length or Unicode validation")
        raw = dict(self._invoke_raw(
            "send-current-dm-message",
            ["--expected-peer-hash", expected_peer_ref_hash, "--content", content],
            _surface=CapabilitySurface.SESSION_CURRENT_PAGE,
        ))
        profile_id = raw.pop("profileId", None)
        if not isinstance(profile_id, str) or hashlib.sha256(
            profile_id.encode("utf-8")
        ).hexdigest() != expected_peer_ref_hash:
            raise RunAgentError("verified DM write returned a different peer identity")
        if raw.get("verified") is not True or raw.get("platform_actions_executed") != 1:
            raise RunAgentError("DM write did not produce exact visible verification")
        expected_content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if raw.get("contentHash") != expected_content_hash:
            raise RunAgentError("DM write content hash differs from the approved message")
        safe = sanitize_run_agent_output(raw)
        safe["peer_ref_hash"] = expected_peer_ref_hash
        return safe

    def bind_active_xhs_tab(self) -> dict[str, Any]:
        """Bind Run Agent to the user's foreground Xiaohongshu tab."""
        return sanitize_run_agent_output(
            self._invoke_raw(
                "bind-active-xhs-tab", [], read_only=True,
                _surface=CapabilitySurface.SETUP_READ,
            )
        )

    def list_xhs_tabs(self) -> dict[str, Any]:
        """List sanitized visible XHS tab contexts without navigation."""
        return sanitize_run_agent_output(
            self._invoke_raw(
                "list-xhs-tabs", [], read_only=True,
                _surface=CapabilitySurface.SETUP_READ,
            )
        )

    def get_current_feed_detail(
        self, feed_id: str, *, max_comment_items: int = 20
    ) -> dict[str, Any]:
        return sanitize_run_agent_output(
            self._invoke_raw(
                "get-current-feed-detail",
                ["--feed-id", feed_id, "--max-comment-items", str(max_comment_items)],
                read_only=True,
                _surface=CapabilitySurface.SETUP_READ,
            )
        )

    def like_current_feed(self, feed_id: str) -> dict[str, Any]:
        return sanitize_run_agent_output(
            self._invoke_raw(
                "like-current-feed", ["--feed-id", feed_id],
                _surface=CapabilitySurface.SESSION_CURRENT_PAGE,
            )
        )

    def inspect_current_like_control(self, feed_id: str) -> dict[str, Any]:
        return sanitize_run_agent_output(
            self._invoke_raw(
                "inspect-current-like-control", ["--feed-id", feed_id],
                read_only=True,
                _surface=CapabilitySurface.SETUP_READ,
            )
        )

    def inspect_current_comment_controls(
        self, feed_id: str, *, comment_id: str = ""
    ) -> dict[str, Any]:
        arguments = ["--feed-id", feed_id]
        if comment_id:
            arguments.extend(["--comment-id", comment_id])
        return sanitize_run_agent_output(
            self._invoke_raw(
                "inspect-current-comment-controls",
                arguments,
                read_only=True,
                _surface=CapabilitySurface.SETUP_READ,
            )
        )

    def post_comment_current(self, feed_id: str, content: str) -> dict[str, Any]:
        return sanitize_run_agent_output(
            self._invoke_raw(
                "post-comment-current",
                ["--feed-id", feed_id, "--content", content],
                _surface=CapabilitySurface.SESSION_CURRENT_PAGE,
            )
        )

    def like_current_comment(self, feed_id: str, comment_id: str) -> dict[str, Any]:
        raise RunAgentError(
            "comment context hash is required; use like_current_comment_bound"
        )

    def _assert_current_comment_context(
        self, *, feed_id: str, comment_id: str, target_context_hash: str
    ) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", target_context_hash or "") is None:
            raise RunAgentError("target_context_hash must be SHA-256 hex")
        detail = self.get_current_feed_detail(feed_id, max_comment_items=200)
        _user_id, nickname, text = _find_comment_identity(detail, comment_id)
        observed = _comment_context_hash(
            note_id=feed_id,
            comment_id=comment_id,
            commenter=nickname,
            text=text,
        )
        if observed != target_context_hash:
            raise RunAgentError("exact comment context changed before write")
        return observed

    def like_current_comment_bound(
        self, feed_id: str, comment_id: str, *, target_context_hash: str
    ) -> dict[str, Any]:
        observed = self._assert_current_comment_context(
            feed_id=feed_id,
            comment_id=comment_id,
            target_context_hash=target_context_hash,
        )
        result = sanitize_run_agent_output(
            self._invoke_raw(
                "like-current-comment",
                ["--feed-id", feed_id, "--comment-id", comment_id],
                _surface=CapabilitySurface.SESSION_CURRENT_PAGE,
            )
        )
        result["targetContextHash"] = observed
        return result

    def reply_current_comment(
        self, feed_id: str, text: str, *, comment_id: str
    ) -> dict[str, Any]:
        raise RunAgentError(
            "comment context hash is required; use reply_current_comment_bound"
        )

    def reply_current_comment_bound(
        self,
        feed_id: str,
        text: str,
        *,
        comment_id: str,
        target_context_hash: str,
    ) -> dict[str, Any]:
        observed = self._assert_current_comment_context(
            feed_id=feed_id,
            comment_id=comment_id,
            target_context_hash=target_context_hash,
        )
        result = sanitize_run_agent_output(
            self._invoke_raw(
                "reply-current-comment",
                ["--feed-id", feed_id, "--comment-id", comment_id, "--content", text],
                _surface=CapabilitySurface.SESSION_CURRENT_PAGE,
            )
        )
        result["targetContextHash"] = observed
        return result

    def publish_image_current(
        self,
        *,
        plan_hash: str,
        title: str,
        content: str,
        tags: list[str],
        image_paths: list[str],
        media_hashes: list[str],
    ) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{64}", plan_hash or "") is None:
            raise RunAgentError("publish plan_hash must be SHA-256 hex")
        if not 1 <= len(image_paths) <= 9 or len(image_paths) != len(media_hashes):
            raise RunAgentError("image publish media binding is invalid")
        if any(re.fullmatch(r"[0-9a-f]{64}", item or "") is None for item in media_hashes):
            raise RunAgentError("image publish media hash is invalid")
        args = ["--plan-hash", plan_hash, "--title", title, "--content", content]
        args.extend(["--images", *image_paths, "--media-hashes", *media_hashes])
        if tags:
            args.extend(["--tags", *tags])
        result = sanitize_run_agent_output(self._invoke_raw(
            "publish-image-current",
            args,
            timeout=15 * 60,
            _surface=CapabilitySurface.PUBLISH_CURRENT_PAGE,
        ))
        self._validate_publish_result(
            result,
            plan_hash=plan_hash,
            media_hashes=media_hashes,
            expected_content_hash=hashlib.sha256(
                json.dumps(
                    {"title": title, "content": content, "tags": tags},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        )
        return result

    def publish_video_current(
        self,
        *,
        plan_hash: str,
        title: str,
        content: str,
        tags: list[str],
        video_path: str,
        media_hash: str,
    ) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{64}", plan_hash or "") is None:
            raise RunAgentError("publish plan_hash must be SHA-256 hex")
        if not video_path or re.fullmatch(r"[0-9a-f]{64}", media_hash or "") is None:
            raise RunAgentError("video publish media binding is invalid")
        args = [
            "--plan-hash", plan_hash,
            "--title", title,
            "--content", content,
            "--video", video_path,
            "--media-hash", media_hash,
        ]
        if tags:
            args.extend(["--tags", *tags])
        result = sanitize_run_agent_output(self._invoke_raw(
            "publish-video-current",
            args,
            timeout=20 * 60,
            _surface=CapabilitySurface.PUBLISH_CURRENT_PAGE,
        ))
        self._validate_publish_result(
            result,
            plan_hash=plan_hash,
            media_hashes=[media_hash],
            expected_content_hash=hashlib.sha256(
                json.dumps(
                    {"title": title, "content": content, "tags": tags},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        )
        return result

    @staticmethod
    def _validate_publish_result(
        result: dict[str, Any],
        *,
        plan_hash: str,
        media_hashes: list[str],
        expected_content_hash: str,
    ) -> None:
        if (
            result.get("verified") is not True
            or result.get("platform_actions_executed") != 1
            or result.get("actionDispatched") is not True
        ):
            raise RunAgentError("publish write did not produce exact visible verification")
        if result.get("planHash") != plan_hash or result.get("mediaHashes") != media_hashes:
            raise RunAgentError("publish result differs from approved plan or media")
        if result.get("contentHash") != expected_content_hash:
            raise RunAgentError("publish result content hash differs from approved text")

    def go_back_and_verify(self, expected_query: str) -> dict[str, Any]:
        if not expected_query.strip():
            raise RunAgentError("expected search query is required")
        return sanitize_run_agent_output(
            self._invoke_raw(
                "go-back-and-verify",
                ["--expected-query", expected_query.strip()],
                read_only=True,
                _surface=CapabilitySurface.SETUP_READ,
            )
        )

    def recover_search_results(
        self, expected_query: str, *, expected_candidate_id: str = ""
    ) -> dict[str, Any]:
        """Recover one candidate failure without submitting another search."""
        value = expected_query.strip()
        if not value:
            raise RunAgentError("expected search query is required")
        context = self.page_context()
        risks = context.get("riskSignals", [])
        if not isinstance(risks, list):
            raise RunAgentError("candidate recovery risk state is unknown")
        if has_explicit_platform_risk(risks):
            raise RunAgentError(
                "explicit platform risk prevents candidate recovery: " + ",".join(risks)
            )
        if context.get("pageType") != "search_results" or str(context.get("query", "")) != value:
            context = self.go_back_and_verify(value)
        time.sleep(_visible_observation_delay_seconds(
            fixture_mode=self._fixture_mode,
            fixture_delay_ms=self._fixture_delay_ms,
        ))
        adopted = self.adopt_current_search_results(value)
        feeds = adopted.get("feeds", [])
        if not isinstance(feeds, list) or not feeds:
            raise RunAgentError("candidate recovery did not restore visible result cards")
        visible_ids = {
            str(item.get("id", "")) for item in feeds if isinstance(item, dict)
        }
        if expected_candidate_id and expected_candidate_id not in visible_ids:
            raise RunAgentError("candidate recovery did not restore the next saved candidate")
        final_context = self.page_context()
        final_risks = final_context.get("riskSignals", [])
        if (
            final_context.get("pageType") != "search_results"
            or str(final_context.get("query", "")) != value
            or not isinstance(final_risks, list)
            or final_risks
        ):
            raise RunAgentError("candidate recovery final context is invalid")
        return final_context

    def open_search_result(self, expected_note_id: str) -> dict[str, Any]:
        if not expected_note_id.strip():
            raise RunAgentError("expected note id is required")
        return sanitize_run_agent_output(
            self._invoke_raw(
                "open-search-result",
                ["--expected-note-id", expected_note_id.strip()],
                read_only=True,
                _surface=CapabilitySurface.SETUP_READ,
            )
        )

    def post_comment(self, feed_id: str, content: str) -> dict[str, Any]:
        raise RunAgentError(
            "legacy post-comment is frozen; use the approved current-page session"
        )

    def reply_comment(
        self, feed_id: str, content: str, *, comment_id: str = "", user_id: str = ""
    ) -> dict[str, Any]:
        raise RunAgentError(
            "legacy reply-comment is frozen; use the approved current-page session"
        )

    def like_feed(self, feed_id: str) -> dict[str, Any]:
        raise RunAgentError(
            "legacy like-feed is frozen; use the approved current-page session"
        )

    def like_comment(self, feed_id: str, comment_id: str) -> dict[str, Any]:
        raise RunAgentError(
            "legacy like-comment is frozen; use the approved current-page session"
        )

    def _token_for(self, feed_id: str) -> str:
        token = self._token_by_feed.get(feed_id, "")
        if not token:
            raise RunAgentError("feed token is not available in the current in-memory search session")
        return token

    def _invoke_raw(
        self, command: str, args: list[str], *, token: str = "", timeout: int = 120,
        read_only: bool = False,
        _surface: CapabilitySurface = CapabilitySurface.LEGACY_RAW,
    ) -> dict[str, Any]:
        """Compatibility shim routed through the closed product gateway.

        Callers that do not provide an internal product surface are treated as
        raw/legacy and rejected before readiness or connection checks.
        """
        access = CapabilityAccess.READ if read_only else CapabilityAccess.WRITE
        try:
            return self._gateway.execute_vendor_command(
                command,
                args,
                surface=_surface,
                access=access,
                token=token,
                timeout=timeout,
            )
        except XhsCapabilityDeniedError as exc:
            raise RunAgentError(str(exc)) from exc

    def _invoke_authorized(
        self,
        capability: XhsCapability,
        args: list[str],
        token: str,
        timeout: int,
    ) -> dict[str, Any]:
        """Invoke one already-authorized capability through the private pipe."""
        require_approved_xhs_call_method(REQUIRED_XHS_CALL_METHOD)
        journal: PlatformWriteJournal | None = None
        if capability.access is CapabilityAccess.READ:
            self.require_readonly_ready(capability)
        else:
            self.require_execution_ready(capability)
            self.assert_current_account_identity()
            config, _ = load_project_config(self.project_root)
            journal = PlatformWriteJournal(config.runtime.runtime_dir)
        cli_path = self.vendor_root / "scripts" / "cli.py"
        env = os.environ.copy()
        # Never allow a parent shell or test runner to accelerate the real
        # Bridge/vendor process. Fixture timing is product-local only.
        env.pop("TONYREDBOOK_TEST_DELAY_MS", None)
        env["TONYREDBOOK_PRIVATE_PIPE"] = "1"
        # The vendor CLI is a closed transport, not a second public API.  It
        # accepts only the exact capability command selected by the product
        # gateway for this subprocess invocation.
        env["TONYREDBOOK_CAPABILITY_COMMAND"] = capability.vendor_command
        if token:
            env["TONYREDBOOK_XSEC_TOKEN"] = token
        else:
            env.pop("TONYREDBOOK_XSEC_TOKEN", None)
        def invoke_process():
            return subprocess.run(
                [sys.executable, str(cli_path), capability.vendor_command, *args],
                cwd=self.vendor_root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                shell=False,
                check=False,
            )

        def parse_payload(completed, *, attempt=None) -> dict[str, Any]:
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                safe_error = sanitize_run_agent_output(completed.stderr.strip())
                if journal is not None and attempt is not None:
                    journal.unknown(attempt, reason_code="invalid_vendor_json")
                raise RunAgentError(f"Run Agent returned invalid JSON: {safe_error}") from exc
            if completed.returncode != 0:
                safe = sanitize_run_agent_output(payload)
                if journal is not None and attempt is not None:
                    if (
                        safe.get("actionDispatched") is False
                        and safe.get("platform_actions_executed") == 0
                    ):
                        journal.not_dispatched(
                            attempt,
                            reason_code=str(safe.get("failureCode") or "vendor_pre_dispatch_failure"),
                        )
                        return safe
                    journal.unknown(attempt, reason_code="vendor_command_failed")
                failure_code = str(safe.get("failureCode") or "vendor_command_failed")
                raise RunAgentError(
                    f"Run Agent command failed: {safe}",
                    failure_code=failure_code,
                )
            if not isinstance(payload, dict):
                if journal is not None and attempt is not None:
                    journal.unknown(attempt, reason_code="vendor_response_not_object")
                raise RunAgentError("Run Agent response must be an object")
            return payload

        if journal is None:
            return parse_payload(invoke_process())

        with journal.lease():
            attempt = journal.prepare(
                command=capability.vendor_command,
                args=args,
                account_id=self._local_account_id(),
            )
            dispatched = False
            try:
                self._consume_bounded_write_action_if_active()
                journal.dispatched(attempt)
                dispatched = True
                try:
                    completed = invoke_process()
                except BaseException:
                    journal.unknown(attempt, reason_code="transport_exception")
                    raise
                payload = parse_payload(completed, attempt=attempt)
                if (
                    payload.get("actionDispatched") is False
                    and payload.get("platform_actions_executed") == 0
                ):
                    return payload
                if payload.get("verified") is not True or payload.get("platform_actions_executed") != 1:
                    journal.unknown(attempt, reason_code="write_verification_missing")
                    raise RunAgentError("Run Agent write response lacked exact visible verification")
                evidence_hash = hashlib.sha256(
                    json.dumps(
                        sanitize_run_agent_output(payload),
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                journal.verified(attempt, evidence_hash=evidence_hash)
                return payload
            except BaseException:
                if not dispatched:
                    # No platform transport was entered.  The consumed lease is
                    # already fail-closed, but this is not an unknown platform result.
                    raise
                raise
