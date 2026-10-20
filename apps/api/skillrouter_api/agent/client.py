"""AragonAgentAdapter：Python 侧的 6 个方法（§7.12）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..security.netpolicy import client_kwargs
from ..settings import settings

log = get_logger("skillrouter.run")


class EngineHealth(Protocol):
    version: str
    status: str
    tools: list[str]


@dataclass
class SessionSpec:
    session_id: str
    system_prompt: str
    model: str | None = None
    tools: list[str] = field(default_factory=list)
    max_turns: int = 40
    max_duration_ms: int = 600_000


@dataclass
class TurnRequest:
    """一轮 Agent 请求（spec §9.5）。

    `model` 必须是网关认识的**对象** `{providerId, modelId, baseUrl?}`，`credential_refs`
    是 `{providerId: credential_id}`：网关不读环境变量里的密钥，也不认识平台的提供商
    UUID（§4.3）。把 `model` 写成字符串会让网关回 `TURN_CONFLICT: no model configured`
    —— 这是「Agent 面板永远没有回复」的根因，因此类型在这里被收紧。
    """

    turn_id: str
    messages: list[dict[str, Any]]
    budget: dict[str, Any] = field(default_factory=dict)
    model: dict[str, Any] | None = None
    system_prompt: str | None = None
    credential_refs: dict[str, str] = field(default_factory=dict)


@dataclass
class StopReport:
    stopped_steps: list[str]
    external_pending: list[str]
    note: str
    turn_id: str | None = None


@dataclass
class AgentCapabilityStatus:
    key: str
    state: str
    detail: str


class AragonAgentAdapter:
    """只暴露 6 个方法；内部全部转成对网关的 HTTP/SSE 调用。

    网关不可用时统一抛 `ENGINE_UNAVAILABLE`，绝不伪造成功。
    """

    def __init__(self, base_url: str | None = None, secret: str | None = None) -> None:
        self.base_url = (base_url or settings.agent_gateway_url).rstrip("/")
        self.secret = secret or settings.agent_gateway_secret
        self._headers = {"X-Internal-Token": self.secret}

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(
                **client_kwargs(self.base_url, timeout=5.0)
            ) as client:
                response = await client.get(f"{self.base_url}/healthz", headers=self._headers)
            if response.status_code >= 400:
                raise ApiError(
                    Code.ENGINE_UNAVAILABLE,
                    message=f"网关健康检查失败（HTTP {response.status_code}）",
                )
            return response.json()
        except ApiError:
            raise
        except (httpx.LocalProtocolError, httpx.InvalidURL) as exc:
            raise ApiError(
                Code.ENGINE_UNAVAILABLE,
                message="无法与网关建立请求（请求头或地址非法）",
                hint=(
                    "检查 SKILLROUTER_AGENT_GATEWAY_SECRET 是否与网关的 "
                    "AGENT_GATEWAY_SHARED_SECRET 完全一致（不要含空格/换行）"
                ),
                details={"reason": redact(str(exc), max_len=200)},
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(
                Code.ENGINE_UNAVAILABLE,
                message="Agent 网关不可达",
                hint="检查 gateway 进程是否运行（默认 127.0.0.1:8500），以及共享密钥是否一致",
                details={"reason": redact(str(exc), max_len=200)},
            ) from exc

    async def capabilities(self) -> list[dict]:
        from sqlalchemy import select

        from ..db import db_session
        from ..models import EngineCapabilityStatus

        with db_session() as s:
            rows = list(
                s.scalars(select(EngineCapabilityStatus).order_by(EngineCapabilityStatus.ordinal))
            )
        return [
            {"key": r.capability_key, "state": r.state, "detail": r.detail, "since_version": r.since_version}
            for r in rows
        ]

    async def start_session(self, spec: SessionSpec) -> str:
        return spec.session_id

    async def send(self, session_id: str, turn: TurnRequest) -> str:
        body = {
            "session_id": session_id,
            "turn_id": turn.turn_id,
            "messages": turn.messages,
            "system_prompt": turn.system_prompt,
            # 只发网关能用的形状；字符串/空值会让网关拒绝整轮（§9.5）
            "model": turn.model,
            "credential_refs": turn.credential_refs,
            "budget": turn.budget or {"max_turns": 40, "max_duration_ms": 600_000},
        }
        try:
            async with httpx.AsyncClient(**client_kwargs(self.base_url, timeout=20.0)) as client:
                response = await client.post(
                    f"{self.base_url}/internal/turns", json=body, headers=self._headers
                )
            if response.status_code >= 400:
                detail = redact(response.text, max_len=400)
                raise ApiError(
                    Code.ENGINE_UNAVAILABLE,
                    message=f"网关拒绝该轮次（HTTP {response.status_code}）",
                    details={"detail": detail},
                )
            return turn.turn_id
        except ApiError:
            raise
        except httpx.HTTPError as exc:
            raise ApiError(
                Code.ENGINE_UNAVAILABLE,
                message="Agent 网关不可达，无法发送消息",
                hint="检查 gateway 进程与 AGENT_GATEWAY_SHARED_SECRET",
                details={"reason": redact(str(exc), max_len=200)},
            ) from exc

    async def stop(self, session_id: str, turn_id: str) -> dict:
        try:
            async with httpx.AsyncClient(**client_kwargs(self.base_url, timeout=30.0)) as client:
                response = await client.post(
                    f"{self.base_url}/internal/turns/{turn_id}/interrupt",
                    json={"session_id": session_id},
                    headers=self._headers,
                )
            if response.status_code >= 400:
                return {
                    "stopped_steps": [],
                    "external_pending": [],
                    "note": f"网关未能返回停止报告（HTTP {response.status_code}）；已停止本地等待，远端可能仍在执行",
                    "turn_id": turn_id,
                }
            data = response.json()
            return {
                "stopped_steps": data.get("stopped_steps") or data.get("stoppedSteps") or [],
                "external_pending": data.get("external_pending") or data.get("externalPending") or [],
                "note": data.get("note") or "已停止当前轮次",
                "turn_id": turn_id,
            }
        except httpx.HTTPError as exc:
            return {
                "stopped_steps": [],
                "external_pending": [],
                "note": f"网关不可达（{redact(str(exc), max_len=120)}）；已停止本地等待，远端可能仍在执行",
                "turn_id": turn_id,
            }

    async def history(self, session_id: str, limit: int = 20) -> list[dict]:
        from sqlalchemy import select

        from ..db import db_session
        from ..models import AgentTurn

        with db_session() as s:
            rows = list(
                s.scalars(
                    select(AgentTurn)
                    .where(AgentTurn.session_id == session_id)
                    .order_by(AgentTurn.ordinal.desc())
                    .limit(limit)
                )
            )
        return [
            {
                "turn_id": r.id,
                "ordinal": r.ordinal,
                "user_text": r.user_text,
                "status": r.status,
                "usage": r.usage or {},
                "created_at": r.created_at,
            }
            for r in reversed(rows)
        ]

    async def replay_messages(self, session_id: str, limit: int = 20) -> list[dict]:
        """重启后用最近 N 轮重建上下文（上游无持久化）。"""
        from sqlalchemy import select

        from ..db import db_session
        from ..models import AgentEventRow

        with db_session() as s:
            rows = list(
                s.scalars(
                    select(AgentEventRow)
                    .where(AgentEventRow.session_id == session_id)
                    .order_by(AgentEventRow.id.desc())
                    .limit(limit * 8)
                )
            )
        messages: list[dict] = []
        for row in reversed(rows):
            payload = row.payload or {}
            if row.type == "assistant.delta" and payload.get("text"):
                if messages and messages[-1]["role"] == "assistant":
                    messages[-1]["content"] += payload["text"]
                else:
                    messages.append({"role": "assistant", "content": payload["text"]})
            elif row.type == "user.message" and payload.get("text"):
                messages.append({"role": "user", "content": payload["text"]})
        return messages[-limit:]

    async def connectivity(self) -> dict:
        health = await self.health()
        return {
            "gateway": {"ok": True, "url": self.base_url},
            "engine": health.get("engine") or {},
            "tools": health.get("tools") or [],
            "active_turns": health.get("active_turns", 0),
        }

    async def reload_engine(self, target_version: str, *, engine_home: str | None = None) -> dict:
        try:
            async with httpx.AsyncClient(**client_kwargs(self.base_url, timeout=180.0)) as client:
                response = await client.post(
                    f"{self.base_url}/internal/admin/reload",
                    json=_reload_body(target_version, engine_home),
                    headers=self._headers,
                )
            return {"status": response.status_code, **(response.json() if response.content else {})}
        except httpx.HTTPError as exc:
            raise ApiError(
                Code.ENGINE_UNAVAILABLE,
                message="无法调用网关热切换接口",
                details={"reason": redact(str(exc), max_len=200)},
            ) from exc


def _reload_body(target_version: str, engine_home: str | None) -> dict:
    """reload 请求体：新字段 `version`/`engine_home` + 兼容旧网关的 `target_version`（§A.5.3）。"""
    body: dict = {"version": target_version, "target_version": target_version}
    if engine_home:
        body["engine_home"] = engine_home
    return body


adapter = AragonAgentAdapter()


def get_adapter() -> AragonAgentAdapter:
    return adapter
