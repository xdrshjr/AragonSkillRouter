#!/usr/bin/env python3
"""校验 GitHub Actions workflow 的**上下文可用性**（纯标准库）。

为什么需要它
------------
`jobs.<id>.env` 只允许 `github / needs / strategy / matrix / vars / secrets / inputs`，
**没有** `runner / steps / job / env`。把 `${{ runner.temp }}` 写在那里不会只坏一个 job，
GitHub 会把整份文件判为 `Invalid workflow file`，表现为
「run 显示 failure 但 0 个 job」——排查成本极高（本项目踩过：ci.yml 连续 6 次 0 job，
最后靠 GitHub 页面那行 `Unrecognized named-value: 'runner'` 才定位）。

用法
----
    python scripts/check_workflow_contexts.py [workflow.yml ...]

不给参数时默认检查 `.github/workflows/` 下所有 `*.yml` / `*.yaml`。
退出码：0 = 全部通过；1 = 有违规（每处都会打印 `::error::` 便于 CI 标红）。

覆盖范围与边界
--------------
本脚本只负责「上下文用错位置」这一类（例如 `runner` 出现在 job 级 `env`）。
它**不**替代完整的 workflow 语法校验：`run:` 里出现字面量 `$` + `{{ ... }}`
（GitHub 会在 `run` 上做表达式替换，内容不合法同样让整份文件被判非法）不在覆盖范围内。
需要权威校验时跑 actionlint：

    actionlint .github/workflows/ci.yml

本项目两类问题都踩过：①`runner.temp` 写在 job env（本脚本能抓）；
②守卫脚本自己内联了字面量表达式（actionlint 抓到的，随后把守卫搬到本文件）。
"""

from __future__ import annotations

import glob
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - CI 里会先 pip install pyyaml
    print('::error::需要 PyYAML：pip install pyyaml', file=sys.stderr)
    raise SystemExit(2) from None

# GitHub 上下文可用性表（只覆盖本项目实际用到的位置）
ALLOWED = {
    'env_top': {'github', 'secrets', 'inputs', 'vars'},
    'env_job': {'github', 'needs', 'strategy', 'matrix', 'vars', 'secrets', 'inputs'},
    'env_step': {
        'github', 'needs', 'strategy', 'matrix', 'job', 'runner', 'env',
        'vars', 'secrets', 'steps', 'inputs',
    },
    'if_job': {'github', 'needs', 'vars', 'inputs'},
    'if_step': {
        'github', 'needs', 'strategy', 'matrix', 'job', 'runner', 'env',
        'vars', 'secrets', 'steps', 'inputs',
    },
    'with': {
        'github', 'needs', 'strategy', 'matrix', 'job', 'runner', 'env',
        'vars', 'secrets', 'steps', 'inputs',
    },
    'run': {
        'github', 'needs', 'strategy', 'matrix', 'job', 'runner', 'env',
        'vars', 'secrets', 'steps', 'inputs',
    },
}

# 用字符串拼接避开 workflow 里出现字面量表达式定界符（那份文件自身也要过 GitHub 的解析器）
_OPEN = '$' + '{{'
_CLOSE = '}' + '}'
EXPR = re.compile(re.escape(_OPEN) + r'(.*?)' + re.escape(_CLOSE), re.S)
NAME = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\.')


def contexts_in(text: str) -> set[str]:
    """取出文本里所有 `${{ ... }}` 表达式用到的顶层上下文名。"""
    names: set[str] = set()
    for match in EXPR.finditer(text):
        body = re.sub(r"'[^']*'", "''", match.group(1))
        names |= set(NAME.findall(body))
    return names


def check_file(path: str) -> list[str]:
    doc = yaml.safe_load(open(path, encoding='utf-8'))
    if not isinstance(doc, dict):
        return []

    problems: list[str] = []

    def audit(where: str, value: object, allowed_key: str) -> None:
        illegal = contexts_in(str(value)) - ALLOWED[allowed_key]
        if illegal:
            problems.append(f'{where} 用了该位置不可用的上下文 {sorted(illegal)}')

    for key, value in (doc.get('env') or {}).items():
        audit(f'env[{key}]', value, 'env_top')

    for job_id, job in (doc.get('jobs') or {}).items():
        if not isinstance(job, dict):
            continue
        for key, value in (job.get('env') or {}).items():
            audit(f'jobs.{job_id}.env[{key}]', value, 'env_job')
        if 'if' in job:
            audit(f'jobs.{job_id}.if', job['if'], 'if_job')
        for index, step in enumerate(job.get('steps') or []):
            if not isinstance(step, dict):
                continue
            for key, value in (step.get('env') or {}).items():
                audit(f'jobs.{job_id}.steps[{index}].env[{key}]', value, 'env_step')
            if 'if' in step:
                audit(f'jobs.{job_id}.steps[{index}].if', step['if'], 'if_step')
            for key, value in (step.get('with') or {}).items():
                audit(f'jobs.{job_id}.steps[{index}].with[{key}]', value, 'with')
            if 'run' in step:
                audit(f'jobs.{job_id}.steps[{index}].run', step['run'], 'run')

    return problems


def main(argv: list[str]) -> int:
    # Windows 控制台默认 GBK，打印 ✓ 会 UnicodeEncodeError（本地跑守卫就会崩）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - 老版本/被重定向
            pass

    paths = argv[1:]
    if not paths:
        paths = sorted(glob.glob('.github/workflows/*.yml') + glob.glob('.github/workflows/*.yaml'))
    if not paths:
        print('::error::没有找到任何 workflow 文件')
        return 1

    all_problems: list[str] = []
    for path in paths:
        for problem in check_file(path):
            all_problems.append(f'{path}: {problem}')

    for problem in all_problems:
        print(f'::error::{problem}')
    if all_problems:
        print(f'\n共 {len(all_problems)} 处上下文违规（会让整份 workflow 被判为非法）', file=sys.stderr)
        return 1

    print(f'workflow 上下文可用性 ✓（{len(paths)} 个文件）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
