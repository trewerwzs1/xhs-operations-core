# V2 Engage and Review autonomous workflow

## Engage

Public commands:

```powershell
.\scripts\xhs-ops.ps1 engage start --project-root . --task-file work/engage-task.json
.\scripts\xhs-ops.ps1 engage heartbeat --project-root .
.\scripts\xhs-ops.ps1 engage status --project-root .
.\scripts\xhs-ops.ps1 engage stop --project-root .
```

Engage accepts `account_note`, `specified_note` and `direct_brief`. The source
is compiled locally into TaskIntent, StrategyPack/Campaign snapshots and one
ExecutionMandate. Strategy and Campaign objects preserve facts and targeting;
they are not user approval state machines.

The live planner performs one exact query, saves one result batch, then opens,
reads, assesses and handles candidates sequentially. A rejected candidate
returns to the same batch. It never performs a new search per candidate.

Each heartbeat produces at most one branch:

- `note_like_only`;
- `note_engagement`;
- `comment_like_only`;
- `comment_engagement`;
- one exact active DM when the task explicitly includes DM.

The exact plan is evaluated against mandate scope, facts, account/page/target,
risk, pacing, caps, dedupe, STOP and unresolved unknown. `allow` creates an
internal one-use permit; `skip` continues later; `stop` pauses with an objective
blocker. No public confirmation or action-approval step exists.

A single note may receive at most one top-level comment. Up to three qualified
exact-comment replies and other comment likes/lead records are separate
heartbeats, at least 600 seconds apart. Active DM requires a stable platform
peer ID and never becomes bulk outreach.

## Review

Public Review is only:

```powershell
.\scripts\xhs-ops.ps1 review status --project-root . --account-id <account-id>
.\scripts\xhs-ops.ps1 review list --project-root . --account-id <account-id>
.\scripts\xhs-ops.ps1 review export --project-root . --account-id <account-id>
```

Review reads OperationLedger and performs zero platform actions. Strategy
analysis remains project/StrategyPack-specific.
