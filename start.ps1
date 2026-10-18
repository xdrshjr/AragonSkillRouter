<#
.SYNOPSIS
    Skill Router 一键本地开发启动器：拉起 api / worker / gateway / web 四个进程。

.DESCRIPTION
    做的事情（全部幂等，可反复执行）：
      1. 检查 Python / Node / npm 与三处 node_modules，缺了就装（--include=dev）
      2. 准备仓库根 .env（首次生成随机网关密钥；缺密钥时自动补齐）
      3. 用**绝对路径**固定数据目录 / DB / 网关 / 引擎指针，避免 cwd 不同导致两套库
      4. 跑 scripts/bootstrap.py（迁移 + 管理员 + 目录）
      5. 后台启动四个进程，日志落到 var\logs\dev\，控制台合并彩色滚动输出
      6. 就绪探测 /healthz 与 /login，然后打开浏览器
      Ctrl+C 停止全部；进程 PID 记在 var\dev\pids.json，可用 -Action stop 兜底。

    与 README §3 的四终端做法等价，但只需一个窗口、一条命令。

.PARAMETER Action
    start（默认）/ stop / restart / status / logs / check

.PARAMETER ApiPort
    FastAPI 端口，默认 8000

.PARAMETER GatewayPort
    Node 边车网关端口，默认 8500

.PARAMETER WebPort
    Next.js 端口，默认 3000

.PARAMETER Components
    要启动的组件子集，默认全部：api,worker,gateway,web

.PARAMETER EnvName
    SKILLROUTER_ENV，默认 development（生产值只影响 Cookie Secure / --reload 等，本地调试请勿用 production）

.PARAMETER DataDir
    运行时数据目录（相对仓库根），默认 var

.PARAMETER NoInstall
    缺依赖也不安装，直接尝试启动

.PARAMETER NoBootstrap
    跳过 scripts/bootstrap.py（数据库已就绪时可加速）

.PARAMETER NoBrowser
    不自动打开浏览器

.PARAMETER Force
    端口被占用时强杀占用进程（默认只报告并退出）

.PARAMETER NoFollow
    加上它就不滚动日志：打印地址后返回，进程留在后台；需要看滚动的日志请去掉这个开关

.EXAMPLE
    .\start.ps1
    # 装依赖（首次）→ 初始化 → 起四个进程 → 边跑边看日志

.EXAMPLE
    .\start.ps1 -Action status
    # 端口 / 进程 / 健康状态一览

.EXAMPLE
    .\start.ps1 -Action stop
    # 停掉本脚本启动的全部进程（taskkill /T，含 uvicorn --reload 的子进程）

.EXAMPLE
    .\start.ps1 -Components api,web -NoBootstrap
    # 只调前端 + 后端；--reload 仍在，改代码立即生效

.NOTES
    编码：本文件是 UTF-8 with BOM（Windows PowerShell 5.1 需要 BOM 才认中文）。
    执行策略：powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'check')]
    [string]$Action = 'start',

    [int]$ApiPort = 8000,
    [int]$GatewayPort = 8500,
    [int]$WebPort = 3000,

    [ValidateSet('api', 'worker', 'gateway', 'web')]
    [string[]]$Components = @('api', 'worker', 'gateway', 'web'),

    [ValidateSet('development', 'staging', 'production')]
    [string]$EnvName = 'development',

    [string]$DataDir = 'var',

    [switch]$NoInstall,
    [switch]$NoBootstrap,
    [switch]$NoBrowser,
    [switch]$Force,

    [switch]$NoFollow
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
# 基础路径与常量
# ─────────────────────────────────────────────────────────────────────────────
$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }

$ApiDir = Join-Path $Root 'apps\api'
$WebDir = Join-Path $Root 'apps\web'
$GatewayDir = Join-Path $Root 'agents\aragon-gateway'
$CliDir = Join-Path $Root 'packages\skillrouter-cli'
$EnvFile = Join-Path $Root '.env'

if (-not (Test-Path (Join-Path $ApiDir 'skillrouter_api\main.py'))) {
    Write-Host "[start] 这个脚本必须放在仓库根（含 apps\api\skillrouter_api\main.py）才能运行。" -ForegroundColor Red
    Write-Host "        当前解析到的根目录：$Root" -ForegroundColor Red
    exit 2
}

$DataRoot = if ([System.IO.Path]::IsPathRooted($DataDir)) { $DataDir } else { Join-Path $Root $DataDir }
$DbFile = Join-Path $DataRoot 'skillrouter.db'
$LogDir = Join-Path $DataRoot 'logs\dev'
$StateDir = Join-Path $DataRoot 'dev'
$PidFile = Join-Path $StateDir 'pids.json'

$Colors = @{
    api     = 'Cyan'
    worker  = 'DarkCyan'
    gateway = 'Magenta'
    web     = 'Green'
    sys     = 'Yellow'
}

$script:BannerShown = $false

# ─────────────────────────────────────────────────────────────────────────────
# 输出helper
# ─────────────────────────────────────────────────────────────────────────────
function Write-Sys([string]$Text, [string]$Level = 'info') {
    $prefix = switch ($Level) { 'warn' { '[warn] ' } 'err' { '[fail] ' } 'ok' { '[ ok ] ' } default { '[start]' } }
    $color = switch ($Level) { 'warn' { 'Yellow' } 'err' { 'Red' } 'ok' { 'Green' } default { 'Gray' } }
    Write-Host ("$prefix $Text") -ForegroundColor $color
}

function Write-Banner {
    $script:BannerShown = $true
    $lines = @(
        '',
        '  ┌──────────────────────────────────────────────────────────────┐',
        '  │  Skill Router · 本地开发一键启动                             │',
        '  └──────────────────────────────────────────────────────────────┘',
        "   仓库根      $Root",
        "   数据目录    $DataRoot",
        "   数据库      $DbFile",
        "   环境        SKILLROUTER_ENV=$EnvName",
        "   端口        api=$ApiPort  gateway=$GatewayPort  web=$WebPort",
        ''
    )
    foreach ($l in $lines) { Write-Host $l -ForegroundColor DarkGray }
}

function Format-Tail([string]$Path, [int]$Count = 40) {
    if (-not (Test-Path $Path)) { return "(无日志：$Path)" }
    $all = @(Get-Content -LiteralPath $Path -Encoding UTF8 -ErrorAction SilentlyContinue)
    if ($all.Count -eq 0) { return "(日志为空：$Path)" }
    $start = [Math]::Max(0, $all.Count - $Count)
    return ($all[$start..($all.Count - 1)] -join [Environment]::NewLine)
}

# ─────────────────────────────────────────────────────────────────────────────
# .env 读写
# ─────────────────────────────────────────────────────────────────────────────
function Read-DotEnv([string]$Path) {
    $map = [ordered]@{}
    if (-not (Test-Path $Path)) { return $map }
    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim()
        if ($val.Length -ge 2 -and (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        if ($key -match '^[A-Za-z_][A-Za-z0-9_]*$') { $map[$key] = $val }
    }
    return $map
}

function Set-DotEnvValue([string]$Path, [string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path $Path) { $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8) }
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*$([regex]::Escape($Key))\s*=") {
            $lines[$i] = "$Key=$Value"
            $found = $true
        }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
}

function New-RandomSecret([int]$Length = 48) {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] ($Length * 2)
        $rng.GetBytes($bytes)
        $b64 = [Convert]::ToBase64String($bytes)
        $clean = ($b64 -replace '[^A-Za-z0-9]', '')
        return $clean.Substring(0, $Length)
    }
    finally { $rng.Dispose() }
}

function Test-SecretPlaceholder([string]$Value) {
    if (-not $Value) { return $true }
    $v = $Value.Trim()
    if ($v.Length -lt 32) { return $true }
    if ($v -match '^<.*>$') { return $true }
    if ($v -like '*generate-me*') { return $true }
    if ($v -eq 'dev-gateway-secret-change-me') { return $true }
    return $false
}

# ─────────────────────────────────────────────────────────────────────────────
# 工具链探测
# ─────────────────────────────────────────────────────────────────────────────
function Get-NpmExe {
    # PowerShell 会把 `npm` 解析成 npm.ps1 shim（strict mode 下会炸），统一走 npm.cmd。
    foreach ($candidate in @('npm.cmd', 'npm.exe', 'npm')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { return $candidate }
    }
    return 'npm'
}

function Resolve-Python {
    $candidates = New-Object System.Collections.ArrayList
    if ($env:PYTHON) { [void]$candidates.Add(@{ Exe = $env:PYTHON; Pre = @() }) }
    [void]$candidates.Add(@{ Exe = 'python'; Pre = @() })
    [void]$candidates.Add(@{ Exe = 'py'; Pre = @('-3') })
    foreach ($c in $candidates) {
        $exe = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $out = & $c.Exe @($c.Pre + @('-c', 'import sys;print(sys.version_info[0]*100+sys.version_info[1])')) 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            return @{ Exe = $c.Exe; Pre = $c.Pre; Version = $out.ToString().Trim() }
        }
    }
    return $null
}

function Invoke-Py($Python, [string[]]$PyArgs, [string]$WorkDir, [switch]$Quiet) {
    Push-Location $WorkDir
    try {
        if ($Quiet) {
            & $Python.Exe @($Python.Pre + $PyArgs) *> $null
        }
        else {
            # 输出直接进控制台（Out-Host），不要混进返回值，否则调用方拿到的不是退出码
            & $Python.Exe @($Python.Pre + $PyArgs) | Out-Host
        }
        return $LASTEXITCODE
    }
    finally { Pop-Location }
}

function Join-Cmd([string]$Exe, [string[]]$Parts) {
    # 路径含空格才需要引号；cmd /c 对多余引号的处理很容易踩坑，所以按需加。
    $head = if ($Exe -match '\s') { "`"$Exe`"" } else { $Exe }
    $all = @($head) + $Parts
    return ($all -join ' ')
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

function Get-PortOwner([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($conn) {
            $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($p) { return "$($p.ProcessName) (PID $($p.Id))" }
            return "PID $($conn.OwningProcess)"
        }
    }
    catch { }
    $line = (& netstat -ano) | Select-String -Pattern "LISTENING" | Select-String -Pattern ":$Port\s" | Select-Object -First 1
    if ($line) { return $line.ToString().Trim() }
    return $null
}

function Stop-ProcessTree([int]$ProcessId) {
    if (-not $ProcessId) { return }
    & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
}

# ─────────────────────────────────────────────────────────────────────────────
# 状态文件
# ─────────────────────────────────────────────────────────────────────────────
function Read-State {
    if (-not (Test-Path $PidFile)) { return @() }
    try {
        $json = Get-Content -LiteralPath $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -eq $json) { return @() }
        return @($json)
    }
    catch { return @() }
}

function Write-State($Entries) {
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    $list = @($Entries)
    if ($list.Count -eq 0) {
        if (Test-Path $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
        return
    }
    $json = ConvertTo-Json -InputObject $list -Depth 4
    Set-Content -LiteralPath $PidFile -Value $json -Encoding UTF8
}

function Get-AliveState {
    $alive = @()
    foreach ($e in (Read-State)) {
        $proc = Get-Process -Id $e.pid -ErrorAction SilentlyContinue
        if ($proc) { $alive += $e }
    }
    return $alive
}

function Stop-AllRunning {
    $entries = @(Get-AliveState)
    if ($entries.Count -eq 0) {
        Write-Sys '没有本脚本启动的进程在运行。'
        Write-State @()
        return
    }
    # 先反向停，web/gateway 先走，api 后走
    $ordered = @($entries | Sort-Object { switch ($_.name) { 'web' { 1 } 'gateway' { 2 } 'worker' { 3 } 'api' { 4 } default { 5 } } })
    foreach ($e in $ordered) {
        Write-Sys ("停止 {0}（PID {1}）" -f $e.name, $e.pid)
        Stop-ProcessTree ([int]$e.pid)
    }
    Start-Sleep -Milliseconds 800
    Write-State @()
    Write-Sys '全部进程已停止。' 'ok'
}

# ─────────────────────────────────────────────────────────────────────────────
# 依赖 / 配置 / 初始化
# ─────────────────────────────────────────────────────────────────────────────
function Ensure-NodeDeps {
    $targets = @(
        @{ Name = 'web'; Dir = $WebDir; Marker = 'node_modules\next\package.json' },
        @{ Name = 'gateway'; Dir = $GatewayDir; Marker = 'node_modules\fastify\package.json' },
        @{ Name = 'cli'; Dir = $CliDir; Marker = 'node_modules\typescript\package.json' }
    )
    $missing = @()
    foreach ($t in $targets) {
        if (-not (Test-Path (Join-Path $t.Dir $t.Marker))) { $missing += $t }
    }
    if ($missing.Count -eq 0) { return }

    $names = ($missing | ForEach-Object { $_.Name }) -join ', '
    if ($NoInstall) {
        Write-Sys "缺少 node_modules：$names（-NoInstall 已指定，跳过安装，启动可能失败）" 'warn'
        return
    }
    Write-Sys "安装 Node 依赖：$names"
    $npm = Get-NpmExe
    foreach ($t in $missing) {
        Write-Sys "  npm install --include=dev（$($t.Name)）"
        Push-Location $t.Dir
        try {
            & $npm install --no-audit --no-fund --include=dev
            if ($LASTEXITCODE -ne 0) { throw "npm install 失败（$($t.Name)），退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }
    Write-Sys 'Node 依赖就绪。' 'ok'
}

function Ensure-PythonDeps($Python) {
    $code = 'import uvicorn, fastapi, pydantic_settings, sqlalchemy, alembic'
    $rc = Invoke-Py $Python @('-c', $code) $Root -Quiet
    if ($rc -eq 0) { return }
    if ($NoInstall) {
        Write-Sys 'Python 依赖缺失（-NoInstall 已指定，跳过安装）' 'warn'
        return
    }
    $req = Join-Path $ApiDir 'requirements-dev.txt'
    if (-not (Test-Path $req)) { throw "缺少 $req" }
    Write-Sys "安装 Python 依赖：pip install -r requirements-dev.txt"
    $rc = Invoke-Py $Python @('-m', 'pip', 'install', '-r', 'requirements-dev.txt') $ApiDir
    if ($rc -ne 0) { throw "pip install 失败，退出码 $rc" }
    Write-Sys 'Python 依赖就绪。' 'ok'
}

function Ensure-EnvFile {
    if (-not (Test-Path $EnvFile)) {
        Write-Sys '未找到 .env，生成开发用配置（含随机网关密钥）'
        $secret = New-RandomSecret 48
        $content = @(
            '# Skill Router 本地开发配置（由 start.ps1 首次生成；不要提交）',
            '# 生产部署请用 deploy\.env.example + deploy\docker-compose.yml',
            'SKILLROUTER_ENV=development',
            'SKILLROUTER_PUBLIC_BASE_URL=http://localhost:3000',
            'SKILLROUTER_DATA_DIR=var',
            'SKILLROUTER_DB_PATH=var/skillrouter.db',
            '# 留空 = 首次启动自动生成 var\credentials-master.key（0600）',
            'SKILLROUTER_MASTER_KEY=',
            'SKILLROUTER_MASTER_KEY_ID=mk1',
            '# 留空 = 首次启动自动生成 var\file-signing.key',
            'SKILLROUTER_FILE_SIGNING_SECRET=',
            '# 仅在 admin_initialized != 1 时生效；首次登录后必须改密',
            'SKILLROUTER_ADMIN_USER=admin',
            'SKILLROUTER_ADMIN_PASSWORD=adminjr',
            'SKILLROUTER_SESSION_TTL_MINUTES=480',
            'SKILLROUTER_AGENT_GATEWAY_URL=http://127.0.0.1:8500',
            'SKILLROUTER_AGENT_GATEWAY_BIND=127.0.0.1',
            "SKILLROUTER_AGENT_GATEWAY_SECRET=$secret",
            'SKILLROUTER_ENGINE_POLICY=notify',
            'SKILLROUTER_ENGINE_CHANNEL=stable',
            'SKILLROUTER_ENGINE_PIN=0.2.34@0a119a64890717b0e3017edbaaf00944babf19a8',
            'SKILLROUTER_EGRESS_POLICY=deny',
            'SKILLROUTER_PAYLOAD_POLICY=redacted_preview',
            '',
            '# [gateway] Node 边车（AGENT_* 命名空间，由本脚本注入）',
            'AGENT_GATEWAY_ENGINE_MODE=auto',
            ''
        )
        Set-Content -LiteralPath $EnvFile -Value $content -Encoding UTF8
        Write-Sys ".env 已生成：$EnvFile" 'ok'
    }
}

function Get-GatewaySecret {
    $map = Read-DotEnv $EnvFile
    $secret = ''
    if ($map.Contains('SKILLROUTER_AGENT_GATEWAY_SECRET')) { $secret = $map['SKILLROUTER_AGENT_GATEWAY_SECRET'] }
    if (Test-SecretPlaceholder $secret) {
        $secret = New-RandomSecret 48
        Set-DotEnvValue $EnvFile 'SKILLROUTER_AGENT_GATEWAY_SECRET' $secret
        Write-Sys '已生成 SKILLROUTER_AGENT_GATEWAY_SECRET 并写回 .env（≥32 字符）' 'ok'
    }
    return $secret
}

function Invoke-Bootstrap($Python) {
    if ($NoBootstrap) { Write-Sys '跳过 bootstrap（-NoBootstrap）' 'warn'; return $true }
    Write-Sys 'python scripts/bootstrap.py（迁移 + 管理员 + 目录）'
    $rc = Invoke-Py $Python @('-u', 'scripts\bootstrap.py') $Root
    if ($rc -ne 0) {
        Write-Sys "bootstrap 失败，退出码 $rc" 'err'
        return $false
    }
    return $true
}

# ─────────────────────────────────────────────────────────────────────────────
# 进程定义
# ─────────────────────────────────────────────────────────────────────────────
function Get-CurrentPin {
    $map = Read-DotEnv $EnvFile
    $pin = '0.2.34@0a119a64890717b0e3017edbaaf00944babf19a8'
    if ($map.Contains('SKILLROUTER_ENGINE_PIN') -and $map['SKILLROUTER_ENGINE_PIN']) { $pin = $map['SKILLROUTER_ENGINE_PIN'] }
    return $pin.Split('@')[0]
}

function New-ComponentSpecs($Python, [string]$GatewaySecret) {
    $pinVersion = Get-CurrentPin
    $specs = @()

    $uvicornArgs = @(
        '-u', '-m', 'uvicorn', 'skillrouter_api.main:app',
        '--host', '127.0.0.1', '--port', "$ApiPort",
        '--reload', '--reload-dir', 'apps/api',
        '--log-level', 'info'
    )
    $specs += [pscustomobject]@{
        Name    = 'api'
        Dir     = $Root
        Cmd     = (Join-Cmd $Python.Exe (@($Python.Pre) + $uvicornArgs))
        Health  = "http://127.0.0.1:$ApiPort/healthz"
        Timeout = 60
    }

    $workerArgs = @($Python.Pre) + @('-u', '-m', 'skillrouter_api.worker')
    $specs += [pscustomobject]@{
        Name    = 'worker'
        Dir     = $Root
        Cmd     = (Join-Cmd $Python.Exe $workerArgs)
        Health  = $null
        Timeout = 0
    }

    $specs += [pscustomobject]@{
        Name    = 'gateway'
        Dir     = $GatewayDir
        Cmd     = 'npm run dev'
        Health  = "http://127.0.0.1:$GatewayPort/healthz"
        Timeout = 40
    }

    $webCmd = if ($WebPort -eq 3000) { 'npm run dev' } else { "npx --yes next dev -p $WebPort" }
    $specs += [pscustomobject]@{
        Name    = 'web'
        Dir     = $WebDir
        Cmd     = $webCmd
        Health  = "http://127.0.0.1:$WebPort/login"
        Timeout = 180
    }

    return $specs
}

# ─────────────────────────────────────────────────────────────────────────────
# 全局环境注入（子进程继承）
# ─────────────────────────────────────────────────────────────────────────────
function Apply-ChildEnvironment([string]$GatewaySecret) {
    # 1) 先把 .env 的键放进进程环境，使网关（无 dotenv）也能拿到想要的 AGENT_* 配置
    $map = Read-DotEnv $EnvFile
    foreach ($key in $map.Keys) {
        $val = [string]$map[$key]
        if (-not $val) { continue }
        if ($val -match '^<.*>$') { continue }
        Set-Item -Path ("env:" + $key) -Value $val
    }

    # 2) 再用精确值覆盖：路径一律绝对，避免「从不同 cwd 启动 = 两套数据库」
    $overrides = [ordered]@{
        'SKILLROUTER_ENV'                      = $EnvName
        'SKILLROUTER_DATA_DIR'                 = $DataRoot
        'SKILLROUTER_DB_PATH'                  = $DbFile
        'SKILLROUTER_PUBLIC_BASE_URL'          = "http://localhost:$WebPort"
        'SKILLROUTER_AGENT_GATEWAY_URL'        = "http://127.0.0.1:$GatewayPort"
        'SKILLROUTER_AGENT_GATEWAY_SECRET'     = $GatewaySecret
        'SKILLROUTER_API_URL'                  = "http://127.0.0.1:$ApiPort"
        'AGENT_GATEWAY_SHARED_SECRET'          = $GatewaySecret
        'AGENT_GATEWAY_BIND'                   = '127.0.0.1'
        'AGENT_GATEWAY_PORT'                   = "$GatewayPort"
        'AGENT_ENGINE_HOME_ROOT'               = (Join-Path $DataRoot 'aragon-home\engines')
        'AGENT_ENGINE_RUNTIME_DIR'             = (Join-Path $DataRoot 'engine-runtime')
        'AGENT_POLICY_PATH'                    = (Join-Path $DataRoot 'agent-policy.json')
        'AGENT_SESSION_DIR'                    = (Join-Path $DataRoot 'agent-sessions')
        'SKILLROUTER_ENGINE_HOME_ROOT'         = (Join-Path $DataRoot 'aragon-home\engines')
        'SKILLROUTER_INTERNAL_URL'             = "http://127.0.0.1:$ApiPort"
        'ENGINE_PIN'                           = (Get-CurrentPin)
        'PYTHONUNBUFFERED'                     = '1'
        'PYTHONUTF8'                           = '1'
        'PYTHONIOENCODING'                     = 'utf-8'
        'PYTHONPATH'                           = $ApiDir
        'NODE_ENV'                             = 'development'
    }
    foreach ($key in $overrides.Keys) {
        Set-Item -Path ("env:" + $key) -Value ([string]$overrides[$key])
    }
    if (-not $env:AGENT_GATEWAY_ENGINE_MODE) { $env:AGENT_GATEWAY_ENGINE_MODE = 'auto' }
    if (-not $env:AGENT_GATEWAY_LOG_LEVEL) { $env:AGENT_GATEWAY_LOG_LEVEL = 'info' }

    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
}

# ─────────────────────────────────────────────────────────────────────────────
# 健康探测 / 进程管理
# ─────────────────────────────────────────────────────────────────────────────
function Test-Endpoint([string]$Url) {
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500)
    }
    catch {
        $r = $_.Exception.Response
        if ($r -and $r.StatusCode) {
            $code = [int]$r.StatusCode
            return ($code -lt 500)
        }
        return $false
    }
}

function Wait-Endpoint([string]$Url, [int]$TimeoutSeconds, [int]$ProcessId) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($ProcessId -and -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return 'dead' }
        if (Test-Endpoint $Url) { return 'ready' }
        Start-Sleep -Milliseconds 700
    }
    return 'timeout'
}

function Start-Component($Spec) {
    $outLog = Join-Path $LogDir ("{0}.out.log" -f $Spec.Name)
    $errLog = Join-Path $LogDir ("{0}.err.log" -f $Spec.Name)
    Set-Content -LiteralPath $outLog -Value '' -Encoding UTF8
    Set-Content -LiteralPath $errLog -Value '' -Encoding UTF8

    $line = "### {0} | cwd={1} | {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Spec.Dir, $Spec.Cmd
    Set-Content -LiteralPath $outLog -Value $line -Encoding UTF8

    $procArgs = @('/c', $Spec.Cmd)
    $proc = Start-Process -FilePath $env:ComSpec -ArgumentList $procArgs -WorkingDirectory $Spec.Dir `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
        -WindowStyle Hidden -PassThru

    Write-Host ("  ▸ {0,-8} pid={1,-7} {2}" -f $Spec.Name, $proc.Id, $Spec.Cmd) -ForegroundColor $Colors[$Spec.Name]
    return [pscustomobject]@{
        name      = $Spec.Name
        pid       = $proc.Id
        port      = switch ($Spec.Name) { 'api' { $ApiPort } 'gateway' { $GatewayPort } 'web' { $WebPort } default { 0 } }
        workdir   = $Spec.Dir
        cmd       = $Spec.Cmd
        outLog    = $outLog
        errLog    = $errLog
        startedAt = (Get-Date -Format 'o')
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# 日志跟随
# ─────────────────────────────────────────────────────────────────────────────
function Show-Logs($Entries, $ProcessMap) {
    $state = @{}
    $dead = @{}
    foreach ($e in $Entries) {
        $state[$e.outLog] = 0L
        $state[$e.errLog] = 0L
    }
    Write-Host ''
    Write-Host '───── 合并日志（Ctrl+C 停止全部进程）─────────────' -ForegroundColor DarkGray

    while ($true) {
        foreach ($e in $Entries) {
            foreach ($path in @($e.outLog, $e.errLog)) {
                if (-not (Test-Path $path)) { continue }
                $off = [long]$state[$path]
                $fs = $null
                try {
                    $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
                    if ($fs.Length -lt $off) { $off = 0L }
                    if ($fs.Length -gt $off) {
                        [void]$fs.Seek($off, [System.IO.SeekOrigin]::Begin)
                        $reader = New-Object System.IO.StreamReader($fs)
                        $text = $reader.ReadToEnd()
                        $state[$path] = $fs.Position
                        foreach ($l in ($text -split "`r?`n")) {
                            if ($l.Trim().Length -gt 0) {
                                Write-Host ("[{0}] {1}" -f $e.name, $l) -ForegroundColor $Colors[$e.name]
                            }
                        }
                    }
                }
                catch { }
                finally { if ($fs) { $fs.Dispose() } }
            }

            if (-not $dead.ContainsKey($e.name)) {
                $p = Get-Process -Id $e.pid -ErrorAction SilentlyContinue
                if (-not $p) {
                    $dead[$e.name] = $true
                    Write-Host ("[{0}] 进程已退出（pid {1}）" -f $e.name, $e.pid) -ForegroundColor Red
                }
            }
        }
        Start-Sleep -Milliseconds 600
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# status / check
# ─────────────────────────────────────────────────────────────────────────────
function Show-Status {
    Write-Host ''
    Write-Host '组件状态' -ForegroundColor DarkGray
    $alive = @(Get-AliveState)
    $byName = @{}
    foreach ($e in $alive) { $byName[$e.name] = $e }

    $rows = @(
        @{ Name = 'api'; Port = $ApiPort; Url = "http://127.0.0.1:$ApiPort/healthz" },
        @{ Name = 'worker'; Port = 0; Url = $null },
        @{ Name = 'gateway'; Port = $GatewayPort; Url = "http://127.0.0.1:$GatewayPort/healthz" },
        @{ Name = 'web'; Port = $WebPort; Url = "http://127.0.0.1:$WebPort/login" }
    )
    foreach ($r in $rows) {
        $procState = if ($byName.ContainsKey($r.Name)) { "运行中 pid=$($byName[$r.Name].pid)" } else { '未由本脚本启动' }
        $portState = if ($r.Port -gt 0) { if (Test-PortInUse $r.Port) { "端口 $($r.Port) 监听中" } else { "端口 $($r.Port) 未监听" } } else { '-' }
        $healthState = if ($r.Url) { if (Test-Endpoint $r.Url) { '健康' } else { '不可达' } } else { '-' }
        Write-Host ("  {0,-8} {1,-24} {2,-20} {3}" -f $r.Name, $procState, $portState, $healthState) -ForegroundColor $Colors[$r.Name]
    }
    Write-Host ''
    Write-Host ("  数据目录：$DataRoot") -ForegroundColor DarkGray
    Write-Host ("  日志目录：$LogDir") -ForegroundColor DarkGray
    Write-Host ''
}

function Invoke-Check {
    Write-Banner
    $ok = $true

    $py = Resolve-Python
    if ($py) {
        Write-Sys ("Python 可用：{0}（版本码 {1}）" -f $py.Exe, $py.Version) 'ok'
        $rc = Invoke-Py $py @('-c', 'import uvicorn, fastapi, pydantic_settings, sqlalchemy, alembic') $Root -Quiet
        if ($rc -eq 0) { Write-Sys 'Python 依赖齐全' 'ok' } else { Write-Sys 'Python 依赖缺失（会自动 pip install）' 'warn'; $ok = $false }
    }
    else {
        Write-Sys '找不到可用的 Python（需要 3.11）' 'err'
        $ok = $false
    }

    foreach ($n in @('node', 'npm')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if (-not $c) { $c = Get-Command "$n.cmd" -ErrorAction SilentlyContinue }
        if ($c) {
            $ver = if ($n -eq 'npm') { & (Get-NpmExe) --version } else { & node --version }
            Write-Sys ("{0} 可用：{1}" -f $n, $ver) 'ok'
        }
        else { Write-Sys "$n 不可用（需要 Node ≥ 22）" 'err'; $ok = $false }
    }

    $targets = @(
        @{ Name = 'web'; Dir = $WebDir; Marker = 'node_modules\next\package.json' },
        @{ Name = 'gateway'; Dir = $GatewayDir; Marker = 'node_modules\fastify\package.json' },
        @{ Name = 'cli'; Dir = $CliDir; Marker = 'node_modules\typescript\package.json' }
    )
    foreach ($t in $targets) {
        if (Test-Path (Join-Path $t.Dir $t.Marker)) { Write-Sys "node_modules 就绪：$($t.Name)" 'ok' }
        else { Write-Sys "node_modules 缺失：$($t.Name)" 'warn'; $ok = $false }
    }

    if (Test-Path $EnvFile) { Write-Sys ".env 存在：$EnvFile" 'ok' } else { Write-Sys '.env 不存在（启动时会生成）' 'warn'; $ok = $false }

    if (Test-Path $DbFile) { Write-Sys "数据库存在：$DbFile" 'ok' } else { Write-Sys "数据库不存在（启动时 bootstrap 会创建）：$DbFile" 'warn' }

    foreach ($p in @($ApiPort, $GatewayPort, $WebPort)) {
        if (Test-PortInUse $p) { Write-Sys "端口 $p 已被占用：$(Get-PortOwner $p)" 'warn'; $ok = $false }
        else { Write-Sys "端口 $p 空闲" 'ok' }
    }

    if (-not $ok) { Write-Sys '存在需要处理的问题（start 仍会尽力继续）。' 'warn' }
    else { Write-Sys '环境检查全部通过。' 'ok' }
    return $ok
}

# ─────────────────────────────────────────────────────────────────────────────
# stop / status / logs 分发
# ─────────────────────────────────────────────────────────────────────────────
switch ($Action) {
    'stop' { Write-Banner; Stop-AllRunning; exit 0 }
    'status' { Write-Banner; Show-Status; exit 0 }
    'check' { [void](Invoke-Check); exit 0 }
    'logs' {
        $entries = @(Get-AliveState)
        if ($entries.Count -eq 0) {
            Write-Sys '没有在跑的进程（先执行 .\start.ps1）' 'warn'
            exit 1
        }
        Write-Sys '仅跟随已有日志，Ctrl+C 退出（不会停止进程）'
        Show-Logs $entries @{}
        exit 0
    }
    'restart' {
        Write-Banner
        Write-Sys 'restart：先停止已有进程'
        Stop-AllRunning
        Start-Sleep -Seconds 1
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# start 主流程
# ─────────────────────────────────────────────────────────────────────────────
if (-not $script:BannerShown) { Write-Banner }

# 0) 先把上一次的残留停掉，避免端口冲突
$previous = @(Get-AliveState)
if ($previous.Count -gt 0) {
    Write-Sys '检测到上一次启动的进程，先停止它们'
    Stop-AllRunning
    Start-Sleep -Milliseconds 500
}

# 1) 环境检查
$python = Resolve-Python
if (-not $python) {
    Write-Sys '找不到可用的 Python 解释器。请安装 Python 3.11 或设置 $env:PYTHON。' 'err'
    exit 2
}
Write-Sys ("Python：{0}（版本码 {1}）" -f $python.Exe, $python.Version) 'ok'
foreach ($n in @('node', 'npm')) {
    $found = Get-Command $n -ErrorAction SilentlyContinue
    if (-not $found) { $found = Get-Command "$n.cmd" -ErrorAction SilentlyContinue }
    if (-not $found) {
        Write-Sys "找不到 $n，请安装 Node ≥ 22 并加入 PATH。" 'err'
        exit 2
    }
}
Write-Sys ("Node：{0}" -f (& node --version)) 'ok'

Ensure-NodeDeps
Ensure-PythonDeps $python
Ensure-EnvFile
$gatewaySecret = Get-GatewaySecret

# 2) 端口
$portOwnerFailed = $false
foreach ($p in @($ApiPort, $GatewayPort, $WebPort)) {
    if (Test-PortInUse $p) {
        $owner = Get-PortOwner $p
        if ($Force) {
            Write-Sys "端口 $p 被占用（$owner），-Force 指定：强杀" 'warn'
            $procId = $null
            try {
                $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop | Select-Object -First 1
                if ($conn) { $procId = [int]$conn.OwningProcess }
            }
            catch { }
            if ($procId) { Stop-ProcessTree $procId; Start-Sleep -Milliseconds 700 }
        }
        else {
            Write-Sys "端口 $p 已被占用：$owner" 'err'
            $portOwnerFailed = $true
        }
    }
}
if ($portOwnerFailed) {
    Write-Sys '换端口（-ApiPort/-GatewayPort/-WebPort）或加 -Force 强杀占用者后重试。' 'err'
    exit 3
}

# 3) 注入子进程环境 + 初始化
Apply-ChildEnvironment $gatewaySecret
if (-not (Invoke-Bootstrap $python)) { exit 4 }

# 4) 启动
$wanted = @($Components | ForEach-Object { $_.ToLower() })
$specs = New-ComponentSpecs $python $gatewaySecret
$started = @()
$failed = $false

Write-Sys ('启动组件：' + ($wanted -join ', '))
$order = @('gateway', 'api', 'worker', 'web')
foreach ($name in $order) {
    if ($wanted -notcontains $name) { continue }
    $spec = $specs | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    $entry = Start-Component $spec
    $started += $entry
    Write-State $started

    if ($spec.Health) {
        Write-Sys ("等待 {0} 就绪：{1}" -f $name, $spec.Health)
        $result = Wait-Endpoint $spec.Health $spec.Timeout ([int]$entry.pid)
        if ($result -eq 'ready') {
            Write-Sys ("{0} 就绪" -f $name) 'ok'
        }
        elseif ($result -eq 'dead') {
            Write-Sys ("{0} 启动后立即退出，日志：" -f $name) 'err'
            Write-Host (Format-Tail $entry.outLog 30) -ForegroundColor DarkYellow
            Write-Host (Format-Tail $entry.errLog 30) -ForegroundColor DarkYellow
            if ($name -eq 'api') { $failed = $true; break }
        }
        else {
            Write-Sys ("{0} 在 {1}s 内未就绪（继续，可能是首次编译）" -f $name, $spec.Timeout) 'warn'
            Write-Host (Format-Tail $entry.errLog 15) -ForegroundColor DarkYellow
            if ($name -eq 'api') { $failed = $true; break }
        }
    }
    else {
        Start-Sleep -Milliseconds 1200
        if (-not (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue)) {
            Write-Sys ("{0} 已退出，日志：" -f $name) 'err'
            Write-Host (Format-Tail $entry.outLog 20) -ForegroundColor DarkYellow
            Write-Host (Format-Tail $entry.errLog 20) -ForegroundColor DarkYellow
        }
        else { Write-Sys ("{0} 已启动" -f $name) 'ok' }
    }
}

if ($failed) {
    Write-Sys '关键组件启动失败，回收已启动的进程。' 'err'
    Stop-AllRunning
    exit 5
}

# 5) 汇总
Write-Host ''
Write-Host '──────────────────────────────────────────────────────────────' -ForegroundColor DarkGray
Write-Host ("  前端控制台   http://localhost:{0}/login      （默认 admin / adminjr，登录后强制改密）" -f $WebPort) -ForegroundColor Green
Write-Host ("  后端接口     http://127.0.0.1:{0}/healthz" -f $ApiPort) -ForegroundColor Cyan
Write-Host ("  接口文档     http://127.0.0.1:{0}/docs" -f $ApiPort) -ForegroundColor Cyan
Write-Host ("  Agent 网关   http://127.0.0.1:{0}/healthz" -f $GatewayPort) -ForegroundColor Magenta
Write-Host ("  日志         {0}（worker 的日志在 {1}）" -f $LogDir, (Join-Path $DataRoot 'logs')) -ForegroundColor DarkGray
Write-Host '──────────────────────────────────────────────────────────────' -ForegroundColor DarkGray
Write-Host '  停止：本窗口 Ctrl+C，或另开窗口执行 .\start.ps1 -Action stop' -ForegroundColor DarkGray
Write-Host '  状态：.\start.ps1 -Action status    查看进程/端口/健康' -ForegroundColor DarkGray
Write-Host ''

if (-not $NoBrowser) {
    try { Start-Process ("http://localhost:{0}/login" -f $WebPort) | Out-Null } catch { }
}

if ($NoFollow) {
    Write-Sys '已按 -NoFollow 保持后台运行并返回。' 'ok'
    exit 0
}

try {
    Show-Logs $started @{}
}
finally {
    Write-Host ''
    Write-Sys '收到停止信号，回收子进程…' 'warn'
    Stop-AllRunning
}
