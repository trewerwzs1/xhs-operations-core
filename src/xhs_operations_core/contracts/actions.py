"""Auditable contracts for every platform-changing action."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4


class ActionContractError(ValueError):
    """Raised when an action record lacks mandatory evidence."""


class ActionType(str, Enum):
    LIKE = "like"
    COMMENT = "comment"
    REPLY = "reply"
    DM = "dm"


class RunMode(str, Enum):
    PREVIEW = "preview"
    SMOKE = "smoke"
    LIMITED_RUN = "limited_run"


class ActionStatus(str, Enum):
    PLANNED = "planned"
    BLOCKED = "blocked"
    EXECUTING = "executing"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TextSource(str, Enum):
    NONE = "none"
    CODEX_GENERATED = "codex_generated"
    USER_EXACT = "user_exact"
    APPROVED_DRAFT = "approved_draft"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    clean = prefix.strip().lower().replace(" ", "-")
    if not clean or not clean.replace("-", "").isalnum():
        raise ActionContractError("id prefix must contain only letters, numbers, or hyphens")
    return f"{clean}_{uuid4().hex}"


def _require_non_empty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ActionContractError(f"{name} must be a non-empty string")


def _parse_aware_timestamp(name: str, value: str) -> datetime:
    _require_non_empty(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActionContractError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ActionContractError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _enum_value(enum_type: type[Enum], value: Any, field_name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise ActionContractError(f"{field_name} must be one of: {allowed}") from exc


@dataclass(frozen=True)
class ValidatorDecision:
    allowed: bool
    evaluated_at: str
    reason_codes: tuple[str, ...] = ()
    fact_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _parse_aware_timestamp("validator.evaluated_at", self.evaluated_at)
        if not self.allowed and not self.reason_codes:
            raise ActionContractError("blocked validator decision requires reason_codes")
        for fact_ref in self.fact_refs:
            _require_non_empty("validator.fact_ref", fact_ref)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    level: RiskLevel
    evaluated_at: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", _enum_value(RiskLevel, self.level, "risk.level"))
        _parse_aware_timestamp("risk.evaluated_at", self.evaluated_at)
        if self.allowed and self.level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            raise ActionContractError("high or critical risk cannot be allowed")
        if not self.allowed and not self.reason_codes:
            raise ActionContractError("blocked risk decision requires reason_codes")


@dataclass(frozen=True)
class ThrottleDecision:
    allowed: bool
    evaluated_at: str
    eligible_at: str
    daily_count: int
    daily_limit: int
    minimum_interval_seconds: int
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        evaluated = _parse_aware_timestamp("throttle.evaluated_at", self.evaluated_at)
        eligible = _parse_aware_timestamp("throttle.eligible_at", self.eligible_at)
        if self.daily_count < 0:
            raise ActionContractError("throttle.daily_count cannot be negative")
        if self.daily_limit < 1:
            raise ActionContractError("throttle.daily_limit must be positive")
        if self.minimum_interval_seconds < 0:
            raise ActionContractError("throttle.minimum_interval_seconds cannot be negative")
        if self.allowed and self.daily_count >= self.daily_limit:
            raise ActionContractError("throttle cannot allow an action at or above daily_limit")
        if self.allowed and evaluated < eligible:
            raise ActionContractError("throttle cannot allow an action before eligible_at")
        if not self.allowed and not self.reason_codes:
            raise ActionContractError("blocked throttle decision requires reason_codes")


@dataclass(frozen=True)
class ActionRecord:
    record_id: str
    run_id: str
    campaign_id: str
    account_id: str
    candidate_id: str
    interaction_plan_id: str
    action_type: ActionType
    run_mode: RunMode
    status: ActionStatus
    created_at: str
    source_context_ref: str
    text_source: TextSource
    validator: ValidatorDecision
    risk: RiskDecision
    throttle: ThrottleDecision
    output_text: str | None = None
    result_ref: str | None = None
    error_code: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "action_type", _enum_value(ActionType, self.action_type, "action_type")
        )
        object.__setattr__(self, "run_mode", _enum_value(RunMode, self.run_mode, "run_mode"))
        object.__setattr__(self, "status", _enum_value(ActionStatus, self.status, "status"))
        object.__setattr__(
            self, "text_source", _enum_value(TextSource, self.text_source, "text_source")
        )

        for name in (
            "record_id",
            "run_id",
            "campaign_id",
            "account_id",
            "candidate_id",
            "interaction_plan_id",
            "source_context_ref",
        ):
            _require_non_empty(name, getattr(self, name))
        created = _parse_aware_timestamp("created_at", self.created_at)

        decision_times = {
            "validator": _parse_aware_timestamp(
                "validator.evaluated_at", self.validator.evaluated_at
            ),
            "risk": _parse_aware_timestamp("risk.evaluated_at", self.risk.evaluated_at),
            "throttle": _parse_aware_timestamp(
                "throttle.evaluated_at", self.throttle.evaluated_at
            ),
        }
        for decision_name, evaluated_at in decision_times.items():
            if evaluated_at > created:
                raise ActionContractError(
                    f"{decision_name} decision cannot be newer than ActionRecord"
                )

        text_actions = {ActionType.COMMENT, ActionType.REPLY, ActionType.DM}
        if self.action_type in text_actions:
            has_text = isinstance(self.output_text, str) and bool(self.output_text.strip())
            if self.status is ActionStatus.BLOCKED and not has_text:
                if self.text_source is not TextSource.NONE:
                    raise ActionContractError(
                        "blocked text action without output_text must use text_source=none"
                    )
                if self.validator.allowed:
                    raise ActionContractError(
                        "missing text can only be recorded when validator blocks the action"
                    )
            else:
                if self.text_source is TextSource.NONE:
                    raise ActionContractError("text action requires a traceable text_source")
                if not has_text:
                    raise ActionContractError("text action requires non-empty output_text")
        elif self.action_type is ActionType.LIKE:
            if self.text_source is not TextSource.NONE or self.output_text is not None:
                raise ActionContractError("like action cannot carry output_text or text_source")

        gates_allowed = self.validator.allowed and self.risk.allowed and self.throttle.allowed
        if self.status is ActionStatus.BLOCKED:
            if gates_allowed:
                raise ActionContractError("blocked action requires at least one blocked gate")
        elif not gates_allowed:
            raise ActionContractError(f"{self.status.value} action requires all gates allowed")

        live_statuses = {
            ActionStatus.EXECUTING,
            ActionStatus.VERIFIED,
            ActionStatus.FAILED,
            ActionStatus.UNKNOWN,
        }
        if self.run_mode is RunMode.PREVIEW and self.status in live_statuses:
            raise ActionContractError("preview mode cannot execute or report a live action")

        if self.status is ActionStatus.VERIFIED:
            _require_non_empty("result_ref", self.result_ref or "")
        elif self.result_ref is not None:
            raise ActionContractError("result_ref is only valid for verified actions")

        if self.status in {ActionStatus.FAILED, ActionStatus.UNKNOWN}:
            _require_non_empty("error_code", self.error_code or "")
        elif self.error_code is not None:
            raise ActionContractError("error_code is only valid for failed or unknown actions")

        try:
            json.dumps(self.metadata, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ActionContractError("metadata must be JSON serializable") from exc

    @property
    def gate_allowed(self) -> bool:
        return self.validator.allowed and self.risk.allowed and self.throttle.allowed

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action_type"] = self.action_type.value
        payload["run_mode"] = self.run_mode.value
        payload["status"] = self.status.value
        payload["text_source"] = self.text_source.value
        payload["risk"]["level"] = self.risk.level.value
        payload["validator"]["reason_codes"] = list(self.validator.reason_codes)
        payload["validator"]["fact_refs"] = list(self.validator.fact_refs)
        payload["risk"]["reason_codes"] = list(self.risk.reason_codes)
        payload["throttle"]["reason_codes"] = list(self.throttle.reason_codes)
        payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionRecord":
        try:
            allowed_fields = {
                "record_id",
                "run_id",
                "campaign_id",
                "account_id",
                "candidate_id",
                "interaction_plan_id",
                "action_type",
                "run_mode",
                "status",
                "created_at",
                "source_context_ref",
                "text_source",
                "validator",
                "risk",
                "throttle",
                "output_text",
                "result_ref",
                "error_code",
                "metadata",
            }
            unknown_fields = set(payload) - allowed_fields
            if unknown_fields:
                raise ActionContractError(
                    f"unknown ActionRecord fields: {', '.join(sorted(unknown_fields))}"
                )
            validator_payload = dict(payload["validator"])
            risk_payload = dict(payload["risk"])
            throttle_payload = dict(payload["throttle"])
            validator_payload["reason_codes"] = tuple(
                validator_payload.get("reason_codes", ())
            )
            validator_payload["fact_refs"] = tuple(validator_payload.get("fact_refs", ()))
            risk_payload["reason_codes"] = tuple(risk_payload.get("reason_codes", ()))
            throttle_payload["reason_codes"] = tuple(
                throttle_payload.get("reason_codes", ())
            )
            return cls(
                record_id=payload["record_id"],
                run_id=payload["run_id"],
                campaign_id=payload["campaign_id"],
                account_id=payload["account_id"],
                candidate_id=payload["candidate_id"],
                interaction_plan_id=payload["interaction_plan_id"],
                action_type=payload["action_type"],
                run_mode=payload["run_mode"],
                status=payload["status"],
                created_at=payload["created_at"],
                source_context_ref=payload["source_context_ref"],
                text_source=payload["text_source"],
                validator=ValidatorDecision(**validator_payload),
                risk=RiskDecision(**risk_payload),
                throttle=ThrottleDecision(**throttle_payload),
                output_text=payload.get("output_text"),
                result_ref=payload.get("result_ref"),
                error_code=payload.get("error_code"),
                metadata=payload.get("metadata", {}),
            )
        except ActionContractError:
            raise
        except (KeyError, TypeError) as exc:
            raise ActionContractError(f"invalid ActionRecord payload: {exc}") from exc
