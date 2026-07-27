# Clean-computer Setup

1. Extract the ZIP to one stable directory.
2. Read `HANDOFF_PROMPT.md`, `AGENTS.md`, `V2_CHECKOUT.json` and the packaged
   operator Skill.
3. Install and run offline UAT:

   ```powershell
   .\scripts\install.ps1 -AccountId <safe-alias> -ProfileName <safe-alias>
   .\scripts\offline-uat.ps1
   ```

4. Restart Codex Desktop and reopen this project.
5. Start the packaged Bridge and dedicated Chrome. If the extension is absent,
   load the packaged unpacked directory containing `manifest.json`.
6. Scan the Xiaohongshu QR code in that dedicated profile.
7. Save the account profile:

   ```powershell
   .\scripts\xhs-ops.ps1 setup configure --project-root . --file work/account_setup.json
   .\scripts\xhs-ops.ps1 setup status --project-root . --account-id <account-id>
   ```

8. Continue until `platform_ready=true`. Environment/account binding is handled
   inside Setup; it is not a per-task authorization flow.
9. AccountVoice learning is optional:

   ```powershell
   .\scripts\xhs-ops.ps1 setup voice-learn --project-root . --account-id <account-id>
   .\scripts\xhs-ops.ps1 setup voice-status --project-root . --account-id <account-id>
   ```

   Only stable platform user-ID equality proves reply ownership. Insufficient
   samples select neutral wording and never create synthetic examples.

After Setup, prepare one project-relative task JSON from
`examples/autonomous/`, then use only `publish prepare/run`,
`service start/heartbeat/status/stop`, or
`engage start/heartbeat/status/stop`.

The receiver never develops browser automation and never uses another live
browser path.
