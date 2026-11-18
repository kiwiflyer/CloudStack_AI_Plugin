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
"""The Alembic migration must match the models it migrates to.

Catches the classic drift where someone edits `aisvc/db/models.py` and
forgets the migration, so a fresh deploy and an upgraded one disagree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from aisvc.api.jobs import JobRecord  # noqa: F401  (registers the table)
from aisvc.db.models import Base

REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture
def alembic_config(tmp_path: Path) -> tuple[Config, str]:
    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "aisvc" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config, url


def schema_of(url: str) -> dict[str, dict[str, str]]:
    """Table and column names, with normalised column types."""
    engine = create_engine(url)
    inspector = inspect(engine)
    schema: dict[str, dict[str, str]] = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        schema[table] = {
            column["name"]: str(column["type"]).upper() for column in inspector.get_columns(table)
        }
    engine.dispose()
    return schema


class TestMigrationMatchesModels:
    def test_upgrade_builds_the_same_tables_as_the_models(
        self, alembic_config: tuple[Config, str], tmp_path: Path
    ) -> None:
        config, url = alembic_config
        upgrade(config, "head")

        metadata_url = f"sqlite+pysqlite:///{tmp_path / 'metadata.db'}"
        engine = create_engine(metadata_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        assert schema_of(url) == schema_of(metadata_url)

    def test_downgrade_removes_everything(self, alembic_config: tuple[Config, str]) -> None:
        config, url = alembic_config
        upgrade(config, "head")
        downgrade(config, "base")

        assert schema_of(url) == {}

    def test_upgrade_is_repeatable_from_scratch(self, alembic_config: tuple[Config, str]) -> None:
        config, url = alembic_config
        upgrade(config, "head")
        downgrade(config, "base")
        upgrade(config, "head")

        assert "aisvc_run" in schema_of(url)


# --------------------------------------------------------------------------
# Bringing a database up on startup
# --------------------------------------------------------------------------
#
# `aisvc apply`, `serve` and `batch` used to call
# `Base.metadata.create_all()` for convenience. Two things were wrong
# with that, and both were found by following docs/installation.md:
#
# - It does not stamp `alembic_version`, so Alembic could no longer
#   manage the database and a later `upgrade head` failed on tables
#   that already existed.
# - `create_all` only knows the tables whose modules have been
#   imported, and `aisvc.api.jobs` is not imported on the apply path.
#   So it built a schema with no `aisvc_job` table, and async commands
#   through the API would have failed against it.
#
# Running the migrations instead fixes both: the schema is whatever the
# migrations say, which is the same thing a fresh deploy and an upgrade
# get, and it is stamped.


class TestUpgradeOnStartup:
    def url_for(self, tmp_path: Path) -> str:
        return f"sqlite+pysqlite:///{tmp_path / 'startup.db'}"

    def test_it_creates_the_job_table_create_all_missed(self, tmp_path: Path) -> None:
        """The table whose absence started this."""
        from aisvc.db.migrate import upgrade_to_head

        url = self.url_for(tmp_path)
        upgrade_to_head(url)

        assert "aisvc_job" in schema_of(url)

    def test_it_creates_every_table_the_migrations_declare(self, tmp_path: Path) -> None:
        from aisvc.db.migrate import upgrade_to_head

        url = self.url_for(tmp_path)
        upgrade_to_head(url)

        tables = set(schema_of(url))
        assert {"aisvc_run", "aisvc_job", "aisvc_usage", "aisvc_private_endpoint"} <= tables

    def test_it_stamps_the_version_so_alembic_can_manage_it(self, tmp_path: Path) -> None:
        from sqlalchemy import create_engine, inspect, text

        from aisvc.db.migrate import head_revision, upgrade_to_head

        url = self.url_for(tmp_path)
        upgrade_to_head(url)

        engine = create_engine(url)
        assert "alembic_version" in inspect(engine).get_table_names()
        with engine.connect() as conn:
            stamped = conn.execute(text("select version_num from alembic_version")).scalar()
        engine.dispose()

        assert stamped == head_revision()

    def test_running_it_twice_changes_nothing(self, tmp_path: Path) -> None:
        # Every startup calls it, so it has to be a no-op once current.
        from aisvc.db.migrate import upgrade_to_head

        url = self.url_for(tmp_path)
        upgrade_to_head(url)
        before = schema_of(url)

        upgrade_to_head(url)

        assert schema_of(url) == before

    def test_it_does_not_depend_on_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # alembic.ini's script_location is relative, so `alembic
        # upgrade head` only works from the repository root. A service
        # starting under systemd is not there.
        from aisvc.db.migrate import upgrade_to_head

        url = self.url_for(tmp_path)
        monkeypatch.chdir(tmp_path)

        upgrade_to_head(url)

        assert "aisvc_job" in schema_of(url)

    def test_an_empty_database_is_not_mistaken_for_an_unmanaged_one(self, tmp_path: Path) -> None:
        from sqlalchemy import create_engine

        from aisvc.db.migrate import upgrade_to_head

        url = self.url_for(tmp_path)
        create_engine(url).connect().close()  # the file now exists, with no tables

        upgrade_to_head(url)

        assert "aisvc_job" in schema_of(url)


class TestADatabaseBuiltTheOldWay:
    """A database `create_all` made has tables and no version stamp.

    Upgrading it would fail on "table already exists", and stamping it
    blindly could mark a genuinely old schema as current. So it is
    refused, with the one command that resolves it.
    """

    def unmanaged(self, tmp_path: Path) -> str:
        from sqlalchemy import create_engine

        from aisvc.db.models import Base

        url = f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}"
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        engine.dispose()
        return url

    def test_it_is_refused_rather_than_half_migrated(self, tmp_path: Path) -> None:
        from aisvc.db.migrate import SchemaUnmanaged, upgrade_to_head

        with pytest.raises(SchemaUnmanaged):
            upgrade_to_head(self.unmanaged(tmp_path))

    def test_the_refusal_names_the_command_that_resolves_it(self, tmp_path: Path) -> None:
        from aisvc.db.migrate import SchemaUnmanaged, upgrade_to_head

        with pytest.raises(SchemaUnmanaged) as caught:
            upgrade_to_head(self.unmanaged(tmp_path))

        assert "alembic stamp head" in str(caught.value)

    def test_a_stamped_database_is_accepted(self, tmp_path: Path) -> None:
        # What the operator does after reading that message.
        from alembic.command import stamp

        from aisvc.db.migrate import alembic_config, upgrade_to_head

        url = self.unmanaged(tmp_path)
        stamp(alembic_config(url), "head")

        upgrade_to_head(url)

        assert "aisvc_run" in schema_of(url)


class TestTheCliDoesNotBuildSchemasItself:
    """The regression guard: a future edit reintroducing create_all.

    A source scan rather than a behaviour test, because the failure is
    a call that should not be there at all — and the behaviour it
    breaks (a missing table on a path nothing here exercises) is
    exactly what went unnoticed the first time.
    """

    def source(self) -> str:
        return (REPO_ROOT / "aisvc" / "cli" / "main.py").read_text()

    def test_the_cli_never_calls_create_all(self) -> None:
        assert "create_all" not in self.source(), (
            "the CLI must run the migrations, not build the schema from the models: "
            "create_all skips alembic_version and misses any table whose module "
            "this path does not import"
        )

    def test_the_cli_upgrades_instead(self) -> None:
        assert "upgrade_to_head" in self.source()


# --------------------------------------------------------------------------
# The schema on MariaDB
# --------------------------------------------------------------------------
#
# ADR-0004 keeps MariaDB open as an alternative backend. These compile
# the schema for MySQL — which is the dialect MariaDB uses — without a
# server, so the things that only bite on MySQL are caught here rather
# than on an operator's first migration: a String with no length, a
# type the dialect cannot render, an index too wide for InnoDB.


def mysql_ddl() -> dict[str, str]:
    """CREATE TABLE for every table, as MySQL would receive it."""
    from sqlalchemy.dialects.mysql import dialect as mysql_dialect
    from sqlalchemy.schema import CreateTable

    import aisvc.api.jobs  # noqa: F401  (registers aisvc_job)
    from aisvc.db.models import Base

    engine = mysql_dialect()
    return {
        table.name: str(CreateTable(table).compile(dialect=engine))
        for table in Base.metadata.sorted_tables
    }


class TestTheSchemaCompilesForMariaDb:
    def test_every_table_renders(self) -> None:
        tables = mysql_ddl()

        assert len(tables) >= 13
        assert "aisvc_job" in tables

    def test_every_string_column_has_a_length(self) -> None:
        # MySQL cannot create a VARCHAR without one; SQLAlchemy raises
        # at compile time, which is what the call above would hit.
        for name, ddl in mysql_ddl().items():
            assert "VARCHAR NOT NULL" not in ddl, f"{name} has an unsized VARCHAR"

    def test_indexed_strings_stay_inside_the_innodb_key_limit(self) -> None:
        # utf8mb4 is 4 bytes a character, and InnoDB caps an index at
        # 3072 bytes, so an indexed VARCHAR over 768 fails to create.
        from sqlalchemy import String

        import aisvc.api.jobs  # noqa: F401
        from aisvc.db.models import Base

        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if not (column.index or column.primary_key):
                    continue
                if isinstance(column.type, String) and column.type.length:
                    assert column.type.length <= 768, (
                        f"{table.name}.{column.name} is indexed and "
                        f"{column.type.length} characters; too wide for utf8mb4 InnoDB"
                    )

    def test_json_columns_render_as_json(self) -> None:
        # The variant falls back from JSONB to JSON off Postgres.
        assert "JSON" in mysql_ddl()["aisvc_run"]

    def test_timestamps_render_even_though_mysql_drops_the_zone(self) -> None:
        # MySQL's DATETIME carries no timezone, so stored values read
        # back naive. Every read path already copes, because SQLite
        # behaves the same way and the whole suite runs on it.
        assert "DATETIME" in mysql_ddl()["aisvc_usage"]


class TestTheMigrationsRunForMariaDb:
    """Alembic offline mode compiles every revision without a server.

    Stronger than compiling the models: it exercises the migration
    scripts themselves, which is what an operator's first `upgrade
    head` actually runs. A type or an ALTER that MySQL cannot render
    fails here rather than halfway through their install.
    """

    def sql(self) -> str:
        import io
        from contextlib import redirect_stdout

        from alembic.command import upgrade

        from aisvc.db.migrate import alembic_config

        config = alembic_config("mysql+pymysql://u:p@localhost/aisvc")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            upgrade(config, "head", sql=True)
        return buffer.getvalue()

    def test_it_compiles_every_revision(self) -> None:
        emitted = self.sql()

        # Alembic writes "-- Running upgrade <from> -> <to>" per step.
        applied = re.findall(r"^-- Running upgrade \S* -> (\d+)", emitted, re.MULTILINE)

        assert applied == [
            "0001",
            "0002",
            "0003",
            "0004",
            "0005",
            "0006",
            "0007",
            "0008",
            "0009",
            "0010",
        ]

    def test_it_creates_the_tables(self) -> None:
        emitted = self.sql()

        for table in ("aisvc_run", "aisvc_job", "aisvc_usage", "aisvc_private_endpoint"):
            assert f"CREATE TABLE {table}" in emitted

    def test_no_postgres_only_type_leaks_through(self) -> None:
        # JSONB is a variant that must fall back off Postgres.
        assert "JSONB" not in self.sql()

    def test_the_alters_render(self) -> None:
        # 0007 adds columns; an unrenderable default would fail here.
        assert "ALTER TABLE aisvc_certificate ADD COLUMN serial" in self.sql()
