# V2 Publish autonomous workflow

Public commands:

```powershell
.\scripts\xhs-ops.ps1 publish prepare --project-root . --task-file work/publish-task.json
.\scripts\xhs-ops.ps1 publish run --project-root .
.\scripts\xhs-ops.ps1 publish status --project-root .
```

`prepare` accepts one explicit publish task and creates immutable TaskIntent and
ExecutionMandate records. It performs zero platform actions. A publish task
must request `publish_image` or `publish_video`, use `publish_brief`, and
reference complete local media/content. Missing content returns an objective
input blocker.

`run` lets the planner compile one exact plan, checks account, media/content
hashes, facts, pacing, duplicate state, STOP and capability, then internally
issues a one-use ActionPermit. One run may call the Gateway once. The public
workflow has no separate strategy, draft or action approval command.

The visible terminal state and content/media hashes determine
`verified/not_dispatched/unknown`. Unknown activates STOP and never causes a
speculative retry. OperationReceipt and OperationLedger remain the evidence
source.
