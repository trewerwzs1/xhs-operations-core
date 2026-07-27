[CmdletBinding()]
param(
    [ValidateSet('start', 'restart', 'status', 'open-extensions')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$BrowserConfigPath = Join-Path $Root 'config\browser.local.json'

if (-not (Test-Path -LiteralPath $BrowserConfigPath)) {
    throw "XhsOperationsCore browser configuration is missing: $BrowserConfigPath"
}

$BrowserConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $BrowserConfigPath | ConvertFrom-Json
$ProfileName = [string]$BrowserConfig.profile_name
if ($ProfileName -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$') {
    throw 'browser.local.json contains an invalid profile_name.'
}

$ProfilesRoot = [System.IO.Path]::GetFullPath((Join-Path $Root 'browser-profiles'))
$ProfilePath = [System.IO.Path]::GetFullPath((Join-Path $ProfilesRoot $ProfileName))
$ProfilesPrefix = $ProfilesRoot.TrimEnd('\') + '\'
if (-not $ProfilePath.StartsWith($ProfilesPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Resolved Chrome profile escaped the XhsOperationsCore profile root: $ProfilePath"
}
if (-not (Test-Path -LiteralPath $ProfilePath)) {
    throw "XhsOperationsCore Chrome profile is missing: $ProfilePath"
}

function Get-XhsOperationsCoreChromeProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'chrome.exe' -and
        $_.CommandLine -and
        $_.CommandLine.IndexOf($ProfilePath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    })
}

function Find-ChromeExecutable {
    $Candidates = @()
    foreach ($InstallRoot in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($InstallRoot) {
            $Candidates += Join-Path $InstallRoot 'Google\Chrome\Application\chrome.exe'
        }
    }
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate)) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    $Command = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    throw 'Google Chrome executable was not found.'
}

$Existing = Get-XhsOperationsCoreChromeProcesses
if ($Action -eq 'status') {
    [pscustomobject]@{
        ok = $true
        running = $Existing.Count -gt 0
        process_ids = @($Existing | ForEach-Object { $_.ProcessId })
        profile_name = $ProfileName
        profile_path = $ProfilePath
        platform_actions_executed = 0
    } | ConvertTo-Json -Depth 3
    return
}

$StoppedProcessIds = @()
if ($Action -eq 'restart') {
    $StoppedProcessIds = @($Existing | ForEach-Object { $_.ProcessId })
    foreach ($Process in $Existing) {
        Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    $Deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 250
        $Existing = Get-XhsOperationsCoreChromeProcesses
    } while ($Existing.Count -gt 0 -and (Get-Date) -lt $Deadline)
    if ($Existing.Count -gt 0) {
        throw "XhsOperationsCore dedicated Chrome profile did not stop cleanly: $($Existing.ProcessId -join ',')"
    }
}

if ($Action -eq 'open-extensions') {
    $Chrome = Find-ChromeExecutable
    $UserDataArgument = '--user-data-dir="' + $ProfilePath + '"'
    $Opened = Start-Process -FilePath $Chrome `
        -ArgumentList @($UserDataArgument, 'chrome://extensions') `
        -WorkingDirectory $Root `
        -PassThru
    Start-Sleep -Seconds 2
    $Processes = Get-XhsOperationsCoreChromeProcesses
    if ($Processes.Count -eq 0) {
        throw 'XhsOperationsCore dedicated Chrome extension page could not be opened.'
    }
    [pscustomobject]@{
        ok = $true
        opened = 'chrome://extensions'
        launcher_process_id = $Opened.Id
        process_ids = @($Processes | ForEach-Object { $_.ProcessId })
        profile_name = $ProfileName
        profile_path = $ProfilePath
        forbidden_extension_flags_used = $false
        platform_actions_executed = 0
    } | ConvertTo-Json -Depth 3
    return
}

if ($Existing.Count -gt 0) {
    [pscustomobject]@{
        ok = $true
        already_running = $true
        process_ids = @($Existing | ForEach-Object { $_.ProcessId })
        profile_name = $ProfileName
        profile_path = $ProfilePath
        platform_actions_executed = 0
    } | ConvertTo-Json -Depth 3
    return
}

$Chrome = Find-ChromeExecutable
$Homepage = [string]$BrowserConfig.homepage_url
if (-not $Homepage.StartsWith('https://www.xiaohongshu.com/', [System.StringComparison]::OrdinalIgnoreCase)) {
    $Homepage = 'https://www.xiaohongshu.com/explore'
}

$UserDataArgument = '--user-data-dir="' + $ProfilePath + '"'
$Started = Start-Process -FilePath $Chrome `
    -ArgumentList @($UserDataArgument, '--new-window', $Homepage) `
    -WorkingDirectory $Root `
    -PassThru
Start-Sleep -Seconds 2
$Processes = Get-XhsOperationsCoreChromeProcesses
if ($Processes.Count -eq 0) {
    throw 'XhsOperationsCore dedicated Chrome process could not be verified after startup.'
}

[pscustomobject]@{
    ok = $true
    started = $true
    restarted = $Action -eq 'restart'
    stopped_process_ids = $StoppedProcessIds
    launcher_process_id = $Started.Id
    process_ids = @($Processes | ForEach-Object { $_.ProcessId })
    profile_name = $ProfileName
    profile_path = $ProfilePath
    homepage = $Homepage
    forbidden_extension_flags_used = $false
    platform_actions_executed = 0
} | ConvertTo-Json -Depth 3
