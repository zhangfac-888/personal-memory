@echo off
setlocal
set "APP=%LOCALAPPDATA%\PersonalMemoryShare\App"
set "DATA=%LOCALAPPDATA%\PersonalMemoryShare\Data"
if not exist "%APP%\.venv\Scripts\python.exe" (
  echo Please install Personal Memory first.
  pause
  exit /b 1
)
echo AI extraction can send selected text to DeepSeek and incur charges. Auto-capture is configured separately.
"%APP%\.venv\Scripts\python.exe" -B "%APP%\tools\first_run.py" configure-key --data-dir "%DATA%"
set "result=%ERRORLEVEL%"
pause
exit /b %result%
