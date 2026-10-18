"""CSRF 双提交校验（§7.3）。"""

from __future__ import annotations

from ..errors import ApiError, Code
from .sessions import CSRF_COOKIE, SessionContext, verify_csrf

WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def require_csrf(ctx: SessionContext, csrf_token: str | None, method: str) -> None:
    if method.upper() not in WRITE_METHODS:
        return
    if not verify_csrf(ctx, csrf_token):
        raise ApiError(Code.CSRF_INVALID)


__all__ = ["require_csrf", "CSRF_COOKIE", "WRITE_METHODS"]
