"""Local, consent-bound corpus of the account owner's historical replies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from xhs_operations_core.source_notes import StyleHistorySnapshot
from xhs_operations_core.storage import read_json, write_json_atomic

from .profile import StyleProfileError


CORPUS_CONFIRMATION = "I_APPROVE_LOCAL_OWN_REPLY_CORPUS"
CORPUS_DELETE_CONFIRMATION = "I_DELETE_LOCAL_OWN_REPLY_CORPUS"


def _timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StyleProfileError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StyleProfileError(f"{field} must include a timezone")
    return value


def _safe_id(name: str, value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text) is None:
        raise StyleProfileError(f"{name} must be a safe id")
    return text


def _normalized(value: object, *, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _tokens(value: str) -> set[str]:
    text = value.casefold()
    tokens = set(re.findall(r"[a-z0-9]{2,}", text))
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return tokens


@dataclass(frozen=True)
class ReplyCorpusEntry:
    entry_id: str
    note_id: str
    note_title: str
    comment_id: str
    parent_comment_id: str
    parent_text: str
    reply_text: str
    published_at: str | None
    ownership_evidence: str
    reply_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "note_id": self.note_id,
            "note_title": self.note_title,
            "comment_id": self.comment_id,
            "parent_comment_id": self.parent_comment_id,
            "parent_text": self.parent_text,
            "reply_text": self.reply_text,
            "published_at": self.published_at,
            "ownership_evidence": self.ownership_evidence,
            "reply_hash": self.reply_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReplyCorpusEntry":
        required = {
            "entry_id", "note_id", "note_title", "comment_id", "parent_comment_id",
            "parent_text", "reply_text", "published_at", "ownership_evidence", "reply_hash",
        }
        if set(value) != required:
            raise StyleProfileError("reply corpus entry fields are incomplete or unknown")
        entry = cls(
            entry_id=_safe_id("entry_id", value["entry_id"]),
            note_id=_safe_id("note_id", value["note_id"]),
            note_title=_normalized(value["note_title"], limit=200),
            comment_id=_safe_id("comment_id", value["comment_id"]),
            parent_comment_id=_safe_id("parent_comment_id", value["parent_comment_id"]),
            parent_text=_normalized(value["parent_text"]),
            reply_text=_normalized(value["reply_text"]),
            published_at=value["published_at"],
            ownership_evidence=str(value["ownership_evidence"]),
            reply_hash=str(value["reply_hash"]),
        )
        if not entry.reply_text or not re.fullmatch(r"[0-9a-f]{64}", entry.reply_hash):
            raise StyleProfileError("reply corpus entry text or hash is invalid")
        if entry.published_at is not None:
            _timestamp(str(entry.published_at), "entry.published_at")
        if sha256(entry.reply_text.encode("utf-8")).hexdigest() != entry.reply_hash:
            raise StyleProfileError("reply corpus entry hash mismatch")
        return entry


@dataclass(frozen=True)
class ReplyCorpus:
    corpus_id: str
    schema_version: int
    account_id: str
    consent_ref: str
    created_at: str
    source_snapshot_id: str
    source_snapshot_hash: str
    entries: tuple[ReplyCorpusEntry, ...]
    excluded_private_count: int
    local_only: bool
    stores_raw_reply_text: bool
    retrieval_method: str
    content_hash: str

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, object]:
        return {
            "corpus_id": self.corpus_id,
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "consent_ref": self.consent_ref,
            "created_at": self.created_at,
            "source_snapshot_id": self.source_snapshot_id,
            "source_snapshot_hash": self.source_snapshot_hash,
            "entries": [item.to_dict() for item in self.entries],
            "excluded_private_count": self.excluded_private_count,
            "local_only": self.local_only,
            "stores_raw_reply_text": self.stores_raw_reply_text,
            "retrieval_method": self.retrieval_method,
            "content_hash": self.content_hash,
        }

    def metadata(self) -> dict[str, object]:
        return {
            "corpus_id": self.corpus_id,
            "account_id": self.account_id,
            "created_at": self.created_at,
            "source_snapshot_id": self.source_snapshot_id,
            "entry_count": self.entry_count,
            "excluded_private_count": self.excluded_private_count,
            "local_only": self.local_only,
            "stores_raw_reply_text": self.stores_raw_reply_text,
            "retrieval_method": self.retrieval_method,
            "content_hash": self.content_hash,
            "platform_actions_executed": 0,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReplyCorpus":
        required = {
            "corpus_id", "schema_version", "account_id", "consent_ref", "created_at",
            "source_snapshot_id", "source_snapshot_hash", "entries",
            "excluded_private_count", "local_only", "stores_raw_reply_text",
            "retrieval_method", "content_hash",
        }
        if set(value) != required or not isinstance(value["entries"], list):
            raise StyleProfileError("reply corpus fields are incomplete or unknown")
        corpus = cls(
            corpus_id=_safe_id("corpus_id", value["corpus_id"]),
            schema_version=int(value["schema_version"]),
            account_id=_safe_id("account_id", value["account_id"]),
            consent_ref=_safe_id("consent_ref", value["consent_ref"]),
            created_at=_timestamp(str(value["created_at"]), "created_at"),
            source_snapshot_id=_safe_id("source_snapshot_id", value["source_snapshot_id"]),
            source_snapshot_hash=str(value["source_snapshot_hash"]),
            entries=tuple(ReplyCorpusEntry.from_dict(item) for item in value["entries"]),
            excluded_private_count=int(value["excluded_private_count"]),
            local_only=value["local_only"] is True,
            stores_raw_reply_text=value["stores_raw_reply_text"] is True,
            retrieval_method=str(value["retrieval_method"]),
            content_hash=str(value["content_hash"]),
        )
        if corpus.schema_version != 1 or not corpus.local_only or not corpus.stores_raw_reply_text:
            raise StyleProfileError("reply corpus storage contract is invalid")
        if corpus.retrieval_method != "lexical_overlap_v1":
            raise StyleProfileError("unsupported reply corpus retrieval method")
        if len({item.entry_id for item in corpus.entries}) != len(corpus.entries):
            raise StyleProfileError("reply corpus entry ids must be unique")
        payload = corpus._hash_payload()
        if sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() != corpus.content_hash:
            raise StyleProfileError("reply corpus content hash mismatch")
        return corpus

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "consent_ref": self.consent_ref,
            "source_snapshot_id": self.source_snapshot_id,
            "source_snapshot_hash": self.source_snapshot_hash,
            "entries": [item.to_dict() for item in self.entries],
            "excluded_private_count": self.excluded_private_count,
            "local_only": self.local_only,
            "stores_raw_reply_text": self.stores_raw_reply_text,
            "retrieval_method": self.retrieval_method,
        }

    def search(self, query: str, *, limit: int = 5) -> tuple[dict[str, object], ...]:
        query_tokens = _tokens(_normalized(query, limit=300))
        if not query_tokens or type(limit) is not int or not 1 <= limit <= 10:
            raise StyleProfileError("reply corpus query or limit is invalid")
        ranked: list[tuple[int, str, ReplyCorpusEntry]] = []
        for entry in self.entries:
            title_score = len(query_tokens & _tokens(entry.note_title)) * 3
            parent_score = len(query_tokens & _tokens(entry.parent_text)) * 2
            reply_score = len(query_tokens & _tokens(entry.reply_text))
            score = title_score + parent_score + reply_score
            if score:
                ranked.append((score, entry.entry_id, entry))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        return tuple(
            {
                "entry_id": entry.entry_id,
                "note_title": entry.note_title,
                "parent_text": entry.parent_text,
                "reply_text": entry.reply_text,
                "published_at": entry.published_at,
                "score": score,
            }
            for score, _, entry in ranked[:limit]
        )


def build_reply_corpus(
    snapshot: StyleHistorySnapshot,
    *,
    created_at: str,
    confirmation: str,
) -> ReplyCorpus:
    if confirmation != CORPUS_CONFIRMATION:
        raise StyleProfileError("exact local reply corpus consent confirmation is required")
    if snapshot.coverage != "bounded_visible_history" or not snapshot.read_only:
        raise StyleProfileError("reply corpus requires a bounded read-only history snapshot")
    _timestamp(created_at, "created_at")
    entries: list[ReplyCorpusEntry] = []
    excluded = 0
    seen_hashes: set[str] = set()
    for note in snapshot.notes:
        comments = {item.comment_id: item for item in note.comments}
        for comment in note.comments:
            if comment.commenter_role != "account_owner":
                continue
            if comment.privacy_flags:
                excluded += 1
                continue
            reply_text = _normalized(comment.text)
            reply_hash = sha256(reply_text.encode("utf-8")).hexdigest()
            if reply_hash in seen_hashes:
                continue
            parent = comments.get(str(comment.parent_comment_id))
            parent_text = ""
            if parent is not None and not parent.privacy_flags:
                parent_text = _normalized(parent.text)
            entry_key = f"{note.note_id}|{comment.comment_id}|{reply_hash}"
            entries.append(
                ReplyCorpusEntry(
                    entry_id="reply_" + sha256(entry_key.encode("utf-8")).hexdigest()[:16],
                    note_id=_safe_id("note_id", note.note_id),
                    note_title=_normalized(note.title, limit=200),
                    comment_id=_safe_id("comment_id", comment.comment_id),
                    parent_comment_id=_safe_id("parent_comment_id", comment.parent_comment_id),
                    parent_text=parent_text,
                    reply_text=reply_text,
                    published_at=comment.published_at,
                    ownership_evidence=str(comment.ownership_evidence),
                    reply_hash=reply_hash,
                )
            )
            seen_hashes.add(reply_hash)
    if not entries:
        raise StyleProfileError("no safe owned replies are available for the local corpus")
    entries.sort(key=lambda item: (item.published_at or "", item.entry_id), reverse=True)
    base = ReplyCorpus(
        corpus_id="pending",
        schema_version=1,
        account_id=_safe_id("account_id", snapshot.account_id),
        consent_ref=_safe_id("consent_ref", snapshot.consent_ref),
        created_at=created_at,
        source_snapshot_id=_safe_id("source_snapshot_id", snapshot.snapshot_id),
        source_snapshot_hash=snapshot.content_hash,
        entries=tuple(entries),
        excluded_private_count=excluded,
        local_only=True,
        stores_raw_reply_text=True,
        retrieval_method="lexical_overlap_v1",
        content_hash="",
    )
    digest = sha256(
        json.dumps(base._hash_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ReplyCorpus(**{**base.__dict__, "corpus_id": "corpus_" + digest[:16], "content_hash": digest})


def merge_reply_corpora(existing: ReplyCorpus, incoming: ReplyCorpus) -> ReplyCorpus:
    """Append a new bounded capture without losing replies captured earlier."""

    if existing.account_id != incoming.account_id:
        raise StyleProfileError("cannot merge reply corpora from different accounts")
    if existing.consent_ref != incoming.consent_ref:
        raise StyleProfileError("reply corpus consent changed; rebuild or delete explicitly")
    if existing.source_snapshot_hash == incoming.source_snapshot_hash:
        return existing
    existing_reply_hashes = {item.reply_hash for item in existing.entries}
    incoming_reply_hashes = {item.reply_hash for item in incoming.entries}
    if incoming_reply_hashes.issubset(existing_reply_hashes):
        return existing
    by_hash = {item.reply_hash: item for item in existing.entries}
    for item in incoming.entries:
        by_hash[item.reply_hash] = item
    entries = tuple(sorted(
        by_hash.values(), key=lambda item: (item.published_at or "", item.entry_id), reverse=True
    ))
    source_key = "|".join(sorted({existing.source_snapshot_hash, incoming.source_snapshot_hash}))
    source_hash = sha256(source_key.encode("utf-8")).hexdigest()
    base = ReplyCorpus(
        corpus_id="pending", schema_version=1, account_id=existing.account_id,
        consent_ref=existing.consent_ref, created_at=incoming.created_at,
        source_snapshot_id="merged_" + source_hash[:16], source_snapshot_hash=source_hash,
        entries=entries,
        excluded_private_count=max(existing.excluded_private_count, incoming.excluded_private_count),
        local_only=True, stores_raw_reply_text=True,
        retrieval_method="lexical_overlap_v1", content_hash="",
    )
    digest = sha256(
        json.dumps(base._hash_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ReplyCorpus(**{**base.__dict__, "corpus_id": "corpus_" + digest[:16], "content_hash": digest})


class ReplyCorpusStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "reply_corpus"

    def path_for(self, account_id: str) -> Path:
        return self.root / f"{_safe_id('account_id', account_id)}.json"

    def save(self, corpus: ReplyCorpus) -> Path:
        path = self.path_for(corpus.account_id)
        write_json_atomic(path, corpus.to_dict())
        return path

    def upsert(self, corpus: ReplyCorpus) -> tuple[Path, ReplyCorpus]:
        path = self.path_for(corpus.account_id)
        current = read_json(path, default=None)
        merged = corpus
        if current is not None:
            if not isinstance(current, dict):
                raise StyleProfileError("local reply corpus is corrupt")
            merged = merge_reply_corpora(ReplyCorpus.from_dict(current), corpus)
        write_json_atomic(path, merged.to_dict())
        return path, merged

    def load(self, account_id: str) -> ReplyCorpus:
        value = read_json(self.path_for(account_id))
        if not isinstance(value, dict):
            raise StyleProfileError("local reply corpus is missing")
        return ReplyCorpus.from_dict(value)

    def delete(self, account_id: str, *, confirmation: str) -> bool:
        if confirmation != CORPUS_DELETE_CONFIRMATION:
            raise StyleProfileError("exact local reply corpus delete confirmation is required")
        path = self.path_for(account_id)
        if not path.exists():
            return False
        path.unlink()
        return True
