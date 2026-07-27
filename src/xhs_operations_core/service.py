"""Minimal inbound comment/DM service queue on the single Run Agent gateway."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .action_preflight import (
    ActionPreflightError,
    RuntimeMode,
    UnifiedActionPreflightStore,
    UnifiedActionRequest,
    UnifiedPreflightDecision,
    UnifiedPreflightState,
)
from .platform.xhs import BOUNDED_WRITE_UAT_CONFIRMATION, RunAgentClient, RunAgentError
from .storage import append_jsonl, read_json, write_json_atomic
from .style import StyleProfileStore
from .account_voice import build_account_voice_constraint


class ServiceContractError(ValueError):
    pass


SERVICE_REPLY_CONFIRMATION = "I_APPROVE_SINGLE_SERVICE_REPLY"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CONTACT = re.compile(
    r"(?:1[3-9]\d{9}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
    r"(?:微信|vx|V信|wxid)[：:\s_-]*[A-Za-z0-9_-]{4,}|https?://\S+)",
    re.I,
)
_FACT_SENSITIVE = re.compile(
    r"(?:\d|[二三四五六七八九十两]点|一点(?:钟|半)|上午|下午|今晚|明天|后天|"
    r"(?:价格|费用|日期|时间|地点|地址|名额|余位|行程)\s*[：:是为在有]|"
    r"(?:名额|余位)\s*(?:还有|剩余)|报名\s*(?:开始|截止|开放))"
)
_PROMOTIONAL = re.compile(r"(?:欢迎报名|赶紧报名|私信我|加微信|加我|点击主页|保证|一定能)")
_OPT_OUT = re.compile(
    r"(?:不要再(?:联系|回复|发消息)|别再(?:联系|回复|发消息)|停止联系|停止回复|退订|"
    r"unsubscribe|do\s+not\s+contact|stop\s+(?:messaging|contacting))",
    re.I,
)


def _safe_id(value: str, field: str) -> str:
    result = str(value or "").strip()
    if _SAFE_ID.fullmatch(result) is None:
        raise ServiceContractError(f"service {field} is invalid")
    return result


def _hash(value: str, field: str, *, optional: bool = False) -> str:
    result = str(value or "")
    if optional and not result:
        return ""
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise ServiceContractError(f"service {field} must be SHA-256 hex")
    return result


def _time(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ServiceContractError(f"service {field} must be timezone-aware ISO") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ServiceContractError(f"service {field} must include timezone")
    return value


def _text(value: str, *, maximum: int) -> str:
    result = " ".join(str(value or "").split())
    if not result or len(result) > maximum or "\ufffd" in result or set(result) == {"?"}:
        raise ServiceContractError("service text is empty, too long or invalid Unicode")
    return result


@dataclass(frozen=True)
class ServiceInboxItem:
    schema_version: int
    item_id: str
    account_id: str
    channel: str
    source_item_hash: str
    incoming_text: str
    incoming_text_hash: str
    privacy_redacted: bool
    unread: bool
    peer_ref_hash: str
    note_id: str
    source_comment_id: str
    conversation_id: str
    target_context_hash: str
    captured_at: str
    opened_at: str
    state: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ServiceContractError("unsupported service item schema")
        _safe_id(self.item_id, "item_id")
        _safe_id(self.account_id, "account_id")
        if self.channel not in {"comments", "dm"}:
            raise ServiceContractError("service item channel is invalid")
        _hash(self.source_item_hash, "source_item_hash")
        text = _text(self.incoming_text, maximum=500)
        if sha256(text.encode("utf-8")).hexdigest() != _hash(
            self.incoming_text_hash, "incoming_text_hash"
        ):
            raise ServiceContractError("service incoming text hash mismatch")
        if type(self.privacy_redacted) is not bool or type(self.unread) is not bool:
            raise ServiceContractError("service privacy/unread state is invalid")
        _hash(self.peer_ref_hash, "peer_ref_hash", optional=True)
        for field in ("note_id", "source_comment_id", "conversation_id"):
            value = getattr(self, field)
            if value:
                _safe_id(value, field)
        _hash(self.target_context_hash, "target_context_hash", optional=True)
        _time(self.captured_at, "captured_at")
        if self.opened_at:
            _time(self.opened_at, "opened_at")
        if self.state not in {"queued", "opened", "replied", "dismissed", "blocked"}:
            raise ServiceContractError("service item state is invalid")
        if self.state in {"opened", "replied"}:
            if not self.opened_at or not self.target_context_hash:
                raise ServiceContractError("opened service item lacks exact target context")
            if self.channel == "comments" and (not self.note_id or not self.source_comment_id):
                raise ServiceContractError("opened comment service item lacks exact comment")
            if self.channel == "dm" and (not self.peer_ref_hash or not self.conversation_id):
                raise ServiceContractError("opened DM service item lacks exact conversation")

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ServiceInboxItem":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ServiceContractError("service item fields are incomplete or unknown")
        return cls(**dict(value))

    @property
    def dedupe_key_hash(self) -> str:
        return sha256(f"{self.channel}|{self.source_item_hash}".encode("utf-8")).hexdigest()


class ServiceQueueStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "service"
        self.items_root = self.root / "items"
        self.scans_path = self.root / "scans.jsonl"
        self.conversations_root = self.root / "conversations"
        self.plans_root = self.root / "plans"
        self.approvals_root = self.root / "approvals"
        self.receipts_root = self.root / "receipts"

    def item_path(self, item_id: str) -> Path:
        return self.items_root / f"{_safe_id(item_id, 'item_id')}.json"

    def save_item(self, item: ServiceInboxItem) -> Path:
        payload = item.to_dict()
        envelope_hash = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        path = self.item_path(item.item_id)
        write_json_atomic(path, {
            "schema_version": 1,
            "item": payload,
            "envelope_hash": envelope_hash,
        })
        return path

    def load_item(self, item_id: str) -> ServiceInboxItem:
        value = read_json(self.item_path(item_id), default=None)
        if not isinstance(value, dict) or set(value) != {"schema_version", "item", "envelope_hash"}:
            raise ServiceContractError("stored service item envelope is invalid")
        if value["schema_version"] != 1 or not isinstance(value["item"], dict):
            raise ServiceContractError("stored service item envelope version is invalid")
        expected = sha256(
            json.dumps(value["item"], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if value["envelope_hash"] != expected:
            raise ServiceContractError("stored service item integrity failed")
        item = ServiceInboxItem.from_dict(value["item"])
        if item.item_id != item_id:
            raise ServiceContractError("stored service item identity mismatch")
        return item

    def list_items(self, *, account_id: str, channel: str | None = None) -> tuple[ServiceInboxItem, ...]:
        _safe_id(account_id, "account_id")
        if channel is not None and channel not in {"comments", "dm"}:
            raise ServiceContractError("service queue channel is invalid")
        items: list[ServiceInboxItem] = []
        if self.items_root.is_dir():
            for path in sorted(self.items_root.glob("*.json")):
                item = self.load_item(path.stem)
                if item.account_id == account_id and (channel is None or item.channel == channel):
                    items.append(item)
        return tuple(sorted(items, key=lambda item: (item.captured_at, item.item_id)))

    def next_queued(self, *, account_id: str, channel: str) -> ServiceInboxItem:
        items = [
            item for item in self.list_items(account_id=account_id, channel=channel)
            if item.state == "queued"
        ]
        if not items:
            raise ServiceContractError("service queue has no queued item for this channel")
        return items[0]

    def upsert_capture(
        self,
        *,
        account_id: str,
        channel: str,
        captured_at: str,
        capture: Mapping[str, Any],
    ) -> tuple[ServiceInboxItem, ...]:
        account_id = _safe_id(account_id, "account_id")
        _time(captured_at, "captured_at")
        if channel not in {"comments", "dm"}:
            raise ServiceContractError("service capture channel is invalid")
        if (
            not isinstance(capture, Mapping)
            or capture.get("channel") != channel
            or capture.get("coverage") != "bounded_visible_service_inbox"
            or capture.get("read_only") is not True
            or capture.get("platform_actions_executed") != 0
            or not isinstance(capture.get("items"), list)
        ):
            raise ServiceContractError("service capture contract is invalid")
        saved: list[ServiceInboxItem] = []
        for raw in capture["items"]:
            if not isinstance(raw, Mapping):
                raise ServiceContractError("service capture item is invalid")
            source_hash = _hash(str(raw.get("itemHash") or ""), "source_item_hash")
            text = _text(str(raw.get("incomingText") or ""), maximum=500)
            text_hash = _hash(str(raw.get("incomingTextHash") or ""), "incoming_text_hash")
            if sha256(text.encode("utf-8")).hexdigest() != text_hash:
                raise ServiceContractError("service capture text hash mismatch")
            item_id = "service_item_" + source_hash[:20]
            existing = None
            if self.item_path(item_id).is_file():
                existing = self.load_item(item_id)
                if existing.account_id != account_id or existing.channel != channel:
                    raise ServiceContractError("service item hash collides across account or channel")
            peer_ref_hash = _hash(str(raw.get("peer_ref_hash") or ""), "peer_ref_hash", optional=True)
            note_id = str(raw.get("noteId") or "")
            comment_id = str(raw.get("commentId") or "")
            if note_id:
                _safe_id(note_id, "note_id")
            if comment_id:
                _safe_id(comment_id, "source_comment_id")
            item = ServiceInboxItem(
                schema_version=1,
                item_id=item_id,
                account_id=account_id,
                channel=channel,
                source_item_hash=source_hash,
                incoming_text=text,
                incoming_text_hash=text_hash,
                privacy_redacted=raw.get("privacyRedacted") is True,
                unread=raw.get("unread") is True,
                peer_ref_hash=(existing.peer_ref_hash if existing and existing.peer_ref_hash else peer_ref_hash),
                note_id=(existing.note_id if existing and existing.note_id else note_id),
                source_comment_id=(existing.source_comment_id if existing and existing.source_comment_id else comment_id),
                conversation_id=existing.conversation_id if existing else "",
                target_context_hash=existing.target_context_hash if existing else "",
                captured_at=existing.captured_at if existing else captured_at,
                opened_at=existing.opened_at if existing else "",
                state=existing.state if existing else "queued",
            )
            self.save_item(item)
            saved.append(item)
        batch_payload = {
            "account_id": account_id,
            "channel": channel,
            "captured_at": captured_at,
            "item_ids": [item.item_id for item in saved],
            "item_hashes": [item.source_item_hash for item in saved],
        }
        batch_hash = sha256(
            json.dumps(batch_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        append_jsonl(self.scans_path, {
            "schema_version": 1,
            **batch_payload,
            "batch_id": "service_batch_" + batch_hash[:20],
            "batch_hash": batch_hash,
            "captured_item_count": len(saved),
            "coverage": "bounded_visible_service_inbox",
            "raw_inbox_returned": False,
            "platform_actions_executed": 0,
        })
        return tuple(saved)

    def save_conversation(self, item_id: str, snapshot: Mapping[str, Any]) -> Path:
        path = self.conversations_root / f"{_safe_id(item_id, 'item_id')}.json"
        write_json_atomic(path, dict(snapshot))
        return path

    def save_plan(self, plan: "ServiceReplyPlan") -> Path:
        path = self.plans_root / f"{plan.plan_id}.json"
        existing = read_json(path, default=None)
        if existing is not None and existing != plan.to_dict():
            raise ServiceContractError("a different service plan already exists")
        write_json_atomic(path, plan.to_dict())
        return path

    def load_plan(self, plan_id: str) -> "ServiceReplyPlan":
        value = read_json(
            self.plans_root / f"{_safe_id(plan_id, 'plan_id')}.json",
            default=None,
        )
        if not isinstance(value, dict):
            raise ServiceContractError("service reply plan is missing")
        plan = ServiceReplyPlan.from_dict(value)
        if plan.plan_id != plan_id:
            raise ServiceContractError("stored service plan identity mismatch")
        return plan

    def save_approval(self, approval: "ServiceReplyApproval") -> Path:
        path = self.approvals_root / f"{approval.plan_id}.json"
        existing = read_json(path, default=None)
        if existing is not None and existing != approval.to_dict():
            raise ServiceContractError("a different service approval already exists")
        write_json_atomic(path, approval.to_dict())
        return path

    def load_approval(self, plan: "ServiceReplyPlan") -> "ServiceReplyApproval":
        value = read_json(self.approvals_root / f"{plan.plan_id}.json", default=None)
        if not isinstance(value, dict):
            raise ServiceContractError("service reply approval is missing")
        approval = ServiceReplyApproval.from_dict(value)
        if approval.plan_id != plan.plan_id or approval.plan_hash != plan.content_hash:
            raise ServiceContractError("service approval does not match exact plan")
        return approval

    def receipt_path(self, plan_id: str) -> Path:
        return self.receipts_root / f"{_safe_id(plan_id, 'plan_id')}.json"

    def save_receipt(
        self,
        *,
        plan: "ServiceReplyPlan",
        result: Mapping[str, Any],
        recorded_at: str,
        operation_receipt: Mapping[str, Any],
    ) -> Path:
        path = self.receipt_path(plan.plan_id)
        if path.exists():
            raise ServiceContractError("service reply plan already has a receipt")
        write_json_atomic(path, {
            "schema_version": 2,
            "plan_id": plan.plan_id,
            "plan_hash": plan.content_hash,
            "item_id": plan.item_id,
            "account_id": plan.account_id,
            "channel": plan.channel,
            "result": dict(result),
            "recorded_at": _time(recorded_at, "recorded_at"),
            "operation_receipt": dict(operation_receipt),
        })
        return path

    def mark_opened(
        self,
        item: ServiceInboxItem,
        *,
        opened_at: str,
        note_id: str = "",
        comment_id: str = "",
        conversation_id: str = "",
        peer_ref_hash: str = "",
        target_context_hash: str,
    ) -> ServiceInboxItem:
        updated = replace(
            item,
            note_id=note_id or item.note_id,
            source_comment_id=comment_id or item.source_comment_id,
            conversation_id=conversation_id or item.conversation_id,
            peer_ref_hash=peer_ref_hash or item.peer_ref_hash,
            target_context_hash=target_context_hash,
            opened_at=_time(opened_at, "opened_at"),
            state="opened",
        )
        self.save_item(updated)
        return updated

    def mark_replied(self, item: ServiceInboxItem) -> ServiceInboxItem:
        updated = replace(item, state="replied")
        self.save_item(updated)
        return updated

    def mark_blocked(self, item: ServiceInboxItem) -> ServiceInboxItem:
        updated = replace(item, state="blocked")
        self.save_item(updated)
        return updated

    def status(self, *, account_id: str) -> dict[str, Any]:
        items = self.list_items(account_id=account_id)
        counts = {state: 0 for state in ("queued", "opened", "replied", "dismissed", "blocked")}
        channel_counts = {"comments": 0, "dm": 0}
        for item in items:
            counts[item.state] += 1
            channel_counts[item.channel] += 1
        return {
            "schema_version": 1,
            "account_id": account_id,
            "total": len(items),
            "by_state": counts,
            "by_channel": channel_counts,
            "raw_items_returned": False,
            "platform_actions_executed": 0,
        }


def scan_service_inbox(
    *,
    client: RunAgentClient,
    store: ServiceQueueStore,
    account_id: str,
    channel: str,
    captured_at: str,
    max_items: int = 20,
) -> dict[str, Any]:
    navigation = client.open_service_inbox(channel=channel)
    capture = client.capture_service_inbox(channel=channel, max_items=max_items)
    items = store.upsert_capture(
        account_id=account_id,
        channel=channel,
        captured_at=captured_at,
        capture=capture,
    )
    batch_payload = {
        "account_id": account_id,
        "channel": channel,
        "captured_at": captured_at,
        "item_ids": [item.item_id for item in items],
        "item_hashes": [item.source_item_hash for item in items],
    }
    batch_hash = sha256(
        json.dumps(batch_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "batch_id": "service_batch_" + batch_hash[:20],
        "batch_hash": batch_hash,
        "navigation": navigation,
        "captured_item_count": len(items),
        "queue_status": store.status(account_id=account_id),
        "raw_items_returned": False,
        "platform_actions_executed": 0,
    }


def _comment_rows(detail: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    result: list[tuple[str, str, str, str]] = []

    def visit(rows: object) -> None:
        if not isinstance(rows, list):
            return
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            comment_id = str(raw.get("id") or "")
            user = raw.get("user") if isinstance(raw.get("user"), Mapping) else raw.get("userInfo")
            user = user if isinstance(user, Mapping) else {}
            user_id = str(user.get("userId") or "")
            nickname = str(user.get("nickname") or user.get("nickName") or "")
            text = " ".join(str(raw.get("content") or "").split())
            if comment_id and user_id and nickname and text:
                result.append((comment_id, user_id, nickname, text))
            visit(raw.get("subComments"))

    visit(detail.get("comments"))
    return result


def _resolve_comment_target(
    item: ServiceInboxItem,
    *,
    note_id: str,
    detail: Mapping[str, Any],
) -> tuple[str, str, str]:
    candidates = _comment_rows(detail)
    if item.source_comment_id:
        candidates = [row for row in candidates if row[0] == item.source_comment_id]
    if item.peer_ref_hash:
        candidates = [
            row for row in candidates
            if sha256(row[1].encode("utf-8")).hexdigest() == item.peer_ref_hash
        ]
    if not item.source_comment_id:
        incoming = item.incoming_text
        candidates = [
            row for row in candidates
            if row[3] in incoming
            or incoming in row[3]
            or sha256(row[3].encode("utf-8")).hexdigest() == item.incoming_text_hash
        ]
    if len(candidates) != 1:
        raise ServiceContractError("service comment target is missing or ambiguous")
    comment_id, user_id, nickname, text = candidates[0]
    peer_hash = sha256(user_id.encode("utf-8")).hexdigest()
    context_hash = sha256(
        f"{note_id}\n{comment_id}\n{nickname.strip()}\n{' '.join(text.split())}".encode("utf-8")
    ).hexdigest()
    return comment_id, peer_hash, context_hash


def _dm_context_hash(snapshot: Mapping[str, Any]) -> str:
    messages = snapshot.get("messages")
    if not isinstance(messages, list):
        raise ServiceContractError("service DM snapshot messages are invalid")
    stable_messages: list[dict[str, str]] = []
    for row in messages:
        if not isinstance(row, Mapping):
            raise ServiceContractError("service DM snapshot message is invalid")
        direction = str(row.get("direction") or "")
        source_text_hash = str(row.get("source_text_hash") or "")
        if direction not in {"incoming", "outgoing"}:
            raise ServiceContractError("service DM message direction is invalid")
        _hash(source_text_hash, "dm_source_text_hash")
        stable_messages.append({
            "direction": direction,
            "source_text_hash": source_text_hash,
        })
    peer_ref_hash = str(snapshot.get("peer_ref_hash") or "")
    _hash(peer_ref_hash, "peer_ref_hash")
    conversation_id = _safe_id(str(snapshot.get("conversation_id") or ""), "conversation_id")
    return sha256(
        json.dumps({
            "conversation_id": conversation_id,
            "peer_ref_hash": peer_ref_hash,
            "messages": stable_messages,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def open_next_service_item(
    *,
    client: RunAgentClient,
    store: ServiceQueueStore,
    account_id: str,
    channel: str,
    opened_at: str,
    max_comments: int = 200,
    max_messages: int = 50,
) -> dict[str, Any]:
    item = store.next_queued(account_id=account_id, channel=channel)
    client.open_service_inbox(channel=channel)
    navigation = client.open_service_item(
        channel=channel,
        expected_item_hash=item.source_item_hash,
    )
    if channel == "comments":
        note_id = str(navigation.get("noteId") or item.note_id)
        if not note_id or (item.note_id and note_id != item.note_id):
            raise ServiceContractError("opened service note differs from queued target")
        detail = client.get_current_feed_detail(note_id, max_comment_items=max_comments)
        comment_id, peer_hash, context_hash = _resolve_comment_target(
            item,
            note_id=note_id,
            detail=detail,
        )
        opened = store.mark_opened(
            item,
            opened_at=opened_at,
            note_id=note_id,
            comment_id=comment_id,
            peer_ref_hash=peer_hash,
            target_context_hash=context_hash,
        )
        context: dict[str, Any] = {
            "note_id": note_id,
            "comment_id": comment_id,
            "target_context_hash": context_hash,
        }
    else:
        peer_hash = str(navigation.get("peer_ref_hash") or item.peer_ref_hash)
        _hash(peer_hash, "peer_ref_hash")
        conversation_id = "xhs_dm_" + peer_hash[:24]
        snapshot, evidence = client.capture_current_dm_snapshot(
            account_id=account_id,
            conversation_id=conversation_id,
            expected_peer_ref_hash=peer_hash,
            captured_at=opened_at,
            max_messages=max_messages,
        )
        snapshot_payload = snapshot.to_dict()
        store.save_conversation(item.item_id, snapshot_payload)
        context_hash = _dm_context_hash(snapshot_payload)
        opened = store.mark_opened(
            item,
            opened_at=opened_at,
            conversation_id=conversation_id,
            peer_ref_hash=peer_hash,
            target_context_hash=context_hash,
        )
        context = {
            "conversation_id": conversation_id,
            "conversation_snapshot": snapshot.to_dict(),
            "capture_evidence": evidence,
            "target_context_hash": context_hash,
        }
    return {
        "item": opened.to_dict(),
        "context": context,
        "navigation": navigation,
        "platform_actions_executed": 0,
    }


@dataclass(frozen=True)
class ServiceReplyPlan:
    schema_version: int
    plan_id: str
    account_id: str
    item_id: str
    channel: str
    target_context_hash: str
    reply_text: str
    reply_hash: str
    reply_voice_profile_id: str
    reply_voice_profile_hash: str
    fact_refs: tuple[str, ...]
    checked_at: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ServiceContractError("unsupported service reply plan schema")
        for field in ("plan_id", "account_id", "item_id", "reply_voice_profile_id"):
            _safe_id(getattr(self, field), field)
        if self.channel not in {"comments", "dm"}:
            raise ServiceContractError("service reply channel is invalid")
        _hash(self.target_context_hash, "target_context_hash")
        maximum = 180 if self.channel == "comments" else 240
        text = _text(self.reply_text, maximum=maximum)
        if sha256(text.encode("utf-8")).hexdigest() != _hash(self.reply_hash, "reply_hash"):
            raise ServiceContractError("service reply hash mismatch")
        _hash(self.reply_voice_profile_hash, "reply_voice_profile_hash")
        if not isinstance(self.fact_refs, tuple) or any(not isinstance(item, str) for item in self.fact_refs):
            raise ServiceContractError("service fact_refs are invalid")
        if self.fact_refs:
            raise ServiceContractError("V2 service facts require a later explicit fact binding")
        _time(self.checked_at, "checked_at")
        _hash(self.content_hash, "content_hash")

    def _hash_payload(self) -> dict[str, object]:
        value = self.to_dict()
        value.pop("plan_id")
        value.pop("content_hash")
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "account_id": self.account_id,
            "item_id": self.item_id,
            "channel": self.channel,
            "target_context_hash": self.target_context_hash,
            "reply_text": self.reply_text,
            "reply_hash": self.reply_hash,
            "reply_voice_profile_id": self.reply_voice_profile_id,
            "reply_voice_profile_hash": self.reply_voice_profile_hash,
            "fact_refs": list(self.fact_refs),
            "checked_at": self.checked_at,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ServiceReplyPlan":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ServiceContractError("service reply plan fields are incomplete or unknown")
        converted = dict(value)
        if not isinstance(converted["fact_refs"], list):
            raise ServiceContractError("service reply fact_refs must be a list")
        converted["fact_refs"] = tuple(converted["fact_refs"])
        plan = cls(**converted)
        expected = sha256(
            json.dumps(plan._hash_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if plan.content_hash != expected or plan.plan_id != "service_plan_" + expected[:20]:
            raise ServiceContractError("service reply plan integrity failed")
        return plan


def build_service_reply_plan(
    *,
    runtime_dir: Path,
    item_id: str,
    draft: Mapping[str, Any],
    checked_at: str,
) -> ServiceReplyPlan:
    if not isinstance(draft, Mapping) or set(draft) != {"reply_text", "fact_refs"}:
        raise ServiceContractError("service draft fields must be reply_text and fact_refs")
    store = ServiceQueueStore(runtime_dir)
    item = store.load_item(item_id)
    if item.state != "opened":
        raise ServiceContractError("service reply requires one opened exact item")
    if _OPT_OUT.search(item.incoming_text):
        store.mark_blocked(item)
        raise ServiceContractError("service item contains an opt-out request")
    maximum = 180 if item.channel == "comments" else 240
    reply = _text(str(draft["reply_text"]), maximum=maximum)
    if _CONTACT.search(reply):
        raise ServiceContractError("service reply cannot contain contact details or links")
    if _PROMOTIONAL.search(reply):
        raise ServiceContractError("service reply cannot contain forced promotion or guarantees")
    if _FACT_SENSITIVE.search(reply):
        raise ServiceContractError("service reply with dynamic facts requires explicit fact binding")
    fact_refs = draft["fact_refs"]
    if not isinstance(fact_refs, list) or fact_refs:
        raise ServiceContractError("initial V2 service reply requires empty fact_refs")
    voice_constraint = build_account_voice_constraint(
        runtime_dir,
        account_id=item.account_id,
    )
    if voice_constraint["mode"] == "blocked":
        raise ServiceContractError("AccountVoice integrity failed; neutral fallback is not allowed")
    if voice_constraint["mode"] == "account_voice":
        profile = StyleProfileStore(runtime_dir).load(item.account_id)
        reply_voice_profile_id = profile.profile_id
        reply_voice_profile_hash = profile.content_hash
    else:
        neutral_hash = sha256(
            json.dumps(
                {
                    "account_id": item.account_id,
                    "mode": "neutral_review_each",
                    "exact_human_review_required": True,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        reply_voice_profile_id = "neutral_review_each_" + neutral_hash[:20]
        reply_voice_profile_hash = neutral_hash
    _time(checked_at, "checked_at")
    reply_hash = sha256(reply.encode("utf-8")).hexdigest()
    base = ServiceReplyPlan(
        schema_version=1,
        plan_id="pending",
        account_id=item.account_id,
        item_id=item.item_id,
        channel=item.channel,
        target_context_hash=item.target_context_hash,
        reply_text=reply,
        reply_hash=reply_hash,
        reply_voice_profile_id=reply_voice_profile_id,
        reply_voice_profile_hash=reply_voice_profile_hash,
        fact_refs=(),
        checked_at=checked_at,
        content_hash="0" * 64,
    )
    digest = sha256(
        json.dumps(base._hash_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ServiceReplyPlan(**{
        **base.__dict__,
        "plan_id": "service_plan_" + digest[:20],
        "content_hash": digest,
    })


@dataclass(frozen=True)
class ServiceReplyApproval:
    schema_version: int
    approval_id: str
    plan_id: str
    plan_hash: str
    account_id: str
    item_id: str
    approved_at: str
    approval_hash: str

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ServiceReplyApproval":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ServiceContractError("service approval fields are incomplete or unknown")
        result = cls(**dict(value))
        if result.schema_version != 1:
            raise ServiceContractError("unsupported service approval schema")
        for field in ("approval_id", "plan_id", "account_id", "item_id"):
            _safe_id(getattr(result, field), field)
        _hash(result.plan_hash, "plan_hash")
        _hash(result.approval_hash, "approval_hash")
        _time(result.approved_at, "approved_at")
        payload = result.to_dict()
        payload.pop("approval_hash")
        expected = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if result.approval_hash != expected:
            raise ServiceContractError("service approval integrity failed")
        return result


def _build_approval(plan: ServiceReplyPlan, *, approved_at: str) -> ServiceReplyApproval:
    base = {
        "schema_version": 1,
        "approval_id": "service_approval_" + plan.content_hash[:20],
        "plan_id": plan.plan_id,
        "plan_hash": plan.content_hash,
        "account_id": plan.account_id,
        "item_id": plan.item_id,
        "approved_at": _time(approved_at, "approved_at"),
    }
    digest = sha256(
        json.dumps(base, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ServiceReplyApproval(**base, approval_hash=digest)


def _preflight_request(
    plan: ServiceReplyPlan,
    item: ServiceInboxItem,
    approval: ServiceReplyApproval,
    *,
    checked_at: str,
    daily_limit: int,
    minimum_interval_seconds: int,
    budget_timezone: str,
) -> UnifiedActionRequest:
    return UnifiedActionRequest(
        schema_version=1,
        action_id=plan.plan_id,
        action_kind=("service_comment_reply" if plan.channel == "comments" else "service_dm_reply"),
        account_id=plan.account_id,
        target_ref_hash=sha256(item.item_id.encode("utf-8")).hexdigest(),
        dedupe_key_hash=item.dedupe_key_hash,
        plan_hash=plan.content_hash,
        approval_ref=approval.approval_id,
        approval_hash=approval.approval_hash,
        checked_at=checked_at,
        budget_timezone=budget_timezone,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        verification_method=(
            "exact_visible_reply_increase"
            if plan.channel == "comments"
            else "exact_visible_outgoing_message_increase"
        ),
    )


def _capability_ready(client: RunAgentClient, plan: ServiceReplyPlan) -> bool:
    operation = "reply_current_comment" if plan.channel == "comments" else "send_current_dm_message"
    return any(
        row.get("operation") == operation
        for row in client.capability_audit().get("allowed", [])
    )


def approve_service_reply(
    *,
    project_root: Path,
    runtime_dir: Path,
    plan: ServiceReplyPlan,
    approved_at: str,
    confirmation: str,
    state: UnifiedPreflightState,
    client: RunAgentClient | None = None,
    daily_limit: int = 10,
    minimum_interval_seconds: int = 600,
    budget_timezone: str = "UTC",
) -> tuple[ServiceReplyApproval, UnifiedPreflightDecision, dict[str, Any]]:
    if confirmation != SERVICE_REPLY_CONFIRMATION:
        raise ServiceContractError("exact single service reply approval is required")
    store = ServiceQueueStore(runtime_dir)
    item = store.load_item(plan.item_id)
    if store.load_plan(plan.plan_id) != plan:
        raise ServiceContractError("service approval requires the persisted exact plan")
    if item.state != "opened" or item.target_context_hash != plan.target_context_hash:
        raise ServiceContractError("service item changed before approval")
    if store.receipt_path(plan.plan_id).exists():
        raise ServiceContractError("service reply was already executed")
    approval = _build_approval(plan, approved_at=approved_at)
    run_agent = client or RunAgentClient(project_root)
    live_identity = run_agent.assert_current_account_identity()
    state = replace(
        state,
        account_identity_ready=(
            state.account_identity_ready and live_identity.get("verified") is True
        ),
        capability_ready=state.capability_ready and _capability_ready(run_agent, plan),
        runtime_mode=RuntimeMode.SCOPED_UAT,
        scoped_uat_authorized=True,
        scoped_uat_actions_remaining=1,
    )
    request = _preflight_request(
        plan,
        item,
        approval,
        checked_at=approved_at,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        budget_timezone=budget_timezone,
    )
    try:
        decision = UnifiedActionPreflightStore(runtime_dir).evaluate(
            request,
            phase="authorize",
            state=state,
        )
    except ActionPreflightError as exc:
        raise ServiceContractError(str(exc)) from exc
    if not decision.allowed:
        raise ServiceContractError("service preflight blocked: " + "; ".join(decision.blockers))
    branch = "service_comment_reply" if plan.channel == "comments" else "service_dm_reply"
    lease = run_agent.authorize_bounded_write_uat(
        confirmation=BOUNDED_WRITE_UAT_CONFIRMATION,
        account_id=plan.account_id,
        session_id=plan.plan_id,
        note_id=item.item_id,
        plan_hash=plan.content_hash,
        branch=branch,
        max_actions=1,
    )
    try:
        store.save_approval(approval)
    except BaseException:
        run_agent.revoke_bounded_write_uat()
        raise
    return approval, decision, lease


def execute_service_reply(
    *,
    project_root: Path,
    runtime_dir: Path,
    plan: ServiceReplyPlan,
    executed_at: str,
    state: UnifiedPreflightState,
    client: RunAgentClient | None = None,
    daily_limit: int = 10,
    minimum_interval_seconds: int = 600,
    budget_timezone: str = "UTC",
) -> tuple[dict[str, Any], Path, UnifiedPreflightDecision]:
    store = ServiceQueueStore(runtime_dir)
    item = store.load_item(plan.item_id)
    if store.load_plan(plan.plan_id) != plan:
        raise ServiceContractError("service execution requires the persisted exact plan")
    approval = store.load_approval(plan)
    if item.state != "opened" or item.target_context_hash != plan.target_context_hash:
        raise ServiceContractError("service item changed before execution")
    if store.receipt_path(plan.plan_id).exists():
        raise ServiceContractError("service reply plan already has a terminal receipt")
    run_agent = client or RunAgentClient(project_root)
    live_identity = run_agent.assert_current_account_identity()
    branch = "service_comment_reply" if plan.channel == "comments" else "service_dm_reply"
    run_agent.require_bounded_write_uat(
        session_id=plan.plan_id,
        note_id=item.item_id,
        plan_hash=plan.content_hash,
        branch=branch,
    )
    state = replace(
        state,
        account_identity_ready=(
            state.account_identity_ready and live_identity.get("verified") is True
        ),
        capability_ready=state.capability_ready and _capability_ready(run_agent, plan),
        exact_lease_ready=True,
        runtime_mode=RuntimeMode.SCOPED_UAT,
        scoped_uat_authorized=True,
        scoped_uat_actions_remaining=1,
    )
    request = _preflight_request(
        plan,
        item,
        approval,
        checked_at=executed_at,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        budget_timezone=budget_timezone,
    )
    preflight = UnifiedActionPreflightStore(runtime_dir)
    try:
        decision = preflight.evaluate(request, phase="execute", state=state)
    except ActionPreflightError as exc:
        raise ServiceContractError(str(exc)) from exc
    if not decision.allowed:
        raise ServiceContractError("service execution preflight blocked: " + "; ".join(decision.blockers))
    result: dict[str, Any]
    try:
        if plan.channel == "comments":
            result = run_agent.reply_current_comment_bound(
                item.note_id,
                plan.reply_text,
                comment_id=item.source_comment_id,
                target_context_hash=item.target_context_hash,
            )
        else:
            snapshot, _evidence = run_agent.capture_current_dm_snapshot(
                account_id=item.account_id,
                conversation_id=item.conversation_id,
                expected_peer_ref_hash=item.peer_ref_hash,
                captured_at=executed_at,
                max_messages=50,
            )
            if _dm_context_hash(snapshot.to_dict()) != item.target_context_hash:
                raise ServiceContractError("service DM conversation changed before execution")
            result = run_agent.send_current_dm_message(
                expected_peer_ref_hash=item.peer_ref_hash,
                content=plan.reply_text,
            )
        if result.get("actionDispatched") is False:
            preflight.record_result(
                request,
                status="not_dispatched",
                recorded_at=executed_at,
                reason_code=str(result.get("failureCode") or "platform_action_not_dispatched"),
            )
            raise ServiceContractError("service write was not dispatched")
        if result.get("verified") is not True or result.get("platform_actions_executed") != 1:
            preflight.record_result(
                request,
                status="unknown",
                recorded_at=executed_at,
                reason_code="service_visible_verification_missing",
            )
            raise RunAgentError("service write lacked exact visible verification")
        if result.get("contentHash") != plan.reply_hash:
            preflight.record_result(
                request,
                status="unknown",
                recorded_at=executed_at,
                reason_code="service_reply_hash_mismatch",
            )
            raise RunAgentError("service write content hash mismatch")
        evidence_hash = sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        operation_receipt = preflight.record_result(
            request,
            status="verified",
            recorded_at=executed_at,
            evidence_hash=evidence_hash,
        )
        path = store.save_receipt(
            plan=plan,
            result=result,
            recorded_at=executed_at,
            operation_receipt=operation_receipt,
        )
        store.mark_replied(item)
        return result, path, decision
    except BaseException as exc:
        stop = read_json(Path(runtime_dir) / "comment_flow" / "STOP.json", default=None)
        if isinstance(stop, Mapping) and stop.get("requires_manual_reconciliation") is True:
            try:
                preflight.record_result(
                    request,
                    status="unknown",
                    recorded_at=executed_at,
                    reason_code="platform_write_unknown",
                )
            except ActionPreflightError:
                pass
        raise exc
    finally:
        run_agent.revoke_bounded_write_uat()
