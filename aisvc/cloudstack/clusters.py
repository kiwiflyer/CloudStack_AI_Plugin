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

__all__ = ["ClusterState", "KubernetesClusters", "read_clusters"]

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
