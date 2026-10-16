. (Join-Path $PSScriptRoot 'admin-common.ps1')
Write-AdminStatus
if (Test-Path -LiteralPath $AdminErrorLog) {
    $recent = Get-Content -LiteralPath $AdminErrorLog -ErrorAction SilentlyContinue | Select-Object -Last 5
    if ($recent) {
        Write-Output 'Recent dashboard errors:'
        $recent | ForEach-Object { Write-Output ("  " + $_) }
    }
}
