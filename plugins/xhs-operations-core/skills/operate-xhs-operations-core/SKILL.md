---
name: operate-xhs-operations-core
description: "Install and operate XHS Operations Core through its five autonomous public workflows: setup, publish, service, engage, and read-only review."
---

# Operate XhsOperationsCore

Read the project `AGENTS.md`, `V2_CHECKOUT.json`, `HANDOFF_PROMPT.md` and
`skills/xhs-operations-core/SKILL.md`; those files are authoritative.

Expose only:

```text
setup doctor/configure/status/voice-learn/voice-status
publish prepare/run/status
service start/heartbeat/status/stop
engage start/heartbeat/status/stop
review status/list/export
```

The user's task is a bounded execution delegation. Create a project-relative
task JSON with instruction, source mode/reference, requested actions, duration
and allowed fact IDs. Do not include or request a caller-supplied hash,
confirmation phrase, strategy/Campaign approval, read authorization or exact
action authorization. Internal policy creates a one-use permit for each safe
exact action.

Run only:

```powershell
.\scripts\xhs-ops.ps1 <group> <command> ...
```

All real Xiaohongshu access remains:

`XhsOperationGateway -> ranfang_run_agent -> XHS Bridge -> dedicated Chrome`

Never use Playwright, generic Chrome/CDP, Codex Browser, Computer Use, XHR,
direct URLs, temporary scripts or another extension as fallback.

One heartbeat may issue at most one action. Keep at least 600 seconds between
account writes. Scope, fact, duplicate, pacing or budget failures skip; CAPTCHA,
risk, login/account/target drift, STOP or unresolved unknown pauses. Never retry
an unknown exact target.

The user may need to load the packaged extension, scan a QR code, solve a
CAPTCHA or supply genuinely missing task content. These are human interface or
input requirements, not business authorization gates.

Report the task state, exact action summary, platform action count, visible
verification, Ledger evidence and objective blockers. Distinguish program
evidence from `live_verified`.
