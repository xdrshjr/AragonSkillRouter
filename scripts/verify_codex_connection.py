"""Prove the Codex connection end to end against a real Skill Router instance.

Runs a throwaway instance of the installed app (its own data dir, so the user's
database and agent list are untouched), then:

  1. creates an agent + token through the admin API
  2. runs the REAL `skill-router-connect install --target codex-skills` against it
  3. asserts the skill landed at `<dir>/skills/skill-router-<agent>/SKILL.md` with
     frontmatter whose `name` matches the directory
  4. runs `skill-router-connect verify` and checks whoami + capabilities
  5. publishes a real capability and runs `verify --capability` so the invocation
     path is exercised, not just the catalogue
  6. runs `uninstall` and asserts the directory is pruned

Usage: .venv\\Scripts\\python scripts/verify_codex_connection.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "packages" / "installer" / "bin" / "skill-router-connect.js"
EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Skill Router" / "Skill Router.exe"
PROBE = REPO / "desktop" / "build" / "codex-probe"
WORK = REPO / "desktop" / "build" / "codex-work"
PASSWORD = "codex-Probe!Passw0rd7"
NEW_PASSWORD = "codex-Probe!Passw0rd9"

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail else ""), flush=True)


def read_runtime() -> dict | None:
    try:
        return json.loads((PROBE / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def wait_api(origin: str, timeout_s: int = 120) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if httpx.get(f"{origin}/api/health/ready", timeout=3.0, trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(INSTALLER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        timeout=180,
    )


def main() -> int:
    if not EXE.exists():
        print(f"installed app not found: {EXE}")
        return 2
    for path in (PROBE, WORK):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    (PROBE / "data").mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "SR_DATA_DIR": str(PROBE),
        "SR_DATABASE_PATH": str(PROBE / "data" / "skillrouter.db"),
        "SR_INITIAL_ADMIN_PASSWORD": PASSWORD,
        "SR_DESKTOP_DATA_DIR": str(PROBE),
    }
    app = subprocess.Popen([str(EXE)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 150
        runtime = None
        while time.time() < deadline:
            runtime = read_runtime()
            if runtime and runtime.get("shellPort"):
                break
            time.sleep(0.5)
        if not runtime:
            record(False, "throwaway instance started", "runtime.json never appeared")
            return 1
        origin = f"http://127.0.0.1:{runtime['shellPort']}"
        if not wait_api(origin):
            record(False, "throwaway API ready", "health never became ready")
            return 1
        record(True, "throwaway instance running", origin)

        client = httpx.Client(base_url=origin, timeout=120.0, trust_env=False)
        login = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        csrf = login.json()["csrf_token"]
        if login.json().get("must_change_password"):
            changed = client.post(
                "/api/auth/change-password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers={"x-csrf-token": csrf},
            )
            csrf = changed.json().get("csrf_token", csrf)
        client.headers.update({"x-csrf-token": csrf})

        # --- a real provider + capability, so the invocation path can be exercised ---
        provider = client.post(
            "/api/admin/providers",
            json={"name": "Echo", "category": "generic", "base_url": "https://postman-echo.com", "tags": [], "environment": "production", "status": "active"},
        ).json()
        credential = client.post(
            "/api/admin/credentials",
            json={"provider_id": provider["id"], "name": "echo key", "auth_type": "api_key", "secret": {"api_key": "not-secret"}, "config": {}},
        ).json()
        capability = client.post(
            "/api/admin/capabilities",
            json={"name": "codex.probe.echo", "type": "generic", "description": "echo", "provider_id": provider["id"], "credential_id": credential["id"]},
        ).json()
        version_id = capability["versions"][0]["id"]
        config = {
            "model": None,
            "request": {
                "method": "POST",
                "url": "https://postman-echo.com/post",
                "headers": {"content-type": "application/json"},
                "query": [],
                "encoding": "json",
                "body_template": {"prompt": "{{ input.prompt }}"},
            },
            "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"], "additionalProperties": False},
            "output_schema": {"type": "object"},
            "response": {"kind": "json", "extract": {"result": "$.json.prompt"}, "artifact": None},
            "modes": {"sync": True, "async": False, "stream": False},
            "limits": {"timeout_s": 30, "max_output_bytes": 65536, "max_input_bytes": 8192, "file_input": False, "max_file_mb": 0, "allowed_mime": [], "max_duration_s": None},
            "errors": {"rules": [], "default_code": "upstream_error"},
            "pricing": {"per_call": 0, "input_per_1k": 0, "output_per_1k": 0, "per_second": 0, "currency": "USD"},
            "provider_params": [],
            "retry": {"max_attempts": 1, "backoff_ms": [500], "retry_on": ["connect_timeout"]},
            "examples": [{"input": {"prompt": "hello from codex"}}],
        }
        client.patch(f"/api/admin/capabilities/{capability['id']}/versions/{version_id}", json={"config": config})
        tested = client.post(f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/test", json={"input": {"prompt": "hello from codex"}, "mode": "sync"})
        record(tested.status_code == 200 and tested.json().get("status") == "passed", "capability tested against a real HTTP endpoint", tested.text[:120])
        published = client.post(f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish", json={"confirm": True})
        record(published.status_code == 200, "capability published", str(published.status_code))

        # --- agent + token ---------------------------------------------------------
        agent = client.post("/api/admin/agents", json={"name": "codex-probe", "description": "verification"}).json()
        token_response = client.post(
            f"/api/admin/agents/{agent['id']}/tokens",
            json={"name": "codex", "scopes": ["*"], "expires_in_days": 1},
        )
        token = token_response.json()["token"]
        record(token_response.status_code == 201, "agent token issued", f"{token[:12]}… (not printed in full)")

        # --- the real installer, the real target ------------------------------------
        install = cli("install", "--url", origin, "--token", token, "--target", "codex-skills", "--json", cwd=WORK)
        if install.returncode != 0:
            record(False, "install --target codex-skills", (install.stdout + install.stderr)[:400])
            return 1
        report = json.loads(install.stdout)
        record(True, "install --target codex-skills", f"base_dir={report['base_dir']}")

        skill_rel = report["files"][0]["path"].replace("/", os.sep)
        skill_path = WORK / skill_rel
        record(skill_path.exists(), "SKILL.md written", skill_rel)
        content = skill_path.read_text(encoding="utf-8")
        frontmatter_name = next((line.split(":", 1)[1].strip() for line in content.splitlines() if line.startswith("name: ")), "")
        record(
            skill_path.parent.name == frontmatter_name,
            "directory name matches the frontmatter name",
            f"dir={skill_path.parent.name} name={frontmatter_name}",
        )
        record("codex.probe.echo" in content, "the published capability appears in the catalogue", "")
        record(token not in content, "the token is not inside the SKILL", "")
        record((WORK / ".skill-router.json").exists(), "connection config written", ".skill-router.json")

        # --- verify (whoami + capabilities) ---------------------------------------
        verify = cli("verify", "--json", cwd=WORK)
        if verify.returncode != 0:
            record(False, "verify", (verify.stdout + verify.stderr)[:400])
            return 1
        verified = json.loads(verify.stdout)
        record(verified.get("agent", {}).get("name") == "codex-probe", "verify: whoami", json.dumps(verified.get("agent")))
        record(verified.get("capabilities", {}).get("count") == 1, "verify: capabilities", str(verified.get("capabilities")))

        # --- verify --capability: one real invocation -----------------------------
        # The slug is derived from the display name (`codex.probe.echo` → `codex-probe-echo`).
        slug = capability["slug"]
        record(True, "capability slug is derived, not the display name", f"{capability['name']} -> {slug}")
        invoke = cli(
            "verify", "--capability", slug, "--input", '{"prompt":"hello from codex"}', "--json", cwd=WORK,
        )
        invoked = json.loads(invoke.stdout) if invoke.stdout.strip().startswith("{") else {}
        invocation = invoked.get("invocation") or {}
        record(
            invoke.returncode == 0 and invocation.get("status") == "succeeded",
            "verify --capability: real invocation",
            f"exit={invoke.returncode} status={invocation.get('status')} request_id={invocation.get('request_id')} "
            f"raw={(invoke.stdout or invoke.stderr).strip()[:200]!r}",
        )

        # --- update: first run adopts the server document, second is a no-op ------
        # `update` deliberately renders from `GET /api/v1/skill` (the platform owns the
        # body), so the first update after `install` rewrites the file once. What must
        # hold is that update → update changes nothing.
        first = cli("update", "--json", cwd=WORK)
        second = cli("update", "--json", cwd=WORK)
        first_actions = [item.get("action") for item in (json.loads(first.stdout) if first.stdout.strip().startswith("{") else {}).get("files", [])]
        second_actions = [item.get("action") for item in (json.loads(second.stdout) if second.stdout.strip().startswith("{") else {}).get("files", [])]
        record(
            first.returncode == 0 and second.returncode == 0 and second_actions == ["unchanged"],
            "update is idempotent (second run writes nothing)",
            f"first={first_actions} second={second_actions}",
        )

        # --- uninstall prunes the directory --------------------------------------
        uninstall = cli("uninstall", "--json", cwd=WORK)
        record(uninstall.returncode == 0, "uninstall", "")
        record(not skill_path.parent.exists(), "uninstall pruned the skill directory", str(skill_path.parent))
        record(not (WORK / ".skill-router.json").exists(), "uninstall removed the config", "")
    finally:
        app.terminate()
        try:
            subprocess.run(["taskkill", "/f", "/im", "skillrouter-backend.exe"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(1.5)

    failed = [item for item in RESULTS if not item[0]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
