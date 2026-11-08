"""Live GPU capacity per zone and class (SPEC.md section 4).

Reads `listHosts`, `listVirtualMachines` and `listServiceOfferings` and
folds them into a `CapacitySnapshot` the reconciler can diff against the
desired state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aisvc.cloudstack import CloudStackClient

if TYPE_CHECKING:
    from aisvc.spec import PlatformSpec

__all__ = [
    "NODE_NAME_RE",
    "CapacitySnapshot",
    "ClassCapacity",
    "HostRecord",
    "PoolCapacity",
    "take_snapshot",
]

#: GPU node VMs are named aisvc-<pool>-<ordinal>; see aisvc.provision.
NODE_NAME_RE = re.compile(r"^aisvc-(?P<pool>.+)-(?P<ordinal>\d{3,})$")

#: A host is usable only when it is up and its resource state is enabled.
HEALTHY_RESOURCE_STATES = frozenset({"Enabled"})
HEALTHY_STATES = frozenset({"Up"})


@dataclass(frozen=True, slots=True)
class HostRecord:
    """One GPU host, as CloudStack reports it."""

    id: str
    name: str
    zone_name: str
    tags: frozenset[str]
    state: str
    resource_state: str

    @property
    def healthy(self) -> bool:
        return self.state in HEALTHY_STATES and self.resource_state in HEALTHY_RESOURCE_STATES

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> HostRecord:
        tags = raw.get("hosttags") or ""
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            zone_name=str(raw.get("zonename", "")),
            tags=frozenset(t.strip() for t in tags.split(",") if t.strip()),
            state=str(raw.get("state", "")),
            resource_state=str(raw.get("resourcestate", "")),
        )


@dataclass(frozen=True, slots=True)
class ClassCapacity:
    """Capacity for one GPU class in one zone."""

    zone: str
    gpu_class: str
    gpus_per_node: int
    nodes_total: int
    nodes_healthy: int
    nodes_allocated: int
    host_ids: tuple[str, ...] = ()

    @property
    def nodes_unhealthy(self) -> int:
        return self.nodes_total - self.nodes_healthy

    @property
    def nodes_free(self) -> int:
        return max(0, self.nodes_healthy - self.nodes_allocated)

    @property
    def gpus_total(self) -> int:
        return self.nodes_total * self.gpus_per_node

    @property
    def gpus_allocated(self) -> int:
        return self.nodes_allocated * self.gpus_per_node

    @property
    def gpus_free(self) -> int:
        return self.nodes_free * self.gpus_per_node

    @property
    def gpus_unhealthy(self) -> int:
        return self.nodes_unhealthy * self.gpus_per_node

    #: Counted per host, from `host_tag`. Correct for passthrough, where a
    #: node VM takes the whole host; wrong for `vgpu` and `mig`, which
    #: share one host between tenants. CloudStack 4.21+ discovers GPU
    #: devices per host and is the proper source
    #: (docs/adr/0003-cloudstack-release.md).
    def can_host(self, *, tensor_parallel: int) -> bool:
        """Whether a replica of this width fits on one free node.

        A replica never spans hosts (SPEC.md section 1), so tensor parallel
        beyond `gpus_per_node` can never be placed.
        """
        if tensor_parallel > self.gpus_per_node:
            return False
        return self.nodes_free > 0


@dataclass(frozen=True, slots=True)
class PoolCapacity:
    """The GPU node VMs that currently belong to one node pool.

    A pool's size is counted in node VMs, not physical hosts: `min_nodes`
    and `max_nodes` size the pool, while `ClassCapacity` says whether the
    hosts to place them on exist.
    """

    pool: str
    zone: str
    gpu_class: str
    #: Node VM names, in ordinal order.
    nodes: tuple[str, ...] = ()

    @property
    def node_count(self) -> int:
        return len(self.nodes)


@dataclass(frozen=True)
class CapacitySnapshot:
    """What CloudStack actually has, at one moment."""

    taken_at: datetime
    classes: dict[tuple[str, str], ClassCapacity] = field(default_factory=dict)
    pools: dict[str, PoolCapacity] = field(default_factory=dict)
    hosts: tuple[HostRecord, ...] = ()

    def for_class(self, zone: str, gpu_class: str) -> ClassCapacity | None:
        return self.classes.get((zone, gpu_class))

    def for_pool(self, pool: str) -> PoolCapacity | None:
        return self.pools.get(pool)

    @classmethod
    def empty_for(cls, spec: PlatformSpec) -> CapacitySnapshot:
        """A zero-capacity snapshot of everything the spec declares.

        Used by `aisvc plan --no-observe` to show what building the
        platform from nothing would take, without calling CloudStack.
        """
        classes = {
            (zone.name, gpu_class.name): ClassCapacity(
                zone=zone.name,
                gpu_class=gpu_class.name,
                gpus_per_node=gpu_class.gpus_per_node,
                nodes_total=0,
                nodes_healthy=0,
                nodes_allocated=0,
            )
            for zone in spec.document.zones
            for gpu_class in spec.document.gpu_classes
        }
        pools = {
            pool.name: PoolCapacity(
                pool=pool.name, zone=pool.zone, gpu_class=pool.gpu_class, nodes=()
            )
            for pool in spec.document.node_pools
        }
        return cls(taken_at=datetime.now(UTC), classes=classes, pools=pools)

    def for_zone(self, zone: str) -> list[ClassCapacity]:
        return [cap for (z, _), cap in self.classes.items() if z == zone]

    @property
    def gpus_total(self) -> int:
        return sum(cap.gpus_total for cap in self.classes.values())

    @property
    def gpus_free(self) -> int:
        return sum(cap.gpus_free for cap in self.classes.values())


async def take_snapshot(client: CloudStackClient, spec: PlatformSpec) -> CapacitySnapshot:
    """Read live GPU capacity for every zone and class the spec declares."""
    raw_hosts = await client.call_paged("listHosts", "host", type="Routing")
    raw_vms = await client.call_paged("listVirtualMachines", "virtualmachine", listall=True)

    hosts = [HostRecord.from_api(h) for h in raw_hosts]

    # host id -> the service offerings of the VMs placed on it
    offerings_by_host: dict[str, set[str]] = {}
    for vm in raw_vms:
        host_id = str(vm.get("hostid") or "")
        if not host_id:
            continue
        offering = str(vm.get("serviceofferingname") or "")
        offerings_by_host.setdefault(host_id, set()).add(offering)

    classes: dict[tuple[str, str], ClassCapacity] = {}

    for zone in spec.document.zones:
        for gpu_class in spec.document.gpu_classes:
            matching = [
                h
                for h in hosts
                if h.zone_name == zone.cloudstack_zone and gpu_class.host_tag in h.tags
            ]
            healthy = [h for h in matching if h.healthy]
            allocated = [
                h
                for h in healthy
                if gpu_class.service_offering in offerings_by_host.get(h.id, set())
            ]
            classes[(zone.name, gpu_class.name)] = ClassCapacity(
                zone=zone.name,
                gpu_class=gpu_class.name,
                gpus_per_node=gpu_class.gpus_per_node,
                nodes_total=len(matching),
                nodes_healthy=len(healthy),
                nodes_allocated=len(allocated),
                host_ids=tuple(h.id for h in matching),
            )

    pools: dict[str, PoolCapacity] = {}
    for pool in spec.document.node_pools:
        members = sorted(
            (
                (int(match.group("ordinal")), str(vm.get("name", "")))
                for vm in raw_vms
                if (match := NODE_NAME_RE.match(str(vm.get("name", ""))))
                and match.group("pool") == pool.name
            ),
        )
        pools[pool.name] = PoolCapacity(
            pool=pool.name,
            zone=pool.zone,
            gpu_class=pool.gpu_class,
            nodes=tuple(name for _, name in members),
        )

    return CapacitySnapshot(
        taken_at=datetime.now(UTC),
        classes=classes,
        pools=pools,
        hosts=tuple(hosts),
    )
