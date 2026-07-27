"""Compile query, queue, and verified-action evidence into a daily review."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping, Sequence
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xhs_operations_core.contracts import ActionRecord, ActionStatus, ActionType
from xhs_operations_core.orchestration import DailyPlan


class ReviewError(ValueError):
    pass


@dataclass(frozen=True)
class QueryRunMetrics:
    run_id: str
    query_id: str
    searched_notes: int
    opened_notes: int
    visible_comments: int
    candidates_a: int
    candidates_b: int
    candidates_c: int
    candidates_x: int
    messages_valid: int
    messages_blocked: int
    human_approved: int
    exhausted: bool
    stop_reasons: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QueryRunMetrics":
        allowed = {
            "run_id", "query_id", "searched_notes", "opened_notes", "visible_comments",
            "candidates_a", "candidates_b", "candidates_c", "candidates_x",
            "messages_valid", "messages_blocked", "human_approved", "exhausted",
            "stop_reasons",
        }
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise ReviewError("query run metric fields are incomplete or unknown")
        if not all(isinstance(value[name], str) and value[name].strip() for name in ("run_id", "query_id")):
            raise ReviewError("query run ids are required")
        count_fields = allowed - {"run_id", "query_id", "exhausted", "stop_reasons"}
        if any(type(value[name]) is not int or value[name] < 0 for name in count_fields):
            raise ReviewError("query run counts must be non-negative integers")
        if type(value["exhausted"]) is not bool or not isinstance(value["stop_reasons"], list):
            raise ReviewError("query run exhausted or stop_reasons type is invalid")
        if any(
            not isinstance(item, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_:.-]{0,127}", item) is None
            for item in value["stop_reasons"]
        ):
            raise ReviewError("query stop reasons must be safe structured codes")
        result = cls(**{**dict(value), "stop_reasons": tuple(value["stop_reasons"])})
        if result.opened_notes > result.searched_notes:
            raise ReviewError("opened_notes cannot exceed searched_notes")
        candidates = result.candidates_a + result.candidates_b + result.candidates_c + result.candidates_x
        if candidates > result.visible_comments:
            raise ReviewError("candidate counts cannot exceed visible comments")
        if result.messages_valid + result.messages_blocked > result.candidates_a + result.candidates_b:
            raise ReviewError("message counts cannot exceed actionable candidates")
        if result.human_approved > result.messages_valid:
            raise ReviewError("human approvals cannot exceed valid messages")
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "query_id": self.query_id,
            "searched_notes": self.searched_notes,
            "opened_notes": self.opened_notes,
            "visible_comments": self.visible_comments,
            "candidates_a": self.candidates_a,
            "candidates_b": self.candidates_b,
            "candidates_c": self.candidates_c,
            "candidates_x": self.candidates_x,
            "messages_valid": self.messages_valid,
            "messages_blocked": self.messages_blocked,
            "human_approved": self.human_approved,
            "exhausted": self.exhausted,
            "stop_reasons": list(self.stop_reasons),
        }


@dataclass(frozen=True)
class ReviewRecommendation:
    recommendation_id: str
    action: str
    rationale: str
    risk_level: str
    auto_applicable: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "recommendation_id": self.recommendation_id,
            "action": self.action,
            "rationale": self.rationale,
            "risk_level": self.risk_level,
            "auto_applicable": self.auto_applicable,
        }


@dataclass(frozen=True)
class DailyReview:
    review_id: str
    plan_id: str
    campaign_id: str
    account_id: str
    plan_date: str
    checked_at: str
    funnel: dict[str, int]
    lead_funnel: dict[str, int]
    queue_status_counts: dict[str, int]
    account_write_status_counts: dict[str, int]
    account_write_kind_counts: dict[str, int]
    service_queue_status_counts: dict[str, int]
    blocker_counts: dict[str, int]
    verified_action_record_ids: tuple[str, ...]
    recommendations: tuple[ReviewRecommendation, ...]
    guarded_changes: tuple[str, ...]
    data_quality_flags: tuple[str, ...]
    review_status: str
    platform_actions_executed: int
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "review_id": self.review_id,
            "plan_id": self.plan_id,
            "campaign_id": self.campaign_id,
            "account_id": self.account_id,
            "plan_date": self.plan_date,
            "checked_at": self.checked_at,
            "funnel": dict(self.funnel),
            "lead_funnel": dict(self.lead_funnel),
            "queue_status_counts": dict(self.queue_status_counts),
            "account_write_status_counts": dict(self.account_write_status_counts),
            "account_write_kind_counts": dict(self.account_write_kind_counts),
            "service_queue_status_counts": dict(self.service_queue_status_counts),
            "blocker_counts": dict(self.blocker_counts),
            "verified_action_record_ids": list(self.verified_action_record_ids),
            "recommendations": [item.to_dict() for item in self.recommendations],
            "guarded_changes": list(self.guarded_changes),
            "data_quality_flags": list(self.data_quality_flags),
            "review_status": self.review_status,
            "platform_actions_executed": self.platform_actions_executed,
            "content_hash": self.content_hash,
        }


def _recommendation(action: str, rationale: str, *, risk: str = "low", auto: bool = True) -> ReviewRecommendation:
    digest = hashlib.sha256(f"{action}|{rationale}|{risk}|{auto}".encode()).hexdigest()[:12]
    return ReviewRecommendation(f"review_rec_{digest}", action, rationale, risk, auto)


def build_daily_review(
    *,
    plan: DailyPlan,
    heartbeat_state: Mapping[str, Any],
    query_runs: Sequence[QueryRunMetrics],
    action_records: Sequence[ActionRecord],
    checked_at: str,
    lead_summary: Mapping[str, int] | None = None,
    timezone_name: str = "UTC",
    unified_action_results: Sequence[Mapping[str, Any]] = (),
    service_queue_summary: Mapping[str, Any] | None = None,
) -> DailyReview:
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewError("checked_at must be ISO-8601") from exc
    if checked.tzinfo is None or checked.utcoffset() is None:
        raise ReviewError("checked_at must include a timezone")
    try:
        review_zone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ReviewError("timezone_name is not recognized") from exc
    if heartbeat_state.get("plan_id") != plan.plan_id or not isinstance(heartbeat_state.get("items"), Mapping):
        raise ReviewError("heartbeat state does not belong to DailyPlan")
    normalized_leads = dict(lead_summary or {})
    if any(not isinstance(key, str) or type(value) is not int or value < 0 for key, value in normalized_leads.items()):
        raise ReviewError("lead_summary must contain non-negative integer counts")
    run_ids = [item.run_id for item in query_runs]
    if len(set(run_ids)) != len(run_ids):
        raise ReviewError("query run_id values must be unique")
    planned_queries = {item.query_id for item in plan.search_slots}
    if any(item.query_id not in planned_queries for item in query_runs):
        raise ReviewError("query run is not in DailyPlan search slots")
    queue_counts: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    for raw in heartbeat_state["items"].values():
        if not isinstance(raw, Mapping) or not isinstance(raw.get("status"), str):
            raise ReviewError("heartbeat queue state is corrupt")
        queue_counts[str(raw["status"])] += 1
        raw_blockers = raw.get("last_blockers", [])
        if not isinstance(raw_blockers, list):
            raise ReviewError("heartbeat blockers are corrupt")
        blockers.update(str(item) for item in raw_blockers if str(item))

    account_write_statuses: Counter[str] = Counter()
    account_write_kinds: Counter[str] = Counter()
    for row in unified_action_results:
        if not isinstance(row, Mapping):
            raise ReviewError("unified action result is corrupt")
        if row.get("account_id") != plan.account_id:
            continue
        status = str(row.get("status") or "")
        action_kind = str(row.get("action_kind") or "")
        if status not in {"verified", "not_dispatched", "unknown"}:
            raise ReviewError("unified action result status is invalid")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", action_kind) is None:
            raise ReviewError("unified action kind is invalid")
        try:
            recorded = datetime.fromisoformat(
                str(row.get("recorded_at") or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ReviewError("unified action result timestamp is invalid") from exc
        if recorded.tzinfo is None or recorded.utcoffset() is None:
            raise ReviewError("unified action result timestamp must include timezone")
        if recorded.astimezone(review_zone).date().isoformat() != plan.plan_date:
            continue
        account_write_statuses[status] += 1
        account_write_kinds[action_kind] += 1
    if account_write_statuses["unknown"]:
        blockers["unresolved_unknown_write"] += account_write_statuses["unknown"]

    service_counts: Counter[str] = Counter()
    if service_queue_summary is not None:
        raw_states = service_queue_summary.get("by_state")
        if (
            service_queue_summary.get("account_id") != plan.account_id
            or not isinstance(raw_states, Mapping)
            or any(
                not isinstance(key, str) or type(value) is not int or value < 0
                for key, value in raw_states.items()
            )
        ):
            raise ReviewError("service queue summary is invalid")
        service_counts.update({str(key): int(value) for key, value in raw_states.items()})

    totals = Counter()
    for item in query_runs:
        for name in (
            "searched_notes", "opened_notes", "visible_comments", "candidates_a",
            "candidates_b", "candidates_c", "candidates_x", "messages_valid",
            "messages_blocked", "human_approved",
        ):
            totals[name] += getattr(item, name)
        blockers.update(item.stop_reasons)
    verified = [
        item for item in action_records
        if item.campaign_id == plan.campaign_id
        and item.account_id == plan.account_id
        and item.status is ActionStatus.VERIFIED
        and datetime.fromisoformat(item.created_at.replace("Z", "+00:00"))
        .astimezone(review_zone)
        .date()
        .isoformat()
        == plan.plan_date
    ]
    totals["verified_comment_likes"] = sum(
        item.action_type is ActionType.LIKE and item.metadata.get("interaction_scope") == "comment"
        for item in verified
    )
    totals["verified_comment_replies"] = sum(item.action_type is ActionType.REPLY for item in verified)
    totals["verified_dm_messages"] = sum(item.action_type is ActionType.DM for item in verified)
    totals["completed_targets"] = queue_counts["completed"]
    totals["blocked_targets"] = sum(
        count for status, count in queue_counts.items() if status.startswith("blocked") or status == "missed_window"
    )

    recommendations: list[ReviewRecommendation] = []
    if any(item.exhausted for item in query_runs):
        recommendations.append(_recommendation("rotate_exhausted_queries", "至少一个查询已明确耗尽，仅轮换到现有 QueryPlan 的下一项"))
    actionable = totals["candidates_a"] + totals["candidates_b"]
    if totals["searched_notes"] and actionable == 0:
        recommendations.append(_recommendation("pause_low_yield_queries", "今日搜索有读取但没有 A/B 候选，建议暂停低质查询"))
    if blockers or totals["blocked_targets"]:
        recommendations.append(_recommendation("pause_and_review_blockers", "存在风险或阻断证据，先暂停相关 query/target 并人工检查"))
    recommendations.append(_recommendation("keep_current_safety_limits", "没有证据支持提高预算或缩短间隔，保持当前安全上限"))
    flags: list[str] = []
    if not query_runs:
        flags.append("no_query_run_evidence")
    if not verified:
        flags.append("no_verified_platform_actions")
    if account_write_statuses["unknown"]:
        flags.append("unresolved_unknown_write")
    guarded = (
        "increase_daily_interaction_targets:requires_user_approval",
        "reduce_minimum_target_interval_below_600:prohibited",
        "loosen_candidate_or_message_risk_rules:requires_user_approval",
        "enable_active_dm:requires_user_approval",
        "change_campaign_authorized_facts:requires_user_approval",
    )
    payload = {
        "plan_id": plan.plan_id,
        "checked_at": checked_at,
        "funnel": dict(sorted(totals.items())),
        "lead_funnel": dict(sorted(normalized_leads.items())),
        "queue": dict(sorted(queue_counts.items())),
        "account_writes": dict(sorted(account_write_statuses.items())),
        "account_write_kinds": dict(sorted(account_write_kinds.items())),
        "service_queue": dict(sorted(service_counts.items())),
        "blockers": dict(sorted(blockers.items())),
        "records": [item.record_id for item in verified],
        "recommendations": [item.to_dict() for item in recommendations],
        "flags": flags,
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return DailyReview(
        review_id="daily_review_" + digest[:16],
        plan_id=plan.plan_id,
        campaign_id=plan.campaign_id,
        account_id=plan.account_id,
        plan_date=plan.plan_date,
        checked_at=checked_at,
        funnel=dict(sorted(totals.items())),
        lead_funnel=dict(sorted(normalized_leads.items())),
        queue_status_counts=dict(sorted(queue_counts.items())),
        account_write_status_counts=dict(sorted(account_write_statuses.items())),
        account_write_kind_counts=dict(sorted(account_write_kinds.items())),
        service_queue_status_counts=dict(sorted(service_counts.items())),
        blocker_counts=dict(sorted(blockers.items())),
        verified_action_record_ids=tuple(item.record_id for item in verified),
        recommendations=tuple(recommendations),
        guarded_changes=guarded,
        data_quality_flags=tuple(flags),
        review_status="awaiting_human_review",
        platform_actions_executed=0,
        content_hash=digest,
    )
