"""Current-page streaming interaction session with immediate per-action audit."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xhs_operations_core.contracts import (
    ActionRecord,
    ActionStatus,
    ActionType,
    RiskDecision,
    RiskLevel,
    RunMode,
    TextSource,
    ThrottleDecision,
    ValidatorDecision,
    new_id,
    utc_now_iso,
)
from xhs_operations_core.storage import append_jsonl, read_json, read_jsonl, write_json_atomic
from xhs_operations_core.unresolved_targets import UnresolvedTargetRegistry


class InteractionSessionError(ValueError):
    def __init__(self, message: str, *, risk_signals: object = ()) -> None:
        super().__init__(message)
        self.risk_signals = risk_signals


CURRENT_PAGE_EXECUTION_CONFIRMATION = "I_CONFIRM_CURRENT_PAGE_INTERACTION_TEST"
CURRENT_PAGE_APPROVAL_CONFIRMATION = "I_APPROVE_CURRENT_PAGE_INTERACTION"
_SAFE_XHS_NOTE_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")


class InteractionBranch(str, Enum):
    NOTE_LIKE_ONLY = "note_like_only"
    NOTE_ENGAGEMENT = "note_engagement"
    COMMENT_ENGAGEMENT = "comment_engagement"
    COMMENT_LIKE_ONLY = "comment_like_only"
    READ_ONLY_SKIP = "read_only_skip"


@dataclass(frozen=True)
class CurrentPageInteractionPlan:
    plan_id: str
    session_id: str
    campaign_id: str
    account_id: str
    candidate_id: str
    note_id: str
    source_context_ref: str
    approval_ref: str
    branch: InteractionBranch
    like_enabled: bool = False
    text: str = ""
    target_comment_id: str = ""
    target_context_hash: str = ""
    message_plan_id: str = ""
    message_content_hash: str = ""
    message_validation_ref: str = ""
    campaign_fact_validation_ref: str = ""
    fact_refs: tuple[str, ...] = ()
    style_profile_id: str = ""
    style_profile_hash: str = ""
    style_exception_ref: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "branch", InteractionBranch(self.branch))
        object.__setattr__(self, "fact_refs", tuple(self.fact_refs))
        for name in (
            "plan_id", "session_id", "campaign_id", "account_id", "candidate_id",
            "note_id", "source_context_ref", "approval_ref",
        ):
            if not str(getattr(self, name)).strip():
                raise InteractionSessionError(f"{name} is required")
        if self.branch is InteractionBranch.READ_ONLY_SKIP:
            if self.like_enabled or self.text or self.target_comment_id:
                raise InteractionSessionError("read_only_skip cannot contain write actions")
            return
        if self.branch in {
            InteractionBranch.NOTE_LIKE_ONLY,
            InteractionBranch.COMMENT_LIKE_ONLY,
        }:
            if not self.like_enabled or self.text.strip():
                raise InteractionSessionError(
                    "like-only branch requires one like and no text action"
                )
            if self.branch is InteractionBranch.COMMENT_LIKE_ONLY and (
                not self.target_comment_id or not self.target_context_hash
            ):
                raise InteractionSessionError(
                    "comment_like_only requires exact comment identity"
                )
            if self.branch is InteractionBranch.NOTE_LIKE_ONLY and (
                self.target_comment_id or self.target_context_hash
            ):
                raise InteractionSessionError(
                    "note_like_only cannot target a comment"
                )
            return
        if self.like_enabled:
            raise InteractionSessionError(
                "text branches cannot bundle a like; one plan allows one platform write"
            )
        if not self.text.strip():
            raise InteractionSessionError("interaction branch requires one text action")
        if self.branch is InteractionBranch.NOTE_ENGAGEMENT:
            if self.target_comment_id or self.target_context_hash:
                raise InteractionSessionError("note_engagement cannot target a comment")
        elif not self.target_comment_id or not self.target_context_hash:
            raise InteractionSessionError("comment_engagement requires exact comment identity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "campaign_id": self.campaign_id,
            "account_id": self.account_id,
            "candidate_id": self.candidate_id,
            "note_id": self.note_id,
            "source_context_ref": self.source_context_ref,
            "approval_ref": self.approval_ref,
            "branch": self.branch.value,
            "like_enabled": self.like_enabled,
            "text": self.text,
            "target_comment_id": self.target_comment_id,
            "target_context_hash": self.target_context_hash,
            "message_plan_id": self.message_plan_id,
            "message_content_hash": self.message_content_hash,
            "message_validation_ref": self.message_validation_ref,
            "campaign_fact_validation_ref": self.campaign_fact_validation_ref,
            "fact_refs": list(self.fact_refs),
            "style_profile_id": self.style_profile_id,
            "style_profile_hash": self.style_profile_hash,
            "style_exception_ref": self.style_exception_ref,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CurrentPageInteractionPlan":
        allowed = {
            "plan_id", "session_id", "campaign_id", "account_id", "candidate_id",
            "note_id", "source_context_ref", "approval_ref", "branch", "like_enabled",
            "text", "target_comment_id", "target_context_hash",
            "message_plan_id", "message_content_hash", "message_validation_ref",
            "campaign_fact_validation_ref", "fact_refs", "style_profile_id",
            "style_profile_hash", "style_exception_ref",
        }
        if set(payload) - allowed:
            raise InteractionSessionError("unknown current-page plan fields")
        return cls(**payload)

    @property
    def planned_action_count(self) -> int:
        return 0 if self.branch is InteractionBranch.READ_ONLY_SKIP else 1

    def content_evidence_blockers(self) -> tuple[str, ...]:
        """Return fail-closed evidence blockers for the exact text about to be written."""
        if self.branch not in {
            InteractionBranch.NOTE_ENGAGEMENT,
            InteractionBranch.COMMENT_ENGAGEMENT,
        }:
            return ()
        blockers: list[str] = []
        content_hash = sha256(self.text.encode("utf-8")).hexdigest()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.message_plan_id) is None:
            blockers.append("message_plan_id_missing_or_invalid")
        if self.message_content_hash != content_hash:
            blockers.append("message_content_hash_mismatch")
        expected_source = f"message:{self.message_plan_id}:{self.message_content_hash}"
        if self.source_context_ref != expected_source:
            blockers.append("message_source_context_mismatch")
        expected_message_validation = (
            f"message-validation:{self.message_plan_id}:{self.message_content_hash}"
        )
        if self.message_validation_ref != expected_message_validation:
            blockers.append("message_validation_evidence_mismatch")
        expected_fact_validation = (
            f"campaign-facts:{self.campaign_id}:{self.message_plan_id}"
        )
        if self.campaign_fact_validation_ref != expected_fact_validation:
            blockers.append("campaign_fact_validation_evidence_mismatch")
        if any(
            not isinstance(item, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item) is None
            for item in self.fact_refs
        ):
            blockers.append("campaign_fact_refs_invalid")
        profile_bound = bool(self.style_profile_id.strip() or self.style_profile_hash.strip())
        exception_bound = bool(self.style_exception_ref.strip())
        if profile_bound:
            if re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.style_profile_id
            ) is None or re.fullmatch(r"[0-9a-f]{64}", self.style_profile_hash) is None:
                blockers.append("style_profile_evidence_invalid")
            if exception_bound:
                blockers.append("style_profile_and_exception_are_mutually_exclusive")
        elif not exception_bound:
            blockers.append("style_profile_or_approved_exception_required")
        elif re.fullmatch(
            r"style-exception-approved:[A-Za-z0-9][A-Za-z0-9_.:-]{2,255}",
            self.style_exception_ref,
        ) is None:
            blockers.append("style_exception_evidence_invalid")
        return tuple(blockers)


class CurrentPagePort(Protocol):
    def page_context(self) -> dict[str, Any]: ...
    def like_current_feed(self, note_id: str) -> dict[str, Any]: ...
    def post_comment_current(self, note_id: str, text: str) -> dict[str, Any]: ...
    def like_current_comment_bound(
        self, note_id: str, comment_id: str, *, target_context_hash: str
    ) -> dict[str, Any]: ...
    def reply_current_comment_bound(
        self, note_id: str, text: str, *, comment_id: str, target_context_hash: str
    ) -> dict[str, Any]: ...


class ReadonlySearchPort(Protocol):
    def bind_active_xhs_tab(self) -> dict[str, Any]: ...
    def search_feeds_visible(self, keyword: str) -> dict[str, Any]: ...
    def adopt_current_search_results(self, keyword: str) -> dict[str, Any]: ...
    def page_context(self) -> dict[str, Any]: ...


def prepare_readonly_search_session(
    *, port: ReadonlySearchPort, query: str
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Bind one healthy tab, search once, and verify the same result-page context."""
    query = query.strip()
    if not query or "\ufffd" in query or set(query) == {"?"}:
        raise InteractionSessionError("query failed Unicode validation")
    binding = port.bind_active_xhs_tab()
    risks = binding.get("riskSignals", [])
    bound_tab_id = binding.get("boundTabId")
    if type(bound_tab_id) is not int or not isinstance(risks, list) or risks:
        raise InteractionSessionError(
            "healthy bound Xiaohongshu tab is required", risk_signals=risks
        )
    search = port.search_feeds_visible(query)
    return _verify_search_batch(
        port=port,
        query=query,
        binding=binding,
        search=search,
    )


def adopt_readonly_search_session(
    *, port: ReadonlySearchPort, query: str
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Persist an already visible query batch without submitting the query again."""
    query = query.strip()
    if not query or "\ufffd" in query or set(query) == {"?"}:
        raise InteractionSessionError("query failed Unicode validation")
    binding = port.bind_active_xhs_tab()
    risks = binding.get("riskSignals", [])
    bound_tab_id = binding.get("boundTabId")
    if type(bound_tab_id) is not int or not isinstance(risks, list) or risks:
        raise InteractionSessionError(
            "healthy bound Xiaohongshu tab is required", risk_signals=risks
        )
    search = port.adopt_current_search_results(query)
    return _verify_search_batch(
        port=port,
        query=query,
        binding=binding,
        search=search,
    )


def _verify_search_batch(
    *,
    port: ReadonlySearchPort,
    query: str,
    binding: dict[str, Any],
    search: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    if not isinstance(search, dict):
        raise InteractionSessionError("search result contract is invalid")
    feeds = search.get("feeds", [])
    context = port.page_context()
    if not isinstance(context, dict):
        raise InteractionSessionError("search page context is invalid")
    context = {
        **context,
        "search_normalized_from_ai": search.get("normalized_from_ai") is True,
        "adopted_current_query": search.get("adopted_current_query") is True,
    }
    if not isinstance(feeds, list) or context.get("pageType") != "search_results":
        raise InteractionSessionError("search session did not remain on results page")
    context_risks = context.get("riskSignals", [])
    if not isinstance(context_risks, list) or context_risks:
        raise InteractionSessionError(
            "search page contains risk or unknown state", risk_signals=context_risks
        )
    if str(context.get("query", "")) != query:
        raise InteractionSessionError("search session query readback mismatch")
    if context.get("boundTabId") != binding.get("boundTabId"):
        raise InteractionSessionError("search session changed the bound browser tab")
    candidate_ids: list[str] = []
    seen_candidate_ids: set[str] = set()
    for item in feeds:
        if not isinstance(item, dict):
            continue
        note_id = str(item.get("id", "")).strip()
        if (
            _SAFE_XHS_NOTE_ID_RE.fullmatch(note_id) is None
            or note_id in seen_candidate_ids
        ):
            continue
        seen_candidate_ids.add(note_id)
        candidate_ids.append(note_id)
    if not candidate_ids:
        raise InteractionSessionError("search returned no safe candidate ids")
    return binding, candidate_ids, context


@dataclass(frozen=True)
class CurrentPageExecutionResult:
    ok: bool
    stage: str
    action_record_ids: tuple[str, ...]
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "action_record_ids": list(self.action_record_ids),
            "blockers": list(self.blockers),
        }


class InteractionSessionStore:
    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.action_path = self.runtime_dir / "comment_flow" / "actions.jsonl"
        self.stop_path = self.runtime_dir / "comment_flow" / "STOP.json"
        self.session_dir = self.runtime_dir / "interaction_sessions"
        self.approval_path = self.session_dir / "approvals.jsonl"
        self.task_approval_path = self.session_dir / "task_plan_approvals.jsonl"
        self.unresolved_targets = UnresolvedTargetRegistry(self.runtime_dir)

    def request_stop(self, reason: str) -> None:
        write_json_atomic(self.stop_path, {"reason": reason, "writes_allowed": False})

    def session_path(self, session_id: str) -> Path:
        if not session_id.replace("_", "").replace("-", "").isalnum():
            raise InteractionSessionError("unsafe session_id")
        return self.session_dir / f"{session_id}.json"

    def update_session(self, plan: CurrentPageInteractionPlan, *, stage: str, status: str) -> None:
        path = self.session_path(plan.session_id)
        existing: dict[str, Any] = {}
        if path.is_file():
            loaded = read_json(path)
            if isinstance(loaded, dict):
                existing = loaded
        existing.update(
            {
                "schema_version": 1,
                "session_id": plan.session_id,
                "plan_id": plan.plan_id,
                "note_id": plan.note_id,
                "branch": plan.branch.value,
                "stage": stage,
                "status": status,
            }
        )
        write_json_atomic(
            path,
            existing,
        )

    def start_session(
        self,
        *,
        session_id: str,
        account_id: str,
        query: str,
        candidate_ids: list[str],
        bound_tab_id: int,
        navigation_count: dict[str, Any],
        campaign_id: str = "",
        query_id: str = "",
        run_id: str = "",
        strategy_pack_id: str = "",
        strategy_pack_hash: str = "",
        strategy_approval_hash: str = "",
        session_origin: str = "visible_search",
        search_count: int = 1,
        search_normalized_from_ai: bool = False,
    ) -> None:
        metric_binding = (campaign_id, query_id, run_id)
        if any(metric_binding) and not all(metric_binding):
            raise InteractionSessionError(
                "campaign_id, query_id, and run_id are required together for metrics"
            )
        if any(
            value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}", value) is None
            for value in metric_binding
        ):
            raise InteractionSessionError("session metrics binding is invalid")
        strategy_binding = (
            strategy_pack_id,
            strategy_pack_hash,
            strategy_approval_hash,
        )
        if any(strategy_binding) and not all(strategy_binding):
            raise InteractionSessionError(
                "strategy_pack_id, strategy_pack_hash, and strategy_approval_hash are required together"
            )
        if any(strategy_binding) and not all(metric_binding):
            raise InteractionSessionError(
                "StrategyPack session requires campaign_id, query_id, and run_id"
            )
        if strategy_pack_id and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", strategy_pack_id
        ) is None:
            raise InteractionSessionError("session strategy_pack_id is invalid")
        if any(
            value and re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (strategy_pack_hash, strategy_approval_hash)
        ):
            raise InteractionSessionError("session strategy hashes are invalid")
        write_json_atomic(
            self.session_path(session_id),
            {
                "schema_version": 1,
                "session_id": session_id,
                "account_id": account_id,
                "query": query,
                "campaign_id": campaign_id,
                "query_id": query_id,
                "run_id": run_id,
                "strategy_pack_id": strategy_pack_id,
                "strategy_pack_hash": strategy_pack_hash,
                "strategy_approval_hash": strategy_approval_hash,
                "candidate_ids": candidate_ids,
                "bound_tab_id": bound_tab_id,
                "session_origin": session_origin,
                "search_count": search_count,
                "search_normalized_from_ai": search_normalized_from_ai,
                "next_index": 0,
                "stage": "search_results",
                "status": "active",
                "navigation_count": navigation_count,
            },
        )

    def mark_current_note(
        self,
        *,
        session_id: str,
        result_index: int,
        note_id: str,
        navigation_count: dict[str, Any],
    ) -> None:
        state = self.load_session(session_id)
        if not state:
            raise InteractionSessionError("session does not exist")
        candidate_ids = state.get("candidate_ids")
        expected_index = state.get("next_index")
        if (
            not isinstance(candidate_ids, list)
            or type(expected_index) is not int
            or result_index != expected_index
            or result_index < 0
            or result_index >= len(candidate_ids)
            or str(candidate_ids[result_index]) != note_id
        ):
            raise InteractionSessionError(
                "current note must be the exact next saved candidate"
            )
        state.update(
            {
                "result_index": result_index,
                "note_id": note_id,
                "next_index": result_index + 1,
                "stage": "note_read",
                "status": "prepared",
                "navigation_count": navigation_count,
            }
        )
        write_json_atomic(self.session_path(session_id), state)

    def rearm_current_note(
        self,
        session_id: str,
        *,
        navigation_count: dict[str, Any],
    ) -> dict[str, Any]:
        """Prepare another exact target on the same still-visible note."""
        state = self.load_session(session_id)
        if not state or not str(state.get("note_id", "")).strip():
            raise InteractionSessionError("session has no current note to continue")
        status = state.get("status")
        reconciled_unknown = (
            status == "unknown"
            and not self.unresolved_targets.is_unresolved(str(state.get("note_id", "")))
        )
        safe_not_dispatched = self._safe_not_dispatched_recovery(state)
        if (
            status not in {"verified", "completed", "prepared"}
            and not reconciled_unknown
            and not safe_not_dispatched
        ):
            raise InteractionSessionError("session is not ready to continue the current note")
        previous = state.get("navigation_count", {})
        if not isinstance(previous, dict) or not isinstance(navigation_count, dict):
            raise InteractionSessionError("navigation count is unavailable")
        if previous.get("forward") != navigation_count.get("forward"):
            raise InteractionSessionError("current note navigation changed before continuation")
        state.update({
            "stage": "note_read",
            "status": "prepared",
            "navigation_count": navigation_count,
        })
        write_json_atomic(self.session_path(session_id), state)
        return state

    def _safe_not_dispatched_recovery(self, state: Mapping[str, Any]) -> bool:
        """Allow only an exact, explicitly retryable pre-dispatch recovery.

        A blocked session is not generally recoverable. The narrow exception
        is a result that the unified write boundary recorded as
        ``not_dispatched`` with ``do_not_retry=false`` for the exact approved
        plan. Unknown, verified, muted/risk, or mismatched plans remain closed.
        """
        if state.get("status") != "blocked" or state.get("stage") != "pre_action":
            return False
        if self.unresolved_targets.is_unresolved(str(state.get("note_id", ""))):
            return False
        stop = read_json(self.stop_path, default=None)
        if not isinstance(stop, Mapping):
            return False
        if stop.get("writes_allowed") is not False:
            return False
        if stop.get("requires_manual_reconciliation") is True:
            return False
        approvals = [
            row for row in read_jsonl(self.approval_path)
            if row.get("session_id") == state.get("session_id")
            and row.get("plan_id") == state.get("plan_id")
            and row.get("note_id") == state.get("note_id")
        ]
        if not approvals:
            return False
        plan_hash = approvals[-1].get("plan_hash")
        if not isinstance(plan_hash, str) or re.fullmatch(r"[0-9a-f]{64}", plan_hash) is None:
            return False
        results_path = self.runtime_dir / "action_preflight" / "results.jsonl"
        matching = [
            row for row in read_jsonl(results_path)
            if row.get("plan_hash") == plan_hash
            and row.get("account_id") == state.get("account_id")
        ]
        if not matching:
            return False
        latest = matching[-1]
        return (
            latest.get("status") == "not_dispatched"
            and latest.get("do_not_retry") is False
            and latest.get("platform_actions_executed") == 0
            and latest.get("reason_code")
            in {
                "comment_editor_pre_dispatch_failure",
                "comment_editor_unavailable",
                "comment_editor_not_visible",
                "comment_editor_readback_mismatch",
                "comment_editor_debugger_unavailable",
                "comment_editor_bridge_timeout",
            }
        )

    def mark_search_results(self, session_id: str, navigation_count: dict[str, Any]) -> None:
        state = self.load_session(session_id)
        if not state:
            raise InteractionSessionError("session does not exist")
        state.update(
            {
                "stage": "search_results",
                "status": "active",
                "navigation_count": navigation_count,
            }
        )
        write_json_atomic(self.session_path(session_id), state)

    def mark_risk_stopped(
        self, session_id: str, *, event_code: str, occurred_at: str
    ) -> None:
        state = self.load_session(session_id)
        if not state:
            return
        state.update(
            {
                "stage": "risk_stopped",
                "status": "stopped",
                "last_event_code": event_code,
                "stopped_at": occurred_at,
            }
        )
        write_json_atomic(self.session_path(session_id), state)

    def mark_candidate_skipped(
        self,
        session_id: str,
        *,
        result_index: int,
        note_id: str,
        reason_code: str,
    ) -> None:
        state = self.load_session(session_id)
        if not state:
            raise InteractionSessionError("session does not exist")
        candidate_ids = state.get("candidate_ids")
        expected_index = state.get("next_index")
        if (
            not isinstance(candidate_ids, list)
            or type(expected_index) is not int
            or result_index != expected_index
            or result_index < 0
            or result_index >= len(candidate_ids)
            or str(candidate_ids[result_index]) != note_id
        ):
            raise InteractionSessionError(
                "skipped candidate must be the exact next saved candidate"
            )
        skipped = state.get("skipped_candidates", [])
        if not isinstance(skipped, list):
            skipped = []
        skipped.append(
            {
                "result_index": result_index,
                "note_id": note_id,
                "reason_code": reason_code,
            }
        )
        state.update(
            {
                "skipped_candidates": skipped,
                "next_index": max(int(state.get("next_index", 0) or 0), result_index + 1),
                "stage": "search_results",
                "status": "active",
            }
        )
        write_json_atomic(self.session_path(session_id), state)

    def mark_search_exhausted(self, session_id: str, *, exhausted_at: str) -> None:
        state = self.load_session(session_id)
        candidate_ids = state.get("candidate_ids")
        next_index = state.get("next_index")
        if (
            not isinstance(candidate_ids, list)
            or type(next_index) is not int
            or next_index < len(candidate_ids)
        ):
            raise InteractionSessionError("search batch is not exhausted")
        state.update({
            "stage": "search_exhausted",
            "status": "completed",
            "exhausted_at": exhausted_at,
        })
        write_json_atomic(self.session_path(session_id), state)

    def load_session(self, session_id: str) -> dict[str, Any]:
        value = read_json(self.session_path(session_id))
        return value if isinstance(value, dict) else {}

    def append_action(self, record: ActionRecord) -> None:
        append_jsonl(self.action_path, record.to_dict())

    @staticmethod
    def plan_hash(plan: CurrentPageInteractionPlan) -> str:
        payload = json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()

    def record_approval(
        self,
        plan: CurrentPageInteractionPlan,
        *,
        confirmed_at: str,
        confirmation: str,
    ) -> str:
        if confirmation != CURRENT_PAGE_APPROVAL_CONFIRMATION:
            raise InteractionSessionError("exact current-page approval confirmation is required")
        evidence_blockers = plan.content_evidence_blockers()
        if evidence_blockers:
            raise InteractionSessionError(
                "current-page text evidence is incomplete: " + ",".join(evidence_blockers)
            )
        digest = self.plan_hash(plan)
        append_jsonl(
            self.approval_path,
            {
                "approval_ref": plan.approval_ref,
                "plan_id": plan.plan_id,
                "session_id": plan.session_id,
                "account_id": plan.account_id,
                "note_id": plan.note_id,
                "plan_hash": digest,
                "confirmed_at": confirmed_at,
            },
        )
        return digest

    def approval_matches(self, plan: CurrentPageInteractionPlan) -> bool:
        digest = self.plan_hash(plan)
        return any(
            item.get("approval_ref") == plan.approval_ref
            and item.get("plan_id") == plan.plan_id
            and item.get("session_id") == plan.session_id
            and item.get("account_id") == plan.account_id
            and item.get("note_id") == plan.note_id
            and item.get("plan_hash") == digest
            for item in read_jsonl(self.approval_path)
        )

    def record_task_plan_approval(
        self,
        plan: CurrentPageInteractionPlan,
        *,
        task_id: str,
        authorization_id: str,
        task_hash: str,
        confirmed_at: str,
    ) -> str:
        evidence_blockers = plan.content_evidence_blockers()
        if evidence_blockers:
            raise InteractionSessionError(
                "task-bound text evidence is incomplete: " + ",".join(evidence_blockers)
            )
        digest = self.plan_hash(plan)
        append_jsonl(
            self.task_approval_path,
            {
                "plan_id": plan.plan_id,
                "plan_hash": digest,
                "session_id": plan.session_id,
                "account_id": plan.account_id,
                "campaign_id": plan.campaign_id,
                "note_id": plan.note_id,
                "task_id": task_id,
                "authorization_id": authorization_id,
                "task_hash": task_hash,
                "confirmed_at": confirmed_at,
            },
        )
        return digest

    def task_plan_approval_matches(
        self,
        plan: CurrentPageInteractionPlan,
        *,
        task_id: str,
        authorization_id: str,
        task_hash: str,
    ) -> bool:
        digest = self.plan_hash(plan)
        return any(
            item.get("plan_id") == plan.plan_id
            and item.get("plan_hash") == digest
            and item.get("session_id") == plan.session_id
            and item.get("account_id") == plan.account_id
            and item.get("campaign_id") == plan.campaign_id
            and item.get("note_id") == plan.note_id
            and item.get("task_id") == task_id
            and item.get("authorization_id") == authorization_id
            and item.get("task_hash") == task_hash
            for item in read_jsonl(self.task_approval_path)
        )

    def execution_gate(
        self,
        *,
        plan: CurrentPageInteractionPlan,
        checked_at: str,
        planned_action_count: int,
        daily_limit: int,
        minimum_interval_seconds: int,
        action_type_limits: Mapping[str, int] | None = None,
        daily_target_limit: int | None = None,
        budget_timezone: str = "UTC",
    ) -> tuple[tuple[str, ...], int]:
        if planned_action_count != 1 or plan.planned_action_count != 1:
            raise InteractionSessionError(
                "one current-page plan must contain exactly one platform write"
            )
        if type(minimum_interval_seconds) is not int or minimum_interval_seconds < 600:
            raise InteractionSessionError(
                "minimum_interval_seconds must be at least 600"
            )
        try:
            budget_zone = ZoneInfo(budget_timezone)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise InteractionSessionError("budget_timezone is not recognized") from exc
        now = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        if now.tzinfo is None or now.utcoffset() is None:
            raise InteractionSessionError("checked_at must include timezone")
        now = now.astimezone(timezone.utc)
        budget_day = now.astimezone(budget_zone).date()
        accounted = [
            ActionRecord.from_dict(item)
            for item in read_jsonl(self.action_path)
            if item.get("account_id") == plan.account_id
            and item.get("status") in {
                ActionStatus.VERIFIED.value,
                ActionStatus.UNKNOWN.value,
            }
        ]
        timed = [
            (
                item,
                datetime.fromisoformat(
                    str(
                        item.metadata.get("accounting_at", item.created_at)
                        if isinstance(item.metadata, dict)
                        else item.created_at
                    ).replace("Z", "+00:00")
                ).astimezone(timezone.utc),
            )
            for item in accounted
        ]
        moments = [moment for _, moment in timed]
        accounted_today = [
            item
            for item, moment in timed
            if moment.astimezone(budget_zone).date() == budget_day
        ]
        today_count = len(accounted_today)
        blockers: list[str] = []
        blockers.extend(plan.content_evidence_blockers())
        if daily_limit < 1 or today_count + planned_action_count > daily_limit:
            blockers.append("daily_action_budget_exceeded")
        if moments and now < max(moments) + timedelta(seconds=minimum_interval_seconds):
            blockers.append("minimum_target_interval_not_elapsed")
        if daily_target_limit is not None:
            target_ids = {
                str(item.metadata.get("note_id") or item.candidate_id)
                for item in accounted_today
                if isinstance(item.metadata, dict)
            }
            planned_new_target = 0 if plan.note_id in target_ids else 1
            if daily_target_limit < 1 or len(target_ids) + planned_new_target > daily_target_limit:
                blockers.append("daily_target_budget_exceeded")
        if action_type_limits is not None:
            planned_types: Counter[str] = Counter()
            if plan.branch in {
                InteractionBranch.NOTE_LIKE_ONLY,
                InteractionBranch.COMMENT_LIKE_ONLY,
            }:
                planned_types[ActionType.LIKE.value] += 1
            if plan.branch is InteractionBranch.NOTE_ENGAGEMENT:
                planned_types[ActionType.COMMENT.value] += 1
            elif plan.branch is InteractionBranch.COMMENT_ENGAGEMENT:
                planned_types[ActionType.REPLY.value] += 1
            today_types = Counter(item.action_type.value for item in accounted_today)
            for action_name, planned_count in planned_types.items():
                limit = action_type_limits.get(action_name)
                if type(limit) is not int or limit < 0 or today_types[action_name] + planned_count > limit:
                    blockers.append(f"daily_{action_name}_budget_exceeded")
        note_rows = [
            item for item in accounted
            if isinstance(item.metadata, dict)
            and item.metadata.get("note_id") == plan.note_id
        ]
        if plan.branch is InteractionBranch.NOTE_LIKE_ONLY and any(
            item.action_type is ActionType.LIKE
            and item.metadata.get("interaction_scope") == "note"
            for item in note_rows
        ):
            blockers.append("note_already_liked")
        if plan.branch is InteractionBranch.NOTE_ENGAGEMENT and any(
            item.action_type is ActionType.COMMENT
            and item.metadata.get("interaction_scope") == "note"
            for item in note_rows
        ):
            blockers.append("note_top_level_comment_already_posted")
        if plan.branch is InteractionBranch.COMMENT_ENGAGEMENT:
            reply_rows = [item for item in note_rows if item.action_type is ActionType.REPLY]
            if len(reply_rows) >= 3:
                blockers.append("note_reply_budget_exceeded")
            if any(
                item.metadata.get("target_comment_id") == plan.target_comment_id
                for item in reply_rows
            ):
                blockers.append("target_comment_already_replied")
        if plan.branch is InteractionBranch.COMMENT_LIKE_ONLY:
            like_rows = [
                item for item in note_rows
                if item.action_type is ActionType.LIKE
                and item.metadata.get("interaction_scope") == "comment"
            ]
            if len(like_rows) >= 3:
                blockers.append("note_comment_like_budget_exceeded")
            if any(
                item.metadata.get("target_comment_id") == plan.target_comment_id
                for item in like_rows
            ):
                blockers.append("target_comment_already_liked")
        return tuple(blockers), today_count


def _context_blockers(context: dict[str, Any], note_id: str) -> tuple[str, ...]:
    risks = context.get("riskSignals", [])
    if not isinstance(risks, list):
        return ("risk_state_unknown",)
    if risks:
        return tuple(f"risk:{item}" for item in risks)
    if context.get("pageType") != "note_detail":
        return ("current_page_not_note_detail",)
    if str(context.get("noteId", "")) != note_id:
        return ("current_note_mismatch",)
    return ()


def _forward_navigation_count(context: dict[str, Any]) -> int | None:
    value = context.get("navigationCount", {})
    if not isinstance(value, dict) or type(value.get("forward")) is not int:
        return None
    return value["forward"]


def _event_timestamp(
    clock: Callable[[], str], *, not_before: str, field: str
) -> str:
    try:
        floor = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
        value = datetime.fromisoformat(clock().replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InteractionSessionError(f"{field} clock must return ISO-8601") from exc
    if floor.tzinfo is None or floor.utcoffset() is None:
        raise InteractionSessionError("created_at must include timezone")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InteractionSessionError(f"{field} must include timezone")
    return max(value.astimezone(timezone.utc), floor.astimezone(timezone.utc)).isoformat()


def _record(
    *,
    plan: CurrentPageInteractionPlan,
    run_id: str,
    gate_checked_at: str,
    action_started_at: str,
    submit_completed_at: str | None,
    accounting_at: str,
    verified_at: str | None,
    unknown_at: str | None,
    operation_trace_ref: str,
    action_type: ActionType,
    scope: str,
    status: ActionStatus,
    prior_daily_count: int,
    daily_limit: int,
    minimum_interval_seconds: int,
    text: str | None = None,
    result_ref: str | None = None,
    error_code: str | None = None,
    task_id: str | None = None,
    authorization_id: str | None = None,
    task_hash: str | None = None,
) -> ActionRecord:
    metadata = {
        "interaction_scope": scope,
        "branch": plan.branch.value,
        "note_id": plan.note_id,
        "target_comment_id": plan.target_comment_id,
        "target_context_hash": plan.target_context_hash,
        "approval_ref": plan.approval_ref,
        "action_started_at": action_started_at,
        "submit_completed_at": submit_completed_at,
        "verified_at": verified_at,
        "unknown_at": unknown_at,
        "accounting_at": accounting_at,
        "next_eligible_at": (
            datetime.fromisoformat(accounting_at.replace("Z", "+00:00"))
            + timedelta(seconds=minimum_interval_seconds)
        ).isoformat(),
        "operation_trace_ref": operation_trace_ref,
        "message_plan_id": plan.message_plan_id,
        "message_content_hash": plan.message_content_hash,
        "message_validation_ref": plan.message_validation_ref,
        "campaign_fact_validation_ref": plan.campaign_fact_validation_ref,
        "fact_refs": list(plan.fact_refs),
        "style_profile_id": plan.style_profile_id,
        "style_profile_hash": plan.style_profile_hash,
        "style_exception_ref": plan.style_exception_ref,
    }
    if task_id is not None:
        metadata.update(
            {
                "task_id": task_id,
                "authorization_id": authorization_id,
                "task_hash": task_hash,
            }
        )
    return ActionRecord(
        record_id=new_id("action"),
        run_id=run_id,
        campaign_id=plan.campaign_id,
        account_id=plan.account_id,
        candidate_id=plan.candidate_id,
        interaction_plan_id=plan.plan_id,
        action_type=action_type,
        run_mode=RunMode.SMOKE,
        status=status,
        created_at=accounting_at,
        source_context_ref=plan.source_context_ref,
        text_source=TextSource.NONE if action_type is ActionType.LIKE else TextSource.APPROVED_DRAFT,
        output_text=text,
        result_ref=result_ref,
        error_code=error_code,
        validator=ValidatorDecision(
            True,
            gate_checked_at,
            reason_codes=(
                ("exact_target_context_bound",)
                if action_type is ActionType.LIKE
                else (
                    "message_plan_content_hash_verified",
                    "campaign_fact_validation_bound",
                    "style_profile_or_exception_bound",
                )
            ),
            fact_refs=(
                (plan.source_context_ref,)
                if action_type is ActionType.LIKE
                else plan.fact_refs
            ),
        ),
        risk=RiskDecision(True, RiskLevel.LOW, gate_checked_at),
        throttle=ThrottleDecision(
            True,
            gate_checked_at,
            gate_checked_at,
            prior_daily_count,
            daily_limit,
            minimum_interval_seconds,
            reason_codes=("account_global_write_interval_elapsed",),
        ),
        metadata=metadata,
    )


def execute_current_page_plan(
    *,
    plan: CurrentPageInteractionPlan,
    port: CurrentPagePort,
    store: InteractionSessionStore,
    run_id: str,
    created_at: str,
    daily_limit: int = 10,
    minimum_interval_seconds: int = 600,
    action_type_limits: Mapping[str, int] | None = None,
    daily_target_limit: int | None = None,
    budget_timezone: str = "UTC",
    task_id: str | None = None,
    authorization_id: str | None = None,
    task_hash: str | None = None,
    event_clock: Callable[[], str] | None = None,
) -> CurrentPageExecutionResult:
    if type(minimum_interval_seconds) is not int or minimum_interval_seconds < 600:
        raise InteractionSessionError(
            "minimum_interval_seconds must be at least 600"
        )
    task_binding = (task_id, authorization_id, task_hash)
    if any(item is not None for item in task_binding):
        if not all(isinstance(item, str) and item.strip() for item in task_binding):
            raise InteractionSessionError("task execution metadata is incomplete")
        if re.fullmatch(r"[0-9a-f]{64}", str(task_hash)) is None:
            raise InteractionSessionError("task_hash must be SHA-256 hex")
    store.update_session(plan, stage="preflight", status="running")
    if plan.branch is InteractionBranch.READ_ONLY_SKIP:
        store.update_session(plan, stage="completed", status="skipped")
        return CurrentPageExecutionResult(True, "completed", ())
    fixture_adapter = (
        getattr(port, "fixture_only_execution", False) is True
        and not type(port).__module__.startswith("xhs_operations_core.")
    )
    stop = read_json(store.stop_path, default=None)
    if fixture_adapter:
        if stop is not None:
            return CurrentPageExecutionResult(
                False, "preflight", (), ("operator_stop_requested",)
            )
    else:
        plan_hash = store.plan_hash(plan)
        try:
            valid_until = datetime.fromisoformat(
                str((stop or {}).get("valid_until", "")).replace("Z", "+00:00")
            )
            execution_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            lease_time_valid = (
                valid_until.tzinfo is not None
                and valid_until.utcoffset() is not None
                and execution_time.tzinfo is not None
                and execution_time.utcoffset() is not None
                and execution_time <= valid_until
            )
        except (TypeError, ValueError):
            lease_time_valid = False
        exact_lease = (
            isinstance(stop, dict)
            and stop.get("schema_version") == 2
            and stop.get("writes_allowed") is True
            and stop.get("reason") == "exact_bounded_write_lease_active"
            and isinstance(stop.get("active_lease_id"), str)
            and str(stop.get("active_lease_id")).startswith("write_lease_")
            and stop.get("session_id") == plan.session_id
            and stop.get("target_ref_hash")
            == sha256(plan.note_id.encode("utf-8")).hexdigest()
            and stop.get("plan_hash") == plan_hash
            and stop.get("requires_manual_reconciliation") is False
            and lease_time_valid
        )
        if not exact_lease:
            return CurrentPageExecutionResult(
                False, "preflight", (), ("exact_active_write_lease_required",)
            )
    if plan.branch is InteractionBranch.NOTE_LIKE_ONLY:
        spec = (ActionType.LIKE, "note", None)
    elif plan.branch is InteractionBranch.COMMENT_LIKE_ONLY:
        spec = (ActionType.LIKE, "comment", None)
    elif plan.branch is InteractionBranch.NOTE_ENGAGEMENT:
        spec = (ActionType.COMMENT, "note", plan.text)
    else:
        spec = (ActionType.REPLY, "comment", plan.text)

    gate_blockers, prior_daily_count = store.execution_gate(
        plan=plan,
        checked_at=created_at,
        planned_action_count=1,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        action_type_limits=action_type_limits,
        daily_target_limit=daily_target_limit,
        budget_timezone=budget_timezone,
    )
    if gate_blockers:
        store.update_session(plan, stage="preflight", status="blocked")
        return CurrentPageExecutionResult(False, "preflight", (), gate_blockers)

    action_type, scope, text = spec
    before_context = port.page_context()
    blockers = _context_blockers(before_context, plan.note_id)
    baseline_forward = _forward_navigation_count(before_context)
    if baseline_forward is None:
        blockers = (*blockers, "navigation_count_unknown")
    if blockers:
        store.request_stop("current_page_pre_action_blocked")
        store.update_session(plan, stage="frozen", status="blocked")
        return CurrentPageExecutionResult(False, "pre_action", (), blockers)

    clock = event_clock or utc_now_iso
    action_started_at = _event_timestamp(
        clock, not_before=created_at, field="action_started_at"
    )
    submit_completed_at: str | None = None
    operation_trace_ref = (
        f"run_agent_trace:{run_id}:{plan.plan_id}:{action_type.value}"
    )
    try:
        if action_type is ActionType.LIKE and scope == "note":
            result = port.like_current_feed(plan.note_id)
        elif action_type is ActionType.LIKE:
            result = port.like_current_comment_bound(
                plan.note_id,
                plan.target_comment_id,
                target_context_hash=plan.target_context_hash,
            )
        elif action_type is ActionType.COMMENT:
            result = port.post_comment_current(plan.note_id, plan.text)
        else:
            result = port.reply_current_comment_bound(
                plan.note_id,
                plan.text,
                comment_id=plan.target_comment_id,
                target_context_hash=plan.target_context_hash,
            )
        if isinstance(result, dict) and result.get("actionDispatched") is False:
            failure_code = str(result.get("failureCode") or "platform_action_not_dispatched")
            store.update_session(plan, stage="pre_action", status="blocked")
            return CurrentPageExecutionResult(
                False, "pre_action", (), (failure_code,)
            )
        submit_completed_at = _event_timestamp(
            clock, not_before=action_started_at, field="submit_completed_at"
        )
        if isinstance(result, dict):
            returned_trace = result.get("operationTraceRef") or result.get("traceRef")
            if isinstance(returned_trace, str) and returned_trace.strip():
                operation_trace_ref = returned_trace.strip()
        if not isinstance(result, dict) or result.get("success") is not True:
            raise RuntimeError("visible_result_unverified")
        after_context = port.page_context()
        after_blockers = _context_blockers(after_context, plan.note_id)
        after_forward = _forward_navigation_count(after_context)
        if after_blockers or after_forward != baseline_forward:
            raise RuntimeError("current_page_changed_during_action")
        verified_at = _event_timestamp(
            clock, not_before=submit_completed_at, field="verified_at"
        )
        result_ref = f"run_agent_current_page:{plan.note_id}:{action_type.value}"
        record = _record(
            plan=plan,
            run_id=run_id,
            gate_checked_at=created_at,
            action_started_at=action_started_at,
            submit_completed_at=submit_completed_at,
            accounting_at=verified_at,
            verified_at=verified_at,
            unknown_at=None,
            operation_trace_ref=operation_trace_ref,
            action_type=action_type,
            scope=scope,
            status=ActionStatus.VERIFIED,
            prior_daily_count=prior_daily_count,
            daily_limit=daily_limit,
            minimum_interval_seconds=minimum_interval_seconds,
            text=text,
            result_ref=result_ref,
            task_id=task_id,
            authorization_id=authorization_id,
            task_hash=task_hash,
        )
        store.append_action(record)
        store.update_session(plan, stage="action_verified", status="running")
    except Exception as exc:
        unknown_at = _event_timestamp(
            clock,
            not_before=submit_completed_at or action_started_at,
            field="unknown_at",
        )
        error_code = str(getattr(exc, "failure_code", "")).strip() or (
            type(exc).__name__ or "unknown_action_error"
        )
        record = _record(
            plan=plan,
            run_id=run_id,
            gate_checked_at=created_at,
            action_started_at=action_started_at,
            submit_completed_at=submit_completed_at,
            accounting_at=unknown_at,
            verified_at=None,
            unknown_at=unknown_at,
            operation_trace_ref=operation_trace_ref,
            action_type=action_type,
            scope=scope,
            status=ActionStatus.UNKNOWN,
            prior_daily_count=prior_daily_count,
            daily_limit=daily_limit,
            minimum_interval_seconds=minimum_interval_seconds,
            text=text,
            error_code=error_code,
            task_id=task_id,
            authorization_id=authorization_id,
            task_hash=task_hash,
        )
        store.append_action(record)
        if _SAFE_XHS_NOTE_ID_RE.fullmatch(plan.note_id):
            store.unresolved_targets.record(
                note_id=plan.note_id,
                reason_code="unknown_write_result",
                recorded_at=unknown_at,
                source_ref="current_page_plan",
            )
        store.request_stop("unknown_current_page_action_result")
        store.update_session(plan, stage="frozen", status="unknown")
        return CurrentPageExecutionResult(
            False, "action_unknown", (record.record_id,), (error_code,)
        )

    store.update_session(plan, stage="completed", status="verified")
    return CurrentPageExecutionResult(True, "completed", (record.record_id,))
