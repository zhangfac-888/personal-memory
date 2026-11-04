param(
  [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'PersonalMemoryShare\Data'),
  [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
  throw 'Python environment is missing. Run the quick installer first.'
}
& $pythonExe -B (Join-Path $projectRoot 'tools\first_run.py') init --data-dir $DataDir --port $Port
if ($LASTEXITCODE -ne 0) { throw 'Instance identity check failed. No library was opened.' }
Push-Location -LiteralPath $projectRoot
try { & $pythonExe -B -m backend --data-dir $DataDir --port $Port }
finally { Pop-Location }
