"""Agent 接入层。"""

from .client import AragonAgentAdapter, SessionSpec, StopReport, TurnRequest, get_adapter
from .prompting import SYSTEM_PROMPT, build_turn_prompt

__all__ = [
    "AragonAgentAdapter",
    "get_adapter",
    "SessionSpec",
    "TurnRequest",
    "StopReport",
    "SYSTEM_PROMPT",
    "build_turn_prompt",
]
