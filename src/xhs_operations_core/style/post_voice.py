"""De-factualized post-writing voice learned from bounded own-profile history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import median
from typing import Any, Mapping

from xhs_operations_core.source_notes import StyleHistorySnapshot
from xhs_operations_core.storage import read_json, write_json_atomic

from .profile import StyleProfileError


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_EMOJI = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF]",
    flags=re.UNICODE,
)
_BULLET = re.compile(r"^(?:[-*•]|\d+[.、]|[✅☑️✔️📍💡✨])")


def _time(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StyleProfileError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StyleProfileError(f"{field} must include a timezone")
    return value


def _safe_id(value: str, field: str) -> str:
    result = str(value or "").strip()
    if _SAFE_ID.fullmatch(result) is None:
        raise StyleProfileError(f"invalid post voice {field}")
    return result


def _normalized(value: str) -> str:
    return " ".join(str(value or "").split())


@dataclass(frozen=True)
class PostVoiceSample:
    sample_id: str
    note_id_hash: str
    post_hash: str
    title_char_count: int
    body_char_count: int
    paragraph_count: int
    hashtag_count: int
    emoji_count: int
    question_mark_count: int
    exclamation_mark_count: int
    bullet_line_count: int

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PostVoiceSample":
        required = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != required:
            raise StyleProfileError("stored post voice sample fields are incomplete or unknown")
        converted = dict(value)
        if not isinstance(converted["sample_id"], str) or not converted["sample_id"].startswith("post_sample_"):
            raise StyleProfileError("stored post voice sample_id is invalid")
        for field in ("note_id_hash", "post_hash"):
            if re.fullmatch(r"[0-9a-f]{64}", str(converted[field])) is None:
                raise StyleProfileError(f"stored post voice {field} is invalid")
        for field in required - {"sample_id", "note_id_hash", "post_hash"}:
            if type(converted[field]) is not int or converted[field] < 0:
                raise StyleProfileError(f"stored post voice {field} is invalid")
        return cls(**converted)


@dataclass(frozen=True)
class PostVoiceProfile:
    profile_id: str
    profile_version: int
    account_id: str
    created_at: str
    source_snapshot_ids: tuple[str, ...]
    source_snapshot_hashes: tuple[str, ...]
    samples: tuple[PostVoiceSample, ...]
    sample_count: int
    confidence: str
    average_title_char_count: float
    average_body_char_count: float
    median_body_char_count: float
    average_paragraph_count: float
    hashtag_per_post: float
    emoji_per_post: float
    question_mark_per_post: float
    exclamation_mark_per_post: float
    bullet_line_per_post: float
    style_directives: tuple[str, ...]
    forbidden_uses: tuple[str, ...]
    stores_raw_post_text: bool
    content_hash: str

    def _hash_payload(self) -> dict[str, object]:
        value = self.to_dict()
        value.pop("profile_id")
        value.pop("content_hash")
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "account_id": self.account_id,
            "created_at": self.created_at,
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "source_snapshot_hashes": list(self.source_snapshot_hashes),
            "samples": [item.to_dict() for item in self.samples],
            "sample_count": self.sample_count,
            "confidence": self.confidence,
            "average_title_char_count": self.average_title_char_count,
            "average_body_char_count": self.average_body_char_count,
            "median_body_char_count": self.median_body_char_count,
            "average_paragraph_count": self.average_paragraph_count,
            "hashtag_per_post": self.hashtag_per_post,
            "emoji_per_post": self.emoji_per_post,
            "question_mark_per_post": self.question_mark_per_post,
            "exclamation_mark_per_post": self.exclamation_mark_per_post,
            "bullet_line_per_post": self.bullet_line_per_post,
            "style_directives": list(self.style_directives),
            "forbidden_uses": list(self.forbidden_uses),
            "stores_raw_post_text": self.stores_raw_post_text,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PostVoiceProfile":
        required = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != required:
            raise StyleProfileError("stored post voice profile fields are incomplete or unknown")
        converted = dict(value)
        for field in ("source_snapshot_ids", "source_snapshot_hashes", "style_directives", "forbidden_uses"):
            raw = converted[field]
            if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
                raise StyleProfileError(f"stored post voice {field} is invalid")
            converted[field] = tuple(raw)
        raw_samples = converted["samples"]
        if not isinstance(raw_samples, list):
            raise StyleProfileError("stored post voice samples are invalid")
        converted["samples"] = tuple(PostVoiceSample.from_dict(item) for item in raw_samples)
        if converted["profile_version"] != 1 or converted["stores_raw_post_text"] is not False:
            raise StyleProfileError("stored post voice version or privacy state is invalid")
        if converted["confidence"] not in {"low", "medium", "high"}:
            raise StyleProfileError("stored post voice confidence is invalid")
        if converted["sample_count"] != len(converted["samples"]) or converted["sample_count"] < 1:
            raise StyleProfileError("stored post voice sample count is inconsistent")
        if len({item.note_id_hash for item in converted["samples"]}) != converted["sample_count"]:
            raise StyleProfileError("stored post voice samples are not unique")
        if len(converted["source_snapshot_ids"]) != len(converted["source_snapshot_hashes"]):
            raise StyleProfileError("stored post voice source binding is invalid")
        if any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in converted["source_snapshot_hashes"]):
            raise StyleProfileError("stored post voice source hash is invalid")
        _safe_id(str(converted["account_id"]), "account_id")
        _time(str(converted["created_at"]), "post_voice.created_at")
        expected = sha256(
            json.dumps(
                {key: value for key, value in cls(**converted)._hash_payload().items()},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if converted["content_hash"] != expected:
            raise StyleProfileError("stored post voice content hash mismatch")
        if converted["profile_id"] != "post_voice_" + expected[:16]:
            raise StyleProfileError("stored post voice profile ID is invalid")
        return cls(**converted)


def _sample(note_id: str, title: str, body: str) -> PostVoiceSample | None:
    title_text = _normalized(title)
    body_text = _normalized(body)
    if not title_text and not body_text:
        return None
    if "[已脱敏]" in title_text or "[已脱敏]" in body_text:
        return None
    raw_lines = [line.strip() for line in str(body or "").splitlines() if line.strip()]
    paragraphs = max(1, len(raw_lines))
    note_id_hash = sha256(note_id.encode("utf-8")).hexdigest()
    post_hash = sha256(
        json.dumps({"title": title_text, "body": body_text}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return PostVoiceSample(
        sample_id="post_sample_" + note_id_hash[:16],
        note_id_hash=note_id_hash,
        post_hash=post_hash,
        title_char_count=len(title_text),
        body_char_count=len(body_text),
        paragraph_count=paragraphs,
        hashtag_count=len(re.findall(r"#[^#\s]+", body_text)),
        emoji_count=len(_EMOJI.findall(title_text + body_text)),
        question_mark_count=body_text.count("?") + body_text.count("？"),
        exclamation_mark_count=body_text.count("!") + body_text.count("！"),
        bullet_line_count=sum(bool(_BULLET.match(line)) for line in raw_lines),
    )


def _average(values: list[int]) -> float:
    return round(sum(values) / len(values), 2)


def build_post_voice_profile(
    snapshot: StyleHistorySnapshot,
    *,
    created_at: str,
    existing: PostVoiceProfile | None = None,
) -> PostVoiceProfile:
    """Build or incrementally merge a raw-text-free post voice profile."""

    if snapshot.coverage != "bounded_visible_history" or not snapshot.read_only:
        raise StyleProfileError("post voice requires a bounded read-only history snapshot")
    _time(created_at, "post_voice.created_at")
    account_id = _safe_id(snapshot.account_id, "account_id")
    if existing is not None and existing.account_id != account_id:
        raise StyleProfileError("cannot merge post voice from different accounts")
    by_note_hash = {item.note_id_hash: item for item in existing.samples} if existing else {}
    for note in snapshot.notes:
        item = _sample(note.note_id, note.title, note.body)
        if item is not None:
            by_note_hash[item.note_id_hash] = item
    samples = tuple(sorted(by_note_hash.values(), key=lambda item: item.note_id_hash))
    if not samples:
        raise StyleProfileError("no safe owned posts are available for post voice")
    source_pairs = set(zip(
        existing.source_snapshot_ids if existing else (),
        existing.source_snapshot_hashes if existing else (),
    ))
    source_pairs.add((snapshot.snapshot_id, snapshot.content_hash))
    ordered_sources = tuple(sorted(source_pairs))
    body_lengths = [item.body_char_count for item in samples]
    title_lengths = [item.title_char_count for item in samples]
    paragraph_counts = [item.paragraph_count for item in samples]
    count = len(samples)
    directives = (
        f"标题长度优先接近约 {round(sum(title_lengths) / count)} 个字符",
        f"正文长度优先接近约 {round(sum(body_lengths) / count)} 个字符，并保持约 {round(sum(paragraph_counts) / count)} 个可读段落",
        "沿用账号历史帖子的结构密度与符号频率，但不复制原句",
        "只参考去事实化写作特征；活动事实必须来自当前任务输入",
    )
    base = PostVoiceProfile(
        profile_id="pending",
        profile_version=1,
        account_id=account_id,
        created_at=created_at,
        source_snapshot_ids=tuple(item[0] for item in ordered_sources),
        source_snapshot_hashes=tuple(item[1] for item in ordered_sources),
        samples=samples,
        sample_count=count,
        confidence="high" if count >= 20 else "medium" if count >= 5 else "low",
        average_title_char_count=_average(title_lengths),
        average_body_char_count=_average(body_lengths),
        median_body_char_count=float(median(body_lengths)),
        average_paragraph_count=_average(paragraph_counts),
        hashtag_per_post=_average([item.hashtag_count for item in samples]),
        emoji_per_post=_average([item.emoji_count for item in samples]),
        question_mark_per_post=_average([item.question_mark_count for item in samples]),
        exclamation_mark_per_post=_average([item.exclamation_mark_count for item in samples]),
        bullet_line_per_post=_average([item.bullet_line_count for item in samples]),
        style_directives=directives,
        forbidden_uses=(
            "不得保存或输出历史帖子正文",
            "不得复用历史帖子的活动事实、日期、价格、地点、承诺或联系方式",
            "不得复制历史帖子原句",
            "不得让 post_voice 覆盖当前发布计划的事实与授权",
        ),
        stores_raw_post_text=False,
        content_hash="",
    )
    digest = sha256(
        json.dumps(base._hash_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return PostVoiceProfile(**{**base.__dict__, "profile_id": "post_voice_" + digest[:16], "content_hash": digest})


class PostVoiceStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "post_voice"

    def path_for(self, account_id: str) -> Path:
        return self.root / f"{_safe_id(account_id, 'account_id')}.json"

    def save(self, profile: PostVoiceProfile) -> Path:
        payload = profile.to_dict()
        envelope_hash = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        path = self.path_for(profile.account_id)
        write_json_atomic(path, {
            "schema_version": 1,
            "profile": payload,
            "envelope_hash": envelope_hash,
        })
        return path

    def load(self, account_id: str, *, missing_ok: bool = False) -> PostVoiceProfile | None:
        value = read_json(self.path_for(account_id), default=None)
        if value is None and missing_ok:
            return None
        if not isinstance(value, dict) or set(value) != {"schema_version", "profile", "envelope_hash"}:
            raise StyleProfileError("stored post voice envelope is missing or invalid")
        if value["schema_version"] != 1 or not isinstance(value["profile"], dict):
            raise StyleProfileError("stored post voice envelope version is invalid")
        expected = sha256(
            json.dumps(value["profile"], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if value["envelope_hash"] != expected:
            raise StyleProfileError("stored post voice envelope integrity failed")
        profile = PostVoiceProfile.from_dict(value["profile"])
        if profile.account_id != account_id:
            raise StyleProfileError("stored post voice account mismatch")
        return profile

    def upsert(self, snapshot: StyleHistorySnapshot, *, created_at: str) -> PostVoiceProfile:
        existing = self.load(snapshot.account_id, missing_ok=True)
        profile = build_post_voice_profile(snapshot, created_at=created_at, existing=existing)
        self.save(profile)
        return profile
