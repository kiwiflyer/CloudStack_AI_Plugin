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
"""Bootstrapping a CKS cluster (ADR-0007, recommendation 3).

A zone with `create: true` should not need an operator to go and build
a cluster by hand first. Everything `createKubernetesCluster` needs
beyond a name and a zone can be resolved from CloudStack — except what
costs money, and that is shown in the plan rather than guessed at
silently.

**It resolves before it creates, and never creates twice.** A cluster
that already exists under the name the zone gives is the cluster; a
second `apply` must plan nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.cloudstack.clusters import (
    ClusterBuilder,
    ClusterUnbuildable,
)

VERSIONS = [
    {
        "id": "v-129",
        "name": "v1.29.4",
        "semanticversion": "1.29.4",
        "state": "Enabled",
        "mincpunumber": 2,
        "minmemory": 2048,
    },
    {
        "id": "v-130",
        "name": "v1.30.1",
        "semanticversion": "1.30.1",
        "state": "Enabled",
        "mincpunumber": 2,
        "minmemory": 2048,
    },
    {
        "id": "v-131",
        "name": "v1.31.0",
        "semanticversion": "1.31.0",
        "state": "Disabled",
        "mincpunumber": 2,
        "minmemory": 2048,
    },
]

OFFERINGS = [
    {"id": "o-tiny", "name": "Tiny", "cpunumber": 1, "memory": 512},
    {"id": "o-small", "name": "Small Instance", "cpunumber": 2, "memory": 2048},
    {"id": "o-medium", "name": "Medium Instance", "cpunumber": 4, "memory": 8192},
]


class FakeClient:
    """Answers the three list calls, and records every write."""

    def __init__(
        self,
        versions: list[dict[str, Any]] | None = None,
        offerings: list[dict[str, Any]] | None = None,
        clusters: list[dict[str, Any]] | None = None,
    ) -> None:
        self.versions = VERSIONS if versions is None else versions
        self.offerings = OFFERINGS if offerings is None else offerings
        self.clusters = [] if clusters is None else clusters
        self.writes: list[tuple[str, dict[str, Any]]] = []

    async def call_paged(self, command: str, item_key: str, **params: Any) -> list[Any]:
        if command == "listKubernetesSupportedVersions":
            return list(self.versions)
        if command == "listServiceOfferings":
            return list(self.offerings)
        if command == "listKubernetesClusters":
            return list(self.clusters)
        if command == "listZones":
            return [{"id": "z-east", "name": "East-1"}]
        raise AssertionError(f"unexpected list {command}")

    async def call_async(self, command: str, **params: Any) -> dict[str, Any]:
        self.writes.append((command, params))
        return {"id": "k-new"}


def builder(client: FakeClient) -> ClusterBuilder:
    return ClusterBuilder(client)


class TestChoosingAVersion:
    async def test_it_takes_the_newest_enabled_version(self) -> None:
        planned = await builder(FakeClient()).resolve(zone="East-1", cluster="aisvc-east-1")

        assert planned.version == "1.30.1"

    async def test_a_disabled_version_is_not_chosen(self) -> None:
        # 1.31.0 is newer and would win on version alone. CloudStack
        # refuses a disabled version for a new cluster, so choosing it
        # would fail at create time with a message about something the
        # engine picked.
        planned = await builder(FakeClient()).resolve(zone="East-1", cluster="aisvc-east-1")

        assert planned.version_id == "v-130"

    async def test_a_pinned_version_is_honoured(self) -> None:
        planned = await builder(FakeClient()).resolve(
            zone="East-1", cluster="aisvc-east-1", version="1.29.4"
        )

        assert planned.version_id == "v-129"

    async def test_a_pinned_version_that_is_not_there_is_refused(self) -> None:
        with pytest.raises(ClusterUnbuildable) as raised:
            await builder(FakeClient()).resolve(
                zone="East-1", cluster="aisvc-east-1", version="1.99.0"
            )

        assert "1.99.0" in str(raised.value)

    async def test_a_pinned_version_that_is_disabled_is_refused(self) -> None:
        with pytest.raises(ClusterUnbuildable) as raised:
            await builder(FakeClient()).resolve(
                zone="East-1", cluster="aisvc-east-1", version="1.31.0"
            )

        assert "disabled" in str(raised.value).lower()

    async def test_no_enabled_version_says_how_to_register_one(self) -> None:
        # The likeliest first-run failure, and an operator has no way
        # to guess `addKubernetesSupportedVersion` from "not found".
        client = FakeClient(versions=[])

        with pytest.raises(ClusterUnbuildable) as raised:
            await builder(client).resolve(zone="East-1", cluster="aisvc-east-1")

        assert "addKubernetesSupportedVersion" in str(raised.value)

    async def test_versions_sort_numerically_not_as_text(self) -> None:
        # "1.9.0" > "1.10.0" as strings, and picking 1.9 would quietly
        # build every cluster on an older Kubernetes than asked for.
        versions = [
            {"id": "v-9", "semanticversion": "1.9.0", "state": "Enabled"},
            {"id": "v-10", "semanticversion": "1.10.0", "state": "Enabled"},
        ]
        planned = await builder(FakeClient(versions=versions)).resolve(
            zone="East-1", cluster="aisvc-east-1"
        )

        assert planned.version_id == "v-10"


class TestChoosingAnOffering:
    async def test_it_takes_the_smallest_that_clears_the_minimums(self) -> None:
        # Tiny is smaller but below the version's 2 cores / 2 GB.
        planned = await builder(FakeClient()).resolve(zone="East-1", cluster="aisvc-east-1")

        assert planned.offering == "Small Instance"

    async def test_a_named_offering_is_honoured(self) -> None:
        planned = await builder(FakeClient()).resolve(
            zone="East-1", cluster="aisvc-east-1", offering="Medium Instance"
        )

        assert planned.offering_id == "o-medium"

    async def test_a_named_offering_that_is_too_small_is_refused(self) -> None:
        # Better than a CloudStack error hours later about a cluster
        # that will not come up.
        with pytest.raises(ClusterUnbuildable) as raised:
            await builder(FakeClient()).resolve(
                zone="East-1", cluster="aisvc-east-1", offering="Tiny"
            )

        assert "Tiny" in str(raised.value)

    async def test_a_named_offering_that_does_not_exist_is_refused(self) -> None:
        with pytest.raises(ClusterUnbuildable):
            await builder(FakeClient()).resolve(
                zone="East-1", cluster="aisvc-east-1", offering="No Such Offering"
            )

    async def test_nothing_big_enough_is_refused_with_the_minimums(self) -> None:
        client = FakeClient(offerings=[{"id": "o", "name": "Tiny", "cpunumber": 1, "memory": 512}])

        with pytest.raises(ClusterUnbuildable) as raised:
            await builder(client).resolve(zone="East-1", cluster="aisvc-east-1")

        assert "2" in str(raised.value)


class TestCreating:
    async def test_it_creates_with_everything_resolved(self) -> None:
        client = FakeClient()
        planned = await builder(client).resolve(zone="East-1", cluster="aisvc-east-1")

        await builder(client).create(planned, dry_run=False)

        command, params = client.writes[0]
        assert command == "createKubernetesCluster"
        assert params["name"] == "aisvc-east-1"
        assert params["zoneid"] == "z-east"
        assert params["kubernetesversionid"] == "v-130"
        assert params["serviceofferingid"] == "o-small"

    async def test_a_dry_run_writes_nothing(self) -> None:
        client = FakeClient()
        planned = await builder(client).resolve(zone="East-1", cluster="aisvc-east-1")

        await builder(client).create(planned, dry_run=True)

        assert client.writes == []

    async def test_the_worker_count_is_carried_through(self) -> None:
        client = FakeClient()
        planned = await builder(client).resolve(zone="East-1", cluster="aisvc-east-1", size=3)

        await builder(client).create(planned, dry_run=False)

        assert client.writes[0][1]["size"] == 3

    async def test_it_refuses_to_create_one_that_already_exists(self) -> None:
        # The idempotence rule. A second apply must not build a second
        # cluster, and the planner is not the only thing that has to
        # know it.
        client = FakeClient(clusters=[{"name": "aisvc-east-1", "state": "Running"}])
        planned = await builder(client).resolve(zone="East-1", cluster="aisvc-east-1")

        await builder(client).create(planned, dry_run=False)

        assert client.writes == []

    async def test_a_named_network_is_passed_on(self) -> None:
        client = FakeClient()
        planned = await builder(client).resolve(
            zone="East-1", cluster="aisvc-east-1", network_id="net-1"
        )

        await builder(client).create(planned, dry_run=False)

        assert client.writes[0][1]["networkid"] == "net-1"

    async def test_no_network_leaves_cloudstack_to_make_one(self) -> None:
        # Omitted rather than empty: CloudStack builds one from
        # cloud.kubernetes.cluster.network.offering, and an empty
        # string would be a network id that resolves to nothing.
        client = FakeClient()
        planned = await builder(client).resolve(zone="East-1", cluster="aisvc-east-1")

        await builder(client).create(planned, dry_run=False)

        assert "networkid" not in client.writes[0][1]


class TestWhatThePlanShows:
    async def test_it_names_what_it_chose(self) -> None:
        # The offering is billable and the engine picked it, so an
        # operator sees it in the plan before confirming rather than
        # discovering it on an invoice.
        planned = await builder(FakeClient()).resolve(zone="East-1", cluster="aisvc-east-1")

        assert "1.30.1" in planned.detail
        assert "Small Instance" in planned.detail
