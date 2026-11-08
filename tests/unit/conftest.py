"""Fixtures shared across the unit tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aisvc.db import Base, KeyStore, UsageStore


@pytest.fixture
def keys() -> Iterator[KeyStore]:
    """A KeyStore over in-memory SQLite, for anything that issues keys."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        yield KeyStore(session)


@pytest.fixture
def usage_store() -> Iterator[UsageStore]:
    """A UsageStore over in-memory SQLite."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        yield UsageStore(session)
