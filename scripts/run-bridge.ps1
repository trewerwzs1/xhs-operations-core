[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$BridgeScript = Join-Path $Root 'vendor\xiaohongshu-skills\scripts\bridge_server.py'
$RuntimeDir = Join-Path $Root 'data\runtime\run_agent'
$StdoutPath = Join-Path $RuntimeDir 'bridge.stdout.log'
$StderrPath = Join-Path $RuntimeDir 'bridge.stderr.log'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "XhsOperationsCore virtual environment is missing: $Python"
}
if (-not (Test-Path -LiteralPath $BridgeScript)) {
    throw "Pinned Bridge server is missing: $BridgeScript"
}

$ResolvedBridgeScript = (Resolve-Path -LiteralPath $BridgeScript).Path

function Get-XhsOperationsCoreBridgeProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python' -and
        $_.CommandLine -and
        $_.CommandLine.Contains($ResolvedBridgeScript)
    })
}

if ($Action -eq 'status') {
    $Processes = Get-XhsOperationsCoreBridgeProcesses
    [pscustomobject]@{
        ok = $true
        running = $Processes.Count -gt 0
        process_ids = @($Processes | ForEach-Object { $_.ProcessId })
        bridge_script = $ResolvedBridgeScript
        platform_actions_executed = 0
    } | ConvertTo-Json -Depth 3
    return
}

$StoppedProcessIds = @()
if ($Action -eq 'stop' -or $Action -eq 'restart') {
    $Processes = Get-XhsOperationsCoreBridgeProcesses
    $StoppedProcessIds = @($Processes | ForEach-Object { $_.ProcessId })
    foreach ($Process in $Processes) {
        Stop-Process -Id $Process.ProcessId -Force
    }
    foreach ($ProcessId in $StoppedProcessIds) {
        Wait-Process -Id $ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
    if ($Action -eq 'stop') {
        [pscustomobject]@{
            ok = $true
            stopped_process_ids = $StoppedProcessIds
            platform_actions_executed = 0
        } | ConvertTo-Json -Depth 3
        return
    }
}

$Existing = Get-XhsOperationsCoreBridgeProcesses
if ($Existing.Count -gt 0) {
    [pscustomobject]@{
        ok = $true
        already_running = $true
        process_ids = @($Existing | ForEach-Object { $_.ProcessId })
        platform_actions_executed = 0
    } | ConvertTo-Json -Depth 3
    return
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
$Started = Start-Process -FilePath $Python `
    -ArgumentList @($ResolvedBridgeScript) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutPath `
    -RedirectStandardError $StderrPath `
    -PassThru
Start-Sleep -Seconds 2
if ($Started.HasExited) {
    throw "Bridge server exited during startup; inspect $StderrPath"
}
$Processes = Get-XhsOperationsCoreBridgeProcesses
if ($Processes.Count -eq 0) {
    throw 'Bridge server process could not be verified after startup.'
}
[pscustomobject]@{
    ok = $true
    started = $true
    restarted = $Action -eq 'restart'
    stopped_process_ids = $StoppedProcessIds
    process_ids = @($Processes | ForEach-Object { $_.ProcessId })
    bridge_script = $ResolvedBridgeScript
    stdout_log = $StdoutPath
    stderr_log = $StderrPath
    platform_actions_executed = 0
} | ConvertTo-Json -Depth 3
