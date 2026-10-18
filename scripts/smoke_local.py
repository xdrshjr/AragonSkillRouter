"""示例：用 TestClient 跑完整闭环（本地手动验收辅助脚本）。

    python scripts/smoke_local.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

tmp = Path(tempfile.mkdtemp(prefix="sr-smoke-"))
try:  # Windows 控制台默认 GBK，强制 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass
os.environ.setdefault("SKILLROUTER_DATA_DIR", str(tmp))
os.environ.setdefault("SKILLROUTER_DB_PATH", str(tmp / "smoke.db"))
os.environ.setdefault("SKILLROUTER_ENV", "development")
os.environ.setdefault("SKILLROUTER_ADMIN_USER", "admin")
os.environ.setdefault("SKILLROUTER_ADMIN_PASSWORD", "adminjr")

from fastapi.testclient import TestClient  # noqa: E402

from skillrouter_api.agent import toolspec  # noqa: E402
from skillrouter_api.main import app  # noqa: E402
from skillrouter_api.services import bootstrap as bootstrap_service  # noqa: E402

OK = "✓"
FAIL = "✗"
results: list[tuple[bool, str]] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    results.append((bool(condition), label))
    print(f" {OK if condition else FAIL} {label}{(' — ' + detail) if detail and not condition else ''}")


def main() -> int:
    summary = bootstrap_service.bootstrap(reason="smoke")
    print(f" bootstrap: initialized={summary['initialized']} admin={summary['admin']}")
    client = TestClient(app)

    # 1) 未登录闸门
    r = client.get("/api/admin/overview")
    check(r.status_code == 401, "未登录访问管理接口返回 401", str(r.status_code))

    # 2) 登录 + 强制改密
    r = client.post("/api/admin/auth/login", json={"username": "admin", "password": "adminjr"})
    check(r.status_code == 200, "默认账号可登录", r.text[:200])
    csrf = r.json().get("csrf_token", "")
    check(r.json()["admin"]["must_change_password"] is True, "首次登录要求改密")
    headers = {"X-CSRF-Token": csrf}
    r = client.get("/api/admin/overview")
    check(r.status_code == 403 and r.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED",
          "未改密时业务接口被闸门拦下", r.text[:160])

    r = client.post("/api/admin/auth/change-password", headers=headers,
                    json={"current_password": "adminjr", "new_password": "SkillRouter2026"})
    check(r.status_code == 200, "改密成功", r.text[:200])
    csrf = r.json().get("csrf_token", csrf)
    headers = {"X-CSRF-Token": csrf}

    # 3) 概览
    r = client.get("/api/admin/overview")
    check(r.status_code == 200, "概览可访问", r.text[:200])
    check("attention" in r.json(), "概览含待办列表")

    # 4) 提供商 + 凭证
    r = client.post("/api/admin/providers", headers=headers, json={
        "slug": "anthropic", "name": "Anthropic", "category": "llm",
        "base_url": "https://api.anthropic.com", "docs_url": "https://docs.anthropic.com",
        "tags": ["llm", "chat"], "environment": "prod",
    })
    check(r.status_code == 201, "创建提供商", r.text[:300])
    provider_id = r.json().get("id", "")

    r = client.post(f"/api/admin/providers/{provider_id}/credentials", headers=headers, json={
        "name": "Anthropic 主密钥", "kind": "llm", "auth_type": "header",
        "header_name": "x-api-key", "secret": "sk-ant-smoke-0123456789abcdef",
        "meta": {"probe_path": "/v1/models"},
    })
    check(r.status_code == 201, "创建凭证", r.text[:300])
    credential_id = r.json().get("id", "")
    check("***" in r.json().get("secret", {}).get("masked", ""), "凭证只回显掩码")
    check("sk-ant-smoke" not in r.text, "响应中不含明文密钥")

    # 5) 能力草稿（多 route）
    definition = {
        "key": "text.generate.demo", "name": "演示文本能力", "kind": "text.generate",
        "modes": ["sync", "stream"],
        "file_io": {"input": [], "output": []},
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"], "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        },
        "examples": [{"title": "短提示", "input": {"prompt": "你好"}}],
        "compatibility_group": None,
        "failover": {"enabled": True, "max_switches": 2},
        "requires_agent_reasoning": False, "tags": ["chat"],
        "routes": [
            {
                "id": "rt_primary", "provider_id": provider_id, "credential_id": credential_id,
                "priority": 0, "enabled": True, "model_hint": "claude-sonnet-4-5",
                "request": {"method": "POST", "url_template": "{base_url}/v1/messages",
                            "headers": {"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
                            "query": [], "body_template": {"model": "${model_hint}", "prompt": "${prompt}"},
                            "encoding": "json"},
                "param_map": {"prompt": {"from": "input.prompt", "transform": "str"},
                              "model": {"from": "route.model_hint"}},
                "response_map": {"format": "json", "jsonpath": "$.completion",
                                 "usage": {"in": "$.usage.input_tokens", "out": "$.usage.output_tokens"}},
                "stream": {"protocol": "sse", "delta_path": "$.delta", "done_markers": ["[DONE]"]},
                "limits": {"max_input_bytes": 262144, "allowed_mime": []},
                "errors": [{"match": {"status": 401}, "code": "UPSTREAM_AUTH", "retryable": False},
                           {"match": {"status": 429}, "code": "UPSTREAM_RATE_LIMIT", "retryable": True}],
                "timeout_ms": 30000, "retry": {"max_attempts": 2, "base_ms": 500, "max_ms": 4000},
            },
            {
                "id": "rt_backup", "provider_id": provider_id, "credential_id": credential_id,
                "priority": 10, "enabled": True, "model_hint": "claude-haiku",
                "request": {"method": "POST", "url_template": "{base_url}/v1/messages",
                            "headers": {"Content-Type": "application/json"}, "query": [],
                            "body_template": {"model": "${model_hint}"}, "encoding": "json"},
                "param_map": {"model": {"from": "route.model_hint"}},
                "response_map": {"format": "json", "jsonpath": "$.completion"},
                "limits": {}, "errors": [{"match": {"status": 429}, "code": "UPSTREAM_RATE_LIMIT", "retryable": True}],
                "timeout_ms": 30000, "retry": {"max_attempts": 2},
            },
        ],
    }
    r = client.post("/api/admin/capabilities", headers=headers, json={
        "key": "text.generate.demo", "name": "演示文本能力", "kind": "text.generate",
        "definition": definition, "tags": ["chat"],
    })
    check(r.status_code == 201, "创建能力草稿（2 条 route）", r.text[:400])
    capability_id = r.json().get("id", "")
    check(r.json().get("route_count") == 2, "能力有 2 条 route")
    check(r.json().get("primary_route", {}).get("route_id") == "rt_primary", "主 route 为 rt_primary")

    r = client.post(f"/api/admin/capabilities/{capability_id}/validate", headers=headers)
    errors = [i for i in r.json().get("issues", []) if i["level"] == "error"]
    check(r.status_code == 200 and not errors, "结构校验通过", json.dumps(errors, ensure_ascii=False)[:300])

    # 6) 发布需要测试证据
    r = client.post(f"/api/admin/capabilities/{capability_id}/publish", headers=headers, json={"version": "1.0.0"})
    check(r.status_code == 409 and r.json()["error"]["code"] == "PUBLISH_EVIDENCE_MISSING",
          "无测试证据时禁止发布", r.text[:200])

    # 7) 外部 Agent + 令牌
    r = client.post("/api/admin/access/agents", headers=headers, json={"name": "demo-agent"})
    check(r.status_code == 201, "创建外部 Agent", r.text[:200])
    agent_id = r.json().get("id", "")
    r = client.post(f"/api/admin/access/agents/{agent_id}/tokens", headers=headers, json={
        "name": "demo-token", "scopes": ["*"], "allowed_modes": ["sync", "async", "stream"],
        "rpm": 120, "concurrency": 4,
    })
    check(r.status_code == 201, "签发平台令牌", r.text[:300])
    token = r.json().get("token", "")
    check(token.startswith("srk_"), "令牌格式 srk_...")
    check("install" in r.json().get("install_command", ""), "返回真实安装命令")
    auth = {"Authorization": f"Bearer {token}"}

    # 8) /api/v1 契约
    r = client.get("/api/v1/health")
    check(r.status_code == 200 and r.json()["status"] == "ok", "/api/v1/health")
    r = client.get("/api/v1/whoami", headers=auth)
    check(r.status_code == 200, "/api/v1/whoami 鉴权通路", r.text[:200])
    r = client.get("/api/v1/capabilities", headers=auth)
    check(r.status_code == 200, "/api/v1/capabilities 可列举", r.text[:200])
    etag = r.headers.get("etag")
    r304 = client.get("/api/v1/capabilities", headers={**auth, "If-None-Match": etag or ""})
    check(r304.status_code == 304, "ETag 命中返回 304")
    r = client.get("/api/v1/capabilities", headers={"Authorization": "Bearer srk_bad_x"})
    check(r.status_code == 401, "无效令牌被拒绝", str(r.status_code))

    # 9) 令牌撤销即时生效
    token_id = client.get("/api/admin/access/tokens", headers=headers).json()["items"][0]["id"]
    r = client.delete(f"/api/admin/access/tokens/{token_id}", headers=headers)
    check(r.status_code == 200, "撤销令牌")
    r = client.get("/api/v1/whoami", headers=auth)
    check(r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_REVOKED",
          "撤销后立即 401 TOKEN_REVOKED", r.text[:160])

    # 10) 审计与脱敏
    r = client.get("/api/admin/settings/audit", headers=headers)
    actions = [i["action"] for i in r.json().get("items", [])]
    check("credential.created" in actions and "token.created" in actions, "审计记录覆盖凭证与令牌")
    check("sk-ant-smoke" not in r.text, "审计响应不含明文密钥")

    # 11) SKILL 导出无密钥
    r = client.post("/api/admin/skills", headers=headers, json={
        "name": "demo-skill", "title": "演示 SKILL", "description": "演示用",
        "instructions": "调用能力 text.generate.demo", "activation": "auto",
    })
    check(r.status_code == 201, "创建 SKILL", r.text[:200])
    skill_id = r.json().get("id", "")
    r = client.post(f"/api/admin/skills/{skill_id}/export", headers=headers, json={"format": "zip"})
    check(r.status_code == 200, "SKILL 导出 zip", r.text[:200])
    r = client.get(f"/api/admin/skills/{skill_id}/preview", headers=headers)
    md = r.json().get("skill_md", "")
    check("allowed-tools: [bash]" in md, "导出的 frontmatter 只声明 bash")
    check("sr_" not in md.split("---")[1], "frontmatter 不含 sr_* 工具")
    check("sk-ant-smoke" not in md, "SKILL.md 不含密钥")

    # 12) 工具清单一致性
    check(not toolspec.drift(), "网关/平台工具白名单一致", str(toolspec.drift()))

    # 13) CSRF 缺失被拒绝
    r = client.post("/api/admin/providers", json={"slug": "x-corp", "name": "X"})
    check(r.status_code == 403 and r.json()["error"]["code"] == "CSRF_INVALID", "缺少 CSRF 被拒绝", r.text[:160])

    # 14) MCP initialize / tools.list（用新令牌，前面的已被撤销）
    r = client.post("/api/admin/access/agents", headers=headers, json={"name": "mcp-agent"})
    mcp_agent_id = r.json().get("id", "")
    r = client.post(f"/api/admin/access/agents/{mcp_agent_id}/tokens", headers=headers, json={
        "name": "mcp-token", "scopes": ["*"], "allowed_modes": ["sync", "async", "stream"],
    })
    mcp_auth = {"Authorization": f"Bearer {r.json().get('token', '')}"}
    r = client.post("/api/v1/mcp", headers=mcp_auth, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                           "params": {"protocolVersion": "2025-06-18"}})
    check(r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-06-18", "MCP initialize",
          r.text[:200])
    session_id = r.headers.get("Mcp-Session-Id", "")
    r = client.post("/api/v1/mcp", headers={**mcp_auth, "Mcp-Session-Id": session_id},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = [t["name"] for t in r.json().get("result", {}).get("tools", [])]
    check("capabilities_list" in tools and all(
        " " not in t and "." not in t for t in tools), "MCP tools/list 工具名合法", str(tools)[:200])

    # 15) 重启不复活默认密码
    again = bootstrap_service.bootstrap(reason="restart")
    check(again["admin"].get("created") is False, "重启不重建默认管理员")
    r = client.post("/api/admin/auth/login", json={"username": "admin", "password": "adminjr"})
    check(r.status_code == 401, "旧默认密码不再可用", r.text[:160])

    passed = sum(1 for ok, _ in results if ok)
    print("\n" + "─" * 68)
    print(f" 结果：{passed}/{len(results)} 项通过")
    if passed != len(results):
        for ok, label in results:
            if not ok:
                print(f"   {FAIL} {label}")
        return 1
    print(f" 数据目录：{tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
