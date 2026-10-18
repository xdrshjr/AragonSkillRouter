# Skill Router —— 常用任务入口（spec §14.1 / §17.2）
#
#   make install     安装全部依赖（api / gateway / web / cli）
#   make dev         初始化 + 起 API（前台；其它进程见 `make help`）
#   make test        pytest + vitest（gateway + cli）
#   make lint        ruff + eslint + prettier 检查
#   make typecheck   mypy（api）+ tsc --noEmit（web/gateway/cli）
#   make e2e         Playwright（需先 `make up` 或本地三进程就绪）
#   make up/down     docker compose 启停（deploy/docker-compose.yml）
#   make backup      备份（DB + var/files + manifest）
#   make restore    恢复：make restore FILE=var/backup/xxx.zip
#   make seed        写入 MOCK 演示数据（生产环境会拒绝）
#   make perf        性能冒烟（管理接口 P95 + 异步提交）
#
# Windows 用户：装了 GNU Make 就直接用；没装请用 `make.ps1`（等价目标，见 README）。
# 说明：仓库里没有根级 package.json / workspace，所以每个前端包单独装依赖、单独跑测试。

SHELL := /bin/bash
PY ?= python
PIP ?= $(PY) -m pip
NPM ?= npm
DATA_DIR ?= var
COMPOSE ?= docker compose -f deploy/docker-compose.yml
API_DIR := apps/api
GATEWAY_DIR := agents/aragon-gateway
WEB_DIR := apps/web
CLI_DIR := packages/skillrouter-cli

.DEFAULT_GOAL := help
.PHONY: help install install-api install-node dev bootstrap up down logs test test-api test-node \
        lint lint-py lint-node typecheck typecheck-py typecheck-node e2e backup restore seed perf \
        smoke clean-backups

help: ## 列出全部目标
	@echo "Skill Router 任务："
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ── 依赖 ───────────────────────────────────────────────────────────────────
install: install-api install-node ## 安装全部依赖

install-api: ## 安装 Python 依赖（含 dev）
	@test -f $(API_DIR)/requirements-dev.txt || (echo "[make] 缺少 $(API_DIR)/requirements-dev.txt（后端 owner 维护）"; exit 1)
	cd $(API_DIR) && $(PIP) install -r requirements-dev.txt

install-node: ## 安装 Node 依赖（gateway / web / cli）
	# NODE_ENV=production 会让 npm 跳过 devDependencies（本机实测踩过），故显式 --include=dev
	cd $(GATEWAY_DIR) && $(NPM) install --no-audit --no-fund --include=dev
	cd $(WEB_DIR) && $(NPM) install --no-audit --no-fund --include=dev
	cd $(CLI_DIR) && $(NPM) install --no-audit --no-fund --include=dev

# ── 本地开发（§14.1）───────────────────────────────────────────────────────
bootstrap: ## 创建 DB + 管理员 + 目录
	$(PY) scripts/bootstrap.py

dev: bootstrap ## 初始化后前台启动 API（另开终端起 worker/gateway/web）
	@echo "[make] 其它进程："
	@echo "  cd $(API_DIR) && python -m skillrouter_api.worker"
	@echo "  cd $(GATEWAY_DIR) && npm run dev      # 需要 AGENT_GATEWAY_SHARED_SECRET"
	@echo "  cd $(WEB_DIR) && npm run dev"
	cd $(API_DIR) && $(PY) -m uvicorn skillrouter_api.main:app --reload --port 8000

# ── 测试 / 静态检查 ────────────────────────────────────────────────────────
test: test-api test-node ## 全部测试

test-api: ## pytest（DB 用临时文件，覆盖 WAL/锁）
	cd $(API_DIR) && $(PY) -m pytest -q

test-node: ## vitest（gateway + cli）
	cd $(GATEWAY_DIR) && $(NPM) test
	cd $(CLI_DIR) && $(NPM) test

lint: lint-py lint-node ## 全部静态检查

lint-py: ## ruff
	$(PY) -m ruff check apps/api scripts
	$(PY) -m ruff format --check apps/api scripts

lint-node: ## eslint（web）+ prettier 检查（gateway/cli 仅类型与测试）
	cd $(WEB_DIR) && $(NPM) run lint
	cd $(GATEWAY_DIR) && npx tsc --noEmit
	cd $(CLI_DIR) && npx tsc -p tsconfig.json --noEmit

typecheck: typecheck-py typecheck-node ## 全部类型检查

typecheck-py: ## mypy（services/executors/security 严格模式）
	cd $(API_DIR) && $(PY) -m mypy skillrouter_api

typecheck-node: ## tsc --noEmit
	cd $(WEB_DIR) && $(NPM) run typecheck
	cd $(GATEWAY_DIR) && $(NPM) run typecheck
	cd $(CLI_DIR) && $(NPM) run typecheck

e2e: ## Playwright（chromium）
	cd $(WEB_DIR) && npx playwright test

# ── 容器 ───────────────────────────────────────────────────────────────────
up: ## docker compose up -d（首次会构建镜像）
	$(COMPOSE) up -d
	@echo "[make] 控制台：https://localhost （证书放 deploy/certs/）"

down: ## docker compose down（保留数据卷）
	$(COMPOSE) down

logs: ## 跟踪 api/worker/gateway 日志
	$(COMPOSE) logs -f api worker gateway

# ── 运维脚本 ───────────────────────────────────────────────────────────────
backup: ## 备份（DB + var/files + manifest.json）
	$(PY) scripts/backup.py --out $(DATA_DIR)/backup --keep 14

restore: ## 恢复：make restore FILE=var/backup/skillrouter-...zip
	@test -n "$(FILE)" || (echo "[make] 用法：make restore FILE=var/backup/skillrouter-xxxx.zip"; exit 2)
	$(PY) scripts/restore.py $(FILE)

seed: ## 写入 MOCK 演示数据（SKILLROUTER_ENV=production 会拒绝）
	$(PY) scripts/seed_demo.py

perf: ## 性能冒烟（需要 --cookie；见 scripts/perf_smoke.py --help）
	$(PY) scripts/perf_smoke.py --base-url http://127.0.0.1:8000

smoke: ## 最小冒烟：/healthz + /api/v1/health
	curl -fsS http://127.0.0.1:8000/healthz
	curl -fsS http://127.0.0.1:8000/api/v1/health

clean-backups: ## 删除全部本地备份产物（危险）
	@echo "[make] 将删除 $(DATA_DIR)/backup/*（Ctrl-C 取消，5s 后继续）"; sleep 5
	rm -f $(DATA_DIR)/backup/*
