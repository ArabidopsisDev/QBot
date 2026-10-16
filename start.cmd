@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-live.ps1"
set "QBOT_EXIT=%ERRORLEVEL%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-admin.ps1"
echo.
if /I not "%~1"=="--no-pause" pause
exit /b %QBOT_EXIT%
