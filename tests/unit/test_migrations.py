"""The Alembic migration must match the models it migrates to.

Catches the classic drift where someone edits `aisvc/db/models.py` and
forgets the migration, so a fresh deploy and an upgraded one disagree.
"""

from __future__ import annotations

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
