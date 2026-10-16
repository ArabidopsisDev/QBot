@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0status-live.ps1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0status-admin.ps1"
set "QBOT_EXIT=%ERRORLEVEL%"
echo.
if /I not "%~1"=="--no-pause" pause
exit /b %QBOT_EXIT%
