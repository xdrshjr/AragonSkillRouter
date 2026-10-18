"""Session 依赖（§7.2）。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from .engine import get_session_factory
from .writer import db_write

__all__ = ["db_session_iter", "db_write", "session_factory"]


def session_factory() -> Session:
    return get_session_factory()()


def db_session_iter() -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
