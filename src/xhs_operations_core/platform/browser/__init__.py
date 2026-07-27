"""Dedicated browser profile and read-only login checks."""

from .config import BrowserConfig, BrowserConfigError, LoginSignature, load_browser_config
from .login import (
    FakeLoginProbe,
    LoginCheckResult,
    LoginCalibrationReceipt,
    LoginEvidence,
    load_calibration_status,
    evaluate_login_evidence,
    run_readonly_login_check,
    run_manual_login_authorization,
    record_run_agent_login_calibration,
    RUN_AGENT_LOGIN_CONFIRMATION,
)
from .profiles import (
    BrowserProfileError,
    BrowserProfileInUseError,
    BrowserProfileManager,
)
from .readiness import BrowserReadinessReport, check_browser_readiness

__all__ = [
    "BrowserConfig",
    "BrowserConfigError",
    "BrowserProfileError",
    "BrowserProfileInUseError",
    "BrowserProfileManager",
    "BrowserReadinessReport",
    "FakeLoginProbe",
    "LoginCheckResult",
    "LoginCalibrationReceipt",
    "LoginEvidence",
    "load_calibration_status",
    "LoginSignature",
    "evaluate_login_evidence",
    "check_browser_readiness",
    "load_browser_config",
    "run_readonly_login_check",
    "run_manual_login_authorization",
    "record_run_agent_login_calibration",
    "RUN_AGENT_LOGIN_CONFIRMATION",
]
