# 备份与恢复（runbook）

> 依据：spec §7.4（加密与主密钥、备份方案）、§10.3（迁移与备份）、§14.2/§14.3、§15.3（备份恢复验收）。
> 实现：`scripts/backup.py` / `scripts/restore.py` —— **纯标准库**（`sqlite3.Connection.backup()`
> + `zipfile` + `manifest.json`），不依赖 `sqlite3` CLI、不依赖 `tar`，Windows 与
> `python:3.11-slim` 镜像内都可直接运行（R09 修正项）。

## 0. 一分钟版

```bash
# 备份（全量：DB + var/files + manifest）
python scripts/backup.py --out var/backup --keep 14

# 恢复（先干跑校验，再真恢复）
python scripts/restore.py var/backup/skillrouter-20261018T102017Z.zip --dry-run
python scripts/restore.py var/backup/skillrouter-20261018T102017Z.zip --yes
```

容器内：

```bash
docker compose -f deploy/docker-compose.yml exec api python scripts/backup.py --out /data/backup --keep 14
docker compose -f deploy/docker-compose.yml stop api worker
docker compose -f deploy/docker-compose.yml run --rm --no-deps api \
  python scripts/restore.py /data/backup/skillrouter-20261018T102017Z.zip --yes
docker compose -f deploy/docker-compose.yml start api worker
```

## 1. 备份策略

| 项 | 值 | 说明 |
|---|---|---|
| 频率 | 每日一次全量 + 每次启动前 `--quick` | 启动前 quick 备份由 `deploy/entrypoint.api.sh` 自动执行 |
| 保留 | 最近 14 份（`--keep 14`） | 更久的历史请转异地冷存 |
| 位置 | `<data-dir>/backup/` → 同步到异地 | 同机备份不算备份 |
| 一致性 | SQLite 热备 API | WAL 模式下安全，不需要停服 |
| 密钥 | **只导出指纹** | `sha256(key_material)[:16]`，永不导出密钥本体 |

两种模式：

| 模式 | 命令 | 内容 | 耗时 | 用途 |
|---|---|---|---|---|
| full | `python scripts/backup.py` | DB + `var/files/**` + manifest | 与文件总量成正比 | 每日备份、恢复演练 |
| quick | `python scripts/backup.py --quick` | 仅 DB + manifest | 秒级 | 启动前、升级切换前 |

额外目录可以附加打包（例如引擎切换前的 home 目录）：

```bash
python scripts/backup.py --label pre-upgrade --include var/aragon-home --include var/engine-runtime
```

附加目录在包内的路径是 `data/extra/<目录名>/…`，恢复时回到 `<data-dir>/<目录名>/`。

## 2. 调度

Linux cron（与 §14.3 一致）：

```cron
# 每天 03:20 全量备份，保留 14 份
20 3 * * * cd /srv/skillrouter && python scripts/backup.py --out /srv/skillrouter/var/backup --keep 14 >> /srv/skillrouter/var/logs/backup.log 2>&1
# 每天 04:00 同步到异地
0 4 * * * rclone sync /srv/skillrouter/var/backup remote:skillrouter-backup >> /srv/skillrouter/var/logs/backup-sync.log 2>&1
```

Windows 计划任务：

```bat
schtasks /create /tn "SkillRouter backup" /sc daily /st 03:20 ^
  /tr "cmd /c cd /d M:\takoAI\AragonSkillRouter && python scripts\backup.py --out var\backup --keep 14 >> var\logs\backup.log 2>&1"
```

（容器部署时更简单：`docker compose exec -T api python scripts/backup.py --keep 14`。）

## 3. 产物与 `manifest.json`

```
var/backup/skillrouter-20261018T102017Z[-label].zip            备份包（内嵌 manifest.json）
var/backup/skillrouter-20261018T102017Z[-label].zip.manifest.json   manifest 副本（不解压即可核对）
var/backup/skillrouter-20261018T102017Z[-label].zip.sha256          包 sha256（可直接 sha256sum -c）
```

manifest 结构（字段即恢复时的校验依据）：

```json
{
  "format": "skillrouter-backup/1",
  "mode": "full",
  "created_at": "2026-10-18T10:20:17Z",
  "app": {"name": "Skill Router", "version": "0.1.0", "engine_pin": "0.2.34@0a119a6..."},
  "schema_version": "1",
  "migration_revision": "0002_engine_upgrade",
  "master_key": {
    "fingerprint": "37b2577bbe2108d0",
    "algorithm": "sha256(key_material)[:16]",
    "source": "file:.../credentials-master.key",
    "key_exported": false
  },
  "db": {
    "path": "db/skillrouter.db",
    "sha256": "937cc57f…",
    "size": 36864,
    "tables": ["alembic_version", "capabilities", "credentials", "…"],
    "row_counts": {"capabilities": 12, "credentials": 4, "skills": 3, "call_logs": 1843}
  },
  "files": {"included": true, "count": 128, "bytes": 44172800, "sha256": "a2fc1c2e…"},
  "extra": {"count": 0, "bytes": 0},
  "entries": [
    {"path": "db/skillrouter.db", "sha256": "937cc57f…", "size": 36864},
    {"path": "data/files/ab/abcdef…", "sha256": "…", "size": 184320}
  ],
  "entry_count": 129
}
```

要点：

- `entries[].sha256` 是**逐文件**校验值，`restore.py` 会先全量校验再落盘；
- `row_counts` 用于恢复后的 `verify_restore()` 行数比对；
- `master_key.fingerprint` 用来拒绝「主密钥不匹配」的恢复；
- quick 模式 `files.included=false`，**不含** `var/files`（恢复这类包不会改动文件目录）。

## 4. 恢复流程

前置：**停止 api/worker**（避免恢复过程中写入），保留 gateway/web 无所谓但建议一起停。

```bash
# 1) 停写
docker compose -f deploy/docker-compose.yml stop api worker

# 2) 干跑：只校验，不写盘（会打印将要做的事）
python scripts/restore.py /data/backup/skillrouter-20261018T102017Z.zip --dry-run

# 3) 真恢复
python scripts/restore.py /data/backup/skillrouter-20261018T102017Z.zip --yes

# 4) 起服务并抽查
docker compose -f deploy/docker-compose.yml start api worker
curl -sS http://127.0.0.1:8000/healthz
```

`restore.py` 的顺序（顺序本身是安全属性，不可调换）：

1. 读包内 `manifest.json`，校验 `format`；
2. 校验**主密钥指纹**（不一致立即拒绝，退出码 3）；
3. 校验**备份 schema 不比当前程序新**（不认识且未加 `--allow-unknown-schema` → 退出码 4）；
4. 逐条校验 `entries[].sha256`（失败 → 退出码 5，**一个字节都不写**）；
5. 把当前 DB 另存为 `var/backup/pre-restore-<时间戳>.db`（`--no-keep-current` 可关）；
6. 原子替换 DB（先写 `.restoring` 再 `os.replace`，并清掉 `-wal`/`-shm`），恢复 `data/files/**`
   与 `data/extra/**`；
7. 若可用则 `alembic upgrade head`（允许从更旧 schema 升级）；
8. `verify_restore()`：与 manifest 的 `row_counts` 逐表比对并打印；
9. 自检通过才删除 `<data-dir>/.restore-in-progress` 维护标记。

常用开关：

| 开关 | 作用 |
|---|---|
| `--dry-run` | 只校验与打印计划 |
| `--yes` | 非交互确认（容器内必须给） |
| `--fresh-files` | 恢复前清空 `var/files`（默认覆盖同名、保留多余文件） |
| `--no-keep-current` | 不另存当前 DB（磁盘紧张时用，慎用） |
| `--no-migrate` | 跳过 `alembic upgrade head` |
| `--allow-unknown-schema` | 允许恢复本地不认识的 revision（默认拒绝：禁止用新库跑旧代码） |
| `--no-marker` | 不写维护标记 |

## 5. 拒绝场景与处置（都是设计好的失败）

| 输出 | 退出码 | 含义 | 处置 |
|---|---|---|---|
| `主密钥与备份不匹配，已拒绝恢复` | 3 | 备份时的主密钥 ≠ 当前主密钥 | 把备份所用主密钥恢复到本机（或从 `SKILLROUTER_MASTER_KEY_PREVIOUS` 取回）再重试 |
| `当前环境没有主密钥` | 3 | `SKILLROUTER_MASTER_KEY` 与 `credentials-master.key` 都缺失 | 先补密钥；否则恢复后的凭证永远解不开 |
| `备份 revision … 不在本地 revision 集合中` | 4 | 备份来自更新版本的程序 | 先升级程序到对应版本；确认无误才加 `--allow-unknown-schema` |
| `备份包完整性校验失败` | 5 | 包损坏 / 传输截断 | 换一份备份或重新同步；sha256 侧车文件可先校验 |
| `自检未通过：表 … 行数不一致` | 1 | 恢复后被写入或包本身不一致 | 保持维护标记、不要对外服务；用 `pre-restore-*.db` 回到恢复前 |

## 6. 主密钥相关

- **主密钥不随备份走**。备份只带指纹，所以「换机恢复」必须同时把主密钥带过去。
- 轮换期间（`SKILLROUTER_MASTER_KEY_PREVIOUS` 存在）恢复能通过指纹校验的前提是：备份使用的
  旧密钥仍在当前环境的 master 或 previous 中。
- 恢复后如需轮换，按 [init.md](init.md) §7 正常走一遍即可（幂等、可重试）。

## 7. 演练与记录（每次发布前至少一次）

恢复演练不是为了「跑通一次」，而是为了**知道恢复要多久、会丢多少数据**。把结果填在这里：

| 日期 | 环境 | 备份文件 | 大小 | 备份耗时 | 恢复耗时 | `verify_restore()` | 抽查（凭证解密/能力调用） | 执行人 |
|---|---|---|---|---|---|---|---|---|
| 待填 | — | — | — | — | — | 待验证 | 待验证 | — |

演练步骤（照抄 §4，用一份**刚从生产备份出来**的包，恢复到 staging）：

```bash
python scripts/restore.py var/backup/<包> --dry-run
python scripts/restore.py var/backup/<包> --yes
# 登录 → 打开 /calls → 打开一个能力 → 用外部 Agent 调一次（真实调用要记账）
```

## 8. 已知限制

- quick 备份不含文件目录；只恢复 quick 包时 DB 与文件可能不同步（能力记录指向缺失文件）。
  正式备份请用 full。
- 备份是**逻辑一致快照**（DB）+ 文件拷贝，不保证文件与 DB 的严格时间点一致；
  恢复后若出现「引用缺失文件」，用 `retention_sweep` 报告或人工核对 `file_objects`。
- 备份包本身**未加密**，内含调用记录与（加密后的）凭证密文。异地存储请用加密桶/加密卷，
  并按数据分级设置访问控制。
- `restore.py` 的「凭证能否解密」抽查**未内置**：AAD 约定属于后端实现细节（§7.4），
  由人工按 §7 表格抽查（1 个凭证 + 1 个能力），结果记入
  [verification-checklist.md](verification-checklist.md)。
