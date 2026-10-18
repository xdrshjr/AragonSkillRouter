"""对外路由汇总（Bearer 平台令牌）。"""

from fastapi import APIRouter

from . import callbacks, discovery, files, invoke, mcp, meta, skills, tasks

router = APIRouter(prefix="/api/v1")
router.include_router(meta.router)
router.include_router(discovery.router)
router.include_router(invoke.router)
router.include_router(tasks.router)
router.include_router(files.router)
router.include_router(skills.router)
router.include_router(mcp.router)
router.include_router(callbacks.router)

__all__ = ["router"]
