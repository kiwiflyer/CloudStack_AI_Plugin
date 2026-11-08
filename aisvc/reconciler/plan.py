"""The typed diff between desired and actual state (SPEC.md section 4).

`observe() -> diff() -> plan()`. Nothing here writes; execution is the
executor's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aisvc.inventory import CapacitySnapshot
    from aisvc.spec import NodePool, PlatformSpec

__all__ = ["Action", "ActionKind", "Plan", "plan_capacity"]


class ActionKind(Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"

    @property
    def sigil(self) -> str:
        return {"create": "+", "update": "~", "delete": "-"}[self.value]


@dataclass(frozen=True, slots=True)
class Action:
    """One intended change, printable as a plan line."""

    kind: ActionKind
    resource_type: str
    name: str
    count: int = 1
    detail: str = ""
    reason: str = ""
    #: Keys of actions that must succeed before this one may run.
    depends_on: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Identifies this action within a plan, for dependency edges."""
        return f"{self.resource_type}:{self.name}:{self.kind.value}"

    def render(self) -> str:
        line = f"{self.kind.sigil} {self.resource_type} {self.name} x{self.count}"
        if self.detail:
            line += f"  ({self.detail})"
        if self.reason:
            line += f"\n    reason: {self.reason}"
        return line


@dataclass(frozen=True)
class Plan:
    """A typed diff, printed like `terraform plan` before anything runs."""

    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.actions)

    @property
    def to_add(self) -> int:
        return sum(a.count for a in self.actions if a.kind is ActionKind.CREATE)

    @property
    def to_remove(self) -> int:
        return sum(a.count for a in self.actions if a.kind is ActionKind.DELETE)

    def render(self) -> str:
        lines: list[str] = []
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"  ! {w}" for w in self.warnings)
            lines.append("")
        if self.notes:
            lines.append("Notes:")
            lines.extend(f"  - {n}" for n in self.notes)
            lines.append("")

        if not self.actions:
            lines.append("No changes. Desired state matches actual state.")
            return "\n".join(lines)

        lines.append("Planned actions:")
        lines.extend(f"  {a.render()}" for a in self.actions)
        lines.append("")
        lines.append(f"Plan: {self.to_add} to add, {self.to_remove} to remove.")
        return "\n".join(lines)


def plan_capacity(spec: PlatformSpec, snapshot: CapacitySnapshot) -> Plan:
    """Diff declared node pools against the GPU nodes that exist.

    A pool is sized in node VMs: below `min_nodes` plans creation, above
    `max_nodes` plans removal, and anything in the band is left alone so
    autoscaling can own it (SPEC.md section 4). Whether the hosts to place
    those nodes on exist is a separate question, answered by the class
    capacity and reported as a warning rather than silently planned.
    """
    actions: list[Action] = []
    warnings: list[str] = list(spec.warnings)
    notes: list[str] = list(spec.notes)

    classes = {c.name: c for c in spec.document.gpu_classes}

    for pool in spec.document.node_pools:
        observed = snapshot.for_pool(pool.name)
        if observed is None:
            warnings.append(f"nodePools.{pool.name}: not observed, so its size is unknown")
            continue

        gpu_class = classes.get(pool.gpu_class)
        offering = gpu_class.service_offering if gpu_class else "unknown offering"
        count = observed.node_count

        if count < pool.min_nodes:
            wanted = pool.min_nodes - count
            _warn_if_no_room(spec, snapshot, pool, wanted, warnings)
            actions.append(
                Action(
                    kind=ActionKind.CREATE,
                    resource_type="gpu-node",
                    name=pool.name,
                    count=wanted,
                    detail=f"service offering {offering}, template {pool.template}",
                    reason=(f"{count} node(s) in the pool, min_nodes is {pool.min_nodes}"),
                )
            )
        elif count > pool.max_nodes:
            actions.append(
                Action(
                    kind=ActionKind.DELETE,
                    resource_type="gpu-node",
                    name=pool.name,
                    count=count - pool.max_nodes,
                    detail=f"service offering {offering}",
                    reason=(f"{count} node(s) in the pool, max_nodes is {pool.max_nodes}"),
                )
            )

    return Plan(actions=actions, warnings=warnings, notes=notes)


def _warn_if_no_room(
    spec: PlatformSpec,
    snapshot: CapacitySnapshot,
    pool: NodePool,
    wanted: int,
    warnings: list[str],
) -> None:
    """Note when the zone has fewer free GPU hosts than the pool needs."""
    capacity = snapshot.for_class(pool.zone, pool.gpu_class)
    if capacity is None:
        warnings.append(
            f"nodePools.{pool.name}: GPU class {pool.gpu_class!r} in zone "
            f"{pool.zone!r} was not observed, so placement cannot be checked"
        )
        return
    if capacity.nodes_free < wanted:
        warnings.append(
            f"nodePools.{pool.name}: {wanted} node(s) to add but only "
            f"{capacity.nodes_free} free {pool.gpu_class} host(s) in "
            f"{pool.zone}; some deployments may fail for want of capacity"
        )
