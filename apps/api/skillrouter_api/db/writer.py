"""写序列化器：单写者，超时抛 DB_BUSY（§7.2）。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from ..errors import ApiError, Code
from .engine import get_session_factory

_WRITE_LOCK = threading.Lock()
_LOCK_TIMEOUT_S = 5.0


@contextmanager
def db_write() -> Iterator[Session]:
    """所有写事务必须经此上下文：排队 → commit → 释放。

    长任务必须在 services 层拆成短事务（≤200ms）。
    """
    acquired = _WRITE_LOCK.acquire(timeout=_LOCK_TIMEOUT_S)
    if not acquired:
        raise ApiError(Code.DB_BUSY)
    session: Session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        _WRITE_LOCK.release()
