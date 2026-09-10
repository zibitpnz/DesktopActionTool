@echo off
setlocal
if "%~1"=="" goto interactive
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
exit /b %errorlevel%

:interactive
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" -Check -Pause
exit /b %errorlevel%
