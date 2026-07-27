"""Deterministic one-time account setup contracts and derived readiness status."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .account_voice import build_account_voice_status
from .storage import read_json, read_jsonl, write_json_atomic


class OnboardingError(ValueError):
    pass


ACCOUNT_TYPES = {"personal", "brand", "organization"}
ALLOWED_ACTIONS = {"like", "comment", "reply"}
APPROVAL_MODES = {"review_each", "bounded_campaign"}
PLATFORM_READ_CONFIRMATION = "I_ENABLE_XHS_OPERATIONS_CORE_PLATFORM_READ"


def _text(name: str, value: object, *, optional: bool = False, limit: int = 500) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise OnboardingError(f"{name} must be text")
    text = " ".join(value.split()).strip()
    if not text and not optional:
        raise OnboardingError(f"{name} is required")
    if len(text) > limit:
        raise OnboardingError(f"{name} is too long")
    return text


def _safe_id(name: str, value: object) -> str:
    text = _text(name, value, limit=128)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text) is None:
        raise OnboardingError(f"{name} must be a safe id")
    return text


def _strings(name: str, value: object, *, maximum: int = 30) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise OnboardingError(f"{name} must be a list with at most {maximum} items")
    rows = tuple(_text(f"{name} item", item, limit=200) for item in value)
    if len(rows) != len(set(rows)):
        raise OnboardingError(f"{name} cannot contain duplicates")
    return rows


def _clock(name: str, value: object) -> str:
    text = _text(name, value, limit=5)
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text) is None:
        raise OnboardingError(f"{name} must use HH:MM")
    return text


@dataclass(frozen=True)
class AccountSetupProfile:
    profile_id: str
    schema_version: int
    account_id: str
    display_name: str
    account_type: str
    identity_summary: str
    verified_experience: tuple[str, ...]
    tone_preferences: tuple[str, ...]
    prohibited_claims: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    raw_reply_corpus_enabled: bool
    timezone: str
    default_duration_days: int
    daily_window_start: str
    daily_window_end: str
    heartbeat_minutes: int
    max_targets_per_day: int
    max_likes_per_day: int
    max_comments_per_day: int
    max_replies_per_day: int
    review_time: str
    approval_mode: str
    corpus_retention_days: int
    created_at: str
    content_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AccountSetupProfile":
        common_fields = {
            "schema_version", "account_id", "display_name", "account_type",
            "identity_summary", "verified_experience", "tone_preferences",
            "prohibited_claims", "allowed_actions", "raw_reply_corpus_enabled",
            "timezone", "default_duration_days", "daily_window_start",
            "daily_window_end", "heartbeat_minutes", "max_targets_per_day",
            "max_likes_per_day", "max_comments_per_day", "max_replies_per_day",
            "approval_mode", "corpus_retention_days", "created_at",
        }
        schema_version = value.get("schema_version")
        if schema_version not in {1, 2}:
            raise OnboardingError("account setup profile schema_version must be 1 or 2")
        input_fields = common_fields | ({"review_time"} if schema_version == 1 else set())
        allowed_input_fields = (
            {frozenset(input_fields)}
            if schema_version == 1
            else {frozenset(common_fields), frozenset(common_fields | {"review_time"})}
        )
        allowed_fields = {
            *allowed_input_fields,
            *(fields | {"profile_id", "content_hash"} for fields in allowed_input_fields),
        }
        keys = set(value)
        if frozenset(keys) not in allowed_fields:
            raise OnboardingError("account setup profile fields are incomplete or unknown")
        account_type = _text("account_type", value["account_type"])
        if account_type not in ACCOUNT_TYPES:
            raise OnboardingError("invalid account_type")
        allowed = _strings("allowed_actions", value["allowed_actions"], maximum=3)
        if not allowed or not set(allowed).issubset(ALLOWED_ACTIONS):
            raise OnboardingError("allowed_actions must use like/comment/reply")
        if type(value["raw_reply_corpus_enabled"]) is not bool:
            raise OnboardingError("raw_reply_corpus_enabled must be boolean")
        timezone = _text("timezone", value["timezone"], limit=64)
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise OnboardingError("timezone is not recognized") from exc
        start = _clock("daily_window_start", value["daily_window_start"])
        end = _clock("daily_window_end", value["daily_window_end"])
        if start >= end:
            raise OnboardingError("daily window must be a same-day increasing range")
        created_at = _text("created_at", value["created_at"], limit=64)
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OnboardingError("created_at must be ISO-8601") from exc
        if created.tzinfo is None or created.utcoffset() is None:
            raise OnboardingError("created_at must include timezone")
        duration = value["default_duration_days"]
        heartbeat = value["heartbeat_minutes"]
        targets = value["max_targets_per_day"]
        likes = value["max_likes_per_day"]
        comments = value["max_comments_per_day"]
        replies = value["max_replies_per_day"]
        retention = value["corpus_retention_days"]
        if type(duration) is not int or not 1 <= duration <= 30:
            raise OnboardingError("default_duration_days must be 1-30")
        if type(heartbeat) is not int or not 10 <= heartbeat <= 60:
            raise OnboardingError("heartbeat_minutes must be 10-60")
        for name, raw, maximum in (
            ("max_targets_per_day", targets, 24),
            ("max_likes_per_day", likes, 24),
            ("max_comments_per_day", comments, 12),
            ("max_replies_per_day", replies, 12),
        ):
            if type(raw) is not int or not 0 <= raw <= maximum:
                raise OnboardingError(f"{name} is outside the product limit")
        if targets < max(comments, replies):
            raise OnboardingError("text-action caps cannot exceed target cap")
        if type(retention) is not int or not 30 <= retention <= 3650:
            raise OnboardingError("corpus_retention_days must be 30-3650")
        approval_mode = _text("approval_mode", value["approval_mode"])
        if approval_mode not in APPROVAL_MODES:
            raise OnboardingError("invalid approval_mode")
        review_time = (
            _clock("review_time", value["review_time"])
            if "review_time" in value and str(value["review_time"] or "").strip()
            else ""
        )
        if schema_version == 1 and not review_time:
            raise OnboardingError("schema_version 1 requires review_time")
        payload = {
            "schema_version": schema_version,
            "account_id": _safe_id("account_id", value["account_id"]),
            "display_name": _text("display_name", value["display_name"], limit=100),
            "account_type": account_type,
            "identity_summary": _text("identity_summary", value["identity_summary"]),
            "verified_experience": list(_strings("verified_experience", value["verified_experience"])),
            "tone_preferences": list(_strings("tone_preferences", value["tone_preferences"])),
            "prohibited_claims": list(_strings("prohibited_claims", value["prohibited_claims"])),
            "allowed_actions": list(allowed),
            "raw_reply_corpus_enabled": value["raw_reply_corpus_enabled"] is True,
            "timezone": timezone,
            "default_duration_days": duration,
            "daily_window_start": start,
            "daily_window_end": end,
            "heartbeat_minutes": heartbeat,
            "max_targets_per_day": targets,
            "max_likes_per_day": likes,
            "max_comments_per_day": comments,
            "max_replies_per_day": replies,
            "approval_mode": approval_mode,
            "corpus_retention_days": retention,
            "created_at": created_at,
        }
        if review_time:
            payload["review_time"] = review_time
        digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        profile_id = "account_setup_" + digest[:16]
        if "profile_id" in value and value["profile_id"] != profile_id:
            raise OnboardingError("account setup profile_id mismatch")
        if "content_hash" in value and value["content_hash"] != digest:
            raise OnboardingError("account setup content hash mismatch")
        return cls(
            profile_id=profile_id,
            schema_version=schema_version,
            account_id=payload["account_id"],
            display_name=payload["display_name"],
            account_type=account_type,
            identity_summary=payload["identity_summary"],
            verified_experience=tuple(payload["verified_experience"]),
            tone_preferences=tuple(payload["tone_preferences"]),
            prohibited_claims=tuple(payload["prohibited_claims"]),
            allowed_actions=allowed,
            raw_reply_corpus_enabled=payload["raw_reply_corpus_enabled"],
            timezone=timezone,
            default_duration_days=duration,
            daily_window_start=start,
            daily_window_end=end,
            heartbeat_minutes=heartbeat,
            max_targets_per_day=targets,
            max_likes_per_day=likes,
            max_comments_per_day=comments,
            max_replies_per_day=replies,
            review_time=review_time,
            approval_mode=approval_mode,
            corpus_retention_days=retention,
            created_at=created_at,
            content_hash=digest,
        )

    def to_dict(self) -> dict[str, object]:
        payload = {
            "profile_id": self.profile_id,
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "display_name": self.display_name,
            "account_type": self.account_type,
            "identity_summary": self.identity_summary,
            "verified_experience": list(self.verified_experience),
            "tone_preferences": list(self.tone_preferences),
            "prohibited_claims": list(self.prohibited_claims),
            "allowed_actions": list(self.allowed_actions),
            "raw_reply_corpus_enabled": self.raw_reply_corpus_enabled,
            "timezone": self.timezone,
            "default_duration_days": self.default_duration_days,
            "daily_window_start": self.daily_window_start,
            "daily_window_end": self.daily_window_end,
            "heartbeat_minutes": self.heartbeat_minutes,
            "max_targets_per_day": self.max_targets_per_day,
            "max_likes_per_day": self.max_likes_per_day,
            "max_comments_per_day": self.max_comments_per_day,
            "max_replies_per_day": self.max_replies_per_day,
            "approval_mode": self.approval_mode,
            "corpus_retention_days": self.corpus_retention_days,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
        }
        if self.review_time:
            payload["review_time"] = self.review_time
        return payload


class AccountSetupStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.path = Path(runtime_dir) / "setup" / "account_profile.json"

    def save(self, profile: AccountSetupProfile) -> Path:
        existing = read_json(self.path, default=None)
        if existing is not None:
            loaded = AccountSetupProfile.from_dict(existing)
            if loaded.account_id != profile.account_id:
                raise OnboardingError("existing account setup belongs to another account")
        write_json_atomic(self.path, profile.to_dict())
        return self.path

    def load(self) -> AccountSetupProfile:
        value = read_json(self.path, default=None)
        if not isinstance(value, dict):
            raise OnboardingError("account setup profile is missing")
        return AccountSetupProfile.from_dict(value)


PLATFORM_SETUP_STEPS = (
    "installation",
    "account_profile",
    "bridge_extension",
    "login_calibration",
)


def _valid_account_profile(value: object, account_id: str) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        return AccountSetupProfile.from_dict(value).account_id == account_id
    except OnboardingError:
        return False


def build_setup_status(
    runtime_dir: Path,
    *,
    account_id: str,
    connection_ready: bool,
    login_ready: bool,
) -> dict[str, object]:
    runtime = Path(runtime_dir)
    account_id = _safe_id("account_id", account_id)
    receipt = read_json(runtime / "setup" / "receipt.json", default=None)
    account_profile = read_json(runtime / "setup" / "account_profile.json", default=None)
    account_voice = build_account_voice_status(runtime, account_id=account_id)
    setup_profile = None
    if isinstance(account_profile, dict):
        try:
            candidate = AccountSetupProfile.from_dict(account_profile)
            if candidate.account_id == account_id:
                setup_profile = candidate
        except OnboardingError:
            setup_profile = None
    readonly_search = False
    session_dir = runtime / "interaction_sessions"
    if session_dir.is_dir():
        for path in session_dir.glob("*.json"):
            if path.name in {"approvals.json", "STOP.json"}:
                continue
            session = read_json(path, default=None)
            if not isinstance(session, dict) or session.get("account_id") != account_id:
                continue
            if session.get("search_count") == 1 and session.get("note_id") and session.get("stage") in {
                "note_read", "completed"
            }:
                readonly_search = True
                break
    action_types = {
        str(item.get("action_type"))
        for item in read_jsonl(runtime / "comment_flow" / "actions.jsonl")
        if item.get("account_id") == account_id and item.get("status") == "verified"
    }
    steps = {
        "installation": isinstance(receipt, dict) and receipt.get("account_id") == account_id,
        "account_profile": _valid_account_profile(account_profile, account_id),
        "bridge_extension": connection_ready,
        "login_calibration": login_ready,
        "account_voice": account_voice["ready"],
        "post_voice": account_voice["post_voice"]["valid"],
        "reply_corpus": account_voice["corpus"]["valid"],
        "style_profile": account_voice["profile"]["valid"],
        "readonly_search": readonly_search,
        "live_smoke": {"like", "comment", "reply"}.issubset(action_types),
    }
    missing = [step for step in PLATFORM_SETUP_STEPS if not steps[step]]
    platform_ready = not missing
    voice_ready = account_voice["voice_ready"] is True
    voice_integrity_ready = account_voice["status"] != "invalid"
    operations_ready = platform_ready and voice_integrity_ready
    drafting_policy = dict(account_voice["drafting_policy"])
    if drafting_policy["mode"] == "account_voice" and setup_profile is not None:
        drafting_policy["text_write_approval_mode"] = setup_profile.approval_mode
    operations_blockers = [] if operations_ready else [
        *missing,
        *(account_voice["blockers"] if not voice_integrity_ready else []),
    ]
    return {
        "schema_version": 3,
        "account_id": account_id,
        "steps": steps,
        "completed_steps": len(PLATFORM_SETUP_STEPS) - len(missing),
        "total_steps": len(PLATFORM_SETUP_STEPS),
        "setup_complete": platform_ready,
        "platform_ready": platform_ready,
        "voice_ready": voice_ready,
        "operations_ready": operations_ready,
        "operations_ready_scope": "local_draft_preview_and_exact_approval_prerequisites",
        "live_write_ready": False,
        "next_step": missing[0] if missing else "ready_for_operations",
        "voice_next_step": account_voice["next_step"],
        "blockers": missing,
        "voice_blockers": account_voice["blockers"],
        "operations_blockers": operations_blockers,
        "account_voice": account_voice,
        "drafting_policy": drafting_policy,
        "operational_observations": {
            "readonly_search_seen": readonly_search,
            "verified_public_action_types": sorted(action_types & {"like", "comment", "reply"}),
        },
        "stop_expected_until_setup_complete": not platform_ready,
        "platform_actions_executed": 0,
    }


def build_handoff_plan(account_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "account_id": _safe_id("account_id", account_id),
        "execution_order": list(PLATFORM_SETUP_STEPS),
        "optional_steps": ["account_voice"],
        "manual_user_steps": [
            "load the staged unpacked XHS Bridge extension once",
            "scan the Xiaohongshu QR code in the dedicated Chrome profile",
            "confirm account identity and operating limits; reply-corpus consent is optional",
            "confirm the first campaign strategy and its bounded operating limits",
        ],
        "codex_must_not_develop_ad_hoc_browser_automation": True,
        "required_live_method": "ranfang_run_agent",
        "next_command": "setup status",
        "platform_actions_executed": 0,
    }
