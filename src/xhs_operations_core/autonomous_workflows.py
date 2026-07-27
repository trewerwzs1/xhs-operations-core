"""Public autonomous workflow control plane.

The public product accepts a bounded task description once.  This module turns
that description into the immutable authority contracts and owns the small
workflow lifecycle used by ``publish``, ``service`` and ``engage``.  It does
not call Xiaohongshu directly; exact platform work is still delegated through
the single Gateway/Run Agent path by a workflow planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .authority import (
    ActionPermit,
    ActionPolicyRequest,
    AuthorityContractError,
    AuthorityStore,
    ExecutionMandate,
    PolicyRuntimeState,
    TaskIntent,
    WORKFLOW_ACTIONS,
    evaluate_action_policy,
)
from .config import load_project_config
from .onboarding import AccountSetupStore
from .paths import find_project_root, resolve_project_relative
from .storage import read_json, write_json_atomic


class AutonomousWorkflowError(ValueError):
    """Raised when a public autonomous task cannot be accepted safely."""


class AutonomousPreDispatchBlocked(RuntimeError):
    """Signal a verified zero-action block after permit issue but before dispatch."""

    def __init__(self, outcome: str, reasons: Sequence[str]) -> None:
        if outcome not in {"skip", "stop"}:
            raise ValueError("pre-dispatch outcome must be skip or stop")
        normalized = tuple(str(item or "").strip() for item in reasons if str(item or "").strip())
        if not normalized:
            raise ValueError("pre-dispatch block requires at least one reason")
        super().__init__(",".join(normalized))
        self.outcome = outcome
        self.reasons = normalized


AUTONOMOUS_WORKFLOWS = frozenset({"publish", "service", "engage"})
TERMINAL_TASK_STATES = frozenset({"completed", "cancelled", "paused_blocked"})
TASK_STATES = frozenset({"created", "running", *TERMINAL_TASK_STATES})


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _moment(value: object, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AutonomousWorkflowError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutonomousWorkflowError(f"{field} must include timezone")
    return parsed.isoformat()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _strings(values: object, field: str, *, maximum: int = 16) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise AutonomousWorkflowError(f"{field} must be a list")
    result = tuple(str(item or "").strip() for item in values)
    if not result or len(result) > maximum or any(not item for item in result):
        raise AutonomousWorkflowError(f"{field} is invalid")
    if len(set(result)) != len(result):
        raise AutonomousWorkflowError(f"{field} contains duplicates")
    return result


@dataclass(frozen=True)
class AutonomousTaskSpec:
    """Human-sized task input with no approval token or caller-supplied hash."""

    schema_version: int
    instruction: str
    source_mode: str
    source_ref: str
    requested_actions: tuple[str, ...]
    search_queries: tuple[str, ...]
    duration_days: int
    allowed_fact_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, workflow: str) -> "AutonomousTaskSpec":
        expected = {
            "schema_version",
            "instruction",
            "source_mode",
            "source_ref",
            "requested_actions",
            "search_queries",
            "duration_days",
            "allowed_fact_ids",
        }
        if set(value) != expected or value.get("schema_version") != 1:
            raise AutonomousWorkflowError("task file fields are incomplete or unknown")
        if workflow not in AUTONOMOUS_WORKFLOWS:
            raise AutonomousWorkflowError("workflow is not autonomous")
        instruction = str(value["instruction"] or "").strip()
        source_mode = str(value["source_mode"] or "").strip()
        source_ref = str(value["source_ref"] or "").strip()
        if not instruction or len(instruction) > 2000:
            raise AutonomousWorkflowError("instruction is invalid")
        if not source_ref or len(source_ref) > 500:
            raise AutonomousWorkflowError("source_ref is invalid")
        actions = _strings(value["requested_actions"], "requested_actions")
        if any(action not in WORKFLOW_ACTIONS[workflow] for action in actions):
            raise AutonomousWorkflowError("requested action is outside the workflow")
        raw_queries = value["search_queries"]
        if isinstance(raw_queries, (str, bytes)) or not isinstance(raw_queries, Sequence):
            raise AutonomousWorkflowError("search_queries must be a list")
        search_queries = tuple(str(item or "").strip() for item in raw_queries)
        if workflow == "engage":
            if (
                not 1 <= len(search_queries) <= 5
                or any(
                    not 2 <= len(item) <= 80
                    or "\ufffd" in item
                    or set(item) == {"?"}
                    for item in search_queries
                )
                or len(set(search_queries)) != len(search_queries)
            ):
                raise AutonomousWorkflowError("engage search_queries is invalid")
        elif search_queries:
            raise AutonomousWorkflowError(
                "search_queries is only valid for the engage workflow"
            )
        duration_days = value["duration_days"]
        if type(duration_days) is not int or not 1 <= duration_days <= 30:
            raise AutonomousWorkflowError("duration_days must be 1-30")
        fact_ids_raw = value["allowed_fact_ids"]
        if isinstance(fact_ids_raw, (str, bytes)) or not isinstance(fact_ids_raw, Sequence):
            raise AutonomousWorkflowError("allowed_fact_ids must be a list")
        fact_ids = tuple(str(item or "").strip() for item in fact_ids_raw)
        if len(fact_ids) > 64 or any(not item for item in fact_ids) or len(set(fact_ids)) != len(fact_ids):
            raise AutonomousWorkflowError("allowed_fact_ids is invalid")
        return cls(
            schema_version=1,
            instruction=instruction,
            source_mode=source_mode,
            source_ref=source_ref,
            requested_actions=actions,
            search_queries=search_queries,
            duration_days=duration_days,
            allowed_fact_ids=fact_ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "instruction": self.instruction,
            "source_mode": self.source_mode,
            "source_ref": self.source_ref,
            "requested_actions": list(self.requested_actions),
            "search_queries": list(self.search_queries),
            "duration_days": self.duration_days,
            "allowed_fact_ids": list(self.allowed_fact_ids),
        }

    def source_hash(self) -> str:
        return _canonical_hash(
            {
                "source_mode": self.source_mode,
                "source_ref": self.source_ref,
                "instruction": self.instruction,
            }
        )


@dataclass(frozen=True)
class AutonomousWorkflowState:
    schema_version: int
    task_id: str
    workflow: str
    account_id: str
    intent_id: str
    intent_hash: str
    mandate_id: str
    mandate_hash: str
    status: str
    created_at: str
    updated_at: str
    last_heartbeat_at: str
    heartbeat_count: int
    gateway_call_count: int
    last_outcome: str
    blocker: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AutonomousWorkflowState":
        expected = {
            "schema_version",
            "task_id",
            "workflow",
            "account_id",
            "intent_id",
            "intent_hash",
            "mandate_id",
            "mandate_hash",
            "status",
            "created_at",
            "updated_at",
            "last_heartbeat_at",
            "heartbeat_count",
            "gateway_call_count",
            "last_outcome",
            "blocker",
        }
        if set(value) != expected or value.get("schema_version") != 1:
            raise AutonomousWorkflowError("workflow state fields are incomplete or unknown")
        workflow = str(value["workflow"])
        status = str(value["status"])
        if workflow not in AUTONOMOUS_WORKFLOWS or status not in TASK_STATES:
            raise AutonomousWorkflowError("workflow state is invalid")
        if type(value["heartbeat_count"]) is not int or value["heartbeat_count"] < 0:
            raise AutonomousWorkflowError("heartbeat_count is invalid")
        if type(value["gateway_call_count"]) is not int or value["gateway_call_count"] < 0:
            raise AutonomousWorkflowError("gateway_call_count is invalid")
        return cls(
            schema_version=1,
            task_id=str(value["task_id"]),
            workflow=workflow,
            account_id=str(value["account_id"]),
            intent_id=str(value["intent_id"]),
            intent_hash=str(value["intent_hash"]),
            mandate_id=str(value["mandate_id"]),
            mandate_hash=str(value["mandate_hash"]),
            status=status,
            created_at=_moment(value["created_at"], "created_at"),
            updated_at=_moment(value["updated_at"], "updated_at"),
            last_heartbeat_at=(
                _moment(value["last_heartbeat_at"], "last_heartbeat_at")
                if value["last_heartbeat_at"]
                else ""
            ),
            heartbeat_count=int(value["heartbeat_count"]),
            gateway_call_count=int(value["gateway_call_count"]),
            last_outcome=str(value["last_outcome"]),
            blocker=str(value["blocker"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_id": self.task_id,
            "workflow": self.workflow,
            "account_id": self.account_id,
            "intent_id": self.intent_id,
            "intent_hash": self.intent_hash,
            "mandate_id": self.mandate_id,
            "mandate_hash": self.mandate_hash,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "heartbeat_count": self.heartbeat_count,
            "gateway_call_count": self.gateway_call_count,
            "last_outcome": self.last_outcome,
            "blocker": self.blocker,
        }


class AutonomousWorkflowStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "autonomous_workflows"

    def path_for(self, workflow: str) -> Path:
        if workflow not in AUTONOMOUS_WORKFLOWS:
            raise AutonomousWorkflowError("workflow is not autonomous")
        return self.root / workflow / "current.json"

    def task_spec_path(self, workflow: str) -> Path:
        if workflow not in AUTONOMOUS_WORKFLOWS:
            raise AutonomousWorkflowError("workflow is not autonomous")
        return self.root / workflow / "task_spec.json"

    def save(self, state: AutonomousWorkflowState) -> Path:
        path = self.path_for(state.workflow)
        write_json_atomic(path, state.to_dict())
        return path

    def load(self, workflow: str, *, missing_ok: bool = False) -> AutonomousWorkflowState | None:
        value = read_json(self.path_for(workflow), default=None)
        if value is None and missing_ok:
            return None
        if not isinstance(value, dict):
            raise AutonomousWorkflowError(f"{workflow} task has not been started")
        return AutonomousWorkflowState.from_dict(value)

    def save_task_spec(self, workflow: str, spec: AutonomousTaskSpec) -> Path:
        path = self.task_spec_path(workflow)
        write_json_atomic(path, spec.to_dict())
        return path

    def load_task_spec(self, workflow: str) -> AutonomousTaskSpec:
        value = read_json(self.task_spec_path(workflow), default=None)
        if not isinstance(value, dict):
            raise AutonomousWorkflowError(f"{workflow} task specification is missing")
        return AutonomousTaskSpec.from_dict(value, workflow=workflow)


def _daily_caps(profile: object, actions: Sequence[str]) -> dict[str, int]:
    names = {
        "publish_image": 1,
        "publish_video": 1,
        "service_comment_reply": getattr(profile, "max_replies_per_day"),
        "service_dm_reply": getattr(profile, "max_replies_per_day"),
        "engage_note_like": getattr(profile, "max_likes_per_day"),
        "engage_note_comment": getattr(profile, "max_comments_per_day"),
        "engage_comment_like": getattr(profile, "max_likes_per_day"),
        "engage_comment_reply": getattr(profile, "max_replies_per_day"),
        "engage_single_dm": getattr(profile, "max_targets_per_day"),
    }
    return {action: min(20, max(1, int(names[action]))) for action in actions}


def _assert_profile_action_scope(profile: object, actions: Sequence[str]) -> None:
    required = {
        "service_comment_reply": "reply",
        "service_dm_reply": "reply",
        "engage_note_like": "like",
        "engage_note_comment": "comment",
        "engage_comment_like": "like",
        "engage_comment_reply": "reply",
    }
    allowed = set(getattr(profile, "allowed_actions"))
    blocked = [action for action in actions if action in required and required[action] not in allowed]
    if blocked:
        raise AutonomousWorkflowError(
            "task actions exceed the configured account scope: " + ",".join(blocked)
        )


def _context(project_root: Path | str | None) -> tuple[Path, Path, object]:
    root = find_project_root(project_root)
    config, _ = load_project_config(root)
    profile = AccountSetupStore(config.runtime.runtime_dir).load()
    return root, config.runtime.runtime_dir, profile


def load_task_file(root: Path, path: Path, *, workflow: str) -> AutonomousTaskSpec:
    resolved = resolve_project_relative(root, str(path), field_name="task_file")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutonomousWorkflowError("task file is missing or invalid") from exc
    if not isinstance(value, dict):
        raise AutonomousWorkflowError("task file root must be an object")
    return AutonomousTaskSpec.from_dict(value, workflow=workflow)


def start_workflow(
    *,
    project_root: Path | str | None,
    workflow: str,
    task_file: Path,
    started_at: str | None = None,
) -> dict[str, object]:
    root, runtime_dir, profile = _context(project_root)
    spec = load_task_file(root, task_file, workflow=workflow)
    _assert_profile_action_scope(profile, spec.requested_actions)
    moment = _moment(started_at or _now_iso(), "started_at")
    valid_until = (datetime.fromisoformat(moment) + timedelta(days=spec.duration_days)).isoformat()
    intent = TaskIntent.create(
        account_id=profile.account_id,
        workflow=workflow,
        instruction=spec.instruction,
        source_mode=spec.source_mode,
        source_ref=spec.source_ref,
        source_hash=spec.source_hash(),
        requested_actions=spec.requested_actions,
        created_at=moment,
    )
    mandate = ExecutionMandate.from_intent(
        intent,
        valid_from=moment,
        valid_until=valid_until,
        timezone_name=profile.timezone,
        daily_caps=_daily_caps(profile, spec.requested_actions),
        minimum_interval_seconds=max(600, int(profile.heartbeat_minutes) * 60),
        allowed_fact_ids=spec.allowed_fact_ids,
        created_at=moment,
    )
    authority = AuthorityStore(runtime_dir)
    authority.save_intent(intent)
    authority.save_mandate(mandate)
    store = AutonomousWorkflowStore(runtime_dir)
    current = store.load(workflow, missing_ok=True)
    if current is not None and current.status not in TERMINAL_TASK_STATES:
        if current.intent_hash != intent.content_hash:
            raise AutonomousWorkflowError(f"{workflow} already has an active task")
        store.save_task_spec(workflow, spec)
        return {
            "ok": True,
            "created": False,
            "workflow": workflow,
            "state": current.to_dict(),
            "platform_actions_executed": 0,
        }
    task_id = "task_" + intent.content_hash[:20]
    state = AutonomousWorkflowState(
        schema_version=1,
        task_id=task_id,
        workflow=workflow,
        account_id=profile.account_id,
        intent_id=intent.intent_id,
        intent_hash=intent.content_hash,
        mandate_id=mandate.mandate_id,
        mandate_hash=mandate.content_hash,
        status="created" if workflow == "publish" else "running",
        created_at=moment,
        updated_at=moment,
        last_heartbeat_at="",
        heartbeat_count=0,
        gateway_call_count=0,
        last_outcome="task_accepted",
        blocker="",
    )
    store.save_task_spec(workflow, spec)
    path = store.save(state)
    return {
        "ok": True,
        "created": True,
        "workflow": workflow,
        "task_id": task_id,
        "intent_id": intent.intent_id,
        "mandate_id": mandate.mandate_id,
        "status": state.status,
        "storage_ref": str(path.relative_to(runtime_dir)),
        "platform_actions_executed": 0,
    }


def workflow_status(
    *,
    project_root: Path | str | None,
    workflow: str,
) -> dict[str, object]:
    _, runtime_dir, _ = _context(project_root)
    state = AutonomousWorkflowStore(runtime_dir).load(workflow, missing_ok=True)
    return {
        "ok": True,
        "workflow": workflow,
        "state": state.to_dict() if state is not None else None,
        "platform_actions_executed": 0,
    }


def stop_workflow(
    *,
    project_root: Path | str | None,
    workflow: str,
    stopped_at: str | None = None,
) -> dict[str, object]:
    _, runtime_dir, _ = _context(project_root)
    store = AutonomousWorkflowStore(runtime_dir)
    current = store.load(workflow)
    assert current is not None
    moment = _moment(stopped_at or _now_iso(), "stopped_at")
    if current.status in TERMINAL_TASK_STATES:
        state = current
    else:
        state = AutonomousWorkflowState(
            **{
                **current.to_dict(),
                "status": "cancelled",
                "updated_at": moment,
                "last_outcome": "stopped_by_task_owner",
            }
        )
        store.save(state)
    return {
        "ok": True,
        "workflow": workflow,
        "state": state.to_dict(),
        "platform_actions_executed": 0,
    }


GatewayCall = Callable[[ActionPermit], Mapping[str, Any]]


def run_heartbeat(
    *,
    project_root: Path | str | None,
    workflow: str,
    request: ActionPolicyRequest | None = None,
    runtime_state: PolicyRuntimeState | None = None,
    gateway_call: GatewayCall | None = None,
    checked_at: str | None = None,
) -> dict[str, object]:
    """Run at most one exact action or return a truthful safe no-op.

    Planners call this function with an exact request and runtime state.  The
    public no-argument heartbeat deliberately returns a safe no-op until a
    planner has produced such a request.
    """

    _, runtime_dir, _ = _context(project_root)
    store = AutonomousWorkflowStore(runtime_dir)
    current = store.load(workflow)
    assert current is not None
    moment = _moment(checked_at or _now_iso(), "checked_at")
    if current.status in TERMINAL_TASK_STATES:
        return {
            "ok": True,
            "workflow": workflow,
            "outcome": "no_op",
            "reason": "task_not_running",
            "state": current.to_dict(),
            "platform_actions_executed": 0,
        }
    heartbeat_count = current.heartbeat_count + 1
    if request is None:
        state = AutonomousWorkflowState(
            **{
                **current.to_dict(),
                "status": "running",
                "updated_at": moment,
                "last_heartbeat_at": moment,
                "heartbeat_count": heartbeat_count,
                "last_outcome": "no_exact_action_ready",
            }
        )
        store.save(state)
        return {
            "ok": True,
            "workflow": workflow,
            "outcome": "no_op",
            "reason": "no_exact_action_ready",
            "state": state.to_dict(),
            "platform_actions_executed": 0,
        }
    if runtime_state is None or gateway_call is None:
        raise AutonomousWorkflowError("exact heartbeat requires runtime_state and gateway_call")
    if request.account_id != current.account_id:
        raise AutonomousWorkflowError("action request belongs to another account")
    authority = AuthorityStore(runtime_dir)
    mandate_value = read_json(
        authority.mandates / f"{current.mandate_id}.json",
        default=None,
    )
    if not isinstance(mandate_value, dict):
        raise AutonomousWorkflowError("workflow mandate is missing")
    mandate = ExecutionMandate.from_dict(mandate_value)
    if mandate.content_hash != current.mandate_hash or mandate.workflow != workflow:
        raise AutonomousWorkflowError("workflow mandate integrity failed")
    decision, permit = evaluate_action_policy(mandate, request, runtime_state)
    authority.record_decision(decision)
    if permit is None:
        status = "paused_blocked" if decision.outcome == "stop" else "running"
        state = AutonomousWorkflowState(
            **{
                **current.to_dict(),
                "status": status,
                "updated_at": moment,
                "last_heartbeat_at": moment,
                "heartbeat_count": heartbeat_count,
                "last_outcome": decision.outcome,
                "blocker": ",".join(decision.reasons) if status == "paused_blocked" else "",
            }
        )
        store.save(state)
        return {
            "ok": decision.outcome == "skip",
            "workflow": workflow,
            "outcome": decision.outcome,
            "reasons": list(decision.reasons),
            "state": state.to_dict(),
            "platform_actions_executed": 0,
        }
    authority.save_permit(permit)
    authority.consume_permit(permit, plan_hash=request.plan_hash, consumed_at=moment)
    try:
        gateway_result = dict(gateway_call(permit))
        platform_actions = gateway_result.get("platform_actions_executed")
        if platform_actions != 1:
            raise AutonomousWorkflowError(
                "Gateway result is ambiguous; exactly one action was permitted"
            )
    except AutonomousPreDispatchBlocked as exc:
        status = "paused_blocked" if exc.outcome == "stop" else "running"
        state = AutonomousWorkflowState(
            **{
                **current.to_dict(),
                "status": status,
                "updated_at": moment,
                "last_heartbeat_at": moment,
                "heartbeat_count": heartbeat_count,
                "last_outcome": exc.outcome,
                "blocker": ",".join(exc.reasons) if status == "paused_blocked" else "",
            }
        )
        store.save(state)
        return {
            "ok": exc.outcome == "skip",
            "workflow": workflow,
            "outcome": exc.outcome,
            "reasons": list(exc.reasons),
            "permit": {
                "permit_id": permit.permit_id,
                "max_actions": permit.max_actions,
                "consumed": True,
            },
            "state": state.to_dict(),
            "gateway_calls_executed": 0,
            "platform_actions_executed": 0,
        }
    except Exception as exc:
        state = AutonomousWorkflowState(
            **{
                **current.to_dict(),
                "status": "paused_blocked",
                "updated_at": moment,
                "last_heartbeat_at": moment,
                "heartbeat_count": heartbeat_count,
                "gateway_call_count": current.gateway_call_count + 1,
                "last_outcome": "unknown",
                "blocker": "gateway_result_unknown",
            }
        )
        store.save(state)
        return {
            "ok": False,
            "workflow": workflow,
            "outcome": "unknown",
            "reason": "gateway_result_unknown",
            "detail": str(exc),
            "retry_exact_target_allowed": False,
            "permit": {
                "permit_id": permit.permit_id,
                "max_actions": permit.max_actions,
                "consumed": True,
            },
            "state": state.to_dict(),
            "gateway_calls_executed": 1,
            "platform_actions_executed": 1,
        }
    state = AutonomousWorkflowState(
        **{
            **current.to_dict(),
            "status": "running",
            "updated_at": moment,
            "last_heartbeat_at": moment,
            "heartbeat_count": heartbeat_count,
            "gateway_call_count": current.gateway_call_count + 1,
            "last_outcome": str(gateway_result.get("outcome", "dispatched")),
            "blocker": "",
        }
    )
    store.save(state)
    return {
        "ok": True,
        "workflow": workflow,
        "outcome": state.last_outcome,
        "decision": decision.to_dict(),
        "permit": {
            "permit_id": permit.permit_id,
            "max_actions": permit.max_actions,
            "consumed": True,
        },
        "gateway": gateway_result,
        "state": state.to_dict(),
        "platform_actions_executed": 1,
    }
