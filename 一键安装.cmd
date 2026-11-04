@echo off
setlocal
cd /d "%~dp0"
title Personal Memory - Quick Install
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "result=%ERRORLEVEL%"
echo.
if not "%result%"=="0" echo Installation was not completed. Keep this window open and check the error above.
pause
exit /b %result%
