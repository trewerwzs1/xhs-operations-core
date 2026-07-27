"""Audience and search discovery contracts."""

from .planning import (
    AudienceProfile,
    AudienceSegment,
    DiscoveryPlan,
    DiscoveryPlanError,
    QuerySpec,
    build_discovery_plan,
)
from .candidate import (
    CandidateAssessmentError,
    CandidateEvidence,
    CandidateInteractionPlan,
    assess_comment_candidate,
)

__all__ = [
    "AudienceProfile",
    "AudienceSegment",
    "DiscoveryPlan",
    "DiscoveryPlanError",
    "QuerySpec",
    "build_discovery_plan",
    "CandidateAssessmentError",
    "CandidateEvidence",
    "CandidateInteractionPlan",
    "assess_comment_candidate",
]
