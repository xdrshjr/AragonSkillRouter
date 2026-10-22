<#
.SYNOPSIS
    Restore a Skill Router backup (database + master keyring).

.DESCRIPTION
    Wraps `python -m app.cli restore`, which:
      * checks the backup's SQLite integrity and that it looks like a Skill Router
        database;
      * renames the current database to *.pre-restore-<timestamp>.db instead of
        deleting it;
      * copies the backup into place and re-applies migrations.

    The CLI refuses to run without --force, because restoring while the API and the
    worker are writing will corrupt the result. This script stops nothing for you: it
    asks, then passes --force once you confirm the services are stopped.

    The keyring is restored too. If the backup was taken on another deployment, its
    keyring is the only thing that can decrypt the credentials in that database — a
    keyring from a different install will make every credential unreadable
    (`doctor` reports credentials_decryptable: false).

.PARAMETER Backup
    Path to the .db file produced by backup.ps1 / `app.cli backup`.

.PARAMETER Keyring
    Path to the matching master.keyring.json. Omit when the keyring is supplied by
    SR_MASTER_KEYS in the environment.

.PARAMETER Force
    Skip the interactive confirmation. You are asserting the API and worker are down.

.EXAMPLE
    .\scripts\restore.ps1 -Backup .\data\backups\skillrouter-20261008T091500Z.db -Keyring .\data\backups\master.keyring.20261008T091500Z.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Backup,
    [string]$Keyring,
    [switch]$Force,
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
if (-not (Test-Path $Backup)) {
    Write-Bad "backup file not found: $Backup"
    exit 2
}

if (-not $DataDir) {
    $DataDir = $env:SR_DATA_DIR
    if (-not $DataDir) { $DataDir = Join-Path $repo 'data' }
}

# --------------------------------------------------------------- confirmation --
Write-Host ""
Write-Host "About to restore" -ForegroundColor Yellow
Write-Host "  database  $Backup"
Write-Host "  into      $DataDir"
if ($Keyring) { Write-Host "  keyring   $Keyring" }
Write-Host ""
Write-Host "STOP THE API AND THE WORKER FIRST." -ForegroundColor Red
Write-Host "  - local dev:      .\scripts\dev.ps1 -Stop"
Write-Host "  - docker compose: docker compose stop api worker"
Write-Host ""
if (-not $Force) {
    $answer = Read-Host "Are the API and worker stopped? Type RESTORE to continue"
    if ($answer -ne 'RESTORE') {
        Write-Warn "aborted (nothing was changed)"
        exit 1
    }
}

# ------------------------------------------------------------------- keyring ---
if ($Keyring) {
    if (-not (Test-Path $Keyring)) {
        Write-Bad "keyring file not found: $Keyring"
        exit 2
    }
    Write-Step "Restoring the master keyring"
    $target = $env:SR_MASTER_KEY_FILE
    if (-not $target) { $target = Join-Path $DataDir 'keys\master.keyring.json' }
    $targetDir = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    if (Test-Path $target) {
        $safety = "$target.pre-restore-$(Get-Date -Format 'yyyyMMddTHHmmssZ').json"
        Move-Item $target $safety -Force
        Write-Ok "existing keyring kept at $safety"
    }
    Copy-Item $Keyring $target -Force
    Write-Ok "keyring restored to $target"
} elseif (-not $env:SR_MASTER_KEYS) {
    Write-Warn "No -Keyring given and SR_MASTER_KEYS is not set: the restored database will keep"
    Write-Warn "whatever keyring is already on this host. Credentials encrypted elsewhere will fail"
    Write-Warn "to decrypt (doctor reports credentials_decryptable: false)."
}

# ------------------------------------------------------------------ database ---
Write-Step "Restoring the database"
Push-Location (Join-Path $repo 'backend')
try {
    $output = & $python -m app.cli restore $Backup --force
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($exit -ne 0) {
    Write-Bad "python -m app.cli restore failed (exit $exit)"
    Write-Host $output
    exit $exit
}
$output | ForEach-Object { Write-Host "  $_" }

# --------------------------------------------------------------------- check ---
Write-Step "Running the post-restore doctor check"
Push-Location (Join-Path $repo 'backend')
try {
    $doctor = & $python -m app.cli doctor
    $doctorExit = $LASTEXITCODE
} finally {
    Pop-Location
}
Write-Host $doctor

Write-Host ""
if ($doctorExit -eq 0) {
    Write-Host "Restore complete and doctor is green." -ForegroundColor Green
} else {
    Write-Warn "Restore finished, but doctor reports a problem (exit $doctorExit)."
    Write-Warn "The most likely cause is a keyring that does not match this database."
}
Write-Host ""
Write-Host "Next: start the services, sign in, then sign out any stale browser session." -ForegroundColor DarkGray
exit $doctorExit
