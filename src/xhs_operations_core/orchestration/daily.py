"""Compile discovery and reviewed-message inputs into an offline DailyPlan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from typing import Sequence

from xhs_operations_core.campaign import Campaign
from xhs_operations_core.discovery import CandidateInteractionPlan, DiscoveryPlan
from xhs_operations_core.messaging import MessagePlan


class LoopPlanError(ValueError):
    pass


def _moment(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LoopPlanError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LoopPlanError(f"{field} must include a timezone")
    return parsed


@dataclass(frozen=True)
class DailyBudget:
    max_search_queries: int
    max_candidate_reviews: int
    max_interaction_targets: int
    minimum_target_interval_seconds: int = 600
    visible_step_min_seconds: int = 10
    visible_step_max_seconds: int = 15
    schedule_window_seconds: int = 180

    def __post_init__(self) -> None:
        for name in ("max_search_queries", "max_candidate_reviews", "max_interaction_targets"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise LoopPlanError(f"{name} must be a non-negative integer")
        if self.minimum_target_interval_seconds < 600:
            raise LoopPlanError("minimum_target_interval_seconds must be at least 600")
        if self.visible_step_min_seconds < 10:
            raise LoopPlanError("visible_step_min_seconds must be at least 10")
        if self.visible_step_max_seconds < self.visible_step_min_seconds:
            raise LoopPlanError("visible step maximum cannot be below minimum")
        if self.visible_step_max_seconds > 15:
            raise LoopPlanError("visible_step_max_seconds cannot exceed 15")
        if not 60 <= self.schedule_window_seconds <= 600:
            raise LoopPlanError("schedule_window_seconds must be between 60 and 600")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_search_queries": self.max_search_queries,
            "max_candidate_reviews": self.max_candidate_reviews,
            "max_interaction_targets": self.max_interaction_targets,
            "minimum_target_interval_seconds": self.minimum_target_interval_seconds,
            "visible_step_min_seconds": self.visible_step_min_seconds,
            "visible_step_max_seconds": self.visible_step_max_seconds,
            "schedule_window_seconds": self.schedule_window_seconds,
        }


@dataclass(frozen=True)
class SearchSlot:
    slot_id: str
    query_id: str
    query: str
    segment_id: str
    priority: str
    max_notes_to_open: int
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_id": self.slot_id,
            "query_id": self.query_id,
            "query": self.query,
            "segment_id": self.segment_id,
            "priority": self.priority,
            "max_notes_to_open": self.max_notes_to_open,
            "status": self.status,
        }


@dataclass(frozen=True)
class InteractionQueueItem:
    item_id: str
    candidate_id: str
    message_plan_id: str
    message_content_hash: str
    message_validation_ref: str
    campaign_fact_validation_ref: str
    fact_refs: tuple[str, ...]
    style_profile_id: str
    style_profile_hash: str
    style_exception_required: bool
    query_id: str
    note_id: str
    target_comment_id: str
    evidence_level: str
    priority_score: int
    status: str
    execution_ready: bool
    window_start: str | None
    window_end: str | None
    primary_target_action: str
    max_platform_writes: int
    source_context_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "candidate_id": self.candidate_id,
            "message_plan_id": self.message_plan_id,
            "message_content_hash": self.message_content_hash,
            "message_validation_ref": self.message_validation_ref,
            "campaign_fact_validation_ref": self.campaign_fact_validation_ref,
            "fact_refs": list(self.fact_refs),
            "style_profile_id": self.style_profile_id,
            "style_profile_hash": self.style_profile_hash,
            "style_exception_required": self.style_exception_required,
            "query_id": self.query_id,
            "note_id": self.note_id,
            "target_comment_id": self.target_comment_id,
            "evidence_level": self.evidence_level,
            "priority_score": self.priority_score,
            "status": self.status,
            "execution_ready": self.execution_ready,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "primary_target_action": self.primary_target_action,
            "max_platform_writes": self.max_platform_writes,
            "source_context_hash": self.source_context_hash,
        }


@dataclass(frozen=True)
class DailyPlan:
    plan_id: str
    plan_date: str
    created_at: str
    account_id: str
    campaign_id: str
    discovery_plan_hash: str
    objective: str
    processing_mode: str
    max_one_primary_target_per_heartbeat: bool
    budget: DailyBudget
    search_slots: tuple[SearchSlot, ...]
    interaction_queue: tuple[InteractionQueueItem, ...]
    deferred_count: int
    approval_required_count: int
    platform_actions_executed: int
    fixture_only: bool
    writes_enabled: bool
    write_authorization: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_date": self.plan_date,
            "created_at": self.created_at,
            "account_id": self.account_id,
            "campaign_id": self.campaign_id,
            "discovery_plan_hash": self.discovery_plan_hash,
            "objective": self.objective,
            "processing_mode": self.processing_mode,
            "max_one_primary_target_per_heartbeat": self.max_one_primary_target_per_heartbeat,
            "budget": self.budget.to_dict(),
            "search_slots": [item.to_dict() for item in self.search_slots],
            "interaction_queue": [item.to_dict() for item in self.interaction_queue],
            "deferred_count": self.deferred_count,
            "approval_required_count": self.approval_required_count,
            "platform_actions_executed": self.platform_actions_executed,
            "fixture_only": self.fixture_only,
            "writes_enabled": self.writes_enabled,
            "write_authorization": self.write_authorization,
        }


def _priority(candidate: CandidateInteractionPlan) -> int:
    level = {"A": 1000, "B": 500}[candidate.evidence_level]
    return (
        level
        + candidate.scores["activity_intent"] * 3
        + candidate.scores["natural_reply_opportunity"] * 2
        + candidate.scores["topic_relevance"]
        + candidate.scores["location_fit"]
        - candidate.scores["risk"] * 5
    )


def build_daily_plan(
    *,
    campaign: Campaign,
    discovery_plan: DiscoveryPlan,
    candidate_messages: Sequence[tuple[CandidateInteractionPlan, MessagePlan]],
    budget: DailyBudget,
    plan_date: str,
    created_at: str,
) -> DailyPlan:
    created = _moment(created_at, "created_at")
    try:
        datetime.strptime(plan_date, "%Y-%m-%d")
    except ValueError as exc:
        raise LoopPlanError("plan_date must be YYYY-MM-DD") from exc
    if discovery_plan.campaign_id != campaign.campaign_id:
        raise LoopPlanError("discovery plan does not belong to campaign")
    supported_processing_modes = {
        "one_candidate_at_a_time",
        "one_candidate_at_a_time_same_search_batch",
    }
    if discovery_plan.processing_mode not in supported_processing_modes:
        raise LoopPlanError(
            "discovery plan must use a supported one-candidate-at-a-time mode"
        )

    search_slots = tuple(
        SearchSlot(
            slot_id="search_" + hashlib.sha256(
                f"{plan_date}|{campaign.campaign_id}|{query.query_id}".encode("utf-8")
            ).hexdigest()[:16],
            query_id=query.query_id,
            query=query.query,
            segment_id=query.segment_id,
            priority=query.priority,
            max_notes_to_open=query.max_notes_to_open,
            status="planned_readonly",
        )
        for query in discovery_plan.queries[: budget.max_search_queries]
    )

    seen_candidates: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    valid_pairs: list[tuple[CandidateInteractionPlan, MessagePlan]] = []
    query_ids = {item.query_id for item in discovery_plan.queries}
    for candidate, message in candidate_messages:
        if candidate.candidate_id in seen_candidates:
            raise LoopPlanError("duplicate candidate_id in daily plan")
        target_key = (candidate.note_id, candidate.target_comment_id)
        if target_key in seen_targets:
            raise LoopPlanError("duplicate target comment in daily plan")
        seen_candidates.add(candidate.candidate_id)
        seen_targets.add(target_key)
        if candidate.campaign_id != campaign.campaign_id or message.campaign_id != campaign.campaign_id:
            raise LoopPlanError("candidate or message campaign mismatch")
        if message.candidate_id != candidate.candidate_id:
            raise LoopPlanError("message candidate mismatch")
        if candidate.query_id not in query_ids:
            raise LoopPlanError("candidate query is not in discovery plan")
        if candidate.evidence_level not in {"A", "B"} or candidate.proposed_action != "reply_comment":
            raise LoopPlanError("daily interaction queue only accepts A/B reply candidates")
        if candidate.hard_blocks or not message.validation.ok:
            raise LoopPlanError("blocked candidate or message cannot enter daily queue")
        if message.approval_status != "awaiting_human_approval":
            raise LoopPlanError("unexpected message approval status")
        valid_pairs.append((candidate, message))

    valid_pairs.sort(key=lambda pair: (-_priority(pair[0]), pair[0].candidate_id))
    queue: list[InteractionQueueItem] = []
    for index, (candidate, message) in enumerate(valid_pairs):
        within_review = index < budget.max_candidate_reviews
        within_target = index < budget.max_interaction_targets
        scheduled = within_review and within_target
        start = created + timedelta(
            seconds=index
            * (budget.minimum_target_interval_seconds + budget.schedule_window_seconds)
        )
        end = start + timedelta(seconds=budget.schedule_window_seconds)
        source_context_hash = hashlib.sha256(
            f"{candidate.note_id}|{candidate.target_comment_id}|{candidate.full_text}|{message.content_hash}".encode("utf-8")
        ).hexdigest()
        item_id = "queue_" + hashlib.sha256(
            f"{plan_date}|{campaign.campaign_id}|{candidate.candidate_id}|{message.message_plan_id}".encode("utf-8")
        ).hexdigest()[:16]
        queue.append(
            InteractionQueueItem(
                item_id=item_id,
                candidate_id=candidate.candidate_id,
                message_plan_id=message.message_plan_id,
                message_content_hash=message.content_hash,
                message_validation_ref=(
                    f"message-validation:{message.message_plan_id}:{message.content_hash}"
                ),
                campaign_fact_validation_ref=(
                    f"campaign-facts:{campaign.campaign_id}:{message.message_plan_id}"
                ),
                fact_refs=tuple(item.fact_id for item in message.fact_uses),
                style_profile_id=message.style_alignment.profile_id or "",
                style_profile_hash=message.style_alignment.profile_hash or "",
                style_exception_required=not message.style_alignment.applied,
                query_id=candidate.query_id,
                note_id=candidate.note_id,
                target_comment_id=candidate.target_comment_id,
                evidence_level=candidate.evidence_level,
                priority_score=_priority(candidate),
                status="awaiting_human_approval" if scheduled else "deferred_daily_budget",
                execution_ready=False,
                window_start=start.isoformat() if scheduled else None,
                window_end=end.isoformat() if scheduled else None,
                primary_target_action="reply_comment",
                max_platform_writes=1,
                source_context_hash=source_context_hash,
            )
        )

    identity = {
        "plan_date": plan_date,
        "account_id": campaign.account_id,
        "campaign_id": campaign.campaign_id,
        "discovery_plan_hash": discovery_plan.content_hash,
        "budget": budget.to_dict(),
        "candidate_message_ids": [
            [candidate.candidate_id, message.message_plan_id] for candidate, message in valid_pairs
        ],
    }
    plan_id = "daily_" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    deferred = sum(item.status == "deferred_daily_budget" for item in queue)
    approvals = sum(item.status == "awaiting_human_approval" for item in queue)
    return DailyPlan(
        plan_id=plan_id,
        plan_date=plan_date,
        created_at=created_at,
        account_id=campaign.account_id,
        campaign_id=campaign.campaign_id,
        discovery_plan_hash=discovery_plan.content_hash,
        objective="value_interaction_to_profile_follow_to_activity_interest",
        processing_mode=discovery_plan.processing_mode,
        max_one_primary_target_per_heartbeat=True,
        budget=budget,
        search_slots=search_slots,
        interaction_queue=tuple(queue),
        deferred_count=deferred,
        approval_required_count=approvals,
        platform_actions_executed=0,
        fixture_only=campaign.metadata.get("fixture_only") is True,
        writes_enabled=False,
        write_authorization="not_granted",
    )
