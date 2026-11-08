"""One reconciler leader per zone (SPEC.md section 7; CLAUDE.md).

"One reconciler leader per zone via Postgres advisory lock." Two engine
replicas reconciling the same zone at once would both plan from the same
observation and both act on it, double-provisioning nodes.

Postgres is faked here: these check which statement is issued and how
the connection is held, not that Postgres honours its own locks.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.engine import Engine

from aisvc.reconciler.leader import (
    NotLeader,
    ZoneLeadership,
    zone_lock_key,
)


class FakeConnection:
    """Records the advisory-lock calls made on one connection."""

    def __init__(self, *, granted: bool = True) -> None:
        self.granted = granted
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.statements.append((str(statement), params or {}))

        class Result:
            def __init__(self, value: bool) -> None:
                self._value = value

            def scalar(self) -> bool:
                return self._value

        return Result(self.granted)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeEngine:
    def __init__(self, *, granted: bool = True, dialect: str = "postgresql") -> None:
        self.connections: list[FakeConnection] = []
        self.granted = granted
        self.dialect = FakeDialect(dialect)

    def connect(self) -> FakeConnection:
        connection = FakeConnection(granted=self.granted)
        self.connections.append(connection)
        return connection


def as_engine(fake: FakeEngine) -> Engine:
    """The fakes stand in for a SQLAlchemy Engine deliberately.

    Only `connect()` and `dialect.name` are used, and faking the real
    class would mean a live database in a unit test.
    """
    return cast(Engine, fake)


class TestLockKey:
    def test_a_zone_maps_to_a_stable_key(self) -> None:
        assert zone_lock_key("zone-east-1") == zone_lock_key("zone-east-1")

    def test_different_zones_get_different_keys(self) -> None:
        """Two zones sharing a key would serialise unrelated work."""
        assert zone_lock_key("zone-east-1") != zone_lock_key("zone-west-1")

    def test_the_key_fits_postgres_bigint(self) -> None:
        """pg_try_advisory_lock takes a signed 64-bit integer."""
        for zone in ("a", "zone-east-1", "x" * 200):
            key = zone_lock_key(zone)
            assert -(2**63) <= key < 2**63


class TestAcquiring:
    def test_the_leader_acquires_the_lock(self) -> None:
        engine = FakeEngine(granted=True)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1") as lock:
            assert lock.is_leader

    def test_it_asks_postgres_without_blocking(self) -> None:
        """A follower should move on, not wait behind the leader."""
        engine = FakeEngine(granted=True)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1"):
            pass

        issued = " ".join(s for s, _ in engine.connections[0].statements)
        assert "pg_try_advisory_lock" in issued
        assert "pg_advisory_lock(" not in issued

    def test_the_zone_key_is_passed_as_a_parameter(self) -> None:
        """Interpolating it would be an injection point for zone names."""
        engine = FakeEngine(granted=True)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1"):
            pass

        _, params = engine.connections[0].statements[0]
        assert params["key"] == zone_lock_key("zone-east-1")

    def test_a_follower_is_not_leader(self) -> None:
        engine = FakeEngine(granted=False)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1") as lock:
            assert not lock.is_leader

    def test_requiring_leadership_refuses_a_follower(self) -> None:
        engine = FakeEngine(granted=False)

        with (
            pytest.raises(NotLeader, match="zone-east-1"),
            ZoneLeadership(as_engine(engine), zone="zone-east-1", required=True),
        ):
            pass


class TestHoldingAndReleasing:
    def test_the_lock_is_released_on_exit(self) -> None:
        engine = FakeEngine(granted=True)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1"):
            pass

        issued = " ".join(s for s, _ in engine.connections[0].statements)
        assert "pg_advisory_unlock" in issued

    def test_the_lock_is_released_even_when_the_run_fails(self) -> None:
        """A crashed run must not leave a zone permanently unreconcilable."""
        engine = FakeEngine(granted=True)

        with pytest.raises(RuntimeError), ZoneLeadership(as_engine(engine), zone="z"):
            raise RuntimeError("the run failed")

        issued = " ".join(s for s, _ in engine.connections[0].statements)
        assert "pg_advisory_unlock" in issued

    def test_one_connection_is_held_for_the_whole_run(self) -> None:
        """Advisory locks belong to a connection.

        Borrowing a fresh connection per statement would release the
        lock the moment the first one went back to the pool, and the
        engine would believe it was leader while holding nothing.
        """
        engine = FakeEngine(granted=True)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1"):
            pass

        assert len(engine.connections) == 1

    def test_the_connection_is_closed_afterwards(self) -> None:
        engine = FakeEngine(granted=True)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1"):
            pass

        assert engine.connections[0].closed

    def test_a_follower_does_not_try_to_unlock(self) -> None:
        """Unlocking a lock you never held logs a Postgres warning."""
        engine = FakeEngine(granted=False)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1"):
            pass

        issued = " ".join(s for s, _ in engine.connections[0].statements)
        assert "pg_advisory_unlock" not in issued

    def test_a_follower_still_releases_its_connection(self) -> None:
        engine = FakeEngine(granted=False)

        with ZoneLeadership(as_engine(engine), zone="zone-east-1"):
            pass

        assert engine.connections[0].closed


class TestNonPostgres:
    """Advisory locks are a Postgres feature.

    A developer running against SQLite should get a working `apply`,
    not a syntax error from a function that does not exist there. One
    process cannot race itself, so there is nothing to protect.
    """

    def test_sqlite_does_not_try_to_lock(self) -> None:
        engine = FakeEngine(dialect="sqlite")

        with ZoneLeadership(as_engine(engine), zone="zone-east-1"):
            pass

        assert engine.connections == []

    def test_sqlite_counts_as_leader(self) -> None:
        engine = FakeEngine(dialect="sqlite")

        with ZoneLeadership(as_engine(engine), zone="zone-east-1") as lock:
            assert lock.is_leader

    def test_requiring_leadership_on_sqlite_is_satisfied(self) -> None:
        engine = FakeEngine(dialect="sqlite")

        with ZoneLeadership(as_engine(engine), zone="z", required=True) as lock:
            assert lock.is_leader
