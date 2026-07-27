"""Aggregate owned replies into explainable style features without retaining prose."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from statistics import median
from typing import TYPE_CHECKING, Any, Mapping

from xhs_operations_core.source_notes import StyleHistorySnapshot

if TYPE_CHECKING:
    from .corpus import ReplyCorpus


class StyleProfileError(ValueError):
    pass


MARKERS = ("呀", "呢", "吧", "啦", "～", "哈哈", "其实", "一般", "可能", "更", "比较", "不用")


@dataclass(frozen=True)
class ReplyStyleProfile:
    profile_id: str
    profile_version: int
    account_id: str
    created_at: str
    source_snapshot_id: str
    source_snapshot_hash: str
    source_sample_ids: tuple[str, ...]
    source_reply_hashes: tuple[str, ...]
    excluded_private_sample_count: int
    sample_count: int
    confidence: str
    average_char_count: float
    median_char_count: float
    average_sentence_count: float
    question_ending_ratio: float
    soft_particle_ending_ratio: float
    exclamation_ratio: float
    first_person_ratio: float
    second_person_ratio: float
    opening_move_distribution: dict[str, int]
    preferred_markers: tuple[str, ...]
    style_directives: tuple[str, ...]
    forbidden_uses: tuple[str, ...]
    stores_raw_reply_text: bool
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "account_id": self.account_id,
            "created_at": self.created_at,
            "source_snapshot_id": self.source_snapshot_id,
            "source_snapshot_hash": self.source_snapshot_hash,
            "source_sample_ids": list(self.source_sample_ids),
            "source_reply_hashes": list(self.source_reply_hashes),
            "excluded_private_sample_count": self.excluded_private_sample_count,
            "sample_count": self.sample_count,
            "confidence": self.confidence,
            "average_char_count": self.average_char_count,
            "median_char_count": self.median_char_count,
            "average_sentence_count": self.average_sentence_count,
            "question_ending_ratio": self.question_ending_ratio,
            "soft_particle_ending_ratio": self.soft_particle_ending_ratio,
            "exclamation_ratio": self.exclamation_ratio,
            "first_person_ratio": self.first_person_ratio,
            "second_person_ratio": self.second_person_ratio,
            "opening_move_distribution": dict(self.opening_move_distribution),
            "preferred_markers": list(self.preferred_markers),
            "style_directives": list(self.style_directives),
            "forbidden_uses": list(self.forbidden_uses),
            "stores_raw_reply_text": self.stores_raw_reply_text,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReplyStyleProfile":
        required = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != required:
            raise StyleProfileError("stored style profile fields are incomplete or unknown")
        tuple_fields = {
            "source_sample_ids", "source_reply_hashes", "preferred_markers",
            "style_directives", "forbidden_uses",
        }
        converted = dict(value)
        for name in tuple_fields:
            raw = converted[name]
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise StyleProfileError(f"stored style profile {name} is invalid")
            converted[name] = tuple(raw)
        if not isinstance(converted["opening_move_distribution"], dict):
            raise StyleProfileError("stored style profile opening distribution is invalid")
        if converted["profile_version"] != 1 or converted["stores_raw_reply_text"] is not False:
            raise StyleProfileError("stored style profile version or privacy state is invalid")
        if converted["confidence"] not in {"low", "medium", "high"}:
            raise StyleProfileError("stored style profile confidence is invalid")
        for name in ("content_hash", "source_snapshot_hash"):
            if re.fullmatch(r"[0-9a-f]{64}", str(converted[name])) is None:
                raise StyleProfileError(f"stored style profile {name} is invalid")
        if converted["profile_id"] != "reply_style_" + converted["content_hash"][:16]:
            raise StyleProfileError("stored style profile ID does not match content hash")
        if converted["sample_count"] != len(converted["source_sample_ids"]):
            raise StyleProfileError("stored style profile sample count is inconsistent")
        return cls(**converted)


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _opening(text: str) -> str:
    if re.match(r"^(不会|不用|别担心|可以)", text):
        return "direct_reassurance"
    if re.match(r"^(确实|是的|对|嗯|其实)", text):
        return "acknowledge_or_context"
    if re.match(r"^(你|想问|更喜欢|平时)", text) or text.startswith(("什么", "哪")):
        return "question_first"
    return "other"


def _build_profile(
    *,
    account_id: str,
    created_at: str,
    source_created_at: str,
    source_snapshot_id: str,
    source_snapshot_hash: str,
    safe: list[tuple[str, str, str]],
    excluded: int,
    minimum_samples: int,
) -> ReplyStyleProfile:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        source_created = datetime.fromisoformat(source_created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StyleProfileError("profile timestamps must be ISO-8601") from exc
    if (
        created.tzinfo is None
        or created.utcoffset() is None
        or source_created.tzinfo is None
        or source_created.utcoffset() is None
    ):
        raise StyleProfileError("profile timestamps must include a timezone")
    if created < source_created:
        raise StyleProfileError("style profile cannot predate its history source")
    if type(minimum_samples) is not int or minimum_samples < 1:
        raise StyleProfileError("minimum_samples must be a positive integer")
    if len(safe) < minimum_samples:
        raise StyleProfileError("insufficient safe owned replies for style profile")

    texts = [text for _, text, _ in safe]
    lengths = [len(text) for text in texts]
    sentence_counts = [max(1, len(re.findall(r"[。！？!?]", text))) for text in texts]
    opening = Counter(_opening(text) for text in texts)
    marker_counts = Counter(marker for text in texts for marker in MARKERS if marker in text)
    preferred = tuple(
        marker
        for marker, count in sorted(
            marker_counts.items(), key=lambda item: (-item[1], MARKERS.index(item[0]))
        )
        if count >= max(1, len(texts) // 3)
    )
    question_ratio = _ratio(
        sum(text.rstrip().endswith(("?", "？")) for text in texts), len(texts)
    )
    soft_ratio = _ratio(
        sum(text.rstrip().endswith(("呀", "呢", "吧", "啦", "～", "~")) for text in texts),
        len(texts),
    )
    directives = (
        f"回复长度优先控制在约 {round(sum(lengths) / len(lengths))} 个字符",
        "优先使用短句和自然口语，不写客服式总结",
        "保持用户原有的提问收尾倾向" if question_ratio >= 0.4 else "不强制使用提问收尾",
        "只参考聚合风格，不复用历史回复原句或其中事实",
    )
    feature_payload = {
        "account_id": account_id,
        "snapshot_hash": source_snapshot_hash,
        "sample_ids": [sample_id for sample_id, _, _ in safe],
        "reply_hashes": [reply_hash for _, _, reply_hash in safe],
        "lengths": lengths,
        "sentence_counts": sentence_counts,
        "opening": dict(opening),
        "preferred": preferred,
        "question_ratio": question_ratio,
        "soft_ratio": soft_ratio,
    }
    content_hash = hashlib.sha256(
        json.dumps(feature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ReplyStyleProfile(
        profile_id="reply_style_" + content_hash[:16],
        profile_version=1,
        account_id=account_id,
        created_at=created_at,
        source_snapshot_id=source_snapshot_id,
        source_snapshot_hash=source_snapshot_hash,
        source_sample_ids=tuple(sample_id for sample_id, _, _ in safe),
        source_reply_hashes=tuple(reply_hash for _, _, reply_hash in safe),
        excluded_private_sample_count=excluded,
        sample_count=len(safe),
        confidence="high" if len(safe) >= 20 else "medium" if len(safe) >= 5 else "low",
        average_char_count=round(sum(lengths) / len(lengths), 2),
        median_char_count=float(median(lengths)),
        average_sentence_count=round(sum(sentence_counts) / len(sentence_counts), 2),
        question_ending_ratio=question_ratio,
        soft_particle_ending_ratio=soft_ratio,
        exclamation_ratio=_ratio(sum("!" in text or "！" in text for text in texts), len(texts)),
        first_person_ratio=_ratio(sum(bool(re.search(r"我|我们", text)) for text in texts), len(texts)),
        second_person_ratio=_ratio(sum("你" in text for text in texts), len(texts)),
        opening_move_distribution=dict(sorted(opening.items())),
        preferred_markers=preferred,
        style_directives=directives,
        forbidden_uses=(
            "不得把历史回复中的活动事实、价格、时间、地点或承诺用于新回复",
            "不得保留或复用联系方式、链接和身份信息",
            "不得复制完整历史回复原句",
            "不得让风格画像覆盖 MessagePlan 事实与风控校验",
        ),
        stores_raw_reply_text=False,
        content_hash=content_hash,
    )


def build_reply_style_profile(
    snapshot: StyleHistorySnapshot,
    *,
    created_at: str,
    minimum_samples: int = 2,
) -> ReplyStyleProfile:
    if snapshot.coverage != "bounded_visible_history" or not snapshot.read_only:
        raise StyleProfileError("style profile requires a bounded read-only history snapshot")
    safe: list[tuple[str, str, str]] = []
    excluded = 0
    for note in snapshot.notes:
        for comment in note.comments:
            if comment.commenter_role != "account_owner":
                continue
            sample_id = f"{note.note_id}:{comment.comment_id}"
            if comment.privacy_flags or "[已脱敏]" in comment.text:
                excluded += 1
                continue
            safe.append(
                (
                    sample_id,
                    comment.text,
                    hashlib.sha256(comment.text.encode("utf-8")).hexdigest(),
                )
            )
    return _build_profile(
        account_id=snapshot.account_id,
        created_at=created_at,
        source_created_at=snapshot.captured_at,
        source_snapshot_id=snapshot.snapshot_id,
        source_snapshot_hash=snapshot.content_hash,
        safe=safe,
        excluded=excluded,
        minimum_samples=minimum_samples,
    )


def build_reply_style_profile_from_corpus(
    corpus: "ReplyCorpus",
    *,
    created_at: str,
    minimum_samples: int = 2,
) -> ReplyStyleProfile:
    """Build one aggregate profile from every consent-bound reply captured so far."""

    if not corpus.local_only or not corpus.stores_raw_reply_text:
        raise StyleProfileError("style profile requires a valid local reply corpus")
    safe = [(entry.entry_id, entry.reply_text, entry.reply_hash) for entry in corpus.entries]
    return _build_profile(
        account_id=corpus.account_id,
        created_at=created_at,
        source_created_at=corpus.created_at,
        source_snapshot_id=corpus.source_snapshot_id,
        source_snapshot_hash=corpus.source_snapshot_hash,
        safe=safe,
        excluded=corpus.excluded_private_count,
        minimum_samples=minimum_samples,
    )
