param([switch]$CheckOnly)
. (Join-Path $PSScriptRoot 'admin-common.ps1')
New-Item -ItemType Directory -Force -Path $AdminRuntime | Out-Null
try {
    if (@(Get-AdminProcesses).Count -gt 0 -or (Test-AdminPort)) {
        Write-Output 'Already running; no duplicate instance started.'
        Write-AdminStatus
        return
    }
    $python = Get-AdminPython
    & $python -X utf8 -c "import admin" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'admin.py failed to import; nothing started.' }
    if ($CheckOnly) {
        Write-Output 'CHECK ONLY passed. No process started.'
        return
    }
    $process = Start-Process -FilePath $python `
        -ArgumentList @('-X','utf8',('"{0}"' -f $AdminScript)) `
        -WorkingDirectory $AdminRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $AdminLog -RedirectStandardError $AdminErrorLog
    @{admin=$process.Id; root=$AdminRoot; port=$AdminPort} | ConvertTo-Json |
        Set-Content -LiteralPath $AdminRecord -Encoding UTF8
    $ready = $false
    for ($i=0; $i -lt 60; $i++) {
        if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
            throw 'The dashboard exited during startup. Check runtime\admin-error.log.'
        }
        if (Test-AdminPort) { $ready = $true; break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'Startup timed out. Check runtime\admin-error.log.' }
    Write-AdminStatus
    Write-Output 'Panel edits need NO bot restart: this process re-reads web\admin.html on every request.'
} catch {
    if ($null -ne $process) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    Write-Host ("ADMIN START FAILED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
