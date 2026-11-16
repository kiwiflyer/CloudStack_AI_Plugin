# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with this
# work for additional information regarding copyright ownership. The ASF
# licenses this file to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Live GPU capacity per zone and class (SPEC.md section 4)."""

from __future__ import annotations

import httpx
import pytest
import yaml

from aisvc.cloudstack import CloudStackClient
from aisvc.inventory import CapacitySnapshot, ClassCapacity, GpuDevice, take_snapshot
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


class TestManagedStateAsCloudStackActuallyReportsIt:
    """`usable` tested for "Enabled", which 4.23.0.0 never returns.

    Measured against a real management server: `managedstate` is
    `Managed` or `Unmanaged`, and the separate `state` field reads
    `Free`. The API reference says "Enabled or Disabled", and the code
    believed it — so every real GPU came back unusable, every class
    counted zero, and every deployment would have been refused for want
    of capacity on hosts full of idle cards.

    The check is now a deny-list rather than an allow-list, because the
    two fail differently: an allow-list that misses a value makes the
    whole platform silently capacity-blind with nothing in the logs to
    explain it, while a deny-list that misses one lets a single
    placement fail loudly where an operator can see it.
    """

    def device(self, **raw: object) -> GpuDevice:
        return GpuDevice.from_api(
            {"id": "d1", "hostid": "h1", "gpucardname": "NVIDIA H100 80GB HBM3", **raw}
        )

    def test_a_managed_device_is_usable(self) -> None:
        assert self.device(managedstate="Managed").usable

    def test_an_unmanaged_device_is_not(self) -> None:
        assert not self.device(managedstate="Unmanaged").usable

    def test_a_disabled_device_is_not(self) -> None:
        # What the documentation claimed. Honoured in case some
        # release does return it.
        assert not self.device(managedstate="Disabled").usable

    def test_the_documented_enabled_still_counts(self) -> None:
        assert self.device(managedstate="Enabled").usable

    def test_a_managed_free_device_is_free(self) -> None:
        device = self.device(managedstate="Managed")

        assert device.free
        assert not device.allocated

    def test_a_managed_device_held_by_a_vm_is_not_free(self) -> None:
        device = self.device(managedstate="Managed", virtualmachineid="vm-1")

        assert device.allocated
        assert not device.free

    def test_an_unrecognised_state_counts_rather_than_vanishing(self) -> None:
        # The deny-list choice, stated as a test: a value nobody
        # anticipated is visible capacity, not silent absence.
        assert self.device(managedstate="SomethingNew").usable

    def test_no_state_at_all_is_still_unusable(self) -> None:
        # Absent differs from unrecognised: a response that never
        # mentioned the field is one to distrust, not to read
        # optimistically.
        assert not self.device(managedstate="").usable
        assert not self.device().usable


class TestDeviceStateAsCloudStackActuallyReportsIt:
    """`state` was never read, so a broken GPU counted as capacity.

    Found on a live 4.23.0.0 zone, the inverse of the `managedstate`
    bug above and from the same cause: CloudStack reports two fields
    about a GPU and the engine read one. Of 112 devices, all 112 came
    back `managedstate=Managed` — and sixteen of them, every H100 in
    the zone and the only inference-capable capacity in it, carried
    `state=Error`. The engine counted 112 usable and 112 free.

    The failure that makes it worth a deny-list of its own is a
    platform-wide one: the capacity planner reports GPUs that
    CloudStack will not place anything on, an operator reads a zone
    with room in it, and every deployment onto those cards fails.

    `GpuDevice.State` in 4.23.0.0 is `Allocated`, `Free`, `Error`,
    `PartiallyAllocated` — read out of the management server's own
    source, not the API reference, which was wrong about `managedstate`.
    Only `Error` means "not capacity": an allocated device is capacity
    that happens to be in use, which is what `allocated` is for.
    """

    def device(self, **raw: object) -> GpuDevice:
        return GpuDevice.from_api(
            {
                "id": "d1",
                "hostid": "h1",
                "gpucardname": "NVIDIA H100 80GB HBM3",
                "managedstate": "Managed",
                **raw,
            }
        )

    def test_a_device_in_error_is_not_capacity(self) -> None:
        # The bug, stated plainly. CloudStack will not place anything
        # on this device; neither should the planner promise it.
        device = self.device(state="Error")

        assert not device.usable
        assert not device.free

    def test_a_free_device_is_capacity(self) -> None:
        device = self.device(state="Free")

        assert device.usable
        assert device.free

    def test_an_allocated_device_is_capacity_in_use(self) -> None:
        # Not the same as broken: it counts toward the total, and
        # `allocated` is what keeps it out of the free pool.
        device = self.device(state="Allocated", virtualmachineid="vm-1")

        assert device.usable
        assert device.allocated
        assert not device.free

    def test_cloudstack_calling_it_allocated_is_enough(self) -> None:
        # Two fields again. If the server says allocated, it is
        # allocated, whether or not this caller can see the VM.
        device = self.device(state="Allocated")

        assert device.allocated
        assert not device.free

    def test_partially_allocated_still_has_room(self) -> None:
        # A vGPU parent with profiles left on it. Not an error, and
        # not fully taken.
        device = self.device(state="PartiallyAllocated")

        assert device.usable

    def test_an_unrecognised_state_counts_rather_than_vanishing(self) -> None:
        assert self.device(state="SomethingNew").usable

    def test_an_absent_state_is_not_a_fault(self) -> None:
        """The asymmetry with `managedstate`, deliberately.

        An absent `managedstate` leaves the primary question — is
        CloudStack managing this device at all — unanswered, so the
        device is distrusted. `state` answers a narrower one: is this
        device currently faulted. A response that never mentions it
        reports no fault, and treating silence as a fault would make
        any server that trims the field a zero-capacity platform,
        which is precisely how the `managedstate` bug failed.
        """
        assert self.device().usable
        assert self.device(state="").usable

    def test_both_fields_have_to_agree(self) -> None:
        assert not self.device(managedstate="Unmanaged", state="Free").usable
        assert not self.device(managedstate="Managed", state="Error").usable
        assert self.device(managedstate="Managed", state="Free").usable

    def test_a_host_of_faulted_cards_offers_no_capacity(self) -> None:
        # What the live zone looked like: every H100 in Error.
        devices = [self.device(id=f"d{i}", state="Error") for i in range(8)]

        assert sum(1 for d in devices if d.free) == 0
        assert sum(1 for d in devices if d.usable) == 0
