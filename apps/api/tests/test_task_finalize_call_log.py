"""异步任务终态回写：`call_logs` 必须真的从 `queued` 走到终态（§A.3.1-2 / I7 / T-46）。

回归背景：`finalize_task_call()` 曾读 `task.error`，而 `Task` 模型没有该列 ——
每次回写都抛 `AttributeError`，把 `db_write()` 事务整体回滚，于是
`call_logs.status` 永远停在 `queued`，`/calls` 一律显示「排队中」。
因为 `worker/runner.py` 用 `try/except` 包住并只打一条 warning，
这个缺陷在日志里非常不显眼，也没有任何用例断言过终态。

本用例锁死三件事：
1. 回写不抛异常且返回 True；
2. `call_logs.status` 变成 `success`（不是 `queued`）；
3. 错误码取自真实字段（`result["error"]["code"]` 优先，其次 `error_code` 列）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select


@pytest.fixture(autouse=True)
def _cleanup_probe_rows():
    """用完即清：本文件会插入 task/call_log 行，若留下非终态任务，
    会污染 `task_service.recover_on_startup()` 的全局计数
    （`tests/test_tasks_recovery.py::test_recover_on_startup_requeues`
    断言 resumed_tasks == 0，实测会被这里留下的一行 `running` 打破）。"""
    yield
    from skillrouter_api.db import db_write
    from skillrouter_api.models import CallLog, Task

    with db_write() as s:
        for row in s.scalars(select(Task).where(Task.capability_key == "text.generate.probe")):
            s.delete(row)
        for row in s.scalars(
            select(CallLog).where(CallLog.capability_key == "text.generate.probe")
        ):
            s.delete(row)


def _seed(task_id: str, request_id: str, status: str, *, result: dict | None = None,
          error_code: str | None = None) -> None:
    from skillrouter_api.db import db_write, now_iso
    from skillrouter_api.models import CallLog, Task

    with db_write() as s:
        s.add(
            Task(
                id=task_id,
                request_id=request_id,
                capability_key="text.generate.probe",
                mode="async",
                status=status,
                error_code=error_code,
                result=result or {},
                created_at=now_iso(),
                updated_at=now_iso(),
                finished_at=now_iso(),
            )
        )
        s.add(
            CallLog(
                id=f"cal_{task_id}",
                request_id=request_id,
                caller_type="token",
                capability_key="text.generate.probe",
                mode="async",
                status="queued",
                payload_policy="redacted_preview",
                task_id=task_id,
                created_at=now_iso(),
            )
        )


def _call_of(task_id: str):
    from skillrouter_api.db import db_session
    from skillrouter_api.models import CallLog

    with db_session() as s:
        return s.scalar(select(CallLog).where(CallLog.task_id == task_id))


def test_finalize_succeeded_task_sets_call_log_success() -> None:
    from skillrouter_api.services import execution

    _seed("tsk_fin_1", "req_fin_1", "succeeded")

    assert execution.finalize_task_call("tsk_fin_1") is True
    call = _call_of("tsk_fin_1")
    assert call is not None
    assert call.status == "success", f"应为 success，实际 {call.status}"
    # 已终态再调一次必须幂等（不重复改行、不返回 True）
    assert execution.finalize_task_call("tsk_fin_1") is False


def test_finalize_failed_task_takes_error_code_from_result() -> None:
    from skillrouter_api.services import execution

    _seed(
        "tsk_fin_2",
        "req_fin_2",
        "failed",
        result={"error": {"code": "UPSTREAM_TIMEOUT", "message": "boom"}},
        error_code="UPSTREAM_ERROR",
    )

    assert execution.finalize_task_call("tsk_fin_2") is True
    call = _call_of("tsk_fin_2")
    assert call is not None and call.status == "failed"
    # result 里的结构化错误优先于兜底列
    assert call.error_code == "UPSTREAM_TIMEOUT"


def test_finalize_canceled_task_marks_failed_with_canceled_code() -> None:
    from skillrouter_api.services import execution

    _seed("tsk_fin_3", "req_fin_3", "canceled")

    assert execution.finalize_task_call("tsk_fin_3") is True
    call = _call_of("tsk_fin_3")
    assert call is not None
    assert call.status == "failed", "取消在 call_logs 里没有独立取值，应落 failed"
    assert call.error_code == "CANCELED"


def test_finalize_ignores_non_terminal_task() -> None:
    from skillrouter_api.services import execution

    _seed("tsk_fin_4", "req_fin_4", "running")

    assert execution.finalize_task_call("tsk_fin_4") is False
    call = _call_of("tsk_fin_4")
    assert call is not None and call.status == "queued", "未终态不许提前写死"
