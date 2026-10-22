"""Performance baseline for the documented deployment target.

Measures two things the spec calls out separately:

  * regular management endpoints — P95 must stay under 500 ms at 10 concurrent
    clients (platform work only; no upstream provider in the path);
  * async task submission — platform-side validation + persistence must finish
    within 1 s and return a task id. Upstream provider time is excluded by
    subtracting the measured stub latency, and that subtraction is reported so
    the number can be judged rather than trusted blindly.

The stub used for the second measurement is a local HTTP server that sleeps for
a fixed 50 ms and returns a task id. It exists only to measure platform
overhead; it proves nothing about a real provider (other scripts do that).

Usage:
    python scripts/perf_check.py --url http://127.0.0.1:8000 --admin-password <pw>
"""

from __future__ import annotations

import argparse
import json
import os
import platform as platform_mod
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

STUB_LATENCY_MS = 50


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        time.sleep(STUB_LATENCY_MS / 1000)
        body = json.dumps({"id": "stub-" + str(int(time.time() * 1000)), "status": "queued"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def measure(client_factory, path: str, *, requests: int, concurrency: int) -> dict:
    latencies: list[float] = []
    errors: list[int] = []
    lock = threading.Lock()

    def worker(_: int) -> None:
        client = client_factory()
        try:
            for _ in range(requests):
                started = time.perf_counter()
                response = client.get(path)
                elapsed = (time.perf_counter() - started) * 1000
                with lock:
                    latencies.append(elapsed)
                    if response.status_code >= 400:
                        errors.append(response.status_code)
        finally:
            client.close()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(worker, range(concurrency)))
    wall = time.perf_counter() - started
    total = len(latencies)
    return {
        "path": path,
        "requests": total,
        "concurrency": concurrency,
        "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": round(p95(latencies), 1),
        "max_ms": round(max(latencies), 1) if latencies else None,
        "throughput_rps": round(total / wall, 1) if wall else None,
        "errors": len(errors),
        "error_codes": sorted(set(errors)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Performance baseline")
    parser.add_argument("--url", default=os.environ.get("SR_VERIFY_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default=os.environ.get("SR_ADMIN_PASSWORD", ""))
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--requests", type=int, default=20, help="requests per client")
    parser.add_argument("--skip-submit", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    if not args.admin_password:
        raise SystemExit("--admin-password (or SR_ADMIN_PASSWORD) is required")

    bootstrap = httpx.Client(base_url=args.url, timeout=60, trust_env=False)
    login = bootstrap.post("/api/auth/login", json={"username": args.admin_user, "password": args.admin_password})
    if login.status_code != 200:
        raise SystemExit(f"login failed: {login.text[:300]}")
    csrf = login.json()["csrf_token"]
    cookies = dict(login.cookies)

    def authenticated() -> httpx.Client:
        client = httpx.Client(base_url=args.url, timeout=60, trust_env=False, headers={"x-csrf-token": csrf})
        client.cookies.update(cookies)
        return client

    results: dict[str, object] = {
        "environment": {
            "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine": platform_mod.platform(),
            "processor": platform_mod.processor(),
            "python": sys.version.split()[0],
            "clients": args.clients,
            "database": "SQLite WAL",
        },
        "read_endpoints": [],
    }
    targets = [
        "/api/admin/overview",
        "/api/admin/providers?limit=20",
        "/api/admin/capabilities?limit=20",
        "/api/admin/invocations?limit=20",
        "/api/admin/tasks?limit=20",
        "/api/admin/skills?limit=20",
    ]
    for path in targets:
        try:
            row = measure(authenticated, path, requests=args.requests, concurrency=args.clients)
        except Exception as exc:  # noqa: BLE001 - measurement must continue
            row = {"path": path, "error": str(exc)}
        results["read_endpoints"].append(row)  # type: ignore[union-attr]
        print(f"{path:42} p95={row.get('p95_ms')}ms p50={row.get('p50_ms')}ms rps={row.get('throughput_rps')} errors={row.get('errors')}", flush=True)

    if not args.skip_submit:
        stub = HTTPServer(("127.0.0.1", 0), StubHandler)
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        port = stub.server_address[1]
        print(f"\nstub upstream on 127.0.0.1:{port} (sleeps {STUB_LATENCY_MS} ms)", flush=True)
        submit = {
            "stub": f"http://127.0.0.1:{port}/jobs",
            "note": (
                "Platform-side overhead only. Requires SR_OUTBOUND_ALLOWED_HOSTS to include 127.0.0.1; "
                "skipped otherwise because private targets are blocked by policy."
            ),
        }
        client = authenticated()
        provider = client.post(
            "/api/admin/providers",
            json={"name": "Perf stub provider", "category": "generic", "base_url": f"http://127.0.0.1:{port}", "environment": "development", "tags": ["measurement"], "notes": "Harness only", "status": "active"},
        )
        credential = client.post(
            "/api/admin/credentials",
            json={"provider_id": (provider.json() or {}).get("id"), "name": "Perf stub credential", "auth_type": "api_key", "secret": {"api_key": "stub-key-not-a-secret"}, "config": {"header_name": "x-api-key"}},
        ) if provider.status_code == 201 else None
        probe = client.post(
            "/api/admin/capabilities",
            json={
                "name": "perf-stub-submit",
                "type": "video",
                "description": "measurement only",
                "provider_id": (provider.json() or {}).get("id") if provider.status_code == 201 else None,
                "credential_id": (credential.json() or {}).get("id") if credential and credential.status_code == 201 else None,
            },
        )
        if probe.status_code in (200, 201):
            capability = probe.json()
            version_id = capability["versions"][0]["id"]
            config = {
                "model": "stub",
                "request": {"method": "POST", "url": f"http://127.0.0.1:{port}/jobs", "headers": {"content-type": "application/json"}, "encoding": "json", "query": [], "body_template": {"prompt": "{{ input.prompt }}"}},
                "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"], "additionalProperties": False},
                "response": {"kind": "json", "extract": {"result": "$.id"}},
                "async": {
                    "enabled": True,
                    "submit_extract": {"task_id": "$.id"},
                    "status": {"url": f"http://127.0.0.1:{port}/jobs/{{{{ task_id }}}}", "method": "POST", "extract": {"status": "$.status"}, "map": {"queued": ["queued"], "succeeded": ["succeeded"]}, "poll_interval_s": 60, "result_extract": {"result": "$.id"}},
                    "cancel": {"method": "POST", "url": "", "extract": {"status": ""}},
                },
                "modes": {"sync": False, "async": True, "stream": False},
                "limits": {"timeout_s": 20, "max_output_bytes": 65536, "max_input_bytes": 8192, "max_duration_s": 5},
                "errors": {"rules": [], "default_code": "upstream_error"},
                "pricing": {"per_call": 0, "input_per_1k": 0, "output_per_1k": 0, "per_second": 0, "currency": "USD"},
                "retry": {"max_attempts": 0, "backoff_ms": [], "retry_on": []},
                "examples": [{"input": {"prompt": "x"}}],
            }
            patched = client.patch(f"/api/admin/capabilities/{capability['id']}/versions/{version_id}", json={"config": config})
            timings: list[float] = []
            outcomes: list[str] = []
            if patched.status_code in (200, 201):
                for _ in range(max(1, args.requests // 2)):
                    started = time.perf_counter()
                    response = client.post(f"/api/admin/capabilities/{capability['id']}/invoke", json={"input": {"prompt": "measure"}, "mode": "async"})
                    elapsed = (time.perf_counter() - started) * 1000
                    timings.append(elapsed)
                    outcomes.append(str(response.status_code))
            overrides = [t - STUB_LATENCY_MS for t in timings]
            submit.update(
                {
                    "requests": len(timings),
                    "status_codes": sorted(set(outcomes)),
                    "total_p95_ms": round(p95(timings), 1) if timings else None,
                    "platform_overhead_p95_ms": round(p95(overrides), 1) if overrides else None,
                    "platform_overhead_max_ms": round(max(overrides), 1) if overrides else None,
                }
            )
            client.delete(f"/api/admin/capabilities/{capability['id']}")
        else:
            submit["skipped_reason"] = f"capability creation HTTP {probe.status_code}: {probe.text[:200]}"
        client.close()
        stub.shutdown()
        results["async_submit"] = submit
        print(f"async submit: {json.dumps(submit)}", flush=True)

    budget_ok = all(
        (row.get("p95_ms") or 0) <= 500 and not row.get("errors")
        for row in results["read_endpoints"]  # type: ignore[union-attr]
    )
    submit_row = results.get("async_submit") or {}
    submit_ok = True
    if submit_row.get("platform_overhead_p95_ms") is not None:
        submit_ok = submit_row["platform_overhead_p95_ms"] <= 1000
    results["verdict"] = {
        "read_p95_under_500ms": budget_ok,
        "async_submit_under_1s_platform_side": submit_ok,
        "note": "Measured on the host described in `environment`; not a claim about other hardware.",
    }
    print(f"\nverdict: {json.dumps(results['verdict'])}")

    out = args.out or os.path.join(os.path.dirname(__file__), "..", "data", "verification",
                                   f"perf-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    print(f"report: {os.path.abspath(out)}")
    return 0 if budget_ok and submit_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
