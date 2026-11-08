"""Creating and removing GPU nodes (SPEC.md section 4).

`deployVirtualMachine` with offering, template, anti-affinity and
cloud-init; `scaleKubernetesCluster`; `destroyVirtualMachine`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import yaml

from aisvc.cloudstack import CloudStackClient
from aisvc.provision import (
    ClusterJoin,
    GpuNode,
    NodeProvisioner,
    ProvisionError,
)
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.conftest import CREDENTIALS
from tests.unit.test_spec import LICENSES, MINIMAL

JOIN = ClusterJoin(
    endpoint="10.0.0.10:6443",
    token="abcdef.0123456789abcdef",
    ca_cert_hash="sha256:deadbeef",
)


def spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


class FakeCloudStack:
    """Scripts CloudStack responses and records every call made."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.vms: list[dict[str, Any]] = []
        self.affinity_groups: list[dict[str, Any]] = []
        self.zones = [{"id": "z-1", "name": "East-1"}]
        self.templates = [{"id": "tpl-1", "name": "gpu-golden-2026.11"}]
        self.offerings = [{"id": "so-1", "name": "GPU-H100-8x"}]
        self.fail_on: str | None = None
        self._next_vm = 1

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def handle(request: httpx.Request) -> httpx.Response:
            params = dict(httpx.QueryParams(request.url.query.decode()))
            command = params.pop("command")
            params.pop("apikey", None)
            params.pop("signature", None)
            params.pop("response", None)
            self.calls.append((command, params))

            if self.fail_on == command:
                return httpx.Response(
                    530,
                    json={
                        f"{command.lower()}response": {
                            "errorcode": 530,
                            "errortext": "scripted failure",
                        }
                    },
                )
            return httpx.Response(
                200, json={f"{command.lower()}response": self._body(command, params)}
            )

        return handle

    def _body(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        match command:
            case "listZones":
                return {"count": len(self.zones), "zone": self.zones}
            case "listTemplates":
                return {"count": len(self.templates), "template": self.templates}
            case "listServiceOfferings":
                return {"count": len(self.offerings), "serviceoffering": self.offerings}
            case "listAffinityGroups":
                return {
                    "count": len(self.affinity_groups),
                    "affinitygroup": self.affinity_groups,
                }
            case "createAffinityGroup":
                group = {"id": f"ag-{len(self.affinity_groups) + 1}", "name": params["name"]}
                self.affinity_groups.append(group)
                return {"affinitygroup": group}
            case "listGpuDevices":
                return {"count": 0, "gpudevice": []}
            case "listVirtualMachines":
                return {"count": len(self.vms), "virtualmachine": self.vms}
            case "deployVirtualMachine":
                vm = {
                    "id": f"vm-{self._next_vm}",
                    "name": params["name"],
                    "displayname": params["name"],
                    "serviceofferingname": "GPU-H100-8x",
                    "state": "Running",
                    "zoneid": "z-1",
                }
                self._next_vm += 1
                self.vms.append(vm)
                return {"jobid": f"job-{vm['id']}"}
            case "destroyVirtualMachine":
                self.vms = [v for v in self.vms if v["id"] != params.get("id")]
                return {"jobid": "job-destroy"}
            case "scaleKubernetesCluster":
                return {"jobid": "job-scale"}
            case "queryAsyncJobResult":
                return {"jobstatus": 1, "jobresult": {"ok": True}}
        raise AssertionError(f"unscripted command {command}")

    def client(self) -> CloudStackClient:
        return CloudStackClient(
            CREDENTIALS,
            transport=httpx.MockTransport(self.handler()),
            poll_interval=0.0,
            backoff_base=0.0,
        )

    def commands(self) -> list[str]:
        return [c for c, _ in self.calls]

    def params_for(self, command: str) -> list[dict[str, Any]]:
        return [p for c, p in self.calls if c == command]

    def add_existing_node(self, name: str) -> None:
        self.vms.append(
            {
                "id": f"vm-existing-{len(self.vms) + 1}",
                "name": name,
                "displayname": name,
                "serviceofferingname": "GPU-H100-8x",
                "state": "Running",
                "zoneid": "z-1",
            }
        )


@pytest.fixture
def fake() -> FakeCloudStack:
    return FakeCloudStack()


def provisioner(fake: FakeCloudStack, client: CloudStackClient) -> NodeProvisioner:
    return NodeProvisioner(client, spec(), join=JOIN)


class TestNodeNaming:
    def test_node_names_are_derived_from_the_pool_and_an_ordinal(self) -> None:
        assert NodeProvisioner.node_name("h100-pool", 1) == "aisvc-h100-pool-001"
        assert NodeProvisioner.node_name("h100-pool", 42) == "aisvc-h100-pool-042"

    def test_the_ordinal_can_be_read_back(self) -> None:
        assert NodeProvisioner.ordinal_of("h100-pool", "aisvc-h100-pool-007") == 7

    def test_a_foreign_name_has_no_ordinal(self) -> None:
        assert NodeProvisioner.ordinal_of("h100-pool", "someone-elses-vm") is None

    def test_another_pools_node_has_no_ordinal_here(self) -> None:
        assert NodeProvisioner.ordinal_of("h100-pool", "aisvc-l40s-pool-001") is None


class TestListNodes:
    async def test_only_this_pools_nodes_are_listed(self, fake: FakeCloudStack) -> None:
        fake.add_existing_node("aisvc-h100-pool-001")
        fake.add_existing_node("aisvc-l40s-pool-001")
        fake.add_existing_node("someone-elses-vm")

        async with fake.client() as client:
            nodes = await provisioner(fake, client).list_nodes("h100-pool")

        assert [n.name for n in nodes] == ["aisvc-h100-pool-001"]

    async def test_nodes_come_back_in_ordinal_order(self, fake: FakeCloudStack) -> None:
        for name in ("aisvc-h100-pool-003", "aisvc-h100-pool-001", "aisvc-h100-pool-002"):
            fake.add_existing_node(name)

        async with fake.client() as client:
            nodes = await provisioner(fake, client).list_nodes("h100-pool")

        assert [n.ordinal for n in nodes] == [1, 2, 3]


class TestCreateNodes:
    async def test_creating_a_node_deploys_a_vm(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            created = await provisioner(fake, client).create_nodes("h100-pool", 1)

        assert len(created) == 1
        assert "deployVirtualMachine" in fake.commands()

    async def test_the_deploy_carries_offering_template_and_zone(
        self, fake: FakeCloudStack
    ) -> None:
        async with fake.client() as client:
            await provisioner(fake, client).create_nodes("h100-pool", 1)

        deploy = fake.params_for("deployVirtualMachine")[0]
        assert deploy["serviceofferingid"] == "so-1"
        assert deploy["templateid"] == "tpl-1"
        assert deploy["zoneid"] == "z-1"

    async def test_the_deploy_carries_an_anti_affinity_group(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            await provisioner(fake, client).create_nodes("h100-pool", 1)

        created = fake.params_for("createAffinityGroup")[0]
        assert created["type"] == "host anti-affinity"
        assert "h100-pool" in created["name"]
        assert fake.params_for("deployVirtualMachine")[0]["affinitygroupids"] == "ag-1"

    async def test_an_existing_affinity_group_is_reused(self, fake: FakeCloudStack) -> None:
        fake.affinity_groups.append({"id": "ag-existing", "name": "aisvc-h100-pool-anti-affinity"})

        async with fake.client() as client:
            await provisioner(fake, client).create_nodes("h100-pool", 1)

        assert "createAffinityGroup" not in fake.commands()
        assert fake.params_for("deployVirtualMachine")[0]["affinitygroupids"] == "ag-existing"

    async def test_userdata_is_attached_and_encoded(self, fake: FakeCloudStack) -> None:
        import base64

        async with fake.client() as client:
            await provisioner(fake, client).create_nodes("h100-pool", 1)

        encoded = fake.params_for("deployVirtualMachine")[0]["userdata"]
        decoded = base64.b64decode(encoded).decode()
        assert yaml.safe_load(decoded)["hostname"] == "aisvc-h100-pool-001"

    async def test_new_nodes_take_the_lowest_free_ordinals(self, fake: FakeCloudStack) -> None:
        fake.add_existing_node("aisvc-h100-pool-001")
        fake.add_existing_node("aisvc-h100-pool-003")

        async with fake.client() as client:
            created = await provisioner(fake, client).create_nodes("h100-pool", 2)

        assert [n.name for n in created] == [
            "aisvc-h100-pool-002",
            "aisvc-h100-pool-004",
        ]

    async def test_creating_several_nodes_deploys_each_one(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            created = await provisioner(fake, client).create_nodes("h100-pool", 3)

        assert len(created) == 3
        assert fake.commands().count("deployVirtualMachine") == 3

    async def test_creating_zero_nodes_calls_nothing(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            created = await provisioner(fake, client).create_nodes("h100-pool", 0)

        assert created == []
        assert "deployVirtualMachine" not in fake.commands()

    async def test_an_unknown_pool_is_an_error(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            with pytest.raises(ProvisionError, match="ghost-pool"):
                await provisioner(fake, client).create_nodes("ghost-pool", 1)

    async def test_a_missing_template_is_a_clear_error(self, fake: FakeCloudStack) -> None:
        fake.templates = []

        async with fake.client() as client:
            with pytest.raises(ProvisionError, match=re.escape("gpu-golden-2026.11")):
                await provisioner(fake, client).create_nodes("h100-pool", 1)

    async def test_a_missing_offering_is_a_clear_error(self, fake: FakeCloudStack) -> None:
        fake.offerings = []

        async with fake.client() as client:
            with pytest.raises(ProvisionError, match="GPU-H100-8x"):
                await provisioner(fake, client).create_nodes("h100-pool", 1)


class TestDryRun:
    async def test_dry_run_makes_no_api_writes(self, fake: FakeCloudStack) -> None:
        """SPEC.md section 12: --dry-run is safe."""
        async with fake.client() as client:
            created = await provisioner(fake, client).create_nodes("h100-pool", 2, dry_run=True)

        assert [n.name for n in created] == [
            "aisvc-h100-pool-001",
            "aisvc-h100-pool-002",
        ]
        writes = {"deployVirtualMachine", "createAffinityGroup", "destroyVirtualMachine"}
        assert writes.isdisjoint(fake.commands())

    async def test_dry_run_destroy_makes_no_api_writes(self, fake: FakeCloudStack) -> None:
        fake.add_existing_node("aisvc-h100-pool-001")

        async with fake.client() as client:
            removed = await provisioner(fake, client).destroy_nodes("h100-pool", 1, dry_run=True)

        assert [n.name for n in removed] == ["aisvc-h100-pool-001"]
        assert "destroyVirtualMachine" not in fake.commands()


class TestDestroyNodes:
    async def test_the_highest_ordinals_are_removed_first(self, fake: FakeCloudStack) -> None:
        for name in ("aisvc-h100-pool-001", "aisvc-h100-pool-002", "aisvc-h100-pool-003"):
            fake.add_existing_node(name)

        async with fake.client() as client:
            removed = await provisioner(fake, client).destroy_nodes("h100-pool", 2)

        assert [n.name for n in removed] == [
            "aisvc-h100-pool-003",
            "aisvc-h100-pool-002",
        ]

    async def test_volumes_are_expunged_with_the_vm(self, fake: FakeCloudStack) -> None:
        fake.add_existing_node("aisvc-h100-pool-001")

        async with fake.client() as client:
            await provisioner(fake, client).destroy_nodes("h100-pool", 1)

        destroy = fake.params_for("destroyVirtualMachine")[0]
        assert destroy["expunge"] == "true"

    async def test_removing_more_than_exist_removes_what_there_is(
        self, fake: FakeCloudStack
    ) -> None:
        fake.add_existing_node("aisvc-h100-pool-001")

        async with fake.client() as client:
            removed = await provisioner(fake, client).destroy_nodes("h100-pool", 5)

        assert len(removed) == 1


class TestScaleCluster:
    async def test_scaling_passes_the_cluster_and_size(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            await provisioner(fake, client).scale_cluster("zone-east-1", size=4)

        params = fake.params_for("scaleKubernetesCluster")[0]
        assert params["size"] == "4"

    async def test_dry_run_does_not_scale(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            await provisioner(fake, client).scale_cluster("zone-east-1", size=4, dry_run=True)

        assert "scaleKubernetesCluster" not in fake.commands()

    async def test_an_unknown_zone_is_an_error(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            with pytest.raises(ProvisionError, match="ghost-zone"):
                await provisioner(fake, client).scale_cluster("ghost-zone", size=1)


class TestFailures:
    async def test_a_failed_deploy_surfaces_as_a_provision_error(
        self, fake: FakeCloudStack
    ) -> None:
        fake.fail_on = "deployVirtualMachine"

        async with fake.client() as client:
            with pytest.raises(ProvisionError, match="aisvc-h100-pool-001"):
                await provisioner(fake, client).create_nodes("h100-pool", 1)

    async def test_nodes_created_before_a_failure_are_reported(self, fake: FakeCloudStack) -> None:
        async with fake.client() as client:
            prov = provisioner(fake, client)
            await prov.create_nodes("h100-pool", 1)
            fake.fail_on = "deployVirtualMachine"
            with pytest.raises(ProvisionError) as exc:
                await prov.create_nodes("h100-pool", 1)

        assert exc.value.created == []


class TestGpuNode:
    def test_a_node_knows_its_pool_and_ordinal(self) -> None:
        node = GpuNode(id="vm-1", name="aisvc-h100-pool-004", pool="h100-pool", ordinal=4)
        assert node.pool == "h100-pool"
        assert node.ordinal == 4
