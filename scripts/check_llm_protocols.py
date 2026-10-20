"""LLM 服务配置的四协议矩阵校验（真实 uvicorn + 4 个本地假上游）。

对应 `docs/plans/llm-provider-config/spec.md` §7.2：
  E-01  python scripts/check_llm_protocols.py
        → 4 协议 × 3 阶段（auth / completion / models）全绿，
          落 `var/reports/llm-protocols.md`（每行带 request_id）
  E-02  python scripts/check_llm_protocols.py --only anthropic --auth-401
        → 假上游一律回 401，脚本断言结论是 UPSTREAM_AUTH 且 hint 提到「API key」

怎么做的（平台侧不 mock）：
  1) 起 4 个本地假上游（每个协议一个 `http.server.ThreadingHTTPServer`，daemon 线程：
     - openai    GET /v1/models + POST /v1/chat/completions
     - anthropic GET /v1/models（has_more 分页）+ POST /v1/messages（必须带 max_tokens）
     - google    GET /v1beta/models + POST /v1beta/models/{model}:generateContent
     - custom    OpenAI 兼容网关，挂在子路径 /openai 下
                 （/openai/models + /openai/chat/completions）
     每个假上游都会校验认证头（`Authorization: Bearer` / `x-api-key` + `anthropic-version` /
     `x-goog-api-key`），头不对就按真实上游的方式回 401/400 —— 这样「认证头漏了」也会被抓到，
     而不是所有协议都「看起来通」。
  2) 起真实 uvicorn（`apps/api`），数据落在临时目录（`SKILLROUTER_DATA_DIR` /
     `SKILLROUTER_DB_PATH` 指向它，不碰开发者的 `var/`）。
  3) 管理端登录（首次会自动完成强制改密），然后跑 4 协议 × 3 阶段矩阵：
     auth        POST /api/admin/llm/test-connection {"mode":"auth"}       不计费
     completion  POST /api/admin/llm/test-connection {"mode":"completion"} 一次生成
     models      POST /api/admin/llm/models                                清单 + 缓存
  4) 打印并落一份 Markdown 证据表（含每行的 request_id 与假上游被请求次数）。

只用标准库；不需要任何第三方凭证，也不需要 Node / Next。

退出码：0 = 全绿；1 = 有行不通过；2 = 环境不可用（API 起不来 / 新端点还没实现）。
"""

from __future__ import annotations

import argparse
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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
REPORT_PATH = ROOT / "var" / "reports" / "llm-protocols.md"

PROTOCOLS = ("openai", "anthropic", "google", "custom")
WIRE = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "custom": "openai",
}
MODEL = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    "google": "gemini-2.5-pro",
    "custom": "qwen2.5-72b-instruct",
}
# 草稿密钥：刻意写成一眼可辨的假值，任何真实凭证都不长这样
DRAFT_SECRET = "local-fake-llm-key-7f3a9c"
ADMIN_USER = "admin"
CHANGED_PASSWORD = "SkillRouter-LLM-2026"
CUSTOM_SUBPATH = "/openai"

OK, FAIL = "✓", "✗"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass


# ── 假上游：四种协议形状 ─────────────────────────────────────────────────────


class FakeLlmServer(ThreadingHTTPServer):
    """带协议标签的假上游；端口由系统分配（0），避免占端口冲突。"""

    daemon_threads = True

    def __init__(self, kind: str, auth_401: bool) -> None:
        super().__init__(("127.0.0.1", 0), FakeLlmHandler)
        self.kind = kind
        self.auth_401 = auth_401
        self.hits: list[str] = []

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def routes(self) -> str:
        if self.kind == "custom":
            return self.base + CUSTOM_SUBPATH
        return self.base


class FakeLlmHandler(BaseHTTPRequestHandler):
    server: FakeLlmServer

    def log_message(self, *args: object) -> None:  # noqa: ARG002
        pass

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── 认证头校验（真实上游也会这么拒）─────────────────────────────────────
    def _auth_failure(self) -> tuple[int, dict] | None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if self.server.auth_401:
            return (
                401,
                {
                    "error": {
                        "type": "authentication_error",
                        "message": "invalid API key provided: local-fake****",
                    }
                },
            )
        if self.server.kind == "anthropic":
            if not headers.get("x-api-key"):
                return (
                    401,
                    {
                        "error": {
                            "type": "authentication_error",
                            "message": "missing x-api-key",
                        }
                    },
                )
            if not headers.get("anthropic-version"):
                return (
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": "missing anthropic-version header",
                        }
                    },
                )
        elif self.server.kind == "google":
            if not headers.get("x-goog-api-key") and not query.get("key"):
                return (
                    401,
                    {
                        "error": {
                            "code": 401,
                            "status": "UNAUTHENTICATED",
                            "message": "API key not valid. Please pass a valid API key.",
                        }
                    },
                )
        else:
            if not (headers.get("authorization") or "").lower().startswith("bearer "):
                return (
                    401,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": "missing Authorization: Bearer header",
                        }
                    },
                )
        return None

    # ── GET：模型清单 ───────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        self.server.hits.append("GET " + self.path)
        failure = self._auth_failure()
        if failure is not None:
            return self._json(*failure)
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        if self.server.kind == "anthropic" and path.endswith("/v1/models"):
            return self._json(200, self._anthropic_models(query))
        if self.server.kind == "google" and path.endswith("/v1beta/models"):
            return self._json(200, self._google_models())
        if self.server.kind in ("openai", "custom") and path.endswith("/models"):
            return self._json(200, self._openai_models())
        return self._json(404, {"error": {"message": f"unknown path {path}"}})

    def _openai_models(self) -> dict:
        if self.server.kind == "custom":
            return {
                "object": "list",
                "data": [
                    {
                        "id": MODEL["custom"],
                        "object": "model",
                        "created": 1700000200,
                        "owned_by": "self-hosted",
                    },
                    {
                        "id": "text-embedding-3-large",
                        "object": "model",
                        "created": 1700000201,
                        "owned_by": "self-hosted",
                    },
                ],
            }
        return {
            "object": "list",
            "data": [
                {
                    "id": MODEL["openai"],
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "openai",
                },
                {
                    "id": "text-embedding-3-large",
                    "object": "model",
                    "created": 1700000001,
                    "owned_by": "openai",
                },
            ],
        }

    def _anthropic_models(self, query: dict[str, list[str]]) -> dict:
        # 第一页 has_more=true；带 after_id 的第二页 has_more=false（spec §3.5 续页口径）
        if query.get("after_id"):
            return {
                "data": [
                    {
                        "id": "claude-haiku-4-5",
                        "display_name": "Claude Haiku 4.5",
                        "created_at": "2025-10-01T00:00:00Z",
                        "type": "model",
                    }
                ],
                "has_more": False,
                "first_id": "claude-haiku-4-5",
                "last_id": "claude-haiku-4-5",
            }
        return {
            "data": [
                {
                    "id": MODEL["anthropic"],
                    "display_name": "Claude Sonnet 4.5",
                    "created_at": "2025-09-29T00:00:00Z",
                    "type": "model",
                }
            ],
            "has_more": True,
            "first_id": MODEL["anthropic"],
            "last_id": MODEL["anthropic"],
        }

    def _google_models(self) -> dict:
        return {
            "models": [
                {
                    "name": f"models/{MODEL['google']}",
                    "displayName": "Gemini 2.5 Pro",
                    "inputTokenLimit": 1048576,
                    "outputTokenLimit": 65536,
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {
                    "name": "models/text-embedding-004",
                    "displayName": "Text Embedding 004",
                    "inputTokenLimit": 2048,
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }

    # ── POST：一次最小生成 ──────────────────────────────────────────────────
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            body = {}
        self.server.hits.append("POST " + self.path)
        failure = self._auth_failure()
        if failure is not None:
            return self._json(*failure)
        path = urllib.parse.urlsplit(self.path).path
        if self.server.kind == "anthropic" and path.endswith("/v1/messages"):
            if not body.get("max_tokens"):
                return self._json(
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": "max_tokens: Field required",
                        }
                    },
                )
            return self._json(200, self._anthropic_message(body))
        if self.server.kind == "google" and ":generateContent" in path:
            return self._json(200, self._google_completion())
        if self.server.kind in ("openai", "custom") and path.endswith(
            "/chat/completions"
        ):
            return self._json(200, self._openai_completion())
        return self._json(404, {"error": {"message": f"unknown path {path}"}})

    def _openai_completion(self) -> dict:
        return {
            "id": "chatcmpl-fake0001",
            "object": "chat.completion",
            "created": 1700000100,
            "model": MODEL.get(self.server.kind, "fake-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "正常"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        }

    def _anthropic_message(self, body: dict) -> dict:
        return {
            "id": "msg_fake0001",
            "type": "message",
            "role": "assistant",
            "model": body.get("model") or MODEL["anthropic"],
            "content": [{"type": "text", "text": "正常"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 9, "output_tokens": 2},
        }

    def _google_completion(self) -> dict:
        return {
            "candidates": [
                {
                    "content": {"parts": [{"text": "正常"}], "role": "model"},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 9,
                "candidatesTokenCount": 2,
                "totalTokenCount": 11,
            },
        }


def start_fake_upstreams(*, auth_401: bool) -> dict[str, FakeLlmServer]:
    servers: dict[str, FakeLlmServer] = {}
    for kind in PROTOCOLS:
        server = FakeLlmServer(kind, auth_401)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers[kind] = server
    return servers


# ── HTTP 客户端（Cookie 会话 + 读响应头拿 request_id）─────────────────────────

_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))


def call(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 120.0,
) -> tuple[int, object, dict[str, str]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            head = {key.lower(): value for key, value in response.headers.items()}
            status = response.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        head = {key.lower(): value for key, value in exc.headers.items()}
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)[:300], {}
    try:
        return status, json.loads(text), head
    except json.JSONDecodeError:
        return status, text[:400], head


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def error_text(payload: dict) -> str:
    error = as_dict(payload.get("error"))
    if error:
        code = str(error.get("code") or "")
        message = str(error.get("hint") or error.get("message") or "")
        return f"{code}：{message}" if code else message
    code = str(payload.get("error_code") or "")
    if code:
        return f"{code}：{payload.get('hint') or ''}"
    return ""


def conclusion(payload: dict) -> tuple[str, str]:
    """从「200 + ok=false」或「非 2xx + error{}」两种形态里取 (code, hint)。"""
    code = str(payload.get("error_code") or "")
    hint = str(payload.get("hint") or "")
    error = as_dict(payload.get("error"))
    if error:
        code = code or str(error.get("code") or "")
        hint = hint or str(error.get("hint") or "")
    return code, hint


def find_stage(payload: dict, stage: str) -> dict | None:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return None
    for item in stages:
        if isinstance(item, dict) and item.get("stage") == stage:
            return item
    return None


def model_count(payload: dict) -> int | None:
    for key in ("count", "model_count"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    items = payload.get("items")
    if isinstance(items, list):
        return len(items)
    stages = payload.get("stages")
    if isinstance(stages, list):
        counts = [
            item["model_count"]
            for item in stages
            if isinstance(item, dict) and isinstance(item.get("model_count"), int)
        ]
        if counts:
            return max(counts)
    return None


def short(value: object, limit: int = 72) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── 报告行 ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Row:
    protocol: str
    stage: str
    endpoint: str
    ok: bool = False
    http_status: int = 0
    models: int | None = None
    upstream_hits: int = 0
    elapsed_ms: int = 0
    request_id: str = ""
    note: str = ""
    #: 这一行触发的上游请求（`GET /v1/models` 这种形态），用于证明**完成**阶段
    #: 打的是 `chat/completions` / `/v1/messages` / `:generateContent` 而不是清单地址
    upstream_paths: list[str] = field(default_factory=list)


def render_table(rows: list[Row]) -> str:
    lines = [
        "| 协议 | 阶段 | 端点 | 结论 | HTTP | 模型数 | 上游请求 | 耗时 (ms) | request_id | 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        models = "—" if row.models is None else str(row.models)
        lines.append(
            f"| `{row.protocol}` | `{row.stage}` | `{row.endpoint}` | {OK if row.ok else FAIL} | "
            f"{row.http_status} | {models} | {row.upstream_hits} | {row.elapsed_ms} | "
            f"{row.request_id or '—'} | {row.note or '—'} |"
        )
    return "\n".join(lines)


def print_row(row: Row) -> None:
    bits = [f"HTTP {row.http_status}"]
    if row.models is not None:
        bits.append(f"{row.models} 个模型")
    bits.append(f"上游 {row.upstream_hits} 次")
    bits.append(f"{row.elapsed_ms}ms")
    if row.request_id:
        bits.append(f"request_id={row.request_id}")
    detail = " · ".join(bits)
    tail = "" if row.ok else f" — {row.note}"
    print(f" {OK if row.ok else FAIL} [{row.protocol}] {row.stage:<10} {detail}{tail}")


# ── 平台侧：起 API / 登录 / 跑矩阵 ───────────────────────────────────────────


def wait_ready(url: str, timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status, _, _ = call("GET", url, timeout=5.0)
        if status and status < 500:
            return True
        time.sleep(0.5)
    return False


def tail(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def login(base: str, password: str) -> tuple[str, str]:
    status, payload, _ = call(
        "POST",
        f"{base}/api/admin/auth/login",
        body={"username": ADMIN_USER, "password": password},
    )
    body = as_dict(payload)
    if status != 200:
        return "", f"登录失败：HTTP {status} {error_text(body) or short(payload, 160)}"
    csrf = str(body.get("csrf_token") or "")
    admin = as_dict(body.get("admin"))
    if admin.get("must_change_password"):
        status, payload, _ = call(
            "POST",
            f"{base}/api/admin/auth/change-password",
            body={"current_password": password, "new_password": CHANGED_PASSWORD},
            headers={"X-CSRF-Token": csrf},
        )
        body = as_dict(payload)
        if status != 200:
            return (
                "",
                f"强制改密失败：HTTP {status} {error_text(body) or short(payload, 160)}",
            )
        csrf = str(body.get("csrf_token") or csrf)
    if not csrf:
        return "", "登录响应里没有 csrf_token（会话契约变了？）"
    return csrf, ""


def target_body(server: FakeLlmServer, protocol: str) -> dict:
    if protocol == "custom":
        return {
            "protocol": "custom",
            "wire": WIRE["custom"],
            "base_url": server.base + CUSTOM_SUBPATH,
            "secret": DRAFT_SECRET,
            "auth_style": "bearer",
        }
    return {"protocol": protocol, "base_url": server.base, "secret": DRAFT_SECRET}


def probe(
    base: str,
    *,
    protocol: str,
    server: FakeLlmServer,
    headers: dict,
    stage: str,
    endpoint: str,
    path: str,
    body: dict,
) -> tuple[Row, dict]:
    row = Row(protocol=protocol, stage=stage, endpoint=endpoint)
    before = len(server.hits)
    started = time.perf_counter()
    status, payload, head = call("POST", f"{base}{path}", body=body, headers=headers)
    row.elapsed_ms = int((time.perf_counter() - started) * 1000)
    row.http_status = status
    row.request_id = str(head.get("x-request-id") or "")
    row.upstream_hits = len(server.hits) - before
    row.upstream_paths = list(server.hits[before:])
    return row, as_dict(payload)


def judge_stage(row: Row, payload: dict, *, stage: str, need_models: bool) -> None:
    row.models = model_count(payload)
    if row.http_status != 200:
        row.note = f"HTTP {row.http_status} {error_text(payload)}".strip()
        return
    stage_result = find_stage(payload, stage)
    if stage_result is None:
        # 没有这一阶段的证据就不算通过（否则「只回 auth 的响应」会被当成 completion 绿）
        row.note = f"响应里没有 {stage} 阶段：无法证明该阶段真的跑过"
        return
    stage_detail = short(stage_result.get("detail"), 80)
    if payload.get("ok") is not True:
        bits = [item for item in (stage_detail, error_text(payload)) if item]
        row.note = (
            " · ".join(bits)
            or f"ok=false（响应里没有 error_code）：{short(payload, 80)}"
        )
        return
    if stage_result.get("ok") is not True:
        row.note = f"{stage} 阶段失败：{stage_detail or '（上游没给 detail）'}"
        return
    if need_models and not row.models:
        row.note = "auth 通过但一个模型都没识别出来（清单为空）"
        return
    if row.upstream_hits == 0:
        row.note = "判为通过，但这次没有发出任何上游请求（INV-2：假成功）"
        return
    row.ok = True
    # 备注写**上游真正收到的那条请求**：完成阶段的 `resolved_url` 是取清单地址，
    # 只写它会让「地址拼对了吗」这件事在 completion 行上得不到证据（spec §7.2 E-01）。
    row.note = "、".join(row.upstream_paths) or short(payload.get("resolved_url"))


def judge_models(row: Row, payload: dict) -> None:
    row.models = model_count(payload)
    if row.http_status != 200:
        row.note = f"HTTP {row.http_status} {error_text(payload)}".strip()
        return
    if not row.models:
        row.note = "清单为空（没有任何模型被归一化出来）"
        return
    source = payload.get("source") or "?"
    if row.upstream_hits == 0:
        row.note = f"refresh=true 却没打上游（source={source}）：缓存没有被绕过"
        return
    if row.protocol == "anthropic" and row.upstream_hits < 2:
        row.note = (
            "anthropic 的 has_more 没有续页（上游只被请求 1 次，spec §3.5 / T-19）"
        )
        return
    row.ok = True
    row.note = f"source={source}"


def judge_mutation(row: Row, payload: dict) -> None:
    row.models = model_count(payload)
    if row.http_status == 0:
        row.note = f"请求没发出去（连接失败）：{short(payload, 80)}"
        return
    code, hint = conclusion(payload)
    if payload.get("ok") is True:
        row.note = "上游回 401 却被判为「通过」——这是假成功（INV-2）"
        return
    if code != "UPSTREAM_AUTH":
        row.note = f"结论是 {code or '（无 error_code）'}，期望 UPSTREAM_AUTH"
        return
    if "api key" not in hint.lower():
        row.note = f"hint 未提到「API key」：{short(hint, 60) or '（空）'}"
        return
    row.ok = True
    row.note = f"{code} · hint: {short(hint, 64)}"


def run_matrix(
    base: str,
    selected: list[str],
    servers: dict[str, FakeLlmServer],
    headers: dict,
    *,
    mutation: bool,
) -> list[Row]:
    rows: list[Row] = []
    endpoint_test = "POST /llm/test-connection"
    for protocol in selected:
        server = servers[protocol]
        target = target_body(server, protocol)
        if mutation:
            row, payload = probe(
                base,
                protocol=protocol,
                server=server,
                headers=headers,
                stage="auth",
                endpoint=endpoint_test,
                path="/api/admin/llm/test-connection",
                body={**target, "mode": "auth"},
            )
            judge_mutation(row, payload)
            rows.append(row)
            print_row(row)
            continue

        row, payload = probe(
            base,
            protocol=protocol,
            server=server,
            headers=headers,
            stage="auth",
            endpoint=endpoint_test,
            path="/api/admin/llm/test-connection",
            body={**target, "mode": "auth"},
        )
        judge_stage(row, payload, stage="auth", need_models=True)
        rows.append(row)
        print_row(row)

        row, payload = probe(
            base,
            protocol=protocol,
            server=server,
            headers=headers,
            stage="completion",
            endpoint=endpoint_test,
            path="/api/admin/llm/test-connection",
            body={**target, "mode": "completion", "model": MODEL[protocol]},
        )
        judge_stage(row, payload, stage="completion", need_models=False)
        rows.append(row)
        print_row(row)

        row, payload = probe(
            base,
            protocol=protocol,
            server=server,
            headers=headers,
            stage="models",
            endpoint="POST /llm/models",
            path="/api/admin/llm/models",
            body={**target, "refresh": True, "include_non_llm": False, "limit": 500},
        )
        judge_models(row, payload)
        rows.append(row)
        print_row(row)
    return rows


# ── 证据表 ──────────────────────────────────────────────────────────────────


def render_report(
    rows: list[Row],
    *,
    mutation: bool,
    base: str,
    data_dir: Path,
    servers: dict[str, FakeLlmServer],
    command: str,
) -> str:
    passed = sum(1 for row in rows if row.ok)
    upstreams = "、".join(
        f"`{kind}` {server.routes()}" for kind, server in servers.items()
    )
    lines = [
        "# LLM 协议矩阵实测（`scripts/check_llm_protocols.py`）",
        "",
        "> 由脚本自动生成，**不要手改**。规格：`docs/plans/llm-provider-config/spec.md` v2.0"
        " §7.2 E-01 / E-02。",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 生成时间（UTC） | {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} |",
        f"| 模式 | {'变异：假上游一律回 401（E-02）' if mutation else '全矩阵：4 协议 × 3 阶段（E-01）'} |",
        f"| 被测服务 | 真实 uvicorn `apps/api` @ `{base}` |",
        f"| 数据目录 | `{data_dir}`（临时目录，未触碰 `var/`） |",
        f"| 假上游 | {upstreams} |",
        "| 依赖 | 纯标准库（`http.server` / `urllib`），无第三方凭证、无 Node |",
        f"| 结论 | **{passed}/{len(rows)} 行通过** |",
        "",
        "## 矩阵",
        "",
        render_table(rows),
        "",
        "列含义：`上游请求` = 该行执行期间假上游收到的 HTTP 次数（用来证明"
        "「结论背后真的有一次上游请求」，INV-2）；`备注` 为**上游实际收到的那条请求**"
        "（`GET /v1/models?limit=1000` / `POST /v1/messages` / `:generateContent` …，"
        "用来证明地址拼接对了）或失败原因。",
        "",
    ]
    if mutation:
        lines += [
            "## 变异断言（E-02）",
            "",
            "假上游对**所有**请求回 401。期望的结论：`200 + ok=false + error_code=UPSTREAM_AUTH`，"
            "且 `hint` 里出现「API key」（告诉人下一步去查密钥，而不是一句「测试失败」）。",
            "",
        ]
    lines += [
        "## 复现命令",
        "",
        "```bash",
        command,
        "```",
        "",
        "## 未覆盖（按仓库规矩保持「待验证」）",
        "",
        "| 项 | 为什么仍未验证 |",
        "|---|---|",
        "| 真实上游凭证（Anthropic `/v1/models` 分页 / Google `pageSize` / OpenAI 兼容网关）"
        " | 本机无第三方 key，这里只证明**协议形状**与地址拼接一致 |",
        "| 多 worker 部署下的探测限流精度 | 限流是进程内 token bucket，单 uvicorn 进程前提 |",
        "| `custom + wire=anthropic/google` 的真实网关 | 本脚本的 custom 行是 OpenAI 兼容网关"
        "（`wire=openai`） |",
        "| 桌面端 18100 下的全流程 | 需出包后真机双击（`npm run smoke`） |",
        "",
    ]
    return "\n".join(lines)


def report_path_for(*, mutation: bool, only: str | None) -> Path:
    """报告路径带上模式后缀：变异/单协议运行**不得**覆盖全矩阵证据（spec §7.2 E-01）。"""
    if mutation:
        suffix = f"{only}-401" if only else "401"
    elif only:
        suffix = only
    else:
        return REPORT_PATH
    return REPORT_PATH.with_name(f"llm-protocols-{suffix}.md")


def write_report(text: str, path: Path | None = None) -> None:
    target = path or REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if DRAFT_SECRET in text:
        raise SystemExit("报告里出现了草稿密钥明文（INV-3 被破坏），拒绝落盘")
    target.write_text(text, encoding="utf-8", newline="\n")


# ── 入口 ────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_llm_protocols.py",
        description="LLM 四协议（openai/anthropic/google/custom）× 三阶段矩阵校验",
        epilog="退出码：0=全绿；1=有行不通过；2=环境不可用（API 起不来 / 新端点未实现）",
    )
    parser.add_argument(
        "--only",
        choices=PROTOCOLS,
        default=None,
        help="只跑这一个协议（默认跑全部四个）",
    )
    parser.add_argument(
        "--auth-401",
        "--mutate-auth",
        dest="auth_401",
        action="store_true",
        help="变异模式：假上游对一切请求回 401，断言结论是 UPSTREAM_AUTH 且 hint 提到「API key」",
    )
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("SKILLROUTER_ADMIN_PASSWORD") or "adminjr",
        help="临时库里首次管理员的初始密码（默认取 SKILLROUTER_ADMIN_PASSWORD 或 adminjr）",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="保留临时数据目录（默认在**成功**后删除；失败时一律保留以便看日志）",
    )
    return parser.parse_args(argv)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = [args.only] if args.only else list(PROTOCOLS)
    mutation = bool(args.auth_401)

    print("─" * 70)
    title = " LLM 四协议矩阵校验"
    print(title + ("（变异模式：假上游一律 401）" if mutation else ""))
    print("─" * 70)

    servers = start_fake_upstreams(auth_401=mutation)
    for kind, server in servers.items():
        print(f" 假上游 {kind:<10} {server.routes()}")

    data_dir = Path(tempfile.mkdtemp(prefix="sr-llm-"))
    api_log = data_dir / "api.out.log"
    env = {
        **os.environ,
        "SKILLROUTER_DATA_DIR": str(data_dir),
        "SKILLROUTER_DB_PATH": str(data_dir / "llm.db"),
        "SKILLROUTER_ENV": "development",
        "SKILLROUTER_ADMIN_USER": ADMIN_USER,
        "SKILLROUTER_ADMIN_PASSWORD": args.admin_password,
        "SKILLROUTER_AGENT_GATEWAY_SECRET": "check-llm-internal-secret-0123456789abcdef",
        "PYTHONIOENCODING": "utf-8",
    }
    api_proc: subprocess.Popen | None = None
    log_handle = None
    success = False
    try:
        bootstrap = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "bootstrap.py")],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if bootstrap.returncode != 0:
            print(" ✗ bootstrap 初始化失败")
            print(((bootstrap.stdout or "") + (bootstrap.stderr or ""))[-1500:])
            return 2
        print(f" ✓ bootstrap 初始化（临时库 {data_dir}）")

        log_handle = api_log.open("w", encoding="utf-8")
        port = free_port()
        api_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "skillrouter_api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(API_DIR),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        if not wait_ready(f"{base}/healthz"):
            print(" ✗ API 进程 90s 内未就绪（日志尾部见下）")
            print(tail(api_log))
            return 2
        print(f" ✓ API 就绪 {base}（真实 uvicorn 进程）")

        csrf, failure = login(base, args.admin_password)
        if failure:
            print(f" ✗ {failure}")
            print(tail(api_log))
            return 2
        print(" ✓ 管理端登录（首次登录已自动完成强制改密）")
        headers = {"X-CSRF-Token": csrf}

        status, payload, _ = call("GET", f"{base}/api/admin/llm/protocols")
        if status != 200:
            print(
                f" ✗ GET /api/admin/llm/protocols → HTTP {status} {error_text(as_dict(payload))}"
            )
            print(
                " ✗ 后端新端点还没就绪（`apps/api/skillrouter_api/api/admin/llm.py`），"
            )
            print("   本脚本已按 spec 写好；等实现合并后直接重跑即可。")
            return 2
        print(" ✓ GET /api/admin/llm/protocols 可达（协议注册表已实现）")
        print("")

        rows = run_matrix(base, selected, servers, headers, mutation=mutation)
        command = "python scripts/check_llm_protocols.py"
        if args.only:
            command += f" --only {args.only}"
        if mutation:
            command += " --auth-401"
        report = render_report(
            rows,
            mutation=mutation,
            base=base,
            data_dir=data_dir,
            servers=servers,
            command=command,
        )
        write_report(report, report_path_for(mutation=mutation, only=args.only))
        print("")
        print(report)

        passed = sum(1 for row in rows if row.ok)
        print("─" * 70)
        if mutation:
            print(
                f" 变异断言：{passed}/{len(rows)} 行符合预期（UPSTREAM_AUTH + hint 提到 API key）"
            )
        else:
            print(f" LLM 协议矩阵：{passed}/{len(rows)} 行通过")
        print(f" 证据已写入 {report_path_for(mutation=mutation, only=args.only)}")
        if passed != len(rows):
            for row in rows:
                if not row.ok:
                    print(f"   {FAIL} [{row.protocol}] {row.stage} — {row.note}")
            return 1
        success = True
        return 0
    finally:
        if log_handle is not None:
            log_handle.close()
        if api_proc is not None and api_proc.poll() is None:
            api_proc.terminate()
            try:
                api_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                api_proc.kill()
        for server in servers.values():
            server.shutdown()
            server.server_close()
        if success and not args.keep_data:
            # 成功时清掉临时库，避免每次运行都在 %TEMP% 留一个含 sqlite 的目录
            shutil.rmtree(data_dir, ignore_errors=True)
        else:
            print(f" 临时数据目录（含 api 日志）：{data_dir}")


if __name__ == "__main__":
    sys.exit(main())
