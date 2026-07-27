"""Atomic one-target heartbeat leasing and offline queue-state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
from typing import Callable, Mapping

from xhs_operations_core.storage import append_jsonl, read_json, update_json_object

from .daily import DailyPlan, LoopPlanError


OUTCOMES = {"verified_complete", "retryable_failure", "blocked", "unknown"}
SAFE_RETRY_BLOCKERS = {"browser_not_opened", "network_before_target", "lease_interrupted"}
UNKNOWN_RESULT_BLOCKERS = {
    "unknown_result",
    "write_result_unknown",
    "verification_unknown",
    "lease_expired_unknown",
}


def _moment(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LoopPlanError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LoopPlanError(f"{field} must include a timezone")
    return parsed


def _plan_hash(plan: DailyPlan) -> str:
    return hashlib.sha256(
        json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class HeartbeatDecision:
    decision: str
    reason: str
    plan_id: str
    item_id: str | None = None
    bridge_id: str | None = None
    lease_token: str | None = None
    scheduled_at: str | None = None
    lease_expires_at: str | None = None
    platform_actions_executed: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "plan_id": self.plan_id,
            "item_id": self.item_id,
            "bridge_id": self.bridge_id,
            "lease_token": self.lease_token,
            "scheduled_at": self.scheduled_at,
            "lease_expires_at": self.lease_expires_at,
            "platform_actions_executed": self.platform_actions_executed,
        }


class HeartbeatStateStore:
    def __init__(self, runtime_dir: Path, *, max_attempts: int = 3, lease_seconds: int = 900) -> None:
        self.runtime_dir = Path(runtime_dir)
        if not 1 <= max_attempts <= 5:
            raise LoopPlanError("max_attempts must be 1-5")
        if not 300 <= lease_seconds <= 1800:
            raise LoopPlanError("lease_seconds must be 300-1800")
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds

    def state_path(self, plan_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", plan_id) is None:
            raise LoopPlanError("invalid plan_id")
        return self.runtime_dir / "heartbeat" / f"{plan_id}.json"

    @property
    def events_path(self) -> Path:
        return self.runtime_dir / "heartbeat" / "events.jsonl"

    def initialize(
        self,
        plan: DailyPlan,
        *,
        jitter_source: Callable[[int], int] | None = None,
    ) -> dict[str, object]:
        jitter = jitter_source or secrets.randbelow
        path = self.state_path(plan.plan_id)

        def updater(current: dict[str, object]) -> dict[str, object]:
            if current:
                if current.get("plan_hash") != _plan_hash(plan):
                    raise LoopPlanError("existing heartbeat state belongs to different DailyPlan content")
                return current
            items: dict[str, object] = {}
            for item in plan.interaction_queue:
                scheduled_at = None
                if item.window_start and item.window_end:
                    start = _moment(item.window_start, "window_start")
                    end = _moment(item.window_end, "window_end")
                    width = int((end - start).total_seconds())
                    if width <= 0:
                        raise LoopPlanError("interaction window must have positive width")
                    offset = jitter(width + 1)
                    if type(offset) is not int or not 0 <= offset <= width:
                        raise LoopPlanError("jitter source returned an invalid offset")
                    scheduled_at = (start + timedelta(seconds=offset)).isoformat()
                items[item.item_id] = {
                    "item_id": item.item_id,
                    "status": (
                        "awaiting_approval"
                        if item.status == "awaiting_human_approval"
                        else "deferred_daily_budget"
                    ),
                    "scheduled_at": scheduled_at,
                    "window_end": item.window_end,
                    "bridge_id": None,
                    "attempt_count": 0,
                    "lease_token": None,
                    "lease_owner": None,
                    "lease_started_at": None,
                    "lease_expires_at": None,
                    "retry_not_before": None,
                    "last_outcome": None,
                    "last_blockers": [],
                }
            return {
                "schema_version": 2,
                "plan_id": plan.plan_id,
                "plan_hash": _plan_hash(plan),
                "updated_at": plan.created_at,
                "last_accounted_write_at": None,
                "next_write_eligible_at": None,
                "unknown_write_lock": False,
                "platform_writes_accounted": 0,
                "items": items,
            }

        state = update_json_object(path, updater)
        append_jsonl(self.events_path, {"event": "initialized", "plan_id": plan.plan_id, "at": plan.created_at})
        return state

    def claim_one(
        self,
        plan: DailyPlan,
        *,
        now: str,
        worker_id: str,
        approved_bridges: Mapping[str, str],
        token_source: Callable[[], str] | None = None,
    ) -> HeartbeatDecision:
        current_time = _moment(now, "now")
        if not worker_id.strip():
            raise LoopPlanError("worker_id is required")
        if plan.fixture_only:
            decision = HeartbeatDecision("noop", "fixture_plan_cannot_claim", plan.plan_id)
            append_jsonl(self.events_path, {"event": "noop", "reason": decision.reason, "plan_id": plan.plan_id, "item_id": None, "at": now})
            return decision
        path = self.state_path(plan.plan_id)
        decision: HeartbeatDecision | None = None

        def updater(state: dict[str, object]) -> dict[str, object]:
            nonlocal decision
            if not state or state.get("plan_hash") != _plan_hash(plan):
                raise LoopPlanError("heartbeat state is missing or DailyPlan hash mismatched")
            items = state.get("items")
            if not isinstance(items, dict):
                raise LoopPlanError("heartbeat item state is corrupt")
            if state.get("unknown_write_lock") is True:
                decision = HeartbeatDecision(
                    "noop", "unknown_write_requires_manual_resolution", plan.plan_id
                )
                return state
            # Recover expired leases first. One active lease globally blocks another target.
            for raw in items.values():
                if not isinstance(raw, dict) or raw.get("status") != "leased":
                    continue
                expiry = _moment(str(raw.get("lease_expires_at")), "lease_expires_at")
                if current_time < expiry:
                    decision = HeartbeatDecision("noop", "active_lease_exists", plan.plan_id)
                    return state
                # An expired lease has no proof that submission did not start.  Treat it
                # as an unknown write and never make it retry-ready automatically.
                raw["status"] = "blocked_unknown"
                raw["last_outcome"] = "lease_expired_unknown"
                raw["last_blockers"] = ["lease_expired_unknown"]
                raw["lease_token"] = raw["lease_owner"] = raw["lease_started_at"] = raw["lease_expires_at"] = None
                state["unknown_write_lock"] = True
                state["last_accounted_write_at"] = now
                state["next_write_eligible_at"] = (
                    current_time
                    + timedelta(seconds=plan.budget.minimum_target_interval_seconds)
                ).isoformat()
                state["platform_writes_accounted"] = int(
                    state.get("platform_writes_accounted", 0)
                ) + 1

            if state.get("unknown_write_lock") is True:
                decision = HeartbeatDecision(
                    "noop", "expired_lease_became_unknown", plan.plan_id
                )
                state["updated_at"] = now
                return state

            next_eligible = state.get("next_write_eligible_at")
            if next_eligible and current_time < _moment(
                str(next_eligible), "next_write_eligible_at"
            ):
                decision = HeartbeatDecision(
                    "noop", "minimum_write_interval_not_elapsed", plan.plan_id
                )
                return state

            for item_id, bridge_id in approved_bridges.items():
                raw = items.get(item_id)
                if raw is None:
                    raise LoopPlanError("approved bridge references unknown queue item")
                if not isinstance(bridge_id, str) or re.fullmatch(r"bridge_[A-Za-z0-9_-]{4,120}", bridge_id) is None:
                    raise LoopPlanError("approved bridge id is invalid")
                if isinstance(raw, dict) and raw.get("status") == "awaiting_approval":
                    raw["status"] = "ready"
                    raw["bridge_id"] = bridge_id

            for planned in plan.interaction_queue:
                raw = items.get(planned.item_id)
                if not isinstance(raw, dict) or raw.get("status") not in {"ready", "retry_ready"}:
                    continue
                scheduled_at = _moment(str(raw.get("scheduled_at")), "scheduled_at")
                window_end = _moment(str(raw.get("window_end")), "window_end")
                if current_time > window_end:
                    raw["status"] = "missed_window"
                    raw["last_outcome"] = "missed_window"
                    continue
                if current_time < scheduled_at:
                    continue
                retry_not_before = raw.get("retry_not_before")
                if retry_not_before and current_time < _moment(str(retry_not_before), "retry_not_before"):
                    continue
                token = (token_source or (lambda: secrets.token_hex(16)))()
                if not isinstance(token, str) or len(token) < 16:
                    raise LoopPlanError("lease token source returned an invalid token")
                expiry = current_time + timedelta(seconds=self.lease_seconds)
                raw["status"] = "leased"
                raw["attempt_count"] = int(raw.get("attempt_count", 0)) + 1
                raw["lease_token"] = token
                raw["lease_owner"] = worker_id
                raw["lease_started_at"] = now
                raw["lease_expires_at"] = expiry.isoformat()
                decision = HeartbeatDecision(
                    "claimed", "one_target_leased", plan.plan_id,
                    item_id=planned.item_id,
                    bridge_id=str(raw.get("bridge_id")),
                    lease_token=token,
                    scheduled_at=str(raw.get("scheduled_at")),
                    lease_expires_at=expiry.isoformat(),
                )
                state["updated_at"] = now
                return state
            decision = HeartbeatDecision("noop", "no_eligible_target", plan.plan_id)
            state["updated_at"] = now
            return state

        update_json_object(path, updater)
        assert decision is not None
        append_jsonl(self.events_path, {"event": decision.decision, "reason": decision.reason, "plan_id": plan.plan_id, "item_id": decision.item_id, "at": now})
        return decision

    def complete(
        self,
        plan: DailyPlan,
        *,
        item_id: str,
        lease_token: str,
        outcome: str,
        completed_at: str,
        blockers: tuple[str, ...] = (),
    ) -> dict[str, object]:
        _moment(completed_at, "completed_at")
        if outcome not in OUTCOMES:
            raise LoopPlanError("unsupported heartbeat completion outcome")
        if any(
            not isinstance(item, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_:.-]{0,127}", item) is None
            for item in blockers
        ):
            raise LoopPlanError("completion blockers must be safe structured codes")
        if outcome == "verified_complete" and blockers:
            raise LoopPlanError("verified completion cannot contain blockers")
        if outcome == "blocked" and not blockers:
            raise LoopPlanError("blocked completion requires a blocker")
        if outcome == "retryable_failure" and (
            not blockers or not set(blockers).issubset(SAFE_RETRY_BLOCKERS)
        ):
            raise LoopPlanError("retryable failure must be a known pre-write failure")
        if outcome == "unknown" and (
            not blockers or not set(blockers).intersection(UNKNOWN_RESULT_BLOCKERS)
        ):
            raise LoopPlanError("unknown completion requires unknown-result evidence")
        if outcome != "unknown" and set(blockers).intersection(UNKNOWN_RESULT_BLOCKERS):
            raise LoopPlanError("unknown-result evidence must use terminal unknown outcome")
        path = self.state_path(plan.plan_id)

        def updater(state: dict[str, object]) -> dict[str, object]:
            if not state or state.get("plan_hash") != _plan_hash(plan):
                raise LoopPlanError("heartbeat state is missing or DailyPlan hash mismatched")
            items = state.get("items")
            raw = items.get(item_id) if isinstance(items, dict) else None
            if not isinstance(raw, dict) or raw.get("status") != "leased":
                raise LoopPlanError("queue item does not hold an active lease")
            if raw.get("lease_token") != lease_token:
                raise LoopPlanError("lease token mismatch")
            completed = _moment(completed_at, "completed_at")
            started = _moment(str(raw.get("lease_started_at")), "lease_started_at")
            expiry = _moment(str(raw.get("lease_expires_at")), "lease_expires_at")
            if completed < started or completed > expiry:
                raise LoopPlanError("completion timestamp is outside active lease")
            raw["status"] = {
                "verified_complete": "completed",
                "retryable_failure": "retry_ready",
                "blocked": "blocked",
                "unknown": "blocked_unknown",
            }[outcome]
            if raw["status"] == "retry_ready" and int(raw.get("attempt_count", 0)) >= self.max_attempts:
                raw["status"] = "blocked_attempt_limit"
            raw["last_outcome"] = outcome
            raw["last_blockers"] = list(blockers)
            raw["retry_not_before"] = (
                (completed + timedelta(seconds=60)).isoformat()
                if raw["status"] == "retry_ready"
                else None
            )
            raw["lease_token"] = raw["lease_owner"] = raw["lease_started_at"] = raw["lease_expires_at"] = None
            if outcome in {"verified_complete", "unknown"}:
                state["last_accounted_write_at"] = completed_at
                state["next_write_eligible_at"] = (
                    completed
                    + timedelta(seconds=plan.budget.minimum_target_interval_seconds)
                ).isoformat()
                state["platform_writes_accounted"] = int(
                    state.get("platform_writes_accounted", 0)
                ) + 1
            if outcome == "unknown":
                state["unknown_write_lock"] = True
            state["updated_at"] = completed_at
            return state

        state = update_json_object(path, updater)
        append_jsonl(self.events_path, {"event": "completed", "outcome": outcome, "plan_id": plan.plan_id, "item_id": item_id, "at": completed_at, "blockers": list(blockers)})
        return state

    def load(self, plan: DailyPlan) -> dict[str, object]:
        state = read_json(self.state_path(plan.plan_id))
        if not isinstance(state, dict) or state.get("plan_hash") != _plan_hash(plan):
            raise LoopPlanError("heartbeat state is invalid")
        return state
