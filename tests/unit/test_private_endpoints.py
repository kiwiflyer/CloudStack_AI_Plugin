"""Private endpoints (SPEC.md section 8).

An endpoint can be attached to a tenant's own VPC tier and its public
address taken away. Two things make this worth careful handling:

- Disabling the public address while nothing private is attached would
  leave a deployment nobody can reach, including the tenant who asked.
- A private endpoint is a security control a regulated buyer is paying
  for, so "attached" has to mean attached to *their* network, not any
  network they named.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.endpoints import (
    EndpointExposure,
    ExposureRejected,
    TlsSource,
    describe_exposure,
    validate_exposure,
)


def exposure(**kw: Any) -> EndpointExposure:
    defaults: dict[str, Any] = {
        "deployment": "llama-3.3-70b-instruct",
        "account": "acme",
        "domain": "/Customers/Acme",
        "public_enabled": True,
    }
    defaults.update(kw)
    return EndpointExposure(**defaults)


class TestReachability:
    def test_a_public_endpoint_is_reachable(self) -> None:
        assert validate_exposure(exposure(), owned_networks=())

    def test_a_private_endpoint_is_reachable(self) -> None:
        assert validate_exposure(
            exposure(public_enabled=False, network_id="net-1"),
            owned_networks=("net-1",),
        )

    def test_both_at_once_is_allowed(self) -> None:
        """A tenant may migrate gradually."""
        assert validate_exposure(
            exposure(public_enabled=True, network_id="net-1"),
            owned_networks=("net-1",),
        )

    def test_neither_leaves_nobody_able_to_reach_it(self) -> None:
        with pytest.raises(ExposureRejected) as caught:
            validate_exposure(exposure(public_enabled=False), owned_networks=())

        assert caught.value.reason == "unreachable"

    def test_the_refusal_explains_the_consequence(self) -> None:
        with pytest.raises(ExposureRejected) as caught:
            validate_exposure(exposure(public_enabled=False), owned_networks=())

        assert "reach" in caught.value.detail.lower()


class TestNetworkOwnership:
    def test_a_network_the_tenant_owns_is_accepted(self) -> None:
        assert validate_exposure(exposure(network_id="net-1"), owned_networks=("net-1", "net-2"))

    def test_a_network_the_tenant_does_not_own_is_refused(self) -> None:
        """Otherwise a tenant names someone else's tier and gets an
        address inside their network."""
        with pytest.raises(ExposureRejected) as caught:
            validate_exposure(exposure(network_id="net-9"), owned_networks=("net-1",))

        assert caught.value.reason == "network_not_owned"

    def test_the_refusal_does_not_say_whether_the_network_exists(self) -> None:
        """A different message for "not yours" and "no such network"
        would enumerate other tenants' networks."""
        with pytest.raises(ExposureRejected) as caught:
            validate_exposure(exposure(network_id="net-9"), owned_networks=("net-1",))

        assert "net-1" not in caught.value.detail


class TestTls:
    def test_a_custom_domain_needs_a_certificate_source(self) -> None:
        with pytest.raises(ExposureRejected) as caught:
            validate_exposure(exposure(custom_domain="ai.acme.example"), owned_networks=())

        assert caught.value.reason == "no_tls_source"

    def test_acme_satisfies_a_custom_domain(self) -> None:
        assert validate_exposure(
            exposure(custom_domain="ai.acme.example", tls=TlsSource.ACME),
            owned_networks=(),
        )

    def test_an_uploaded_certificate_satisfies_it(self) -> None:
        assert validate_exposure(
            exposure(
                custom_domain="ai.acme.example",
                tls=TlsSource.UPLOADED,
                certificate_id="cert-1",
            ),
            owned_networks=(),
        )

    def test_an_uploaded_source_without_a_certificate_is_refused(self) -> None:
        with pytest.raises(ExposureRejected) as caught:
            validate_exposure(
                exposure(custom_domain="ai.acme.example", tls=TlsSource.UPLOADED),
                owned_networks=(),
            )

        assert caught.value.reason == "no_certificate"

    def test_acme_on_a_private_only_endpoint_is_refused(self) -> None:
        """ACME has to reach the host to validate the domain, and a
        private-only endpoint is not reachable from the internet."""
        with pytest.raises(ExposureRejected) as caught:
            validate_exposure(
                exposure(
                    public_enabled=False,
                    network_id="net-1",
                    custom_domain="ai.acme.example",
                    tls=TlsSource.ACME,
                ),
                owned_networks=("net-1",),
            )

        assert caught.value.reason == "acme_needs_public"

    def test_an_uploaded_certificate_works_on_a_private_endpoint(self) -> None:
        assert validate_exposure(
            exposure(
                public_enabled=False,
                network_id="net-1",
                custom_domain="ai.acme.example",
                tls=TlsSource.UPLOADED,
                certificate_id="cert-1",
            ),
            owned_networks=("net-1",),
        )

    def test_no_custom_domain_needs_no_certificate(self) -> None:
        assert validate_exposure(exposure(), owned_networks=())


class TestDescription:
    def test_a_public_endpoint_describes_itself_as_public(self) -> None:
        assert describe_exposure(exposure()) == "public"

    def test_a_private_endpoint_says_which_network(self) -> None:
        described = describe_exposure(exposure(public_enabled=False, network_id="net-1"))

        assert "net-1" in described
        assert "private" in described

    def test_both_is_described_as_both(self) -> None:
        described = describe_exposure(exposure(network_id="net-1"))

        assert "public" in described
        assert "private" in described

    def test_a_custom_domain_is_named(self) -> None:
        described = describe_exposure(exposure(custom_domain="ai.acme.example", tls=TlsSource.ACME))

        assert "ai.acme.example" in described
