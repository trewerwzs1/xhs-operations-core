"""Compile trusted campaign artifacts into one immutable current-page write plan.

The current-page executor deliberately does not infer business intent.  This
module is the only supported bridge from validated campaign/candidate/message
artifacts to an executable public-reply plan.  The compiler output is also
registered locally so a hand-written JSON plan cannot be approved or executed
through the product CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from xhs_operations_core.campaign import Campaign
from xhs_operations_core.contracts import ActionType
from xhs_operations_core.discovery import CandidateInteractionPlan, DiscoveryPlan
from xhs_operations_core.messaging import MessagePlan
from xhs_operations_core.orchestration import PostEngagementRequest, build_post_engagement_plan
from xhs_operations_core.source_notes import NoteDetailCapture
from xhs_operations_core.style import ReplyStyleProfile
from xhs_operations_core.storage import append_jsonl, read_jsonl

from .planning import MessageApproval, build_approved_comment_plan
from .comment_flow import CommentTarget
from .note_comment_flow import NoteCommentPlan, note_context_hash
from .session import CurrentPageInteractionPlan, InteractionBranch, InteractionSessionError


COMPILER_VERSION = "current-page-reply-v1"
NOTE_COMMENT_COMPILER_VERSION = "current-page-note-comment-v1"
COMMENT_LIKE_COMPILER_VERSION = "current-page-comment-like-v1"
NOTE_LIKE_COMPILER_VERSION = "current-page-note-like-v1"
COMPILER_SOURCE_NAMES = {
    COMPILER_VERSION: {"campaign", "discovery", "candidate", "message", "approval"},
    NOTE_COMMENT_COMPILER_VERSION: {"campaign", "note", "note_comment_plan", "approval"},
    COMMENT_LIKE_COMPILER_VERSION: {
        "campaign", "candidate", "post_engagement_request", "post_engagement_plan"
    },
    NOTE_LIKE_COMPILER_VERSION: {"campaign", "note", "approval"},
}


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompiledCurrentPagePlan:
    """Integrity envelope for a compiler-produced one-write plan."""

    compiler_version: str
    plan: CurrentPageInteractionPlan
    source_artifact_hashes: dict[str, str]
    compiler_hash: str

    def __post_init__(self) -> None:
        if self.compiler_version not in COMPILER_SOURCE_NAMES:
            raise InteractionSessionError("unsupported current-page compiler version")
        expected_names = COMPILER_SOURCE_NAMES[self.compiler_version]
        if set(self.source_artifact_hashes) != expected_names or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in self.source_artifact_hashes.values()
        ):
            raise InteractionSessionError("compiled plan source hashes are incomplete or invalid")
        if self.compiler_hash != self.compute_hash(
            compiler_version=self.compiler_version,
            plan=self.plan,
            source_artifact_hashes=self.source_artifact_hashes,
        ):
            raise InteractionSessionError("compiled current-page plan integrity mismatch")

    @staticmethod
    def compute_hash(
        *,
        compiler_version: str,
        plan: CurrentPageInteractionPlan,
        source_artifact_hashes: Mapping[str, str],
    ) -> str:
        return _canonical_hash({
            "compiler_version": compiler_version,
            "plan": plan.to_dict(),
            "source_artifact_hashes": dict(source_artifact_hashes),
        })

    @classmethod
    def create(
        cls,
        *,
        plan: CurrentPageInteractionPlan,
        source_artifact_hashes: Mapping[str, str],
        compiler_version: str = COMPILER_VERSION,
    ) -> "CompiledCurrentPagePlan":
        hashes = dict(source_artifact_hashes)
        return cls(
            compiler_version=compiler_version,
            plan=plan,
            source_artifact_hashes=hashes,
            compiler_hash=cls.compute_hash(
                compiler_version=compiler_version,
                plan=plan,
                source_artifact_hashes=hashes,
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompiledCurrentPagePlan":
        if not isinstance(value, Mapping) or set(value) != {
            "compiler_version", "plan", "source_artifact_hashes", "compiler_hash"
        }:
            raise InteractionSessionError("compiled current-page plan fields are incomplete or unknown")
        plan = value["plan"]
        hashes = value["source_artifact_hashes"]
        if not isinstance(plan, dict) or not isinstance(hashes, Mapping):
            raise InteractionSessionError("compiled current-page plan payload is invalid")
        if any(not isinstance(key, str) or not isinstance(item, str) for key, item in hashes.items()):
            raise InteractionSessionError("compiled current-page source hashes are invalid")
        return cls(
            compiler_version=str(value["compiler_version"]),
            plan=CurrentPageInteractionPlan.from_dict(plan),
            source_artifact_hashes=dict(hashes),
            compiler_hash=str(value["compiler_hash"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "compiler_version": self.compiler_version,
            "plan": self.plan.to_dict(),
            "source_artifact_hashes": dict(self.source_artifact_hashes),
            "compiler_hash": self.compiler_hash,
        }


def compile_approved_reply_plan(
    *,
    campaign: Campaign,
    discovery_plan: DiscoveryPlan,
    candidate: CandidateInteractionPlan,
    message: MessagePlan,
    approval: MessageApproval,
    result_index: int,
    session_id: str,
    style_exception_ref: str = "",
) -> CompiledCurrentPagePlan:
    """Compile one exact approved reply; no like is bundled into the write."""

    bridge = build_approved_comment_plan(
        campaign=campaign,
        discovery_plan=discovery_plan,
        candidate=candidate,
        message=message,
        approval=approval,
        result_index=result_index,
    )
    style = message.style_alignment
    if style.applied:
        if not style.profile_id or not style.profile_hash:
            raise InteractionSessionError("applied message style profile is incomplete")
        style_profile_id = style.profile_id
        style_profile_hash = style.profile_hash
        approved_style_exception = ""
    else:
        if re.fullmatch(
            r"style-exception-approved:[A-Za-z0-9][A-Za-z0-9_.:-]{2,255}",
            style_exception_ref,
        ) is None:
            raise InteractionSessionError(
                "compiled reply requires an account style profile or exact approved style exception"
            )
        style_profile_id = ""
        style_profile_hash = ""
        approved_style_exception = style_exception_ref

    target = bridge.comment_plan.target
    plan = CurrentPageInteractionPlan(
        plan_id="current_" + bridge.bridge_id.removeprefix("bridge_"),
        session_id=session_id,
        campaign_id=campaign.campaign_id,
        account_id=campaign.account_id,
        candidate_id=candidate.candidate_id,
        note_id=candidate.note_id,
        source_context_ref=f"message:{message.message_plan_id}:{message.content_hash}",
        approval_ref=approval.approval_id,
        branch=InteractionBranch.COMMENT_ENGAGEMENT,
        text=message.reply_text,
        target_comment_id=target.target_comment_id,
        target_context_hash=target.context_hash,
        message_plan_id=message.message_plan_id,
        message_content_hash=message.content_hash,
        message_validation_ref=(
            f"message-validation:{message.message_plan_id}:{message.content_hash}"
        ),
        campaign_fact_validation_ref=(
            f"campaign-facts:{campaign.campaign_id}:{message.message_plan_id}"
        ),
        fact_refs=tuple(item.fact_id for item in message.fact_uses),
        style_profile_id=style_profile_id,
        style_profile_hash=style_profile_hash,
        style_exception_ref=approved_style_exception,
    )
    blockers = plan.content_evidence_blockers()
    if blockers:
        raise InteractionSessionError(
            "compiler produced incomplete content evidence: " + ",".join(blockers)
        )
    return CompiledCurrentPagePlan.create(
        plan=plan,
        source_artifact_hashes={
            "campaign": _canonical_hash(campaign.to_dict()),
            "discovery": _canonical_hash(discovery_plan.to_dict()),
            "candidate": _canonical_hash(candidate.to_dict()),
            "message": _canonical_hash(message.to_dict()),
            "approval": _canonical_hash(approval.to_dict()),
        },
    )


def _moment(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise InteractionSessionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InteractionSessionError(f"{field} must include a timezone")
    return parsed


@dataclass(frozen=True)
class NoteCommentApproval:
    """One user's approval of one exact top-level comment on one exact note."""

    approval_id: str
    account_id: str
    campaign_id: str
    note_id: str
    note_comment_plan_id: str
    comment_content_hash: str
    note_context_hash: str
    approved_at: str
    approved_by: str
    scope: str

    def __post_init__(self) -> None:
        for name in (
            "approval_id", "account_id", "campaign_id", "note_id",
            "note_comment_plan_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value
            ) is None:
                raise InteractionSessionError(f"note comment approval {name} is invalid")
        for name in ("comment_content_hash", "note_context_hash"):
            if re.fullmatch(r"[0-9a-f]{64}", getattr(self, name)) is None:
                raise InteractionSessionError(f"note comment approval {name} is invalid")
        _moment(self.approved_at, "note_comment_approval.approved_at")
        if self.approved_by not in {"user", "controller"} or self.scope != "single_note_target":
            raise InteractionSessionError(
                "note comment compile review must bind one exact note and an allowed reviewer"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NoteCommentApproval":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != fields:
            raise InteractionSessionError(
                "note comment approval fields are incomplete or unknown"
            )
        if any(not isinstance(value[name], str) for name in fields):
            raise InteractionSessionError("note comment approval fields must be strings")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class NoteLikeApproval:
    """One user's approval of one exact visible note like."""

    approval_id: str
    account_id: str
    campaign_id: str
    note_id: str
    note_context_hash: str
    approved_at: str
    approved_by: str
    scope: str

    def __post_init__(self) -> None:
        for name in ("approval_id", "account_id", "campaign_id", "note_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value
            ) is None:
                raise InteractionSessionError(f"note like approval {name} is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.note_context_hash) is None:
            raise InteractionSessionError("note like approval context hash is invalid")
        _moment(self.approved_at, "note_like_approval.approved_at")
        if self.approved_by != "user" or self.scope != "single_note_like":
            raise InteractionSessionError(
                "note like approval must be a user decision for one exact note"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NoteLikeApproval":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != fields:
            raise InteractionSessionError(
                "note like approval fields are incomplete or unknown"
            )
        if any(not isinstance(value[name], str) for name in fields):
            raise InteractionSessionError("note like approval fields must be strings")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def compile_approved_note_like_plan(
    *,
    campaign: Campaign,
    note: NoteDetailCapture,
    approval: NoteLikeApproval,
    session_id: str,
) -> CompiledCurrentPagePlan:
    """Compile one exact note like from immutable visible-note evidence."""

    if ActionType.LIKE not in campaign.allowed_actions:
        raise InteractionSessionError("campaign does not allow likes")
    expected = {
        "account_id": campaign.account_id,
        "campaign_id": campaign.campaign_id,
        "note_id": note.note_id,
        "note_context_hash": note_context_hash(note),
    }
    for field, expected_value in expected.items():
        if getattr(approval, field) != expected_value:
            raise InteractionSessionError(f"note like approval {field} mismatch")
    approval_time = _moment(approval.approved_at, "note_like_approval.approved_at")
    if approval_time < _moment(note.captured_at, "note.captured_at"):
        raise InteractionSessionError("note like approval cannot predate exact note capture")
    if not (
        _moment(campaign.active_from, "campaign.active_from")
        <= approval_time
        <= _moment(campaign.active_until, "campaign.active_until")
    ):
        raise InteractionSessionError("note like approval is outside campaign window")
    identity = f"{campaign.campaign_id}|{note.note_id}|{approval.approval_id}"
    plan = CurrentPageInteractionPlan(
        plan_id="current_note_like_" + sha256(identity.encode("utf-8")).hexdigest()[:16],
        session_id=session_id,
        campaign_id=campaign.campaign_id,
        account_id=campaign.account_id,
        candidate_id=note.note_id,
        note_id=note.note_id,
        source_context_ref=f"note:{note.note_id}:{note_context_hash(note)}",
        approval_ref=approval.approval_id,
        branch=InteractionBranch.NOTE_LIKE_ONLY,
        like_enabled=True,
    )
    return CompiledCurrentPagePlan.create(
        compiler_version=NOTE_LIKE_COMPILER_VERSION,
        plan=plan,
        source_artifact_hashes={
            "campaign": _canonical_hash(campaign.to_dict()),
            "note": _canonical_hash(note.to_dict()),
            "approval": _canonical_hash(approval.to_dict()),
        },
    )


def compile_approved_note_comment_plan(
    *,
    campaign: Campaign,
    note: NoteDetailCapture,
    note_comment_plan: NoteCommentPlan,
    approval: NoteCommentApproval,
    session_id: str,
    style_profile: ReplyStyleProfile | None = None,
    style_exception_ref: str = "",
) -> CompiledCurrentPagePlan:
    """Compile one exact top-level note comment from bounded reviewed inputs."""

    if ActionType.COMMENT not in campaign.allowed_actions:
        raise InteractionSessionError("campaign does not allow top-level comments")
    if (
        note_comment_plan.campaign_id != campaign.campaign_id
        or note_comment_plan.account_id != campaign.account_id
        or note_comment_plan.note_id != note.note_id
        or not note_comment_plan.matches(note)
    ):
        raise InteractionSessionError("note comment plan does not match campaign or exact note")
    content_hash = sha256(note_comment_plan.comment_text.encode("utf-8")).hexdigest()
    expected = {
        "account_id": campaign.account_id,
        "campaign_id": campaign.campaign_id,
        "note_id": note.note_id,
        "note_comment_plan_id": note_comment_plan.plan_id,
        "comment_content_hash": content_hash,
        "note_context_hash": note_context_hash(note),
    }
    for field, expected_value in expected.items():
        if getattr(approval, field) != expected_value:
            raise InteractionSessionError(f"note comment approval {field} mismatch")
    approval_time = _moment(approval.approved_at, "note_comment_approval.approved_at")
    if approval_time < _moment(note.captured_at, "note.captured_at"):
        raise InteractionSessionError("note comment approval cannot predate exact note capture")
    if approval.approved_at != note_comment_plan.approved_at:
        raise InteractionSessionError("note comment plan and approval timestamps must match")
    if not (
        _moment(campaign.active_from, "campaign.active_from")
        <= approval_time
        <= _moment(campaign.active_until, "campaign.active_until")
    ):
        raise InteractionSessionError("note comment approval is outside campaign window")
    if any(claim in note_comment_plan.comment_text for claim in campaign.prohibited_claims):
        raise InteractionSessionError("note comment contains a campaign-prohibited claim")

    fact_refs: list[str] = []
    for fact in campaign.facts:
        rendered = str(fact.value).strip()
        if not rendered or rendered not in note_comment_plan.comment_text:
            continue
        if not fact.approved_for_public or not fact.is_valid_at(approval.approved_at):
            raise InteractionSessionError(
                f"note comment uses an unavailable campaign fact: {fact.fact_id}"
            )
        fact_refs.append(fact.fact_id)

    if style_profile is not None:
        if (
            style_profile.account_id != campaign.account_id
            or style_profile.stores_raw_reply_text
            or re.fullmatch(r"[0-9a-f]{64}", style_profile.content_hash) is None
        ):
            raise InteractionSessionError("note comment style profile is invalid")
        style_profile_id = style_profile.profile_id
        style_profile_hash = style_profile.content_hash
        approved_style_exception = ""
    else:
        if re.fullmatch(
            r"style-exception-approved:[A-Za-z0-9][A-Za-z0-9_.:-]{2,255}",
            style_exception_ref,
        ) is None:
            raise InteractionSessionError(
                "compiled note comment requires style profile or exact approved exception"
            )
        style_profile_id = ""
        style_profile_hash = ""
        approved_style_exception = style_exception_ref

    message_plan_id = "note_message_" + sha256(
        f"{note_comment_plan.plan_id}|{content_hash}|{approval.approval_id}".encode("utf-8")
    ).hexdigest()[:16]
    plan = CurrentPageInteractionPlan(
        plan_id="current_" + note_comment_plan.plan_id,
        session_id=session_id,
        campaign_id=campaign.campaign_id,
        account_id=campaign.account_id,
        candidate_id=note_comment_plan.candidate_id,
        note_id=note.note_id,
        source_context_ref=f"message:{message_plan_id}:{content_hash}",
        approval_ref=approval.approval_id,
        branch=InteractionBranch.NOTE_ENGAGEMENT,
        text=note_comment_plan.comment_text,
        message_plan_id=message_plan_id,
        message_content_hash=content_hash,
        message_validation_ref=f"message-validation:{message_plan_id}:{content_hash}",
        campaign_fact_validation_ref=(
            f"campaign-facts:{campaign.campaign_id}:{message_plan_id}"
        ),
        fact_refs=tuple(fact_refs),
        style_profile_id=style_profile_id,
        style_profile_hash=style_profile_hash,
        style_exception_ref=approved_style_exception,
    )
    blockers = plan.content_evidence_blockers()
    if blockers:
        raise InteractionSessionError(
            "compiler produced incomplete note-comment evidence: " + ",".join(blockers)
        )
    return CompiledCurrentPagePlan.create(
        compiler_version=NOTE_COMMENT_COMPILER_VERSION,
        plan=plan,
        source_artifact_hashes={
            "campaign": _canonical_hash(campaign.to_dict()),
            "note": _canonical_hash(note.to_dict()),
            "note_comment_plan": _canonical_hash(note_comment_plan.to_dict()),
            "approval": _canonical_hash(approval.to_dict()),
        },
    )


def compile_comment_like_plan(
    *,
    campaign: Campaign,
    candidate: CandidateInteractionPlan,
    post_engagement_request: PostEngagementRequest,
    session_id: str,
) -> CompiledCurrentPagePlan:
    """Compile one policy-selected exact comment like without bundling a reply."""

    if ActionType.LIKE not in campaign.allowed_actions:
        raise InteractionSessionError("campaign does not allow likes")
    if (
        candidate.campaign_id != campaign.campaign_id
        or post_engagement_request.campaign_id != campaign.campaign_id
        or post_engagement_request.account_id != campaign.account_id
        or post_engagement_request.note_id != candidate.note_id
    ):
        raise InteractionSessionError(
            "comment-like campaign, candidate, and post engagement request mismatch"
        )
    post_plan = build_post_engagement_plan(post_engagement_request)
    selected = [
        item for item in post_plan.public_actions
        if item.action == "like_comment"
        and item.candidate_id == candidate.candidate_id
        and item.target_comment_id == candidate.target_comment_id
        and item.status == "awaiting_runtime_gates"
        and not item.requires_exact_approval
    ]
    if len(selected) != 1:
        raise InteractionSessionError(
            "comment like is not selected exactly once by the post engagement policy"
        )
    target = CommentTarget.create(
        candidate_id=candidate.candidate_id,
        target_comment_id=candidate.target_comment_id,
        note_id=candidate.note_id,
        commenter=candidate.commenter,
        full_text=candidate.full_text,
        anchor_text=candidate.anchor_text,
    )
    action = selected[0]
    identity = f"{post_plan.plan_id}|{candidate.candidate_id}|{target.context_hash}"
    plan = CurrentPageInteractionPlan(
        plan_id="current_like_" + sha256(identity.encode("utf-8")).hexdigest()[:16],
        session_id=session_id,
        campaign_id=campaign.campaign_id,
        account_id=campaign.account_id,
        candidate_id=candidate.candidate_id,
        note_id=candidate.note_id,
        source_context_ref=(
            f"comment:{candidate.target_comment_id}:{target.context_hash}"
        ),
        approval_ref=f"policy_{post_plan.plan_id}_{action.order}",
        branch=InteractionBranch.COMMENT_LIKE_ONLY,
        like_enabled=True,
        target_comment_id=candidate.target_comment_id,
        target_context_hash=target.context_hash,
    )
    return CompiledCurrentPagePlan.create(
        compiler_version=COMMENT_LIKE_COMPILER_VERSION,
        plan=plan,
        source_artifact_hashes={
            "campaign": _canonical_hash(campaign.to_dict()),
            "candidate": _canonical_hash(candidate.to_dict()),
            "post_engagement_request": _canonical_hash(post_engagement_request.to_dict()),
            "post_engagement_plan": _canonical_hash(post_plan.to_dict()),
        },
    )


class CompiledPlanStore:
    """Append-only provenance registry required by approval and execution."""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.path = Path(runtime_dir) / "interaction_sessions" / "compiled_plans.jsonl"

    def record(self, compiled: CompiledCurrentPagePlan, *, compiled_at: str) -> str:
        for row in read_jsonl(self.path):
            if row.get("plan_id") != compiled.plan.plan_id:
                continue
            if row.get("compiler_hash") != compiled.compiler_hash:
                raise InteractionSessionError(
                    "compiled plan ID already exists with different provenance"
                )
            return compiled.compiler_hash
        append_jsonl(self.path, {
            "plan_id": compiled.plan.plan_id,
            "session_id": compiled.plan.session_id,
            "account_id": compiled.plan.account_id,
            "campaign_id": compiled.plan.campaign_id,
            "note_id": compiled.plan.note_id,
            "plan_hash": _canonical_hash(compiled.plan.to_dict()),
            "compiler_version": compiled.compiler_version,
            "source_artifact_hashes": dict(compiled.source_artifact_hashes),
            "compiler_hash": compiled.compiler_hash,
            "compiled_at": compiled_at,
        })
        return compiled.compiler_hash

    def matches(self, compiled: CompiledCurrentPagePlan) -> bool:
        plan = compiled.plan
        plan_hash = _canonical_hash(plan.to_dict())
        return any(
            row.get("plan_id") == plan.plan_id
            and row.get("session_id") == plan.session_id
            and row.get("account_id") == plan.account_id
            and row.get("campaign_id") == plan.campaign_id
            and row.get("note_id") == plan.note_id
            and row.get("plan_hash") == plan_hash
            and row.get("compiler_version") == compiled.compiler_version
            and row.get("source_artifact_hashes") == compiled.source_artifact_hashes
            and row.get("compiler_hash") == compiled.compiler_hash
            for row in read_jsonl(self.path)
        )
