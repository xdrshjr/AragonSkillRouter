<#
.SYNOPSIS
    Skill Router 桌面端（Electron）一键出包脚本。

.DESCRIPTION
    一条命令完成：环境体检 → 纯净 CPython 3.11 venv → 依赖 → 打包 → 产物校验 → 汇总。

    它只负责「调现成流程」，不重新实现任何构建逻辑：
      npm run dist:win   = scripts/build-all.mjs（gateway → web → sidecar → main）+ electron-builder
      npm run verify     = scripts/verify-artifact.mjs（资源三件套 + 端口契约 + 哈希清单）
      npm run smoke      = scripts/smoke.mjs（净化 PATH 下端到端冒烟）

    本脚本额外负责两件最容易翻车的事（见 apps/desktop/README.md §2.1）：
      1. 自动准备**纯净** CPython 3.11 venv（uv 优先、py -3.11 兜底），并设置 PYTHON_BIN。
         本机 where python 常是 Anaconda；用它跑 PyInstaller 会把 conda DLL 烘进产物，
         在没装过 Python 的干净机上表现为「启动即缺 DLL」。
      2. 出包前体检（Node ≥ 22 / npm / 磁盘空间 / 长路径 / Electron 缓存），
         把「20 分钟后才失败」提前成「5 秒内失败并说清原因」。

.PARAMETER Mode
    all（默认）= 体检 + venv + 依赖 + 出包 + 校验
    prepare     = 只做体检 + venv + 依赖（不出包，排错用）
    build       = 只出包（含 venv + 依赖）
    verify      = 只校验已有产物（verify-artifact.mjs）
    smoke       = 只跑端到端冒烟（需要已出包）
    clean       = 清理 build / dist / resources / release
    help        = 显示帮助

.PARAMETER SkipInstall
    跳过 npm install（要求 apps\desktop\node_modules 已存在 electron-builder）

.PARAMETER SkipVenv
    不要自动创建 venv；必须由 -PythonBin / $env:PYTHON_BIN 提供纯净 CPython 3.11

.PARAMETER PythonBin
    指定构建用解释器（必须是非 conda 的 CPython 3.11）

.PARAMETER Clean
    出包前清掉旧产物（build / dist / resources / release，含 venv；会重新装构建依赖）

.PARAMETER NoVerify
    all 模式跳过 npm run verify

.PARAMETER Smoke
    all 模式出包后追加 npm run smoke（约 1–2 分钟，会真起一次应用）

.PARAMETER Open
    结束后打开 apps\desktop\release

.PARAMETER MinFreeGB
    出包所需最小空闲磁盘（GB），默认 6

.PARAMETER Force
    clean 模式不再交互确认

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\build-desktop.ps1

.EXAMPLE
    .\build-desktop.ps1 -Mode build -Clean -Smoke

.EXAMPLE
    .\build-desktop.ps1 -Mode verify

.NOTES
    编码：本文件是 UTF-8 with BOM（Windows PowerShell 5.1 需要 BOM 才认中文）。
    产物：apps\desktop\release\SkillRouter-Setup-<version>-win-x64.exe
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('all', 'prepare', 'build', 'verify', 'smoke', 'clean', 'help')]
    [string]$Mode = 'all',

    [switch]$SkipInstall,
    [switch]$SkipVenv,
    [string]$PythonBin = '',
    [switch]$Clean,
    [switch]$NoVerify,
    [switch]$Smoke,
    [switch]$Open,
    [int]$MinFreeGB = 6,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# 本文件是 UTF-8；把控制台与 PowerShell 的输出编码对齐到 UTF-8，中文才不乱码。
try {
    if ([Console]::OutputEncoding.CodePage -ne 65001) {
        & "$env:ComSpec" /c chcp 65001 | Out-Null
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    }
    $OutputEncoding = New-Object System.Text.UTF8Encoding $false
}
catch { }

# ─────────────────────────────────────────────────────────────────────────────
# 路径与常量（全文只有一套路径口径，与 apps/desktop/electron-builder.yml 对齐）
# ─────────────────────────────────────────────────────────────────────────────
$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }

$DesktopDir   = Join-Path $Root 'apps\desktop'
$BuildDir     = Join-Path $DesktopDir 'build'
$DistDir      = Join-Path $DesktopDir 'dist'
$ResourcesDir = Join-Path $DesktopDir 'resources'
$ReleaseDir   = Join-Path $DesktopDir 'release'
$VenvDir      = Join-Path $BuildDir '.venv'
$VenvPython   = Join-Path $VenvDir 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath (Join-Path $DesktopDir 'package.json'))) {
    Write-Host "[build] 这个脚本必须放在仓库根（含 apps\desktop\package.json）才能运行。" -ForegroundColor Red
    Write-Host "        当前解析到的根目录：$Root" -ForegroundColor Red
    exit 2
}

$script:LogPath   = ''
$script:LogDir    = Join-Path $Root 'var\logs\desktop'
$script:StartTime = Get-Date
$script:NpmExe    = ''
$script:StepTotal = 0
$script:StepIndex = 0

# ─────────────────────────────────────────────────────────────────────────────
# 输出 / 日志
# ─────────────────────────────────────────────────────────────────────────────
function Write-Sys([string]$Text, [string]$Level = 'info') {
    $prefix = switch ($Level) {
        'warn'  { '[warn]  ' }
        'err'   { '[fail]  ' }
        'ok'    { '[ ok ]  ' }
        'step'  { '[step]  ' }
        default { '[build] ' }
    }
    $color = switch ($Level) {
        'warn'  { 'Yellow' }
        'err'   { 'Red' }
        'ok'    { 'Green' }
        'step'  { 'Cyan' }
        default { 'Gray' }
    }
    Write-Host ("$prefix$Text") -ForegroundColor $color
    if ($script:LogPath) {
        try {
            Add-Content -LiteralPath $script:LogPath -Encoding UTF8 -ErrorAction SilentlyContinue `
                -Value ("{0} {1}{2}" -f (Get-Date -Format 'HH:mm:ss'), $prefix, $Text)
        }
        catch { }
    }
}

function Write-StepHeader([string]$Text) {
    $script:StepIndex++
    $label = if ($script:StepTotal -gt 0) { "($($script:StepIndex)/$($script:StepTotal)) " } else { '' }
    Write-Sys ("$label$Text") 'step'
}

# 不计入步数的章节标题（汇总 / 清理这类辅助动作用它，避免出现 (4/3)）
function Write-Section([string]$Text) {
    Write-Sys $Text 'step'
}

function Initialize-Log {
    New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $script:LogPath = Join-Path $script:LogDir ("build-{0}.log" -f $stamp)
    $header = @(
        'Skill Router desktop build log',
        "started : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "mode    : $Mode",
        "repo    : $Root",
        "PS      : $($PSVersionTable.PSVersion)",
        ''
    )
    Set-Content -LiteralPath $script:LogPath -Value $header -Encoding UTF8
}

# ─────────────────────────────────────────────────────────────────────────────
# 通用工具
# ─────────────────────────────────────────────────────────────────────────────
function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} B' -f $Bytes)
}

function Measure-DirSize([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return [double]0 }
    try {
        $sum = (Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
        if (-not $sum) { return [double]0 }
        return [double]$sum
    }
    catch { return [double]0 }
}

function Get-NpmExe {
    # PowerShell 会把 `npm` 解析成 npm.ps1 shim（strict mode 下会炸），统一走 npm.cmd。
    foreach ($candidate in @('npm.cmd', 'npm.exe', 'npm')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { return $candidate }
    }
    return ''
}

# 探针：跑一条命令只取输出与退出码，不因为非 0 退出把整个脚本掀翻。
function Invoke-Probe([string]$Exe, [string[]]$ArgList) {
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ Ok = $false; Code = 127; Out = "找不到命令：$Exe" }
    }
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 127
    try {
        $out = & $Exe @ArgList 2>&1
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    }
    catch {
        return [pscustomobject]@{ Ok = $false; Code = 127; Out = ($_ | Out-String).Trim() }
    }
    finally { $ErrorActionPreference = $prevEap }
    return [pscustomobject]@{ Ok = ($code -eq 0); Code = $code; Out = ($out | Out-String).Trim() }
}

# 跑一个长步骤：实时回显 + 落日志；非 0 退出即抛（带耗时与日志路径）。
function Invoke-Child([string]$Label, [string]$Exe, [string[]]$ArgList, [string]$WorkDir = $Root) {
    Write-Sys ("▶ {0}" -f $Label) 'step'
    if ($script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Encoding UTF8 -ErrorAction SilentlyContinue `
            -Value ('$ ' + $Exe + ' ' + ($ArgList -join ' '))
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 0
    Push-Location -LiteralPath $WorkDir
    try {
        & $Exe @ArgList 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { [string]$_ }
            Write-Host "    $line"
            if ($script:LogPath) {
                try {
                    Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
                }
                catch { }
            }
        }
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    }
    finally {
        $ErrorActionPreference = $prevEap
        Pop-Location
        $sw.Stop()
    }
    if ($code -ne 0) {
        throw ("{0} 失败（退出码 {1}），耗时 {2:N1} s。完整日志：{3}" -f $Label, $code, $sw.Elapsed.TotalSeconds, $script:LogPath)
    }
    Write-Sys ("✔ {0} 完成（{1:N1} s）" -f $Label, $sw.Elapsed.TotalSeconds) 'ok'
}

# ─────────────────────────────────────────────────────────────────────────────
# 1) 环境体检
# ─────────────────────────────────────────────────────────────────────────────
function Assert-Preflight {
    Write-StepHeader '环境体检'

    if ($env:OS -ne 'Windows_NT') { throw '本脚本只支持 Windows：electron-builder 的目标是 win-x64。' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw '需要 64 位 Windows 10/11。' }

    # Node ≥ 22（构建链：electron 33 / electron-builder 26 / Next 15 standalone）
    $node = Invoke-Probe 'node' @('-v')
    if (-not $node.Ok) {
        throw '找不到 node。请安装 Node.js ≥ 22（winget install --id=OpenJS.NodeJS.LTS -e），装完重开终端。'
    }
    $nodeText = ($node.Out -split "`r?`n")[0].Trim().TrimStart('v')
    $nodeMajor = 0
    [void][int]::TryParse(($nodeText -split '\.')[0], [ref]$nodeMajor)
    if ($nodeMajor -lt 22) {
        throw ("需要 Node ≥ 22（当前 v{0}）。升级：winget upgrade --id=OpenJS.NodeJS.LTS -e" -f $nodeText)
    }
    Write-Sys ("node v{0}" -f $nodeText) 'ok'

    $script:NpmExe = Get-NpmExe
    if (-not $script:NpmExe) { throw '找不到 npm（Node 安装不完整？）' }
    $npm = Invoke-Probe $script:NpmExe @('-v')
    if (-not $npm.Ok) { throw ("npm 无法执行：{0}" -f $npm.Out) }
    Write-Sys ("npm v{0}（{1}）" -f (($npm.Out -split "`r?`n")[0].Trim()), $script:NpmExe) 'ok'

    # 磁盘空间
    $spaceOk = $false
    try {
        $qualifier = [System.IO.Path]::GetPathRoot($Root)
        $driveName = $qualifier.Substring(0, 2).TrimEnd(':', '\', '/')
        $drive = Get-PSDrive -Name $driveName -ErrorAction Stop
        $freeBytes = [double]$drive.Free
        $needBytes = [double]$MinFreeGB * 1GB
        $spaceOk = $true
        if ($freeBytes -lt $needBytes) {
            throw ("磁盘空间不足：{0} 只剩 {1}，至少需要 {2} GB（PyInstaller 边车 + Next standalone + Electron 缓存）。" -f $qualifier, (Format-Size $freeBytes), $MinFreeGB)
        }
        Write-Sys ("磁盘 {0} 可用 {1}" -f $qualifier, (Format-Size $freeBytes)) 'ok'
    }
    catch {
        if ($spaceOk) { throw }
        Write-Sys '无法读取磁盘剩余空间，跳过该检查' 'warn'
    }

    # 长路径（node_modules 嵌套很深时 electron-builder 可能超 260 字符）
    $longPaths = $null
    try {
        $longPaths = (Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
                -Name 'LongPathsEnabled' -ErrorAction Stop).LongPathsEnabled
    }
    catch { }
    if ($longPaths -ne 1) {
        Write-Sys '提示：LongPathsEnabled 未启用。若出包报「路径过长 / ENAMETOOLONG」，启用后重启再试。' 'warn'
    }

    # 首次出包需要联网拉 Electron / NSIS
    $cacheRoot = Join-Path $env:LOCALAPPDATA 'electron-builder\Cache'
    if (-not (Test-Path -LiteralPath $cacheRoot)) {
        Write-Sys '首次出包需联网下载 Electron / NSIS 资源（约 200-300 MB，缓存到 %LOCALAPPDATA%\electron-builder\Cache）' 'warn'
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# 2) 纯净 CPython 3.11（PyInstaller 边车的硬约束，P1-16）
# ─────────────────────────────────────────────────────────────────────────────
function Test-Interpreter([string]$Exe) {
    $result = [pscustomobject]@{ Exe = $Exe; Ok = $false; Version = ''; BasePrefix = ''; Reason = '' }
    $probe = Invoke-Probe $Exe @('-c', "import sys;print(sys.base_prefix);print('{}.{}'.format(*sys.version_info[:2]))")
    if (-not $probe.Ok) {
        $first = ($probe.Out -split "`r?`n")[0]
        if (-not $first) { $first = '不可执行' }
        $result.Reason = "无法执行：$first"
        return $result
    }
    $lines = @($probe.Out -split "`r?`n" | Where-Object { $_.Trim().Length -gt 0 })
    if ($lines.Count -ge 2) {
        $result.BasePrefix = $lines[0].Trim()
        $result.Version = $lines[1].Trim()
    }
    $hay = "$($result.BasePrefix) $Exe"
    if ($hay -match 'conda|anaconda|miniconda|windowsapps') {
        $result.Reason = "解释器来自 conda / Microsoft Store（$($result.BasePrefix)），会把 conda DLL 烘进产物"
        return $result
    }
    if ($result.Version -ne '3.11') {
        $result.Reason = "版本是 $($result.Version)，需要 CPython 3.11"
        return $result
    }
    $result.Ok = $true
    return $result
}

function Ensure-PythonVenv {
    Write-StepHeader '准备纯净 CPython 3.11 解释器'

    $candidates = New-Object System.Collections.ArrayList
    if ($PythonBin) { [void]$candidates.Add($PythonBin) }
    if ($env:PYTHON_BIN) { [void]$candidates.Add($env:PYTHON_BIN) }
    if (Test-Path -LiteralPath $VenvPython) { [void]$candidates.Add($VenvPython) }

    foreach ($cand in $candidates) {
        $info = Test-Interpreter $cand
        if ($info.Ok) {
            $env:PYTHON_BIN = $info.Exe
            Write-Sys ("复用解释器 {0}（{1}，base_prefix={2}）" -f $info.Exe, $info.Version, $info.BasePrefix) 'ok'
            return
        }
        Write-Sys ("跳过 {0}：{1}" -f $cand, $info.Reason) 'warn'
    }

    if ($SkipVenv) {
        throw '没有可用的纯净 CPython 3.11，而指定了 -SkipVenv。请用 -PythonBin 指定，或去掉 -SkipVenv 让脚本用 uv 创建 venv。'
    }

    if (Test-Path -LiteralPath $VenvDir) {
        Write-Sys "删除旧 venv（解释器来源不合规）：$VenvDir" 'warn'
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

    $uv = Get-Command 'uv' -ErrorAction SilentlyContinue
    if ($uv) {
        # --managed-python 是关键：没有它，本机存在 Anaconda 3.11 时 uv 会直接复用 conda 解释器，
        # 建出来的 venv base_prefix 仍指向 anaconda3，PyInstaller 产物在干净机上启动即缺 DLL（P1-16）。
        Write-Sys 'uv 会用自己管理的 CPython 3.11（首次需联网下载约 30 MB）' 'info'
        $uvArgs = @('venv', '--seed', '--python', '3.11', '--managed-python', $VenvDir)
        try {
            Invoke-Child "uv venv --seed --python 3.11 --managed-python $VenvDir" $uv.Source $uvArgs $Root
        }
        catch {
            Write-Sys 'uv 不支持 --managed-python（版本过旧），回退到普通 uv venv；若解释器来自 conda 会再被拦下' 'warn'
            if (Test-Path -LiteralPath $VenvDir) { Remove-Item -LiteralPath $VenvDir -Recurse -Force }
            Invoke-Child "uv venv --seed --python 3.11 $VenvDir" $uv.Source `
                @('venv', '--seed', '--python', '3.11', $VenvDir) $Root
        }
    }
    else {
        $pyProbe = Invoke-Probe 'py' @('-3.11', '-c', 'import sys;print(sys.version)')
        if (-not $pyProbe.Ok) {
            throw ('既没有 uv，也没有 Python 3.11。二选一安装后重跑：`n' +
                '  winget install --id=astral-sh.uv -e          # 推荐，体积小且能固定 3.11`n' +
                '  winget install --id=Python.Python.3.11 -e')
        }
        Invoke-Child 'py -3.11 -m venv（纯净 CPython 3.11）' 'py' @('-3.11', '-m', 'venv', $VenvDir) $Root
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "venv 创建后仍找不到 $VenvPython"
    }
    $info = Test-Interpreter $VenvPython
    if (-not $info.Ok) {
        $hint = ''
        if ($info.Reason -match 'conda|Store') {
            $hint = "`n对策：升级 uv（winget upgrade --id=astral-sh.uv -e，0.12+ 支持 --managed-python），或用 -PythonBin 指向官方 CPython 3.11。"
        }
        throw ("新建的 venv 不可用：{0}（{1}）{2}" -f $info.Reason, $VenvPython, $hint)
    }
    $env:PYTHON_BIN = $VenvPython
    Write-Sys ("解释器就绪：{0}（{1}，base_prefix={2}）" -f $VenvPython, $info.Version, $info.BasePrefix) 'ok'
}

# ─────────────────────────────────────────────────────────────────────────────
# 3) Node 依赖
# ─────────────────────────────────────────────────────────────────────────────
function Ensure-NodeModules {
    Write-StepHeader '准备 apps\desktop 依赖'
    $hasBuilder = Test-Path -LiteralPath (Join-Path $DesktopDir 'node_modules\electron-builder')

    if ($SkipInstall) {
        if (-not $hasBuilder) {
            throw 'apps\desktop\node_modules 缺 electron-builder，但指定了 -SkipInstall。'
        }
        Write-Sys '按 -SkipInstall 跳过 npm install' 'warn'
        return
    }
    if ($hasBuilder) {
        Write-Sys 'node_modules 已就绪（要强制重装请先删 apps\desktop\node_modules）' 'ok'
        return
    }
    Invoke-Child 'npm install --include=dev' $script:NpmExe `
        @('install', '--no-audit', '--no-fund', '--include=dev') $DesktopDir
}

# ─────────────────────────────────────────────────────────────────────────────
# 4) 出包 / 5) 校验
# ─────────────────────────────────────────────────────────────────────────────
function Invoke-Dist {
    Write-StepHeader '打包（gateway → web → sidecar → main → electron-builder）'
    Write-Sys 'sidecar 会跑 PyInstaller 双检（bootstrap + api 探活），整体约 5-15 分钟' 'info'
    Invoke-Child 'npm run dist:win' $script:NpmExe @('run', 'dist:win') $DesktopDir
}

function Invoke-Verify {
    Write-StepHeader '产物校验（verify-artifact）'
    Invoke-Child 'npm run verify' $script:NpmExe @('run', 'verify') $DesktopDir
}

function Invoke-Smoke {
    Write-StepHeader '端到端冒烟（净化 PATH，真起一次应用）'
    if (-not (Test-Path -LiteralPath (Join-Path $ReleaseDir 'win-unpacked\SkillRouter.exe'))) {
        throw 'release\win-unpacked\SkillRouter.exe 不存在，先出包（-Mode build）再冒烟。'
    }
    Invoke-Child 'npm run smoke' $script:NpmExe @('run', 'smoke') $DesktopDir
}

# ─────────────────────────────────────────────────────────────────────────────
# 6) 清理
# ─────────────────────────────────────────────────────────────────────────────
function Invoke-CleanTargets([bool]$Ask) {
    Write-Section '清理旧产物'
    # 注意：apps\desktop\build\ 里有提交物 icon.png（electron-builder 的 buildResources），
    # 因此这里只删 build 下的**生成物**，绝不删整个 build 目录。
    $targets = @(
        (Join-Path $BuildDir '.venv'),
        (Join-Path $BuildDir '.pyi'),
        (Join-Path $BuildDir '.smoke-data'),
        (Join-Path $BuildDir 'python'),
        (Join-Path $BuildDir 'gateway'),
        (Join-Path $BuildDir 'web'),
        $DistDir,
        $ResourcesDir,
        $ReleaseDir
    )
    $existing = @($targets | Where-Object { Test-Path -LiteralPath $_ })
    if ($existing.Count -eq 0) {
        Write-Sys '没有需要清理的目录' 'ok'
        return
    }
    foreach ($t in $existing) {
        Write-Sys ("  {0}  ({1})" -f $t, (Format-Size (Measure-DirSize $t)))
    }
    if ($Ask -and -not $Force) {
        $answer = Read-Host '确认删除以上目录？(y/N)'
        if ($answer -notmatch '^(y|yes|Y)$') {
            Write-Sys '已取消清理' 'warn'
            return
        }
    }
    foreach ($t in $existing) {
        try {
            Remove-Item -LiteralPath $t -Recurse -Force
            Write-Sys ("已删除 {0}" -f $t) 'ok'
        }
        catch {
            throw ("删除 {0} 失败（可能有进程占用，比如 SkillRouter.exe 还开着）：{1}" -f $t, $_.Exception.Message)
        }
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# 7) 汇总
# ─────────────────────────────────────────────────────────────────────────────
function Show-Summary([double]$Seconds) {
    Write-Section '产物汇总'
    if (-not (Test-Path -LiteralPath $ReleaseDir)) {
        Write-Sys "release 目录不存在（$ReleaseDir）：本次没有出包。" 'warn'
        return
    }

    $installer = Get-ChildItem -LiteralPath $ReleaseDir -Filter 'SkillRouter-Setup-*-win-x64.exe' `
        -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $unpacked = Join-Path $ReleaseDir 'win-unpacked\SkillRouter.exe'
    $sums = Join-Path $ReleaseDir 'SKILLROUTER-SHA256SUMS.txt'
    $report = Join-Path $ReleaseDir 'smoke-report.json'

    if ($installer) {
        Write-Sys ("安装器   {0}" -f $installer.FullName) 'ok'
        Write-Sys ("         {0}   {1}" -f (Format-Size $installer.Length), $installer.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))
        try {
            $hash = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash
            Write-Sys ("         SHA256 {0}" -f $hash)
        }
        catch { Write-Sys '         计算 SHA256 失败（文件被占用？）' 'warn' }
    }
    else {
        Write-Sys "没找到 SkillRouter-Setup-<version>-win-x64.exe（$ReleaseDir）" 'warn'
    }

    if (Test-Path -LiteralPath $unpacked) {
        $u = Get-Item -LiteralPath $unpacked
        Write-Sys ("免安装   {0}  ({1})" -f $unpacked, (Format-Size $u.Length))
    }
    if (Test-Path -LiteralPath $sums) { Write-Sys ("哈希清单 {0}" -f $sums) }
    else { Write-Sys '哈希清单缺失：还没跑 npm run verify' 'warn' }
    if (Test-Path -LiteralPath $report) { Write-Sys ("冒烟报告 {0}" -f $report) }

    Write-Sys ("release 总大小 {0}" -f (Format-Size (Measure-DirSize $ReleaseDir)))
    Write-Sys ("总耗时 {0:N1} 分钟 · 日志 {1}" -f ($Seconds / 60), $script:LogPath) 'ok'

    $installerText = '（尚未生成）'
    if ($installer) { $installerText = $installer.FullName }
    Write-Host ''
    Write-Host '  下一步：' -ForegroundColor Cyan
    Write-Host ("    1. 双击安装：{0}" -f $installerText) -ForegroundColor Gray
    Write-Host '    2. 复跑校验：.\build-desktop.ps1 -Mode verify' -ForegroundColor Gray
    Write-Host '    3. 端到端冒烟：.\build-desktop.ps1 -Mode smoke' -ForegroundColor Gray
    Write-Host '    4. 未做代码签名（R1）：SmartScreen 会提示「更多信息 → 仍要运行」' -ForegroundColor DarkGray
    Write-Host ''
}

function Show-Help {
    @'
Skill Router 桌面端（Electron）一键出包

用法：
  powershell -NoProfile -ExecutionPolicy Bypass -File .\build-desktop.ps1 [参数]

模式（-Mode）：
  all        默认。体检 → venv → 依赖 → 出包 → 校验 → 汇总
  prepare    只做体检 + venv + 依赖（不出包，排错用）
  build      出包（含 venv + 依赖）
  verify     只校验已有产物（verify-artifact.mjs）
  smoke      只跑端到端冒烟（需要已出包）
  clean      清理 build 下的生成物 + dist / resources / release（保留 build\icon.png）
  help       本帮助

参数：
  -SkipInstall      跳过 npm install（要求 node_modules 已就绪）
  -SkipVenv         不自动建 venv（需 -PythonBin / $env:PYTHON_BIN 提供纯净 CPython 3.11）
  -PythonBin <path> 指定构建解释器（必须非 conda 的 CPython 3.11）
  -Clean            出包前清旧产物（含 venv，会重装构建依赖）
  -NoVerify         all 模式跳过 npm run verify
  -Smoke            all 模式出包后追加 npm run smoke
  -Open             结束后打开 apps\desktop\release
  -MinFreeGB <n>    最小空闲磁盘，默认 6
  -Force            clean 模式不再交互确认

前置（脚本自动检查，缺了就报错并给出安装命令）：
  Node ≥ 22、npm、uv（或 Python 3.11）、目标盘 ≥ 6 GB 空闲
  首次出包需联网下载 Electron / NSIS（约 200-300 MB）

它调用的现成流程（apps/desktop/package.json）：
  npm run dist:win   build-all.mjs（gateway → web → sidecar → main）+ electron-builder
  npm run verify     verify-artifact.mjs（资源三件套 + 端口契约 127.0.0.1:18100 + 哈希清单）
  npm run smoke      smoke.mjs（净化 PATH 下端到端冒烟 + 孤儿进程检查）

产物（相对仓库根）：
  apps\desktop\release\SkillRouter-Setup-<version>-win-x64.exe   双击安装
  apps\desktop\release\win-unpacked\SkillRouter.exe              免安装目录
  apps\desktop\release\SKILLROUTER-SHA256SUMS.txt                哈希清单
  apps\desktop\release\smoke-report.json                         冒烟报告

日志：var\logs\desktop\build-<时间戳>.log

示例：
  .\build-desktop.ps1                      # 一键出包
  .\build-desktop.ps1 -Mode build -Clean   # 彻底重来
  .\build-desktop.ps1 -Mode prepare        # 只把环境准备好
  .\build-desktop.ps1 -Mode smoke -Open    # 冒烟后打开产物目录
'@ | Write-Host
}

# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────
if ($Mode -eq 'help') { Show-Help; exit 0 }

if ($Mode -eq 'clean') {
    Initialize-Log
    try {
        Invoke-CleanTargets -Ask $true
        exit 0
    }
    catch {
        Write-Sys $_.Exception.Message 'err'
        exit 1
    }
}

Initialize-Log
switch ($Mode) {
    'all'     { $script:StepTotal = 5; if ($Smoke) { $script:StepTotal = 6 } }
    'prepare' { $script:StepTotal = 3 }
    'build'   { $script:StepTotal = 4 }
    'verify'  { $script:StepTotal = 2 }
    'smoke'   { $script:StepTotal = 2 }
}

try {
    Assert-Preflight

    if ($Clean -and $Mode -in @('all', 'build')) {
        Invoke-CleanTargets -Ask $false
    }

    if ($Mode -in @('all', 'prepare', 'build')) {
        Ensure-PythonVenv
        Ensure-NodeModules
    }

    switch ($Mode) {
        'prepare' {
            Write-Sys '环境已就绪（未出包）。接下来：.\build-desktop.ps1 -Mode build' 'ok'
        }
        'build' {
            Invoke-Dist
        }
        'all' {
            Invoke-Dist
            if (-not $NoVerify) { Invoke-Verify }
            else { Write-Sys '按 -NoVerify 跳过产物校验' 'warn' }
            if ($Smoke) { Invoke-Smoke }
        }
        'verify' { Invoke-Verify }
        'smoke'  { Invoke-Smoke }
    }

    $elapsed = ((Get-Date) - $script:StartTime).TotalSeconds
    Show-Summary $elapsed

    if ($Open -and (Test-Path -LiteralPath $ReleaseDir)) { Invoke-Item -LiteralPath $ReleaseDir }
    exit 0
}
catch {
    Write-Sys $_.Exception.Message 'err'
    Write-Sys ("总耗时 {0:N1} 分钟 · 完整日志 {1}" -f (((Get-Date) - $script:StartTime).TotalSeconds / 60), $script:LogPath) 'err'
    Write-Host ''
    Write-Host '  常见原因：' -ForegroundColor Yellow
    Write-Host '    * 解释器 / Node 版本不对 → 用 -Mode prepare 单独复现' -ForegroundColor Gray
    Write-Host '    * PyInstaller 双检红 → 看日志里 build:sidecar 的最后 40 行' -ForegroundColor Gray
    Write-Host '    * 端口 18099 / 18100 被占 → 关掉残留的 SkillRouter.exe 后重试' -ForegroundColor Gray
    Write-Host ("    * 日志：{0}" -f $script:LogPath) -ForegroundColor Gray
    Write-Host ''
    exit 1
}
