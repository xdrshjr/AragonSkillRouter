"""`param_map` 求值与模板填充（严格模式，未知来源即报错）。"""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import ApiError, Code

VAR = re.compile(r"\$\{([A-Za-z0-9_.]+)\}|\{([A-Za-z_][A-Za-z0-9_.]*)\}")

TRANSFORMS = {"passthrough", "int", "float", "str", "bool", "json", "upper", "lower", "trim"}


def _placeholder_key(template: str) -> str | None:
    """整串就是单个占位符时返回其 key（支持 `${x}` 与 `{x}`）。"""
    match = VAR.fullmatch(template.strip())
    if not match:
        return None
    return match.group(1) or match.group(2)


def _nested_get(data: dict | None, path: str, default: Any = None) -> Any:
    current: Any = data or {}
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        else:
            return default
    return current


def resolve_source(item: dict, ctx: dict) -> Any:
    source = item.get("from")
    if not source:
        if "const" in item:
            return item["const"]
        if "default" in item:
            return item["default"]
        return None
    if "." not in source:
        raise ApiError(
            Code.PARAM_MAP_UNRESOLVED,
            message=f"无法解析的映射来源：{source}",
            hint="来源必须形如 input.<field> / route.<field> / const",
            details={"source": source},
        )
    namespace, _, path = source.partition(".")
    if namespace == "input":
        value = _nested_get(ctx.get("input"), path)
    elif namespace == "route":
        value = _nested_get(ctx.get("route"), path)
    elif namespace == "options":
        value = _nested_get(ctx.get("options"), path)
    elif namespace == "const":
        value = path
    else:
        raise ApiError(
            Code.PARAM_MAP_UNRESOLVED,
            message=f"未知的映射命名空间：{namespace}",
            hint="只允许 input.* / route.* / options.* / const.*",
            details={"source": source},
        )
    if value is None:
        if "default" in item:
            return item["default"]
        return None
    return value


def _apply_transform(value: Any, transform: str | None, item: dict) -> Any:
    if transform is None or transform == "passthrough":
        return _clip(value, item)
    if transform not in TRANSFORMS:
        raise ApiError(
            Code.PARAM_MAP_UNRESOLVED,
            message=f"未知的 transform：{transform}",
            details={"transform": transform},
        )
    try:
        if transform == "int":
            out: Any = int(value)
        elif transform == "float":
            out = float(value)
        elif transform == "str":
            out = str(value)
        elif transform == "bool":
            if isinstance(value, str):
                out = value.lower() in ("1", "true", "yes", "on")
            else:
                out = bool(value)
        elif transform == "json":
            out = json.loads(value) if isinstance(value, str) else value
        elif transform == "upper":
            out = str(value).upper()
        elif transform == "lower":
            out = str(value).lower()
        elif transform == "trim":
            out = str(value).strip()
        else:  # pragma: no cover
            out = value
    except (TypeError, ValueError) as exc:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message=f"参数 {item.get('from')} 无法转换为 {transform}：{exc}",
        ) from exc
    return _clip(out, item)


def _clip(value: Any, item: dict) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "max" in item and value > item["max"]:
            return item["max"]
        if "min" in item and value < item["min"]:
            return item["min"]
    if isinstance(value, str) and "max_length" in item:
        return value[: int(item["max_length"])]
    return value


def evaluate(param_map: dict, ctx: dict) -> dict:
    """严格模式：每个声明项都要能解析，未声明字段不进入输出。"""
    out: dict[str, Any] = {}
    for target, item in (param_map or {}).items():
        if not isinstance(item, dict):
            raise ApiError(Code.PARAM_MAP_UNRESOLVED, f"param_map.{target} 必须是对象")
        value = resolve_source(item, ctx)
        if value is None and "default" not in item:
            continue
        out[target] = _apply_transform(value, item.get("transform"), item)
    return out


def fill_template(template: Any, values: dict) -> Any:
    """把 `{path}` / `${path}` 替换为 values 中的值（递归处理 dict/list/str）。"""
    if isinstance(template, str):
        whole = _placeholder_key(template)
        if whole:
            if whole in values:
                return values[whole]
            if whole == "base_url":
                return values.get("base_url", "")
            if whole in ("model", "model_hint"):
                return values.get("model") or values.get("model_hint") or ""
            return values.get(whole, template)
        return VAR.sub(lambda m: str(values.get(m.group(1) or m.group(2), m.group(0))), template)
    if isinstance(template, dict):
        return {k: fill_template(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [fill_template(v, values) for v in template]
    return template


def resolve_dotted(data: dict, path: str, default: Any = None) -> Any:
    return _nested_get(data, path, default)
