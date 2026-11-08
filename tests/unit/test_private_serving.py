"""Serving a private endpoint (SPEC.md section 8).

The exposure was validated and stored; nothing acted on it. Two things
have to happen for it to mean anything:

- a model with its public endpoint disabled must disappear from the
  public gateway, or "private" is a label on a thing anyone can still
  call;
- a proxy has to exist on the tenant's VPC tier, with an address on
  that network.

**The second cannot be a Kubernetes LoadBalancer Service.** CloudStack's
cloud-controller-manager derives a service's network from the nodes'
first NIC and offers no annotation to choose one; `loadBalancerIP` only
picks among public IPs the account can already see. So the proxy is a
VM on the tenant's tier, which is what SPEC's "per-tenant gateway
proxy" means in practice.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from aisvc.endpoints import EndpointExposure, TlsSource
from aisvc.endpoints.proxy import (
    PROXY_PORT,
    ProxyPlan,
    plan_proxy,
    proxy_name,
    render_proxy_userdata,
)
from aisvc.gateway import build_litellm_config
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

BASE = "llama-3.3-70b-instruct"


def spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


def exposure(**kw: Any) -> EndpointExposure:
    defaults: dict[str, Any] = {
        "deployment": BASE,
        "account": "acme",
        "domain": "/Customers/Acme",
        "public_enabled": False,
        "network_id": "net-1",
    }
    defaults.update(kw)
    return EndpointExposure(**defaults)


def names(config: dict[str, Any]) -> list[str]:
    return [entry["model_name"] for entry in config["model_list"]]


class TestThePublicGateway:
    def test_a_public_model_is_served_publicly(self) -> None:
        config = build_litellm_config(spec(), exposures=[])

        assert BASE in names(config)

    def test_a_private_only_model_is_not(self) -> None:
        """Otherwise "private" is a label on something anyone can call."""
        config = build_litellm_config(spec(), exposures=[exposure()])

        assert BASE not in names(config)

    def test_a_model_exposed_both_ways_stays_public(self) -> None:
        config = build_litellm_config(spec(), exposures=[exposure(public_enabled=True)])

        assert BASE in names(config)

    def test_an_exposure_for_another_model_changes_nothing(self) -> None:
        config = build_litellm_config(spec(), exposures=[exposure(deployment="something-else")])

        assert BASE in names(config)

    def test_no_exposures_renders_as_before(self) -> None:
        assert build_litellm_config(spec()) == build_litellm_config(spec(), exposures=[])

    def test_an_adapter_follows_its_base_off_the_public_gateway(self) -> None:
        """A private base with a publicly reachable adapter would be a
        hole straight through the control."""
        from aisvc.adapters import AdapterRequest

        adapter = AdapterRequest(
            name="legal-tone",
            base_model=BASE,
            account="acme",
            domain="/Customers/Acme",
            rank=16,
            size_bytes=1024,
            files=("adapter_model.safetensors", "adapter_config.json"),
        )

        config = build_litellm_config(spec(), adapters=[adapter], exposures=[exposure()])

        assert not any(":" in name for name in names(config))


class TestPlanningTheProxy:
    def test_a_private_exposure_plans_a_proxy(self) -> None:
        plan = plan_proxy(exposure(), zone_id="zone-1", gateway_host="gw.aisvc.svc")

        assert isinstance(plan, ProxyPlan)
        assert plan is not None
        assert plan.network_id == "net-1"

    def test_a_public_only_exposure_plans_nothing(self) -> None:
        plan = plan_proxy(
            exposure(public_enabled=True, network_id=""),
            zone_id="zone-1",
            gateway_host="gw",
        )

        assert plan is None

    def test_the_proxy_is_named_for_its_deployment_and_account(self) -> None:
        """Two tenants may both front the same model."""
        one = proxy_name("acme", BASE)
        other = proxy_name("other", BASE)

        assert one != other
        assert one.startswith("aisvc-gw-")

    def test_the_name_survives_awkward_account_names(self) -> None:
        """CloudStack instance names are not free-form."""
        name = proxy_name("Acme Corp / EU", BASE)

        assert " " not in name
        assert "/" not in name
        assert name.islower()

    def test_the_name_is_stable(self) -> None:
        """Provisioning reads membership back from the name, as GPU
        nodes do; an unstable name would orphan the VM."""
        assert proxy_name("acme", BASE) == proxy_name("acme", BASE)

    def test_the_plan_carries_the_account_for_ownership(self) -> None:
        plan = plan_proxy(exposure(), zone_id="z", gateway_host="gw")

        assert plan is not None
        assert plan.account == "acme"
        assert plan.domain == "/Customers/Acme"


class TestTheProxyUserdata:
    def userdata(self, **kw: Any) -> str:
        plan = plan_proxy(exposure(**kw), zone_id="z", gateway_host="gw.aisvc.svc.cluster.local")
        assert plan is not None
        return render_proxy_userdata(plan)

    def test_it_is_valid_cloud_init(self) -> None:
        parsed = yaml.safe_load(self.userdata())

        assert isinstance(parsed, dict)

    def test_it_forwards_to_the_gateway(self) -> None:
        assert "gw.aisvc.svc.cluster.local" in self.userdata()

    def test_it_listens_on_the_documented_port(self) -> None:
        assert str(PROXY_PORT) in self.userdata()

    def test_it_only_fronts_its_own_deployment(self) -> None:
        """A tenant's proxy must not become a route to every model."""
        assert BASE in self.userdata()

    def test_an_uploaded_certificate_is_installed(self) -> None:
        text = self.userdata(
            custom_domain="ai.acme.example",
            tls=TlsSource.UPLOADED,
            certificate_id="cert-1",
        )

        assert "ai.acme.example" in text
        assert "ssl_certificate" in text

    def test_acme_is_configured_where_it_is_reachable(self) -> None:
        text = self.userdata(
            public_enabled=True,
            custom_domain="ai.acme.example",
            tls=TlsSource.ACME,
        )

        assert "acme" in text.lower()

    def test_no_tls_serves_plain_http(self) -> None:
        assert "ssl_certificate" not in self.userdata()

    def test_the_certificate_body_is_not_baked_into_userdata(self) -> None:
        """Userdata is readable from the CloudStack API by anyone who
        can see the VM; a private key there is a private key published."""
        text = self.userdata(
            custom_domain="ai.acme.example",
            tls=TlsSource.UPLOADED,
            certificate_id="cert-1",
        )

        assert "BEGIN PRIVATE KEY" not in text
        assert "BEGIN RSA PRIVATE KEY" not in text


class TestOwnedNetworks:
    async def test_the_tenants_networks_come_from_cloudstack(self) -> None:
        """The engine must not guess at a tenant's VPC tiers."""
        from aisvc.endpoints.proxy import owned_networks

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def call_paged(
                self, command: str, key: str, **params: Any
            ) -> list[dict[str, Any]]:
                self.calls.append((command, params))
                return [{"id": "net-1"}, {"id": "net-2"}]

        client = FakeClient()
        networks = await owned_networks(client, account="acme", domain_id="d-1")

        assert networks == ["net-1", "net-2"]
        assert client.calls[0][0] == "listNetworks"

    async def test_it_asks_only_for_that_account(self) -> None:
        from aisvc.endpoints.proxy import owned_networks

        class FakeClient:
            def __init__(self) -> None:
                self.params: dict[str, Any] = {}

            async def call_paged(
                self, command: str, key: str, **params: Any
            ) -> list[dict[str, Any]]:
                self.params = params
                return []

        client = FakeClient()
        await owned_networks(client, account="acme", domain_id="d-1")

        assert client.params["account"] == "acme"
        assert client.params["domainid"] == "d-1"

    async def test_no_networks_is_not_an_error(self) -> None:
        from aisvc.endpoints.proxy import owned_networks

        class Empty:
            async def call_paged(
                self, command: str, key: str, **params: Any
            ) -> list[dict[str, Any]]:
                return []

        assert await owned_networks(Empty(), account="a", domain_id="d") == []


class TestTheDeployerHonoursExposures:
    """`apply_routes` writes the public gateway's config, so it is what
    has to leave a private deployment out."""

    def deployer(self, exposures: Any = ()) -> Any:
        from aisvc.k8s.client import FakeKubernetes
        from aisvc.reconciler.deployer import ServingDeployer
        from aisvc.registry import InMemoryObjectStore, ModelRegistry

        return ServingDeployer(
            FakeKubernetes(),
            spec(),
            ModelRegistry(InMemoryObjectStore(), bucket="models"),
            exposures_for=lambda: exposures,
        )

    def served(self, cluster: Any) -> list[str]:
        from aisvc.k8s.client import observe_weights

        return list(observe_weights(cluster))

    async def test_a_public_model_is_in_the_config(self) -> None:
        deployer = self.deployer()

        await deployer.apply_routes()

        assert BASE in self.served(deployer.cluster)

    async def test_a_private_model_is_not(self) -> None:
        deployer = self.deployer(exposures=(exposure(),))

        await deployer.apply_routes()

        assert BASE not in self.served(deployer.cluster)

    async def test_making_it_private_removes_it_on_the_next_run(self) -> None:
        """The control has to take effect without anyone deleting the
        deployment: switching public off is the whole action."""
        deployer = self.deployer()
        await deployer.apply_routes()

        private = self.deployer(exposures=(exposure(),))
        private.cluster = deployer.cluster
        await private.apply_routes()

        assert BASE not in self.served(deployer.cluster)

    async def test_without_a_lookup_everything_stays_public(self) -> None:
        from aisvc.k8s.client import FakeKubernetes
        from aisvc.reconciler.deployer import ServingDeployer
        from aisvc.registry import InMemoryObjectStore, ModelRegistry

        deployer = ServingDeployer(
            FakeKubernetes(),
            spec(),
            ModelRegistry(InMemoryObjectStore(), bucket="models"),
        )

        await deployer.apply_routes()

        assert BASE in self.served(deployer.cluster)


class FakeCloudStack:
    """Records the calls a provisioner makes."""

    def __init__(self, *, existing: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.existing = existing or []

    async def call_paged(self, command: str, key: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((command, params))
        if command == "listVirtualMachines":
            return self.existing
        return [{"id": "x-1"}]

    async def call_async(self, command: str, **params: Any) -> dict[str, Any]:
        self.calls.append((command, params))
        return {"id": "vm-1"}

    def commands(self) -> list[str]:
        return [name for name, _ in self.calls]

    def params_for(self, command: str) -> dict[str, Any]:
        return next(params for name, params in self.calls if name == command)


class TestProvisioningTheProxy:
    def provisioner(self, client: Any) -> Any:
        from aisvc.endpoints.proxy import ProxyProvisioner

        return ProxyProvisioner(
            client,
            zone_id="zone-1",
            template_id="tmpl-1",
            offering_id="off-1",
            gateway_host="gw.aisvc.svc.cluster.local",
        )

    def plan(self, **kw: Any) -> Any:
        from aisvc.endpoints.proxy import plan_proxy

        return plan_proxy(exposure(**kw), zone_id="zone-1", gateway_host="gw")

    async def test_a_proxy_vm_is_deployed(self) -> None:
        client = FakeCloudStack()

        await self.provisioner(client).ensure(self.plan())

        assert "deployVirtualMachine" in client.commands()

    async def test_it_lands_on_the_tenants_network(self) -> None:
        """The whole point: an address on their tier, not ours."""
        client = FakeCloudStack()

        await self.provisioner(client).ensure(self.plan())

        assert client.params_for("deployVirtualMachine")["networkids"] == "net-1"

    async def test_it_carries_its_cloud_init(self) -> None:
        client = FakeCloudStack()

        await self.provisioner(client).ensure(self.plan())

        assert client.params_for("deployVirtualMachine")["userdata"]

    async def test_it_is_named_so_it_can_be_found_again(self) -> None:
        from aisvc.endpoints.proxy import proxy_name

        client = FakeCloudStack()

        await self.provisioner(client).ensure(self.plan())

        assert client.params_for("deployVirtualMachine")["name"] == proxy_name("acme", BASE)

    async def test_an_existing_proxy_is_not_deployed_twice(self) -> None:
        """A second apply must plan nothing, as everywhere else."""
        from aisvc.endpoints.proxy import proxy_name

        client = FakeCloudStack(existing=[{"id": "vm-1", "name": proxy_name("acme", BASE)}])

        await self.provisioner(client).ensure(self.plan())

        assert "deployVirtualMachine" not in client.commands()

    async def test_a_dry_run_writes_nothing(self) -> None:
        client = FakeCloudStack()

        await self.provisioner(client).ensure(self.plan(), dry_run=True)

        assert "deployVirtualMachine" not in client.commands()

    async def test_nothing_to_do_for_a_public_endpoint(self) -> None:
        client = FakeCloudStack()

        await self.provisioner(client).ensure(None)

        assert client.calls == []

    async def test_a_removed_exposure_destroys_its_proxy(self) -> None:
        from aisvc.endpoints.proxy import proxy_name

        client = FakeCloudStack(existing=[{"id": "vm-1", "name": proxy_name("acme", BASE)}])

        await self.provisioner(client).remove(self.plan())

        assert "destroyVirtualMachine" in client.commands()

    async def test_destroying_expunges(self) -> None:
        """A proxy left in the recycle bin keeps its NIC on the tenant's
        tier, which is the thing they asked to take away."""
        from aisvc.endpoints.proxy import proxy_name

        client = FakeCloudStack(existing=[{"id": "vm-1", "name": proxy_name("acme", BASE)}])

        await self.provisioner(client).remove(self.plan())

        assert client.params_for("destroyVirtualMachine")["expunge"] is True

    async def test_removing_one_that_is_gone_is_harmless(self) -> None:
        client = FakeCloudStack(existing=[])

        await self.provisioner(client).remove(self.plan())

        assert "destroyVirtualMachine" not in client.commands()


class TestNameBasedProvisioning:
    """The executor knows an action's name, not its plan, so the
    provisioner has to resolve one back to the other."""

    def provisioner(self, client: Any, exposures: Any = ()) -> Any:
        from aisvc.endpoints.proxy import ProxyProvisioner

        return ProxyProvisioner(
            client,
            zone_id="zone-1",
            template_id="tmpl-1",
            offering_id="off-1",
            gateway_host="gw.aisvc.svc.cluster.local",
            exposures_for=lambda: exposures,
        )

    async def test_a_named_create_finds_its_exposure(self) -> None:
        from aisvc.endpoints.proxy import proxy_name

        client = FakeCloudStack()
        name = proxy_name("acme", BASE)

        await self.provisioner(client, (exposure(),)).ensure_named(name)

        assert client.params_for("deployVirtualMachine")["name"] == name

    async def test_a_name_with_no_exposure_is_an_error(self) -> None:
        """Silently doing nothing would leave the run green and the
        tenant without the endpoint they asked for."""
        client = FakeCloudStack()

        from aisvc.endpoints.proxy import ProxyError

        with pytest.raises(ProxyError, match="no private endpoint"):
            await self.provisioner(client, ()).ensure_named("aisvc-gw-ghost-model")

    async def test_a_named_delete_does_not_need_an_exposure(self) -> None:
        """By the time a proxy is removed its exposure is already gone;
        the name is all there is to go on."""
        from aisvc.endpoints.proxy import proxy_name

        name = proxy_name("acme", BASE)
        client = FakeCloudStack(existing=[{"id": "vm-1", "name": name}])

        await self.provisioner(client, ()).remove_named(name)

        assert "destroyVirtualMachine" in client.commands()

    async def test_the_names_it_reports_are_what_it_touched(self) -> None:
        from aisvc.endpoints.proxy import proxy_name

        client = FakeCloudStack()
        name = proxy_name("acme", BASE)

        assert await self.provisioner(client, (exposure(),)).ensure_named(name) == [name]


class TestObservingProxies:
    async def test_existing_proxies_are_listed_by_name(self) -> None:
        from aisvc.endpoints.proxy import observe_proxies, proxy_name

        name = proxy_name("acme", BASE)
        client = FakeCloudStack(existing=[{"id": "vm-1", "name": name}])

        assert await observe_proxies(client) == {name: "vm-1"}

    async def test_other_machines_are_ignored(self) -> None:
        """GPU nodes and tenant VMs share the account; only ours count."""
        from aisvc.endpoints.proxy import observe_proxies

        client = FakeCloudStack(
            existing=[
                {"id": "vm-1", "name": "aisvc-h100-pool-001"},
                {"id": "vm-2", "name": "someones-database"},
            ]
        )

        assert await observe_proxies(client) == {}

    async def test_nothing_deployed_observes_nothing(self) -> None:
        from aisvc.endpoints.proxy import observe_proxies

        assert await observe_proxies(FakeCloudStack(existing=[])) == {}


class TestTheFetcherKnowsWhereToGo:
    """aisvc-fetch-cert refuses without an engine URL, so the cloud-init
    has to supply one or the proxy boots and serves nothing."""

    def userdata(self, **kw: Any) -> str:
        from aisvc.endpoints.proxy import plan_proxy, render_proxy_userdata

        plan = plan_proxy(
            exposure(
                custom_domain="ai.acme.example",
                tls=TlsSource.UPLOADED,
                certificate_id="cert-1",
                **kw,
            ),
            zone_id="z",
            gateway_host="gw",
            engine_url="https://engine.aisvc.internal:8443",
        )
        assert plan is not None
        return render_proxy_userdata(plan)

    def test_the_engine_url_is_in_the_userdata(self) -> None:
        assert "https://engine.aisvc.internal:8443" in self.userdata()

    def test_it_is_set_where_the_fetcher_reads_it(self) -> None:
        text = self.userdata()

        assert "AISVC_ENGINE_URL" in text

    def test_a_plain_endpoint_needs_no_engine_url(self) -> None:
        from aisvc.endpoints.proxy import plan_proxy, render_proxy_userdata

        plan = plan_proxy(exposure(), zone_id="z", gateway_host="gw")
        assert plan is not None

        assert "aisvc-fetch-cert" not in render_proxy_userdata(plan)


class TestTheUserdataCarriesAToken:
    """Not a certificate id and not mTLS material.

    The id alone would need an identity the proxy has not got; mTLS
    material would be a private key in a readable document, which is
    the problem the whole mechanism exists to avoid.
    """

    def userdata(self, **kw: Any) -> str:
        from aisvc.endpoints.proxy import plan_proxy, render_proxy_userdata

        plan = plan_proxy(
            exposure(
                custom_domain="ai.acme.example",
                tls=TlsSource.UPLOADED,
                certificate_id="cert-1",
                **kw,
            ),
            zone_id="z",
            gateway_host="gw",
            engine_url="https://engine.aisvc.internal:8443",
            bootstrap_token="aisvc-boot-abcd1234-thesecret",
        )
        assert plan is not None
        return render_proxy_userdata(plan)

    def test_the_token_is_there(self) -> None:
        assert "aisvc-boot-abcd1234-thesecret" in self.userdata()

    def test_no_mtls_material_is(self) -> None:
        text = self.userdata()

        assert "client.key" not in text
        assert "PRIVATE KEY" not in text

    def test_the_fetcher_is_given_the_token(self) -> None:
        assert "--token" in self.userdata()

    def test_the_engine_is_still_named(self) -> None:
        assert "https://engine.aisvc.internal:8443" in self.userdata()

    def test_a_plain_endpoint_carries_no_token(self) -> None:
        from aisvc.endpoints.proxy import plan_proxy, render_proxy_userdata

        plan = plan_proxy(exposure(), zone_id="z", gateway_host="gw")
        assert plan is not None
        text = render_proxy_userdata(plan)

        assert "aisvc-boot" not in text
        assert "aisvc-fetch-cert" not in text


class TestMintingAtProvisionTime:
    """A token is minted when the VM is deployed, not before.

    Minting earlier would start the clock while the plan was still
    being reviewed, and a thirty-minute token is no use if it was
    issued an hour ago.
    """

    def setup_store(self) -> tuple[Any, Any]:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from aisvc.db import Base
        from aisvc.db.tier2 import CertificateStore

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(engine)()

        from tests.unit.test_certificates import make_cert

        cert, key = make_cert()
        record = CertificateStore(session).add(
            account="acme",
            domain="/Customers/Acme",
            custom_domain="ai.acme.example",
            certificate=cert,
            private_key=key,
            fingerprint="ff",
            not_after=__import__("datetime").datetime.now(__import__("datetime").UTC)
            + __import__("datetime").timedelta(days=90),
        )
        session.commit()
        return session, record

    def provisioner(self, client: Any, session: Any, exposures: Any) -> Any:
        from aisvc.endpoints.proxy import ProxyProvisioner

        return ProxyProvisioner(
            client,
            zone_id="zone-1",
            template_id="tmpl-1",
            offering_id="off-1",
            gateway_host="gw",
            engine_url="https://engine.invalid:8443",
            exposures_for=lambda: exposures,
            mint_token=lambda certificate_id, proxy: (
                __import__("aisvc.certificates.bootstrap", fromlist=["BootstrapTokens"])
                .BootstrapTokens(session)
                .issue(certificate_id=certificate_id, proxy=proxy)
                .plaintext
            ),
        )

    async def test_a_token_reaches_the_userdata(self) -> None:
        import base64

        from aisvc.endpoints.proxy import proxy_name

        session, record = self.setup_store()
        exposed = exposure(
            custom_domain="ai.acme.example",
            tls=TlsSource.UPLOADED,
            certificate_id=record.id,
        )
        client = FakeCloudStack()

        await self.provisioner(client, session, (exposed,)).ensure_named(proxy_name("acme", BASE))

        userdata = base64.b64decode(client.params_for("deployVirtualMachine")["userdata"]).decode()
        assert "aisvc-boot-" in userdata

    async def test_the_token_is_stored_against_that_proxy(self) -> None:
        from aisvc.certificates.bootstrap import BootstrapTokens
        from aisvc.db.models import BootstrapTokenRecord
        from aisvc.endpoints.proxy import proxy_name

        session, record = self.setup_store()
        exposed = exposure(
            custom_domain="ai.acme.example",
            tls=TlsSource.UPLOADED,
            certificate_id=record.id,
        )

        await self.provisioner(FakeCloudStack(), session, (exposed,)).ensure_named(
            proxy_name("acme", BASE)
        )

        stored = session.query(BootstrapTokenRecord).all()
        assert len(stored) == 1
        assert stored[0].proxy == proxy_name("acme", BASE)
        assert stored[0].certificate_id == record.id
        assert BootstrapTokens(session)  # store is usable

    async def test_a_plain_endpoint_mints_nothing(self) -> None:
        """No certificate, no token: there is nothing to fetch."""
        from aisvc.db.models import BootstrapTokenRecord
        from aisvc.endpoints.proxy import proxy_name

        session, _ = self.setup_store()

        await self.provisioner(FakeCloudStack(), session, (exposure(),)).ensure_named(
            proxy_name("acme", BASE)
        )

        assert session.query(BootstrapTokenRecord).all() == []

    async def test_an_existing_proxy_mints_nothing(self) -> None:
        """It already has what it needs; a second token would be a
        second credential in a readable place for no reason."""
        from aisvc.db.models import BootstrapTokenRecord
        from aisvc.endpoints.proxy import proxy_name

        session, record = self.setup_store()
        name = proxy_name("acme", BASE)
        exposed = exposure(
            custom_domain="ai.acme.example",
            tls=TlsSource.UPLOADED,
            certificate_id=record.id,
        )
        client = FakeCloudStack(existing=[{"id": "vm-1", "name": name}])

        await self.provisioner(client, session, (exposed,)).ensure_named(name)

        assert session.query(BootstrapTokenRecord).all() == []
