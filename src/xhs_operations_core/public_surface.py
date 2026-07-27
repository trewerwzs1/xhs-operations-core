"""Truthful status and command contract for the five V2 public groups."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


LIVE_XHS_PROVIDER = "ranfang_run_agent_xhs_bridge"

PUBLIC_COMMANDS: dict[str, tuple[str, ...]] = {
    "setup": ("doctor", "configure", "status", "voice-learn", "voice-status"),
    "publish": ("prepare", "run", "status"),
    "service": ("start", "heartbeat", "status", "stop"),
    "engage": ("start", "heartbeat", "status", "stop"),
    "review": ("status", "list", "export"),
}

_GROUPS: dict[str, dict[str, Any]] = {
    "setup": {
        "implementation_status": "ready",
        "available_operations": [
            "install",
            "doctor",
            "account_config",
            "extension_instance_enroll",
            "connection_check",
            "platform_account_enroll",
            "platform_read_enable",
            "voice_learn",
            "voice_status",
        ],
        "pending_operations": [],
    },
    "publish": {
        "implementation_status": "autonomous_public_surface_program_ready_live_uat_pending",
        "available_operations": [
            "task_prepare",
            "internal_policy_permit",
            "single_run",
            "visible_verification",
            "operation_ledger_recovery",
        ],
        "pending_operations": ["image_note_live_uat", "video_note_live_uat"],
    },
    "service": {
        "implementation_status": "autonomous_public_surface_program_ready_live_verified_empty_batch",
        "available_operations": [
            "task_start_heartbeat_stop",
            "bounded_inbox_scan",
            "sequential_item_open",
            "conversation_queue",
            "reply_voice_bound_plan",
            "internal_policy_permit",
            "comment_reply_adapter",
            "passive_dm_reply_adapter",
            "saved_scan_batch",
            "opt_out_block",
            "operation_ledger_recovery",
        ],
        "pending_operations": [
            "comment_exact_item_live_read",
            "dm_exact_item_live_read",
            "comment_reply_live_uat",
            "dm_reply_live_uat",
        ],
    },
    "engage": {
        "implementation_status": "autonomous_public_surface_program_ready_live_uat_pending",
        "available_operations": [
            "task_start_heartbeat_stop",
            "task_intent",
            "execution_mandate",
            "internal_action_permit",
            "strategy_pack",
            "exact_query_search",
            "same_batch_sequential_candidate",
            "unified_action_preflight",
            "comment_like_readonly_precheck",
            "unknown_write_reconciliation",
            "note_like",
            "note_comment",
            "comment_like",
            "comment_reply",
            "single_active_dm",
            "latest_account_note",
            "active_dm_navigation",
            "campaign_task",
        ],
        "pending_operations": [
            "note_comment_live_uat",
            "comment_reply_live_uat",
            "active_dm_live_uat",
        ],
    },
    "review": {
        "implementation_status": "ready",
        "surface_role": "readonly_operation_ledger_alias",
        "available_operations": ["status", "list", "export"],
        "pending_operations": [],
        "legacy_daily_review_public": False,
    },
}


def public_group_names() -> tuple[str, ...]:
    """Return the deliberately small V2 public surface."""

    return tuple(_GROUPS)


def public_command_manifest() -> dict[str, tuple[str, ...]]:
    """Return the frozen command tree used by the public dispatcher."""

    return deepcopy(PUBLIC_COMMANDS)


def public_group_status(group: str) -> dict[str, Any]:
    """Return a copy of one group's status without touching the platform."""

    if group not in _GROUPS:
        raise ValueError(f"unsupported public capability group: {group}")
    status = deepcopy(_GROUPS[group])
    status.update(
        {
            "group": group,
            "live_xhs_provider": LIVE_XHS_PROVIDER,
            "fallback_live_browser": False,
            "platform_actions_executed": 0,
        }
    )
    return status


def public_surface_status() -> dict[str, Any]:
    """Return the complete public capability manifest."""

    return {name: public_group_status(name) for name in public_group_names()}
