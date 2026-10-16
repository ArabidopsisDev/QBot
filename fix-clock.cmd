@echo off
setlocal
rem Self-elevating wrapper: relaunch through UAC when not already elevated.
rem Keep this file ASCII-only: cmd.exe reads .cmd in the OEM codepage, and
rem non-ASCII text here corrupts the command line (learned the hard way).
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix-clock.ps1"
echo.
pause
