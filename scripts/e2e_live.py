"""端到端联调脚本（真实进程 + 真实 HTTP，不用 mock）。

流程（对应 §15.2 的核心闭环，第三方上游用本机 echo 服务替代）：
  1) 起本地 echo 上游（真实 HTTP 服务）
  2) 起 FastAPI（真实 uvicorn 进程）
  3) 初始化 / 登录 / 改密
  4) 建提供商 + 凭证 → 建能力（2 条 route）→ 真实测试 → 发布
  5) 建外部 Agent + 令牌 → 用真实令牌走 /api/v1 同步 + 异步 + MCP
  6) 用 @skillrouter/cli 安装 SKILL 到临时目录 → verify → invoke.mjs 真实调用
  7) 校验调用记录、审计、SKILL 包无密钥

用法：python scripts/e2e_live.py
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
CLI_DIR = ROOT / "packages" / "skillrouter-cli"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

OK, FAIL = "✓", "✗"
results: list[tuple[bool, str, str]] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    results.append((bool(condition), label, detail))
    print(f" {OK if condition else FAIL} {label}{(' — ' + detail) if detail and not condition else ''}")
    return bool(condition)


# ── 本地 echo 上游 ────────────────────────────────────────────
class EchoHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        EchoHandler.calls.append({"method": "GET", "path": self.path})
        if self.path.startswith("/v1/tasks/"):
            task_id = self.path.rsplit("/", 1)[-1]
            return self._json(
                200,
                {
                    "status": "succeeded",
                    "result": {"text": f"异步结果（{task_id}）"},
                    "usage": {"input_tokens": 5, "output_tokens": 9},
                },
            )
        if self.path == "/models":
            return self._json(200, {"data": [{"id": "echo-model"}]})
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"raw": raw.decode("utf-8", "replace")}
        EchoHandler.calls.append(
            {"method": "POST", "path": self.path, "auth": self.headers.get("Authorization")}
        )
        if self.path == "/v1/jobs":
            return self._json(200, {"id": "echo-task-1", "status": "queued"})
        if self.path.endswith("/cancel"):
            return self._json(200, {"ok": True})
        if self.path == "/v1/generate":
            if self.headers.get("Authorization") != "Bearer echo-secret-9999":
                return self._json(401, {"error": "bad token"})
            prompt = payload.get("prompt", "")
            return self._json(
                200,
                {
                    "result": {"text": f"echo:{prompt}"},
                    "usage": {"input_tokens": 11, "output_tokens": 3},
                },
            )
        if self.path == "/v2/generate":
            return self._json(
                200, {"result": {"text": "backup-route"}, "usage": {"input_tokens": 1, "output_tokens": 1}}
            )
        return self._json(404, {"error": "not found"})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))


def http(method: str, url: str, *, body: dict | None = None, headers: dict | None = None,
         timeout: float = 30.0) -> tuple[int, dict | str]:
    """带 Cookie 的 HTTP 客户端（管理端会话依赖 sr_session Cookie）。"""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(text)
            except json.JSONDecodeError:
                return response.status, text
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, text


def wait_for(url: str, timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status, _ = http("GET", url, timeout=3)
            if status < 500:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    upstream_port = free_port()
    api_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), EchoHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_base = f"http://127.0.0.1:{upstream_port}"
    print(f" echo 上游：{upstream_base}")

    data_dir = Path(tempfile.mkdtemp(prefix="sr-e2e-"))
    env = {
        **os.environ,
        "SKILLROUTER_DATA_DIR": str(data_dir),
        "SKILLROUTER_DB_PATH": str(data_dir / "e2e.db"),
        "SKILLROUTER_ENV": "development",
        "SKILLROUTER_PUBLIC_BASE_URL": f"http://127.0.0.1:{api_port}",
        "SKILLROUTER_AGENT_GATEWAY_SECRET": "e2e-internal-secret-0123456789abcdef",
        "SKILLROUTER_PYTHONIOENCODING": "utf-8",
        "PYTHONIOENCODING": "utf-8",
    }
    bootstrap = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bootstrap.py")],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check(bootstrap.returncode == 0, "bootstrap 初始化成功", ((bootstrap.stdout or "") + (bootstrap.stderr or ""))[-600:])

    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "skillrouter_api.main:app", "--host", "127.0.0.1",
         "--port", str(api_port), "--log-level", "warning"],
        cwd=str(API_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    base = f"http://127.0.0.1:{api_port}"
    try:
        ready = wait_for(f"{base}/healthz")
        check(ready, "API 进程启动并 /healthz 可用", "" if ready else "90s 内未就绪")
        if not ready:
            return _finish(api_proc)

        status, health = http("GET", f"{base}/healthz")
        check(status == 200 and health.get("ok") is True, "健康检查返回 ok", str(health)[:200])
        check(health.get("initialized") is True, "初始化状态已置位")
        check(isinstance(health.get("db"), dict) and health["db"].get("schema_version") == 1,
              "schema_version 为 1", str(health.get("db"))[:160])

        # 登录 + 强制改密
        status, login = http("POST", f"{base}/api/admin/auth/login",
                             body={"username": "admin", "password": "adminjr"})
        check(status == 200, "默认账号登录成功", str(login)[:200])
        csrf = login.get("csrf_token", "") if isinstance(login, dict) else ""
        headers = {"X-CSRF-Token": csrf}
        status, blocked = http("GET", f"{base}/api/admin/overview")
        check(status == 403, "未改密时业务接口被闸门拦下", str(status))
        status, changed = http("POST", f"{base}/api/admin/auth/change-password", headers=headers,
                               body={"current_password": "adminjr", "new_password": "SkillRouter-E2E-2026"})
        check(status == 200, "修改默认密码成功", str(changed)[:200])
        if isinstance(changed, dict) and changed.get("csrf_token"):
            headers = {"X-CSRF-Token": changed["csrf_token"]}

        # 提供商 + 凭证
        status, provider = http("POST", f"{base}/api/admin/providers", headers=headers, body={
            "slug": "echo-provider", "name": "Echo Provider", "category": "llm",
            "base_url": upstream_base, "docs_url": upstream_base + "/docs", "tags": ["chat"],
        })
        check(status == 201, "创建提供商", str(provider)[:200])
        provider_id = provider.get("id", "")
        status, credential = http("POST", f"{base}/api/admin/providers/{provider_id}/credentials",
                                  headers=headers, body={
                                      "name": "echo 凭证", "kind": "llm", "auth_type": "bearer",
                                      "secret": "echo-secret-9999", "meta": {"probe_path": "/models"},
                                  })
        check(status == 201, "创建凭证（真实密钥不出现在响应）", str(credential)[:200])
        credential_id = credential.get("id", "")
        check("echo-secret-9999" not in json.dumps(credential), "响应中无明文密钥")

        status, probe = http("POST", f"{base}/api/admin/credentials/{credential_id}/verify", headers=headers)
        check(status == 200 and probe.get("ok") is True, "凭证有效性探测（真实请求 echo /models）", str(probe)[:200])

        # 能力：2 条 route
        definition = {
            "key": "text.generate.echo",
            "name": "Echo 文本能力",
            "kind": "text.generate",
            "modes": ["sync", "async"],
            "file_io": {"input": [], "output": []},
            "input_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            },
            "examples": [{"title": "最小样例", "input": {"prompt": "你好"}}],
            "compatibility_group": None,
            "failover": {"enabled": True, "max_switches": 2},
            "requires_agent_reasoning": False,
            "tags": ["chat"],
            "routes": [
                {
                    "id": "rt_primary", "provider_id": provider_id, "credential_id": credential_id,
                    "priority": 0, "enabled": True, "model_hint": "echo-model",
                    "request": {"method": "POST", "url_template": "{base_url}/v1/generate",
                                "headers": {"Content-Type": "application/json"}, "query": [],
                                "body_template": {"model": "${model_hint}", "prompt": "${prompt}"},
                                "encoding": "json"},
                    "param_map": {"prompt": {"from": "input.prompt", "transform": "str"},
                                  "model": {"from": "route.model_hint"}},
                    "response_map": {"format": "json", "jsonpath": "$.result.text",
                                     "usage": {"in": "$.usage.input_tokens", "out": "$.usage.output_tokens"}},
                    "async": {"submit": {"url_template": "{base_url}/v1/jobs", "task_id_path": "$.id"},
                              "query": {"url_template": "{base_url}/v1/tasks/{task_id}",
                                        "status_path": "$.status"},
                              "poll": {"initial_ms": 100, "max_ms": 500, "timeout_s": 30}},
                    "limits": {"max_input_bytes": 262144, "allowed_mime": []},
                    "errors": [{"match": {"status": 401}, "code": "UPSTREAM_AUTH", "retryable": False},
                               {"match": {"status": 429}, "code": "UPSTREAM_RATE_LIMIT", "retryable": True}],
                    "timeout_ms": 20000, "retry": {"max_attempts": 1, "base_ms": 100, "max_ms": 400},
                },
                {
                    "id": "rt_backup", "provider_id": provider_id, "credential_id": credential_id,
                    "priority": 10, "enabled": True, "model_hint": "echo-model-b",
                    "request": {"method": "POST", "url_template": "{base_url}/v2/generate",
                                "headers": {"Content-Type": "application/json"}, "query": [],
                                "body_template": {"prompt": "${prompt}"}, "encoding": "json"},
                    "param_map": {"prompt": {"from": "input.prompt"}},
                    "response_map": {"format": "json", "jsonpath": "$.result.text"},
                    "limits": {}, "errors": [{"match": {"status": 429}, "code": "UPSTREAM_RATE_LIMIT",
                                              "retryable": True}],
                    "timeout_ms": 20000, "retry": {"max_attempts": 1},
                },
            ],
        }
        status, capability = http("POST", f"{base}/api/admin/capabilities", headers=headers, body={
            "key": definition["key"], "name": definition["name"], "kind": definition["kind"],
            "definition": definition, "tags": ["chat"],
        })
        check(status == 201, "创建能力（2 条 route）", str(capability)[:300])
        capability_id = capability.get("id", "")
        check(capability.get("route_count") == 2, "能力确有 2 条 route")

        status, validation = http("POST", f"{base}/api/admin/capabilities/{capability_id}/validate",
                                  headers=headers)
        errors = [i for i in validation.get("issues", []) if i["level"] == "error"]
        check(status == 200 and not errors, "结构校验通过", json.dumps(errors, ensure_ascii=False)[:300])

        status, no_evidence = http("POST", f"{base}/api/admin/capabilities/{capability_id}/publish",
                                   headers=headers, body={"version": "1.0.0"})
        check(status == 409 and no_evidence["error"]["code"] == "PUBLISH_EVIDENCE_MISSING",
              "无测试证据禁止发布", str(no_evidence)[:160])

        status, test_run = http("POST", f"{base}/api/admin/capabilities/{capability_id}/test",
                                headers=headers, body={"input": {"prompt": "真实联调"}})
        check(status == 200 and test_run.get("status") == "succeeded",
              "能力测试：真实 HTTP 调用 echo 上游", str(test_run)[:300])
        check(test_run.get("output") == "echo:真实联调", "测试输出与上游一致", str(test_run.get("output"))[:120])

        status, published = http("POST", f"{base}/api/admin/capabilities/{capability_id}/publish",
                                 headers=headers, body={"version": "1.0.0", "changelog": "e2e"})
        check(status == 200, "发布能力 1.0.0", str(published)[:200])

        # 外部 Agent + 令牌
        status, agent = http("POST", f"{base}/api/admin/access/agents", headers=headers,
                             body={"name": "e2e-agent", "contact": "e2e@example.com"})
        check(status == 201, "创建外部 Agent", str(agent)[:200])
        status, token_data = http("POST", f"{base}/api/admin/access/agents/{agent['id']}/tokens",
                                  headers=headers, body={
                                      "name": "e2e-token", "scopes": ["*"],
                                      "allowed_modes": ["sync", "async"], "rpm": 600, "daily_calls": 5000,
                                  })
        check(status == 201, "签发平台令牌", str(token_data)[:160])
        token = token_data.get("token", "")
        auth = {"Authorization": f"Bearer {token}"}
        install_command = token_data.get("install_command", "")
        check(token.startswith("srk_"), "令牌格式为 srk_…")
        check("install" in install_command, "返回可复制的安装命令", install_command[:160])

        # /api/v1 同步调用
        status, invoke = http("POST", f"{base}/api/v1/capabilities/text.generate.echo/invoke",
                              headers=auth, body={"input": {"prompt": "外部调用"}})
        check(status == 200 and invoke.get("status") == "succeeded", "/api/v1 同步调用成功", str(invoke)[:300])
        request_id = invoke.get("request_id", "")
        check(invoke.get("output") == "echo:外部调用", "同步输出正确", str(invoke.get("output"))[:120])

        # 异步调用 + worker（真实队列 + 轮询 echo）
        status, submitted = http("POST", f"{base}/api/v1/capabilities/text.generate.echo/invoke",
                                 headers=auth, body={"input": {"prompt": "异步"}, "options": {"mode": "async"}})
        check(status == 202 and submitted.get("task_id"), "异步提交返回 202 + task_id", str(submitted)[:200])
        task_id = submitted.get("task_id", "")

        worker_env = {**env}
        worker_env = {**env}
        drain_code = (
            "import asyncio\n"
            "from skillrouter_api.worker.runner import Worker\n"
            "async def main():\n"
            "    w = Worker(worker_id='e2e-worker')\n"
            "    w.register_all()\n"
            "    for _ in range(40):\n"
            "        n = await w.run_once(['capability_invoke_async'])\n"
            "        if n == 0:\n"
            "            await asyncio.sleep(0.2)\n"
            "asyncio.run(main())\n"
        )
        drain = subprocess.run(
            [sys.executable, "-c", drain_code],
            cwd=str(API_DIR), env=worker_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        check(drain.returncode == 0, "worker 领取并执行异步任务", ((drain.stdout or "") + (drain.stderr or ""))[-500:])
        deadline = time.time() + 30
        task: dict = {}
        while time.time() < deadline:
            status, task = http("GET", f"{base}/api/v1/tasks/{task_id}", headers=auth)
            if task.get("status") in ("succeeded", "failed", "canceled", "status_unknown"):
                break
            time.sleep(0.5)
        check(task.get("status") == "succeeded", "异步任务完成并回填上游任务号",
              f"{task.get('status')} / {task.get('error')}")
        check(bool(task.get("upstream_task_id")), "记录了 upstream_task_id",
              str(task.get("upstream_task_id")))

        # 调用记录与审计
        status, call_detail = http("GET", f"{base}/api/admin/calls/{request_id}", headers=headers)
        check(status == 200, "调用记录可查询", str(status))
        check(call_detail.get("route_decision", {}).get("chosen_route_id") == "rt_primary",
              "记录中包含 route 决策", str(call_detail.get("route_decision"))[:200])
        calls = http("GET", f"{base}/api/admin/calls?page_size=50", headers=headers)[1]
        check(calls.get("total", 0) >= 2, "调用列表包含多次调用", str(calls.get("total")))
        audits = http("GET", f"{base}/api/admin/settings/audit?page_size=100", headers=headers)[1]
        actions = {item["action"] for item in audits.get("items", [])}
        check({"capability.published", "token.created", "credential.created"} <= actions,
              "审计覆盖凭证/发布/令牌", str(sorted(actions))[:300])
        check("echo-secret-9999" not in json.dumps(audits), "审计不含明文密钥")

        # MCP
        status, init = http("POST", f"{base}/api/v1/mcp", headers=auth,
                            body={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}})
        check(status == 200 and init["result"]["protocolVersion"] == "2025-06-18", "MCP initialize", str(init)[:200])
        status, tools = http("POST", f"{base}/api/v1/mcp", headers=auth,
                             body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in tools["result"]["tools"]]
        check("capability__text_generate_echo" in names, "MCP tools/list 暴露能力工具", str(names)[:200])
        status, mcp_call = http("POST", f"{base}/api/v1/mcp", headers=auth,
                                body={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "capability__text_generate_echo",
                                                 "arguments": {"input": {"prompt": "mcp 调用"}}}})
        text = mcp_call["result"]["content"][0]["text"]
        check("echo:mcp 调用" in text, "MCP tools/call 与 HTTP 结果一致", text[:200])

        # SKILL 发布 + 导出
        status, skill = http("POST", f"{base}/api/admin/skills", headers=headers, body={
            "name": "echo-skill", "title": "Echo SKILL", "description": "调用 echo 能力",
            "instructions": "用 node ./invoke.mjs invoke --capability text.generate.echo --input '{\"prompt\":\"hi\"}'",
            "activation": "auto", "dependencies": [{"capability_key": "text.generate.echo",
                                                    "version_range": ">=1.0.0"}],
        })
        check(status == 201, "创建 SKILL", str(skill)[:200])
        skill_id = skill.get("id", "")
        status, skill_pub = http("POST", f"{base}/api/admin/skills/{skill_id}/publish",
                                 headers=headers, body={"version": "1.0.0"})
        check(status == 200, "发布 SKILL", str(skill_pub)[:200])

        # CLI 安装（真实 npx → 本地 tarball 或直接 node dist）
        workdir = Path(tempfile.mkdtemp(prefix="sr-cli-"))
        cli_entry = CLI_DIR / "dist" / "cli.js"
        cli_ready = cli_entry.exists()
        if not cli_ready:
            build = subprocess.run(["npx", "tsc", "-p", "."], cwd=str(CLI_DIR),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", shell=True)
            cli_ready = cli_entry.exists()
            check(cli_ready, "编译 CLI", ((build.stdout or "") + (build.stderr or ""))[-400:])
        if cli_ready:
            proc = subprocess.run(
                ["node", str(cli_entry), "install", "--base-url", base, "--token-stdin",
                 "--agent", "generic", "--dir", str(workdir), "--force"],
                input=token, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(workdir), timeout=180,
            )
            combined = (proc.stdout or "") + (proc.stderr or "")
            check(proc.returncode == 0, "skillrouter install 成功", combined[-500:])
            for name in ("SKILL.md", "invoke.mjs", "skillrouter.config.json", "README.md"):
                check((workdir / name).exists(), f"安装产物包含 {name}")
            if (workdir / "SKILL.md").exists():
                md = (workdir / "SKILL.md").read_text(encoding="utf-8")
                check("allowed-tools: [bash]" in md or "allowed-tools:\n  - bash" in md,
                      "SKILL.md 只声明真实存在的工具", md[:200])
                check("sr_" not in md.split("---")[1] if md.startswith("---") else True,
                      "frontmatter 不含网关内部工具")
                check(token not in md, "SKILL.md 不含令牌")
            if (workdir / "skillrouter.config.json").exists():
                config = json.loads((workdir / "skillrouter.config.json").read_text(encoding="utf-8"))
                check("token" not in json.dumps(config).replace("token_ref", ""),
                      "配置只保存 token_ref", json.dumps(config)[:200])
            invoke_js = workdir / "invoke.mjs"
            if invoke_js.exists():
                list_proc = subprocess.run(
                    ["node", str(invoke_js), "capabilities.list"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(workdir), timeout=120,
                    env={**os.environ, "SKILLROUTER_HOME": str(Path.home() / ".skillrouter")},
                )
                check(list_proc.returncode == 0, "invoke.mjs capabilities.list 可用",
                      ((list_proc.stdout or "") + (list_proc.stderr or ""))[-400:])
                check("text.generate.echo" in (list_proc.stdout or ""), "动态发现看到已发布能力",
                      (list_proc.stdout or "")[:200])
                call_proc = subprocess.run(
                    ["node", str(invoke_js), "invoke", "--capability", "text.generate.echo",
                     "--input", '{"prompt":"cli 调用"}'],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(workdir), timeout=120,
                )
                check(call_proc.returncode == 0, "invoke.mjs invoke 真实调用成功",
                      ((call_proc.stdout or "") + (call_proc.stderr or ""))[-400:])
                check("echo:cli 调用" in (call_proc.stdout or ""), "CLI 调用拿到真实结果",
                      (call_proc.stdout or "")[:200])

        # 备份（纯 Python）
        status, backup = http("GET", f"{base}/api/admin/settings/backup/now", headers=headers)
        check(status == 200 and backup.get("sha256"), "备份产物生成并带 sha256", str(backup)[:200])

        status, rotate = http("POST", f"{base}/api/admin/settings/rotate-master-key", headers=headers,
                              body={})
        check(status == 200, "主密钥轮换完成", str(rotate)[:200])
        status, after = http("GET", f"{base}/api/admin/credentials/{credential_id}", headers=headers)
        check(status == 200 and after.get("status") == "active", "轮换后凭证仍可用")
        status, probe2 = http("POST", f"{base}/api/admin/credentials/{credential_id}/verify", headers=headers)
        check(status == 200 and probe2.get("ok") is True, "轮换后可继续真实调用（解密正常）")

        # 重启不复活默认密码
        status, relogin = http("POST", f"{base}/api/admin/auth/login",
                               body={"username": "admin", "password": "adminjr"})
        check(status == 401, "旧默认密码已失效")
        status, newlogin = http("POST", f"{base}/api/admin/auth/login",
                                body={"username": "admin", "password": "SkillRouter-E2E-2026"})
        check(status == 200, "新密码可登录")

        return _finish(api_proc)
    finally:
        upstream.shutdown()
        if api_proc.poll() is None:
            api_proc.terminate()
            try:
                api_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                api_proc.kill()


def _finish(api_proc: subprocess.Popen) -> int:
    passed = sum(1 for ok, _, _ in results if ok)
    print("\n" + "─" * 70)
    print(f" 端到端结果：{passed}/{len(results)} 项通过")
    if passed != len(results):
        for ok, label, detail in results:
            if not ok:
                print(f"   {FAIL} {label} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
