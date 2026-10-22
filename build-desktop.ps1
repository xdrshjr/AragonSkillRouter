<#
.SYNOPSIS
    One command that builds the Skill Router desktop installer — and optionally
    installs and smoke-tests it.

.DESCRIPTION
    Wraps the whole desktop pipeline so nobody has to remember the order of six
    steps or the environment they need:

        1. preflight   node / npm / electron / .venv / PyInstaller / package.json
        2. deps        npm ci in desktop/ and frontend/ when node_modules is missing
        3. shell       tsc -p desktop            (main, preload, proxy, supervisor)
        4. UI          Next.js standalone build into desktop/resources/runtime/web
        5. engine      PyInstaller freeze into desktop/resources/runtime/backend
                       + the compiled AragonAgent sidecar
        6. install     NSIS installer via electron-builder  ->  dist-desktop\

    Every step is skipped when its inputs are newer than its outputs (`-Fast` is
    implied by `-NoInstall` reuse), so a rebuild after one frontend change does not
    re-freeze Python. `-Clean` forces everything.

    Failures are printed with the exact command that failed and the log file that
    holds its output; nothing is reported as built unless the artefact exists.

.PARAMETER Install
    Run the built installer silently afterwards (per-user, no admin needed).

.PARAMETER Verify
    After building (and installing), start the packaged app once with an isolated
    data directory and run the end-to-end check, then stop it. Implies nothing
    about -Install: the unpacked build is verified in place.

.PARAMETER Portable
    Produce the unpacked app only (fast, no installer). Good for a quick check.

.PARAMETER Clean
    Delete the previous runtime and electron-builder output first.

.PARAMETER Fast
    Skip a step when its output already exists and is newer than every input.

.PARAMETER SkipTests
    Do not run `npm test` in desktop/ (the shell unit tests).

.PARAMETER AppDataDir
    Data directory used by -Verify, so the real user database is never touched.

.EXAMPLE
    .\build-desktop.ps1

.EXAMPLE
    .\build-desktop.ps1 -Install -Verify        # build, install, verify end to end

.EXAMPLE
    .\build-desktop.ps1 -Portable -Clean        # clean unpacked build only

.EXAMPLE
    .\build-desktop.ps1 -Fast                   # rebuild only what changed

.NOTES
    Runs on Windows PowerShell 5.1 (the default `powershell.exe`) as well as
    PowerShell 7. Deliberately avoids `&&`, ternaries and null-conditional
    operators so the same file works in both.
#>

[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Verify,
    [switch]$Portable,
    [switch]$Clean,
    [switch]$Fast,
    [switch]$SkipTests,
    [string]$AppDataDir
)

$ErrorActionPreference = 'Stop'
$script:StartedAt = Get-Date

# The script lives at the repo root; everything is relative to it.
$Repo = $PSScriptRoot
if (-not $Repo) { $Repo = (Get-Location).Path }
$Desktop = Join-Path $Repo 'desktop'
$Frontend = Join-Path $Repo 'frontend'
$Runtime = Join-Path $Desktop 'resources\runtime'
$OutDir = Join-Path $Repo 'dist-desktop'
$LogDir = Join-Path $Repo 'build-logs'
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Installer = Join-Path $OutDir 'SkillRouter-Setup-1.0.0.exe'
$UnpackedExe = Join-Path $OutDir 'win-unpacked\Skill Router.exe'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# ------------------------------------------------------------------ output ------
function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}
function Write-Ok([string]$Text) { Write-Host "    ok    $Text" -ForegroundColor Green }
function Write-Skip([string]$Text) { Write-Host "    skip  $Text" -ForegroundColor DarkGray }
function Write-Warn([string]$Text) { Write-Host "    warn  $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "    FAIL  $Text" -ForegroundColor Red }

function Stop-Build([string]$Message, [string]$Hint) {
    Write-Fail $Message
    if ($Hint) { Write-Host "    hint  $Hint" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host ("Build failed after {0:N1}s" -f ((Get-Date) - $script:StartedAt).TotalSeconds) -ForegroundColor Red
    exit 1
}

<#
    Run a native command, stream nothing to this console (the tools are chatty and
    their output is only interesting when something breaks), and keep the full
    output in build-logs/. On failure: print the last lines and stop.

    Logs are written as UTF-8 through cmd.exe redirection: PowerShell's `*>` writes
    UTF-16LE, which every other tool here (Node, Python, `Get-Content` without
    -Encoding Unicode) then misreads as binary.
#>
function Get-NativeLogEncoding {
    return [System.Text.Encoding]::UTF8
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string]$LogName
    )
    if (-not $LogName) {
        $LogName = ($Label -replace '[^A-Za-z0-9]+', '-').Trim('-') + '.log'
    }
    $logPath = Join-Path $LogDir $LogName
    Write-Step $Label
    # Normalise the command line and hand it to cmd.exe so the redirection (and thus
    # the encoding) is cmd's, not PowerShell's.
    $quoted = @()
    foreach ($argument in $Arguments) {
        if ($argument -match '[\s"&|<>]') { $quoted += '"' + $argument.Replace('"', '\"') + '"' }
        else { $quoted += $argument }
    }
    $commandLine = '"' + $Command + '" ' + ($quoted -join ' ')
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # let us inspect $LASTEXITCODE instead of throwing
    Push-Location $WorkingDirectory
    try {
        & cmd.exe /c "$commandLine > `"$logPath`" 2>&1"
        $code = $LASTEXITCODE
    } catch {
        $code = 1
        ($_ | Out-String) | Out-File -FilePath $logPath -Encoding utf8 -Append
    } finally {
        Pop-Location
        $ErrorActionPreference = $saved
    }
    if ($code -ne 0) {
        Write-Host "    command: $commandLine" -ForegroundColor DarkGray
        Write-Host "    in:      $WorkingDirectory" -ForegroundColor DarkGray
        Write-Host "    log:     $logPath" -ForegroundColor DarkGray
        if (Test-Path $logPath) {
            Write-Host "    --- last 25 lines ---" -ForegroundColor DarkGray
            Get-Content $logPath -Tail 25 -Encoding UTF8 -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        }
        Stop-Build "$Label failed (exit $code)" "read $logPath"
    }
    Write-Ok "$Label (log: $LogName)"
}

function Test-Newer([string]$OutputPath, [string[]]$InputPaths) {
    if (-not (Test-Path $OutputPath)) { return $false }
    $output = (Get-Item $OutputPath).LastWriteTimeUtc
    foreach ($path in $InputPaths) {
        if (-not (Test-Path $path)) { continue }
        $item = Get-Item $path
        if ($item.PSIsContainer) {
            $newest = Get-ChildItem -Path $item.FullName -Recurse -File -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
            if ($newest -and $newest.LastWriteTimeUtc -gt $output) { return $false }
        } elseif ($item.LastWriteTimeUtc -gt $output) {
            return $false
        }
    }
    return $true
}

# ------------------------------------------------------------------ preflight ---
function Test-Preflight {
    Write-Step 'Preflight'
    $problems = @()

    if (-not (Test-Path (Join-Path $Repo 'desktop\package.json'))) {
        Stop-Build "this does not look like the Skill Router repo (desktop\package.json missing)" "run the script from the repository root"
    }

    foreach ($tool in @('node', 'npm')) {
        $cmd = Get-Command $tool -ErrorAction SilentlyContinue
        if ($null -eq $cmd) {
            $problems += "$tool is not on PATH"
        } else {
            # `node --version` prints a leading "v" (v22.18.0); strip it before casting.
            $version = (($(& $tool --version) 2>$null) -join '').Trim()
            if ($tool -eq 'node') {
                $bare = $version.TrimStart('v', 'V')
                $major = 0
                [void][int]::TryParse(($bare -split '\.')[0], [ref]$major)
                if ($major -lt 20) { $problems += "node $version is too old (need >= 20)" }
                else { Write-Ok "node $version" }
            } else {
                Write-Ok "npm $version"
            }
        }
    }

    if (-not (Test-Path $Python)) {
        $problems += ".venv\Scripts\python.exe not found - create it: python -m venv .venv"
    } else {
        $pyVersion = (& $Python --version) 2>$null
        Write-Ok "$pyVersion ($Python)"
        $pyi = (& $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $pyi) {
            $problems += "PyInstaller is missing in .venv - run: .venv\Scripts\python -m pip install -r backend\requirements-desktop.txt"
        } else {
            Write-Ok "PyInstaller $pyi"
        }
    }

    if (Test-Path (Join-Path $Desktop 'node_modules\electron\package.json')) {
        $electronVersion = (Get-Content (Join-Path $Desktop 'node_modules\electron\package.json') -Raw | ConvertFrom-Json).version
        Write-Ok "electron $electronVersion (desktop\node_modules)"
    } elseif (Test-Path (Join-Path $Desktop 'node_modules')) {
        Write-Warn 'desktop\node_modules exists but electron is missing; it will be installed'
    } else {
        Write-Warn 'desktop\node_modules missing; dependencies will be installed'
    }

    foreach ($dir in @($Runtime, $OutDir)) {
        if (Test-Path $dir) {
            $size = [math]::Round(((Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB), 1)
            Write-Ok "$(Split-Path $dir -Leaf)/ present ($size MB)"
        }
    }

    if ($problems.Count -gt 0) {
        Write-Host ""
        foreach ($problem in $problems) { Write-Fail $problem }
        Stop-Build 'preflight failed' 'fix the items above, then re-run'
    }
}

# ---------------------------------------------------------------------- steps ---
function Clear-Previous {
    Write-Step 'Clean'
    foreach ($path in @((Join-Path $Desktop 'resources\runtime'), (Join-Path $Desktop 'dist'), $OutDir)) {
        if (Test-Path $path) {
            Remove-Item -Path $path -Recurse -Force -ErrorAction SilentlyContinue
            Write-Ok "removed $path"
        }
    }
}

function Stop-RunningApp {
    <#
        electron-builder unpacks into dist-desktop\win-unpacked and fails with
        "Access is denied" when any file there is held open — which happens if the
        user is running the portable build. Close it first, and say so.
    #>
    $running = @()
    $running += Get-Process -Name 'Skill Router' -ErrorAction SilentlyContinue
    $running += Get-Process -Name 'skillrouter-backend' -ErrorAction SilentlyContinue
    if (-not $running -or $running.Count -eq 0) { return }
    Write-Step 'Close the running app (it locks dist-desktop)'
    foreach ($process in $running) {
        Write-Host ("    stop  {0} (pid {1})" -f $process.ProcessName, $process.Id) -ForegroundColor DarkGray
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    $left = @()
    $left += Get-Process -Name 'Skill Router' -ErrorAction SilentlyContinue
    $left += Get-Process -Name 'skillrouter-backend' -ErrorAction SilentlyContinue
    if ($left -and $left.Count -gt 0) {
        Stop-Build 'the app is still running, so its files cannot be replaced' 'quit Skill Router from the tray menu, then re-run'
    }
    Write-Ok 'closed; packaging can replace the files now'
}

function Install-Deps {
    if (-not (Test-Path (Join-Path $Desktop 'node_modules\electron'))) {
        Invoke-Step -Label 'desktop dependencies (npm ci)' -Command 'npm' -Arguments @('ci', '--no-audit', '--no-fund') -WorkingDirectory $Desktop -LogName 'npm-ci-desktop.log'
    } else {
        Write-Skip 'desktop dependencies present'
    }
    if (-not (Test-Path (Join-Path $Frontend 'node_modules\next'))) {
        Invoke-Step -Label 'frontend dependencies (npm ci)' -Command 'npm' -Arguments @('ci', '--no-audit', '--no-fund') -WorkingDirectory $Frontend -LogName 'npm-ci-frontend.log'
    } else {
        Write-Skip 'frontend dependencies present'
    }
    if (-not (Test-Path (Join-Path $Desktop 'node_modules\@esbuild'))) {
        Write-Warn 'esbuild missing in desktop\node_modules (used by the verification scripts only)'
    }
}

function Build-Shell {
    $tsc = Join-Path $Desktop 'node_modules\.bin\tsc.cmd'
    $inputs = @((Join-Path $Desktop 'src'), (Join-Path $Desktop 'tsconfig.json'))
    if ($Fast -and (Test-Newer (Join-Path $Desktop 'dist\main.js') $inputs)) {
        Write-Skip 'shell TypeScript is newer than its sources'
        return
    }
    Invoke-Step -Label 'compile the Electron shell (tsc)' -Command $tsc -Arguments @('-p', 'tsconfig.json') -WorkingDirectory $Desktop -LogName 'tsc-desktop.log'
    if (-not (Test-Path (Join-Path $Desktop 'dist\main.js'))) {
        Stop-Build 'the shell did not compile (desktop\dist\main.js missing)' 'see build-logs\tsc-desktop.log'
    }
}

function Build-Web {
    if ($Fast -and (Test-Newer (Join-Path $Runtime 'web\server.js') @((Join-Path $Frontend 'app'), (Join-Path $Frontend 'components'), (Join-Path $Frontend 'lib'), (Join-Path $Frontend 'next.config.mjs')))) {
        Write-Skip 'UI runtime is newer than frontend sources'
        return
    }
    Invoke-Step -Label 'build the UI runtime (Next.js standalone)' -Command 'node' -Arguments @('scripts/build-web.mjs') -WorkingDirectory $Desktop -LogName 'build-web.log'
    if (-not (Test-Path (Join-Path $Runtime 'web\server.js'))) {
        Stop-Build 'the UI runtime was not produced (resources\runtime\web\server.js missing)' 'see build-logs\build-web.log'
    }
}

function Build-Backend {
    if ($Fast -and (Test-Newer (Join-Path $Runtime 'backend\skillrouter-backend.exe') @((Join-Path $Repo 'backend\app'), (Join-Path $Repo 'backend\desktop_entry.py'), (Join-Path $Repo 'sidecar\src')))) {
        Write-Skip 'backend runtime is newer than backend sources'
        return
    }
    Invoke-Step -Label 'freeze the API and bundle the agent engine' -Command 'node' -Arguments @('scripts/build-backend.mjs') -WorkingDirectory $Desktop -LogName 'build-backend.log'
    if (-not (Test-Path (Join-Path $Runtime 'backend\skillrouter-backend.exe'))) {
        Stop-Build 'the backend runtime was not produced' 'see build-logs\build-backend.log'
    }
    if (-not (Test-Path (Join-Path $Runtime 'sidecar\main.js'))) {
        Stop-Build 'the agent engine was not bundled' 'see build-logs\build-backend.log'
    }
}

function Invoke-UnitTests {
    if ($SkipTests) {
        Write-Skip 'shell unit tests skipped (-SkipTests)'
        return
    }
    Invoke-Step -Label 'shell unit tests (node --test)' -Command 'node' -Arguments @('--test', 'test/*.test.mjs') -WorkingDirectory $Desktop -LogName 'unit-tests.log'
}

function Invoke-Package {
    $builder = Join-Path $Desktop 'node_modules\.bin\electron-builder.cmd'
    if (-not (Test-Path $builder)) {
        Stop-Build 'electron-builder is not installed' 'run: cd desktop; npm install'
    }
    Stop-RunningApp
    # Not `$args`: that is an automatic variable inside functions and assigning to it
    # shadows the caller's arguments.
    $builderArgs = @('--win')
    $label = 'package the NSIS installer (electron-builder)'
    if ($Portable) {
        $builderArgs += '--dir'
        $label = 'package the unpacked app (electron-builder --dir)'
    }
    Invoke-Step -Label $label -Command $builder -Arguments $builderArgs -WorkingDirectory $Desktop -LogName 'electron-builder.log'
    if ($Portable) {
        if (-not (Test-Path $UnpackedExe)) { Stop-Build "the unpacked app was not produced (expected $UnpackedExe)" 'see build-logs\electron-builder.log' }
    } else {
        if (-not (Test-Path $Installer)) { Stop-Build "the installer was not produced (expected $Installer)" 'see build-logs\electron-builder.log' }
    }
}

function Invoke-Install {
    if (-not (Test-Path $Installer)) {
        Write-Warn "no installer at $Installer - skipping install"
        return
    }
    # A running instance holds the binaries; the installer would fail on file locks.
    $running = Get-Process -Name 'Skill Router' -ErrorAction SilentlyContinue
    if ($running) {
        Write-Step 'Close the running app'
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        $still = Get-Process -Name 'Skill Router' -ErrorAction SilentlyContinue
        $still2 = Get-Process -Name 'skillrouter-backend' -ErrorAction SilentlyContinue
        if ($still -or $still2) { Stop-Build 'the app is still running' 'quit it from the tray menu and re-run with -Install' }
        Write-Ok 'closed the running instance'
    }
    Invoke-Step -Label 'install (silent, per-user)' -Command $Installer -Arguments @('/S') -WorkingDirectory $OutDir -LogName 'installer.log'
    $installed = Join-Path $env:LOCALAPPDATA 'Programs\Skill Router\Skill Router.exe'
    if (-not (Test-Path $installed)) { Stop-Build "the installer finished but $installed is missing" 'check build-logs\installer.log' }
    Write-Ok "installed to $(Split-Path $installed -Parent)"
}

function Invoke-Verify {
    $exe = $UnpackedExe
    if ($Install -and (Test-Path (Join-Path $env:LOCALAPPDATA 'Programs\Skill Router\Skill Router.exe'))) {
        $exe = Join-Path $env:LOCALAPPDATA 'Programs\Skill Router\Skill Router.exe'
    }
    if (-not (Test-Path $exe)) {
        Write-Warn "no packaged app at $exe - skipping verification"
        return
    }
    if (-not $AppDataDir) {
        $AppDataDir = Join-Path $Repo 'desktop\build\verify-data'
    }
    if (Test-Path $AppDataDir) { Remove-Item $AppDataDir -Recurse -Force -ErrorAction SilentlyContinue }

    Write-Step "verify the packaged app ($(Split-Path $exe -Leaf))"
    $logPath = Join-Path $LogDir 'verify-packaged.log'
    $verifyArgs = @('scripts/verify-packaged.mjs', '--exe', $exe, '--data', $AppDataDir)
    $quoted = @()
    foreach ($argument in $verifyArgs) {
        if ($argument -match '[\s"&|<>]') { $quoted += '"' + $argument + '"' } else { $quoted += $argument }
    }
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location $Desktop
    try {
        & cmd.exe /c "`"node`" $($quoted -join ' ') > `"$logPath`" 2>&1"
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $saved
    }
    $lines = Get-Content $logPath -Encoding UTF8 -ErrorAction SilentlyContinue
    foreach ($line in $lines) {
        if ($line -match '^\[FAIL\]') { Write-Host "    $line" -ForegroundColor Red }
        elseif ($line -match '^\[PASS\]') { Write-Host "    $line" -ForegroundColor Green }
        elseif ($line -match 'passed,') { Write-Host "    $line" -ForegroundColor White }
    }
    if ($code -ne 0) {
        if (-not ($lines | Where-Object { $_ -match '^\[FAIL\]' })) {
            Write-Host '    --- last 20 lines of the verify log ---' -ForegroundColor DarkGray
            $lines | Select-Object -Last 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        }
        Stop-Build 'the packaged app did not pass its end-to-end check' "read $logPath"
    }
    Write-Ok 'end-to-end check passed (log: verify-packaged.log)'
    if (Test-Path $AppDataDir) { Remove-Item $AppDataDir -Recurse -Force -ErrorAction SilentlyContinue }
}

# ------------------------------------------------------------------------ run ---
Write-Host ""
Write-Host '  Skill Router - desktop build' -ForegroundColor White
Write-Host ("  repo     {0}" -f $Repo) -ForegroundColor DarkGray
Write-Host ("  output   {0}" -f $OutDir) -ForegroundColor DarkGray
Write-Host ("  logs     {0}" -f $LogDir) -ForegroundColor DarkGray
Write-Host ("  mode     portable={0} clean={1} fast={2} install={3} verify={4}" -f [bool]$Portable, [bool]$Clean, [bool]$Fast, [bool]$Install, [bool]$Verify) -ForegroundColor DarkGray

Test-Preflight
if ($Clean) { Clear-Previous }
Install-Deps
Build-Shell
Invoke-UnitTests
Build-Web
Build-Backend
Invoke-Package
if ($Install) { Invoke-Install }
if ($Verify) { Invoke-Verify }

$elapsed = ((Get-Date) - $script:StartedAt).TotalSeconds
Write-Host ""
Write-Host ("Done in {0:N1}s" -f $elapsed) -ForegroundColor Green
if ($Portable) {
    $exe = $UnpackedExe
    if (Test-Path $exe) {
        $mb = [math]::Round(((Get-ChildItem (Split-Path $exe -Parent) -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 0)
        Write-Host ("  app      {0}  ({1} MB unpacked)" -f $exe, $mb)
    }
} else {
    if (Test-Path $Installer) {
        $item = Get-Item $Installer
        Write-Host ("  installer {0}" -f $item.FullName)
        Write-Host ("           {0:N1} MB, built {1:yyyy-MM-dd HH:mm}" -f ($item.Length / 1MB), $item.LastWriteTime)
        if (-not $Install) { Write-Host ("  install  {0} /S      (or re-run with -Install)" -f $item.FullName) -ForegroundColor DarkGray }
    }
    if (Test-Path $UnpackedExe) { Write-Host ("  portable {0}" -f $UnpackedExe) -ForegroundColor DarkGray }
}
Write-Host ("  logs     {0}" -f $LogDir) -ForegroundColor DarkGray
Write-Host ""
