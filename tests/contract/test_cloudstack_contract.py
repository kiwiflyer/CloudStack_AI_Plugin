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
"""The CloudStack calls we depend on, against recorded responses.

Recorded fixtures stand in for a live management server (SPEC.md section
12). `make record` refreshes them against a lab zone; until then these
tests pin the response shape every caller in `aisvc` relies on.
"""

from __future__ import annotations

import httpx
import pytest

from aisvc.cloudstack import CloudStackClient
from aisvc.inventory import take_snapshot
from aisvc.spec import load_spec
from tests.conftest import CREDENTIALS, load_fixture, recorded_handler

EXAMPLE_SPEC = "examples/platform.yaml"


@pytest.fixture
def client() -> CloudStackClient:
    return CloudStackClient(
        CREDENTIALS,
        transport=httpx.MockTransport(recorded_handler()),
        poll_interval=0.0,
        backoff_base=0.0,
    )


class TestRecordedShapes:
    @pytest.mark.parametrize(
        ("fixture", "item_key", "required"),
        [
            ("listhosts", "host", {"id", "name", "zonename", "state", "resourcestate"}),
            (
                "listvirtualmachines",
                "virtualmachine",
                {"id", "name", "serviceofferingname", "state"},
            ),
            ("listserviceofferings", "serviceoffering", {"id", "name"}),
            (
                "listgpudevices",
                "gpudevice",
                {"id", "hostid", "gpucardname", "managedstate"},
            ),
        ],
    )
    def test_every_record_carries_the_fields_we_read(
        self, fixture: str, item_key: str, required: set[str]
    ) -> None:
        body = load_fixture(fixture)
        items = body[item_key]
        assert items, f"{fixture} has no {item_key} records to check"
        for item in items:
            missing = required - set(item)
            assert not missing, f"{fixture}: {item.get('id')} is missing {sorted(missing)}"

    def test_count_matches_the_number_of_records(self) -> None:
        for fixture, key in (
            ("listhosts", "host"),
            ("listvirtualmachines", "virtualmachine"),
            ("listserviceofferings", "serviceoffering"),
        ):
            body = load_fixture(fixture)
            assert body["count"] == len(body[key])


class TestClientAgainstRecordings:
    async def test_list_hosts_round_trips(self, client: CloudStackClient) -> None:
        async with client:
            hosts = await client.call_paged("listHosts", "host", type="Routing")
        assert [h["id"] for h in hosts] == [
            "host-1",
            "host-2",
            "host-3",
            "host-4",
            "host-5",
        ]

    async def test_service_offerings_cover_every_gpu_class_in_the_example(
        self, client: CloudStackClient
    ) -> None:
        spec = load_spec(EXAMPLE_SPEC)
        async with client:
            offerings = await client.call_paged("listServiceOfferings", "serviceoffering")

        names = {o["name"] for o in offerings}
        for gpu_class in spec.document.gpu_classes:
            assert gpu_class.service_offering in names

    async def test_snapshot_of_the_example_spec_is_consistent(
        self, client: CloudStackClient
    ) -> None:
        spec = load_spec(EXAMPLE_SPEC)
        async with client:
            snapshot = await take_snapshot(client, spec)

        for zone in spec.document.zones:
            for gpu_class in spec.document.gpu_classes:
                capacity = snapshot.for_class(zone.name, gpu_class.name)
                assert capacity is not None
                assert capacity.nodes_healthy <= capacity.nodes_total
                assert capacity.nodes_allocated <= capacity.nodes_healthy
                # GPUs are counted per device now, so the total is whatever
                # CloudStack reports. `gpus_per_node` bounds one node VM,
                # not a host: a vGPU host can hold more free GPUs than a
                # single node would ever take.
                assert capacity.gpus_allocated + capacity.gpus_free <= capacity.gpus_total
                assert capacity.gpus_free == sum(capacity.free_by_host.values())


class TestExampleSpecContract:
    def test_the_example_spec_is_valid(self) -> None:
        spec = load_spec(EXAMPLE_SPEC)
        assert spec.api_version == "aisvc/v1"

    def test_every_model_in_the_example_pins_a_commit_sha(self) -> None:
        spec = load_spec(EXAMPLE_SPEC)
        for model in spec.document.models:
            assert model.source_is_pinned, model.name

    def test_every_model_fits_the_class_it_is_pinned_to(self) -> None:
        """Validation already enforces this; asserted here as a contract."""
        spec = load_spec(EXAMPLE_SPEC)
        for model in spec.document.models:
            pool = spec.node_pool(model.node_pool)
            gpu_class = spec.gpu_class(pool.gpu_class)
            assert model.tensor_parallel <= gpu_class.gpus_per_node
