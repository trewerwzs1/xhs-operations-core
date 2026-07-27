[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Local environment is missing. Run scripts\install.ps1 first.'
}
& $Python -m xhs_operations_core.public_cli @Arguments
exit $LASTEXITCODE
