"""Deterministic per-note engagement policy for the Codex campaign driver.

This module plans one bounded engagement package after one note and its visible
comments have been read.  It never performs a platform action and it never
authors message text.  Text plans must already have passed their dedicated
validators; the output remains approval-bound and non-executable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence


class PostEngagementError(ValueError):
    """Raised when a post engagement request is incomplete or unsafe."""


SCORE_FIELDS = {
    "topic_relevance",
    "activity_intent",
    "natural_reply_opportunity",
    "risk",
}


def _required_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PostEngagementError(f"{name} is required")
    return " ".join(value.split())


def _safe_id(name: str, value: object) -> str:
    text = _required_text(name, value)
    if len(text) > 256 or re.search(r"[\x00-\x1f]", text):
        raise PostEngagementError(f"{name} is invalid")
    return text


def _moment(name: str, value: object) -> str:
    text = _required_text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PostEngagementError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PostEngagementError(f"{name} must include timezone")
    return text


def _boolean(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise PostEngagementError(f"{name} must be a boolean")
    return value


@dataclass(frozen=True)
class PostEngagementPolicy:
    max_top_level_comments_per_note: int = 1
    min_replies_when_eligible: int = 1
    max_replies_per_note: int = 3
    max_comment_likes_per_note: int = 3
    max_dm_candidates_per_note: int = 1
    minimum_target_interval_seconds: int = 600
    visible_step_min_seconds: int = 10
    visible_step_max_seconds: int = 15
    dm_min_topic_relevance: int = 80
    dm_min_activity_intent: int = 80

    def __post_init__(self) -> None:
        if self.max_top_level_comments_per_note != 1:
            raise PostEngagementError("a note must allow exactly one top-level comment at most")
        if not 0 <= self.min_replies_when_eligible <= self.max_replies_per_note <= 3:
            raise PostEngagementError("reply policy must remain within 0-3 replies")
        if not 0 <= self.max_comment_likes_per_note <= 5:
            raise PostEngagementError("comment-like budget must remain within 0-5")
        if not 0 <= self.max_dm_candidates_per_note <= 1:
            raise PostEngagementError("at most one DM candidate may be selected per note")
        if self.minimum_target_interval_seconds < 600:
            raise PostEngagementError("minimum target interval must be at least 600 seconds")
        if self.visible_step_min_seconds < 10:
            raise PostEngagementError("visible step minimum must be at least 10 seconds")
        if not self.visible_step_min_seconds <= self.visible_step_max_seconds <= 15:
            raise PostEngagementError("visible step maximum must remain within 10-15 seconds")
        for name in ("dm_min_topic_relevance", "dm_min_activity_intent"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 100:
                raise PostEngagementError(f"{name} must be an integer from 0 to 100")

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "PostEngagementPolicy":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise PostEngagementError("policy must be an object")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise PostEngagementError(f"unknown policy fields: {sorted(unknown)}")
        if any(type(item) is not int for item in value.values()):
            raise PostEngagementError("policy values must be integers")
        return cls(**dict(value))


@dataclass(frozen=True)
class TopLevelCommentOption:
    plan_id: str
    validation_ok: bool
    already_commented: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _safe_id("top_level_comment.plan_id", self.plan_id))
        _boolean("top_level_comment.validation_ok", self.validation_ok)
        _boolean("top_level_comment.already_commented", self.already_commented)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TopLevelCommentOption":
        allowed = {"plan_id", "validation_ok", "already_commented"}
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise PostEngagementError("top_level_comment fields are incomplete or unknown")
        return cls(**dict(value))


@dataclass(frozen=True)
class DMTemplateApproval:
    template_id: str
    template_hash: str
    approval_ref: str
    approved_for_campaign: bool

    def __post_init__(self) -> None:
        for name in ("template_id", "approval_ref"):
            object.__setattr__(self, name, _safe_id(f"dm_template.{name}", getattr(self, name)))
        if re.fullmatch(r"[0-9a-f]{64}", self.template_hash) is None:
            raise PostEngagementError("dm_template.template_hash must be SHA-256 hex")
        _boolean("dm_template.approved_for_campaign", self.approved_for_campaign)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DMTemplateApproval":
        allowed = {"template_id", "template_hash", "approval_ref", "approved_for_campaign"}
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise PostEngagementError("dm_template fields are incomplete or unknown")
        return cls(**dict(value))


@dataclass(frozen=True)
class PostCandidateSignal:
    candidate_id: str
    target_comment_id: str
    user_id: str
    evidence_level: str
    scores: dict[str, int]
    reply_plan_id: str | None
    exact_target_visible: bool
    like_supported: bool
    previously_replied: bool
    previously_liked: bool
    previously_dm_contacted: bool
    hard_blocks: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("candidate_id", "target_comment_id", "user_id"):
            object.__setattr__(self, name, _safe_id(f"candidate.{name}", getattr(self, name)))
        if self.evidence_level not in {"A", "B", "C", "X"}:
            raise PostEngagementError("candidate evidence_level is invalid")
        if set(self.scores) != SCORE_FIELDS or any(
            type(value) is not int or not 0 <= value <= 100 for value in self.scores.values()
        ):
            raise PostEngagementError("candidate scores are incomplete or invalid")
        if self.reply_plan_id is not None:
            object.__setattr__(self, "reply_plan_id", _safe_id("candidate.reply_plan_id", self.reply_plan_id))
        for name in (
            "exact_target_visible", "like_supported", "previously_replied",
            "previously_liked", "previously_dm_contacted",
        ):
            _boolean(f"candidate.{name}", getattr(self, name))
        if any(not isinstance(item, str) or not item.strip() for item in self.hard_blocks):
            raise PostEngagementError("candidate hard_blocks must contain non-empty strings")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PostCandidateSignal":
        allowed = {
            "candidate_id", "target_comment_id", "user_id", "evidence_level",
            "scores", "reply_plan_id", "exact_target_visible", "like_supported",
            "previously_replied", "previously_liked", "previously_dm_contacted",
            "hard_blocks",
        }
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise PostEngagementError("candidate fields are incomplete or unknown")
        scores, hard_blocks = value["scores"], value["hard_blocks"]
        if not isinstance(scores, Mapping) or not isinstance(hard_blocks, list):
            raise PostEngagementError("candidate scores and hard_blocks are invalid")
        return cls(
            candidate_id=value["candidate_id"],
            target_comment_id=value["target_comment_id"],
            user_id=value["user_id"],
            evidence_level=value["evidence_level"],
            scores=dict(scores),
            reply_plan_id=value["reply_plan_id"],
            exact_target_visible=value["exact_target_visible"],
            like_supported=value["like_supported"],
            previously_replied=value["previously_replied"],
            previously_liked=value["previously_liked"],
            previously_dm_contacted=value["previously_dm_contacted"],
            hard_blocks=tuple(hard_blocks),
        )


@dataclass(frozen=True)
class PostEngagementRequest:
    campaign_id: str
    account_id: str
    note_id: str
    checked_at: str
    note_relevance_score: int
    top_level_comment: TopLevelCommentOption | None
    candidates: tuple[PostCandidateSignal, ...]
    dm_template: DMTemplateApproval | None
    dm_capability_available: bool
    policy: PostEngagementPolicy

    def __post_init__(self) -> None:
        for name in ("campaign_id", "account_id", "note_id"):
            object.__setattr__(self, name, _safe_id(name, getattr(self, name)))
        object.__setattr__(self, "checked_at", _moment("checked_at", self.checked_at))
        if type(self.note_relevance_score) is not int or not 0 <= self.note_relevance_score <= 100:
            raise PostEngagementError("note_relevance_score must be an integer from 0 to 100")
        _boolean("dm_capability_available", self.dm_capability_available)
        ids = [item.candidate_id for item in self.candidates]
        comments = [item.target_comment_id for item in self.candidates]
        if len(ids) != len(set(ids)) or len(comments) != len(set(comments)):
            raise PostEngagementError("candidate and target comment ids must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "account_id": self.account_id,
            "note_id": self.note_id,
            "checked_at": self.checked_at,
            "note_relevance_score": self.note_relevance_score,
            "top_level_comment": (
                None if self.top_level_comment is None else {
                    "plan_id": self.top_level_comment.plan_id,
                    "validation_ok": self.top_level_comment.validation_ok,
                    "already_commented": self.top_level_comment.already_commented,
                }
            ),
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "target_comment_id": item.target_comment_id,
                    "user_id": item.user_id,
                    "evidence_level": item.evidence_level,
                    "scores": dict(item.scores),
                    "reply_plan_id": item.reply_plan_id,
                    "exact_target_visible": item.exact_target_visible,
                    "like_supported": item.like_supported,
                    "previously_replied": item.previously_replied,
                    "previously_liked": item.previously_liked,
                    "previously_dm_contacted": item.previously_dm_contacted,
                    "hard_blocks": list(item.hard_blocks),
                }
                for item in self.candidates
            ],
            "dm_template": (
                None if self.dm_template is None else {
                    "template_id": self.dm_template.template_id,
                    "template_hash": self.dm_template.template_hash,
                    "approval_ref": self.dm_template.approval_ref,
                    "approved_for_campaign": self.dm_template.approved_for_campaign,
                }
            ),
            "dm_capability_available": self.dm_capability_available,
            "policy": self.policy.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PostEngagementRequest":
        allowed = {
            "campaign_id", "account_id", "note_id", "checked_at",
            "note_relevance_score", "top_level_comment", "candidates",
            "dm_template", "dm_capability_available", "policy",
        }
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise PostEngagementError("post engagement request fields are incomplete or unknown")
        if not isinstance(value["candidates"], list):
            raise PostEngagementError("candidates must be a list")
        top = value["top_level_comment"]
        dm = value["dm_template"]
        return cls(
            campaign_id=value["campaign_id"],
            account_id=value["account_id"],
            note_id=value["note_id"],
            checked_at=value["checked_at"],
            note_relevance_score=value["note_relevance_score"],
            top_level_comment=None if top is None else TopLevelCommentOption.from_dict(top),
            candidates=tuple(PostCandidateSignal.from_dict(item) for item in value["candidates"]),
            dm_template=None if dm is None else DMTemplateApproval.from_dict(dm),
            dm_capability_available=value["dm_capability_available"],
            policy=PostEngagementPolicy.from_dict(value["policy"]),
        )


@dataclass(frozen=True)
class PlannedPublicAction:
    order: int
    action: str
    candidate_id: str
    target_comment_id: str | None
    plan_ref: str | None
    status: str
    minimum_delay_from_previous_target_seconds: int
    requires_exact_approval: bool
    execution_ready: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "order": self.order,
            "action": self.action,
            "candidate_id": self.candidate_id,
            "target_comment_id": self.target_comment_id,
            "plan_ref": self.plan_ref,
            "status": self.status,
            "minimum_delay_from_previous_target_seconds": self.minimum_delay_from_previous_target_seconds,
            "requires_exact_approval": self.requires_exact_approval,
            "execution_ready": self.execution_ready,
        }


@dataclass(frozen=True)
class LeadRecordPlan:
    candidate_id: str
    target_comment_id: str
    user_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "target_comment_id": self.target_comment_id,
            "user_id": self.user_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DMCandidatePlan:
    candidate_id: str
    user_id: str
    template_id: str | None
    template_hash: str | None
    approval_ref: str | None
    status: str
    blockers: tuple[str, ...]
    execution_ready: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "user_id": self.user_id,
            "template_id": self.template_id,
            "template_hash": self.template_hash,
            "approval_ref": self.approval_ref,
            "status": self.status,
            "blockers": list(self.blockers),
            "execution_ready": self.execution_ready,
        }


@dataclass(frozen=True)
class PostEngagementPlan:
    plan_id: str
    campaign_id: str
    account_id: str
    note_id: str
    checked_at: str
    policy: PostEngagementPolicy
    public_actions: tuple[PlannedPublicAction, ...]
    lead_records: tuple[LeadRecordPlan, ...]
    dm_candidates: tuple[DMCandidatePlan, ...]
    reply_eligible_count: int
    reply_selected_count: int
    platform_actions_executed: int = 0

    @property
    def execution_ready(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "campaign_id": self.campaign_id,
            "account_id": self.account_id,
            "note_id": self.note_id,
            "checked_at": self.checked_at,
            "processing_mode": "one_note_one_engagement_package",
            "one_target_per_heartbeat": True,
            "one_search_per_query": True,
            "policy": self.policy.to_dict(),
            "public_actions": [item.to_dict() for item in self.public_actions],
            "lead_records": [item.to_dict() for item in self.lead_records],
            "dm_candidates": [item.to_dict() for item in self.dm_candidates],
            "reply_eligible_count": self.reply_eligible_count,
            "reply_selected_count": self.reply_selected_count,
            "execution_ready": self.execution_ready,
            "platform_actions_executed": self.platform_actions_executed,
        }


def _candidate_priority(item: PostCandidateSignal) -> tuple[int, int, int, int, str]:
    return (
        0 if item.evidence_level == "A" else 1,
        -item.scores["activity_intent"],
        -item.scores["topic_relevance"],
        -item.scores["natural_reply_opportunity"],
        item.candidate_id,
    )


def _reply_eligible(item: PostCandidateSignal) -> bool:
    return (
        item.evidence_level in {"A", "B"}
        and item.exact_target_visible
        and item.reply_plan_id is not None
        and not item.previously_replied
        and not item.hard_blocks
        and item.scores["risk"] == 0
    )


def build_post_engagement_plan(request: PostEngagementRequest) -> PostEngagementPlan:
    """Select a bounded, non-executable action package for exactly one note."""

    policy = request.policy
    public: list[PlannedPublicAction] = []
    records: list[LeadRecordPlan] = []

    top = request.top_level_comment
    if (
        top is not None
        and request.note_relevance_score >= 50
        and top.validation_ok
        and not top.already_commented
    ):
        public.append(PlannedPublicAction(
            order=1,
            action="top_level_comment",
            candidate_id=f"note:{request.note_id}",
            target_comment_id=None,
            plan_ref=top.plan_id,
            status="awaiting_exact_approval",
            minimum_delay_from_previous_target_seconds=0,
            requires_exact_approval=True,
        ))

    eligible = sorted((item for item in request.candidates if _reply_eligible(item)), key=_candidate_priority)
    selected = eligible[: policy.max_replies_per_note]
    selected_ids = {item.candidate_id for item in selected}
    for item in selected:
        public.append(PlannedPublicAction(
            order=len(public) + 1,
            action="reply_comment",
            candidate_id=item.candidate_id,
            target_comment_id=item.target_comment_id,
            plan_ref=item.reply_plan_id,
            status="awaiting_exact_approval",
            minimum_delay_from_previous_target_seconds=(
                0 if not public else policy.minimum_target_interval_seconds
            ),
            requires_exact_approval=True,
        ))

    likes_used = 0
    for item in sorted(request.candidates, key=_candidate_priority):
        if item.candidate_id in selected_ids or item.evidence_level not in {"A", "B"}:
            continue
        if item.hard_blocks or item.scores["risk"] > 0 or not item.exact_target_visible:
            continue
        if item.like_supported and not item.previously_liked and likes_used < policy.max_comment_likes_per_note:
            public.append(PlannedPublicAction(
                order=len(public) + 1,
                action="like_comment",
                candidate_id=item.candidate_id,
                target_comment_id=item.target_comment_id,
                plan_ref=None,
                status="awaiting_runtime_gates",
                minimum_delay_from_previous_target_seconds=(
                    0 if not public else policy.minimum_target_interval_seconds
                ),
                requires_exact_approval=False,
            ))
            likes_used += 1
        else:
            records.append(LeadRecordPlan(
                candidate_id=item.candidate_id,
                target_comment_id=item.target_comment_id,
                user_id=item.user_id,
                reason="qualified_intent_not_selected_for_public_action",
            ))

    dm_pool = [
        item for item in eligible
        if item.evidence_level == "A"
        and item.scores["topic_relevance"] >= policy.dm_min_topic_relevance
        and item.scores["activity_intent"] >= policy.dm_min_activity_intent
        and not item.previously_dm_contacted
    ]
    dm_candidates: list[DMCandidatePlan] = []
    for item in dm_pool[: policy.max_dm_candidates_per_note]:
        blockers: list[str] = []
        template = request.dm_template
        if template is None or not template.approved_for_campaign:
            blockers.append("preapproved_dm_template_missing")
        if not request.dm_capability_available:
            blockers.append("dm_capability_unavailable")
        dm_candidates.append(DMCandidatePlan(
            candidate_id=item.candidate_id,
            user_id=item.user_id,
            template_id=None if template is None else template.template_id,
            template_hash=None if template is None else template.template_hash,
            approval_ref=None if template is None else template.approval_ref,
            status=("awaiting_exact_message_resolution" if not blockers else "record_only"),
            blockers=tuple(blockers),
        ))

    identity = {
        "campaign_id": request.campaign_id,
        "account_id": request.account_id,
        "note_id": request.note_id,
        "checked_at": request.checked_at,
        "policy": policy.to_dict(),
        "public_actions": [item.to_dict() for item in public],
        "lead_records": [item.to_dict() for item in records],
        "dm_candidates": [item.to_dict() for item in dm_candidates],
    }
    plan_id = "postpkg_" + sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return PostEngagementPlan(
        plan_id=plan_id,
        campaign_id=request.campaign_id,
        account_id=request.account_id,
        note_id=request.note_id,
        checked_at=request.checked_at,
        policy=policy,
        public_actions=tuple(public),
        lead_records=tuple(records),
        dm_candidates=tuple(dm_candidates),
        reply_eligible_count=len(eligible),
        reply_selected_count=len(selected),
    )
