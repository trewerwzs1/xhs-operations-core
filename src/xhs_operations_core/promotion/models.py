"""Unified promotion-intent inputs and Codex-authored strategy contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any, Mapping
import json
import re


class PromotionStrategyError(ValueError):
    pass


class PromotionInputMode(str, Enum):
    ACCOUNT_NOTE = "account_note"
    SPECIFIED_NOTE = "specified_note"
    DIRECT_BRIEF = "direct_brief"


class TopicLayer(str, Enum):
    CORE = "core"
    ADJACENT = "adjacent"
    SCENE = "scene"
    BROAD = "broad"


LAYER_PRIORITY = {
    TopicLayer.CORE: "P0",
    TopicLayer.ADJACENT: "P1",
    TopicLayer.SCENE: "P2",
    TopicLayer.BROAD: "P3",
}


def _text(name: str, value: object, *, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise PromotionStrategyError(f"{name} must be text")
    normalized = " ".join(value.split()).strip()
    if not normalized and not optional:
        raise PromotionStrategyError(f"{name} must be non-empty")
    return normalized


def _safe_id(name: str, value: object) -> str:
    normalized = _text(name, value)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", normalized) is None:
        raise PromotionStrategyError(f"{name} must be a safe id")
    return normalized


def _timestamp(name: str, value: object) -> str:
    normalized = _text(name, value)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromotionStrategyError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PromotionStrategyError(f"{name} must include timezone")
    return normalized


def _strings(name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PromotionStrategyError(f"{name} must be a list")
    rows = tuple(_text(f"{name} item", item) for item in value)
    if len(rows) != len(set(rows)):
        raise PromotionStrategyError(f"{name} cannot contain duplicates")
    return rows


@dataclass(frozen=True)
class PromotionIntent:
    mode: PromotionInputMode
    source_id: str
    source_ref: str
    title: str
    body: str
    brief: str
    user_keywords: tuple[str, ...]
    exclusions: tuple[str, ...]
    created_at: str
    content_hash: str

    @property
    def source_text(self) -> str:
        return " ".join(item for item in (self.title, self.body, self.brief) if item).strip()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PromotionIntent":
        allowed = {"mode", "source_id", "source_ref", "title", "body", "brief", "user_keywords", "exclusions", "created_at"}
        if set(value) != allowed:
            raise PromotionStrategyError("promotion intent fields are incomplete or unknown")
        try:
            mode = PromotionInputMode(value["mode"])
        except (TypeError, ValueError) as exc:
            raise PromotionStrategyError("invalid promotion input mode") from exc
        source_id = _safe_id("source_id", value["source_id"])
        source_ref = _text("source_ref", value["source_ref"])
        title = _text("title", value["title"], optional=True)
        body = _text("body", value["body"], optional=True)
        brief = _text("brief", value["brief"], optional=True)
        if mode in {PromotionInputMode.ACCOUNT_NOTE, PromotionInputMode.SPECIFIED_NOTE} and not (title or body):
            raise PromotionStrategyError("note input requires title or body")
        if mode is PromotionInputMode.DIRECT_BRIEF and not brief:
            raise PromotionStrategyError("direct_brief requires brief")
        user_keywords = _strings("user_keywords", value["user_keywords"])
        exclusions = _strings("exclusions", value["exclusions"])
        created_at = _timestamp("created_at", value["created_at"])
        payload = {
            "mode": mode.value, "source_id": source_id, "source_ref": source_ref,
            "title": title, "body": body, "brief": brief,
            "user_keywords": list(user_keywords), "exclusions": list(exclusions),
            "created_at": created_at,
        }
        content_hash = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return cls(mode, source_id, source_ref, title, body, brief, user_keywords, exclusions, created_at, content_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value, "source_id": self.source_id, "source_ref": self.source_ref,
            "title": self.title, "body": self.body, "brief": self.brief,
            "user_keywords": list(self.user_keywords), "exclusions": list(self.exclusions),
            "created_at": self.created_at, "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class StrategyTopic:
    topic_id: str
    label: str
    layer: TopicLayer
    evidence_quote: str
    parent_topic_ids: tuple[str, ...]
    relationship: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, intent: PromotionIntent) -> "StrategyTopic":
        allowed = {"topic_id", "label", "layer", "evidence_quote", "parent_topic_ids", "relationship"}
        if set(value) != allowed:
            raise PromotionStrategyError("strategy topic fields are incomplete or unknown")
        topic_id = _safe_id("topic_id", value["topic_id"])
        label = _text("topic label", value["label"])
        try:
            layer = TopicLayer(value["layer"])
        except (TypeError, ValueError) as exc:
            raise PromotionStrategyError("invalid topic layer") from exc
        evidence = _text("evidence_quote", value["evidence_quote"], optional=True)
        parents = _strings("parent_topic_ids", value["parent_topic_ids"])
        relationship = _text("relationship", value["relationship"], optional=True)
        if layer is TopicLayer.CORE:
            if not evidence or evidence not in intent.source_text:
                raise PromotionStrategyError("core topic requires an exact source evidence quote")
            if parents:
                raise PromotionStrategyError("core topic cannot have parent topics")
        else:
            if not parents or not relationship:
                raise PromotionStrategyError("expanded topic requires parents and relationship")
        return cls(topic_id, label, layer, evidence, parents, relationship)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id, "label": self.label, "layer": self.layer.value,
            "evidence_quote": self.evidence_quote,
            "parent_topic_ids": list(self.parent_topic_ids), "relationship": self.relationship,
        }


@dataclass(frozen=True)
class StrategyQuery:
    query_id: str
    query: str
    layer: TopicLayer
    topic_ids: tuple[str, ...]
    rationale: str
    max_notes_to_open: int
    daily_share_cap: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StrategyQuery":
        allowed = {"query_id", "query", "layer", "topic_ids", "rationale", "max_notes_to_open", "daily_share_cap"}
        if set(value) != allowed:
            raise PromotionStrategyError("strategy query fields are incomplete or unknown")
        try:
            layer = TopicLayer(value["layer"])
        except (TypeError, ValueError) as exc:
            raise PromotionStrategyError("invalid query layer") from exc
        limit = value["max_notes_to_open"]
        cap = value["daily_share_cap"]
        if type(limit) is not int or not 1 <= limit <= 10:
            raise PromotionStrategyError("max_notes_to_open must be 1-10")
        if type(cap) not in {int, float} or not 0 < float(cap) <= 1:
            raise PromotionStrategyError("daily_share_cap must be within (0, 1]")
        if layer is TopicLayer.BROAD and float(cap) > 0.25:
            raise PromotionStrategyError("broad query daily_share_cap cannot exceed 0.25")
        return cls(
            _safe_id("query_id", value["query_id"]), _text("query", value["query"]), layer,
            _strings("topic_ids", value["topic_ids"]), _text("query rationale", value["rationale"]),
            limit, float(cap),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id, "query": self.query, "layer": self.layer.value,
            "priority": LAYER_PRIORITY[self.layer], "topic_ids": list(self.topic_ids),
            "rationale": self.rationale, "max_notes_to_open": self.max_notes_to_open,
            "daily_share_cap": self.daily_share_cap,
        }


@dataclass(frozen=True)
class PromotionStrategy:
    strategy_id: str
    source_mode: PromotionInputMode
    source_ref: str
    source_hash: str
    checked_at: str
    interaction_goal: str
    topics: tuple[StrategyTopic, ...]
    queries: tuple[StrategyQuery, ...]
    exclusions: tuple[str, ...]
    processing_mode: str
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id, "source_mode": self.source_mode.value,
            "source_ref": self.source_ref, "source_hash": self.source_hash,
            "checked_at": self.checked_at, "interaction_goal": self.interaction_goal,
            "topics": [item.to_dict() for item in self.topics],
            "queries": [item.to_dict() for item in self.queries],
            "exclusions": list(self.exclusions), "processing_mode": self.processing_mode,
            "content_hash": self.content_hash,
        }


def build_promotion_strategy(*, intent: PromotionIntent, draft: Mapping[str, Any]) -> PromotionStrategy:
    allowed = {"strategy_id", "checked_at", "interaction_goal", "topics", "queries", "exclusions"}
    if set(draft) != allowed:
        raise PromotionStrategyError("promotion strategy draft fields are incomplete or unknown")
    raw_topics, raw_queries = draft["topics"], draft["queries"]
    if not isinstance(raw_topics, list) or not isinstance(raw_queries, list):
        raise PromotionStrategyError("topics and queries must be lists")
    topics = tuple(StrategyTopic.from_dict(item, intent=intent) for item in raw_topics)
    if not topics or not any(item.layer is TopicLayer.CORE for item in topics):
        raise PromotionStrategyError("strategy requires at least one core topic")
    topic_map = {item.topic_id: item for item in topics}
    if len(topic_map) != len(topics):
        raise PromotionStrategyError("topic ids must be unique")
    for topic in topics:
        if any(parent not in topic_map for parent in topic.parent_topic_ids):
            raise PromotionStrategyError("topic parent is missing")
        if any(topic_map[parent].layer is TopicLayer.BROAD for parent in topic.parent_topic_ids):
            raise PromotionStrategyError("broad topic cannot be an expansion parent")
    queries = tuple(StrategyQuery.from_dict(item) for item in raw_queries)
    if not queries:
        raise PromotionStrategyError("strategy requires queries")
    if len({item.query_id for item in queries}) != len(queries) or len({item.query for item in queries}) != len(queries):
        raise PromotionStrategyError("query ids and query text must be unique")
    for query in queries:
        if not query.topic_ids or any(topic_id not in topic_map for topic_id in query.topic_ids):
            raise PromotionStrategyError("query topic reference is missing")
        if not any(topic_map[topic_id].layer is query.layer for topic_id in query.topic_ids):
            raise PromotionStrategyError("query layer must match at least one referenced topic")
    exclusions = tuple(dict.fromkeys((*intent.exclusions, *_strings("exclusions", draft["exclusions"]))))
    payload = {
        "strategy_id": _safe_id("strategy_id", draft["strategy_id"]),
        "source_mode": intent.mode.value, "source_ref": intent.source_ref,
        "source_hash": intent.content_hash, "checked_at": _timestamp("checked_at", draft["checked_at"]),
        "interaction_goal": _text("interaction_goal", draft["interaction_goal"]),
        "topics": [item.to_dict() for item in topics], "queries": [item.to_dict() for item in queries],
        "exclusions": list(exclusions), "processing_mode": "one_candidate_at_a_time_same_search_batch",
    }
    content_hash = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return PromotionStrategy(
        payload["strategy_id"], intent.mode, intent.source_ref, intent.content_hash,
        payload["checked_at"], payload["interaction_goal"], topics, queries,
        exclusions, payload["processing_mode"], content_hash,
    )
