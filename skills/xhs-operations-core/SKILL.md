---
name: xhs-operations-core
description: Set up and operate the packaged XHS Operations Core from Codex Desktop through setup, publish, service, engage, and read-only review workflows.
---

# XHS Operations Core

Use Codex as the reasoning plane and the packaged CLI as the deterministic
execution boundary. Do not recreate a packaged operation with ad-hoc code.

## Product contract

The user gives one task. Treat that task as a bounded execution delegation.
Codex converts it to a small task JSON; the product creates `TaskIntent`,
`ExecutionMandate`, exact action plans, internal policy decisions and one-use
permits. Do not ask the user to copy a hash, approve a strategy, confirm a
Campaign, approve a draft, authorize a read receipt or authorize an exact
action.

Only these commands are public:

```text
setup doctor/configure/status/voice-learn/voice-status
publish prepare/run/status
service start/heartbeat/status/stop
engage start/heartbeat/status/stop
review status/list/export
```

Legacy confirm/approve/authorize parsers may remain packaged for old data and
developer regression. Never show or call them in a recipient operating flow.

Run every product command from the extracted project root through:

```powershell
.\scripts\xhs-ops.ps1 <group> <command> ...
```

## Fixed live-access boundary

Every real Xiaohongshu read or write must use:

```text
XhsOperationGateway -> ranfang_run_agent -> XHS Bridge -> dedicated Chrome
```

Never fall back to Playwright, generic Chrome control, Computer Use, Codex
Browser, direct CDP, XHR/fetch, direct URLs or temporary scripts. Stop on
CAPTCHA, risk text, login loss, account mismatch, page/target mismatch, vendor
hash drift or an unresolved unknown write.

An exact action permit is internal, binds one account/target/content/plan and
allows one action only. Every account write remains at least 600 seconds apart.
Unknown writes are never retried; reconcile visible state through the packaged
read path and permanently isolate an unresolved exact target.

## Clean-computer Setup

Read `HANDOFF_PROMPT.md` and
[references/handoff-setup.md](references/handoff-setup.md).

1. Run `scripts/install.ps1`, then `scripts/offline-uat.ps1`.
2. Restart Codex Desktop and reopen the extracted project.
3. Start the packaged Bridge and dedicated Chrome.
4. If the extension is absent, guide the user to load the packaged unpacked
   extension directory that contains `manifest.json`.
5. Guide the user through visible Xiaohongshu QR login.
6. Use packaged Setup to bind the extension instance, visible account and local
   account profile. These one-time environment operations are not campaign or
   per-action business approvals.
7. Continue until `setup status` reports `platform_ready=true`.
8. AccountVoice learning is optional. Insufficient owned replies select neutral
   language; they do not block platform readiness.

Visible extension loading, QR login and CAPTCHA are the only normal reasons to
pause for a human interface action. Do not translate an internal safety gate
into a user authorization request.

## Prepare one task file

Codex creates a project-relative JSON file with exactly:

```json
{
  "schema_version": 1,
  "instruction": "寻找对本地周末手作活动感兴趣的用户并进行自然互动",
  "source_mode": "specified_note",
  "source_ref": "user_specified_note:safe-local-reference",
  "requested_actions": ["engage_note_like", "engage_note_comment"],
  "search_queries": ["周末 手作活动", "本地 体验课"],
  "duration_days": 5,
  "allowed_fact_ids": ["activity_title", "city"]
}
```

`search_queries` are compiled by Codex from the user's source and strategy, not
copied from a generic template. The fixed runner submits the first query once,
then reads the saved batch sequentially. Do not add caller-supplied hashes or
confirmation fields. Valid Engage source
modes are `account_note`, `specified_note`, and `direct_brief`. Use the supplied
post exactly for `specified_note`; never replace it with the test account's
latest post. Valid Service source mode is `service_queue`. Publish normally
uses `publish_brief`.

Requested actions must follow the user's verbs:

- reading or analysis alone does not request a write action;
- ordinary interaction may include note/comment likes, one top-level comment
  and an exact-comment reply;
- active DM is allowed only when the task explicitly requests private outreach;
- publish actions are allowed only when the task explicitly requests publishing
  and complete media/content are present.

Missing facts never become permission to invent time, price, capacity,
itinerary, venue, organizer, availability or registration information.

## Run Engage

```powershell
.\scripts\xhs-ops.ps1 engage start --project-root . --task-file <task.json>
.\scripts\xhs-ops.ps1 engage heartbeat --project-root .
.\scripts\xhs-ops.ps1 engage status --project-root .
.\scripts\xhs-ops.ps1 engage stop --project-root .
```

The planner must use one search and one saved result batch. Open, read, assess
and act on candidates sequentially. If one candidate is unsuitable, return to
the same result page and continue; never re-search per candidate.

Each heartbeat may dispatch at most one exact action. A large note may receive
at most one top-level comment, one to three safe exact-comment replies, and
likes or local lead records for other qualified comments, all separated into
different heartbeats. The atomic branches remain `note_like_only`,
`note_engagement`, `comment_like_only`, and `comment_engagement`. Active DM is
never bulk outreach and requires a stable platform peer ID.

If no exact action is safe or ready, heartbeat returns a truthful zero-action
no-op. Out-of-scope, duplicate, pacing or exhausted-budget actions are skipped.
Risk, account drift, capability drift, STOP or unresolved unknown pauses the
task with an objective blocker; do not ask the user to override it.

## Run Service

```powershell
.\scripts\xhs-ops.ps1 service start --project-root . --task-file <task.json>
.\scripts\xhs-ops.ps1 service heartbeat --project-root .
.\scripts\xhs-ops.ps1 service status --project-root .
.\scripts\xhs-ops.ps1 service stop --project-root .
```

Process one inbound comment or DM item at a time. Opt-out, privacy, minor,
identity, fact or target uncertainty is an automatic skip/stop. Passive Service
DM replies remain separate from proactive Engage DM.

## Run Publish

```powershell
.\scripts\xhs-ops.ps1 publish prepare --project-root . --task-file <task.json>
.\scripts\xhs-ops.ps1 publish run --project-root .
.\scripts\xhs-ops.ps1 publish status --project-root .
```

Publish only when the task explicitly requests it and complete local media and
content are available. A missing input is `blocked_missing_input`, not a prompt
for user authorization. One run may issue at most one publish action and must
perform visible terminal readback.

## Review and reporting

`review status/list/export` only reads OperationLedger. It does not change
strategy or platform state.

Report task status, target/action summary, `platform_actions_executed`, visible
verification, Ledger evidence and any objective blocker. Distinguish
program/fixture evidence from `live_verified`. Continue automatically unless
the user must visibly load the extension, scan a QR code, solve a CAPTCHA or
provide genuinely missing task content.

Read [references/cli-contract.md](references/cli-contract.md) for the exact
public commands.
