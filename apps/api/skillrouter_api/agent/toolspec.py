"""平台工具清单（与网关 `policy.ts` 同源生成，§12.3）。"""

from __future__ import annotations

from ..services.settings import read_agent_policy

TOOL_SPECS: list[dict] = [
    {
        "name": "sr_capabilities_list",
        "description": "列出平台已有能力（key/name/kind/modes/status）",
        "params": {"type": "object", "properties": {"query": {"type": "string"}, "kind": {"type": "string"}, "mode": {"type": "string"}}, "additionalProperties": False},
        "readonly": True,
    },
    {
        "name": "sr_capability_get",
        "description": "获取能力详情（schema / limits / 示例）",
        "params": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False},
        "readonly": True,
    },
    {
        "name": "sr_fetch_source",
        "description": "抓取外部资料（受 fetch_guard 的 SSRF/大小/超时/重定向限制）",
        "params": {"type": "object", "properties": {"url": {"type": "string"}, "purpose": {"type": "string"}}, "required": ["url"], "additionalProperties": False},
        "readonly": False,
    },
    {
        "name": "sr_parse_document",
        "description": "解析已上传文档（pdf/txt/md/json），返回结构化摘要",
        "params": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"], "additionalProperties": False},
        "readonly": True,
    },
    {
        "name": "sr_propose_capability_draft",
        "description": "提交能力配置草案（不发布）",
        "params": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "name": {"type": "string"},
                "kind": {"type": "string"},
                "modes": {"type": "array", "items": {"type": "string"}},
                "file_io": {"type": "object"},
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "examples": {"type": "array"},
                "routes": {"type": "array"},
                "evidence": {"type": "array"},
            },
            "required": ["key", "name", "kind", "input_schema", "routes"],
            "additionalProperties": False,
        },
        "readonly": False,
    },
    {
        "name": "sr_update_capability_draft",
        "description": "修改已有草案",
        "params": {"type": "object", "properties": {"draft_id": {"type": "string"}, "patch": {"type": "object"}}, "required": ["draft_id", "patch"], "additionalProperties": False},
        "readonly": False,
    },
    {
        "name": "sr_test_capability",
        "description": "真实调用上游做连接测试（预算内，脱敏返回）",
        "params": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
                "input": {"type": "object"},
                "route_id": {"type": "string"},
                "save_payload": {"type": "boolean"},
            },
            "required": ["draft_id", "input"],
            "additionalProperties": False,
        },
        "readonly": False,
        "timeout_s": 60,
    },
    {
        "name": "sr_sample_invoke",
        "description": "用最小样例调用已发布能力（脱敏返回）",
        "params": {"type": "object", "properties": {"capability_key": {"type": "string"}, "version": {"type": "string"}, "input": {"type": "object"}}, "required": ["capability_key", "input"], "additionalProperties": False},
        "readonly": False,
        "timeout_s": 60,
    },
    {
        "name": "sr_create_skill_draft",
        "description": "创建 SKILL 草稿",
        "params": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "instructions": {"type": "string"}, "execution_plan": {"type": "object"}, "permissions": {"type": "object"}}, "required": ["name"], "additionalProperties": False},
        "readonly": False,
    },
    {
        "name": "sr_validate_skill",
        "description": "校验 SKILL 结构与依赖",
        "params": {"type": "object", "properties": {"skill_id": {"type": "string"}}, "required": ["skill_id"], "additionalProperties": False},
        "readonly": True,
    },
    {
        "name": "sr_record_finding",
        "description": "记录发现（必须带 confidence，推断内容不得进入发布必填映射）",
        "params": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "confidence": {"type": "string", "enum": ["verified", "inferred", "unknown"]},
                "text": {"type": "string"},
                "targets": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["kind", "confidence", "text"],
            "additionalProperties": False,
        },
        "readonly": False,
    },
    {
        "name": "sr_request_approval",
        "description": "为越权/超预算操作申请管理员审批",
        "params": {"type": "object", "properties": {"action": {"type": "string"}, "target": {"type": "string"}, "impact": {"type": "object"}, "requested_scope": {"type": "object"}}, "required": ["action"], "additionalProperties": False},
        "readonly": False,
    },
]


def names() -> list[str]:
    return [spec["name"] for spec in TOOL_SPECS]


def policy_names() -> list[str]:
    return list(read_agent_policy().get("allowed_tools") or [])


def drift() -> list[str]:
    """返回两侧不一致的工具名（契约测试用）。"""
    local = set(names())
    remote = set(policy_names())
    return sorted(local.symmetric_difference(remote))
