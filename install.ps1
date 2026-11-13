param(
  [switch]$CheckOnly,
  [switch]$Yes,
  [string]$Root = (Join-Path $env:LOCALAPPDATA 'PersonalMemoryShare'),
  [int]$Port = 8765,
  [switch]$NoStartup,
  [switch]$NoBrowser,
  [string]$Connect = '',
  [switch]$Json
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$source = $PSScriptRoot
$root = [IO.Path]::GetFullPath($Root)
$app = Join-Path $root 'App'
$data = Join-Path $root 'Data'
$port = $Port
$manifestPath = Join-Path $source 'package-manifest.json'
$uvVersion = '0.13.7'
$uvHash = '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021'
$result = @{ status = 'not_started'; app = $app; data = $data; port = $port; snapshot = $null; service = 'unknown'; schema = $null; startup_task = 'unknown'; connected = @{}; warnings = @() }

function Say([string]$text) { if (-not $Json) { Write-Host $text } }
function Note([string]$text) { if (-not $Json) { Write-Host $text } else { $script:result.warnings += $text } }
function Write-Result {
  Write-Host ('PM_RESULT ' + ($script:result | ConvertTo-Json -Compress -Depth 6))
}
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

function Check-Package {
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Missing package-manifest.json. Extract the entire ZIP first.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($file in $manifest.files.PSObject.Properties) {
        $relative = $file.Name
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[\\/])\.\.([\\/]|$)') { throw 'Invalid package member.' }
        $path = Join-Path $source $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing package file: $relative" }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.Value) { throw "File checksum mismatch: $relative" }
    }
    return $manifest
}

function Download-VerifiedUv([string]$destination) {
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $client = New-Object Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(5)
    $url = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"
    try {
        for ($n = 0; $n -lt 10; $n++) {
            $uri = [Uri]$url
            if ($uri.Scheme -ne 'https' -or $uri.Host -notin @('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')) { throw 'Unapproved download redirect.' }
            $response = $client.GetAsync($uri).GetAwaiter().GetResult()
            try {
                $code = [int]$response.StatusCode
                if ($code -in @(301, 302, 303, 307, 308)) {
                    $url = ([Uri]::new($uri, $response.Headers.Location)).AbsoluteUri
                    continue
                }
                $null = $response.EnsureSuccessStatusCode()
                [IO.File]::WriteAllBytes($destination, $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult())
                if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $uvHash) { throw 'uv checksum mismatch; nothing will be executed.' }
                return
            } finally { $response.Dispose() }
        }
        throw 'Too many download redirects.'
    } finally { $client.Dispose(); $handler.Dispose() }
}

function Invoke-Connections([string]$spec) {
    $allowed = @('claude', 'codex', 'workbuddy', 'opencode')
    $wanted = @($spec.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
    foreach ($client in $wanted) {
        if ($allowed -notcontains $client) { throw "Unsupported -Connect value: $client" }
    }
    $python = Join-Path $app '.venv\Scripts\python.exe'
    $env:PERSONAL_MEMORY_DATA = $data
    $env:PERSONAL_MEMORY_BASE = "http://127.0.0.1:$port"
    foreach ($client in $wanted) {
        # Never redirect an existing client that already belongs to another
        # installation on this machine: its config, token and hooks must stay put.
        $existingConfig = Join-Path $env:USERPROFILE ('.personal-memory\automation\' + $client + '\config.json')
        if (Test-Path -LiteralPath $existingConfig -PathType Leaf) {
            $existing = Get-Content -LiteralPath $existingConfig -Raw -Encoding UTF8 | ConvertFrom-Json
            $marker = [string]$existing.data_marker
            $foreign = $false
            if ($marker) {
                $foreign = -not (Same-Path (Split-Path -Parent $marker) $data)
            } elseif ($existing.base_url) {
                $foreign = ([string]$existing.base_url -ne "http://127.0.0.1:$port")
            }
            if ($foreign) {
                $script:result.connected[$client] = 'skipped_other_installation'
                Say "  $client is already connected to a different Personal Memory installation."
                Say '  Uninstall that installation first; nothing was changed.'
                continue
            }
        }
        Say ("Connecting $client ...")
        if ($client -eq 'claude') {
            Say 'Claude connection writes MCP plus automatic-capture/task-enforcement hooks to your Claude Code settings.'
            Say 'Captured turns are stored locally; configured extraction can send selected text to DeepSeek.'
            $entry = Join-Path $app 'connect-agent\scripts\connect_claude.py'
        } else {
            Say "$client connection adds a read-only MCP entry only; it does not enable capture or Active writes."
            $entry = Join-Path $app 'connect-agent\scripts\connect_agent.py'
        }
        $output = ''
        $code = 1
        try {
            if ($client -eq 'claude') {
                $output = (& $python -B $entry install 2>&1 | Out-String)
            } else {
                $output = (& $python -B $entry mcp --host $client 2>&1 | Out-String)
            }
            $code = $LASTEXITCODE
        } catch {
            $output = $_.Exception.Message
            $code = 1
        }
        if ($code -eq 0) {
            $script:result.connected[$client] = 'configured'
            Say "  $client configured. Restart/reload that client and confirm its own trust prompt."
        } else {
            $script:result.connected[$client] = 'failed'
            $message = ($output -replace '\s+', ' ').Trim()
            if ($message.Length -gt 400) { $message = $message.Substring(0, 400) }
            Say "  $client connection was not completed: $message"
            Say '  Installation itself is complete; retry with Connect-Agents.cmd after reviewing that client.'
        }
    }
}

try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'This preview supports Windows x64 only.' }
    if ($port -lt 1024 -or $port -gt 65535) { throw 'Choose a port between 1024 and 65535.' }
    if (-not [IO.Path]::IsPathRooted($root)) { throw '-Root must be an absolute path.' }
    if ($Connect) {
        foreach ($client in @($Connect.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })) {
            if (@('claude', 'codex', 'workbuddy', 'opencode') -notcontains $client) { throw "Unsupported -Connect value: $client" }
        }
    }
    $manifest = Check-Package
    $result.snapshot = $manifest.snapshot
    if ($CheckOnly) {
        $result.status = 'package_verified'
        $result.service = 'not_checked'
        if (-not $Json) {
            [pscustomobject]@{Package='verified';Files=@($manifest.files.PSObject.Properties).Count;Snapshot=$manifest.snapshot;Application=$app;Data=$data;Port=$port;Task=(Get-TaskName);InstallExecuted=$false} | ConvertTo-Json
        }
        Write-Result
        exit 0
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Say 'Personal Memory - Windows quick install'
    Say 'Downloads Python/dependencies from official sources. Creates a local service and a login-startup task.'
    Say "App: $app"
    Say "Data: $data"
    Say "Port: $port"
    Say 'No existing memories or AI-client settings will be imported. Network access is required.'
    if (-not (Confirm-Yes 'Install now? [Y/N]')) { $result.status = 'cancelled'; Write-Result; exit 0 }

    $identity = Join-Path $root 'share-installation.json'
    $taskName = Get-TaskName
    if (Test-Path -LiteralPath $app) {
        if (-not (Test-Path -LiteralPath $identity)) { throw 'Application directory already exists without an owned installation marker. Nothing was replaced.' }
        $owned = Get-Content -LiteralPath $identity -Raw | ConvertFrom-Json
        if (-not (Same-Path $owned.app $app) -or -not (Same-Path $owned.data $data) -or $owned.snapshot -ne $manifest.snapshot) { throw 'Different existing installation. Run uninstall.ps1 first; no automatic upgrade or migration was performed.' }
    } elseif (Test-Path -LiteralPath $data) {
        # A previous uninstall may keep data on purpose. Adopt it only when the
        # stored instance identity already points at this same app, data and port.
        $instance = $null
        $instancePath = Join-Path $data 'installation.json'
        if (Test-Path -LiteralPath $instancePath -PathType Leaf) {
            $instance = Get-Content -LiteralPath $instancePath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        $marker = Test-Path -LiteralPath (Join-Path $data '.personal-memory-data') -PathType Leaf
        $adoptable = $instance -and (Same-Path $instance.data_root $data) -and (Same-Path $instance.source_root $app) -and ([int]$instance.port -eq $port) -and $marker
        if (-not $adoptable) {
            throw 'Data directory already exists without a matching installation identity. No existing library will be reset or migrated.'
        }
        Say 'Kept data from a previous uninstall matches this installation; existing memories are preserved.'
    }
    $listener = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    if ($listener.Count -gt 0) {
        if (-not (Test-Path -LiteralPath $identity)) { throw "Port $port is already in use. No service was stopped." }
        $owners = @($listener | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -ne 1) { throw 'Cannot identify local listener.' }
        $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owners[0])
        if (-not $process.CommandLine -or $process.CommandLine.IndexOf($app, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or $process.CommandLine.IndexOf($data, [StringComparison]::OrdinalIgnoreCase) -lt 0) { throw 'Port belongs to another process. Nothing was stopped.' }
        $running = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 5
        if ($running.status -ne 'ok') { throw 'Owned service health check failed.' }
        Say 'Already installed and running; data preserved.'
        $result.status = 'already_installed'
        $result.service = 'running'
        if ($Connect) {
            Invoke-Connections $Connect
            if (@($result.connected.Values | Where-Object { $_ -ne 'configured' }).Count -gt 0) { $result.status = 'already_installed_connect_incomplete'; Write-Result; exit 2 }
        }
        Write-Result
        if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
        exit 0
    }
    $pythonw = Join-Path $app '.venv\Scripts\pythonw.exe'
    $launcher = Join-Path $app 'start-personal-memory-background.pyw'
    $taskArgs = '"' + $launcher + '" --data-dir "' + $data + '" --port ' + $port
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask -and ($existingTask.Actions.Execute -ne $pythonw -or $existingTask.Actions.Arguments -ne $taskArgs)) { throw 'A different startup task exists. Nothing was replaced.' }

    $null = New-Item -ItemType Directory -Force -Path $root
    $toolDir = Join-Path $root 'Runtime'
    $null = New-Item -ItemType Directory -Force -Path $toolDir
    $download = Join-Path $toolDir 'uv-download.zip'
    $uv = Join-Path $toolDir 'uv.exe'
    Say '[1/4] Downloading verified Python environment manager...'
    Download-VerifiedUv $download
    $unpack = Join-Path $toolDir ('uv-' + [Guid]::NewGuid().ToString('N'))
    Expand-Archive -LiteralPath $download -DestinationPath $unpack
    $binary = Get-ChildItem -LiteralPath $unpack -Recurse -Filter uv.exe | Select-Object -First 1
    if (-not $binary) { throw 'Verified download contains no uv.exe.' }
    Copy-Item -LiteralPath $binary.FullName -Destination $uv -Force
    Remove-Item -LiteralPath $download -Force
    Remove-Item -LiteralPath $unpack -Recurse -Force
    Say '[2/4] Installing application and locked dependencies...'
    # Reserve only this owned snapshot, so an interrupted dependency install can be retried.
    @{app=$app;data=$data;port=$port;snapshot=$manifest.snapshot;stage='preparing'} | ConvertTo-Json | Set-Content -LiteralPath $identity -Encoding UTF8
    $null = New-Item -ItemType Directory -Force -Path $app
    foreach ($member in $manifest.files.PSObject.Properties) {
        $target = Join-Path $app $member.Name
        $null = New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target)
        Copy-Item -LiteralPath (Join-Path $source $member.Name) -Destination $target -Force
    }
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $app 'package-manifest.json') -Force
    & $uv sync --project $app --frozen --no-dev --python 3.11.11
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Retry after checking your network.' }
    $python = Join-Path $app '.venv\Scripts\python.exe'
    $env:PERSONAL_MEMORY_DATA = $data
    $env:PERSONAL_MEMORY_BASE = "http://127.0.0.1:$port"
    $env:PERSONAL_MEMORY_DISABLE_DISCOVERY = '1'
    & $python -B (Join-Path $app 'tools\first_run.py') init --data-dir $data --port $port
    if ($LASTEXITCODE -ne 0) { throw 'Instance initialization failed.' }
    @{app=$app;data=$data;port=$port;snapshot=$manifest.snapshot} | ConvertTo-Json | Set-Content -LiteralPath $identity -Encoding UTF8

    Say '[3/4] Optional AI extraction (your own key, your own consent)...'
    Say 'Without a DeepSeek key, manual memory management and keyword search still work.'
    Say 'AI extraction sends selected text to DeepSeek and may incur charges. Auto-capture is not enabled by installation.'
    if ($Yes) {
        Say 'Running unattended: no key was configured. Use Configure-Key.cmd later; the key is typed locally and never shown to an assistant.'
    } elseif (Confirm-Yes 'Configure your own DeepSeek key? [Y/N]') {
        & $python -B (Join-Path $app 'tools\first_run.py') configure-key --data-dir $data
        if ($LASTEXITCODE -ne 0) { throw 'Key setup failed; automatic extraction is not configured.' }
    }
    Say '[4/4] Starting the local service...'
    if ($NoStartup) {
        $result.startup_task = 'not_configured'
        Say 'Login auto-start was skipped by -NoStartup. Starting this session only.'
        Start-Process -FilePath $pythonw -ArgumentList $taskArgs -WorkingDirectory $app
    } else {
        $startup = $true
        if (-not $existingTask) {
            try {
                $action = New-ScheduledTaskAction -Execute $pythonw -Argument $taskArgs -WorkingDirectory $app
                $trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
                $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
                $null = Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Personal Memory shared snapshot (local only)'
            } catch {
                $startup = $false
                Note 'Login auto-start is unavailable under your policy. Starting this session only; use Start.cmd next time.'
            }
        }
        if ($startup) {
            try { Start-ScheduledTask -TaskName $taskName }
            catch {
                $startup = $false
                Note 'Startup task could not run. Trying this session only; use Start.cmd next time.'
            }
        }
        if (-not $startup) { Start-Process -FilePath $pythonw -ArgumentList $taskArgs -WorkingDirectory $app }
        if ($startup) { $result.startup_task = 'configured' } else { $result.startup_task = 'session_only' }
    }
    $ready = $false
    for ($n = 0; $n -lt 60; $n++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 1
            if ($health.status -eq 'ok') { $ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "Service not ready. See logs in $data. No existing service was stopped." }
    $result.service = 'running'
    Say 'Installed. Enter your management key in the browser.'
    Say 'Keyword search is ready. Semantic search needs a separately installed Ollama/BGE-M3.'
    Say 'Connect-Agents.cmd is optional. It changes only the client you select, after confirmation.'
    if ($Connect) { Invoke-Connections $Connect }
    if ($NoBrowser) {
        Say 'Browser was not opened (-NoBrowser).'
    } elseif ($Yes) {
        Say "Open http://127.0.0.1:$port/ and read the management key from $data\admin-key.txt. Do not share it."
    } elseif (Confirm-Yes 'Copy local management key to clipboard for login? [Y/N]') {
        $key = (Get-Content -LiteralPath (Join-Path $data 'admin-key.txt') -Raw).Trim()
        Set-Clipboard -Value $key
        $key = $null
        Say 'Management key copied. Paste it only into the local PM login page; clear clipboard afterwards.'
    } else { Say "Open the local file $data\admin-key.txt to obtain your management key. Do not share it." }
    $result.status = 'installed'
    if ($Connect -and (@($result.connected.Values | Where-Object { $_ -ne 'configured' }).Count -gt 0)) { $result.status = 'installed_connect_incomplete' }
    Write-Result
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
    if ($result.status -eq 'installed_connect_incomplete') { exit 2 }
    exit 0
} catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
    if (-not $Json) { Write-Host ('Installation not complete: ' + $_.Exception.Message) -ForegroundColor Red }
    Write-Result
    exit 1
}
