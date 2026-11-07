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

    async def test_nodes_running_the_class_offering_count_as_allocated(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert capacity.nodes_allocated == 1  # vm-1 on host-1

    async def test_free_nodes_exclude_allocated_and_unhealthy(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert capacity.nodes_free == 1  # host-2 only

    async def test_gpu_counts_scale_by_gpus_per_node(
        self, client: CloudStackClient, spec: PlatformSpec
    ) -> None:
        async with client:
            snapshot = await take_snapshot(client, spec)

        capacity = snapshot.for_class("zone-east-1", "h100-8x")
        assert capacity is not None
        assert capacity.gpus_total == 3 * 8
        assert capacity.gpus_allocated == 1 * 8
        assert capacity.gpus_free == 1 * 8

    async def test_a_vm_with_an_unrelated_offering_does_not_allocate(
        self, client: CloudStackClient
    ) -> None:
        """host-4 runs a Medium-Instance VM, which is not the l40s offering."""
        async with client:
            snapshot = await take_snapshot(client, spec_with_two_classes())

        capacity = snapshot.for_class("zone-east-1", "l40s-1x")
        assert capacity is not None
        assert capacity.nodes_total == 1
        assert capacity.nodes_allocated == 0
        assert capacity.nodes_free == 1

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
    def test_can_host_asks_whether_free_gpus_cover_a_replica(self) -> None:
        capacity = ClassCapacity(
            zone="zone-east-1",
            gpu_class="h100-8x",
            gpus_per_node=8,
            nodes_total=3,
            nodes_healthy=2,
            nodes_allocated=1,
        )
        assert capacity.can_host(tensor_parallel=8)
        assert not capacity.can_host(tensor_parallel=16)

    def test_tensor_parallel_beyond_one_node_never_fits(self) -> None:
        """A replica is capped at one 8-GPU host (SPEC.md section 1)."""
        capacity = ClassCapacity(
            zone="zone-east-1",
            gpu_class="h100-8x",
            gpus_per_node=8,
            nodes_total=4,
            nodes_healthy=4,
            nodes_allocated=0,
        )
        assert not capacity.can_host(tensor_parallel=16)

    def test_derived_counts_are_consistent(self) -> None:
        capacity = ClassCapacity(
            zone="zone-east-1",
            gpu_class="h100-8x",
            gpus_per_node=8,
            nodes_total=5,
            nodes_healthy=4,
            nodes_allocated=2,
        )
        assert capacity.nodes_unhealthy == 1
        assert capacity.nodes_free == 2
        assert capacity.gpus_total == 40
        assert capacity.gpus_free == 16
