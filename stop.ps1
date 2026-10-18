<#
.SYNOPSIS
    Skill Router 一键停止：结束 start.ps1 起的全部本地开发进程。

.DESCRIPTION
    停止顺序与逻辑：
      1. 读 <DataDir>\dev\pids.json，按记录逐个 taskkill /T /F（含 uvicorn --reload、
         tsx watch、next dev 派生的子进程与 engine-host）；
      2. 再按端口与命令行特征兜底清扫「孤儿进程」—— 状态文件被删、上次异常退出、
         或不是本脚本启动但占着 8000/8500/3000 的本项目进程；
      3. 校验端口是否已释放，打印结果；仍有占用则退出码 1。

    只处理能识别为本项目的进程（命令行里带 skillrouter_api / next dev / src/main.ts /
    engine-host 等特征）。其它程序占着端口时只报告、不动手，除非加 -Force。

.PARAMETER DataDir
    运行时数据目录，必须与 start.ps1 用的一致（默认 var）

.PARAMETER ApiPort
    FastAPI 端口，默认 8000

.PARAMETER GatewayPort
    Node 边车网关端口，默认 8500

.PARAMETER WebPort
    Next.js 端口，默认 3000

.PARAMETER Force
    端口仍被占用时，连同「特征不匹配」的占用者一起强杀（默认只报告）

.PARAMETER Quiet
    只输出结论，不逐条打印

.EXAMPLE
    .\stop.ps1
    # 结束全部四个进程并确认端口释放

.EXAMPLE
    .\stop.ps1 -DataDir var\verify -ApiPort 8100 -GatewayPort 8600 -WebPort 3100
    # 停止隔离实例

.EXAMPLE
    .\stop.ps1 -Force
    # 端口被别人占着、且必须让出端口时

.NOTES
    编码：本文件是 UTF-8 with BOM（Windows PowerShell 5.1 需要 BOM 才认中文）。
    执行策略：powershell -NoProfile -ExecutionPolicy Bypass -File .\stop.ps1
#>

[CmdletBinding()]
param(
    [string]$DataDir = 'var',
    [int]$ApiPort = 8000,
    [int]$GatewayPort = 8500,
    [int]$WebPort = 3000,
    [switch]$Force,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

try {
    if ([Console]::OutputEncoding.CodePage -ne 65001) {
        & "$env:ComSpec" /c chcp 65001 | Out-Null
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    }
    $OutputEncoding = New-Object System.Text.UTF8Encoding $false
}
catch { }

$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }

$StartScript = Join-Path $Root 'start.ps1'
$DataRoot = if ([System.IO.Path]::IsPathRooted($DataDir)) { $DataDir } else { Join-Path $Root $DataDir }
$PidFile = Join-Path $DataRoot 'dev\pids.json'

# 只认这些命令行特征，避免误杀别人的进程。
# `$Root` 会在下面追加（含正/反斜杠两种写法）—— tsx / next / uvicorn 的启动命令行里
# 通常带绝对路径，这是最可靠的特征；`npm run dev` 只包一层 cmd，真正占用端口的
# node 子进程一定带路径。
$Markers = @(
    'skillrouter_api.main:app',
    'skillrouter_api.worker',
    'skillrouter_api',
    'aragon-gateway',
    'engine-host',
    'next dev',
    'next-server'
)

# 仓库根路径本身作为特征（覆盖 `node --require <root>\...\tsx ... src/main.ts` 这类
# 命令里不含项目专有词、但绝对路径暴露归属的情况）。正反斜杠都试。
$Markers += @($Root, ($Root -replace '\\', '/'))

function Write-Line([string]$Text, [string]$Level = 'info') {
    if ($Quiet -and $Level -eq 'info') { return }
    $prefix = switch ($Level) {
        'warn' { '[warn] ' }
        'err' { '[fail] ' }
        'ok' { '[ ok ] ' }
        default { '[stop] ' }
    }
    $color = switch ($Level) { 'warn' { 'Yellow' } 'err' { 'Red' } 'ok' { 'Green' } default { 'Gray' } }
    Write-Host ("$prefix $Text") -ForegroundColor $color
}

function Test-PortInUse([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(400)) { return $false }
        return $client.Connected
    }
    catch { return $false }
    finally { $client.Close() }
}

function Get-PortOwnerPid([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    }
    catch { }
    try {
        $line = (& netstat -ano) | Select-String -Pattern 'LISTENING' | Select-String -Pattern ":$Port\s" | Select-Object -First 1
        if ($line) {
            $parts = ($line.ToString().Trim() -split '\s+')
            $candidate = $parts[$parts.Count - 1]
            if ($candidate -match '^\d+$') { return [int]$candidate }
        }
    }
    catch { }
    return 0
}

function Stop-ProcessTree([int]$ProcessId) {
    if (-not $ProcessId) { return $false }
    # 父进程用 /T 结束时会连带干掉子进程，所以轮到子进程时它可能已经不存在；
    # 先查在不在，避免 taskkill 往 stderr 写「not found」。
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $false }
    # taskkill 的 stderr 在 $ErrorActionPreference='Stop' 下会升级成终止性错误，
    # 因此这里临时放宽，并吞掉所有流。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & taskkill.exe /PID $ProcessId /T /F *> $null
    }
    catch { }
    finally { $ErrorActionPreference = $previous }
    return $true
}

function Get-CommandLine([int]$ProcessId) {
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if ($p -and $p.CommandLine) { return [string]$p.CommandLine }
    }
    catch { }
    return ''
}

function Test-IsOurs([string]$CommandLine) {
    if (-not $CommandLine) { return $false }
    foreach ($marker in $Markers) {
        if ($CommandLine.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    }
    return $false
}

function Get-RelatedProcesses {
    # 全量扫一遍，按特征挑出本项目的进程（覆盖 pids.json 丢失的情况）
    $result = @()
    try {
        $all = Get-CimInstance Win32_Process -ErrorAction Stop
    }
    catch {
        return $result
    }
    foreach ($p in $all) {
        if (-not $p.CommandLine) { continue }
        $cl = [string]$p.CommandLine
        if (Test-IsOurs $cl) {
            $result += [pscustomobject]@{ pid = [int]$p.ProcessId; name = [string]$p.Name; cmd = $cl }
        }
    }
    return $result
}

# ─────────────────────────────────────────────────────────────────────────────
# 0) 头部
# ─────────────────────────────────────────────────────────────────────────────
if (-not $Quiet) {
    Write-Host ''
    Write-Host '  Skill Router · 停止本地开发进程' -ForegroundColor DarkGray
    Write-Host ("  数据目录  {0}" -f $DataRoot) -ForegroundColor DarkGray
    Write-Host ("  端口      api={0}  gateway={1}  web={2}" -f $ApiPort, $GatewayPort, $WebPort) -ForegroundColor DarkGray
    Write-Host ''
}

$stoppedSomething = $false

# ─────────────────────────────────────────────────────────────────────────────
# 1) 走 start.ps1 的官方停止路径（状态文件 + 正确的停止顺序）
# ─────────────────────────────────────────────────────────────────────────────
if (Test-Path $StartScript) {
    $stateExists = Test-Path $PidFile
    if ($stateExists) {
        $args = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$StartScript`"",
            '-Action', 'stop', '-DataDir', "`"$DataDir`"",
            '-ApiPort', "$ApiPort", '-GatewayPort', "$GatewayPort", '-WebPort', "$WebPort"
        )
        try {
            $out = & powershell.exe @args 2>&1
            foreach ($line in @($out)) {
                $text = [string]$line
                if ($text -match '^\s*\[start\]\s*停止') {
                    if (-not $Quiet) { Write-Host $text -ForegroundColor Gray }
                    $stoppedSomething = $true
                }
            }
        }
        catch {
            Write-Line ("调用 start.ps1 -Action stop 失败：{0}（改用兜底清扫）" -f $_.Exception.Message) 'warn'
        }
    }
    else {
        Write-Line '没有 pids.json，跳过按记录停止，直接做兜底清扫' 'warn'
    }
}
else {
    Write-Line '找不到 start.ps1，仅做兜底清扫' 'warn'
}

# ─────────────────────────────────────────────────────────────────────────────
# 2) 兜底：按特征清扫剩余进程
# ─────────────────────────────────────────────────────────────────────────────
Start-Sleep -Milliseconds 400
$related = @(Get-RelatedProcesses | Where-Object { $_.pid -ne $PID })
if ($related.Count -gt 0) {
    Write-Line ("发现 {0} 个残留进程，逐个结束" -f $related.Count) 'warn'
    foreach ($proc in $related) {
        # 父进程 /T 结束会连带子进程，这里只对仍然存在的进程下手
        if (Stop-ProcessTree $proc.pid) {
            Write-Line ("  结束 {0}（PID {1}）" -f $proc.name, $proc.pid)
            $stoppedSomething = $true
        }
    }
    Start-Sleep -Milliseconds 700
}

# ─────────────────────────────────────────────────────────────────────────────
# 3) 端口校验（有占用就定位、必要时强杀）
# ─────────────────────────────────────────────────────────────────────────────
$leftover = @()
foreach ($port in @($ApiPort, $GatewayPort, $WebPort)) {
    if (-not $port) { continue }
    if (-not (Test-PortInUse $port)) { continue }

    $ownerPid = Get-PortOwnerPid $port
    $cmd = if ($ownerPid) { Get-CommandLine $ownerPid } else { '' }
    $ours = Test-IsOurs $cmd

    if ($ours -or $Force) {
        Write-Line ("端口 {0} 仍被占用（PID {1}），结束它" -f $port, $ownerPid) 'warn'
        if (Stop-ProcessTree $ownerPid) { $stoppedSomething = $true }
        Start-Sleep -Milliseconds 700
    }
    else {
        $short = if ($cmd) { $cmd.Substring(0, [Math]::Min(120, $cmd.Length)) } else { '（读不到命令行）' }
        Write-Line ("端口 {0} 被其它程序占用：PID {1} :: {2}" -f $port, $ownerPid, $short) 'err'
        $leftover += $port
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# 4) 复核
# ─────────────────────────────────────────────────────────────────────────────
Start-Sleep -Milliseconds 300
$stillListening = @()
foreach ($port in @($ApiPort, $GatewayPort, $WebPort)) {
    if ($port -and (Test-PortInUse $port)) { $stillListening += $port }
}
$stillRunning = @(Get-RelatedProcesses | Where-Object { $_.pid -ne $PID })

if (Test-Path $PidFile) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($stillListening.Count -eq 0 -and $stillRunning.Count -eq 0) {
    if ($stoppedSomething) { Write-Line '全部进程已停止，端口已释放。' 'ok' }
    else { Write-Line '没有在运行的本项目进程（端口也都空闲）。' 'ok' }
    exit 0
}

if ($stillListening.Count -gt 0) {
    Write-Line ("端口仍被占用：{0}" -f ($stillListening -join ', ')) 'err'
    Write-Line '这些不是本项目的进程，或用例特征不匹配；确认后可用 .\stop.ps1 -Force 强杀。' 'err'
}
foreach ($proc in $stillRunning) {
    Write-Line ("进程仍在：{0}（PID {1}）" -f $proc.name, $proc.pid) 'err'
}
exit 1
