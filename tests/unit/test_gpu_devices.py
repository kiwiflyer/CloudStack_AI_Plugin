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
"""Per-GPU capacity from CloudStack's GPU device API (ADR-0003).

Counting GPUs per host from `host_tag` is right for passthrough, where a
node VM takes the whole host, and wrong for vGPU and MIG, where one host
is shared. CloudStack 4.21+ reports devices individually, with
`virtualmachineid` saying which VM holds each one.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.inventory import GpuDevice, devices_by_host, parse_gpu_devices
from tests.conftest import load_fixture

PASSTHROUGH = {
    "id": "gpu-1",
    "hostid": "host-1",
    "hostname": "gpu-h100-01",
    "gpucardid": "card-h100",
    "gpucardname": "NVIDIA H100 80GB HBM3",
    "gpudevicetype": "PCI",
    "busaddress": "0000:17:00.0",
    "managedstate": "Enabled",
    "numanode": "0",
}


def device(**overrides: Any) -> GpuDevice:
    return GpuDevice.from_api({**PASSTHROUGH, **overrides})


class TestGpuDevice:
    def test_it_reads_the_identifying_fields(self) -> None:
        parsed = device()

        assert parsed.id == "gpu-1"
        assert parsed.host_id == "host-1"
        assert parsed.card_name == "NVIDIA H100 80GB HBM3"
        assert parsed.bus_address == "0000:17:00.0"

    def test_a_device_with_no_vm_is_free(self) -> None:
        assert not device().allocated

    def test_a_device_held_by_a_vm_is_allocated(self) -> None:
        assert device(virtualmachineid="vm-1").allocated

    def test_an_empty_vm_id_is_not_an_allocation(self) -> None:
        assert not device(virtualmachineid="").allocated

    def test_an_enabled_device_is_usable(self) -> None:
        assert device().usable

    def test_a_disabled_device_is_not_usable(self) -> None:
        """A disabled device must not be counted as free capacity."""
        assert not device(managedstate="Disabled").usable

    def test_a_physical_device_has_no_parent(self) -> None:
        assert device().parent_id is None
        assert not device().is_vgpu

    def test_a_vgpu_names_its_parent(self) -> None:
        vgpu = device(
            id="vgpu-1",
            parentgpudeviceid="gpu-1",
            vgpuprofilename="NVIDIA L40S-12Q",
            gpudevicetype="MDEV",
        )

        assert vgpu.parent_id == "gpu-1"
        assert vgpu.is_vgpu
        assert vgpu.vgpu_profile == "NVIDIA L40S-12Q"

    def test_a_missing_managed_state_is_treated_as_unusable(self) -> None:
        """Absent means unknown, and unknown capacity is not free capacity."""
        raw = {k: v for k, v in PASSTHROUGH.items() if k != "managedstate"}
        assert not GpuDevice.from_api(raw).usable


class TestParsing:
    def test_the_recorded_response_parses(self) -> None:
        devices = parse_gpu_devices(load_fixture("listgpudevices")["gpudevice"])
        assert len(devices) == 15

    def test_the_fixture_still_looks_like_a_real_server(self) -> None:
        """The fake agreed with two bugs; pin it to what 4.23.0.0 sends.

        This file recorded `managedstate: "Enabled"` — a value
        CloudStack never returns — and omitted `state`, which it always
        sends. Both bugs lived behind that: `usable` tested for
        "Enabled" and passed here while failing on every real GPU, and
        `state` went unread while sixteen faulted H100s counted as free
        capacity on a live zone.

        A fixture that agrees with the code proves nothing. These are
        the values the management server's own `GpuDevice` enums
        declare.
        """
        raw = load_fixture("listgpudevices")["gpudevice"]

        assert {d["managedstate"] for d in raw} <= {"Managed", "Unmanaged"}
        assert all("state" in d for d in raw), "every device reports a state"
        assert {d["state"] for d in raw} <= {
            "Allocated",
            "Free",
            "Error",
            "PartiallyAllocated",
        }

    def test_devices_group_by_host(self) -> None:
        devices = parse_gpu_devices(load_fixture("listgpudevices")["gpudevice"])
        grouped = devices_by_host(devices)

        assert set(grouped) == {"host-1", "host-2", "host-3", "host-4"}
        assert len(grouped["host-2"]) == 4


class TestAllocatableUnits:
    """A physical GPU carved into vGPUs is a container, not a unit."""

    def test_passthrough_devices_are_their_own_units(self) -> None:
        devices = [device(id="a"), device(id="b")]

        assert {d.id for d in _allocatable(devices)} == {"a", "b"}

    def test_a_carved_physical_device_is_not_counted(self) -> None:
        """Counting parent and children would double-count the same silicon."""
        parent = device(id="phys")
        children = [
            device(id="vgpu-1", parentgpudeviceid="phys"),
            device(id="vgpu-2", parentgpudeviceid="phys"),
        ]

        units = _allocatable([parent, *children])

        assert {d.id for d in units} == {"vgpu-1", "vgpu-2"}

    def test_an_uncarved_physical_device_is_still_a_unit(self) -> None:
        """A vGPU-capable card with no MDEVs yet is one allocatable GPU."""
        units = _allocatable([device(id="phys")])

        assert {d.id for d in units} == {"phys"}

    def test_the_fixture_counts_fourteen_allocatable_units(self) -> None:
        """10 passthrough H100s, plus 4 L40S vGPUs whose parent is excluded."""
        devices = parse_gpu_devices(load_fixture("listgpudevices")["gpudevice"])

        assert len(_allocatable(devices)) == 14


def _allocatable(devices: list[GpuDevice]) -> list[GpuDevice]:
    from aisvc.inventory import allocatable_devices

    return allocatable_devices(devices)


class TestAllocatableEdgeCases:
    def test_a_child_whose_parent_is_absent_is_still_a_unit(self) -> None:
        """Paging could split a parent from its children; never drop a unit."""
        orphan = device(id="vgpu-9", parentgpadeviceid=None, parentgpudeviceid="missing")

        assert _allocatable([orphan]) == [orphan]

    def test_an_empty_list_yields_nothing(self) -> None:
        assert _allocatable([]) == []


@pytest.mark.parametrize(
    ("managed", "vm", "expect_free"),
    [
        ("Enabled", None, True),
        ("Enabled", "vm-1", False),
        ("Disabled", None, False),
        ("Disabled", "vm-1", False),
    ],
)
def test_free_means_usable_and_unallocated(managed: str, vm: str | None, expect_free: bool) -> None:
    parsed = device(managedstate=managed, virtualmachineid=vm)
    assert parsed.free is expect_free
