"""Bounded, read-only history snapshots for account-specific style setup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


class StyleHistoryError(ValueError):
    pass


OWNER_EVIDENCE = {"author_badge", "account_user_id_match"}
PRIVATE_PATTERNS = (
    ("phone", re.compile(r"1[3-9]\d{9}")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("external_contact", re.compile(r"(?:微信|vx|V信|wxid)[：:\s_-]*[A-Za-z0-9_-]{4,}", re.I)),
    ("url", re.compile(r"https?://\S+", re.I)),
)


def _redact(value: Any) -> tuple[str, tuple[str, ...]]:
    text = " ".join(str(value or "").split())
    flags: list[str] = []
    for name, pattern in PRIVATE_PATTERNS:
        if pattern.search(text):
            flags.append(name)
            text = pattern.sub("[已脱敏]", text)
    return text, tuple(flags)


def _safe_ref(value: Any) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StyleHistoryError("history note ref must be an http(s) URL")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _timestamp(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise StyleHistoryError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StyleHistoryError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StyleHistoryError(f"{field} must include a timezone")
    return value


@dataclass(frozen=True)
class HistoryComment:
    comment_id: str
    parent_comment_id: str | None
    commenter_role: str
    text: str
    published_at: str | None
    ownership_evidence: str | None
    privacy_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.comment_id or not self.text:
            raise StyleHistoryError("history comment id and text are required")
        if self.commenter_role not in {"account_owner", "visitor", "unknown"}:
            raise StyleHistoryError("invalid commenter_role")
        if self.commenter_role == "account_owner":
            if self.ownership_evidence not in OWNER_EVIDENCE or not self.parent_comment_id:
                raise StyleHistoryError("owner reply requires parent and stable ownership evidence")
        elif self.ownership_evidence is not None:
            raise StyleHistoryError("non-owner comment cannot carry ownership evidence")

    def to_dict(self) -> dict[str, object]:
        return {
            "comment_id": self.comment_id,
            "parent_comment_id": self.parent_comment_id,
            "commenter_role": self.commenter_role,
            "text": self.text,
            "published_at": self.published_at,
            "ownership_evidence": self.ownership_evidence,
            "privacy_flags": list(self.privacy_flags),
        }


@dataclass(frozen=True)
class HistoryNote:
    note_id: str
    note_ref: str
    title: str
    body: str
    published_at: str | None
    comments: tuple[HistoryComment, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "note_id": self.note_id,
            "note_ref": self.note_ref,
            "title": self.title,
            "body": self.body,
            "published_at": self.published_at,
            "comments": [item.to_dict() for item in self.comments],
        }


@dataclass(frozen=True)
class StyleHistorySnapshot:
    snapshot_id: str
    account_id: str
    consent_ref: str
    captured_at: str
    coverage: str
    setup_status: str
    notes: tuple[HistoryNote, ...]
    owned_reply_sample_ids: tuple[str, ...]
    page_count: int
    max_pages: int
    max_notes: int
    max_comments_per_note: int
    has_more: bool
    next_note_position: int | None
    read_only: bool
    platform_actions_executed: int
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "account_id": self.account_id,
            "consent_ref": self.consent_ref,
            "captured_at": self.captured_at,
            "coverage": self.coverage,
            "setup_status": self.setup_status,
            "notes": [item.to_dict() for item in self.notes],
            "owned_reply_sample_ids": list(self.owned_reply_sample_ids),
            "page_count": self.page_count,
            "max_pages": self.max_pages,
            "max_notes": self.max_notes,
            "max_comments_per_note": self.max_comments_per_note,
            "has_more": self.has_more,
            "next_note_position": self.next_note_position,
            "read_only": self.read_only,
            "platform_actions_executed": self.platform_actions_executed,
            "content_hash": self.content_hash,
        }


def build_style_history_snapshot(
    *,
    account_id: str,
    consent_ref: str,
    captured_at: str,
    capture: Mapping[str, Any],
    max_pages: int = 5,
    max_notes: int = 30,
    max_comments_per_note: int = 100,
    minimum_owned_replies: int = 3,
) -> StyleHistorySnapshot:
    if not account_id or not consent_ref:
        raise StyleHistoryError("account_id and activation consent_ref are required")
    _timestamp(captured_at, "captured_at")
    if not 1 <= max_pages <= 10 or not 1 <= max_notes <= 50:
        raise StyleHistoryError("style setup page or note limit is invalid")
    if not 1 <= max_comments_per_note <= 200:
        raise StyleHistoryError("style setup comment limit is invalid")
    if type(minimum_owned_replies) is not int or minimum_owned_replies < 1:
        raise StyleHistoryError("minimum_owned_replies must be a positive integer")
    if set(capture) != {"page_count", "has_more", "next_note_position", "notes"}:
        raise StyleHistoryError("style history capture fields are incomplete or unknown")
    page_count, has_more, raw_notes = capture["page_count"], capture["has_more"], capture["notes"]
    if type(page_count) is not int or not 0 <= page_count <= max_pages:
        raise StyleHistoryError("capture page_count exceeds setup bound")
    if type(has_more) is not bool or not isinstance(raw_notes, list) or len(raw_notes) > max_notes:
        raise StyleHistoryError("capture notes or continuation state is invalid")
    notes: list[HistoryNote] = []
    note_ids: set[str] = set()
    sample_ids: list[str] = []
    for raw_note in raw_notes:
        if not isinstance(raw_note, Mapping) or set(raw_note) != {
            "note_id", "note_ref", "title", "body", "published_at", "comments"
        }:
            raise StyleHistoryError("history note fields are incomplete or unknown")
        note_id, raw_comments = raw_note["note_id"], raw_note["comments"]
        if not isinstance(note_id, str) or not note_id or note_id in note_ids:
            raise StyleHistoryError("history note ids must be non-empty and unique")
        if not isinstance(raw_comments, list) or len(raw_comments) > max_comments_per_note:
            raise StyleHistoryError("history note comments exceed bound")
        if not isinstance(raw_note["title"], str) or not isinstance(raw_note["body"], str):
            raise StyleHistoryError("history note title and body must be strings")
        note_published = _timestamp(raw_note["published_at"], "note.published_at", optional=True)
        note_ids.add(note_id)
        comments: list[HistoryComment] = []
        comment_ids: set[str] = set()
        for raw in raw_comments:
            if not isinstance(raw, Mapping) or set(raw) != {
                "comment_id", "parent_comment_id", "commenter_role", "text",
                "published_at", "ownership_evidence",
            }:
                raise StyleHistoryError("history comment fields are incomplete or unknown")
            comment_id = raw["comment_id"]
            if not isinstance(comment_id, str) or not comment_id or comment_id in comment_ids:
                raise StyleHistoryError("history comment ids must be non-empty and unique per note")
            comment_ids.add(comment_id)
            text, flags = _redact(raw["text"])
            item = HistoryComment(
                comment_id=comment_id,
                parent_comment_id=raw["parent_comment_id"],
                commenter_role=raw["commenter_role"],
                text=text,
                published_at=_timestamp(raw["published_at"], "comment.published_at", optional=True),
                ownership_evidence=raw["ownership_evidence"],
                privacy_flags=flags,
            )
            comments.append(item)
            if item.commenter_role == "account_owner":
                sample_ids.append(f"{note_id}:{comment_id}")
        ids = {item.comment_id for item in comments}
        if any(item.commenter_role == "account_owner" and item.parent_comment_id not in ids for item in comments):
            raise StyleHistoryError("owner reply parent must be visible in the same note snapshot")
        title, _ = _redact(raw_note["title"])
        body, _ = _redact(raw_note["body"])
        notes.append(HistoryNote(note_id, _safe_ref(raw_note["note_ref"]), title, body, note_published, tuple(comments)))
    next_position = capture["next_note_position"]
    if next_position is not None and (type(next_position) is not int or next_position < len(raw_notes)):
        raise StyleHistoryError("next_note_position must continue after captured notes")
    if has_more != (next_position is not None):
        raise StyleHistoryError("has_more and next_note_position are inconsistent")
    payload = {"account_id": account_id, "captured_at": captured_at, "notes": [x.to_dict() for x in notes], "samples": sample_ids, "page_count": page_count, "has_more": has_more}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return StyleHistorySnapshot(
        snapshot_id="style_history_" + digest[:16], account_id=account_id,
        consent_ref=consent_ref, captured_at=captured_at, coverage="bounded_visible_history",
        setup_status="ready_for_style_profile" if len(sample_ids) >= minimum_owned_replies else "insufficient_owned_replies",
        notes=tuple(notes), owned_reply_sample_ids=tuple(sample_ids), page_count=page_count,
        max_pages=max_pages, max_notes=max_notes, max_comments_per_note=max_comments_per_note,
        has_more=has_more,
        next_note_position=next_position,
        read_only=True, platform_actions_executed=0, content_hash=digest,
    )
