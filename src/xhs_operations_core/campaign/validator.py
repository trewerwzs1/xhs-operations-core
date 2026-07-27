"""Higher-level Campaign readiness validation and review findings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum

from .models import (
    ActivityType,
    Campaign,
    CampaignContractError,
    CampaignStatus,
    FactKind,
    _timestamp,
)


class FindingSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class CampaignFinding:
    code: str
    severity: FindingSeverity
    message: str

    def to_dict(self) -> dict[str, str]:
        payload = asdict(self)
        payload["severity"] = self.severity.value
        return payload


@dataclass(frozen=True)
class CampaignValidationReport:
    campaign_id: str
    checked_at: str
    structurally_valid: bool
    can_activate: bool
    findings: tuple[CampaignFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(item.severity is FindingSeverity.ERROR for item in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "campaign_id": self.campaign_id,
            "checked_at": self.checked_at,
            "structurally_valid": self.structurally_valid,
            "can_activate": self.can_activate,
            "findings": [item.to_dict() for item in self.findings],
        }


def validate_campaign(
    campaign: Campaign,
    *,
    checked_at: str,
    minimum_classification_confidence: float = 0.75,
) -> CampaignValidationReport:
    """Evaluate whether a structurally valid Campaign is ready for activation."""

    now: datetime = _timestamp("checked_at", checked_at)
    findings: list[CampaignFinding] = []

    if type(minimum_classification_confidence) not in {int, float} or not (
        0 <= minimum_classification_confidence <= 1
    ):
        raise CampaignContractError(
            "minimum_classification_confidence must be between 0 and 1"
        )

    if now < _timestamp("updated_at", campaign.updated_at):
        findings.append(
            CampaignFinding(
                "validation_time_before_campaign_update",
                FindingSeverity.ERROR,
                "Campaign cannot be validated using a time before its latest update.",
            )
        )

    if campaign.activity_type == ActivityType.NOT_AN_ACTIVITY:
        findings.append(
            CampaignFinding(
                "not_an_activity",
                FindingSeverity.ERROR,
                "The latest note is not classified as an activity.",
            )
        )
    if campaign.classification_confidence < minimum_classification_confidence:
        findings.append(
            CampaignFinding(
                "classification_confidence_low",
                FindingSeverity.ERROR,
                "Activity classification confidence is below the configured threshold.",
            )
        )

    present = {fact.kind: fact for fact in campaign.facts}
    for kind in campaign.required_fact_kinds:
        item = present.get(kind)
        if item is None:
            findings.append(
                CampaignFinding(
                    f"required_fact_missing:{kind.value}",
                    FindingSeverity.ERROR,
                    f"Required fact is missing: {kind.value}.",
                )
            )
        elif not item.approved_for_public:
            findings.append(
                CampaignFinding(
                    f"required_fact_not_public:{kind.value}",
                    FindingSeverity.ERROR,
                    f"Required fact is not approved for public use: {kind.value}.",
                )
            )
        elif not item.is_valid_at(checked_at):
            findings.append(
                CampaignFinding(
                    f"required_fact_expired:{kind.value}",
                    FindingSeverity.ERROR,
                    f"Required fact has expired: {kind.value}.",
                )
            )

    if campaign.missing_fact_kinds:
        findings.append(
            CampaignFinding(
                "declared_missing_facts",
                FindingSeverity.ERROR,
                "Campaign still declares missing required facts.",
            )
        )
    if not campaign.allowed_actions:
        findings.append(
            CampaignFinding(
                "no_allowed_actions",
                FindingSeverity.ERROR,
                "Campaign has no allowed interaction actions.",
            )
        )
    if now > _timestamp("active_until", campaign.active_until):
        findings.append(
            CampaignFinding(
                "campaign_window_ended",
                FindingSeverity.ERROR,
                "Campaign active window has ended.",
            )
        )
    elif now < _timestamp("active_from", campaign.active_from):
        findings.append(
            CampaignFinding(
                "campaign_window_not_started",
                FindingSeverity.INFO,
                "Campaign is valid but its active window has not started.",
            )
        )

    if campaign.metadata.get("fixture_only") is True:
        findings.append(
            CampaignFinding(
                "fixture_only",
                FindingSeverity.ERROR,
                "Fixture Campaign cannot be activated or used for platform actions.",
            )
        )
    if campaign.status in {CampaignStatus.COMPLETED, CampaignStatus.EXPIRED}:
        findings.append(
            CampaignFinding(
                "terminal_campaign",
                FindingSeverity.ERROR,
                "Completed or expired Campaign cannot be activated.",
            )
        )
    if campaign.status is CampaignStatus.BLOCKED:
        findings.append(
            CampaignFinding(
                "campaign_blocked",
                FindingSeverity.ERROR,
                "Blocked Campaign must be re-analyzed before activation.",
            )
        )
    if FactKind.PRICE not in present:
        findings.append(
            CampaignFinding(
                "price_not_available",
                FindingSeverity.WARNING,
                "Price is unavailable and must not be invented in interactions.",
            )
        )

    errors = any(item.severity is FindingSeverity.ERROR for item in findings)
    can_activate = not errors and campaign.status in {
        CampaignStatus.READY,
        CampaignStatus.PAUSED,
        CampaignStatus.ACTIVE,
    }
    return CampaignValidationReport(
        campaign_id=campaign.campaign_id,
        checked_at=checked_at,
        structurally_valid=True,
        can_activate=can_activate,
        findings=tuple(findings),
    )
