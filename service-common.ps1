$ErrorActionPreference = 'Stop'
$QBotRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$QBotRuntime = Join-Path $QBotRoot 'runtime'
$QBotStop = Join-Path $QBotRuntime 'STOP'
$QBotNode = Join-Path $QBotRoot 'vendor\napcat\node.exe'
$QBotScript = Join-Path $QBotRoot 'live.py'
$QBotPorts = @(5701, 5702, 6099)

function Get-QBotPython {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $resolved = & python -c 'import sys; print(sys.executable)'
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $resolved = & py -3 -c 'import sys; print(sys.executable)'
    } else { throw 'Python 3 is not installed or not on PATH.' }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath "$resolved" -PathType Leaf)) {
        throw 'Cannot resolve Python executable.'
    }
    return "$resolved"
}

function Enter-QBotServiceLock {
    New-Item -ItemType Directory -Force -Path $QBotRuntime | Out-Null
    for ($i = 0; $i -lt 50; $i++) {
        try {
            return [System.IO.File]::Open((Join-Path $QBotRuntime 'service.lock'),
                [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None)
        } catch [System.IO.IOException] { Start-Sleep -Milliseconds 100 }
    }
    throw 'Another start/stop operation is in progress. Please retry shortly.'
}

function Get-QBotOwnedProcesses {
    $escapedScript = [regex]::Escape($QBotScript)
    Get-CimInstance Win32_Process | ForEach-Object {
        if ($_.ExecutablePath -eq $QBotNode) {
            [pscustomobject]@{Role='NapCat'; Id=$_.ProcessId}
        } elseif ($_.Name -like 'python*' -and $_.CommandLine -match "(?:`"|\s)$escapedScript(?:`"|\s|$)") {
            [pscustomobject]@{Role='Bot'; Id=$_.ProcessId}
        }
    }
}

function Get-QBotListeners {
    @(Get-NetTCPConnection -LocalPort $QBotPorts -State Listen -ErrorAction SilentlyContinue)
}

function Stop-QBotOwnedProcesses {
    foreach ($process in @(Get-QBotOwnedProcesses)) {
        Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
    }
}

function Write-QBotStatus {
    $owned = @(Get-QBotOwnedProcesses)
    $listeners = @(Get-QBotListeners)
    $stopped = Test-Path -LiteralPath $QBotStop
    if ($owned.Count -eq 0 -and $listeners.Count -eq 0) {
        Write-Output 'QBot: STOPPED'
    } elseif ($stopped) {
        Write-Output 'QBot: STOP requested; automatic sending disabled.'
    } elseif (@($owned | Where-Object Role -eq 'Bot').Count -gt 0 -and
              @($owned | Where-Object Role -eq 'NapCat').Count -gt 0 -and
              5702 -in $listeners.LocalPort -and 6099 -in $listeners.LocalPort) {
        Write-Output 'QBot: local processes are running.'
    } else {
        Write-Output 'QBot: PARTIAL state or occupied ports. Run stop.cmd before starting again.'
    }
    foreach ($entry in $owned) { Write-Output ("{0} PID: {1}" -f $entry.Role, $entry.Id) }
    foreach ($entry in $listeners) {
        Write-Output ("Listener: {0}:{1} (PID {2})" -f $entry.LocalAddress,$entry.LocalPort,$entry.OwningProcess)
    }
    if (-not $stopped -and @($owned | Where-Object Role -eq 'NapCat').Count -gt 0 -and 6099 -in $listeners.LocalPort) {
        $pythonExe = Get-QBotPython
        & $pythonExe -X utf8 (Join-Path $QBotRoot 'login_status.py')
        if ($LASTEXITCODE -ne 0) { Write-Output 'QQ login status unavailable; check local logs.' }
    }
    Write-Output 'Send whitelist: 312579123 (test), 1050045834 (production), 1006808801, 929618496. Global interval: 5 seconds.'
    Write-Output 'Admin dashboard runs as a separate process: use start-admin.cmd / status-admin.cmd.'
}
