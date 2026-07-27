# V2 autonomous Setup runbook

Setup prepares one dedicated local account. It never grants campaign/action
authority and does not create temporary read/write receipts.

Public commands:

```powershell
.\scripts\xhs-ops.ps1 setup doctor --project-root .
.\scripts\xhs-ops.ps1 setup configure --project-root . --file work/account_setup.json
.\scripts\xhs-ops.ps1 setup status --project-root . --account-id <account-id>
.\scripts\xhs-ops.ps1 setup voice-learn --project-root . --account-id <account-id>
.\scripts\xhs-ops.ps1 setup voice-status --project-root . --account-id <account-id>
```

`setup configure` saves the immutable local account profile and changes
platform-read policy to `valid_execution_mandate`. It does not enable writes.

`setup status` uses the fixed Run Agent/Bridge path. When the packaged extension
is connected and current, it automatically binds that extension instance. When
the visible Xiaohongshu account is logged in, it automatically binds the stable
account identity. These are idempotent local Setup events, not user business
approval gates.

If the extension is absent or login is not visible, status reports
`human_action_required=load_extension_or_scan_qr`. The user loads the packaged
unpacked extension or scans the QR code in the dedicated Chrome profile, then
runs status again. CAPTCHA and risk indicators stop Setup.

AccountVoice learning is optional. Only stable platform user-ID equality proves
reply ownership. Insufficient samples select neutral wording and do not block
`platform_ready`; corpus/profile integrity drift blocks text operations.

All real Xiaohongshu reads remain:

```text
XhsOperationGateway -> ranfang_run_agent -> XHS Bridge -> dedicated Chrome
```

No browser fallback is permitted.
