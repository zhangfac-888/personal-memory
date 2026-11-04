param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$source = $PSScriptRoot
$root = Join-Path $env:LOCALAPPDATA 'PersonalMemoryShare'
$app = Join-Path $root 'App'
$data = Join-Path $root 'Data'
$port = 8765
$taskName = 'PersonalMemory_SharedLocalService'
$manifestPath = Join-Path $source 'package-manifest.json'
$uvVersion = '0.13.7'
$uvHash = '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021'

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

try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'This preview supports Windows x64 only.' }
    $manifest = Check-Package
    if ($CheckOnly) {
        [pscustomobject]@{Package='verified';Files=@($manifest.files.PSObject.Properties).Count;Application=$app;Data=$data;Python='3.11.11';Uv=$uvVersion;InstallExecuted=$false} | ConvertTo-Json
        exit 0
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Host 'Personal Memory - Windows quick install'
    Write-Host 'Downloads Python/dependencies from official sources. Creates a local service and a login-startup task.'
    Write-Host "App: $app"
    Write-Host "Data: $data"
    Write-Host 'No existing memories or AI-client settings will be imported. Network access is required.'
    if ((Read-Host 'Install now? [Y/N]') -notmatch '^(y|yes)$') { exit 0 }

    $identity = Join-Path $root 'share-installation.json'
    if (Test-Path -LiteralPath $app) {
        if (-not (Test-Path -LiteralPath $identity)) { throw 'Application directory already exists without an owned installation marker. Nothing was replaced.' }
        $owned = Get-Content -LiteralPath $identity -Raw | ConvertFrom-Json
        if ($owned.app -ne $app -or $owned.data -ne $data -or $owned.snapshot -ne $manifest.snapshot) { throw 'Different existing installation. No automatic upgrade or migration was performed.' }
    } elseif (Test-Path -LiteralPath $data) {
        throw 'Data directory already exists. No existing library will be reset or migrated.'
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
        Write-Host 'Already installed and running; data preserved.'
        Start-Process "http://127.0.0.1:$port/"
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
    Write-Host '[1/4] Downloading verified Python environment manager...'
    Download-VerifiedUv $download
    $unpack = Join-Path $toolDir ('uv-' + [Guid]::NewGuid().ToString('N'))
    Expand-Archive -LiteralPath $download -DestinationPath $unpack
    $binary = Get-ChildItem -LiteralPath $unpack -Recurse -Filter uv.exe | Select-Object -First 1
    if (-not $binary) { throw 'Verified download contains no uv.exe.' }
    Copy-Item -LiteralPath $binary.FullName -Destination $uv -Force
    Remove-Item -LiteralPath $download -Force
    Remove-Item -LiteralPath $unpack -Recurse -Force
    Write-Host '[2/4] Installing application and locked dependencies...'
    # Reserve only this owned snapshot, so an interrupted dependency install can be retried.
    @{app=$app;data=$data;snapshot=$manifest.snapshot;stage='preparing'} | ConvertTo-Json | Set-Content -LiteralPath $identity -Encoding UTF8
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
    @{app=$app;data=$data;snapshot=$manifest.snapshot} | ConvertTo-Json | Set-Content -LiteralPath $identity -Encoding UTF8

    Write-Host '[3/4] Optional AI extraction (your own key, your own consent)...'
    Write-Host 'Without a DeepSeek key, manual memory management and keyword search still work.'
    Write-Host 'AI extraction sends selected text to DeepSeek and may incur charges. Auto-capture is not enabled by installation.'
    if ((Read-Host 'Configure your own DeepSeek key? [Y/N]') -match '^(y|yes)$') {
        & $python -B (Join-Path $app 'tools\first_run.py') configure-key --data-dir $data
        if ($LASTEXITCODE -ne 0) { throw 'Key setup failed; automatic extraction is not configured.' }
    }
    Write-Host '[4/4] Starting the local service...'
    $startup = $true
    if (-not $existingTask) {
        try {
            $action = New-ScheduledTaskAction -Execute $pythonw -Argument $taskArgs -WorkingDirectory $app
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
            $null = Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Personal Memory shared snapshot (local only)'
        } catch {
            $startup = $false
            Write-Warning 'Login auto-start is unavailable under your policy. Starting this session only; use Start.cmd next time.'
        }
    }
    if ($startup) {
        try { Start-ScheduledTask -TaskName $taskName }
        catch {
            $startup = $false
            Write-Warning 'Startup task could not run. Trying this session only; use Start.cmd next time.'
        }
    }
    if (-not $startup) { Start-Process -FilePath $pythonw -ArgumentList $taskArgs -WorkingDirectory $app }
    $ready = $false
    for ($n = 0; $n -lt 60; $n++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 1
            if ($health.status -eq 'ok') { $ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "Service not ready. See logs in $data. No existing service was stopped." }
    Write-Host 'Installed. Enter your management key in the browser.'
    Write-Host 'Keyword search is ready. Semantic search needs a separately installed Ollama/BGE-M3.'
    Write-Host 'Connect-Agents.cmd is optional. It changes only the client you select, after confirmation.'
    if ((Read-Host 'Copy local management key to clipboard for login? [Y/N]') -match '^(y|yes)$') {
        $key = (Get-Content -LiteralPath (Join-Path $data 'admin-key.txt') -Raw).Trim()
        Set-Clipboard -Value $key
        $key = $null
        Write-Host 'Management key copied. Paste it only into the local PM login page; clear clipboard afterwards.'
    } else { Write-Host "Open the local file $data\admin-key.txt to obtain your management key. Do not share it." }
    Start-Process "http://127.0.0.1:$port/"
} catch {
    Write-Host ('Installation not complete: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
