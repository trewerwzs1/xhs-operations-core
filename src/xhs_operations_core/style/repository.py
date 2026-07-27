"""Local lifecycle operations for deletable style-profile artifacts."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re

from xhs_operations_core.storage import read_json, write_json_atomic

from .profile import ReplyStyleProfile, StyleProfileError


class StyleProfileStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "style_profiles"

    def path_for(self, account_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", account_id) is None:
            raise StyleProfileError("invalid style profile account_id")
        return self.root / f"{account_id}.json"

    def save(self, profile: ReplyStyleProfile) -> Path:
        path = self.path_for(profile.account_id)
        payload = profile.to_dict()
        envelope_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        write_json_atomic(path, {
            "schema_version": 1,
            "profile": payload,
            "envelope_hash": envelope_hash,
        })
        return path

    def load(self, account_id: str, *, missing_ok: bool = False) -> ReplyStyleProfile | None:
        path = self.path_for(account_id)
        value = read_json(path, default=None)
        if value is None and missing_ok:
            return None
        if not isinstance(value, dict) or set(value) != {"schema_version", "profile", "envelope_hash"}:
            raise StyleProfileError("stored style profile envelope is missing or invalid")
        if value["schema_version"] != 1 or not isinstance(value["profile"], dict):
            raise StyleProfileError("stored style profile envelope version is invalid")
        expected = hashlib.sha256(
            json.dumps(value["profile"], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if value["envelope_hash"] != expected:
            raise StyleProfileError("stored style profile envelope integrity failed")
        profile = ReplyStyleProfile.from_dict(value["profile"])
        if profile.account_id != account_id:
            raise StyleProfileError("stored style profile account mismatch")
        return profile

    def delete(self, account_id: str) -> bool:
        path = self.path_for(account_id)
        if not path.exists():
            return False
        path.unlink()
        return True
