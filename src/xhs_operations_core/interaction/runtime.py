"""Runtime gates and audit storage for single-comment interactions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from xhs_operations_core.contracts import (
    ActionRecord,
    ActionStatus,
    ActionType,
    RiskDecision,
    RiskLevel,
    RunMode,
    TextSource,
    ThrottleDecision,
    ValidatorDecision,
    new_id,
)
from xhs_operations_core.storage import append_jsonl, read_jsonl

from .comment_flow import (
    CommentFlowGate,
    CommentFlowResult,
    CommentInteractionPlan,
    CommentTarget,
)


ACTION_LOG = Path("comment_flow") / "actions.jsonl"
STOP_FILE = Path("comment_flow") / "STOP.json"


def _moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("ActionRecord timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


class CommentActionStore:
    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.path = self.runtime_dir / ACTION_LOG

    @property
    def stop_path(self) -> Path:
        return self.runtime_dir / STOP_FILE

    def records(self) -> list[ActionRecord]:
        return [ActionRecord.from_dict(item) for item in read_jsonl(self.path)]

    def append(self, record: ActionRecord) -> Path:
        return append_jsonl(self.path, record.to_dict())

    def build_gate(
        self,
        *,
        account_id: str,
        target: CommentTarget,
        checked_at: str,
        login_ready: bool,
        platform_access_allowed: bool,
        daily_action_limit: int,
        minimum_target_interval_seconds: int,
    ) -> CommentFlowGate:
        now = _moment(checked_at)
        verified = [
            item
            for item in self.records()
            if item.account_id == account_id and item.status is ActionStatus.VERIFIED
        ]
        today = [item for item in verified if _moment(item.created_at).date() == now.date()]
        latest = max((_moment(item.created_at) for item in verified), default=None)
        minimum_elapsed = (
            latest is None
            or now >= latest + timedelta(seconds=minimum_target_interval_seconds)
        )
        target_rows = [
            item
            for item in verified
            if item.candidate_id == target.candidate_id
            and item.metadata.get("target_context_hash") == target.context_hash
        ]
        duplicate_like = any(
            item.action_type is ActionType.LIKE
            and item.metadata.get("interaction_scope") == "comment"
            for item in target_rows
        )
        duplicate_reply = any(
            item.action_type is ActionType.REPLY for item in target_rows
        )
        return CommentFlowGate(
            platform_access_allowed=platform_access_allowed,
            login_ready=login_ready,
            stop_requested=self.stop_path.exists(),
            daily_budget_remaining=max(0, daily_action_limit - len(today)),
            minimum_interval_elapsed=minimum_elapsed,
            duplicate_like=duplicate_like,
            duplicate_reply=duplicate_reply,
        )

    def append_verified_flow(
        self,
        *,
        plan: CommentInteractionPlan,
        result: CommentFlowResult,
        run_id: str,
        created_at: str,
        daily_action_limit: int,
        minimum_target_interval_seconds: int,
    ) -> tuple[ActionRecord, ActionRecord]:
        if not result.ok or result.like is None or result.reply is None:
            raise ValueError("only a fully verified comment flow can be persisted")
        records: list[ActionRecord] = []
        for ordinal, (action_type, action) in enumerate(
            ((ActionType.LIKE, result.like), (ActionType.REPLY, result.reply))
        ):
            records.append(
                ActionRecord(
                    record_id=new_id("action"),
                    run_id=run_id,
                    campaign_id=plan.campaign_id,
                    account_id=plan.account_id,
                    candidate_id=plan.target.candidate_id,
                    interaction_plan_id=plan.plan_id,
                    action_type=action_type,
                    run_mode=RunMode.SMOKE,
                    status=ActionStatus.VERIFIED,
                    created_at=created_at,
                    source_context_ref=plan.source_context_ref,
                    text_source=(
                        TextSource.NONE
                        if action_type is ActionType.LIKE
                        else TextSource.APPROVED_DRAFT
                    ),
                    output_text=None if action_type is ActionType.LIKE else plan.reply_text,
                    result_ref=action.result_ref,
                    validator=ValidatorDecision(
                        True,
                        created_at,
                        fact_refs=(plan.target.context_hash,),
                    ),
                    risk=RiskDecision(True, RiskLevel.LOW, created_at),
                    throttle=ThrottleDecision(
                        True,
                        created_at,
                        created_at,
                        ordinal,
                        daily_action_limit,
                        minimum_target_interval_seconds,
                    ),
                    metadata={
                        "interaction_scope": "comment",
                        "target_context_hash": plan.target.context_hash,
                        "approval_ref": plan.approval_ref,
                        "action_evidence": dict(action.evidence),
                    },
                )
            )
        for record in records:
            self.append(record)
        return records[0], records[1]

    def append_verified_actions(
        self,
        *,
        plan: CommentInteractionPlan,
        result: CommentFlowResult,
        run_id: str,
        created_at: str,
        daily_action_limit: int,
        minimum_target_interval_seconds: int,
    ) -> tuple[ActionRecord, ...]:
        action_rows = []
        if result.like is not None and result.like.verified and result.like.attempted:
            action_rows.append((ActionType.LIKE, result.like))
        if result.reply is not None and result.reply.verified and result.reply.attempted:
            action_rows.append((ActionType.REPLY, result.reply))
        records: list[ActionRecord] = []
        for ordinal, (action_type, action) in enumerate(action_rows):
            record = ActionRecord(
                record_id=new_id("action"),
                run_id=run_id,
                campaign_id=plan.campaign_id,
                account_id=plan.account_id,
                candidate_id=plan.target.candidate_id,
                interaction_plan_id=plan.plan_id,
                action_type=action_type,
                run_mode=RunMode.SMOKE,
                status=ActionStatus.VERIFIED,
                created_at=created_at,
                source_context_ref=plan.source_context_ref,
                text_source=(
                    TextSource.NONE
                    if action_type is ActionType.LIKE
                    else TextSource.APPROVED_DRAFT
                ),
                output_text=None if action_type is ActionType.LIKE else plan.reply_text,
                result_ref=action.result_ref,
                validator=ValidatorDecision(
                    True, created_at, fact_refs=(plan.target.context_hash,)
                ),
                risk=RiskDecision(True, RiskLevel.LOW, created_at),
                throttle=ThrottleDecision(
                    True,
                    created_at,
                    created_at,
                    ordinal,
                    daily_action_limit,
                    minimum_target_interval_seconds,
                ),
                metadata={
                    "interaction_scope": "comment",
                    "target_context_hash": plan.target.context_hash,
                    "approval_ref": plan.approval_ref,
                    "action_evidence": dict(action.evidence),
                },
            )
            self.append(record)
            records.append(record)
        return tuple(records)
