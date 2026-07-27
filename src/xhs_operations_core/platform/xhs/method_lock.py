"""Project-wide hard lock for the only approved Xiaohongshu call method."""

from __future__ import annotations


REQUIRED_XHS_CALL_METHOD = "ranfang_run_agent"
FROZEN_XHS_CALL_METHODS = (
    "legacy_playwright",
    "direct_chrome",
    "ad_hoc_script",
    "browser_extension_control",
    "computer_use",
)


class XhsCallMethodLockedError(RuntimeError):
    pass


def require_approved_xhs_call_method(selected_method: str) -> None:
    if selected_method != REQUIRED_XHS_CALL_METHOD:
        raise XhsCallMethodLockedError(
            "Xiaohongshu live access is frozen: required method is "
            f"{REQUIRED_XHS_CALL_METHOD}; selected method was {selected_method}"
        )
