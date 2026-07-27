"""Strict, portable contracts for a bounded multi-day promotion task.

The scheduler wakes Codex; it never grants platform access. Every live action
still passes the Run Agent, account-binding, page-context, risk and verification
gates implemented by the interaction layer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..storage import read_json, update_json_object, write_json_atomic


class TaskContractError(ValueError):
    pass


BOUNDED_RUN_CONFIRMATION = "I_APPROVE_BOUNDED_CAMPAIGN_RUN"
SOURCE_MODES = {"account_note", "specified_note", "direct_brief"}
TASK_STATUSES = {"draft", "approved", "running", "paused", "completed", "cancelled"}
ALLOWED_ACTIONS = {"like", "comment", "reply"}
SCHEDULE_JOB_KINDS = {"daily_plan", "heartbeat", "daily_review"}
APPROVAL_MODES = {"review_each", "bounded_campaign"}
TRANSITIONS = {
    "draft": {"approved", "cancelled"},
    "approved": {"running", "cancelled"},
    "running": {"paused", "completed", "cancelled"},
    "paused": {"running", "completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def _text(name: str, value: object, *, optional: bool = False, limit: int = 500) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise TaskContractError(f"{name} must be text")
    result = " ".join(value.split()).strip()
    if not result and not optional:
        raise TaskContractError(f"{name} is required")
    if len(result) > limit:
        raise TaskContractError(f"{name} is too long")
    return result


def _safe_id(name: str, value: object) -> str:
    result = _text(name, value, limit=128)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", result) is None:
        raise TaskContractError(f"{name} must be a safe id")
    return result


def _clock(name: str, value: object) -> str:
    result = _text(name, value, limit=5)
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result) is None:
        raise TaskContractError(f"{name} must use HH:MM")
    return result


def _day(name: str, value: object) -> str:
    result = _text(name, value, limit=10)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise TaskContractError(f"{name} must use YYYY-MM-DD") from exc
    return result


def _moment(name: str, value: object) -> str:
    result = _text(name, value, limit=64)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskContractError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TaskContractError(f"{name} must include timezone")
    return result


def _strings(name: str, value: object, *, maximum: int, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise TaskContractError(f"{name} must be a list with at most {maximum} items")
    result = tuple(_text(f"{name} item", item, limit=100) for item in value)
    if required and not result:
        raise TaskContractError(f"{name} cannot be empty")
    if len(result) != len(set(result)):
        raise TaskContractError(f"{name} cannot contain duplicates")
    return result


def _caps(value: object) -> dict[str, int]:
    expected = {"targets", "likes", "comments", "replies"}
    if not isinstance(value, dict) or set(value) != expected:
        raise TaskContractError("daily_caps must contain targets/likes/comments/replies")
    limits = {"targets": 24, "likes": 24, "comments": 12, "replies": 12}
    result: dict[str, int] = {}
    for key, maximum in limits.items():
        raw = value[key]
        if type(raw) is not int or not 0 <= raw <= maximum:
            raise TaskContractError(f"daily_caps.{key} is outside the product limit")
        result[key] = raw
    if result["targets"] < max(result["comments"], result["replies"]):
        raise TaskContractError("text-action caps cannot exceed target cap")
    return result


def _canonical_hash(payload: Mapping[str, object]) -> str:
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CampaignTask:
    task_id: str
    schema_version: int
    account_id: str
    campaign_id: str
    strategy_id: str
    strategy_hash: str
    source_mode: str
    source_ref: str
    source_title: str
    direct_brief: str
    start_date: str
    end_date: str
    duration_days: int
    timezone: str
    daily_window_start: str
    daily_window_end: str
    heartbeat_minutes: int
    review_time: str
    allowed_actions: tuple[str, ...]
    daily_caps: Mapping[str, int]
    audience_segments: tuple[str, ...]
    search_keywords: tuple[str, ...]
    exclusions: tuple[str, ...]
    approval_mode: str
    status: str
    created_at: str
    updated_at: str
    content_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CampaignTask":
        input_fields = {
            "schema_version", "account_id", "campaign_id", "strategy_id", "strategy_hash",
            "source_mode", "source_ref", "source_title", "direct_brief", "start_date",
            "duration_days", "timezone", "daily_window_start", "daily_window_end",
            "heartbeat_minutes", "review_time", "allowed_actions", "daily_caps",
            "audience_segments", "search_keywords", "exclusions", "approval_mode", "created_at",
        }
        persisted_fields = input_fields | {"task_id", "end_date", "status", "updated_at", "content_hash"}
        keys = set(value)
        if keys != input_fields and keys != persisted_fields:
            raise TaskContractError("campaign task fields are incomplete or unknown")
        if value["schema_version"] != 1:
            raise TaskContractError("campaign task schema_version must be 1")
        source_mode = _text("source_mode", value["source_mode"])
        if source_mode not in SOURCE_MODES:
            raise TaskContractError("invalid source_mode")
        source_ref = _text("source_ref", value["source_ref"], optional=True)
        source_title = _text("source_title", value["source_title"], optional=True, limit=200)
        direct_brief = _text("direct_brief", value["direct_brief"], optional=True, limit=2000)
        if source_mode == "account_note" and (source_ref or direct_brief):
            raise TaskContractError("account_note cannot pre-bind source_ref or direct_brief")
        if source_mode == "specified_note" and not (source_ref or source_title):
            raise TaskContractError("specified_note requires source_ref or source_title")
        if source_mode == "direct_brief" and not direct_brief:
            raise TaskContractError("direct_brief source requires direct_brief text")
        start_date = _day("start_date", value["start_date"])
        duration = value["duration_days"]
        if type(duration) is not int or not 1 <= duration <= 30:
            raise TaskContractError("duration_days must be 1-30")
        end_date = (date.fromisoformat(start_date) + timedelta(days=duration - 1)).isoformat()
        timezone = _text("timezone", value["timezone"], limit=64)
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise TaskContractError("timezone is not recognized") from exc
        window_start = _clock("daily_window_start", value["daily_window_start"])
        window_end = _clock("daily_window_end", value["daily_window_end"])
        if window_start >= window_end:
            raise TaskContractError("daily window must be a same-day increasing range")
        heartbeat = value["heartbeat_minutes"]
        if type(heartbeat) is not int or not 10 <= heartbeat <= 60:
            raise TaskContractError("heartbeat_minutes must be 10-60")
        actions = _strings("allowed_actions", value["allowed_actions"], maximum=3, required=True)
        if not set(actions).issubset(ALLOWED_ACTIONS):
            raise TaskContractError("allowed_actions must use like/comment/reply")
        approval_mode = _text("approval_mode", value["approval_mode"])
        if approval_mode not in APPROVAL_MODES:
            raise TaskContractError("invalid approval_mode")
        created_at = _moment("created_at", value["created_at"])
        payload: dict[str, object] = {
            "schema_version": 1,
            "account_id": _safe_id("account_id", value["account_id"]),
            "campaign_id": _safe_id("campaign_id", value["campaign_id"]),
            "strategy_id": _safe_id("strategy_id", value["strategy_id"]),
            "strategy_hash": _text("strategy_hash", value["strategy_hash"], limit=128),
            "source_mode": source_mode,
            "source_ref": source_ref,
            "source_title": source_title,
            "direct_brief": direct_brief,
            "start_date": start_date,
            "duration_days": duration,
            "timezone": timezone,
            "daily_window_start": window_start,
            "daily_window_end": window_end,
            "heartbeat_minutes": heartbeat,
            "review_time": _clock("review_time", value["review_time"]),
            "allowed_actions": list(actions),
            "daily_caps": _caps(value["daily_caps"]),
            "audience_segments": list(_strings("audience_segments", value["audience_segments"], maximum=20, required=True)),
            "search_keywords": list(_strings("search_keywords", value["search_keywords"], maximum=30, required=True)),
            "exclusions": list(_strings("exclusions", value["exclusions"], maximum=30)),
            "approval_mode": approval_mode,
            "created_at": created_at,
        }
        digest = _canonical_hash(payload)
        if re.fullmatch(r"[0-9a-f]{64}", str(payload["strategy_hash"])) is None:
            raise TaskContractError("strategy_hash must be SHA-256 hex")
        task_id = "task_" + digest[:16]
        if "task_id" in value and value["task_id"] != task_id:
            raise TaskContractError("campaign task_id mismatch")
        if "content_hash" in value and value["content_hash"] != digest:
            raise TaskContractError("campaign task content hash mismatch")
        if "end_date" in value and value["end_date"] != end_date:
            raise TaskContractError("campaign task end_date mismatch")
        status = value.get("status", "draft")
        if status not in TASK_STATUSES:
            raise TaskContractError("invalid campaign task status")
        updated_at = _moment("updated_at", value.get("updated_at", created_at))
        return cls(
            task_id=task_id, schema_version=1, account_id=str(payload["account_id"]),
            campaign_id=str(payload["campaign_id"]), strategy_id=str(payload["strategy_id"]),
            strategy_hash=str(payload["strategy_hash"]), source_mode=source_mode,
            source_ref=source_ref, source_title=source_title, direct_brief=direct_brief,
            start_date=start_date, end_date=end_date, duration_days=duration, timezone=timezone,
            daily_window_start=window_start, daily_window_end=window_end,
            heartbeat_minutes=heartbeat, review_time=str(payload["review_time"]),
            allowed_actions=actions, daily_caps=dict(payload["daily_caps"]),
            audience_segments=tuple(payload["audience_segments"]),
            search_keywords=tuple(payload["search_keywords"]), exclusions=tuple(payload["exclusions"]),
            approval_mode=approval_mode, status=str(status), created_at=created_at,
            updated_at=updated_at, content_hash=digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id, "schema_version": 1, "account_id": self.account_id,
            "campaign_id": self.campaign_id, "strategy_id": self.strategy_id,
            "strategy_hash": self.strategy_hash, "source_mode": self.source_mode,
            "source_ref": self.source_ref, "source_title": self.source_title,
            "direct_brief": self.direct_brief, "start_date": self.start_date,
            "end_date": self.end_date, "duration_days": self.duration_days,
            "timezone": self.timezone, "daily_window_start": self.daily_window_start,
            "daily_window_end": self.daily_window_end, "heartbeat_minutes": self.heartbeat_minutes,
            "review_time": self.review_time, "allowed_actions": list(self.allowed_actions),
            "daily_caps": dict(self.daily_caps), "audience_segments": list(self.audience_segments),
            "search_keywords": list(self.search_keywords), "exclusions": list(self.exclusions),
            "approval_mode": self.approval_mode, "status": self.status,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class CampaignRunAuthorization:
    authorization_id: str
    schema_version: int
    task_id: str
    task_hash: str
    account_id: str
    allowed_actions: tuple[str, ...]
    daily_caps: Mapping[str, int]
    valid_from: str
    valid_until: str
    approval_mode: str
    confirmed_at: str
    content_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CampaignRunAuthorization":
        expected = {"authorization_id", "schema_version", "task_id", "task_hash", "account_id", "allowed_actions", "daily_caps", "valid_from", "valid_until", "approval_mode", "confirmed_at", "content_hash"}
        if set(value) != expected or value["schema_version"] != 1:
            raise TaskContractError("campaign authorization fields are incomplete or unknown")
        payload = {key: value[key] for key in expected - {"authorization_id", "content_hash"}}
        payload["task_id"] = _safe_id("task_id", payload["task_id"])
        payload["account_id"] = _safe_id("account_id", payload["account_id"])
        payload["task_hash"] = _text("task_hash", payload["task_hash"], limit=128)
        payload["allowed_actions"] = list(_strings("allowed_actions", payload["allowed_actions"], maximum=3, required=True))
        payload["daily_caps"] = _caps(payload["daily_caps"])
        payload["valid_from"] = _moment("valid_from", payload["valid_from"])
        payload["valid_until"] = _moment("valid_until", payload["valid_until"])
        payload["confirmed_at"] = _moment("confirmed_at", payload["confirmed_at"])
        if payload["approval_mode"] not in APPROVAL_MODES:
            raise TaskContractError("invalid authorization approval_mode")
        digest = _canonical_hash(payload)
        auth_id = "task_auth_" + digest[:16]
        if value["authorization_id"] != auth_id or value["content_hash"] != digest:
            raise TaskContractError("campaign authorization hash mismatch")
        return cls(
            authorization_id=auth_id, schema_version=1, task_id=str(payload["task_id"]),
            task_hash=str(payload["task_hash"]), account_id=str(payload["account_id"]),
            allowed_actions=tuple(payload["allowed_actions"]), daily_caps=dict(payload["daily_caps"]),
            valid_from=str(payload["valid_from"]), valid_until=str(payload["valid_until"]),
            approval_mode=str(payload["approval_mode"]), confirmed_at=str(payload["confirmed_at"]),
            content_hash=digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id, "schema_version": 1,
            "task_id": self.task_id, "task_hash": self.task_hash,
            "account_id": self.account_id, "allowed_actions": list(self.allowed_actions),
            "daily_caps": dict(self.daily_caps), "valid_from": self.valid_from,
            "valid_until": self.valid_until, "approval_mode": self.approval_mode,
            "confirmed_at": self.confirmed_at, "content_hash": self.content_hash,
        }


def authorize_campaign_task(task: CampaignTask, *, confirmed_at: str, confirmation: str) -> CampaignRunAuthorization:
    if confirmation != BOUNDED_RUN_CONFIRMATION:
        raise TaskContractError("exact bounded campaign confirmation is required")
    if task.status not in {"draft", "approved"}:
        raise TaskContractError("only a draft or approved task can be authorized")
    confirmed_at = _moment("confirmed_at", confirmed_at)
    zone = ZoneInfo(task.timezone)
    valid_from = datetime.combine(date.fromisoformat(task.start_date), time.min, zone).isoformat()
    valid_until = datetime.combine(date.fromisoformat(task.end_date), time.max, zone).isoformat()
    payload: dict[str, object] = {
        "schema_version": 1, "task_id": task.task_id, "task_hash": task.content_hash,
        "account_id": task.account_id, "allowed_actions": list(task.allowed_actions),
        "daily_caps": dict(task.daily_caps), "valid_from": valid_from,
        "valid_until": valid_until, "approval_mode": task.approval_mode,
        "confirmed_at": confirmed_at,
    }
    digest = _canonical_hash(payload)
    return CampaignRunAuthorization(
        authorization_id="task_auth_" + digest[:16], schema_version=1,
        task_id=task.task_id, task_hash=task.content_hash, account_id=task.account_id,
        allowed_actions=task.allowed_actions, daily_caps=dict(task.daily_caps),
        valid_from=valid_from, valid_until=valid_until, approval_mode=task.approval_mode,
        confirmed_at=confirmed_at, content_hash=digest,
    )


class CampaignTaskStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "tasks"

    def task_path(self, task_id: str) -> Path:
        return self.root / f"{_safe_id('task_id', task_id)}.json"

    def authorization_path(self, task_id: str) -> Path:
        return self.root / "authorizations" / f"{_safe_id('task_id', task_id)}.json"

    def create(self, task: CampaignTask) -> Path:
        path = self.task_path(task.task_id)
        existing = read_json(path, default=None)
        if existing is not None and CampaignTask.from_dict(existing).content_hash != task.content_hash:
            raise TaskContractError("task_id collision with different content")
        write_json_atomic(path, task.to_dict())
        return path

    def load(self, task_id: str) -> CampaignTask:
        value = read_json(self.task_path(task_id), default=None)
        if not isinstance(value, dict):
            raise TaskContractError("campaign task is missing")
        return CampaignTask.from_dict(value)

    def transition(self, task_id: str, *, to_status: str, changed_at: str) -> CampaignTask:
        current = self.load(task_id)
        if to_status not in TRANSITIONS[current.status]:
            raise TaskContractError(f"invalid task transition {current.status}->{to_status}")
        updated = replace(current, status=to_status, updated_at=_moment("changed_at", changed_at))
        write_json_atomic(self.task_path(task_id), updated.to_dict())
        return updated

    def save_authorization(self, authorization: CampaignRunAuthorization) -> Path:
        task = self.load(authorization.task_id)
        if task.content_hash != authorization.task_hash or task.account_id != authorization.account_id:
            raise TaskContractError("authorization does not match stored task")
        path = self.authorization_path(task.task_id)
        write_json_atomic(path, authorization.to_dict())
        return path

    def load_authorization(self, task_id: str) -> CampaignRunAuthorization:
        value = read_json(self.authorization_path(task_id), default=None)
        if not isinstance(value, dict):
            raise TaskContractError("campaign task authorization is missing")
        return CampaignRunAuthorization.from_dict(value)


class TaskOccurrenceStore:
    """Cross-process idempotency for Codex scheduler wake-ups.

    An occurrence lease only prevents duplicate workers.  It never grants a
    platform write; the task authorization, heartbeat item lease, Run Agent
    write lease, current-page binding and write journal remain mandatory.
    """

    def __init__(self, runtime_dir: Path, *, lease_seconds: int = 900) -> None:
        self.root = Path(runtime_dir) / "tasks" / "occurrences"
        if not 300 <= lease_seconds <= 1800:
            raise TaskContractError("occurrence lease_seconds must be 300-1800")
        self.lease_seconds = lease_seconds

    def path(self, task_id: str) -> Path:
        return self.root / f"{_safe_id('task_id', task_id)}.json"

    @staticmethod
    def _scheduled_occurrence(task: CampaignTask, *, kind: str, at: str) -> tuple[str, datetime] | None:
        if kind not in SCHEDULE_JOB_KINDS:
            raise TaskContractError("unsupported schedule job kind")
        moment = datetime.fromisoformat(_moment("at", at).replace("Z", "+00:00")).astimezone(
            ZoneInfo(task.timezone)
        )
        current_day = moment.date().isoformat()
        if task.status != "running" or not task.start_date <= current_day <= task.end_date:
            return None
        start_clock = time.fromisoformat(task.daily_window_start)
        end_clock = time.fromisoformat(task.daily_window_end)
        start = datetime.combine(moment.date(), start_clock, moment.tzinfo)
        end = datetime.combine(moment.date(), end_clock, moment.tzinfo)
        if kind == "heartbeat":
            if not start <= moment < end:
                return None
            slot = int((moment - start).total_seconds()) // (task.heartbeat_minutes * 60)
            scheduled = start + timedelta(minutes=slot * task.heartbeat_minutes)
            suffix = f"heartbeat_{slot:03d}"
        else:
            scheduled_clock = start_clock if kind == "daily_plan" else time.fromisoformat(task.review_time)
            scheduled = datetime.combine(moment.date(), scheduled_clock, moment.tzinfo)
            # Codex can wake a little late, but never claims an older missed occurrence.
            if not scheduled <= moment < scheduled + timedelta(minutes=15):
                return None
            suffix = kind
        occurrence_id = f"{task.task_id}_{current_day.replace('-', '')}_{suffix}"
        return occurrence_id, scheduled

    def claim(
        self,
        task: CampaignTask,
        *,
        kind: str,
        at: str,
        worker_id: str,
        token: str | None = None,
    ) -> dict[str, object]:
        worker = _safe_id("worker_id", worker_id)
        resolved = self._scheduled_occurrence(task, kind=kind, at=at)
        if resolved is None:
            return {
                "claimed": False,
                "reason": "outside_current_occurrence",
                "task_id": task.task_id,
                "kind": kind,
                "occurrence_id": None,
                "platform_write_authorized": False,
            }
        occurrence_id, scheduled = resolved
        now = datetime.fromisoformat(_moment("at", at).replace("Z", "+00:00"))
        lease_token = token or secrets.token_hex(16)
        if not isinstance(lease_token, str) or len(lease_token) < 16:
            raise TaskContractError("occurrence lease token is invalid")
        decision: dict[str, object] = {}

        def updater(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal decision
            if state and (
                state.get("schema_version") != 1
                or state.get("task_id") != task.task_id
                or state.get("task_hash") != task.content_hash
                or not isinstance(state.get("occurrences"), dict)
            ):
                raise TaskContractError("task occurrence state is corrupt or task hash changed")
            if not state:
                state = {
                    "schema_version": 1,
                    "task_id": task.task_id,
                    "task_hash": task.content_hash,
                    "occurrences": {},
                }
            occurrences = state["occurrences"]
            current = occurrences.get(occurrence_id)
            if isinstance(current, dict):
                if current.get("status") == "leased":
                    expires = datetime.fromisoformat(str(current["lease_expires_at"]).replace("Z", "+00:00"))
                    if now >= expires:
                        current["status"] = "expired"
                        current["expired_at"] = at
                decision = {
                    "claimed": False,
                    "reason": "occurrence_already_claimed",
                    "task_id": task.task_id,
                    "kind": kind,
                    "occurrence_id": occurrence_id,
                    "status": current.get("status"),
                    "platform_write_authorized": False,
                }
                return state
            expiry = now + timedelta(seconds=self.lease_seconds)
            occurrences[occurrence_id] = {
                "occurrence_id": occurrence_id,
                "kind": kind,
                "scheduled_at": scheduled.isoformat(),
                "status": "leased",
                "worker_id": worker,
                "claimed_at": at,
                "lease_token": lease_token,
                "lease_expires_at": expiry.isoformat(),
                "completed_at": None,
                "outcome": None,
            }
            # Keep one bounded task file without deleting active/current evidence.
            if len(occurrences) > 200:
                removable = [
                    key for key, value in occurrences.items()
                    if key != occurrence_id and isinstance(value, dict) and value.get("status") in {"completed", "expired"}
                ]
                for key in sorted(removable)[: len(occurrences) - 200]:
                    del occurrences[key]
            decision = {
                "claimed": True,
                "reason": "occurrence_lease_acquired",
                "task_id": task.task_id,
                "kind": kind,
                "occurrence_id": occurrence_id,
                "lease_token": lease_token,
                "lease_expires_at": expiry.isoformat(),
                "platform_write_authorized": False,
            }
            return state

        update_json_object(self.path(task.task_id), updater)
        return decision

    def complete(
        self,
        task: CampaignTask,
        *,
        occurrence_id: str,
        lease_token: str,
        completed_at: str,
        outcome: str,
    ) -> dict[str, object]:
        if re.fullmatch(r"[A-Za-z0-9_-]{8,220}", occurrence_id or "") is None:
            raise TaskContractError("occurrence_id is invalid")
        if not lease_token or outcome not in {"completed", "noop", "blocked"}:
            raise TaskContractError("occurrence completion fields are invalid")
        completed = datetime.fromisoformat(_moment("completed_at", completed_at).replace("Z", "+00:00"))
        result: dict[str, object] = {}

        def updater(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            if (
                state.get("task_id") != task.task_id
                or state.get("task_hash") != task.content_hash
                or not isinstance(state.get("occurrences"), dict)
            ):
                raise TaskContractError("task occurrence state is missing or mismatched")
            current = state["occurrences"].get(occurrence_id)
            if not isinstance(current, dict) or current.get("status") != "leased":
                raise TaskContractError("occurrence does not hold an active lease")
            if current.get("lease_token") != lease_token:
                raise TaskContractError("occurrence lease token mismatch")
            claimed = datetime.fromisoformat(str(current["claimed_at"]).replace("Z", "+00:00"))
            expiry = datetime.fromisoformat(str(current["lease_expires_at"]).replace("Z", "+00:00"))
            if completed < claimed or completed > expiry:
                raise TaskContractError("occurrence completion is outside the active lease")
            current["status"] = "completed"
            current["completed_at"] = completed_at
            current["outcome"] = outcome
            current["lease_token"] = None
            result = {
                "completed": True,
                "task_id": task.task_id,
                "occurrence_id": occurrence_id,
                "kind": current["kind"],
                "outcome": outcome,
                "platform_write_authorized": False,
            }
            return state

        update_json_object(self.path(task.task_id), updater)
        return result


def build_schedule_manifest(task: CampaignTask) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "task_hash": task.content_hash,
        "timezone": task.timezone,
        "active_date_range": {"start": task.start_date, "end": task.end_date},
        "catch_up_policy": "skip_missed_runs_no_burst",
        "max_primary_targets_per_heartbeat": 1,
        "max_platform_writes_per_heartbeat": 1,
        "minimum_write_interval_seconds": 600,
        "write_interval_scope": "account_global_including_same_note_and_target",
        "unknown_write_retry_policy": "never_auto_retry",
        "prewrite_retry_policy": "structured_evidence_required",
        "occurrence_claim_required": True,
        "occurrence_claim_is_write_authorization": False,
        "jobs": [
            {
                "job_id": f"{task.task_id}_daily_plan",
                "kind": "daily_plan",
                "local_time": task.daily_window_start,
                "prompt": (
                    f"Use the packaged XHS Operations Core Skill for task {task.task_id}. "
                    "Use public engage task-claim for the current daily_plan occurrence once; "
                    "stop on duplicate or missed occurrence. "
                    "Load the exact task and authorization, compile today's bounded query order "
                    "and action budgets, and perform no platform write. Stop if Setup, account, "
                    "Campaign, strategy hash, date, authorization, or release checks fail."
                ),
            },
            {
                "job_id": f"{task.task_id}_heartbeat",
                "kind": "heartbeat",
                "every_minutes": task.heartbeat_minutes,
                "window_start": task.daily_window_start,
                "window_end": task.daily_window_end,
                "prompt": (
                    f"Use the packaged XHS Operations Core Skill for one heartbeat of task {task.task_id}. "
                    "Use public engage task-claim for the current heartbeat occurrence once; "
                    "this claim is coordination only and never write authorization. "
                    "First run engage task-due. If due, keep one saved search batch, open and read "
                    "one candidate, compile one public engage atomic preview for that note, "
                    "then bind the exact current-page plan with engage task-plan-approve and process "
                    "exactly one approved platform write using the same task ID. Never bundle a like "
                    "with a comment or reply. Persist each result immediately; never catch up missed runs."
                ),
            },
            {
                "job_id": f"{task.task_id}_daily_review",
                "kind": "daily_review",
                "local_time": task.review_time,
                "prompt": (
                    f"Use the packaged XHS Operations Core Skill for task {task.task_id} daily review. "
                    "Use public engage task-claim for the current daily_review occurrence once; "
                    "stop on duplicate or missed occurrence. Use public review status/list/export "
                    "to summarize persisted operation facts, unknowns, and blockers. "
                    "Perform no platform write or strategy adjustment."
                ),
            },
        ],
        "platform_actions_executed": 0,
    }


def evaluate_task_due(task: CampaignTask, *, at: str) -> dict[str, object]:
    moment_text = _moment("at", at)
    moment = datetime.fromisoformat(moment_text.replace("Z", "+00:00")).astimezone(ZoneInfo(task.timezone))
    today = moment.date().isoformat()
    in_dates = task.start_date <= today <= task.end_date
    in_window = task.daily_window_start <= moment.strftime("%H:%M") < task.daily_window_end
    executable_status = task.status == "running"
    due = in_dates and in_window and executable_status
    reason = "due"
    if not executable_status:
        reason = f"task_{task.status}"
    elif today < task.start_date:
        reason = "before_start"
    elif today > task.end_date:
        reason = "expired"
    elif not in_window:
        reason = "outside_daily_window"
    return {
        "task_id": task.task_id, "at": moment.isoformat(), "due": due,
        "reason": reason, "max_primary_targets": 1 if due else 0,
        "catch_up_allowed": False,
    }


def evaluate_task_execution_authorization(
    task: CampaignTask,
    authorization: CampaignRunAuthorization,
    *,
    at: str,
    account_id: str,
    campaign_id: str,
    required_actions: tuple[str, ...],
) -> dict[str, object]:
    """Fail closed unless one exact interaction plan is covered by a live task.

    A scheduler wake-up is never authorization by itself.  This evaluator binds
    the stored task and authorization to the account, Campaign, time window and
    exact action types requested by the current-page plan.
    """

    due = evaluate_task_due(task, at=at)
    at_text = _moment("at", at)
    moment = datetime.fromisoformat(at_text.replace("Z", "+00:00"))
    valid_from = datetime.fromisoformat(authorization.valid_from.replace("Z", "+00:00"))
    valid_until = datetime.fromisoformat(authorization.valid_until.replace("Z", "+00:00"))
    confirmed = datetime.fromisoformat(authorization.confirmed_at.replace("Z", "+00:00"))
    requested = tuple(dict.fromkeys(_text("required action", item, limit=16) for item in required_actions))
    blockers: list[str] = []
    if task.task_id != authorization.task_id:
        blockers.append("task_authorization_id_mismatch")
    if task.content_hash != authorization.task_hash:
        blockers.append("task_authorization_hash_mismatch")
    if task.account_id != authorization.account_id or task.account_id != account_id:
        blockers.append("task_account_mismatch")
    if task.campaign_id != campaign_id:
        blockers.append("task_campaign_mismatch")
    if task.approval_mode != "bounded_campaign" or authorization.approval_mode != "bounded_campaign":
        blockers.append("bounded_campaign_approval_required")
    if not due["due"]:
        blockers.append(f"task_not_due:{due['reason']}")
    if confirmed > moment:
        blockers.append("task_authorization_not_yet_confirmed")
    if not valid_from <= moment <= valid_until:
        blockers.append("task_authorization_outside_validity")
    if any(item not in ALLOWED_ACTIONS for item in requested):
        blockers.append("unsupported_required_action")
    if len(requested) != 1:
        blockers.append("exactly_one_platform_write_required")
    if any(item not in task.allowed_actions or item not in authorization.allowed_actions for item in requested):
        blockers.append("required_action_not_authorized")
    cap_keys = {"like": "likes", "comment": "comments", "reply": "replies"}
    if any(authorization.daily_caps.get(cap_keys[item], 0) < 1 for item in requested if item in cap_keys):
        blockers.append("required_action_daily_cap_is_zero")
    if authorization.daily_caps != task.daily_caps:
        blockers.append("task_authorization_caps_mismatch")
    return {
        "allowed": not blockers,
        "task_id": task.task_id,
        "authorization_id": authorization.authorization_id,
        "task_hash": task.content_hash,
        "account_id": task.account_id,
        "campaign_id": task.campaign_id,
        "required_actions": list(requested),
        "daily_caps": dict(task.daily_caps),
        "max_primary_targets": 1 if not blockers else 0,
        "blockers": blockers,
    }
