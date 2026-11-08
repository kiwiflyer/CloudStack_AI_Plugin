"""One reconciler leader per zone (SPEC.md section 7).

Engine HA runs at least two API replicas, and the reconciler runs beside
them. Two replicas reconciling one zone at the same time would observe
the same state, plan the same actions and both execute them: two sets of
GPU node VMs for one pool, each believing it created them.

A Postgres advisory lock settles it without another moving part. The
lock lives in the database the engine already depends on, it is released
automatically if the holder dies, and it needs no table, no heartbeat
and no lease to expire.

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

__all__ = ["NotLeader", "ZoneLeadership", "zone_lock_key"]

log = logging.getLogger("aisvc.reconciler.leader")

_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK = text("SELECT pg_advisory_unlock(:key)")


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
        self.is_leader = False
        self._connection: Any = None

    def __enter__(self) -> ZoneLeadership:
        # Advisory locks are Postgres's. A developer pointed at SQLite
        # gets a working apply instead of a missing-function error, and
        # loses nothing: one process cannot race itself.
        if getattr(self.engine.dialect, "name", "") != "postgresql":
            log.debug(
                "%s has no advisory locks; running zone %s unlocked",
                getattr(self.engine.dialect, "name", "this database"),
                self.zone,
            )
            self.is_leader = True
            return self

        self._connection = self.engine.connect()
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
                self._connection.execute(_UNLOCK, {"key": self.key})
                self._connection.commit()
        finally:
            self._connection.close()
            self._connection = None
            self.is_leader = False
