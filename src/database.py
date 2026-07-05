"""
SQLAlchemy engine, session factory, and context manager.
"""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from src.config import get_config

_engine = None
_SessionLocal = None


class Base(DeclarativeBase):
    pass


def get_engine():
    global _engine
    if _engine is None:
        cfg = get_config()
        _engine = create_engine(
            cfg.postgres.url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=False,
        )
    return _engine


def _get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal


def get_session() -> Session:
    """Create a new session (caller must close it)."""
    return _get_session_factory()()


@contextmanager
def session_scope():
    """Context manager: auto-commit/rollback + close."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """Create all tables if they don't exist."""
    import src.models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())
