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
"""What CloudStack says about a zone's Kubernetes cluster (SPEC.md section 7).

`platform.yaml` names a `cks_cluster` per zone, and until now nothing
ever checked it. The name reaches cloud-init as a label and is
otherwise unused, so a typo is silent: GPU nodes are deployed, billed,
and fail to join a cluster nobody ever looked for.

**A failed lookup is never read as "no such cluster."** The two have
different remedies — one sends an operator to edit `platform.yaml`, the
other to the API — and conflating them is the same mistake
`aisvc/cloudstack/networks.py` was written to undo.

Clusters are matched on name alone. CloudStack scopes them by zone and
two zones could in principle carry the same cluster name; resolving the
zone first would cost a call per zone to guard against a case an
operator has to create deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "ClusterBuilder",
    "ClusterPlanned",
    "ClusterState",
    "ClusterUnbuildable",
    "KubeconfigUnavailable",
    "KubernetesClusters",
    "cluster_for_kubeconfig",
    "fetch_kubeconfig",
    "read_clusters",
    "write_kubeconfig",
]

#: CloudStack's list item key for a Kubernetes cluster.
ITEM_KEY = "kubernetescluster"


@dataclass(frozen=True, slots=True)
class ClusterState:
    """What CloudStack said about one zone's cluster, or why it could not."""

    zone: str
    cluster: str
    #: CloudStack's own word for the cluster's state, `""` when it
    #: listed the cluster without one, and None when it lists no cluster
    #: by that name.
    state: str | None = None
    #: Why the lookup failed. Set means `state` says nothing at all: a
    #: failed lookup must never be reported as a missing cluster.
    error: str = ""


class KubernetesClusters:
    """Reads CloudStack's own view of the CKS clusters it manages."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def listed(self) -> Sequence[dict[str, Any]]:
        """Every Kubernetes cluster CloudStack will admit to."""
        found: Sequence[dict[str, Any]] = await self.client.call_paged(
            "listKubernetesClusters", ITEM_KEY, listall="true"
        )
        return found

    async def state_of(self, name: str) -> str | None:
        """The state CloudStack reports for `name`, or None if it knows none.

        The state is passed through verbatim. An operator reads these
        words in the CloudStack UI, and translating an unfamiliar one
        would produce a message matching nothing they can look up.
        """
        return _state_in(await self.listed(), name)


def _state_in(listed: Iterable[dict[str, Any]], name: str) -> str | None:
    """Match exactly: CloudStack's own name filters are partial."""
    for cluster in listed:
        if str(cluster.get("name", "")) == name:
            return str(cluster.get("state", ""))
    return None


async def read_clusters(client: Any, zones: Sequence[tuple[str, str]]) -> list[ClusterState]:
    """One `ClusterState` per `(zone, cluster)`, from a single list call.

    A failure is recorded against every zone rather than raised.
    Preflight's contract is that every check runs even after one fails,
    so an unreachable CloudStack must not abort the whole report.
    """
    try:
        listed = await KubernetesClusters(client).listed()
    except Exception as failure:
        return [
            ClusterState(zone=zone, cluster=cluster, error=str(failure)) for zone, cluster in zones
        ]

    return [
        ClusterState(zone=zone, cluster=cluster, state=_state_in(listed, cluster))
        for zone, cluster in zones
    ]


class ClusterUnbuildable(Exception):
    """The cluster cannot be created, with the reason and the fix."""


#: CloudStack's word for a version that may be used for a new cluster.
ENABLED = "enabled"

#: What CKS asks of a node when a version does not say (admin guide).
FALLBACK_MIN_CPU = 2
FALLBACK_MIN_MEMORY_MB = 2048


def _semver(raw: str) -> tuple[int, ...]:
    """Sort key for a semantic version.

    Compared as numbers, not text: "1.9.0" sorts above "1.10.0" as a
    string, and picking 1.9 would quietly build clusters on an older
    Kubernetes than the operator had available.
    """
    parts: list[int] = []
    for piece in raw.split("-")[0].split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class ClusterPlanned:
    """Everything `createKubernetesCluster` needs, resolved."""

    zone: str
    zone_id: str
    cluster: str
    version: str
    version_id: str
    offering: str
    offering_id: str
    size: int = 1
    network_id: str = ""

    @property
    def detail(self) -> str:
        """What the plan line says.

        The offering is billable and the engine may have chosen it, so
        it is named before an operator confirms rather than discovered
        afterwards.
        """
        return f"Kubernetes {self.version}, {self.offering}, {self.size} worker(s) in {self.zone}"


class ClusterBuilder:
    """Resolves and creates a CKS cluster for a zone (ADR-0007).

    Everything `createKubernetesCluster` needs beyond a name and a zone
    is resolved from CloudStack. What cannot be resolved without
    choosing how much to spend — the service offering — is chosen by
    smallest-that-fits and named in the plan, so the choice is visible
    before it is made rather than after.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def resolve(
        self,
        *,
        zone: str,
        cluster: str,
        version: str = "",
        offering: str = "",
        size: int = 1,
        network_id: str = "",
    ) -> ClusterPlanned:
        """Work out what would be created, without creating it."""
        zone_id = await self._zone_id(zone)
        chosen = await self._version(zone_id, version)
        min_cpu = int(chosen.get("mincpunumber") or FALLBACK_MIN_CPU)
        min_memory = int(chosen.get("minmemory") or FALLBACK_MIN_MEMORY_MB)
        picked = await self._offering(offering, min_cpu=min_cpu, min_memory=min_memory)

        return ClusterPlanned(
            zone=zone,
            zone_id=zone_id,
            cluster=cluster,
            version=str(chosen.get("semanticversion", "")),
            version_id=str(chosen["id"]),
            offering=str(picked.get("name", "")),
            offering_id=str(picked["id"]),
            size=size,
            network_id=network_id,
        )

    async def create(self, planned: ClusterPlanned, *, dry_run: bool) -> str:
        """Create the cluster, unless CloudStack already has it.

        The existence check is here and not only in the planner: a plan
        is made before it is executed, and a cluster that appeared in
        between must not become a second one.
        """
        if dry_run:
            return ""

        existing = _state_in(await KubernetesClusters(self.client).listed(), planned.cluster)
        if existing is not None:
            return planned.cluster

        params: dict[str, Any] = {
            "name": planned.cluster,
            "zoneid": planned.zone_id,
            "kubernetesversionid": planned.version_id,
            "serviceofferingid": planned.offering_id,
            "size": planned.size,
        }
        # Omitted rather than empty: CloudStack builds a network from
        # cloud.kubernetes.cluster.network.offering when none is given,
        # and "" would be a network id resolving to nothing.
        if planned.network_id:
            params["networkid"] = planned.network_id

        await self.client.call_async("createKubernetesCluster", **params)
        return planned.cluster

    async def _zone_id(self, zone: str) -> str:
        zones: Sequence[dict[str, Any]] = await self.client.call_paged(
            "listZones", "zone", listall="true"
        )
        for found in zones:
            if str(found.get("name", "")) == zone:
                return str(found["id"])
        raise ClusterUnbuildable(f"CloudStack has no zone named {zone!r}")

    async def _version(self, zone_id: str, wanted: str) -> dict[str, Any]:
        """The version to build on, pinned or newest enabled."""
        listed: Sequence[dict[str, Any]] = await self.client.call_paged(
            "listKubernetesSupportedVersions", "kubernetessupportedversion", zoneid=zone_id
        )

        if wanted:
            for version in listed:
                if str(version.get("semanticversion", "")) != wanted:
                    continue
                if str(version.get("state", "")).lower() != ENABLED:
                    raise ClusterUnbuildable(
                        f"Kubernetes version {wanted} is disabled in this zone, so "
                        f"CloudStack will not build a cluster on it. Enable it with "
                        f"updateKubernetesSupportedVersion, or pin another version."
                    )
                return dict(version)
            raise ClusterUnbuildable(
                f"CloudStack has no Kubernetes version {wanted} in this zone. "
                f"Register it with addKubernetesSupportedVersion, or leave the "
                f"version unset to take the newest enabled one."
            )

        usable = [v for v in listed if str(v.get("state", "")).lower() == ENABLED]
        if not usable:
            raise ClusterUnbuildable(
                "no enabled Kubernetes version in this zone, so CloudStack cannot "
                "build a cluster. Register one with addKubernetesSupportedVersion — "
                "it needs an ISO URL, a checksum and the node minimums — or point "
                "the zone at a cluster that already exists."
            )
        newest = max(usable, key=lambda v: _semver(str(v.get("semanticversion", ""))))
        return dict(newest)

    async def _offering(self, wanted: str, *, min_cpu: int, min_memory: int) -> dict[str, Any]:
        """The compute offering for the cluster's own nodes."""
        listed: Sequence[dict[str, Any]] = await self.client.call_paged(
            "listServiceOfferings", "serviceoffering", listall="true"
        )

        def fits(offering: dict[str, Any]) -> bool:
            return (
                int(offering.get("cpunumber") or 0) >= min_cpu
                and int(offering.get("memory") or 0) >= min_memory
            )

        if wanted:
            for offering in listed:
                if str(offering.get("name", "")) != wanted:
                    continue
                if not fits(offering):
                    raise ClusterUnbuildable(
                        f"service offering {wanted!r} is below what this Kubernetes "
                        f"version needs ({min_cpu} cores, {min_memory} MB), so the "
                        f"cluster would not come up. Name a larger one."
                    )
                return dict(offering)
            raise ClusterUnbuildable(f"CloudStack has no service offering named {wanted!r}")

        usable = [o for o in listed if fits(o)]
        if not usable:
            raise ClusterUnbuildable(
                f"no service offering meets what this Kubernetes version needs "
                f"({min_cpu} cores, {min_memory} MB). Create one, or name an "
                f"existing offering for the cluster."
            )
        # Smallest that fits: the engine is choosing something billable,
        # and the plan names it before an operator confirms.
        smallest = min(
            usable,
            key=lambda o: (int(o.get("cpunumber") or 0), int(o.get("memory") or 0)),
        )
        return dict(smallest)


class KubeconfigUnavailable(Exception):
    """The cluster's kubeconfig could not be had, with the reason.

    Distinct from `ClusterUnbuildable`: that is a cluster that cannot
    be created, this is one that exists but cannot be talked to. They
    happen at different times and have different fixes.
    """


#: Where a fetched kubeconfig is kept. One file, replaced each run.
KUBECONFIG_NAME = "kubeconfig"


def cluster_for_kubeconfig(zones: Sequence[tuple[str, str]]) -> str | None:
    """Which cluster's kubeconfig to fetch, given the CKS zones.

    The engine holds one Kubernetes connection, so one cluster has to
    be chosen. Two different ones is ambiguous, and picking either
    silently would deploy every model into whichever sorted first —
    so it asks for `KUBECONFIG` instead of guessing.
    """
    named = {cluster for _, cluster in zones if cluster}
    if not named:
        return None
    if len(named) > 1:
        raise KubeconfigUnavailable(
            f"zones name {len(named)} different CKS clusters "
            f"({', '.join(sorted(named))}) and the engine holds one Kubernetes "
            f"connection, so it cannot choose. Set KUBECONFIG to the cluster "
            f"this engine serves."
        )
    return named.pop()


async def fetch_kubeconfig(client: Any, cluster: str) -> str:
    """The kubeconfig CloudStack holds for `cluster`.

    **The contents are cluster-admin credentials.** No part of them
    appears in an error here, and nothing logs the return value.
    """
    listed = await KubernetesClusters(client).listed()
    cluster_id = ""
    for found in listed:
        if str(found.get("name", "")) == cluster:
            cluster_id = str(found.get("id", ""))
            break

    if not cluster_id:
        raise KubeconfigUnavailable(
            f"CloudStack lists no Kubernetes cluster named {cluster!r}, so there "
            f"is no kubeconfig to fetch. Check the zone's cluster name, or set "
            f"KUBECONFIG."
        )

    try:
        response = await client.call("getKubernetesClusterConfig", id=cluster_id)
    except Exception as failure:
        raise KubeconfigUnavailable(
            f"could not read the kubeconfig for {cluster!r}: {failure}"
        ) from failure

    data = str(response.get("configdata", "") or "")
    if not data:
        # An empty file would turn this clear failure into an opaque
        # Kubernetes client error much later.
        raise KubeconfigUnavailable(
            f"CloudStack returned no kubeconfig for {cluster!r}. Check the "
            f"cluster is Running, or set KUBECONFIG."
        )
    return data


def write_kubeconfig(data: str, *, directory: str) -> str:
    """Write `data` where the Kubernetes client can load it.

    The file holds cluster-admin credentials and the path is
    predictable, so neither the directory nor the file is taken as
    found. A local user can create `<tmp>/aisvc-<uid>` and a symlink
    inside it well in advance — there is no race to win — and `chmod
    0700` narrows a mode without ever changing an owner, so an
    attacker-owned directory stays attacker-readable however it is
    chmodded.

    Two checks and one flag close that: the directory must be owned by
    this user and not writable by anyone else, and the file is opened
    `O_NOFOLLOW` so a symlink is refused rather than followed.
    """
    import os
    import stat

    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as failure:
        raise KubeconfigUnavailable(
            f"could not create {directory} for the kubeconfig: {failure}"
        ) from failure

    _check_private_directory(directory)

    path = os.path.join(directory, KUBECONFIG_NAME)
    try:
        handle = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as failure:
        # ELOOP here is the attack, not a mishap: something that is not
        # a regular file is sitting on the path we are about to write.
        raise KubeconfigUnavailable(
            f"refusing to write the kubeconfig to {path}: {failure}. Remove "
            f"whatever is at that path, or set AISVC_KUBECONFIG_DIR to a "
            f"directory only this user can write."
        ) from failure

    with os.fdopen(handle, "w") as out:
        out.write(data)
    # An existing file keeps its old mode, so it is set either way.
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def _check_private_directory(directory: str) -> None:
    """Refuse a directory this user does not own, or others can write.

    Ownership is the half `chmod` cannot fix: narrowing the mode of a
    directory somebody else owns leaves them owning it, free to widen
    it again and to read whatever lands inside.
    """
    import os
    import stat

    try:
        found = os.lstat(directory)
    except OSError as failure:
        raise KubeconfigUnavailable(f"could not read {directory}: {failure}") from failure

    if not stat.S_ISDIR(found.st_mode):
        raise KubeconfigUnavailable(f"{directory} is not a directory")

    if found.st_uid != os.geteuid():
        raise KubeconfigUnavailable(
            f"{directory} is not owned by this user, so a kubeconfig written "
            f"there would be readable by whoever does own it. Set "
            f"AISVC_KUBECONFIG_DIR to a directory this user owns."
        )

    if found.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise KubeconfigUnavailable(
            f"{directory} is writable by other users, so a kubeconfig written "
            f"there could be replaced or redirected. Narrow it to 0700, or set "
            f"AISVC_KUBECONFIG_DIR elsewhere."
        )
