"""V2 adapter that binds inherited atomic engagement to the unified preflight."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .campaign import (
    Campaign,
    CampaignRepository,
    CampaignRepositoryError,
    CampaignStatus,
    FindingSeverity,
    validate_campaign,
)
from .action_preflight import (
    ActionPreflightError,
    RuntimeMode,
    UnifiedActionPreflightStore,
    UnifiedActionRequest,
    UnifiedPreflightDecision,
    UnifiedPreflightState,
)
from .interaction import (
    CurrentPageExecutionResult,
    CurrentPageInteractionPlan,
    InteractionBranch,
)
from .dm import ApprovedDMPlan, DMWriteResult
from .platform.xhs import RunAgentClient, RunAgentError
from .strategy_pack import StrategyPack


class EngageContractError(ValueError):
    pass


_ACTION_KIND = {
    InteractionBranch.NOTE_LIKE_ONLY: "engage_note_like",
    InteractionBranch.NOTE_ENGAGEMENT: "engage_note_comment",
    InteractionBranch.COMMENT_LIKE_ONLY: "engage_comment_like",
    InteractionBranch.COMMENT_ENGAGEMENT: "engage_comment_reply",
}
_CAPABILITY = {
    InteractionBranch.NOTE_LIKE_ONLY: "like_current_feed",
    InteractionBranch.NOTE_ENGAGEMENT: "post_comment_current",
    InteractionBranch.COMMENT_LIKE_ONLY: "like_current_comment",
    InteractionBranch.COMMENT_ENGAGEMENT: "reply_current_comment",
}
_VERIFICATION = {
    InteractionBranch.NOTE_LIKE_ONLY: "visible_like_state_change",
    InteractionBranch.NOTE_ENGAGEMENT: "exact_visible_comment_increase",
    InteractionBranch.COMMENT_LIKE_ONLY: "visible_like_state_change",
    InteractionBranch.COMMENT_ENGAGEMENT: "exact_visible_reply_increase",
}


def require_engage_search_campaign(
    *,
    runtime_dir: Path,
    campaign_id: str,
    strategy_pack: StrategyPack,
    checked_at: str,
) -> Campaign:
    """Fail before Run Agent unless one current Campaign exactly binds the pack."""

    try:
        campaign = CampaignRepository(runtime_dir).get(campaign_id)
    except CampaignRepositoryError as exc:
        raise EngageContractError(
            f"engage search Campaign is unavailable: {campaign_id}"
        ) from exc

    blockers: list[str] = []
    if campaign.status not in {CampaignStatus.READY, CampaignStatus.ACTIVE}:
        blockers.append("campaign_status_not_ready_or_active")
    report = validate_campaign(campaign, checked_at=checked_at)
    blockers.extend(
        finding.code
        for finding in report.findings
        if finding.severity is FindingSeverity.ERROR
    )
    if campaign.account_id != strategy_pack.account_id:
        blockers.append("campaign_account_mismatch")
    if campaign.source_note_id != strategy_pack.intent["source_id"]:
        blockers.append("campaign_source_id_mismatch")
    if campaign.source_note_ref != strategy_pack.intent["source_ref"]:
        blockers.append("campaign_source_ref_mismatch")
    if campaign.source_note_hash != strategy_pack.intent["content_hash"]:
        blockers.append("campaign_source_hash_mismatch")
    if campaign.metadata.get("promotion_input_mode") != strategy_pack.intent["mode"]:
        blockers.append("campaign_strategy_input_mode_mismatch")
    if (
        campaign.metadata.get("promotion_strategy_id")
        != strategy_pack.strategy["strategy_id"]
    ):
        blockers.append("campaign_strategy_id_mismatch")
    if campaign.metadata.get("fixture_only") is True:
        blockers.append("campaign_fixture_only")
    blockers = list(dict.fromkeys(blockers))
    if blockers:
        raise EngageContractError(
            "engage search Campaign blocked: " + "; ".join(blockers)
        )
    return campaign


def _request(
    plan: CurrentPageInteractionPlan,
    *,
    plan_hash: str,
    checked_at: str,
    daily_limit: int,
    minimum_interval_seconds: int,
    budget_timezone: str,
) -> UnifiedActionRequest:
    if plan.branch not in _ACTION_KIND:
        raise EngageContractError("engage write requires one atomic write branch")
    target_payload = (
        f"{plan.note_id}|{plan.branch.value}"
        if not plan.target_comment_id
        else (
            f"{plan.note_id}|{plan.target_comment_id}|"
            f"{plan.target_context_hash}|{plan.branch.value}"
        )
    )
    target_hash = sha256(target_payload.encode("utf-8")).hexdigest()
    dedupe_hash = sha256(
        f"{plan.account_id}|{target_payload}".encode("utf-8")
    ).hexdigest()
    return UnifiedActionRequest(
        schema_version=1,
        action_id=plan.plan_id,
        action_kind=_ACTION_KIND[plan.branch],
        account_id=plan.account_id,
        target_ref_hash=target_hash,
        dedupe_key_hash=dedupe_hash,
        plan_hash=plan_hash,
        approval_ref=plan.approval_ref,
        approval_hash=plan_hash,
        checked_at=checked_at,
        budget_timezone=budget_timezone,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        verification_method=_VERIFICATION[plan.branch],
    )


def authorize_engage_action(
    *,
    runtime_dir: Path,
    plan: CurrentPageInteractionPlan,
    plan_hash: str,
    checked_at: str,
    confirmation: str,
    expected_bound_tab_id: int,
    state: UnifiedPreflightState,
    client: RunAgentClient,
    daily_limit: int = 10,
    minimum_interval_seconds: int = 600,
    budget_timezone: str = "UTC",
) -> tuple[UnifiedActionRequest, UnifiedPreflightDecision, dict[str, Any]]:
    identity = client.assert_current_account_identity()
    context = client.page_context()
    risks = context.get("riskSignals")
    target_ready = (
        context.get("pageType") == "note_detail"
        and str(context.get("noteId") or "") == plan.note_id
        and context.get("boundTabId") == expected_bound_tab_id
        and isinstance(risks, list)
        and not risks
    )
    capability = _CAPABILITY.get(plan.branch)
    capability_ready = any(
        row.get("operation") == capability
        for row in client.capability_audit().get("allowed", [])
    )
    evaluated_state = replace(
        state,
        account_identity_ready=(
            state.account_identity_ready and identity.get("verified") is True
        ),
        target_ready=state.target_ready and target_ready,
        capability_ready=state.capability_ready and capability_ready,
        runtime_mode=RuntimeMode.SCOPED_UAT,
        scoped_uat_authorized=True,
        scoped_uat_actions_remaining=1,
    )
    request = _request(
        plan,
        plan_hash=plan_hash,
        checked_at=checked_at,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        budget_timezone=budget_timezone,
    )
    try:
        decision = UnifiedActionPreflightStore(runtime_dir).evaluate(
            request,
            phase="authorize",
            state=evaluated_state,
        )
    except ActionPreflightError as exc:
        raise EngageContractError(str(exc)) from exc
    if not decision.allowed:
        raise EngageContractError(
            "engage unified preflight blocked: " + "; ".join(decision.blockers)
        )
    try:
        lease = client.authorize_bounded_write_uat(
            confirmation=confirmation,
            account_id=plan.account_id,
            session_id=plan.session_id,
            note_id=plan.note_id,
            plan_hash=plan_hash,
            branch=plan.branch.value,
            max_actions=1,
        )
    except RunAgentError:
        raise
    return request, decision, lease


def require_engage_execution(
    *,
    runtime_dir: Path,
    request: UnifiedActionRequest,
    state: UnifiedPreflightState,
) -> UnifiedPreflightDecision:
    try:
        decision = UnifiedActionPreflightStore(runtime_dir).evaluate(
            request,
            phase="execute",
            state=replace(
                state,
                exact_lease_ready=True,
                runtime_mode=RuntimeMode.SCOPED_UAT,
                scoped_uat_authorized=True,
                scoped_uat_actions_remaining=1,
            ),
        )
    except ActionPreflightError as exc:
        raise EngageContractError(str(exc)) from exc
    if not decision.allowed:
        raise EngageContractError(
            "engage execution preflight blocked: " + "; ".join(decision.blockers)
        )
    return decision


def record_engage_result(
    *,
    runtime_dir: Path,
    request: UnifiedActionRequest,
    result: CurrentPageExecutionResult,
    recorded_at: str,
) -> dict[str, Any]:
    store = UnifiedActionPreflightStore(runtime_dir)
    if result.ok:
        evidence_hash = sha256(
            json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return store.record_result(
            request,
            status="verified",
            recorded_at=recorded_at,
            evidence_hash=evidence_hash,
        )
    reason = str(result.blockers[0] if result.blockers else result.stage)
    reason = reason if reason.replace("_", "").isalnum() else "engage_action_failed"
    return store.record_result(
        request,
        status=("unknown" if result.stage == "action_unknown" else "not_dispatched"),
        recorded_at=recorded_at,
        reason_code=reason,
    )


def build_dm_action_request(
    approved: ApprovedDMPlan,
    *,
    expected_peer_ref_hash: str,
    checked_at: str,
    daily_limit: int,
    minimum_interval_seconds: int,
    budget_timezone: str,
) -> UnifiedActionRequest:
    target_hash = str(expected_peer_ref_hash or "")
    dedupe_hash = sha256(
        (
            f"{approved.message_plan.account_id}|{approved.message_plan.conversation_id}|"
            f"{approved.message_plan.content_hash}"
        ).encode("utf-8")
    ).hexdigest()
    return UnifiedActionRequest(
        schema_version=1,
        action_id=approved.execution_id,
        action_kind="engage_single_dm",
        account_id=approved.message_plan.account_id,
        target_ref_hash=target_hash,
        dedupe_key_hash=dedupe_hash,
        plan_hash=approved.message_plan.content_hash,
        approval_ref=approved.approval_id,
        approval_hash=approved.approval_hash,
        checked_at=checked_at,
        budget_timezone=budget_timezone,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        verification_method="exact_visible_outgoing_message_increase",
    )


def authorize_dm_action(
    *,
    runtime_dir: Path,
    approved: ApprovedDMPlan,
    expected_peer_ref_hash: str,
    checked_at: str,
    confirmation: str,
    state: UnifiedPreflightState,
    client: RunAgentClient,
    daily_limit: int = 2,
    minimum_interval_seconds: int = 600,
    budget_timezone: str = "UTC",
) -> tuple[UnifiedActionRequest, UnifiedPreflightDecision, dict[str, Any]]:
    identity = client.assert_current_account_identity()
    capability_ready = any(
        row.get("operation") == "send_current_dm_message"
        for row in client.capability_audit().get("allowed", [])
    )
    evaluated_state = replace(
        state,
        account_identity_ready=(
            state.account_identity_ready and identity.get("verified") is True
        ),
        capability_ready=state.capability_ready and capability_ready,
        runtime_mode=RuntimeMode.SCOPED_UAT,
        scoped_uat_authorized=True,
        scoped_uat_actions_remaining=1,
    )
    request = build_dm_action_request(
        approved,
        expected_peer_ref_hash=expected_peer_ref_hash,
        checked_at=checked_at,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        budget_timezone=budget_timezone,
    )
    try:
        decision = UnifiedActionPreflightStore(runtime_dir).evaluate(
            request,
            phase="authorize",
            state=evaluated_state,
        )
    except ActionPreflightError as exc:
        raise EngageContractError(str(exc)) from exc
    if not decision.allowed:
        raise EngageContractError(
            "DM unified preflight blocked: " + "; ".join(decision.blockers)
        )
    target_ref = expected_peer_ref_hash[:24]
    lease = client.authorize_bounded_write_uat(
        confirmation=confirmation,
        account_id=approved.message_plan.account_id,
        session_id=approved.execution_id,
        note_id=target_ref,
        plan_hash=approved.approval_hash,
        branch="dm_message",
        max_actions=1,
    )
    return request, decision, lease


def require_dm_execution(
    *,
    runtime_dir: Path,
    request: UnifiedActionRequest,
    state: UnifiedPreflightState,
) -> UnifiedPreflightDecision:
    return require_engage_execution(
        runtime_dir=runtime_dir,
        request=request,
        state=state,
    )


def record_dm_result(
    *,
    runtime_dir: Path,
    request: UnifiedActionRequest,
    result: DMWriteResult,
    recorded_at: str,
) -> dict[str, Any]:
    store = UnifiedActionPreflightStore(runtime_dir)
    if result.attempted and result.verified:
        evidence_hash = sha256(
            json.dumps(result.evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return store.record_result(
            request,
            status="verified",
            recorded_at=recorded_at,
            evidence_hash=evidence_hash,
        )
    return store.record_result(
        request,
        status=("unknown" if result.attempted else "not_dispatched"),
        recorded_at=recorded_at,
        reason_code=(
            "dm_write_not_verified" if result.attempted else "dm_write_not_dispatched"
        ),
    )
