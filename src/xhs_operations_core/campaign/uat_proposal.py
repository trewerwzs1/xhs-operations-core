"""Immutable confirmation-anchored Campaign proposal for one bounded UAT."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from .models import Campaign
from .models import CampaignStatus, StatusActor
from .validator import CampaignValidationReport, validate_campaign


class CampaignUatProposalError(ValueError):
    pass


CONFIRMATION_TIME_WINDOW = "confirmation_time_plus_duration"
CAMPAIGN_UAT_PROPOSAL_CONFIRMATION = "I_CONFIRM_CAMPAIGN_UAT_PROPOSAL"
_BRANCH_ACTION = {
    "note_like_only": "like",
    "note_engagement": "comment",
    "comment_like_only": "like",
    "comment_engagement": "reply",
}
_TEMPLATE_FIELDS = set(Campaign.__dataclass_fields__) - {
    "created_at",
    "updated_at",
    "active_from",
    "active_until",
    "transitions",
}


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _confirmed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignUatProposalError(
            "confirmed_at must be timezone-aware ISO"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignUatProposalError("confirmed_at must include timezone")
    return parsed


@dataclass(frozen=True)
class BoundedCampaignUatProposal:
    schema_version: int
    proposal_id: str
    window_policy: str
    window_duration_seconds: int
    intended_preview_branch: str
    excluded_preview_branches: tuple[str, ...]
    text_required: bool
    campaign_template: dict[str, Any]
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise CampaignUatProposalError("unsupported Campaign UAT proposal schema")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.proposal_id) is None:
            raise CampaignUatProposalError("proposal_id is invalid")
        if self.window_policy != CONFIRMATION_TIME_WINDOW:
            raise CampaignUatProposalError("unsupported Campaign UAT window policy")
        if type(self.window_duration_seconds) is not int or not (
            60 <= self.window_duration_seconds <= 7 * 24 * 60 * 60
        ):
            raise CampaignUatProposalError(
                "window_duration_seconds must be between 60 and 604800"
            )
        if self.intended_preview_branch not in _BRANCH_ACTION:
            raise CampaignUatProposalError("intended_preview_branch is invalid")
        if not isinstance(self.excluded_preview_branches, tuple) or len(
            set(self.excluded_preview_branches)
        ) != len(self.excluded_preview_branches):
            raise CampaignUatProposalError(
                "excluded_preview_branches must be a unique tuple"
            )
        if any(item not in _BRANCH_ACTION for item in self.excluded_preview_branches):
            raise CampaignUatProposalError("excluded_preview_branch is invalid")
        if self.intended_preview_branch in self.excluded_preview_branches:
            raise CampaignUatProposalError("intended branch cannot also be excluded")
        if (
            self.intended_preview_branch == "comment_like_only"
            and "note_like_only" not in self.excluded_preview_branches
        ):
            raise CampaignUatProposalError(
                "comment_like_only UAT must exclude inherited note_like_only"
            )
        if type(self.text_required) is not bool:
            raise CampaignUatProposalError("text_required must be boolean")
        if self.intended_preview_branch.endswith("like_only") and self.text_required:
            raise CampaignUatProposalError("like-only preview cannot require text")
        if not isinstance(self.campaign_template, dict) or set(
            self.campaign_template
        ) != _TEMPLATE_FIELDS:
            raise CampaignUatProposalError(
                "campaign_template fields are incomplete or unknown"
            )
        if self.campaign_template.get("status") != "analyzed":
            raise CampaignUatProposalError("campaign_template must start analyzed")
        metadata = self.campaign_template.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("fixture_only") is not False:
            raise CampaignUatProposalError("campaign_template must be non-fixture")
        allowed = self.campaign_template.get("allowed_actions")
        if not isinstance(allowed, list) or _BRANCH_ACTION[
            self.intended_preview_branch
        ] not in allowed:
            raise CampaignUatProposalError(
                "campaign allowed_actions do not cover intended preview branch"
            )
        base = self.to_dict()
        base.pop("content_hash")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_hash or ""):
            raise CampaignUatProposalError("content_hash must be SHA-256 hex")
        if self.content_hash != _canonical_hash(base):
            raise CampaignUatProposalError("Campaign UAT proposal hash mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "window_policy": self.window_policy,
            "window_duration_seconds": self.window_duration_seconds,
            "intended_preview_branch": self.intended_preview_branch,
            "excluded_preview_branches": list(self.excluded_preview_branches),
            "text_required": self.text_required,
            "campaign_template": self.campaign_template,
            "content_hash": self.content_hash,
        }

    @classmethod
    def build(
        cls,
        *,
        proposal_id: str,
        window_duration_seconds: int,
        intended_preview_branch: str,
        excluded_preview_branches: tuple[str, ...],
        text_required: bool,
        campaign_template: Mapping[str, Any],
    ) -> "BoundedCampaignUatProposal":
        base = {
            "schema_version": 1,
            "proposal_id": proposal_id,
            "window_policy": CONFIRMATION_TIME_WINDOW,
            "window_duration_seconds": window_duration_seconds,
            "intended_preview_branch": intended_preview_branch,
            "excluded_preview_branches": list(excluded_preview_branches),
            "text_required": text_required,
            "campaign_template": dict(campaign_template),
        }
        return cls(
            schema_version=1,
            proposal_id=proposal_id,
            window_policy=CONFIRMATION_TIME_WINDOW,
            window_duration_seconds=window_duration_seconds,
            intended_preview_branch=intended_preview_branch,
            excluded_preview_branches=excluded_preview_branches,
            text_required=text_required,
            campaign_template=dict(campaign_template),
            content_hash=_canonical_hash(base),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BoundedCampaignUatProposal":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise CampaignUatProposalError(
                "Campaign UAT proposal fields are incomplete or unknown"
            )
        payload = dict(value)
        excluded = payload.get("excluded_preview_branches")
        if not isinstance(excluded, list):
            raise CampaignUatProposalError("excluded_preview_branches must be a list")
        payload["excluded_preview_branches"] = tuple(excluded)
        return cls(**payload)

    def materialize(self, *, confirmed_at: str) -> Campaign:
        anchor = _confirmed_at(confirmed_at)
        active_until = anchor + timedelta(seconds=self.window_duration_seconds)
        payload = dict(self.campaign_template)
        payload.update(
            {
                "created_at": confirmed_at,
                "updated_at": confirmed_at,
                "active_from": confirmed_at,
                "active_until": active_until.isoformat(),
                "transitions": [],
            }
        )
        metadata = dict(payload["metadata"])
        metadata.update(
            {
                "uat_proposal_id": self.proposal_id,
                "uat_proposal_hash": self.content_hash,
                "uat_window_policy": self.window_policy,
                "uat_window_duration_seconds": self.window_duration_seconds,
                "uat_window_confirmed_at": confirmed_at,
                "intended_preview_branch": self.intended_preview_branch,
                "excluded_preview_branches": list(self.excluded_preview_branches),
                "text_required": self.text_required,
            }
        )
        payload["metadata"] = metadata
        return Campaign.from_dict(payload)

    def validate_materialization(
        self,
        *,
        confirmed_at: str,
    ) -> CampaignValidationReport:
        campaign = self.materialize(confirmed_at=confirmed_at)
        return validate_campaign(campaign, checked_at=confirmed_at)

    def confirm(
        self,
        *,
        confirmed_at: str,
        confirmation: str,
    ) -> tuple[Campaign, CampaignValidationReport]:
        """Materialize one immutable proposal and preserve its exact lifecycle."""

        if confirmation != CAMPAIGN_UAT_PROPOSAL_CONFIRMATION:
            raise CampaignUatProposalError(
                "exact Campaign UAT proposal confirmation is required"
            )
        analyzed = self.materialize(confirmed_at=confirmed_at)
        awaiting = analyzed.transition(
            CampaignStatus.AWAITING_CONFIRMATION,
            changed_at=confirmed_at,
            actor=StatusActor.SYSTEM,
            reason="proposal hash verified and exact confirmation received",
        )
        ready = awaiting.transition(
            CampaignStatus.READY,
            changed_at=confirmed_at,
            actor=StatusActor.USER,
            reason="user confirmed exact UAT bounds, facts, and readonly scope",
        )
        report = validate_campaign(ready, checked_at=confirmed_at)
        if not report.ok or not report.can_activate:
            codes = ",".join(item.code for item in report.findings)
            raise CampaignUatProposalError(
                "confirmed Campaign failed ready validation: " + codes
            )
        return ready, report
