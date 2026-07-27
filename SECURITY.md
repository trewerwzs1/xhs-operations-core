# Security policy

## Supported release

Security fixes are applied to the latest published Alpha or later release.

## Report a vulnerability

Do not publish credentials, cookies, browser profiles, private customer data,
or a working exploit in a public issue. Use the repository host's private
security-reporting channel when enabled. Until that channel exists, open a
minimal public issue requesting a private contact route without including the
sensitive details.

## Runtime boundary

- Real Xiaohongshu access is restricted to the packaged
  `XhsOperationGateway -> ranfang_run_agent -> XHS Bridge` chain.
- The project does not need account passwords, cookies, tokens, or verification
  codes in source files, task JSON, issue reports, or chat.
- CAPTCHA, login loss, account mismatch, risk text, target drift, unknown write
  results, and STOP fail closed.
- Unknown writes are never retried automatically.
- Account writes are globally separated by at least 600 seconds.
- Local runtime data, browser profiles, logs, and generated reports are excluded
  from delivery archives and version control.

## Platform use

XHS Operations Core is an independent, unofficial local tool. Operators remain
responsible for platform terms, applicable law, content rights, privacy, and
their organization's policies. The project must not be used to bypass platform
controls, automate bulk unsolicited outreach, or target minors.
