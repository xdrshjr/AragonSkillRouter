<#
.SYNOPSIS
    Start (or stop) the whole Skill Router stack locally, with readable console output.

.DESCRIPTION
    Starts four processes, each in its own console window so their logs stay
    separable:

        sidecar   node dist/main.js            (AragonAgent engine, 127.0.0.1:7317)
        api       uvicorn app.main:app         (FastAPI, 127.0.0.1:8000)
        worker    python -m app.worker_main    (async tasks / job queue)
        web       npm run dev                  (Next.js, 127.0.0.1:3000)

    Process ids are written to scripts\.dev-pids.json so `-Stop` can shut down
    exactly what this script started (and nothing else).

    Prerequisites it checks before starting anything:
        .venv\Scripts\python.exe, backend\requirements.txt installed,
        sidecar\node_modules + a build, frontend\node_modules, .env in the repo root.

.EXAMPLE
    .\scripts\dev.ps1
    .\scripts\dev.ps1 -NoSidecar          # backend + web only (no engine)
    .\scripts\dev.ps1 -Stop               # stop everything it started
    .\scripts\dev.ps1 -Restart            # stop, then start
#>
[CmdletBinding()]
param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 3000,
    [int]$SidecarPort = 7317,
    [switch]$NoSidecar,
    [switch]$NoWorker,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$SkipChecks
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $PSScriptRoot '.dev-pids.json'
$python = Join-Path $repo '.venv\Scripts\python.exe'

function Write-Step([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "  ok   $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "  warn $text" -ForegroundColor Yellow }
function Write-Bad([string]$text) { Write-Host "  FAIL $text" -ForegroundColor Red }

function Stop-Started {
    if (-not (Test-Path $pidFile)) {
        Write-Warn "no $pidFile - nothing to stop"
        return
    }
    $recorded = Get-Content $pidFile -Raw | ConvertFrom-Json
    foreach ($entry in $recorded) {
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            Write-Warn "$($entry.name) (pid $($entry.pid)) is not running"
            continue
        }
        try {
            Stop-Process -Id $entry.pid -Force -ErrorAction Stop
            Write-Ok "stopped $($entry.name) (pid $($entry.pid))"
        } catch {
            Write-Bad "could not stop $($entry.name) (pid $($entry.pid)): $($_.Exception.Message)"
        }
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

if ($Stop -or $Restart) { Stop-Started }
if ($Stop -and -not $Restart) {
    Write-Host ""
    Write-Host "Stopped. Note: a browser tab or `npm run dev` started outside this script is not affected." -ForegroundColor DarkGray
    return
}

# ------------------------------------------------------------------ preflight --
Write-Step "Checking the local toolchain"
if (-not $SkipChecks) {
    if (-not (Test-Path $python)) {
        Write-Bad ".venv\Scripts\python.exe is missing."
        Write-Host "     python -m venv .venv; .\.venv\Scripts\python -m pip install -r backend\requirements.txt -r backend\requirements-dev.txt"
        return
    }
    Write-Ok "python: $python"

    if (-not (Test-Path (Join-Path $repo '.env'))) {
        Write-Warn ".env is missing in the repo root (copy .env.example and edit it)."
        Write-Warn "The API will fall back to development defaults, which is fine for local use."
    } else {
        Write-Ok ".env present"
    }

    if (-not (Test-Path (Join-Path $repo 'frontend\node_modules'))) {
        Write-Bad "frontend\node_modules is missing. Run: cd frontend; npm ci"
        return
    }
    Write-Ok "frontend dependencies present"

    if (-not $NoSidecar) {
        if (-not (Test-Path (Join-Path $repo 'sidecar\node_modules'))) {
            Write-Bad "sidecar\node_modules is missing. Run: cd sidecar; npm ci"
            return
        }
        if (-not (Test-Path (Join-Path $repo 'sidecar\dist\main.js'))) {
            Write-Step "Building the sidecar (dist\main.js is missing)"
            Push-Location (Join-Path $repo 'sidecar')
            try { npm run build } finally { Pop-Location }
            if ($LASTEXITCODE -ne 0) { Write-Bad "sidecar build failed"; return }
        }
        Write-Ok "sidecar built"
    }
}

# The worker and the API initialise the database through `python -m app.cli init`.
Write-Step "Initialising the database (idempotent)"
Push-Location (Join-Path $repo 'backend')
try { & $python -m app.cli init | Out-Null } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { Write-Bad "database init failed (see the output above)"; return }
Write-Ok "database and keyring ready"

# -------------------------------------------------------------------- start ----
$started = @()
$startedAt = Get-Date

function Get-EnvFileValue([string]$path, [string]$key) {
    if (-not (Test-Path $path)) { return $null }
    foreach ($line in Get-Content $path) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith('#') -or -not $trimmed.Contains('=')) { continue }
        $parts = $trimmed.Split('=', 2)
        if ($parts[0].Trim() -eq $key) { return $parts[1].Trim() }
    }
    return $null
}

if (-not $NoSidecar) {
    Write-Step "Starting sidecar on 127.0.0.1:$SidecarPort"
    # The sidecar and the API must share one bearer token. Prefer the value from
    # .env; otherwise generate one for this session and say so loudly.
    if (-not $env:SR_ARAGON_SIDECAR_TOKEN) {
        $env:SR_ARAGON_SIDECAR_TOKEN = Get-EnvFileValue (Join-Path $repo '.env') 'SR_ARAGON_SIDECAR_TOKEN'
    }
    if (-not $env:SR_ARAGON_SIDECAR_TOKEN) {
        $generated = -join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Minimum 0 -Maximum 15) })
        $env:SR_ARAGON_SIDECAR_TOKEN = $generated
        Write-Warn "SR_ARAGON_SIDECAR_TOKEN was neither in the environment nor in .env: generated one for this session."
        Write-Warn "For a permanent setup put this line in .env and restart:"
        Write-Host "          SR_ARAGON_SIDECAR_TOKEN=$generated" -ForegroundColor DarkGray
    }
    $env:SR_ARAGON_SIDECAR_PORT = "$SidecarPort"
    $process = Start-Process -FilePath 'node' -ArgumentList @('dist/main.js') -WorkingDirectory (Join-Path $repo 'sidecar') -PassThru -WindowStyle Minimized
    $started += [pscustomobject]@{ name = 'sidecar'; pid = $process.Id }
    Write-Ok "sidecar pid $($process.Id)"
}

if (-not $NoWorker) {
    Write-Step "Starting worker"
    $process = Start-Process -FilePath $python -ArgumentList @('-m', 'app.worker_main') -WorkingDirectory (Join-Path $repo 'backend') -PassThru -WindowStyle Minimized
    $started += [pscustomobject]@{ name = 'worker'; pid = $process.Id }
    Write-Ok "worker pid $($process.Id)"
}

Write-Step "Starting API on http://127.0.0.1:$ApiPort"
$apiProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ApiPort", '--reload') -WorkingDirectory (Join-Path $repo 'backend') -PassThru -WindowStyle Minimized
$started += [pscustomobject]@{ name = 'api'; pid = $apiProcess.Id }
Write-Ok "api pid $($apiProcess.Id)"

Write-Step "Waiting for the API to answer /api/health"
$healthy = $false
for ($attempt = 1; $attempt -le 40; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/api/health" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        # not up yet
    }
}
if ($healthy) { Write-Ok "API answered on attempt $attempt" } else { Write-Bad "API did not answer within 20s (check the api console window)" }

Write-Step "Starting web on http://127.0.0.1:$WebPort"
$webProcess = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', 'npm run dev') -WorkingDirectory (Join-Path $repo 'frontend') -PassThru -WindowStyle Minimized
$started += [pscustomobject]@{ name = 'web'; pid = $webProcess.Id }
Write-Ok "web pid $($webProcess.Id)"

$started | ConvertTo-Json -Depth 3 | Set-Content -Path $pidFile -Encoding UTF8

Write-Host ""
Write-Host "Skill Router is running:" -ForegroundColor Green
Write-Host ("  web       http://localhost:$WebPort        (sign in, first run sets the admin password)")
Write-Host ("  api       http://127.0.0.1:$ApiPort        /api/health  /api/docs")
if (-not $NoSidecar) { Write-Host ("  sidecar   http://127.0.0.1:$SidecarPort  /health") }
if (-not $NoWorker) { Write-Host  "  worker    no HTTP surface; follow its window" }
Write-Host ""
Write-Host "  stop      .\scripts\dev.ps1 -Stop"
Write-Host "  smoke     .\scripts\smoke.ps1"
Write-Host ("  pids      {0}" -f $pidFile) -ForegroundColor DarkGray
Write-Host ""
Write-Host "Each process has its own console window (minimised). Logs there, not here." -ForegroundColor DarkGray
Write-Host ("Started at {0:HH:mm:ss}" -f $startedAt) -ForegroundColor DarkGray
