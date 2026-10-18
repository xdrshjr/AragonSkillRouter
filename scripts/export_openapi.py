"""对运行中的 API 做一次 OpenAPI/健康检查，并导出 docs/api/openapi.json（构建产物）。"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "api" / "openapi.json"
BASE = "http://127.0.0.1:8000"


def main() -> int:
    health = json.loads(urllib.request.urlopen(f"{BASE}/healthz", timeout=15).read())
    print("healthz:", {k: health[k] for k in ("ok", "initialized", "env")}, health["db"])
    v1 = json.loads(urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=15).read())
    print("v1/health:", v1["status"], v1["version"], v1["schema_version"])
    spec = json.loads(urllib.request.urlopen(f"{BASE}/openapi.json", timeout=30).read())
    paths = spec["paths"]
    v1_paths = [p for p in paths if p.startswith("/api/v1")]
    admin_paths = [p for p in paths if p.startswith("/api/admin")]
    internal = [p for p in paths if p.startswith("/internal")]
    print(f"openapi: {len(paths)} paths (v1={len(v1_paths)}, admin={len(admin_paths)}, internal={len(internal)})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", OUT.relative_to(ROOT), f"{OUT.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
