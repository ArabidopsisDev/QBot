. (Join-Path $PSScriptRoot 'admin-common.ps1')
try {
    $owned = @(Get-AdminProcesses)
    if ($owned.Count -eq 0) {
        Write-Output 'Admin dashboard is not running; nothing to stop.'
        if (Test-Path -LiteralPath $AdminRecord) { Remove-Item -LiteralPath $AdminRecord -Force }
        Write-AdminStatus
        return
    }
    foreach ($process in $owned) {
        Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
    }
    for ($i=0; $i -lt 24; $i++) {
        if (@(Get-AdminProcesses).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    if (@(Get-AdminProcesses).Count -gt 0) { throw 'The dashboard process is still running.' }
    if (Test-Path -LiteralPath $AdminRecord) { Remove-Item -LiteralPath $AdminRecord -Force }
    Write-Output 'Admin dashboard stopped. The bot service was not touched.'
    Write-AdminStatus
} catch {
    Write-Host ("ADMIN STOP FAILED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
