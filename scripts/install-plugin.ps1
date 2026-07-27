[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CodexPath = $env:XHS_OPERATIONS_CORE_CODEX_PATH
)

$ErrorActionPreference = 'Stop'

function Resolve-XhsOperationsCoreCodex {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        $Resolved = [System.IO.Path]::GetFullPath($ExplicitPath)
        if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
            throw "Configured Codex executable does not exist: $Resolved"
        }
        return $Resolved
    }

    foreach ($Name in @('codex.exe', 'codex')) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command) {
            return $Command.Source
        }
    }

    if ($env:LOCALAPPDATA) {
        $DesktopCandidate = Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'
        if (Test-Path -LiteralPath $DesktopCandidate -PathType Leaf) {
            return $DesktopCandidate
        }
    }

    throw 'Codex Desktop CLI was not found. Install or update Codex Desktop, then run install.ps1 again.'
}

function Invoke-XhsOperationsCoreCodexJson {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )

    # Windows PowerShell 5.1 surfaces native stderr lines as NativeCommandError
    # records when ErrorActionPreference is Stop.  Codex may emit a harmless
    # PATH-alias warning in isolated homes, so keep stdout JSON separate and
    # decide success exclusively from the native process exit code.
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $Output = & $Executable @Arguments
        $ExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($ExitCode -ne 0) {
        $Detail = ($Output | Out-String).Trim()
        throw "$Label failed with exit code $ExitCode. $Detail"
    }
    try {
        return (($Output | Out-String) | ConvertFrom-Json)
    } catch {
        throw "$Label did not return valid JSON. $($_.Exception.Message)"
    }
}

$Root = [System.IO.Path]::GetFullPath($ProjectRoot)
$MarketplaceManifest = Join-Path $Root '.agents\plugins\marketplace.json'
$PluginManifest = Join-Path $Root 'plugins\xhs-operations-core\.codex-plugin\plugin.json'
if (-not (Test-Path -LiteralPath $MarketplaceManifest -PathType Leaf)) {
    throw "XhsOperationsCore local Marketplace manifest is missing: $MarketplaceManifest"
}
if (-not (Test-Path -LiteralPath $PluginManifest -PathType Leaf)) {
    throw "XhsOperationsCore Plugin manifest is missing: $PluginManifest"
}

$PluginDefinition = Get-Content -LiteralPath $PluginManifest -Raw -Encoding UTF8 | ConvertFrom-Json
if ($PluginDefinition.name -ne 'xhs-operations-core' -or -not $PluginDefinition.version) {
    throw 'XhsOperationsCore Plugin manifest has an invalid name or version.'
}

$Codex = Resolve-XhsOperationsCoreCodex -ExplicitPath $CodexPath
$MarketplaceName = 'xhs-operations-core-local'
$PluginSelector = 'xhs-operations-core@xhs-operations-core-local'

$MarketplaceList = Invoke-XhsOperationsCoreCodexJson -Executable $Codex -Arguments @(
    'plugin', 'marketplace', 'list', '--json'
) -Label 'Codex marketplace inspection'
$ExistingMarketplace = @($MarketplaceList.marketplaces) |
    Where-Object { $_.name -eq $MarketplaceName } |
    Select-Object -First 1

$PluginList = Invoke-XhsOperationsCoreCodexJson -Executable $Codex -Arguments @(
    'plugin', 'list', '--json'
) -Label 'Codex Plugin inspection'
$ExistingPlugin = @($PluginList.installed) |
    Where-Object { $_.pluginId -eq $PluginSelector -and $_.installed -eq $true } |
    Select-Object -First 1

if ($ExistingPlugin) {
    $null = Invoke-XhsOperationsCoreCodexJson -Executable $Codex -Arguments @(
        'plugin', 'remove', $PluginSelector, '--json'
    ) -Label 'XhsOperationsCore Plugin refresh removal'
}

if ($ExistingMarketplace) {
    $ExistingRoot = [System.IO.Path]::GetFullPath([string]$ExistingMarketplace.root)
    if (-not $ExistingRoot.Equals($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
        $null = Invoke-XhsOperationsCoreCodexJson -Executable $Codex -Arguments @(
            'plugin', 'marketplace', 'remove', $MarketplaceName, '--json'
        ) -Label 'XhsOperationsCore Marketplace rebind removal'
        $ExistingMarketplace = $null
    }
}

if (-not $ExistingMarketplace) {
    $null = Invoke-XhsOperationsCoreCodexJson -Executable $Codex -Arguments @(
        'plugin', 'marketplace', 'add', $Root, '--json'
    ) -Label 'XhsOperationsCore Marketplace installation'
}

$Installed = Invoke-XhsOperationsCoreCodexJson -Executable $Codex -Arguments @(
    'plugin', 'add', $PluginSelector, '--json'
) -Label 'XhsOperationsCore Plugin installation'
$VerifiedList = Invoke-XhsOperationsCoreCodexJson -Executable $Codex -Arguments @(
    'plugin', 'list', '--json'
) -Label 'XhsOperationsCore Plugin verification'
$Verified = @($VerifiedList.installed) |
    Where-Object {
        $_.pluginId -eq $PluginSelector -and
        $_.installed -eq $true -and
        $_.enabled -eq $true -and
        $_.version -eq [string]$PluginDefinition.version
    } |
    Select-Object -First 1
if (-not $Verified) {
    throw 'XhsOperationsCore Plugin verification failed: installed, enabled, and version state did not match.'
}

[pscustomobject]@{
    ok = $true
    plugin_id = $PluginSelector
    version = [string]$PluginDefinition.version
    marketplace = $MarketplaceName
    installed_path = [string]$Installed.installedPath
    codex = $Codex
} | ConvertTo-Json -Depth 5
