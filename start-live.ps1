param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service-common.ps1')
$serviceLock = $null
$launching = $false
try {
    $serviceLock = Enter-QBotServiceLock
    Set-Location -LiteralPath $QBotRoot
    $pythonExe = Get-QBotPython
    & $pythonExe -X utf8 (Join-Path $QBotRoot 'check_ready.py')
    if ($LASTEXITCODE -ne 0) { throw 'Safety preflight failed; no service started.' }
    & $QBotNode (Join-Path $QBotRoot 'test_napcat_guard.cjs')
    if ($LASTEXITCODE -ne 0) { throw 'NapCat send guard failed; no service started.' }
    if ($CheckOnly) {
        Write-Output 'CHECK ONLY passed. No process started; STOP state preserved.'
        return
    }
    $owned = @(Get-QBotOwnedProcesses)
    $listeners = @(Get-QBotListeners)
    if (-not (Test-Path -LiteralPath $QBotStop) -and
        @($owned | Where-Object Role -eq 'Bot').Count -gt 0 -and
        @($owned | Where-Object Role -eq 'NapCat').Count -gt 0 -and
        5702 -in $listeners.LocalPort -and 6099 -in $listeners.LocalPort) {
        Write-Output 'Already running; no duplicate instance started.'
        Write-QBotStatus
        return
    }
    if ($owned.Count -gt 0 -or $listeners.Count -gt 0) {
        throw 'Existing processes or occupied ports detected. Run stop.cmd first; unrelated programs will not be killed.'
    }
    if (Test-Path -LiteralPath $QBotStop) { Remove-Item -LiteralPath $QBotStop }
    $launching = $true
    $botProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-X','utf8',('"{0}"' -f $QBotScript)) `
        -WorkingDirectory $QBotRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $QBotRuntime 'bot.log') `
        -RedirectStandardError (Join-Path $QBotRuntime 'bot-error.log')
    $napcatProcess = Start-Process -FilePath $QBotNode -ArgumentList @('index.js') `
        -WorkingDirectory (Split-Path -Parent $QBotNode) -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $QBotRuntime 'napcat.log') `
        -RedirectStandardError (Join-Path $QBotRuntime 'napcat-error.log')
    @{bot=$botProcess.Id; napcat=$napcatProcess.Id; root=$QBotRoot} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $QBotRuntime 'processes.json') -Encoding UTF8
    $ready = $false
    for ($i=0; $i -lt 40; $i++) {
        if (Test-Path -LiteralPath $QBotStop) { throw 'Startup cancelled by Stop.' }
        if (-not (Get-Process -Id $botProcess.Id -ErrorAction SilentlyContinue) -or
            -not (Get-Process -Id $napcatProcess.Id -ErrorAction SilentlyContinue)) {
            throw 'A service exited during startup. Check runtime logs.'
        }
        $ports = @(Get-QBotListeners).LocalPort
        if (5702 -in $ports -and 6099 -in $ports) { $ready=$true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Startup timed out. Check runtime logs.' }
    Write-QBotStatus
    Write-Output 'If QQ needs login, scan vendor\napcat\napcat\cache\qrcode.png.'
    Write-Output 'Send group commands after QQ reports isLogin=true and coreReady=true.'
} catch {
    if ($launching) {
        New-Item -ItemType File -Force -Path $QBotStop | Out-Null
        Stop-QBotOwnedProcesses
    }
    Write-Host ("START FAILED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if ($null -ne $serviceLock) { $serviceLock.Dispose() }
}
