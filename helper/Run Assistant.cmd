@echo off
cd /d "%~dp0"
echo co-Link conversion assistant. Keep this window open while using the conversion page.
echo https://colink.namanavi.com/dongle
"%~dp0runtime\python\python.exe" -B "%~dp0helper_server.py" --origin https://colink.namanavi.com --enable-conversion
if errorlevel 1 pause
