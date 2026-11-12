"""The observed world (SPEC.md section 4).

"Observe CloudStack, Kubernetes, gateway, registry and Prometheus state
into one `ActualState`." Milestone 3 covers CloudStack capacity and the
Kubernetes serving workloads; the rest join as their modules land.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aisvc.autoscale import Metrics
from aisvc.cloudstack.clusters import ClusterPlanned, ClusterState
from aisvc.inventory import CapacitySnapshot

if TYPE_CHECKING:
    from aisvc.k8s.client import DeployedModel
    from aisvc.spec import PlatformSpec

__all__ = ["ActualState", "DeploymentHealth"]


@dataclass(frozen=True, slots=True)
class DeploymentHealth:
    """What Prometheus says about one model, and for how long.

    The durations are what keep a passing spike from adding a GPU node
    and a quiet minute from removing one (SPEC.md section 4).
    """

    metrics: Metrics
    breaching_for_seconds: int = 0
    idle_for_seconds: int = 0


@dataclass(frozen=True)
class ActualState:
    """What exists right now, across every system the reconciler touches."""

    capacity: CapacitySnapshot
    #: Keyed by (model, colour).
    deployments: dict[tuple[str, str], DeployedModel] = field(default_factory=dict)
    #: The gateway config currently in effect, by its digest.
    route_hash: str = ""
    #: Traffic weights per model, as the gateway currently has them.
    #: The colour carrying the most traffic is the one that is live.
    weights: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Prometheus signals per model, where they have been observed.
    health: dict[str, DeploymentHealth] = field(default_factory=dict)
    #: How deployments are exposed, from the database (SPEC.md section 8).
    exposures: tuple[Any, ...] = ()
    #: Per-tenant gateway proxy VMs that exist, by name.
    proxies: dict[str, str] = field(default_factory=dict)
    #: Each certificate's current serial, so a proxy built from an
    #: older one can be spotted (SPEC.md section 8).
    certificate_serials: dict[str, int] = field(default_factory=dict)
    #: What CloudStack says about each zone's cluster (ADR-0007).
    clusters: tuple[ClusterState, ...] = ()
    #: For a zone asking for a cluster that is not there: what would be
    #: built, resolved read-only at observe time so the plan line can
    #: name the Kubernetes version and the offering it chose.
    cluster_plans: dict[str, ClusterPlanned] = field(default_factory=dict)
    #: For a zone asking for one that cannot be built: why not. A
    #: warning rather than an action, because nothing can be planned.
    cluster_problems: dict[str, str] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def deployment(self, model: str, colour: str) -> DeployedModel | None:
        return self.deployments.get((model, colour))

    def colours_of(self, model: str) -> list[str]:
        return sorted(colour for name, colour in self.deployments if name == model)

    def live_colour(self, model: str, *, default: str = "blue") -> str:
        """Which colour is currently carrying traffic for this model.

        Read from the gateway's weights rather than stored, so there is
        no rollout state to fall out of step with reality.
        """
        split = self.weights.get(model)
        if not split:
            return default
        return max(split, key=lambda colour: split[colour])

    @classmethod
    def empty_for(cls, spec: PlatformSpec) -> ActualState:
        """A world where nothing exists yet, shaped by the spec.

        Used by `--no-observe` so a plan can be read without reaching
        CloudStack or the cluster.
        """
        return cls(capacity=CapacitySnapshot.empty_for(spec), deployments={})
