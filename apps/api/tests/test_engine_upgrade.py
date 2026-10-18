"""检测 / 验证失败回滚 / 成功切换 / 升级期间调用不受影响（C6）。"""

from __future__ import annotations

import pytest

from skillrouter_api.db import db_write
from skillrouter_api.db.seed import set_state
from skillrouter_api.errors import ApiError
from skillrouter_api.settings import settings
from skillrouter_api.upgrade import build, detect, rollback, state, switch, verify
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
    monkeypatch.setattr(build, "_run", lambda cmd, cwd, timeout=0: (1, f"模拟失败：{' '.join(cmd)}"))
    _set_pin("0.2.34@0a119a6")
    run_id = state.create_run(to_version="0.8.0", mode="verify_only", policy="manual")
    state.mark_step(run_id, "detected", "passed", "候选 0.8.0")
    built = build.build({"version": "0.8.0", "provenance": "npm-only"})
    assert built["ok"] is False
    assert built["stage"] in ("npm_pack", "clone")
    state.finish_run(run_id, result="failed", error=built["stage"])
    assert state.get_run(run_id)["result"] == "failed"
    assert detect.current_pin().startswith("0.2.34"), "构建失败不得改变 pin"


def test_conformance_suite_runs_offline_checks() -> None:
    report = verify.conformance_suite({"version": "0.2.34"}, artifact_dir=None)
    names = {c["check"] for c in report["checks"]}
    assert "工具清单" in names
    assert "数据兼容" in names
    assert report["status"] in ("passed", "failed")
    assert report["needs_migration"] is False


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


async def test_switch_success_updates_pin_and_pointer(monkeypatch) -> None:
    _set_pin("0.2.34@0a119a6", "0.2.30@old")
    gateway = FakeGateway()
    patch_gateway(monkeypatch, gateway)
    run_id = state.create_run(to_version="0.2.99", mode="deploy", policy="manual")
    result = await switch.switch(run_id, {"version": "0.2.99", "provenance": "tagged"})
    assert result["ok"] is True, result
    assert detect.current_pin().startswith("0.2.99")
    pointer = switch.read_pointer()
    assert pointer["version"] == "0.2.99"
    assert pointer["home"].endswith("0.2.99")
    assert detect.previous_pin().startswith("0.2.34")
    assert state.get_run(run_id)["result"] == "success"


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
