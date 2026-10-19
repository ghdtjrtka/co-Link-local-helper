@echo off
powershell.exe -NoProfile -File "%~dp0native\install-assistant.ps1" -SourceDirectory "%~dp0." -Version 0.1.5
if errorlevel 1 pause
