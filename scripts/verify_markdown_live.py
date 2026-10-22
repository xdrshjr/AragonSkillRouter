"""Live Markdown round trip: a real model answer rendered by the panel's component.

Why this exists: `verify-markdown.mjs` proves the renderer handles Markdown syntax,
but not that a real model answer, arriving through the platform, ends up as elements
in the assistant bubble. This closes that gap without ever handling the plaintext key:

  1. a throwaway instance of the installed app is started with the user's keyring
     file (SR_MASTER_KEY_FILE) so the *existing* encrypted credential can be used —
     the secret is never decrypted outside the app;
  2. the provider row + credential row (ciphertext only) are copied into the throwaway
     database, plus an `agent.model` setting;
  3. one real turn is requested and the assistant answer is read back through the API;
  4. that exact text is piped through `desktop/scripts/render-markdown.mjs` and the HTML
     is checked for heading / strong / code / table elements.

Run from the repo root:
    .venv\\Scripts\\python scripts\\verify_markdown_live.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
DESKTOP = REPO / "desktop"
EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Skill Router" / "Skill Router.exe"
APP_DATA = Path(os.environ.get("APPDATA", "")) / "Skill Router" / "data"
PROBE = DESKTOP / "build" / "md-live"
PASSWORD = "md-Live!Passw0rd7"
NEW_PASSWORD = "md-Live!Passw0rd9"
MODEL = "glm-5.3-flash"
BASE_URL = "https://open.bigmodel.cn/api/anthropic"

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def seed_probe_database() -> None:
    """Copy the provider + encrypted credential into a fresh probe database."""
    user_db = APP_DATA / "skillrouter.db"
    if not user_db.exists():
        raise SystemExit(f"no user database at {user_db}")

    source = sqlite3.connect(f"file:{user_db}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    row = source.execute(
        """SELECT c.id, c.name, c.auth_type, c.secret_ciphertext, c.hint, c.field_names, c.config, c.key_id,
                  p.id AS provider_id, p.name AS provider_name, p.slug, p.base_url, p.category
             FROM credentials c JOIN providers p ON p.id = c.provider_id
            WHERE c.status = 'active' AND p.category = 'llm'
            ORDER BY c.created_at LIMIT 1"""
    ).fetchone()
    source.close()
    if row is None:
        raise SystemExit("no active llm credential in the user database")

    probe_db = PROBE / "data" / "skillrouter.db"
    target = sqlite3.connect(probe_db, timeout=15.0)
    stamp = now_iso()
    target.execute(
        """INSERT OR REPLACE INTO providers
             (id, name, slug, category, base_url, docs_url, tags, environment, notes, status, created_at, updated_at)
           VALUES (?, ?, ?, 'llm', ?, NULL, '["llm"]', 'production', NULL, 'active', ?, ?)""",
        (row["provider_id"], row["provider_name"], row["slug"], row["base_url"], stamp, stamp),
    )
    # The ciphertext is copied verbatim: the probe uses the same keyring file, so the
    # app decrypts it in memory exactly as it does for the real install.
    target.execute(
        """INSERT OR REPLACE INTO credentials
             (id, provider_id, name, auth_type, secret_ciphertext, hint, field_names, config,
              key_id, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (
            row["id"],
            row["provider_id"],
            row["name"],
            row["auth_type"],
            row["secret_ciphertext"],
            row["hint"],
            row["field_names"],
            json.dumps({"model": MODEL}),
            row["key_id"],
            stamp,
            stamp,
        ),
    )
    target.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('agent.model', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            json.dumps(
                {"credential_id": row["id"], "model_id": MODEL, "provider_id": "anthropic", "base_url": BASE_URL}
            ),
            stamp,
        ),
    )
    target.commit()
    target.close()
    record(True, "probe instance seeded with the existing encrypted credential", f"provider={row['provider_name']}")


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


def render_markdown(text: str) -> str:
    proc = subprocess.run(
        ["node", str(DESKTOP / "scripts" / "render-markdown.mjs")],
        input=text.encode("utf-8"),
        capture_output=True,
        cwd=str(DESKTOP),
        timeout=180,
    )
    if proc.returncode != 0:
        raise SystemExit(f"render-markdown failed: {proc.stderr.decode('utf-8', 'replace')[:400]}")
    return proc.stdout.decode("utf-8", "replace")


def main() -> int:
    if not EXE.exists():
        print(f"installed app not found: {EXE}")
        return 2
    if PROBE.exists():
        shutil.rmtree(PROBE, ignore_errors=True)
    (PROBE / "data").mkdir(parents=True, exist_ok=True)

    # Bootstrap the probe schema with the app's own frozen binary.
    # The backend reads SR_DATA_DIR (that is what the desktop supervisor sets);
    # SR_DESKTOP_DATA_DIR is the shell's own variable and means nothing to it.
    env = {
        **os.environ,
        "SR_DATA_DIR": str(PROBE),
        "SR_DATABASE_PATH": str(PROBE / "data" / "skillrouter.db"),
        "SR_MASTER_KEY_FILE": str(APP_DATA / "keys" / "master.keyring.json"),
        "SR_INITIAL_ADMIN_PASSWORD": PASSWORD,
        "SR_DESKTOP_DATA_DIR": str(PROBE),
    }
    backend = EXE.parent / "resources" / "runtime" / "backend" / "skillrouter-backend.exe"
    boot = subprocess.run([str(backend), "bootstrap", "--json"], env=env, capture_output=True, timeout=180)
    if boot.returncode != 0:
        print(boot.stdout.decode("utf-8", "replace")[-400:], boot.stderr.decode("utf-8", "replace")[-400:])
        return 2
    record(True, "probe database bootstrapped", boot.stdout.decode("utf-8", "replace").strip()[:110])

    # The desktop supervisor decides the keyring path itself
    # (`<data>/keys/master.keyring.json`), overriding SR_MASTER_KEY_FILE. To reuse the
    # existing encrypted credential we therefore place the user's keyring exactly where
    # the probe expects it — the probe then decrypts in memory, like the real install.
    user_keyring = APP_DATA / "keys" / "master.keyring.json"
    probe_keyring = PROBE / "data" / "keys" / "master.keyring.json"
    probe_keyring.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(user_keyring, probe_keyring)
    record(True, "probe uses the existing master keyring", "copied, never printed")

    seed_probe_database()

    app_proc = subprocess.Popen(
        [str(EXE)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 150
        runtime = None
        while time.time() < deadline:
            runtime = read_runtime()
            if runtime and runtime.get("shellPort"):
                break
            time.sleep(0.5)
        if not runtime:
            record(False, "probe app started", "runtime.json never appeared")
            return 1
        origin = f"http://127.0.0.1:{runtime['shellPort']}"
        if not wait_api(origin):
            record(False, "probe API ready", "health never became ready")
            return 1
        record(True, "probe app running", f"shell port {runtime['shellPort']}")

        client = httpx.Client(base_url=origin, timeout=120.0, trust_env=False)
        login = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        if login.status_code != 200:
            record(False, "sign-in", login.text[:200])
            return 1
        csrf = login.json()["csrf_token"]
        if login.json().get("must_change_password"):
            changed = client.post(
                "/api/auth/change-password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers={"x-csrf-token": csrf},
            )
            csrf = changed.json().get("csrf_token", csrf)
        client.headers.update({"x-csrf-token": csrf})

        health = client.get("/api/admin/agent/health").json()
        model = health.get("platform", {}).get("model", {})
        record(model.get("configured") is True, "probe instance resolves a model", f"{model.get('provider_id')}/{model.get('model_id')}")

        session = client.post("/api/admin/agent/sessions", json={"title": "markdown round trip"}).json()
        sent = client.post(
            f"/api/admin/agent/sessions/{session['id']}/messages",
            json={
                "text": (
                    "Answer with exactly these four blocks and nothing else: "
                    "a level-2 markdown heading named Findings; then one bold word; "
                    'then a fenced code block tagged json containing {"ok":true}; '
                    "then a markdown table with two body rows."
                ),
                "attachments": [],
                "queue_if_busy": False,
            },
        )
        record(sent.status_code in (200, 202), "message accepted", f"HTTP {sent.status_code}")

        answer = ""
        deadline = time.time() + 180
        while time.time() < deadline:
            detail = client.get(f"/api/admin/agent/sessions/{session['id']}").json()
            assistant = [m for m in detail.get("messages", []) if m.get("role") == "assistant"]
            if assistant and assistant[-1]["content"].strip():
                answer = assistant[-1]["content"]
                break
            time.sleep(1.5)
        if not answer:
            record(False, "assistant answered", "no assistant message within 180 s")
            return 1
        record(True, "assistant answered", f"{len(answer)} chars")
        record(
            any(token in answer for token in ("##", "**", "```", "|")),
            "the model answer contains Markdown syntax",
            answer.replace("\n", " ")[:110],
        )

        html = render_markdown(answer)
        record("<h" in html and ("Findings" in html), "heading rendered as an element", "")
        record("<strong" in html, "bold rendered as a strong element", "")
        record("<pre" in html or "<code" in html, "code block rendered through CodeBlock", "")
        record("<table" in html, "table rendered as a table element", "")
        record("&lt;h2&gt;" not in html and "```" not in html, "no Markdown leaked as literal text", "")

        client.close()
    finally:
        app_proc.terminate()
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
