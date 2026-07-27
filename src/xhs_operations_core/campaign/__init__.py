"""Campaign domain models."""

from .models import (
    ActivityType,
    AuthorizedFact,
    Campaign,
    CampaignContractError,
    CampaignStatus,
    FactKind,
    FactSourceType,
    StatusActor,
    StatusTransition,
)
from .repository import (
    CampaignAlreadyExistsError,
    CampaignConflictError,
    CampaignNotFoundError,
    CampaignRepository,
    CampaignRepositoryError,
)
from .validator import (
    CampaignFinding,
    CampaignValidationReport,
    FindingSeverity,
    validate_campaign,
)
from .uat_proposal import (
    BoundedCampaignUatProposal,
    CampaignUatProposalError,
    CAMPAIGN_UAT_PROPOSAL_CONFIRMATION,
    CONFIRMATION_TIME_WINDOW,
)

__all__ = [
    "ActivityType",
    "AuthorizedFact",
    "Campaign",
    "CampaignContractError",
    "CampaignStatus",
    "FactKind",
    "FactSourceType",
    "StatusActor",
    "StatusTransition",
    "CampaignAlreadyExistsError",
    "CampaignConflictError",
    "CampaignNotFoundError",
    "CampaignRepository",
    "CampaignRepositoryError",
    "CampaignFinding",
    "CampaignValidationReport",
    "FindingSeverity",
    "validate_campaign",
    "BoundedCampaignUatProposal",
    "CampaignUatProposalError",
    "CAMPAIGN_UAT_PROPOSAL_CONFIRMATION",
    "CONFIRMATION_TIME_WINDOW",
]
