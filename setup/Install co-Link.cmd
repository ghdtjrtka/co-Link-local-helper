@echo off
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0install-colink.ps1" -PackagePath "%~dp0co-Link-Standard-Desktop-0.1.2-win-x64.zip" -Execute -Interactive
if errorlevel 1 pause
