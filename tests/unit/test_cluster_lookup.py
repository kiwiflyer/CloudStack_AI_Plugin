"""What CloudStack says about the Kubernetes cluster a zone names.

`platform.yaml` names a `cks_cluster` per zone and nothing ever checked
it. The name reaches cloud-init as a label and is otherwise unused, so
a typo is silent: nodes are deployed, billed, and fail to join a
cluster nobody looked for.

This asks CloudStack. As with `TenantNetworks`, **a failed lookup is
never read as "no such cluster"** — the two have different remedies,
and conflating them sends an operator to edit a spec file when
CloudStack was simply unreachable.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.cloudstack.clusters import KubernetesClusters

CLUSTERS = [
    {"id": "k-1", "name": "aisvc-east-1", "state": "Running", "zonename": "east"},
    {"id": "k-2", "name": "aisvc-west-1", "state": "Stopped", "zonename": "west"},
]


class FakeClient:
    """Answers listKubernetesClusters, recording what was asked."""

    def __init__(self, clusters: list[dict[str, Any]] | None = None, fail: str = "") -> None:
        self.clusters = CLUSTERS if clusters is None else clusters
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_paged(self, command: str, item_key: str, **params: Any) -> list[Any]:
        self.calls.append((command, params))
        if self.fail:
            raise RuntimeError(self.fail)
        assert command == "listKubernetesClusters"
        assert item_key == "kubernetescluster"
        return list(self.clusters)


class TestAskingCloudStack:
    async def test_it_reports_the_state_of_a_known_cluster(self) -> None:
        state = await KubernetesClusters(FakeClient()).state_of("aisvc-east-1")

        assert state == "Running"

    async def test_it_reports_a_state_that_is_not_running_verbatim(self) -> None:
        # Not interpreted: CloudStack's own word is what an operator
        # will see in the UI, and guessing at an unfamiliar state would
        # produce a message that matches nothing they can look up.
        state = await KubernetesClusters(FakeClient()).state_of("aisvc-west-1")

        assert state == "Stopped"

    async def test_an_unknown_cluster_is_none(self) -> None:
        state = await KubernetesClusters(FakeClient()).state_of("aisvc-nowhere")

        assert state is None

    async def test_the_match_is_exact(self) -> None:
        # CloudStack's own name filters are partial on some list APIs,
        # so a prefix must not be taken for the cluster itself.
        state = await KubernetesClusters(FakeClient()).state_of("aisvc-east")

        assert state is None

    async def test_a_cluster_with_no_state_reads_as_unknown_not_absent(self) -> None:
        listed = [{"id": "k-3", "name": "aisvc-east-1"}]
        state = await KubernetesClusters(FakeClient(clusters=listed)).state_of("aisvc-east-1")

        assert state == ""


class TestWhenCloudStackIsUnhappy:
    async def test_a_failure_is_not_read_as_a_missing_cluster(self) -> None:
        # The whole point. "No such cluster" sends an operator to edit
        # platform.yaml; "could not ask" sends them to the API.
        with pytest.raises(RuntimeError):
            await KubernetesClusters(FakeClient(fail="boom")).state_of("aisvc-east-1")

    async def test_an_empty_list_is_still_a_missing_cluster(self) -> None:
        state = await KubernetesClusters(FakeClient(clusters=[])).state_of("anything")

        assert state is None


class TestReadingEveryZone:
    async def test_it_reports_one_state_per_zone(self) -> None:
        from aisvc.cloudstack.clusters import read_clusters

        states = await read_clusters(
            FakeClient(), [("east", "aisvc-east-1"), ("west", "aisvc-west-1")]
        )

        assert [(s.zone, s.cluster, s.state) for s in states] == [
            ("east", "aisvc-east-1", "Running"),
            ("west", "aisvc-west-1", "Stopped"),
        ]

    async def test_a_failure_is_recorded_against_every_zone_not_raised(self) -> None:
        # Preflight's contract is that every check runs even after one
        # fails, so this reports the failure rather than aborting the
        # whole report over an unreachable CloudStack.
        from aisvc.cloudstack.clusters import read_clusters

        states = await read_clusters(
            FakeClient(fail="connection refused"), [("east", "aisvc-east-1")]
        )

        assert states[0].state is None
        assert "connection refused" in states[0].error

    async def test_it_asks_cloudstack_once_for_every_zone(self) -> None:
        from aisvc.cloudstack.clusters import read_clusters

        client = FakeClient()
        await read_clusters(client, [("east", "aisvc-east-1"), ("west", "aisvc-west-1")])

        assert len([c for c, _ in client.calls if c == "listKubernetesClusters"]) == 1
