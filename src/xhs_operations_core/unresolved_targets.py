"""Persistent no-retry registry for platform actions with unknown outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from .storage import append_jsonl, read_jsonl


class UnresolvedTargetError(ValueError):
    pass


_NOTE_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
UNRESOLVED_TARGET_RESOLUTION_CONFIRMATION = "I_CONFIRM_MANUAL_TARGET_RECONCILIATION"


@dataclass(frozen=True)
class UnresolvedTargetRegistry:
    runtime_dir: Path

    def __init__(self, runtime_dir: str | Path) -> None:
        object.__setattr__(self, "runtime_dir", Path(runtime_dir))

    @property
    def path(self) -> Path:
        return self.runtime_dir / "comment_flow" / "unresolved_targets.jsonl"

    @staticmethod
    def _note_id(value: str) -> str:
        normalized = value.strip()
        if _NOTE_ID_RE.fullmatch(normalized) is None:
            raise UnresolvedTargetError("unresolved target note_id is invalid")
        return normalized

    def record(
        self,
        *,
        note_id: str,
        reason_code: str,
        recorded_at: str,
        source_ref: str,
    ) -> dict[str, Any]:
        normalized_note_id = self._note_id(note_id)
        if _SAFE_CODE_RE.fullmatch(reason_code.strip()) is None:
            raise UnresolvedTargetError("unresolved target reason_code is invalid")
        if _SAFE_CODE_RE.fullmatch(source_ref.strip()) is None:
            raise UnresolvedTargetError("unresolved target source_ref is invalid")
        try:
            parsed_at = datetime.fromisoformat(recorded_at.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise UnresolvedTargetError("unresolved target recorded_at is invalid") from exc
        if parsed_at.tzinfo is None:
            raise UnresolvedTargetError("unresolved target recorded_at must be timezone-aware")
        event = {
            "schema_version": 1,
            "note_id": normalized_note_id,
            "status": "unresolved",
            "reason_code": reason_code.strip(),
            "source_ref": source_ref.strip(),
            "recorded_at": parsed_at.isoformat(),
            "do_not_retry": True,
            "platform_actions_executed": 0,
        }
        append_jsonl(self.path, event)
        return event

    def get(self, note_id: str) -> dict[str, Any] | None:
        normalized_note_id = self._note_id(note_id)
        for event in reversed(read_jsonl(self.path)):
            if str(event.get("note_id", "")) != normalized_note_id:
                continue
            if event.get("status") == "resolved":
                return None
            if event.get("status") == "unresolved" and event.get("do_not_retry") is True:
                return event
        return None

    def is_unresolved(self, note_id: str) -> bool:
        return self.get(note_id) is not None

    def resolve(
        self,
        *,
        note_id: str,
        resolution: str,
        evidence_ref: str,
        resolved_at: str,
        confirmation: str,
    ) -> dict[str, Any]:
        normalized_note_id = self._note_id(note_id)
        if confirmation != UNRESOLVED_TARGET_RESOLUTION_CONFIRMATION:
            raise UnresolvedTargetError("exact manual target reconciliation confirmation is required")
        if resolution not in {"verified_present", "verified_absent"}:
            raise UnresolvedTargetError("target resolution must be verified_present or verified_absent")
        if _SAFE_CODE_RE.fullmatch(evidence_ref.strip()) is None:
            raise UnresolvedTargetError("target resolution evidence_ref is invalid")
        current = self.get(normalized_note_id)
        if current is None:
            raise UnresolvedTargetError("target has no unresolved write to reconcile")
        try:
            parsed_at = datetime.fromisoformat(resolved_at.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise UnresolvedTargetError("target resolved_at is invalid") from exc
        if parsed_at.tzinfo is None:
            raise UnresolvedTargetError("target resolved_at must be timezone-aware")
        event = {
            "schema_version": 1,
            "note_id": normalized_note_id,
            "status": "resolved",
            "resolution": resolution,
            "evidence_ref": evidence_ref.strip(),
            "resolved_at": parsed_at.isoformat(),
            "retry_exact_target_allowed": False,
            "platform_actions_executed": 0,
        }
        append_jsonl(self.path, event)
        return event
