function Get-XhsOperationsCoreFileSha256 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath
    )

    $ResolvedPath = [System.IO.Path]::GetFullPath($LiteralPath)
    $Stream = $null
    $Hasher = $null
    try {
        $Stream = [System.IO.File]::Open(
            $ResolvedPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        $Hasher = [System.Security.Cryptography.SHA256]::Create()
        $HashBytes = $Hasher.ComputeHash($Stream)
        return [System.BitConverter]::ToString($HashBytes).Replace('-', '')
    } finally {
        if ($null -ne $Stream) {
            $Stream.Dispose()
        }
        if ($null -ne $Hasher) {
            $Hasher.Dispose()
        }
    }
}
