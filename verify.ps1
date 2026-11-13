param(
  [string]$Root = (Join-Path $env:LOCALAPPDATA 'PersonalMemoryShare'),
  [int]$Port = 8765,
  [switch]$SkipPackage,
  [switch]$Json
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = [IO.Path]::GetFullPath($Root)
$app = Join-Path $root 'App'
$data = Join-Path $root 'Data'
$port = $Port
$identity = Join-Path $root 'share-installation.json'
$checks = New-Object System.Collections.ArrayList
$report = @{ root = $root; app = $app; data = $data; port = $port; snapshot = $null; checks = $null; clients = @{}; ok = $false }

function Say([string]$text) { if (-not $Json) { Write-Host $text } }
function Add-Check([string]$name, [bool]$ok, [string]$detail, [bool]$required = $true, [bool]$warnOnFail = $true) {
    $null = $script:checks.Add([pscustomobject]@{ name = $name; ok = $ok; required = $required; warn = $warnOnFail; detail = $detail })
}
function Same-Path([string]$left, [string]$right) {
    if (-not $left -or -not $right) { return $false }
    return ([IO.Path]::GetFullPath($left).TrimEnd('\') -eq [IO.Path]::GetFullPath($right).TrimEnd('\'))
}
function Get-TaskName {
    $defaultRoot = Join-Path $env:LOCALAPPDATA 'PersonalMemoryShare'
    if (Same-Path $script:root $defaultRoot) { return 'PersonalMemory_SharedLocalService' }
    return ('PersonalMemory_SharedLocalService_' + $script:port)
}
function Read-Json([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}
function Finish([int]$code) {
    $report.checks = @($script:checks)
    $report.ok = -not (@($script:checks) | Where-Object { $_.required -and -not $_.ok })
    Write-Host ('PM_VERIFY ' + ($script:report | ConvertTo-Json -Compress -Depth 6))
    exit $code
}

try {
    Say 'Personal Memory - read-only verification'
    Say "Root: $root"
    Say "Port: $port"

    # 1. Package integrity exactly as installed.
    if ($SkipPackage) {
        Add-Check 'package' $true 'skipped by -SkipPackage' $false
    } else {
        $manifest = Read-Json (Join-Path $app 'package-manifest.json')
        if ($null -eq $manifest) {
            Add-Check 'package' $false 'package-manifest.json is missing from the application directory'
        } else {
            $report.snapshot = $manifest.snapshot
            $bad = New-Object System.Collections.ArrayList
            $count = 0
            foreach ($file in $manifest.files.PSObject.Properties) {
                $count++
                $relative = $file.Name
                if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[\\/])\.\.([\\/]|$)') { $null = $bad.Add($relative); continue }
                $path = Join-Path $app $relative
                if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $null = $bad.Add($relative); continue }
                if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.Value) { $null = $bad.Add($relative) }
            }
            if ($bad.Count -eq 0) {
                Add-Check 'package' $true ("$count files match the manifest")
            } else {
                $preview = ($bad | Select-Object -First 5) -join ', '
                Add-Check 'package' $false ("$($bad.Count) file(s) differ: $preview")
            }
        }
    }

    # 2. Installation identity and data marker.
    $owned = Read-Json $identity
    if ($null -eq $owned) {
        Add-Check 'identity' $false 'share-installation.json is missing; this root is not an owned installation'
    } else {
        $sameTarget = (Same-Path $owned.app $app) -and (Same-Path $owned.data $data)
        Add-Check 'identity' $sameTarget ('app=' + $owned.app + '; data=' + $owned.data)
        if ($manifest) {
            Add-Check 'identity_snapshot' ($owned.snapshot -eq $report.snapshot) ('installed=' + $owned.snapshot + '; package=' + $report.snapshot)
        }
    }
    $instance = Read-Json (Join-Path $data 'installation.json')
    if ($null -eq $instance) {
        Add-Check 'instance' $false 'Data\installation.json is missing'
    } else {
        $okInstance = ($instance.format_version -eq 1) -and (Same-Path $instance.data_root $data) -and (Same-Path $instance.source_root $app) -and ([int]$instance.port -eq $port)
        Add-Check 'instance' $okInstance ('data_root=' + $instance.data_root + '; source_root=' + $instance.source_root + '; port=' + $instance.port)
        Add-Check 'discovery' ($instance.discovery_enabled -eq $false) ('discovery_enabled=' + $instance.discovery_enabled + ' (false is expected for this share install)') $false $false
    }
    Add-Check 'data_marker' (Test-Path -LiteralPath (Join-Path $data '.personal-memory-data') -PathType Leaf) '.personal-memory-data marker'

    # 3. Running service owned by this installation.
    $healthy = $false
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3
        $healthy = ($health.status -eq 'ok')
    } catch {}
    Add-Check 'service_health' $healthy ("http://127.0.0.1:$port/api/health")
    $listener = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    if ($listener.Count -eq 0) {
        Add-Check 'service_process' $false 'no listener on the port' $false
    } else {
        $owners = @($listener | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -ne 1) {
            Add-Check 'service_process' $false 'cannot identify a single listener' $false
        } else {
            $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owners[0])
            $launcher = Join-Path $app 'start-personal-memory-background.pyw'
            $owned_ok = $process.CommandLine -and $process.CommandLine.IndexOf($launcher, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $process.CommandLine.IndexOf($data, [StringComparison]::OrdinalIgnoreCase) -ge 0
            Add-Check 'service_process' $owned_ok ('pid=' + $owners[0] + '; owned=' + [bool]$owned_ok) $false
        }
    }

    # 4. Read-only database integrity and schema.
    $python = Join-Path $app '.venv\Scripts\python.exe'
    $db = Join-Path $data 'memory.db'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Add-Check 'database' $false 'application Python runtime is missing'
    } elseif (-not (Test-Path -LiteralPath $db -PathType Leaf)) {
        Add-Check 'database' $false 'memory.db is missing'
    } else {
        $probe = @'
import json, sqlite3, sys
from pathlib import Path
app, data = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(app))
from backend.db import SCHEMA_VERSION
con = sqlite3.connect((data / "memory.db").resolve().as_uri() + "?mode=ro", uri=True)
try:
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    version = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
finally:
    con.close()
print(json.dumps({"integrity": integrity, "schema": version, "expected": SCHEMA_VERSION}))
'@
        $probeOut = $probe | & $python -B -X utf8 - $app $data
        if ($LASTEXITCODE -ne 0) {
            Add-Check 'database' $false ('probe failed with exit code ' + $LASTEXITCODE)
        } else {
            $probeJson = ($probeOut | Out-String).Trim() | ConvertFrom-Json
            $dbOk = ($probeJson.integrity -eq 'ok') -and ([int]$probeJson.schema -eq [int]$probeJson.expected)
            Add-Check 'database' $dbOk ('integrity=' + $probeJson.integrity + '; schema=' + $probeJson.schema + '; expected=' + $probeJson.expected)
            $report.schema = $probeJson.schema
        }
    }

    # 5. Optional login startup task.
    $taskName = Get-TaskName
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Add-Check 'startup_task' $true 'not configured (Start.cmd starts the service manually)' $false
    } else {
        $pythonw = Join-Path $app '.venv\Scripts\pythonw.exe'
        $launcher = Join-Path $app 'start-personal-memory-background.pyw'
        $expectedArgs = '"' + $launcher + '" --data-dir "' + $data + '" --port ' + $port
        $taskOk = (Same-Path $task.Actions.Execute $pythonw) -and ($task.Actions.Arguments -eq $expectedArgs)
        Add-Check 'startup_task' $taskOk ($taskName + '; matches=' + [bool]$taskOk) $false
    }

    # 6. Local-only optional capabilities (informational).
    $keyFile = Test-Path -LiteralPath (Join-Path $data 'secrets\deepseek-key.dpapi') -PathType Leaf
    $keyDetail = 'not configured; AI extraction stays off (value is never read)'
    if ($keyFile) { $keyDetail = 'encrypted key configured (value is never read)' }
    Add-Check 'deepseek_key' $keyFile $keyDetail $false $false
    $ollama = $false
    try {
        $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2
        $ollama = @($tags.models | Where-Object { $_.name -eq 'bge-m3:latest' }).Count -gt 0
    } catch {}
    $semanticDetail = 'unavailable; keyword search only'
    if ($ollama) { $semanticDetail = 'bge-m3:latest reachable through local Ollama' }
    Add-Check 'semantic_model' $ollama $semanticDetail $false $false

    # 7. Client integrations that point at this installation (informational).
    $automation = Join-Path $env:USERPROFILE '.personal-memory\automation'
    if (Test-Path -LiteralPath $automation) {
        foreach ($folder in Get-ChildItem -LiteralPath $automation -Directory -ErrorAction SilentlyContinue) {
            $config = Read-Json (Join-Path $folder.FullName 'config.json')
            if ($null -eq $config) { continue }
            $marker = [string]$config.data_marker
            $ours = $false
            if ($marker) { $ours = Same-Path (Split-Path -Parent $marker) $data }
            $state = 'other'
            if ($ours) { $state = 'ours' }
            $report.clients[$folder.Name] = @{ state = $state; enabled = [bool]$config.enabled; base_url = [string]$config.base_url }
        }
    }
    $claudeSettings = Join-Path $env:USERPROFILE '.claude\settings.json'
    $claudeHooked = $false
    if (Test-Path -LiteralPath $claudeSettings) {
        try {
            $raw = Get-Content -LiteralPath $claudeSettings -Raw -Encoding UTF8
            $claudeHooked = $raw.IndexOf($app, [StringComparison]::OrdinalIgnoreCase) -ge 0
        } catch {}
    }
    $report.clients['claude_settings_reference'] = @{ state = $(if ($claudeHooked) { 'references_this_app' } else { 'not_referenced' }) }

    if (-not $Json) {
        foreach ($check in $checks) {
            $label = 'INFO'
            if ($check.required) { if ($check.ok) { $label = 'PASS' } else { $label = 'FAIL' } }
            elseif (-not $check.ok -and $check.warn) { $label = 'WARN' }
            Write-Host ("[{0}] {1}: {2}" -f $label, $check.name, $check.detail)
        }
    }
    $requiredFailed = @($checks | Where-Object { $_.required -and -not $_.ok })
    if ($requiredFailed.Count -gt 0) { Finish 1 }
    Finish 0
} catch {
    Add-Check 'verify' $false $_.Exception.Message
    if (-not $Json) { Write-Host ('Verification did not complete: ' + $_.Exception.Message) -ForegroundColor Red }
    Finish 1
}
