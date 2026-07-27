"""Sanitized, immutable snapshots of one visible note and its loaded comments."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .latest import LatestNoteContractError, _text, _timestamp


_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_WECHAT = re.compile(
    r"(?i)(微信(?:号)?|vx|v信|wechat)\s*[:：]?\s*([a-z][-_a-z0-9]{5,19})"
)


def redact_visible_contact_text(value: str) -> tuple[str, tuple[str, ...]]:
    text = " ".join(str(value).split()).strip()
    flags: list[str] = []
    if _MOBILE.search(text):
        flags.append("mobile_redacted")
        text = _MOBILE.sub("[REDACTED_MOBILE]", text)
    if _WECHAT.search(text):
        flags.append("wechat_redacted")
        text = _WECHAT.sub(lambda match: f"{match.group(1)}: [REDACTED_WECHAT]", text)
    return text, tuple(flags)


@dataclass(frozen=True)
class VisibleComment:
    comment_id: str
    visible_order: int
    commenter: str
    text: str
    kind: str
    parent_comment_id: str | None
    privacy_flags: tuple[str, ...]
    content_hash: str

    @classmethod
    def create(
        cls,
        *,
        note_id: str,
        visible_order: int,
        commenter: object,
        text: object,
        raw_comment_id: object = "",
        kind: object = "main",
        parent_comment_id: object = None,
    ) -> "VisibleComment":
        if type(visible_order) is not int or visible_order < 0:
            raise LatestNoteContractError("visible_order must be >= 0")
        commenter_text = _text("commenter", commenter)
        clean_text, flags = redact_visible_contact_text(_text("comment text", text))
        kind_text = _text("comment kind", kind)
        if kind_text not in {"main", "reply"}:
            raise LatestNoteContractError("comment kind must be main or reply")
        parent = None if parent_comment_id in {None, ""} else _text("parent_comment_id", parent_comment_id)
        if kind_text == "reply" and parent is None:
            raise LatestNoteContractError("reply comment requires parent_comment_id")
        supplied_id = str(raw_comment_id or "").strip()
        digest_input = f"{note_id}|{visible_order}|{commenter_text}|{clean_text}"
        comment_id = supplied_id or hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:24]
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", comment_id) is None:
            raise LatestNoteContractError("comment_id must be a safe id")
        content_hash = hashlib.sha256(
            f"{note_id}|{commenter_text}|{clean_text}|{kind_text}|{parent or ''}".encode("utf-8")
        ).hexdigest()
        return cls(
            comment_id=comment_id,
            visible_order=visible_order,
            commenter=commenter_text,
            text=clean_text,
            kind=kind_text,
            parent_comment_id=parent,
            privacy_flags=flags,
            content_hash=content_hash,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "comment_id": self.comment_id,
            "visible_order": self.visible_order,
            "commenter": self.commenter,
            "text": self.text,
            "kind": self.kind,
            "parent_comment_id": self.parent_comment_id,
            "privacy_flags": list(self.privacy_flags),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class VisibleThreadSnapshot:
    schema_version: int
    note_id: str
    source_url: str
    title: str
    body: str
    comments: tuple[VisibleComment, ...]
    captured_at: str
    coverage: str
    content_hash: str

    @classmethod
    def create(
        cls,
        *,
        note_id: object,
        source_url: object,
        title: object,
        body: object,
        comments: Sequence[VisibleComment],
        captured_at: object,
    ) -> "VisibleThreadSnapshot":
        note = _text("note_id", note_id)
        url = _text("source_url", source_url)
        if url != f"https://www.xiaohongshu.com/explore/{note}":
            raise LatestNoteContractError("thread source_url must be a sanitized note URL")
        captured, _ = _timestamp("captured_at", captured_at)
        rows = tuple(comments)
        if [item.visible_order for item in rows] != list(range(len(rows))):
            raise LatestNoteContractError("comment visible_order must be contiguous")
        ids = [item.comment_id for item in rows]
        if len(ids) != len(set(ids)):
            raise LatestNoteContractError("comment ids cannot repeat")
        payload = {
            "note_id": note,
            "source_url": url,
            "title": _text("title", title, allow_empty=True),
            "body": _text("body", body, allow_empty=True),
            "comments": [item.to_dict() for item in rows],
            "captured_at": captured,
            "coverage": "visible_only",
        }
        if not payload["title"] and not payload["body"]:
            raise LatestNoteContractError("thread snapshot requires visible note content")
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return cls(schema_version=1, content_hash=digest, comments=rows, **{k: v for k, v in payload.items() if k != "comments"})

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "note_id": self.note_id,
            "source_url": self.source_url,
            "title": self.title,
            "body": self.body,
            "comments": [item.to_dict() for item in self.comments],
            "captured_at": self.captured_at,
            "coverage": self.coverage,
            "content_hash": self.content_hash,
        }


def build_visible_thread_snapshot_from_dict(
    value: Mapping[str, Any],
) -> VisibleThreadSnapshot:
    if not isinstance(value, Mapping):
        raise LatestNoteContractError("visible thread input must be an object")
    allowed = {"note_id", "source_url", "title", "body", "captured_at", "comments"}
    unknown = set(value) - allowed
    if unknown:
        raise LatestNoteContractError(f"unknown visible thread fields: {sorted(unknown)}")
    raw_comments = value.get("comments")
    if not isinstance(raw_comments, list):
        raise LatestNoteContractError("comments must be a list")
    comments: list[VisibleComment] = []
    note_id = _text("note_id", value.get("note_id"))
    for index, item in enumerate(raw_comments):
        if not isinstance(item, Mapping):
            raise LatestNoteContractError("comment input must be an object")
        comment_allowed = {
            "raw_comment_id",
            "commenter",
            "text",
            "kind",
            "parent_visible_order",
        }
        comment_unknown = set(item) - comment_allowed
        if comment_unknown:
            raise LatestNoteContractError(
                f"unknown comment input fields: {sorted(comment_unknown)}"
            )
        parent_order = item.get("parent_visible_order")
        if parent_order is None:
            parent_id = None
        elif type(parent_order) is int and 0 <= parent_order < index:
            parent_id = comments[parent_order].comment_id
        else:
            raise LatestNoteContractError("parent_visible_order must reference an earlier comment")
        comments.append(
            VisibleComment.create(
                note_id=note_id,
                visible_order=index,
                commenter=item.get("commenter"),
                text=item.get("text"),
                raw_comment_id=item.get("raw_comment_id", ""),
                kind=item.get("kind", "main"),
                parent_comment_id=parent_id,
            )
        )
    return VisibleThreadSnapshot.create(
        note_id=note_id,
        source_url=value.get("source_url"),
        title=value.get("title", ""),
        body=value.get("body", ""),
        comments=comments,
        captured_at=value.get("captured_at"),
    )
