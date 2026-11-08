"""Engine and transaction plumbing (SPEC.md section 4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from aisvc.db import Base, RunLog, create_session_factory, session_scope
from aisvc.db.session import DEFAULT_DATABASE_URL, database_url
from aisvc.reconciler import Plan

SQLITE = "sqlite+pysqlite:///"


class TestDatabaseUrl:
    def test_the_default_is_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AISVC_DATABASE_URL", raising=False)
        assert database_url() == DEFAULT_DATABASE_URL
        assert database_url().startswith("postgresql")

    def test_the_environment_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AISVC_DATABASE_URL", "postgresql+psycopg://elsewhere/db")
        assert database_url() == "postgresql+psycopg://elsewhere/db"


class TestSessionScope:
    def test_work_is_committed_on_success(self, tmp_path: Path) -> None:
        factory = create_session_factory(f"{SQLITE}{tmp_path}/a.db")
        Base.metadata.create_all(factory.kw["bind"])

        with session_scope(factory) as session:
            RunLog(session).start_run(Plan(), zone="z", dry_run=False)

        with session_scope(factory) as session:
            assert len(RunLog(session).recent_runs()) == 1

    def test_work_is_rolled_back_on_error(self, tmp_path: Path) -> None:
        factory = create_session_factory(f"{SQLITE}{tmp_path}/b.db")
        Base.metadata.create_all(factory.kw["bind"])

        with pytest.raises(RuntimeError, match="boom"), session_scope(factory) as session:
            RunLog(session).start_run(Plan(), zone="z", dry_run=False)
            raise RuntimeError("boom")

        with session_scope(factory) as session:
            assert RunLog(session).recent_runs() == []

    def test_the_session_is_closed_either_way(self, tmp_path: Path) -> None:
        factory = create_session_factory(f"{SQLITE}{tmp_path}/c.db")
        Base.metadata.create_all(factory.kw["bind"])

        leaked: Session | None = None
        with session_scope(factory) as session:
            leaked = session

        assert leaked is not None
        assert not leaked.is_active or leaked.get_transaction() is None


class TestFactory:
    def test_the_factory_binds_the_url_it_was_given(self, tmp_path: Path) -> None:
        url = f"{SQLITE}{tmp_path}/d.db"
        factory = create_session_factory(url)
        assert str(factory.kw["bind"].url) == url
