@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_lookup_source.ps1" -Uninstall
echo.
pause
