"""Persist one confirmed V2 strategy without embedding industry logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .promotion import (
    PromotionIntent,
    PromotionStrategy,
    PromotionStrategyError,
    build_promotion_strategy,
)
from .storage import read_json, write_json_atomic


class StrategyPackError(ValueError):
    pass


STRATEGY_PACK_CONFIRMATION = "I_CONFIRM_STRATEGY_PACK"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _safe_id(value: object, field: str) -> str:
    result = str(value or "").strip()
    if _SAFE_ID.fullmatch(result) is None:
        raise StrategyPackError(f"{field} is invalid")
    return result


def _hash(value: object, field: str) -> str:
    result = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise StrategyPackError(f"{field} must be SHA-256 hex")
    return result


def _time(value: object, field: str) -> str:
    result = str(value or "")
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StrategyPackError(f"{field} must be timezone-aware ISO") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StrategyPackError(f"{field} must include timezone")
    return result


def _rebuild(value: Mapping[str, Any]) -> tuple[PromotionIntent, PromotionStrategy]:
    raw_intent = value.get("intent")
    raw_strategy = value.get("strategy")
    if not isinstance(raw_intent, Mapping) or not isinstance(raw_strategy, Mapping):
        raise StrategyPackError("strategy pack intent and strategy must be objects")
    intent_payload = dict(raw_intent)
    expected_intent_hash = intent_payload.pop("content_hash", None)
    try:
        intent = PromotionIntent.from_dict(intent_payload)
    except PromotionStrategyError as exc:
        raise StrategyPackError(str(exc)) from exc
    if intent.content_hash != expected_intent_hash:
        raise StrategyPackError("strategy pack intent integrity failed")
    topics = raw_strategy.get("topics")
    queries = raw_strategy.get("queries")
    if not isinstance(topics, list) or not isinstance(queries, list):
        raise StrategyPackError("strategy pack topics and queries must be lists")
    draft_queries: list[dict[str, Any]] = []
    for item in queries:
        if not isinstance(item, Mapping):
            raise StrategyPackError("strategy pack query is invalid")
        row = dict(item)
        row.pop("priority", None)
        draft_queries.append(row)
    draft = {
        "strategy_id": raw_strategy.get("strategy_id"),
        "checked_at": raw_strategy.get("checked_at"),
        "interaction_goal": raw_strategy.get("interaction_goal"),
        "topics": [dict(item) if isinstance(item, Mapping) else item for item in topics],
        "queries": draft_queries,
        "exclusions": list(raw_strategy.get("exclusions") or []),
    }
    try:
        strategy = build_promotion_strategy(intent=intent, draft=draft)
    except PromotionStrategyError as exc:
        raise StrategyPackError(str(exc)) from exc
    if strategy.to_dict() != dict(raw_strategy):
        raise StrategyPackError("strategy pack strategy integrity failed")
    return intent, strategy


@dataclass(frozen=True)
class StrategyPack:
    schema_version: int
    strategy_pack_id: str
    account_id: str
    intent: dict[str, Any]
    strategy: dict[str, Any]
    created_at: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise StrategyPackError("unsupported strategy pack schema")
        _safe_id(self.strategy_pack_id, "strategy_pack_id")
        _safe_id(self.account_id, "account_id")
        _time(self.created_at, "created_at")
        _hash(self.content_hash, "content_hash")
        intent, strategy = _rebuild({"intent": self.intent, "strategy": self.strategy})
        expected = sha256(json.dumps({
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "intent_hash": intent.content_hash,
            "strategy_hash": strategy.content_hash,
            "created_at": self.created_at,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if self.content_hash != expected:
            raise StrategyPackError("strategy pack content hash mismatch")
        if self.strategy_pack_id != "strategy_pack_" + expected[:20]:
            raise StrategyPackError("strategy pack ID does not match content")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy_pack_id": self.strategy_pack_id,
            "account_id": self.account_id,
            "intent": self.intent,
            "strategy": self.strategy,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StrategyPack":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise StrategyPackError("strategy pack fields are incomplete or unknown")
        return cls(**dict(value))

    def query(self, query_id: str) -> dict[str, Any]:
        query_id = _safe_id(query_id, "query_id")
        rows = [row for row in self.strategy["queries"] if row.get("query_id") == query_id]
        if len(rows) != 1:
            raise StrategyPackError("strategy pack query is missing or ambiguous")
        return dict(rows[0])


@dataclass(frozen=True)
class StrategyPackApproval:
    schema_version: int
    approval_id: str
    strategy_pack_id: str
    strategy_pack_hash: str
    account_id: str
    confirmed_at: str
    approval_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise StrategyPackError("unsupported strategy pack approval schema")
        for field in ("approval_id", "strategy_pack_id", "account_id"):
            _safe_id(getattr(self, field), field)
        _hash(self.strategy_pack_hash, "strategy_pack_hash")
        _hash(self.approval_hash, "approval_hash")
        _time(self.confirmed_at, "confirmed_at")
        payload = self.to_dict()
        payload.pop("approval_hash")
        expected = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if self.approval_hash != expected:
            raise StrategyPackError("strategy pack approval integrity failed")

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StrategyPackApproval":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise StrategyPackError("strategy pack approval fields are incomplete or unknown")
        return cls(**dict(value))


def build_strategy_pack(
    *,
    account_id: str,
    manifest: Mapping[str, Any],
) -> StrategyPack:
    account_id = _safe_id(account_id, "account_id")
    if not isinstance(manifest, Mapping) or set(manifest) != {"intent", "strategy_draft"}:
        raise StrategyPackError("strategy manifest requires intent and strategy_draft")
    if not isinstance(manifest["intent"], Mapping) or not isinstance(
        manifest["strategy_draft"], Mapping
    ):
        raise StrategyPackError("strategy manifest sections must be objects")
    try:
        intent = PromotionIntent.from_dict(manifest["intent"])
        strategy = build_promotion_strategy(intent=intent, draft=manifest["strategy_draft"])
    except PromotionStrategyError as exc:
        raise StrategyPackError(str(exc)) from exc
    created_at = strategy.checked_at
    content_hash = sha256(json.dumps({
        "schema_version": 1,
        "account_id": account_id,
        "intent_hash": intent.content_hash,
        "strategy_hash": strategy.content_hash,
        "created_at": created_at,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return StrategyPack(
        schema_version=1,
        strategy_pack_id="strategy_pack_" + content_hash[:20],
        account_id=account_id,
        intent=intent.to_dict(),
        strategy=strategy.to_dict(),
        created_at=created_at,
        content_hash=content_hash,
    )


class StrategyPackStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "strategy_packs"
        self.approvals_root = self.root / "approvals"

    def pack_path(self, strategy_pack_id: str) -> Path:
        return self.root / f"{_safe_id(strategy_pack_id, 'strategy_pack_id')}.json"

    def save(self, pack: StrategyPack) -> Path:
        payload = pack.to_dict()
        envelope_hash = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        path = self.pack_path(pack.strategy_pack_id)
        existing = read_json(path, default=None)
        envelope = {"schema_version": 1, "strategy_pack": payload, "envelope_hash": envelope_hash}
        if existing is not None and existing != envelope:
            raise StrategyPackError("a different strategy pack already uses this ID")
        write_json_atomic(path, envelope)
        return path

    def load(self, strategy_pack_id: str) -> StrategyPack:
        value = read_json(self.pack_path(strategy_pack_id), default=None)
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "strategy_pack", "envelope_hash"
        }:
            raise StrategyPackError("stored strategy pack envelope is invalid")
        if value["schema_version"] != 1 or not isinstance(value["strategy_pack"], dict):
            raise StrategyPackError("stored strategy pack envelope version is invalid")
        expected = sha256(json.dumps(
            value["strategy_pack"], ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()
        if value["envelope_hash"] != expected:
            raise StrategyPackError("stored strategy pack integrity failed")
        pack = StrategyPack.from_dict(value["strategy_pack"])
        if pack.strategy_pack_id != strategy_pack_id:
            raise StrategyPackError("stored strategy pack identity mismatch")
        return pack

    def approval_path(self, strategy_pack_id: str) -> Path:
        return self.approvals_root / f"{_safe_id(strategy_pack_id, 'strategy_pack_id')}.json"

    def confirm(
        self,
        pack: StrategyPack,
        *,
        confirmed_at: str,
        confirmation: str,
    ) -> StrategyPackApproval:
        if confirmation != STRATEGY_PACK_CONFIRMATION:
            raise StrategyPackError("exact strategy pack confirmation is required")
        if self.load(pack.strategy_pack_id) != pack:
            raise StrategyPackError("strategy confirmation requires the persisted exact pack")
        base = {
            "schema_version": 1,
            "approval_id": "strategy_approval_" + pack.content_hash[:20],
            "strategy_pack_id": pack.strategy_pack_id,
            "strategy_pack_hash": pack.content_hash,
            "account_id": pack.account_id,
            "confirmed_at": _time(confirmed_at, "confirmed_at"),
        }
        approval = StrategyPackApproval(
            **base,
            approval_hash=sha256(
                json.dumps(base, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        )
        path = self.approval_path(pack.strategy_pack_id)
        existing = read_json(path, default=None)
        if existing is not None and existing != approval.to_dict():
            raise StrategyPackError("a different strategy approval already exists")
        write_json_atomic(path, approval.to_dict())
        return approval

    def load_approval(self, pack: StrategyPack) -> StrategyPackApproval:
        value = read_json(self.approval_path(pack.strategy_pack_id), default=None)
        if not isinstance(value, dict):
            raise StrategyPackError("strategy pack confirmation is missing")
        approval = StrategyPackApproval.from_dict(value)
        if (
            approval.strategy_pack_id != pack.strategy_pack_id
            or approval.strategy_pack_hash != pack.content_hash
            or approval.account_id != pack.account_id
        ):
            raise StrategyPackError("strategy pack confirmation does not match exact pack")
        return approval
