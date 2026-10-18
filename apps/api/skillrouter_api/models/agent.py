"""Agent 域模型（§10.1，含 v2 的 flow / approval）。"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin


class AgentSession(Base, IdMixin):
    __tablename__ = "agent_sessions"

    title: Mapped[str] = mapped_column(String(200), default="新会话", nullable=False)
    model_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    permission_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class AgentTurn(Base, IdMixin):
    __tablename__ = "agent_turns"

    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    user_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attachments: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    queue_position: Mapped[int | None] = mapped_column(Integer)
    stop_report: Mapped[dict | None] = mapped_column(JSON)
    usage: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail_redacted: Mapped[str | None] = mapped_column(Text)
    retry_of: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[str | None] = mapped_column(String(32))
    finished_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_agent_turns_session", "session_id", "ordinal"),)


class AgentEventRow(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("uq_agent_events_seq", "turn_id", "seq", unique=True),
        Index("ix_agent_events_session", "session_id", "id"),
    )


class AgentToolCall(Base, IdMixin):
    __tablename__ = "agent_tool_calls"

    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    args_redacted: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    upstream_task_id: Mapped[str | None] = mapped_column(String(128))
    at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_agent_tool_calls_turn", "turn_id"),)


class AgentArtifact(Base, IdMixin):
    __tablename__ = "agent_artifacts"

    turn_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="finding", nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending_review", nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_agent_artifacts_turn", "turn_id"),)


class AgentFlow(Base, IdMixin):
    __tablename__ = "agent_flows"

    title: Mapped[str] = mapped_column(String(200), default="接入流程", nullable=False)
    capability_id: Mapped[str | None] = mapped_column(String(64))
    skill_id: Mapped[str | None] = mapped_column(String(64))
    credential_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    auto_publish: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    budget: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    spent: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    current_step: Mapped[str] = mapped_column(String(24), default="parse", nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_agent_flows_status", "status", "updated_at"),)


class AgentFlowStep(Base, IdMixin):
    __tablename__ = "agent_flow_steps"

    flow_id: Mapped[str] = mapped_column(String(64), nullable=False)
    step: Mapped[str] = mapped_column(String(24), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[str | None] = mapped_column(String(32))
    finished_at: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("uq_agent_flow_steps", "flow_id", "step", unique=True),)


class AgentApproval(Base, IdMixin):
    __tablename__ = "agent_approvals"

    flow_id: Mapped[str | None] = mapped_column(String(64))
    turn_id: Mapped[str | None] = mapped_column(String(64))
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(256))
    impact: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    requested_scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_agent_approvals_status", "status", "expires_at"),)
