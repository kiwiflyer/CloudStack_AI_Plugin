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
"""The Postgres run log (SPEC.md section 4).

"Record every run (plan, actions, outcomes) in Postgres for audit and the
UI activity view." Exercised here on in-memory SQLite; the schema is
dialect-neutral and Alembic targets Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aisvc.db import (
    ActionOutcome,
    ActionRecord,
    Base,
    RunLog,
    RunRecord,
    RunStatus,
)
from aisvc.reconciler import Action, ActionKind, Plan


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as s:
        yield s


@pytest.fixture
def log(session: Session) -> RunLog:
    return RunLog(session)


SAMPLE_PLAN = Plan(
    actions=[
        Action(ActionKind.CREATE, "gpu-node", "h100-pool", count=2, detail="GPU-H100-8x"),
        Action(ActionKind.DELETE, "gpu-node", "l40s-pool", count=1),
    ],
    warnings=["heads up"],
    notes=["for information"],
)


class TestStartRun:
    def test_a_run_records_the_plan_it_started_from(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="zone-east-1", dry_run=False)

        assert run.id is not None
        assert run.zone == "zone-east-1"
        assert run.status is RunStatus.RUNNING
        assert run.dry_run is False
        assert "h100-pool" in run.plan_text

    def test_the_plan_diff_is_stored_structurally_too(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="zone-east-1", dry_run=False)

        assert len(run.actions) == 2
        kinds = {a.kind for a in run.actions}
        assert kinds == {"create", "delete"}

    def test_actions_start_pending(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="zone-east-1", dry_run=False)
        assert all(a.outcome is ActionOutcome.PENDING for a in run.actions)

    def test_warnings_and_notes_are_kept(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="zone-east-1", dry_run=False)
        assert run.warnings == ["heads up"]
        assert run.notes == ["for information"]

    def test_a_dry_run_is_marked_as_one(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="zone-east-1", dry_run=True)
        assert run.dry_run is True


class TestRecordingOutcomes:
    def test_a_successful_action_is_recorded_with_its_result(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="z", dry_run=False)
        action = run.actions[0]

        log.action_succeeded(action, detail="created vm-1, vm-2")

        assert action.outcome is ActionOutcome.SUCCEEDED
        assert action.result == "created vm-1, vm-2"
        assert action.finished_at is not None

    def test_a_failed_action_keeps_the_error(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="z", dry_run=False)
        action = run.actions[0]

        log.action_failed(action, error="insufficient capacity")

        assert action.outcome is ActionOutcome.FAILED
        assert "insufficient capacity" in (action.error or "")

    def test_a_skipped_action_says_why(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="z", dry_run=False)
        action = run.actions[1]

        log.action_skipped(action, reason="a dependency failed")

        assert action.outcome is ActionOutcome.SKIPPED
        assert "dependency" in (action.error or "")


class TestFinishRun:
    def test_a_run_with_every_action_succeeding_is_successful(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="z", dry_run=False)
        for action in run.actions:
            log.action_succeeded(action)

        log.finish_run(run)

        assert run.status is RunStatus.SUCCEEDED
        assert run.finished_at is not None

    def test_a_run_with_any_failure_is_failed(self, log: RunLog) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="z", dry_run=False)
        log.action_succeeded(run.actions[0])
        log.action_failed(run.actions[1], error="boom")

        log.finish_run(run)

        assert run.status is RunStatus.FAILED

    def test_an_empty_plan_still_records_a_successful_run(self, log: RunLog) -> None:
        run = log.start_run(Plan(), zone="z", dry_run=False)
        log.finish_run(run)

        assert run.status is RunStatus.SUCCEEDED
        assert run.actions == []


class TestQuerying:
    def test_recent_runs_come_back_newest_first(self, log: RunLog) -> None:
        first = log.start_run(SAMPLE_PLAN, zone="z", dry_run=False)
        log.finish_run(first)
        second = log.start_run(SAMPLE_PLAN, zone="z", dry_run=False)
        log.finish_run(second)

        recent = log.recent_runs(limit=10)

        assert [r.id for r in recent] == [second.id, first.id]

    def test_runs_can_be_filtered_by_zone(self, log: RunLog) -> None:
        log.finish_run(log.start_run(SAMPLE_PLAN, zone="east", dry_run=False))
        log.finish_run(log.start_run(SAMPLE_PLAN, zone="west", dry_run=False))

        assert [r.zone for r in log.recent_runs(zone="west")] == ["west"]

    def test_limit_is_respected(self, log: RunLog) -> None:
        for _ in range(5):
            log.finish_run(log.start_run(SAMPLE_PLAN, zone="z", dry_run=False))

        assert len(log.recent_runs(limit=2)) == 2


class TestSchema:
    def test_tables_are_named_for_the_plugin(self) -> None:
        assert RunRecord.__tablename__ == "aisvc_run"
        assert ActionRecord.__tablename__ == "aisvc_run_action"

    def test_actions_are_removed_with_their_run(self, log: RunLog, session: Session) -> None:
        run = log.start_run(SAMPLE_PLAN, zone="z", dry_run=False)
        run_id = run.id

        session.delete(run)
        session.flush()

        remaining = session.query(ActionRecord).filter_by(run_id=run_id).all()
        assert remaining == []


class TestMigrationsMatchTheModels:
    """A fresh deploy and an upgraded one must end up identical.

    `Base.metadata.create_all` builds the schema in tests, Alembic
    builds it in production. Nothing checks they agree, so a table added
    to the models without a migration would pass every test here and
    fail on the first real deploy.
    """

    def schemas(self) -> tuple[set[str], set[str]]:
        import importlib
        import pathlib

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        import aisvc.api.jobs
        import aisvc.db

        folder = pathlib.Path(aisvc.db.__file__).parent / "migrations" / "versions"
        revisions = sorted(path.stem for path in folder.glob("0*.py"))
        assert revisions, "no migrations found to compare against"

        engine = sa.create_engine("sqlite+pysqlite:///:memory:")
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            for revision in revisions:
                module = importlib.import_module(f"aisvc.db.migrations.versions.{revision}")
                with Operations.context(ctx):
                    module.upgrade()
            migrated = set(sa.inspect(conn).get_table_names())

        declared_engine = sa.create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(declared_engine)
        declared = set(sa.inspect(declared_engine).get_table_names())
        return migrated, declared

    def test_no_table_is_declared_without_a_migration(self) -> None:
        migrated, declared = self.schemas()

        assert declared - migrated == set()

    def test_no_migration_creates_a_table_the_models_do_not_know(self) -> None:
        migrated, declared = self.schemas()

        assert migrated - declared - {"alembic_version"} == set()
