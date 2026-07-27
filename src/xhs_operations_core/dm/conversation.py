"""Bounded read-only DM conversation snapshot and reply permission state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping


class DMContractError(ValueError):
    pass


PRIVATE_PATTERNS = (
    ("phone", re.compile(r"1[3-9]\d{9}")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("external_contact", re.compile(r"(?:微信|vx|V信|wxid)[：:\s_-]*[A-Za-z0-9_-]{4,}", re.I)),
    ("url", re.compile(r"https?://\S+", re.I)),
)
OPT_OUT_PATTERNS = ("不要联系", "别联系", "别私信", "不用回复", "停止联系", "不感兴趣")
MINOR_PATTERNS = ("未成年", "未满18", "初中生", "高中生")


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise DMContractError(f"{field} must be ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DMContractError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DMContractError(f"{field} must include a timezone")
    return parsed


def _redact(value: Any) -> tuple[str, tuple[str, ...]]:
    text = " ".join(str(value or "").split())
    flags: list[str] = []
    for name, pattern in PRIVATE_PATTERNS:
        if pattern.search(text):
            flags.append(name)
            text = pattern.sub("[已脱敏]", text)
    return text, tuple(flags)


@dataclass(frozen=True)
class DMMessage:
    message_id: str
    direction: str
    text: str
    sent_at: str
    privacy_flags: tuple[str, ...]
    source_text_hash: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.message_id) is None:
            raise DMContractError("DM message_id is invalid")
        if self.direction not in {"incoming", "outgoing"}:
            raise DMContractError("DM direction must be incoming or outgoing")
        if not self.text:
            raise DMContractError("DM text is required")
        _time(self.sent_at, "message.sent_at")
        if re.fullmatch(r"[0-9a-f]{64}", self.source_text_hash) is None:
            raise DMContractError("DM source_text_hash is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "direction": self.direction,
            "text": self.text,
            "sent_at": self.sent_at,
            "privacy_flags": list(self.privacy_flags),
            "source_text_hash": self.source_text_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DMMessage":
        required = {
            "message_id", "direction", "text", "sent_at",
            "privacy_flags", "source_text_hash",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise DMContractError("persisted DM message fields are incomplete or unknown")
        flags = value["privacy_flags"]
        if not isinstance(flags, list) or any(not isinstance(item, str) for item in flags):
            raise DMContractError("persisted DM privacy_flags are invalid")
        return cls(
            message_id=value["message_id"],
            direction=value["direction"],
            text=value["text"],
            sent_at=value["sent_at"],
            privacy_flags=tuple(flags),
            source_text_hash=value["source_text_hash"],
        )


@dataclass(frozen=True)
class DMConversationSnapshot:
    snapshot_id: str
    account_id: str
    conversation_id: str
    peer_ref_hash: str
    captured_at: str
    coverage: str
    messages: tuple[DMMessage, ...]
    latest_incoming_message_id: str | None
    passive_reply_pending: bool
    active_outreach_allowed: bool
    opt_out: bool
    minor_risk: bool
    risk_signals: tuple[str, ...]
    state: str
    read_only: bool
    platform_actions_executed: int
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "account_id": self.account_id,
            "conversation_id": self.conversation_id,
            "peer_ref_hash": self.peer_ref_hash,
            "captured_at": self.captured_at,
            "coverage": self.coverage,
            "messages": [item.to_dict() for item in self.messages],
            "latest_incoming_message_id": self.latest_incoming_message_id,
            "passive_reply_pending": self.passive_reply_pending,
            "active_outreach_allowed": self.active_outreach_allowed,
            "opt_out": self.opt_out,
            "minor_risk": self.minor_risk,
            "risk_signals": list(self.risk_signals),
            "state": self.state,
            "read_only": self.read_only,
            "platform_actions_executed": self.platform_actions_executed,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DMConversationSnapshot":
        required = {
            "snapshot_id", "account_id", "conversation_id", "peer_ref_hash",
            "captured_at", "coverage", "messages", "latest_incoming_message_id",
            "passive_reply_pending", "active_outreach_allowed", "opt_out",
            "minor_risk", "risk_signals", "state", "read_only",
            "platform_actions_executed", "content_hash",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise DMContractError("persisted DM snapshot fields are incomplete or unknown")
        raw_messages = value["messages"]
        raw_risks = value["risk_signals"]
        if not isinstance(raw_messages, list) or not isinstance(raw_risks, list):
            raise DMContractError("persisted DM messages or risk signals are invalid")
        messages = tuple(DMMessage.from_dict(item) for item in raw_messages)
        risks = tuple(raw_risks)
        if any(not isinstance(item, str) for item in risks):
            raise DMContractError("persisted DM risk signals are invalid")
        _time(value["captured_at"], "captured_at")
        if value["coverage"] != "bounded_visible_conversation":
            raise DMContractError("persisted DM coverage is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", str(value["peer_ref_hash"])) is None:
            raise DMContractError("persisted DM peer_ref_hash is invalid")
        payload = {
            "account_id": value["account_id"],
            "conversation_id": value["conversation_id"],
            "peer_ref_hash": value["peer_ref_hash"],
            "messages": [item.to_dict() for item in messages],
            "risks": list(risks),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        combined = "\n".join(item.text for item in messages)
        opt_out = any(term in combined for term in OPT_OUT_PATTERNS)
        minor = any(term in combined for term in MINOR_PATTERNS)
        latest_incoming = next((item for item in reversed(messages) if item.direction == "incoming"), None)
        latest_outgoing = next((item for item in reversed(messages) if item.direction == "outgoing"), None)
        pending = bool(
            latest_incoming
            and (latest_outgoing is None or _time(latest_incoming.sent_at, "incoming") > _time(latest_outgoing.sent_at, "outgoing"))
            and not opt_out and not minor and not risks
        )
        state = (
            "blocked_risk" if risks or minor
            else "opted_out" if opt_out
            else "awaiting_passive_reply_plan" if pending
            else "no_reply_due"
        )
        expected = {
            "snapshot_id": "dm_snapshot_" + digest[:16],
            "latest_incoming_message_id": latest_incoming.message_id if latest_incoming else None,
            "passive_reply_pending": pending,
            "active_outreach_allowed": False,
            "opt_out": opt_out,
            "minor_risk": minor,
            "state": state,
            "read_only": True,
            "platform_actions_executed": 0,
            "content_hash": digest,
        }
        if any(value[name] != expected_value for name, expected_value in expected.items()):
            raise DMContractError("persisted DM snapshot integrity check failed")
        return cls(
            snapshot_id=value["snapshot_id"],
            account_id=value["account_id"],
            conversation_id=value["conversation_id"],
            peer_ref_hash=value["peer_ref_hash"],
            captured_at=value["captured_at"],
            coverage=value["coverage"],
            messages=messages,
            latest_incoming_message_id=value["latest_incoming_message_id"],
            passive_reply_pending=pending,
            active_outreach_allowed=False,
            opt_out=opt_out,
            minor_risk=minor,
            risk_signals=risks,
            state=state,
            read_only=True,
            platform_actions_executed=0,
            content_hash=digest,
        )


def build_dm_conversation_snapshot(
    *, account_id: str, captured_at: str, capture: Mapping[str, Any], max_messages: int = 50
) -> DMConversationSnapshot:
    if not account_id or _time(captured_at, "captured_at") is None:
        raise DMContractError("account_id is required")
    if not 1 <= max_messages <= 100:
        raise DMContractError("max_messages must be 1-100")
    if set(capture) != {"conversation_id", "peer_ref", "messages", "risk_signals"}:
        raise DMContractError("DM capture fields are incomplete or unknown")
    conversation_id = capture["conversation_id"]
    peer_ref = capture["peer_ref"]
    raw_messages = capture["messages"]
    risks = capture["risk_signals"]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", str(conversation_id)) is None:
        raise DMContractError("conversation_id is invalid")
    if not isinstance(peer_ref, str) or not peer_ref or not isinstance(raw_messages, list):
        raise DMContractError("peer_ref and messages are required")
    if len(raw_messages) > max_messages or not isinstance(risks, list):
        raise DMContractError("DM capture exceeds bounds or risk signals are invalid")
    if any(not isinstance(item, str) or re.fullmatch(r"[a-z0-9][a-z0-9_:.-]{0,127}", item) is None for item in risks):
        raise DMContractError("DM risk signals must be safe codes")
    messages: list[DMMessage] = []
    seen: set[str] = set()
    previous: datetime | None = None
    for raw in raw_messages:
        if not isinstance(raw, Mapping) or set(raw) != {"message_id", "direction", "text", "sent_at"}:
            raise DMContractError("DM message fields are incomplete or unknown")
        message_id = raw["message_id"]
        if not isinstance(message_id, str) or message_id in seen:
            raise DMContractError("DM message IDs must be unique")
        raw_text = " ".join(str(raw["text"] or "").split())
        text, flags = _redact(raw_text)
        item = DMMessage(
            message_id, raw["direction"], text, raw["sent_at"], flags,
            hashlib.sha256(raw_text.encode()).hexdigest(),
        )
        moment = _time(item.sent_at, "message.sent_at")
        if previous is not None and moment < previous:
            raise DMContractError("DM messages must be chronological")
        previous = moment
        seen.add(message_id)
        messages.append(item)
    combined = "\n".join(item.text for item in messages)
    opt_out = any(term in combined for term in OPT_OUT_PATTERNS)
    minor = any(term in combined for term in MINOR_PATTERNS)
    latest_incoming = next((item for item in reversed(messages) if item.direction == "incoming"), None)
    latest_outgoing = next((item for item in reversed(messages) if item.direction == "outgoing"), None)
    pending = bool(
        latest_incoming
        and (latest_outgoing is None or _time(latest_incoming.sent_at, "incoming") > _time(latest_outgoing.sent_at, "outgoing"))
        and not opt_out and not minor and not risks
    )
    state = (
        "blocked_risk" if risks or minor
        else "opted_out" if opt_out
        else "awaiting_passive_reply_plan" if pending
        else "no_reply_due"
    )
    payload = {
        "account_id": account_id,
        "conversation_id": conversation_id,
        "peer_ref_hash": hashlib.sha256(peer_ref.encode()).hexdigest(),
        "messages": [item.to_dict() for item in messages],
        "risks": risks,
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return DMConversationSnapshot(
        snapshot_id="dm_snapshot_" + digest[:16], account_id=account_id,
        conversation_id=str(conversation_id), peer_ref_hash=payload["peer_ref_hash"],
        captured_at=captured_at, coverage="bounded_visible_conversation",
        messages=tuple(messages), latest_incoming_message_id=latest_incoming.message_id if latest_incoming else None,
        passive_reply_pending=pending, active_outreach_allowed=False,
        opt_out=opt_out, minor_risk=minor, risk_signals=tuple(risks), state=state,
        read_only=True, platform_actions_executed=0, content_hash=digest,
    )
