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
"""Which networks a tenant owns (SPEC.md section 8).

A private endpoint puts a proxy VM on a tenant's own network, and
`validate_exposure` refuses any network the caller does not own. That
check needs a list of the caller's networks, and in production there
was none: the API read `networks_for` off `Services`, nothing ever set
it, and so every attempt to attach an endpoint to a VPC tier was
refused as "not available to this account".

CloudStack is the authority on who owns a network, so this asks it
rather than keeping a second copy of the answer. Two calls: the
caller's domain path resolves to a domain id, then networks are listed
scoped to that account and domain.

**A failure is never read as "owns nothing."** That would refuse the
attachment with a message about ownership, sending an operator to look
at permissions when CloudStack was simply unreachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["TenantNetworks"]


def _normalise(path: str) -> str:
    """Domain paths differ by a trailing slash between callers."""
    return path.rstrip("/")


class TenantNetworks:
    """Looks up the networks an account may attach a proxy to."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def for_account(self, account: str, domain_path: str) -> list[str]:
        """Every network id this account owns in this domain.

        An unknown domain yields nothing rather than an error: the
        attachment is refused either way, and distinguishing "no such
        domain" would confirm which domains exist to a caller who
        cannot see them.
        """
        domain_id = await self._domain_id(domain_path)
        if domain_id is None:
            return []

        networks: Sequence[dict[str, Any]] = await self.client.call_paged(
            "listNetworks",
            "network",
            account=account,
            domainid=domain_id,
            listall="true",
        )
        return [str(network["id"]) for network in networks if network.get("id")]

    async def _domain_id(self, domain_path: str) -> str | None:
        """The id of the domain at this path, or None.

        Matched on `path` rather than `name`: two domains under
        different parents may share a name, and picking the wrong one
        would list another tenant's networks.
        """
        wanted = _normalise(domain_path)
        domains: Sequence[dict[str, Any]] = await self.client.call_paged(
            "listDomains", "domain", listall="true"
        )
        for domain in domains:
            if _normalise(str(domain.get("path", ""))) == wanted:
                return str(domain["id"])
        return None
