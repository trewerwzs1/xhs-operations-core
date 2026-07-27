"""Task-scoped autonomous execution authority.

The user's task is the product intent boundary.  Exact action permits are
derived locally from an immutable mandate and never require a second user
confirmation token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .storage import append_jsonl, read_jsonl, update_json_object


class AuthorityContractError(ValueError):
    """Raised when autonomous authority data is incomplete or unsafe."""


WORKFLOW_ACTIONS: dict[str, frozenset[str]] = {
    "setup": frozenset(),
    "review": frozenset(),
    "publish": frozenset({"publish_image", "publish_video"}),
    "service": frozenset({"service_comment_reply", "service_dm_reply"}),
    "engage": frozenset({
        "engage_note_like",
        "engage_note_comment",
        "engage_comment_like",
        "engage_comment_reply",
        "engage_single_dm",
    }),
}
SOURCE_MODES = {"account_note", "specified_note", "direct_brief", "service_queue", "publish_brief"}
WORKFLOW_SOURCE_MODES: dict[str, frozenset[str]] = {
    "setup": frozenset({"direct_brief"}),
    "review": frozenset({"direct_brief"}),
    "publish": frozenset({"publish_brief", "direct_brief"}),
    "service": frozenset({"service_queue", "direct_brief"}),
    "engage": frozenset({"account_note", "specified_note", "direct_brief"}),
}
TEXT_ACTIONS = {
    "publish_image",
    "publish_video",
    "service_comment_reply",
    "service_dm_reply",
    "engage_note_comment",
    "engage_comment_reply",
    "engage_single_dm",
}
ACTIVE_DM_ACTION = "engage_single_dm"
PUBLISH_ACTIONS = {"publish_image", "publish_video"}
LEGACY_ENGAGE_ACTIONS: dict[str, tuple[str, ...]] = {
    "like": ("engage_note_like", "engage_comment_like"),
    "comment": ("engage_note_comment",),
    "reply": ("engage_comment_reply",),
    "dm": ("engage_single_dm",),
}


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _safe_id(value: object, field: str, *, optional: bool = False) -> str:
    text = str(value or "").strip()
    if optional and not text:
        return ""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", text) is None:
        raise AuthorityContractError(f"{field} is invalid")
    return text


def _hash(value: object, field: str, *, optional: bool = False) -> str:
    text = str(value or "").strip()
    if optional and not text:
        return ""
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise AuthorityContractError(f"{field} must be SHA-256 hex")
    return text


def _text(value: object, field: str, *, limit: int = 2000, optional: bool = False) -> str:
    text = str(value or "").strip()
    if optional and not text:
        return ""
    if not text or len(text) > limit:
        raise AuthorityContractError(f"{field} is invalid")
    return text


def _moment(value: object, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AuthorityContractError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorityContractError(f"{field} must include timezone")
    return parsed.isoformat()


def _datetime(value: object, field: str) -> datetime:
    return datetime.fromisoformat(_moment(value, field).replace("Z", "+00:00"))


def _strings(
    values: Sequence[object],
    field: str,
    *,
    allowed: set[str] | frozenset[str] | None = None,
    maximum: int = 32,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise AuthorityContractError(f"{field} must be a list")
    result = tuple(str(item or "").strip() for item in values)
    if len(result) > maximum or any(not item for item in result) or len(set(result)) != len(result):
        raise AuthorityContractError(f"{field} is invalid")
    if allowed is not None and any(item not in allowed for item in result):
        raise AuthorityContractError(f"{field} contains an unsupported value")
    return result


def _ids(values: Sequence[object], field: str, *, maximum: int = 64) -> tuple[str, ...]:
    return tuple(_safe_id(value, field) for value in _strings(values, field, maximum=maximum))


def _caps(values: Mapping[str, object], allowed_actions: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(values, Mapping) or set(values) != set(allowed_actions):
        raise AuthorityContractError("daily_caps must exactly match allowed_actions")
    result: dict[str, int] = {}
    for action in allowed_actions:
        value = values[action]
        if type(value) is not int or not 1 <= value <= 20:
            raise AuthorityContractError("daily cap must be 1-20")
        result[action] = value
    return result


@dataclass(frozen=True)
class TaskIntent:
    schema_version: int
    intent_id: str
    account_id: str
    workflow: str
    instruction: str
    source_mode: str
    source_ref: str
    source_hash: str
    requested_actions: tuple[str, ...]
    created_at: str
    content_hash: str

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        workflow: str,
        instruction: str,
        source_mode: str,
        source_ref: str,
        source_hash: str,
        requested_actions: Sequence[str],
        created_at: str,
    ) -> "TaskIntent":
        account_id = _safe_id(account_id, "account_id")
        if workflow not in WORKFLOW_ACTIONS:
            raise AuthorityContractError("workflow is unsupported")
        instruction = _text(instruction, "instruction")
        if source_mode not in SOURCE_MODES:
            raise AuthorityContractError("source_mode is unsupported")
        if source_mode not in WORKFLOW_SOURCE_MODES[workflow]:
            raise AuthorityContractError("source_mode does not match workflow")
        source_ref = _text(source_ref, "source_ref", limit=500)
        source_hash = _hash(source_hash, "source_hash")
        actions = _strings(
            requested_actions,
            "requested_actions",
            allowed=WORKFLOW_ACTIONS[workflow],
            maximum=len(WORKFLOW_ACTIONS[workflow]),
        )
        created_at = _moment(created_at, "created_at")
        payload: dict[str, object] = {
            "schema_version": 1,
            "account_id": account_id,
            "workflow": workflow,
            "instruction": instruction,
            "source_mode": source_mode,
            "source_ref": source_ref,
            "source_hash": source_hash,
            "requested_actions": list(actions),
            "created_at": created_at,
        }
        digest = _canonical_hash(payload)
        return cls(
            schema_version=1,
            intent_id="intent_" + digest[:20],
            account_id=account_id,
            workflow=workflow,
            instruction=instruction,
            source_mode=source_mode,
            source_ref=source_ref,
            source_hash=source_hash,
            requested_actions=actions,
            created_at=created_at,
            content_hash=digest,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskIntent":
        expected = {
            "schema_version",
            "intent_id",
            "account_id",
            "workflow",
            "instruction",
            "source_mode",
            "source_ref",
            "source_hash",
            "requested_actions",
            "created_at",
            "content_hash",
        }
        if set(value) != expected or value.get("schema_version") != 1:
            raise AuthorityContractError("TaskIntent fields are incomplete or unknown")
        rebuilt = cls.create(
            account_id=value["account_id"],
            workflow=value["workflow"],
            instruction=value["instruction"],
            source_mode=value["source_mode"],
            source_ref=value["source_ref"],
            source_hash=value["source_hash"],
            requested_actions=value["requested_actions"],
            created_at=value["created_at"],
        )
        if value["intent_id"] != rebuilt.intent_id or value["content_hash"] != rebuilt.content_hash:
            raise AuthorityContractError("TaskIntent integrity failed")
        return rebuilt

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "intent_id": self.intent_id,
            "account_id": self.account_id,
            "workflow": self.workflow,
            "instruction": self.instruction,
            "source_mode": self.source_mode,
            "source_ref": self.source_ref,
            "source_hash": self.source_hash,
            "requested_actions": list(self.requested_actions),
            "created_at": self.created_at,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ExecutionMandate:
    schema_version: int
    mandate_id: str
    intent_id: str
    intent_hash: str
    account_id: str
    workflow: str
    strategy_pack_id: str
    strategy_pack_hash: str
    campaign_id: str
    valid_from: str
    valid_until: str
    timezone: str
    allowed_actions: tuple[str, ...]
    daily_caps: Mapping[str, int]
    minimum_interval_seconds: int
    allowed_fact_ids: tuple[str, ...]
    active_dm_allowed: bool
    publish_allowed: bool
    max_actions_per_heartbeat: int
    created_at: str
    content_hash: str

    @classmethod
    def from_intent(
        cls,
        intent: TaskIntent,
        *,
        valid_from: str,
        valid_until: str,
        timezone_name: str,
        daily_caps: Mapping[str, int],
        minimum_interval_seconds: int = 600,
        allowed_actions: Sequence[str] | None = None,
        strategy_pack_id: str = "",
        strategy_pack_hash: str = "",
        campaign_id: str = "",
        allowed_fact_ids: Sequence[str] = (),
        created_at: str | None = None,
    ) -> "ExecutionMandate":
        actions = _strings(
            list(intent.requested_actions if allowed_actions is None else allowed_actions),
            "allowed_actions",
            allowed=WORKFLOW_ACTIONS[intent.workflow],
            maximum=len(WORKFLOW_ACTIONS[intent.workflow]),
        )
        if not set(actions).issubset(intent.requested_actions):
            raise AuthorityContractError("mandate cannot expand TaskIntent actions")
        valid_from = _moment(valid_from, "valid_from")
        valid_until = _moment(valid_until, "valid_until")
        if _datetime(valid_until, "valid_until") <= _datetime(valid_from, "valid_from"):
            raise AuthorityContractError("valid_until must be after valid_from")
        try:
            ZoneInfo(timezone_name)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise AuthorityContractError("timezone is invalid") from exc
        if type(minimum_interval_seconds) is not int or minimum_interval_seconds < 600:
            raise AuthorityContractError("minimum_interval_seconds must be at least 600")
        caps = _caps(daily_caps, actions)
        strategy_pack_id = _safe_id(strategy_pack_id, "strategy_pack_id", optional=True)
        strategy_pack_hash = _hash(strategy_pack_hash, "strategy_pack_hash", optional=True)
        if bool(strategy_pack_id) != bool(strategy_pack_hash):
            raise AuthorityContractError("strategy_pack_id and strategy_pack_hash are required together")
        campaign_id = _safe_id(campaign_id, "campaign_id", optional=True)
        fact_ids = _ids(allowed_fact_ids, "allowed_fact_ids", maximum=64)
        created_at = _moment(created_at or intent.created_at, "created_at")
        payload: dict[str, object] = {
            "schema_version": 1,
            "intent_id": intent.intent_id,
            "intent_hash": intent.content_hash,
            "account_id": intent.account_id,
            "workflow": intent.workflow,
            "strategy_pack_id": strategy_pack_id,
            "strategy_pack_hash": strategy_pack_hash,
            "campaign_id": campaign_id,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "timezone": timezone_name,
            "allowed_actions": list(actions),
            "daily_caps": caps,
            "minimum_interval_seconds": minimum_interval_seconds,
            "allowed_fact_ids": list(fact_ids),
            "active_dm_allowed": ACTIVE_DM_ACTION in actions,
            "publish_allowed": bool(PUBLISH_ACTIONS.intersection(actions)),
            "max_actions_per_heartbeat": 1,
            "created_at": created_at,
        }
        digest = _canonical_hash(payload)
        return cls(
            schema_version=1,
            mandate_id="mandate_" + digest[:20],
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash,
            account_id=intent.account_id,
            workflow=intent.workflow,
            strategy_pack_id=strategy_pack_id,
            strategy_pack_hash=strategy_pack_hash,
            campaign_id=campaign_id,
            valid_from=valid_from,
            valid_until=valid_until,
            timezone=timezone_name,
            allowed_actions=actions,
            daily_caps=caps,
            minimum_interval_seconds=minimum_interval_seconds,
            allowed_fact_ids=fact_ids,
            active_dm_allowed=ACTIVE_DM_ACTION in actions,
            publish_allowed=bool(PUBLISH_ACTIONS.intersection(actions)),
            max_actions_per_heartbeat=1,
            created_at=created_at,
            content_hash=digest,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionMandate":
        expected = {
            "schema_version",
            "mandate_id",
            "intent_id",
            "intent_hash",
            "account_id",
            "workflow",
            "strategy_pack_id",
            "strategy_pack_hash",
            "campaign_id",
            "valid_from",
            "valid_until",
            "timezone",
            "allowed_actions",
            "daily_caps",
            "minimum_interval_seconds",
            "allowed_fact_ids",
            "active_dm_allowed",
            "publish_allowed",
            "max_actions_per_heartbeat",
            "created_at",
            "content_hash",
        }
        if set(value) != expected or value.get("schema_version") != 1:
            raise AuthorityContractError("ExecutionMandate fields are incomplete or unknown")
        workflow = str(value["workflow"])
        if workflow not in WORKFLOW_ACTIONS:
            raise AuthorityContractError("mandate workflow is unsupported")
        actions = _strings(
            value["allowed_actions"],
            "allowed_actions",
            allowed=WORKFLOW_ACTIONS[workflow],
            maximum=len(WORKFLOW_ACTIONS[workflow]),
        )
        caps = _caps(value["daily_caps"], actions)
        valid_from = _moment(value["valid_from"], "valid_from")
        valid_until = _moment(value["valid_until"], "valid_until")
        if _datetime(valid_until, "valid_until") <= _datetime(valid_from, "valid_from"):
            raise AuthorityContractError("valid_until must be after valid_from")
        try:
            ZoneInfo(str(value["timezone"]))
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise AuthorityContractError("timezone is invalid") from exc
        if type(value["minimum_interval_seconds"]) is not int or value["minimum_interval_seconds"] < 600:
            raise AuthorityContractError("minimum_interval_seconds must be at least 600")
        if value["max_actions_per_heartbeat"] != 1:
            raise AuthorityContractError("max_actions_per_heartbeat must be one")
        fact_ids = _ids(value["allowed_fact_ids"], "allowed_fact_ids", maximum=64)
        strategy_pack_id = _safe_id(value["strategy_pack_id"], "strategy_pack_id", optional=True)
        strategy_pack_hash = _hash(value["strategy_pack_hash"], "strategy_pack_hash", optional=True)
        if bool(strategy_pack_id) != bool(strategy_pack_hash):
            raise AuthorityContractError("strategy binding is incomplete")
        payload: dict[str, object] = {
            "schema_version": 1,
            "intent_id": _safe_id(value["intent_id"], "intent_id"),
            "intent_hash": _hash(value["intent_hash"], "intent_hash"),
            "account_id": _safe_id(value["account_id"], "account_id"),
            "workflow": workflow,
            "strategy_pack_id": strategy_pack_id,
            "strategy_pack_hash": strategy_pack_hash,
            "campaign_id": _safe_id(value["campaign_id"], "campaign_id", optional=True),
            "valid_from": valid_from,
            "valid_until": valid_until,
            "timezone": str(value["timezone"]),
            "allowed_actions": list(actions),
            "daily_caps": caps,
            "minimum_interval_seconds": value["minimum_interval_seconds"],
            "allowed_fact_ids": list(fact_ids),
            "active_dm_allowed": value["active_dm_allowed"],
            "publish_allowed": value["publish_allowed"],
            "max_actions_per_heartbeat": 1,
            "created_at": _moment(value["created_at"], "created_at"),
        }
        if type(payload["active_dm_allowed"]) is not bool or type(payload["publish_allowed"]) is not bool:
            raise AuthorityContractError("mandate action flags must be booleans")
        if payload["active_dm_allowed"] != (ACTIVE_DM_ACTION in actions):
            raise AuthorityContractError("active_dm_allowed does not match allowed_actions")
        if payload["publish_allowed"] != bool(PUBLISH_ACTIONS.intersection(actions)):
            raise AuthorityContractError("publish_allowed does not match allowed_actions")
        digest = _canonical_hash(payload)
        if value["mandate_id"] != "mandate_" + digest[:20] or value["content_hash"] != digest:
            raise AuthorityContractError("ExecutionMandate integrity failed")
        return cls(
            schema_version=1,
            mandate_id=str(value["mandate_id"]),
            intent_id=str(payload["intent_id"]),
            intent_hash=str(payload["intent_hash"]),
            account_id=str(payload["account_id"]),
            workflow=workflow,
            strategy_pack_id=strategy_pack_id,
            strategy_pack_hash=strategy_pack_hash,
            campaign_id=str(payload["campaign_id"]),
            valid_from=valid_from,
            valid_until=valid_until,
            timezone=str(payload["timezone"]),
            allowed_actions=actions,
            daily_caps=caps,
            minimum_interval_seconds=int(payload["minimum_interval_seconds"]),
            allowed_fact_ids=fact_ids,
            active_dm_allowed=bool(payload["active_dm_allowed"]),
            publish_allowed=bool(payload["publish_allowed"]),
            max_actions_per_heartbeat=1,
            created_at=str(payload["created_at"]),
            content_hash=digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "mandate_id": self.mandate_id,
            "intent_id": self.intent_id,
            "intent_hash": self.intent_hash,
            "account_id": self.account_id,
            "workflow": self.workflow,
            "strategy_pack_id": self.strategy_pack_id,
            "strategy_pack_hash": self.strategy_pack_hash,
            "campaign_id": self.campaign_id,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "timezone": self.timezone,
            "allowed_actions": list(self.allowed_actions),
            "daily_caps": dict(self.daily_caps),
            "minimum_interval_seconds": self.minimum_interval_seconds,
            "allowed_fact_ids": list(self.allowed_fact_ids),
            "active_dm_allowed": self.active_dm_allowed,
            "publish_allowed": self.publish_allowed,
            "max_actions_per_heartbeat": 1,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
        }


def adapt_legacy_campaign_authorization(value: Mapping[str, Any]) -> ExecutionMandate:
    """Read one legacy CampaignRunAuthorization as a bounded engage mandate."""

    expected = {
        "authorization_id",
        "schema_version",
        "task_id",
        "task_hash",
        "account_id",
        "allowed_actions",
        "daily_caps",
        "valid_from",
        "valid_until",
        "approval_mode",
        "confirmed_at",
        "content_hash",
    }
    if set(value) != expected or value.get("schema_version") != 1:
        raise AuthorityContractError("legacy campaign authorization is invalid")
    legacy_hash = _hash(value["content_hash"], "legacy content_hash")
    if isinstance(value["allowed_actions"], (str, bytes)) or not isinstance(
        value["allowed_actions"], Sequence
    ):
        raise AuthorityContractError("legacy allowed_actions is invalid")
    legacy_actions = [str(item or "").strip() for item in value["allowed_actions"]]
    if not legacy_actions or any(not item for item in legacy_actions):
        raise AuthorityContractError("legacy allowed_actions is invalid")
    legacy_payload: dict[str, object] = {
        "schema_version": 1,
        "task_id": _safe_id(value["task_id"], "task_id"),
        "task_hash": _text(value["task_hash"], "task_hash", limit=128),
        "account_id": _safe_id(value["account_id"], "account_id"),
        "allowed_actions": legacy_actions,
        "daily_caps": dict(value["daily_caps"]) if isinstance(value["daily_caps"], Mapping) else {},
        "valid_from": _moment(value["valid_from"], "valid_from"),
        "valid_until": _moment(value["valid_until"], "valid_until"),
        "approval_mode": _text(value["approval_mode"], "approval_mode", limit=64),
        "confirmed_at": _moment(value["confirmed_at"], "confirmed_at"),
    }
    expected_legacy_hash = _canonical_hash(legacy_payload)
    if (
        legacy_hash != expected_legacy_hash
        or value["authorization_id"] != "task_auth_" + expected_legacy_hash[:16]
    ):
        raise AuthorityContractError("legacy campaign authorization integrity failed")
    expanded: list[str] = []
    caps: dict[str, int] = {}
    raw_caps = value["daily_caps"]
    if not isinstance(raw_caps, Mapping):
        raise AuthorityContractError("legacy daily_caps is invalid")
    for action in legacy_actions:
        name = str(action)
        mapped = LEGACY_ENGAGE_ACTIONS.get(name, (name,))
        for current in mapped:
            if current not in WORKFLOW_ACTIONS["engage"]:
                raise AuthorityContractError("legacy action cannot be mapped")
            if current not in expanded:
                expanded.append(current)
            cap = raw_caps.get(name, raw_caps.get(current, 1))
            if type(cap) is not int or not 1 <= cap <= 20:
                raise AuthorityContractError("legacy daily cap is invalid")
            caps[current] = cap
    source_hash = _hash(value["task_hash"], "task_hash") if re.fullmatch(
        r"[0-9a-f]{64}", str(value["task_hash"])
    ) else sha256(str(value["task_hash"]).encode("utf-8")).hexdigest()
    intent = TaskIntent.create(
        account_id=str(value["account_id"]),
        workflow="engage",
        instruction="Legacy bounded campaign task imported for compatibility.",
        source_mode="direct_brief",
        source_ref="legacy_task:" + _safe_id(value["task_id"], "task_id"),
        source_hash=source_hash,
        requested_actions=expanded,
        created_at=str(value["confirmed_at"]),
    )
    mandate = ExecutionMandate.from_intent(
        intent,
        valid_from=str(value["valid_from"]),
        valid_until=str(value["valid_until"]),
        timezone_name="UTC",
        daily_caps=caps,
        minimum_interval_seconds=600,
        created_at=str(value["confirmed_at"]),
    )
    return mandate


@dataclass(frozen=True)
class ActionPolicyRequest:
    schema_version: int
    plan_id: str
    plan_hash: str
    account_id: str
    action_kind: str
    target_ref_hash: str
    content_hash: str
    fact_ids: tuple[str, ...]
    checked_at: str
    planned_action_count: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise AuthorityContractError("unsupported ActionPolicyRequest schema")
        _safe_id(self.plan_id, "plan_id")
        _hash(self.plan_hash, "plan_hash")
        _safe_id(self.account_id, "account_id")
        if self.action_kind not in set().union(*WORKFLOW_ACTIONS.values()):
            raise AuthorityContractError("action_kind is unsupported")
        _hash(self.target_ref_hash, "target_ref_hash")
        _hash(self.content_hash, "content_hash")
        _ids(self.fact_ids, "fact_ids", maximum=64)
        _moment(self.checked_at, "checked_at")
        if self.planned_action_count != 1:
            raise AuthorityContractError("planned_action_count must be one")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "account_id": self.account_id,
            "action_kind": self.action_kind,
            "target_ref_hash": self.target_ref_hash,
            "content_hash": self.content_hash,
            "fact_ids": list(self.fact_ids),
            "checked_at": self.checked_at,
            "planned_action_count": 1,
        }


@dataclass(frozen=True)
class PolicyRuntimeState:
    platform_ready: bool
    current_account_id: str
    target_ready: bool
    content_ready: bool
    capability_ready: bool
    pacing_ready: bool
    daily_budget_ready: bool
    duplicate: bool = False
    stop_active: bool = False
    unresolved_unknown: bool = False
    risk_signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _safe_id(self.current_account_id, "current_account_id")
        _strings(self.risk_signals, "risk_signals", maximum=32)
        for field in (
            "platform_ready",
            "target_ready",
            "content_ready",
            "capability_ready",
            "pacing_ready",
            "daily_budget_ready",
            "duplicate",
            "stop_active",
            "unresolved_unknown",
        ):
            if type(getattr(self, field)) is not bool:
                raise AuthorityContractError(f"{field} must be boolean")


@dataclass(frozen=True)
class PolicyDecision:
    schema_version: int
    decision_id: str
    mandate_id: str
    mandate_hash: str
    plan_id: str
    plan_hash: str
    outcome: str
    reasons: tuple[str, ...]
    checked_at: str
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "decision_id": self.decision_id,
            "mandate_id": self.mandate_id,
            "mandate_hash": self.mandate_hash,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "checked_at": self.checked_at,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ActionPermit:
    schema_version: int
    permit_id: str
    mandate_id: str
    mandate_hash: str
    decision_id: str
    account_id: str
    action_kind: str
    plan_id: str
    plan_hash: str
    target_ref_hash: str
    action_content_hash: str
    issued_at: str
    valid_until: str
    max_actions: int
    content_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActionPermit":
        expected = {
            "schema_version",
            "permit_id",
            "mandate_id",
            "mandate_hash",
            "decision_id",
            "account_id",
            "action_kind",
            "plan_id",
            "plan_hash",
            "target_ref_hash",
            "action_content_hash",
            "issued_at",
            "valid_until",
            "max_actions",
            "content_hash",
        }
        if set(value) != expected or value.get("schema_version") != 1:
            raise AuthorityContractError("ActionPermit fields are incomplete or unknown")
        payload: dict[str, object] = {
            "schema_version": 1,
            "mandate_id": _safe_id(value["mandate_id"], "mandate_id"),
            "mandate_hash": _hash(value["mandate_hash"], "mandate_hash"),
            "decision_id": _safe_id(value["decision_id"], "decision_id"),
            "account_id": _safe_id(value["account_id"], "account_id"),
            "action_kind": str(value["action_kind"]),
            "plan_id": _safe_id(value["plan_id"], "plan_id"),
            "plan_hash": _hash(value["plan_hash"], "plan_hash"),
            "target_ref_hash": _hash(value["target_ref_hash"], "target_ref_hash"),
            "action_content_hash": _hash(value["action_content_hash"], "action_content_hash"),
            "issued_at": _moment(value["issued_at"], "issued_at"),
            "valid_until": _moment(value["valid_until"], "valid_until"),
            "max_actions": value["max_actions"],
        }
        if payload["action_kind"] not in set().union(*WORKFLOW_ACTIONS.values()):
            raise AuthorityContractError("permit action_kind is unsupported")
        if payload["max_actions"] != 1:
            raise AuthorityContractError("permit max_actions must be one")
        if _datetime(payload["valid_until"], "valid_until") <= _datetime(payload["issued_at"], "issued_at"):
            raise AuthorityContractError("permit validity window is invalid")
        digest = _canonical_hash(payload)
        if value["permit_id"] != "permit_" + digest[:20] or value["content_hash"] != digest:
            raise AuthorityContractError("ActionPermit integrity failed")
        return cls(
            schema_version=1,
            permit_id=str(value["permit_id"]),
            mandate_id=str(payload["mandate_id"]),
            mandate_hash=str(payload["mandate_hash"]),
            decision_id=str(payload["decision_id"]),
            account_id=str(payload["account_id"]),
            action_kind=str(payload["action_kind"]),
            plan_id=str(payload["plan_id"]),
            plan_hash=str(payload["plan_hash"]),
            target_ref_hash=str(payload["target_ref_hash"]),
            action_content_hash=str(payload["action_content_hash"]),
            issued_at=str(payload["issued_at"]),
            valid_until=str(payload["valid_until"]),
            max_actions=1,
            content_hash=digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "permit_id": self.permit_id,
            "mandate_id": self.mandate_id,
            "mandate_hash": self.mandate_hash,
            "decision_id": self.decision_id,
            "account_id": self.account_id,
            "action_kind": self.action_kind,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "target_ref_hash": self.target_ref_hash,
            "action_content_hash": self.action_content_hash,
            "issued_at": self.issued_at,
            "valid_until": self.valid_until,
            "max_actions": 1,
            "content_hash": self.content_hash,
        }


def evaluate_action_policy(
    mandate: ExecutionMandate,
    request: ActionPolicyRequest,
    state: PolicyRuntimeState,
    *,
    permit_ttl_seconds: int = 300,
) -> tuple[PolicyDecision, ActionPermit | None]:
    if type(permit_ttl_seconds) is not int or not 30 <= permit_ttl_seconds <= 600:
        raise AuthorityContractError("permit_ttl_seconds must be 30-600")
    checked = _datetime(request.checked_at, "checked_at")
    stop_reasons: list[str] = []
    skip_reasons: list[str] = []
    if not state.platform_ready:
        stop_reasons.append("platform_not_ready")
    if request.account_id != mandate.account_id or state.current_account_id != mandate.account_id:
        stop_reasons.append("account_identity_mismatch")
    if state.risk_signals:
        stop_reasons.append("platform_risk_signal")
    if state.stop_active:
        stop_reasons.append("global_stop_active")
    if state.unresolved_unknown:
        stop_reasons.append("unresolved_unknown_write")
    if not state.target_ready:
        stop_reasons.append("exact_target_not_ready")
    if not state.capability_ready:
        stop_reasons.append("capability_not_ready")
    if checked < _datetime(mandate.valid_from, "valid_from") or checked >= _datetime(
        mandate.valid_until, "valid_until"
    ):
        skip_reasons.append("outside_mandate_window")
    if request.action_kind not in mandate.allowed_actions:
        skip_reasons.append("action_outside_mandate")
    if not set(request.fact_ids).issubset(mandate.allowed_fact_ids):
        skip_reasons.append("fact_outside_mandate")
    if request.action_kind == ACTIVE_DM_ACTION and not mandate.active_dm_allowed:
        skip_reasons.append("active_dm_not_in_task")
    if request.action_kind in PUBLISH_ACTIONS and not mandate.publish_allowed:
        skip_reasons.append("publish_not_in_task")
    if request.action_kind in TEXT_ACTIONS and not state.content_ready:
        skip_reasons.append("content_not_ready")
    if state.duplicate:
        skip_reasons.append("duplicate_target_action")
    if not state.pacing_ready:
        skip_reasons.append("minimum_interval_not_elapsed")
    if not state.daily_budget_ready:
        skip_reasons.append("daily_budget_exhausted")
    outcome = "stop" if stop_reasons else ("skip" if skip_reasons else "allow")
    reasons = tuple(dict.fromkeys(stop_reasons + skip_reasons))
    decision_payload: dict[str, object] = {
        "schema_version": 1,
        "mandate_id": mandate.mandate_id,
        "mandate_hash": mandate.content_hash,
        "plan_id": request.plan_id,
        "plan_hash": request.plan_hash,
        "outcome": outcome,
        "reasons": list(reasons),
        "checked_at": request.checked_at,
    }
    decision_hash = _canonical_hash(decision_payload)
    decision = PolicyDecision(
        schema_version=1,
        decision_id="policy_" + decision_hash[:20],
        mandate_id=mandate.mandate_id,
        mandate_hash=mandate.content_hash,
        plan_id=request.plan_id,
        plan_hash=request.plan_hash,
        outcome=outcome,
        reasons=reasons,
        checked_at=request.checked_at,
        content_hash=decision_hash,
    )
    if outcome != "allow":
        return decision, None
    permit_until = min(
        checked + timedelta(seconds=permit_ttl_seconds),
        _datetime(mandate.valid_until, "valid_until"),
    )
    permit_payload: dict[str, object] = {
        "schema_version": 1,
        "mandate_id": mandate.mandate_id,
        "mandate_hash": mandate.content_hash,
        "decision_id": decision.decision_id,
        "account_id": request.account_id,
        "action_kind": request.action_kind,
        "plan_id": request.plan_id,
        "plan_hash": request.plan_hash,
        "target_ref_hash": request.target_ref_hash,
        "action_content_hash": request.content_hash,
        "issued_at": request.checked_at,
        "valid_until": permit_until.isoformat(),
        "max_actions": 1,
    }
    permit_hash = _canonical_hash(permit_payload)
    permit = ActionPermit(
        schema_version=1,
        permit_id="permit_" + permit_hash[:20],
        mandate_id=mandate.mandate_id,
        mandate_hash=mandate.content_hash,
        decision_id=decision.decision_id,
        account_id=request.account_id,
        action_kind=request.action_kind,
        plan_id=request.plan_id,
        plan_hash=request.plan_hash,
        target_ref_hash=request.target_ref_hash,
        action_content_hash=request.content_hash,
        issued_at=request.checked_at,
        valid_until=permit_until.isoformat(),
        max_actions=1,
        content_hash=permit_hash,
    )
    return decision, permit


class AuthorityStore:
    """Persist immutable intent, mandate, decision, permit, and consumption evidence."""

    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "authority"
        self.intents = self.root / "intents"
        self.mandates = self.root / "mandates"
        self.permits = self.root / "permits"
        self.consumed = self.root / "consumed"
        self.decisions = self.root / "decisions.jsonl"

    @staticmethod
    def _save_immutable(path: Path, value: Mapping[str, Any]) -> Path:
        expected = dict(value)

        def update(existing: dict[str, Any]) -> dict[str, Any]:
            if existing and existing != expected:
                raise AuthorityContractError(f"immutable authority record collision: {path.name}")
            return existing or expected

        update_json_object(path, update)
        return path

    def save_intent(self, intent: TaskIntent) -> Path:
        return self._save_immutable(self.intents / f"{intent.intent_id}.json", intent.to_dict())

    def save_mandate(self, mandate: ExecutionMandate) -> Path:
        intent_path = self.intents / f"{mandate.intent_id}.json"
        try:
            intent_value = json.loads(intent_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise AuthorityContractError("mandate TaskIntent is missing or corrupt") from exc
        intent = TaskIntent.from_dict(intent_value)
        if intent.content_hash != mandate.intent_hash or intent.account_id != mandate.account_id:
            raise AuthorityContractError("mandate does not match persisted TaskIntent")
        return self._save_immutable(self.mandates / f"{mandate.mandate_id}.json", mandate.to_dict())

    def record_decision(self, decision: PolicyDecision) -> Path:
        mandate_path = self.mandates / f"{decision.mandate_id}.json"
        try:
            mandate_value = json.loads(mandate_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise AuthorityContractError("policy decision mandate is missing or corrupt") from exc
        mandate = ExecutionMandate.from_dict(mandate_value)
        if mandate.content_hash != decision.mandate_hash:
            raise AuthorityContractError("policy decision does not match persisted mandate")
        for row in read_jsonl(self.decisions):
            if row.get("decision_id") == decision.decision_id:
                if row != decision.to_dict():
                    raise AuthorityContractError("policy decision ID collision")
                return self.decisions
        return append_jsonl(self.decisions, decision.to_dict())

    def save_permit(self, permit: ActionPermit) -> Path:
        mandate_path = self.mandates / f"{permit.mandate_id}.json"
        try:
            mandate_value = json.loads(mandate_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise AuthorityContractError("permit mandate is missing or corrupt") from exc
        mandate = ExecutionMandate.from_dict(mandate_value)
        if (
            permit.mandate_hash != mandate.content_hash
            or permit.account_id != mandate.account_id
            or permit.action_kind not in mandate.allowed_actions
        ):
            raise AuthorityContractError("permit does not match persisted mandate")
        return self._save_immutable(self.permits / f"{permit.permit_id}.json", permit.to_dict())

    def consume_permit(
        self,
        permit: ActionPermit,
        *,
        plan_hash: str,
        consumed_at: str,
    ) -> Path:
        plan_hash = _hash(plan_hash, "plan_hash")
        consumed_at = _moment(consumed_at, "consumed_at")
        permit_path = self.permits / f"{permit.permit_id}.json"
        try:
            stored_value = json.loads(permit_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise AuthorityContractError("permit is missing or corrupt") from exc
        if ActionPermit.from_dict(stored_value) != permit:
            raise AuthorityContractError("permit does not match persisted record")
        if plan_hash != permit.plan_hash:
            raise AuthorityContractError("permit does not match the action plan")
        moment = _datetime(consumed_at, "consumed_at")
        if moment < _datetime(permit.issued_at, "issued_at") or moment > _datetime(
            permit.valid_until, "valid_until"
        ):
            raise AuthorityContractError("permit is outside its validity window")
        path = self.consumed / f"{permit.permit_id}.json"
        record = {
            "schema_version": 1,
            "permit_id": permit.permit_id,
            "permit_hash": permit.content_hash,
            "plan_hash": plan_hash,
            "consumed_at": consumed_at,
            "max_actions_consumed": 1,
        }

        def consume(existing: dict[str, Any]) -> dict[str, Any]:
            if existing:
                raise AuthorityContractError("permit has already been consumed")
            return record

        update_json_object(path, consume)
        return path
