param(
  [string]$Root = (Join-Path $env:LOCALAPPDATA 'PersonalMemoryShare'),
  [int]$Port = 8765,
  [switch]$KeepClients,
  [switch]$RemoveData,
  [switch]$Yes,
  [switch]$Json
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = [IO.Path]::GetFullPath($Root)
$app = Join-Path $root 'App'
$data = Join-Path $root 'Data'
$port = $Port
$identity = Join-Path $root 'share-installation.json'
$report = @{ status = 'not_started'; app = $app; data = $data; port = $port; service = 'unknown'; startup_task = 'unknown'; clients = @{}; data_removed = $false; warnings = @() }

function Say([string]$text) { if (-not $Json) { Write-Host $text } }
function Write-Result { Write-Host ('PM_UNINSTALL ' + ($script:report | ConvertTo-Json -Compress -Depth 6)) }
function Same-Path([string]$left, [string]$right) {
    if (-not $left -or -not $right) { return $false }
    return ([IO.Path]::GetFullPath($left).TrimEnd('\') -eq [IO.Path]::GetFullPath($right).TrimEnd('\'))
}
function Confirm-Yes([string]$question) {
    if ($Yes) { return $true }
    return ((Read-Host $question) -match '^(y|yes)$')
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

try {
    Say 'Personal Memory - uninstall'
    Say "Root: $root"
    $owned = Read-Json $identity
    if ($null -eq $owned) {
        $report.status = 'refused'
        $report.error = 'share-installation.json is missing; this root is not an owned installation'
        Say $report.error
        Write-Result
        exit 3
    }
    if (-not (Same-Path $owned.app $app) -or -not (Same-Path $owned.data $data)) {
        $report.status = 'refused'
        $report.error = 'Owned installation identity does not match this root; nothing was changed'
        Say $report.error
        Write-Result
        exit 3
    }
    if (-not (Confirm-Yes 'Stop the service, remove startup, and delete the application copy? [Y/N]')) {
        $report.status = 'cancelled'
        Write-Result
        exit 0
    }

    $python = Join-Path $app '.venv\Scripts\python.exe'
    $env:PERSONAL_MEMORY_DATA = $data
    $env:PERSONAL_MEMORY_BASE = "http://127.0.0.1:$port"

    # 1. Disconnect clients whose automation config belongs to this installation.
    if ($KeepClients) {
        Say 'Client integrations were left unchanged (-KeepClients).'
    } else {
        $automation = Join-Path $env:USERPROFILE '.personal-memory\automation'
        if ((Test-Path -LiteralPath $python -PathType Leaf) -and (Test-Path -LiteralPath $automation)) {
            foreach ($folder in Get-ChildItem -LiteralPath $automation -Directory -ErrorAction SilentlyContinue) {
                $config = Read-Json (Join-Path $folder.FullName 'config.json')
                if ($null -eq $config) {
                    # Read-only MCP connections keep no automation config, and this
                    # package has no verified way to remove their client entry.
                    # Report it instead of silently leaving it behind.
                    $mcp = Read-Json (Join-Path $folder.FullName 'mcp.json')
                    if ($null -ne $mcp) {
                        $mcpMarker = [string]$mcp.data_marker
                        if ($mcpMarker -and (Same-Path (Split-Path -Parent $mcpMarker) $data)) {
                            $mcpTarget = [string]$mcp.target
                            $report.clients[$folder.Name] = 'mcp_entry_left'
                            $report.warnings += ('client ' + $folder.Name + ': read-only MCP entry left in ' + $mcpTarget)
                            Say ("  $($folder.Name): read-only MCP entry was left in " + $mcpTarget)
                            Say '    remove the personal_memory entry from that file if you no longer want it'
                        }
                    }
                    continue
                }
                $marker = [string]$config.data_marker
                if (-not $marker -or -not (Same-Path (Split-Path -Parent $marker) $data)) { continue }
                $output = ''
                $code = 1
                try {
                    if ($folder.Name -eq 'claude') {
                        $entry = Join-Path $app 'connect-agent\scripts\connect_claude.py'
                        $output = (& $python -B $entry uninstall 2>&1 | Out-String)
                    } else {
                        $entry = Join-Path $app 'connect-agent\scripts\connect_agent.py'
                        $output = (& $python -B $entry disable --host $folder.Name 2>&1 | Out-String)
                    }
                    $code = $LASTEXITCODE
                } catch {
                    $output = $_.Exception.Message
                    $code = 1
                }
                if ($code -eq 0) {
                    $report.clients[$folder.Name] = 'disconnected'
                    Say ("  $($folder.Name): disconnected")
                } else {
                    $message = ($output -replace '\s+', ' ').Trim()
                    if ($message.Length -gt 300) { $message = $message.Substring(0, 300) }
                    $report.clients[$folder.Name] = 'not_disconnected'
                    $report.warnings += ('client ' + $folder.Name + ': ' + $message)
                    Say ("  $($folder.Name): NOT disconnected - review that client before deleting anything manually")
                }
            }
        } else {
            $report.warnings += 'application runtime or client automation folder is absent; client cleanup was skipped'
        }
    }

    # 2. Stop only the process that runs this installation.
    $listener = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    if ($listener.Count -gt 0) {
        $owners = @($listener | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -eq 1) {
            $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owners[0])
            if ($process.CommandLine -and $process.CommandLine.IndexOf($app, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $process.CommandLine.IndexOf($data, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                Stop-Process -Id $owners[0] -Force
                for ($n = 0; $n -lt 20; $n++) {
                    if (-not (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)) { break }
                    Start-Sleep -Milliseconds 250
                }
                $report.service = 'stopped'
                Say '  service stopped'
            } else {
                $report.service = 'foreign_listener'
                $report.warnings += 'the port listener does not belong to this installation; it was not stopped'
                Say '  port listener does not belong to this installation; left running'
            }
        } else {
            $report.service = 'unknown_listener'
            $report.warnings += 'could not identify a single listener; nothing was stopped'
            Say '  could not identify a single listener; nothing was stopped'
        }
    } else {
        $report.service = 'not_running'
        Say '  service was not running'
    }

    # 3. Remove only a startup task whose action matches this installation.
    $taskName = Get-TaskName
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        $pythonw = Join-Path $app '.venv\Scripts\pythonw.exe'
        $launcher = Join-Path $app 'start-personal-memory-background.pyw'
        $expectedArgs = '"' + $launcher + '" --data-dir "' + $data + '" --port ' + $port
        if ((Same-Path $task.Actions.Execute $pythonw) -and ($task.Actions.Arguments -eq $expectedArgs)) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            $report.startup_task = 'removed'
            Say '  startup task removed'
        } else {
            $report.startup_task = 'mismatch'
            $report.warnings += 'a different startup task uses the same name; it was left unchanged'
            Say '  startup task does not match this installation; left unchanged'
        }
    } else {
        $report.startup_task = 'absent'
        Say '  no startup task'
    }

    # 4. Remove the application copy and the downloaded runtime; keep data by default.
    if (Test-Path -LiteralPath $app) {
        Remove-Item -LiteralPath $app -Recurse -Force
    }
    $runtime = Join-Path $root 'Runtime'
    if (Test-Path -LiteralPath $runtime) {
        Remove-Item -LiteralPath $runtime -Recurse -Force
    }
    if ($RemoveData) {
        $instance = Read-Json (Join-Path $data 'installation.json')
        $marker = Test-Path -LiteralPath (Join-Path $data '.personal-memory-data') -PathType Leaf
        if ($null -eq $instance -or -not $marker -or -not (Same-Path $instance.data_root $data)) {
            $report.warnings += 'data directory identity did not match; data was preserved'
            Say '  data directory identity did not match; data preserved'
        } elseif (-not (Confirm-Yes 'Delete ALL memories, backups and the local key in the data directory? Type Y to confirm. [Y/N]')) {
            Say '  data deletion was not confirmed; data preserved'
        } else {
            Remove-Item -LiteralPath $data -Recurse -Force
            $report.data_removed = $true
            Say '  data directory deleted'
        }
    } else {
        Say "  data preserved at $data (use -RemoveData to delete it explicitly)"
    }

    Remove-Item -LiteralPath $identity -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $root) {
        $left = @(Get-ChildItem -LiteralPath $root -Force -ErrorAction SilentlyContinue)
        if ($left.Count -eq 0) { Remove-Item -LiteralPath $root -Force -ErrorAction SilentlyContinue }
    }
    $report.status = 'uninstalled'
    Say 'Uninstalled. The extracted package folder was not touched.'
    Write-Result
    exit 0
} catch {
    $report.status = 'failed'
    $report.error = $_.Exception.Message
    if (-not $Json) { Write-Host ('Uninstall did not complete: ' + $_.Exception.Message) -ForegroundColor Red }
    Write-Result
    exit 1
}
