"""Read-only, privacy-minimal operation records across V2 workflows."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .storage import read_jsonl


WORKFLOWS = ("setup", "publish", "service", "engage")
STATUSES = ("verified", "not_dispatched", "unknown")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_PUBLISH_COMMANDS = {
    "publish-image-current": "publish_image",
    "publish-video-current": "publish_video",
}


class OperationLedgerError(ValueError):
    pass


def _timestamp(value: str, field: str, *, optional: bool = False) -> str:
    text = str(value or "").strip()
    if optional and not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationLedgerError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperationLedgerError(f"{field} must include timezone")
    return parsed.isoformat()


def _safe_id(value: object, field: str, *, optional: bool = False) -> str:
    text = str(value or "").strip()
    if optional and not text:
        return ""
    if _SAFE_ID.fullmatch(text) is None:
        raise OperationLedgerError(f"{field} must be a safe id")
    return text


def _hash_ref(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if _HASH.fullmatch(text) else sha256(text.encode("utf-8")).hexdigest()


def _workflow_for_action(action_kind: str) -> str:
    if action_kind.startswith("publish_"):
        return "publish"
    if action_kind.startswith("service_"):
        return "service"
    if action_kind.startswith("engage_"):
        return "engage"
    raise OperationLedgerError(f"unsupported operation action_kind: {action_kind}")


@dataclass(frozen=True)
class OperationLedgerQuery:
    schema_version: int = 1
    account_id: str = ""
    workflow: str = ""
    status: str = ""
    since: str = ""
    until: str = ""
    limit: int = 100

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise OperationLedgerError("unsupported OperationLedgerQuery schema")
        _safe_id(self.account_id, "account_id", optional=True)
        if self.workflow and self.workflow not in WORKFLOWS:
            raise OperationLedgerError("workflow is outside the public V2 workflows")
        if self.status and self.status not in STATUSES:
            raise OperationLedgerError("status is outside the terminal operation states")
        since = _timestamp(self.since, "since", optional=True)
        until = _timestamp(self.until, "until", optional=True)
        if since and until and datetime.fromisoformat(since) > datetime.fromisoformat(until):
            raise OperationLedgerError("since cannot be later than until")
        if type(self.limit) is not int or not 1 <= self.limit <= 500:
            raise OperationLedgerError("limit must be 1-500")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class OperationLedgerEntry:
    schema_version: int
    operation_id: str
    source_kind: str
    source_ref_hash: str
    workflow: str
    action_kind: str
    account_id: str
    status: str
    occurred_at: str
    time_evidence: str
    target_ref_hash: str
    plan_hash: str
    approval_ref: str
    approval_hash: str
    evidence_hash: str
    reason_code: str
    dedupe_key_hash: str
    strategy_pack_id: str
    strategy_pack_hash: str
    campaign_id: str
    task_id: str
    session_id: str
    run_id: str
    recovery_state: str
    do_not_retry: bool
    platform_actions_executed: int

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise OperationLedgerError("unsupported OperationLedgerEntry schema")
        _safe_id(self.operation_id, "operation_id")
        _safe_id(self.account_id, "account_id")
        for field in ("source_kind", "action_kind", "time_evidence", "recovery_state"):
            _safe_id(getattr(self, field), field)
        for field in (
            "approval_ref", "reason_code", "strategy_pack_id", "campaign_id",
            "task_id", "session_id", "run_id",
        ):
            _safe_id(getattr(self, field), field, optional=True)
        _timestamp(self.occurred_at, "occurred_at")
        if self.workflow not in WORKFLOWS or self.status not in STATUSES:
            raise OperationLedgerError("operation entry workflow or status is invalid")
        if self.platform_actions_executed not in {0, 1}:
            raise OperationLedgerError("platform_actions_executed must be zero or one")
        if self.status == "verified" and self.platform_actions_executed != 1:
            raise OperationLedgerError("verified operation must account for exactly one action")
        if self.status == "verified" and not self.evidence_hash:
            raise OperationLedgerError("verified operation requires an evidence hash")
        if self.status != "verified" and self.platform_actions_executed != 0:
            raise OperationLedgerError("non-verified operation cannot account for a platform action")
        if self.status != "verified" and not self.reason_code:
            raise OperationLedgerError("non-verified operation requires a reason code")
        if self.status == "unknown" and not self.do_not_retry:
            raise OperationLedgerError("unknown operation must be do_not_retry")
        for field in (
            "source_ref_hash", "target_ref_hash", "plan_hash", "approval_hash",
            "evidence_hash", "dedupe_key_hash", "strategy_pack_hash",
        ):
            value = getattr(self, field)
            if value and _HASH.fullmatch(value) is None:
                raise OperationLedgerError(f"{field} must be SHA-256 when present")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class OperationLedgerStore:
    """Projects existing append-only evidence into a stable read-only schema."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = Path(runtime_dir)

    def _unified_entries(self) -> list[OperationLedgerEntry]:
        decisions: dict[str, Mapping[str, Any]] = {}
        for row in read_jsonl(self.runtime_dir / "action_preflight" / "decisions.jsonl"):
            request_hash = str(row.get("request_hash") or "")
            if _HASH.fullmatch(request_hash):
                decisions[request_hash] = row
        entries: list[OperationLedgerEntry] = []
        for row in read_jsonl(self.runtime_dir / "action_preflight" / "results.jsonl"):
            status = str(row.get("status") or "")
            if status not in STATUSES:
                raise OperationLedgerError("unified action result has an invalid terminal status")
            action_kind = str(row.get("action_kind") or "")
            request_hash = str(row.get("request_hash") or "")
            decision = decisions.get(request_hash, {})
            occurred_at = _timestamp(str(row.get("recorded_at") or ""), "recorded_at")
            operation_id = _safe_id(row.get("result_id"), "result_id")
            entries.append(OperationLedgerEntry(
                schema_version=1,
                operation_id=operation_id,
                source_kind="unified_action_result",
                source_ref_hash=_hash_ref(f"unified_action_result:{operation_id}"),
                workflow=_workflow_for_action(action_kind),
                action_kind=action_kind,
                account_id=_safe_id(row.get("account_id"), "account_id"),
                status=status,
                occurred_at=occurred_at,
                time_evidence="recorded_at",
                target_ref_hash=_hash_ref(row.get("target_ref_hash")),
                plan_hash=_hash_ref(row.get("plan_hash")),
                approval_ref=_safe_id(decision.get("approval_ref"), "approval_ref", optional=True),
                approval_hash=_hash_ref(decision.get("approval_hash")),
                evidence_hash=_hash_ref(row.get("evidence_hash")),
                reason_code=_safe_id(row.get("reason_code"), "reason_code", optional=True),
                dedupe_key_hash=_hash_ref(row.get("dedupe_key_hash")),
                strategy_pack_id="",
                strategy_pack_hash="",
                campaign_id="",
                task_id="",
                session_id="",
                run_id="",
                recovery_state=("manual_reconciliation_required" if status == "unknown" else "complete"),
                do_not_retry=status == "unknown" or row.get("do_not_retry") is True,
                platform_actions_executed=1 if status == "verified" else 0,
            ))
        return entries

    def _publish_entries(self, existing_plan_hashes: set[str]) -> list[OperationLedgerEntry]:
        attempts: dict[str, dict[str, Any]] = {}
        for row in read_jsonl(self.runtime_dir / "run_agent" / "write_journal.jsonl"):
            attempt_id = str(row.get("attempt_id") or "")
            if not attempt_id:
                continue
            if row.get("status") == "prepared":
                attempts[attempt_id] = dict(row)
                continue
            if row.get("status") not in STATUSES or attempt_id not in attempts:
                continue
            attempts[attempt_id]["terminal"] = dict(row)
        entries: list[OperationLedgerEntry] = []
        for attempt_id, prepared in attempts.items():
            command = str(prepared.get("command") or "")
            terminal = prepared.get("terminal")
            if command not in _PUBLISH_COMMANDS or not isinstance(terminal, Mapping):
                continue
            plan_hash = _hash_ref(prepared.get("content_hash"))
            if plan_hash in existing_plan_hashes:
                continue
            status = str(terminal.get("status"))
            entries.append(OperationLedgerEntry(
                schema_version=1,
                operation_id=_safe_id(attempt_id, "attempt_id"),
                source_kind="platform_write_journal",
                source_ref_hash=_hash_ref(f"platform_write_journal:{attempt_id}"),
                workflow="publish",
                action_kind=_PUBLISH_COMMANDS[command],
                account_id=_safe_id(prepared.get("account_id"), "account_id"),
                status=status,
                occurred_at=_timestamp(str(terminal.get("recorded_at") or ""), "recorded_at"),
                time_evidence="run_agent_journal",
                target_ref_hash=_hash_ref(prepared.get("target_ref_hash")),
                plan_hash=plan_hash,
                approval_ref="",
                approval_hash="",
                evidence_hash=_hash_ref(terminal.get("evidence_hash")),
                reason_code=_safe_id(terminal.get("reason_code"), "reason_code", optional=True),
                dedupe_key_hash=_hash_ref(prepared.get("invocation_hash")),
                strategy_pack_id="",
                strategy_pack_hash="",
                campaign_id="",
                task_id="",
                session_id="",
                run_id="",
                recovery_state=("manual_reconciliation_required" if status == "unknown" else "complete"),
                do_not_retry=status == "unknown" or terminal.get("do_not_retry") is True,
                platform_actions_executed=1 if status == "verified" else 0,
            ))
        return entries

    def _legacy_entries(self, represented: set[tuple[str, str, str, str]]) -> list[OperationLedgerEntry]:
        entries: list[OperationLedgerEntry] = []
        for relative in (("comment_flow", "actions.jsonl"), ("dm", "actions.jsonl")):
            for row in read_jsonl(self.runtime_dir.joinpath(*relative)):
                legacy_status = str(row.get("status") or "")
                status = {
                    "verified": "verified",
                    "blocked": "not_dispatched",
                    "failed": "unknown",
                    "unknown": "unknown",
                }.get(legacy_status)
                if status is None:
                    continue
                raw_type = str(row.get("action_type") or "")
                scope = str((row.get("metadata") or {}).get("interaction_scope") or "") if isinstance(row.get("metadata"), Mapping) else ""
                action_kind = {
                    "like": "engage_note_like" if scope == "note" else "engage_comment_like",
                    "comment": "engage_note_comment",
                    "reply": "engage_comment_reply",
                    "dm": "engage_single_dm",
                }.get(raw_type)
                if action_kind is None:
                    continue
                occurred_at = _timestamp(str(row.get("created_at") or ""), "created_at")
                account_id = _safe_id(row.get("account_id"), "account_id")
                identity = (account_id, action_kind, status, occurred_at)
                if identity in represented:
                    continue
                operation_id = _safe_id(row.get("record_id"), "record_id")
                entries.append(OperationLedgerEntry(
                    schema_version=1,
                    operation_id=operation_id,
                    source_kind="legacy_action_record",
                    source_ref_hash=_hash_ref(f"legacy_action_record:{operation_id}"),
                    workflow="engage",
                    action_kind=action_kind,
                    account_id=account_id,
                    status=status,
                    occurred_at=occurred_at,
                    time_evidence="legacy_action_record",
                    target_ref_hash=_hash_ref(row.get("source_context_ref")),
                    plan_hash=_hash_ref(row.get("interaction_plan_id")),
                    approval_ref="",
                    approval_hash="",
                    evidence_hash=_hash_ref(row.get("result_ref")),
                    reason_code=(
                        _safe_id(row.get("error_code"), "error_code", optional=True)
                        or ("legacy_gate_blocked" if status == "not_dispatched" else "legacy_unknown")
                    ),
                    dedupe_key_hash="",
                    strategy_pack_id="",
                    strategy_pack_hash="",
                    campaign_id=_safe_id(row.get("campaign_id"), "campaign_id", optional=True),
                    task_id="",
                    session_id="",
                    run_id=_safe_id(row.get("run_id"), "run_id", optional=True),
                    recovery_state=("manual_reconciliation_required" if status == "unknown" else "complete"),
                    do_not_retry=status == "unknown",
                    platform_actions_executed=1 if status == "verified" else 0,
                ))
        return entries

    def entries(self) -> tuple[OperationLedgerEntry, ...]:
        unified = self._unified_entries()
        represented = {
            (item.account_id, item.action_kind, item.status, item.occurred_at)
            for item in unified
        }
        publish = self._publish_entries({item.plan_hash for item in unified if item.plan_hash})
        legacy = self._legacy_entries(represented)
        combined = [*unified, *publish, *legacy]
        identifiers = [item.operation_id for item in combined]
        if len(identifiers) != len(set(identifiers)):
            raise OperationLedgerError("operation ledger contains duplicate operation_id values")
        return tuple(sorted(combined, key=lambda item: (item.occurred_at, item.operation_id)))

    def query(self, query: OperationLedgerQuery) -> dict[str, Any]:
        rows = list(self.entries())
        if query.account_id:
            rows = [item for item in rows if item.account_id == query.account_id]
        if query.workflow:
            rows = [item for item in rows if item.workflow == query.workflow]
        if query.status:
            rows = [item for item in rows if item.status == query.status]
        if query.since:
            since = datetime.fromisoformat(_timestamp(query.since, "since")).astimezone(timezone.utc)
            rows = [item for item in rows if datetime.fromisoformat(item.occurred_at).astimezone(timezone.utc) >= since]
        if query.until:
            until = datetime.fromisoformat(_timestamp(query.until, "until")).astimezone(timezone.utc)
            rows = [item for item in rows if datetime.fromisoformat(item.occurred_at).astimezone(timezone.utc) <= until]
        matched_count = len(rows)
        rows = list(reversed(rows))[: query.limit]
        return {
            "schema_version": 1,
            "query": query.to_dict(),
            "matched_count": matched_count,
            "returned_count": len(rows),
            "records": [item.to_dict() for item in rows],
            "platform_actions_executed": 0,
        }

    def status(self, *, account_id: str = "") -> dict[str, Any]:
        _safe_id(account_id, "account_id", optional=True)
        rows = list(self.entries())
        if account_id:
            rows = [item for item in rows if item.account_id == account_id]
        return {
            "schema_version": 1,
            "account_id": account_id,
            "record_count": len(rows),
            "by_workflow": dict(sorted(Counter(item.workflow for item in rows).items())),
            "by_status": dict(sorted(Counter(item.status for item in rows).items())),
            "unknown_record_count": sum(item.status == "unknown" for item in rows),
            "legacy_daily_review_public": False,
            "raw_content_included": False,
            "contact_values_included": False,
            "platform_actions_executed": 0,
        }

    def export(self, query: OperationLedgerQuery) -> dict[str, Any]:
        result = self.query(query)
        payload = {
            "schema_version": 1,
            "export_kind": "operation_ledger",
            "query": result["query"],
            "matched_count": result["matched_count"],
            "returned_count": result["returned_count"],
            "records": result["records"],
            "privacy": {
                "raw_content_included": False,
                "contact_values_included": False,
                "cookies_or_tokens_included": False,
            },
            "platform_actions_executed": 0,
        }
        payload["content_hash"] = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return payload
