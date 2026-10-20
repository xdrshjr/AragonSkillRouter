# 接口示例（`/api/v1`）

> 目标是**可复制即用**。示例里的 `https://router.example.com`、`srk_xxx_yyy`、能力 key
> 都是占位符，替换成你自己的值即可。
> 示例覆盖：health、whoami、capabilities（list/get）、invoke（sync / async / stream）、
> task status / cancel、file upload + 签名下载、skills（list / read / bundle）、MCP（initialize / tools.list / tools.call）、
> 管理端 tasks 列表与备份（POST）、管理端 LLM 服务配置（协议注册表 / 实时连通 / 取模型 / 探测历史）。
>
> **令牌只用请求头**：`Authorization: Bearer <token>`。把令牌放进查询串的旧通道（`token` 查询参数）
> 默认关闭，传了会返回 `400 TOKEN_INVALID`；逃生阀 `SKILLROUTER_ALLOW_QUERY_TOKEN` 见 §0 表格最后一行。

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
| 令牌位置 | **只在请求头**。查询串通道默认关闭（`400 TOKEN_INVALID`）；`SKILLROUTER_ALLOW_QUERY_TOKEN=1` 才恢复，且每次用都会记一条 `query_token_used` 告警 | §A.3.3 |
| 维护模式 | 恢复备份期间所有写请求与 invoke 返回 `503 MAINTENANCE`；`GET /healthz` 的 `maintenance` 为 `true` | §A.3.13 |

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
# 运维探针用 /healthz（同域、不带 /api/v1），额外给 db/initialized/maintenance 三个字段
curl -sS "$SR_BASE/healthz"
```

```json
{"status":"ok","version":"0.1.0",
 "engine":{"version":"0.2.34","status":"declared","note":"本端点是声明值，实际以 /settings/engine 为准"},
 "schema_version":3,"time":"2026-10-18T05:41:25Z"}
```

```json
// GET /healthz
{"ok":true,"service":"skillrouter-api","version":"0.1.0","env":"production",
 "db":{"ok":true,"schema_version":3,"error":null},"initialized":true,
 "engine_pin":"0.2.34@0a119a6","maintenance":false,"time":"2026-10-18T05:41:25Z"}
```

> `status` 只有 `ok` / `initializing`；`maintenance: true` 表示正在恢复备份，此时写请求与
> invoke 都会 `503 MAINTENANCE`（读请求放行）。

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
  "token_id": "tok_1",
  "agent": {"id": "agt_1", "name": "laptop-aragon"},
  "prefix": "srk_k1_x",
  "scopes": {"capabilities": ["text.generate.*", "image.generate.kling"], "skills": ["*"]},
  "allowed_modes": ["sync", "async"],
  "limits": {"rpm": 60, "concurrency": 4, "daily_calls": 5000, "budget_usd": 20.0},
  "expires_at": null,
  "usage": {"calls_today": 128, "cost_month_usd": 3.41, "calls_this_minute": 2, "concurrent": 1}
}
```

> `limits` 是**配置**，`usage` 是**实际用量**（读 `quota_counters` / `rate_counters`）。
> 成本只在 `quota_counters.cost_usd` 上累计，`rate_counters` 只记 `calls`/`concurrent`。

```ts
const who = await fetch(`${base}/api/v1/whoami`, { headers: auth() }).then((r) => r.json());
console.log(who.usage.cost_month_usd, '/', who.limits.budget_usd);
```

```python
who = httpx.get(f"{SR_BASE}/api/v1/whoami", headers=AUTH, timeout=10).json()
print(who["usage"], who["limits"])
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
  "status_unknown": false,
  "needs_reconcile": false,
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

`status ∈ {succeeded, failed, status_unknown}`（§A.10）。两个布尔字段是**给机器的**判定位，不要用它们
去猜 `status` 之外的东西：

| 字段 | 含义 | 你该做什么 |
|---|---|---|
| `status_unknown: true` | 无法判定上游是否受理（超时发生在**提交过程中**），`status` 同时是 `status_unknown` | **不要重投**（可能重复扣费）：先 `POST /api/admin/tasks/{id}/reconcile` |
| `needs_reconcile: true` | 本次调用被标记为需要人工对账（`call_logs.needs_reconcile=1`），概览页「待对账调用」会显示 | 同上；确认上游无记录后再重发 |

失败样例（`status: failed`，`error` 为错误映射后的对象，`retryable` 决定能否重试）：

```json
{"request_id":"req_01J9Z8X1","status":"failed","status_unknown":false,"needs_reconcile":false,
 "capability":{"key":"text.generate.gpt4o-mini","version":"1.2.0"},
 "provider":{"id":"prv_openai","name":"OpenAI"},"output":null,"output_files":[],
 "usage":null,"timings":{"queue_ms":3,"upstream_ms":30021,"total_ms":30024},
 "error":{"code":"UPSTREAM_TIMEOUT","message":"上游超时","hint":"查任务状态，勿重复提交","retryable":true}}
```

```ts
type InvokeResult = {
  request_id: string;
  // 注意：三态。`status_unknown` 不是失败，不要在这里重试。
  status: 'succeeded' | 'failed' | 'status_unknown';
  status_unknown: boolean;
  needs_reconcile: boolean;
  output: Record<string, unknown> | null;
  // 每个文件都带 10 分钟有效的签名 URL（下载不需要令牌）
  output_files: { file_id: string; mime: string; size: number; signed_url?: string; expires_at?: number }[];
  usage?: { unit: string; total?: number; cost_usd?: number; source: 'provider' | 'estimated' | 'unknown' | 'probe' } | null;
  timings?: { queue_ms?: number; upstream_ms?: number; total_ms?: number };
  error: { code: string; message: string; hint?: string; retryable?: boolean } | null;
};

async function invokeSync(key: string, input: unknown): Promise<InvokeResult> {
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
  const result = body as InvokeResult;
  if (result.status === 'status_unknown' || result.needs_reconcile) {
    // 结果不确定：交给对账接口，绝不自动重投（重复扣费风险）
    throw Object.assign(new Error(`结果不确定，请对账：${result.request_id}`), {
      code: 'STATUS_UNKNOWN',
      request_id: result.request_id,
    });
  }
  return result;
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
# 1) 先上传，拿 file_id + 可直接用的签名 URL（10 分钟有效、下载免令牌）
curl -sS -X POST "$SR_BASE/api/v1/files" \
  -H "Authorization: Bearer $SR_TOKEN" \
  -F "file=@./ref.png;type=image/png"
# → {"file_id":"fil_01J9Z9D0","mime":"image/png","size":184320,"sha256":"...",
#    "filename":"ref.png","kind":"upload","expires_at":null,"created_at":"2026-10-18T05:41:25Z",
#    "signed_url":"/api/v1/files/fil_01J9Z9D0?sig=<hmac>&exp=1792300885"}

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

文件输出一律走 `output_files[{file_id, mime, size, signed_url, expires_at}]`，**不内联 base64**。
两种下载方式（先看有没有签名链接，再考虑令牌）：

```bash
# 方式 A（推荐给浏览器/前端 <img>/<a>）：签名 URL，**不需要任何令牌**
#   signed_url 是相对路径，前面拼上 $SR_BASE 即可
curl -sS -L "$SR_BASE$(echo '<signed_url>')" -o out.png

# 方式 B（服务端调用）：带平台令牌，需要 files:read 作用域
curl -sS -L "$SR_BASE/api/v1/files/fil_01J9Z9D1" \
  -H "Authorization: Bearer $SR_TOKEN" -o out.png
```

签名的三个坑（§A.3.10）：

| 现象 | 原因 | 处置 |
|---|---|---|
| `401 UNAUTHENTICATED` + hint「链接已过期」 | 签名 URL 有效期只有 **10 分钟**（`exp` 是 Unix 秒） | 重新取一次 `output_files[].signed_url`（任务详情/调用响应里都有），或改用令牌 |
| `401 UNAUTHENTICATED` + hint「缺少 Authorization 头」 | 没带签名也没带令牌 | 二选一 |
| `403 TOKEN_SCOPE_DENIED` | 令牌没有 `files:read` | 管理端给令牌加作用域；或改用签名 URL |

> 以前无效/过期签名返回 `404`，会被误读成「文件不存在」；本轮统一为 **401** 并在 hint 里区分原因。

## 7. 任务：`GET /api/v1/tasks/{task_id}` / `POST .../cancel`

```bash
curl -sS "$SR_BASE/api/v1/tasks/tsk_01J9Z9AB" -H "Authorization: Bearer $SR_TOKEN"
```

```json
{"task_id":"tsk_01J9Z9AB","request_id":"req_01J9Z9AA","capability_key":"image.generate.kling",
 "mode":"async","status":"running","status_detail":"上游处理中",
 "progress":{"percent":42,"stage":"upstream_polling"},"upstream_task_id":"kling-9f2",
 "attempts":0,"max_attempts":3,"failure_class":null,"cancel_capability":"remote",
 "cancel_requested_at":null,"cancel_scope":"none","resume_policy":"poll",
 "error":null,"result":{},"output_files":[],
 "created_at":"2026-10-18T05:42:00Z","updated_at":"2026-10-18T05:42:31Z","finished_at":null}
```

`status ∈ queued | running | succeeded | failed | canceling | canceled | canceled_local | status_unknown`
（`status_unknown` 表示无法判定上游是否受理，见 §6.1 的对账说明）。

取消语义（§A.10 / §A.3.6）：`cancel_scope ∈ {none, remote, local_only, pending_submit}`，
**`pending_submit` 时 HTTP 状态码是 `202`**（其余情况 `200`）：

```bash
curl -sS -i -X POST "$SR_BASE/api/v1/tasks/tsk_01J9Z9AB/cancel" \
  -H "Authorization: Bearer $SR_TOKEN"

# A) 上游已受理 → 200，真的能停
# {"ok":true,"task_id":"tsk_01J9Z9AB","status":"canceling","cancel_scope":"remote",
#  "cancel_requested_at":"2026-10-18T05:43:02Z"}
# B) 上游不支持取消 → 409 CANCEL_UNSUPPORTED（本地已停止等待，远端可能仍在跑）
# C) 子任务刚起、还没提交给上游 → 202，按「提交前取消」处理
# HTTP/1.1 202 Accepted
# {"ok":true,"status":"canceling","cancel_scope":"pending_submit",
#  "cancel_requested_at":"2026-10-18T05:43:02Z","note":"<说明：本地已受理取消，提交中的请求会被追取消>"}
# D) 只有本地可停 → 200，直接到 canceled + cancel_scope="local_only"（带 upstream_task_id 供人工追）
# E) 已经终结 → 200 {"ok":false,"status":"succeeded","cancel_scope":"none"}
```

| `cancel_scope` | 语义 | 客户端该做什么 |
|---|---|---|
| `none` | 没有可取消的东西（已终结） | 不用再轮询 |
| `remote` | 已向上游发起取消 | 继续轮询到 `canceled` |
| `local_only` | 上游不支持/不允许取消，本地已停止等待 | 上游可能仍在跑并计费，看 `provider` 账单 |
| `pending_submit` | 提交前拦下（**202**） | 等 `canceled` 即可，不会再打上游 |

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

### 7.1 管理端：`GET /api/admin/tasks`（列表参数，§A.3.11）

```bash
# 组合：按状态 + 能力 + 关键字搜 request_id/task_id/capability_key/upstream_task_id + 排序 + 翻页
curl -sS -b cookies.txt \
  "$SR_BASE/api/admin/tasks?status=unknown&capability_key=image.generate.kling&q=kling-&page=1&page_size=20&sort=-created_at"
```

| 参数 | 取值 | 说明 |
|---|---|---|
| `q` | 任意子串 | 在 `request_id` / `task_id` / `capability_key` / `upstream_task_id` 上做子串匹配（不区分大小写） |
| `status` | `active` / `unknown` / 具体状态 | `active` = `queued\|running\|canceling` 的合集；`unknown` = `status_unknown`（概览页「待对账调用」就是它） |
| `capability_key` | 能力 key | 旧参数 `capability` 仍兼容，二者同值 |
| `page` / `page_size` | ≥1 | 返回 `{items,page,page_size,total}` |
| `sort` | `created_at` / `updated_at` / `status` | 前加 `-` 表示倒序；**只接受这三个字段**，其余回落到默认值（不做「假排序」） |

```bash
# 对账：status_unknown 的行用它收口（不会重投上游）
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/tasks/tsk_01J9Z9AB/reconcile" \
  -H "X-CSRF-Token: <csrf_token>"
```

### 7.2 管理端备份：`POST /api/admin/settings/backup/now`（同步，立即产出）

```bash
# 旧接口：POST /api/admin/settings/backup（202，交给 worker 排队）
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/settings/backup" \
  -H "X-CSRF-Token: <csrf_token>"
# → {"queued":true,"queue_id":"...","note":"<说明：由 worker 异步执行>"}

# 新接口：POST /api/admin/settings/backup/now（同步执行，返回真实产物名与 sha256）
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/settings/backup/now" \
  -H "X-CSRF-Token: <csrf_token>"
```

```json
{"ok":true,"product":"skillrouter-backup-20261018T054125Z.tar.gz","file_id":"fil_...",
 "sha256":"9f2c…","size":1843200,"path":"/data/backup/skillrouter-backup-20261018T054125Z.tar.gz",
 "manifest":{"schema_version":3,"files":[...]},"download_url":"/api/admin/files/fil_..."}
```

> **旧 `GET /api/admin/settings/backup/now` 已删除**，`GET` 现在返回 `405`：备份是写操作，
> 必须 POST + `X-CSRF-Token`（防「一个链接就触发备份」）。

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

## 10. 管理端 LLM 服务配置（四协议 · 实时测试 · 一键取模型）

四个 `/api/admin/llm/*` 新端点 + 一条「凭证 → 模型清单」。**协议差异只在后端**
（`apps/api/skillrouter_api/services/llm/protocols.py`）——前端只渲染注册表返回，
不要写 `if protocol === 'anthropic'`。

```bash
export SR_BASE="https://router.example.com"   # 本机开发：http://127.0.0.1:8000
export CSRF="<登录响应里的 csrf_token>"          # 写方法必须带，否则 403 CSRF_INVALID
```

| 端点 | 用途 | 计费 |
|---|---|---|
| `GET /api/admin/llm/protocols` | 协议注册表（四张卡片的唯一数据源） | 否 |
| `POST /api/admin/llm/test-connection` | 实时连通测试（`auth` / `completion` / `both`） | `completion` / `both` 计费 1 次 |
| `POST /api/admin/llm/models` | 取模型清单（归一化 + TTL 缓存 + 降级） | 否 |
| `GET /api/admin/credentials/{credential_id}/models` | 用**已保存**凭证取清单（协议可覆盖） | 否 |
| `GET /api/admin/llm/probes` | 探测历史（列表项已含完整 `stages`） | 否 |

`test-connection` / `models` 的请求体里，凭证二选一：`credential_id`（已保存）
**或** `secret`（草稿，≤8192 字符，只在本次请求的进程内存里用，不落库不落日志）。
两者都给 / 都不给都是 `400 VALIDATION_FAILED`。

### 10.1 `GET /api/admin/llm/protocols`（协议注册表）

```bash
curl -sS -b cookies.txt "$SR_BASE/api/admin/llm/protocols"
```

```json
{
  "items": [
    {"key":"openai","label":"OpenAI","label_zh":"OpenAI","one_liner":"Chat Completions 与 /models",
     "wire":"openai","default_base_url":"https://api.openai.com/v1",
     "base_url_hint":"https://api.openai.com/v1（含 /v1）","docs_url":"https://platform.openai.com/docs",
     "auth_styles":["bearer"],"default_auth_style":"bearer",
     "models_path":"models","chat_path":"chat/completions","supports_models_list":true},
    {"key":"anthropic","label":"Anthropic","label_zh":"Anthropic","one_liner":"Messages API（Claude 家族）",
     "wire":"anthropic","default_base_url":"https://api.anthropic.com",
     "base_url_hint":"https://api.anthropic.com（不要带 /v1）","docs_url":"https://docs.anthropic.com",
     "auth_styles":["api_key"],"default_auth_style":"api_key",
     "models_path":"/v1/models","chat_path":"/v1/messages","supports_models_list":true},
    {"key":"google","label":"Google","label_zh":"Google","one_liner":"Gemini generateContent",
     "wire":"google","default_base_url":"https://generativelanguage.googleapis.com",
     "base_url_hint":"https://generativelanguage.googleapis.com（不要带 /v1beta）",
     "docs_url":"https://ai.google.dev/api","auth_styles":["api_key"],"default_auth_style":"api_key",
     "models_path":"/v1beta/models","chat_path":"/v1beta/models/{model}:generateContent",
     "supports_models_list":true},
    {"key":"custom","label":"Custom","label_zh":"自定义",
     "one_liner":"任意兼容网关（vLLM / LiteLLM / One-API / 自建）",
     "wire":null,"default_base_url":"","base_url_hint":"https://your-gateway/v1","docs_url":"",
     "auth_styles":["bearer","api_key","basic","header","none"],"default_auth_style":"bearer",
     "models_path":null,"chat_path":null,"supports_models_list":true,
     "paths_inherit_from_wire":true,"paginate":null}
  ],
  "wires": ["openai","anthropic","google"]
}
```

> `custom` 的 `models_path` / `chat_path` 回 **`null`**：它的默认路径**继承所选 `wire`**
> （`wire=anthropic` → 取模型 `/v1/models`、完成 `/v1/messages`），由后端 `effective_paths()` 现算。
> `supports_models_list` 仍然是 `true` —— `null` **不等于**「这个网关没有模型清单」。

```ts
type LlmProtocolSpec = {
  key: 'openai' | 'anthropic' | 'google' | 'custom';
  label: string; label_zh: string; one_liner: string;
  wire: 'openai' | 'anthropic' | 'google' | null;
  default_base_url: string; base_url_hint: string; docs_url: string;
  auth_styles: string[]; default_auth_style: string;
  models_path: string | null; chat_path: string | null;
  supports_models_list: boolean;
  paths_inherit_from_wire?: boolean;
};

const protocols = (await (await fetch(`${base}/api/admin/llm/protocols`)).json()) as {
  items: LlmProtocolSpec[];
  wires: string[];
};
// 四张协议卡片 = protocols.items；地址框 placeholder = base_url_hint
const card = protocols.items.find((p) => p.key === 'anthropic')!;
```

### 10.2 `POST /api/admin/llm/test-connection`（实时连通，草稿也能测）

`mode` 决定花不花钱：`auth`（**默认，不计费**，只 `GET` 模型清单）/ `completion`（一次最小生成，
计费 1 次）/ `both`（先 auth，成功才跑 completion）。

```bash
# 10.2.1 主按钮「测试连接」：草稿密钥也能测，不计费（mode 缺省即 auth）
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/llm/test-connection" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"protocol":"anthropic","secret":"sk-ant-xxx","base_url":"https://api.anthropic.com"}'

# 10.2.2 「测试生成（计费 1 次）」：显式 mode=both，必须带 model
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/llm/test-connection" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"protocol":"anthropic","secret":"sk-ant-xxx","base_url":"https://api.anthropic.com",
       "mode":"both","model":"claude-sonnet-4-5"}'

# 10.2.3 用已保存的凭证测（不传 secret，避免密钥第二次经过前端）
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/llm/test-connection" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"protocol":"openai","credential_id":"crd_01J9Z9AB","mode":"auth"}'

# 10.2.4 自定义网关：地址带子路径，线协议与认证方式自己选
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/llm/test-connection" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"protocol":"custom","wire":"openai","base_url":"https://gw.internal/openai",
       "secret":"gw-xxxx","auth_style":"bearer","mode":"auth"}'
```

成功（`mode=both`）：

```json
{
  "ok": true,
  "protocol": "anthropic",
  "wire": "anthropic",
  "base_url": "https://api.anthropic.com",
  "resolved_url": "https://api.anthropic.com/v1/models?limit=1000",
  "mode": "both",
  "stages": [
    {"stage": "auth", "ok": true, "http_status": 200, "latency_ms": 212,
     "detail": "GET …/v1/models → 200，识别到 12 个模型", "model_count": 12},
    {"stage": "completion", "ok": true, "http_status": 200, "latency_ms": 640,
     "model": "claude-sonnet-4-5", "detail": "模型正常回话", "text_chars": 2}
  ],
  "probe_id": "llmp_9f3c1a2b7d4e",
  "checked_at": "2026-02-11T08:31:07Z",
  "problems": [],
  "credential": {"id": "crd_…", "name": "anthropic-prod", "secret_mask": "sk-…-9f2c"},
  "inferred": false,
  "notes": []
}
```

**失败也算一次成功调用**（HTTP `200` + `ok:false`，不是 500）——「上游拒绝鉴权」和
「模型名写错」必须能被页面上的人话解释清楚：

```json
{
  "ok": false, "protocol": "google", "wire": "google",
  "resolved_url": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
  "stages": [{"stage": "auth", "ok": false, "http_status": 403, "latency_ms": 188,
              "detail": "{\"error\":{\"code\":403,\"message\":\"API key not valid\"}}"}],
  "error_code": "UPSTREAM_AUTH",
  "hint": "上游拒绝鉴权：检查 API key 是否属于该项目，或该 key 是否启用了 Generative Language API",
  "problems": ["鉴权阶段失败，未发出计费请求"]
}
```

`auth` 失败时 `completion` 阶段**不执行**（记「未执行（鉴权未通过）」），所以 `mode=both`
在鉴权失败时不会产生任何计费请求。

| HTTP | code | 何时 |
|---|---|---|
| 400 | `VALIDATION_FAILED` | `secret` 与 `credential_id` 同时给/都不给、`custom` 缺 `base_url`、`mode` 含 completion 却没模型名、非 `custom` 协议给了 `wire` |
| 400 | `LLM_PROTOCOL_UNSUPPORTED` | `protocol` 不在 `openai\|anthropic\|google\|custom` 内 |
| 400 | `SSRF_BLOCKED` | 地址不是 http(s)、缺主机名，或命中云元数据地址（`netpolicy` 硬编码拒绝；**不读** `SKILLROUTER_SSRF_DENY`，管理员配置的上游允许私网与回环） |
| 404 | `NOT_FOUND` | `credential_id` 指向的凭证不存在或已删除 |
| 409 | `CREDENTIAL_DISABLED` | 凭证不是 `active` |
| 429 | `LLM_PROBE_THROTTLED` | 同一身份超过 10 次/60 秒；`details.retry_after_s` 给等待秒数 |
| 502 | `UPSTREAM_AUTH` / `UPSTREAM_INVALID_RESPONSE` | 上游 401/403；或上游 200 但响应里没有模型数组 / 不是该协议的完成结构 |
| 503 | `UPSTREAM_UNAVAILABLE` | 连不上（含直连回退后仍失败） |
| 504 | `UPSTREAM_TIMEOUT` | 超时（`auth` 20s / `completion` 45s） |

```ts
async function testConnection(body: Record<string, unknown>) {
  const res = await fetch(`${base}/api/admin/llm/test-connection`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
    body: JSON.stringify({ mode: 'auth', ...body }),   // 默认不计费
  });
  const data = await res.json();
  if (!res.ok) throw new Error(`${data.error.code}: ${data.error.hint ?? data.error.message}`);
  // 200 也可能 ok=false：结论在 error_code + hint 里，不在 HTTP 状态码里
  if (!data.ok) console.warn(data.error_code, data.hint, data.stages);
  return data as { ok: boolean; stages: { stage: string; http_status: number; latency_ms: number }[] };
}
```

### 10.3 `POST /api/admin/llm/models`（一键取模型）

请求 = `ProbeTarget`（`protocol` + `credential_id`/`secret` + `base_url` …）+ 下面三个参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `refresh` | `false` | `true` = 跳过缓存直打上游；`false` = 命中未过期缓存就不发请求 |
| `include_non_llm` | `false` | `true` 时连 embedding / audio / image 一起返回 |
| `limit` | `500` | 上限 500，超出置 `truncated=true` |

```bash
# 首次取（走上游）：
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/llm/models" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"protocol":"anthropic","secret":"sk-ant-xxx","base_url":"https://api.anthropic.com","refresh":true}'

# 之后取（命中 TTL 缓存，10 分钟内的默认行为）：
curl -sS -b cookies.txt -X POST "$SR_BASE/api/admin/llm/models" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"protocol":"anthropic","secret":"sk-ant-xxx","base_url":"https://api.anthropic.com"}'
```

```json
{
  "items": [
    {"id":"claude-sonnet-4-5","display_name":"Claude Sonnet 4.5","kind":"chat","recommended":true,
     "context_window":null,"methods":[],"created_at":"2025-09-29T00:00:00Z","owned_by":null},
    {"id":"claude-haiku-4-5","display_name":"Claude Haiku 4.5","kind":"chat","recommended":true,
     "context_window":null,"methods":[],"created_at":"2025-10-01T00:00:00Z","owned_by":null}
  ],
  "count": 2,
  "source": "upstream",
  "fetched_at": "2026-02-11T08:31:07Z",
  "expires_at": "2026-02-11T08:41:07Z",
  "truncated": false,
  "probe_id": "llmp_9f3c1a2b7d4e",
  "notes": ["已从地址尾部去掉重复的版本段 /v1"],
  "warning": null
}
```

> `source` 有四种取值：`upstream`（这次真打了上游）/ `cache`（未过期缓存）/
> `cache-stale`（上游失败，展示 <24h 的旧值，**200 + warning**）/
> 无旧值可降级 → `503 UPSTREAM_UNAVAILABLE`（`details.upstream` 给脱敏摘要）。
> `kind` 只有 `chat / embedding / audio / image / rerank / other`；`recommended` 只是**排序**提示，
> 不代表可用来做对话。

```ts
type LlmModelEntry = { id: string; display_name: string; kind: string; recommended: boolean;
  context_window: number | null; methods: string[] };

async function fetchModels(protocol: string, credentialId: string, refresh = false) {
  const res = await fetch(`${base}/api/admin/llm/models`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
    body: JSON.stringify({ protocol, credential_id: credentialId, refresh }),
  });
  if (res.status === 503) throw new Error('上游不可用，且没有可降级的旧清单');
  const data = await res.json();
  if (data.warning) console.warn(data.warning);   // cache-stale 时提醒「展示的是旧清单」
  return (data.items as LlmModelEntry[]).filter((m) => m.kind === 'chat');
}
```

### 10.4 `GET /api/admin/credentials/{credential_id}/models`（已保存凭证 → 立刻有清单）

`ProbeTarget` 由凭证推导（协议取 `meta.llm_protocol`，缺省按提供商名推断并在响应 `notes` 里说明）。

```bash
# 默认读缓存（refresh 默认 false）
curl -sS -b cookies.txt "$SR_BASE/api/admin/credentials/crd_01J9Z9AB/models"

# 强制刷新；协议可覆盖（例如同一把钥匙挂在「自定义」提供商下但走 anthropic 线协议）
curl -sS -b cookies.txt \
  "$SR_BASE/api/admin/credentials/crd_01J9Z9AB/models?refresh=1&protocol=anthropic"
```

响应与 §10.3 完全一致（同一套归一化与缓存）。

> 凭证不是 `active` → `409 CREDENTIAL_DISABLED`；不存在 → `404 NOT_FOUND`；
> `llm` 类凭证的「检测」也走同一条协议感知探测（`GET {base}/v1/models` 而不是 `GET {base}/models`），
> 且**不计费**。

### 10.5 `GET /api/admin/llm/probes`（探测历史）

`page` / `page_size`（≤100）→ `{items, page, page_size, total}`；`credential_id` 可筛选。

```bash
# 最近 10 条（按时间倒序；列表项已带完整 stages，前端「点开」是纯就地展开）
curl -sS -b cookies.txt "$SR_BASE/api/admin/llm/probes?page=1&page_size=10"

# 只看某张凭证的探测
curl -sS -b cookies.txt "$SR_BASE/api/admin/llm/probes?credential_id=crd_01J9Z9AB&page_size=20"
```

```json
{
  "items": [
    {"id":"llmp_9f3c1a2b7d4e","protocol":"anthropic","wire":"anthropic",
     "base_url":"https://api.anthropic.com","mode":"both","ok":true,
     "stages":[{"stage":"auth","ok":true,"http_status":200,"latency_ms":212,"model_count":12},
               {"stage":"completion","ok":true,"http_status":200,"latency_ms":640,"text_chars":2}],
     "http_status":200,"latency_ms":852,"error_code":null,"detail_redacted":null,
     "credential_id":"crd_01J9Z9AB","at":"2026-02-11T08:31:07Z"}
  ],
  "page": 1, "page_size": 10, "total": 1
}
```

> **没有** `GET /api/admin/llm/probes/{probe_id}`：一次探测最多两个阶段，列表项已经全带了。
> `probe_id` 的用途是和 `var/logs` 里的 `llm_probe_*` 事件、`audit_logs` 的 `llm.probe` 行对上号。
> `detail` / `detail_redacted` 都已脱敏；草稿探测的 `credential_id` 是 `null`（不回显任何密钥）。

## 11. 错误处理（照 `code` 分支，不要匹配文案）

```ts
type ApiError = { code: string; message: string; hint?: string; retryable?: boolean; request_id: string };

export function handleError(status: number, body: any): never {
  const e: ApiError = body?.error ?? { code: 'UNKNOWN', message: 'non-json error', request_id: '' };
  switch (e.code) {
    case 'TOKEN_REVOKED':
    case 'SESSION_EXPIRED':
      throw new Error(`需要重新认证：${e.code}（request_id=${e.request_id}）`);
    case 'TOKEN_INVALID':
      // 令牌只在 Authorization 头里；查询串通道默认关闭（§A.3.3）
      throw new Error(`令牌无效或位置不对：请用 -H "Authorization: Bearer <token>"`);
    case 'TOKEN_SCOPE_DENIED':
      throw new Error(`令牌作用域不足：${e.code}`);
    case 'MAINTENANCE':
      // 平台正在恢复备份：写操作被暂停，退避后重试（读请求照常）
      throw Object.assign(new Error(`维护中：${e.hint ?? ''}`), { retryable: true, code: e.code });
    case 'IDEMPOTENCY_IN_PROGRESS':
      // 同键同体已有请求在飞：等它结束（用同键重试是安全的，不要换键）
      throw Object.assign(new Error('同一幂等键的请求正在处理中，请稍后用同一键重试'), {
        retryable: true,
        code: e.code,
      });
    case 'LEASE_LOST':
      // 任务租约被其他 worker 接手：不是失败，任务会被重新执行
      throw Object.assign(new Error('任务处理权已转移，稍后查任务状态'), { code: e.code });
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
    "TOKEN_INVALID": "令牌只在 Authorization 头里（查询串通道默认关闭），检查是否带了头、是否拼错",
    "TOKEN_REVOKED": "令牌已撤销，请管理员重新签发",
    "TOKEN_SCOPE_DENIED": "令牌作用域不足，请管理员扩大作用域",
    "QUOTA_EXCEEDED": "等待窗口重置或调整配额",
    "BUDGET_EXCEEDED": "调整月预算",
    "INPUT_SCHEMA_INVALID": "按 details.fields 修正输入",
    "UPSTREAM_RATE_LIMIT": "降低并发或稍后重试",
    "UPSTREAM_TIMEOUT": "查任务状态，勿重复提交",
    "TASK_STATUS_UNKNOWN": "调用对账接口，不要重投",
    "IDEMPOTENCY_IN_PROGRESS": "同键请求正在处理中：等它结束，用**同一个** Idempotency-Key 重试",
    "MAINTENANCE": "平台正在恢复备份（写操作暂停）：退避重试，读接口可用",
    "LEASE_LOST": "任务租约被其他 worker 接手：无需处理，稍后查任务状态",
    # MIGRATION_NOT_ADOPTED 是启动期常量，只会出现在日志与退出码里（不会作为 HTTP 错误码返回）
}

def explain(err: dict) -> str:
    e = err["error"]
    return f'{e["code"]}: {e.get("hint") or CODE_ACTIONS.get(e["code"], "")} (request_id={e["request_id"]})'
```

完整错误码表见 [runbook/troubleshooting.md](../runbook/troubleshooting.md)（与 spec 附录 A 一致）。

## 12. OpenAPI

`docs/api/openapi.json` 由 CI 从 FastAPI 应用导出（不是手写）：

```bash
# 本地导出（apps/api 下）
python -c "import json,skillrouter_api.main as m;print(json.dumps(m.create_app().openapi(),ensure_ascii=False,indent=2))" > ../docs/api/openapi.json
```

契约测试会断言生成物与该文件**无 diff**，避免文档与实现漂移（§15.1）。
