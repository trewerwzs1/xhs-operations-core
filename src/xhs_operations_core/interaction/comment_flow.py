"""Fail-closed single-comment like-and-reply workflow."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol
import re


class CommentFlowContractError(ValueError):
    """Raised when a comment target or plan is ambiguous or unsafe."""


COMMENT_FLOW_SMOKE_CONFIRMATION = "I_CONFIRM_SINGLE_COMMENT_LIKE_AND_REPLY_TEST"


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommentFlowContractError(f"{name} must be a non-empty string")
    return " ".join(value.split())


def _safe_id(name: str, value: object) -> str:
    text = _text(name, value)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text) is None:
        raise CommentFlowContractError(f"{name} must be a safe id")
    return text


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


REPLY_RED_LINES = (
    "私信我",
    "私聊我",
    "加我微信",
    "加微信",
    "联系电话",
    "扫码",
    "保证报名",
    "保证名额",
)


@dataclass(frozen=True)
class CommentTarget:
    candidate_id: str
    target_comment_id: str
    note_id: str
    commenter: str
    full_text: str
    anchor_text: str
    context_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _safe_id("candidate_id", self.candidate_id))
        object.__setattr__(self, "target_comment_id", _safe_id("target_comment_id", self.target_comment_id))
        object.__setattr__(self, "note_id", _safe_id("note_id", self.note_id))
        object.__setattr__(self, "commenter", _text("commenter", self.commenter))
        object.__setattr__(self, "full_text", _text("full_text", self.full_text))
        object.__setattr__(self, "anchor_text", _text("anchor_text", self.anchor_text))
        if len(self.anchor_text) < 4 or len(self.anchor_text) > 32:
            raise CommentFlowContractError("anchor_text must contain 4-32 characters")
        if self.anchor_text not in self.full_text:
            raise CommentFlowContractError("anchor_text must be a substring of full_text")
        if re.fullmatch(r"[0-9a-f]{64}", self.context_hash) is None:
            raise CommentFlowContractError("context_hash must be SHA-256 hex")

    @classmethod
    def create(
        cls,
        *,
        candidate_id: str,
        target_comment_id: str,
        note_id: str,
        commenter: str,
        full_text: str,
        anchor_text: str,
    ) -> "CommentTarget":
        normalized = " ".join(full_text.split())
        context = f"{note_id}\n{target_comment_id}\n{commenter.strip()}\n{normalized}"
        return cls(
            candidate_id=candidate_id,
            target_comment_id=target_comment_id,
            note_id=note_id,
            commenter=commenter,
            full_text=normalized,
            anchor_text=anchor_text,
            context_hash=_digest(context),
        )

    def matches(self, snapshot: "CommentSnapshot") -> bool:
        return (
            self.note_id == snapshot.note_id
            and self.target_comment_id == snapshot.target_comment_id
            and self.commenter == snapshot.commenter
            and self.anchor_text in snapshot.full_text
            and self.context_hash == snapshot.context_hash
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "target_comment_id": self.target_comment_id,
            "note_id": self.note_id,
            "commenter": self.commenter,
            "full_text": self.full_text,
            "anchor_text": self.anchor_text,
            "context_hash": self.context_hash,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CommentTarget":
        try:
            return cls(
                candidate_id=value["candidate_id"],
                target_comment_id=value["target_comment_id"],
                note_id=value["note_id"],
                commenter=value["commenter"],
                full_text=value["full_text"],
                anchor_text=value["anchor_text"],
                context_hash=value["context_hash"],
            )
        except KeyError as exc:
            raise CommentFlowContractError(f"missing CommentTarget field: {exc}") from exc


@dataclass(frozen=True)
class CommentSnapshot:
    note_id: str
    target_comment_id: str
    commenter: str
    full_text: str
    context_hash: str
    risk_signals: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        note_id: str,
        target_comment_id: str,
        commenter: str,
        full_text: str,
        risk_signals: tuple[str, ...] = (),
    ) -> "CommentSnapshot":
        normalized = " ".join(full_text.split())
        context = f"{note_id}\n{target_comment_id}\n{commenter.strip()}\n{normalized}"
        return cls(
            note_id=_safe_id("snapshot.note_id", note_id),
            target_comment_id=_safe_id("snapshot.target_comment_id", target_comment_id),
            commenter=_text("snapshot.commenter", commenter),
            full_text=_text("snapshot.full_text", normalized),
            context_hash=_digest(context),
            risk_signals=tuple(_text("risk_signal", item) for item in risk_signals),
        )


@dataclass(frozen=True)
class CommentInteractionPlan:
    plan_id: str
    campaign_id: str
    account_id: str
    query: str
    result_index: int
    target: CommentTarget
    reply_text: str
    approval_ref: str
    source_context_ref: str
    bridge_id: str
    message_plan_id: str
    message_content_hash: str
    approval_hash: str
    run_mode: str = "smoke"

    def __post_init__(self) -> None:
        for name in ("plan_id", "campaign_id", "account_id"):
            object.__setattr__(self, name, _safe_id(name, getattr(self, name)))
        object.__setattr__(self, "query", _text("query", self.query))
        object.__setattr__(self, "reply_text", _text("reply_text", self.reply_text))
        object.__setattr__(self, "approval_ref", _text("approval_ref", self.approval_ref))
        object.__setattr__(
            self,
            "source_context_ref",
            _text("source_context_ref", self.source_context_ref),
        )
        for name in ("bridge_id", "message_plan_id"):
            object.__setattr__(self, name, _safe_id(name, getattr(self, name)))
        for name in ("message_content_hash", "approval_hash"):
            if re.fullmatch(r"[0-9a-f]{64}", getattr(self, name)) is None:
                raise CommentFlowContractError(f"{name} must be SHA-256 hex")
        if _digest(self.reply_text) != self.message_content_hash:
            raise CommentFlowContractError("reply_text does not match approved message content hash")
        expected_source = f"message:{self.message_plan_id}:{self.message_content_hash}"
        if self.source_context_ref != expected_source:
            raise CommentFlowContractError("source_context_ref does not match approved message")
        if type(self.result_index) is not int or self.result_index < 0:
            raise CommentFlowContractError("result_index must be a non-negative integer")
        if not isinstance(self.target, CommentTarget):
            raise CommentFlowContractError("target must be CommentTarget")
        if self.run_mode != "smoke":
            raise CommentFlowContractError("first comment flow supports smoke only")
        if len(self.reply_text) > 180:
            raise CommentFlowContractError("reply_text exceeds 180 characters")
        hits = [term for term in REPLY_RED_LINES if term in self.reply_text]
        if hits:
            raise CommentFlowContractError(
                "reply_text violates public-reply red lines: " + ", ".join(hits)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "campaign_id": self.campaign_id,
            "account_id": self.account_id,
            "query": self.query,
            "result_index": self.result_index,
            "target": self.target.to_dict(),
            "reply_text": self.reply_text,
            "approval_ref": self.approval_ref,
            "source_context_ref": self.source_context_ref,
            "bridge_id": self.bridge_id,
            "message_plan_id": self.message_plan_id,
            "message_content_hash": self.message_content_hash,
            "approval_hash": self.approval_hash,
            "run_mode": self.run_mode,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CommentInteractionPlan":
        try:
            target_value = value["target"]
            if not isinstance(target_value, dict):
                raise CommentFlowContractError("plan.target must be an object")
            return cls(
                plan_id=value["plan_id"],
                campaign_id=value["campaign_id"],
                account_id=value["account_id"],
                query=value["query"],
                result_index=value["result_index"],
                target=CommentTarget.from_dict(target_value),
                reply_text=value["reply_text"],
                approval_ref=value["approval_ref"],
                source_context_ref=value["source_context_ref"],
                bridge_id=value["bridge_id"],
                message_plan_id=value["message_plan_id"],
                message_content_hash=value["message_content_hash"],
                approval_hash=value["approval_hash"],
                run_mode=value.get("run_mode", "smoke"),
            )
        except KeyError as exc:
            raise CommentFlowContractError(f"missing CommentInteractionPlan field: {exc}") from exc


@dataclass(frozen=True)
class CommentFlowGate:
    platform_access_allowed: bool
    login_ready: bool
    stop_requested: bool
    daily_budget_remaining: int
    minimum_interval_elapsed: bool
    duplicate_like: bool
    duplicate_reply: bool

    def blockers(self) -> tuple[str, ...]:
        rows: list[str] = []
        if not self.platform_access_allowed:
            rows.append("platform_access_disabled")
        if not self.login_ready:
            rows.append("login_not_ready")
        if self.stop_requested:
            rows.append("operator_stop_requested")
        required_budget = 1 if self.duplicate_like else 2
        if self.daily_budget_remaining < required_budget:
            rows.append("insufficient_action_budget")
        if not self.minimum_interval_elapsed:
            rows.append("minimum_interval_not_elapsed")
        if self.duplicate_reply:
            rows.append("duplicate_comment_reply")
        return tuple(rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": not self.blockers(),
            "blockers": list(self.blockers()),
            "platform_access_allowed": self.platform_access_allowed,
            "login_ready": self.login_ready,
            "stop_requested": self.stop_requested,
            "daily_budget_remaining": self.daily_budget_remaining,
            "minimum_interval_elapsed": self.minimum_interval_elapsed,
            "duplicate_like": self.duplicate_like,
            "duplicate_reply": self.duplicate_reply,
        }


@dataclass(frozen=True)
class UiActionResult:
    action: str
    attempted: bool
    verified: bool
    result_ref: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "attempted": self.attempted,
            "verified": self.verified,
            "result_ref": self.result_ref,
            "evidence": dict(self.evidence),
        }


class CommentFlowPort(Protocol):
    def open_single_candidate(self, plan: CommentInteractionPlan) -> str:
        """Return the opened note id."""

    def read_target_comment(self, target: CommentTarget) -> CommentSnapshot:
        """Read exactly one target comment in the currently open note."""

    def like_target_comment(self, target: CommentTarget) -> UiActionResult:
        """Like only the matched comment container."""

    def reply_target_comment(
        self, target: CommentTarget, reply_text: str
    ) -> UiActionResult:
        """Reply only inside the matched comment container."""

    def current_risk_signals(self) -> tuple[str, ...]:
        """Return current blocking platform risk signals."""


@dataclass(frozen=True)
class CommentFlowResult:
    ok: bool
    stage: str
    blockers: tuple[str, ...]
    opened_note_id: str | None = None
    snapshot: CommentSnapshot | None = None
    like: UiActionResult | None = None
    reply: UiActionResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "blockers": list(self.blockers),
            "opened_note_id": self.opened_note_id,
            "snapshot": None
            if self.snapshot is None
            else {
                "note_id": self.snapshot.note_id,
                "target_comment_id": self.snapshot.target_comment_id,
                "commenter": self.snapshot.commenter,
                "full_text": self.snapshot.full_text,
                "context_hash": self.snapshot.context_hash,
                "risk_signals": list(self.snapshot.risk_signals),
            },
            "like": None if self.like is None else self.like.to_dict(),
            "reply": None if self.reply is None else self.reply.to_dict(),
        }


def execute_single_comment_flow(
    *,
    plan: CommentInteractionPlan,
    gate: CommentFlowGate,
    port: CommentFlowPort,
) -> CommentFlowResult:
    blockers = gate.blockers()
    if blockers:
        return CommentFlowResult(False, "preflight", blockers)
    opened_note_id = port.open_single_candidate(plan)
    if opened_note_id != plan.target.note_id:
        return CommentFlowResult(
            False,
            "candidate_match",
            ("opened_note_mismatch",),
            opened_note_id=opened_note_id,
        )
    risks = port.current_risk_signals()
    if risks:
        return CommentFlowResult(
            False,
            "before_target_read",
            tuple(f"risk:{item}" for item in risks),
            opened_note_id=opened_note_id,
        )
    snapshot = port.read_target_comment(plan.target)
    if snapshot.risk_signals:
        return CommentFlowResult(
            False,
            "target_read",
            tuple(f"risk:{item}" for item in snapshot.risk_signals),
            opened_note_id=opened_note_id,
            snapshot=snapshot,
        )
    if not plan.target.matches(snapshot):
        return CommentFlowResult(
            False,
            "target_match",
            ("reply_target_mismatch",),
            opened_note_id=opened_note_id,
            snapshot=snapshot,
        )
    like = (
        UiActionResult(
            "like_comment",
            False,
            True,
            f"comment_like_existing:{plan.target.context_hash}",
            {"dedupe_resume": True},
        )
        if gate.duplicate_like
        else port.like_target_comment(plan.target)
    )
    if not like.verified:
        return CommentFlowResult(
            False,
            "comment_like",
            ("comment_like_unverified",),
            opened_note_id=opened_note_id,
            snapshot=snapshot,
            like=like,
        )
    risks = port.current_risk_signals()
    if risks:
        return CommentFlowResult(
            False,
            "between_actions",
            tuple(f"risk:{item}" for item in risks),
            opened_note_id=opened_note_id,
            snapshot=snapshot,
            like=like,
        )
    reply = port.reply_target_comment(plan.target, plan.reply_text)
    if not reply.verified:
        return CommentFlowResult(
            False,
            "comment_reply",
            ("comment_reply_unverified",),
            opened_note_id=opened_note_id,
            snapshot=snapshot,
            like=like,
            reply=reply,
        )
    return CommentFlowResult(
        True,
        "completed",
        (),
        opened_note_id=opened_note_id,
        snapshot=snapshot,
        like=like,
        reply=reply,
    )
