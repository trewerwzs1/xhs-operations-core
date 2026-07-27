"""Fact-bound DM message plans with passive/active permission separation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping

from xhs_operations_core.campaign import Campaign
from xhs_operations_core.contracts import ActionType

from .conversation import DMContractError, DMConversationSnapshot


DM_BLOCK_PATTERNS = (
    ("request_contact", r"(?:把|发|留)(?:你的)?(?:微信|电话|手机号|邮箱)|加我微信|扫码"),
    ("external_link", r"https?://|报名链接"),
    ("guaranteed_outcome", r"保证(?:报名|名额|有位|满意)|一定(?:有位|成功)|肯定(?:有位|成功)"),
    ("medical_or_health_claim", r"治疗|治愈|改善睡眠|软化血管|抗癌|保健功效"),
    ("unsafe_alcohol_promotion", r"未成年|未满18|多喝|喝到醉|拼酒|灌酒"),
)


@dataclass(frozen=True)
class DMFactUse:
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
class DMMessagePlan:
    plan_id: str
    campaign_id: str
    account_id: str
    conversation_id: str
    conversation_snapshot_id: str
    conversation_snapshot_hash: str
    mode: str
    reply_text: str
    evidence_message_ids: tuple[str, ...]
    fact_uses: tuple[DMFactUse, ...]
    checked_at: str
    validation_ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    approval_status: str
    content_hash: str
    platform_actions_executed: int

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "campaign_id": self.campaign_id,
            "account_id": self.account_id,
            "conversation_id": self.conversation_id,
            "conversation_snapshot_id": self.conversation_snapshot_id,
            "conversation_snapshot_hash": self.conversation_snapshot_hash,
            "mode": self.mode,
            "reply_text": self.reply_text,
            "evidence_message_ids": list(self.evidence_message_ids),
            "fact_uses": [item.to_dict() for item in self.fact_uses],
            "checked_at": self.checked_at,
            "validation_ok": self.validation_ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "approval_status": self.approval_status,
            "content_hash": self.content_hash,
            "platform_actions_executed": self.platform_actions_executed,
        }


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DMContractError("DM plan checked_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DMContractError("DM plan checked_at must include timezone")
    return parsed


def build_dm_message_plan(
    *, campaign: Campaign, snapshot: DMConversationSnapshot, draft: Mapping[str, Any]
) -> DMMessagePlan:
    allowed = {"mode", "reply_text", "checked_at", "evidence_message_ids", "fact_uses"}
    if not isinstance(draft, Mapping) or set(draft) != allowed:
        raise DMContractError("DM draft fields are incomplete or unknown")
    mode, text, checked_at = draft["mode"], draft["reply_text"], draft["checked_at"]
    evidence_ids, raw_facts = draft["evidence_message_ids"], draft["fact_uses"]
    if mode not in {"passive_reply", "active_outreach"}:
        raise DMContractError("DM mode is invalid")
    if not isinstance(text, str) or not text.strip() or not isinstance(evidence_ids, list) or not isinstance(raw_facts, list):
        raise DMContractError("DM reply_text, evidence_message_ids, and fact_uses are invalid")
    text = " ".join(text.split())
    checked = _time(checked_at)
    errors: list[str] = []
    warnings: list[str] = []
    if campaign.account_id != snapshot.account_id:
        errors.append("dm_snapshot_account_mismatch")
    if snapshot.opt_out or snapshot.minor_risk or snapshot.risk_signals:
        errors.append("dm_conversation_blocked")
    if mode == "passive_reply":
        if not snapshot.passive_reply_pending:
            errors.append("no_passive_reply_due")
        if snapshot.latest_incoming_message_id not in evidence_ids:
            errors.append("latest_incoming_message_evidence_required")
    else:
        active_allowed = (
            ActionType.DM in campaign.allowed_actions
            and campaign.metadata.get("active_dm_user_approved") is True
            and isinstance(campaign.metadata.get("active_dm_user_approval_ref"), str)
            and bool(campaign.metadata.get("active_dm_user_approval_ref"))
        )
        if not active_allowed:
            errors.append("active_dm_campaign_permission_missing")
    visible_ids = {item.message_id for item in snapshot.messages}
    if any(not isinstance(item, str) or item not in visible_ids for item in evidence_ids):
        errors.append("dm_evidence_message_not_visible")
    if len(text) > 240:
        errors.append("dm_reply_too_long")
    for code, pattern in DM_BLOCK_PATTERNS:
        if re.search(pattern, text, re.I):
            errors.append(code)
    by_id = {item.fact_id: item for item in campaign.facts}
    uses: list[DMFactUse] = []
    seen: set[str] = set()
    for raw in raw_facts:
        if not isinstance(raw, Mapping) or set(raw) != {"fact_id", "claim_text"}:
            raise DMContractError("DM fact use must contain fact_id and claim_text")
        fact_id, claim = raw["fact_id"], raw["claim_text"]
        if not isinstance(fact_id, str) or not isinstance(claim, str) or not fact_id or not claim:
            raise DMContractError("DM fact use values are required")
        if fact_id in seen:
            errors.append("duplicate_dm_fact_use")
            continue
        seen.add(fact_id)
        fact = by_id.get(fact_id)
        if fact is None:
            errors.append(f"unknown_fact_id:{fact_id}")
            continue
        if claim != str(fact.value).strip() or claim not in text:
            errors.append(f"dm_fact_claim_not_exact:{fact_id}")
        if not fact.approved_for_public or not fact.is_valid_at(checked_at):
            errors.append(f"dm_fact_not_usable:{fact_id}")
        if checked < _time(fact.verified_at):
            errors.append(f"dm_fact_not_yet_verified:{fact_id}")
        source = json.dumps(fact.value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        uses.append(DMFactUse(fact.fact_id, fact.kind.value, claim, hashlib.sha256(source.encode()).hexdigest(), fact.dynamic))
    for fact in campaign.facts:
        if fact.dynamic and str(fact.value) in text and fact.fact_id not in seen:
            errors.append(f"unreferenced_dynamic_fact:{fact.fact_id}")
    if not raw_facts:
        warnings.append("dm_value_reply_without_activity_fact")
    errors = list(dict.fromkeys(errors))
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    plan_id = "dm_plan_" + hashlib.sha256(
        f"{campaign.campaign_id}|{snapshot.snapshot_id}|{mode}|{content_hash}".encode()
    ).hexdigest()[:16]
    return DMMessagePlan(
        plan_id, campaign.campaign_id, campaign.account_id, snapshot.conversation_id,
        snapshot.snapshot_id, snapshot.content_hash, mode, text, tuple(evidence_ids), tuple(uses),
        checked_at, not errors, tuple(errors), tuple(warnings),
        "awaiting_human_approval" if not errors else "blocked", content_hash, 0,
    )
