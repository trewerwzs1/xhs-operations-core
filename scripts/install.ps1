[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')][string]$AccountId,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')][string]$ProfileName,
    [switch]$SkipPlugin,
    [switch]$SkipSkill,
    [switch]$UpdateSkill
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'install-support.ps1')
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$ExtensionSource = Join-Path $Root 'vendor\xiaohongshu-skills\extension'
$ExtensionTarget = Join-Path $env:LOCALAPPDATA 'XhsOperationsCore\xhs-bridge-extension'

if (-not (Test-Path -LiteralPath $Python)) {
    $SystemPython = $null
    $SystemPythonArgs = @()
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        try {
            & $PythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -eq 0) {
                $SystemPython = $PythonCommand.Source
            }
        } catch {
            $SystemPython = $null
        }
    }
    if (-not $SystemPython) {
        $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($PyLauncher) {
            try {
                & $PyLauncher.Source -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
                if ($LASTEXITCODE -eq 0) {
                    $SystemPython = $PyLauncher.Source
                    $SystemPythonArgs = @('-3')
                }
            } catch {
                $SystemPython = $null
            }
        }
    }
    if (-not $SystemPython) {
        throw 'Python 3.11 or newer is required. Install Python and enable either the python command or Windows py launcher.'
    }
    & $SystemPython @SystemPythonArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) {
        throw "Virtual environment creation failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment creation failed: $Python"
}

& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip upgrade failed with exit code $LASTEXITCODE."
}
& $Python -m pip install -e "$Root"
if ($LASTEXITCODE -ne 0) {
    throw "XhsOperationsCore installation failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath (Join-Path $ExtensionSource 'manifest.json'))) {
    throw "Run Agent extension is missing: $ExtensionSource"
}

$ExpectedExtensionTarget = [System.IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA 'XhsOperationsCore\xhs-bridge-extension')
)
$ResolvedExtensionTarget = [System.IO.Path]::GetFullPath($ExtensionTarget)
if ($ResolvedExtensionTarget -ne $ExpectedExtensionTarget) {
    throw "Refusing to refresh unexpected extension target: $ResolvedExtensionTarget"
}
if (Test-Path -LiteralPath $ResolvedExtensionTarget) {
    Remove-Item -LiteralPath $ResolvedExtensionTarget -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $ResolvedExtensionTarget | Out-Null
Copy-Item -Path (Join-Path $ExtensionSource '*') -Destination $ExtensionTarget -Recurse -Force

$SourceFiles = Get-ChildItem -LiteralPath $ExtensionSource -Recurse -File
$TargetFiles = Get-ChildItem -LiteralPath $ExtensionTarget -Recurse -File
if ($SourceFiles.Count -ne $TargetFiles.Count) {
    throw 'XHS Bridge staging verification failed: file count mismatch.'
}
$ExtensionSourcePrefix = $ExtensionSource.TrimEnd('\') + '\'
foreach ($SourceFile in $SourceFiles) {
    if (-not $SourceFile.FullName.StartsWith($ExtensionSourcePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "XHS Bridge staging verification failed: source escaped extension root."
    }
    $RelativePath = $SourceFile.FullName.Substring($ExtensionSourcePrefix.Length)
    $TargetFile = Join-Path $ExtensionTarget $RelativePath
    if (-not (Test-Path -LiteralPath $TargetFile)) {
        throw "XHS Bridge staging verification failed: missing $RelativePath"
    }
    $SourceHash = Get-XhsOperationsCoreFileSha256 -LiteralPath $SourceFile.FullName
    $TargetHash = Get-XhsOperationsCoreFileSha256 -LiteralPath $TargetFile
    if ($SourceHash -ne $TargetHash) {
        throw "XHS Bridge staging verification failed: hash mismatch for $RelativePath"
    }
}

if (-not $SkipSkill) {
    $SkillSource = Join-Path $Root 'skills\xhs-operations-core'
    $SkillRoot = Join-Path $env:USERPROFILE '.codex\skills'
    $SkillTarget = Join-Path $SkillRoot 'xhs-operations-core'
    New-Item -ItemType Directory -Force -Path $SkillRoot | Out-Null
    if ((Test-Path -LiteralPath $SkillTarget) -and -not $UpdateSkill) {
        throw "Codex Skill already exists at $SkillTarget. Re-run with -UpdateSkill or -SkipSkill."
    }
    New-Item -ItemType Directory -Force -Path $SkillTarget | Out-Null
    Copy-Item -Path (Join-Path $SkillSource '*') -Destination $SkillTarget -Recurse -Force
}

if (-not $SkipPlugin) {
    & (Join-Path $PSScriptRoot 'install-plugin.ps1') -ProjectRoot $Root
}

$ProjectLocal = Join-Path $Root 'config\project.local.json'
$BrowserLocal = Join-Path $Root 'config\browser.local.json'
if ((Test-Path -LiteralPath $ProjectLocal) -and (Test-Path -LiteralPath $BrowserLocal)) {
    $ExistingBrowserConfig = Get-Content -LiteralPath $BrowserLocal -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($ExistingBrowserConfig.account_id -ne $AccountId -or $ExistingBrowserConfig.profile_name -ne $ProfileName) {
        throw 'Existing browser.local.json belongs to a different account or profile.'
    }
    Write-Host 'Existing local configuration preserved; setup init skipped for upgrade install.'
    & $Python -m xhs_operations_core setup migrate-existing --project-root $Root --account-id $AccountId --profile-name $ProfileName
    if ($LASTEXITCODE -ne 0) {
        throw "XhsOperationsCore existing-install migration failed with exit code $LASTEXITCODE."
    }
} elseif ((Test-Path -LiteralPath $ProjectLocal) -or (Test-Path -LiteralPath $BrowserLocal)) {
    throw 'Partial local configuration detected; both project.local.json and browser.local.json are required.'
} else {
    & $Python -m xhs_operations_core setup init --project-root $Root --account-id $AccountId --profile-name $ProfileName
    if ($LASTEXITCODE -ne 0) {
        throw "XhsOperationsCore setup failed with exit code $LASTEXITCODE."
    }
}
& $Python -m xhs_operations_core doctor --project-root $Root --init-runtime --format json
if ($LASTEXITCODE -ne 0) {
    throw "XhsOperationsCore doctor failed with exit code $LASTEXITCODE."
}

$ProjectConfig = Get-Content -LiteralPath $ProjectLocal -Raw -Encoding UTF8 | ConvertFrom-Json
$RuntimeDir = Join-Path $Root ([string]$ProjectConfig.runtime.runtime_dir)
$StopPath = Join-Path $RuntimeDir 'comment_flow\STOP.json'
if (-not (Test-Path -LiteralPath $StopPath)) {
    throw "STOP file is missing after installation: $StopPath"
}
$StopState = Get-Content -LiteralPath $StopPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($StopState.writes_allowed -ne $false) {
    throw 'STOP verification failed: writes_allowed must be false.'
}

$InstalledBrowserConfig = Get-Content -LiteralPath $BrowserLocal -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host 'Installation complete. STOP remains enabled and real writes remain blocked.'
Write-Host "Existing platform access setting preserved: $($InstalledBrowserConfig.allow_platform_access)"
Write-Host "XHS Bridge staged at: $ExtensionTarget"
Write-Host 'XHS Bridge staging integrity verified. Reload the extension card after every product upgrade.'
if (-not $SkipPlugin) {
    Write-Host 'XhsOperationsCore Codex Plugin installed and verified. Restart Codex Desktop after setup.'
}
Write-Host 'Next: run .\scripts\run-chrome.ps1 start, then .\scripts\run-bridge.ps1 start; complete the one-time XHS Bridge approval if needed, then run connection-check.'
