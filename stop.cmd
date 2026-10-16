@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-live.ps1"
set "QBOT_EXIT=%ERRORLEVEL%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-admin.ps1"
echo.
if /I not "%~1"=="--no-pause" pause
exit /b %QBOT_EXIT%
