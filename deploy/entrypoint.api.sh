#!/usr/bin/env bash
# Skill Router api/worker 入口脚本（spec §14.2）
#
#   entrypoint.api.sh api      # 启动前 quick 备份 → 迁移/初始化 → uvicorn
#   entrypoint.api.sh worker   # 等 DB 就绪 → python -m skillrouter_api.worker
#   entrypoint.api.sh run <cmd...>   # 一次性命令：不做备份/迁移，直接 exec 剩余参数
#                              # 例：docker compose run --rm --entrypoint /app/deploy/entrypoint.api.sh \
#                              #       api run python scripts/backup.py --list
#                              # 也可用环境变量 SKILLROUTER_ROLE=run 指定角色（§A.3.12 / F-22）。
#
# 行为与失败策略
# --------------
# * api：数据库已存在时先跑 `scripts/backup.py --quick`（§14.2「启动前自动备份」）。
#   备份失败即中止启动（避免「升级后才发现回滚点没有」），可用
#   `SKILLROUTER_SKIP_STARTUP_BACKUP=1` 临时跳过。
# * api：优先 `python scripts/bootstrap.py`（§7.1 的完整启动顺序：init_engine → 迁移 →
#   seed_system_state → 首次管理员初始化 → 清理过期会话 → ensure_dirs → 打印启动摘要）；
#   没有该文件时退回 `alembic upgrade head`；两者都不可用则按 §7.1 的 [FATAL] 约定退出。
# * worker：不重复跑迁移（api 的 healthcheck 已保证迁移完成），只等 DB 出现后启动。
# * 任何一步失败都打印 `[FATAL] <code> <hint>` 并以非 0 退出，不静默降级（§7.1）。

set -euo pipefail

DATA_DIR="${SKILLROUTER_DATA_DIR:-/data}"
DB_PATH="${SKILLROUTER_DB_PATH:-${DATA_DIR}/skillrouter.db}"
APP_DIR="${APP_DIR:-/app}"
PORT="${SKILLROUTER_API_PORT:-8000}"

log() { printf '[entrypoint] %s\n' "$*"; }
fatal() { printf '[FATAL] %s %s\n' "$1" "${2:-}" >&2; exit 2; }

# ── 角色解析（§A.3.12 / F-22）───────────────────────────────────────────────
# 两种写法都支持，且优先级固定：
#   ① `SKILLROUTER_ROLE=<角色>`：**环境变量优先**，此时所有位置参数都是要执行的命令/参数，
#      不做 shift（否则 `-e SKILLROUTER_ROLE=run … api python x.py` 会把 `python` 当角色名）；
#   ② `entrypoint.api.sh <角色> [参数…]`：首个位置参数是角色，其余是命令/参数。
ROLE_FROM_ENV=0
if [ -n "${SKILLROUTER_ROLE:-}" ]; then
  ROLE="${SKILLROUTER_ROLE}"
  ROLE_FROM_ENV=1
else
  ROLE="${1:-api}"
  shift || true
fi

mkdir -p "${DATA_DIR}" "${DATA_DIR}/logs" "${DATA_DIR}/files" "${DATA_DIR}/backup" \
         "${DATA_DIR}/engine-builds" "${DATA_DIR}/aragon-home" "${DATA_DIR}/engine-runtime" \
         "${DATA_DIR}/downloads" 2>/dev/null || fatal "DATA_DIR_NOT_WRITABLE" "无法创建 ${DATA_DIR}（检查卷权限与 uid 1000）"

log "role=${ROLE} data_dir=${DATA_DIR} db=${DB_PATH}"

# ── 1. 启动前快速备份（仅 api，且 DB 已存在）─────────────────────────────────
do_backup() {
  if [ "${SKILLROUTER_SKIP_STARTUP_BACKUP:-0}" = "1" ]; then
    log "SKILLROUTER_SKIP_STARTUP_BACKUP=1：跳过启动前备份（不建议）"
    return 0
  fi
  if [ ! -f "${DB_PATH}" ]; then
    log "数据库尚未创建（首次启动）：跳过启动前备份"
    return 0
  fi
  if [ ! -f "${APP_DIR}/scripts/backup.py" ]; then
    log "[WARN] 未找到 ${APP_DIR}/scripts/backup.py：跳过启动前备份"
    return 0
  fi
  log "启动前 quick 备份（DB + manifest，纯标准库）…"
  if ! python "${APP_DIR}/scripts/backup.py" --quick --keep 14 --out "${DATA_DIR}/backup"; then
    fatal "STARTUP_BACKUP_FAILED" "启动前备份失败；确认磁盘空间，或临时设置 SKILLROUTER_SKIP_STARTUP_BACKUP=1"
  fi
}

# ── 2. 迁移与初始化 ────────────────────────────────────────────────────────
do_migrate() {
  cd "${APP_DIR}" || fatal "APP_DIR_MISSING" "${APP_DIR} 不存在"
  if [ -f "${APP_DIR}/scripts/bootstrap.py" ]; then
    log "运行 scripts/bootstrap.py（迁移 + 种子 + 首次管理员初始化）…"
    python "${APP_DIR}/scripts/bootstrap.py" || fatal "BOOTSTRAP_FAILED" "见上面的 [FATAL] 行"
    return 0
  fi
  local ini=""
  for candidate in "migrations/alembic.ini" "alembic.ini"; do
    if [ -f "${candidate}" ]; then ini="${candidate}"; break; fi
  done
  if [ -n "${ini}" ]; then
    log "[WARN] 未找到 scripts/bootstrap.py，退回 ${ini} 迁移（种子与管理员初始化将缺失）"
    python -m alembic -c "${ini}" upgrade head || fatal "MIGRATION_FAILED" "alembic upgrade head 失败"
    return 0
  fi
  fatal "BOOTSTRAP_MISSING" "缺少 ${APP_DIR}/scripts/bootstrap.py 且找不到 alembic.ini；按 §7.1 无法安全启动"
}

# ── 3. worker：等 DB 就绪 ──────────────────────────────────────────────────
wait_for_db() {
  if [ -f "${DB_PATH}" ]; then
    return 0
  fi
  log "等待迁移完成（DB 出现）：${DB_PATH}"
  for _ in $(seq 1 60); do
    if [ -f "${DB_PATH}" ]; then
      log "DB 已就绪"
      return 0
    fi
    sleep 2
  done
  fatal "DB_NOT_READY" "120s 内未等到 ${DB_PATH}；请检查 api 容器日志"
}

cd "${APP_DIR}" || fatal "APP_DIR_MISSING" "${APP_DIR} 不存在"

case "${ROLE}" in
  # ── run：一次性命令透传（必须在 api|worker 之前判断，§A.3.12）──────────────
  # 为什么不复用 `docker compose run --rm api python …`：镜像 ENTRYPOINT 是本脚本，
  # `python` 会被当作角色名 → `[FATAL] UNKNOWN_ROLE`。`run` 角色把剩余参数原样 exec。
  # 两种入口到这里时 `"$@"` 都已经是「要执行的命令」：位置参数写法已 shift 掉 `run`；
  # 环境变量写法本来就没有吃掉位置参数。
  run)
    if [ "$#" -eq 0 ]; then
      fatal "RUN_NO_COMMAND" "用法：entrypoint.api.sh run <命令>（例：run python scripts/backup.py --list）"
    fi
    log "run 角色（role_from_env=${ROLE_FROM_ENV}）：exec $*"
    exec "$@"
    ;;
  api)
    do_backup
    do_migrate
    # production 下禁止 --reload（§7.1）；单 worker 进程 + 写序列化器（§7.2）
    log "启动 uvicorn（0.0.0.0:${PORT}，无 --reload）…"
    exec uvicorn skillrouter_api.main:app \
      --host 0.0.0.0 --port "${PORT}" \
      --proxy-headers --forwarded-allow-ips='*' \
      --no-server-header \
      "$@"
    ;;
  worker)
    wait_for_db
    log "启动队列 worker（python -m skillrouter_api.worker）…"
    exec python -m skillrouter_api.worker "$@"
    ;;
  *)
    fatal "UNKNOWN_ROLE" "用法：entrypoint.api.sh api|worker|run（收到 '${ROLE}'）"
    ;;
esac
