$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service-common.ps1')
$serviceLock = $null
try {
    New-Item -ItemType Directory -Force -Path $QBotRuntime | Out-Null
    # Set STOP immediately, even if a concurrent start holds the operation lock.
    New-Item -ItemType File -Force -Path $QBotStop | Out-Null
    $serviceLock = Enter-QBotServiceLock
    foreach ($process in @(Get-QBotOwnedProcesses | Where-Object Role -eq 'NapCat')) {
        Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
    }
    for ($i=0; $i -lt 24; $i++) {
        if (@(Get-QBotOwnedProcesses).Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    }
    if (@(Get-QBotOwnedProcesses).Count -gt 0) { Stop-QBotOwnedProcesses }
    Start-Sleep -Milliseconds 300
    if (@(Get-QBotOwnedProcesses).Count -gt 0) { throw 'An owned process is still running.' }
    Write-Output 'QBot stopped. STOP remains active; learning data is preserved.'
    $listeners = @(Get-QBotListeners)
    if ($listeners.Count -gt 0) {
        Write-Output 'Some ports remain occupied by unrecognized processes; they were not killed.'
        $listeners | Select-Object LocalAddress,LocalPort,OwningProcess | Format-Table
    } else { Write-Output 'All QBot ports are closed.' }
} catch {
    Write-Host ("STOP FAILED: " + $_.Exception.Message + ' STOP flag remains active.') -ForegroundColor Red
    exit 1
} finally {
    if ($null -ne $serviceLock) { $serviceLock.Dispose() }
}
