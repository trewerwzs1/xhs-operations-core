"""Xiaohongshu visible-UI platform adapter."""

from .errors import LiveInteractionError
from .capabilities import (
    CapabilityAccess,
    CapabilityRegistry,
    CapabilitySurface,
    XhsCapability,
    XhsCapabilityDeniedError,
)
from .gateway import XhsOperationGateway
from .provenance import audit_v1_primitive_provenance
from .readonly_candidate import (
    CANDIDATE_READONLY_CONFIRMATION,
    CandidateReadOnlyResult,
    CandidateSequenceResult,
    capture_candidate_sequence_readonly,
    capture_single_candidate_readonly,
)
from .method_lock import (
    REQUIRED_XHS_CALL_METHOD,
    FROZEN_XHS_CALL_METHODS,
    XhsCallMethodLockedError,
    require_approved_xhs_call_method,
)
from .run_agent import (
    BOUNDED_WRITE_UAT_CONFIRMATION,
    RunAgentClient,
    RunAgentError,
    RunAgentVendorStatus,
    RISK_CLASS_PLATFORM,
    RISK_CLASS_TECHNICAL,
    has_explicit_platform_risk,
    sanitize_run_agent_output,
)

__all__ = [
    "LiveInteractionError",
    "CapabilityAccess",
    "CapabilityRegistry",
    "CapabilitySurface",
    "XhsCapability",
    "XhsCapabilityDeniedError",
    "XhsOperationGateway",
    "audit_v1_primitive_provenance",
    "CANDIDATE_READONLY_CONFIRMATION",
    "CandidateReadOnlyResult",
    "CandidateSequenceResult",
    "capture_candidate_sequence_readonly",
    "capture_single_candidate_readonly",
    "REQUIRED_XHS_CALL_METHOD",
    "FROZEN_XHS_CALL_METHODS",
    "XhsCallMethodLockedError",
    "require_approved_xhs_call_method",
    "RunAgentClient",
    "BOUNDED_WRITE_UAT_CONFIRMATION",
    "RunAgentError",
    "RunAgentVendorStatus",
    "RISK_CLASS_PLATFORM",
    "RISK_CLASS_TECHNICAL",
    "has_explicit_platform_risk",
    "sanitize_run_agent_output",
]
