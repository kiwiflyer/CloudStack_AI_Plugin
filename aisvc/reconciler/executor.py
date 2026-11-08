"""Executing a plan (SPEC.md section 4).

"Execute actions in dependency order with per-action timeouts; a failed
action stops its dependants only." Every run, and every action's outcome,
is written to the run log before and after it happens, so an operator can
always see what was attempted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from aisvc.db import ActionOutcome, ActionRecord, RunLog, RunRecord
from aisvc.reconciler.plan import Action, ActionKind, Plan

if TYPE_CHECKING:
    from aisvc.provision import GpuNode

__all__ = ["Executor", "ExecutorError"]

#: An action that has not finished in this long is treated as failed.
DEFAULT_ACTION_TIMEOUT = 900.0


class ExecutorError(Exception):
    """The plan itself could not be executed, independently of any action."""


class SupportsNodeProvisioning(Protocol):
    """The slice of `NodeProvisioner` the executor needs."""

    async def create_nodes(
        self, pool: str, count: int, *, dry_run: bool = False
    ) -> list[GpuNode]: ...

    async def destroy_nodes(
        self, pool: str, count: int, *, dry_run: bool = False
    ) -> list[GpuNode]: ...


def _topological_order(actions: list[Action]) -> list[int]:
    """Indices of `actions` in dependency order.

    Raises ExecutorError on an unknown dependency or a cycle, rather than
    deadlocking or silently dropping work.
    """
    by_key: dict[str, int] = {action.key: index for index, action in enumerate(actions)}

    for action in actions:
        for dependency in action.depends_on:
            if dependency not in by_key:
                raise ExecutorError(
                    f"action {action.key!r} depends on {dependency!r}, which is not in the plan"
                )

    ordered: list[int] = []
    state: dict[int, int] = {}  # 0 = unvisited, 1 = visiting, 2 = done

    def visit(index: int, trail: tuple[str, ...]) -> None:
        mark = state.get(index, 0)
        if mark == 2:
            return
        if mark == 1:
            cycle = " -> ".join([*trail, actions[index].key])
            raise ExecutorError(f"dependency cycle in the plan: {cycle}")

        state[index] = 1
        for dependency in actions[index].depends_on:
            visit(by_key[dependency], (*trail, actions[index].key))
        state[index] = 2
        ordered.append(index)

    for index in range(len(actions)):
        visit(index, ())
    return ordered


class Executor:
    """Runs a plan's actions and records what happened."""

    def __init__(
        self,
        provisioner: SupportsNodeProvisioning,
        run_log: RunLog,
        *,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> None:
        self.provisioner = provisioner
        self.run_log = run_log
        self.action_timeout = action_timeout

    async def execute(self, plan: Plan, *, zone: str, dry_run: bool = False) -> RunRecord:
        """Execute `plan`, returning the recorded run."""
        run = self.run_log.start_run(plan, zone=zone, dry_run=dry_run)

        try:
            order = _topological_order(plan.actions)
        except ExecutorError as exc:
            for record in run.actions:
                self.run_log.action_skipped(record, reason=str(exc))
            self.run_log.finish_run(run, error=str(exc))
            return run

        records: dict[str, ActionRecord] = {
            action.key: run.actions[index] for index, action in enumerate(plan.actions)
        }

        for index in order:
            action = plan.actions[index]
            record = records[action.key]

            blocker = self._blocked_by(action, records)
            if blocker is not None:
                self.run_log.action_skipped(record, reason=f"dependency {blocker} did not succeed")
                continue

            await self._run_one(action, record, dry_run=dry_run)

        self.run_log.finish_run(run)
        return run

    @staticmethod
    def _blocked_by(action: Action, records: dict[str, ActionRecord]) -> str | None:
        for dependency in action.depends_on:
            outcome = records[dependency].outcome
            if outcome is not ActionOutcome.SUCCEEDED:
                return dependency
        return None

    async def _run_one(self, action: Action, record: ActionRecord, *, dry_run: bool) -> None:
        handler = self._handler_for(action)
        if handler is None:
            self.run_log.action_failed(
                record,
                error=(f"no handler for {action.kind.value} of {action.resource_type!r}"),
            )
            return

        self.run_log.action_started(record)
        try:
            nodes = await asyncio.wait_for(handler(dry_run), timeout=self.action_timeout)
        except TimeoutError:
            self.run_log.action_failed(
                record,
                error=f"timed out after {self.action_timeout:g}s",
            )
        except Exception as exc:
            self.run_log.action_failed(record, error=f"{type(exc).__name__}: {exc}")
        else:
            names = ", ".join(node.name for node in nodes)
            self.run_log.action_succeeded(record, detail=names)

    def _handler_for(self, action: Action) -> Callable[[bool], Awaitable[list[GpuNode]]] | None:
        if action.resource_type != "gpu-node":
            return None

        if action.kind is ActionKind.CREATE:

            async def create(dry_run: bool) -> list[GpuNode]:
                return await self.provisioner.create_nodes(
                    action.name, action.count, dry_run=dry_run
                )

            return create

        if action.kind is ActionKind.DELETE:

            async def destroy(dry_run: bool) -> list[GpuNode]:
                return await self.provisioner.destroy_nodes(
                    action.name, action.count, dry_run=dry_run
                )

            return destroy

        return None
