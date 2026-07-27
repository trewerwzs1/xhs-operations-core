"""Compile a generic PromotionStrategy into an explicit DiscoveryPlan."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from xhs_operations_core.campaign import Campaign, FactKind
from xhs_operations_core.promotion import PromotionStrategy, TopicLayer


class DiscoveryPlanError(ValueError):
    pass


def _clean(value: str) -> str:
    return " ".join(value.split()).strip()


@dataclass(frozen=True)
class AudienceSegment:
    segment_id: str
    label: str
    rationale: str
    location_mode: str
    interest_signals: tuple[str, ...]
    intent_signals: tuple[str, ...]
    exclusion_signals: tuple[str, ...]
    confidence_level: str
    source_ref: str

    def to_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "label": self.label,
            "rationale": self.rationale,
            "location_mode": self.location_mode,
            "interest_signals": list(self.interest_signals),
            "intent_signals": list(self.intent_signals),
            "exclusion_signals": list(self.exclusion_signals),
            "confidence_level": self.confidence_level,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class AudienceProfile:
    objective: str
    segments: tuple[AudienceSegment, ...]
    global_exclusions: tuple[str, ...]
    evidence_rule: str

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "segments": [item.to_dict() for item in self.segments],
            "global_exclusions": list(self.global_exclusions),
            "evidence_rule": self.evidence_rule,
        }


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    query: str
    priority: str
    segment_id: str
    rationale: str
    expected_signals: tuple[str, ...]
    exclusion_signals: tuple[str, ...]
    max_notes_to_open: int = 3
    strategy_layer: str = "policy"
    daily_share_cap: float = 1.0

    def to_dict(self) -> dict[str, object]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "priority": self.priority,
            "segment_id": self.segment_id,
            "rationale": self.rationale,
            "expected_signals": list(self.expected_signals),
            "exclusion_signals": list(self.exclusion_signals),
            "max_notes_to_open": self.max_notes_to_open,
            "strategy_layer": self.strategy_layer,
            "daily_share_cap": self.daily_share_cap,
        }


@dataclass(frozen=True)
class DiscoveryPlan:
    schema_version: int
    campaign_id: str
    source_note_hash: str
    source_note_ref: str
    fact_refs: tuple[str, ...]
    activity_classification_confidence: float
    city: str
    audience_profile: AudienceProfile
    queries: tuple[QuerySpec, ...]
    max_queries_per_run: int
    processing_mode: str
    strategy_id: str | None
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "source_note_hash": self.source_note_hash,
            "source_note_ref": self.source_note_ref,
            "fact_refs": list(self.fact_refs),
            "activity_classification_confidence": self.activity_classification_confidence,
            "city": self.city,
            "audience_profile": self.audience_profile.to_dict(),
            "queries": [item.to_dict() for item in self.queries],
            "max_queries_per_run": self.max_queries_per_run,
            "processing_mode": self.processing_mode,
            "strategy_id": self.strategy_id,
            "content_hash": self.content_hash,
        }


def _optional_public_city(campaign: Campaign, checked_at: str) -> tuple[str, str] | None:
    matches = [
        fact for fact in campaign.facts
        if fact.kind is FactKind.CITY and fact.approved_for_public and fact.is_valid_at(checked_at)
    ]
    if len(matches) > 1:
        raise DiscoveryPlanError("discovery cannot use multiple public city facts")
    if not matches:
        return None
    return _clean(str(matches[0].value)), matches[0].fact_id


def build_discovery_plan(
    campaign: Campaign,
    *,
    checked_at: str,
    promotion_strategy: PromotionStrategy | None = None,
) -> DiscoveryPlan:
    if promotion_strategy is None:
        raise DiscoveryPlanError(
            "generic discovery requires an explicit PromotionStrategy from StrategyPack"
        )
    if campaign.source_note_hash != promotion_strategy.source_hash:
        raise DiscoveryPlanError("promotion strategy source hash does not match Campaign")
    if campaign.source_note_ref != promotion_strategy.source_ref:
        raise DiscoveryPlanError("promotion strategy source ref does not match Campaign")
    city_value = _optional_public_city(campaign, checked_at)
    city = "" if city_value is None else city_value[0]
    fact_refs = () if city_value is None else (city_value[1],)
    layer_labels = {
        TopicLayer.CORE: "核心主题人群",
        TopicLayer.ADJACENT: "相邻兴趣人群",
        TopicLayer.SCENE: "场景相关人群",
        TopicLayer.BROAD: "泛相关兴趣人群",
    }
    segments = tuple(
        AudienceSegment(
            segment_id=f"strategy_{layer.value}",
            label=layer_labels[layer],
            rationale=f"由 {layer.value} 主题关系生成；搜索命中只证明相关兴趣。",
            location_mode="strategy_defined",
            interest_signals=tuple(
                item.label for item in promotion_strategy.topics if item.layer is layer
            ),
            intent_signals=("自然互动机会",),
            exclusion_signals=promotion_strategy.exclusions,
            confidence_level="strategy_hypothesis",
            source_ref=f"promotion_strategy:{promotion_strategy.strategy_id}:{layer.value}",
        )
        for layer in TopicLayer
        if any(item.layer is layer for item in promotion_strategy.topics)
    )
    audience = AudienceProfile(
        objective=promotion_strategy.interaction_goal,
        segments=segments,
        global_exclusions=promotion_strategy.exclusions,
        evidence_rule=(
            "related post evidence is sufficient for adjacent-interest engagement; "
            "explicit intent is required only for the comment-intent lane"
        ),
    )
    queries = tuple(
        QuerySpec(
            query_id=item.query_id,
            query=item.query,
            priority={
                TopicLayer.CORE: "P0",
                TopicLayer.ADJACENT: "P1",
                TopicLayer.SCENE: "P2",
                TopicLayer.BROAD: "P3",
            }[item.layer],
            segment_id=f"strategy_{item.layer.value}",
            rationale=item.rationale,
            expected_signals=tuple(
                topic.label
                for topic in promotion_strategy.topics
                if topic.topic_id in item.topic_ids
            ),
            exclusion_signals=promotion_strategy.exclusions,
            max_notes_to_open=item.max_notes_to_open,
            strategy_layer=item.layer.value,
            daily_share_cap=item.daily_share_cap,
        )
        for item in promotion_strategy.queries[:6]
    )
    payload = {
        "campaign_id": campaign.campaign_id,
        "source_note_hash": campaign.source_note_hash,
        "source_note_ref": campaign.source_note_ref,
        "fact_refs": list(fact_refs),
        "activity_classification_confidence": campaign.classification_confidence,
        "city": city,
        "audience_profile": audience.to_dict(),
        "queries": [item.to_dict() for item in queries],
        "max_queries_per_run": len(queries),
        "processing_mode": "one_candidate_at_a_time_same_search_batch",
        "strategy_id": promotion_strategy.strategy_id,
    }
    content_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return DiscoveryPlan(
        schema_version=1,
        campaign_id=campaign.campaign_id,
        source_note_hash=campaign.source_note_hash,
        source_note_ref=campaign.source_note_ref,
        fact_refs=fact_refs,
        activity_classification_confidence=campaign.classification_confidence,
        city=city,
        audience_profile=audience,
        queries=queries,
        max_queries_per_run=len(queries),
        processing_mode="one_candidate_at_a_time_same_search_batch",
        strategy_id=promotion_strategy.strategy_id,
        content_hash=content_hash,
    )
