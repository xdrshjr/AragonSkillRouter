"""维护模式（F-25 / §A.3.13 / T-25）。

判据是文件标记 `data_dir/.restore-in-progress`：存在即维护中。写方法 503
`MAINTENANCE`，读方法放行，`/healthz.maintenance` 为 `true`；陈旧标记（>24h）
仍然按维护中处理，只额外打一条 `stale_maintenance_marker`。
"""

from __future__ import annotations

import logging
import os
import time

import pytest

from skillrouter_api.services import maintenance


@pytest.fixture()
def marker():
    path = maintenance.marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("restore in progress", encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def test_writes_blocked_reads_allowed(client, admin, marker) -> None:
    """T-25：标记存在 → 写 503 MAINTENANCE、读 200、`/healthz.maintenance=true`。"""
    assert maintenance.is_maintenance() is True

    blocked = client.post(
        "/api/admin/providers",
        headers=admin["headers"],
        json={"slug": "maintenance-blocked", "name": "维护中不应写入", "category": "llm"},
    )
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["error"]["code"] == "MAINTENANCE"

    allowed = client.get("/api/admin/providers", headers=admin["headers"])
    assert allowed.status_code == 200, allowed.text

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["maintenance"] is True

    # 对外调用与 MCP 同样被拦（读方法不受影响）
    invoke = client.post(
        "/api/v1/capabilities/text.chat/invoke",
        headers={"Authorization": "Bearer srk_0000_does-not-matter"},
        json={"input": {}},
    )
    assert invoke.status_code == 503
    assert invoke.json()["error"]["code"] == "MAINTENANCE"

    mcp = client.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    assert mcp.status_code == 503
    assert mcp.json()["error"]["code"] == -32000


def test_stale_marker_still_blocks_and_warns(client, marker) -> None:
    """陈旧标记：仍返回 True（宁可 503），并记一条 `stale_maintenance_marker`。"""
    old = time.time() - 25 * 3600
    os.utime(marker, (old, old))

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("skillrouter.run")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        maintenance._last_stale_warn["at"] = 0.0  # 清掉抑制窗口，确保本次一定打日志
        assert maintenance.is_maintenance() is True
        assert client.get("/healthz").json()["maintenance"] is True
    finally:
        logger.removeHandler(handler)
    assert "stale_maintenance_marker" in [record.getMessage() for record in records]
