"""Fail-closed single top-level note-comment workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
import re

from xhs_operations_core.contracts import (
    ActionRecord, ActionStatus, ActionType, RiskDecision, RiskLevel, RunMode,
    TextSource, ThrottleDecision, ValidatorDecision, new_id,
)
from xhs_operations_core.source_notes import NoteDetailCapture
from xhs_operations_core.storage import append_jsonl, read_jsonl

from .comment_flow import REPLY_RED_LINES, UiActionResult


NOTE_COMMENT_CONFIRMATION = "I_CONFIRM_SINGLE_TOP_LEVEL_NOTE_COMMENT_TEST"
ACTION_LOG = Path("comment_flow") / "actions.jsonl"
STOP_FILE = Path("comment_flow") / "STOP.json"


class NoteCommentError(ValueError):
    pass


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NoteCommentError(f"{name} must be a non-empty string")
    return " ".join(value.split())


def _safe_id(name: str, value: object) -> str:
    text = _text(name, value)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text) is None:
        raise NoteCommentError(f"{name} must be a safe id")
    return text


def note_context_hash(note: NoteDetailCapture) -> str:
    value = "\n".join((note.note_id, note.title, note.body, *note.hashtags, *note.image_text))
    return sha256(value.encode("utf-8")).hexdigest()


def _moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NoteCommentError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class NoteCommentPlan:
    plan_id: str
    campaign_id: str
    account_id: str
    candidate_id: str
    query: str
    result_index: int
    note_id: str
    note_title: str
    note_context_hash: str
    comment_text: str
    approval_ref: str
    approved_at: str

    def __post_init__(self) -> None:
        for name in ("plan_id", "campaign_id", "account_id", "candidate_id", "note_id"):
            object.__setattr__(self, name, _safe_id(name, getattr(self, name)))
        for name in ("query", "note_title", "comment_text", "approval_ref"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        _moment(self.approved_at)
        if type(self.result_index) is not int or self.result_index < 0:
            raise NoteCommentError("result_index must be non-negative")
        if re.fullmatch(r"[0-9a-f]{64}", self.note_context_hash) is None:
            raise NoteCommentError("note_context_hash must be SHA-256 hex")
        if not 6 <= len(self.comment_text) <= 80:
            raise NoteCommentError("comment_text must contain 6-80 characters")
        hits = [term for term in REPLY_RED_LINES if term in self.comment_text]
        if hits:
            raise NoteCommentError("comment_text violates public-comment red lines")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NoteCommentPlan":
        try:
            return cls(**{name: value[name] for name in cls.__dataclass_fields__})
        except KeyError as exc:
            raise NoteCommentError(f"missing note comment plan field: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def matches(self, note: NoteDetailCapture) -> bool:
        return (
            note.note_id == self.note_id
            and note.title == self.note_title
            and note_context_hash(note) == self.note_context_hash
        )


@dataclass(frozen=True)
class NoteCommentGate:
    platform_access_allowed: bool
    login_ready: bool
    stop_requested: bool
    daily_budget_remaining: int
    minimum_interval_elapsed: bool
    duplicate_comment: bool

    def blockers(self) -> tuple[str, ...]:
        rows = []
        if not self.platform_access_allowed: rows.append("platform_access_disabled")
        if not self.login_ready: rows.append("login_not_ready")
        if self.stop_requested: rows.append("operator_stop_requested")
        if self.daily_budget_remaining < 1: rows.append("insufficient_action_budget")
        if not self.minimum_interval_elapsed: rows.append("minimum_interval_not_elapsed")
        if self.duplicate_comment: rows.append("duplicate_note_comment")
        return tuple(rows)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": not self.blockers(), "blockers": list(self.blockers()), **self.__dict__}


class NoteCommentPort(Protocol):
    def open_home_and_search(self, keyword: str) -> dict[str, object]: ...
    def open_one_result(self, *, index: int) -> dict[str, object]: ...
    def read_current_note_detail(self, *, expected_note_id: str, captured_at: str) -> NoteDetailCapture: ...
    def current_risk_signals(self) -> tuple[str, ...]: ...
    def comment_current_note(self, *, note_id: str, comment_text: str) -> UiActionResult: ...


@dataclass(frozen=True)
class NoteCommentResult:
    ok: bool
    stage: str
    blockers: tuple[str, ...]
    action: UiActionResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "stage": self.stage, "blockers": list(self.blockers), "action": None if self.action is None else self.action.to_dict()}


def execute_note_comment(*, plan: NoteCommentPlan, gate: NoteCommentGate, port: NoteCommentPort, captured_at: str) -> NoteCommentResult:
    if gate.blockers():
        return NoteCommentResult(False, "preflight", gate.blockers())
    port.open_home_and_search(plan.query)
    opened = port.open_one_result(index=plan.result_index)
    if opened.get("note_id") != plan.note_id:
        return NoteCommentResult(False, "candidate_match", ("opened_note_mismatch",))
    if port.current_risk_signals():
        return NoteCommentResult(False, "before_note_read", ("risk_signal",))
    note = port.read_current_note_detail(expected_note_id=plan.note_id, captured_at=captured_at)
    if not plan.matches(note):
        return NoteCommentResult(False, "note_match", ("note_context_mismatch",))
    action = port.comment_current_note(note_id=plan.note_id, comment_text=plan.comment_text)
    if not action.verified:
        return NoteCommentResult(False, "note_comment", ("note_comment_unverified",), action)
    return NoteCommentResult(True, "completed", (), action)


class NoteCommentStore:
    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.path = self.runtime_dir / ACTION_LOG

    def records(self) -> list[ActionRecord]:
        return [ActionRecord.from_dict(item) for item in read_jsonl(self.path)]

    def build_gate(self, *, plan: NoteCommentPlan, checked_at: str, login_ready: bool, platform_access_allowed: bool, daily_action_limit: int, minimum_target_interval_seconds: int) -> NoteCommentGate:
        now = _moment(checked_at)
        verified = [row for row in self.records() if row.account_id == plan.account_id and row.status is ActionStatus.VERIFIED]
        today = [row for row in verified if _moment(row.created_at).date() == now.date()]
        latest = max((_moment(row.created_at) for row in verified), default=None)
        duplicate = any(row.action_type is ActionType.COMMENT and row.candidate_id == plan.candidate_id and row.output_text == plan.comment_text for row in verified)
        return NoteCommentGate(platform_access_allowed, login_ready, (self.runtime_dir / STOP_FILE).exists(), max(0, daily_action_limit - len(today)), latest is None or now >= latest + timedelta(seconds=minimum_target_interval_seconds), duplicate)

    def append_verified(self, *, plan: NoteCommentPlan, result: NoteCommentResult, run_id: str, created_at: str, daily_action_limit: int, minimum_target_interval_seconds: int) -> ActionRecord:
        if not result.ok or result.action is None or not result.action.verified:
            raise NoteCommentError("only verified note comments can be persisted")
        record = ActionRecord(
            record_id=new_id("action"), run_id=run_id, campaign_id=plan.campaign_id,
            account_id=plan.account_id, candidate_id=plan.candidate_id,
            interaction_plan_id=plan.plan_id, action_type=ActionType.COMMENT,
            run_mode=RunMode.SMOKE, status=ActionStatus.VERIFIED, created_at=created_at,
            source_context_ref=f"note:{plan.note_id}:{plan.note_context_hash}",
            text_source=TextSource.APPROVED_DRAFT, output_text=plan.comment_text,
            result_ref=result.action.result_ref,
            validator=ValidatorDecision(True, created_at, fact_refs=(plan.note_context_hash,)),
            risk=RiskDecision(True, RiskLevel.LOW, created_at),
            throttle=ThrottleDecision(True, created_at, created_at, 0, daily_action_limit, minimum_target_interval_seconds),
            metadata={"interaction_scope": "note", "approval_ref": plan.approval_ref, "action_evidence": dict(result.action.evidence)},
        )
        append_jsonl(self.path, record.to_dict())
        return record
