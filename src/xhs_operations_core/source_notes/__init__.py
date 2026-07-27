"""Immutable source-note capture contracts."""

from .latest import (
    LatestNoteContractError,
    NoteDetailCapture,
    NoteSnapshot,
    PinStatus,
    ProfileNoteCard,
    build_latest_note_snapshot,
    select_latest_non_pinned,
    select_latest_visible_profile_note,
)
from .xhs_dom import ParsedPublishedAt, parse_visible_published_at
from .thread import (
    VisibleComment,
    VisibleThreadSnapshot,
    build_visible_thread_snapshot_from_dict,
    redact_visible_contact_text,
)
from .style_history import (
    HistoryComment,
    HistoryNote,
    StyleHistoryError,
    StyleHistorySnapshot,
    build_style_history_snapshot,
)

__all__ = [
    "LatestNoteContractError",
    "NoteDetailCapture",
    "NoteSnapshot",
    "PinStatus",
    "ProfileNoteCard",
    "build_latest_note_snapshot",
    "select_latest_non_pinned",
    "select_latest_visible_profile_note",
    "ParsedPublishedAt",
    "parse_visible_published_at",
    "VisibleComment",
    "VisibleThreadSnapshot",
    "build_visible_thread_snapshot_from_dict",
    "redact_visible_contact_text",
    "HistoryComment",
    "HistoryNote",
    "StyleHistoryError",
    "StyleHistorySnapshot",
    "build_style_history_snapshot",
]
