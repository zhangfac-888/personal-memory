@echo off
setlocal
set "APP=%LOCALAPPDATA%\PersonalMemoryShare\App"
set "PERSONAL_MEMORY_DATA=%LOCALAPPDATA%\PersonalMemoryShare\Data"
set "PERSONAL_MEMORY_BASE=http://127.0.0.1:8765"
if not exist "%APP%\.venv\Scripts\python.exe" (
  echo Please install Personal Memory first.
  pause
  exit /b 1
)
"%APP%\.venv\Scripts\python.exe" -B "%APP%\tools\connect_selected.py"
set "result=%ERRORLEVEL%"
pause
exit /b %result%
