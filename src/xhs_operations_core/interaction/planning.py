"""Bridge one approved MessagePlan into the fixed comment like-and-reply executor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping

from xhs_operations_core.campaign import Campaign
from xhs_operations_core.contracts import ActionType
from xhs_operations_core.discovery import CandidateInteractionPlan, DiscoveryPlan
from xhs_operations_core.messaging import MessagePlan

from .comment_flow import CommentInteractionPlan, CommentTarget


class ApprovedPlanError(ValueError):
    pass


def _time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovedPlanError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApprovedPlanError(f"{field} must include a timezone")
    return parsed


@dataclass(frozen=True)
class MessageApproval:
    approval_id: str
    account_id: str
    campaign_id: str
    candidate_id: str
    message_plan_id: str
    message_content_hash: str
    target_comment_id: str
    approved_at: str
    approved_by: str
    scope: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MessageApproval":
        allowed = {
            "approval_id", "account_id", "campaign_id", "candidate_id",
            "message_plan_id", "message_content_hash", "target_comment_id",
            "approved_at", "approved_by", "scope",
        }
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise ApprovedPlanError("message approval fields are incomplete or unknown")
        if any(not isinstance(value[name], str) or not value[name].strip() for name in allowed):
            raise ApprovedPlanError("message approval fields must be non-empty strings")
        if value["scope"] != "single_comment_target":
            raise ApprovedPlanError("message approval scope must be single_comment_target")
        if value["approved_by"] != "user":
            raise ApprovedPlanError("message approval must be made by user")
        if len(value["message_content_hash"]) != 64:
            raise ApprovedPlanError("message approval content hash is invalid")
        _time(value["approved_at"], "approved_at")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, str]:
        return {
            "approval_id": self.approval_id,
            "account_id": self.account_id,
            "campaign_id": self.campaign_id,
            "candidate_id": self.candidate_id,
            "message_plan_id": self.message_plan_id,
            "message_content_hash": self.message_content_hash,
            "target_comment_id": self.target_comment_id,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class ApprovedCommentPlan:
    bridge_id: str
    comment_plan: CommentInteractionPlan
    message_plan_id: str
    message_content_hash: str
    approval_id: str
    approval_hash: str
    fixture_only: bool
    execution_ready: bool
    platform_actions_executed: int
    next_gate: str

    def to_dict(self) -> dict[str, object]:
        return {
            "bridge_id": self.bridge_id,
            "comment_plan": self.comment_plan.to_dict(),
            "message_plan_id": self.message_plan_id,
            "message_content_hash": self.message_content_hash,
            "approval_id": self.approval_id,
            "approval_hash": self.approval_hash,
            "fixture_only": self.fixture_only,
            "execution_ready": self.execution_ready,
            "platform_actions_executed": self.platform_actions_executed,
            "next_gate": self.next_gate,
        }


def build_approved_comment_plan(
    *,
    campaign: Campaign,
    discovery_plan: DiscoveryPlan,
    candidate: CandidateInteractionPlan,
    message: MessagePlan,
    approval: MessageApproval,
    result_index: int,
) -> ApprovedCommentPlan:
    if discovery_plan.campaign_id != campaign.campaign_id:
        raise ApprovedPlanError("discovery campaign mismatch")
    query = next((item for item in discovery_plan.queries if item.query_id == candidate.query_id), None)
    if query is None:
        raise ApprovedPlanError("candidate query is not in discovery plan")
    if candidate.campaign_id != campaign.campaign_id or message.campaign_id != campaign.campaign_id:
        raise ApprovedPlanError("candidate or message campaign mismatch")
    if message.candidate_id != candidate.candidate_id:
        raise ApprovedPlanError("message candidate mismatch")
    if not message.validation.ok or message.approval_status != "awaiting_human_approval":
        raise ApprovedPlanError("message is not eligible for approval")
    if candidate.hard_blocks or candidate.proposed_action != "reply_comment":
        raise ApprovedPlanError("candidate is not eligible for reply execution")
    if ActionType.LIKE not in campaign.allowed_actions or ActionType.REPLY not in campaign.allowed_actions:
        raise ApprovedPlanError("campaign does not allow comment like and reply")
    expected = {
        "account_id": campaign.account_id,
        "campaign_id": campaign.campaign_id,
        "candidate_id": candidate.candidate_id,
        "message_plan_id": message.message_plan_id,
        "message_content_hash": message.content_hash,
        "target_comment_id": candidate.target_comment_id,
    }
    for field, value in expected.items():
        if getattr(approval, field) != value:
            raise ApprovedPlanError(f"approval {field} mismatch")
    if _time(approval.approved_at, "approved_at") < _time(message.checked_at, "message.checked_at"):
        raise ApprovedPlanError("approval cannot predate message validation")
    approval_hash = hashlib.sha256(
        json.dumps(approval.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    bridge_id = "bridge_" + hashlib.sha256(
        f"{message.message_plan_id}|{approval_hash}|{candidate.target_comment_id}".encode("utf-8")
    ).hexdigest()[:16]
    plan = CommentInteractionPlan(
        plan_id="comment_" + bridge_id.removeprefix("bridge_"),
        campaign_id=campaign.campaign_id,
        account_id=campaign.account_id,
        query=query.query,
        result_index=result_index,
        target=CommentTarget.create(
            candidate_id=candidate.candidate_id,
            target_comment_id=candidate.target_comment_id,
            note_id=candidate.note_id,
            commenter=candidate.commenter,
            full_text=candidate.full_text,
            anchor_text=candidate.anchor_text,
        ),
        reply_text=message.reply_text,
        approval_ref=approval.approval_id,
        source_context_ref=f"message:{message.message_plan_id}:{message.content_hash}",
        bridge_id=bridge_id,
        message_plan_id=message.message_plan_id,
        message_content_hash=message.content_hash,
        approval_hash=approval_hash,
        run_mode="smoke",
    )
    fixture = campaign.metadata.get("fixture_only") is True
    return ApprovedCommentPlan(
        bridge_id=bridge_id,
        comment_plan=plan,
        message_plan_id=message.message_plan_id,
        message_content_hash=message.content_hash,
        approval_id=approval.approval_id,
        approval_hash=approval_hash,
        fixture_only=fixture,
        execution_ready=False,
        platform_actions_executed=0,
        next_gate="runtime_readiness",
    )
