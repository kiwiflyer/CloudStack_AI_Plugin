"""The observed world (SPEC.md section 4).

"Observe CloudStack, Kubernetes, gateway, registry and Prometheus state
into one `ActualState`." Milestone 3 covers CloudStack capacity and the
Kubernetes serving workloads; the rest join as their modules land.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aisvc.inventory import CapacitySnapshot

if TYPE_CHECKING:
    from aisvc.k8s.client import DeployedModel
    from aisvc.spec import PlatformSpec

__all__ = ["ActualState"]


@dataclass(frozen=True)
class ActualState:
    """What exists right now, across every system the reconciler touches."""

    capacity: CapacitySnapshot
    #: Keyed by (model, colour).
    deployments: dict[tuple[str, str], DeployedModel] = field(default_factory=dict)
    #: The gateway config currently in effect, by its digest.
    route_hash: str = ""
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def deployment(self, model: str, colour: str) -> DeployedModel | None:
        return self.deployments.get((model, colour))

    def colours_of(self, model: str) -> list[str]:
        return sorted(colour for name, colour in self.deployments if name == model)

    @classmethod
    def empty_for(cls, spec: PlatformSpec) -> ActualState:
        """A world where nothing exists yet, shaped by the spec.

        Used by `--no-observe` so a plan can be read without reaching
        CloudStack or the cluster.
        """
        return cls(capacity=CapacitySnapshot.empty_for(spec), deployments={})
