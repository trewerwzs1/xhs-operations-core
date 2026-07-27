"""Unified command-line entry point for Codex Desktop tool calls."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from . import __version__
from .config import ConfigError, load_project_config
from .doctor import run_doctor
from .contracts import ActionContractError, ActionRecord
from .paths import ProjectPathError, find_project_root, resolve_project_relative
from .storage import (
    StorageCorruptionError,
    StorageError,
    read_json,
    read_jsonl,
    write_json_atomic,
)
from .campaign import (
    BoundedCampaignUatProposal,
    CAMPAIGN_UAT_PROPOSAL_CONFIRMATION,
    Campaign,
    CampaignContractError,
    CampaignRepository,
    CampaignRepositoryError,
    CampaignUatProposalError,
    CampaignStatus,
    StatusActor,
    validate_campaign,
)
from .contracts import utc_now_iso
from .platform.browser import (
    BrowserConfigError,
    BrowserProfileError,
    BrowserProfileManager,
    check_browser_readiness,
    LoginEvidence,
    load_calibration_status,
    evaluate_login_evidence,
    load_browser_config,
    record_run_agent_login_calibration,
    RUN_AGENT_LOGIN_CONFIRMATION,
)
from .platform.browser.login import (
    BrowserLoginError,
    MANUAL_LOGIN_CONFIRMATION,
    READONLY_CONFIRMATION,
)
from .platform.xhs import (
    LiveInteractionError,
    RunAgentClient,
    RunAgentError,
    RISK_CLASS_PLATFORM,
    RISK_CLASS_TECHNICAL,
    XhsCallMethodLockedError,
    has_explicit_platform_risk,
)
from .storage import append_jsonl
from .unresolved_targets import UnresolvedTargetError
from .interaction import (
    CommentActionStore,
    CommentFlowContractError,
    CommentInteractionPlan,
    ApprovedPlanError,
    MessageApproval,
    build_approved_comment_plan,
    APPROVAL_RECORD_CONFIRMATION,
    MessageApprovalStore,
    NoteCommentError,
    NoteCommentPlan,
    NoteCommentStore,
    CURRENT_PAGE_EXECUTION_CONFIRMATION,
    CURRENT_PAGE_APPROVAL_CONFIRMATION,
    CurrentPageInteractionPlan,
    CompiledCurrentPagePlan,
    CompiledPlanStore,
    NoteCommentApproval,
    NoteLikeApproval,
    InteractionBranch,
    InteractionSessionError,
    InteractionSessionStore,
    adopt_readonly_search_session,
    execute_current_page_plan,
    compile_approved_reply_plan,
    compile_approved_note_comment_plan,
    compile_approved_note_like_plan,
    compile_comment_like_plan,
    prepare_readonly_search_session,
)
from .source_notes import (
    LatestNoteContractError,
    NoteDetailCapture,
    ProfileNoteCard,
    build_latest_note_snapshot,
    select_latest_visible_profile_note,
    build_visible_thread_snapshot_from_dict,
    select_latest_non_pinned,
    StyleHistoryError,
    build_style_history_snapshot,
)
from .discovery import (
    CandidateAssessmentError,
    CandidateEvidence,
    CandidateInteractionPlan,
    DiscoveryPlanError,
    assess_comment_candidate,
    build_discovery_plan,
)
from .messaging import MessagePlanError, build_message_plan
from .orchestration import (
    DailyBudget,
    LoopPlanError,
    PostEngagementError,
    PostEngagementRequest,
    build_daily_plan,
    build_post_engagement_plan,
)
from .orchestration import HeartbeatStateStore
from .style import (
    CORPUS_CONFIRMATION,
    CORPUS_DELETE_CONFIRMATION,
    ReplyCorpusStore,
    PostVoiceStore,
    StyleProfileStore,
    StyleProfileError,
    build_reply_corpus,
    build_reply_style_profile,
    build_reply_style_profile_from_corpus,
)
from .reporting import (
    LeadRecordStore,
    LeadStoreError,
    QueryRunMetrics,
    QueryMetricsStore,
    ReviewError,
    build_daily_review,
)
from .dm import (
    DMContractError, build_dm_conversation_snapshot, build_dm_message_plan,
    DMSingleApproval, build_approved_dm_plan, DMApprovalStore, DMGate, DMRuntimeStore,
    DM_APPROVAL_CONFIRMATION, RunAgentDMPort, execute_single_dm,
)
from .setup import SetupError, initialize_user_project, register_existing_user_project
from .onboarding import (
    AccountSetupProfile,
    AccountSetupStore,
    OnboardingError,
    build_handoff_plan,
    build_setup_status,
    PLATFORM_READ_CONFIRMATION,
)
from .promotion import PromotionIntent, PromotionStrategyError, build_promotion_strategy
from .account_voice import build_account_voice_status
from .operations import (
    BOUNDED_RUN_CONFIRMATION,
    CampaignTask,
    CampaignTaskStore,
    TaskOccurrenceStore,
    TaskContractError,
    authorize_campaign_task,
    build_schedule_manifest,
    evaluate_task_due,
    evaluate_task_execution_authorization,
)
from .public_surface import public_group_status
from .operation_ledger import (
    OperationLedgerError,
    OperationLedgerQuery,
    OperationLedgerStore,
)
from .publishing import (
    PUBLISH_APPROVAL_CONFIRMATION,
    PublishContractError,
    PublishPlan,
    PublishRuntimeStore,
    approve_publish_plan,
    execute_publish_plan,
)
from .action_preflight import RuntimeMode, UnifiedActionPreflightStore, UnifiedPreflightState
from .service import (
    SERVICE_REPLY_CONFIRMATION,
    ServiceContractError,
    ServiceQueueStore,
    build_service_reply_plan,
    scan_service_inbox,
    open_next_service_item,
    approve_service_reply,
    execute_service_reply,
)
from .strategy_pack import (
    STRATEGY_PACK_CONFIRMATION,
    StrategyPackError,
    StrategyPackStore,
    build_strategy_pack,
)
from .engage import (
    EngageContractError,
    authorize_engage_action,
    authorize_dm_action,
    build_dm_action_request,
    require_engage_search_campaign,
    record_dm_result,
    record_engage_result,
    require_dm_execution,
    require_engage_execution,
)


def _add_account_voice_learn_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--project-root", type=Path, default=None)
    command.add_argument("--browser-config", type=Path, default=Path("config/browser.local.json"))
    command.add_argument("--consent-ref", required=True)
    command.add_argument("--start-position", type=int, default=0)
    command.add_argument("--max-notes", type=int, default=10)
    command.add_argument("--max-comments-per-note", type=int, default=200)
    command.add_argument("--captured-at", required=True)
    command.add_argument("--created-at", required=True)
    command.add_argument("--confirm-own-profile", required=True)
    command.add_argument("--confirm-local-corpus", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xhs-operations-core")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate the local project environment")
    doctor.add_argument("--project-root", type=Path, default=None)
    doctor.add_argument("--config", type=Path, default=None)
    doctor.add_argument(
        "--init-runtime",
        action="store_true",
        help="create writable runtime, logs, and report directories",
    )
    doctor.add_argument("--format", choices=("json", "text"), default="text")

    setup = subparsers.add_parser("setup", help="initialize one local Codex Desktop user")
    setup_sub = setup.add_subparsers(dest="setup_command", required=True)
    setup_init = setup_sub.add_parser("init")
    setup_init.add_argument("--project-root", type=Path, default=None)
    setup_init.add_argument("--account-id", required=True)
    setup_init.add_argument("--profile-name", required=True)
    setup_migrate = setup_sub.add_parser("migrate-existing")
    setup_migrate.add_argument("--project-root", type=Path, default=None)
    setup_migrate.add_argument("--account-id", required=True)
    setup_migrate.add_argument("--profile-name", required=True)
    setup_account = setup_sub.add_parser("account-config")
    setup_account.add_argument("--project-root", type=Path, default=None)
    setup_account.add_argument("--file", type=Path, required=True)
    setup_status = setup_sub.add_parser("status")
    setup_status.add_argument("--project-root", type=Path, default=None)
    setup_status.add_argument("--account-id", required=True)
    setup_handoff = setup_sub.add_parser("handoff-plan")
    setup_handoff.add_argument("--project-root", type=Path, default=None)
    setup_handoff.add_argument("--account-id", required=True)
    setup_extension_enroll = setup_sub.add_parser("extension-enroll")
    setup_extension_enroll.add_argument("--project-root", type=Path, default=None)
    setup_extension_enroll.add_argument("--confirm-enrollment", required=True)
    setup_connection_check = setup_sub.add_parser("connection-check")
    setup_connection_check.add_argument("--project-root", type=Path, default=None)
    setup_account_enroll = setup_sub.add_parser("account-enroll")
    setup_account_enroll.add_argument("--project-root", type=Path, default=None)
    setup_account_enroll.add_argument("--confirm-account-enrollment", required=True)
    setup_enable_read = setup_sub.add_parser("enable-platform-read")
    setup_enable_read.add_argument("--project-root", type=Path, default=None)
    setup_enable_read.add_argument("--account-id", required=True)
    setup_enable_read.add_argument("--confirm-platform-read", required=True)
    setup_voice_status = setup_sub.add_parser("voice-status")
    setup_voice_status.add_argument("--project-root", type=Path, default=None)
    setup_voice_status.add_argument("--account-id", required=True)
    setup_voice_learn = setup_sub.add_parser("voice-learn")
    _add_account_voice_learn_arguments(setup_voice_learn)
    setup_uat_preflight = setup_sub.add_parser("uat-preflight")
    setup_uat_preflight.add_argument("--project-root", type=Path, default=None)
    setup_uat_preflight.add_argument("--confirm-readonly-uat", required=True)
    setup_uat_preflight.add_argument("--duration-seconds", type=int, default=1800)
    setup_uat_revoke = setup_sub.add_parser("uat-revoke")
    setup_uat_revoke.add_argument("--project-root", type=Path, default=None)
    setup_uat_revoke.add_argument("--confirm-revoke", required=True)

    for group_name, help_text in (
        ("publish", "publish Xiaohongshu image or video notes"),
        ("service", "serve inbound Xiaohongshu comments and messages"),
        ("engage", "run outbound Xiaohongshu engagement"),
    ):
        group = subparsers.add_parser(group_name, help=help_text)
        group_sub = group.add_subparsers(dest=f"{group_name}_command", required=True)
        group_sub.add_parser("status", help="show truthful implementation status")
        if group_name == "publish":
            publish_preview = group_sub.add_parser("preview")
            publish_preview.add_argument("--project-root", type=Path, default=None)
            publish_preview.add_argument("--file", type=Path, required=True)
            publish_approve = group_sub.add_parser("approve")
            publish_approve.add_argument("--project-root", type=Path, default=None)
            publish_approve.add_argument("--file", type=Path, required=True)
            publish_approve.add_argument("--approved-at", required=True)
            publish_approve.add_argument("--confirm-publish", required=True)
            publish_run = group_sub.add_parser("run")
            publish_run.add_argument("--project-root", type=Path, default=None)
            publish_run.add_argument("--file", type=Path, required=True)
        elif group_name == "service":
            service_scan = group_sub.add_parser("scan")
            service_scan.add_argument("--project-root", type=Path, default=None)
            service_scan.add_argument("--account-id", required=True)
            service_scan.add_argument("--channel", choices=("comments", "dm"), required=True)
            service_scan.add_argument("--captured-at", required=True)
            service_scan.add_argument("--max-items", type=int, default=20)
            service_queue = group_sub.add_parser("queue-status")
            service_queue.add_argument("--project-root", type=Path, default=None)
            service_queue.add_argument("--account-id", required=True)
            service_next = group_sub.add_parser("next")
            service_next.add_argument("--project-root", type=Path, default=None)
            service_next.add_argument("--account-id", required=True)
            service_next.add_argument("--channel", choices=("comments", "dm"), required=True)
            service_next.add_argument("--opened-at", required=True)
            service_next.add_argument("--max-comments", type=int, default=200)
            service_next.add_argument("--max-messages", type=int, default=50)
            service_preview = group_sub.add_parser("preview")
            service_preview.add_argument("--project-root", type=Path, default=None)
            service_preview.add_argument("--item-id", required=True)
            service_preview.add_argument("--file", type=Path, required=True)
            service_preview.add_argument("--checked-at", required=True)
            service_approve = group_sub.add_parser("approve")
            service_approve.add_argument("--project-root", type=Path, default=None)
            service_approve.add_argument("--plan-id", required=True)
            service_approve.add_argument("--approved-at", required=True)
            service_approve.add_argument("--confirm-service-reply", required=True)
            service_approve.add_argument("--daily-limit", type=int, default=10)
            service_approve.add_argument("--minimum-interval-seconds", type=int, default=600)
            service_approve.add_argument("--budget-timezone", default="UTC")
            service_run = group_sub.add_parser("run")
            service_run.add_argument("--project-root", type=Path, default=None)
            service_run.add_argument("--plan-id", required=True)
            service_run.add_argument("--executed-at", required=True)
            service_run.add_argument("--daily-limit", type=int, default=10)
            service_run.add_argument("--minimum-interval-seconds", type=int, default=600)
            service_run.add_argument("--budget-timezone", default="UTC")
        elif group_name == "engage":
            latest_account_note = group_sub.add_parser("latest-account-note")
            latest_account_note.add_argument("--project-root", type=Path, default=None)
            latest_account_note.add_argument("--created-at", required=True)
            latest_account_note.add_argument("--max-notes", type=int, default=10)
            latest_account_note.add_argument("--user-keyword", action="append", default=[])
            latest_account_note.add_argument("--exclusion", action="append", default=[])
            strategy_preview = group_sub.add_parser("strategy-preview")
            strategy_preview.add_argument("--project-root", type=Path, default=None)
            strategy_preview.add_argument("--account-id", required=True)
            strategy_preview.add_argument("--file", type=Path, required=True)
            strategy_show = group_sub.add_parser("strategy-show")
            strategy_show.add_argument("--project-root", type=Path, default=None)
            strategy_show.add_argument("--strategy-pack-id", required=True)
            strategy_confirm = group_sub.add_parser("strategy-confirm")
            strategy_confirm.add_argument("--project-root", type=Path, default=None)
            strategy_confirm.add_argument("--strategy-pack-id", required=True)
            strategy_confirm.add_argument("--confirmed-at", required=True)
            strategy_confirm.add_argument("--confirm-strategy", required=True)
            campaign_confirm = group_sub.add_parser("campaign-confirm")
            campaign_confirm.add_argument("--project-root", type=Path, default=None)
            campaign_confirm.add_argument("--file", type=Path, required=True)
            campaign_confirm.add_argument("--proposal-id", required=True)
            campaign_confirm.add_argument("--proposal-hash", required=True)
            campaign_confirm.add_argument(
                "--confirm-campaign-uat-proposal",
                required=True,
            )
            engage_start = group_sub.add_parser("search-start")
            engage_start.add_argument("--project-root", type=Path, default=None)
            engage_start.add_argument(
                "--browser-config", type=Path, default=Path("config/browser.local.json")
            )
            engage_start.add_argument("--strategy-pack-id", required=True)
            engage_start.add_argument("--query-id", required=True)
            engage_start.add_argument("--session-id", required=True)
            engage_start.add_argument("--campaign-id", required=True)
            engage_start.add_argument("--run-id", required=True)
            engage_next = group_sub.add_parser("next")
            engage_next.add_argument("--project-root", type=Path, default=None)
            engage_next.add_argument("--session-id", required=True)
            engage_next.add_argument("--max-comments", type=int, default=30)
            engage_continue = group_sub.add_parser("continue")
            engage_continue.add_argument("--project-root", type=Path, default=None)
            engage_continue.add_argument("--session-id", required=True)
            engage_continue.add_argument("--max-comments", type=int, default=30)
            engage_return = group_sub.add_parser("return")
            engage_return.add_argument("--project-root", type=Path, default=None)
            engage_return.add_argument("--session-id", required=True)
            engage_note_like = group_sub.add_parser("note-like-preview")
            engage_note_like.add_argument("--project-root", type=Path, default=None)
            engage_note_like.add_argument("--campaign", type=Path, required=True)
            engage_note_like.add_argument("--note", type=Path, required=True)
            engage_note_like.add_argument("--approval", type=Path, required=True)
            engage_note_like.add_argument("--session-id", required=True)
            engage_note_like.add_argument("--output", type=Path, required=True)
            engage_note_comment = group_sub.add_parser("note-comment-preview")
            engage_note_comment.add_argument("--project-root", type=Path, default=None)
            engage_note_comment.add_argument("--campaign", type=Path, required=True)
            engage_note_comment.add_argument("--note", type=Path, required=True)
            engage_note_comment.add_argument("--plan", type=Path, required=True)
            engage_note_comment.add_argument("--approval", type=Path, required=True)
            engage_note_comment.add_argument("--session-id", required=True)
            engage_note_comment.add_argument("--style-exception-ref", default="")
            engage_note_comment.add_argument("--output", type=Path, required=True)
            engage_comment_like = group_sub.add_parser("comment-like-preview")
            engage_comment_like.add_argument("--project-root", type=Path, default=None)
            engage_comment_like.add_argument("--campaign", type=Path, required=True)
            engage_comment_like.add_argument("--candidate", type=Path, required=True)
            engage_comment_like.add_argument(
                "--post-engagement-request", type=Path, required=True
            )
            engage_comment_like.add_argument("--session-id", required=True)
            engage_comment_like.add_argument("--output", type=Path, required=True)
            engage_comment_reply = group_sub.add_parser("comment-reply-preview")
            engage_comment_reply.add_argument("--project-root", type=Path, default=None)
            engage_comment_reply.add_argument("--campaign", type=Path, required=True)
            engage_comment_reply.add_argument("--candidate", type=Path, required=True)
            engage_comment_reply.add_argument("--draft", type=Path, required=True)
            engage_comment_reply.add_argument("--approval", type=Path, required=True)
            engage_comment_reply.add_argument("--result-index", type=int, required=True)
            engage_comment_reply.add_argument("--session-id", required=True)
            engage_comment_reply.add_argument("--promotion-file", type=Path, default=None)
            engage_comment_reply.add_argument("--style-exception-ref", default="")
            engage_comment_reply.add_argument("--run-id", default="")
            engage_comment_reply.add_argument("--output", type=Path, required=True)
            engage_comment_like_precheck = group_sub.add_parser("comment-like-precheck")
            engage_comment_like_precheck.add_argument("--project-root", type=Path, default=None)
            engage_comment_like_precheck.add_argument("--note-id", required=True)
            engage_comment_like_precheck.add_argument("--comment-id", required=True)
            engage_reconcile = group_sub.add_parser("reconcile-unknown-write")
            engage_reconcile.add_argument("--project-root", type=Path, default=None)
            engage_reconcile.add_argument("--attempt-id", required=True)
            engage_reconcile.add_argument(
                "--observed-outcome",
                choices=("verified_present", "verified_absent"),
                required=True,
            )
            engage_reconcile.add_argument("--evidence-ref", required=True)
            engage_reconcile.add_argument("--reconciled-at", required=True)
            engage_reconcile.add_argument("--note-id", required=True)
            engage_reconcile.add_argument("--confirm-reconciliation", required=True)
            engage_action = group_sub.add_parser("action-preview")
            engage_action.add_argument("--project-root", type=Path, default=None)
            engage_action.add_argument("--file", type=Path, required=True)
            engage_approve = group_sub.add_parser("approve")
            engage_approve.add_argument("--project-root", type=Path, default=None)
            engage_approve.add_argument("--file", type=Path, required=True)
            engage_approve.add_argument("--confirm-approval", required=True)
            engage_run = group_sub.add_parser("run")
            engage_run.add_argument("--project-root", type=Path, default=None)
            engage_run.add_argument("--file", type=Path, required=True)
            engage_run.add_argument(
                "--browser-config", type=Path, default=Path("config/browser.local.json")
            )
            engage_run.add_argument("--daily-action-limit", type=int, default=10)
            engage_run.add_argument("--minimum-target-interval-seconds", type=int, default=600)
            engage_run.add_argument("--confirm-current-page-interaction", required=True)
            engage_run.add_argument("--confirm-bounded-write-uat", required=True)
            engage_run.add_argument("--task-id", default=None)
            dm_open_profile = group_sub.add_parser("dm-open-profile")
            dm_open_profile.add_argument("--project-root", type=Path, default=None)
            dm_open_profile.add_argument("--feed-id", required=True)
            dm_open_profile.add_argument("--comment-id", required=True)
            dm_open_profile.add_argument("--target-context-hash", required=True)
            dm_open_conversation = group_sub.add_parser("dm-open-conversation")
            dm_open_conversation.add_argument("--project-root", type=Path, default=None)
            dm_open_conversation.add_argument("--expected-peer-ref-hash", required=True)
            dm_capture = group_sub.add_parser("dm-capture")
            dm_capture.add_argument("--project-root", type=Path, default=None)
            dm_capture.add_argument("--account-id", required=True)
            dm_capture.add_argument("--conversation-id", required=True)
            dm_capture.add_argument("--expected-peer-ref-hash", required=True)
            dm_capture.add_argument("--captured-at", required=True)
            dm_capture.add_argument("--max-messages", type=int, default=50)
            dm_return_source = group_sub.add_parser("dm-return-source")
            dm_return_source.add_argument("--project-root", type=Path, default=None)
            dm_return_source.add_argument("--feed-id", required=True)
            dm_return_source.add_argument("--comment-id", required=True)
            dm_return_source.add_argument("--target-context-hash", required=True)
            for command_name in ("dm-preview", "dm-approve", "dm-ready", "dm-run"):
                command = group_sub.add_parser(command_name)
                command.add_argument("--project-root", type=Path, default=None)
                command.add_argument("--campaign", type=Path, required=True)
                command.add_argument("--conversation", type=Path, required=True)
                command.add_argument("--draft", type=Path, required=True)
                command.add_argument("--captured-at", required=True)
                if command_name != "dm-preview":
                    command.add_argument("--approval", type=Path, required=True)
                if command_name == "dm-approve":
                    command.add_argument("--confirm-approval", required=True)
                if command_name in {"dm-ready", "dm-run"}:
                    command.add_argument(
                        "--browser-config",
                        type=Path,
                        default=Path("config/browser.local.json"),
                    )
                    command.add_argument("--expected-peer-ref-hash", required=True)
                    command.add_argument("--daily-dm-limit", type=int, default=2)
                    command.add_argument(
                        "--minimum-target-interval-seconds", type=int, default=600
                    )
                if command_name == "dm-run":
                    command.add_argument("--confirm-single-dm", required=True)
                    command.add_argument("--confirm-bounded-write-uat", required=True)
            task_create_public = group_sub.add_parser("task-create")
            task_create_public.add_argument("--project-root", type=Path, default=None)
            task_create_public.add_argument("--file", type=Path, required=True)
            task_authorize_public = group_sub.add_parser("task-authorize")
            task_authorize_public.add_argument("--project-root", type=Path, default=None)
            task_authorize_public.add_argument("--task-id", required=True)
            task_authorize_public.add_argument("--confirmed-at", required=True)
            task_authorize_public.add_argument("--confirm-bounded-run", required=True)
            task_status_public = group_sub.add_parser("task-status")
            task_status_public.add_argument("--project-root", type=Path, default=None)
            task_status_public.add_argument("--task-id", required=True)
            task_transition_public = group_sub.add_parser("task-transition")
            task_transition_public.add_argument("--project-root", type=Path, default=None)
            task_transition_public.add_argument("--task-id", required=True)
            task_transition_public.add_argument(
                "--to", choices=("running", "paused", "completed", "cancelled"), required=True
            )
            task_transition_public.add_argument("--changed-at", required=True)
            task_schedule_public = group_sub.add_parser("task-schedule")
            task_schedule_public.add_argument("--project-root", type=Path, default=None)
            task_schedule_public.add_argument("--task-id", required=True)
            task_due_public = group_sub.add_parser("task-due")
            task_due_public.add_argument("--project-root", type=Path, default=None)
            task_due_public.add_argument("--task-id", required=True)
            task_due_public.add_argument("--at", required=True)
            task_claim_public = group_sub.add_parser("task-claim")
            task_claim_public.add_argument("--project-root", type=Path, default=None)
            task_claim_public.add_argument("--task-id", required=True)
            task_claim_public.add_argument(
                "--kind", choices=("daily_plan", "heartbeat", "daily_review"), required=True
            )
            task_claim_public.add_argument("--at", required=True)
            task_claim_public.add_argument("--worker-id", required=True)
            task_complete_public = group_sub.add_parser("task-complete")
            task_complete_public.add_argument("--project-root", type=Path, default=None)
            task_complete_public.add_argument("--task-id", required=True)
            task_complete_public.add_argument("--occurrence-id", required=True)
            task_complete_public.add_argument("--lease-token", required=True)
            task_complete_public.add_argument("--completed-at", required=True)
            task_complete_public.add_argument(
                "--outcome", choices=("completed", "noop", "blocked"), required=True
            )
            task_plan_approve_public = group_sub.add_parser("task-plan-approve")
            task_plan_approve_public.add_argument("--project-root", type=Path, default=None)
            task_plan_approve_public.add_argument("--file", type=Path, required=True)
            task_plan_approve_public.add_argument("--task-id", required=True)
            task_plan_approve_public.add_argument("--at", required=True)

    run_agent = subparsers.add_parser("run-agent", help="pinned Ranfang Run Agent vendor boundary")
    run_agent_sub = run_agent.add_subparsers(dest="run_agent_command", required=True)
    run_agent_status = run_agent_sub.add_parser("status")
    run_agent_status.add_argument("--project-root", type=Path, default=None)
    run_agent_setup = run_agent_sub.add_parser("setup-guide")
    run_agent_setup.add_argument("--project-root", type=Path, default=None)
    run_agent_connection = run_agent_sub.add_parser("connection-check")
    run_agent_connection.add_argument("--project-root", type=Path, default=None)
    run_agent_enroll = run_agent_sub.add_parser("enroll-extension-instance")
    run_agent_enroll.add_argument("--project-root", type=Path, default=None)
    run_agent_enroll.add_argument("--confirm-enrollment", required=True)
    run_agent_enroll_account = run_agent_sub.add_parser("enroll-platform-account")
    run_agent_enroll_account.add_argument("--project-root", type=Path, default=None)
    run_agent_enroll_account.add_argument("--confirm-account-enrollment", required=True)
    run_agent_read_status = run_agent_sub.add_parser("readonly-uat-status")
    run_agent_read_status.add_argument("--project-root", type=Path, default=None)
    run_agent_page_context = run_agent_sub.add_parser("readonly-page-context")
    run_agent_page_context.add_argument("--project-root", type=Path, default=None)
    run_agent_bind_tab = run_agent_sub.add_parser("readonly-bind-active-tab")
    run_agent_bind_tab.add_argument("--project-root", type=Path, default=None)
    run_agent_list_tabs = run_agent_sub.add_parser("readonly-list-tabs")
    run_agent_list_tabs.add_argument("--project-root", type=Path, default=None)
    run_agent_inspect_like = run_agent_sub.add_parser("inspect-current-like-control")
    run_agent_inspect_like.add_argument("--project-root", type=Path, default=None)
    run_agent_inspect_like.add_argument("--note-id", required=True)
    run_agent_inspect_comments = run_agent_sub.add_parser("inspect-current-comment-controls")
    run_agent_inspect_comments.add_argument("--project-root", type=Path, default=None)
    run_agent_inspect_comments.add_argument("--note-id", required=True)
    run_agent_inspect_comments.add_argument("--comment-id", default="")
    run_agent_current_detail = run_agent_sub.add_parser("readonly-current-feed-detail")
    run_agent_current_detail.add_argument("--project-root", type=Path, default=None)
    run_agent_current_detail.add_argument("--note-id", required=True)
    run_agent_current_detail.add_argument("--max-comments", type=int, default=200)
    run_agent_read_authorize = run_agent_sub.add_parser("readonly-uat-authorize")
    run_agent_read_authorize.add_argument("--project-root", type=Path, default=None)
    run_agent_read_authorize.add_argument("--confirm-readonly-uat", required=True)
    run_agent_read_authorize.add_argument("--confirm-risk-override", default="")
    run_agent_read_authorize.add_argument("--duration-seconds", type=int, default=3600)
    run_agent_read_revoke = run_agent_sub.add_parser("readonly-uat-revoke")
    run_agent_read_revoke.add_argument("--project-root", type=Path, default=None)
    run_agent_read_revoke.add_argument("--confirm-revoke", required=True)
    run_agent_preflight = run_agent_sub.add_parser("readonly-uat-preflight")
    run_agent_preflight.add_argument("--project-root", type=Path, default=None)
    run_agent_preflight.add_argument("--confirm-readonly-uat", required=True)
    run_agent_preflight.add_argument("--confirm-risk-override", default="")
    run_agent_preflight.add_argument("--duration-seconds", type=int, default=1800)
    run_agent_latest_note = run_agent_sub.add_parser("latest-account-note")
    run_agent_latest_note.add_argument("--project-root", type=Path, default=None)
    run_agent_latest_note.add_argument("--created-at", required=True)
    run_agent_latest_note.add_argument("--max-notes", type=int, default=10)
    run_agent_latest_note.add_argument("--user-keyword", action="append", default=[])
    run_agent_latest_note.add_argument("--exclusion", action="append", default=[])
    run_agent_open_own = run_agent_sub.add_parser("open-own-profile")
    run_agent_open_own.add_argument("--project-root", type=Path, default=None)
    run_agent_open_commenter = run_agent_sub.add_parser("open-commenter-profile")
    run_agent_open_commenter.add_argument("--project-root", type=Path, default=None)
    run_agent_open_commenter.add_argument("--feed-id", required=True)
    run_agent_open_commenter.add_argument("--comment-id", required=True)
    run_agent_open_commenter.add_argument("--target-context-hash", required=True)
    run_agent_return_comment = run_agent_sub.add_parser("return-to-source-comment")
    run_agent_return_comment.add_argument("--project-root", type=Path, default=None)
    run_agent_return_comment.add_argument("--feed-id", required=True)
    run_agent_return_comment.add_argument("--comment-id", required=True)
    run_agent_return_comment.add_argument("--target-context-hash", required=True)
    run_agent_open_dm = run_agent_sub.add_parser("open-dm-conversation")
    run_agent_open_dm.add_argument("--project-root", type=Path, default=None)
    run_agent_open_dm.add_argument("--expected-peer-ref-hash", required=True)
    run_agent_capture_dm = run_agent_sub.add_parser("capture-current-dm-conversation")
    run_agent_capture_dm.add_argument("--project-root", type=Path, default=None)
    run_agent_capture_dm.add_argument("--account-id", required=True)
    run_agent_capture_dm.add_argument("--conversation-id", required=True)
    run_agent_capture_dm.add_argument("--expected-peer-ref-hash", required=True)
    run_agent_capture_dm.add_argument("--captured-at", required=True)
    run_agent_capture_dm.add_argument("--max-messages", type=int, default=50)
    run_agent_reconcile = run_agent_sub.add_parser("reconcile-unknown-write")
    run_agent_reconcile.add_argument("--project-root", type=Path, default=None)
    run_agent_reconcile.add_argument("--attempt-id", required=True)
    run_agent_reconcile.add_argument(
        "--observed-outcome",
        choices=("verified_present", "verified_absent"),
        required=True,
    )
    run_agent_reconcile.add_argument("--evidence-ref", required=True)
    run_agent_reconcile.add_argument("--reconciled-at", required=True)
    run_agent_reconcile.add_argument("--note-id", default="")
    run_agent_reconcile.add_argument("--confirm-reconciliation", required=True)

    promotion = subparsers.add_parser("promotion", help="normalize promotion input and validate strategy")
    promotion_sub = promotion.add_subparsers(dest="promotion_command", required=True)
    for name in ("intent-preview", "strategy-preview"):
        command = promotion_sub.add_parser(name)
        command.add_argument("--project-root", type=Path, default=None)
        command.add_argument("--file", type=Path, required=True)

    validate_action = subparsers.add_parser(
        "validate-action-record", help="validate one project-local ActionRecord JSON file"
    )
    validate_action.add_argument("--project-root", type=Path, default=None)
    validate_action.add_argument("--file", type=Path, required=True)

    campaign = subparsers.add_parser("campaign", help="manage local Campaign state")
    campaign_sub = campaign.add_subparsers(dest="campaign_command", required=True)
    for name in ("validate", "create"):
        command = campaign_sub.add_parser(name)
        command.add_argument("--project-root", type=Path, default=None)
        command.add_argument("--file", type=Path, required=True)
        command.add_argument("--checked-at", default=None)
        command.add_argument("--minimum-confidence", type=float, default=0.75)
    show = campaign_sub.add_parser("show")
    show.add_argument("--project-root", type=Path, default=None)
    show.add_argument("--id", required=True)
    list_command = campaign_sub.add_parser("list")
    list_command.add_argument("--project-root", type=Path, default=None)
    transition = campaign_sub.add_parser("transition")
    transition.add_argument("--project-root", type=Path, default=None)
    transition.add_argument("--id", required=True)
    transition.add_argument("--to", choices=[item.value for item in CampaignStatus], required=True)
    transition.add_argument("--actor", choices=[item.value for item in StatusActor], required=True)
    transition.add_argument("--reason", required=True)
    transition.add_argument("--changed-at", default=None)
    transition.add_argument("--minimum-confidence", type=float, default=0.75)

    task = subparsers.add_parser("task", help="manage bounded multi-day promotion tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_create = task_sub.add_parser("create")
    task_create.add_argument("--project-root", type=Path, default=None)
    task_create.add_argument("--file", type=Path, required=True)
    task_show = task_sub.add_parser("show")
    task_show.add_argument("--project-root", type=Path, default=None)
    task_show.add_argument("--task-id", required=True)
    task_authorize = task_sub.add_parser("authorize")
    task_authorize.add_argument("--project-root", type=Path, default=None)
    task_authorize.add_argument("--task-id", required=True)
    task_authorize.add_argument("--confirmed-at", required=True)
    task_authorize.add_argument("--confirm-bounded-run", required=True)
    task_transition = task_sub.add_parser("transition")
    task_transition.add_argument("--project-root", type=Path, default=None)
    task_transition.add_argument("--task-id", required=True)
    task_transition.add_argument("--to", choices=("running", "paused", "completed", "cancelled"), required=True)
    task_transition.add_argument("--changed-at", required=True)
    task_schedule = task_sub.add_parser("schedule-preview")
    task_schedule.add_argument("--project-root", type=Path, default=None)
    task_schedule.add_argument("--task-id", required=True)
    task_due = task_sub.add_parser("due-status")
    task_due.add_argument("--project-root", type=Path, default=None)
    task_due.add_argument("--task-id", required=True)
    task_due.add_argument("--at", required=True)
    task_occurrence_claim = task_sub.add_parser("occurrence-claim")
    task_occurrence_claim.add_argument("--project-root", type=Path, default=None)
    task_occurrence_claim.add_argument("--task-id", required=True)
    task_occurrence_claim.add_argument(
        "--kind", choices=("daily_plan", "heartbeat", "daily_review"), required=True
    )
    task_occurrence_claim.add_argument("--at", required=True)
    task_occurrence_claim.add_argument("--worker-id", required=True)
    task_occurrence_complete = task_sub.add_parser("occurrence-complete")
    task_occurrence_complete.add_argument("--project-root", type=Path, default=None)
    task_occurrence_complete.add_argument("--task-id", required=True)
    task_occurrence_complete.add_argument("--occurrence-id", required=True)
    task_occurrence_complete.add_argument("--lease-token", required=True)
    task_occurrence_complete.add_argument("--completed-at", required=True)
    task_occurrence_complete.add_argument(
        "--outcome", choices=("completed", "noop", "blocked"), required=True
    )

    browser = subparsers.add_parser("browser", help="dedicated browser profile tools")
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)
    for name in ("validate-config", "profile-init", "readiness"):
        command = browser_sub.add_parser(name)
        command.add_argument("--project-root", type=Path, default=None)
        command.add_argument("--config", type=Path, required=True)
    evaluate = browser_sub.add_parser("evaluate-login")
    evaluate.add_argument("--project-root", type=Path, default=None)
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--evidence", type=Path, required=True)
    login_check = browser_sub.add_parser("login-check")
    login_check.add_argument("--project-root", type=Path, default=None)
    login_check.add_argument("--config", type=Path, required=True)
    login_check.add_argument("--confirm-readonly", required=True)
    login_authorize = browser_sub.add_parser("login-authorize")
    login_authorize.add_argument("--project-root", type=Path, default=None)
    login_authorize.add_argument("--config", type=Path, required=True)
    login_authorize.add_argument("--confirm-manual-login", required=True)
    profile_probe = browser_sub.add_parser("profile-probe")
    profile_probe.add_argument("--project-root", type=Path, default=None)
    profile_probe.add_argument("--config", type=Path, required=True)
    profile_probe.add_argument("--confirm-profile-readonly", required=True)
    latest_readonly = browser_sub.add_parser("latest-readonly")
    latest_readonly.add_argument("--project-root", type=Path, default=None)
    latest_readonly.add_argument("--config", type=Path, required=True)
    latest_readonly.add_argument("--profile-probe-run-id", required=True)
    latest_readonly.add_argument("--confirm-profile-layout", required=True)
    candidate_readonly = browser_sub.add_parser("candidate-readonly")
    candidate_readonly.add_argument("--project-root", type=Path, default=None)
    candidate_readonly.add_argument("--config", type=Path, required=True)
    candidate_readonly.add_argument("--query", required=True)
    candidate_readonly.add_argument("--result-index", type=int, default=0)
    candidate_readonly.add_argument("--max-comments", type=int, default=20)
    candidate_readonly.add_argument("--confirm-candidate-readonly", required=True)
    candidate_sequence = browser_sub.add_parser("candidate-sequence-readonly")
    candidate_sequence.add_argument("--project-root", type=Path, default=None)
    candidate_sequence.add_argument("--config", type=Path, required=True)
    candidate_sequence.add_argument("--query", required=True)
    candidate_sequence.add_argument("--start-index", type=int, default=0)
    candidate_sequence.add_argument("--max-candidates", type=int, default=3)
    candidate_sequence.add_argument("--max-comments", type=int, default=20)
    candidate_sequence.add_argument(
        "--selection-mode",
        choices=("comment_intent", "adjacent_interest"),
        default="comment_intent",
    )
    candidate_sequence.add_argument("--confirm-candidate-readonly", required=True)

    source = subparsers.add_parser("source", help="build immutable source snapshots")
    source_sub = source.add_subparsers(dest="source_command", required=True)
    latest_preview = source_sub.add_parser("latest-preview")
    latest_preview.add_argument("--project-root", type=Path, default=None)
    latest_preview.add_argument("--file", type=Path, required=True)
    thread_preview = source_sub.add_parser("thread-preview")
    thread_preview.add_argument("--project-root", type=Path, default=None)
    thread_preview.add_argument("--file", type=Path, required=True)

    discovery = subparsers.add_parser("discovery", help="compile audience and query plans")
    discovery_sub = discovery.add_subparsers(dest="discovery_command", required=True)
    discovery_plan = discovery_sub.add_parser("plan")
    discovery_plan.add_argument("--project-root", type=Path, default=None)
    discovery_plan.add_argument("--file", type=Path, required=True)
    discovery_plan.add_argument("--checked-at", required=True)
    discovery_plan.add_argument("--promotion-file", type=Path, default=None)
    candidate_preview = discovery_sub.add_parser("candidate-preview")
    candidate_preview.add_argument("--project-root", type=Path, default=None)
    candidate_preview.add_argument("--campaign", type=Path, required=True)
    candidate_preview.add_argument("--thread", type=Path, required=True)
    candidate_preview.add_argument("--file", type=Path, required=True)
    candidate_preview.add_argument("--promotion-file", type=Path, default=None)

    message = subparsers.add_parser("message", help="validate a Codex-authored reply plan")
    message_sub = message.add_subparsers(dest="message_command", required=True)
    message_preview = message_sub.add_parser("plan-preview")
    message_preview.add_argument("--project-root", type=Path, default=None)
    message_preview.add_argument("--campaign", type=Path, required=True)
    message_preview.add_argument("--candidate", type=Path, required=True)
    message_preview.add_argument("--draft", type=Path, required=True)
    message_preview.add_argument("--style-history-capture", type=Path, default=None)
    message_preview.add_argument("--style-consent-ref", default=None)
    message_preview.add_argument("--style-captured-at", default=None)
    message_preview.add_argument("--style-profile-created-at", default=None)
    message_preview.add_argument("--run-id", default="")

    loop = subparsers.add_parser("loop", help="compile offline daily search and interaction queues")
    loop_sub = loop.add_subparsers(dest="loop_command", required=True)
    post_preview = loop_sub.add_parser("post-preview")
    post_preview.add_argument("--project-root", type=Path, default=None)
    post_preview.add_argument("--file", type=Path, required=True)
    post_record = loop_sub.add_parser("post-record")
    post_record.add_argument("--project-root", type=Path, default=None)
    post_record.add_argument("--file", type=Path, required=True)
    loop_preview = loop_sub.add_parser("daily-preview")
    loop_preview.add_argument("--project-root", type=Path, default=None)
    loop_preview.add_argument("--campaign", type=Path, required=True)
    loop_preview.add_argument("--file", type=Path, required=True)
    loop_preview.add_argument("--promotion-file", type=Path, default=None)
    heartbeat_init = loop_sub.add_parser("heartbeat-init")
    heartbeat_init.add_argument("--project-root", type=Path, default=None)
    heartbeat_init.add_argument("--campaign", type=Path, required=True)
    heartbeat_init.add_argument("--file", type=Path, required=True)
    heartbeat_init.add_argument("--promotion-file", type=Path, default=None)
    heartbeat_claim = loop_sub.add_parser("heartbeat-claim")
    heartbeat_claim.add_argument("--project-root", type=Path, default=None)
    heartbeat_claim.add_argument("--campaign", type=Path, required=True)
    heartbeat_claim.add_argument("--file", type=Path, required=True)
    heartbeat_claim.add_argument("--promotion-file", type=Path, default=None)
    heartbeat_claim.add_argument("--now", required=True)
    heartbeat_claim.add_argument("--worker-id", required=True)
    heartbeat_claim.add_argument("--approved-plan", type=Path, action="append", default=[])
    heartbeat_complete = loop_sub.add_parser("heartbeat-complete")
    heartbeat_complete.add_argument("--project-root", type=Path, default=None)
    heartbeat_complete.add_argument("--campaign", type=Path, required=True)
    heartbeat_complete.add_argument("--file", type=Path, required=True)
    heartbeat_complete.add_argument("--promotion-file", type=Path, default=None)
    heartbeat_complete.add_argument("--item-id", required=True)
    heartbeat_complete.add_argument("--lease-token", required=True)
    heartbeat_complete.add_argument("--outcome", required=True)
    heartbeat_complete.add_argument("--completed-at", required=True)
    heartbeat_complete.add_argument("--blocker", action="append", default=[])

    style = subparsers.add_parser("style", help="account-specific reply-style setup tools")
    style_sub = style.add_subparsers(dest="style_command", required=True)
    style_setup = style_sub.add_parser("setup-preview")
    style_setup.add_argument("--project-root", type=Path, default=None)
    style_setup.add_argument("--file", type=Path, required=True)
    style_setup.add_argument("--account-id", required=True)
    style_setup.add_argument("--consent-ref", required=True)
    style_setup.add_argument("--captured-at", required=True)
    style_setup.add_argument("--max-pages", type=int, default=5)
    style_setup.add_argument("--max-notes", type=int, default=30)
    style_setup.add_argument("--max-comments-per-note", type=int, default=100)
    style_profile = style_sub.add_parser("profile-preview")
    style_profile.add_argument("--project-root", type=Path, default=None)
    style_profile.add_argument("--file", type=Path, required=True)
    style_profile.add_argument("--account-id", required=True)
    style_profile.add_argument("--consent-ref", required=True)
    style_profile.add_argument("--captured-at", required=True)
    style_profile.add_argument("--created-at", required=True)
    style_profile_build = style_sub.add_parser("profile-build")
    style_profile_build.add_argument("--project-root", type=Path, default=None)
    style_profile_build.add_argument("--file", type=Path, required=True)
    style_profile_build.add_argument("--account-id", required=True)
    style_profile_build.add_argument("--consent-ref", required=True)
    style_profile_build.add_argument("--captured-at", required=True)
    style_profile_build.add_argument("--created-at", required=True)
    style_corpus_build = style_sub.add_parser("corpus-build")
    style_corpus_build.add_argument("--project-root", type=Path, default=None)
    style_corpus_build.add_argument("--file", type=Path, required=True)
    style_corpus_build.add_argument("--account-id", required=True)
    style_corpus_build.add_argument("--consent-ref", required=True)
    style_corpus_build.add_argument("--captured-at", required=True)
    style_corpus_build.add_argument("--created-at", required=True)
    style_corpus_build.add_argument("--confirm-local-corpus", required=True)
    style_corpus_status = style_sub.add_parser("corpus-status")
    style_corpus_status.add_argument("--project-root", type=Path, default=None)
    style_corpus_status.add_argument("--account-id", required=True)
    style_corpus_search = style_sub.add_parser("corpus-search")
    style_corpus_search.add_argument("--project-root", type=Path, default=None)
    style_corpus_search.add_argument("--account-id", required=True)
    style_corpus_search.add_argument("--query", required=True)
    style_corpus_search.add_argument("--limit", type=int, default=5)
    style_corpus_delete = style_sub.add_parser("corpus-delete")
    style_corpus_delete.add_argument("--project-root", type=Path, default=None)
    style_corpus_delete.add_argument("--account-id", required=True)
    style_corpus_delete.add_argument("--confirm-delete", required=True)
    style_learn = style_sub.add_parser("learn-from-account")
    _add_account_voice_learn_arguments(style_learn)

    review = subparsers.add_parser("review", help="read OperationLedger records")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_status = review_sub.add_parser("status", help="show read-only ledger status")
    review_status.add_argument("--project-root", type=Path, default=None)
    review_status.add_argument("--account-id", default="")
    for command_name in ("list", "export"):
        command = review_sub.add_parser(command_name)
        command.add_argument("--project-root", type=Path, default=None)
        command.add_argument("--account-id", default="")
        command.add_argument("--workflow", choices=("setup", "publish", "service", "engage"), default="")
        command.add_argument("--status", choices=("verified", "not_dispatched", "unknown"), default="")
        command.add_argument("--since", default="")
        command.add_argument("--until", default="")
        command.add_argument("--limit", type=int, default=100)
        if command_name == "export":
            command.add_argument("--output", type=Path, default=None)
    review_daily = review_sub.add_parser("daily-preview")
    review_daily.add_argument("--project-root", type=Path, default=None)
    review_daily.add_argument("--campaign", type=Path, required=True)
    review_daily.add_argument("--file", type=Path, required=True)
    review_daily.add_argument("--promotion-file", type=Path, default=None)
    review_daily.add_argument(
        "--metrics",
        type=Path,
        default=None,
        help="optional external metrics; default aggregates the local evidence journal",
    )
    review_daily.add_argument("--checked-at", required=True)

    dm = subparsers.add_parser("dm", help="privacy-preserving DM tools")
    dm_sub = dm.add_subparsers(dest="dm_command", required=True)
    dm_conversation = dm_sub.add_parser("conversation-preview")
    dm_conversation.add_argument("--project-root", type=Path, default=None)
    dm_conversation.add_argument("--file", type=Path, required=True)
    dm_conversation.add_argument("--account-id", required=True)
    dm_conversation.add_argument("--captured-at", required=True)
    for command_name in ("message-preview", "approved-plan-preview", "approval-record", "smoke-ready", "smoke-run"):
        command = dm_sub.add_parser(command_name)
        command.add_argument("--project-root", type=Path, default=None)
        command.add_argument("--campaign", type=Path, required=True)
        command.add_argument("--conversation", type=Path, required=True)
        command.add_argument("--draft", type=Path, required=True)
        command.add_argument("--captured-at", required=True)
        if command_name != "message-preview":
            command.add_argument("--approval", type=Path, required=True)
        if command_name == "approval-record":
            command.add_argument("--confirm-approval", required=True)
        if command_name in {"smoke-ready", "smoke-run"}:
            command.add_argument("--browser-config", type=Path, required=True)
            command.add_argument("--expected-peer-ref-hash", required=True)
            command.add_argument("--daily-dm-limit", type=int, default=2)
            command.add_argument("--minimum-target-interval-seconds", type=int, default=600)
        if command_name == "smoke-run":
            command.add_argument("--confirm-single-dm", required=True)
            command.add_argument("--confirm-bounded-write-uat", default="")

    interaction = subparsers.add_parser(
        "interaction", help="single-target interaction preview and readiness"
    )
    interaction_sub = interaction.add_subparsers(
        dest="interaction_command", required=True
    )
    approved_preview = interaction_sub.add_parser("approved-plan-preview")
    approved_preview.add_argument("--project-root", type=Path, default=None)
    approved_preview.add_argument("--campaign", type=Path, required=True)
    approved_preview.add_argument("--candidate", type=Path, required=True)
    approved_preview.add_argument("--draft", type=Path, required=True)
    approved_preview.add_argument("--approval", type=Path, required=True)
    approved_preview.add_argument("--result-index", type=int, required=True)
    approved_preview.add_argument("--promotion-file", type=Path, default=None)
    compile_reply = interaction_sub.add_parser("session-compile-reply-plan")
    compile_reply.add_argument("--project-root", type=Path, default=None)
    compile_reply.add_argument("--campaign", type=Path, required=True)
    compile_reply.add_argument("--candidate", type=Path, required=True)
    compile_reply.add_argument("--draft", type=Path, required=True)
    compile_reply.add_argument("--approval", type=Path, required=True)
    compile_reply.add_argument("--result-index", type=int, required=True)
    compile_reply.add_argument("--session-id", required=True)
    compile_reply.add_argument("--promotion-file", type=Path, default=None)
    compile_reply.add_argument("--style-exception-ref", default="")
    compile_reply.add_argument("--run-id", default="")
    compile_reply.add_argument("--output", type=Path, default=None)
    compile_note = interaction_sub.add_parser("session-compile-note-comment")
    compile_note.add_argument("--project-root", type=Path, default=None)
    compile_note.add_argument("--campaign", type=Path, required=True)
    compile_note.add_argument("--note", type=Path, required=True)
    compile_note.add_argument("--plan", type=Path, required=True)
    compile_note.add_argument("--approval", type=Path, required=True)
    compile_note.add_argument("--session-id", required=True)
    compile_note.add_argument("--style-exception-ref", default="")
    compile_note.add_argument("--output", type=Path, default=None)
    compile_note_like = interaction_sub.add_parser("session-compile-note-like")
    compile_note_like.add_argument("--project-root", type=Path, default=None)
    compile_note_like.add_argument("--campaign", type=Path, required=True)
    compile_note_like.add_argument("--note", type=Path, required=True)
    compile_note_like.add_argument("--approval", type=Path, required=True)
    compile_note_like.add_argument("--session-id", required=True)
    compile_note_like.add_argument("--output", type=Path, default=None)
    compile_like = interaction_sub.add_parser("session-compile-comment-like")
    compile_like.add_argument("--project-root", type=Path, default=None)
    compile_like.add_argument("--campaign", type=Path, required=True)
    compile_like.add_argument("--candidate", type=Path, required=True)
    compile_like.add_argument("--post-engagement-request", type=Path, required=True)
    compile_like.add_argument("--session-id", required=True)
    compile_like.add_argument("--output", type=Path, default=None)
    approval_record = interaction_sub.add_parser("approval-record")
    approval_record.add_argument("--project-root", type=Path, default=None)
    approval_record.add_argument("--file", type=Path, required=True)
    approval_record.add_argument("--confirm-approval", required=True)
    comment_preview = interaction_sub.add_parser("comment-preview")
    comment_preview.add_argument("--project-root", type=Path, default=None)
    comment_preview.add_argument("--file", type=Path, required=True)
    comment_ready = interaction_sub.add_parser("comment-smoke-ready")
    comment_ready.add_argument("--project-root", type=Path, default=None)
    comment_ready.add_argument("--file", type=Path, required=True)
    comment_ready.add_argument("--browser-config", type=Path, required=True)
    comment_ready.add_argument("--daily-action-limit", type=int, default=10)
    comment_ready.add_argument("--minimum-target-interval-seconds", type=int, default=600)
    comment_run = interaction_sub.add_parser("comment-smoke-run")
    comment_run.add_argument("--project-root", type=Path, default=None)
    comment_run.add_argument("--file", type=Path, required=True)
    comment_run.add_argument("--browser-config", type=Path, required=True)
    comment_run.add_argument("--daily-action-limit", type=int, default=10)
    comment_run.add_argument("--minimum-target-interval-seconds", type=int, default=600)
    comment_run.add_argument("--confirm-single-comment-flow", required=True)
    for name in ("note-comment-preview", "note-comment-smoke-ready", "note-comment-smoke-run"):
        command = interaction_sub.add_parser(name)
        command.add_argument("--project-root", type=Path, default=None)
        command.add_argument("--file", type=Path, required=True)
        if name != "note-comment-preview":
            command.add_argument("--browser-config", type=Path, required=True)
            command.add_argument("--daily-action-limit", type=int, default=10)
            command.add_argument("--minimum-target-interval-seconds", type=int, default=600)
        if name == "note-comment-smoke-run":
            command.add_argument("--confirm-single-note-comment", required=True)
    session_preview = interaction_sub.add_parser("session-plan-preview")
    session_preview.add_argument("--project-root", type=Path, default=None)
    session_preview.add_argument("--file", type=Path, required=True)
    session_approval = interaction_sub.add_parser("session-approval-record")
    session_approval.add_argument("--project-root", type=Path, default=None)
    session_approval.add_argument("--file", type=Path, required=True)
    session_approval.add_argument("--confirm-approval", required=True)
    session_task_approval = interaction_sub.add_parser("session-task-plan-approve")
    session_task_approval.add_argument("--project-root", type=Path, default=None)
    session_task_approval.add_argument("--file", type=Path, required=True)
    session_task_approval.add_argument("--task-id", required=True)
    session_task_approval.add_argument("--at", required=True)
    session_start = interaction_sub.add_parser("session-start")
    session_start.add_argument("--project-root", type=Path, default=None)
    session_start.add_argument("--browser-config", type=Path, required=True)
    session_start.add_argument("--session-id", required=True)
    session_start.add_argument("--query", required=True)
    session_adopt = interaction_sub.add_parser("session-adopt-current-results")
    session_adopt.add_argument("--project-root", type=Path, default=None)
    session_adopt.add_argument("--browser-config", type=Path, required=True)
    session_adopt.add_argument("--session-id", required=True)
    session_adopt.add_argument("--query", required=True)
    for command in (session_start, session_adopt):
        command.add_argument("--campaign-id", default="")
        command.add_argument("--query-id", default="")
        command.add_argument("--run-id", default="")
        command.add_argument("--strategy-pack-id", default="")
        command.add_argument("--strategy-pack-hash", default="")
        command.add_argument("--strategy-approval-hash", default="")
    session_open = interaction_sub.add_parser("session-open-next")
    session_open.add_argument("--project-root", type=Path, default=None)
    session_open.add_argument("--session-id", required=True)
    session_open.add_argument("--result-index", type=int)
    session_open.add_argument("--max-comments", type=int, default=30)
    session_continue = interaction_sub.add_parser("session-continue-current")
    session_continue.add_argument("--project-root", type=Path, default=None)
    session_continue.add_argument("--session-id", required=True)
    session_continue.add_argument("--max-comments", type=int, default=30)
    session_execute = interaction_sub.add_parser("session-execute-current")
    session_execute.add_argument("--project-root", type=Path, default=None)
    session_execute.add_argument("--file", type=Path, required=True)
    session_execute.add_argument("--browser-config", type=Path, required=True)
    session_execute.add_argument("--daily-action-limit", type=int, default=10)
    session_execute.add_argument("--minimum-target-interval-seconds", type=int, default=600)
    session_execute.add_argument("--confirm-current-page-interaction", required=True)
    session_execute.add_argument("--confirm-bounded-write-uat", required=True)
    session_execute.add_argument("--task-id", default=None)
    session_status = interaction_sub.add_parser("session-status")
    session_status.add_argument("--project-root", type=Path, default=None)
    session_status.add_argument("--session-id", required=True)
    session_return = interaction_sub.add_parser("session-return-results")
    session_return.add_argument("--project-root", type=Path, default=None)
    session_return.add_argument("--session-id", required=True)
    return parser


def _format_text(report: dict[str, object]) -> str:
    lines = [f"overall: {'PASS' if report['ok'] else 'FAIL'}"]
    for check in report["checks"]:  # type: ignore[union-attr]
        status = "PASS" if check["ok"] else "FAIL"
        lines.append(f"[{status}] {check['name']}: {check['detail']}")
    return "\n".join(lines)


def _current_page_action_names(plan: CurrentPageInteractionPlan) -> tuple[str, ...]:
    actions: list[str] = []
    if plan.like_enabled:
        actions.append("like")
    if plan.branch is InteractionBranch.NOTE_ENGAGEMENT:
        actions.append("comment")
    elif plan.branch is InteractionBranch.COMMENT_ENGAGEMENT:
        actions.append("reply")
    return tuple(actions)


def _classify_comment_like_precheck(result: dict[str, object]) -> dict[str, object]:
    if result.get("targetRootFound") is not True:
        return {
            "state": "target_unavailable",
            "authorization_eligible": False,
            "blockers": ["comment_like_target_unavailable"],
        }
    raw_controls = result.get("likeControls")
    controls = [
        row for row in raw_controls
        if isinstance(row, dict) and row.get("visible") is True
    ] if isinstance(raw_controls, list) else []
    if len(controls) != 1:
        return {
            "state": "unknown",
            "authorization_eligible": False,
            "blockers": ["comment_like_control_state_ambiguous"],
        }
    control = controls[0]
    signals = control.get("activeSignals")
    signals = signals if isinstance(signals, dict) else {}
    active_classes = signals.get("activeClassNodes")
    active_classes = active_classes if isinstance(active_classes, list) else []
    explicit_liked_classes = signals.get("explicitLikedClassNodes")
    explicit_liked_classes = (
        explicit_liked_classes
        if isinstance(explicit_liked_classes, list)
        else active_classes
    )
    red_colors = signals.get("redColorNodes")
    red_colors = red_colors if isinstance(red_colors, list) else []
    if (
        signals.get("ariaPressedTrue") is True
        or explicit_liked_classes
        or red_colors
    ):
        return {
            "state": "already_liked",
            "authorization_eligible": False,
            "blockers": ["comment_like_already_in_target_state"],
        }
    aria_pressed = control.get("ariaPressed")
    aria_pressed = aria_pressed if isinstance(aria_pressed, list) else []
    class_name = str(control.get("className") or "").lower()
    if "false" in aria_pressed or "like-wrapper" in class_name:
        return {
            "state": "unliked",
            "authorization_eligible": True,
            "blockers": [],
        }
    return {
        "state": "unknown",
        "authorization_eligible": False,
        "blockers": ["comment_like_control_state_ambiguous"],
    }


def _load_promotion_strategy(
    root: Path,
    promotion_file: Path | None,
    *,
    campaign: Campaign | None = None,
):
    if promotion_file is None:
        if campaign is not None and (
            campaign.metadata.get("promotion_strategy_id")
            or campaign.metadata.get("promotion_input_mode")
        ):
            raise PromotionStrategyError(
                "campaign is promotion-strategy-bound; --promotion-file is required"
            )
        return None
    payload = read_json(
        resolve_project_relative(
            root,
            str(promotion_file),
            field_name="promotion_strategy_file",
        )
    )
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("intent"), dict)
        or not isinstance(payload.get("strategy_draft"), dict)
    ):
        raise PromotionStrategyError(
            "promotion strategy file requires intent and strategy_draft objects"
        )
    intent = PromotionIntent.from_dict(payload["intent"])
    strategy = build_promotion_strategy(intent=intent, draft=payload["strategy_draft"])
    if campaign is not None:
        expected_strategy_id = campaign.metadata.get("promotion_strategy_id")
        expected_input_mode = campaign.metadata.get("promotion_input_mode")
        if expected_strategy_id and strategy.strategy_id != expected_strategy_id:
            raise PromotionStrategyError(
                "promotion strategy does not match Campaign promotion_strategy_id"
            )
        if expected_input_mode and intent.mode.value != expected_input_mode:
            raise PromotionStrategyError(
                "promotion intent mode does not match Campaign promotion_input_mode"
            )
        if (
            intent.source_id != campaign.source_note_id
            or intent.source_ref != campaign.source_note_ref
            or intent.content_hash != campaign.source_note_hash
        ):
            raise PromotionStrategyError(
                "promotion intent source does not match Campaign source binding"
            )
    return strategy


def _compile_daily_plan(
    root: Path,
    campaign_file: Path,
    manifest_file: Path,
    promotion_file: Path | None = None,
):
    campaign_payload = read_json(
        resolve_project_relative(root, str(campaign_file), field_name="loop_campaign_file")
    )
    manifest = read_json(
        resolve_project_relative(root, str(manifest_file), field_name="loop_manifest_file")
    )
    if not isinstance(campaign_payload, dict) or not isinstance(manifest, dict):
        raise LoopPlanError("loop preview inputs must be objects")
    allowed = {"checked_at", "plan_date", "created_at", "budget", "items"}
    if set(manifest) != allowed:
        raise LoopPlanError("loop manifest fields are incomplete or unknown")
    budget_payload, item_payloads = manifest["budget"], manifest["items"]
    if not isinstance(budget_payload, dict) or not isinstance(item_payloads, list):
        raise LoopPlanError("loop budget must be an object and items must be a list")
    budget_allowed = {
        "max_search_queries", "max_candidate_reviews", "max_interaction_targets",
        "minimum_target_interval_seconds", "visible_step_min_seconds",
        "visible_step_max_seconds", "schedule_window_seconds",
    }
    if set(budget_payload) != budget_allowed:
        raise LoopPlanError("loop budget fields are incomplete or unknown")
    campaign_value = Campaign.from_dict(campaign_payload)
    promotion_strategy = _load_promotion_strategy(
        root, promotion_file, campaign=campaign_value
    )
    discovery_value = build_discovery_plan(
        campaign_value,
        checked_at=manifest["checked_at"],
        promotion_strategy=promotion_strategy,
    )
    pairs = []
    for index, item in enumerate(item_payloads):
        if not isinstance(item, dict) or set(item) != {"candidate", "draft"}:
            raise LoopPlanError(f"items[{index}] must contain candidate and draft paths")
        candidate_payload = read_json(
            resolve_project_relative(root, str(item["candidate"]), field_name=f"loop_candidate_{index}")
        )
        draft_payload = read_json(
            resolve_project_relative(root, str(item["draft"]), field_name=f"loop_draft_{index}")
        )
        if not isinstance(candidate_payload, dict) or not isinstance(draft_payload, dict):
            raise LoopPlanError(f"items[{index}] files must contain objects")
        candidate_value = CandidateInteractionPlan.from_dict(candidate_payload)
        pairs.append((candidate_value, build_message_plan(
            campaign=campaign_value, candidate=candidate_value, draft=draft_payload
        )))
    return build_daily_plan(
        campaign=campaign_value,
        discovery_plan=discovery_value,
        candidate_messages=pairs,
        budget=DailyBudget(**budget_payload),
        plan_date=manifest["plan_date"],
        created_at=manifest["created_at"],
    )


def _compile_dm_plan(root: Path, campaign_file: Path, conversation_file: Path, draft_file: Path, captured_at: str):
    campaign_raw = read_json(resolve_project_relative(root, str(campaign_file), field_name="dm_campaign_file"))
    conversation_raw = read_json(resolve_project_relative(root, str(conversation_file), field_name="dm_conversation_file"))
    draft_raw = read_json(resolve_project_relative(root, str(draft_file), field_name="dm_draft_file"))
    if not all(isinstance(item, dict) for item in (campaign_raw, conversation_raw, draft_raw)):
        raise DMContractError("DM campaign, conversation, and draft inputs must be objects")
    campaign_value = Campaign.from_dict(campaign_raw)
    persisted_snapshot = conversation_raw.get("dm_conversation", conversation_raw)
    if isinstance(persisted_snapshot, dict) and "snapshot_id" in persisted_snapshot:
        from .dm import DMConversationSnapshot

        snapshot = DMConversationSnapshot.from_dict(persisted_snapshot)
        if snapshot.account_id != campaign_value.account_id:
            raise DMContractError("persisted DM snapshot account differs from Campaign")
    else:
        snapshot = build_dm_conversation_snapshot(
            account_id=campaign_value.account_id,
            captured_at=captured_at,
            capture=conversation_raw,
        )
    plan = build_dm_message_plan(campaign=campaign_value, snapshot=snapshot, draft=draft_raw)
    return campaign_value, snapshot, plan


def _configure_windows_stdout_utf8() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def _load_xhs_preflight_state(
    *,
    root: Path,
    account_id: str,
    checked_at: str,
    browser_config_path: Path = Path("config/browser.local.json"),
) -> tuple[object, UnifiedPreflightState]:
    project_config, _ = load_project_config(root)
    browser_config, _ = load_browser_config(root, browser_config_path)
    identity = read_json(
        project_config.runtime.runtime_dir / "setup" / "platform_identity.json",
        default=None,
    )
    identity_hash = identity.get("platform_identity_hash") if isinstance(identity, dict) else ""
    identity_ready = (
        isinstance(identity, dict)
        and identity.get("account_id") == account_id
        and isinstance(identity_hash, str)
        and len(identity_hash) == 64
        and all(character in "0123456789abcdef" for character in identity_hash)
    )
    account_matches = browser_config.account_id == account_id
    state = UnifiedPreflightState(
        platform_access_allowed=(
            account_matches
            and browser_config.allow_platform_access
            and not browser_config.fixture_only
        ),
        login_ready=(
            account_matches
            and bool(load_calibration_status(
                project_config.runtime.runtime_dir,
                browser_config,
                checked_at=checked_at,
            ))
        ),
        account_identity_ready=account_matches and identity_ready,
        target_ready=True,
        approval_ready=True,
        capability_ready=True,
    )
    return project_config, state


_PUBLIC_SETUP_COMMAND_REWRITES = {
    "run-agent enroll-extension-instance": "setup extension-enroll",
    "run-agent connection-check": "setup connection-check",
    "run-agent enroll-platform-account": "setup account-enroll",
    "run-agent readonly-uat-preflight": "setup uat-preflight",
    "run-agent status": "setup status",
    "style learn-from-account": "setup voice-learn",
}


def _public_setup_payload(value: object) -> object:
    """Remove internal CLI names from recipient-facing Setup results."""

    if isinstance(value, dict):
        return {key: _public_setup_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_setup_payload(item) for item in value]
    if isinstance(value, str):
        result = value
        for internal, public in _PUBLIC_SETUP_COMMAND_REWRITES.items():
            result = result.replace(internal, public)
        return result
    return value


def _public_operation_projection(
    *,
    workflow: str,
    target_ref_hash: str,
    target_context_hash: str,
    content_hash: str,
    approval: Mapping[str, Any] | None = None,
    receipt_ref: str | None = None,
    verification_method: str,
    verification_status: str = "pending",
    action_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    approval_payload = dict(approval) if approval is not None else None
    approval_hash = None
    approval_ref = None
    if approval_payload is not None:
        approval_ref = approval_payload.get("approval_id")
        approval_hash = approval_payload.get("approval_hash") or sha256(
            json.dumps(approval_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    return {
        "workflow": workflow,
        "target": {
            "ref_hash": target_ref_hash,
            "context_hash": target_context_hash,
        },
        "content_hash": content_hash,
        "approval": {
            "ref": approval_ref,
            "hash": approval_hash,
        },
        "receipt": {"ref": receipt_ref},
        "verification": {
            "status": verification_status,
            "method": verification_method,
        },
        "action_record": dict(action_record) if action_record is not None else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    _configure_windows_stdout_utf8()
    args = build_parser().parse_args(argv)
    if args.command == "setup" and args.setup_command == "voice-learn":
        args.command = "style"
        args.style_command = "learn-from-account"
    if args.command == "setup":
        try:
            root = find_project_root(args.project_root)
            if args.setup_command == "handoff-plan":
                print(json.dumps({"ok": True, "handoff_plan": build_handoff_plan(args.account_id)}, ensure_ascii=False, indent=2))
                return 0
            if args.setup_command == "extension-enroll":
                result = _public_setup_payload(
                    RunAgentClient(root).enroll_current_extension_instance(
                        confirmation=args.confirm_enrollment,
                    )
                )
                print(json.dumps({
                    "ok": True,
                    "extension_enrollment": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.setup_command == "connection-check":
                result = _public_setup_payload(RunAgentClient(root).connection_status())
                if not isinstance(result, dict):
                    raise RunAgentError("public setup connection result must be an object")
                ready = result.get("ready_for_login_check") is True
                print(json.dumps({
                    "ok": ready,
                    "connection": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0 if ready else 2
            if args.setup_command == "account-enroll":
                result = _public_setup_payload(
                    RunAgentClient(root).enroll_current_account_identity(
                        confirmation=args.confirm_account_enrollment,
                    )
                )
                print(json.dumps({
                    "ok": True,
                    "platform_account_enrollment": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.setup_command == "uat-preflight":
                result = RunAgentClient(root).run_readonly_uat_preflight(
                    confirmation=args.confirm_readonly_uat,
                    duration_seconds=args.duration_seconds,
                )
                print(json.dumps({
                    "ok": True,
                    "readonly_uat_preflight": result,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.setup_command == "uat-revoke":
                result = RunAgentClient(root).revoke_readonly_uat(
                    confirmation=args.confirm_revoke,
                )
                print(json.dumps({
                    "ok": True,
                    "readonly_uat": result,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.setup_command == "enable-platform-read":
                if args.confirm_platform_read != PLATFORM_READ_CONFIRMATION:
                    raise OnboardingError("exact platform-read confirmation is required")
                browser_config, selected = load_browser_config(root, Path("config/browser.local.json"))
                if browser_config.account_id != args.account_id:
                    raise OnboardingError("platform-read account does not match browser account")
                payload = read_json(selected)
                if not isinstance(payload, dict):
                    raise OnboardingError("browser config is invalid")
                payload["fixture_only"] = False
                payload["allow_platform_access"] = True
                write_json_atomic(selected, payload)
                project_config, _ = load_project_config(root)
                append_jsonl(
                    project_config.runtime.runtime_dir / "setup" / "settings_audit.jsonl",
                    {"account_id": args.account_id, "setting": "allow_platform_access",
                     "value": True, "changed_at": utc_now_iso(), "platform_actions_executed": 0},
                )
                print(json.dumps({
                    "ok": True, "account_id": args.account_id,
                    "platform_read_enabled": True, "platform_writes_enabled": False,
                    "browser_config": str(selected.relative_to(root)),
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.setup_command == "account-config":
                payload = read_json(
                    resolve_project_relative(root, str(args.file), field_name="account_setup_file")
                )
                if not isinstance(payload, dict):
                    raise OnboardingError("account setup file must be an object")
                profile = AccountSetupProfile.from_dict(payload)
                project_config, _ = load_project_config(root)
                browser_config, _ = load_browser_config(root, Path("config/browser.local.json"))
                if browser_config.account_id != profile.account_id:
                    raise OnboardingError("account setup does not match browser account")
                path = AccountSetupStore(project_config.runtime.runtime_dir).save(profile)
                print(json.dumps({
                    "ok": True,
                    "account_setup": profile.to_dict(),
                    "storage_ref": str(path.relative_to(project_config.runtime.runtime_dir)),
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.setup_command == "voice-status":
                project_config, _ = load_project_config(root)
                voice = build_account_voice_status(
                    project_config.runtime.runtime_dir,
                    account_id=args.account_id,
                )
                print(json.dumps({
                    "ok": voice["status"] != "invalid",
                    "account_voice": voice,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0 if voice["status"] != "invalid" else 2
            if args.setup_command == "status":
                project_config, _ = load_project_config(root)
                browser_config, _ = load_browser_config(root, Path("config/browser.local.json"))
                if browser_config.account_id != args.account_id:
                    raise OnboardingError("setup status account does not match browser account")
                connection = RunAgentClient(root).connection_status()
                login_ready = load_calibration_status(
                    project_config.runtime.runtime_dir,
                    browser_config,
                    checked_at=utc_now_iso(),
                )
                status = build_setup_status(
                    project_config.runtime.runtime_dir,
                    account_id=args.account_id,
                    connection_ready=connection.get("ready_for_login_check") is True,
                    login_ready=bool(login_ready),
                )
                print(json.dumps({"ok": True, "setup_status": status}, ensure_ascii=False, indent=2))
                return 0
            if args.setup_command == "migrate-existing":
                result = register_existing_user_project(
                    root, account_id=args.account_id, profile_name=args.profile_name
                )
                print(json.dumps({"ok": True, "setup": result}, ensure_ascii=False, indent=2))
                return 0
            result = initialize_user_project(
                root, account_id=args.account_id, profile_name=args.profile_name
            )
            print(json.dumps({"ok": True, "setup": result.to_dict()}, ensure_ascii=False, indent=2))
            return 0
        except (
            SetupError, OnboardingError, ConfigError, BrowserConfigError, BrowserProfileError,
            FileNotFoundError, ProjectPathError, StorageError, RunAgentError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command in {"publish", "service", "engage"}:
        public_command = getattr(args, f"{args.command}_command")
        if public_command == "status":
            print(json.dumps({
                "ok": True,
                "capability": public_group_status(args.command),
                "platform_actions_executed": 0,
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "engage":
            try:
                root = find_project_root(args.project_root)
                project_config, _ = load_project_config(root)
                store = StrategyPackStore(project_config.runtime.runtime_dir)
                if args.engage_command == "latest-account-note":
                    forwarded = [
                        "run-agent", "latest-account-note",
                        "--project-root", str(root),
                        "--created-at", args.created_at,
                        "--max-notes", str(args.max_notes),
                    ]
                    for value in args.user_keyword:
                        forwarded.extend(["--user-keyword", value])
                    for value in args.exclusion:
                        forwarded.extend(["--exclusion", value])
                    return main(forwarded)
                compiler_commands = {
                    "note-like-preview": "session-compile-note-like",
                    "note-comment-preview": "session-compile-note-comment",
                    "comment-reply-preview": "session-compile-reply-plan",
                }
                if args.engage_command in compiler_commands:
                    forwarded = [
                        "interaction", compiler_commands[args.engage_command],
                        "--project-root", str(root),
                        "--campaign", str(args.campaign),
                        "--approval", str(args.approval),
                        "--session-id", args.session_id,
                        "--output", str(args.output),
                    ]
                    if args.engage_command == "note-like-preview":
                        forwarded.extend(["--note", str(args.note)])
                    elif args.engage_command == "note-comment-preview":
                        forwarded.extend([
                            "--note", str(args.note),
                            "--plan", str(args.plan),
                        ])
                        if args.style_exception_ref:
                            forwarded.extend([
                                "--style-exception-ref", args.style_exception_ref,
                            ])
                    else:
                        forwarded.extend([
                            "--candidate", str(args.candidate),
                            "--draft", str(args.draft),
                            "--result-index", str(args.result_index),
                        ])
                        if args.promotion_file is not None:
                            forwarded.extend([
                                "--promotion-file", str(args.promotion_file),
                            ])
                        if args.style_exception_ref:
                            forwarded.extend([
                                "--style-exception-ref", args.style_exception_ref,
                            ])
                        if args.run_id:
                            forwarded.extend(["--run-id", args.run_id])
                    return main(forwarded)
                dm_navigation_commands = {
                    "dm-open-profile": "open-commenter-profile",
                    "dm-open-conversation": "open-dm-conversation",
                    "dm-capture": "capture-current-dm-conversation",
                    "dm-return-source": "return-to-source-comment",
                }
                if args.engage_command in dm_navigation_commands:
                    forwarded = [
                        "run-agent", dm_navigation_commands[args.engage_command],
                        "--project-root", str(root),
                    ]
                    if args.engage_command in {"dm-open-profile", "dm-return-source"}:
                        forwarded.extend([
                            "--feed-id", args.feed_id,
                            "--comment-id", args.comment_id,
                            "--target-context-hash", args.target_context_hash,
                        ])
                    elif args.engage_command == "dm-open-conversation":
                        forwarded.extend([
                            "--expected-peer-ref-hash", args.expected_peer_ref_hash,
                        ])
                    else:
                        forwarded.extend([
                            "--account-id", args.account_id,
                            "--conversation-id", args.conversation_id,
                            "--expected-peer-ref-hash", args.expected_peer_ref_hash,
                            "--captured-at", args.captured_at,
                            "--max-messages", str(args.max_messages),
                        ])
                    return main(forwarded)
                task_commands = {
                    "task-create": "create",
                    "task-authorize": "authorize",
                    "task-status": "show",
                    "task-transition": "transition",
                    "task-schedule": "schedule-preview",
                    "task-due": "due-status",
                    "task-claim": "occurrence-claim",
                    "task-complete": "occurrence-complete",
                }
                if args.engage_command in task_commands:
                    forwarded = [
                        "task", task_commands[args.engage_command],
                        "--project-root", str(root),
                    ]
                    if args.engage_command == "task-create":
                        forwarded.extend(["--file", str(args.file)])
                    else:
                        forwarded.extend(["--task-id", args.task_id])
                    if args.engage_command == "task-authorize":
                        forwarded.extend([
                            "--confirmed-at", args.confirmed_at,
                            "--confirm-bounded-run", args.confirm_bounded_run,
                        ])
                    elif args.engage_command == "task-transition":
                        forwarded.extend([
                            "--to", args.to,
                            "--changed-at", args.changed_at,
                        ])
                    elif args.engage_command == "task-due":
                        forwarded.extend(["--at", args.at])
                    elif args.engage_command == "task-claim":
                        forwarded.extend([
                            "--kind", args.kind,
                            "--at", args.at,
                            "--worker-id", args.worker_id,
                        ])
                    elif args.engage_command == "task-complete":
                        forwarded.extend([
                            "--occurrence-id", args.occurrence_id,
                            "--lease-token", args.lease_token,
                            "--completed-at", args.completed_at,
                            "--outcome", args.outcome,
                        ])
                    return main(forwarded)
                if args.engage_command == "task-plan-approve":
                    return main([
                        "interaction", "session-task-plan-approve",
                        "--project-root", str(root),
                        "--file", str(args.file),
                        "--task-id", args.task_id,
                        "--at", args.at,
                    ])
                if args.engage_command in {
                    "dm-preview", "dm-approve", "dm-ready", "dm-run"
                }:
                    _campaign, _snapshot, dm_plan = _compile_dm_plan(
                        root,
                        args.campaign,
                        args.conversation,
                        args.draft,
                        args.captured_at,
                    )
                    if dm_plan.mode != "active_outreach":
                        raise EngageContractError(
                            "public engage DM accepts active_outreach only; "
                            "use service for inbound DM replies"
                        )
                    internal_command = {
                        "dm-preview": "message-preview",
                        "dm-approve": "approval-record",
                        "dm-ready": "smoke-ready",
                        "dm-run": "smoke-run",
                    }[args.engage_command]
                    forwarded = [
                        "dm", internal_command,
                        "--project-root", str(root),
                        "--campaign", str(args.campaign),
                        "--conversation", str(args.conversation),
                        "--draft", str(args.draft),
                        "--captured-at", args.captured_at,
                    ]
                    if args.engage_command != "dm-preview":
                        forwarded.extend(["--approval", str(args.approval)])
                    if args.engage_command == "dm-approve":
                        forwarded.extend([
                            "--confirm-approval", args.confirm_approval,
                        ])
                    if args.engage_command in {"dm-ready", "dm-run"}:
                        forwarded.extend([
                            "--browser-config", str(args.browser_config),
                            "--expected-peer-ref-hash", args.expected_peer_ref_hash,
                            "--daily-dm-limit", str(args.daily_dm_limit),
                            "--minimum-target-interval-seconds",
                            str(args.minimum_target_interval_seconds),
                        ])
                    if args.engage_command == "dm-run":
                        forwarded.extend([
                            "--confirm-single-dm", args.confirm_single_dm,
                            "--confirm-bounded-write-uat",
                            args.confirm_bounded_write_uat,
                        ])
                    return main(forwarded)
                if args.engage_command == "action-preview":
                    return main([
                        "interaction", "session-plan-preview",
                        "--project-root", str(root),
                        "--file", str(args.file),
                    ])
                if args.engage_command == "reconcile-unknown-write":
                    result = RunAgentClient(root).reconcile_unknown_write(
                        attempt_id=args.attempt_id,
                        observed_outcome=args.observed_outcome,
                        evidence_ref=args.evidence_ref,
                        reconciled_at=args.reconciled_at,
                        note_id=args.note_id,
                        confirmation=args.confirm_reconciliation,
                    )
                    print(json.dumps({
                        "ok": True,
                        "reconciliation": result,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                if args.engage_command == "comment-like-precheck":
                    result = RunAgentClient(root).inspect_current_comment_controls(
                        args.note_id,
                        comment_id=args.comment_id,
                    )
                    summary = _classify_comment_like_precheck(result)
                    print(json.dumps({
                        "ok": True,
                        "note_id": args.note_id,
                        "comment_id": args.comment_id,
                        **summary,
                        "comment_like_precheck": result,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                if args.engage_command == "comment-like-preview":
                    return main([
                        "interaction", "session-compile-comment-like",
                        "--project-root", str(root),
                        "--campaign", str(args.campaign),
                        "--candidate", str(args.candidate),
                        "--post-engagement-request", str(args.post_engagement_request),
                        "--session-id", args.session_id,
                        "--output", str(args.output),
                    ])
                if args.engage_command == "approve":
                    return main([
                        "interaction", "session-approval-record",
                        "--project-root", str(root),
                        "--file", str(args.file),
                        "--confirm-approval", args.confirm_approval,
                    ])
                if args.engage_command == "run":
                    forwarded = [
                        "interaction", "session-execute-current",
                        "--project-root", str(root),
                        "--file", str(args.file),
                        "--browser-config", str(args.browser_config),
                        "--daily-action-limit", str(args.daily_action_limit),
                        "--minimum-target-interval-seconds",
                        str(args.minimum_target_interval_seconds),
                        "--confirm-current-page-interaction",
                        args.confirm_current_page_interaction,
                        "--confirm-bounded-write-uat",
                        args.confirm_bounded_write_uat,
                    ]
                    if args.task_id:
                        forwarded.extend(["--task-id", args.task_id])
                    return main(forwarded)
                if args.engage_command == "next":
                    return main([
                        "interaction", "session-open-next",
                        "--project-root", str(root),
                        "--session-id", args.session_id,
                        "--max-comments", str(args.max_comments),
                    ])
                if args.engage_command == "continue":
                    return main([
                        "interaction", "session-continue-current",
                        "--project-root", str(root),
                        "--session-id", args.session_id,
                        "--max-comments", str(args.max_comments),
                    ])
                if args.engage_command == "return":
                    return main([
                        "interaction", "session-return-results",
                        "--project-root", str(root),
                        "--session-id", args.session_id,
                    ])
                if args.engage_command == "strategy-preview":
                    manifest = read_json(resolve_project_relative(
                        root, str(args.file), field_name="strategy_pack_manifest_file"
                    ))
                    if not isinstance(manifest, dict):
                        raise StrategyPackError("strategy pack manifest must be an object")
                    pack = build_strategy_pack(
                        account_id=args.account_id,
                        manifest=manifest,
                    )
                    path = store.save(pack)
                    print(json.dumps({
                        "ok": True,
                        "strategy_pack": pack.to_dict(),
                        "strategy_pack_ref": str(
                            path.relative_to(project_config.runtime.runtime_dir)
                        ),
                        "confirmation": STRATEGY_PACK_CONFIRMATION,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                if args.engage_command == "campaign-confirm":
                    proposal_path = resolve_project_relative(
                        root,
                        str(args.file),
                        field_name="campaign_uat_proposal_file",
                    )
                    proposal_payload = read_json(proposal_path)
                    if not isinstance(proposal_payload, dict):
                        raise CampaignUatProposalError(
                            "Campaign UAT proposal JSON root must be an object"
                        )
                    proposal = BoundedCampaignUatProposal.from_dict(proposal_payload)
                    if proposal.proposal_id != args.proposal_id:
                        raise CampaignUatProposalError("Campaign UAT proposal id mismatch")
                    if proposal.content_hash != args.proposal_hash:
                        raise CampaignUatProposalError("Campaign UAT proposal hash mismatch")
                    confirmed_at = utc_now_iso()
                    campaign_value, report = proposal.confirm(
                        confirmed_at=confirmed_at,
                        confirmation=args.confirm_campaign_uat_proposal,
                    )
                    campaign_path = CampaignRepository(
                        project_config.runtime.runtime_dir
                    ).create(campaign_value)
                    print(json.dumps({
                        "ok": True,
                        "proposal_id": proposal.proposal_id,
                        "proposal_hash": proposal.content_hash,
                        "confirmed_at": confirmed_at,
                        "campaign": campaign_value.to_dict(),
                        "validation": report.to_dict(),
                        "campaign_ref": str(
                            campaign_path.relative_to(project_config.runtime.runtime_dir)
                        ),
                        "confirmation": CAMPAIGN_UAT_PROPOSAL_CONFIRMATION,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                if args.engage_command == "search-start":
                    pack = store.load(args.strategy_pack_id)
                    approval = store.load_approval(pack)
                    checked_at = utc_now_iso()
                    require_engage_search_campaign(
                        runtime_dir=project_config.runtime.runtime_dir,
                        campaign_id=args.campaign_id,
                        strategy_pack=pack,
                        checked_at=checked_at,
                    )
                    session_store = InteractionSessionStore(
                        project_config.runtime.runtime_dir
                    )
                    if session_store.session_path(args.session_id).exists():
                        raise StrategyPackError(
                            "engage search requires a new session_id; existing session "
                            "cannot be overwritten or re-searched"
                        )
                    browser_config, _ = load_browser_config(root, args.browser_config)
                    if browser_config.account_id != pack.account_id:
                        raise StrategyPackError(
                            "strategy pack account does not match browser account"
                        )
                    identity = RunAgentClient(root).assert_current_account_identity()
                    if identity.get("verified") is not True:
                        raise StrategyPackError(
                            "live Xiaohongshu account identity was not verified"
                        )
                    exact_query = str(pack.query(args.query_id)["query"])
                    forwarded = [
                        "interaction", "session-start",
                        "--project-root", str(root),
                        "--browser-config", str(args.browser_config),
                        "--session-id", args.session_id,
                        "--query", exact_query,
                        "--strategy-pack-id", pack.strategy_pack_id,
                        "--strategy-pack-hash", pack.content_hash,
                        "--strategy-approval-hash", approval.approval_hash,
                    ]
                    forwarded.extend([
                        "--campaign-id", args.campaign_id,
                        "--query-id", args.query_id,
                        "--run-id", args.run_id,
                    ])
                    return main(forwarded)
                pack = store.load(args.strategy_pack_id)
                if args.engage_command == "strategy-show":
                    try:
                        approval = store.load_approval(pack).to_dict()
                    except StrategyPackError:
                        approval = None
                    print(json.dumps({
                        "ok": True,
                        "strategy_pack": pack.to_dict(),
                        "approval": approval,
                        "confirmed": approval is not None,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                approval = store.confirm(
                    pack,
                    confirmed_at=args.confirmed_at,
                    confirmation=args.confirm_strategy,
                )
                print(json.dumps({
                    "ok": True,
                    "strategy_pack_approval": approval.to_dict(),
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            except (
                EngageContractError,
                DMContractError,
                CampaignContractError,
                CampaignRepositoryError,
                CampaignUatProposalError,
                BrowserConfigError,
                StrategyPackError,
                PromotionStrategyError,
                ConfigError,
                FileNotFoundError,
                ProjectPathError,
                StorageError,
            ) as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
                return 1
        if args.command == "publish":
            try:
                root = find_project_root(args.project_root)
                project_config, _ = load_project_config(root)
                raw = read_json(
                    resolve_project_relative(root, str(args.file), field_name="publish_plan_file")
                )
                if not isinstance(raw, dict):
                    raise PublishContractError("publish plan file must be an object")
                plan = PublishPlan.from_dict(raw, project_root=root)
                if args.publish_command == "preview":
                    print(json.dumps({
                        "ok": True,
                        "publish_plan": plan.to_dict(),
                        "operation": _public_operation_projection(
                            workflow="publish",
                            target_ref_hash=sha256(plan.plan_id.encode("utf-8")).hexdigest(),
                            target_context_hash="",
                            content_hash=plan.content_hash,
                            verification_method="visible_publish_terminal",
                        ),
                        "approval_confirmation": PUBLISH_APPROVAL_CONFIRMATION,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                if args.publish_command == "approve":
                    _config, publish_state = _load_xhs_preflight_state(
                        root=root,
                        account_id=plan.account_id,
                        checked_at=args.approved_at,
                    )
                    approval, lease = approve_publish_plan(
                        project_root=root,
                        runtime_dir=project_config.runtime.runtime_dir,
                        plan=plan,
                        approved_at=args.approved_at,
                        confirmation=args.confirm_publish,
                        state=publish_state,
                    )
                    print(json.dumps({
                        "ok": True,
                        "publish_approval": approval.to_dict(),
                        "operation": _public_operation_projection(
                            workflow="publish",
                            target_ref_hash=sha256(plan.plan_id.encode("utf-8")).hexdigest(),
                            target_context_hash="",
                            content_hash=plan.content_hash,
                            approval=approval.to_dict(),
                            verification_method="visible_publish_terminal",
                            verification_status="approved",
                        ),
                        "write_lease": lease,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                executed_at = utc_now_iso()
                _config, publish_state = _load_xhs_preflight_state(
                    root=root,
                    account_id=plan.account_id,
                    checked_at=executed_at,
                )
                result, receipt_path = execute_publish_plan(
                    project_root=root,
                    runtime_dir=project_config.runtime.runtime_dir,
                    plan=plan,
                    executed_at=executed_at,
                    state=publish_state,
                )
                publish_receipt = read_json(receipt_path)
                action_record = publish_receipt.get("operation_receipt")
                receipt_ref = str(receipt_path.relative_to(project_config.runtime.runtime_dir))
                print(json.dumps({
                    "ok": True,
                    "publish_result": result,
                    "operation": _public_operation_projection(
                        workflow="publish",
                        target_ref_hash=sha256(plan.plan_id.encode("utf-8")).hexdigest(),
                        target_context_hash="",
                        content_hash=plan.content_hash,
                        approval=PublishRuntimeStore(
                            project_config.runtime.runtime_dir
                        ).load_approval(plan).to_dict(),
                        receipt_ref=receipt_ref,
                        verification_method="visible_publish_terminal",
                        verification_status=str(action_record.get("status")),
                        action_record=action_record,
                    ),
                    "receipt_ref": receipt_ref,
                    "platform_actions_executed": 1,
                }, ensure_ascii=False, indent=2))
                return 0
            except (
                PublishContractError,
                RunAgentError,
                ConfigError,
                FileNotFoundError,
                ProjectPathError,
                StorageError,
            ) as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
                return 1
        if args.command == "service":
            try:
                root = find_project_root(args.project_root)
                project_config, _ = load_project_config(root)
                store = ServiceQueueStore(project_config.runtime.runtime_dir)
                if args.service_command == "queue-status":
                    print(json.dumps({
                        "ok": True,
                        "service_queue": store.status(account_id=args.account_id),
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                if args.service_command in {"scan", "next"}:
                    checked_at = (
                        args.captured_at if args.service_command == "scan" else args.opened_at
                    )
                    project_config, state = _load_xhs_preflight_state(
                        root=root,
                        account_id=args.account_id,
                        checked_at=checked_at,
                    )
                    read_blockers = []
                    if not state.platform_access_allowed:
                        read_blockers.append("platform_access_disabled_or_account_mismatch")
                    if not state.login_ready:
                        read_blockers.append("login_not_ready")
                    if not state.account_identity_ready:
                        read_blockers.append("account_identity_not_ready")
                    if read_blockers:
                        raise ServiceContractError(
                            "service read blocked: " + "; ".join(read_blockers)
                        )
                    client = RunAgentClient(root)
                    identity = client.assert_current_account_identity()
                    if identity.get("verified") is not True:
                        raise ServiceContractError("live Xiaohongshu account identity is unverified")
                    if args.service_command == "scan":
                        result = scan_service_inbox(
                            client=client,
                            store=store,
                            account_id=args.account_id,
                            channel=args.channel,
                            captured_at=args.captured_at,
                            max_items=args.max_items,
                        )
                    else:
                        result = open_next_service_item(
                            client=client,
                            store=store,
                            account_id=args.account_id,
                            channel=args.channel,
                            opened_at=args.opened_at,
                            max_comments=args.max_comments,
                            max_messages=args.max_messages,
                        )
                    print(json.dumps({"ok": True, "service": result}, ensure_ascii=False, indent=2))
                    return 0
                if args.service_command == "preview":
                    draft = read_json(
                        resolve_project_relative(
                            root, str(args.file), field_name="service_draft_file"
                        )
                    )
                    if not isinstance(draft, dict):
                        raise ServiceContractError("service draft file must be an object")
                    plan = build_service_reply_plan(
                        runtime_dir=project_config.runtime.runtime_dir,
                        item_id=args.item_id,
                        draft=draft,
                        checked_at=args.checked_at,
                    )
                    plan_path = store.save_plan(plan)
                    print(json.dumps({
                        "ok": True,
                        "service_reply_plan": plan.to_dict(),
                        "operation": _public_operation_projection(
                            workflow="service",
                            target_ref_hash=sha256(plan.item_id.encode("utf-8")).hexdigest(),
                            target_context_hash=plan.target_context_hash,
                            content_hash=plan.content_hash,
                            verification_method=(
                                "exact_visible_reply_increase"
                                if plan.channel == "comments"
                                else "exact_visible_outgoing_message_increase"
                            ),
                        ),
                        "plan_ref": str(plan_path.relative_to(project_config.runtime.runtime_dir)),
                        "approval_confirmation": SERVICE_REPLY_CONFIRMATION,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                checked_at = (
                    args.approved_at if args.service_command == "approve" else args.executed_at
                )
                project_config, state = _load_xhs_preflight_state(
                    root=root,
                    account_id=store.load_plan(args.plan_id).account_id,
                    checked_at=checked_at,
                )
                plan = store.load_plan(args.plan_id)
                if args.service_command == "approve":
                    approval, decision, lease = approve_service_reply(
                        project_root=root,
                        runtime_dir=project_config.runtime.runtime_dir,
                        plan=plan,
                        approved_at=args.approved_at,
                        confirmation=args.confirm_service_reply,
                        state=state,
                        daily_limit=args.daily_limit,
                        minimum_interval_seconds=args.minimum_interval_seconds,
                        budget_timezone=args.budget_timezone,
                    )
                    print(json.dumps({
                        "ok": True,
                        "service_reply_approval": approval.to_dict(),
                        "operation": _public_operation_projection(
                            workflow="service",
                            target_ref_hash=sha256(plan.item_id.encode("utf-8")).hexdigest(),
                            target_context_hash=plan.target_context_hash,
                            content_hash=plan.content_hash,
                            approval=approval.to_dict(),
                            verification_method=(
                                "exact_visible_reply_increase"
                                if plan.channel == "comments"
                                else "exact_visible_outgoing_message_increase"
                            ),
                            verification_status="approved",
                        ),
                        "preflight": decision.to_dict(),
                        "write_lease": lease,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                result, receipt_path, decision = execute_service_reply(
                    project_root=root,
                    runtime_dir=project_config.runtime.runtime_dir,
                    plan=plan,
                    executed_at=args.executed_at,
                    state=state,
                    daily_limit=args.daily_limit,
                    minimum_interval_seconds=args.minimum_interval_seconds,
                    budget_timezone=args.budget_timezone,
                )
                service_receipt = read_json(receipt_path)
                action_record = service_receipt.get("operation_receipt")
                receipt_ref = str(receipt_path.relative_to(project_config.runtime.runtime_dir))
                print(json.dumps({
                    "ok": True,
                    "service_reply_result": result,
                    "operation": _public_operation_projection(
                        workflow="service",
                        target_ref_hash=sha256(plan.item_id.encode("utf-8")).hexdigest(),
                        target_context_hash=plan.target_context_hash,
                        content_hash=plan.content_hash,
                        approval=store.load_approval(plan).to_dict(),
                        receipt_ref=receipt_ref,
                        verification_method=(
                            "exact_visible_reply_increase"
                            if plan.channel == "comments"
                            else "exact_visible_outgoing_message_increase"
                        ),
                        verification_status=str(action_record.get("status")),
                        action_record=action_record,
                    ),
                    "preflight": decision.to_dict(),
                    "receipt_ref": receipt_ref,
                    "platform_actions_executed": 1,
                }, ensure_ascii=False, indent=2))
                return 0
            except (
                ServiceContractError,
                RunAgentError,
                BrowserConfigError,
                BrowserProfileError,
                ConfigError,
                FileNotFoundError,
                ProjectPathError,
                StorageError,
            ) as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
                return 1
    if args.command == "doctor":
        report = run_doctor(
            start=args.project_root,
            config_path=args.config,
            init_runtime=args.init_runtime,
        ).to_dict()
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(_format_text(report))
        return 0 if report["ok"] else 1
    if args.command == "validate-action-record":
        try:
            root = find_project_root(args.project_root)
            record_path = resolve_project_relative(
                root, str(args.file), field_name="action_record_file"
            )
            payload = read_json(record_path)
            if not isinstance(payload, dict):
                raise ActionContractError("ActionRecord JSON root must be an object")
            record = ActionRecord.from_dict(payload)
        except (
            ActionContractError,
            FileNotFoundError,
            ProjectPathError,
            StorageCorruptionError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(
            json.dumps(
                {"ok": True, "record_id": record.record_id, "status": record.status.value},
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "campaign":
        try:
            root = find_project_root(args.project_root)
            config, _ = load_project_config(root)
            repository = CampaignRepository(config.runtime.runtime_dir)
            if args.campaign_command in {"validate", "create"}:
                campaign_path = resolve_project_relative(
                    root, str(args.file), field_name="campaign_file"
                )
                payload = read_json(campaign_path)
                if not isinstance(payload, dict):
                    raise CampaignContractError("Campaign JSON root must be an object")
                campaign_value = Campaign.from_dict(payload)
                report = validate_campaign(
                    campaign_value,
                    checked_at=args.checked_at or utc_now_iso(),
                    minimum_classification_confidence=args.minimum_confidence,
                )
                if args.campaign_command == "create":
                    if campaign_value.status in {
                        CampaignStatus.READY,
                        CampaignStatus.ACTIVE,
                    } and not report.ok:
                        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                        return 2
                    repository.create(campaign_value)
                    output = report.to_dict()
                    output["stored"] = True
                    print(json.dumps(output, ensure_ascii=False, indent=2))
                    return 0
                print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                return 0 if report.ok else 2
            if args.campaign_command == "show":
                print(
                    json.dumps(repository.get(args.id).to_dict(), ensure_ascii=False, indent=2)
                )
                return 0
            if args.campaign_command == "list":
                rows = [
                    {
                        "campaign_id": item.campaign_id,
                        "activity_type": item.activity_type.value,
                        "status": item.status.value,
                        "updated_at": item.updated_at,
                    }
                    for item in repository.list()
                ]
                print(json.dumps({"ok": True, "campaigns": rows}, ensure_ascii=False, indent=2))
                return 0
            if args.campaign_command == "transition":
                current = repository.get(args.id)
                changed = current.transition(
                    CampaignStatus(args.to),
                    changed_at=args.changed_at or utc_now_iso(),
                    actor=StatusActor(args.actor),
                    reason=args.reason,
                )
                if changed.status in {CampaignStatus.READY, CampaignStatus.ACTIVE}:
                    report = validate_campaign(
                        changed,
                        checked_at=changed.updated_at,
                        minimum_classification_confidence=args.minimum_confidence,
                    )
                    if not report.ok:
                        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                        return 2
                repository.update(changed, expected_updated_at=current.updated_at)
                print(json.dumps(changed.to_dict(), ensure_ascii=False, indent=2))
                return 0
        except (
            CampaignContractError,
            CampaignRepositoryError,
            ConfigError,
            FileNotFoundError,
            ProjectPathError,
            StorageError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "task":
        try:
            root = find_project_root(args.project_root)
            config, _ = load_project_config(root)
            store = CampaignTaskStore(config.runtime.runtime_dir)
            if args.task_command == "create":
                payload = read_json(resolve_project_relative(root, str(args.file), field_name="campaign_task_file"))
                if not isinstance(payload, dict):
                    raise TaskContractError("campaign task file must be an object")
                task_value = CampaignTask.from_dict(payload)
                setup_profile = AccountSetupStore(config.runtime.runtime_dir).load()
                if task_value.account_id != setup_profile.account_id:
                    raise TaskContractError("task account does not match account setup")
                if not set(task_value.allowed_actions).issubset(set(setup_profile.allowed_actions)):
                    raise TaskContractError("task actions exceed account setup authorization")
                setup_caps = {
                    "targets": setup_profile.max_targets_per_day,
                    "likes": setup_profile.max_likes_per_day,
                    "comments": setup_profile.max_comments_per_day,
                    "replies": setup_profile.max_replies_per_day,
                }
                if any(task_value.daily_caps[key] > setup_caps[key] for key in setup_caps):
                    raise TaskContractError("task daily caps exceed account setup limits")
                campaign_value = CampaignRepository(config.runtime.runtime_dir).get(task_value.campaign_id)
                if campaign_value.account_id != task_value.account_id:
                    raise TaskContractError("task account does not match campaign")
                if campaign_value.status not in {CampaignStatus.READY, CampaignStatus.ACTIVE}:
                    raise TaskContractError("task requires a ready or active campaign")
                path = store.create(task_value)
                print(json.dumps({
                    "ok": True, "task": task_value.to_dict(),
                    "storage_ref": str(path.relative_to(config.runtime.runtime_dir)),
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            task_value = store.load(args.task_id)
            if args.task_command == "show":
                print(json.dumps({"ok": True, "task": task_value.to_dict()}, ensure_ascii=False, indent=2))
                return 0
            if args.task_command == "authorize":
                authorization = authorize_campaign_task(
                    task_value,
                    confirmed_at=args.confirmed_at,
                    confirmation=args.confirm_bounded_run,
                )
                auth_path = store.save_authorization(authorization)
                if task_value.status == "draft":
                    task_value = store.transition(
                        task_value.task_id, to_status="approved", changed_at=args.confirmed_at
                    )
                print(json.dumps({
                    "ok": True, "task": task_value.to_dict(),
                    "authorization": authorization.to_dict(),
                    "authorization_ref": str(auth_path.relative_to(config.runtime.runtime_dir)),
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.task_command == "transition":
                changed = store.transition(task_value.task_id, to_status=args.to, changed_at=args.changed_at)
                print(json.dumps({"ok": True, "task": changed.to_dict(), "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            if args.task_command == "schedule-preview":
                print(json.dumps({"ok": True, "schedule": build_schedule_manifest(task_value)}, ensure_ascii=False, indent=2))
                return 0
            if args.task_command == "due-status":
                result = evaluate_task_due(task_value, at=args.at)
                authorization = store.load_authorization(task_value.task_id)
                authorized = authorization.task_hash == task_value.content_hash
                result["authorization_valid"] = authorized
                if not authorized:
                    result["due"] = False
                    result["reason"] = "authorization_mismatch"
                print(json.dumps({"ok": True, "due_status": result, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            occurrence_store = TaskOccurrenceStore(config.runtime.runtime_dir)
            if args.task_command == "occurrence-claim":
                result = occurrence_store.claim(
                    task_value,
                    kind=args.kind,
                    at=args.at,
                    worker_id=args.worker_id,
                )
                print(json.dumps({
                    "ok": result["claimed"],
                    "occurrence": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0 if result["claimed"] else 2
            if args.task_command == "occurrence-complete":
                result = occurrence_store.complete(
                    task_value,
                    occurrence_id=args.occurrence_id,
                    lease_token=args.lease_token,
                    completed_at=args.completed_at,
                    outcome=args.outcome,
                )
                print(json.dumps({
                    "ok": True,
                    "occurrence": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
        except (
            TaskContractError, OnboardingError, CampaignRepositoryError,
            ConfigError, FileNotFoundError, ProjectPathError, StorageError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "source":
        try:
            root = find_project_root(args.project_root)
            input_path = resolve_project_relative(
                root, str(args.file), field_name="source_snapshot_file"
            )
            payload = read_json(input_path)
            if not isinstance(payload, dict):
                raise LatestNoteContractError("source snapshot input root must be an object")
            fixture_only = payload.get("fixture_only")
            if type(fixture_only) is not bool:
                raise LatestNoteContractError("fixture_only must be a boolean")
            if args.source_command == "thread-preview":
                raw_thread = payload.get("thread")
                snapshot = build_visible_thread_snapshot_from_dict(raw_thread)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "fixture_only": fixture_only,
                            "snapshot": snapshot.to_dict(),
                            "platform_actions_executed": 0,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            raw_cards = payload.get("profile_cards")
            if not isinstance(raw_cards, list):
                raise LatestNoteContractError("profile_cards must be a list")
            cards = tuple(ProfileNoteCard.from_dict(item) for item in raw_cards)
            detail = NoteDetailCapture.from_dict(payload.get("note_detail", {}))
            profile_order_verified = payload.get("profile_order_verified")
            if type(profile_order_verified) is not bool:
                raise LatestNoteContractError("profile_order_verified must be a boolean")
            snapshot = build_latest_note_snapshot(
                cards=cards,
                detail=detail,
                profile_order_verified=profile_order_verified,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "fixture_only": fixture_only,
                        "snapshot": snapshot.to_dict(),
                        "platform_actions_executed": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except (
            FileNotFoundError,
            LatestNoteContractError,
            ProjectPathError,
            StorageError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "promotion":
        try:
            root = find_project_root(args.project_root)
            path = resolve_project_relative(root, str(args.file), field_name="promotion_input_file")
            payload = read_json(path)
            if not isinstance(payload, dict) or not isinstance(payload.get("intent"), dict):
                raise PromotionStrategyError("promotion file requires intent object")
            intent = PromotionIntent.from_dict(payload["intent"])
            if args.promotion_command == "intent-preview":
                print(json.dumps({"ok": True, "intent": intent.to_dict(), "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            draft = payload.get("strategy_draft")
            if not isinstance(draft, dict):
                raise PromotionStrategyError("strategy-preview requires strategy_draft object")
            strategy = build_promotion_strategy(intent=intent, draft=draft)
            print(json.dumps({"ok": True, "intent": intent.to_dict(), "strategy": strategy.to_dict(), "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
            return 0
        except (FileNotFoundError, ProjectPathError, PromotionStrategyError, StorageError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "run-agent":
        try:
            root = find_project_root(args.project_root)
            if args.run_agent_command == "status":
                status = RunAgentClient(root).status()
                print(json.dumps({"ok": True, "run_agent": status.to_dict(), "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "setup-guide":
                print(json.dumps({"ok": True, "setup": RunAgentClient(root).setup_guide(), "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "connection-check":
                result = RunAgentClient(root).connection_status()
                print(json.dumps({"ok": result["ready_for_login_check"], "connection": result}, ensure_ascii=False, indent=2))
                return 0 if result["ready_for_login_check"] else 2
            if args.run_agent_command == "enroll-extension-instance":
                result = RunAgentClient(root).enroll_current_extension_instance(
                    confirmation=args.confirm_enrollment
                )
                print(json.dumps({"ok": True, "extension_enrollment": result}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "enroll-platform-account":
                result = RunAgentClient(root).enroll_current_account_identity(
                    confirmation=args.confirm_account_enrollment
                )
                print(json.dumps({
                    "ok": True,
                    "platform_account_enrollment": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "readonly-uat-status":
                result = RunAgentClient(root).readonly_uat_status()
                print(json.dumps({"ok": True, "readonly_uat": result}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "inspect-current-like-control":
                result = RunAgentClient(root).inspect_current_like_control(args.note_id)
                print(json.dumps({
                    "ok": True,
                    "like_control": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "inspect-current-comment-controls":
                result = RunAgentClient(root).inspect_current_comment_controls(
                    args.note_id, comment_id=args.comment_id
                )
                print(json.dumps({
                    "ok": True,
                    "comment_controls": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "readonly-current-feed-detail":
                if not 1 <= args.max_comments <= 200:
                    raise RunAgentError("max-comments must be 1-200")
                result = RunAgentClient(root).get_current_feed_detail(
                    args.note_id, max_comment_items=args.max_comments
                )
                print(json.dumps({
                    "ok": True,
                    "detail": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "latest-account-note":
                if not 1 <= args.max_notes <= 30:
                    raise RunAgentError("max-notes must be 1-30")
                capture = RunAgentClient(root).capture_own_reply_history(
                    start_position=0,
                    max_notes=args.max_notes,
                    max_comment_items=1,
                )
                latest = select_latest_visible_profile_note(capture)
                intent = PromotionIntent.from_dict({
                    "mode": "account_note",
                    "source_id": latest["note_id"],
                    "source_ref": latest["source_ref"],
                    "title": latest["title"],
                    "body": latest["body"],
                    "brief": "",
                    "user_keywords": args.user_keyword,
                    "exclusions": args.exclusion,
                    "created_at": args.created_at,
                })
                print(json.dumps({
                    "ok": True,
                    "intent": intent.to_dict(),
                    "selection": {
                        "rule": "newest_published_timestamp_in_bounded_visible_profile_batch",
                        "captured_note_count": capture.get("captured_note_count"),
                        "available_note_count": capture.get("available_note_count"),
                        "published_at": latest["published_at"],
                    },
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "open-own-profile":
                result = RunAgentClient(root).open_own_profile()
                print(json.dumps({
                    "ok": True,
                    "navigation": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "open-commenter-profile":
                result = RunAgentClient(root).open_commenter_profile(
                    feed_id=args.feed_id,
                    comment_id=args.comment_id,
                    target_context_hash=args.target_context_hash,
                )
                print(json.dumps({
                    "ok": True,
                    "navigation": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "return-to-source-comment":
                result = RunAgentClient(root).return_to_source_comment(
                    feed_id=args.feed_id,
                    comment_id=args.comment_id,
                    target_context_hash=args.target_context_hash,
                )
                print(json.dumps({
                    "ok": True,
                    "navigation": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "open-dm-conversation":
                result = RunAgentClient(root).open_dm_conversation(
                    expected_peer_ref_hash=args.expected_peer_ref_hash,
                )
                print(json.dumps({
                    "ok": True,
                    "navigation": result,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "capture-current-dm-conversation":
                result = RunAgentClient(root).capture_current_dm_conversation(
                    account_id=args.account_id,
                    conversation_id=args.conversation_id,
                    expected_peer_ref_hash=args.expected_peer_ref_hash,
                    captured_at=args.captured_at,
                    max_messages=args.max_messages,
                )
                print(json.dumps({
                    "ok": True,
                    "dm_conversation": result["snapshot"],
                    "capture_evidence": result["capture_evidence"],
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "reconcile-unknown-write":
                result = RunAgentClient(root).reconcile_unknown_write(
                    attempt_id=args.attempt_id,
                    observed_outcome=args.observed_outcome,
                    evidence_ref=args.evidence_ref,
                    reconciled_at=args.reconciled_at,
                    confirmation=args.confirm_reconciliation,
                    note_id=args.note_id,
                )
                print(json.dumps({
                    "ok": True,
                    "reconciliation": result,
                    "local_state_written": True,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "readonly-page-context":
                result = RunAgentClient(root).page_context()
                print(json.dumps({"ok": True, "page_context": result}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "readonly-bind-active-tab":
                result = RunAgentClient(root).bind_active_xhs_tab()
                print(json.dumps({"ok": True, "tab_binding": result}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "readonly-list-tabs":
                result = RunAgentClient(root).list_xhs_tabs()
                print(json.dumps({"ok": True, "xhs_tabs": result}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "readonly-uat-authorize":
                result = RunAgentClient(root).authorize_readonly_uat(
                    confirmation=args.confirm_readonly_uat,
                    risk_override_confirmation=args.confirm_risk_override,
                    duration_seconds=args.duration_seconds,
                )
                print(json.dumps({"ok": True, "readonly_uat": result}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "readonly-uat-revoke":
                result = RunAgentClient(root).revoke_readonly_uat(
                    confirmation=args.confirm_revoke
                )
                print(json.dumps({"ok": True, "readonly_uat": result}, ensure_ascii=False, indent=2))
                return 0
            if args.run_agent_command == "readonly-uat-preflight":
                result = RunAgentClient(root).run_readonly_uat_preflight(
                    confirmation=args.confirm_readonly_uat,
                    risk_override_confirmation=args.confirm_risk_override,
                    duration_seconds=args.duration_seconds,
                )
                print(json.dumps({"ok": True, "readonly_uat_preflight": result}, ensure_ascii=False, indent=2))
                return 0
        except (
            FileNotFoundError,
            ProjectPathError,
            RunAgentError,
            LatestNoteContractError,
            PromotionStrategyError,
            PostEngagementError,
            EngageContractError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "interaction":
        try:
            root = find_project_root(args.project_root)
            if args.interaction_command == "session-compile-note-like":
                values = []
                for field, path in (
                    ("campaign", args.campaign),
                    ("note", args.note),
                    ("approval", args.approval),
                ):
                    value = read_json(resolve_project_relative(
                        root, str(path), field_name=f"compiled_note_like_{field}_file"
                    ))
                    if not isinstance(value, dict):
                        raise InteractionSessionError(
                            f"compiled note like {field} input must be an object"
                        )
                    values.append(value)
                compiled = compile_approved_note_like_plan(
                    campaign=Campaign.from_dict(values[0]),
                    note=NoteDetailCapture.from_dict(values[1]),
                    approval=NoteLikeApproval.from_dict(values[2]),
                    session_id=args.session_id,
                )
                project_config, _ = load_project_config(root)
                CompiledPlanStore(project_config.runtime.runtime_dir).record(
                    compiled, compiled_at=utc_now_iso()
                )
                output_path = None
                if args.output is not None:
                    output_path = resolve_project_relative(
                        root,
                        str(args.output),
                        field_name="compiled_note_like_output_file",
                    )
                    write_json_atomic(output_path, compiled.to_dict())
                print(json.dumps({
                    "ok": True,
                    "compiled_plan": compiled.to_dict(),
                    "compiled_plan_file": (
                        None if output_path is None else str(output_path.relative_to(root))
                    ),
                    "execution_ready": False,
                    "next_gate": "engage approve",
                    "local_provenance_written": True,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-compile-comment-like":
                campaign_payload = read_json(resolve_project_relative(
                    root, str(args.campaign), field_name="compiled_like_campaign_file"
                ))
                candidate_payload = read_json(resolve_project_relative(
                    root, str(args.candidate), field_name="compiled_like_candidate_file"
                ))
                request_payload = read_json(resolve_project_relative(
                    root,
                    str(args.post_engagement_request),
                    field_name="compiled_like_post_engagement_request_file",
                ))
                if not all(isinstance(item, dict) for item in (
                    campaign_payload, candidate_payload, request_payload
                )):
                    raise InteractionSessionError(
                        "compiled comment-like inputs must be objects"
                    )
                compiled = compile_comment_like_plan(
                    campaign=Campaign.from_dict(campaign_payload),
                    candidate=CandidateInteractionPlan.from_dict(candidate_payload),
                    post_engagement_request=PostEngagementRequest.from_dict(request_payload),
                    session_id=args.session_id,
                )
                project_config, _ = load_project_config(root)
                CompiledPlanStore(project_config.runtime.runtime_dir).record(
                    compiled, compiled_at=utc_now_iso()
                )
                output_path = None
                if args.output is not None:
                    output_path = resolve_project_relative(
                        root,
                        str(args.output),
                        field_name="compiled_comment_like_output_file",
                    )
                    write_json_atomic(output_path, compiled.to_dict())
                print(json.dumps({
                    "ok": True,
                    "compiled_plan": compiled.to_dict(),
                    "compiled_plan_file": (
                        None
                        if output_path is None
                        else str(output_path.relative_to(root))
                    ),
                    "execution_ready": False,
                    "next_gate": "engage approve",
                    "local_provenance_written": True,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-compile-note-comment":
                values = []
                for field, path in (
                    ("campaign", args.campaign),
                    ("note", args.note),
                    ("plan", args.plan),
                    ("approval", args.approval),
                ):
                    value = read_json(resolve_project_relative(
                        root, str(path), field_name=f"compiled_note_{field}_file"
                    ))
                    if not isinstance(value, dict):
                        raise InteractionSessionError(
                            f"compiled note {field} input must be an object"
                        )
                    values.append(value)
                campaign_value = Campaign.from_dict(values[0])
                note_value = NoteDetailCapture.from_dict(values[1])
                note_plan_value = NoteCommentPlan.from_dict(values[2])
                project_config, _ = load_project_config(root)
                style_profile_value = None
                if campaign_value.metadata.get("fixture_only") is not True:
                    style_profile_value = StyleProfileStore(
                        project_config.runtime.runtime_dir
                    ).load(campaign_value.account_id, missing_ok=True)
                compiled = compile_approved_note_comment_plan(
                    campaign=campaign_value,
                    note=note_value,
                    note_comment_plan=note_plan_value,
                    approval=NoteCommentApproval.from_dict(values[3]),
                    session_id=args.session_id,
                    style_profile=style_profile_value,
                    style_exception_ref=args.style_exception_ref,
                )
                CompiledPlanStore(project_config.runtime.runtime_dir).record(
                    compiled, compiled_at=utc_now_iso()
                )
                output_path = None
                if args.output is not None:
                    output_path = resolve_project_relative(
                        root,
                        str(args.output),
                        field_name="compiled_note_comment_output_file",
                    )
                    write_json_atomic(output_path, compiled.to_dict())
                print(json.dumps({
                    "ok": True,
                    "compiled_plan": compiled.to_dict(),
                    "compiled_plan_file": (
                        None if output_path is None else str(output_path.relative_to(root))
                    ),
                    "execution_ready": False,
                    "next_gate": "engage approve",
                    "local_provenance_written": True,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-compile-reply-plan":
                values = []
                for field, path in (
                    ("campaign", args.campaign),
                    ("candidate", args.candidate),
                    ("draft", args.draft),
                    ("approval", args.approval),
                ):
                    value = read_json(
                        resolve_project_relative(
                            root, str(path), field_name=f"compiled_{field}_file"
                        )
                    )
                    if not isinstance(value, dict):
                        raise ApprovedPlanError(f"compiled {field} input must be an object")
                    values.append(value)
                campaign_value = Campaign.from_dict(values[0])
                candidate_value = CandidateInteractionPlan.from_dict(values[1])
                project_config, _ = load_project_config(root)
                style_profile_value = None
                if campaign_value.metadata.get("fixture_only") is not True:
                    style_profile_value = StyleProfileStore(
                        project_config.runtime.runtime_dir
                    ).load(campaign_value.account_id, missing_ok=True)
                message_value = build_message_plan(
                    campaign=campaign_value,
                    candidate=candidate_value,
                    draft=values[2],
                    style_profile=style_profile_value,
                )
                discovery_value = build_discovery_plan(
                    campaign_value,
                    checked_at=message_value.checked_at,
                    promotion_strategy=_load_promotion_strategy(
                        root, args.promotion_file, campaign=campaign_value
                    ),
                )
                compiled = compile_approved_reply_plan(
                    campaign=campaign_value,
                    discovery_plan=discovery_value,
                    candidate=candidate_value,
                    message=message_value,
                    approval=MessageApproval.from_dict(values[3]),
                    result_index=args.result_index,
                    session_id=args.session_id,
                    style_exception_ref=args.style_exception_ref,
                )
                CompiledPlanStore(project_config.runtime.runtime_dir).record(
                    compiled, compiled_at=utc_now_iso()
                )
                if args.run_id:
                    QueryMetricsStore(project_config.runtime.runtime_dir).record(
                        campaign_id=campaign_value.campaign_id,
                        account_id=campaign_value.account_id,
                        run_id=args.run_id,
                        query_id=candidate_value.query_id,
                        kind="approval",
                        ref=compiled.plan.approval_ref,
                        occurred_at=MessageApproval.from_dict(values[3]).approved_at,
                    )
                output_path = None
                if args.output is not None:
                    output_path = resolve_project_relative(
                        root,
                        str(args.output),
                        field_name="compiled_comment_reply_output_file",
                    )
                    write_json_atomic(output_path, compiled.to_dict())
                print(json.dumps({
                    "ok": True,
                    "compiled_plan": compiled.to_dict(),
                    "compiled_plan_file": (
                        None if output_path is None else str(output_path.relative_to(root))
                    ),
                    "execution_ready": False,
                    "next_gate": "engage approve",
                    "local_provenance_written": True,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-plan-preview":
                payload = read_json(resolve_project_relative(root, str(args.file), field_name="current_page_plan_file"))
                if not isinstance(payload, dict):
                    raise InteractionSessionError("current-page plan must be an object")
                compiled_value = None
                if set(payload) == {
                    "compiler_version", "plan", "source_artifact_hashes", "compiler_hash"
                }:
                    compiled_value = CompiledCurrentPagePlan.from_dict(payload)
                    plan_value = compiled_value.plan
                elif isinstance(payload.get("compiled_plan"), dict):
                    compiled_value = CompiledCurrentPagePlan.from_dict(payload["compiled_plan"])
                    plan_value = compiled_value.plan
                elif isinstance(payload.get("plan"), dict):
                    plan_value = CurrentPageInteractionPlan.from_dict(payload["plan"])
                else:
                    plan_value = CurrentPageInteractionPlan.from_dict(payload)
                print(json.dumps({
                    "ok": True,
                    "plan": plan_value.to_dict(),
                    "compiled_provenance": None if compiled_value is None else {
                        "compiler_version": compiled_value.compiler_version,
                        "compiler_hash": compiled_value.compiler_hash,
                    },
                    "execution_ready": False,
                    "blockers": ([] if compiled_value is not None else ["untrusted_handwritten_plan"]),
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-approval-record":
                payload = read_json(resolve_project_relative(root, str(args.file), field_name="current_page_plan_file"))
                if not isinstance(payload, dict):
                    raise InteractionSessionError("current-page plan must be an object")
                if isinstance(payload.get("compiled_plan"), dict):
                    payload = payload["compiled_plan"]
                compiled_value = CompiledCurrentPagePlan.from_dict(payload)
                plan_value = compiled_value.plan
                project_config, _ = load_project_config(root)
                if not CompiledPlanStore(project_config.runtime.runtime_dir).matches(compiled_value):
                    raise InteractionSessionError(
                        "compiled plan provenance is missing or mismatched"
                    )
                digest = InteractionSessionStore(project_config.runtime.runtime_dir).record_approval(
                    plan_value,
                    confirmed_at=utc_now_iso(),
                    confirmation=args.confirm_approval,
                )
                print(json.dumps({"ok": True, "approval_ref": plan_value.approval_ref, "plan_hash": digest, "local_state_written": True, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-task-plan-approve":
                payload = read_json(resolve_project_relative(root, str(args.file), field_name="current_page_plan_file"))
                if not isinstance(payload, dict):
                    raise InteractionSessionError("current-page plan must be an object")
                if isinstance(payload.get("compiled_plan"), dict):
                    payload = payload["compiled_plan"]
                compiled_value = CompiledCurrentPagePlan.from_dict(payload)
                plan_value = compiled_value.plan
                project_config, _ = load_project_config(root)
                if not CompiledPlanStore(project_config.runtime.runtime_dir).matches(compiled_value):
                    raise InteractionSessionError(
                        "compiled plan provenance is missing or mismatched"
                    )
                task_store = CampaignTaskStore(project_config.runtime.runtime_dir)
                task_value = task_store.load(args.task_id)
                authorization = task_store.load_authorization(args.task_id)
                decision = evaluate_task_execution_authorization(
                    task_value,
                    authorization,
                    at=args.at,
                    account_id=plan_value.account_id,
                    campaign_id=plan_value.campaign_id,
                    required_actions=_current_page_action_names(plan_value),
                )
                if not decision["allowed"]:
                    raise InteractionSessionError(
                        "task does not authorize this exact plan: " + ",".join(decision["blockers"])
                    )
                store = InteractionSessionStore(project_config.runtime.runtime_dir)
                prepared = store.load_session(plan_value.session_id)
                if prepared.get("status") != "prepared" or prepared.get("note_id") != plan_value.note_id:
                    raise InteractionSessionError("plan does not match a prepared current-page session")
                if prepared.get("account_id") != plan_value.account_id:
                    raise InteractionSessionError(
                        "plan account does not match prepared search session"
                    )
                if prepared.get("campaign_id") and (
                    prepared.get("campaign_id") != plan_value.campaign_id
                ):
                    raise InteractionSessionError(
                        "plan Campaign does not match prepared search session"
                    )
                if prepared.get("strategy_pack_id"):
                    strategy_store = StrategyPackStore(
                        project_config.runtime.runtime_dir
                    )
                    strategy_pack = strategy_store.load(
                        str(prepared["strategy_pack_id"])
                    )
                    strategy_approval = strategy_store.load_approval(strategy_pack)
                    if (
                        strategy_pack.account_id != plan_value.account_id
                        or prepared.get("strategy_pack_hash")
                        != strategy_pack.content_hash
                        or prepared.get("strategy_approval_hash")
                        != strategy_approval.approval_hash
                        or prepared.get("query") not in {
                            str(row.get("query") or "")
                            for row in strategy_pack.strategy["queries"]
                        }
                    ):
                        raise InteractionSessionError(
                            "prepared search session StrategyPack binding is invalid"
                        )
                digest = store.record_task_plan_approval(
                    plan_value,
                    task_id=task_value.task_id,
                    authorization_id=authorization.authorization_id,
                    task_hash=task_value.content_hash,
                    confirmed_at=args.at,
                )
                print(json.dumps({
                    "ok": True,
                    "task_plan_approval": {
                        "task_id": task_value.task_id,
                        "authorization_id": authorization.authorization_id,
                        "plan_id": plan_value.plan_id,
                        "plan_hash": digest,
                    },
                    "execution_authorization": decision,
                    "local_state_written": True,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-status":
                project_config, _ = load_project_config(root)
                state = InteractionSessionStore(project_config.runtime.runtime_dir).load_session(args.session_id)
                print(json.dumps({"ok": bool(state), "session": state, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0 if state else 2
            if args.interaction_command == "session-adopt-current-results":
                query = args.query.strip()
                if not query or "\ufffd" in query or set(query) == {"?"}:
                    raise InteractionSessionError("query failed Unicode validation")
                metric_binding = (args.campaign_id, args.query_id, args.run_id)
                if any(metric_binding) and not all(metric_binding):
                    raise InteractionSessionError(
                        "campaign-id, query-id, and run-id are required together"
                    )
                project_config, _ = load_project_config(root)
                browser_config, selected = load_browser_config(root, args.browser_config)
                if not browser_config.allow_platform_access:
                    raise InteractionSessionError("browser config does not permit platform read")
                checked_at = utc_now_iso()
                if not load_calibration_status(project_config.runtime.runtime_dir, browser_config, checked_at=checked_at):
                    raise InteractionSessionError("valid login calibration is required")
                client = RunAgentClient(root)
                client.require_readonly_ready()
                try:
                    binding, candidate_ids, context = adopt_readonly_search_session(
                        port=client, query=query
                    )
                except (RunAgentError, InteractionSessionError) as exc:
                    signals = getattr(exc, "risk_signals", exc)
                    risk_class = (
                        RISK_CLASS_PLATFORM
                        if has_explicit_platform_risk(signals)
                        else RISK_CLASS_TECHNICAL
                    )
                    client.fail_closed_readonly_session(
                        stage="session_adopt",
                        event_code=(
                            "explicit_platform_risk_signal"
                            if risk_class == RISK_CLASS_PLATFORM
                            else "adopted_results_validation_failure"
                        ),
                        session_id=args.session_id,
                        risk_class=risk_class,
                    )
                    raise
                bound_tab_id = binding["boundTabId"]
                store = InteractionSessionStore(project_config.runtime.runtime_dir)
                store.start_session(
                    session_id=args.session_id,
                    account_id=browser_config.account_id,
                    query=query,
                    candidate_ids=candidate_ids,
                    bound_tab_id=bound_tab_id,
                    navigation_count=dict(context.get("navigationCount", {})),
                    campaign_id=args.campaign_id,
                    query_id=args.query_id,
                    run_id=args.run_id,
                    strategy_pack_id=args.strategy_pack_id,
                    strategy_pack_hash=args.strategy_pack_hash,
                    strategy_approval_hash=args.strategy_approval_hash,
                    session_origin="adopted_current_results",
                    search_count=1,
                    search_normalized_from_ai=context.get("search_normalized_from_ai") is True,
                )
                if args.campaign_id:
                    QueryMetricsStore(project_config.runtime.runtime_dir).record(
                        campaign_id=args.campaign_id,
                        account_id=browser_config.account_id,
                        run_id=args.run_id,
                        query_id=args.query_id,
                        kind="search",
                        ref=args.session_id,
                        occurred_at=checked_at,
                        count=len(candidate_ids),
                    )
                print(json.dumps({
                    "ok": True,
                    "session_id": args.session_id,
                    "browser_config": str(selected.relative_to(root)),
                    "query": query,
                    "search_count": 1,
                    "retyped_query": False,
                    "candidate_ids": candidate_ids,
                    "tab_binding": binding,
                    "page_context": context,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-start":
                query = args.query.strip()
                if not query or "\ufffd" in query or set(query) == {"?"}:
                    raise InteractionSessionError("query failed Unicode validation")
                metric_binding = (args.campaign_id, args.query_id, args.run_id)
                if any(metric_binding) and not all(metric_binding):
                    raise InteractionSessionError(
                        "campaign-id, query-id, and run-id are required together"
                    )
                project_config, _ = load_project_config(root)
                browser_config, selected = load_browser_config(root, args.browser_config)
                if not browser_config.allow_platform_access:
                    raise InteractionSessionError("browser config does not permit platform read")
                checked_at = utc_now_iso()
                if not load_calibration_status(project_config.runtime.runtime_dir, browser_config, checked_at=checked_at):
                    raise InteractionSessionError("valid login calibration is required")
                client = RunAgentClient(root)
                client.require_readonly_ready()
                try:
                    binding, candidate_ids, context = prepare_readonly_search_session(
                        port=client, query=query
                    )
                except (RunAgentError, InteractionSessionError) as exc:
                    signals = getattr(exc, "risk_signals", exc)
                    risk_class = (
                        RISK_CLASS_PLATFORM
                        if has_explicit_platform_risk(signals)
                        else RISK_CLASS_TECHNICAL
                    )
                    client.fail_closed_readonly_session(
                        stage="session_start",
                        event_code=(
                            "explicit_platform_risk_signal"
                            if risk_class == RISK_CLASS_PLATFORM
                            else "search_or_binding_failure"
                        ),
                        session_id=args.session_id,
                        risk_class=risk_class,
                    )
                    raise
                bound_tab_id = binding["boundTabId"]
                store = InteractionSessionStore(project_config.runtime.runtime_dir)
                store.start_session(
                    session_id=args.session_id,
                    account_id=browser_config.account_id,
                    query=query,
                    candidate_ids=candidate_ids,
                    bound_tab_id=bound_tab_id,
                    navigation_count=dict(context.get("navigationCount", {})),
                    campaign_id=args.campaign_id,
                    query_id=args.query_id,
                    run_id=args.run_id,
                    strategy_pack_id=args.strategy_pack_id,
                    strategy_pack_hash=args.strategy_pack_hash,
                    strategy_approval_hash=args.strategy_approval_hash,
                    session_origin="visible_search",
                    search_count=1,
                    search_normalized_from_ai=context.get("search_normalized_from_ai") is True,
                )
                if args.campaign_id:
                    QueryMetricsStore(project_config.runtime.runtime_dir).record(
                        campaign_id=args.campaign_id,
                        account_id=browser_config.account_id,
                        run_id=args.run_id,
                        query_id=args.query_id,
                        kind="search",
                        ref=args.session_id,
                        occurred_at=checked_at,
                        count=len(candidate_ids),
                    )
                print(json.dumps({"ok": True, "session_id": args.session_id, "browser_config": str(selected.relative_to(root)), "query": query, "search_count": 1, "candidate_ids": candidate_ids, "tab_binding": binding, "page_context": context, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-open-next":
                project_config, _ = load_project_config(root)
                store = InteractionSessionStore(project_config.runtime.runtime_dir)
                state = store.load_session(args.session_id)
                if state.get("status") != "active" or state.get("stage") != "search_results":
                    raise InteractionSessionError("session is not active on the search results page")
                candidate_ids = state.get("candidate_ids", [])
                if not isinstance(candidate_ids, list) or not candidate_ids:
                    raise InteractionSessionError("session has no search batch")
                next_index = state.get("next_index", 0)
                if args.result_index is not None and args.result_index != next_index:
                    raise InteractionSessionError(
                        "candidate index must equal the exact next saved result"
                    )
                result_index = next_index
                if type(result_index) is not int or result_index < 0:
                    raise InteractionSessionError("candidate index is outside the saved search batch")
                if result_index >= len(candidate_ids):
                    exhausted_at = utc_now_iso()
                    store.mark_search_exhausted(args.session_id, exhausted_at=exhausted_at)
                    metric_event_id = None
                    if state.get("campaign_id"):
                        metric_event_id = QueryMetricsStore(
                            project_config.runtime.runtime_dir
                        ).record(
                            campaign_id=str(state["campaign_id"]),
                            account_id=str(state["account_id"]),
                            run_id=str(state["run_id"]),
                            query_id=str(state["query_id"]),
                            kind="exhausted",
                            ref=args.session_id,
                            occurred_at=exhausted_at,
                        )
                    print(json.dumps({
                        "ok": True,
                        "session_id": args.session_id,
                        "search_count": 1,
                        "exhausted": True,
                        "metric_event_id": metric_event_id,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                note_id = str(candidate_ids[result_index])
                try:
                    unresolved = store.unresolved_targets.get(note_id)
                except UnresolvedTargetError:
                    store.mark_candidate_skipped(
                        args.session_id,
                        result_index=result_index,
                        note_id=note_id,
                        reason_code="invalid_saved_candidate_id",
                    )
                    print(json.dumps({
                        "ok": True,
                        "session_id": args.session_id,
                        "result_index": result_index,
                        "note_id": note_id,
                        "skipped": True,
                        "reason_code": "invalid_saved_candidate_id",
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                if unresolved is not None:
                    store.mark_candidate_skipped(
                        args.session_id,
                        result_index=result_index,
                        note_id=note_id,
                        reason_code="unresolved_prior_action_state",
                    )
                    print(json.dumps({
                        "ok": True,
                        "session_id": args.session_id,
                        "result_index": result_index,
                        "note_id": note_id,
                        "skipped": True,
                        "reason_code": "unresolved_prior_action_state",
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                client = RunAgentClient(root)
                client.require_readonly_ready()
                try:
                    opened = client.open_search_result(note_id)
                    if opened.get("pageType") != "note_detail" or str(opened.get("noteId", "")) != note_id:
                        raise InteractionSessionError("opened current page identity mismatch")
                    detail = client.get_current_feed_detail(note_id, max_comment_items=args.max_comments)
                    context = client.page_context()
                    risks = context.get("riskSignals", [])
                    if not isinstance(risks, list) or risks:
                        raise InteractionSessionError(
                            "opened current page contains risk or unknown state",
                            risk_signals=risks,
                        )
                    if context.get("boundTabId") != state.get("bound_tab_id"):
                        raise InteractionSessionError("candidate read changed the bound browser tab")
                except (RunAgentError, InteractionSessionError) as exc:
                    failed_at = utc_now_iso()
                    signals = getattr(exc, "risk_signals", exc)
                    risk_class = (
                        RISK_CLASS_PLATFORM
                        if has_explicit_platform_risk(signals)
                        else RISK_CLASS_TECHNICAL
                    )
                    if risk_class == RISK_CLASS_TECHNICAL:
                        try:
                            next_candidate_id = (
                                str(candidate_ids[result_index + 1])
                                if result_index + 1 < len(candidate_ids)
                                else ""
                            )
                            recovered = client.recover_search_results(
                                str(state.get("query", "")),
                                expected_candidate_id=next_candidate_id,
                            )
                            if recovered.get("boundTabId") != state.get("bound_tab_id"):
                                raise InteractionSessionError(
                                    "candidate recovery changed the bound browser tab"
                                )
                        except (RunAgentError, InteractionSessionError) as recovery_exc:
                            risk_class = (
                                RISK_CLASS_PLATFORM
                                if has_explicit_platform_risk(recovery_exc)
                                else RISK_CLASS_TECHNICAL
                            )
                            event_code = (
                                "explicit_platform_risk_signal"
                                if risk_class == RISK_CLASS_PLATFORM
                                else "candidate_recovery_failure"
                            )
                        else:
                            event_code = "candidate_unavailable_or_identity_failure"
                            client.record_readonly_risk_event(
                                stage="session_open_next",
                                event_code=event_code,
                                session_id=args.session_id,
                                risk_class=RISK_CLASS_TECHNICAL,
                            )
                            store.mark_candidate_skipped(
                                args.session_id,
                                result_index=result_index,
                                note_id=note_id,
                                reason_code=event_code,
                            )
                            store.mark_search_results(
                                args.session_id,
                                dict(recovered.get("navigationCount", {})),
                            )
                            print(json.dumps({
                                "ok": True,
                                "session_id": args.session_id,
                                "search_count": 1,
                                "result_index": result_index,
                                "note_id": note_id,
                                "skipped": True,
                                "reason_code": event_code,
                                "recovered_to_search_results": True,
                                "page_context": recovered,
                                "platform_actions_executed": 0,
                            }, ensure_ascii=False, indent=2))
                            return 0
                    else:
                        event_code = "explicit_platform_risk_signal"
                    store.mark_risk_stopped(
                        args.session_id,
                        event_code=event_code,
                        occurred_at=failed_at,
                    )
                    if state.get("campaign_id"):
                        QueryMetricsStore(project_config.runtime.runtime_dir).record(
                            campaign_id=str(state["campaign_id"]),
                            account_id=str(state["account_id"]),
                            run_id=str(state["run_id"]),
                            query_id=str(state["query_id"]),
                            kind="stop",
                            ref=args.session_id,
                            occurred_at=failed_at,
                            reason_code=event_code,
                        )
                    client.fail_closed_readonly_session(
                        stage="session_open_next",
                        event_code=event_code,
                        session_id=args.session_id,
                        risk_class=risk_class,
                    )
                    raise
                if state.get("campaign_id"):
                    comments = detail.get("comments") if isinstance(detail, dict) else None
                    if not isinstance(comments, list):
                        raise InteractionSessionError(
                            "metrics-bound note detail requires a visible comments list"
                        )
                    QueryMetricsStore(project_config.runtime.runtime_dir).record(
                        campaign_id=str(state["campaign_id"]),
                        account_id=str(state["account_id"]),
                        run_id=str(state["run_id"]),
                        query_id=str(state["query_id"]),
                        kind="note_open",
                        ref=note_id,
                        occurred_at=utc_now_iso(),
                        count=len(comments),
                    )
                store.mark_current_note(session_id=args.session_id, result_index=result_index, note_id=note_id, navigation_count=dict(context.get("navigationCount", {})))
                print(json.dumps({"ok": True, "session_id": args.session_id, "search_count": 1, "result_index": result_index, "note_id": note_id, "detail": detail, "page_context": context, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-continue-current":
                project_config, _ = load_project_config(root)
                store = InteractionSessionStore(project_config.runtime.runtime_dir)
                state = store.load_session(args.session_id)
                note_id = str(state.get("note_id", ""))
                reconciled_unknown = (
                    state.get("status") == "unknown"
                    and not store.unresolved_targets.is_unresolved(note_id)
                )
                safe_not_dispatched = store._safe_not_dispatched_recovery(state)
                if not note_id or (
                    state.get("status") not in {"verified", "completed", "prepared"}
                    and not reconciled_unknown
                    and not safe_not_dispatched
                ):
                    raise InteractionSessionError(
                        "session is not ready to continue the current note"
                    )
                client = RunAgentClient(root)
                client.require_readonly_ready()
                context = client.page_context()
                risks = context.get("riskSignals", [])
                if not isinstance(risks, list) or risks:
                    raise InteractionSessionError(
                        "current note contains risk or unknown state",
                        risk_signals=risks,
                    )
                if context.get("pageType") != "note_detail" or str(context.get("noteId", "")) != note_id:
                    raise InteractionSessionError("current note identity changed before continuation")
                if context.get("boundTabId") != state.get("bound_tab_id"):
                    raise InteractionSessionError("current note continuation changed the bound tab")
                detail = client.get_current_feed_detail(
                    note_id, max_comment_items=args.max_comments
                )
                after = client.page_context()
                after_risks = after.get("riskSignals", [])
                if (
                    not isinstance(after_risks, list)
                    or after_risks
                    or after.get("pageType") != "note_detail"
                    or str(after.get("noteId", "")) != note_id
                    or after.get("boundTabId") != state.get("bound_tab_id")
                ):
                    raise InteractionSessionError(
                        "current note changed during continuation read",
                        risk_signals=after_risks,
                    )
                continued = store.rearm_current_note(
                    args.session_id,
                    navigation_count=dict(after.get("navigationCount", {})),
                )
                print(json.dumps({
                    "ok": True,
                    "session_id": args.session_id,
                    "note_id": note_id,
                    "detail": detail,
                    "page_context": after,
                    "session_status": continued["status"],
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-return-results":
                project_config, _ = load_project_config(root)
                store = InteractionSessionStore(project_config.runtime.runtime_dir)
                state = store.load_session(args.session_id)
                query = str(state.get("query", ""))
                if not query:
                    raise InteractionSessionError("prepared session query is missing")
                client = RunAgentClient(root)
                client.require_readonly_ready()
                try:
                    context = client.go_back_and_verify(query)
                    if context.get("boundTabId") != state.get("bound_tab_id"):
                        raise InteractionSessionError(
                            "return to results changed the bound browser tab"
                        )
                except (RunAgentError, InteractionSessionError) as exc:
                    failed_at = utc_now_iso()
                    risk_class = (
                        RISK_CLASS_PLATFORM
                        if has_explicit_platform_risk(exc)
                        else RISK_CLASS_TECHNICAL
                    )
                    event_code = (
                        "explicit_platform_risk_signal"
                        if risk_class == RISK_CLASS_PLATFORM
                        else "return_results_failure"
                    )
                    store.mark_risk_stopped(
                        args.session_id,
                        event_code=event_code,
                        occurred_at=failed_at,
                    )
                    if state.get("campaign_id"):
                        QueryMetricsStore(project_config.runtime.runtime_dir).record(
                            campaign_id=str(state["campaign_id"]),
                            account_id=str(state["account_id"]),
                            run_id=str(state["run_id"]),
                            query_id=str(state["query_id"]),
                            kind="stop",
                            ref=args.session_id,
                            occurred_at=failed_at,
                            reason_code=event_code,
                        )
                    client.fail_closed_readonly_session(
                        stage="session_return_results",
                        event_code=event_code,
                        session_id=args.session_id,
                        risk_class=risk_class,
                    )
                    raise
                store.mark_search_results(args.session_id, dict(context.get("navigationCount", {})))
                print(json.dumps({"ok": True, "session_id": args.session_id, "page_context": context, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            if args.interaction_command == "session-execute-current":
                if args.confirm_current_page_interaction != CURRENT_PAGE_EXECUTION_CONFIRMATION:
                    raise InteractionSessionError("exact current-page interaction confirmation is required")
                if args.minimum_target_interval_seconds < 600:
                    raise InteractionSessionError(
                        "minimum-target-interval-seconds must be at least 600"
                    )
                payload = read_json(resolve_project_relative(root, str(args.file), field_name="current_page_plan_file"))
                if not isinstance(payload, dict):
                    raise InteractionSessionError("current-page plan must be an object")
                if isinstance(payload.get("compiled_plan"), dict):
                    payload = payload["compiled_plan"]
                compiled_value = CompiledCurrentPagePlan.from_dict(payload)
                plan_value = compiled_value.plan
                project_config, _ = load_project_config(root)
                if not CompiledPlanStore(project_config.runtime.runtime_dir).matches(compiled_value):
                    raise InteractionSessionError(
                        "compiled plan provenance is missing or mismatched"
                    )
                browser_config, _ = load_browser_config(root, args.browser_config)
                if browser_config.account_id != plan_value.account_id:
                    raise InteractionSessionError("browser account does not match current-page plan")
                if not browser_config.allow_platform_access:
                    raise InteractionSessionError("browser config does not permit platform actions")
                checked_at = utc_now_iso()
                if not load_calibration_status(project_config.runtime.runtime_dir, browser_config, checked_at=checked_at):
                    raise InteractionSessionError("valid login calibration is required")
                store = InteractionSessionStore(project_config.runtime.runtime_dir)
                if store.unresolved_targets.is_unresolved(plan_value.note_id):
                    raise InteractionSessionError(
                        "current-page target has unresolved prior action state"
                    )
                prepared = store.load_session(plan_value.session_id)
                if prepared.get("status") != "prepared" or prepared.get("note_id") != plan_value.note_id:
                    raise InteractionSessionError("plan does not match a prepared current-page session")
                action_type_limits = None
                daily_target_limit = None
                effective_daily_limit = args.daily_action_limit
                task_authorization = None
                budget_timezone = "UTC"
                execution_task_id = None
                execution_authorization_id = None
                execution_task_hash = None
                if args.task_id:
                    task_store = CampaignTaskStore(project_config.runtime.runtime_dir)
                    task_value = task_store.load(args.task_id)
                    authorization = task_store.load_authorization(args.task_id)
                    task_authorization = evaluate_task_execution_authorization(
                        task_value,
                        authorization,
                        at=checked_at,
                        account_id=plan_value.account_id,
                        campaign_id=plan_value.campaign_id,
                        required_actions=_current_page_action_names(plan_value),
                    )
                    if not task_authorization["allowed"]:
                        raise InteractionSessionError(
                            "task does not authorize this execution: "
                            + ",".join(task_authorization["blockers"])
                        )
                    if not store.task_plan_approval_matches(
                        plan_value,
                        task_id=task_value.task_id,
                        authorization_id=authorization.authorization_id,
                        task_hash=task_value.content_hash,
                    ):
                        raise InteractionSessionError("task-bound plan approval is missing or mismatched")
                    caps = task_value.daily_caps
                    action_type_limits = {
                        "like": caps["likes"],
                        "comment": caps["comments"],
                        "reply": caps["replies"],
                    }
                    daily_target_limit = caps["targets"]
                    budget_timezone = task_value.timezone
                    execution_task_id = task_value.task_id
                    execution_authorization_id = authorization.authorization_id
                    execution_task_hash = task_value.content_hash
                    effective_daily_limit = min(
                        args.daily_action_limit,
                        caps["likes"] + caps["comments"] + caps["replies"],
                    )
                elif not store.approval_matches(plan_value):
                    raise InteractionSessionError("current-page approval record is missing or mismatched")
                client = RunAgentClient(root)
                plan_hash = store.plan_hash(plan_value)
                _project_config, unified_state = _load_xhs_preflight_state(
                    root=root,
                    account_id=plan_value.account_id,
                    checked_at=checked_at,
                    browser_config_path=args.browser_config,
                )
                unified_request, unified_authorize, _lease = authorize_engage_action(
                    runtime_dir=project_config.runtime.runtime_dir,
                    plan=plan_value,
                    plan_hash=plan_hash,
                    checked_at=checked_at,
                    confirmation=args.confirm_bounded_write_uat,
                    expected_bound_tab_id=int(prepared.get("bound_tab_id")),
                    state=unified_state,
                    client=client,
                    daily_limit=effective_daily_limit,
                    minimum_interval_seconds=args.minimum_target_interval_seconds,
                    budget_timezone=budget_timezone,
                )
                try:
                    client.require_bounded_write_uat(
                        session_id=plan_value.session_id,
                        note_id=plan_value.note_id,
                        plan_hash=plan_hash,
                        branch=plan_value.branch.value,
                    )
                    # Finish local and transport preflight before the action
                    # executor starts platform-attempt accounting. A missing
                    # account enrollment is blocked, never an unknown write.
                    client.require_execution_ready()
                    unified_execute = require_engage_execution(
                        runtime_dir=project_config.runtime.runtime_dir,
                        request=unified_request,
                        state=unified_state,
                    )
                    result = execute_current_page_plan(
                        plan=plan_value,
                        port=client,
                        store=store,
                        run_id=f"current_page_{checked_at.replace(':', '').replace('+', '_')}",
                        created_at=checked_at,
                        daily_limit=effective_daily_limit,
                        minimum_interval_seconds=args.minimum_target_interval_seconds,
                        action_type_limits=action_type_limits,
                        daily_target_limit=daily_target_limit,
                        budget_timezone=budget_timezone,
                        task_id=execution_task_id,
                        authorization_id=execution_authorization_id,
                        task_hash=execution_task_hash,
                    )
                    unified_result = record_engage_result(
                        runtime_dir=project_config.runtime.runtime_dir,
                        request=unified_request,
                        result=result,
                        recorded_at=utc_now_iso(),
                    )
                finally:
                    client.revoke_bounded_write_uat()
                print(json.dumps({
                    "ok": result.ok,
                    "method": "ranfang_run_agent",
                    "task_authorization": task_authorization,
                    "unified_preflight": {
                        "authorize": unified_authorize.to_dict(),
                        "execute": unified_execute.to_dict(),
                        "result": unified_result,
                    },
                    "result": result.to_dict(),
                }, ensure_ascii=False, indent=2))
                return 0 if result.ok else 2
            if args.interaction_command.startswith("note-comment-"):
                plan_path = resolve_project_relative(root, str(args.file), field_name="note_comment_plan_file")
                payload = read_json(plan_path)
                if not isinstance(payload, dict) or not isinstance(payload.get("plan"), dict):
                    raise NoteCommentError("note comment plan file requires plan object")
                fixture_only = payload.get("fixture_only") is True
                plan_value = NoteCommentPlan.from_dict(payload["plan"])
                if args.interaction_command == "note-comment-preview":
                    print(json.dumps({"ok": True, "fixture_only": fixture_only, "plan": plan_value.to_dict(), "platform_actions_executed": 0, "next_gate": "note-comment-smoke-ready"}, ensure_ascii=False, indent=2))
                    return 0
                project_config, _ = load_project_config(root)
                browser_config, selected = load_browser_config(root, args.browser_config)
                if browser_config.account_id != plan_value.account_id:
                    raise NoteCommentError("browser account does not match note comment plan")
                checked_at = utc_now_iso()
                calibrated = load_calibration_status(project_config.runtime.runtime_dir, browser_config, checked_at=checked_at)
                store = NoteCommentStore(project_config.runtime.runtime_dir)
                gate = store.build_gate(plan=plan_value, checked_at=checked_at, login_ready=calibrated, platform_access_allowed=browser_config.allow_platform_access, daily_action_limit=args.daily_action_limit, minimum_target_interval_seconds=args.minimum_target_interval_seconds)
                blockers = list(gate.blockers())
                if fixture_only: blockers.append("fixture_plan_cannot_execute")
                output = {"ok": not blockers, "plan_id": plan_value.plan_id, "browser_config": str(selected.relative_to(root)), "gate": gate.to_dict(), "blockers": blockers, "platform_actions_executed": 0}
                if args.interaction_command == "note-comment-smoke-ready":
                    print(json.dumps(output, ensure_ascii=False, indent=2))
                    return 0 if not blockers else 2
                raise InteractionSessionError(
                    "legacy note-comment smoke execution is superseded by session-execute-current"
                )
            if args.interaction_command == "approval-record":
                raw_approval = read_json(
                    resolve_project_relative(root, str(args.file), field_name="approval_record_file")
                )
                if not isinstance(raw_approval, dict):
                    raise ApprovedPlanError("approval record input must be an object")
                approval_value = MessageApproval.from_dict(raw_approval)
                project_config, _ = load_project_config(root)
                digest = MessageApprovalStore(project_config.runtime.runtime_dir).record(
                    approval_value,
                    recorded_at=utc_now_iso(),
                    confirmation=args.confirm_approval,
                )
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "approval_id": approval_value.approval_id,
                            "approval_hash": digest,
                            "local_state_written": True,
                            "platform_actions_executed": 0,
                            "next_gate": "approved-plan-preview",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.interaction_command == "approved-plan-preview":
                values = []
                for field, path in (
                    ("campaign", args.campaign),
                    ("candidate", args.candidate),
                    ("draft", args.draft),
                    ("approval", args.approval),
                ):
                    value = read_json(
                        resolve_project_relative(root, str(path), field_name=f"approved_{field}_file")
                    )
                    if not isinstance(value, dict):
                        raise ApprovedPlanError(f"approved {field} input must be an object")
                    values.append(value)
                campaign_value = Campaign.from_dict(values[0])
                candidate_value = CandidateInteractionPlan.from_dict(values[1])
                message_value = build_message_plan(
                    campaign=campaign_value,
                    candidate=candidate_value,
                    draft=values[2],
                )
                discovery_value = build_discovery_plan(
                    campaign_value,
                    checked_at=message_value.checked_at,
                    promotion_strategy=_load_promotion_strategy(
                        root, args.promotion_file, campaign=campaign_value
                    ),
                )
                approval_value = MessageApproval.from_dict(values[3])
                bridge = build_approved_comment_plan(
                    campaign=campaign_value,
                    discovery_plan=discovery_value,
                    candidate=candidate_value,
                    message=message_value,
                    approval=approval_value,
                    result_index=args.result_index,
                )
                print(json.dumps({"ok": True, "approved_plan": bridge.to_dict()}, ensure_ascii=False, indent=2))
                return 0
            plan_path = resolve_project_relative(
                root, str(args.file), field_name="comment_plan_file"
            )
            payload = read_json(plan_path)
            if not isinstance(payload, dict):
                raise CommentFlowContractError("comment plan root must be an object")
            fixture_only = payload.get("fixture_only") is True
            raw_plan = payload.get("plan")
            if not isinstance(raw_plan, dict):
                raise CommentFlowContractError("comment plan file requires plan object")
            plan_value = CommentInteractionPlan.from_dict(raw_plan)
            if args.interaction_command == "comment-preview":
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "fixture_only": fixture_only,
                            "plan": plan_value.to_dict(),
                            "platform_actions_executed": 0,
                            "next_gate": "comment-smoke-ready",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            project_config, _ = load_project_config(root)
            browser_config, selected = load_browser_config(root, args.browser_config)
            checked_at = utc_now_iso()
            calibrated = load_calibration_status(
                project_config.runtime.runtime_dir,
                browser_config,
                checked_at=checked_at,
            )
            store = CommentActionStore(project_config.runtime.runtime_dir)
            gate = store.build_gate(
                account_id=plan_value.account_id,
                target=plan_value.target,
                checked_at=checked_at,
                login_ready=calibrated,
                platform_access_allowed=browser_config.allow_platform_access,
                daily_action_limit=args.daily_action_limit,
                minimum_target_interval_seconds=args.minimum_target_interval_seconds,
            )
            blockers = list(gate.blockers())
            if not MessageApprovalStore(project_config.runtime.runtime_dir).matches(plan_value):
                blockers.append("message_approval_record_missing_or_mismatch")
            if fixture_only:
                blockers.append("fixture_plan_cannot_execute")
            output = {
                "ok": not blockers,
                "fixture_only": fixture_only,
                "plan_id": plan_value.plan_id,
                "browser_config": str(selected.relative_to(root)),
                "gate": gate.to_dict(),
                "blockers": blockers,
                "platform_actions_executed": 0,
                "next_gate": "explicit_user_confirmation" if not blockers else "resolve_blockers",
            }
            if args.interaction_command == "comment-smoke-ready":
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 0 if not blockers else 2
            raise InteractionSessionError(
                "legacy comment smoke execution is superseded by session-execute-current"
            )
        except (
            BrowserConfigError,
            ApprovedPlanError,
            CampaignContractError,
            CandidateAssessmentError,
            CommentFlowContractError,
            ConfigError,
            FileNotFoundError,
            ProjectPathError,
            StorageError,
            DiscoveryPlanError,
            MessagePlanError,
            StyleProfileError,
            NoteCommentError,
            InteractionSessionError,
            EngageContractError,
            TaskContractError,
            UnresolvedTargetError,
            RunAgentError,
            LiveInteractionError,
            XhsCallMethodLockedError,
            PromotionStrategyError,
            PostEngagementError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "discovery":
        try:
            root = find_project_root(args.project_root)
            if args.discovery_command == "candidate-preview":
                campaign_path = resolve_project_relative(
                    root, str(args.campaign), field_name="candidate_campaign_file"
                )
                thread_path = resolve_project_relative(
                    root, str(args.thread), field_name="candidate_thread_file"
                )
                assessment_path = resolve_project_relative(
                    root, str(args.file), field_name="candidate_assessment_file"
                )
                campaign_payload = read_json(campaign_path)
                thread_payload = read_json(thread_path)
                assessment_payload = read_json(assessment_path)
                if not all(
                    isinstance(item, dict)
                    for item in (campaign_payload, thread_payload, assessment_payload)
                ):
                    raise CandidateAssessmentError("candidate preview inputs must be objects")
                checked_at = assessment_payload.get("checked_at")
                if not isinstance(checked_at, str) or not checked_at.strip():
                    raise CandidateAssessmentError("checked_at is required")
                campaign_value = Campaign.from_dict(campaign_payload)
                discovery_value = build_discovery_plan(
                    campaign_value,
                    checked_at=checked_at,
                    promotion_strategy=_load_promotion_strategy(
                        root, args.promotion_file, campaign=campaign_value
                    ),
                )
                thread_value = build_visible_thread_snapshot_from_dict(
                    thread_payload.get("thread")
                )
                query_index = assessment_payload.get("query_index")
                target_order = assessment_payload.get("target_visible_order")
                if type(query_index) is not int or not 0 <= query_index < len(discovery_value.queries):
                    raise CandidateAssessmentError("query_index is invalid")
                if type(target_order) is not int or not 0 <= target_order < len(thread_value.comments):
                    raise CandidateAssessmentError("target_visible_order is invalid")
                raw_evidence = assessment_payload.get("evidence")
                if not isinstance(raw_evidence, list):
                    raise CandidateAssessmentError("evidence must be a list")
                allowed_assessment_fields = {
                    "fixture_only",
                    "checked_at",
                    "query_index",
                    "target_visible_order",
                    "location_status",
                    "minor_risk",
                    "commercial_ad",
                    "previously_contacted",
                    "opt_out",
                    "evidence",
                    "run_id",
                }
                unknown_assessment_fields = set(assessment_payload) - allowed_assessment_fields
                if unknown_assessment_fields:
                    raise CandidateAssessmentError(
                        f"unknown candidate assessment fields: {sorted(unknown_assessment_fields)}"
                    )
                for boolean_name in (
                    "fixture_only",
                    "minor_risk",
                    "commercial_ad",
                    "previously_contacted",
                    "opt_out",
                ):
                    if type(assessment_payload.get(boolean_name)) is not bool:
                        raise CandidateAssessmentError(f"{boolean_name} must be a boolean")
                query_value = discovery_value.queries[query_index]
                result = assess_comment_candidate(
                    discovery_plan=discovery_value,
                    thread=thread_value,
                    target=thread_value.comments[target_order],
                    query_id=query_value.query_id,
                    segment_id=query_value.segment_id,
                    evidence=tuple(CandidateEvidence.from_dict(item) for item in raw_evidence),
                    location_status=assessment_payload.get("location_status"),
                    minor_risk=assessment_payload["minor_risk"],
                    commercial_ad=assessment_payload["commercial_ad"],
                    previously_contacted=assessment_payload["previously_contacted"],
                    opt_out=assessment_payload["opt_out"],
                )
                run_id = assessment_payload.get("run_id")
                if run_id is not None:
                    if not isinstance(run_id, str) or not run_id.strip():
                        raise CandidateAssessmentError("run_id must be a non-empty string")
                    project_config, _ = load_project_config(root)
                    QueryMetricsStore(project_config.runtime.runtime_dir).record(
                        campaign_id=campaign_value.campaign_id,
                        account_id=campaign_value.account_id,
                        run_id=run_id,
                        query_id=query_value.query_id,
                        kind="candidate",
                        ref=result.candidate_id,
                        occurred_at=checked_at,
                        level=result.evidence_level,
                    )
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "fixture_only": campaign_value.metadata.get("fixture_only") is True,
                            "candidate": result.to_dict(),
                            "platform_actions_executed": 0,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            input_path = resolve_project_relative(
                root, str(args.file), field_name="discovery_campaign_file"
            )
            payload = read_json(input_path)
            if not isinstance(payload, dict):
                raise CampaignContractError("discovery campaign root must be an object")
            campaign_value = Campaign.from_dict(payload)
            plan_value = build_discovery_plan(
                campaign_value,
                checked_at=args.checked_at,
                promotion_strategy=_load_promotion_strategy(
                    root, args.promotion_file, campaign=campaign_value
                ),
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "fixture_only": campaign_value.metadata.get("fixture_only") is True,
                        "plan": plan_value.to_dict(),
                        "platform_actions_executed": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except (
            CampaignContractError,
            CandidateAssessmentError,
            DiscoveryPlanError,
            FileNotFoundError,
            ProjectPathError,
            StorageError,
            PromotionStrategyError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "message":
        try:
            root = find_project_root(args.project_root)
            campaign_payload = read_json(
                resolve_project_relative(root, str(args.campaign), field_name="message_campaign_file")
            )
            candidate_payload = read_json(
                resolve_project_relative(root, str(args.candidate), field_name="message_candidate_file")
            )
            draft_payload = read_json(
                resolve_project_relative(root, str(args.draft), field_name="message_draft_file")
            )
            if not all(isinstance(item, dict) for item in (campaign_payload, candidate_payload, draft_payload)):
                raise MessagePlanError("message preview inputs must be objects")
            if set(candidate_payload) == {"candidate"}:
                candidate_payload = candidate_payload["candidate"]
            if not isinstance(candidate_payload, dict):
                raise MessagePlanError("candidate payload must be an object")
            campaign_value = Campaign.from_dict(campaign_payload)
            candidate_value = CandidateInteractionPlan.from_dict(candidate_payload)
            style_values = (
                args.style_history_capture,
                args.style_consent_ref,
                args.style_captured_at,
                args.style_profile_created_at,
            )
            if any(item is not None for item in style_values) and not all(item is not None for item in style_values):
                raise MessagePlanError("all style profile preview arguments are required together")
            style_profile_value = None
            if args.style_history_capture is not None:
                style_capture = read_json(
                    resolve_project_relative(
                        root,
                        str(args.style_history_capture),
                        field_name="message_style_history_capture",
                    )
                )
                if not isinstance(style_capture, dict):
                    raise MessagePlanError("style history capture must be an object")
                style_snapshot = build_style_history_snapshot(
                    account_id=campaign_value.account_id,
                    consent_ref=args.style_consent_ref,
                    captured_at=args.style_captured_at,
                    capture=style_capture,
                )
                style_profile_value = build_reply_style_profile(
                    style_snapshot,
                    created_at=args.style_profile_created_at,
                )
            elif campaign_value.metadata.get("fixture_only") is not True:
                project_config, _ = load_project_config(root)
                style_profile_value = StyleProfileStore(
                    project_config.runtime.runtime_dir
                ).load(campaign_value.account_id, missing_ok=True)
            plan_value = build_message_plan(
                campaign=campaign_value,
                candidate=candidate_value,
                draft=draft_payload,
                style_profile=style_profile_value,
            )
            if args.run_id:
                project_config, _ = load_project_config(root)
                QueryMetricsStore(project_config.runtime.runtime_dir).record(
                    campaign_id=campaign_value.campaign_id,
                    account_id=campaign_value.account_id,
                    run_id=args.run_id,
                    query_id=candidate_value.query_id,
                    kind="message",
                    ref=plan_value.message_plan_id,
                    occurred_at=plan_value.checked_at,
                    outcome="valid" if plan_value.validation.ok else "blocked",
                )
            print(
                json.dumps(
                    {
                        "ok": plan_value.validation.ok,
                        "fixture_only": campaign_value.metadata.get("fixture_only") is True,
                        "message_plan": plan_value.to_dict(),
                        "platform_actions_executed": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if plan_value.validation.ok else 2
        except (
            CampaignContractError,
            CandidateAssessmentError,
            MessagePlanError,
            StyleHistoryError,
            StyleProfileError,
            FileNotFoundError,
            ProjectPathError,
            StorageError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "loop":
        try:
            root = find_project_root(args.project_root)
            if args.loop_command in {"post-preview", "post-record"}:
                payload = read_json(
                    resolve_project_relative(
                        root, str(args.file), field_name="post_engagement_file"
                    )
                )
                if not isinstance(payload, dict):
                    raise PostEngagementError("post engagement file must contain an object")
                request = PostEngagementRequest.from_dict(payload)
                plan = build_post_engagement_plan(request)
                output = {
                    "ok": True,
                    "post_engagement_plan": plan.to_dict(),
                }
                if args.loop_command == "post-record":
                    project_config, _ = load_project_config(root)
                    output["lead_persistence"] = LeadRecordStore(
                        project_config.runtime.runtime_dir
                    ).persist_post_engagement_plan(plan)
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 0
            plan_value = _compile_daily_plan(
                root,
                args.campaign,
                args.file,
                args.promotion_file,
            )
            project_config, _ = load_project_config(root)
            heartbeat = HeartbeatStateStore(project_config.runtime.runtime_dir)
            if args.loop_command == "daily-preview":
                print(json.dumps({"ok": True, "daily_plan": plan_value.to_dict()}, ensure_ascii=False, indent=2))
                return 0
            if args.loop_command == "heartbeat-init":
                state = heartbeat.initialize(plan_value)
                print(json.dumps({"ok": True, "state": state, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            if args.loop_command == "heartbeat-claim":
                approved_bridges: dict[str, str] = {}
                approval_store = MessageApprovalStore(project_config.runtime.runtime_dir)
                for index, approved_path in enumerate(args.approved_plan):
                    raw_bridge = read_json(
                        resolve_project_relative(
                            root, str(approved_path), field_name=f"heartbeat_approved_plan_{index}"
                        )
                    )
                    if isinstance(raw_bridge, dict) and set(raw_bridge) == {"approved_plan"}:
                        raw_bridge = raw_bridge["approved_plan"]
                    if not isinstance(raw_bridge, dict) or not isinstance(raw_bridge.get("comment_plan"), dict):
                        raise LoopPlanError("approved plan receipt is invalid")
                    comment_plan = CommentInteractionPlan.from_dict(raw_bridge["comment_plan"])
                    bridge_id = raw_bridge.get("bridge_id")
                    if bridge_id != comment_plan.bridge_id or not approval_store.matches(comment_plan):
                        raise LoopPlanError("approved plan has no matching local approval record")
                    queue_item = next(
                        (
                            item for item in plan_value.interaction_queue
                            if item.candidate_id == comment_plan.target.candidate_id
                            and item.message_plan_id == comment_plan.message_plan_id
                            and item.note_id == comment_plan.target.note_id
                            and item.target_comment_id == comment_plan.target.target_comment_id
                        ),
                        None,
                    )
                    if queue_item is None:
                        raise LoopPlanError("approved plan does not belong to DailyPlan queue")
                    approved_bridges[queue_item.item_id] = str(bridge_id)
                decision = heartbeat.claim_one(
                    plan_value,
                    now=args.now,
                    worker_id=args.worker_id,
                    approved_bridges=approved_bridges,
                )
                print(json.dumps({"ok": True, "heartbeat": decision.to_dict()}, ensure_ascii=False, indent=2))
                return 0
            state = heartbeat.complete(
                plan_value,
                item_id=args.item_id,
                lease_token=args.lease_token,
                outcome=args.outcome,
                completed_at=args.completed_at,
                blockers=tuple(args.blocker),
            )
            print(json.dumps({"ok": True, "state": state, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
            return 0
        except (
            CampaignContractError,
            CandidateAssessmentError,
            DiscoveryPlanError,
            MessagePlanError,
            LoopPlanError,
            PostEngagementError,
            LeadStoreError,
            FileNotFoundError,
            ProjectPathError,
            StorageError,
            PromotionStrategyError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "style":
        try:
            root = find_project_root(args.project_root)
            project_config, _ = load_project_config(root)
            corpus_store = ReplyCorpusStore(project_config.runtime.runtime_dir)
            if args.style_command == "learn-from-account":
                if args.confirm_own_profile != RUN_AGENT_LOGIN_CONFIRMATION:
                    raise BrowserLoginError("exact own-profile login confirmation is required")
                browser_config, selected = load_browser_config(root, args.browser_config)
                if not browser_config.allow_platform_access or browser_config.fixture_only:
                    raise StyleHistoryError("browser config does not permit account history learning")
                account_setup = AccountSetupStore(project_config.runtime.runtime_dir).load()
                if account_setup.account_id != browser_config.account_id:
                    raise StyleHistoryError("account setup does not match browser account")
                if not account_setup.raw_reply_corpus_enabled:
                    raise StyleHistoryError("account setup does not permit a local raw reply corpus")
                client = RunAgentClient(root)
                navigation = client.open_own_profile()
                result = client.capture_own_reply_history(
                    start_position=args.start_position,
                    max_notes=args.max_notes,
                    max_comment_items=args.max_comments_per_note,
                )
                capture = result.get("capture")
                platform_user_id = result.get("account_user_id")
                if not isinstance(capture, dict) or not isinstance(platform_user_id, str):
                    raise StyleHistoryError("Run Agent own reply history result is incomplete")
                identity_hash = __import__("hashlib").sha256(platform_user_id.encode("utf-8")).hexdigest()
                identity_path = project_config.runtime.runtime_dir / "setup" / "platform_identity.json"
                existing_identity = read_json(identity_path, default=None)
                if existing_identity is not None and (
                    not isinstance(existing_identity, dict)
                    or existing_identity.get("account_id") != browser_config.account_id
                    or existing_identity.get("platform_identity_hash") != identity_hash
                ):
                    raise StyleHistoryError("current Xiaohongshu profile differs from the enrolled account")
                write_json_atomic(identity_path, {
                    "account_id": browser_config.account_id,
                    "platform_identity_hash": identity_hash,
                    "enrolled_at": args.captured_at,
                    "provider": "ranfang_run_agent",
                })
                receipt = record_run_agent_login_calibration(
                    runtime_dir=project_config.runtime.runtime_dir,
                    config=browser_config,
                    platform_user_id=platform_user_id,
                    confirmation=args.confirm_own_profile,
                    verified_at=args.captured_at,
                )
                snapshot = build_style_history_snapshot(
                    account_id=browser_config.account_id,
                    consent_ref=args.consent_ref,
                    captured_at=args.captured_at,
                    capture=capture,
                    max_pages=1,
                    max_notes=args.max_notes,
                    max_comments_per_note=args.max_comments_per_note,
                )
                post_profile = PostVoiceStore(
                    project_config.runtime.runtime_dir
                ).upsert(snapshot, created_at=args.created_at)
                corpus_metadata = None
                if snapshot.owned_reply_sample_ids:
                    corpus = build_reply_corpus(
                        snapshot,
                        created_at=args.created_at,
                        confirmation=args.confirm_local_corpus,
                    )
                    _, corpus = corpus_store.upsert(corpus)
                    corpus_metadata = corpus.metadata()
                profile_payload = None
                if corpus_metadata is not None and corpus.entry_count >= 2:
                    profile = build_reply_style_profile_from_corpus(
                        corpus, created_at=args.created_at
                    )
                    StyleProfileStore(project_config.runtime.runtime_dir).save(profile)
                    profile_payload = profile.to_dict()
                progress = {
                    "account_id": browser_config.account_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_hash": snapshot.content_hash,
                    "captured_at": args.captured_at,
                    "start_position": args.start_position,
                    "captured_note_count": len(snapshot.notes),
                    "owned_reply_count": len(snapshot.owned_reply_sample_ids),
                    "owned_post_count": post_profile.sample_count,
                    "has_more": snapshot.has_more,
                    "next_note_position": snapshot.next_note_position,
                    "learning_status": (
                        "ready" if profile_payload is not None
                        else "continue_required" if snapshot.has_more
                        else "insufficient_owned_replies"
                    ),
                    "platform_actions_executed": 0,
                }
                append_jsonl(project_config.runtime.runtime_dir / "style" / "history_runs.jsonl", progress)
                account_voice = build_account_voice_status(
                    project_config.runtime.runtime_dir,
                    account_id=browser_config.account_id,
                )
                print(json.dumps({
                    "ok": True,
                    "browser_config": str(selected.relative_to(root)),
                    "login_calibration": receipt.to_dict(),
                    "navigation": navigation,
                    "learning": progress,
                    "reply_corpus": corpus_metadata,
                    "style_profile": profile_payload,
                    "post_voice": post_profile.to_dict(),
                    "account_voice": account_voice,
                    "raw_history_returned": False,
                    "raw_post_history_returned": False,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.style_command == "corpus-status":
                corpus = corpus_store.load(args.account_id)
                print(json.dumps({"ok": True, "reply_corpus": corpus.metadata()}, ensure_ascii=False, indent=2))
                return 0
            if args.style_command == "corpus-search":
                corpus = corpus_store.load(args.account_id)
                results = corpus.search(args.query, limit=args.limit)
                print(json.dumps({
                    "ok": True,
                    "account_id": corpus.account_id,
                    "query": args.query,
                    "results": list(results),
                    "result_count": len(results),
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.style_command == "corpus-delete":
                deleted = corpus_store.delete(
                    args.account_id,
                    confirmation=args.confirm_delete,
                )
                print(json.dumps({
                    "ok": True,
                    "account_id": args.account_id,
                    "deleted": deleted,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            capture = read_json(
                resolve_project_relative(root, str(args.file), field_name="style_history_capture")
            )
            if not isinstance(capture, dict):
                raise StyleHistoryError("style history capture must be an object")
            snapshot = build_style_history_snapshot(
                account_id=args.account_id,
                consent_ref=args.consent_ref,
                captured_at=args.captured_at,
                capture=capture,
                max_pages=getattr(args, "max_pages", 5),
                max_notes=getattr(args, "max_notes", 30),
                max_comments_per_note=getattr(args, "max_comments_per_note", 100),
            )
            if args.style_command in {"profile-preview", "profile-build"}:
                profile = build_reply_style_profile(snapshot, created_at=args.created_at)
                if args.style_command == "profile-build":
                    path = StyleProfileStore(project_config.runtime.runtime_dir).save(profile)
                    print(json.dumps({
                        "ok": True,
                        "style_profile": profile.to_dict(),
                        "storage_ref": str(path.relative_to(project_config.runtime.runtime_dir)),
                        "local_state_written": True,
                        "platform_actions_executed": 0,
                    }, ensure_ascii=False, indent=2))
                    return 0
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "style_profile": profile.to_dict(),
                            "platform_actions_executed": 0,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.style_command == "corpus-build":
                corpus = build_reply_corpus(
                    snapshot,
                    created_at=args.created_at,
                    confirmation=args.confirm_local_corpus,
                )
                path, corpus = corpus_store.upsert(corpus)
                print(json.dumps({
                    "ok": True,
                    "reply_corpus": corpus.metadata(),
                    "storage_ref": str(path.relative_to(project_config.runtime.runtime_dir)),
                    "local_state_written": True,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            print(json.dumps({"ok": True, "style_history": snapshot.to_dict()}, ensure_ascii=False, indent=2))
            return 0
        except (
            StyleHistoryError,
            StyleProfileError,
            OnboardingError,
            BrowserConfigError,
            BrowserLoginError,
            RunAgentError,
            FileNotFoundError,
            ProjectPathError,
            StorageError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "review":
        try:
            if args.review_command == "status":
                root = find_project_root(args.project_root)
                project_config, _ = load_project_config(root)
                ledger = OperationLedgerStore(project_config.runtime.runtime_dir)
                print(json.dumps({
                    "ok": True,
                    "capability": public_group_status("review"),
                    "operation_ledger": ledger.status(account_id=args.account_id),
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            if args.review_command in {"list", "export"}:
                root = find_project_root(args.project_root)
                project_config, _ = load_project_config(root)
                ledger = OperationLedgerStore(project_config.runtime.runtime_dir)
                query = OperationLedgerQuery(
                    account_id=args.account_id,
                    workflow=args.workflow,
                    status=args.status,
                    since=args.since,
                    until=args.until,
                    limit=args.limit,
                )
                payload = ledger.query(query) if args.review_command == "list" else ledger.export(query)
                output_path = ""
                if args.review_command == "export" and args.output is not None:
                    target = resolve_project_relative(
                        root,
                        str(args.output),
                        field_name="operation_ledger_export",
                    )
                    write_json_atomic(target, payload)
                    output_path = target.relative_to(root).as_posix()
                print(json.dumps({
                    "ok": True,
                    "operation_ledger": payload,
                    "output_path": output_path,
                    "platform_actions_executed": 0,
                }, ensure_ascii=False, indent=2))
                return 0
            root = find_project_root(args.project_root)
            plan_value = _compile_daily_plan(
                root,
                args.campaign,
                args.file,
                args.promotion_file,
            )
            project_config, _ = load_project_config(root)
            state = HeartbeatStateStore(project_config.runtime.runtime_dir).load(plan_value)
            records = [
                *CommentActionStore(project_config.runtime.runtime_dir).records(),
                *DMRuntimeStore(project_config.runtime.runtime_dir).records(),
            ]
            lead_store = LeadRecordStore(project_config.runtime.runtime_dir)
            try:
                review_timezone = AccountSetupStore(
                    project_config.runtime.runtime_dir
                ).load().timezone
            except OnboardingError:
                review_timezone = "UTC"
            if args.metrics is None:
                query_runs = QueryMetricsStore(
                    project_config.runtime.runtime_dir
                ).aggregate_daily(
                    campaign_id=plan_value.campaign_id,
                    account_id=plan_value.account_id,
                    plan_date=plan_value.plan_date,
                    timezone_name=review_timezone,
                    query_ids={item.query_id for item in plan_value.search_slots},
                )
                metrics_source = "local_evidence_journal"
            else:
                metrics_payload = read_json(resolve_project_relative(
                    root, str(args.metrics), field_name="daily_review_metrics"
                ))
                if not isinstance(metrics_payload, dict) or set(metrics_payload) != {"query_runs"}:
                    raise ReviewError("daily review metrics must contain only query_runs")
                raw_runs = metrics_payload["query_runs"]
                if not isinstance(raw_runs, list):
                    raise ReviewError("daily review query_runs must be a list")
                query_runs = [QueryRunMetrics.from_dict(item) for item in raw_runs]
                metrics_source = "external_review_file"
            daily_lead_summary = lead_store.summary(
                plan_value.campaign_id,
                plan_date=plan_value.plan_date,
                timezone_name=review_timezone,
            )
            cumulative_lead_summary = lead_store.summary(plan_value.campaign_id)
            result = build_daily_review(
                plan=plan_value,
                heartbeat_state=state,
                query_runs=query_runs,
                action_records=records,
                checked_at=args.checked_at,
                lead_summary=daily_lead_summary,
                timezone_name=review_timezone,
                unified_action_results=read_jsonl(
                    project_config.runtime.runtime_dir
                    / "action_preflight"
                    / "results.jsonl"
                ),
                service_queue_summary=ServiceQueueStore(
                    project_config.runtime.runtime_dir
                ).status(account_id=plan_value.account_id),
            )
            review_payload = result.to_dict()
            review_payload["cumulative_lead_funnel"] = cumulative_lead_summary
            review_payload["metrics_source"] = metrics_source
            print(json.dumps({"ok": True, "daily_review": review_payload}, ensure_ascii=False, indent=2))
            return 0
        except (
            CampaignContractError,
            CandidateAssessmentError,
            DiscoveryPlanError,
            MessagePlanError,
            LoopPlanError,
            ReviewError,
            OperationLedgerError,
            LeadStoreError,
            FileNotFoundError,
            ProjectPathError,
            StorageError,
            PromotionStrategyError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "dm":
        try:
            root = find_project_root(args.project_root)
            if args.dm_command == "conversation-preview":
                capture = read_json(
                    resolve_project_relative(root, str(args.file), field_name="dm_conversation_capture")
                )
                if not isinstance(capture, dict):
                    raise DMContractError("DM conversation capture must be an object")
                snapshot = build_dm_conversation_snapshot(
                    account_id=args.account_id, captured_at=args.captured_at, capture=capture
                )
                print(json.dumps({"ok": True, "dm_conversation": snapshot.to_dict()}, ensure_ascii=False, indent=2))
                return 0
            campaign_value, snapshot, plan = _compile_dm_plan(
                root, args.campaign, args.conversation, args.draft, args.captured_at
            )
            if args.dm_command == "message-preview":
                print(json.dumps({"ok": plan.validation_ok, "dm_message_plan": plan.to_dict()}, ensure_ascii=False, indent=2))
                return 0 if plan.validation_ok else 2
            approval_raw = read_json(
                resolve_project_relative(root, str(args.approval), field_name="dm_approval_file")
            )
            if not isinstance(approval_raw, dict):
                raise DMContractError("DM approval input must be an object")
            approval = DMSingleApproval.from_dict(approval_raw)
            approved = build_approved_dm_plan(
                message_plan=plan,
                approval=approval,
                fixture_only=campaign_value.metadata.get("fixture_only") is True,
            )
            project_config, _ = load_project_config(root)
            approval_store = DMApprovalStore(project_config.runtime.runtime_dir)
            if args.dm_command == "approved-plan-preview":
                print(json.dumps({"ok": True, "approved_dm_plan": approved.to_dict()}, ensure_ascii=False, indent=2))
                return 0
            if args.dm_command == "approval-record":
                digest = approval_store.record(
                    approval, recorded_at=utc_now_iso(), confirmation=args.confirm_approval
                )
                print(json.dumps({"ok": True, "approval_id": approval.approval_id, "approval_hash": digest, "platform_actions_executed": 0}, ensure_ascii=False, indent=2))
                return 0
            browser_config, selected = load_browser_config(root, args.browser_config)
            if browser_config.account_id != campaign_value.account_id:
                raise DMContractError("browser account does not match DM Campaign account")
            checked_at = utc_now_iso()
            calibrated = load_calibration_status(
                project_config.runtime.runtime_dir, browser_config, checked_at=checked_at
            )
            runtime = DMRuntimeStore(project_config.runtime.runtime_dir)
            lead_store = LeadRecordStore(project_config.runtime.runtime_dir)
            lead_binding = None
            if plan.mode == "active_outreach":
                lead_binding = lead_store.resolve_dm_candidate(
                    campaign_id=campaign_value.campaign_id,
                    account_id=campaign_value.account_id,
                    peer_ref_hash=args.expected_peer_ref_hash,
                )
            peer_hash_valid = re.fullmatch(
                r"[0-9a-f]{64}", args.expected_peer_ref_hash or ""
            ) is not None
            if not peer_hash_valid:
                raise DMContractError("expected DM peer hash must be SHA-256 hex")
            expected_conversation_id = "xhs_dm_" + args.expected_peer_ref_hash[:24]
            target_ready = (
                peer_hash_valid
                and snapshot.conversation_id == expected_conversation_id
                and not approved.fixture_only
            )
            target_ref = args.expected_peer_ref_hash[:24]
            _config, base_state = _load_xhs_preflight_state(
                root=root,
                account_id=campaign_value.account_id,
                checked_at=checked_at,
                browser_config_path=args.browser_config,
            )
            dm_state = UnifiedPreflightState(
                platform_access_allowed=base_state.platform_access_allowed,
                login_ready=base_state.login_ready and calibrated,
                account_identity_ready=base_state.account_identity_ready,
                target_ready=target_ready,
                approval_ready=approval_store.matches(approved),
                capability_ready=base_state.capability_ready,
                additional_blockers=(
                    ("fixture_dm_plan_cannot_execute",) if approved.fixture_only else ()
                ),
                runtime_mode=RuntimeMode.SCOPED_UAT,
                scoped_uat_authorized=True,
                scoped_uat_actions_remaining=1,
            )
            unified_request = build_dm_action_request(
                approved,
                expected_peer_ref_hash=args.expected_peer_ref_hash,
                checked_at=checked_at,
                daily_limit=args.daily_dm_limit,
                minimum_interval_seconds=args.minimum_target_interval_seconds,
                budget_timezone="UTC",
            )
            readiness = UnifiedActionPreflightStore(
                project_config.runtime.runtime_dir
            ).evaluate(
                unified_request,
                phase="authorize",
                state=dm_state,
                record=False,
            )
            blockers = list(readiness.blockers)
            output = {
                "ok": readiness.allowed,
                "execution_id": approved.execution_id,
                "browser_config": str(selected.relative_to(root)),
                "blockers": blockers,
                "platform_actions_executed": 0,
                "unified_preflight": readiness.to_dict(),
                "write_lease_binding": {
                    "session_id": approved.execution_id,
                    "target_ref": target_ref,
                    "plan_hash": approved.approval_hash,
                    "branch": "dm_message",
                },
            }
            if args.dm_command == "smoke-ready":
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 0 if not blockers else 2
            if args.confirm_single_dm != DM_APPROVAL_CONFIRMATION:
                raise DMContractError("exact single DM confirmation is required")
            if blockers:
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 2
            client = RunAgentClient(root)
            unified_request, unified_authorize, _lease = authorize_dm_action(
                runtime_dir=project_config.runtime.runtime_dir,
                approved=approved,
                expected_peer_ref_hash=args.expected_peer_ref_hash,
                checked_at=checked_at,
                confirmation=args.confirm_bounded_write_uat,
                state=dm_state,
                client=client,
                daily_limit=args.daily_dm_limit,
                minimum_interval_seconds=args.minimum_target_interval_seconds,
            )
            try:
                client.require_bounded_write_uat(
                    session_id=approved.execution_id,
                    note_id=target_ref,
                    plan_hash=approved.approval_hash,
                    branch="dm_message",
                )
                client.require_execution_ready()
                unified_execute = require_dm_execution(
                    runtime_dir=project_config.runtime.runtime_dir,
                    request=unified_request,
                    state=dm_state,
                )
                port = RunAgentDMPort(
                    root,
                    account_id=campaign_value.account_id,
                    conversation_id=snapshot.conversation_id,
                    expected_peer_ref_hash=args.expected_peer_ref_hash,
                    captured_at=snapshot.captured_at,
                )
                result = execute_single_dm(
                    approved=approved,
                    gate=DMGate(True, True, False, True, False, 1, True),
                    port=port,
                )
                unified_result = record_dm_result(
                    runtime_dir=project_config.runtime.runtime_dir,
                    request=unified_request,
                    result=result,
                    recorded_at=utc_now_iso(),
                )
                if not result.attempted or not result.verified:
                    raise DMContractError(
                        "DM write was not exactly verified; retry is forbidden"
                    )
            finally:
                client.revoke_bounded_write_uat()
            record = runtime.append_verified(
                approved,
                result,
                run_id=approved.execution_id,
                created_at=checked_at,
                daily_dm_limit=args.daily_dm_limit,
                minimum_interval_seconds=args.minimum_target_interval_seconds,
            )
            lead_persistence = None
            if plan.mode == "active_outreach":
                lead_persistence = lead_store.mark_dm_verified(
                    campaign_id=campaign_value.campaign_id,
                    account_id=campaign_value.account_id,
                    peer_ref_hash=args.expected_peer_ref_hash,
                    approval_ref=approval.approval_id,
                    action_record_id=record.record_id,
                    verified_at=checked_at,
                )
            print(json.dumps({
                "ok": True,
                "execution_id": approved.execution_id,
                "unified_preflight": {
                    "authorize": unified_authorize.to_dict(),
                    "execute": unified_execute.to_dict(),
                    "result": unified_result,
                },
                "action_record": record.to_dict(),
                "lead_binding": lead_binding,
                "lead_persistence": lead_persistence,
                "platform_actions_executed": 1,
            }, ensure_ascii=False, indent=2))
            return 0
        except (
            BrowserConfigError,
            BrowserProfileError,
            CampaignContractError,
            ConfigError,
            DMContractError,
            FileNotFoundError,
            LiveInteractionError,
            LeadStoreError,
            RunAgentError,
            XhsCallMethodLockedError,
            ProjectPathError,
            StorageError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "browser":
        try:
            legacy_live_commands = {
                "login-check",
                "login-authorize",
                "profile-probe",
                "latest-readonly",
                "candidate-readonly",
                "candidate-sequence-readonly",
            }
            if args.browser_command in legacy_live_commands:
                raise BrowserLoginError(
                    "legacy Playwright browser command is frozen; use public setup and "
                    "engage commands through scripts/xhs-ops.ps1"
                )
            root = find_project_root(args.project_root)
            project_config, _ = load_project_config(root)
            browser_config, selected = load_browser_config(root, args.config)
            manager = BrowserProfileManager(project_config.runtime.browser_profiles_dir)
            if args.browser_command == "validate-config":
                output = browser_config.to_safe_dict()
                output.update({"ok": True, "config_path": str(selected.relative_to(root))})
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 0
            if args.browser_command == "profile-init":
                path = manager.initialize(browser_config)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "account_id": browser_config.account_id,
                            "profile_name": browser_config.profile_name,
                            "profile_path": str(path.relative_to(root)),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.browser_command == "readiness":
                report = check_browser_readiness(
                    runtime_dir=project_config.runtime.runtime_dir,
                    manager=manager,
                    config=browser_config,
                    checked_at=utc_now_iso(),
                )
                print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                return 0 if report.ok else 2
            if args.browser_command == "evaluate-login":
                evidence_path = resolve_project_relative(
                    root, str(args.evidence), field_name="login_evidence"
                )
                payload = read_json(evidence_path)
                if not isinstance(payload, dict):
                    raise BrowserLoginError("login evidence root must be an object")
                evidence = LoginEvidence.from_dict(payload)
                result = evaluate_login_evidence(evidence, browser_config)
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
                return 0 if result.ok else 2
        except (
            BrowserConfigError,
            BrowserLoginError,
            BrowserProfileError,
            ConfigError,
            FileNotFoundError,
            ProjectPathError,
            StorageError,
            LiveInteractionError,
            XhsCallMethodLockedError,
        ) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    raise AssertionError(f"unhandled command: {args.command}")
