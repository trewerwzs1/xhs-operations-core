"""Fail-closed selection and immutable capture of the latest non-pinned note."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


class LatestNoteContractError(ValueError):
    """Raised when latest-note evidence is ambiguous or inconsistent."""


class PinStatus(str, Enum):
    PINNED = "pinned"
    NOT_PINNED = "not_pinned"
    UNKNOWN = "unknown"


def _text(name: str, value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise LatestNoteContractError(f"{name} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise LatestNoteContractError(f"{name} cannot be empty")
    return result


def _timestamp(name: str, value: object) -> tuple[str, datetime]:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LatestNoteContractError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LatestNoteContractError(f"{name} must include a timezone")
    return text, parsed


def _strings(name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise LatestNoteContractError(f"{name} must be a list")
    result = tuple(_text(f"{name}[]", item) for item in value)
    if len(result) != len(set(result)):
        raise LatestNoteContractError(f"{name} cannot contain duplicates")
    return result


def _safe_note_url(name: str, value: object) -> tuple[str, str]:
    raw = _text(name, value)
    parsed = urlparse(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise LatestNoteContractError(f"{name} must be a Xiaohongshu HTTPS URL")
    match = re.fullmatch(r"/(?:explore|search_result)/([A-Za-z0-9_-]+)", parsed.path.rstrip("/"))
    if match is None:
        raise LatestNoteContractError(f"{name} must identify one note")
    note_id = match.group(1)
    return note_id, f"https://www.xiaohongshu.com/explore/{note_id}"


@dataclass(frozen=True)
class ProfileNoteCard:
    position: int
    note_id: str
    source_url: str
    title_hint: str
    pin_status: PinStatus

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProfileNoteCard":
        if not isinstance(value, Mapping):
            raise LatestNoteContractError("profile card must be an object")
        allowed = {"position", "note_id", "source_url", "title_hint", "pin_status"}
        unknown = set(value) - allowed
        if unknown:
            raise LatestNoteContractError(f"unknown profile card fields: {sorted(unknown)}")
        position = value.get("position")
        if type(position) is not int or position < 0:
            raise LatestNoteContractError("profile card position must be >= 0")
        note_id, safe_url = _safe_note_url("profile card source_url", value.get("source_url"))
        supplied_id = _text("profile card note_id", value.get("note_id"))
        if supplied_id != note_id:
            raise LatestNoteContractError("profile card note_id does not match source_url")
        try:
            pin_status = PinStatus(value.get("pin_status"))
        except (TypeError, ValueError) as exc:
            raise LatestNoteContractError("profile card pin_status is invalid") from exc
        return cls(
            position=position,
            note_id=note_id,
            source_url=safe_url,
            title_hint=_text("profile card title_hint", value.get("title_hint"), allow_empty=True),
            pin_status=pin_status,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "position": self.position,
            "note_id": self.note_id,
            "source_url": self.source_url,
            "title_hint": self.title_hint,
            "pin_status": self.pin_status.value,
        }


@dataclass(frozen=True)
class NoteDetailCapture:
    note_id: str
    source_url: str
    title: str
    body: str
    hashtags: tuple[str, ...]
    image_text: tuple[str, ...]
    published_at: str
    published_text: str
    published_at_precision: str
    captured_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "note_id": self.note_id,
            "source_url": self.source_url,
            "title": self.title,
            "body": self.body,
            "hashtags": list(self.hashtags),
            "image_text": list(self.image_text),
            "published_at": self.published_at,
            "published_text": self.published_text,
            "published_at_precision": self.published_at_precision,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NoteDetailCapture":
        if not isinstance(value, Mapping):
            raise LatestNoteContractError("note detail must be an object")
        allowed = {
            "note_id",
            "source_url",
            "title",
            "body",
            "hashtags",
            "image_text",
            "published_at",
            "published_text",
            "published_at_precision",
            "captured_at",
        }
        unknown = set(value) - allowed
        if unknown:
            raise LatestNoteContractError(f"unknown note detail fields: {sorted(unknown)}")
        note_id, safe_url = _safe_note_url("note detail source_url", value.get("source_url"))
        supplied_id = _text("note detail note_id", value.get("note_id"))
        if supplied_id != note_id:
            raise LatestNoteContractError("note detail note_id does not match source_url")
        published_at, published = _timestamp("note detail published_at", value.get("published_at"))
        captured_at, captured = _timestamp("note detail captured_at", value.get("captured_at"))
        if captured < published:
            raise LatestNoteContractError("note detail captured_at cannot predate published_at")
        published_precision = _text(
            "note detail published_at_precision", value.get("published_at_precision")
        )
        if published_precision not in {"minute", "day"}:
            raise LatestNoteContractError(
                "note detail published_at_precision must be minute or day"
            )
        title = _text("note detail title", value.get("title"), allow_empty=True)
        body = _text("note detail body", value.get("body"), allow_empty=True)
        image_text = _strings("note detail image_text", value.get("image_text", []))
        if not title and not body and not image_text:
            raise LatestNoteContractError("note detail requires visible content")
        return cls(
            note_id=note_id,
            source_url=safe_url,
            title=title,
            body=body,
            hashtags=_strings("note detail hashtags", value.get("hashtags", [])),
            image_text=image_text,
            published_at=published_at,
            published_text=_text("note detail published_text", value.get("published_text")),
            published_at_precision=published_precision,
            captured_at=captured_at,
        )


def select_latest_non_pinned(
    cards: Sequence[ProfileNoteCard], *, profile_order_verified: bool
) -> tuple[ProfileNoteCard, tuple[str, ...]]:
    if not profile_order_verified:
        raise LatestNoteContractError("profile note order is not verified")
    if not cards:
        raise LatestNoteContractError("profile has no visible note cards")
    ordered = sorted(cards, key=lambda item: item.position)
    if [item.position for item in ordered] != list(range(len(ordered))):
        raise LatestNoteContractError("profile card positions must be contiguous from zero")
    ids = [item.note_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise LatestNoteContractError("profile note cards cannot contain duplicate note ids")
    skipped: list[str] = []
    for card in ordered:
        if card.pin_status is PinStatus.UNKNOWN:
            raise LatestNoteContractError("pin status is unknown before latest note")
        if card.pin_status is PinStatus.PINNED:
            skipped.append(card.note_id)
            continue
        return card, tuple(skipped)
    raise LatestNoteContractError("no visible non-pinned note")


def select_latest_visible_profile_note(capture: Mapping[str, Any]) -> dict[str, str]:
    """Select the newest timestamped note from a bounded first profile batch.

    Run Agent captures the visible profile batch starting at position zero. Pin
    order is deliberately ignored; the newest published timestamp wins. Missing
    or tied timestamps fail closed instead of guessing.
    """

    if not isinstance(capture, Mapping):
        raise LatestNoteContractError("account profile capture must be an object")
    raw_capture = capture.get("capture")
    if not isinstance(raw_capture, Mapping) or not isinstance(raw_capture.get("notes"), list):
        raise LatestNoteContractError("account profile capture notes are missing")
    rows: list[tuple[datetime, dict[str, str]]] = []
    for index, raw in enumerate(raw_capture["notes"]):
        if not isinstance(raw, Mapping):
            raise LatestNoteContractError("account profile note must be an object")
        note_id = _text(f"account profile note[{index}].note_id", raw.get("note_id"))
        source_id, source_ref = _safe_note_url(
            f"account profile note[{index}].note_ref", raw.get("note_ref")
        )
        if source_id != note_id:
            raise LatestNoteContractError("account profile note id does not match note_ref")
        published_text, published = _timestamp(
            f"account profile note[{index}].published_at", raw.get("published_at")
        )
        title = _text(
            f"account profile note[{index}].title", raw.get("title", ""), allow_empty=True
        )
        body = _text(
            f"account profile note[{index}].body", raw.get("body", ""), allow_empty=True
        )
        if not title and not body:
            continue
        rows.append((published, {
            "note_id": note_id,
            "source_ref": source_ref,
            "title": title,
            "body": body,
            "published_at": published_text,
        }))
    if not rows:
        raise LatestNoteContractError("account profile batch has no valid timestamped note")
    rows.sort(key=lambda item: item[0], reverse=True)
    if len(rows) > 1 and rows[0][0] == rows[1][0]:
        raise LatestNoteContractError("latest account note timestamp is ambiguous")
    return rows[0][1]


@dataclass(frozen=True)
class NoteSnapshot:
    schema_version: int
    note_id: str
    source_url: str
    title: str
    body: str
    hashtags: tuple[str, ...]
    image_text: tuple[str, ...]
    published_at: str
    published_text: str
    published_at_precision: str
    captured_at: str
    profile_position: int
    skipped_pinned_note_ids: tuple[str, ...]
    selection_rule: str
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "note_id": self.note_id,
            "source_url": self.source_url,
            "title": self.title,
            "body": self.body,
            "hashtags": list(self.hashtags),
            "image_text": list(self.image_text),
            "published_at": self.published_at,
            "published_text": self.published_text,
            "published_at_precision": self.published_at_precision,
            "captured_at": self.captured_at,
            "profile_position": self.profile_position,
            "skipped_pinned_note_ids": list(self.skipped_pinned_note_ids),
            "selection_rule": self.selection_rule,
            "content_hash": self.content_hash,
        }


def build_latest_note_snapshot(
    *,
    cards: Sequence[ProfileNoteCard],
    detail: NoteDetailCapture,
    profile_order_verified: bool,
) -> NoteSnapshot:
    selected, skipped = select_latest_non_pinned(
        cards, profile_order_verified=profile_order_verified
    )
    if selected.note_id != detail.note_id:
        raise LatestNoteContractError("selected profile card does not match note detail")
    payload = {
        "note_id": detail.note_id,
        "source_url": detail.source_url,
        "title": detail.title,
        "body": detail.body,
        "hashtags": detail.hashtags,
        "image_text": detail.image_text,
        "published_at": detail.published_at,
        "published_text": detail.published_text,
        "published_at_precision": detail.published_at_precision,
        "captured_at": detail.captured_at,
        "profile_position": selected.position,
        "skipped_pinned_note_ids": skipped,
        "selection_rule": "first_visible_non_pinned_in_verified_reverse_chronological_profile_order",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return NoteSnapshot(
        schema_version=1,
        content_hash=hashlib.sha256(encoded).hexdigest(),
        **payload,
    )
