"""Engine and session plumbing (SPEC.md section 4)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

__all__ = ["DEFAULT_DATABASE_URL", "create_session_factory", "database_url", "session_scope"]

#: Overridden by AISVC_DATABASE_URL; Postgres in every real deployment.
#: pg8000 rather than psycopg: BSD-3-Clause instead of LGPL-3.0, which keeps
#: the dependency tree Apache-2.0 clean (docs/third-party-licenses.md).
DEFAULT_DATABASE_URL = "postgresql+pg8000://aisvc@localhost:5432/aisvc"


def database_url() -> str:
    return os.environ.get("AISVC_DATABASE_URL", DEFAULT_DATABASE_URL)


def create_session_factory(url: str | None = None, *, echo: bool = False) -> sessionmaker[Session]:
    engine: Engine = create_engine(url or database_url(), echo=echo, future=True)
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A transaction that commits on success and rolls back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
