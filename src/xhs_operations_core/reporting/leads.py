"""Privacy-minimized persistence for qualified leads and DM candidates."""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xhs_operations_core.orchestration import PostEngagementPlan
from xhs_operations_core.storage import read_json, write_json_atomic


class LeadStoreError(ValueError):
    pass


def _lead_id(campaign_id: str, user_id: str, note_id: str, target_comment_id: str) -> str:
    value = f"{campaign_id}|{user_id}|{note_id}|{target_comment_id}"
    return "lead_" + sha256(value.encode("utf-8")).hexdigest()[:20]


def _later(left: str, right: str) -> str:
    a = datetime.fromisoformat(left.replace("Z", "+00:00"))
    b = datetime.fromisoformat(right.replace("Z", "+00:00"))
    return right if b > a else left


class LeadRecordStore:
    """Persist identifiers and structured dispositions, never raw comment text."""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.root = Path(runtime_dir) / "leads"

    def path(self, campaign_id: str) -> Path:
        if not campaign_id or any(char in campaign_id for char in "\\/:*?\"<>|"):
            raise LeadStoreError("campaign_id is unsafe")
        return self.root / f"{campaign_id}.json"

    def _load(self, campaign_id: str) -> dict[str, Any]:
        value = read_json(self.path(campaign_id), default=None)
        if value is None:
            return {"schema_version": 1, "campaign_id": campaign_id, "account_id": None, "records": {}}
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("campaign_id") != campaign_id
            or not isinstance(value.get("records"), dict)
        ):
            raise LeadStoreError("lead store is corrupt or belongs to another Campaign")
        return value

    def persist_post_engagement_plan(self, plan: PostEngagementPlan) -> dict[str, object]:
        state = self._load(plan.campaign_id)
        if state["account_id"] not in {None, plan.account_id}:
            raise LeadStoreError("lead store account does not match post engagement plan")
        state["account_id"] = plan.account_id
        records: dict[str, dict[str, Any]] = state["records"]
        inputs: dict[tuple[str, str], dict[str, Any]] = {}
        for lead in plan.lead_records:
            key = (lead.user_id, lead.target_comment_id)
            inputs[key] = {
                "candidate_id": lead.candidate_id,
                "user_id": lead.user_id,
                "target_comment_id": lead.target_comment_id,
                "dispositions": {"qualified_lead"},
                "reason_codes": {lead.reason},
                "dm_status": None,
                "dm_blockers": set(),
                "dm_template_id": None,
                "dm_template_hash": None,
                "dm_approval_ref": None,
            }
        for dm in plan.dm_candidates:
            target_comment_id = next(
                (item.target_comment_id for item in plan.lead_records if item.candidate_id == dm.candidate_id),
                f"candidate:{dm.candidate_id}",
            )
            key = (dm.user_id, target_comment_id)
            item = inputs.setdefault(key, {
                "candidate_id": dm.candidate_id,
                "user_id": dm.user_id,
                "target_comment_id": target_comment_id,
                "dispositions": set(),
                "reason_codes": set(),
                "dm_status": None,
                "dm_blockers": set(),
                "dm_template_id": None,
                "dm_template_hash": None,
                "dm_approval_ref": None,
            })
            item["dispositions"].add("dm_candidate")
            item["reason_codes"].add("high_intent_dm_candidate")
            item["dm_status"] = dm.status
            item["dm_blockers"].update(dm.blockers)
            item["dm_template_id"] = dm.template_id
            item["dm_template_hash"] = dm.template_hash
            item["dm_approval_ref"] = dm.approval_ref

        inserted = 0
        updated = 0
        for item in inputs.values():
            lead_id = _lead_id(
                plan.campaign_id, item["user_id"], plan.note_id, item["target_comment_id"]
            )
            existing = records.get(lead_id)
            if existing is None:
                inserted += 1
                first_seen = plan.checked_at
                source_plan_ids: set[str] = set()
                dispositions: set[str] = set()
                reason_codes: set[str] = set()
            else:
                updated += 1
                first_seen = existing["first_seen_at"]
                source_plan_ids = set(existing.get("source_plan_ids", []))
                dispositions = set(existing.get("dispositions", []))
                reason_codes = set(existing.get("reason_codes", []))
            source_plan_ids.add(plan.plan_id)
            dispositions.update(item["dispositions"])
            reason_codes.update(item["reason_codes"])
            records[lead_id] = {
                "lead_id": lead_id,
                "campaign_id": plan.campaign_id,
                "account_id": plan.account_id,
                "note_id": plan.note_id,
                "candidate_id": item["candidate_id"],
                "target_comment_id": item["target_comment_id"],
                "user_id": item["user_id"],
                "dispositions": sorted(dispositions),
                "reason_codes": sorted(reason_codes),
                "dm_status": item["dm_status"] if item["dm_status"] is not None else (existing or {}).get("dm_status"),
                "dm_blockers": sorted(item["dm_blockers"] or set((existing or {}).get("dm_blockers", []))),
                "dm_template_id": item["dm_template_id"] if item["dm_template_id"] is not None else (existing or {}).get("dm_template_id"),
                "dm_template_hash": item["dm_template_hash"] if item["dm_template_hash"] is not None else (existing or {}).get("dm_template_hash"),
                "dm_approval_ref": item["dm_approval_ref"] if item["dm_approval_ref"] is not None else (existing or {}).get("dm_approval_ref"),
                "first_seen_at": first_seen,
                "last_seen_at": plan.checked_at if existing is None else _later(existing["last_seen_at"], plan.checked_at),
                "source_plan_ids": sorted(source_plan_ids),
            }
        state["records"] = dict(sorted(records.items()))
        write_json_atomic(self.path(plan.campaign_id), state)
        return {
            "inserted": inserted,
            "updated": updated,
            "total": len(records),
            "storage_ref": f"runtime/leads/{plan.campaign_id}.json",
            "summary": self.summary(plan.campaign_id),
            "raw_comment_text_stored": False,
            "platform_actions_executed": 0,
        }

    def resolve_dm_candidate(
        self,
        *,
        campaign_id: str,
        account_id: str,
        peer_ref_hash: str,
    ) -> dict[str, Any]:
        """Resolve one previously qualified DM candidate without exposing its raw user ID."""
        if len(peer_ref_hash) != 64 or any(char not in "0123456789abcdef" for char in peer_ref_hash):
            raise LeadStoreError("peer_ref_hash must be lowercase SHA-256 hex")
        state = self._load(campaign_id)
        if state["account_id"] != account_id:
            raise LeadStoreError("lead store account does not match DM account")
        matches = [
            item
            for item in state["records"].values()
            if "dm_candidate" in item.get("dispositions", [])
            and sha256(str(item.get("user_id", "")).encode("utf-8")).hexdigest() == peer_ref_hash
        ]
        if len(matches) != 1:
            raise LeadStoreError("exactly one persisted DM candidate must match the peer binding")
        item = matches[0]
        return {
            "lead_id": item["lead_id"],
            "candidate_id": item["candidate_id"],
            "peer_ref_hash": peer_ref_hash,
            "dm_status": item.get("dm_status"),
        }

    def mark_dm_verified(
        self,
        *,
        campaign_id: str,
        account_id: str,
        peer_ref_hash: str,
        approval_ref: str,
        action_record_id: str,
        verified_at: str,
    ) -> dict[str, Any]:
        """Record a verified one-message DM against its exact persisted lead."""
        _later(verified_at, verified_at)
        if not approval_ref or not action_record_id:
            raise LeadStoreError("DM approval_ref and action_record_id are required")
        resolved = self.resolve_dm_candidate(
            campaign_id=campaign_id,
            account_id=account_id,
            peer_ref_hash=peer_ref_hash,
        )
        state = self._load(campaign_id)
        item = state["records"][resolved["lead_id"]]
        existing_action = item.get("dm_action_record_id")
        if existing_action not in {None, action_record_id}:
            raise LeadStoreError("lead already references a different verified DM action")
        item["dm_status"] = "verified_sent"
        item["dm_blockers"] = []
        item["dm_approval_ref"] = approval_ref
        item["dm_action_record_id"] = action_record_id
        item["dm_sent_at"] = verified_at
        state["records"][resolved["lead_id"]] = item
        write_json_atomic(self.path(campaign_id), state)
        return {
            "lead_id": resolved["lead_id"],
            "candidate_id": resolved["candidate_id"],
            "dm_status": "verified_sent",
            "dm_action_record_id": action_record_id,
            "raw_user_id_returned": False,
        }

    def summary(
        self,
        campaign_id: str,
        *,
        plan_date: str | None = None,
        timezone_name: str = "UTC",
    ) -> dict[str, int]:
        records = list(self._load(campaign_id)["records"].values())
        if plan_date is not None:
            try:
                selected_date = date.fromisoformat(plan_date)
            except (TypeError, ValueError) as exc:
                raise LeadStoreError("plan_date must use YYYY-MM-DD") from exc
            try:
                selected_zone = ZoneInfo(timezone_name)
            except (TypeError, ZoneInfoNotFoundError) as exc:
                raise LeadStoreError("timezone_name is not recognized") from exc
            daily_records: list[dict[str, Any]] = []
            for item in records:
                try:
                    first_seen = datetime.fromisoformat(
                        str(item["first_seen_at"]).replace("Z", "+00:00")
                    )
                except (KeyError, ValueError) as exc:
                    raise LeadStoreError("lead record first_seen_at is invalid") from exc
                if first_seen.tzinfo is None or first_seen.utcoffset() is None:
                    raise LeadStoreError("lead record first_seen_at must include timezone")
                if first_seen.astimezone(selected_zone).date() == selected_date:
                    daily_records.append(item)
            records = daily_records
        return {
            "total_leads": sum(1 for _ in records),
            "qualified_leads": sum("qualified_lead" in item.get("dispositions", []) for item in records),
            "dm_candidates": sum("dm_candidate" in item.get("dispositions", []) for item in records),
            "dm_record_only": sum(item.get("dm_status") == "record_only" for item in records),
            "dm_awaiting_exact_message_resolution": sum(
                item.get("dm_status") == "awaiting_exact_message_resolution" for item in records
            ),
            "dm_verified_sent": sum(item.get("dm_status") == "verified_sent" for item in records),
        }
