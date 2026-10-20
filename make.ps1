# Skill Router —— Windows 任务入口（与 Makefile 等价，spec §14.1）
#
# 用法：
#   .\make.ps1                       # 列出全部目标
#   .\make.ps1 install               # 安装全部依赖
#   .\make.ps1 dev                   # 初始化 + 前台起 API
#   .\make.ps1 test | lint | typecheck | e2e
#   .\make.ps1 up | down | logs
#   .\make.ps1 backup
#   .\make.ps1 restore -File var\backup\skillrouter-20261018T102017Z.zip
#   .\make.ps1 seed
#   .\make.ps1 perf
#
# 说明：
#   * 仓库没有根级 package.json，每个前端包单独装依赖、单独跑测试。
#   * 本机若设置了 NODE_ENV=production，`npm install` 会**静默跳过** devDependencies
#     （typescript/vitest 缺失），因此这里一律加 --include=dev。
#   * 脚本只做「调用现成命令」这一件事，不重复实现业务逻辑。

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Task = 'help',

    # restore 用：备份包路径
    [string]$File = '',

    # perf 用：管理端 Cookie 与 CSRF
    [string]$Cookie = '',
    [string]$Csrf = '',
    [string]$Token = '',

    # seed 用：生产环境强制写入 MOCK 数据
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$PY = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
$COMPOSE = @('compose', '-f', 'deploy/docker-compose.yml')

function Write-Step([string]$Text) { Write-Host "[make] $Text" -ForegroundColor Cyan }
function Fail([string]$Text) { Write-Host "[make] $Text" -ForegroundColor Red; exit 1 }
function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$WorkDir = $Root) {
    Push-Location $WorkDir
    try {
        & $Exe @Arguments
        if ($LASTEXITCODE -ne 0) { Fail "$Exe $($Arguments -join ' ') 退出码 $LASTEXITCODE" }
    }
    finally { Pop-Location }
}

function Show-Help {
    @'
Skill Router 任务（.\make.ps1 <task>）：
  help          显示本帮助
  install       安装全部依赖（api / gateway / web / cli）
  install-api   仅 Python 依赖（apps/api/requirements-dev.txt）
  install-node  仅 Node 依赖（gateway / web / cli）
  dev           初始化（scripts/bootstrap.py）后前台启动 API
  bootstrap     创建 DB + 管理员 + 目录
  test          pytest + vitest（gateway + cli）
  test-api      仅 pytest
  test-node     仅 vitest
  lint          ruff + eslint
  typecheck     mypy + tsc --noEmit
  e2e           Playwright（chromium）
  desktop       桌面端一键出包（apps\desktop → release\SkillRouter-Setup-*.exe）
  up            docker compose up -d
  down          docker compose down（保留数据卷）
  logs          docker compose logs -f api worker gateway
  backup        备份（DB + var/files + manifest）
  restore       恢复：.\make.ps1 restore -File var\backup\xxx.zip
  seed          写入 MOCK 演示数据（生产环境拒绝；-Force 强制）
  perf          性能冒烟：.\make.ps1 perf -Cookie "sr_session=..."
  smoke         /healthz + /api/v1/health 最小冒烟
'@ | Write-Host
}

function Ensure-Requirements([string]$Path) {
    if (-not (Test-Path $Path)) {
        Fail "缺少 $Path（不属于本脚本维护范围：后端 owner 提供 requirements.txt / requirements-dev.txt）"
    }
}

switch ($Task.ToLower()) {
    'help' { Show-Help }

    'install' { & $MyInvocation.MyCommand.Path install-api; & $MyInvocation.MyCommand.Path install-node }

    'install-api' {
        Ensure-Requirements 'apps/api/requirements-dev.txt'
        Write-Step 'pip install -r requirements-dev.txt'
        Invoke-Native $PY @('-m', 'pip', 'install', '-r', 'requirements-dev.txt') (Join-Path $Root 'apps/api')
    }

    'install-node' {
        Write-Step 'npm install（gateway / web / cli，显式 --include=dev）'
        Invoke-Native 'npm' @('install', '--no-audit', '--no-fund', '--include=dev') (Join-Path $Root 'agents/aragon-gateway')
        Invoke-Native 'npm' @('install', '--no-audit', '--no-fund', '--include=dev') (Join-Path $Root 'apps/web')
        Invoke-Native 'npm' @('install', '--no-audit', '--no-fund', '--include=dev') (Join-Path $Root 'packages/skillrouter-cli')
    }

    'bootstrap' {
        Write-Step 'python scripts/bootstrap.py'
        Invoke-Native $PY @('scripts/bootstrap.py')
    }

    'dev' {
        & $MyInvocation.MyCommand.Path bootstrap
        Write-Step '其它进程请另开终端：'
        Write-Host '  cd apps/api; python -m skillrouter_api.worker'
        Write-Host '  cd agents/aragon-gateway; npm run dev   # 需要 AGENT_GATEWAY_SHARED_SECRET'
        Write-Host '  cd apps/web; npm run dev'
        Write-Step 'uvicorn --reload（仅开发环境）'
        Invoke-Native $PY @('-m', 'uvicorn', 'skillrouter_api.main:app', '--reload', '--port', '8000') (Join-Path $Root 'apps/api')
    }

    'test' { & $MyInvocation.MyCommand.Path test-api; & $MyInvocation.MyCommand.Path test-node }

    'test-api' {
        Write-Step 'pytest'
        Invoke-Native $PY @('-m', 'pytest', '-q') (Join-Path $Root 'apps/api')
    }

    'test-node' {
        Write-Step 'vitest（gateway）'
        Invoke-Native 'npm' @('test') (Join-Path $Root 'agents/aragon-gateway')
        Write-Step 'vitest（cli）'
        Invoke-Native 'npm' @('test') (Join-Path $Root 'packages/skillrouter-cli')
    }

    'lint' {
        Write-Step 'ruff'
        Invoke-Native $PY @('-m', 'ruff', 'check', 'apps/api', 'scripts')
        Write-Step 'eslint（web）'
        Invoke-Native 'npm' @('run', 'lint') (Join-Path $Root 'apps/web')
    }

    'typecheck' {
        Write-Step 'mypy（api）'
        Invoke-Native $PY @('-m', 'mypy', 'skillrouter_api') (Join-Path $Root 'apps/api')
        Write-Step 'tsc --noEmit（web / gateway / cli）'
        Invoke-Native 'npm' @('run', 'typecheck') (Join-Path $Root 'apps/web')
        Invoke-Native 'npm' @('run', 'typecheck') (Join-Path $Root 'agents/aragon-gateway')
        Invoke-Native 'npm' @('run', 'typecheck') (Join-Path $Root 'packages/skillrouter-cli')
    }

    'e2e' {
        Write-Step 'playwright test'
        Invoke-Native 'npx' @('playwright', 'test') (Join-Path $Root 'apps/web')
    }

    'desktop' {
        Write-Step '桌面端一键出包（build-desktop.ps1：venv → 依赖 → dist:win → verify）'
        Invoke-Native 'powershell' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Root 'build-desktop.ps1'))
    }

    'up' { Write-Step 'docker compose up -d'; Invoke-Native 'docker' ($COMPOSE + @('up', '-d')) }
    'down' { Write-Step 'docker compose down'; Invoke-Native 'docker' ($COMPOSE + @('down')) }
    'logs' { Invoke-Native 'docker' ($COMPOSE + @('logs', '-f', 'api', 'worker', 'gateway')) }

    'backup' {
        Write-Step 'scripts/backup.py（DB + var/files + manifest）'
        Invoke-Native $PY @('scripts/backup.py', '--out', 'var/backup', '--keep', '14')
    }

    'restore' {
        if (-not $File) { Fail '用法：.\make.ps1 restore -File var\backup\skillrouter-xxxx.zip' }
        Write-Step "scripts/restore.py $File"
        Invoke-Native $PY @('scripts/restore.py', $File)
    }

    'seed' {
        $seedArgs = @('scripts/seed_demo.py')
        if ($Force) { $seedArgs += '--force' }
        Invoke-Native $PY $seedArgs
    }

    'perf' {
        $perfArgs = @('scripts/perf_smoke.py', '--base-url', 'http://127.0.0.1:8000')
        if ($Cookie) { $perfArgs += @('--cookie', $Cookie) }
        if ($Csrf) { $perfArgs += @('--csrf', $Csrf) }
        if ($Token) { $perfArgs += @('--token', $Token) }
        if (-not $Cookie -and -not $Token) {
            Write-Host '[make] 提示：未提供 -Cookie/-Token，管理接口会返回 401。' -ForegroundColor Yellow
        }
        Invoke-Native $PY $perfArgs
    }

    'smoke' {
        Write-Step 'GET /healthz 与 /api/v1/health'
        Invoke-Native 'curl.exe' @('-fsS', 'http://127.0.0.1:8000/healthz')
        Invoke-Native 'curl.exe' @('-fsS', 'http://127.0.0.1:8000/api/v1/health')
    }

    default { Write-Host "[make] 未知目标：$Task" -ForegroundColor Red; Show-Help; exit 2 }
}
