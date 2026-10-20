"""管理端路由汇总（cookie + CSRF 鉴权）。"""

from fastapi import APIRouter

from . import (
    access,
    agent_chat,
    agentic_flow,
    auth,
    calls,
    capabilities,
    engine,
    llm,
    overview,
    providers,
    settings,
    skills,
    tasks,
)

router = APIRouter(prefix="/api/admin")
router.include_router(auth.router)
router.include_router(overview.router)
router.include_router(providers.router)
router.include_router(capabilities.router)
router.include_router(skills.router)
router.include_router(access.router)
router.include_router(calls.router)
router.include_router(tasks.router)
router.include_router(agent_chat.router)
router.include_router(agentic_flow.router)
router.include_router(engine.router)
router.include_router(settings.router)
router.include_router(llm.router)

__all__ = ["router"]
