@echo off
setlocal
set "APP=%LOCALAPPDATA%\PersonalMemoryShare\App"
set "PERSONAL_MEMORY_DATA=%LOCALAPPDATA%\PersonalMemoryShare\Data"
set "PERSONAL_MEMORY_BASE=http://127.0.0.1:8765"
if not exist "%APP%\.venv\Scripts\python.exe" (
  echo Personal Memory was not found at the default location:
  echo   %APP%
  echo If you installed it with -Root or -Port, connect from the package instead:
  echo   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes -Connect claude
  pause
  exit /b 1
)
"%APP%\.venv\Scripts\python.exe" -B "%APP%\tools\connect_selected.py"
set "result=%ERRORLEVEL%"
pause
exit /b %result%
