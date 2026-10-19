#!/usr/bin/env python3
"""性能冒烟（spec §15.1 / §15.4）。

测什么
------
1. **管理接口 P95**：并发 10 打一组幂等 GET（`/api/admin/overview`、列表页…），
   目标 `P95 ≤ 500 ms`（§15.4）。
2. **异步任务提交耗时**：`POST /api/v1/capabilities/{key}/invoke` + `options.mode=async`，
   目标 `P95 ≤ 1000 ms`（§15.4「异步任务提交（平台侧）≤1 s」，排除上游耗时）；
   注意 §4.4(d)：请求内不触达上游，因此这个数就是平台开销。
3. **SSE 首字节**（可选 `--sse-url`）：目标 `≤ 300 ms`（§15.4）。

不测同步调用平台开销（≤150ms）：那需要 §15.1 的「假上游 10ms 场景」，属于 pytest 集成测试
（`tests/`）的职责，本脚本打的是真实部署，量到的数字含真实上游耗时，不能当平台开销。

用法
----
    python scripts/perf_smoke.py --base-url http://127.0.0.1:8000 ^
        --cookie "sr_session=<32B base64url>" --csrf "<X-CSRF-Token>"

    python scripts/perf_smoke.py --base-url https://router.example.com ^
        --cookie "sr_session=..." --token srk_xxx_yyy ^
        --invoke-capability text.generate.demo --invoke-input "{\"prompt\":\"ping\"}" ^
        --duration 30 --json var/logs/perf-smoke.json

退出码：0 全部达标；1 有指标超预算；2 参数错误；3 连不上平台。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型注解
    import httpx

DEFAULT_TARGETS = (
    "/api/admin/overview",
    "/api/admin/capabilities?page=1&page_size=20",
    "/api/admin/calls?page=1&page_size=20",
    "/api/admin/providers",
    "/api/admin/skills",
)


# ── 基础工具 ────────────────────────────────────────────────────────────────


def _utf8_stdout() -> None:
    try:  # pragma: no cover - 取决于宿主终端
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )
    except Exception:
        pass


def log(message: str = "") -> None:
    print(message, flush=True)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[index]


@dataclass
class Sample:
    """一组请求的统计结果。"""

    name: str
    method: str
    path: str
    latencies: list[float] = field(default_factory=list)
    statuses: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    wall_s: float = 0.0

    def add(self, latency_ms: float, status: int | str) -> None:
        self.latencies.append(latency_ms)
        key = str(status)
        self.statuses[key] = self.statuses.get(key, 0) + 1

    # ── 派生指标 ──
    @property
    def count(self) -> int:
        return len(self.latencies)

    @property
    def failures(self) -> int:
        return sum(v for k, v in self.statuses.items() if not k.startswith("2"))

    @property
    def p50(self) -> float:
        return percentile(self.latencies, 50)

    @property
    def p95(self) -> float:
        return percentile(self.latencies, 95)

    @property
    def p99(self) -> float:
        return percentile(self.latencies, 99)

    @property
    def worst(self) -> float:
        return max(self.latencies) if self.latencies else 0.0

    @property
    def rps(self) -> float:
        return (self.count / self.wall_s) if self.wall_s > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "count": self.count,
            "failures": self.failures,
            "statuses": self.statuses,
            "p50_ms": round(self.p50, 1),
            "p95_ms": round(self.p95, 1),
            "p99_ms": round(self.p99, 1),
            "max_ms": round(self.worst, 1),
            "rps": round(self.rps, 2),
            "errors": self.errors[:5],
        }


def print_table(samples: list[Sample], budgets: dict[str, float | None]) -> None:
    header = f"{'group':<26}{'method':<7}{'samples':>8}{'fail':>6}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}{'rps':>8}   verdict"
    log("")
    log(header)
    log("-" * len(header))
    for sample in samples:
        budget = budgets.get(sample.name)
        if budget is None:
            verdict = "—"
        elif sample.p95 <= budget:
            verdict = f"✓ p95<= {budget:.0f}ms"
        else:
            verdict = f"✗ p95 {sample.p95:.0f}ms > {budget:.0f}ms"
        log(
            f"{sample.name[:25]:<26}{sample.method:<7}{sample.count:>8}{sample.failures:>6}"
            f"{sample.p50:>8.0f}ms{sample.p95:>8.0f}ms{sample.p99:>8.0f}ms{sample.worst:>8.0f}ms"
            f"{sample.rps:>8.1f}   {verdict}"
        )
    log("")


async def _run_get(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, sample: Sample, path: str
) -> None:
    import httpx

    async with sem:
        started = time.perf_counter()
        try:
            response = await client.get(path)
            sample.add((time.perf_counter() - started) * 1000, response.status_code)
        except httpx.HTTPError as exc:
            sample.add((time.perf_counter() - started) * 1000, "ERR")
            sample.errors.append(f"{type(exc).__name__}: {exc}")


async def measure_endpoint(
    client: httpx.AsyncClient, path: str, requests: int, concurrency: int
) -> Sample:
    sem = asyncio.Semaphore(concurrency)
    sample = Sample(
        name=path.split("?")[0].split("/")[-1] or "root", method="GET", path=path
    )
    started = time.perf_counter()
    await asyncio.gather(
        *(_run_get(client, sem, sample, path) for _ in range(requests))
    )
    sample.wall_s = time.perf_counter() - started
    return sample


async def measure_async_submit(
    client: httpx.AsyncClient, capability: str, body: dict, requests: int, concurrency: int
) -> Sample:
    import httpx

    sem = asyncio.Semaphore(concurrency)
    path = f"/api/v1/capabilities/{capability}/invoke"
    sample = Sample(name="invoke(async) 提交", method="POST", path=path)
    payload = dict(body)
    options = dict(payload.get("options") or {})
    options["mode"] = "async"
    payload["options"] = options

    async def one() -> None:
        async with sem:
            started = time.perf_counter()
            try:
                response = await client.post(
                    path,
                    json=payload,
                    headers={"Idempotency-Key": f"perf-{time.time_ns()}"},
                )
                sample.add((time.perf_counter() - started) * 1000, response.status_code)
                if response.status_code not in (200, 202):
                    sample.errors.append(
                        f"{response.status_code}: {response.text[:200]}"
                    )
            except httpx.HTTPError as exc:
                sample.add((time.perf_counter() - started) * 1000, "ERR")
                sample.errors.append(f"{type(exc).__name__}: {exc}")

    started = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(requests)))
    sample.wall_s = time.perf_counter() - started
    return sample


async def measure_sse_first_byte(client: httpx.AsyncClient, url: str) -> Sample:
    import httpx

    sample = Sample(name="SSE 首字节", method="GET", path=url)
    started = time.perf_counter()
    try:
        async with client.stream("GET", url) as response:
            first = (time.perf_counter() - started) * 1000
            sample.add(first, response.status_code)
            async for _ in response.aiter_raw():
                break
    except httpx.HTTPError as exc:
        sample.add((time.perf_counter() - started) * 1000, "ERR")
        sample.errors.append(f"{type(exc).__name__}: {exc}")
    sample.wall_s = max((time.perf_counter() - started), 0.001)
    return sample


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_smoke.py",
        description="Skill Router 性能冒烟（httpx 并发 10）：管理接口 P95 + 异步提交延迟",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="平台基址")
    parser.add_argument(
        "--cookie",
        help="管理端 Cookie。可给完整串（`sr_session=...; sr_csrf=...`）或只给 sr_session 的值",
    )
    parser.add_argument("--csrf", help="X-CSRF-Token（写接口需要；只读 GET 可不给）")
    parser.add_argument("--token", help="平台令牌 srk_...（测 /api/v1 异步提交时必填）")
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="被测管理端点（可重复）。默认：" + ", ".join(DEFAULT_TARGETS),
    )
    parser.add_argument(
        "--concurrency", type=int, default=10, help="并发数（默认 10，§15.4 条件）"
    )
    parser.add_argument(
        "--duration", type=float, default=20.0, help="每组压力持续秒数（默认 20）"
    )
    parser.add_argument(
        "--max-requests", type=int, default=600, help="每组最多请求数（默认 600）"
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="单请求超时秒（默认 30）"
    )
    parser.add_argument(
        "--p95-budget", type=float, default=500.0, help="管理接口 P95 预算 ms（§15.4）"
    )
    parser.add_argument(
        "--submit-budget",
        type=float,
        default=1000.0,
        help="异步提交 P95 预算 ms（§15.4）",
    )
    parser.add_argument(
        "--sse-budget", type=float, default=300.0, help="SSE 首字节预算 ms（§15.4）"
    )
    parser.add_argument("--invoke-capability", help="用于测异步提交的能力 key")
    parser.add_argument(
        "--invoke-input", default="{}", help="异步提交的 input（JSON 字符串，默认 {}）"
    )
    parser.add_argument("--invoke-version", help="capability_version（可选）")
    parser.add_argument(
        "--invoke-count", type=int, default=20, help="异步提交采样次数（默认 20）"
    )
    parser.add_argument(
        "--sse-url", help="可选的 SSE 端点（例如 /api/v1/mcp 或任务事件流）"
    )
    parser.add_argument(
        "--no-gate", action="store_true", help="超预算也返回 0（只出报告）"
    )
    parser.add_argument("--json", help="把结构化报告写到该路径")
    parser.add_argument("--label", help="报告里的环境标签（如 staging-4c8g）")
    return parser


async def run(
    args: argparse.Namespace,
) -> tuple[list[Sample], dict[str, float | None], int]:
    import httpx

    headers: dict[str, str] = {"Accept": "application/json"}
    if args.cookie:
        cookie = args.cookie.strip()
        if "=" not in cookie:
            cookie = f"sr_session={cookie}"
        headers["Cookie"] = cookie
    if args.csrf:
        headers["X-CSRF-Token"] = args.csrf
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    targets = args.target or list(DEFAULT_TARGETS)
    budgets: dict[str, float | None] = {}
    samples: list[Sample] = []

    limits = httpx.Limits(
        max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=httpx.Timeout(args.timeout),
        limits=limits,
        follow_redirects=False,
    ) as client:
        # 连通性预检：明确区分「服务没起」与「鉴权失败」
        try:
            probe = await client.get("/api/v1/health")
        except httpx.HTTPError as exc:
            log(f"[perf] [FATAL] 连不上 {args.base_url}：{type(exc).__name__}: {exc}")
            return [], {}, 3
        log(f"[perf] 连通性：GET /api/v1/health → {probe.status_code}")
        if probe.status_code >= 500:
            log(f"[perf] [FATAL] 平台未就绪（{probe.status_code}）：{probe.text[:200]}")
            return [], {}, 3

        budget_per_target = max(1, min(args.max_requests, int(args.duration * 20)))
        for path in targets:
            name = path.split("?")[0].split("/")[-1] or "root"
            log(
                f"[perf] 压测 {path}（样本上限 {budget_per_target}，并发 {args.concurrency}）…"
            )
            sample = await measure_endpoint(
                client, path, budget_per_target, args.concurrency
            )
            sample.name = name
            samples.append(sample)
            budgets[name] = args.p95_budget

        if args.invoke_capability:
            if not args.token:
                log(
                    "[perf] [SKIP] 未提供 --token：/api/v1/capabilities/{key}/invoke 需要 Bearer 令牌，"
                    "已跳过异步提交测量（不会用 mock 数字冒充）。"
                )
            else:
                try:
                    invoke_input = json.loads(args.invoke_input)
                except json.JSONDecodeError as exc:
                    log(f"[perf] [FATAL] --invoke-input 不是合法 JSON：{exc}")
                    return samples, budgets, 2
                body: dict = {"input": invoke_input, "options": {}}
                if args.invoke_version:
                    body["capability_version"] = args.invoke_version
                log(
                    f"[perf] 压测异步提交 {args.invoke_capability}"
                    f"（样本 {args.invoke_count}，并发 {args.concurrency}）…"
                )
                submit = await measure_async_submit(
                    client,
                    args.invoke_capability,
                    body,
                    args.invoke_count,
                    args.concurrency,
                )
                samples.append(submit)
                budgets[submit.name] = args.submit_budget
        else:
            log("[perf] [SKIP] 未提供 --invoke-capability：跳过异步提交测量。")

        if args.sse_url:
            log(f"[perf] 测量 SSE 首字节 {args.sse_url} …")
            sse = await measure_sse_first_byte(client, args.sse_url)
            samples.append(sse)
            budgets[sse.name] = args.sse_budget

    return samples, budgets, 0


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)

    log(
        "[perf] Skill Router 性能冒烟（§15.4 基线：管理接口 P95 ≤ 500ms，异步提交 ≤ 1s）"
    )
    log(f"[perf] 目标　：{args.base_url}")
    log(
        f"[perf] 并发　：{args.concurrency}    每组时长：{args.duration}s  上限：{args.max_requests} 请求"
    )
    if args.label:
        log(f"[perf] 环境标签：{args.label}")
    if not args.cookie and not args.token:
        log(
            "[perf] [WARN] 未提供 --cookie/--token：管理接口会返回 401，报告里的失败数是预期行为。"
        )

    try:
        samples, budgets, code = asyncio.run(run(args))
    except KeyboardInterrupt:
        log("[perf] 已中断。")
        return 2
    if code != 0:
        return code

    print_table(samples, budgets)

    breaches = [
        (s, budgets[s.name])
        for s in samples
        if budgets.get(s.name) is not None and s.p95 > float(budgets[s.name] or 0)
    ]
    for sample in samples:
        if sample.errors:
            log(f"[perf] {sample.name} 失败样例：{sample.errors[0][:220]}")

    report = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": args.label,
        "base_url": args.base_url,
        "concurrency": args.concurrency,
        "budgets_ms": budgets,
        "samples": [s.as_dict() for s in samples],
        "breaches": [s.name for s, _ in breaches],
        "notes": [
            "同步调用平台开销（≤150ms）不在此脚本测量范围：需 §15.1 的假上游集成测试。",
            "结论只对本机/本次采样有效，归档时请保留 --label 与机器规格。",
        ],
    }
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"[perf] 报告已写入：{path}")

    if breaches:
        log(
            f"[perf] ✗ {len(breaches)} 组指标超预算："
            + "、".join(s.name for s, _ in breaches)
        )
        for sample, budget in breaches:
            log(
                f"[perf]   - {sample.name}: p95 {sample.p95:.0f}ms > 预算 {float(budget):.0f}ms"
            )
        log("[perf] 处置参考 §15.4：call_logs 归档、补索引、概览改物化快照表。")
        return 0 if args.no_gate else 1

    log("[perf] ✓ 全部指标在预算内（采样条件见上表；归档时附上机器规格与 --label）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
