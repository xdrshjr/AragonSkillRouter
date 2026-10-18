"""校验 spec §8「文件 / 模块变更计划」中的每个文件是否真实存在（DoD 第 1 条）。

    python scripts/check_file_plan.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "plans" / "skill-router" / "spec.md"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

# 计划里用目录/通配表达的条目
WILDCARDS = {
    "apps/web/components/ui/*.tsx": ("apps/web/components/ui", ("*.tsx", "*.ts")),
    "packages/skillrouter-cli/test/*.test.ts": ("packages/skillrouter-cli/test", ("*.test.ts",)),
}


def parse_plan() -> list[str]:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("## 8. 文件 / 模块变更计划")
    end = text.index("## 9. 接口设计")
    section = text[start:end]
    return _paths_in(section)


def parse_iteration_plan() -> list[str | list[str]]:
    """§A.4「迭代 2 文件 / 模块变更计划」四张表（T-40：新增文件必须同步进清单）。

    四张表的「文件」列是相对路径（表头写了作用目录），必须按小节补齐前缀。
    """
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("### A.4 文件 / 模块变更计划")
    end = text.index("### A.5 接口设计")
    section = text[start:end]
    prefixes = [
        ("#### A.4.1", "apps/api/skillrouter_api/"),
        ("#### A.4.2", "apps/api/tests/"),
        ("#### A.4.3", "apps/web/"),
        ("#### A.4.4", ""),
    ]
    paths: list[str] = []
    for index, (heading, prefix) in enumerate(prefixes):
        block_start = section.index(heading)
        block_end = (
            section.index(prefixes[index + 1][0]) if index + 1 < len(prefixes) else len(section)
        )
        for path in _paths_in(section[block_start:block_end]):
            # 少数条目虽列在 A.4.1，但实际位于 apps/api/ 下（如 migrations/versions/*.py）
            candidates = [path] if not prefix else [f"{prefix}{path}", f"apps/api/{path}"]
            paths.append(path if not prefix else candidates)
    return paths


def _paths_in(section: str) -> list[str]:
    paths: list[str] = []
    for line in section.split("\n"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        first = cells[0].strip()
        if first.startswith("|---") or first in ("文件", ""):
            continue
        # 去掉反引号
        first = first.replace("`", "").strip()
        if first.startswith("---") or first in ("文件", "设备"):
            continue
        # 仅用「两侧带空格的 /」拆分多文件单元格（如 `package.json` / `package-lock.json`）
        for part in re.split(r"\s+/\s+", first):
            part = part.strip().rstrip(",")
            # 计划里的简写单元格（如 `apps/web/package.json` / `package-lock.json`）
            if not part or "/" not in part:
                continue
            paths.append(part)
    return paths


def main() -> int:
    plan = parse_plan() + parse_iteration_plan()
    checked = 0
    missing: list[str] = []
    seen: set[str] = set()
    wildcard_report: list[tuple[str, int]] = []
    for path in plan:
        if isinstance(path, list):
            checked += 1
            if not any((ROOT / candidate).exists() for candidate in path):
                missing.append(" 或 ".join(path))
            continue
        if path in seen:
            continue
        seen.add(path)
        if path in WILDCARDS:
            directory, patterns = WILDCARDS[path]
            target = ROOT / directory
            count = sum(len(list(target.glob(p))) for p in patterns) if target.exists() else 0
            wildcard_report.append((path, count))
            checked += 1
            if count == 0:
                missing.append(path)
            continue
        checked += 1
        if not (ROOT / path).exists():
            missing.append(path)

    print(f"计划条目（去重后）：{checked}")
    print(f"已存在：{checked - len(missing)}")
    print(f"缺失：{len(missing)}")
    for name, count in wildcard_report:
        print(f"  通配 {name}: {count} 个文件")
    for item in missing:
        print("  ✗", item)
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
