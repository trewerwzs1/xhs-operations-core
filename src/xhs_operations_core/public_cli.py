"""Small product dispatcher with autonomous public workflow commands."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Sequence

from .autonomous_engage import run_autonomous_engage_heartbeat
from .autonomous_workflows import (
    AutonomousWorkflowError,
    run_heartbeat,
    start_workflow,
    stop_workflow,
    workflow_status,
)
from .authority import AuthorityStore, ExecutionMandate, TaskIntent
from .config import load_project_config
from .cli import main as internal_main
from .onboarding import AccountSetupProfile, AccountSetupStore, build_setup_status
from .paths import find_project_root, resolve_project_relative
from .platform.browser.config import load_browser_config
from .platform.browser.login import load_calibration_status
from .platform.xhs.run_agent import (
    EXTENSION_ENROLL_CONFIRMATION,
    PLATFORM_ACCOUNT_ENROLLMENT_CONFIRMATION,
    RunAgentClient,
    RunAgentError,
)
from .public_surface import PUBLIC_COMMANDS
from .storage import append_jsonl, read_json, write_json_atomic


def _top_help() -> str:
    return (
        "usage: xhs-operations-core {setup,publish,service,engage,review} ...\n\n"
        "XHS Operations Core public workflows:\n"
        "  setup    install, configure and inspect one local account\n"
        "  publish  prepare and run one bounded image/video publish job\n"
        "  service  start and heartbeat one inbound-service job\n"
        "  engage   start and heartbeat one bounded outreach job\n"
        "  review   read-only OperationLedger status/list/export\n"
    )


def _group_help(group: str) -> str:
    commands = ",".join(PUBLIC_COMMANDS[group])
    return f"usage: xhs-operations-core {group} {{{commands}}} ...\n"


def _translate(argv: list[str]) -> list[str]:
    if argv[:2] == ["setup", "doctor"]:
        return ["doctor", *argv[2:]]
    if argv[:2] == ["setup", "configure"]:
        return ["setup", "account-config", *argv[2:]]
    return argv


def _autonomous_parser(group: str, command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"xhs-operations-core {group} {command}")
    parser.add_argument("--project-root", type=Path, default=None)
    if command in {"start", "prepare"}:
        parser.add_argument("--task-file", type=Path, required=True)
        parser.add_argument("--at", default=None)
    elif command in {"heartbeat", "run"}:
        parser.add_argument("--at", default=None)
    elif command == "stop":
        parser.add_argument("--at", default=None)
    return parser


def _autonomous_dispatch(arguments: list[str]) -> int | None:
    group, command = arguments[:2]
    if group not in {"publish", "service", "engage"}:
        return None
    if command not in PUBLIC_COMMANDS[group]:
        return None
    args = _autonomous_parser(group, command).parse_args(arguments[2:])
    try:
        if command in {"start", "prepare"}:
            payload = start_workflow(
                project_root=args.project_root,
                workflow=group,
                task_file=args.task_file,
                started_at=args.at,
            )
        elif command == "status":
            payload = workflow_status(project_root=args.project_root, workflow=group)
        elif command == "stop":
            payload = stop_workflow(
                project_root=args.project_root,
                workflow=group,
                stopped_at=args.at,
            )
        else:
            payload = (
                run_autonomous_engage_heartbeat(
                    project_root=args.project_root,
                    checked_at=args.at,
                )
                if group == "engage"
                else run_heartbeat(
                    project_root=args.project_root,
                    workflow=group,
                    checked_at=args.at,
                )
            )
    except AutonomousWorkflowError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "workflow": group,
                    "error": str(exc),
                    "platform_actions_executed": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 2


def _setup_dispatch(arguments: list[str]) -> int | None:
    command = arguments[1]
    if command not in {"configure", "status"}:
        return None
    parser = argparse.ArgumentParser(prog=f"xhs-operations-core setup {command}")
    parser.add_argument("--project-root", type=Path, default=None)
    if command == "configure":
        parser.add_argument("--file", type=Path, required=True)
    else:
        parser.add_argument("--account-id", default="")
    args = parser.parse_args(arguments[2:])
    try:
        root = find_project_root(args.project_root)
        project_config, _ = load_project_config(root)
        if command == "configure":
            task_path = resolve_project_relative(
                root, str(args.file), field_name="account_setup_file"
            )
            value = read_json(task_path)
            if not isinstance(value, dict):
                raise AutonomousWorkflowError("account setup file must be an object")
            profile = AccountSetupProfile.from_dict(value)
            browser, browser_path = load_browser_config(
                root, Path("config/browser.local.json")
            )
            if browser.account_id != profile.account_id:
                raise AutonomousWorkflowError(
                    "account setup does not match the dedicated browser profile"
                )
            profile_path = AccountSetupStore(project_config.runtime.runtime_dir).save(profile)
            browser_payload = read_json(browser_path)
            if not isinstance(browser_payload, dict):
                raise AutonomousWorkflowError("browser config is invalid")
            browser_payload["fixture_only"] = False
            browser_payload["allow_platform_access"] = True
            write_json_atomic(browser_path, browser_payload)
            append_jsonl(
                project_config.runtime.runtime_dir / "setup" / "settings_audit.jsonl",
                {
                    "account_id": profile.account_id,
                    "setting": "mandate_bound_platform_read",
                    "value": True,
                    "changed_at": profile.created_at,
                    "platform_actions_executed": 0,
                },
            )
            payload: dict[str, object] = {
                "ok": True,
                "account_id": profile.account_id,
                "account_setup": profile.to_dict(),
                "storage_ref": str(profile_path.relative_to(project_config.runtime.runtime_dir)),
                "platform_read_policy": "valid_execution_mandate",
                "platform_writes_enabled": False,
                "next_step": "load_extension_login_then_run_setup_status",
                "platform_actions_executed": 0,
            }
        else:
            profile = AccountSetupStore(project_config.runtime.runtime_dir).load()
            if args.account_id and args.account_id != profile.account_id:
                raise AutonomousWorkflowError("status account does not match Setup")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            setup_intent = TaskIntent.create(
                account_id=profile.account_id,
                workflow="setup",
                instruction="Verify and complete the dedicated local platform setup.",
                source_mode="direct_brief",
                source_ref="setup:current_dedicated_profile",
                source_hash=sha256(
                    f"setup:{profile.account_id}".encode("utf-8")
                ).hexdigest(),
                requested_actions=(),
                created_at=now.isoformat(),
            )
            setup_mandate = ExecutionMandate.from_intent(
                setup_intent,
                valid_from=now.isoformat(),
                valid_until=(now + timedelta(minutes=30)).isoformat(),
                timezone_name=profile.timezone,
                daily_caps={},
                created_at=now.isoformat(),
            )
            authority = AuthorityStore(project_config.runtime.runtime_dir)
            authority.save_intent(setup_intent)
            authority.save_mandate(setup_mandate)
            client = RunAgentClient(root, mandate_id=setup_mandate.mandate_id)
            connection = client.connection_status()
            enrollment_events: list[str] = []
            if (
                connection.get("extension_connected") is True
                and connection.get("staged_extension_current") is True
                and connection.get("loaded_extension_current") is True
                and connection.get("extension_instance_matches") is not True
            ):
                client.enroll_current_extension_instance(
                    confirmation=EXTENSION_ENROLL_CONFIRMATION
                )
                enrollment_events.append("extension_instance_bound")
                connection = client.connection_status()
            identity_ready = False
            identity_blocker = ""
            if connection.get("ready_for_login_check") is True:
                try:
                    identity = client.assert_current_account_identity()
                    identity_ready = identity.get("verified") is True
                except RunAgentError:
                    try:
                        identity = client.enroll_current_account_identity(
                            confirmation=PLATFORM_ACCOUNT_ENROLLMENT_CONFIRMATION
                        )
                        identity_ready = identity.get("identity_enrolled") is True
                        enrollment_events.append("visible_platform_account_bound")
                    except RunAgentError as exc:
                        identity_blocker = str(exc)
            login_ready = identity_ready or bool(
                load_calibration_status(
                    project_config.runtime.runtime_dir,
                    load_browser_config(root, Path("config/browser.local.json"))[0],
                    checked_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            status = build_setup_status(
                project_config.runtime.runtime_dir,
                account_id=profile.account_id,
                connection_ready=connection.get("ready_for_login_check") is True,
                login_ready=login_ready,
            )
            blockers = list(status.get("blockers", []))
            if identity_blocker:
                blockers.append("visible_account_not_ready")
                status = {
                    **status,
                    "setup_complete": False,
                    "platform_ready": False,
                    "operations_ready": False,
                    "next_step": "visible_account_login",
                    "operations_blockers": list(
                        dict.fromkeys(
                            [*status.get("operations_blockers", []), "visible_account_not_ready"]
                        )
                    ),
                }
            payload = {
                "ok": True,
                "setup_status": {**status, "blockers": list(dict.fromkeys(blockers))},
                "connection": connection,
                "automatic_setup_events": enrollment_events,
                "setup_mandate_id": setup_mandate.mandate_id,
                "identity_blocker": identity_blocker,
                "human_action_required": (
                    "load_extension_or_scan_qr"
                    if not connection.get("ready_for_login_check") or not login_ready
                    else ""
                ),
                "platform_actions_executed": 0,
            }
    except (AutonomousWorkflowError, RunAgentError, OSError, ValueError) as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "platform_actions_executed": 0,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 2


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments == ["--help"] or arguments == ["-h"]:
        print(_top_help(), end="")
        return 0
    if arguments[0] == "doctor":
        # Hidden compatibility alias used by installers; public help keeps the
        # product grouped under setup.
        return internal_main(arguments)
    group = arguments[0]
    if group not in PUBLIC_COMMANDS:
        print(f"unsupported public command group: {group}", file=sys.stderr)
        return 2
    if len(arguments) == 1 or arguments[1] in {"--help", "-h"}:
        print(_group_help(group), end="")
        return 0
    command = arguments[1]
    if command not in PUBLIC_COMMANDS[group]:
        print(f"unsupported public {group} command: {command}", file=sys.stderr)
        return 2
    setup = _setup_dispatch(arguments) if group == "setup" else None
    if setup is not None:
        return setup
    autonomous = _autonomous_dispatch(arguments)
    if autonomous is not None:
        return autonomous
    return internal_main(_translate(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
