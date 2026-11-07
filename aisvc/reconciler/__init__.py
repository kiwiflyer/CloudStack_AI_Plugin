"""Converging desired state onto actual state (SPEC.md section 4).

Milestone 1 implements `observe() -> diff()` for GPU capacity and renders
the plan. Execution arrives with milestone 2, so nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aisvc.inventory import CapacitySnapshot
    from aisvc.spec import PlatformSpec

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

        if not self.actions:
            lines.append("No changes. Desired state matches actual state.")
            return "\n".join(lines)

        lines.append("Planned actions:")
        lines.extend(f"  {a.render()}" for a in self.actions)
        lines.append("")
        lines.append(f"Plan: {self.to_add} to add, {self.to_remove} to remove.")
        return "\n".join(lines)


def plan_capacity(spec: PlatformSpec, snapshot: CapacitySnapshot) -> Plan:
    """Diff declared node pools against observed GPU capacity.

    A pool below `min_nodes` plans node creation; one above `max_nodes`
    plans removal; anything in the band is left alone so autoscaling can
    own it (SPEC.md section 4).
    """
    actions: list[Action] = []
    warnings: list[str] = list(spec.warnings)

    classes = {c.name: c for c in spec.document.gpu_classes}

    for pool in spec.document.node_pools:
        observed = snapshot.for_class(pool.zone, pool.gpu_class)
        if observed is None:
            warnings.append(
                f"nodePools.{pool.name}: class {pool.gpu_class!r} in zone "
                f"{pool.zone!r} was not observed, so its capacity is unknown"
            )
            continue

        gpu_class = classes.get(pool.gpu_class)
        offering = gpu_class.service_offering if gpu_class else "unknown offering"

        if observed.nodes_total < pool.min_nodes:
            actions.append(
                Action(
                    kind=ActionKind.CREATE,
                    resource_type="gpu-node",
                    name=pool.name,
                    count=pool.min_nodes - observed.nodes_total,
                    detail=f"service offering {offering}, template {pool.template}",
                    reason=(
                        f"{observed.nodes_total} node(s) observed, min_nodes is {pool.min_nodes}"
                    ),
                )
            )
        elif observed.nodes_total > pool.max_nodes:
            actions.append(
                Action(
                    kind=ActionKind.DELETE,
                    resource_type="gpu-node",
                    name=pool.name,
                    count=observed.nodes_total - pool.max_nodes,
                    detail=f"service offering {offering}",
                    reason=(
                        f"{observed.nodes_total} node(s) observed, max_nodes is {pool.max_nodes}"
                    ),
                )
            )

    return Plan(actions=actions, warnings=warnings)
