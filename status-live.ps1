$ErrorActionPreference = 'Stop'
try {
    . (Join-Path $PSScriptRoot 'service-common.ps1')
    Write-QBotStatus
} catch {
    Write-Host ("STATUS FAILED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
