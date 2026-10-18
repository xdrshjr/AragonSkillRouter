"""工具链归属（§A.3.7-0 / T-41）：构建必须能区分「环境跑不了」与「候选坏了」。

三条断言：
1. `ENGINE_TOOLCHAIN=gateway` + 网关不可达 → `blocked`（不是 `failed`），且**不产生候选行**；
2. `local` 在非 development 环境被忽略（生产不存在 node/npm/git）；
3. 构建产物路径必须落在 `settings.data_path` 下（防网关回传越权路径）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillrouter_api.settings import settings
from skillrouter_api.upgrade import build, detect, jobs, state, toolchain
from tests.fixtures.fake_gateway import FakeGateway, patch_gateway


def _abort_running() -> None:
    run = state.current_run()
    if run:
        state.finish_run(run["id"], result="aborted", error="pytest isolation")


@pytest.fixture(autouse=True)
def _isolate_upgrade_runs():
    _abort_running()
    yield
    _abort_running()


async def test_build_blocked_when_gateway_unreachable(monkeypatch) -> None:
    """T-41：网关不可达 → `blocked`（不是 `failed`），且不产生候选行。"""
    monkeypatch.setattr(settings, "engine_toolchain", "gateway")
    patch_gateway(monkeypatch, FakeGateway(available=False))
    before = {c["version"] for c in detect.list_candidates()}

    result = build.build({"version": "0.3.99", "provenance": "tagged"})
    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert "网关不可达" in result["detail"]
    assert result["artifact_dir"] is None

    # 走完整作业：不产生候选行、不写 failed、run 以 aborted 结束
    run_id = state.create_run(to_version="0.3.99", mode="verify_only", policy="manual")
    await jobs.handle({"run_id": run_id, "version": "0.3.99", "mode": "verify_only"})
    after = {c["version"] for c in detect.list_candidates()}
    assert after == before, "工具链不可用时不得产生候选行"
    run = state.get_run(run_id)
    assert run["result"] == "aborted"
    assert run["verify_status"] == "blocked"
    assert any("工具链" in reason for reason in run["blocked_reasons"])


def test_local_toolchain_ignored_outside_development(monkeypatch) -> None:
    """`local` 仅在 development 可用；生产忽略并记 `toolchain_local_ignored`。"""
    monkeypatch.setattr(settings, "engine_toolchain", "local")
    monkeypatch.setattr(settings, "env", "production")
    assert toolchain.resolve() == "gateway"

    monkeypatch.setattr(settings, "env", "development")
    assert toolchain.resolve() == "local"

    monkeypatch.setattr(settings, "engine_toolchain", "gateway")
    assert toolchain.resolve() == "gateway"


def test_build_artifact_stays_under_data_dir(monkeypatch) -> None:
    """产物路径必须在 `settings.data_path` 下；越权路径一律 `blocked`。"""
    monkeypatch.setattr(settings, "engine_toolchain", "gateway")
    gateway = FakeGateway()
    patch_gateway(monkeypatch, gateway)

    result = build.build({"version": "0.3.98", "provenance": "tagged", "commit_sha": "abc123"})
    assert result["status"] == "built", result
    artifact = Path(result["artifact_dir"])
    assert toolchain.inside_data_dir(artifact)
    assert artifact.is_relative_to(settings.data_path)
    assert gateway.builds and gateway.builds[-1]["version"] == "0.3.98"

    gateway.build_result = {
        "ok": True,
        "status": "built",
        "stage": "built",
        "detail": "构建完成",
        "artifact_dir": "C:/Windows/System32",
        "manifest": {},
        "steps": [],
    }
    escaped = build.build({"version": "0.3.97", "provenance": "tagged"})
    assert escaped["status"] == "blocked"
    assert "不在数据目录" in escaped["detail"]
