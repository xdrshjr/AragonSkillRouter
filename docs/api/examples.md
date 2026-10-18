# 接口示例（`/api/v1`）

> 目标是**可复制即用**。示例里的 `https://router.example.com`、`srk_xxx_yyy`、能力 key
> 都是占位符，替换成你自己的值即可。
> 示例覆盖：health、whoami、capabilities（list/get）、invoke（sync / async / stream）、
> task status / cancel、file upload、skills（list / read / bundle）、MCP（initialize / tools.list / tools.call）。

## 0. 约定（读一次就够）

| 项 | 规则 | 出处 |
|---|---|---|
| 基址 | 管理端 `/api/admin`，对外 `/api/v1` | §9.1 |
| 认证 | 对外 `Authorization: Bearer srk_<keyid>_<secret>`；管理端 `sr_cookie` 会话 + 写请求 `X-CSRF-Token` | §9.1 / §7.3 |
| 错误体 | 所有非 2xx 都是 `{"error":{"code","message","hint","retryable","details","request_id"}}` | §9.1 |
| 追踪 | 可带 `X-Request-Id`，响应始终回显；报障请提供它 | §9.1 / §7.14 |
| 幂等 | 写操作支持 `Idempotency-Key`，同键同体返回原结果，同键异体返回 `409 IDEMPOTENCY_KEY_REUSED` | §4.4(c) / 附录 A |
| 分页 | `?page=1&page_size=20&sort=-created_at&q=&status=` → `{items,page,page_size,total}` | §9.1 |
| 时间 | 全部 UTC ISO8601（`2026-10-18T05:41:25.072Z`） | — |
| 模式 | `options.mode ∈ capability.modes`（`sync / async / stream`），省略取 `modes[0]` | §9.3 |

Shell 差异（同一串命令在三种终端里的写法）：

```bash
# bash / zsh / Git Bash
curl -sS "https://router.example.com/api/v1/health" \
  -H "Authorization: Bearer $SR_TOKEN"
```

```bat
:: cmd.exe —— 续行用 ^，引号用双引号并转义内部双引号
curl.exe -sS "https://router.example.com/api/v1/health" ^
  -H "Authorization: Bearer %SR_TOKEN%"
```

```powershell
# PowerShell —— 续行用反引号
curl.exe -sS "https://router.example.com/api/v1/health" `
  -H "Authorization: Bearer $env:SR_TOKEN"
```

## 1. 先拿到一个令牌（管理端，一次性）

令牌只在创建时**完整返回一次**（库里只存 `sha256(secret)` 与前缀）。

```bash
# 1.1 登录（拿 cookie + csrf）
curl -sS -c cookies.txt -X POST https://router.example.com/api/admin/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<你的密码>"}'
# → {"admin":{"id":"adm_...","username":"admin","must_change_password":false},"csrf_token":"..."}
```

```bash
# 1.2 （首次登录）先改密，否则除 auth/me、auth/change-password、auth/logout 外一律 403 PASSWORD_CHANGE_REQUIRED
curl -sS -b cookies.txt -X POST https://router.example.com/api/admin/auth/change-password \
  -H "Content-Type: application/json" -H "X-CSRF-Token: <csrf_token>" \
  -d '{"current_password":"adminjr","new_password":"<≥12位，含字母+数字>"}'
```

```bash
# 1.3 创建外部 Agent 与其令牌（token 与 install_command 只在这里出现一次）
curl -sS -b cookies.txt -X POST https://router.example.com/api/admin/access/agents/agt_1/tokens \
  -H "Content-Type: application/json" -H "X-CSRF-Token: <csrf_token>" \
  -d '{"name":"laptop-aragon"}'
# → {"token":"srk_k1_xxxxxxxx","install_command":"npx ./skillrouter-cli-0.1.0.tgz install --base-url ... --token <srk_...>"}
```

```bash
export SR_TOKEN="srk_k1_xxxxxxxx"
export SR_BASE="https://router.example.com"
```

## 2. `GET /api/v1/health`（无需鉴权）

```bash
curl -sS "$SR_BASE/api/v1/health"
```

```json
{"status":"ok","version":"0.1.0","engine":{"version":"0.2.34","status":"available"},"time":"2026-10-18T05:41:25Z"}
```

```ts
// TypeScript（Node ≥ 18，内置 fetch）
type Health = {
  status: 'ok' | 'degraded';
  version: string;
  engine: { version: string | null; status: 'available' | 'unavailable' | 'degraded' };
  time: string;
};

const health = (await (await fetch(`${base}/api/v1/health`)).json()) as Health;
if (health.engine.status !== 'available') {
  console.warn('引擎不可用（能力调用不受影响，Agent 面板会受影响）', health.engine);
}
```

```python
# Python（httpx）
import httpx

health = httpx.get(f"{SR_BASE}/api/v1/health", timeout=10).json()
print(health["status"], health["engine"])
```

## 3. `GET /api/v1/whoami`

令牌主体、作用域、配额余量。

```bash
curl -sS "$SR_BASE/api/v1/whoami" -H "Authorization: Bearer $SR_TOKEN"
```

```json
{
  "token": {"id": "tok_1", "prefix": "srk_k1_x", "agent": "laptop-aragon"},
  "scopes": {"capabilities": ["text.generate.*", "image.generate.kling"], "skills": ["*"]},
  "quota": {"rpm": 60, "daily_calls_used": 128, "daily_calls_limit": 5000,
            "monthly_budget_usd": 20.0, "monthly_spent_usd": 3.41}
}
```

```ts
const who = await fetch(`${base}/api/v1/whoami`, { headers: auth() }).then((r) => r.json());
console.log(who.quota.monthly_spent_usd, '/', who.quota.monthly_budget_usd);
```

```python
who = httpx.get(f"{SR_BASE}/api/v1/whoami", headers=AUTH, timeout=10).json()
print(who["quota"])
```

## 4. `GET /api/v1/capabilities`（目录，带 ETag）

```bash
# 4.1 首次：拿到 ETag 与目录
curl -sS -D headers.txt "$SR_BASE/api/v1/capabilities" -H "Authorization: Bearer $SR_TOKEN"
grep -i '^etag:' headers.txt

# 4.2 之后：带上 If-None-Match，未变化返回 304（省流量、省解析）
curl -sS -i "$SR_BASE/api/v1/capabilities" \
  -H "Authorization: Bearer $SR_TOKEN" \
  -H 'If-None-Match: "<上次的 etag>"'

# 4.3 强制刷新（新增能力后立即看到）
curl -sS "$SR_BASE/api/v1/capabilities?refresh=1" -H "Authorization: Bearer $SR_TOKEN"
```

```json
{
  "items": [
    {"key":"text.generate.gpt4o-mini","name":"GPT-4o mini 文本生成","kind":"text.generate",
     "modes":["sync","stream"],"version":"1.2.0","compatibility_group":"text.chat.openai-v1"}
  ],
  "total": 1,
  "etag": "W/\"cap-1287\""
}
```

```ts
// 带缓存的最小客户端：304 时复用上次结果
let cache: { etag?: string; items: any[] } = { items: [] };

async function listCapabilities(refresh = false): Promise<any[]> {
  const res = await fetch(`${base}/api/v1/capabilities${refresh ? '?refresh=1' : ''}`, {
    headers: { ...auth(), ...(cache.etag && !refresh ? { 'If-None-Match': cache.etag } : {}) },
  });
  if (res.status === 304) return cache.items;
  if (!res.ok) throw new Error(JSON.stringify(await res.json()));
  cache = { etag: res.headers.get('etag') ?? undefined, items: (await res.json()).items };
  return cache.items;
}
```

```python
caps = httpx.get(f"{SR_BASE}/api/v1/capabilities", headers=AUTH, timeout=20)
caps.raise_for_status()
for cap in caps.json()["items"]:
    print(cap["key"], cap["modes"])
```

## 5. `GET /api/v1/capabilities/{key}`（详情）

返回 `input_schema / output_schema / examples / limits / modes / file_io / version /
requires_agent_reasoning` 与 `routes_summary[{provider,priority,available}]`（**不含**上游 URL 与凭证）。

```bash
curl -sS "$SR_BASE/api/v1/capabilities/text.generate.gpt4o-mini" \
  -H "Authorization: Bearer $SR_TOKEN"
```

```python
detail = httpx.get(f"{SR_BASE}/api/v1/capabilities/text.generate.gpt4o-mini",
                   headers=AUTH, timeout=20).json()
print(detail["input_schema"]["required"])
print([r["provider"] for r in detail["routes_summary"]])
```

```ts
const detail = await fetch(`${base}/api/v1/capabilities/${encodeURIComponent('text.generate.gpt4o-mini')}`, {
  headers: auth(),
}).then((r) => r.json());
```

> 动手写输入之前先看 `input_schema`。`additionalProperties: false` 时，未声明字段必须放进
> `options.extensions[custom]`，否则返回 `400 INPUT_SCHEMA_INVALID` / `400 UNSUPPORTED_PARAM`。

## 6. `POST /api/v1/capabilities/{key}/invoke`

请求体（§9.3）：

```json
{
  "capability_version": "1.2.0",
  "input": {"prompt": "一只在雨中的柴犬"},
  "options": {
    "mode": "sync",
    "timeout_ms": 30000,
    "preferred_provider": "prv_openai",
    "max_cost_usd": 0.05,
    "idempotency_key": "agent-a-94f2",
    "extensions": {"openai": {"quality": "hd"}}
  }
}
```

### 6.1 同步（`mode: sync`）

```bash
curl -sS -X POST "$SR_BASE/api/v1/capabilities/text.generate.gpt4o-mini/invoke" \
  -H "Authorization: Bearer $SR_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: agent-a-94f2" \
  -d '{"input":{"prompt":"用一句话解释信封加密"},"options":{"mode":"sync","timeout_ms":30000}}'
```

```json
{
  "request_id": "req_01J9Z8W3",
  "status": "succeeded",
  "capability": {"key": "text.generate.gpt4o-mini", "version": "1.2.0"},
  "provider": {"id": "prv_openai", "name": "OpenAI"},
  "output": {"text": "……"},
  "output_files": [],
  "usage": {"unit": "tokens", "input": 812, "output": 96, "total": 908,
            "cost_usd": 0.0004, "source": "provider"},
  "timings": {"queue_ms": 4, "upstream_ms": 1180, "total_ms": 1233},
  "error": null
}
```

```ts
type InvokeOk = {
  request_id: string;
  status: 'succeeded';
  output: Record<string, unknown>;
  output_files: { file_id: string; mime: string; size: number }[];
  usage?: { unit: string; total?: number; cost_usd?: number; source: 'provider' | 'estimated' };
  timings?: { queue_ms?: number; upstream_ms?: number; total_ms?: number };
};

async function invokeSync(key: string, input: unknown): Promise<InvokeOk> {
  const res = await fetch(`${base}/api/v1/capabilities/${key}/invoke`, {
    method: 'POST',
    headers: { ...auth(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ input, options: { mode: 'sync', timeout_ms: 30000 } }),
  });
  const body = await res.json();
  if (!res.ok) {
    // 只用 code 分支，不要匹配 message（文案会变）
    const { code, hint, request_id } = body.error;
    throw Object.assign(new Error(`${code}: ${hint ?? ''}`), { code, request_id });
  }
  return body as InvokeOk;
}
```

```python
r = httpx.post(
    f"{SR_BASE}/api/v1/capabilities/text.generate.gpt4o-mini/invoke",
    headers={**AUTH, "Content-Type": "application/json", "Idempotency-Key": "agent-a-94f2"},
    json={"input": {"prompt": "用一句话解释信封加密"},
          "options": {"mode": "sync", "timeout_ms": 30000}},
    timeout=60,
)
if r.status_code >= 400:
    err = r.json()["error"]
    raise RuntimeError(f'{err["code"]} hint={err.get("hint")} request_id={err["request_id"]}')
print(r.json()["output"])
```

### 6.2 异步（`mode: async`）→ 202

请求内**不触达上游**（只做鉴权/校验/幂等/预算/配额/落库/入队），因此返回很快（§4.4(d)，§15.4 ≤1s）。

```bash
curl -sS -i -X POST "$SR_BASE/api/v1/capabilities/image.generate.kling/invoke" \
  -H "Authorization: Bearer $SR_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: img-0001" \
  -d '{"input":{"prompt":"雨中的柴犬，竖版"},"options":{"mode":"async"}}'
# HTTP/1.1 202 Accepted
# {"request_id":"req_01J9Z9AA","task_id":"tsk_01J9Z9AB","status":"queued"}
```

```python
sub = httpx.post(f"{SR_BASE}/api/v1/capabilities/image.generate.kling/invoke",
                 headers={**AUTH, "Content-Type": "application/json"},
                 json={"input": {"prompt": "雨中的柴犬，竖版"}, "options": {"mode": "async"}},
                 timeout=30)
sub.raise_for_status()
task_id = sub.json()["task_id"]
print("queued", task_id)
```

```ts
const sub = await fetch(`${base}/api/v1/capabilities/image.generate.kling/invoke`, {
  method: 'POST',
  headers: { ...auth(), 'Content-Type': 'application/json' },
  body: JSON.stringify({ input: { prompt: '雨中的柴犬，竖版' }, options: { mode: 'async' } }),
});
if (sub.status !== 202) throw new Error(`期望 202，实际 ${sub.status}`);
const { task_id } = await sub.json();
```

### 6.3 流式（`mode: stream`，SSE）

```bash
curl -sS -N -X POST "$SR_BASE/api/v1/capabilities/text.generate.gpt4o-mini/invoke" \
  -H "Authorization: Bearer $SR_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"input":{"prompt":"数到五"},"options":{"mode":"stream"}}'
```

事件统一为一行 `data:` JSON（§9.4）：`call.delta` / `call.usage` / `call.done` / `call.error`。

```
data: {"id":1287,"type":"call.delta","at":"2026-10-18T05:41:25.072Z","payload":{"chunk":"一"}}
data: {"id":1288,"type":"call.delta","at":"2026-10-18T05:41:25.113Z","payload":{"chunk":"、二"}}
data: {"id":1289,"type":"call.usage","at":"2026-10-18T05:41:25.400Z","payload":{"unit":"tokens","total":37}}
data: {"id":1290,"type":"call.done","at":"2026-10-18T05:41:25.402Z","payload":{"request_id":"req_01J9Z9C1","status":"succeeded"}}
```

```ts
// 解析 SSE：按空行分帧，忽略 ": keep-alive" 注释行
async function invokeStream(key: string, input: unknown, onEvent: (e: any) => void) {
  const res = await fetch(`${base}/api/v1/capabilities/${key}/invoke`, {
    method: 'POST',
    headers: { ...auth(), 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ input, options: { mode: 'stream' } }),
  });
  if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const data = frame
        .split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).trim())
        .join('');
      if (data) onEvent(JSON.parse(data));
    }
  }
}
```

```python
# httpx 流式：逐行读 data:
with httpx.stream("POST",
                  f"{SR_BASE}/api/v1/capabilities/text.generate.gpt4o-mini/invoke",
                  headers={**AUTH, "Content-Type": "application/json",
                           "Accept": "text/event-stream"},
                  json={"input": {"prompt": "数到五"}, "options": {"mode": "stream"}},
                  timeout=120) as resp:
    resp.raise_for_status()
    for line in resp.iter_lines():
        if not line or not line.startswith("data:"):
            continue          # 空行与 ": keep-alive" 注释都跳过
        event = json.loads(line[5:].strip())
        if event["type"] == "call.delta":
            print(event["payload"]["chunk"], end="", flush=True)
        elif event["type"] == "call.done":
            break
```

> 断线重连带 `Last-Event-ID` 可补事件（服务端从 `sse_events` 重放，重放不触发任何写操作，§9.4）。

### 6.4 文件输入 / 输出

```bash
# 1) 先上传，拿 file_id
curl -sS -X POST "$SR_BASE/api/v1/files" \
  -H "Authorization: Bearer $SR_TOKEN" \
  -F "file=@./ref.png;type=image/png"
# → {"file_id":"fil_01J9Z9D0","mime":"image/png","size":184320,"sha256":"...","expires_at":"..."}

# 2) 用 {"$file": ...} 引用它
curl -sS -X POST "$SR_BASE/api/v1/capabilities/image.generate.kling/invoke" \
  -H "Authorization: Bearer $SR_TOKEN" -H "Content-Type: application/json" \
  -d '{"input":{"prompt":"改成水彩风格","reference_image":{"$file":"fil_01J9Z9D0","mime":"image/png"}},
       "options":{"mode":"async"}}'
```

```python
with open("ref.png", "rb") as fh:
    up = httpx.post(f"{SR_BASE}/api/v1/files", headers=AUTH,
                    files={"file": ("ref.png", fh, "image/png")}, timeout=60)
up.raise_for_status()
file_id = up.json()["file_id"]

res = httpx.post(
    f"{SR_BASE}/api/v1/capabilities/image.generate.kling/invoke",
    headers={**AUTH, "Content-Type": "application/json"},
    json={"input": {"prompt": "改成水彩风格",
                    "reference_image": {"$file": file_id, "mime": "image/png"}},
          "options": {"mode": "async"}},
    timeout=30,
).json()
```

文件输出一律走 `output_files[{file_id, mime, size}]`，**不内联 base64**；下载：

```bash
curl -sS -L "$SR_BASE/api/v1/files/fil_01J9Z9D1" \
  -H "Authorization: Bearer $SR_TOKEN" -o out.png
# 预览可用签名 URL（10 分钟有效，无需令牌）：/api/v1/files/{id}?sig=...&exp=...
```

## 7. 任务：`GET /api/v1/tasks/{task_id}` / `POST .../cancel`

```bash
curl -sS "$SR_BASE/api/v1/tasks/tsk_01J9Z9AB" -H "Authorization: Bearer $SR_TOKEN"
```

```json
{"task_id":"tsk_01J9Z9AB","status":"running","progress":{"percent":42,"stage":"upstream_polling"},
 "upstream_task_id":"kling-9f2","created_at":"2026-10-18T05:42:00Z","error":null}
```

`status ∈ queued | running | succeeded | failed | canceling | canceled | canceled_local | status_unknown`

```bash
curl -sS -X POST "$SR_BASE/api/v1/tasks/tsk_01J9Z9AB/cancel" \
  -H "Authorization: Bearer $SR_TOKEN"
# 上游支持取消 → {"task_id":"...","status":"canceling","cancel_scope":"remote"}
# 上游不支持   → 409 CANCEL_UNSUPPORTED（本地已停止等待，远端可能仍在跑）
```

```python
import time

def wait_task(task_id: str, timeout_s: float = 900) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = httpx.get(f"{SR_BASE}/api/v1/tasks/{task_id}", headers=AUTH, timeout=30).json()
        if t["status"] in {"succeeded", "failed", "canceled", "canceled_local"}:
            return t
        time.sleep(3)
    raise TimeoutError(task_id)
```

```ts
async function waitTask(taskId: string, timeoutMs = 900_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const t = await fetch(`${base}/api/v1/tasks/${taskId}`, { headers: auth() }).then((r) => r.json());
    if (['succeeded', 'failed', 'canceled', 'canceled_local'].includes(t.status)) return t;
    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error(`task ${taskId} 超时`);
}
```

`status_unknown` 的含义：无法判定上游是否受理（例如超时发生在提交过程中）。
先 `POST /api/admin/tasks/{id}/reconcile` 对账，**不要**直接重投（可能产生重复扣费）。

## 8. SKILL：`GET /api/v1/skills` / `{name}` / `{name}/bundle`

```bash
# 目录（只返回 enabled=1 且 status=published 的 SKILL）
curl -sS "$SR_BASE/api/v1/skills" -H "Authorization: Bearer $SR_TOKEN"

# 正文（frontmatter + instructions + examples）
curl -sS "$SR_BASE/api/v1/skills/video-short-clip" -H "Authorization: Bearer $SR_TOKEN"

# 连接包（zip，不含任何密钥）
curl -sS -L "$SR_BASE/api/v1/skills/video-short-clip/bundle" \
  -H "Authorization: Bearer $SR_TOKEN" -o video-short-clip.zip
```

```json
{"items":[{"name":"video-short-clip","description":"用一个文本提示生成 5 秒竖版短视频",
           "version":"1.2.0","required_capabilities":["video.generate.kling-v2"]}],"total":1}
```

```python
import io, zipfile

bundle = httpx.get(f"{SR_BASE}/api/v1/skills/video-short-clip/bundle", headers=AUTH, timeout=60)
bundle.raise_for_status()
with zipfile.ZipFile(io.BytesIO(bundle.content)) as zf:
    print(zf.namelist())   # SKILL.md / invoke.mjs / skillrouter.config.json / README.md
```

安装到外部 Agent 请**不要**自己解压摆放，用 CLI（命令见 [../skills/connect.md](../skills/connect.md)）——
它负责目录探测、令牌安全存储与安装后验证。

## 9. MCP（Streamable HTTP，`POST /api/v1/mcp`）

协议版本 `2025-06-18`（也接受 `2025-03-26`）。`Mcp-Session-Id` 由 `initialize` 响应下发，后续请求必须带上。

```bash
# 9.1 initialize
curl -sS -D mcp-headers.txt -X POST "$SR_BASE/api/v1/mcp" \
  -H "Authorization: Bearer $SR_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"curl","version":"1.0"}}}'
# 响应体里 result.protocolVersion 为协商结果；响应头里带 Mcp-Session-Id
SID=$(grep -i '^mcp-session-id:' mcp-headers.txt | tr -d '\r' | awk '{print $2}')

# 9.2 notifications/initialized（无 id，服务端不回响应体）
curl -sS -X POST "$SR_BASE/api/v1/mcp" \
  -H "Authorization: Bearer $SR_TOKEN" -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# 9.3 tools/list —— 每个已授权能力一个工具，名字是 capability__<key 的 . 换成 _>
curl -sS -X POST "$SR_BASE/api/v1/mcp" \
  -H "Authorization: Bearer $SR_TOKEN" -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# 9.4 tools/call
curl -sS -X POST "$SR_BASE/api/v1/mcp" \
  -H "Authorization: Bearer $SR_TOKEN" -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"capability__text_generate_gpt4o_mini",
                 "arguments":{"input":{"prompt":"你好"},"options":{"mode":"sync"}}}}'
```

工具清单 = 每个已授权能力一个 + 五个元工具：`capabilities_list`、`skills_list`、`skills_read`、
`task_status`、`task_cancel`。

```json
{"jsonrpc":"2.0","id":3,"result":{
  "content":[{"type":"text","text":"{\"request_id\":\"req_...\",\"output\":{\"text\":\"你好！\"}}"}],
  "structuredContent":{"request_id":"req_...","output":{"text":"你好！"}},
  "isError":false}}
```

```python
def mcp_call(method: str, params: dict | None = None, req_id: int = 1, session: str | None = None):
    headers = {**AUTH, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    r = httpx.post(f"{SR_BASE}/api/v1/mcp", headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    sid = r.headers.get("mcp-session-id")
    # 服务端可能返回 SSE 帧，需要取最后一行 data:
    text = r.text.strip()
    body = json.loads(text.splitlines()[-1][5:].strip()) if text.startswith("event:") or "\ndata:" in text else r.json()
    return body, sid
```

> MCP 通道的**真实客户端**联调仍是「待验证」（附录 C 全部保持待验证）。
> 上面的命令只保证协议形状可复制，不等于已用 MCP 客户端跑通。

## 10. 错误处理（照 `code` 分支，不要匹配文案）

```ts
type ApiError = { code: string; message: string; hint?: string; retryable?: boolean; request_id: string };

export function handleError(status: number, body: any): never {
  const e: ApiError = body?.error ?? { code: 'UNKNOWN', message: 'non-json error', request_id: '' };
  switch (e.code) {
    case 'TOKEN_REVOKED':
    case 'SESSION_EXPIRED':
      throw new Error(`需要重新认证：${e.code}（request_id=${e.request_id}）`);
    case 'TOKEN_SCOPE_DENIED':
      throw new Error(`令牌作用域不足：${e.code}`);
    case 'QUOTA_EXCEEDED':
    case 'BUDGET_EXCEEDED':
    case 'UPSTREAM_RATE_LIMIT':
      // retryable 时按退避重试；否则交给人工
      throw Object.assign(new Error(e.message), { retryable: e.retryable, code: e.code });
    case 'INPUT_SCHEMA_INVALID':
    case 'UNSUPPORTED_PARAM':
    case 'UNSUPPORTED_MODE':
      throw new Error(`请求不符合能力契约：${JSON.stringify((e as any).details ?? {})}`);
    case 'NOT_INITIALIZED':
      throw new Error('平台未初始化：先运行 scripts/bootstrap.py');
    default:
      throw new Error(`${status} ${e.code}: ${e.message} (request_id=${e.request_id})`);
  }
}
```

```python
CODE_ACTIONS = {
    "NOT_INITIALIZED": "运行 scripts/bootstrap.py 完成初始化",
    "PASSWORD_CHANGE_REQUIRED": "先完成强制改密",
    "SESSION_EXPIRED": "重新登录",
    "TOKEN_REVOKED": "令牌已撤销，请管理员重新签发",
    "TOKEN_SCOPE_DENIED": "令牌作用域不足，请管理员扩大作用域",
    "QUOTA_EXCEEDED": "等待窗口重置或调整配额",
    "BUDGET_EXCEEDED": "调整月预算",
    "INPUT_SCHEMA_INVALID": "按 details.fields 修正输入",
    "UPSTREAM_RATE_LIMIT": "降低并发或稍后重试",
    "UPSTREAM_TIMEOUT": "查任务状态，勿重复提交",
    "TASK_STATUS_UNKNOWN": "调用对账接口，不要重投",
}

def explain(err: dict) -> str:
    e = err["error"]
    return f'{e["code"]}: {e.get("hint") or CODE_ACTIONS.get(e["code"], "")} (request_id={e["request_id"]})'
```

完整错误码表见 [runbook/troubleshooting.md](../runbook/troubleshooting.md)（与 spec 附录 A 一致）。

## 11. OpenAPI

`docs/api/openapi.json` 由 CI 从 FastAPI 应用导出（不是手写）：

```bash
# 本地导出（apps/api 下）
python -c "import json,skillrouter_api.main as m;print(json.dumps(m.create_app().openapi(),ensure_ascii=False,indent=2))" > ../docs/api/openapi.json
```

契约测试会断言生成物与该文件**无 diff**，避免文档与实现漂移（§15.1）。
