@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0YandexStationAdvanced.exe" (
  start "Yandex Station Advanced" "%~dp0YandexStationAdvanced.exe"
  exit /b 0
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" -InstallOnly
if errorlevel 1 goto failed
start "Yandex Station Advanced" "%~dp0.runtime\pythonw.exe" "%~dp0advanced_entry.py"
exit /b 0
:failed
echo Setup failed. Review the specific message above.
pause
exit /b 1
