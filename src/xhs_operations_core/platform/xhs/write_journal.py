"""Cross-process lease and crash-safe journal for every Xiaohongshu write."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterator
from contextlib import contextmanager
from uuid import uuid4

from xhs_operations_core.storage import append_jsonl, file_lock, read_json, read_jsonl, write_json_atomic


WRITE_RECONCILIATION_CONFIRMATION = "I_CONFIRM_MANUAL_WRITE_RECONCILIATION"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_arg_fingerprint(command: str, args: list[str]) -> dict[str, str]:
    target_tokens: list[str] = []
    content_hash = ""
    for index, token in enumerate(args):
        previous = args[index - 1] if index else ""
        if previous in {"--feed-id", "--comment-id", "--expected-peer-hash", "--plan-hash"}:
            target_tokens.append(f"{previous}:{token}")
            if previous == "--plan-hash":
                content_hash = token
        elif previous == "--content":
            content_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    target_ref_hash = hashlib.sha256(
        "|".join(target_tokens).encode("utf-8")
    ).hexdigest()
    invocation_hash = hashlib.sha256(
        json.dumps(
            {"command": command, "target_ref_hash": target_ref_hash, "content_hash": content_hash},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "target_ref_hash": target_ref_hash,
        "content_hash": content_hash,
        "invocation_hash": invocation_hash,
    }


@dataclass(frozen=True)
class WriteAttempt:
    attempt_id: str
    command: str
    target_ref_hash: str
    content_hash: str
    invocation_hash: str


class PlatformWriteJournal:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.path = self.runtime_dir / "run_agent" / "write_journal.jsonl"
        self.lease_path = self.runtime_dir / "run_agent" / "global_write.lease"
        self.stop_path = self.runtime_dir / "comment_flow" / "STOP.json"

    @contextmanager
    def lease(self) -> Iterator[None]:
        with file_lock(self.lease_path):
            yield

    def prepare(self, *, command: str, args: list[str], account_id: str) -> WriteAttempt:
        fingerprint = _safe_arg_fingerprint(command, args)
        attempt = WriteAttempt(
            attempt_id="write_" + uuid4().hex,
            command=command,
            **fingerprint,
        )
        append_jsonl(self.path, {
            "schema_version": 1,
            "attempt_id": attempt.attempt_id,
            "status": "prepared",
            "command": command,
            "account_id": account_id,
            **fingerprint,
            "recorded_at": _now(),
            "raw_arguments_retained": False,
            "platform_actions_executed": 0,
        })
        return attempt

    def dispatched(self, attempt: WriteAttempt) -> None:
        append_jsonl(self.path, {
            "schema_version": 1,
            "attempt_id": attempt.attempt_id,
            "status": "dispatched",
            "command": attempt.command,
            "invocation_hash": attempt.invocation_hash,
            "recorded_at": _now(),
            "platform_actions_executed": 0,
        })

    def not_dispatched(self, attempt: WriteAttempt, *, reason_code: str) -> None:
        append_jsonl(self.path, {
            "schema_version": 1,
            "attempt_id": attempt.attempt_id,
            "status": "not_dispatched",
            "command": attempt.command,
            "invocation_hash": attempt.invocation_hash,
            "reason_code": reason_code,
            "recorded_at": _now(),
            "platform_actions_executed": 0,
        })

    def verified(self, attempt: WriteAttempt, *, evidence_hash: str) -> None:
        append_jsonl(self.path, {
            "schema_version": 1,
            "attempt_id": attempt.attempt_id,
            "status": "verified",
            "command": attempt.command,
            "invocation_hash": attempt.invocation_hash,
            "evidence_hash": evidence_hash,
            "recorded_at": _now(),
            "platform_actions_executed": 1,
        })

    def unknown(self, attempt: WriteAttempt, *, reason_code: str) -> None:
        append_jsonl(self.path, {
            "schema_version": 1,
            "attempt_id": attempt.attempt_id,
            "status": "unknown",
            "command": attempt.command,
            "invocation_hash": attempt.invocation_hash,
            "reason_code": reason_code,
            "recorded_at": _now(),
            "do_not_retry": True,
            "platform_actions_executed": 0,
        })
        write_json_atomic(self.stop_path, {
            "schema_version": 2,
            "writes_allowed": False,
            "reason": "unknown_platform_write_result",
            "attempt_id": attempt.attempt_id,
            "command": attempt.command,
            "recorded_at": _now(),
            "requires_manual_reconciliation": True,
        })

    def reconcile_unknown(
        self,
        *,
        attempt_id: str,
        observed_outcome: str,
        evidence_ref: str,
        reconciled_at: str,
        confirmation: str,
    ) -> dict[str, Any]:
        """Resolve one unknown transport result after an explicit visible check.

        Reconciliation only clears the global STOP.  It never changes the
        original unknown accounting row and never allows an automatic retry of
        that exact target.
        """
        if confirmation != WRITE_RECONCILIATION_CONFIRMATION:
            raise ValueError("exact manual write reconciliation confirmation is required")
        if observed_outcome not in {"verified_present", "verified_absent"}:
            raise ValueError("observed_outcome must be verified_present or verified_absent")
        if re.fullmatch(r"write_[0-9a-f]{32}", attempt_id) is None:
            raise ValueError("write reconciliation attempt_id is invalid")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", evidence_ref) is None:
            raise ValueError("write reconciliation evidence_ref is invalid")
        try:
            moment = datetime.fromisoformat(reconciled_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("write reconciliation timestamp is invalid") from exc
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("write reconciliation timestamp must include timezone")
        rows = [row for row in read_jsonl(self.path) if row.get("attempt_id") == attempt_id]
        if not rows or rows[-1].get("status") != "unknown":
            raise ValueError("write attempt is not in an unknown terminal state")
        stop = read_json(self.stop_path, default=None)
        matching_stop = (
            isinstance(stop, dict)
            and stop.get("requires_manual_reconciliation") is True
            and stop.get("attempt_id") == attempt_id
        )
        # Older builds revoked the bounded lease after every execution and
        # accidentally overwrote the unknown-result STOP. Recover only the
        # latest journaled unknown attempt; never accept an older target.
        if not matching_stop:
            all_rows = read_jsonl(self.path)
            latest_by_attempt: dict[str, dict[str, Any]] = {}
            for row in all_rows:
                row_attempt = row.get("attempt_id")
                if isinstance(row_attempt, str):
                    latest_by_attempt[row_attempt] = row
            unresolved = [
                row for row in latest_by_attempt.values()
                if row.get("status") == "unknown"
            ]
            unresolved.sort(key=lambda row: str(row.get("recorded_at", "")))
            recoverable_overwrite = (
                isinstance(stop, dict)
                and stop.get("reason") == "bounded_write_lease_revoked"
                and bool(unresolved)
                and unresolved[-1].get("attempt_id") == attempt_id
            )
            recoverable_unified_stop = (
                isinstance(stop, dict)
                and stop.get("reason") == "unknown_unified_action_result"
                and stop.get("requires_manual_reconciliation") is True
                and isinstance(stop.get("result_id"), str)
                and bool(unresolved)
                and unresolved[-1].get("attempt_id") == attempt_id
            )
            if not (recoverable_overwrite or recoverable_unified_stop):
                raise ValueError("global STOP does not match the unknown write attempt")
        event = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "status": "reconciled",
            "observed_outcome": observed_outcome,
            "evidence_ref": evidence_ref,
            "recorded_at": moment.isoformat(),
            "retry_exact_target_allowed": False,
            "platform_actions_executed": 0,
        }
        append_jsonl(self.path, event)
        write_json_atomic(self.stop_path, {
            "schema_version": 2,
            "writes_allowed": False,
            "reason": "manual_write_reconciliation_completed",
            "attempt_id": attempt_id,
            "observed_outcome": observed_outcome,
            "recorded_at": moment.isoformat(),
            "requires_manual_reconciliation": False,
        })
        return event
