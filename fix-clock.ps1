# Correct this machine's clock. Requires administrator rights.
#
# Root cause on this host: the Windows Time service (w32time) was Stopped with a
# Manual start type, so the clock never synced and drifted about 3 minutes ahead.
# The bot is affected because NapCat mixes QQ server time with local time; a host
# clock that runs minutes fast made fresh messages look out of order.
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI.
$ErrorActionPreference = 'Stop'

function Test-Elevated {
    [bool]((whoami /groups) -match 'S-1-16-12288')
}

function Get-OffsetSeconds {
    # The HTTP Date header is coarse but dependency-free.
    try {
        $response = Invoke-WebRequest -Uri 'https://www.baidu.com' -Method Head `
            -UseBasicParsing -TimeoutSec 10
        $server = [datetime]::Parse($response.Headers['Date']).ToUniversalTime()
        return ((Get-Date).ToUniversalTime() - $server).TotalSeconds
    } catch {
        return $null
    }
}

function Show-Offset([string]$label) {
    $offset = Get-OffsetSeconds
    if ($null -eq $offset) {
        Write-Output ("  {0}: reference time unavailable (no network)" -f $label)
        return
    }
    Write-Output ("  {0}: local - standard = {1:N1} s ({2:N2} min)" -f $label, $offset, ($offset / 60))
}

if (-not (Test-Elevated)) {
    Write-Host 'Administrator rights required: right-click fix-clock.cmd and choose Run as administrator.' -ForegroundColor Red
    exit 1
}

Write-Output '=== BEFORE ==='
Write-Output ('  local time : ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Show-Offset 'offset    '
Write-Output ''

Write-Output '=== 1. Set the Windows Time service to start automatically ==='
Set-Service -Name w32time -StartupType Automatic
Start-Service -Name w32time
Get-Service w32time | Select-Object Name, Status, StartType | Format-Table -AutoSize

Write-Output '=== 2. Point it at reachable time servers ==='
& w32tm /config /manualpeerlist:"ntp.aliyun.com,0x8 time.windows.com,0x8" /syncfromflags:manual /update | Out-Null
Write-Output '  peers: ntp.aliyun.com, time.windows.com'

Write-Output '=== 3. Resync now ==='
$resync = & w32tm /resync /force 2>&1
Write-Output ('  ' + ($resync -join ' '))

Write-Output ''
Write-Output '=== AFTER ==='
Write-Output ('  local time : ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Show-Offset 'offset    '
Write-Output ''
& w32tm /query /status 2>&1 | Select-Object -First 10
