"""能力等价性判定（D12 / §7.7）。绝不按名称相似匹配。"""

from __future__ import annotations


def _required(schema: dict | None) -> set[str]:
    return set((schema or {}).get("required") or [])


def _types(schema: dict | None) -> dict:
    props = (schema or {}).get("properties") or {}
    return {k: (v or {}).get("type") for k, v in props.items()}


def equivalent(a: dict, b: dict) -> tuple[bool, list[str]]:
    """等价需要五项同时成立，返回 (是否等价, 不满足的理由列表)。"""
    reasons: list[str] = []
    if (a.get("kind") or "") != (b.get("kind") or ""):
        reasons.append("kind 不一致")
    a_req, b_req = _required(a.get("input_schema")), _required(b.get("input_schema"))
    if not (a_req <= b_req or b_req <= a_req):
        reasons.append("input_schema 必填字段集合不构成超集关系")
    a_types, b_types = _types(a.get("input_schema")), _types(b.get("input_schema"))
    for field in a_req & b_req:
        if a_types.get(field) != b_types.get(field):
            reasons.append(f"字段 {field} 类型不一致")
    if ((a.get("output_schema") or {}).get("type")) != ((b.get("output_schema") or {}).get("type")):
        reasons.append("output_schema 顶层类型不一致")
    if not (set(a.get("modes") or []) & set(b.get("modes") or [])):
        reasons.append("modes 无交集")
    if not a.get("compatibility_group") or a.get("compatibility_group") != b.get("compatibility_group"):
        reasons.append("未显式确认为同一 compatibility_group")
    return (not reasons, reasons)


def fingerprint(definition: dict) -> dict:
    """用于「审批过的 schema 指纹」比对。"""
    import hashlib
    import json

    payload = json.dumps(
        {
            "kind": definition.get("kind"),
            "input_schema": definition.get("input_schema"),
            "output_schema": definition.get("output_schema"),
            "modes": sorted(definition.get("modes") or []),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return {"sha256": hashlib.sha256(payload.encode()).hexdigest()}
