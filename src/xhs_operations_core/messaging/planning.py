"""Build deterministic, review-only reply plans from Codex-authored drafts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from xhs_operations_core.campaign import Campaign, FactKind
from xhs_operations_core.contracts import ActionType
from xhs_operations_core.discovery import CandidateInteractionPlan
from xhs_operations_core.style import ReplyStyleProfile


class MessagePlanError(ValueError):
    """Raised when a message draft cannot be parsed safely."""


@dataclass(frozen=True)
class FactUse:
    fact_id: str
    fact_kind: str
    claim_text: str
    source_value_hash: str
    dynamic: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "fact_kind": self.fact_kind,
            "claim_text": self.claim_text,
            "source_value_hash": self.source_value_hash,
            "dynamic": self.dynamic,
        }


@dataclass(frozen=True)
class MessageValidation:
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


@dataclass(frozen=True)
class StyleAlignment:
    applied: bool
    profile_id: str | None
    profile_hash: str | None
    confidence: str | None
    score: int | None
    findings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "profile_id": self.profile_id,
            "profile_hash": self.profile_hash,
            "confidence": self.confidence,
            "score": self.score,
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class MessagePlan:
    message_plan_id: str
    campaign_id: str
    candidate_id: str
    action: str
    target_comment_id: str
    target_anchor_text: str
    reply_goal: str
    reply_text: str
    source_evidence_quotes: tuple[str, ...]
    fact_uses: tuple[FactUse, ...]
    public_activity_mention: bool
    checked_at: str
    approval_status: str
    content_hash: str
    validation: MessageValidation
    style_alignment: StyleAlignment

    def to_dict(self) -> dict[str, object]:
        return {
            "message_plan_id": self.message_plan_id,
            "campaign_id": self.campaign_id,
            "candidate_id": self.candidate_id,
            "action": self.action,
            "target_comment_id": self.target_comment_id,
            "target_anchor_text": self.target_anchor_text,
            "reply_goal": self.reply_goal,
            "reply_text": self.reply_text,
            "source_evidence_quotes": list(self.source_evidence_quotes),
            "fact_uses": [item.to_dict() for item in self.fact_uses],
            "public_activity_mention": self.public_activity_mention,
            "checked_at": self.checked_at,
            "approval_status": self.approval_status,
            "content_hash": self.content_hash,
            "validation": self.validation.to_dict(),
            "style_alignment": self.style_alignment.to_dict(),
        }


PUBLIC_LEAD_PATTERNS = (
    r"私信(?:我|一下)?", r"私聊(?:我|一下)?", r"加(?:我)?微信", r"微信号",
    r"联系方式", r"联系电话", r"手机号", r"扫码", r"主页(?:联系|找我|看)",
    r"联系我", r"报名链接", r"小窗", r"wxid[_-]?[a-z0-9]+",
    r"1[3-9]\d{9}", r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}",
)
GUARANTEE_PATTERNS = (
    r"保证(?:报名|名额|有位|成功|体验)", r"肯定(?:有位|能报名|满意)",
    r"一定(?:有位|能报名|满意)", r"包(?:报名|名额|满意)",
)
HEALTH_PATTERNS = (
    r"治疗", r"治愈", r"保健功效", r"改善睡眠", r"软化血管", r"抗癌", r"减肥功效",
)
UNSAFE_DRINKING_PATTERNS = (
    r"未成年", r"未满18", r"中学生", r"高中生", r"多喝", r"喝到醉", r"拼酒", r"灌酒",
)
SENSITIVE_KIND_PATTERNS: dict[FactKind, tuple[str, ...]] = {
    FactKind.START_AT: (r"\d{1,2}月\d{1,2}日", r"\d{1,2}[：:]\d{2}"),
    FactKind.END_AT: (r"结束时间",),
    FactKind.VENUE: (r"地点(?:是|在|：|:)", r"场地(?:是|在|：|:)"),
    FactKind.PRICE: (r"(?:¥|￥)\s*\d+", r"\d+\s*元"),
    FactKind.CAPACITY: (r"限\s*\d+\s*人",),
    FactKind.REMAINING_CAPACITY: (r"(?:剩|余)\s*\d+\s*(?:位|个名额)",),
    FactKind.REGISTRATION_METHOD: (r"报名(?:方式|入口|链接|方法)",),
    FactKind.REGISTRATION_DEADLINE: (r"报名截止",),
    FactKind.ITINERARY: (r"行程(?:是|为|安排|包含|包括)",),
}


def _source_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _matches_any(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _moment(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MessagePlanError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MessagePlanError(f"{field} must include a timezone")
    return parsed


def _style_alignment(
    *, reply_text: str, profile: ReplyStyleProfile | None, campaign: Campaign
) -> tuple[StyleAlignment, list[str], list[str]]:
    if profile is None:
        return StyleAlignment(False, None, None, None, None, ()), [], ["style_profile_not_applied"]
    errors: list[str] = []
    warnings: list[str] = []
    findings: list[str] = []
    if profile.account_id != campaign.account_id:
        errors.append("style_profile_account_mismatch")
    if profile.stores_raw_reply_text:
        errors.append("style_profile_contains_raw_reply_text")
    if hashlib.sha256(reply_text.encode("utf-8")).hexdigest() in profile.source_reply_hashes:
        errors.append("historical_reply_exact_copy")
    score = 100
    target = profile.average_char_count
    lower, upper = max(6, target * 0.5), max(20, target * 1.8)
    if not lower <= len(reply_text) <= upper:
        score -= 30
        findings.append("reply_length_outside_profile_range")
    ends_question = reply_text.rstrip().endswith(("?", "？"))
    if profile.question_ending_ratio >= 0.6 and not ends_question:
        score -= 20
        findings.append("profile_prefers_question_ending")
    elif profile.question_ending_ratio <= 0.2 and ends_question:
        score -= 10
        findings.append("profile_rarely_uses_question_ending")
    ends_soft = reply_text.rstrip().endswith(("呀", "呢", "吧", "啦", "～", "~"))
    if profile.soft_particle_ending_ratio >= 0.6 and not ends_soft:
        score -= 15
        findings.append("profile_prefers_soft_particle_ending")
    if profile.preferred_markers and not any(item in reply_text for item in profile.preferred_markers):
        score -= 10
        findings.append("no_preferred_style_marker")
    score = max(0, score)
    if profile.confidence == "low":
        warnings.append("style_profile_low_confidence")
    if score < 70:
        warnings.append("reply_style_alignment_low")
    return (
        StyleAlignment(
            True,
            profile.profile_id,
            profile.content_hash,
            profile.confidence,
            score,
            tuple(findings),
        ),
        errors,
        warnings,
    )


def build_message_plan(
    *,
    campaign: Campaign,
    candidate: CandidateInteractionPlan,
    draft: Mapping[str, Any],
    style_profile: ReplyStyleProfile | None = None,
) -> MessagePlan:
    allowed = {
        "checked_at", "reply_goal", "reply_text", "source_evidence_quotes",
        "fact_uses", "public_activity_mention",
    }
    unknown = set(draft) - allowed
    if unknown:
        raise MessagePlanError(f"unknown message draft fields: {sorted(unknown)}")
    missing = allowed - set(draft)
    if missing:
        raise MessagePlanError(f"missing message draft fields: {sorted(missing)}")
    checked_at = draft["checked_at"]
    reply_goal = draft["reply_goal"]
    reply_text = draft["reply_text"]
    evidence_quotes = draft["source_evidence_quotes"]
    raw_uses = draft["fact_uses"]
    public_activity_mention = draft["public_activity_mention"]
    if any(not isinstance(item, str) or not item.strip() for item in (checked_at, reply_goal, reply_text)):
        raise MessagePlanError("checked_at, reply_goal, and reply_text must be non-empty strings")
    reply_text = " ".join(reply_text.split())
    if not isinstance(evidence_quotes, list) or any(
        not isinstance(item, str) or not item.strip() for item in evidence_quotes
    ):
        raise MessagePlanError("source_evidence_quotes must be a non-empty string list")
    if not evidence_quotes:
        raise MessagePlanError("at least one source evidence quote is required")
    if not isinstance(raw_uses, list):
        raise MessagePlanError("fact_uses must be a list")
    if type(public_activity_mention) is not bool:
        raise MessagePlanError("public_activity_mention must be a boolean")

    errors: list[str] = []
    warnings: list[str] = []
    if candidate.campaign_id != campaign.campaign_id:
        errors.append("candidate_campaign_mismatch")
    checked_moment = _moment(checked_at, "checked_at")
    if checked_moment < _moment(campaign.updated_at, "campaign.updated_at"):
        errors.append("checked_at_predates_campaign")
    if checked_moment > _moment(campaign.active_until, "campaign.active_until"):
        errors.append("campaign_expired")
    if ActionType.REPLY not in campaign.allowed_actions:
        errors.append("campaign_does_not_allow_reply")
    if candidate.evidence_level not in {"A", "B"}:
        errors.append("candidate_not_actionable")
    if candidate.proposed_action != "reply_comment":
        errors.append("candidate_action_not_reply_comment")
    if candidate.hard_blocks:
        errors.append("candidate_has_hard_blocks")
    if len(reply_text) > 180:
        errors.append("reply_too_long")
    if len(reply_text) < 6:
        errors.append("reply_too_short")

    visible_quotes = {item.quote for item in candidate.evidence}
    if any(quote not in visible_quotes for quote in evidence_quotes):
        errors.append("source_evidence_quote_not_in_candidate")

    fact_by_id = {item.fact_id: item for item in campaign.facts}
    uses: list[FactUse] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_uses):
        if not isinstance(raw, Mapping) or set(raw) != {"fact_id", "claim_text"}:
            raise MessagePlanError(f"fact_uses[{index}] must contain only fact_id and claim_text")
        fact_id, claim_text = raw["fact_id"], raw["claim_text"]
        if not isinstance(fact_id, str) or not fact_id or not isinstance(claim_text, str) or not claim_text:
            raise MessagePlanError(f"fact_uses[{index}] values must be non-empty strings")
        if fact_id in used_ids:
            errors.append("duplicate_fact_use")
            continue
        used_ids.add(fact_id)
        fact = fact_by_id.get(fact_id)
        if fact is None:
            errors.append(f"unknown_fact_id:{fact_id}")
            continue
        if claim_text not in reply_text:
            errors.append(f"fact_claim_not_in_reply:{fact_id}")
        if claim_text != str(fact.value).strip():
            errors.append(f"fact_claim_not_exact_source_value:{fact_id}")
        if not fact.approved_for_public:
            errors.append(f"fact_not_public:{fact_id}")
        if checked_moment < _moment(fact.verified_at, f"fact.verified_at:{fact_id}"):
            errors.append(f"fact_not_yet_verified:{fact_id}")
        if not fact.is_valid_at(checked_at):
            errors.append(f"fact_expired:{fact_id}")
        uses.append(
            FactUse(
                fact_id=fact.fact_id,
                fact_kind=fact.kind.value,
                claim_text=claim_text,
                source_value_hash=_source_hash(fact.value),
                dynamic=fact.dynamic,
            )
        )

    used_kinds = {FactKind(item.fact_kind) for item in uses}
    for kind, patterns in SENSITIVE_KIND_PATTERNS.items():
        if _matches_any(reply_text, patterns) and kind not in used_kinds:
            errors.append(f"unreferenced_sensitive_fact:{kind.value}")
    for fact in campaign.facts:
        rendered = str(fact.value).strip()
        if fact.dynamic and rendered and rendered in reply_text and fact.fact_id not in used_ids:
            errors.append(f"unreferenced_dynamic_fact:{fact.fact_id}")

    if uses and not public_activity_mention:
        errors.append("fact_use_requires_public_activity_mention")
    if public_activity_mention and not uses:
        errors.append("public_activity_mention_requires_fact_use")
    if _matches_any(reply_text, PUBLIC_LEAD_PATTERNS):
        errors.append("public_private_lead")
    if _matches_any(reply_text, GUARANTEE_PATTERNS):
        errors.append("guaranteed_outcome")
    if _matches_any(reply_text, HEALTH_PATTERNS):
        errors.append("medical_or_health_claim")
    if _matches_any(reply_text, UNSAFE_DRINKING_PATTERNS):
        errors.append("unsafe_alcohol_promotion")
    if "http://" in reply_text.lower() or "https://" in reply_text.lower():
        errors.append("external_link")
    if not uses:
        warnings.append("value_only_reply_without_activity_fact")

    style_alignment, style_errors, style_warnings = _style_alignment(
        reply_text=reply_text,
        profile=style_profile,
        campaign=campaign,
    )
    errors.extend(style_errors)
    warnings.extend(style_warnings)

    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))
    content_hash = hashlib.sha256(reply_text.encode("utf-8")).hexdigest()
    message_plan_id = "message_" + hashlib.sha256(
        f"{campaign.campaign_id}|{candidate.candidate_id}|{content_hash}|{style_alignment.profile_hash or 'no_style'}".encode("utf-8")
    ).hexdigest()[:16]
    validation = MessageValidation(not errors, tuple(errors), tuple(warnings))
    return MessagePlan(
        message_plan_id=message_plan_id,
        campaign_id=campaign.campaign_id,
        candidate_id=candidate.candidate_id,
        action="reply_comment",
        target_comment_id=candidate.target_comment_id,
        target_anchor_text=candidate.anchor_text,
        reply_goal=reply_goal.strip(),
        reply_text=reply_text,
        source_evidence_quotes=tuple(evidence_quotes),
        fact_uses=tuple(uses),
        public_activity_mention=public_activity_mention,
        checked_at=checked_at,
        approval_status="awaiting_human_approval" if validation.ok else "blocked",
        content_hash=content_hash,
        validation=validation,
        style_alignment=style_alignment,
    )
