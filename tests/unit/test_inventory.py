"""Live GPU capacity per zone and class (SPEC.md section 4)."""

from __future__ import annotations

import httpx
import pytest
import yaml

from aisvc.cloudstack import CloudStackClient
from aisvc.inventory import CapacitySnapshot, ClassCapacity, take_snapshot
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.conftest import CREDENTIALS, recorded_handler
from tests.unit.test_spec import LICENSES, MINIMAL


def spec_with_two_classes() -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    doc["gpuClasses"].append(
        {
            "name": "l40s-1x",
            "host_tag": "l40s",
            "service_offering": "GPU-L40S-1x",
            "gpus_per_node": 1,
            "gpu_mem_gb": 48,
            "mode": "passthrough",
        }
    )
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


@pytest.fixture
def spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


@pytest.fixture
def client() -> CloudStackClient:
    return CloudStackClient(
        CREDENTIALS,
        transport=httpx.MockTransport(recorded_handler()),
        poll_interval=0.0,
        backoff_base=0.0,
    )


class TestSnapshot:
    async def test_snapshot_covers_every_zone_and_class_in_the_spec(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        assert isinstance(snapshot, CapacitySnapshot)
        assert snapshot.for_class("zone-east-1", "h100-8x") is not None

    async def test_only_hosts_carrying_the_class_tag_are_counted(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert capacity.nodes_total == 3  # host-1, host-2, host-3; not the l40s or cpu host

    async def test_hosts_in_maintenance_are_not_healthy(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert capacity.nodes_healthy == 2
        assert capacity.nodes_unhealthy == 1

    async def test_gpus_are_counted_per_device_not_per_host(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        """ADR-0003: capacity comes from listGpuDevices, not host tags."""
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        # host-1 has 4 devices, host-2 has 4; host-3 is in maintenance.
        assert capacity.gpus_total == 8

    async def test_allocation_is_read_from_the_device_not_inferred(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert capacity.gpus_allocated == 2  # gpu-1 and gpu-2 hold vm-1

    async def test_a_disabled_device_is_neither_free_nor_usable(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert capacity.gpus_unusable == 1  # gpu-4 is Disabled
        # host-1: 4 devices, 2 allocated, 1 disabled -> 1 free. host-2: 4 free.
        assert capacity.gpus_free == 5

    async def test_free_gpus_are_tracked_per_host(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        """A replica never spans hosts, so the per-host split is what matters."""
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert capacity.free_by_host == {"host-1": 1, "host-2": 4}

    async def test_an_unhealthy_host_contributes_no_capacity(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert "host-3" not in capacity.free_by_host
        assert capacity.nodes_unhealthy == 1

    async def test_vgpu_capacity_counts_the_mdevs_not_the_host(
        self, client: CloudStackClient
    ) -> None:
        """The whole point of ADR-0003: one shared host is not one GPU."""
        async with client:
            snapshot = await take_snapshot(client, spec_with_two_classes())

        capacity = snapshot.for_class("zone-east-1", "l40s-1x")
        assert capacity is not None
        assert capacity.nodes_total == 1
        # Four vGPUs carved from one card; the parent is not double-counted.
        assert capacity.gpus_total == 4

    async def test_vgpu_allocation_is_per_tenant_not_per_host(
        self, client: CloudStackClient
    ) -> None:
        """Two tenants share host-4; host-tag counting could not see that."""
        async with client:
            snapshot = await take_snapshot(client, spec_with_two_classes())

        capacity = snapshot.for_class("zone-east-1", "l40s-1x")
        assert capacity is not None
        assert capacity.gpus_allocated == 2
        assert capacity.gpus_free == 2

    async def test_unknown_class_returns_none(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        assert snapshot.for_class("zone-east-1", "no-such-class") is None

    async def test_snapshot_records_when_it_was_taken(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        assert snapshot.taken_at.tzinfo is not None


class TestClassCapacity:
    def test_can_host_asks_whether_one_host_has_the_gpus(self) -> None:
        capacity = ClassCapacity(
            zone="zone-east-1",
            gpu_class="h100-8x",
            gpus_per_node=8,
            nodes_total=3,
            nodes_healthy=2,
            gpus_total=16,
            free_by_host={"host-1": 8, "host-2": 2},
        )

        assert capacity.can_host(tensor_parallel=8)
        assert capacity.can_host(tensor_parallel=2)

    def test_scattered_free_gpus_do_not_add_up_across_hosts(self) -> None:
        """Eight free GPUs spread over four hosts cannot run an 8-way replica."""
        capacity = ClassCapacity(
            zone="zone-east-1",
            gpu_class="h100-8x",
            gpus_per_node=8,
            nodes_total=4,
            nodes_healthy=4,
            gpus_total=32,
            free_by_host={"a": 2, "b": 2, "c": 2, "d": 2},
        )

        assert capacity.gpus_free == 8
        assert not capacity.can_host(tensor_parallel=8)
        assert capacity.can_host(tensor_parallel=2)

    def test_tensor_parallel_beyond_one_node_never_fits(self) -> None:
        """A replica is capped at one 8-GPU host (SPEC.md section 1)."""
        capacity = ClassCapacity(
            zone="zone-east-1",
            gpu_class="h100-8x",
            gpus_per_node=8,
            nodes_total=4,
            nodes_healthy=4,
            gpus_total=32,
            free_by_host={"a": 8, "b": 8, "c": 8, "d": 8},
        )

        assert not capacity.can_host(tensor_parallel=16)

    def test_nothing_free_hosts_nothing(self) -> None:
        capacity = ClassCapacity(
            zone="z", gpu_class="c", gpus_per_node=8, nodes_total=1, nodes_healthy=1
        )

        assert capacity.gpus_free == 0
        assert not capacity.can_host(tensor_parallel=1)

    def test_free_nodes_counts_hosts_with_room_for_a_whole_node(self) -> None:
        capacity = ClassCapacity(
            zone="z",
            gpu_class="c",
            gpus_per_node=8,
            nodes_total=3,
            nodes_healthy=3,
            free_by_host={"a": 8, "b": 4, "c": 8},
        )

        assert capacity.nodes_free == 2
        assert capacity.hosts_with_free(4) == 3

    def test_the_largest_free_block_is_reported(self) -> None:
        capacity = ClassCapacity(
            zone="z",
            gpu_class="c",
            gpus_per_node=8,
            nodes_total=2,
            nodes_healthy=2,
            free_by_host={"a": 3, "b": 6},
        )

        assert capacity.largest_free_block() == 6

    def test_a_partly_used_host_counts_as_allocated(self) -> None:
        capacity = ClassCapacity(
            zone="z",
            gpu_class="c",
            gpus_per_node=8,
            nodes_total=2,
            nodes_healthy=2,
            free_by_host={"a": 8, "b": 5},
            allocated_by_host={"a": 0, "b": 3},
            host_ids=("a", "b"),
        )

        assert capacity.nodes_allocated == 1

    def test_a_vgpu_host_is_both_allocated_and_free(self) -> None:
        """Subtracting free from capacity cannot express this; two counts can."""
        capacity = ClassCapacity(
            zone="z",
            gpu_class="l40s-1x",
            gpus_per_node=1,
            nodes_total=1,
            nodes_healthy=1,
            gpus_total=4,
            free_by_host={"host-4": 2},
            allocated_by_host={"host-4": 2},
            host_ids=("host-4",),
        )

        assert capacity.nodes_allocated == 1
        assert capacity.nodes_free == 1
        assert capacity.gpus_free == 2

    def test_unhealthy_nodes_are_derived(self) -> None:
        capacity = ClassCapacity(
            zone="z", gpu_class="c", gpus_per_node=8, nodes_total=5, nodes_healthy=4
        )

        assert capacity.nodes_unhealthy == 1
