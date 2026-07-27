"""Evidence-bound scoring for exactly one visible comment candidate."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Sequence
from typing import Any, Mapping

from xhs_operations_core.source_notes import VisibleComment, VisibleThreadSnapshot

from .planning import DiscoveryPlan


class CandidateAssessmentError(ValueError):
    pass


EVIDENCE_TYPES = {
    "topic_interest",
    "activity_intent",
    "location_match",
    "location_mismatch",
    "question_or_request",
}
SCORE_FIELDS = {
    "topic_relevance",
    "activity_intent",
    "location_fit",
    "natural_reply_opportunity",
    "risk",
}


@dataclass(frozen=True)
class CandidateEvidence:
    evidence_type: str
    quote: str
    rationale: str

    def __post_init__(self) -> None:
        if self.evidence_type not in EVIDENCE_TYPES:
            raise CandidateAssessmentError("unsupported candidate evidence type")
        for name, value in (("quote", self.quote), ("rationale", self.rationale)):
            if not isinstance(value, str) or not value.strip():
                raise CandidateAssessmentError(f"candidate evidence {name} is required")

    def to_dict(self) -> dict[str, str]:
        return {
            "evidence_type": self.evidence_type,
            "quote": self.quote,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateEvidence":
        if not isinstance(value, Mapping):
            raise CandidateAssessmentError("candidate evidence must be an object")
        unknown = set(value) - {"evidence_type", "quote", "rationale"}
        if unknown:
            raise CandidateAssessmentError(
                f"unknown candidate evidence fields: {sorted(unknown)}"
            )
        try:
            return cls(
                evidence_type=value["evidence_type"],
                quote=value["quote"],
                rationale=value["rationale"],
            )
        except KeyError as exc:
            raise CandidateAssessmentError(f"missing candidate evidence field: {exc}") from exc


@dataclass(frozen=True)
class CandidateInteractionPlan:
    candidate_id: str
    campaign_id: str
    query_id: str
    segment_id: str
    note_id: str
    target_comment_id: str
    commenter: str
    full_text: str
    anchor_text: str
    evidence_level: str
    proposed_action: str
    scores: dict[str, int]
    evidence: tuple[CandidateEvidence, ...]
    hard_blocks: tuple[str, ...]
    decision_reason: str
    message_status: str
    approval_status: str

    def __post_init__(self) -> None:
        for name in (
            "candidate_id", "campaign_id", "query_id", "segment_id", "note_id",
            "target_comment_id", "commenter", "full_text", "anchor_text",
            "decision_reason", "message_status", "approval_status",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise CandidateAssessmentError(f"candidate {name} is required")
        if self.evidence_level not in {"A", "B", "C", "X"}:
            raise CandidateAssessmentError("candidate evidence_level is invalid")
        if self.proposed_action not in {"reply_comment", "skip"}:
            raise CandidateAssessmentError("candidate proposed_action is invalid")
        if set(self.scores) != SCORE_FIELDS or any(
            type(value) is not int or not 0 <= value <= 100 for value in self.scores.values()
        ):
            raise CandidateAssessmentError("candidate scores are incomplete or out of range")
        if not self.evidence or any(not isinstance(item, CandidateEvidence) for item in self.evidence):
            raise CandidateAssessmentError("candidate evidence is required")
        if any(not isinstance(item, str) or not item.strip() for item in self.hard_blocks):
            raise CandidateAssessmentError("candidate hard_blocks must contain non-empty strings")
        if self.hard_blocks and (self.evidence_level != "X" or self.proposed_action != "skip"):
            raise CandidateAssessmentError("candidate hard blocks must force X and skip")
        if self.evidence_level in {"A", "B"} and self.proposed_action != "reply_comment":
            raise CandidateAssessmentError("actionable candidate must propose reply_comment")
        if self.evidence_level in {"C", "X"} and self.proposed_action != "skip":
            raise CandidateAssessmentError("non-actionable candidate must skip")
        if self.anchor_text not in self.full_text:
            raise CandidateAssessmentError("candidate anchor_text must be inside full_text")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "campaign_id": self.campaign_id,
            "query_id": self.query_id,
            "segment_id": self.segment_id,
            "note_id": self.note_id,
            "target_comment_id": self.target_comment_id,
            "commenter": self.commenter,
            "full_text": self.full_text,
            "anchor_text": self.anchor_text,
            "evidence_level": self.evidence_level,
            "proposed_action": self.proposed_action,
            "scores": dict(self.scores),
            "evidence": [item.to_dict() for item in self.evidence],
            "hard_blocks": list(self.hard_blocks),
            "decision_reason": self.decision_reason,
            "message_status": self.message_status,
            "approval_status": self.approval_status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateInteractionPlan":
        if not isinstance(value, Mapping):
            raise CandidateAssessmentError("candidate interaction plan must be an object")
        allowed = {
            "candidate_id", "campaign_id", "query_id", "segment_id", "note_id",
            "target_comment_id", "commenter", "full_text", "anchor_text",
            "evidence_level", "proposed_action", "scores", "evidence", "hard_blocks",
            "decision_reason", "message_status", "approval_status",
        }
        unknown = set(value) - allowed
        if unknown:
            raise CandidateAssessmentError(
                f"unknown candidate interaction fields: {sorted(unknown)}"
            )
        required = allowed
        missing = required - set(value)
        if missing:
            raise CandidateAssessmentError(
                f"missing candidate interaction fields: {sorted(missing)}"
            )
        scores = value["scores"]
        evidence = value["evidence"]
        hard_blocks = value["hard_blocks"]
        if not isinstance(scores, Mapping) or any(type(item) is not int for item in scores.values()):
            raise CandidateAssessmentError("candidate scores must be an integer mapping")
        if not isinstance(evidence, list) or not isinstance(hard_blocks, list):
            raise CandidateAssessmentError("candidate evidence and hard_blocks must be lists")
        if any(not isinstance(item, str) for item in hard_blocks):
            raise CandidateAssessmentError("candidate hard_blocks must contain strings")
        string_fields = required - {"scores", "evidence", "hard_blocks"}
        if any(not isinstance(value[name], str) for name in string_fields):
            raise CandidateAssessmentError("candidate string fields must be strings")
        return cls(
            candidate_id=value["candidate_id"],
            campaign_id=value["campaign_id"],
            query_id=value["query_id"],
            segment_id=value["segment_id"],
            note_id=value["note_id"],
            target_comment_id=value["target_comment_id"],
            commenter=value["commenter"],
            full_text=value["full_text"],
            anchor_text=value["anchor_text"],
            evidence_level=value["evidence_level"],
            proposed_action=value["proposed_action"],
            scores=dict(scores),
            evidence=tuple(CandidateEvidence.from_dict(item) for item in evidence),
            hard_blocks=tuple(str(item) for item in hard_blocks),
            decision_reason=value["decision_reason"],
            message_status=value["message_status"],
            approval_status=value["approval_status"],
        )


def assess_comment_candidate(
    *,
    discovery_plan: DiscoveryPlan,
    thread: VisibleThreadSnapshot,
    target: VisibleComment,
    query_id: str,
    segment_id: str,
    evidence: Sequence[CandidateEvidence],
    location_status: str,
    minor_risk: bool = False,
    commercial_ad: bool = False,
    previously_contacted: bool = False,
    opt_out: bool = False,
) -> CandidateInteractionPlan:
    if thread.note_id != target_note_id(thread, target):
        raise CandidateAssessmentError("target comment does not belong to thread")
    query = next((item for item in discovery_plan.queries if item.query_id == query_id), None)
    if query is None:
        raise CandidateAssessmentError("query_id is not in discovery plan")
    segment = next(
        (item for item in discovery_plan.audience_profile.segments if item.segment_id == segment_id),
        None,
    )
    if segment is None or query.segment_id != segment_id:
        raise CandidateAssessmentError("segment does not match query")
    if location_status not in {"match", "unknown", "mismatch"}:
        raise CandidateAssessmentError("location_status is invalid")
    combined = f"{thread.title}\n{thread.body}\n{target.text}"
    rows = tuple(evidence)
    for item in rows:
        if item.quote not in combined:
            raise CandidateAssessmentError("candidate evidence quote is not visible in source")

    hard: list[str] = []
    if minor_risk:
        hard.append("minor_risk")
    if commercial_ad:
        hard.append("commercial_ad")
    if previously_contacted:
        hard.append("previously_contacted")
    if opt_out:
        hard.append("opt_out")
    if target.privacy_flags:
        hard.append("contact_information_present")
    if location_status == "mismatch":
        hard.append("location_mismatch")
    if any(term in combined for term in ("未成年人", "未满18", "初中生", "高中生")):
        hard.append("minor_signal_in_text")
    if any(term in target.text for term in ("别回复", "不要联系", "不用联系", "别私信")):
        hard.append("opt_out_signal_in_text")

    counts = {kind: sum(1 for item in rows if item.evidence_type == kind) for kind in EVIDENCE_TYPES}
    if location_status == "match" and counts["location_match"] < 1:
        raise CandidateAssessmentError("location match requires visible location evidence")
    if location_status == "mismatch" and counts["location_mismatch"] < 1:
        raise CandidateAssessmentError("location mismatch requires visible location evidence")
    topic_score = min(100, counts["topic_interest"] * 35 + counts["activity_intent"] * 15)
    intent_score = min(100, counts["activity_intent"] * 45 + counts["question_or_request"] * 20)
    location_score = {"match": 100, "unknown": 25, "mismatch": 0}[location_status]
    natural_score = 0
    if 4 <= len(target.text) <= 80:
        natural_score += 55
    if "?" in target.text or "？" in target.text:
        natural_score += 25
    if counts["question_or_request"]:
        natural_score += 20
    natural_score = min(100, natural_score)
    risk_score = min(100, len(set(hard)) * 35)
    scores = {
        "topic_relevance": topic_score,
        "activity_intent": intent_score,
        "location_fit": location_score,
        "natural_reply_opportunity": natural_score,
        "risk": risk_score,
    }
    if hard:
        level, action = "X", "skip"
        reason = "hard_block:" + ",".join(dict.fromkeys(hard))
    elif topic_score >= 50 and intent_score >= 45 and natural_score >= 55 and location_score >= 25:
        level, action = "A", "reply_comment"
        reason = "visible topic and activity-intent evidence support a natural reply"
    elif topic_score >= 35 and natural_score >= 55 and location_score >= 25:
        level, action = "B", "reply_comment"
        reason = "visible interest evidence supports a cautious value reply"
    else:
        level, action = "C", "skip"
        reason = "visible evidence is insufficient for public interaction"
    normalized = " ".join(target.text.split())
    anchor = normalized if len(normalized) <= 32 else normalized[:32]
    if len(anchor) < 4:
        level, action, reason = "C", "skip", "target comment is too short for exact matching"
    candidate_id = "candidate_" + hashlib.sha256(
        f"{discovery_plan.campaign_id}|{query_id}|{thread.note_id}|{target.comment_id}".encode("utf-8")
    ).hexdigest()[:16]
    return CandidateInteractionPlan(
        candidate_id=candidate_id,
        campaign_id=discovery_plan.campaign_id,
        query_id=query_id,
        segment_id=segment_id,
        note_id=thread.note_id,
        target_comment_id=target.comment_id,
        commenter=target.commenter,
        full_text=normalized,
        anchor_text=anchor,
        evidence_level=level,
        proposed_action=action,
        scores=scores,
        evidence=rows,
        hard_blocks=tuple(dict.fromkeys(hard)),
        decision_reason=reason,
        message_status="not_generated" if action == "reply_comment" else "not_applicable",
        approval_status="awaiting_message_and_human_review" if action == "reply_comment" else "blocked",
    )


def target_note_id(thread: VisibleThreadSnapshot, target: VisibleComment) -> str:
    if target not in thread.comments:
        return ""
    return thread.note_id
