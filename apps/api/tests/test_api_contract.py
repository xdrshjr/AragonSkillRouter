"""`/api/v1` 契约与 OpenAPI 一致性（§9.2.1 / C8）。"""

from __future__ import annotations

import json

V1_ENDPOINTS = [
    ("get", "/api/v1/health", False),
    ("get", "/api/v1/whoami", True),
    ("get", "/api/v1/capabilities", True),
    ("get", "/api/v1/capabilities/{key}", True),
    ("post", "/api/v1/capabilities/{key}/invoke", True),
    ("get", "/api/v1/tasks/{task_id}", True),
    ("post", "/api/v1/tasks/{task_id}/cancel", True),
    ("get", "/api/v1/files/{file_id}", True),
    ("post", "/api/v1/files", True),
    ("get", "/api/v1/skills", True),
    ("get", "/api/v1/skills/{name}", True),
    ("get", "/api/v1/skills/{name}/bundle", True),
    ("post", "/api/v1/mcp", True),
    ("get", "/api/v1/mcp", True),
    ("post", "/api/v1/callbacks/{provider_slug}", False),
]

ADMIN_ENDPOINTS = [
    "/api/admin/auth/bootstrap",
    "/api/admin/auth/login",
    "/api/admin/auth/logout",
    "/api/admin/auth/me",
    "/api/admin/auth/change-password",
    "/api/admin/overview",
    "/api/admin/events",
    "/api/admin/providers",
    "/api/admin/providers/{provider_id}",
    "/api/admin/providers/{provider_id}/credentials",
    "/api/admin/credentials/{credential_id}",
    "/api/admin/credentials/{credential_id}/verify",
    "/api/admin/credentials/{credential_id}/verifications",
    "/api/admin/credentials/{credential_id}/disable",
    "/api/admin/credentials/{credential_id}/enable",
    "/api/admin/credentials/{credential_id}/replace",
    "/api/admin/credentials/{credential_id}/rotate",
    "/api/admin/credentials/{credential_id}/impact",
    "/api/admin/capabilities",
    "/api/admin/capabilities/{capability_id}",
    "/api/admin/capabilities/{capability_id}/validate",
    "/api/admin/capabilities/{capability_id}/test",
    "/api/admin/capabilities/{capability_id}/publish",
    "/api/admin/capabilities/{capability_id}/deprecate",
    "/api/admin/capabilities/{capability_id}/versions",
    "/api/admin/capabilities/{capability_id}/diff",
    "/api/admin/capabilities/{capability_id}/rollback",
    "/api/admin/capabilities/{capability_id}/compatibility",
    "/api/admin/capabilities/{capability_id}/impact",
    "/api/admin/capabilities/{capability_id}/routes",
    "/api/admin/skills",
    "/api/admin/skills/{skill_id}",
    "/api/admin/skills/import/preview",
    "/api/admin/skills/import/commit",
    "/api/admin/skills/{skill_id}/test",
    "/api/admin/skills/{skill_id}/publish",
    "/api/admin/skills/{skill_id}/export",
    "/api/admin/skills/{skill_id}/enable",
    "/api/admin/skills/{skill_id}/disable",
    "/api/admin/skills/{skill_id}/dependencies",
    "/api/admin/skills/{skill_id}/preview",
    "/api/admin/access/agents",
    "/api/admin/access/agents/{agent_id}",
    "/api/admin/access/agents/{agent_id}/tokens",
    "/api/admin/access/tokens",
    "/api/admin/access/tokens/{token_id}",
    "/api/admin/calls",
    "/api/admin/calls/{request_id}",
    "/api/admin/tasks",
    "/api/admin/tasks/{task_id}",
    "/api/admin/tasks/{task_id}/cancel",
    "/api/admin/tasks/{task_id}/reconcile",
    "/api/admin/agent/sessions",
    "/api/admin/agent/sessions/{session_id}",
    "/api/admin/agent/sessions/{session_id}/messages",
    "/api/admin/agent/sessions/{session_id}/stream",
    "/api/admin/agent/sessions/{session_id}/stop",
    "/api/admin/agent/sessions/{session_id}/retry",
    "/api/admin/agent/flows",
    "/api/admin/agent/flows/{flow_id}",
    "/api/admin/agent/flows/{flow_id}/steps/{step}/run",
    "/api/admin/agent/flows/{flow_id}/publish",
    "/api/admin/agent/approvals",
    "/api/admin/agent/approvals/{approval_id}/decide",
    "/api/admin/agent/config",
    "/api/admin/agent/connectivity",
    "/api/admin/engine/status",
    "/api/admin/engine/candidates",
    "/api/admin/engine/check",
    "/api/admin/engine/upgrade",
    "/api/admin/engine/runs",
    "/api/admin/engine/runs/{run_id}",
    "/api/admin/engine/rollback",
    "/api/admin/engine/capabilities",
    "/api/admin/settings",
    "/api/admin/settings/rotate-master-key",
    "/api/admin/settings/audit",
    "/api/admin/settings/backup",
    "/api/admin/settings/backup/now",
]


def test_openapi_generated(client) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "Skill Router"
    assert "/api/v1/capabilities/{key}/invoke" in spec["paths"]


def test_all_v1_endpoints_documented(client) -> None:
    spec = client.get("/openapi.json").json()
    missing = []
    for method, path, _auth in V1_ENDPOINTS:
        if path not in spec["paths"] or method not in spec["paths"][path]:
            missing.append(f"{method.upper()} {path}")
    assert not missing, missing


def test_all_admin_endpoints_present(client) -> None:
    spec = client.get("/openapi.json").json()
    missing = [p for p in ADMIN_ENDPOINTS if p not in spec["paths"]]
    assert not missing, missing


def test_health_contract(client) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] in ("ok", "initializing")
    assert "version" in body
    assert "engine" in body
    assert "time" in body


def test_invoke_error_contract(client, make_token) -> None:
    token = make_token()
    r = client.post(
        "/api/v1/capabilities/text.generate.none/invoke",
        headers=token["headers"],
        json={"input": {}},
    )
    assert r.status_code == 404
    error = r.json()["error"]
    for key in ("code", "message", "hint", "retryable", "details", "request_id"):
        assert key in error


def test_capability_detail_hides_upstream_urls(client, admin, make_capability, make_token, monkeypatch) -> None:
    from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx

    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(key="text.generate.detail")
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    r = client.get(f"/api/v1/capabilities/{cap['key']}", headers=token["headers"])
    assert r.status_code == 200
    body = r.json()
    assert "input_schema" in body and "output_schema" in body
    assert "routes_summary" in body
    text = json.dumps(body)
    assert "fake-upstream.test" not in text
    assert "credential" not in text.lower()


def test_etag_and_cache_control(client, make_token) -> None:
    token = make_token()
    r = client.get("/api/v1/capabilities", headers=token["headers"])
    assert r.headers["ETag"].startswith('W/"')
    assert "private" in r.headers["Cache-Control"]


def test_pagination_shape(client, admin) -> None:
    r = client.get("/api/admin/providers", headers=admin["headers"], params={"page": 1, "page_size": 5})
    body = r.json()
    assert set(body) == {"items", "page", "page_size", "total"}
    assert body["page"] == 1 and body["page_size"] == 5


# ── §A.3.11 列表契约（T-28 / T-29 / T-30）与 §A.6.2 I7（T-43） ──────────


def _iso(delta_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _seed_calls(marker: str, count: int = 3) -> list[dict]:
    """直接落 3 条调用记录（避免依赖执行器耗时），字段可预测地不同。"""
    import os

    from skillrouter_api.db import db_write
    from skillrouter_api.models import CallLog

    rows = []
    for index in range(count):
        request_id = f"req_{marker}_{index}_{os.urandom(3).hex()}"
        caller_id = f"caller-{marker}"
        rows.append(
            {
                "request_id": request_id,
                "caller_id": caller_id,
                "capability_key": f"cap.{marker}",
                "latency_ms": (index + 1) * 100,
                "created_at": _iso(-index * 60),
            }
        )
    with db_write() as s:
        for row in rows:
            s.add(
                CallLog(
                    id=f"cal_{row['request_id']}",
                    request_id=row["request_id"],
                    caller_type="admin",
                    caller_id=row["caller_id"],
                    capability_key=row["capability_key"],
                    mode="sync",
                    status="success",
                    latency_ms=row["latency_ms"],
                    created_at=row["created_at"],
                )
            )
    return rows


def test_calls_needs_reconcile_filter(client, admin) -> None:
    """I6：`call_logs.needs_reconcile=1` 的行必须能被 `/calls?needs_reconcile=1` 找出来。"""
    import os

    from sqlalchemy import select

    from skillrouter_api.db import db_write
    from skillrouter_api.models import CallLog

    marker = os.urandom(3).hex()
    rows = _seed_calls(marker)
    with db_write() as s:
        target = s.scalar(
            select(CallLog).where(CallLog.request_id == rows[1]["request_id"])
        )
        target.needs_reconcile = 1
        target.status = "status_unknown"

    r = client.get(
        "/api/admin/calls",
        headers=admin["headers"],
        params={"needs_reconcile": 1, "capability": f"cap.{marker}"},
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["request_id"] for i in items] == [rows[1]["request_id"]]
    assert items[0]["needs_reconcile"] is True


def test_calls_filter_aliases(client, admin) -> None:
    """T-28：`from`/`to` 与 `date_from`/`date_to` 结果一致，且返回 `X-Deprecated-Param`。"""
    import os

    marker = os.urandom(3).hex()
    _seed_calls(marker)
    window = {"date_from": _iso(-3600), "date_to": _iso(60)}

    canonical = client.get(
        "/api/admin/calls",
        headers=admin["headers"],
        params={**window, "capability": f"cap.{marker}"},
    )
    assert canonical.status_code == 200
    assert "X-Deprecated-Param" not in canonical.headers

    aliased = client.get(
        "/api/admin/calls",
        headers=admin["headers"],
        params={
            "from": window["date_from"],
            "to": window["date_to"],
            "capability": f"cap.{marker}",
        },
    )
    assert aliased.status_code == 200
    assert aliased.headers["X-Deprecated-Param"] == "from,to"
    assert [i["request_id"] for i in aliased.json()["items"]] == [
        i["request_id"] for i in canonical.json()["items"]
    ]


def test_calls_sort_whitelist(client, admin) -> None:
    """T-29：`-duration_ms` 生效；非法字段 400。"""
    import os

    marker = os.urandom(3).hex()
    _seed_calls(marker)

    r = client.get(
        "/api/admin/calls",
        headers=admin["headers"],
        params={"sort": "-duration_ms", "capability": f"cap.{marker}"},
    )
    assert r.status_code == 200, r.text
    durations = [i["duration_ms"] for i in r.json()["items"]]
    assert durations == [300, 200, 100], durations

    ascending = client.get(
        "/api/admin/calls",
        headers=admin["headers"],
        params={"sort": "duration_ms", "capability": f"cap.{marker}"},
    )
    assert [i["duration_ms"] for i in ascending.json()["items"]] == [100, 200, 300]

    bad = client.get(
        "/api/admin/calls", headers=admin["headers"], params={"sort": "-cost_usd"}
    )
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "VALIDATION_FAILED"


def _seed_tasks(marker: str) -> dict:
    """落 3 条任务行（不建队列项，避免干扰其它用例的 worker 排空）。"""
    import os

    from skillrouter_api.db import db_write
    from skillrouter_api.models import Task

    created = {}
    with db_write() as s:
        for index, status in enumerate(("queued", "running", "queued")):
            task_id = f"tsk_{marker}_{index}_{os.urandom(3).hex()}"
            s.add(
                Task(
                    id=task_id,
                    request_id=f"req_{marker}_{index}",
                    capability_key=f"cap.{marker}",
                    mode="async",
                    status=status,
                    progress_percent=0,
                    progress_stage="queued",
                    input_snapshot={"input": {}},
                    upstream_task_id=f"up-{marker}" if index == 1 else None,
                    cancel_capability="remote" if index == 1 else "none",
                    attempts=1,
                    max_attempts=3,
                    resume_policy="check",
                    created_at=_iso(-index * 60),
                    updated_at=_iso(-index * 60),
                )
            )
            created[index] = task_id
    return created


def test_tasks_list_filters(client, admin) -> None:
    """T-30：`q` / `status` / `capability_key` / 分页各一条断言。"""
    import os

    marker = os.urandom(3).hex()
    tasks = _seed_tasks(marker)

    by_capability = client.get(
        "/api/admin/tasks", headers=admin["headers"], params={"capability_key": f"cap.{marker}"}
    )
    assert by_capability.status_code == 200, by_capability.text
    assert by_capability.json()["total"] == 3

    by_status = client.get(
        "/api/admin/tasks",
        headers=admin["headers"],
        params={"capability_key": f"cap.{marker}", "status": "running"},
    )
    assert by_status.json()["total"] == 1
    assert by_status.json()["items"][0]["task_id"] == tasks[1]

    by_q_request = client.get(
        "/api/admin/tasks", headers=admin["headers"], params={"q": f"req_{marker}_2"}
    )
    assert [i["task_id"] for i in by_q_request.json()["items"]] == [tasks[2]]

    by_q_upstream = client.get("/api/admin/tasks", headers=admin["headers"], params={"q": f"up-{marker}"})
    assert [i["task_id"] for i in by_q_upstream.json()["items"]] == [tasks[1]]

    paged = client.get(
        "/api/admin/tasks",
        headers=admin["headers"],
        params={"capability_key": f"cap.{marker}", "page": 1, "page_size": 2, "sort": "created_at"},
    )
    body = paged.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    assert body["page"] == 1 and body["page_size"] == 2

    bad = client.get("/api/admin/tasks", headers=admin["headers"], params={"sort": "mode"})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "VALIDATION_FAILED"

    # 收尾：种子改用终态，避免后续用例的 recover_on_startup 把它们当「未完成任务」补队列入队
    from skillrouter_api.db import db_write
    from skillrouter_api.models import Task

    with db_write() as s:
        for task_id in tasks.values():
            row = s.get(Task, task_id)
            if row.status in ("queued", "running"):
                row.status = "canceled"


def test_call_log_status_mapping(client, admin, make_capability, make_token, monkeypatch) -> None:
    """T-43：`LOGGED_STATUS` 覆盖执行器三种状态；落库与成功率聚合用同一常量。"""
    from skillrouter_api.services import execution

    mapping = execution.LOGGED_STATUS
    assert {"succeeded", "failed", "status_unknown"} <= set(mapping)
    assert mapping["succeeded"] == "success", "成功率聚合的分子固定是 success"

    from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx

    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability()
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test",
        headers=admin["headers"],
        json={"input": {"prompt": "x"}},
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish",
        headers=admin["headers"],
        json={"version": "1.0.0"},
    )
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "统计口径"}},
    )
    assert r.status_code == 200, r.text
    request_id = r.json()["request_id"]

    from skillrouter_api.db import db_session
    from skillrouter_api.models import CallLog
    from sqlalchemy import select

    with db_session() as s:
        row = s.scalar(select(CallLog).where(CallLog.request_id == request_id))
    assert row is not None
    assert row.status == mapping["succeeded"], row.status

    overview = client.get("/api/admin/overview", headers=admin["headers"]).json()
    assert overview["success_rate_24h"] is not None
    assert overview["success_rate_24h"] > 0
