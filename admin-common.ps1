# Helpers for the dashboard process. The dashboard is deliberately a SEPARATE
# process from the bot: editing the panel (or its Python backend) then only needs
# a dashboard restart, so group messages never hit the bot's restart blackout.
$ErrorActionPreference = 'Stop'
$AdminRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$AdminRuntime = Join-Path $AdminRoot 'runtime'
$AdminPort = 5703
$AdminUrl = "http://127.0.0.1:$AdminPort/"
$AdminRecord = Join-Path $AdminRuntime 'admin.json'
$AdminScript = Join-Path $AdminRoot 'admin.py'
$AdminLog = Join-Path $AdminRuntime 'admin.log'
$AdminErrorLog = Join-Path $AdminRuntime 'admin-error.log'

function Get-AdminPython {
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

function Get-AdminProcesses {
    $escaped = [regex]::Escape($AdminScript)
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*' -and $_.CommandLine -match $escaped
    })
}

function Test-AdminPort {
    $found = @(Get-NetTCPConnection -LocalPort $AdminPort -State Listen -ErrorAction SilentlyContinue)
    if ($found.Count -gt 0) { return $true }
    # Get-NetTCPConnection intermittently returns nothing on this host; netstat is the fallback.
    $line = netstat -ano | Select-String ":$AdminPort\s" | Select-String 'LISTENING'
    return [bool]$line
}

function Write-AdminStatus {
    $running = @(Get-AdminProcesses)
    $listening = Test-AdminPort
    if ($running.Count -gt 0 -and $listening) {
        Write-Output "Admin dashboard: RUNNING  $AdminUrl"
        foreach ($entry in $running) { Write-Output ("  PID {0}" -f $entry.ProcessId) }
    } elseif ($running.Count -gt 0) {
        Write-Output 'Admin dashboard: process is alive but port 5703 is not listening yet.'
    } elseif ($listening) {
        Write-Output 'Admin dashboard: port 5703 is held by another program (not this project).'
    } else {
        Write-Output 'Admin dashboard: STOPPED'
    }
}
