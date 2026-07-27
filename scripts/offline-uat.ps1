[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Local environment is missing. Run scripts\install.ps1 first.'
}
& $Python (Join-Path $PSScriptRoot 'offline_uat.py') --project-root $Root
exit $LASTEXITCODE

