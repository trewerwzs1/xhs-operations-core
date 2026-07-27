# V2 autonomous public CLI contract

Run recipient commands only through `.\scripts\xhs-ops.ps1`.

The public command manifest is:

```text
setup doctor/configure/status/voice-learn/voice-status
publish prepare/run/status
service start/heartbeat/status/stop
engage start/heartbeat/status/stop
review status/list/export
```

Internal compatibility commands are not recipient commands.

## Setup

```powershell
.\scripts\xhs-ops.ps1 setup doctor --project-root .
.\scripts\xhs-ops.ps1 setup configure --project-root . --file work/account_setup.json
.\scripts\xhs-ops.ps1 setup status --project-root . --account-id <account-id>
.\scripts\xhs-ops.ps1 setup voice-status --project-root . --account-id <account-id>
.\scripts\xhs-ops.ps1 setup voice-learn --project-root . --account-id <account-id>
```

Bridge extension loading, visible QR login and account enrollment are one-time
Setup operations performed by the packaged setup/run-agent adapter. They are
not per-task or per-action approvals.

## Task file

Every `prepare` or `start` command receives a project-relative JSON file:

```json
{
  "schema_version": 1,
  "instruction": "寻找对本地周末手作活动感兴趣的用户并进行自然互动",
  "source_mode": "specified_note",
  "source_ref": "user_specified_note:local-ref",
  "requested_actions": ["engage_note_comment"],
  "search_queries": ["周末 手作活动", "本地 体验课"],
  "duration_days": 5,
  "allowed_fact_ids": ["activity_title", "city"]
}
```

The file contains no caller-supplied hash and no confirmation field.
`search_queries` are a Codex-compiled strategy output; Engage requires one to
five exact queries while Service and Publish use an empty list. The program
computes immutable intent, source and mandate hashes locally.

## Engage

```powershell
.\scripts\xhs-ops.ps1 engage start --project-root . --task-file work/engage-task.json
.\scripts\xhs-ops.ps1 engage heartbeat --project-root .
.\scripts\xhs-ops.ps1 engage status --project-root .
.\scripts\xhs-ops.ps1 engage stop --project-root .
```

Valid source modes are `account_note`, `specified_note`, and `direct_brief`.
One heartbeat dispatches at most one action. No exact plan produces a safe
zero-action result. Scope/fact/duplicate/pacing/budget failures skip; risk,
identity drift, capability drift, STOP and unresolved unknown pause. Account
writes remain at least 600 seconds apart.

## Service

```powershell
.\scripts\xhs-ops.ps1 service start --project-root . --task-file work/service-task.json
.\scripts\xhs-ops.ps1 service heartbeat --project-root .
.\scripts\xhs-ops.ps1 service status --project-root .
.\scripts\xhs-ops.ps1 service stop --project-root .
```

Use `source_mode=service_queue`. The planner reads and handles one exact inbox
item per heartbeat.

## Publish

```powershell
.\scripts\xhs-ops.ps1 publish prepare --project-root . --task-file work/publish-task.json
.\scripts\xhs-ops.ps1 publish run --project-root .
.\scripts\xhs-ops.ps1 publish status --project-root .
```

Use `source_mode=publish_brief`. Publishing requires an explicit publish action
and complete media/content. One run dispatches at most one platform action.

## Records

```powershell
.\scripts\xhs-ops.ps1 review status --project-root . --account-id <account-id>
.\scripts\xhs-ops.ps1 review list --project-root . --account-id <account-id>
.\scripts\xhs-ops.ps1 review export --project-root . --account-id <account-id>
```

Review is read-only. Every result reports `platform_actions_executed`.
