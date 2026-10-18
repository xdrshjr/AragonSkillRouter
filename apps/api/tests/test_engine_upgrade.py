"""检测 / 验证三态 / 备份优先 / 装入 home / 状态兼容 / 升级期间调用不受影响（C6 + §A.7.1）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillrouter_api.db import db_write, now_iso, uuid_pk
from skillrouter_api.db.seed import set_state
from skillrouter_api.errors import ApiError
from skillrouter_api.models import EngineCandidate
from skillrouter_api.settings import settings
from skillrouter_api.upgrade import (
    build,
    compat,
    detect,
    install_home,
    jobs,
    rollback,
    state,
    switch,
    verify,
)
from tests.fixtures.fake_gateway import FakeGateway, patch_gateway
from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx


def _abort_running() -> None:
    run = state.current_run()
    if run:
        state.finish_run(run["id"], result="aborted", error="pytest isolation")


@pytest.fixture(autouse=True)
def _isolate_upgrade_runs():
    """每个用例前后都清掉未结束的升级运行，避免「已有升级正在执行」互相污染。"""
    _abort_running()
    yield
    _abort_running()


def _set_pin(pin: str, previous: str = "") -> None:
    with db_write() as s:
        set_state(s, "engine_pin", pin)
        set_state(s, "engine_previous_pin", previous)


def _fake_artifact(version: str, *, provenance: str = "tagged", manifest: dict | None = None) -> Path:
    """伪造一个结构完整的候选产物（`engine-builds/<version>-<provenance>/runtime`）。"""
    root = settings.builds_dir / f"{version}-{provenance}" / "runtime"
    dist = root / "packages" / "core" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.js").write_text("export const booted = 1;\n", encoding="utf-8")
    (dist / "index.d.ts").write_text(
        "export { AragonAgent, Agent, defineTool, initProviders, getProviderRegistry } "
        "from './engine/agent.js';\n",
        encoding="utf-8",
    )
    payload = {
        "version": version,
        "commit": "0a119a64890717b0e3017edbaaf00944babf19a8",
        "pruned_lock_sha256": "sha256:test",
        "node": "v22.0.0",
        "built_at": "2026-01-01T00:00:00Z",
    }
    payload.update(manifest or {})
    (root / "build-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _create_candidate(version: str, status: str) -> None:
    with db_write() as s:
        s.add(
            EngineCandidate(
                id=uuid_pk("egc"),
                version=version,
                commit_sha=None,
                provenance="tagged",
                published_at=None,
                channel="stable",
                notes="pytest",
                detected_at=now_iso(),
                verification_status=status,
                verification_report={},
            )
        )


def test_version_compare() -> None:
    assert detect.compare("0.2.37", "0.2.34") > 0
    assert detect.compare("0.2.34", "0.2.34") == 0
    assert detect.compare("0.1.9", "0.2.0") < 0


def test_detect_skips_network_when_egress_denied(monkeypatch) -> None:
    monkeypatch.setattr(settings, "egress_policy", "deny")
    monkeypatch.setattr(detect, "_git_tags", lambda: [])
    result = detect.check()
    assert result["egress_policy"] == "deny"
    assert result["note"] and "deny" in result["note"]


def test_detect_records_tagged_candidate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "egress_policy", "deny")
    monkeypatch.setattr(
        detect,
        "_git_tags",
        lambda: [
            {"version": "0.2.34", "tag": "core-v0.2.34", "commit": None, "published_at": None},
            {"version": "0.9.0", "tag": "core-v0.9.0", "commit": "abc123", "published_at": None},
        ],
    )
    result = detect.check()
    assert "0.9.0" in [item["version"] for item in result["detected"]]
    candidates = {c["version"]: c for c in detect.list_candidates()}
    assert candidates["0.9.0"]["provenance"] == "tagged"


def test_detect_npm_only_provenance(monkeypatch) -> None:
    monkeypatch.setattr(settings, "egress_policy", "allow")
    monkeypatch.setattr(detect, "_git_tags", lambda: [])
    monkeypatch.setattr(
        detect,
        "_npm_versions",
        lambda: {"core_latest": "0.2.37", "cli_latest": "0.6.25", "integrity": "sha512-x", "published_at": None},
    )
    result = detect.check()
    npm_items = [i for i in result["detected"] if i["provenance"] == "npm-only"]
    assert npm_items and npm_items[0]["version"] == "0.2.37"


def test_upgrade_rejects_concurrent_runs(client, admin) -> None:
    state.create_run(to_version="0.9.0", mode="verify_only", policy="manual")
    assert state.current_run() is not None
    r = client.post(
        "/api/admin/engine/upgrade",
        headers=admin["headers"],
        json={"version": "0.9.9", "mode": "verify_only"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "UPGRADE_IN_PROGRESS"


def test_build_failure_reported_without_touching_pin(client, admin, monkeypatch) -> None:
    # 本用例验证的是**本地工具链**的失败分支（生产默认走网关，见 test_upgrade_toolchain.py）
    monkeypatch.setattr(settings, "engine_toolchain", "local")
    monkeypatch.setattr(build, "_run", lambda cmd, cwd, timeout=0: (1, f"模拟失败：{' '.join(cmd)}"))
    _set_pin("0.2.34@0a119a6")
    run_id = state.create_run(to_version="0.8.0", mode="verify_only", policy="manual")
    state.mark_step(run_id, "detected", "passed", "候选 0.8.0")
    built = build.build({"version": "0.8.0", "provenance": "npm-only"})
    assert built["ok"] is False
    assert built["status"] == "failed"
    assert built["stage"] in ("npm_pack", "clone")
    state.finish_run(run_id, result="failed", error=built["stage"])
    assert state.get_run(run_id)["result"] == "failed"
    assert detect.current_pin().startswith("0.2.34"), "构建失败不得改变 pin"


def test_conformance_suite_runs_offline_checks() -> None:
    """无产物时代价最小的结论是 blocked（「不能验证」必须说出来，永不 passed）。"""
    report = verify.conformance_suite({"version": "0.2.34"}, artifact_dir=None)
    names = {c["check"] for c in report["checks"]}
    assert "工具清单" in names
    assert "状态兼容" in names
    assert report["status"] == "blocked"
    assert report["verify_status"] == "blocked"
    assert report["needs_migration"] is False
    assert all(c["detail"] for c in report["checks"]), "每一项都必须有 detail"
    assert {c["status"] for c in report["checks"]} <= {"passed", "failed", "blocked"}, "skipped 已删除"


async def test_switch_keeps_old_engine_when_reload_fails(monkeypatch) -> None:
    _set_pin("0.2.34@0a119a6", "0.2.30@old")
    gateway = FakeGateway(reload_ok=False)
    patch_gateway(monkeypatch, gateway)
    pin_before = detect.current_pin()
    run_id = state.create_run(to_version="0.7.0", mode="deploy", policy="manual")
    result = await switch.switch(run_id, {"version": "0.7.0", "provenance": "tagged"})
    assert result["ok"] is False
    assert result["stage"] in ("reload", "start_child")
    assert "engine_pin 未改变" in result["note"]
    assert detect.current_pin() == pin_before
    # 先备份：切换尝试过就必须留下备份事实
    assert result["backup_file_id"]


async def test_switch_success_updates_pin_and_pointer(monkeypatch) -> None:
    _set_pin("0.2.34@0a119a6", "0.2.30@old")
    gateway = FakeGateway()
    patch_gateway(monkeypatch, gateway)
    artifact = _fake_artifact("0.2.99")
    run_id = state.create_run(to_version="0.2.99", mode="deploy", policy="manual")
    result = await switch.switch(
        run_id, {"version": "0.2.99", "provenance": "tagged", "artifact_dir": str(artifact)}
    )
    assert result["ok"] is True, result
    assert detect.current_pin().startswith("0.2.99")
    pointer = switch.read_pointer()
    assert pointer["version"] == "0.2.99"
    assert pointer["home"].endswith("0.2.99")
    assert detect.previous_pin().startswith("0.2.34")
    assert state.get_run(run_id)["result"] == "success"
    # 指针指向已安装的 home，且 pin_source 为 pointer
    assert switch.pin_source() == "pointer"
    assert Path(switch.resolved_path()) == install_home.target("0.2.99")


async def test_rollback_restores_previous(monkeypatch) -> None:
    _set_pin("0.2.99@newsha", "0.2.34@0a119a6")
    gateway = FakeGateway(engine_version="0.2.99")
    patch_gateway(monkeypatch, gateway)
    result = await rollback.rollback_to_previous(reason="pytest")
    assert result["ok"] is True, result
    assert result["to_version"] == "0.2.34"
    assert detect.current_pin().startswith("0.2.34")
    assert switch.read_pointer()["version"] == "0.2.34"


def test_rollback_history_lists_deployments(client, admin, monkeypatch) -> None:
    _set_pin("0.2.34@0a119a6", "")
    patch_gateway(monkeypatch, FakeGateway())
    r = client.get("/api/admin/engine/deployments", headers=admin["headers"])
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)


def test_engine_status_endpoint(client, admin, monkeypatch) -> None:
    patch_gateway(monkeypatch, FakeGateway())
    r = client.get("/api/admin/engine/status", headers=admin["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["engine"]["version"] == "0.2.34"
    assert body["adapter_version"] == "1"
    assert body["pinned_package"] == "@aragon-agent/core"
    caps = {c["state"] for c in body["capabilities"]}
    assert "available" in caps and "unsupported" in caps
    assert body["upgrade_in_progress"] is False
    # §A.5.3：「装了但没生效」的排查字段
    assert body["pin_source"] in ("pointer", "env")
    assert body["resolved_path"]
    assert "verify_status" in body and isinstance(body["blocked_reasons"], list)


def test_engine_status_when_gateway_down(client, admin, monkeypatch) -> None:
    patch_gateway(monkeypatch, FakeGateway(available=False))
    r = client.get("/api/admin/engine/status", headers=admin["headers"])
    assert r.status_code == 200
    assert r.json()["engine"]["reachable"] is False
    assert r.json()["engine"]["status"] == "unavailable"


def test_engine_capability_groups(client, admin) -> None:
    r = client.get("/api/admin/engine/capabilities", headers=admin["headers"])
    assert r.status_code == 200
    groups = r.json()["groups"]
    assert groups["unsupported"]
    assert groups["permission_limited"]
    assert "不得因为引擎版本升级" in r.json()["note"]


def test_calls_during_upgrade_are_unaffected(client, admin, make_capability, make_token, monkeypatch) -> None:
    """C6：升级作业与执行路径隔离，已发布能力调用成功率 100%。"""
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(key="text.generate.duringupgrade")
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test",
        headers=admin["headers"],
        json={"input": {"prompt": "x"}},
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    state.create_run(to_version="0.6.0", mode="verify_only", policy="manual")
    ok = 0
    for index in range(3):
        r = client.post(
            f"/api/v1/capabilities/{cap['key']}/invoke",
            headers=token["headers"],
            json={"input": {"prompt": f"调用 {index}"}},
        )
        if r.status_code == 200 and r.json()["status"] == "succeeded":
            ok += 1
    assert ok == 3


def test_upgrade_endpoint_queues_run(client, admin) -> None:
    r = client.post(
        "/api/admin/engine/upgrade",
        headers=admin["headers"],
        json={"version": "0.3.0", "mode": "verify_only"},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    detail = client.get(f"/api/admin/engine/runs/{run_id}", headers=admin["headers"])
    assert detail.status_code == 200
    assert detail.json()["to_version"] == "0.3.0"
    assert detail.json()["mode"] == "verify_only"


def test_invalid_upgrade_mode(client, admin) -> None:
    r = client.post(
        "/api/admin/engine/upgrade",
        headers=admin["headers"],
        json={"version": "0.3.0", "mode": "yolo"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_engine_check_endpoint(client, admin, monkeypatch) -> None:
    monkeypatch.setattr(settings, "egress_policy", "deny")
    monkeypatch.setattr(detect, "_git_tags", lambda: [])
    r = client.post("/api/admin/engine/check", headers=admin["headers"])
    assert r.status_code == 200
    assert r.json()["egress_policy"] == "deny"


def test_rollback_endpoint_without_previous(client, admin, monkeypatch) -> None:
    _set_pin("0.2.34@0a119a6", "")
    patch_gateway(monkeypatch, FakeGateway())
    r = client.post("/api/admin/engine/rollback", headers=admin["headers"], json={})
    assert r.status_code == 200
    assert r.json().get("ok") is False
    assert "没有可回滚" in r.json().get("detail", "")


def test_api_error_codes_registered_for_engine() -> None:
    for code in ("UPGRADE_IN_PROGRESS", "ENGINE_UNAVAILABLE", "ENGINE_SWITCH_FAILED"):
        with pytest.raises(ApiError) as exc:
            raise ApiError(code)
        assert exc.value.status in (409, 503, 500)


# ---------------------------------------------------------------------------
# T-16 … T-21（§A.7.1）
# ---------------------------------------------------------------------------


def test_verify_blocked_without_artifact(monkeypatch) -> None:
    """T-16：无 artifact_dir 或网关不可达 → `blocked`，且**永不** `passed`。"""
    report = verify.conformance_suite({"version": "0.2.61"}, None)
    assert report["verify_status"] == "blocked"
    assert report["needs_adapter_review"]
    assert report["status"] != "passed"

    # 有产物但网关不可达：node 侧检查（导出清单/事件映射）只能 blocked
    artifact = _fake_artifact("0.2.61")
    patch_gateway(monkeypatch, FakeGateway(available=False))
    report = verify.conformance_suite({"version": "0.2.61"}, str(artifact))
    node_checks = {c["check"]: c for c in report["checks"]}
    assert node_checks["导出清单兼容"]["status"] == "blocked"
    assert node_checks["事件映射契约"]["status"] == "blocked"
    assert node_checks["构建产物完整性"]["status"] == "passed"
    assert report["verify_status"] == "blocked"
    assert "导出清单兼容" in report["needs_adapter_review"]
    assert report["status"] != "passed"


def test_verify_fails_on_bad_manifest(monkeypatch) -> None:
    """T-17：manifest 版本不符 → `failed`，`detail` 指出不一致字段。"""
    artifact = _fake_artifact("0.2.62", manifest={"version": "0.2.50"})
    patch_gateway(monkeypatch, FakeGateway())
    report = verify.conformance_suite({"version": "0.2.62"}, str(artifact))
    manifest_check = next(c for c in report["checks"] if c["check"] == "构建清单一致")
    assert manifest_check["status"] == "failed"
    assert "version" in manifest_check["detail"]
    assert "0.2.50" in manifest_check["detail"]
    assert report["verify_status"] == "failed"


async def test_blocked_blocks_auto_switch(monkeypatch) -> None:
    """T-18：`policy=auto` + `blocked` → 不切换；切不动时结论/结果分列。"""
    _set_pin("0.2.34@0a119a6", "")
    gateway = FakeGateway()
    patch_gateway(monkeypatch, gateway)
    run_id = state.create_run(to_version="0.2.63", mode="deploy", policy="auto")
    state.set_verify_status(run_id, "blocked", ["冒烟调用"])
    result = await switch.switch(run_id, {"version": "0.2.63", "provenance": "tagged"})
    assert result["ok"] is False
    assert result["needs_acknowledge"] is True
    assert gateway.reloads == [], "blocked 未确认时不得调用网关切换"
    assert detect.current_pin().startswith("0.2.34")
    assert switch.read_pointer().get("version") != "0.2.63"
    run = state.get_run(run_id)
    assert run["verify_status"] == "blocked", "result 不得顶替 verify_status"
    assert run["result"] == "aborted"
    assert run["blocked_reasons"] == ["冒烟调用"]

    # 管理员确认后允许切换（acknowledge_blocked=true）
    run_id2 = state.create_run(to_version="0.2.63", mode="deploy", policy="manual")
    state.set_verify_status(run_id2, "blocked", ["冒烟调用"])
    result2 = await switch.switch(
        run_id2, {"version": "0.2.63", "provenance": "tagged"}, acknowledge_blocked=True
    )
    assert result2["ok"] is True, result2
    assert gateway.reloads, "确认后应调用一次网关切换"


async def test_auto_policy_does_not_deploy_blocked_candidate(monkeypatch) -> None:
    """T-18（作业层）：`policy=auto` + `blocked` → 进「待升级」，不自动切换。"""
    _set_pin("0.2.34@0a119a6", "")
    gateway = FakeGateway()
    patch_gateway(monkeypatch, gateway)
    artifact = _fake_artifact("0.2.68")
    blocked_report = {
        "status": "blocked",
        "verify_status": "blocked",
        "checks": [{"check": "冒烟调用", "status": "blocked", "detail": "无模型凭证"}],
        "needs_adapter_review": ["冒烟调用"],
        "needs_migration": False,
        "migration_hint": "",
    }
    monkeypatch.setattr(jobs, "policy", lambda: "auto")
    monkeypatch.setattr(
        build,
        "build",
        lambda candidate: {
            "ok": True,
            "status": "built",
            "stage": "built",
            "detail": "",
            "artifact_dir": str(artifact),
            "manifest": {"pruned_lock_sha256": "sha256:test"},
            "steps": [],
        },
    )
    monkeypatch.setattr(verify, "conformance_suite", lambda candidate, artifact_dir=None: blocked_report)
    run_id = state.create_run(to_version="0.2.68", mode="deploy", policy="auto")
    await jobs.handle({"run_id": run_id, "version": "0.2.68", "mode": "deploy"})
    run = state.get_run(run_id)
    assert run["verify_status"] == "blocked"
    assert run["blocked_reasons"] == ["冒烟调用"]
    assert run["result"] == "aborted"
    assert gateway.reloads == [], "auto + blocked 不得切换"
    assert not detect.current_pin().startswith("0.2.68")


def test_blocked_candidate_requires_acknowledge_in_api(client, admin) -> None:
    """T-18（接口层）：`blocked` 候选要部署，缺 `acknowledge_blocked` → 409 VALIDATION_FAILED。"""
    _create_candidate("0.2.64", "blocked")
    r = client.post(
        "/api/admin/engine/upgrade",
        headers=admin["headers"],
        json={"version": "0.2.64", "mode": "deploy"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    r2 = client.post(
        "/api/admin/engine/upgrade",
        headers=admin["headers"],
        json={"version": "0.2.64", "mode": "deploy", "acknowledge_blocked": True},
    )
    assert r2.status_code == 202, r2.text


async def test_switch_backs_up_first_and_aborts_on_backup_failure(monkeypatch) -> None:
    """T-19：备份失败 → 无指针变更、`result='failed'`、`verify_status` 不变、`backup_file_id is None`。"""
    from skillrouter_api.services import backup_job

    _set_pin("0.2.34@0a119a6", "")
    gateway = FakeGateway()
    patch_gateway(monkeypatch, gateway)
    artifact = _fake_artifact("0.2.65")

    def boom(**kwargs):  # noqa: ANN003, ANN202
        raise OSError("磁盘写满：模拟备份失败")

    monkeypatch.setattr(backup_job, "quick_backup", boom)
    pointer_before = switch.read_pointer()
    run_id = state.create_run(to_version="0.2.65", mode="deploy", policy="manual")
    state.set_verify_status(run_id, "passed", [])
    result = await switch.switch(
        run_id, {"version": "0.2.65", "provenance": "tagged", "artifact_dir": str(artifact)}
    )
    assert result["ok"] is False
    assert result["stage"] == "backup"
    assert result["backup_file_id"] is None
    assert gateway.reloads == [], "备份失败后不得调用网关切换"
    assert switch.read_pointer() == pointer_before, "备份失败不得改动指针"
    run = state.get_run(run_id)
    assert run["result"] == "failed"
    assert run["verify_status"] == "passed", "verify_status 不被 result 顶替"
    assert run["backup_file_id"] is None


def test_installed_home_is_resolved(client, admin, monkeypatch) -> None:
    """T-20：产物装入 home 后解析到候选版本；`pin_source='pointer'`；指针损坏回落 `env`。"""
    artifact = _fake_artifact("0.2.66")
    installed = install_home.install(str(artifact), "0.2.66")
    assert installed["installed"] is True
    assert installed["path"].replace("\\", "/").endswith("0.2.66/node_modules/@aragon-agent/core")
    package = json.loads((Path(installed["path"]) / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == "0.2.66", "解析出的 package.json.version 必须是候选版本"
    assert install_home.resolved_version("0.2.66") == "0.2.66"
    # 幂等：重复安装不重复拷贝
    again = install_home.install(str(artifact), "0.2.66")
    assert again["installed"] is False and again["action"] == "reused"

    _set_pin("0.2.66@0a119a6", "")
    switch.write_pointer("0.2.66")
    assert switch.pin_source() == "pointer"
    assert switch.resolved_path() == installed["path"]

    gateway = FakeGateway(engine_version="0.2.66", pin_source="pointer", resolved_path=installed["path"])
    patch_gateway(monkeypatch, gateway)
    body = client.get("/api/admin/engine/status", headers=admin["headers"]).json()
    assert body["pin_source"] == "pointer"
    assert "0.2.66" in body["resolved_path"]

    # 指针损坏 → 回落 env（不静默）
    (settings.engine_runtime_dir / "current.json").write_text("{ 坏掉的 JSON", encoding="utf-8")
    assert switch.pin_source() == "env"


async def test_compat_detects_session_shape_change(tmp_path, monkeypatch) -> None:
    """T-21：会话结构变化 → `needs_migration=True` + `migration_hint`，且切换被拦。"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for index in range(3):
        (sessions / f"s{index}.json").write_text(
            json.dumps({"session_id": f"s{index}", "messages": [], "schema_version": 1}),
            encoding="utf-8",
        )
    (sessions / "broken.json").write_text(
        json.dumps({"session_id": "s9", "turns": [], "schema_version": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr(compat, "sessions_dir", lambda: sessions)

    result = compat.check({"version": "0.2.67", "schema_version": 1})
    assert result["needs_migration"] is True
    assert result["migration_hint"]
    assert result["sampled"] == 4 and result["total"] == 4

    report = verify.conformance_suite({"version": "0.2.67", "schema_version": 1}, None)
    state_check = next(c for c in report["checks"] if c["check"] == "状态兼容")
    assert state_check["status"] == "failed"
    assert "迁移提示" in state_check["detail"]
    assert report["needs_migration"] is True

    # 切换被拦：failed 的候选不得切换
    _set_pin("0.2.34@0a119a6", "")
    run_id = state.create_run(to_version="0.2.67", mode="deploy", policy="manual")
    state.set_verify_status(run_id, report["verify_status"], report["needs_adapter_review"])
    switch_result = await switch.switch(run_id, {"version": "0.2.67", "provenance": "tagged"})
    assert switch_result["ok"] is False
    assert switch_result["stage"] == "verify"
    assert detect.current_pin().startswith("0.2.34")
