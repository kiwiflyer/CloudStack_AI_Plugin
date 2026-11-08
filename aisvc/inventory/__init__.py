"""Live GPU capacity per zone and class (SPEC.md section 4).

Reads `listHosts`, `listVirtualMachines` and `listServiceOfferings` and
folds them into a `CapacitySnapshot` the reconciler can diff against the
desired state.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
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
    "GpuDevice",
    "HostRecord",
    "PoolCapacity",
    "allocatable_devices",
    "devices_by_host",
    "parse_gpu_devices",
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
class GpuDevice:
    """One GPU as CloudStack reports it (`listGpuDevices`, 4.21+).

    Allocation is per device: `virtualmachineid` names the VM holding
    it. That is what makes vGPU and MIG countable, where inferring from
    host tags cannot be (ADR-0003).
    """

    id: str
    host_id: str
    card_name: str
    bus_address: str = ""
    managed_state: str = ""
    numa_node: str = ""
    device_type: str = ""
    parent_id: str | None = None
    vgpu_profile: str | None = None
    virtual_machine_id: str | None = None
    vm_state: str = ""

    @property
    def allocated(self) -> bool:
        return bool(self.virtual_machine_id)

    @property
    def usable(self) -> bool:
        """Enabled devices only; anything else is not free capacity."""
        return self.managed_state == "Enabled"

    @property
    def free(self) -> bool:
        return self.usable and not self.allocated

    @property
    def is_vgpu(self) -> bool:
        return self.parent_id is not None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GpuDevice:
        return cls(
            id=str(raw.get("id", "")),
            host_id=str(raw.get("hostid", "")),
            card_name=str(raw.get("gpucardname", "")),
            bus_address=str(raw.get("busaddress", "")),
            managed_state=str(raw.get("managedstate", "")),
            numa_node=str(raw.get("numanode", "")),
            device_type=str(raw.get("gpudevicetype", "")),
            parent_id=str(raw["parentgpudeviceid"]) if raw.get("parentgpudeviceid") else None,
            vgpu_profile=str(raw["vgpuprofilename"]) if raw.get("vgpuprofilename") else None,
            virtual_machine_id=str(raw["virtualmachineid"])
            if raw.get("virtualmachineid")
            else None,
            vm_state=str(raw.get("vmstate", "")),
        )


def parse_gpu_devices(raw: Iterable[dict[str, Any]]) -> list[GpuDevice]:
    return [GpuDevice.from_api(item) for item in raw]


def devices_by_host(devices: Iterable[GpuDevice]) -> dict[str, list[GpuDevice]]:
    grouped: dict[str, list[GpuDevice]] = {}
    for device in devices:
        grouped.setdefault(device.host_id, []).append(device)
    return grouped


def allocatable_devices(devices: Sequence[GpuDevice]) -> list[GpuDevice]:
    """The devices that can actually be given to a VM.

    A physical GPU carved into vGPUs is a container: counting it and its
    children would double-count the same silicon. One that has not been
    carved is itself the unit, and a child whose parent is missing from
    the list still counts, so paging can never lose capacity.
    """
    parents = {d.parent_id for d in devices if d.parent_id}
    return [d for d in devices if d.id not in parents]


@dataclass(frozen=True)
class ClassCapacity:
    """Capacity for one GPU class in one zone, counted per GPU.

    Counted from CloudStack's GPU devices rather than inferred from host
    tags, so vGPU and MIG classes — where one host is shared between
    tenants — count correctly (ADR-0003).
    """

    zone: str
    gpu_class: str
    gpus_per_node: int
    nodes_total: int = 0
    nodes_healthy: int = 0
    gpus_total: int = 0
    gpus_allocated: int = 0
    gpus_unusable: int = 0
    #: Free GPUs per host. A replica never spans hosts, so placement is
    #: a question about one host, not about the zone's total.
    free_by_host: dict[str, int] = field(default_factory=dict)
    #: Allocated GPUs per host. Tracked separately from `free_by_host`
    #: because on a vGPU host both are non-zero at once.
    allocated_by_host: dict[str, int] = field(default_factory=dict)
    host_ids: tuple[str, ...] = ()

    @property
    def nodes_unhealthy(self) -> int:
        return self.nodes_total - self.nodes_healthy

    @property
    def gpus_free(self) -> int:
        return sum(self.free_by_host.values())

    @property
    def nodes_allocated(self) -> int:
        """Hosts with at least one GPU handed to a VM.

        Read from allocations rather than inferred from free GPUs: a
        vGPU host can be both partly allocated and partly free, which
        subtracting free from capacity cannot express.
        """
        return sum(1 for count in self.allocated_by_host.values() if count > 0)

    @property
    def nodes_free(self) -> int:
        """Hosts with room for a whole node VM of this class."""
        return self.hosts_with_free(self.gpus_per_node)

    def hosts_with_free(self, gpus: int) -> int:
        if gpus <= 0:
            return len(self.free_by_host)
        return sum(1 for free in self.free_by_host.values() if free >= gpus)

    def largest_free_block(self) -> int:
        """The most free GPUs on any single host."""
        return max(self.free_by_host.values(), default=0)

    def can_host(self, *, tensor_parallel: int) -> bool:
        """Whether a replica of this width fits on one host.

        A replica never spans hosts (SPEC.md section 1), so this asks
        whether some single host has that many free GPUs — not whether
        the zone does.
        """
        if tensor_parallel > self.gpus_per_node:
            return False
        return self.largest_free_block() >= tensor_parallel


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


def _is_free(device: GpuDevice) -> bool:
    return device.free


def _is_allocated(device: GpuDevice) -> bool:
    return device.allocated


def _count_per_host(
    hosts: Sequence[HostRecord],
    by_host: Mapping[str, Sequence[GpuDevice]],
    card: str | None,
    predicate: Callable[[GpuDevice], bool],
) -> dict[str, int]:
    """Count matching devices on each host, optionally narrowed by card."""
    return {
        host.id: sum(
            1
            for device in by_host.get(host.id, [])
            if predicate(device) and (not card or device.card_name == card)
        )
        for host in hosts
    }


async def take_snapshot(client: CloudStackClient, spec: PlatformSpec) -> CapacitySnapshot:
    """Read live GPU capacity for every zone and class the spec declares.

    GPU counts come from `listGpuDevices` (CloudStack 4.21+), which
    reports allocation per device. Host tags still select which hosts
    belong to a class, since that is what placement uses, but they no
    longer decide how much capacity there is.
    """
    raw_hosts = await client.call_paged("listHosts", "host", type="Routing")
    raw_vms = await client.call_paged("listVirtualMachines", "virtualmachine", listall=True)
    raw_devices = await client.call_paged("listGpuDevices", "gpudevice")

    hosts = [HostRecord.from_api(h) for h in raw_hosts]
    units = allocatable_devices(parse_gpu_devices(raw_devices))
    by_host = devices_by_host(units)

    classes: dict[tuple[str, str], ClassCapacity] = {}

    for zone in spec.document.zones:
        for gpu_class in spec.document.gpu_classes:
            matching = [
                h
                for h in hosts
                if h.zone_name == zone.cloudstack_zone and gpu_class.host_tag in h.tags
            ]
            healthy = [h for h in matching if h.healthy]

            card = getattr(gpu_class, "gpu_card", None)
            devices: list[GpuDevice] = []
            for host in healthy:
                for device in by_host.get(host.id, []):
                    if card and device.card_name != card:
                        continue
                    devices.append(device)

            free_by_host = _count_per_host(healthy, by_host, card, _is_free)
            allocated_by_host = _count_per_host(healthy, by_host, card, _is_allocated)

            classes[(zone.name, gpu_class.name)] = ClassCapacity(
                zone=zone.name,
                gpu_class=gpu_class.name,
                gpus_per_node=gpu_class.gpus_per_node,
                nodes_total=len(matching),
                nodes_healthy=len(healthy),
                gpus_total=len(devices),
                gpus_allocated=sum(1 for d in devices if d.allocated),
                gpus_unusable=sum(1 for d in devices if not d.usable),
                free_by_host=free_by_host,
                allocated_by_host=allocated_by_host,
                host_ids=tuple(h.id for h in healthy),
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
