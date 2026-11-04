# Compatibility entry point. Use the public quick installer; no eager client repair.
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
if ($CheckOnly) { & (Join-Path $PSScriptRoot 'install.ps1') -CheckOnly }
else { & (Join-Path $PSScriptRoot 'install.ps1') }
exit $LASTEXITCODE
