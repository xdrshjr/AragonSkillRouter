<#
.SYNOPSIS
    Take a Skill Router backup: a consistent SQLite copy plus the master keyring.

.DESCRIPTION
    Wraps `python -m app.cli backup`, which uses SQLite's online backup API (safe
    while the API is running) and writes a manifest next to the database file.

    The database on its own is NOT a usable backup: every provider credential in it
    is encrypted with the master keyring. This script therefore copies the keyring
    too — by default into the SAME folder, which is convenient for a restore but
    means one folder now holds both halves of the secret. Use -KeyringOut to put the
    keyring somewhere else (a secrets manager, an encrypted volume, a different
    host) and treat that as the real backup of the key material.

    Uploads and artifacts under <data>\uploads and <data>\artifacts (and generated
    files referenced by Records) are NOT included: they are large and are re-derivable
    per file_retention_days policy. Copy them yourself if you need them:
        robocopy <data>\uploads <target>\uploads /E

.PARAMETER Out
    Target directory for the database backup. Default: <data>\backups

.PARAMETER KeyringOut
    Where to copy the keyring. Default: the same as -Out.

.PARAMETER NoKeyring
    Skip the keyring copy (only if the keyring is already backed up elsewhere and you
    know it — a database without its keyring is unreadable).

.EXAMPLE
    .\scripts\backup.ps1
    .\scripts\backup.ps1 -Out D:\backups\skillrouter -KeyringOut E:\secrets
#>
[CmdletBinding()]
param(
    [string]$Out,
    [string]$KeyringOut,
    [switch]$NoKeyring,
    [string]$DataDir
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'

function Write-Step([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "  ok   $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "  warn $text" -ForegroundColor Yellow }
function Write-Bad([string]$text) { Write-Host "  FAIL $text" -ForegroundColor Red }

if (-not (Test-Path $python)) {
    Write-Bad ".venv\Scripts\python.exe is missing; run the backend from its virtualenv."
    exit 2
}

# Resolve the effective data directory the same way the backend does.
if (-not $DataDir) {
    $DataDir = $env:SR_DATA_DIR
    if (-not $DataDir) { $DataDir = Join-Path $repo 'data' }
}
Write-Step "Data directory: $DataDir"
if (-not (Test-Path $DataDir)) {
    Write-Bad "data directory does not exist: $DataDir"
    exit 2
}

if (-not $Out) { $Out = Join-Path $DataDir 'backups' }
New-Item -ItemType Directory -Force -Path $Out | Out-Null
if (-not $KeyringOut) { $KeyringOut = $Out }
New-Item -ItemType Directory -Force -Path $KeyringOut | Out-Null

# ------------------------------------------------------------------- database --
Write-Step "Creating the database backup (online backup API)"
Push-Location (Join-Path $repo 'backend')
try {
    $json = & $python -m app.cli backup --out $Out
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($exit -ne 0) {
    Write-Bad "python -m app.cli backup failed (exit $exit)"
    exit $exit
}
$result = $json | ConvertFrom-Json
Write-Ok "database: $($result.backup) ($([math]::Round($result.size_bytes / 1MB, 2)) MiB)"
Write-Ok "manifest: $($result.manifest)"

# -------------------------------------------------------------------- keyring --
if (-not $NoKeyring) {
    Write-Step "Copying the master keyring"
    $keyringPath = $env:SR_MASTER_KEY_FILE
    if (-not $keyringPath) { $keyringPath = Join-Path $DataDir 'keys\master.keyring.json' }
    if ($env:SR_MASTER_KEYS) {
        Write-Warn "This deployment supplies keys through SR_MASTER_KEYS (environment), not a file."
        Write-Warn "There is no keyring file to copy. Back up that environment variable in your secrets store:"
        Write-Warn "  SR_MASTER_KEYS=<value> / SR_MASTER_KEY_ACTIVE=<key id>"
    } elseif (-not (Test-Path $keyringPath)) {
        Write-Bad "keyring not found at $keyringPath"
        Write-Bad "A database backup without its keyring cannot be restored into a working system."
        exit 3
    } else {
        $stamp = Get-Date -Format 'yyyyMMddTHHmmssZ'
        $keyringTarget = Join-Path $KeyringOut "master.keyring.$stamp.json"
        Copy-Item $keyringPath $keyringTarget -Force
        Write-Ok "keyring: $keyringTarget"
        if ($KeyringOut -eq $Out) {
            Write-Warn "The keyring copy sits next to the database copy: one folder now holds both."
            Write-Warn "Move it to a separate protected location and delete it from $Out."
        }
    }
}

Write-Host ""
Write-Host "Backup complete." -ForegroundColor Green
Write-Host "  database  $($result.backup)"
Write-Host "  manifest  $($result.manifest)"
if (-not $NoKeyring) { Write-Host "  keyring   $KeyringOut" }
Write-Host ""
Write-Host "Restore with: .\scripts\restore.ps1 -Backup <file.db> -Keyring <file.json>" -ForegroundColor DarkGray
Write-Host "Verify a backup at any time with: python -m app.cli doctor" -ForegroundColor DarkGray
