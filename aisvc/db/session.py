# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with this
# work for additional information regarding copyright ownership. The ASF
# licenses this file to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
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
