"""One-target, visible-only Xiaohongshu candidate capture contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from xhs_operations_core.source_notes import NoteDetailCapture, VisibleThreadSnapshot

from .errors import LiveInteractionError


CANDIDATE_READONLY_CONFIRMATION = "I_CONFIRM_SINGLE_CANDIDATE_READONLY"


class CandidateReadOnlyPort(Protocol):
    def open_home_and_search(self, keyword: str) -> dict[str, object]: ...

    def open_one_result(self, *, index: int) -> dict[str, object]: ...

    def return_to_search_results(self) -> dict[str, object]: ...

    def read_current_note_detail(
        self, *, expected_note_id: str, captured_at: str
    ) -> NoteDetailCapture: ...

    def read_visible_thread_snapshot(
        self, *, expected_note_id: str, captured_at: str, max_comments: int = 20
    ) -> VisibleThreadSnapshot: ...


@dataclass(frozen=True)
class CandidateReadOnlyResult:
    query: str
    result_index: int
    navigation: dict[str, object]
    opened: dict[str, object]
    note: NoteDetailCapture
    thread: VisibleThreadSnapshot

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "result_index": self.result_index,
            "navigation": dict(self.navigation),
            "opened": dict(self.opened),
            "note": self.note.to_dict(),
            "thread": self.thread.to_dict(),
            "processing_mode": "one_candidate_at_a_time",
            "read_only": True,
            "platform_actions_executed": 0,
        }


@dataclass(frozen=True)
class CandidateSequenceResult:
    query: str
    selection_mode: str
    start_index: int
    inspected: tuple[dict[str, object], ...]
    selected_result_index: int | None
    selected_note_id: str | None
    selected_comment_id: str | None
    selected_target_type: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "selection_mode": self.selection_mode,
            "start_index": self.start_index,
            "inspected": [dict(item) for item in self.inspected],
            "selected_result_index": self.selected_result_index,
            "selected_note_id": self.selected_note_id,
            "selected_comment_id": self.selected_comment_id,
            "selected_target_type": self.selected_target_type,
            "processing_mode": "one_candidate_at_a_time_same_search_session",
            "search_count": 1,
            "read_only": True,
            "platform_actions_executed": 0,
        }


_REQUEST_MARKERS = (
    "想了解", "想参加", "想试", "想学", "准备", "计划", "下次", "报名", "预约",
    "怎么", "如何", "哪里", "有没有", "可以吗", "求推荐", "多少", "多少钱",
)
_EXCLUSION_TERMS = ("邀请码", "加入群聊", "加微信", "代购", "广告")
_GENERIC_QUERY_TERMS = {"相关", "内容", "分享", "推荐", "活动", "体验"}


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(
        term for term in re.split(r"[\s、，,]+", query)
        if len(term) >= 2 and term not in _GENERIC_QUERY_TERMS
    )


def _note_matches_adjacent_interest(note: NoteDetailCapture, query: str) -> bool:
    context = " ".join((note.title, note.body, *note.hashtags, *note.image_text))
    for term in _query_terms(query):
        if term in context:
            return True
        if len(term) >= 4 and any(
            term[index:index + 2] in context for index in range(len(term) - 1)
        ):
            return True
    return False


def _select_intent_comment(thread: VisibleThreadSnapshot, query: str):
    note_context = f"{thread.title} {thread.body}"
    query_terms = _query_terms(query)
    note_matches_query = any(term in note_context for term in query_terms)
    for comment in thread.comments:
        if any(term in comment.text for term in _EXCLUSION_TERMS):
            continue
        comment_matches_query = any(term in comment.text for term in query_terms)
        has_request_marker = any(term in comment.text for term in _REQUEST_MARKERS)
        if has_request_marker and (comment_matches_query or note_matches_query):
            return comment
    return None


def capture_candidate_sequence_readonly(
    *,
    port: CandidateReadOnlyPort,
    query: str,
    start_index: int,
    max_candidates: int,
    captured_at_factory,
    max_comments: int = 20,
    selection_mode: str = "comment_intent",
) -> CandidateSequenceResult:
    normalized_query = " ".join(str(query).split()).strip()
    if not 2 <= len(normalized_query) <= 100:
        raise LiveInteractionError("query must contain 2-100 characters", code="invalid_query")
    if type(start_index) is not int or not 0 <= start_index <= 20:
        raise LiveInteractionError("start_index must be 0-20", code="invalid_result_index")
    if type(max_candidates) is not int or not 1 <= max_candidates <= 10:
        raise LiveInteractionError("max_candidates must be 1-10", code="invalid_candidate_limit")
    if type(max_comments) is not int or not 0 <= max_comments <= 50:
        raise LiveInteractionError("max_comments must be 0-50", code="invalid_comment_limit")
    if selection_mode not in {"comment_intent", "adjacent_interest"}:
        raise LiveInteractionError("invalid candidate selection mode", code="invalid_selection_mode")

    navigation = port.open_home_and_search(normalized_query)
    inspected: list[dict[str, object]] = []
    for offset in range(max_candidates):
        result_index = start_index + offset
        opened = port.open_one_result(index=result_index)
        note_id = opened.get("note_id")
        if not isinstance(note_id, str) or not note_id:
            raise LiveInteractionError("opened result has no note id", code="note_id_missing")
        captured_at = captured_at_factory()
        note = port.read_current_note_detail(
            expected_note_id=note_id,
            captured_at=captured_at,
        )
        thread = port.read_visible_thread_snapshot(
            expected_note_id=note_id,
            captured_at=captured_at,
            max_comments=max_comments,
        )
        selected = (
            _select_intent_comment(thread, normalized_query)
            if selection_mode == "comment_intent"
            else None
        )
        adjacent_match = (
            selection_mode == "adjacent_interest"
            and _note_matches_adjacent_interest(note, normalized_query)
        )
        is_selected = selected is not None or adjacent_match
        decision = (
            "selected_comment_intent" if selected is not None
            else "selected_adjacent_interest_note" if adjacent_match
            else "skip_no_intent_comment" if selection_mode == "comment_intent"
            else "skip_no_adjacent_interest_evidence"
        )
        row: dict[str, object] = {
            "result_index": result_index,
            "navigation": navigation if offset == 0 else None,
            "opened": opened,
            "note": note.to_dict(),
            "thread": thread.to_dict(),
            "decision": decision,
            "selected_target_type": (
                "commenter" if selected is not None else "note_author" if adjacent_match else None
            ),
            "selected_comment_id": None if selected is None else selected.comment_id,
        }
        inspected.append(row)
        if is_selected:
            return CandidateSequenceResult(
                query=normalized_query,
                selection_mode=selection_mode,
                start_index=start_index,
                inspected=tuple(inspected),
                selected_result_index=result_index,
                selected_note_id=note.note_id,
                selected_comment_id=None if selected is None else selected.comment_id,
                selected_target_type="commenter" if selected is not None else "note_author",
            )
        if offset + 1 < max_candidates:
            row["returned_to_search"] = port.return_to_search_results()
    return CandidateSequenceResult(
        query=normalized_query,
        selection_mode=selection_mode,
        start_index=start_index,
        inspected=tuple(inspected),
        selected_result_index=None,
        selected_note_id=None,
        selected_comment_id=None,
        selected_target_type=None,
    )


def capture_single_candidate_readonly(
    *,
    port: CandidateReadOnlyPort,
    query: str,
    result_index: int,
    captured_at: str,
    max_comments: int = 20,
) -> CandidateReadOnlyResult:
    normalized_query = " ".join(str(query).split()).strip()
    if not 2 <= len(normalized_query) <= 100:
        raise LiveInteractionError("query must contain 2-100 characters", code="invalid_query")
    if type(result_index) is not int or not 0 <= result_index <= 20:
        raise LiveInteractionError("result_index must be 0-20", code="invalid_result_index")
    if type(max_comments) is not int or not 0 <= max_comments <= 50:
        raise LiveInteractionError("max_comments must be 0-50", code="invalid_comment_limit")

    navigation = port.open_home_and_search(normalized_query)
    opened = port.open_one_result(index=result_index)
    note_id = opened.get("note_id")
    if not isinstance(note_id, str) or not note_id:
        raise LiveInteractionError("opened result has no note id", code="note_id_missing")
    note = port.read_current_note_detail(
        expected_note_id=note_id,
        captured_at=captured_at,
    )
    thread = port.read_visible_thread_snapshot(
        expected_note_id=note_id,
        captured_at=captured_at,
        max_comments=max_comments,
    )
    if note.note_id != thread.note_id or note.note_id != note_id:
        raise LiveInteractionError("candidate evidence note ids differ", code="candidate_evidence_mismatch")
    return CandidateReadOnlyResult(
        query=normalized_query,
        result_index=result_index,
        navigation=navigation,
        opened=opened,
        note=note,
        thread=thread,
    )
