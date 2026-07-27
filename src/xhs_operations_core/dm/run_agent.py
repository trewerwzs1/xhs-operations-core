"""Identity-bound DMPort implemented only through the pinned Run Agent gateway."""

from __future__ import annotations

import hashlib
from pathlib import Path

from xhs_operations_core.platform.xhs import RunAgentClient

from .conversation import DMConversationSnapshot
from .runtime import DMWriteResult


class RunAgentDMPort:
    """Read and send on one already-open exact DM conversation."""

    def __init__(
        self,
        project_root: Path,
        *,
        account_id: str,
        conversation_id: str,
        expected_peer_ref_hash: str,
        captured_at: str,
        max_messages: int = 50,
    ) -> None:
        self.client = RunAgentClient(Path(project_root))
        self.account_id = account_id
        self.conversation_id = conversation_id
        self.expected_peer_ref_hash = expected_peer_ref_hash
        self.captured_at = captured_at
        self.max_messages = max_messages

    def read_current_conversation(self, conversation_id: str) -> DMConversationSnapshot:
        if conversation_id != self.conversation_id:
            raise ValueError("DM port conversation identity mismatch")
        snapshot, _evidence = self.client.capture_current_dm_snapshot(
            account_id=self.account_id,
            conversation_id=self.conversation_id,
            expected_peer_ref_hash=self.expected_peer_ref_hash,
            captured_at=self.captured_at,
            max_messages=self.max_messages,
        )
        return snapshot

    def send_one_message(self, conversation_id: str, text: str) -> DMWriteResult:
        if conversation_id != self.conversation_id:
            raise ValueError("DM port conversation identity mismatch")
        result = self.client.send_current_dm_message(
            expected_peer_ref_hash=self.expected_peer_ref_hash,
            content=text,
        )
        content_hash = hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
        verified = (
            result.get("verified") is True
            and result.get("contentHash") == content_hash
            and result.get("peer_ref_hash") == self.expected_peer_ref_hash
            and result.get("platform_actions_executed") == 1
        )
        return DMWriteResult(
            attempted=True,
            verified=verified,
            result_ref="xhs_dm_visible_" + content_hash[:20] if verified else "",
            evidence={
                "visible_text_verified": verified,
                "verification": result.get("verification", ""),
                "peer_ref_hash": self.expected_peer_ref_hash,
                "content_hash": content_hash,
            },
        )
