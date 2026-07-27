# V2 Service autonomous workflow

Public commands:

```powershell
.\scripts\xhs-ops.ps1 service start --project-root . --task-file work/service-task.json
.\scripts\xhs-ops.ps1 service heartbeat --project-root .
.\scripts\xhs-ops.ps1 service status --project-root .
.\scripts\xhs-ops.ps1 service stop --project-root .
```

The task uses `source_mode=service_queue` and may request
`service_comment_reply` and/or `service_dm_reply`. Start creates the immutable
TaskIntent and ExecutionMandate. Each heartbeat scans or opens one exact inbound
item, reads context, drafts a bounded response, runs automatic policy and
dispatches no more than one reply.

An empty scan is evidence only for that visible batch. It does not prove item
read or reply capability. Opt-out, privacy, minor, account, target, context,
fact, content or capability uncertainty is a skip/stop and never becomes a
request to bypass the rule.

All writes share the account-global 600-second interval. Unknown results are not
retried. Passive DM replies remain separate from proactive Engage DM.
