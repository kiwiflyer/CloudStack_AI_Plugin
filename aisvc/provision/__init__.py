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
"""Creating and removing GPU nodes (SPEC.md section 4).

Nodes are named `aisvc-<pool>-<ordinal>`, which is what makes provisioning
idempotent: the set of nodes a pool should have is derived from the names
already present, so re-running `apply` creates nothing new.

Passthrough GPUs cannot live-migrate, so nodes are drained and replaced
rather than moved (SPEC.md section 1); nothing here migrates anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aisvc.cloudstack import CloudStackClient, CloudStackError
from aisvc.provision.cloudinit import (
    GpuNodeUserdata,
    encode_userdata,
    render_gpu_node_userdata,
)

if TYPE_CHECKING:
    from aisvc.spec import NodePool, PlatformSpec

__all__ = [
    "ClusterJoin",
    "GpuNode",
    "NodeProvisioner",
    "ProvisionError",
]

NODE_PREFIX = "aisvc"
_NAME_RE = re.compile(r"^aisvc-(?P<pool>.+)-(?P<ordinal>\d{3,})$")


class ProvisionError(Exception):
    """A node could not be created or removed."""

    def __init__(self, message: str, *, created: list[GpuNode] | None = None) -> None:
        super().__init__(message)
        #: Nodes created before the failure, so the caller can report them.
        self.created = created or []


@dataclass(frozen=True, slots=True)
class ClusterJoin:
    """How a new node joins the CKS cluster. Secrets stay out of repr()."""

    endpoint: str = field(repr=False)
    token: str = field(repr=False)
    ca_cert_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class GpuNode:
    """One GPU node VM belonging to a pool."""

    id: str
    name: str
    pool: str
    ordinal: int
    state: str = "Unknown"


class NodeProvisioner:
    """Creates and removes the GPU node VMs a pool declares."""

    def __init__(
        self,
        client: CloudStackClient,
        spec: PlatformSpec,
        *,
        join: ClusterJoin,
        weight_cache_device: str | None = "/dev/nvme1n1",
    ) -> None:
        self.client = client
        self.spec = spec
        self.join = join
        self.weight_cache_device = weight_cache_device

    # -- naming -----------------------------------------------------------

    @staticmethod
    def node_name(pool: str, ordinal: int) -> str:
        return f"{NODE_PREFIX}-{pool}-{ordinal:03d}"

    @staticmethod
    def ordinal_of(pool: str, name: str) -> int | None:
        """The ordinal of `name` if it belongs to `pool`, else None."""
        match = _NAME_RE.match(name)
        if match is None or match.group("pool") != pool:
            return None
        return int(match.group("ordinal"))

    # -- lookups ----------------------------------------------------------

    def _pool(self, pool_name: str) -> NodePool:
        for pool in self.spec.document.node_pools:
            if pool.name == pool_name:
                return pool
        raise ProvisionError(f"no node pool named {pool_name!r} in the spec")

    def _zone(self, zone_name: str) -> Any:
        for zone in self.spec.document.zones:
            if zone.name == zone_name:
                return zone
        raise ProvisionError(f"no zone named {zone_name!r} in the spec")

    async def _zone_id(self, cloudstack_zone: str) -> str:
        zones = await self.client.call_paged("listZones", "zone")
        for zone in zones:
            if zone.get("name") == cloudstack_zone:
                return str(zone["id"])
        raise ProvisionError(f"CloudStack has no zone named {cloudstack_zone!r}")

    async def _template_id(self, template: str, zone_id: str) -> str:
        templates = await self.client.call_paged(
            "listTemplates", "template", templatefilter="all", zoneid=zone_id
        )
        for candidate in templates:
            if candidate.get("name") == template:
                return str(candidate["id"])
        raise ProvisionError(f"no template named {template!r} in zone {zone_id}")

    async def _offering_id(self, offering: str) -> str:
        offerings = await self.client.call_paged("listServiceOfferings", "serviceoffering")
        for candidate in offerings:
            if candidate.get("name") == offering:
                return str(candidate["id"])
        raise ProvisionError(f"no service offering named {offering!r}")

    async def _affinity_group_id(self, pool_name: str, *, dry_run: bool) -> str:
        """The pool's host anti-affinity group, created if it is missing."""
        name = f"{NODE_PREFIX}-{pool_name}-anti-affinity"
        groups = await self.client.call_paged("listAffinityGroups", "affinitygroup")
        for group in groups:
            if group.get("name") == name:
                return str(group["id"])

        if dry_run:
            return f"<would create {name}>"

        body = await self.client.call("createAffinityGroup", name=name, type="host anti-affinity")
        group = body.get("affinitygroup") or {}
        if "id" not in group:
            raise ProvisionError(f"createAffinityGroup returned no id for {name!r}")
        return str(group["id"])

    # -- reading state ----------------------------------------------------

    async def list_nodes(self, pool_name: str) -> list[GpuNode]:
        """Every GPU node VM belonging to `pool_name`, in ordinal order."""
        vms = await self.client.call_paged("listVirtualMachines", "virtualmachine", listall=True)
        nodes: list[GpuNode] = []
        for vm in vms:
            name = str(vm.get("name", ""))
            ordinal = self.ordinal_of(pool_name, name)
            if ordinal is None:
                continue
            nodes.append(
                GpuNode(
                    id=str(vm.get("id", "")),
                    name=name,
                    pool=pool_name,
                    ordinal=ordinal,
                    state=str(vm.get("state", "Unknown")),
                )
            )
        return sorted(nodes, key=lambda n: n.ordinal)

    @staticmethod
    def _free_ordinals(taken: set[int], count: int) -> list[int]:
        """The `count` lowest ordinals not already in use."""
        free: list[int] = []
        candidate = 1
        while len(free) < count:
            if candidate not in taken:
                free.append(candidate)
            candidate += 1
        return free

    # -- writing ----------------------------------------------------------

    async def create_nodes(
        self, pool_name: str, count: int, *, dry_run: bool = False
    ) -> list[GpuNode]:
        """Deploy `count` new GPU nodes into `pool_name`."""
        if count <= 0:
            return []

        pool = self._pool(pool_name)
        zone = self._zone(pool.zone)
        gpu_class = self.spec.gpu_class(pool.gpu_class)

        existing = await self.list_nodes(pool_name)
        ordinals = self._free_ordinals({n.ordinal for n in existing}, count)

        zone_id = await self._zone_id(zone.cloudstack_zone)
        template_id = await self._template_id(pool.template, zone_id)
        offering_id = await self._offering_id(gpu_class.service_offering)
        affinity_id = await self._affinity_group_id(pool_name, dry_run=dry_run)

        created: list[GpuNode] = []
        for ordinal in ordinals:
            name = self.node_name(pool_name, ordinal)

            if dry_run:
                created.append(GpuNode(id="<dry-run>", name=name, pool=pool_name, ordinal=ordinal))
                continue

            userdata = GpuNodeUserdata(
                hostname=name,
                pool=pool_name,
                gpu_class=gpu_class.name,
                zone=zone.name,
                cks_cluster=zone.cks_cluster,
                join_endpoint=self.join.endpoint,
                join_token=self.join.token,
                ca_cert_hash=self.join.ca_cert_hash,
                weight_cache_device=self.weight_cache_device,
            )
            try:
                await self.client.call_async(
                    "deployVirtualMachine",
                    name=name,
                    displayname=name,
                    zoneid=zone_id,
                    templateid=template_id,
                    serviceofferingid=offering_id,
                    affinitygroupids=affinity_id,
                    userdata=encode_userdata(render_gpu_node_userdata(userdata)),
                    startvm=True,
                )
            except CloudStackError as exc:
                raise ProvisionError(f"could not deploy {name}: {exc}", created=created) from exc

            created.append(GpuNode(id="", name=name, pool=pool_name, ordinal=ordinal))

        return created

    async def destroy_nodes(
        self, pool_name: str, count: int, *, dry_run: bool = False
    ) -> list[GpuNode]:
        """Remove `count` nodes, highest ordinal first."""
        if count <= 0:
            return []

        self._pool(pool_name)  # reject unknown pools before touching anything
        existing = await self.list_nodes(pool_name)
        doomed = sorted(existing, key=lambda n: n.ordinal, reverse=True)[:count]

        if dry_run:
            return doomed

        removed: list[GpuNode] = []
        for node in doomed:
            try:
                await self.client.call_async("destroyVirtualMachine", id=node.id, expunge=True)
            except CloudStackError as exc:
                raise ProvisionError(
                    f"could not destroy {node.name}: {exc}", created=removed
                ) from exc
            removed.append(node)
        return removed

    async def scale_cluster(self, zone_name: str, *, size: int, dry_run: bool = False) -> None:
        """Scale the zone's CKS cluster to `size` worker nodes."""
        zone = self._zone(zone_name)
        if dry_run:
            return
        await self.client.call_async("scaleKubernetesCluster", name=zone.cks_cluster, size=size)
