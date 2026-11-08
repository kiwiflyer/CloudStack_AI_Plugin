"""Bringing a database up to the current schema (SPEC.md section 4).

`aisvc apply` and `aisvc batch` call `upgrade_to_head` at startup, so a
fresh deploy and an upgraded one end up with the same schema — which is
the thing that makes a migration worth writing.

**`aisvc serve` deliberately does not.** It is the one command that runs
as several replicas, and nothing here serialises concurrent DDL, so
replicas racing to migrate on startup is a worse failure than an
operator being told to migrate first. Run `alembic upgrade head` as a
deployment step; the two calls below are a convenience for a single
operator bringing a zone up, not the supported path for a cluster.

This replaces `Base.metadata.create_all()`, which was two bugs rather
than a shortcut:

- It never stamped `alembic_version`, so after it ran Alembic could not
  manage the database at all: `alembic upgrade head` failed on tables
  that already existed, and there was no record of which revision the
  schema actually was.
- `create_all` only builds the tables whose modules have been imported.
  `aisvc.api.jobs` is not imported on the apply path, so a database
  created by `aisvc apply` had no `aisvc_job` table and every async
  command through the API would have failed against it. The migrations
  have no such dependency: they say what they create.

Both were found by following `docs/installation.md` rather than by a
test, which is why the guard against the first one coming back is a
source scan over the CLI.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.command import upgrade
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

__all__ = [
    "MIGRATIONS",
    "SchemaUnmanaged",
    "alembic_config",
    "head_revision",
    "upgrade_to_head",
]

log = logging.getLogger("aisvc.db.migrate")

#: Absolute, because `alembic.ini` sets a relative `script_location`
#: and a service does not start in the repository root.
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class SchemaUnmanaged(RuntimeError):
    """The database has tables but no Alembic version stamp."""


def alembic_config(url: str) -> Config:
    """An Alembic config that works from any working directory."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url)
    return config


def head_revision() -> str:
    """The revision the migrations currently end at."""
    revision = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
    if revision is None:  # pragma: no cover - there is always a head
        raise RuntimeError("no migrations found")
    return revision


def upgrade_to_head(url: str) -> None:
    """Migrate this database to the current schema.

    A no-op once it is current, because every startup calls it.
    """
    _refuse_if_unmanaged(url)
    log.info("migrating %s to %s", _safe(url), head_revision())
    upgrade(alembic_config(url), "head")


def _refuse_if_unmanaged(url: str) -> None:
    """Stop on a database built by the old `create_all` path.

    Upgrading it would fail partway on "table already exists", and
    stamping it automatically could mark a genuinely old schema as
    current — the one case where a quiet recovery is worse than
    stopping. So it says the single command that resolves it, and
    leaves the decision with whoever knows what that database is.
    """
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    if not tables or "alembic_version" in tables:
        return

    raise SchemaUnmanaged(
        f"{_safe(url)} already has tables but no alembic_version, so it was "
        "created by an older build that used create_all() instead of the "
        "migrations. That schema may also be missing aisvc_job. Check it "
        "against the migrations and then either drop and recreate the "
        "database, or, if you are sure it is current, run "
        "`alembic stamp head` against it once."
    )


def _safe(url: str) -> str:
    """A URL without its password, for a log line or an error."""
    if "@" not in url:
        return url
    prefix, _, host = url.rpartition("@")
    scheme, _, credentials = prefix.partition("://")
    user = credentials.partition(":")[0]
    return f"{scheme}://{user}:***@{host}"
