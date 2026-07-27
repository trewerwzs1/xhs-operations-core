"""One fail-closed preflight contract shared by every V2 Xiaohongshu write."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .storage import append_jsonl, read_json, read_jsonl, write_json_atomic


class ActionPreflightError(ValueError):
    pass


class RuntimeMode(str, Enum):
    OFFLINE = "offline"
    SCOPED_UAT = "scoped_uat"
    AUTONOMOUS_TASK = "autonomous_task"
    RECIPIENT_RELEASE = "recipient_release"


ACTION_KINDS = {
    "publish_image",
    "publish_video",
    "service_comment_reply",
    "service_dm_reply",
    "engage_note_like",
    "engage_note_comment",
    "engage_comment_like",
    "engage_comment_reply",
    "engage_single_dm",
}
VERIFICATION_METHODS = {
    "visible_publish_terminal",
    "exact_visible_comment_increase",
    "exact_visible_reply_increase",
    "exact_visible_outgoing_message_increase",
    "visible_like_state_change",
}


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ActionPreflightError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ActionPreflightError(f"{field} must include timezone")
    return parsed


def _safe_id(value: str, field: str) -> str:
    result = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", result) is None:
        raise ActionPreflightError(f"{field} is invalid")
    return result


def _hash(value: str, field: str) -> str:
    result = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise ActionPreflightError(f"{field} must be SHA-256 hex")
    return result


@dataclass(frozen=True)
class UnifiedActionRequest:
    schema_version: int
    action_id: str
    action_kind: str
    account_id: str
    target_ref_hash: str
    dedupe_key_hash: str
    plan_hash: str
    approval_ref: str
    approval_hash: str
    checked_at: str
    budget_timezone: str
    daily_limit: int
    minimum_interval_seconds: int
    verification_method: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ActionPreflightError("unsupported unified preflight schema")
        _safe_id(self.action_id, "action_id")
        _safe_id(self.account_id, "account_id")
        _safe_id(self.approval_ref, "approval_ref")
        if self.action_kind not in ACTION_KINDS:
            raise ActionPreflightError("action_kind is outside the V2 public surface")
        for field in ("target_ref_hash", "dedupe_key_hash", "plan_hash", "approval_hash"):
            _hash(getattr(self, field), field)
        _aware(self.checked_at, "checked_at")
        try:
            ZoneInfo(self.budget_timezone)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise ActionPreflightError("budget_timezone is invalid") from exc
        if type(self.daily_limit) is not int or not 1 <= self.daily_limit <= 100:
            raise ActionPreflightError("daily_limit must be 1-100")
        if type(self.minimum_interval_seconds) is not int or self.minimum_interval_seconds < 600:
            raise ActionPreflightError("minimum_interval_seconds must be at least 600")
        if self.verification_method not in VERIFICATION_METHODS:
            raise ActionPreflightError("verification_method is not an audited visible readback")

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_action_permit(
        cls,
        permit: Mapping[str, Any] | object,
        *,
        action_id: str,
        dedupe_key_hash: str,
        checked_at: str,
        budget_timezone: str,
        daily_limit: int,
        minimum_interval_seconds: int,
        verification_method: str,
    ) -> "UnifiedActionRequest":
        """Build the legacy-compatible preflight envelope from an internal permit."""

        value = permit.to_dict() if hasattr(permit, "to_dict") else permit
        if not isinstance(value, Mapping):
            raise ActionPreflightError("action permit must be a mapping")
        try:
            from .authority import ActionPermit

            validated = ActionPermit.from_dict(value)
        except (ImportError, ValueError) as exc:
            raise ActionPreflightError("action permit integrity failed") from exc
        checked = _aware(checked_at, "checked_at")
        issued = _aware(validated.issued_at, "permit.issued_at")
        expires = _aware(validated.valid_until, "permit.valid_until")
        if checked < issued or checked > expires:
            raise ActionPreflightError("action permit is outside its validity window")
        return cls(
            schema_version=1,
            action_id=action_id,
            action_kind=validated.action_kind,
            account_id=validated.account_id,
            target_ref_hash=validated.target_ref_hash,
            dedupe_key_hash=dedupe_key_hash,
            plan_hash=validated.plan_hash,
            approval_ref=validated.permit_id,
            approval_hash=validated.content_hash,
            checked_at=checked_at,
            budget_timezone=budget_timezone,
            daily_limit=daily_limit,
            minimum_interval_seconds=minimum_interval_seconds,
            verification_method=verification_method,
        )

    @property
    def permit_ref(self) -> str:
        """Return the internal permit ID; approval_ref is retained for schema v1."""

        return self.approval_ref

    @property
    def permit_hash(self) -> str:
        """Return the internal permit hash; approval_hash is retained for schema v1."""

        return self.approval_hash

    @property
    def request_hash(self) -> str:
        return sha256(
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class UnifiedPreflightState:
    platform_access_allowed: bool
    login_ready: bool
    account_identity_ready: bool
    target_ready: bool
    approval_ready: bool
    capability_ready: bool
    exact_lease_ready: bool = False
    additional_blockers: tuple[str, ...] = ()
    runtime_mode: RuntimeMode = RuntimeMode.OFFLINE
    scoped_uat_authorized: bool = False
    scoped_uat_actions_remaining: int = 0
    recipient_execution_enabled: bool = False
    recipient_release_ready: bool = False
    recipient_known_blockers: tuple[str, ...] = ()
    mandate_ready: bool | None = None
    permit_ready: bool | None = None
    permit_actions_remaining: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_mode, RuntimeMode):
            raise ActionPreflightError("runtime_mode must be a RuntimeMode")
        if (
            type(self.scoped_uat_actions_remaining) is not int
            or self.scoped_uat_actions_remaining < 0
        ):
            raise ActionPreflightError("scoped_uat_actions_remaining must be non-negative")
        for field in ("mandate_ready", "permit_ready"):
            value = getattr(self, field)
            if value is not None and type(value) is not bool:
                raise ActionPreflightError(f"{field} must be boolean or null")
        if (
            self.permit_actions_remaining is not None
            and (
                type(self.permit_actions_remaining) is not int
                or self.permit_actions_remaining < 0
            )
        ):
            raise ActionPreflightError("permit_actions_remaining must be non-negative or null")


@dataclass(frozen=True)
class UnifiedPreflightDecision:
    decision_id: str
    request_hash: str
    phase: str
    allowed: bool
    blockers: tuple[str, ...]
    prior_daily_count: int
    eligible_at: str
    duplicate: bool
    checked_at: str
    runtime_mode: str
    platform_actions_executed: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "request_hash": self.request_hash,
            "phase": self.phase,
            "allowed": self.allowed,
            "blockers": list(self.blockers),
            "prior_daily_count": self.prior_daily_count,
            "eligible_at": self.eligible_at,
            "duplicate": self.duplicate,
            "checked_at": self.checked_at,
            "runtime_mode": self.runtime_mode,
            "platform_actions_executed": self.platform_actions_executed,
        }


class UnifiedActionPreflightStore:
    """Evaluate and audit the one shared V2 write boundary.

    Platform dispatch and exact visible readback remain inside Run Agent. This
    store owns the product-level account, target, approval, duplicate, pacing
    and result-confirmation contract used before that dispatch.
    """

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.root = self.runtime_dir / "action_preflight"
        self.decisions_path = self.root / "decisions.jsonl"
        self.results_path = self.root / "results.jsonl"
        self.platform_journal_path = self.runtime_dir / "run_agent" / "write_journal.jsonl"
        self.stop_path = self.runtime_dir / "comment_flow" / "STOP.json"

    def _platform_verified(self, account_id: str) -> list[dict[str, Any]]:
        prepared: dict[str, dict[str, Any]] = {}
        verified: list[dict[str, Any]] = []
        for row in read_jsonl(self.platform_journal_path):
            attempt_id = row.get("attempt_id")
            if not isinstance(attempt_id, str):
                continue
            if row.get("status") == "prepared":
                prepared[attempt_id] = row
            elif row.get("status") == "verified":
                source = prepared.get(attempt_id)
                if isinstance(source, dict) and source.get("account_id") == account_id:
                    verified.append({**row, "account_id": account_id})
        return verified

    def _duplicate(self, request: UnifiedActionRequest) -> bool:
        return self._duplicate_key(
            account_id=request.account_id,
            dedupe_key_hash=request.dedupe_key_hash,
        )

    def _duplicate_key(self, *, account_id: str, dedupe_key_hash: str) -> bool:
        return any(
            (
                row.get("status") == "verified"
                or (
                    row.get("status") == "unknown"
                    and row.get("do_not_retry") is True
                )
            )
            and row.get("account_id") == account_id
            and row.get("dedupe_key_hash") == dedupe_key_hash
            for row in read_jsonl(self.results_path)
        )

    def policy_snapshot(
        self,
        *,
        account_id: str,
        dedupe_key_hash: str,
        checked_at: str,
        budget_timezone: str,
        daily_limit: int,
        minimum_interval_seconds: int,
    ) -> dict[str, Any]:
        """Return the deterministic budget inputs used by autonomous policy.

        This is a read-only projection.  It does not create a permit, lease,
        decision, or platform action.
        """

        _safe_id(account_id, "account_id")
        _hash(dedupe_key_hash, "dedupe_key_hash")
        checked = _aware(checked_at, "checked_at")
        try:
            zone = ZoneInfo(budget_timezone)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise ActionPreflightError("budget_timezone is invalid") from exc
        if type(daily_limit) is not int or not 1 <= daily_limit <= 100:
            raise ActionPreflightError("daily_limit must be 1-100")
        if (
            type(minimum_interval_seconds) is not int
            or minimum_interval_seconds < 600
        ):
            raise ActionPreflightError(
                "minimum_interval_seconds must be at least 600"
            )
        verified = self._platform_verified(account_id)
        same_day: list[dict[str, Any]] = []
        moments: list[datetime] = []
        for row in verified:
            try:
                moment = _aware(
                    str(row.get("recorded_at") or ""),
                    "journal.recorded_at",
                )
            except ActionPreflightError:
                continue
            moments.append(moment)
            if moment.astimezone(zone).date() == checked.astimezone(zone).date():
                same_day.append(row)
        latest = max(moments) if moments else None
        eligible = (
            latest + timedelta(seconds=minimum_interval_seconds)
            if latest is not None
            else checked
        )
        return {
            "duplicate": self._duplicate_key(
                account_id=account_id,
                dedupe_key_hash=dedupe_key_hash,
            ),
            "prior_daily_count": len(same_day),
            "daily_budget_ready": len(same_day) < daily_limit,
            "pacing_ready": checked.astimezone(timezone.utc)
            >= eligible.astimezone(timezone.utc),
            "eligible_at": eligible.isoformat(),
            "platform_actions_executed": 0,
        }

    def evaluate(
        self,
        request: UnifiedActionRequest,
        *,
        phase: str,
        state: UnifiedPreflightState,
        record: bool = True,
    ) -> UnifiedPreflightDecision:
        if phase not in {"authorize", "execute"}:
            raise ActionPreflightError("preflight phase must be authorize or execute")
        snapshot = self.policy_snapshot(
            account_id=request.account_id,
            dedupe_key_hash=request.dedupe_key_hash,
            checked_at=request.checked_at,
            budget_timezone=request.budget_timezone,
            daily_limit=request.daily_limit,
            minimum_interval_seconds=request.minimum_interval_seconds,
        )
        eligible = _aware(str(snapshot["eligible_at"]), "eligible_at")
        duplicate = bool(snapshot["duplicate"])
        blockers: list[str] = []
        if state.runtime_mode is RuntimeMode.OFFLINE:
            blockers.append("offline_mode_blocks_writes")
        elif state.runtime_mode is RuntimeMode.SCOPED_UAT:
            if not state.scoped_uat_authorized:
                blockers.append("scoped_uat_exact_authorization_missing")
            if state.scoped_uat_actions_remaining != 1:
                blockers.append("scoped_uat_exact_action_budget_required")
        elif state.runtime_mode is RuntimeMode.AUTONOMOUS_TASK:
            if state.mandate_ready is not True:
                blockers.append("execution_mandate_not_ready")
            if state.permit_ready is not True:
                blockers.append("internal_action_permit_not_ready")
            if state.permit_actions_remaining != 1:
                blockers.append("internal_single_action_permit_required")
        elif state.runtime_mode is RuntimeMode.RECIPIENT_RELEASE:
            if not state.recipient_execution_enabled:
                blockers.append("recipient_release_execution_disabled")
            if not state.recipient_release_ready:
                blockers.append("recipient_release_not_ready")
            if state.recipient_known_blockers:
                blockers.append("recipient_release_has_blockers")
        else:
            blockers.append("runtime_mode_invalid")
        if not state.platform_access_allowed:
            blockers.append("platform_access_disabled")
        if not state.login_ready:
            blockers.append("login_not_ready")
        if not state.account_identity_ready:
            blockers.append("account_identity_not_ready")
        if not state.target_ready:
            blockers.append("exact_target_not_ready")
        if state.runtime_mode is not RuntimeMode.AUTONOMOUS_TASK and not state.approval_ready:
            blockers.append("exact_approval_not_ready")
        if not state.capability_ready:
            blockers.append("capability_not_ready")
        if duplicate:
            blockers.append("duplicate_target_action")
        if not snapshot["daily_budget_ready"]:
            blockers.append("daily_write_limit_reached")
        if not snapshot["pacing_ready"]:
            blockers.append("minimum_global_write_interval_not_elapsed")
        stop = read_json(self.stop_path, default=None)
        if not isinstance(stop, Mapping):
            blockers.append("global_stop_state_missing")
        elif stop.get("requires_manual_reconciliation") is True:
            blockers.append("unresolved_unknown_write")
        elif phase == "authorize" and stop.get("writes_allowed") is not False:
            blockers.append("another_write_lease_active")
        elif phase == "execute":
            if state.runtime_mode is RuntimeMode.AUTONOMOUS_TASK:
                if (
                    stop.get("writes_allowed") is not False
                    or not state.exact_lease_ready
                ):
                    blockers.append("internal_action_permit_lease_required")
            elif stop.get("writes_allowed") is not True or not state.exact_lease_ready:
                blockers.append("exact_active_write_lease_required")
        for blocker in state.additional_blockers:
            value = str(blocker or "").strip()
            if value and value not in blockers:
                blockers.append(value)
        request_hash = request.request_hash
        decision = UnifiedPreflightDecision(
            decision_id="preflight_" + sha256(
                f"{request_hash}|{phase}|{request.checked_at}".encode("utf-8")
            ).hexdigest()[:20],
            request_hash=request_hash,
            phase=phase,
            allowed=not blockers,
            blockers=tuple(blockers),
            prior_daily_count=int(snapshot["prior_daily_count"]),
            eligible_at=eligible.isoformat(),
            duplicate=duplicate,
            checked_at=request.checked_at,
            runtime_mode=state.runtime_mode.value,
        )
        if record:
            append_jsonl(self.decisions_path, {
                **decision.to_dict(),
                "account_id": request.account_id,
                "action_kind": request.action_kind,
                "target_ref_hash": request.target_ref_hash,
                "dedupe_key_hash": request.dedupe_key_hash,
                "plan_hash": request.plan_hash,
                "approval_ref": request.approval_ref,
                "approval_hash": request.approval_hash,
                "permit_ref": request.permit_ref,
                "permit_hash": request.permit_hash,
                "verification_method": request.verification_method,
                "runtime_mode": state.runtime_mode.value,
                "raw_target_retained": False,
                "raw_content_retained": False,
            })
        return decision

    def record_result(
        self,
        request: UnifiedActionRequest,
        *,
        status: str,
        recorded_at: str,
        evidence_hash: str = "",
        reason_code: str = "",
    ) -> dict[str, Any]:
        if status not in {"verified", "not_dispatched", "unknown"}:
            raise ActionPreflightError("unified action result status is invalid")
        _aware(recorded_at, "recorded_at")
        if status == "verified":
            _hash(evidence_hash, "evidence_hash")
        elif not _safe_id(reason_code, "reason_code"):
            raise ActionPreflightError("reason_code is required")
        row = {
            "schema_version": 1,
            "result_id": "action_result_" + sha256(
                f"{request.request_hash}|{status}|{recorded_at}".encode("utf-8")
            ).hexdigest()[:20],
            "request_hash": request.request_hash,
            "account_id": request.account_id,
            "action_kind": request.action_kind,
            "target_ref_hash": request.target_ref_hash,
            "dedupe_key_hash": request.dedupe_key_hash,
            "plan_hash": request.plan_hash,
            "status": status,
            "evidence_hash": evidence_hash,
            "reason_code": reason_code,
            "verification_method": request.verification_method,
            "recorded_at": recorded_at,
            "do_not_retry": status == "unknown",
            "platform_actions_executed": 1 if status == "verified" else 0,
        }
        for existing in read_jsonl(self.results_path):
            if existing.get("result_id") != row["result_id"]:
                continue
            if existing != row:
                raise ActionPreflightError("unified action result_id collision")
            return existing
        append_jsonl(self.results_path, row)
        if status == "unknown":
            current_stop = read_json(self.stop_path, default=None)
            if not (
                isinstance(current_stop, Mapping)
                and current_stop.get("requires_manual_reconciliation") is True
            ):
                write_json_atomic(self.stop_path, {
                    "schema_version": 2,
                    "writes_allowed": False,
                    "reason": "unknown_unified_action_result",
                    "result_id": row["result_id"],
                    "request_hash": request.request_hash,
                    "recorded_at": recorded_at,
                    "requires_manual_reconciliation": True,
                })
        return row
