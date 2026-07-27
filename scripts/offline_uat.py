"""Run the delivered product's full offline acceptance path.

This script never invokes a browser command. It requires the first-run setup to
have left platform access disabled and STOP enabled, then executes only fixture
preview commands and proves that every reported platform action count is zero.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from xhs_operations_core.campaign import Campaign
from xhs_operations_core.contracts import ActionRecord, ActionStatus
from xhs_operations_core.discovery import (
    CandidateEvidence,
    CandidateInteractionPlan,
    assess_comment_candidate,
    build_discovery_plan,
)
from xhs_operations_core.interaction import (
    CURRENT_PAGE_APPROVAL_CONFIRMATION,
    CurrentPageInteractionPlan,
    InteractionBranch,
    InteractionSessionStore,
    execute_current_page_plan,
)
from xhs_operations_core.messaging import build_message_plan
from xhs_operations_core.orchestration import (
    DailyBudget,
    HeartbeatStateStore,
    build_daily_plan,
)
from xhs_operations_core.promotion import (
    PromotionInputMode,
    PromotionIntent,
    build_promotion_strategy,
)
from xhs_operations_core.reporting import QueryRunMetrics, build_daily_review
from xhs_operations_core.setup import initialize_user_project
from xhs_operations_core.source_notes import build_visible_thread_snapshot_from_dict
from xhs_operations_core.storage import read_jsonl


FIXTURE_TIME = "2026-07-11T12:00:00+00:00"
STYLE_CAPTURED_AT = "2026-07-11T08:00:00+00:00"
STYLE_CREATED_AT = "2026-07-11T08:05:00+00:00"
CORPUS_CONFIRMATION = "I_APPROVE_LOCAL_OWN_REPLY_CORPUS"
TASK_CONFIRMATION = "I_APPROVE_BOUNDED_CAMPAIGN_RUN"


class OfflineUatError(RuntimeError):
    """Raised when a clean delivery fails an offline acceptance gate."""


class _FixtureCurrentPagePort:
    """Truthfully labelled, zero-network CurrentPagePort acceptance double.

    It simulates the Bridge/extension contract in memory and records every
    visible-operation event.  It never opens a browser, connects to the
    packaged extension, or touches Xiaohongshu.  Reports therefore call its
    writes *fixture simulations*, never platform actions.
    """

    fixture_only_execution = True

    def __init__(
        self,
        note_id: str,
        *,
        outcome: str = "verified",
        candidate_id: str = "",
        candidate_analysis_ref: str = "",
        thread_content_hash: str = "",
        target_content_hash: str = "",
    ) -> None:
        if outcome not in {"verified", "unknown"}:
            raise OfflineUatError("fixture port outcome must be verified or unknown")
        self.note_id = note_id
        self.outcome = outcome
        self.candidate_id = candidate_id
        self.candidate_analysis_ref = candidate_analysis_ref
        self.thread_content_hash = thread_content_hash
        self.target_content_hash = target_content_hash
        self.events: list[dict[str, Any]] = []
        self.fixture_write_attempts = 0
        self._context_reads = 0
        self._submitted_text_sha256 = ""

    def _event(self, event: str, **evidence: Any) -> None:
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "event": event,
                "driver": "fixture_current_page_port",
                "mocked": True,
                "fixture_only": True,
                "platform_network_accessed": False,
                "real_platform_action": False,
                **evidence,
            }
        )

    def page_context(self) -> dict[str, Any]:
        self._context_reads += 1
        if self._context_reads % 2 == 1:
            self._event("orient", page_type="note_detail", note_id=self.note_id)
            self._event(
                "read",
                phase="required_reading",
                verification_scope="fixture_contract_only",
                simulated_dwell_seconds=12,
                wall_clock_sleep_performed=False,
            )
            if self.candidate_id and self.candidate_analysis_ref:
                self._event(
                    "analyze",
                    phase="fixture_visible_snapshot_analysis_bound",
                    analysis_executed_by="assess_comment_candidate",
                    candidate_id=self.candidate_id,
                    candidate_analysis_ref=self.candidate_analysis_ref,
                    thread_content_hash=self.thread_content_hash,
                    target_content_hash=self.target_content_hash,
                )
        else:
            self._event(
                "verify",
                phase="post_write_page_context",
                note_id=self.note_id,
                post_submit_exact_readback=bool(self._submitted_text_sha256),
                readback_sha256=self._submitted_text_sha256,
            )
        return {
            "pageType": "note_detail",
            "noteId": self.note_id,
            "riskSignals": [],
            "navigationCount": {"forward": 1, "back": 0},
        }

    def _text_write(self, *, action: str, text: str, target_id: str) -> dict[str, Any]:
        # Character-by-character assembly models the progressive input contract;
        # no DOM, browser, extension, or network is involved in this fixture.
        readback = "".join(character for character in text)
        digest = sha256(text.encode("utf-8")).hexdigest()
        readback_digest = sha256(readback.encode("utf-8")).hexdigest()
        self._event(
            "progressive_input",
            action=action,
            character_count=len(text),
            input_sha256=digest,
        )
        self._event(
            "verify",
            phase="exact_pre_submit_readback",
            exact_readback=(readback == text),
            input_sha256=digest,
            readback_sha256=readback_digest,
        )
        self._event(
            "semantic_click",
            action=action,
            target_id=target_id,
            semantic_target="current_page_submit",
        )
        self.fixture_write_attempts += 1
        self._event(
            "single_write",
            action=action,
            target_id=target_id,
            write_scope="fixture_simulation",
            fixture_write_attempt=self.fixture_write_attempts,
        )
        if self.outcome == "unknown":
            raise RuntimeError("fixture_unknown_after_submit")
        self._submitted_text_sha256 = readback_digest
        return {
            "success": readback == text,
            "operationTraceRef": f"fixture_trace:{action}:{digest[:16]}",
            "fixtureOnly": True,
        }

    def _like_write(self, *, action: str, target_id: str) -> dict[str, Any]:
        self._event("semantic_click", action=action, target_id=target_id)
        self.fixture_write_attempts += 1
        self._event(
            "single_write",
            action=action,
            target_id=target_id,
            write_scope="fixture_simulation",
            fixture_write_attempt=self.fixture_write_attempts,
        )
        if self.outcome == "unknown":
            raise RuntimeError("fixture_unknown_after_submit")
        return {
            "success": True,
            "operationTraceRef": f"fixture_trace:{action}:{target_id}",
            "fixtureOnly": True,
        }

    def like_current_feed(self, note_id: str) -> dict[str, Any]:
        return self._like_write(action="like_current_feed", target_id=note_id)

    def post_comment_current(self, note_id: str, text: str) -> dict[str, Any]:
        return self._text_write(action="post_comment_current", text=text, target_id=note_id)

    def like_current_comment_bound(
        self, note_id: str, comment_id: str, *, target_context_hash: str
    ) -> dict[str, Any]:
        self._event(
            "verify",
            phase="exact_comment_context_bound",
            target_context_hash=target_context_hash,
        )
        return self._like_write(action="like_current_comment", target_id=comment_id)

    def reply_current_comment_bound(
        self,
        note_id: str,
        text: str,
        *,
        comment_id: str,
        target_context_hash: str,
    ) -> dict[str, Any]:
        self._event(
            "verify",
            phase="exact_comment_context_bound",
            target_context_hash=target_context_hash,
        )
        return self._text_write(
            action="reply_current_comment", text=text, target_id=comment_id
        )

    def return_to_results(self, *, next_index: int) -> None:
        self._event("return_to_results", next_candidate_index=next_index)


class _FixtureClock:
    def __init__(self, values: list[str]) -> None:
        self._values = iter(values)
        self._last = values[-1]

    def __call__(self) -> str:
        try:
            self._last = next(self._values)
        except StopIteration:
            pass
        return self._last


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OfflineUatError(f"required first-run file is missing: {path}") from exc
    if not isinstance(value, dict):
        raise OfflineUatError(f"expected a JSON object: {path}")
    return value


def _platform_action_counts(value: Any) -> list[int]:
    counts: list[int] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "platform_actions_executed":
                if isinstance(item, bool) or not isinstance(item, int):
                    raise OfflineUatError("platform_actions_executed must be an integer")
                counts.append(item)
            else:
                counts.extend(_platform_action_counts(item))
    elif isinstance(value, list):
        for item in value:
            counts.extend(_platform_action_counts(item))
    return counts


def _run_case(
    project_root: Path,
    python_executable: str,
    name: str,
    arguments: list[str],
) -> dict[str, Any]:
    completed = subprocess.run(
        [python_executable, "-m", "xhs_operations_core", *arguments],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise OfflineUatError(f"{name} failed with exit {completed.returncode}: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OfflineUatError(f"{name} did not return one JSON object") from exc
    accepted_non_ready_campaign = (
        isinstance(payload, dict)
        and name == "campaign_create"
        and payload.get("stored") is True
    )
    if not isinstance(payload, dict) or (payload.get("ok") is not True and not accepted_non_ready_campaign):
        raise OfflineUatError(f"{name} did not report ok=true")
    counts = _platform_action_counts(payload)
    if any(count != 0 for count in counts):
        raise OfflineUatError(f"{name} reported a non-zero platform action count: {counts}")
    return payload


def _require_check(
    checks: list[dict[str, Any]],
    check_id: str,
    condition: bool,
    evidence: Any,
) -> None:
    checks.append({"check_id": check_id, "passed": bool(condition), "evidence": evidence})
    if not condition:
        raise OfflineUatError(f"full-flow check failed: {check_id}")


def _prime_session(
    store: InteractionSessionStore,
    plan: CurrentPageInteractionPlan,
    *,
    candidate_note_ids: list[str],
    query: str,
    confirmed_at: str,
) -> None:
    store.start_session(
        session_id=plan.session_id,
        account_id=plan.account_id,
        query=query,
        candidate_ids=candidate_note_ids,
        bound_tab_id=9001,
        navigation_count={"forward": 0, "back": 0},
        search_count=1,
    )
    store.mark_current_note(
        session_id=plan.session_id,
        result_index=0,
        note_id=plan.note_id,
        navigation_count={"forward": 1, "back": 0},
    )
    store.record_approval(
        plan,
        confirmed_at=confirmed_at,
        confirmation=CURRENT_PAGE_APPROVAL_CONFIRMATION,
    )


def _build_current_page_plan(
    *,
    campaign: Campaign,
    candidate: CandidateInteractionPlan,
    message: Any,
    queue_item: Any,
    suffix: str,
) -> CurrentPageInteractionPlan:
    approval_ref = f"approval_full_flow_{suffix}"
    return CurrentPageInteractionPlan(
        plan_id=f"plan_full_flow_{suffix}",
        session_id=f"session_full_flow_{suffix}",
        campaign_id=campaign.campaign_id,
        account_id=campaign.account_id,
        candidate_id=candidate.candidate_id,
        note_id=candidate.note_id,
        source_context_ref=f"message:{message.message_plan_id}:{message.content_hash}",
        approval_ref=approval_ref,
        branch=InteractionBranch.COMMENT_ENGAGEMENT,
        like_enabled=False,
        text=message.reply_text,
        target_comment_id=candidate.target_comment_id,
        target_context_hash=queue_item.source_context_hash,
        message_plan_id=message.message_plan_id,
        message_content_hash=message.content_hash,
        message_validation_ref=queue_item.message_validation_ref,
        campaign_fact_validation_ref=queue_item.campaign_fact_validation_ref,
        fact_refs=queue_item.fact_refs,
        style_profile_id=queue_item.style_profile_id,
        style_profile_hash=queue_item.style_profile_hash,
        style_exception_ref=(
            f"style-exception-approved:{approval_ref}"
            if queue_item.style_exception_required
            else ""
        ),
    )


def _single_write_branch_contracts(
    base: CurrentPageInteractionPlan,
) -> list[dict[str, Any]]:
    note_approval = "approval_contract_note_text"
    note_text = replace(
        base,
        plan_id="plan_contract_note_text",
        session_id="session_contract_note_text",
        approval_ref=note_approval,
        branch=InteractionBranch.NOTE_ENGAGEMENT,
        target_comment_id="",
        target_context_hash="",
        style_exception_ref=f"style-exception-approved:{note_approval}",
    )
    note_like = replace(
        base,
        plan_id="plan_contract_note_like",
        session_id="session_contract_note_like",
        source_context_ref=f"note:{base.note_id}",
        approval_ref="approval_contract_note_like",
        branch=InteractionBranch.NOTE_LIKE_ONLY,
        like_enabled=True,
        text="",
        target_comment_id="",
        target_context_hash="",
        message_plan_id="",
        message_content_hash="",
        message_validation_ref="",
        campaign_fact_validation_ref="",
        fact_refs=(),
        style_profile_id="",
        style_profile_hash="",
        style_exception_ref="",
    )
    comment_like = replace(
        note_like,
        plan_id="plan_contract_comment_like",
        session_id="session_contract_comment_like",
        source_context_ref=f"comment:{base.target_comment_id}:{base.target_context_hash}",
        approval_ref="approval_contract_comment_like",
        branch=InteractionBranch.COMMENT_LIKE_ONLY,
        target_comment_id=base.target_comment_id,
        target_context_hash=base.target_context_hash,
    )
    rows = (note_like, note_text, comment_like, base)
    return [
        {
            "branch": item.branch.value,
            "planned_write_count": item.planned_action_count,
            "like_enabled": item.like_enabled,
            "has_text": bool(item.text),
            "content_evidence_blockers": list(item.content_evidence_blockers()),
            "contract_valid": (
                item.planned_action_count == 1
                and not item.content_evidence_blockers()
                and (
                    (item.like_enabled and not item.text)
                    or (not item.like_enabled and bool(item.text))
                )
            ),
        }
        for item in rows
    ]


def _run_atomic_action_regression(
    *,
    generated: Path,
    daily_plan: Any,
    queue_item: Any,
    base_plan: CurrentPageInteractionPlan,
    candidate_analysis_ref: str,
    thread_content_hash: str,
    target_content_hash: str,
) -> dict[str, Any]:
    """Execute all four atomic branches on one fixture current page.

    Each branch owns an independent heartbeat lease while the shared
    InteractionSessionStore supplies the account-global interval and
    deduplication ledger.  The port is in-memory only, so this is developer
    regression evidence and never a claim about Xiaohongshu or packaged DOM
    behavior.
    """

    runtime = generated / "atomic_action_regression"
    if runtime.exists():
        shutil.rmtree(runtime)

    session_id = "session_full_flow_atomic"
    note_like_approval = "approval_atomic_note_like"
    note_comment_approval = "approval_atomic_note_comment"
    comment_like_approval = "approval_atomic_comment_like"
    comment_reply_approval = "approval_atomic_comment_reply"
    text_style_ref = lambda approval: (
        f"style-exception-approved:{approval}"
        if base_plan.style_exception_ref
        else ""
    )
    plans = (
        replace(
            base_plan,
            plan_id="plan_atomic_note_like",
            session_id=session_id,
            source_context_ref=f"note:{base_plan.note_id}",
            approval_ref=note_like_approval,
            branch=InteractionBranch.NOTE_LIKE_ONLY,
            like_enabled=True,
            text="",
            target_comment_id="",
            target_context_hash="",
            message_plan_id="",
            message_content_hash="",
            message_validation_ref="",
            campaign_fact_validation_ref="",
            fact_refs=(),
            style_profile_id="",
            style_profile_hash="",
            style_exception_ref="",
        ),
        replace(
            base_plan,
            plan_id="plan_atomic_note_comment",
            session_id=session_id,
            approval_ref=note_comment_approval,
            branch=InteractionBranch.NOTE_ENGAGEMENT,
            like_enabled=False,
            target_comment_id="",
            target_context_hash="",
            style_exception_ref=text_style_ref(note_comment_approval),
        ),
        replace(
            base_plan,
            plan_id="plan_atomic_comment_like",
            session_id=session_id,
            source_context_ref=(
                f"comment:{base_plan.target_comment_id}:"
                f"{base_plan.target_context_hash}"
            ),
            approval_ref=comment_like_approval,
            branch=InteractionBranch.COMMENT_LIKE_ONLY,
            like_enabled=True,
            text="",
            message_plan_id="",
            message_content_hash="",
            message_validation_ref="",
            campaign_fact_validation_ref="",
            fact_refs=(),
            style_profile_id="",
            style_profile_hash="",
            style_exception_ref="",
        ),
        replace(
            base_plan,
            plan_id="plan_atomic_comment_reply",
            session_id=session_id,
            approval_ref=comment_reply_approval,
            branch=InteractionBranch.COMMENT_ENGAGEMENT,
            like_enabled=False,
            style_exception_ref=text_style_ref(comment_reply_approval),
        ),
    )
    expected_scopes = (
        ("like", "note"),
        ("comment", "note"),
        ("like", "comment"),
        ("reply", "comment"),
    )
    primary_actions = (
        "note_like",
        "top_level_comment",
        "comment_like",
        "comment_reply",
    )

    session_store = InteractionSessionStore(runtime / "interaction")
    origin = datetime.fromisoformat(daily_plan.created_at.replace("Z", "+00:00"))
    first_confirmed_at = (origin - timedelta(seconds=1)).isoformat()
    _prime_session(
        session_store,
        plans[0],
        candidate_note_ids=[base_plan.note_id],
        query=daily_plan.search_slots[0].query,
        confirmed_at=first_confirmed_at,
    )
    port = _FixtureCurrentPagePort(
        base_plan.note_id,
        candidate_id=base_plan.candidate_id,
        candidate_analysis_ref=candidate_analysis_ref,
        thread_content_hash=thread_content_hash,
        target_content_hash=target_content_hash,
    )

    heartbeats: list[dict[str, Any]] = []
    for index, (plan, expected, primary_action) in enumerate(
        zip(plans, expected_scopes, primary_actions, strict=True)
    ):
        # Every verified accounting timestamp is 603 seconds after the prior
        # one; this is safely above the 600-second product floor.
        created = origin + timedelta(seconds=index * 603)
        created_at = created.isoformat()
        if index:
            session_store.rearm_current_note(
                session_id,
                navigation_count={"forward": 1, "back": 0},
            )
            session_store.record_approval(
                plan,
                confirmed_at=(created - timedelta(seconds=1)).isoformat(),
                confirmation=CURRENT_PAGE_APPROVAL_CONFIRMATION,
            )

        item_id = f"atomic_item_{index + 1}"
        heartbeat_item = replace(
            queue_item,
            item_id=item_id,
            primary_target_action=primary_action,
            max_platform_writes=1,
            window_start=created_at,
            window_end=(created + timedelta(seconds=180)).isoformat(),
        )
        heartbeat_plan = replace(
            daily_plan,
            plan_id=f"daily_atomic_{index + 1}",
            created_at=created_at,
            interaction_queue=(heartbeat_item,),
            deferred_count=0,
            approval_required_count=1,
        )
        heartbeat_store = HeartbeatStateStore(runtime / f"heartbeat_{index + 1}")
        heartbeat_store.initialize(heartbeat_plan, jitter_source=lambda _: 0)
        claim = heartbeat_store.claim_one(
            heartbeat_plan,
            now=created_at,
            worker_id=f"offline_atomic_worker_{index + 1}",
            approved_bridges={item_id: f"bridge_atomic_{index + 1:02d}"},
            token_source=lambda index=index: f"fixture_atomic_token_{index + 1:02d}",
        )

        records_before = len(read_jsonl(session_store.action_path))
        writes_before = port.fixture_write_attempts
        events_before = len(port.events)
        event_times = [
            (created + timedelta(seconds=offset)).isoformat()
            for offset in (0, 1, 2)
        ]
        execution = execute_current_page_plan(
            plan=plan,
            port=port,
            store=session_store,
            run_id=f"run_atomic_{index + 1}",
            created_at=created_at,
            minimum_interval_seconds=daily_plan.budget.minimum_target_interval_seconds,
            action_type_limits={"like": 3, "comment": 1, "reply": 3},
            event_clock=_FixtureClock(event_times),
        )
        all_records = [
            ActionRecord.from_dict(item)
            for item in read_jsonl(session_store.action_path)
        ]
        new_records = all_records[records_before:]
        if len(new_records) != 1:
            raise OfflineUatError(
                f"atomic branch {plan.branch.value} did not produce one ActionRecord"
            )
        record = new_records[0]
        completion = heartbeat_store.complete(
            heartbeat_plan,
            item_id=str(claim.item_id),
            lease_token=str(claim.lease_token),
            outcome="verified_complete",
            completed_at=str(record.metadata.get("accounting_at")),
        )
        heartbeats.append(
            {
                "heartbeat_index": index + 1,
                "daily_plan_id": heartbeat_plan.plan_id,
                "item_id": item_id,
                "plan_id": plan.plan_id,
                "session_id": plan.session_id,
                "candidate_id": plan.candidate_id,
                "note_id": plan.note_id,
                "branch": plan.branch.value,
                "planned_action_count": plan.planned_action_count,
                "expected_action_type": expected[0],
                "expected_scope": expected[1],
                "claim": claim.to_dict(),
                "execution": execution.to_dict(),
                "completion": {
                    "item_status": completion["items"][item_id]["status"],
                    "platform_writes_accounted": completion[
                        "platform_writes_accounted"
                    ],
                },
                "action_record": {
                    "record_id": record.record_id,
                    "status": record.status.value,
                    "action_type": record.action_type.value,
                    "scope": record.metadata.get("interaction_scope"),
                    "accounting_at": record.metadata.get("accounting_at"),
                    "next_eligible_at": record.metadata.get("next_eligible_at"),
                },
                "fixture_write_attempts": port.fixture_write_attempts - writes_before,
                "trace": port.events[events_before:],
            }
        )

    records = [
        ActionRecord.from_dict(item) for item in read_jsonl(session_store.action_path)
    ]
    accounting_times = [
        datetime.fromisoformat(str(item.metadata["accounting_at"]).replace("Z", "+00:00"))
        for item in records
    ]
    interval_seconds = [
        int((current - previous).total_seconds())
        for previous, current in zip(accounting_times, accounting_times[1:])
    ]
    all_events = [event for item in heartbeats for event in item["trace"]]
    verified_scopes = [
        (item.action_type.value, str(item.metadata.get("interaction_scope")))
        for item in records
    ]
    ok = (
        len(heartbeats) == 4
        and len(records) == 4
        and verified_scopes == list(expected_scopes)
        and all(item.status is ActionStatus.VERIFIED for item in records)
        and all(item["claim"]["decision"] == "claimed" for item in heartbeats)
        and all(item["execution"]["ok"] is True for item in heartbeats)
        and all(item["planned_action_count"] == 1 for item in heartbeats)
        and all(item["fixture_write_attempts"] == 1 for item in heartbeats)
        and all(
            sum(event["event"] == "single_write" for event in item["trace"]) == 1
            for item in heartbeats
        )
        and all(value >= 600 for value in interval_seconds)
        and port.fixture_write_attempts == 4
        and all(event.get("real_platform_action") is False for event in all_events)
    )
    return {
        "ok": ok,
        "mode": "offline_atomic_action_regression",
        "acceptance_scope": "mocked_fixture_atomic_branches_only",
        "live_platform_acceptance_performed": False,
        "driver": "fixture_current_page_port",
        "mocked": True,
        "fixture_only": True,
        "platform_network_accessed": False,
        "real_platform_action": False,
        "platform_actions_executed": 0,
        "same_session_id": session_id,
        "same_candidate_id": base_plan.candidate_id,
        "same_note_id": base_plan.note_id,
        "heartbeat_count": len(heartbeats),
        "action_record_count": len(records),
        "fixture_platform_write_simulations": port.fixture_write_attempts,
        "verified_scopes": [
            {"action_type": action_type, "scope": scope}
            for action_type, scope in verified_scopes
        ],
        "accounting_interval_seconds": interval_seconds,
        "heartbeats": heartbeats,
        "does_not_prove": [
            "live_xiaohongshu_write",
            "packaged_extension_bridge_dom",
        ],
    }


def _run_full_flow_fixture(
    root: Path,
    *,
    account_id: str,
    generated: Path,
) -> dict[str, Any]:
    """Run one joined, zero-network Campaign-to-review acceptance scenario."""

    runtime = generated / "full_flow_runtime"
    fault_runtime = generated / "full_flow_fault_runtime"
    for path in (runtime, fault_runtime):
        if path.exists():
            shutil.rmtree(path)

    strategy_input = _read_json(
        root / "config/examples/promotion_strategy.generic_event.fixture.json"
    )
    intent = PromotionIntent.from_dict(strategy_input["intent"])
    strategy = build_promotion_strategy(
        intent=intent, draft=strategy_input["strategy_draft"]
    )
    normalized_inputs = (
        intent,
        PromotionIntent.from_dict(
            {
                **strategy_input["intent"],
                "mode": "account_note",
                "source_id": "note_account_fixture_001",
                "source_ref": "account_note:note_account_fixture_001",
                "title": "周末手作活动",
                "body": strategy_input["intent"]["brief"],
                "brief": "",
            }
        ),
        PromotionIntent.from_dict(
            {
                **strategy_input["intent"],
                "mode": "specified_note",
                "source_id": "note_specified_fixture_001",
                "source_ref": "specified_note:note_specified_fixture_001",
                "title": "周末手作活动",
                "body": strategy_input["intent"]["brief"],
                "brief": "",
            }
        ),
    )

    raw_campaign = _read_json(root / "config/examples/campaign.generic_event.fixture.json")
    fixture_campaign = Campaign.from_dict(raw_campaign)
    campaign = replace(
        fixture_campaign,
        account_id=account_id,
        source_note_id=intent.source_id,
        source_note_ref=intent.source_ref,
        source_note_hash=intent.content_hash,
        conversion_goal=strategy.interaction_goal,
        metadata={
            **dict(fixture_campaign.metadata),
            "fixture_only": False,
            "delivery_uat_only": True,
            "promotion_input_mode": intent.mode.value,
            "promotion_strategy_id": strategy.strategy_id,
        },
    )
    discovery = build_discovery_plan(
        campaign,
        checked_at=strategy.checked_at,
        promotion_strategy=strategy,
    )
    visible_thread = build_visible_thread_snapshot_from_dict(
        {
            "note_id": "fixture_workshop_note_001",
            "source_url": "https://www.xiaohongshu.com/explore/fixture_workshop_note_001",
            "title": "周末手作体验怎么选",
            "body": "记录本地周末手作体验和活动环境",
            "captured_at": strategy.checked_at,
            "comments": [
                {
                    "raw_comment_id": "comment_workshop_target_001",
                    "commenter": "本地活动爱好者",
                    "text": "第一次参加手作课，想找周末能完成作品的体验，有什么选择思路吗？",
                    "kind": "main",
                    "parent_visible_order": None,
                }
            ],
        }
    )
    target_comment = visible_thread.comments[0]
    candidate = assess_comment_candidate(
        discovery_plan=discovery,
        thread=visible_thread,
        target=target_comment,
        query_id=discovery.queries[0].query_id,
        segment_id=discovery.queries[0].segment_id,
        evidence=(
            CandidateEvidence("topic_interest", "手作课", "评论明确出现手作主题"),
            CandidateEvidence("activity_intent", "想找", "评论表达活动选择意图"),
            CandidateEvidence("question_or_request", "吗？", "评论提出明确问题"),
        ),
        location_status="unknown",
    )
    message_checked = (
        datetime.fromisoformat(strategy.checked_at.replace("Z", "+00:00"))
        + timedelta(minutes=1)
    ).isoformat()
    message = build_message_plan(
        campaign=campaign,
        candidate=candidate,
        draft={
            "checked_at": message_checked,
            "reply_goal": "了解用户更偏好的手作体验类型",
            "reply_text": "这个方向很有意思，你会更想做偏实用的作品，还是更看重现场体验感？",
            "source_evidence_quotes": ["手作课", "想找"],
            "fact_uses": [],
            "public_activity_mention": False,
        },
    )
    daily_input = _read_json(root / "config/examples/daily_plan.fixture.json")
    daily_created = (datetime.fromisoformat(message_checked) + timedelta(minutes=1))
    daily_plan = build_daily_plan(
        campaign=campaign,
        discovery_plan=discovery,
        candidate_messages=((candidate, message),),
        budget=DailyBudget(**daily_input["budget"]),
        plan_date=daily_created.date().isoformat(),
        created_at=daily_created.isoformat(),
    )
    if not daily_plan.interaction_queue:
        raise OfflineUatError("full-flow DailyPlan did not produce an interaction queue")
    queue_item = daily_plan.interaction_queue[0]
    current_plan = _build_current_page_plan(
        campaign=campaign,
        candidate=candidate,
        message=message,
        queue_item=queue_item,
        suffix="happy",
    )
    branch_contracts = _single_write_branch_contracts(current_plan)
    candidate_analysis_hash = sha256(
        json.dumps(
            candidate.to_dict(), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    candidate_analysis_ref = (
        f"candidate-analysis:{candidate.candidate_id}:{candidate_analysis_hash}"
    )
    atomic_regression = _run_atomic_action_regression(
        generated=generated,
        daily_plan=daily_plan,
        queue_item=queue_item,
        base_plan=current_plan,
        candidate_analysis_ref=candidate_analysis_ref,
        thread_content_hash=visible_thread.content_hash,
        target_content_hash=target_comment.content_hash,
    )
    approval_at = (daily_created - timedelta(seconds=30)).isoformat()
    action_times = [
        (daily_created + timedelta(seconds=offset)).isoformat()
        for offset in (10, 20, 30)
    ]

    heartbeat = HeartbeatStateStore(runtime)
    heartbeat.initialize(daily_plan, jitter_source=lambda _: 0)
    claim = heartbeat.claim_one(
        daily_plan,
        now=daily_plan.created_at,
        worker_id="offline_full_flow_worker",
        approved_bridges={queue_item.item_id: "bridge_full_flow_fixture_001"},
        token_source=lambda: "fixture_lease_token_000001",
    )

    session_store = InteractionSessionStore(runtime)
    _prime_session(
        session_store,
        current_plan,
        candidate_note_ids=[candidate.note_id, "fixture_workshop_note_next_002"],
        query=discovery.queries[0].query,
        confirmed_at=approval_at,
    )
    port = _FixtureCurrentPagePort(
        current_plan.note_id,
        candidate_id=candidate.candidate_id,
        candidate_analysis_ref=candidate_analysis_ref,
        thread_content_hash=visible_thread.content_hash,
        target_content_hash=target_comment.content_hash,
    )
    execution = execute_current_page_plan(
        plan=current_plan,
        port=port,
        store=session_store,
        run_id="run_full_flow_fixture_happy",
        created_at=daily_plan.created_at,
        minimum_interval_seconds=daily_plan.budget.minimum_target_interval_seconds,
        action_type_limits={"like": 3, "comment": 1, "reply": 3},
        event_clock=_FixtureClock(action_times),
    )
    action_records = [
        ActionRecord.from_dict(item) for item in read_jsonl(session_store.action_path)
    ]
    if not action_records:
        raise OfflineUatError("full-flow fixture did not persist an ActionRecord")
    action = action_records[-1]
    accounting_at = str(action.metadata.get("accounting_at"))
    heartbeat_state = heartbeat.complete(
        daily_plan,
        item_id=str(claim.item_id),
        lease_token=str(claim.lease_token),
        outcome="verified_complete",
        completed_at=accounting_at,
    )

    early_at = (
        datetime.fromisoformat(accounting_at)
        + timedelta(seconds=daily_plan.budget.minimum_target_interval_seconds - 1)
    ).isoformat()
    early_approval = "approval_full_flow_early"
    early_plan = replace(
        current_plan,
        plan_id="plan_full_flow_early",
        approval_ref=early_approval,
        target_comment_id="comment_fixture_early",
        target_context_hash="hash_fixture_early",
        style_exception_ref=f"style-exception-approved:{early_approval}",
    )
    session_store.record_approval(
        early_plan,
        confirmed_at=(daily_created + timedelta(seconds=40)).isoformat(),
        confirmation=CURRENT_PAGE_APPROVAL_CONFIRMATION,
    )
    early_port = _FixtureCurrentPagePort(current_plan.note_id)
    early_result = execute_current_page_plan(
        plan=early_plan,
        port=early_port,
        store=session_store,
        run_id="run_full_flow_fixture_early",
        created_at=early_at,
        minimum_interval_seconds=daily_plan.budget.minimum_target_interval_seconds,
    )
    early_heartbeat = heartbeat.claim_one(
        daily_plan,
        now=early_at,
        worker_id="offline_full_flow_worker",
        approved_bridges={},
    )
    duplicate_at = (
        datetime.fromisoformat(accounting_at)
        + timedelta(seconds=daily_plan.budget.minimum_target_interval_seconds + 1)
    ).isoformat()
    duplicate_port = _FixtureCurrentPagePort(current_plan.note_id)
    duplicate_result = execute_current_page_plan(
        plan=current_plan,
        port=duplicate_port,
        store=session_store,
        run_id="run_full_flow_fixture_duplicate",
        created_at=duplicate_at,
        minimum_interval_seconds=daily_plan.budget.minimum_target_interval_seconds,
    )

    port.return_to_results(next_index=1)
    session_store.mark_search_results(
        current_plan.session_id, {"forward": 1, "back": 1}
    )
    final_session = session_store.load_session(current_plan.session_id)

    query_metrics = QueryRunMetrics.from_dict(
        {
            "run_id": "query_run_full_flow_fixture",
            "query_id": daily_plan.search_slots[0].query_id,
            "searched_notes": 2,
            "opened_notes": 1,
            "visible_comments": 1,
            "candidates_a": 1,
            "candidates_b": 0,
            "candidates_c": 0,
            "candidates_x": 0,
            "messages_valid": 1,
            "messages_blocked": 0,
            "human_approved": 1,
            "exhausted": False,
            "stop_reasons": [],
        }
    )
    review = build_daily_review(
        plan=daily_plan,
        heartbeat_state=heartbeat_state,
        query_runs=(query_metrics,),
        action_records=action_records,
        checked_at=(daily_created + timedelta(hours=3)).isoformat(),
        lead_summary={"qualified_candidates": 1, "profile_interest_signals": 0},
    )

    # Fault injection 1: submission happens, verification is unknown.  Both the
    # session STOP gate and heartbeat unknown lock must prevent every retry.
    fault_heartbeat = HeartbeatStateStore(fault_runtime)
    fault_heartbeat.initialize(daily_plan, jitter_source=lambda _: 0)
    fault_claim = fault_heartbeat.claim_one(
        daily_plan,
        now=daily_plan.created_at,
        worker_id="offline_fault_worker",
        approved_bridges={queue_item.item_id: "bridge_full_flow_fault_001"},
        token_source=lambda: "fixture_fault_token_000001",
    )
    fault_plan = _build_current_page_plan(
        campaign=campaign,
        candidate=candidate,
        message=message,
        queue_item=queue_item,
        suffix="unknown",
    )
    fault_store = InteractionSessionStore(fault_runtime)
    _prime_session(
        fault_store,
        fault_plan,
        candidate_note_ids=[candidate.note_id],
        query=discovery.queries[0].query,
        confirmed_at=approval_at,
    )
    unknown_port = _FixtureCurrentPagePort(
        fault_plan.note_id,
        outcome="unknown",
        candidate_id=candidate.candidate_id,
        candidate_analysis_ref=candidate_analysis_ref,
        thread_content_hash=visible_thread.content_hash,
        target_content_hash=target_comment.content_hash,
    )
    unknown_result = execute_current_page_plan(
        plan=fault_plan,
        port=unknown_port,
        store=fault_store,
        run_id="run_full_flow_fixture_unknown",
        created_at=daily_plan.created_at,
        minimum_interval_seconds=daily_plan.budget.minimum_target_interval_seconds,
        event_clock=_FixtureClock(action_times[:2]),
    )
    unknown_records = [
        ActionRecord.from_dict(item) for item in read_jsonl(fault_store.action_path)
    ]
    unknown_accounting_at = str(unknown_records[-1].metadata.get("accounting_at"))
    fault_state = fault_heartbeat.complete(
        daily_plan,
        item_id=str(fault_claim.item_id),
        lease_token=str(fault_claim.lease_token),
        outcome="unknown",
        completed_at=unknown_accounting_at,
        blockers=("write_result_unknown",),
    )
    retry_port = _FixtureCurrentPagePort(fault_plan.note_id)
    retry_result = execute_current_page_plan(
        plan=fault_plan,
        port=retry_port,
        store=fault_store,
        run_id="run_full_flow_fixture_unknown_retry",
        created_at=(datetime.fromisoformat(unknown_accounting_at) + timedelta(seconds=601)).isoformat(),
        minimum_interval_seconds=daily_plan.budget.minimum_target_interval_seconds,
    )
    heartbeat_retry = fault_heartbeat.claim_one(
        daily_plan,
        now=(datetime.fromisoformat(unknown_accounting_at) + timedelta(seconds=601)).isoformat(),
        worker_id="offline_fault_worker",
        approved_bridges={},
    )
    stop_payload = _read_json(fault_store.stop_path)

    event_types = [item["event"] for item in port.events]
    required_events = {
        "orient", "read", "analyze", "progressive_input", "verify",
        "semantic_click", "single_write",
    }
    readback_events = [
        item for item in port.events
        if item["event"] == "verify"
        and item.get("phase") == "exact_pre_submit_readback"
    ]
    post_write_readback_events = [
        item for item in port.events
        if item["event"] == "verify"
        and item.get("phase") == "post_write_page_context"
    ]
    record_next = str(action.metadata.get("next_eligible_at"))
    expected_next = (
        datetime.fromisoformat(accounting_at)
        + timedelta(seconds=daily_plan.budget.minimum_target_interval_seconds)
    ).isoformat()
    session_candidate_ids = final_session.get("candidate_ids", [])
    next_index = final_session.get("next_index")
    next_candidate_note_id = (
        session_candidate_ids[next_index]
        if isinstance(session_candidate_ids, list)
        and type(next_index) is int
        and 0 <= next_index < len(session_candidate_ids)
        else None
    )
    all_trace_events = [
        *port.events,
        *unknown_port.events,
        *early_port.events,
        *duplicate_port.events,
        *retry_port.events,
    ]
    visible_candidate_text = " ".join(
        (visible_thread.title, visible_thread.body, target_comment.text)
    )

    checks: list[dict[str, Any]] = []
    _require_check(
        checks,
        "three_user_input_modes_normalized",
        {item.mode for item in normalized_inputs}
        == {
            PromotionInputMode.DIRECT_BRIEF,
            PromotionInputMode.ACCOUNT_NOTE,
            PromotionInputMode.SPECIFIED_NOTE,
        }
        and all(len(item.content_hash) == 64 and item.source_text for item in normalized_inputs),
        {
            "primary_execution_mode": intent.mode.value,
            "normalized_modes": [item.mode.value for item in normalized_inputs],
            "contract": "PromotionIntent",
        },
    )
    _require_check(
        checks,
        "campaign_source_and_strategy_bound",
        campaign.source_note_id == intent.source_id
        and campaign.source_note_ref == intent.source_ref
        and campaign.source_note_hash == intent.content_hash
        and strategy.source_hash == intent.content_hash
        and discovery.strategy_id == strategy.strategy_id
        and daily_plan.processing_mode == "one_candidate_at_a_time_same_search_batch",
        {
            "input_mode": intent.mode.value,
            "source_note_ref": campaign.source_note_ref,
            "strategy_id": strategy.strategy_id,
            "discovery_strategy_id": discovery.strategy_id,
            "activity_type": campaign.activity_type.value,
            "conversion_goal": campaign.conversion_goal,
            "processing_mode": daily_plan.processing_mode,
        },
    )
    _require_check(
        checks,
        "discovery_keywords_generated",
        len(daily_plan.search_slots) >= 1,
        [item.query for item in daily_plan.search_slots],
    )
    _require_check(
        checks,
        "single_search_sequential_session",
        final_session.get("search_count") == 1
        and final_session.get("query") == discovery.queries[0].query,
        {
            "search_count": final_session.get("search_count"),
            "query": final_session.get("query"),
            "next_index": final_session.get("next_index"),
        },
    )
    _require_check(
        checks,
        "single_visible_candidate_read_and_analyzed",
        visible_thread.coverage == "visible_only"
        and candidate.evidence_level == "A"
        and candidate.proposed_action == "reply_comment"
        and not candidate.hard_blocks
        and candidate.target_comment_id == target_comment.comment_id
        and all(item.quote in visible_candidate_text for item in candidate.evidence)
        and any(
            item.get("event") == "analyze"
            and item.get("candidate_analysis_ref") == candidate_analysis_ref
            and item.get("thread_content_hash") == visible_thread.content_hash
            and item.get("target_content_hash") == target_comment.content_hash
            for item in port.events
        ),
        {
            "thread_content_hash": visible_thread.content_hash,
            "target_comment_id": target_comment.comment_id,
            "target_content_hash": target_comment.content_hash,
            "candidate_id": candidate.candidate_id,
            "candidate_analysis_ref": candidate_analysis_ref,
            "evidence_level": candidate.evidence_level,
            "evidence_quotes": [item.quote for item in candidate.evidence],
            "hard_blocks": list(candidate.hard_blocks),
        },
    )
    _require_check(
        checks,
        "daily_plan_one_write_per_heartbeat",
        daily_plan.max_one_primary_target_per_heartbeat
        and queue_item.max_platform_writes == 1,
        {"item_id": queue_item.item_id, "max_platform_writes": queue_item.max_platform_writes},
    )
    _require_check(
        checks,
        "four_single_write_branch_contracts",
        {item["branch"] for item in branch_contracts}
        == {
            "note_like_only",
            "note_engagement",
            "comment_like_only",
            "comment_engagement",
        }
        and all(item["contract_valid"] for item in branch_contracts),
        branch_contracts,
    )
    _require_check(
        checks,
        "four_atomic_branches_executed_on_same_current_page",
        atomic_regression["ok"]
        and atomic_regression["heartbeat_count"] == 4
        and atomic_regression["same_session_id"] == "session_full_flow_atomic"
        and atomic_regression["same_candidate_id"] == candidate.candidate_id
        and atomic_regression["same_note_id"] == candidate.note_id,
        {
            "heartbeat_count": atomic_regression["heartbeat_count"],
            "same_session_id": atomic_regression["same_session_id"],
            "same_candidate_id": atomic_regression["same_candidate_id"],
            "same_note_id": atomic_regression["same_note_id"],
        },
    )
    _require_check(
        checks,
        "four_atomic_heartbeats_each_write_once",
        atomic_regression["fixture_platform_write_simulations"] == 4
        and all(
            item["planned_action_count"] == 1
            and item["fixture_write_attempts"] == 1
            and item["claim"]["decision"] == "claimed"
            and item["completion"]["platform_writes_accounted"] == 1
            for item in atomic_regression["heartbeats"]
        ),
        atomic_regression["heartbeats"],
    )
    _require_check(
        checks,
        "four_atomic_verified_action_records",
        atomic_regression["action_record_count"] == 4
        and atomic_regression["verified_scopes"]
        == [
            {"action_type": "like", "scope": "note"},
            {"action_type": "comment", "scope": "note"},
            {"action_type": "like", "scope": "comment"},
            {"action_type": "reply", "scope": "comment"},
        ]
        and all(
            item["action_record"]["status"] == "verified"
            and len(item["execution"]["action_record_ids"]) == 1
            for item in atomic_regression["heartbeats"]
        ),
        atomic_regression["verified_scopes"],
    )
    _require_check(
        checks,
        "four_atomic_global_intervals_and_provenance",
        len(atomic_regression["accounting_interval_seconds"]) == 3
        and all(
            value >= daily_plan.budget.minimum_target_interval_seconds
            for value in atomic_regression["accounting_interval_seconds"]
        )
        and atomic_regression["mocked"] is True
        and atomic_regression["fixture_only"] is True
        and atomic_regression["platform_network_accessed"] is False
        and atomic_regression["real_platform_action"] is False
        and atomic_regression["platform_actions_executed"] == 0,
        {
            "interval_seconds": atomic_regression[
                "accounting_interval_seconds"
            ],
            "driver": atomic_regression["driver"],
            "mocked": atomic_regression["mocked"],
            "real_platform_action": atomic_regression["real_platform_action"],
        },
    )
    _require_check(
        checks,
        "heartbeat_claimed_one_target",
        claim.decision == "claimed" and claim.item_id == queue_item.item_id,
        claim.to_dict(),
    )
    _require_check(
        checks,
        "fixture_trace_required_events",
        required_events.issubset(set(event_types)),
        event_types,
    )
    _require_check(
        checks,
        "progressive_input_and_post_write_exact_readback",
        len(readback_events) == 1
        and readback_events[0].get("exact_readback") is True
        and len(post_write_readback_events) == 1
        and post_write_readback_events[0].get("post_submit_exact_readback") is True
        and readback_events[0].get("readback_sha256")
        == post_write_readback_events[0].get("readback_sha256"),
        {
            "pre_submit": readback_events,
            "post_submit": post_write_readback_events,
        },
    )
    _require_check(
        checks,
        "single_fixture_write_simulated",
        execution.ok
        and port.fixture_write_attempts == 1
        and event_types.count("single_write") == 1
        and len(execution.action_record_ids) == 1,
        {
            "execution": execution.to_dict(),
            "fixture_write_attempts": port.fixture_write_attempts,
            "single_write_trace_count": event_types.count("single_write"),
        },
    )
    _require_check(
        checks,
        "verified_action_record_persisted",
        len(action_records) == 1 and action.status is ActionStatus.VERIFIED,
        {"record_id": action.record_id, "status": action.status.value},
    )
    _require_check(
        checks,
        "global_600_second_next_eligible",
        record_next == expected_next
        and heartbeat_state.get("next_write_eligible_at") == expected_next,
        {"accounting_at": accounting_at, "next_eligible_at": expected_next},
    )
    _require_check(
        checks,
        "return_to_results_and_next_candidate",
        final_session.get("stage") == "search_results"
        and final_session.get("status") == "active"
        and final_session.get("next_index") == 1
        and next_candidate_note_id == "fixture_workshop_note_next_002",
        {
            "stage": final_session.get("stage"),
            "status": final_session.get("status"),
            "next_index": final_session.get("next_index"),
            "next_candidate_note_id": next_candidate_note_id,
        },
    )
    _require_check(
        checks,
        "daily_review_joins_verified_record",
        review.funnel.get("verified_comment_replies") == 1
        and action.record_id in review.verified_action_record_ids
        and review.queue_status_counts.get("completed") == 1
        and review.plan_id == daily_plan.plan_id,
        {
            "review_id": review.review_id,
            "plan_id": review.plan_id,
            "funnel": review.funnel,
            "queue_status_counts": review.queue_status_counts,
        },
    )
    _require_check(
        checks,
        "zero_real_platform_actions",
        all(item.get("real_platform_action") is False for item in all_trace_events)
        and review.platform_actions_executed == 0,
        {"platform_actions_executed": 0, "browser_started": False},
    )
    _require_check(
        checks,
        "unknown_result_stops_accounts_and_never_retries",
        not unknown_result.ok
        and unknown_records[-1].status is ActionStatus.UNKNOWN
        and stop_payload.get("writes_allowed") is False
        and fault_state.get("platform_writes_accounted") == 1
        and fault_state.get("unknown_write_lock") is True
        and retry_result.blockers == ("operator_stop_requested",)
        and retry_port.fixture_write_attempts == 0
        and heartbeat_retry.reason == "unknown_write_requires_manual_resolution",
        {
            "session_stage": unknown_result.stage,
            "action_status": unknown_records[-1].status.value,
            "stop": stop_payload,
            "heartbeat_retry_reason": heartbeat_retry.reason,
        },
    )
    _require_check(
        checks,
        "second_write_before_600_seconds_blocked",
        "minimum_target_interval_not_elapsed" in early_result.blockers
        and early_port.fixture_write_attempts == 0
        and early_heartbeat.reason == "minimum_write_interval_not_elapsed",
        {"action_blockers": list(early_result.blockers), "heartbeat_reason": early_heartbeat.reason},
    )
    _require_check(
        checks,
        "same_action_deduplicated",
        "target_comment_already_replied" in duplicate_result.blockers
        and duplicate_port.fixture_write_attempts == 0,
        {"blockers": list(duplicate_result.blockers), "fixture_write_attempts": duplicate_port.fixture_write_attempts},
    )

    assertions = {
        name: name in set(event_types)
        for name in (
            "orient", "read", "analyze", "progressive_input", "verify",
            "semantic_click", "single_write",
        )
    }
    return {
        "ok": True,
        "mode": "offline_joined_fixture_flow",
        "acceptance_scope": "offline_contract_and_state_machine_only",
        "live_platform_acceptance_performed": False,
        "does_not_prove": [
            "live_xiaohongshu_login",
            "live_xiaohongshu_read",
            "live_xiaohongshu_write",
            "packaged_extension_bridge_dom",
        ],
        "driver": "fixture_current_page_port",
        "mocked": True,
        "fixture_only": True,
        "platform_network_accessed": False,
        "real_platform_action": False,
        "browser_started": False,
        "platform_actions_executed": 0,
        "fixture_platform_write_simulations": port.fixture_write_attempts,
        "fixture_execution_contract": {
            "campaign_fixture_only_flag": campaign.metadata.get("fixture_only"),
            "campaign_delivery_uat_only": campaign.metadata.get("delivery_uat_only"),
            "reason": "exercise_nonfixture_heartbeat_claim_against_fixture_port",
            "transport_remained_fixture": True,
        },
        "checklist": checks,
        "check_count": len(checks),
        "assertions": assertions,
        "user_input": {
            "primary_mode": intent.mode.value,
            "source_id": intent.source_id,
            "source_ref": intent.source_ref,
            "content_hash": intent.content_hash,
            "user_keywords": list(intent.user_keywords),
            "normalized_contract_modes": [
                item.mode.value for item in normalized_inputs
            ],
        },
        "campaign_source": {
            "campaign_id": campaign.campaign_id,
            "source_note_id": campaign.source_note_id,
            "source_note_ref": campaign.source_note_ref,
            "activity_type": campaign.activity_type.value,
            "conversion_goal": campaign.conversion_goal,
        },
        "strategy": {
            "strategy_id": strategy.strategy_id,
            "source_hash": strategy.source_hash,
            "content_hash": strategy.content_hash,
            "topic_layers": [item.layer.value for item in strategy.topics],
            "audience_segment_ids": [item.segment_id for item in discovery.audience_profile.segments],
            "search_keywords": [item.query for item in daily_plan.search_slots],
            "processing_mode": daily_plan.processing_mode,
        },
        "candidate_analysis": {
            "thread_content_hash": visible_thread.content_hash,
            "thread_coverage": visible_thread.coverage,
            "target_comment_id": target_comment.comment_id,
            "target_content_hash": target_comment.content_hash,
            "candidate_id": candidate.candidate_id,
            "analysis_ref": candidate_analysis_ref,
            "evidence_level": candidate.evidence_level,
            "proposed_action": candidate.proposed_action,
            "evidence": [item.to_dict() for item in candidate.evidence],
            "hard_blocks": list(candidate.hard_blocks),
        },
        "heartbeat_claim": claim.to_dict(),
        "branch_contracts": branch_contracts,
        "branch_execution": {
            "executed_happy_path_branch": current_plan.branch.value,
            "executed_happy_path_write_count": 1,
            "other_branches_scope": "executed_in_developer_atomic_regression",
        },
        "developer_atomic_action_regression": atomic_regression,
        "fixture_trace": port.events,
        "action_record": {
            "record_id": action.record_id,
            "status": action.status.value,
            "action_type": action.action_type.value,
            "evidence_scope": "fixture_simulation_only",
            "operation_trace_ref": action.metadata.get("operation_trace_ref"),
            "accounting_at": accounting_at,
            "next_eligible_at": record_next,
        },
        "next_candidate_state": {
            "stage": final_session.get("stage"),
            "status": final_session.get("status"),
            "next_index": final_session.get("next_index"),
            "next_candidate_note_id": next_candidate_note_id,
            "search_count": final_session.get("search_count"),
        },
        "daily_review": review.to_dict(),
        "fault_injection": {
            "unknown_result": {
                "result": unknown_result.to_dict(),
                "action_status": unknown_records[-1].status.value,
                "stop_enabled": stop_payload.get("writes_allowed") is False,
                "heartbeat_unknown_lock": fault_state.get("unknown_write_lock"),
                "writes_accounted": fault_state.get("platform_writes_accounted"),
                "retry_action": retry_result.to_dict(),
                "retry_heartbeat": heartbeat_retry.to_dict(),
                "fixture_write_attempts": unknown_port.fixture_write_attempts,
                "retry_fixture_write_attempts": retry_port.fixture_write_attempts,
            },
            "early_second_write": {
                "attempted_at": early_at,
                "result": early_result.to_dict(),
                "heartbeat": early_heartbeat.to_dict(),
                "fixture_write_attempts": early_port.fixture_write_attempts,
            },
            "same_action_deduplication": {
                "attempted_at": duplicate_at,
                "result": duplicate_result.to_dict(),
                "fixture_write_attempts": duplicate_port.fixture_write_attempts,
            },
        },
    }


def run(project_root: Path, *, python_executable: str = sys.executable) -> dict[str, Any]:
    root = project_root.resolve()
    browser = _read_json(root / "config" / "browser.local.json")
    stop = _read_json(root / "data" / "runtime" / "comment_flow" / "STOP.json")
    receipt = _read_json(root / "data" / "runtime" / "setup" / "receipt.json")
    account_id = str(receipt.get("account_id") or "").strip()
    if not account_id:
        raise OfflineUatError("first-run receipt account_id is missing")

    if browser.get("allow_platform_access") is not False:
        raise OfflineUatError("offline UAT requires allow_platform_access=false")
    if stop.get("writes_allowed") is not False:
        raise OfflineUatError("offline UAT requires STOP with writes_allowed=false")
    if receipt.get("platform_actions_executed") != 0:
        raise OfflineUatError("first-run receipt must report zero platform actions")

    campaign = "config/examples/campaign.generic_event.fixture.json"
    candidate = "config/examples/candidate_interaction_plan.fixture.json"
    draft = "config/examples/message_plan.fixture.json"
    daily_plan = "config/examples/daily_plan.fixture.json"
    promotion_strategy = (
        "config/examples/promotion_strategy.generic_event.fixture.json"
    )
    style_history = "config/examples/style_history_capture.fixture.json"
    dm_conversation = "config/examples/dm_conversation_capture.fixture.json"
    dm_draft = "config/examples/dm_message_plan.fixture.json"

    generated = root / "work" / "offline_uat"
    generated.mkdir(parents=True, exist_ok=True)
    setup_payload = _read_json(root / "config/examples/account_setup.default.json")
    setup_payload["account_id"] = account_id
    setup_file = generated / "account_setup.json"
    setup_file.write_text(json.dumps(setup_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    setup_ref = setup_file.relative_to(root).as_posix()
    campaign_payload = _read_json(root / campaign)
    campaign_payload["account_id"] = account_id
    campaign_payload["status"] = "ready"
    campaign_payload["metadata"] = {
        **campaign_payload.get("metadata", {}),
        "fixture_only": False,
        "delivery_uat_only": True,
    }
    campaign_file = generated / "campaign.json"
    campaign_file.write_text(json.dumps(campaign_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    campaign_ref = campaign_file.relative_to(root).as_posix()
    task_payload = _read_json(root / "config/examples/campaign_task.generic_event.default.json")
    task_payload["account_id"] = account_id
    task_file = generated / "task.json"
    task_file.write_text(json.dumps(task_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    task_ref = task_file.relative_to(root).as_posix()
    post_payload = _read_json(root / "config/examples/post_engagement.fixture.json")
    post_payload["campaign_id"] = str(campaign_payload["campaign_id"])
    post_payload["account_id"] = account_id
    # Keep persisted lead evidence on the same local review day as DailyPlan.
    post_payload["checked_at"] = FIXTURE_TIME
    post_file = generated / "post_engagement.json"
    post_file.write_text(json.dumps(post_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    post_ref = post_file.relative_to(root).as_posix()
    task_id = "task_" + __import__("hashlib").sha256(
        json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    cases: list[tuple[str, list[str]]] = [
        ("doctor", ["doctor", "--project-root", str(root), "--init-runtime", "--format", "json"]),
        ("account_setup", ["setup", "account-config", "--project-root", str(root), "--file", setup_ref]),
        ("campaign_create", ["campaign", "create", "--project-root", str(root), "--file", campaign_ref, "--checked-at", FIXTURE_TIME]),
        ("style_profile", [
            "style", "profile-build", "--project-root", str(root), "--file", style_history,
            "--account-id", account_id, "--consent-ref", "offline_uat_consent",
            "--captured-at", STYLE_CAPTURED_AT, "--created-at", STYLE_CREATED_AT,
        ]),
        ("style_corpus", [
            "style", "corpus-build", "--project-root", str(root), "--file", style_history,
            "--account-id", account_id, "--consent-ref", "offline_uat_consent",
            "--captured-at", STYLE_CAPTURED_AT, "--created-at", STYLE_CREATED_AT,
            "--confirm-local-corpus", CORPUS_CONFIRMATION,
        ]),
        ("style_corpus_status", [
            "style", "corpus-status", "--project-root", str(root), "--account-id", account_id,
        ]),
        ("message_plan", [
            "message", "plan-preview", "--project-root", str(root), "--campaign", campaign,
            "--candidate", candidate, "--draft", draft, "--style-history-capture", style_history,
            "--style-consent-ref", "offline_uat_consent", "--style-captured-at", STYLE_CAPTURED_AT,
            "--style-profile-created-at", STYLE_CREATED_AT,
        ]),
        ("daily_plan", [
            "loop", "daily-preview", "--project-root", str(root),
            "--campaign", campaign, "--file", daily_plan,
            "--promotion-file", promotion_strategy,
        ]),
        ("post_engagement", [
            "loop", "post-preview", "--project-root", str(root),
            "--file", post_ref,
        ]),
        ("post_lead_record", [
            "loop", "post-record", "--project-root", str(root),
            "--file", post_ref,
        ]),
        ("heartbeat_init", [
            "loop", "heartbeat-init", "--project-root", str(root),
            "--campaign", campaign, "--file", daily_plan,
            "--promotion-file", promotion_strategy,
        ]),
        ("heartbeat_noop", [
            "loop", "heartbeat-claim", "--project-root", str(root), "--campaign", campaign,
            "--file", daily_plan, "--now", "2026-07-11T09:00:00+00:00", "--worker-id", "offline-uat",
            "--promotion-file", promotion_strategy,
        ]),
        ("daily_review", [
            "review", "daily-preview", "--project-root", str(root), "--campaign", campaign,
            "--file", daily_plan, "--metrics", "config/examples/daily_review_metrics.fixture.json",
            "--checked-at", FIXTURE_TIME,
            "--promotion-file", promotion_strategy,
        ]),
        ("dm_readonly", [
            "dm", "conversation-preview", "--project-root", str(root), "--file", dm_conversation,
            "--account-id", "account_fixture_001", "--captured-at", FIXTURE_TIME,
        ]),
        ("dm_message", [
            "dm", "message-preview", "--project-root", str(root), "--campaign", campaign,
            "--conversation", dm_conversation, "--draft", dm_draft, "--captured-at", FIXTURE_TIME,
        ]),
        ("dm_approved_bridge", [
            "dm", "approved-plan-preview", "--project-root", str(root), "--campaign", campaign,
            "--conversation", dm_conversation, "--draft", dm_draft, "--captured-at", FIXTURE_TIME,
            "--approval", "config/examples/dm_approval.fixture.json",
        ]),
        ("comment_approved_bridge", [
            "interaction", "approved-plan-preview", "--project-root", str(root), "--campaign", campaign,
            "--candidate", candidate, "--draft", draft,
            "--approval", "config/examples/message_approval.fixture.json", "--result-index", "1",
            "--promotion-file", promotion_strategy,
        ]),
        ("task_create", ["task", "create", "--project-root", str(root), "--file", task_ref]),
        ("task_authorize", [
            "task", "authorize", "--project-root", str(root), "--task-id", task_id,
            "--confirmed-at", "2026-07-12T22:31:00+08:00", "--confirm-bounded-run", TASK_CONFIRMATION,
        ]),
        ("task_start", [
            "task", "transition", "--project-root", str(root), "--task-id", task_id,
            "--to", "running", "--changed-at", "2026-07-13T10:00:00+08:00",
        ]),
        ("task_schedule", ["task", "schedule-preview", "--project-root", str(root), "--task-id", task_id]),
        ("task_due", [
            "task", "due-status", "--project-root", str(root), "--task-id", task_id,
            "--at", "2026-07-13T11:00:00+08:00",
        ]),
    ]

    results = {
        name: _run_case(root, python_executable, name, arguments)
        for name, arguments in cases
    }

    daily = results["daily_plan"]["daily_plan"]
    if daily["processing_mode"] != "one_candidate_at_a_time_same_search_batch":
        raise OfflineUatError("daily plan is not single-candidate processing")
    if daily["budget"]["minimum_target_interval_seconds"] < 600:
        raise OfflineUatError("target interval is below 600 seconds")
    post = results["post_engagement"]["post_engagement_plan"]
    actions = post["public_actions"]
    if sum(item["action"] == "top_level_comment" for item in actions) > 1:
        raise OfflineUatError("post engagement exceeds one top-level comment")
    if not 1 <= sum(item["action"] == "reply_comment" for item in actions) <= 3:
        raise OfflineUatError("post engagement reply count is outside 1-3")
    if post["execution_ready"] is not False:
        raise OfflineUatError("post engagement preview must remain approval-bound")
    if any(item["minimum_delay_from_previous_target_seconds"] < 600 for item in actions[1:]):
        raise OfflineUatError("post engagement target interval is below 600 seconds")
    if post["dm_candidates"][0]["status"] != "record_only":
        raise OfflineUatError("unsupported DM capability must remain record-only")
    lead_persistence = results["post_lead_record"]["lead_persistence"]
    if lead_persistence["summary"]["dm_candidates"] < 1:
        raise OfflineUatError("post engagement DM candidate was not persisted")
    if lead_persistence["raw_comment_text_stored"] is not False:
        raise OfflineUatError("lead persistence must not store raw comment text")
    if results["daily_review"]["daily_review"]["lead_funnel"]["dm_candidates"] < 1:
        raise OfflineUatError("daily review did not include persisted lead evidence")
    if results["style_profile"]["style_profile"]["stores_raw_reply_text"] is not False:
        raise OfflineUatError("style profile must not store raw historical replies")
    if results["style_corpus"]["reply_corpus"]["entry_count"] < 1:
        raise OfflineUatError("local owner reply corpus must contain captured examples")
    if "entries" in results["style_corpus"]["reply_corpus"]:
        raise OfflineUatError("corpus build output must expose metadata only")
    if results["heartbeat_noop"]["heartbeat"]["decision"] != "noop":
        raise OfflineUatError("unapproved fixture heartbeat must be a noop")
    if results["dm_readonly"]["dm_conversation"]["read_only"] is not True:
        raise OfflineUatError("DM fixture path must remain read-only")
    if results["dm_approved_bridge"]["approved_dm_plan"]["execution_ready"] is not False:
        raise OfflineUatError("DM fixture approval must remain non-executable")
    if results["comment_approved_bridge"]["approved_plan"]["execution_ready"] is not False:
        raise OfflineUatError("comment fixture approval must remain non-executable")
    if results["task_schedule"]["schedule"]["catch_up_policy"] != "skip_missed_runs_no_burst":
        raise OfflineUatError("task scheduler must not catch up missed interactions")
    if results["task_due"]["due_status"]["max_primary_targets"] != 1:
        raise OfflineUatError("one heartbeat may process at most one target")

    full_flow = _run_full_flow_fixture(
        root,
        account_id=account_id,
        generated=generated,
    )
    full_flow_counts = _platform_action_counts(full_flow)
    if any(count != 0 for count in full_flow_counts):
        raise OfflineUatError(
            f"joined fixture flow reported a real platform action: {full_flow_counts}"
        )
    full_flow_ids = [item["check_id"] for item in full_flow["checklist"]]

    return {
        "ok": True,
        "mode": "offline_fixture_uat",
        "acceptance_scope": "offline_contract_and_state_machine_only",
        "live_platform_acceptance_performed": False,
        "does_not_prove": [
            "live_xiaohongshu_login",
            "live_xiaohongshu_read",
            "live_xiaohongshu_write",
            "packaged_extension_bridge_dom",
        ],
        "project_root": str(root),
        "checks_passed": [*results, *full_flow_ids],
        "cli_check_count": len(results),
        "full_flow_check_count": full_flow["check_count"],
        "check_count": len(results) + full_flow["check_count"],
        "stop_enabled": True,
        "platform_access_allowed": False,
        "platform_actions_executed": 0,
        "browser_started": False,
        "full_flow_uat": full_flow,
    }


def run_isolated_development_sandbox(
    project_root: Path,
    *,
    python_executable: str = sys.executable,
) -> dict[str, Any]:
    """Run the UAT against a temporary STOP-on configuration sandbox.

    This keeps an activated developer checkout untouched.  It copies only the
    immutable project markers and committed example configuration needed by
    the fixture CLI flow; no browser profile, login state, local configuration,
    delivery archive, or platform connection is copied or created.
    """

    source = project_root.resolve()

    def ignore_local_config(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if ".local.json" in name.lower()
            or name.lower().endswith((".lock", ".bak", ".tmp"))
        }

    with tempfile.TemporaryDirectory(prefix="xhs-operations-core-dev-acceptance-") as temp:
        sandbox = Path(temp) / "xhs-operations-core"
        sandbox.mkdir()
        for marker in ("pyproject.toml", "AGENTS.md"):
            shutil.copy2(source / marker, sandbox / marker)
        shutil.copytree(
            source / "config",
            sandbox / "config",
            ignore=ignore_local_config,
        )
        initialize_user_project(
            sandbox,
            account_id="offline_uat_account_001",
            profile_name="offline_uat_profile_001",
        )
        report = run(sandbox, python_executable=python_executable)

    return {
        **report,
        "project_root": str(source),
        "execution_sandbox": "temporary_stop_on_clean_config",
        "isolated_sandbox_removed_after_run": True,
        "local_developer_configuration_read": False,
        "delivery_archive_created": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the zero-platform-action delivery UAT")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--isolated-development-sandbox",
        action="store_true",
        help="run in a temporary STOP-on fixture configuration without touching local account state",
    )
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args()
    try:
        if args.isolated_development_sandbox:
            report = run_isolated_development_sandbox(args.project_root)
        else:
            report = run(args.project_root)
    except (OfflineUatError, OSError) as exc:
        serialized = json.dumps(
            {"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2
        ) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
        print(serialized, end="")
        return 1
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
