"""Lean autonomous Engage planner for one-search, one-action heartbeats.

Codex compiles the semantic search strategy into ``search_queries`` in the
task file.  This module owns only deterministic runtime work: one saved search
batch, sequential candidates, an exact note-like plan, policy/permit gates,
the fixed Run Agent write, visible verification, and local ledger evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .action_preflight import (
    RuntimeMode,
    UnifiedActionPreflightStore,
    UnifiedActionRequest,
    UnifiedPreflightState,
)
from .authority import (
    ActionPermit,
    ActionPolicyRequest,
    ExecutionMandate,
    PolicyRuntimeState,
)
from .autonomous_workflows import (
    AutonomousPreDispatchBlocked,
    AutonomousWorkflowError,
    AutonomousWorkflowStore,
    run_heartbeat,
)
from .config import load_project_config
from .interaction import (
    InteractionSessionError,
    InteractionSessionStore,
    prepare_readonly_search_session,
)
from .onboarding import AccountSetupStore
from .paths import find_project_root
from .platform.xhs.run_agent import RunAgentClient, RunAgentError
from .storage import read_json, write_json_atomic
from .unresolved_targets import UnresolvedTargetRegistry


MAX_CANDIDATES_PER_QUERY = 3
NOTE_LIKE_CONTROL_SELECTOR = ".interact-container .left .like-wrapper .count"
HARD_PREFLIGHT_BLOCKERS = frozenset(
    {
        "platform_access_disabled",
        "login_not_ready",
        "account_identity_not_ready",
        "exact_target_not_ready",
        "capability_not_ready",
        "execution_mandate_not_ready",
        "internal_action_permit_not_ready",
        "internal_single_action_permit_required",
        "unresolved_unknown_write",
        "internal_action_permit_lease_required",
    }
)


class EngageRunAgentPort(Protocol):
    def connection_status(self) -> dict[str, Any]: ...
    def assert_current_account_identity(self) -> dict[str, Any]: ...
    def bind_active_xhs_tab(self) -> dict[str, Any]: ...
    def search_feeds_visible(self, keyword: str) -> dict[str, Any]: ...
    def page_context(self) -> dict[str, Any]: ...
    def open_search_result(self, expected_note_id: str) -> dict[str, Any]: ...
    def get_current_feed_detail(
        self, feed_id: str, *, max_comment_items: int = 20
    ) -> dict[str, Any]: ...
    def inspect_current_like_control(self, feed_id: str) -> dict[str, Any]: ...
    def like_current_feed(self, feed_id: str) -> dict[str, Any]: ...
    def go_back_and_verify(self, expected_query: str) -> dict[str, Any]: ...


ClientFactory = Callable[..., EngageRunAgentPort]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _load_mandate(runtime_dir: Path, mandate_id: str) -> ExecutionMandate:
    value = read_json(
        runtime_dir / "authority" / "mandates" / f"{mandate_id}.json",
        default=None,
    )
    if not isinstance(value, dict):
        raise AutonomousWorkflowError("engage execution mandate is missing")
    mandate = ExecutionMandate.from_dict(value)
    if mandate.workflow != "engage":
        raise AutonomousWorkflowError("engage workflow mandate type mismatch")
    return mandate


def _session_id(task_id: str) -> str:
    return "auto_engage_" + task_id.removeprefix("task_")[:24]


def _planner_root(runtime_dir: Path) -> Path:
    return runtime_dir / "autonomous_planner" / "engage"


def _save_plan(runtime_dir: Path, plan: Mapping[str, Any]) -> Path:
    path = _planner_root(runtime_dir) / "plans" / f"{plan['plan_id']}.json"
    existing = read_json(path, default=None)
    if existing is not None and existing != dict(plan):
        raise AutonomousWorkflowError("autonomous exact plan ID collision")
    write_json_atomic(path, dict(plan))
    return path


def _record_assessment(
    runtime_dir: Path,
    *,
    task_id: str,
    session_id: str,
    result_index: int,
    note_id: str,
    disposition: str,
) -> None:
    path = _planner_root(runtime_dir) / "candidate_assessments.json"
    value = read_json(path, default={"schema_version": 1, "items": []})
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise AutonomousWorkflowError("candidate assessment store is invalid")
    row = {
        "task_id": task_id,
        "session_id": session_id,
        "result_index": result_index,
        "note_ref_hash": sha256(f"xhs-note:{note_id}".encode("utf-8")).hexdigest(),
        "disposition": disposition,
    }
    if row not in value["items"]:
        value["items"].append(row)
        write_json_atomic(path, value)


def _build_note_like_plan(
    *,
    task_id: str,
    mandate: ExecutionMandate,
    session_id: str,
    query: str,
    result_index: int,
    note_id: str,
) -> dict[str, Any]:
    target_ref_hash = sha256(f"xhs-note:{note_id}".encode("utf-8")).hexdigest()
    action_kind = "engage_note_like"
    content_hash = sha256(b"").hexdigest()
    dedupe_key_hash = sha256(
        f"{mandate.account_id}|{action_kind}|{target_ref_hash}".encode("utf-8")
    ).hexdigest()
    core = {
        "schema_version": 1,
        "task_id": task_id,
        "mandate_id": mandate.mandate_id,
        "mandate_hash": mandate.content_hash,
        "session_id": session_id,
        "query": query,
        "result_index": result_index,
        "note_id": note_id,
        "action_kind": action_kind,
        "target_ref_hash": target_ref_hash,
        "content_hash": content_hash,
        "dedupe_key_hash": dedupe_key_hash,
        "planned_action_count": 1,
    }
    plan_hash = _canonical_hash(core)
    return {
        **core,
        "plan_id": "auto_plan_" + plan_hash[:20],
        "plan_hash": plan_hash,
    }


def _eligible_like_control(value: Mapping[str, Any]) -> bool:
    control = value.get("control")
    controls = control.get("controls") if isinstance(control, Mapping) else None
    exact_controls = (
        [
            item
            for item in controls
            if isinstance(item, Mapping)
            and item.get("selector") == NOTE_LIKE_CONTROL_SELECTOR
            and item.get("index") == 0
        ]
        if isinstance(controls, list)
        else []
    )
    return (
        value.get("stateAvailable") is True
        and value.get("liked") is False
        and isinstance(control, Mapping)
        and control.get("ok") is True
        and len(exact_controls) == 1
    )


def _safe_noop(
    *,
    root: Path,
    checked_at: str,
    planner_reason: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    result = run_heartbeat(
        project_root=root,
        workflow="engage",
        checked_at=checked_at,
    )
    return {
        **result,
        "planner_reason": planner_reason,
        **dict(extra or {}),
    }


def run_autonomous_engage_heartbeat(
    *,
    project_root: Path | str | None,
    checked_at: str | None = None,
    client_factory: ClientFactory = RunAgentClient,
) -> dict[str, object]:
    """Plan and execute at most one exact note like from one saved search batch."""

    root = find_project_root(project_root)
    project_config, _ = load_project_config(root)
    runtime_dir = project_config.runtime.runtime_dir
    profile = AccountSetupStore(runtime_dir).load()
    workflows = AutonomousWorkflowStore(runtime_dir)
    current = workflows.load("engage")
    assert current is not None
    if current.status != "running":
        return _safe_noop(
            root=root,
            checked_at=checked_at or _now_iso(),
            planner_reason="engage_task_not_running",
        )
    spec = workflows.load_task_spec("engage")
    mandate = _load_mandate(runtime_dir, current.mandate_id)
    if (
        mandate.content_hash != current.mandate_hash
        or mandate.account_id != current.account_id
        or mandate.account_id != profile.account_id
    ):
        raise AutonomousWorkflowError("engage workflow authority integrity failed")
    moment = checked_at or _now_iso()
    query = spec.search_queries[0]
    read_client = client_factory(root, mandate_id=mandate.mandate_id)
    connection = read_client.connection_status()
    if connection.get("ready_for_login_check") is not True:
        raise AutonomousWorkflowError("fixed Run Agent connection is not ready")
    identity = read_client.assert_current_account_identity()
    if identity.get("verified") is not True:
        raise AutonomousWorkflowError("current Xiaohongshu account is not verified")

    session_id = _session_id(current.task_id)
    sessions = InteractionSessionStore(runtime_dir)
    session_path = sessions.session_path(session_id)
    if not session_path.exists():
        binding, candidate_ids, context = prepare_readonly_search_session(
            port=read_client,
            query=query,
        )
        sessions.start_session(
            session_id=session_id,
            account_id=current.account_id,
            query=query,
            candidate_ids=candidate_ids,
            bound_tab_id=int(binding["boundTabId"]),
            navigation_count=dict(context.get("navigationCount") or {}),
            session_origin="autonomous_task_visible_search",
            search_count=1,
            search_normalized_from_ai=context.get("search_normalized_from_ai") is True,
        )
    session = sessions.load_session(session_id)
    if session.get("search_count") != 1 or session.get("query") != query:
        raise AutonomousWorkflowError("autonomous engage search session integrity failed")
    if session.get("stage") == "note_read" and session.get("status") == "prepared":
        note_id = str(session.get("note_id") or "")
        result_index = int(session.get("result_index", -1))
    elif session.get("stage") == "search_results" and session.get("status") == "active":
        candidate_ids = session.get("candidate_ids")
        result_index = session.get("next_index")
        if (
            not isinstance(candidate_ids, list)
            or type(result_index) is not int
            or result_index < 0
            or result_index >= len(candidate_ids)
            or result_index >= MAX_CANDIDATES_PER_QUERY
        ):
            return _safe_noop(
                root=root,
                checked_at=moment,
                planner_reason="bounded_candidate_window_exhausted",
                extra={"session_id": session_id, "search_count": 1},
            )
        note_id = str(candidate_ids[result_index])
        if UnresolvedTargetRegistry(runtime_dir).is_unresolved(note_id):
            sessions.mark_candidate_skipped(
                session_id,
                result_index=result_index,
                note_id=note_id,
                reason_code="unresolved_prior_action_state",
            )
            _record_assessment(
                runtime_dir,
                task_id=current.task_id,
                session_id=session_id,
                result_index=result_index,
                note_id=note_id,
                disposition="skip_unresolved_prior_action",
            )
            return _safe_noop(
                root=root,
                checked_at=moment,
                planner_reason="unresolved_prior_action_state",
                extra={"session_id": session_id, "search_count": 1},
            )
        try:
            opened = read_client.open_search_result(note_id)
            if (
                opened.get("pageType") != "note_detail"
                or str(opened.get("noteId") or "") != note_id
            ):
                raise AutonomousWorkflowError("opened candidate identity mismatch")
            read_client.get_current_feed_detail(note_id, max_comment_items=20)
            context = read_client.page_context()
            risks = context.get("riskSignals")
            if (
                context.get("pageType") != "note_detail"
                or str(context.get("noteId") or "") != note_id
                or context.get("boundTabId") != session.get("bound_tab_id")
                or not isinstance(risks, list)
                or risks
            ):
                raise AutonomousWorkflowError(
                    "opened candidate page or risk state is invalid"
                )
            like_control = read_client.inspect_current_like_control(note_id)
        except (RunAgentError, InteractionSessionError) as exc:
            recovered = read_client.go_back_and_verify(query)
            sessions.mark_candidate_skipped(
                session_id,
                result_index=result_index,
                note_id=note_id,
                reason_code="candidate_read_or_control_unavailable",
            )
            sessions.mark_search_results(
                session_id,
                dict(recovered.get("navigationCount") or {}),
            )
            return _safe_noop(
                root=root,
                checked_at=moment,
                planner_reason="candidate_read_or_control_unavailable",
                extra={"detail": str(exc), "session_id": session_id, "search_count": 1},
            )
        sessions.mark_current_note(
            session_id=session_id,
            result_index=result_index,
            note_id=note_id,
            navigation_count=dict(context.get("navigationCount") or {}),
        )
        if (
            "engage_note_like" not in mandate.allowed_actions
            or not _eligible_like_control(like_control)
        ):
            returned = read_client.go_back_and_verify(query)
            sessions.mark_search_results(
                session_id,
                dict(returned.get("navigationCount") or {}),
            )
            _record_assessment(
                runtime_dir,
                task_id=current.task_id,
                session_id=session_id,
                result_index=result_index,
                note_id=note_id,
                disposition="skip_note_like_not_eligible",
            )
            return _safe_noop(
                root=root,
                checked_at=moment,
                planner_reason="note_like_not_eligible",
                extra={"session_id": session_id, "search_count": 1},
            )
    else:
        raise AutonomousWorkflowError("autonomous engage session is not resumable")

    plan = _build_note_like_plan(
        task_id=current.task_id,
        mandate=mandate,
        session_id=session_id,
        query=query,
        result_index=result_index,
        note_id=note_id,
    )
    plan_path = _save_plan(runtime_dir, plan)
    preflight = UnifiedActionPreflightStore(runtime_dir)
    snapshot = preflight.policy_snapshot(
        account_id=current.account_id,
        dedupe_key_hash=str(plan["dedupe_key_hash"]),
        checked_at=moment,
        budget_timezone=mandate.timezone,
        daily_limit=int(mandate.daily_caps["engage_note_like"]),
        minimum_interval_seconds=mandate.minimum_interval_seconds,
    )
    stop = read_json(
        runtime_dir / "comment_flow" / "STOP.json",
        default=None,
    )
    request = ActionPolicyRequest(
        schema_version=1,
        plan_id=str(plan["plan_id"]),
        plan_hash=str(plan["plan_hash"]),
        account_id=current.account_id,
        action_kind="engage_note_like",
        target_ref_hash=str(plan["target_ref_hash"]),
        content_hash=str(plan["content_hash"]),
        fact_ids=(),
        checked_at=moment,
    )
    runtime_state = PolicyRuntimeState(
        platform_ready=True,
        current_account_id=current.account_id,
        target_ready=True,
        content_ready=True,
        capability_ready=True,
        pacing_ready=bool(snapshot["pacing_ready"]),
        daily_budget_ready=bool(snapshot["daily_budget_ready"]),
        duplicate=bool(snapshot["duplicate"]),
        stop_active=(
            isinstance(stop, Mapping)
            and stop.get("requires_manual_reconciliation") is True
        ),
        unresolved_unknown=UnresolvedTargetRegistry(runtime_dir).is_unresolved(
            note_id
        ),
        risk_signals=(),
    )

    def gateway_call(permit: ActionPermit) -> Mapping[str, Any]:
        unified = UnifiedActionRequest.from_action_permit(
            permit,
            action_id="action_" + permit.permit_id.removeprefix("permit_")[:20],
            dedupe_key_hash=str(plan["dedupe_key_hash"]),
            checked_at=moment,
            budget_timezone=mandate.timezone,
            daily_limit=int(mandate.daily_caps["engage_note_like"]),
            minimum_interval_seconds=mandate.minimum_interval_seconds,
            verification_method="visible_like_state_change",
        )
        decision = preflight.evaluate(
            unified,
            phase="execute",
            state=UnifiedPreflightState(
                platform_access_allowed=True,
                login_ready=True,
                account_identity_ready=True,
                target_ready=True,
                approval_ready=False,
                capability_ready=True,
                exact_lease_ready=True,
                runtime_mode=RuntimeMode.AUTONOMOUS_TASK,
                mandate_ready=True,
                permit_ready=True,
                permit_actions_remaining=1,
            ),
        )
        if not decision.allowed:
            outcome = (
                "stop"
                if HARD_PREFLIGHT_BLOCKERS.intersection(decision.blockers)
                else "skip"
            )
            raise AutonomousPreDispatchBlocked(outcome, decision.blockers)
        write_client = client_factory(
            root,
            mandate_id=mandate.mandate_id,
            action_permit_id=permit.permit_id,
        )
        write_client.assert_current_account_identity()
        page = write_client.page_context()
        risks = page.get("riskSignals")
        if (
            page.get("pageType") != "note_detail"
            or str(page.get("noteId") or "") != note_id
            or not isinstance(risks, list)
            or risks
        ):
            raise AutonomousPreDispatchBlocked(
                "stop", ("current_note_or_risk_state_changed",)
            )
        control = write_client.inspect_current_like_control(note_id)
        if not _eligible_like_control(control):
            raise AutonomousPreDispatchBlocked(
                "skip", ("note_like_state_changed_before_dispatch",)
            )
        try:
            result = write_client.like_current_feed(note_id)
        except Exception:
            preflight.record_result(
                unified,
                status="unknown",
                recorded_at=_now_iso(),
                reason_code="run_agent_write_or_visible_readback_failed",
            )
            raise
        if (
            result.get("verified") is not True
            or result.get("actionDispatched") is not True
            or result.get("platform_actions_executed") != 1
        ):
            preflight.record_result(
                unified,
                status="not_dispatched",
                recorded_at=_now_iso(),
                reason_code=str(
                    result.get("failureCode") or "write_not_dispatched"
                ),
            )
            raise AutonomousPreDispatchBlocked(
                "skip", ("write_not_dispatched",)
            )
        evidence_hash = _canonical_hash(result)
        action_result = preflight.record_result(
            unified,
            status="verified",
            recorded_at=_now_iso(),
            evidence_hash=evidence_hash,
        )
        return {
            "outcome": "verified",
            "action_result": action_result,
            "visible_verification_hash": evidence_hash,
            "platform_actions_executed": 1,
        }

    result = run_heartbeat(
        project_root=root,
        workflow="engage",
        request=request,
        runtime_state=runtime_state,
        gateway_call=gateway_call,
        checked_at=moment,
    )
    returned_to_batch = False
    return_blocker = ""
    if result.get("outcome") == "verified":
        try:
            returned = read_client.go_back_and_verify(query)
            sessions.mark_search_results(
                session_id,
                dict(returned.get("navigationCount") or {}),
            )
            returned_to_batch = True
        except (RunAgentError, InteractionSessionError) as exc:
            return_blocker = str(exc)
    return {
        **result,
        "planner": {
            "session_id": session_id,
            "search_count": 1,
            "result_index": result_index,
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "plan_ref": str(plan_path.relative_to(runtime_dir)),
            "action_kind": "engage_note_like",
            "returned_to_saved_batch": returned_to_batch,
            "return_blocker": return_blocker,
        },
    }
