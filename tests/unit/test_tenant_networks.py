"""Which VPC tiers a tenant may attach a private endpoint to.

`validate_exposure` refuses a `network_id` that is not in the caller's
owned networks, and in production that list was never supplied: the API
read `networks_for` off `Services`, nothing ever set it, and every
attempt to attach an endpoint to a VPC tier was refused as "not
available to this account".

CloudStack is the authority on who owns a network, so this asks it.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.cloudstack.networks import TenantNetworks

DOMAINS = [
    {"id": "d-root", "path": "ROOT"},
    {"id": "d-acme", "path": "ROOT/Customers/Acme"},
    {"id": "d-other", "path": "ROOT/Customers/Other"},
]

NETWORKS = [
    {"id": "net-tier-a", "account": "acme", "domainid": "d-acme", "vpcid": "vpc-1"},
    {"id": "net-tier-b", "account": "acme", "domainid": "d-acme", "vpcid": "vpc-1"},
    {"id": "net-isolated", "account": "acme", "domainid": "d-acme"},
]


class FakeClient:
    """Answers listDomains and listNetworks, recording what was asked."""

    def __init__(
        self,
        domains: list[dict[str, Any]] | None = None,
        networks: list[dict[str, Any]] | None = None,
        fail: str = "",
    ) -> None:
        self.domains = DOMAINS if domains is None else domains
        self.networks = NETWORKS if networks is None else networks
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_paged(self, command: str, item_key: str, **params: Any) -> list[Any]:
        self.calls.append((command, params))
        if self.fail == command:
            raise RuntimeError(f"{command} is unavailable")
        if command == "listDomains":
            return list(self.domains)
        if command == "listNetworks":
            account = params.get("account")
            domainid = params.get("domainid")
            return [
                n
                for n in self.networks
                if (account is None or n["account"] == account)
                and (domainid is None or n["domainid"] == domainid)
            ]
        raise AssertionError(f"unexpected command {command}")


class TestLookingUpTiers:
    async def test_it_returns_the_accounts_networks(self) -> None:
        found = await TenantNetworks(FakeClient()).for_account("acme", "ROOT/Customers/Acme")

        assert set(found) == {"net-tier-a", "net-tier-b", "net-isolated"}

    async def test_it_resolves_the_domain_path_to_an_id(self) -> None:
        client = FakeClient()

        await TenantNetworks(client).for_account("acme", "ROOT/Customers/Acme")

        networks_call = next(p for c, p in client.calls if c == "listNetworks")
        assert networks_call["domainid"] == "d-acme"
        assert networks_call["account"] == "acme"

    async def test_another_accounts_networks_are_not_returned(self) -> None:
        # The whole point: naming someone else's tier must not work.
        theirs = [{"id": "net-theirs", "account": "other", "domainid": "d-other"}]
        found = await TenantNetworks(FakeClient(networks=theirs)).for_account(
            "acme", "ROOT/Customers/Acme"
        )

        assert found == []

    async def test_a_trailing_slash_in_the_path_still_matches(self) -> None:
        found = await TenantNetworks(FakeClient()).for_account("acme", "ROOT/Customers/Acme/")

        assert "net-tier-a" in found

    async def test_an_unknown_domain_yields_nothing(self) -> None:
        # Not an error: it refuses the attachment, and saying "no such
        # domain" would confirm which domains exist.
        found = await TenantNetworks(FakeClient()).for_account("acme", "ROOT/Nowhere")

        assert found == []

    async def test_it_does_not_ask_for_networks_it_cannot_scope(self) -> None:
        client = FakeClient()

        await TenantNetworks(client).for_account("acme", "ROOT/Nowhere")

        assert not [c for c, _ in client.calls if c == "listNetworks"]


class TestWhenCloudStackIsUnhappy:
    async def test_a_failure_is_not_read_as_an_empty_list(self) -> None:
        # Swallowing it would refuse the attachment with "not available
        # to this account", which sends an operator to the wrong place.
        with pytest.raises(RuntimeError):
            await TenantNetworks(FakeClient(fail="listNetworks")).for_account(
                "acme", "ROOT/Customers/Acme"
            )

    async def test_a_domain_lookup_failure_propagates_too(self) -> None:
        with pytest.raises(RuntimeError):
            await TenantNetworks(FakeClient(fail="listDomains")).for_account(
                "acme", "ROOT/Customers/Acme"
            )


class TestWhatCountsAsAttachable:
    async def test_vpc_tiers_are_included(self) -> None:
        found = await TenantNetworks(FakeClient()).for_account("acme", "ROOT/Customers/Acme")

        assert "net-tier-a" in found

    async def test_a_plain_isolated_network_is_included_too(self) -> None:
        # A proxy needs a NIC on a network the tenant owns; nothing in
        # the design requires that network to belong to a VPC, and
        # excluding one would refuse a legitimate attachment.
        found = await TenantNetworks(FakeClient()).for_account("acme", "ROOT/Customers/Acme")

        assert "net-isolated" in found
