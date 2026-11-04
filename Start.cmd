@echo off
setlocal
set "APP=%LOCALAPPDATA%\PersonalMemoryShare\App"
set "DATA=%LOCALAPPDATA%\PersonalMemoryShare\Data"
if not exist "%APP%\.venv\Scripts\pythonw.exe" (
  echo Please run the quick installer first.
  pause
  exit /b 1
)
start "" "%APP%\.venv\Scripts\pythonw.exe" "%APP%\start-personal-memory-background.pyw" --data-dir "%DATA%" --port 8765
start "" "http://127.0.0.1:8765/"
