"""AccountVoice status derived from the existing consent-bound style artifacts."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .storage import StorageError, read_jsonl
from .style import PostVoiceStore, ReplyCorpusStore, StyleProfileError, StyleProfileStore


def _safe_account_id(value: str) -> str:
    account_id = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", account_id) is None:
        raise StyleProfileError("invalid AccountVoice account_id")
    return account_id


def _latest_learning_progress(runtime: Path, account_id: str) -> tuple[dict[str, Any], bool]:
    latest: dict[str, Any] | None = None
    for row in read_jsonl(runtime / "style" / "history_runs.jsonl"):
        if row.get("account_id") == account_id:
            latest = row
    if latest is None:
        return {
            "known": False,
            "has_more": None,
            "next_note_position": None,
            "capture_exhausted": False,
            "should_repeat_capture": False,
        }, False
    has_more = latest.get("has_more")
    next_position = latest.get("next_note_position")
    learning_status = str(latest.get("learning_status") or "")
    exhausted = has_more is False and learning_status == "insufficient_owned_replies"
    invalid = (
        type(has_more) is not bool
        or (
            (type(next_position) is not int or next_position < 0)
            and not (exhausted and next_position is None)
        )
        or learning_status not in {"ready", "continue_required", "insufficient_owned_replies"}
    )
    if invalid:
        return {
            "known": True,
            "has_more": None,
            "next_note_position": None,
            "capture_exhausted": False,
            "should_repeat_capture": False,
        }, True
    return {
        "known": True,
        "has_more": has_more,
        "next_note_position": next_position,
        "capture_exhausted": exhausted,
        "should_repeat_capture": has_more is True,
    }, False


def build_account_voice_status(runtime_dir: Path, *, account_id: str) -> dict[str, Any]:
    """Validate corpus/profile integrity without returning historical reply text."""

    account_id = _safe_account_id(account_id)
    runtime = Path(runtime_dir)
    corpus_store = ReplyCorpusStore(runtime)
    profile_store = StyleProfileStore(runtime)
    post_store = PostVoiceStore(runtime)
    corpus_path = corpus_store.path_for(account_id)
    profile_path = profile_store.path_for(account_id)
    post_path = post_store.path_for(account_id)
    blockers: list[str] = []
    learning_progress, learning_progress_invalid = _latest_learning_progress(runtime, account_id)
    if learning_progress_invalid:
        blockers.append("voice_learning_progress_invalid")

    corpus = None
    if corpus_path.is_file():
        try:
            corpus = corpus_store.load(account_id)
        except (StyleProfileError, StorageError):
            blockers.append("reply_corpus_integrity_invalid")

    profile = None
    if profile_path.is_file():
        try:
            profile = profile_store.load(account_id)
        except (StyleProfileError, StorageError):
            blockers.append("style_profile_integrity_invalid")

    if profile is not None and corpus is None:
        blockers.append("style_profile_without_valid_corpus")
    if corpus is not None and profile is not None:
        expected_ids = tuple(entry.entry_id for entry in corpus.entries)
        expected_hashes = tuple(entry.reply_hash for entry in corpus.entries)
        if (
            profile.source_sample_ids != expected_ids
            or profile.source_reply_hashes != expected_hashes
            or profile.source_snapshot_hash != corpus.source_snapshot_hash
        ):
            blockers.append("style_profile_corpus_binding_invalid")

    post_profile = None
    if post_path.is_file():
        try:
            post_profile = post_store.load(account_id)
        except (StyleProfileError, StorageError):
            blockers.append("post_voice_integrity_invalid")

    if blockers:
        status = "invalid"
        next_step = "repair_or_relearn"
    elif corpus is None and post_profile is None:
        status = "not_started"
        next_step = "learn_from_account"
    elif post_profile is None:
        status = "post_voice_required"
        next_step = "learn_from_account"
    elif corpus is None:
        status = "reply_voice_required"
        next_step = (
            "continue_history_capture"
            if learning_progress["should_repeat_capture"]
            else "use_neutral_review_each"
            if learning_progress["capture_exhausted"]
            else "collect_reply_samples"
        )
    elif profile is None and corpus.entry_count < 2:
        status = "continue_required"
        next_step = (
            "continue_history_capture"
            if learning_progress["should_repeat_capture"]
            else "use_neutral_review_each"
            if learning_progress["capture_exhausted"]
            else "collect_reply_samples"
        )
    elif profile is None:
        status = "profile_build_required"
        next_step = "rebuild_profile"
    else:
        status = "ready"
        next_step = "ready"

    corpus_summary = {
        "exists": corpus_path.is_file(),
        "valid": corpus is not None,
        "entry_count": corpus.entry_count if corpus is not None else 0,
        "excluded_private_count": corpus.excluded_private_count if corpus is not None else 0,
        "content_hash": corpus.content_hash if corpus is not None else None,
    }
    profile_summary = {
        "exists": profile_path.is_file(),
        "valid": profile is not None,
        "profile_id": profile.profile_id if profile is not None else None,
        "sample_count": profile.sample_count if profile is not None else 0,
        "confidence": profile.confidence if profile is not None else None,
        "content_hash": profile.content_hash if profile is not None else None,
        "stores_raw_reply_text": profile.stores_raw_reply_text if profile is not None else False,
        "style_directives": list(profile.style_directives) if profile is not None else [],
        "preferred_markers": list(profile.preferred_markers) if profile is not None else [],
    }
    post_summary = {
        "exists": post_path.is_file(),
        "valid": post_profile is not None,
        "profile_id": post_profile.profile_id if post_profile is not None else None,
        "sample_count": post_profile.sample_count if post_profile is not None else 0,
        "confidence": post_profile.confidence if post_profile is not None else None,
        "content_hash": post_profile.content_hash if post_profile is not None else None,
        "stores_raw_post_text": post_profile.stores_raw_post_text if post_profile is not None else False,
        "style_directives": list(post_profile.style_directives) if post_profile is not None else [],
        "aggregate_features": {
            "average_title_char_count": post_profile.average_title_char_count,
            "average_body_char_count": post_profile.average_body_char_count,
            "average_paragraph_count": post_profile.average_paragraph_count,
            "hashtag_per_post": post_profile.hashtag_per_post,
            "emoji_per_post": post_profile.emoji_per_post,
            "bullet_line_per_post": post_profile.bullet_line_per_post,
        } if post_profile is not None else {},
    }
    if status == "invalid":
        drafting_policy = {
            "mode": "blocked",
            "confidence": "none",
            "reason": "account_voice_integrity_invalid",
            "can_generate_draft": False,
            "text_write_approval_mode": "blocked",
            "exact_human_review_required": True,
            "review_each_required": True,
        }
    elif status == "ready":
        drafting_policy = {
            "mode": "account_voice",
            "confidence": profile.confidence if profile is not None else "high",
            "reason": "account_voice_valid",
            "can_generate_draft": True,
            "text_write_approval_mode": "account_setup_policy",
            "exact_human_review_required": False,
            "review_each_required": False,
        }
    else:
        drafting_policy = {
            "mode": "neutral_review_each",
            "confidence": "low",
            "reason": (
                "insufficient_owned_reply_samples"
                if status in {"reply_voice_required", "continue_required"}
                else "account_voice_not_available"
            ),
            "can_generate_draft": True,
            "text_write_approval_mode": "review_each",
            "exact_human_review_required": True,
            "review_each_required": True,
        }
    return {
        "schema_version": 3,
        "account_id": account_id,
        "status": status,
        "ready": status == "ready",
        "voice_ready": status == "ready",
        "post_voice": post_summary,
        "reply_voice": {
            "corpus": corpus_summary,
            "profile": profile_summary,
        },
        # Compatibility aliases for V1 callers. V2 callers should use reply_voice.
        "corpus": corpus_summary,
        "profile": profile_summary,
        "blockers": blockers,
        "next_step": next_step,
        "learning_progress": learning_progress,
        "drafting_policy": drafting_policy,
        "raw_history_returned": False,
        "raw_post_history_returned": False,
        "platform_actions_executed": 0,
    }


def build_account_voice_constraint(runtime_dir: Path, *, account_id: str) -> dict[str, Any]:
    """Return the only style constraint safe for a new text draft."""

    status = build_account_voice_status(runtime_dir, account_id=account_id)
    return {
        "schema_version": 1,
        "account_id": status["account_id"],
        **status["drafting_policy"],
        "post_voice_profile_id": status["post_voice"]["profile_id"],
        "post_voice_profile_hash": status["post_voice"]["content_hash"],
        "reply_voice_profile_id": status["profile"]["profile_id"],
        "reply_voice_profile_hash": status["profile"]["content_hash"],
        "platform_actions_executed": 0,
    }
