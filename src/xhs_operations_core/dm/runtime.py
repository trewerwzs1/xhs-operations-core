"""Single-message DM approval, gate, execution, audit, and non-sensitive conversion events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol

from xhs_operations_core.contracts import (
    ActionRecord, ActionStatus, ActionType, RiskDecision, RiskLevel, RunMode,
    TextSource, ThrottleDecision, ValidatorDecision, new_id,
)
from xhs_operations_core.storage import append_jsonl, read_jsonl

from .conversation import DMContractError, DMConversationSnapshot
from .planning import DMMessagePlan


DM_APPROVAL_CONFIRMATION = "I_APPROVE_SINGLE_DM_MESSAGE"
CONVERSION_TYPES = {
    "asked_activity_details", "requested_registration_method", "declined", "follow_up_received"
}


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DMContractError("DM timestamp must include timezone")
    return parsed


@dataclass(frozen=True)
class DMSingleApproval:
    approval_id: str
    account_id: str
    campaign_id: str
    conversation_id: str
    conversation_snapshot_hash: str
    dm_plan_id: str
    message_content_hash: str
    mode: str
    approved_at: str
    approved_by: str
    scope: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DMSingleApproval":
        allowed = {
            "approval_id", "account_id", "campaign_id", "conversation_id",
            "conversation_snapshot_hash", "dm_plan_id", "message_content_hash", "mode",
            "approved_at", "approved_by", "scope",
        }
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise DMContractError("DM approval fields are incomplete or unknown")
        if any(not isinstance(value[name], str) or not value[name] for name in allowed):
            raise DMContractError("DM approval fields must be strings")
        if value["approved_by"] != "user" or value["scope"] != "single_dm_message":
            raise DMContractError("DM approval must be one user-approved message")
        if value["mode"] not in {"passive_reply", "active_outreach"}:
            raise DMContractError("DM approval mode is invalid")
        for name in ("conversation_snapshot_hash", "message_content_hash"):
            if re.fullmatch(r"[0-9a-f]{64}", value[name]) is None:
                raise DMContractError(f"{name} is invalid")
        _time(value["approved_at"])
        return cls(**dict(value))

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def dm_approval_hash(value: DMSingleApproval) -> str:
    return hashlib.sha256(json.dumps(value.to_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ApprovedDMPlan:
    execution_id: str
    message_plan: DMMessagePlan
    approval_id: str
    approval_hash: str
    fixture_only: bool
    execution_ready: bool
    next_gate: str
    platform_actions_executed: int

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "message_plan": self.message_plan.to_dict(),
            "approval_id": self.approval_id,
            "approval_hash": self.approval_hash,
            "fixture_only": self.fixture_only,
            "execution_ready": self.execution_ready,
            "next_gate": self.next_gate,
            "platform_actions_executed": self.platform_actions_executed,
        }


def build_approved_dm_plan(
    *, message_plan: DMMessagePlan, approval: DMSingleApproval, fixture_only: bool
) -> ApprovedDMPlan:
    if not message_plan.validation_ok or message_plan.approval_status != "awaiting_human_approval":
        raise DMContractError("DM message plan is not approval eligible")
    expected = {
        "account_id": message_plan.account_id,
        "campaign_id": message_plan.campaign_id,
        "conversation_id": message_plan.conversation_id,
        "conversation_snapshot_hash": message_plan.conversation_snapshot_hash,
        "dm_plan_id": message_plan.plan_id,
        "message_content_hash": message_plan.content_hash,
        "mode": message_plan.mode,
    }
    if any(getattr(approval, name) != value for name, value in expected.items()):
        raise DMContractError("DM approval does not match message plan")
    if _time(approval.approved_at) < _time(message_plan.checked_at):
        raise DMContractError("DM approval cannot predate validation")
    digest = dm_approval_hash(approval)
    execution_id = "dm_execution_" + hashlib.sha256(
        f"{message_plan.plan_id}|{digest}".encode()
    ).hexdigest()[:16]
    return ApprovedDMPlan(execution_id, message_plan, approval.approval_id, digest, fixture_only, False, "runtime_readiness", 0)


class DMApprovalStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.path = Path(runtime_dir) / "dm" / "approvals.jsonl"

    def record(self, approval: DMSingleApproval, *, recorded_at: str, confirmation: str) -> str:
        if confirmation != DM_APPROVAL_CONFIRMATION:
            raise DMContractError("exact DM approval confirmation is required")
        _time(recorded_at)
        digest = dm_approval_hash(approval)
        for row in read_jsonl(self.path):
            if row.get("approval_id") == approval.approval_id:
                if row.get("approval_hash") != digest:
                    raise DMContractError("DM approval ID already has different content")
                return digest
        append_jsonl(self.path, {"approval_id": approval.approval_id, "approval_hash": digest, "recorded_at": recorded_at, "approval": approval.to_dict()})
        return digest

    def matches(self, approved: ApprovedDMPlan) -> bool:
        return any(
            row.get("approval_id") == approved.approval_id
            and row.get("approval_hash") == approved.approval_hash
            for row in read_jsonl(self.path)
        )


@dataclass(frozen=True)
class DMGate:
    platform_access_allowed: bool
    login_ready: bool
    stop_requested: bool
    approval_record_ready: bool
    duplicate_message: bool
    daily_budget_remaining: int
    minimum_interval_elapsed: bool

    def blockers(self) -> tuple[str, ...]:
        values = []
        if not self.platform_access_allowed: values.append("platform_access_disabled")
        if not self.login_ready: values.append("login_not_ready")
        if self.stop_requested: values.append("operator_stop_requested")
        if not self.approval_record_ready: values.append("dm_approval_record_missing")
        if self.duplicate_message: values.append("duplicate_dm_message")
        if self.daily_budget_remaining < 1: values.append("insufficient_dm_budget")
        if not self.minimum_interval_elapsed: values.append("minimum_target_interval_not_elapsed")
        return tuple(values)


@dataclass(frozen=True)
class DMWriteResult:
    attempted: bool
    verified: bool
    result_ref: str
    evidence: dict[str, Any]


class DMPort(Protocol):
    def read_current_conversation(self, conversation_id: str) -> DMConversationSnapshot: ...
    def send_one_message(self, conversation_id: str, text: str) -> DMWriteResult: ...


def execute_single_dm(*, approved: ApprovedDMPlan, gate: DMGate, port: DMPort) -> DMWriteResult:
    if gate.blockers():
        return DMWriteResult(False, False, "", {"blockers": list(gate.blockers())})
    current = port.read_current_conversation(approved.message_plan.conversation_id)
    if current.content_hash != approved.message_plan.conversation_snapshot_hash:
        return DMWriteResult(False, False, "", {"blockers": ["dm_conversation_changed"]})
    return port.send_one_message(approved.message_plan.conversation_id, approved.message_plan.reply_text)


class DMRuntimeStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.action_path = self.runtime_dir / "dm" / "actions.jsonl"
        self.conversion_path = self.runtime_dir / "dm" / "conversion_events.jsonl"

    def duplicate(self, approved: ApprovedDMPlan) -> bool:
        return any(
            isinstance(row.get("metadata"), dict)
            and row["metadata"].get("conversation_id") == approved.message_plan.conversation_id
            and row["metadata"].get("message_content_hash") == approved.message_plan.content_hash
            and row.get("status") == "verified"
            for row in read_jsonl(self.action_path)
        )

    def records(self) -> list[ActionRecord]:
        return [ActionRecord.from_dict(row) for row in read_jsonl(self.action_path)]

    def build_gate(
        self,
        approved: ApprovedDMPlan,
        *,
        checked_at: str,
        platform_access_allowed: bool,
        login_ready: bool,
        approval_record_ready: bool,
        daily_dm_limit: int,
        minimum_target_interval_seconds: int = 600,
    ) -> DMGate:
        now = _time(checked_at)
        rows = read_jsonl(self.action_path)
        public_rows = read_jsonl(self.runtime_dir / "comment_flow" / "actions.jsonl")
        today = [
            row for row in rows
            if row.get("account_id") == approved.message_plan.account_id
            and row.get("status") == "verified"
            and _time(str(row.get("created_at"))).date() == now.date()
        ]
        return DMGate(
            platform_access_allowed=platform_access_allowed,
            login_ready=login_ready,
            stop_requested=(self.runtime_dir / "comment_flow" / "STOP.json").exists(),
            approval_record_ready=approval_record_ready,
            duplicate_message=self.duplicate(approved),
            daily_budget_remaining=max(0, daily_dm_limit - len(today)),
            minimum_interval_elapsed=(
                not [row for row in (*rows, *public_rows) if row.get("status") == "verified"]
                or now >= max(
                    _time(str(row["created_at"]))
                    for row in (*rows, *public_rows)
                    if row.get("status") == "verified"
                ) + timedelta(seconds=minimum_target_interval_seconds)
            ),
        )

    def append_verified(
        self, approved: ApprovedDMPlan, result: DMWriteResult, *, run_id: str,
        created_at: str, daily_dm_limit: int = 2, minimum_interval_seconds: int = 600
    ) -> ActionRecord:
        if not result.attempted or not result.verified:
            raise DMContractError("only verified DM can be recorded")
        record = ActionRecord(
            record_id=new_id("action"), run_id=run_id,
            campaign_id=approved.message_plan.campaign_id, account_id=approved.message_plan.account_id,
            candidate_id=approved.message_plan.conversation_id,
            interaction_plan_id=approved.execution_id, action_type=ActionType.DM,
            run_mode=RunMode.SMOKE, status=ActionStatus.VERIFIED, created_at=created_at,
            source_context_ref=f"dm:{approved.message_plan.conversation_snapshot_hash}",
            text_source=TextSource.APPROVED_DRAFT, output_text=approved.message_plan.reply_text,
            result_ref=result.result_ref,
            validator=ValidatorDecision(True, created_at, fact_refs=tuple(item.fact_id for item in approved.message_plan.fact_uses)),
            risk=RiskDecision(True, RiskLevel.LOW, created_at),
            throttle=ThrottleDecision(
                True, created_at, created_at,
                sum(row.get("status") == "verified" for row in read_jsonl(self.action_path)),
                daily_dm_limit, minimum_interval_seconds,
            ),
            metadata={"conversation_id": approved.message_plan.conversation_id, "message_content_hash": approved.message_plan.content_hash, "approval_ref": approved.approval_id},
        )
        append_jsonl(self.action_path, record.to_dict())
        return record

    def append_conversion(self, *, event_id: str, conversation_id: str, snapshot_hash: str, event_type: str, observed_at: str) -> None:
        if event_type not in CONVERSION_TYPES:
            raise DMContractError("unsupported non-sensitive conversion type")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", event_id) is None:
            raise DMContractError("conversion event_id is invalid")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", conversation_id) is None:
            raise DMContractError("conversion conversation_id is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", snapshot_hash) is None:
            raise DMContractError("conversion snapshot_hash is invalid")
        _time(observed_at)
        append_jsonl(self.conversion_path, {"event_id": event_id, "conversation_id": conversation_id, "snapshot_hash": snapshot_hash, "event_type": event_type, "observed_at": observed_at})
