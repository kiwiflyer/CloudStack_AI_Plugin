"""One reconciler leader per zone (SPEC.md section 7).

Engine HA runs at least two API replicas, and the reconciler runs beside
them. Two replicas reconciling one zone at the same time would observe
the same state, plan the same actions and both execute them: two sets of
GPU node VMs for one pool, each believing it created them.

An advisory lock settles it without another moving part. The lock lives
in the database the engine already depends on, it is released
automatically if the holder dies, and it needs no table, no heartbeat
and no lease to expire.

Both supported backends have one, with the same two properties
([ADR-0004](../../docs/adr/0004-state-store.md)):

- Postgres: `pg_try_advisory_lock(bigint)`.
- MariaDB and MySQL: `GET_LOCK(name, 0)`. Names are server-wide rather
  than per database, so ours are prefixed: this engine may share a
  server with CloudStack's management database.

Two details matter and are easy to get wrong:

- The lock belongs to a *connection*, not a transaction or a session.
  Borrowing a pooled connection per statement would hand the lock back
  the moment that connection returned to the pool, leaving a replica
  convinced it was leader while holding nothing. So one connection is
  checked out and held for the whole run.
- `pg_try_advisory_lock` rather than `pg_advisory_lock`: a follower
  should find out immediately and go back to sleep, not queue up behind
  the leader and start a second run the moment it finishes.
"""

from __future__ import annotations

import hashlib
import logging
from types import TracebackType
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

__all__ = ["NotLeader", "ZoneLeadership", "zone_lock_key", "zone_lock_name"]

log = logging.getLogger("aisvc.reconciler.leader")

_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK = text("SELECT pg_advisory_unlock(:key)")

#: `GET_LOCK(name, 0)` returns immediately, like pg_try_advisory_lock.
_MYSQL_TRY_LOCK = text("SELECT GET_LOCK(:name, :timeout)")
_MYSQL_UNLOCK = text("SELECT RELEASE_LOCK(:name)")

#: SQLAlchemy calls the dialect "mysql" for MariaDB too; "mariadb"
#: is accepted so a driver or release that reports it does not fall
#: through to the unlocked branch.
_MYSQL_DIALECTS = frozenset({"mysql", "mariadb"})

#: Keeps a zone's lock clear of anything else on the same server.
LOCK_PREFIX = "aisvc-zone-"


class NotLeader(Exception):
    """Another replica holds this zone, and this one was told to insist."""


def zone_lock_key(zone: str) -> int:
    """A stable 64-bit lock key for a zone.

    Hashed rather than enumerated so zones can be added without a
    registry of numbers, and signed to fit Postgres's bigint: the
    function takes a signed 64-bit integer and silently wraps anything
    larger, which would collide two zones onto one lock.
    """
    digest = hashlib.sha256(zone.encode()).digest()[:8]
    unsigned = int.from_bytes(digest, "big")
    return unsigned - 2**64 if unsigned >= 2**63 else unsigned


def zone_lock_name(zone: str) -> str:
    """A stable lock name for a zone, for backends that name locks.

    Hashed rather than using the zone name directly: MariaDB caps a
    lock name at 64 characters and a zone name has no such limit, so
    a long one would be truncated into a collision with its neighbour.
    """
    digest = hashlib.sha256(zone.encode()).hexdigest()[:32]
    return f"{LOCK_PREFIX}{digest}"


class ZoneLeadership:
    """Hold this zone's advisory lock for the duration of a run.

    Used as a context manager. `is_leader` says whether the lock was
    granted; with `required=True` a follower raises `NotLeader` instead,
    for callers that have nothing useful to do without it.
    """

    def __init__(self, engine: Engine, *, zone: str, required: bool = False) -> None:
        self.engine = engine
        self.zone = zone
        self.required = required
        self.key = zone_lock_key(zone)
        self.name = zone_lock_name(zone)
        self.is_leader = False
        self._connection: Any = None

    @property
    def _dialect(self) -> str:
        return str(getattr(self.engine.dialect, "name", ""))

    def __enter__(self) -> ZoneLeadership:
        dialect = self._dialect

        # Anything else — SQLite, in practice — has no advisory lock.
        # A developer pointed at a file database gets a working apply
        # instead of a missing-function error, and loses nothing: one
        # process cannot race itself. This branch is for development
        # only; both supported production backends lock for real.
        if dialect != "postgresql" and dialect not in _MYSQL_DIALECTS:
            log.warning(
                "%s has no advisory lock; running zone %s unlocked. Safe for one "
                "process, unsafe for more than one.",
                dialect or "this database",
                self.zone,
            )
            self.is_leader = True
            return self

        self._connection = self.engine.connect()
        if dialect in _MYSQL_DIALECTS:
            granted = self._connection.execute(
                _MYSQL_TRY_LOCK, {"name": self.name, "timeout": 0}
            ).scalar()
        else:
            granted = self._connection.execute(_TRY_LOCK, {"key": self.key}).scalar()
        self.is_leader = bool(granted)

        if not self.is_leader:
            log.info("another replica is reconciling zone %s; standing down", self.zone)
            if self.required:
                self._release()
                raise NotLeader(f"another engine replica holds the lock for zone {self.zone!r}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Released even when the run raised: a crashed reconcile must
        # not leave a zone that nothing can ever reconcile again.
        self._release()

    def _release(self) -> None:
        if self._connection is None:
            return
        try:
            if self.is_leader:
                # Only the holder unlocks. Unlocking a lock we never took
                # is a Postgres warning and tells us nothing.
                if self._dialect in _MYSQL_DIALECTS:
                    self._connection.execute(_MYSQL_UNLOCK, {"name": self.name})
                else:
                    self._connection.execute(_UNLOCK, {"key": self.key})
                self._connection.commit()
        finally:
            self._connection.close()
            self._connection = None
            self.is_leader = False
