@echo off
set "INSTALLED=%ProgramFiles%\co-Link Conversion Assistant\0.1.8\Run Assistant.cmd"
if not exist "%INSTALLED%" (echo Install first by running Install Assistant.cmd from the extracted package. & pause & exit /b 1)
call "%INSTALLED%"
