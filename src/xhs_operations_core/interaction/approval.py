"""Append-only local proof that a user approved one exact message and target."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json

from xhs_operations_core.storage import append_jsonl, read_jsonl

from .comment_flow import CommentInteractionPlan
from .planning import ApprovedPlanError, MessageApproval


APPROVAL_RECORD_CONFIRMATION = "I_APPROVE_SINGLE_COMMENT_TARGET"


def approval_hash(approval: MessageApproval) -> str:
    return hashlib.sha256(
        json.dumps(approval.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class MessageApprovalStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.path = Path(runtime_dir) / "interaction" / "message_approvals.jsonl"

    def record(
        self,
        approval: MessageApproval,
        *,
        recorded_at: str,
        confirmation: str,
    ) -> str:
        if confirmation != APPROVAL_RECORD_CONFIRMATION:
            raise ApprovedPlanError("exact single-target approval confirmation is required")
        digest = approval_hash(approval)
        for row in read_jsonl(self.path):
            if row.get("approval_id") == approval.approval_id:
                if row.get("approval_hash") != digest:
                    raise ApprovedPlanError("approval_id already exists with different content")
                return digest
        append_jsonl(
            self.path,
            {
                "approval_id": approval.approval_id,
                "approval_hash": digest,
                "recorded_at": recorded_at,
                "approval": approval.to_dict(),
            },
        )
        return digest

    def matches(self, plan: CommentInteractionPlan) -> bool:
        for row in reversed(read_jsonl(self.path)):
            if row.get("approval_id") != plan.approval_ref:
                continue
            raw = row.get("approval")
            if not isinstance(raw, dict):
                return False
            try:
                approval = MessageApproval.from_dict(raw)
            except ApprovedPlanError:
                return False
            digest = approval_hash(approval)
            return (
                digest == row.get("approval_hash") == plan.approval_hash
                and approval.account_id == plan.account_id
                and approval.campaign_id == plan.campaign_id
                and approval.candidate_id == plan.target.candidate_id
                and approval.message_plan_id == plan.message_plan_id
                and approval.message_content_hash == plan.message_content_hash
                and approval.target_comment_id == plan.target.target_comment_id
            )
        return False
