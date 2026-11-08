"""The per-tenant gateway proxy (SPEC.md section 8).

"An endpoint can attach to a tenant's CloudStack VPC tier through a
per-tenant gateway proxy with an address on that network."

**Why this is a VM and not a Kubernetes Service.** The obvious
implementation is a `LoadBalancer` Service annotated onto the tenant's
tier, and it does not work: CloudStack's cloud-controller-manager
derives a service's network from the *nodes'* first NIC and exposes no
annotation to choose one, while `spec.loadBalancerIP` only selects
among public IPs the account can already see. Neither can reach an
arbitrary VPC tier. So the proxy is a small VM with a NIC on that tier,
forwarding to the in-cluster gateway — which is what "per-tenant
gateway proxy" meant anyway.

The proxy fronts **one deployment**. A per-tenant proxy that forwarded
anything would turn a private endpoint into a second, unaudited route
to the whole platform.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aisvc.endpoints import TlsSource

if TYPE_CHECKING:
    from aisvc.endpoints import EndpointExposure

__all__ = [
    "PROXY_PORT",
    "ProxyError",
    "ProxyPlan",
    "ProxyProvisioner",
    "observe_proxies",
    "owned_networks",
    "plan_proxy",
    "proxy_name",
    "render_proxy_userdata",
]

#: What the proxy listens on inside the tenant's network.
PROXY_PORT = 8443

#: Mirrors `aisvc-<pool>-<ordinal>` for GPU nodes: membership is read
#: back from the name, so it has to be stable and CloudStack-legal.
NAME_PREFIX = "aisvc-gw"


def _slug(value: str) -> str:
    """A CloudStack-legal fragment of an arbitrary name.

    Account and domain names are free-form; instance names are not.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return cleaned or "x"


def proxy_name(account: str, deployment: str) -> str:
    """The proxy VM's name.

    Carries both the account and the deployment because two tenants may
    each front the same model, and the name is how the VM is found
    again on the next reconcile.
    """
    return f"{NAME_PREFIX}-{_slug(account)}-{_slug(deployment)}"


@dataclass(frozen=True, slots=True)
class ProxyPlan:
    """What to provision for one private endpoint."""

    name: str
    deployment: str
    account: str
    domain: str
    network_id: str
    zone_id: str
    gateway_host: str
    #: Where aisvc-fetch-cert goes for the certificate material. It
    #: refuses without one, so a proxy with an uploaded certificate and
    #: no engine URL would boot and serve nothing.
    engine_url: str = ""
    custom_domain: str = ""
    tls: TlsSource = TlsSource.NONE
    certificate_id: str = ""
    public_enabled: bool = True


def plan_proxy(
    exposure: EndpointExposure,
    *,
    zone_id: str,
    gateway_host: str,
    engine_url: str = "",
) -> ProxyPlan | None:
    """What this exposure needs on the tenant's network, if anything.

    A public-only endpoint needs no proxy: it is already served by the
    shared gateway.
    """
    if not exposure.network_id:
        return None

    return ProxyPlan(
        name=proxy_name(exposure.account, exposure.deployment),
        deployment=exposure.deployment,
        account=exposure.account,
        domain=exposure.domain,
        network_id=exposure.network_id,
        zone_id=zone_id,
        gateway_host=gateway_host,
        engine_url=engine_url,
        custom_domain=exposure.custom_domain,
        tls=exposure.tls,
        certificate_id=exposure.certificate_id,
        public_enabled=exposure.public_enabled,
    )


def render_proxy_userdata(plan: ProxyPlan) -> str:
    """cloud-init for the proxy VM.

    **No certificate material here.** Userdata is readable through the
    CloudStack API by anyone who can see the VM, so an uploaded
    certificate is fetched at boot from the engine by its id, over
    mTLS, and never written into this document.
    """
    server_name = plan.custom_domain or "_"
    listen = f"{PROXY_PORT} ssl" if plan.tls is not TlsSource.NONE else str(PROXY_PORT)

    lines = [
        "#cloud-config",
        "package_update: true",
        "packages:",
        "  - nginx",
    ]
    if plan.tls is TlsSource.ACME:
        lines.append("  - certbot")
        lines.append("  - python3-certbot-nginx")

    tls_block = ""
    if plan.tls is TlsSource.UPLOADED:
        tls_block = (
            "      ssl_certificate /etc/aisvc/tls/fullchain.pem;\n"
            "      ssl_certificate_key /etc/aisvc/tls/privkey.pem;\n"
        )
    elif plan.tls is TlsSource.ACME:
        tls_block = (
            "      # certbot writes the certificate here on first run.\n"
            f"      ssl_certificate /etc/letsencrypt/live/{plan.custom_domain}/fullchain.pem;\n"
            f"      ssl_certificate_key /etc/letsencrypt/live/{plan.custom_domain}/privkey.pem;\n"
        )

    # One deployment only: a proxy that forwarded anything would be a
    # second, unaudited route to the whole platform.
    config = (
        "server {\n"
        f"  listen {listen};\n"
        f"  server_name {server_name};\n"
        f"{tls_block}"
        f"  # Only this tenant's deployment, {plan.deployment}.\n"
        "  location /v1/ {\n"
        f"    proxy_set_header X-Aisvc-Deployment {plan.deployment};\n"
        "    proxy_set_header Host $host;\n"
        f"    proxy_pass http://{plan.gateway_host}/v1/;\n"
        "  }\n"
        "  location / { return 404; }\n"
        "}\n"
    )

    lines.append("write_files:")
    lines.append("  - path: /etc/nginx/conf.d/aisvc.conf")
    lines.append("    permissions: '0644'")
    lines.append("    content: |")
    lines.extend(f"      {line}" for line in config.splitlines())

    lines.append("runcmd:")
    if plan.tls is TlsSource.UPLOADED:
        lines.append("  - mkdir -p /etc/aisvc/tls")
        lines.append(f"  - echo 'AISVC_ENGINE_URL={plan.engine_url}' >> /etc/environment")
        lines.append("  # Fetched from the engine over mTLS by id: userdata is readable")
        lines.append("  # through the CloudStack API and must not carry a key.")
        lines.append(f"  - aisvc-fetch-cert --id {plan.certificate_id} --out /etc/aisvc/tls")
    if plan.tls is TlsSource.ACME:
        lines.append(
            f"  - certbot --nginx -n --agree-tos -d {plan.custom_domain} "
            "--register-unsafely-without-email"
        )
    lines.append("  - systemctl enable --now nginx")

    return "\n".join(lines) + "\n"


async def owned_networks(client: Any, *, account: str, domain_id: str) -> list[str]:
    """The networks this account may attach a private endpoint to.

    Asked of CloudStack rather than inferred: the engine has no view of
    a tenant's VPC tiers, and a guess here would let one tenant name
    another's network.
    """
    networks = await client.call_paged(
        "listNetworks", "network", account=account, domainid=domain_id, listall=True
    )
    return [str(network["id"]) for network in networks if network.get("id")]


class ProxyError(Exception):
    """A proxy could not be built or removed."""


class ProxyProvisioner:
    """Creates and destroys per-tenant gateway proxies.

    Idempotent by name, the way GPU nodes are (CLAUDE.md): membership
    is read back from `aisvc-gw-<account>-<deployment>`, so a second
    apply finds the VM already there and plans nothing.
    """

    def __init__(
        self,
        client: Any,
        *,
        zone_id: str,
        template_id: str,
        offering_id: str,
        gateway_host: str,
        engine_url: str = "",
        exposures_for: Any = None,
    ) -> None:
        self.client = client
        # How a name is resolved back to what it should be: the
        # executor knows an action's name and nothing else.
        self.exposures_for = exposures_for
        self.zone_id = zone_id
        self.template_id = template_id
        self.offering_id = offering_id
        self.gateway_host = gateway_host
        self.engine_url = engine_url

    async def _existing(self, name: str) -> dict[str, Any] | None:
        machines = await self.client.call_paged(
            "listVirtualMachines", "virtualmachine", name=name, listall=True
        )
        return next((vm for vm in machines if vm.get("name") == name), None)

    async def ensure(self, plan: ProxyPlan | None, *, dry_run: bool = False) -> None:
        """Make sure this exposure's proxy exists."""
        if plan is None:
            # A public-only endpoint needs nothing on a tenant network.
            return

        if await self._existing(plan.name) is not None:
            return

        if dry_run:
            return

        await self.client.call_async(
            "deployVirtualMachine",
            name=plan.name,
            displayname=plan.name,
            zoneid=self.zone_id,
            templateid=self.template_id,
            serviceofferingid=self.offering_id,
            # The address on the tenant's tier, which is the whole
            # point and the thing a LoadBalancer Service cannot do.
            networkids=plan.network_id,
            userdata=_encode(render_proxy_userdata(plan)),
            startvm=True,
        )

    def _plan_named(self, name: str) -> ProxyPlan | None:
        for exposure in self.exposures_for() if self.exposures_for else ():
            plan = plan_proxy(exposure, zone_id=self.zone_id, gateway_host=self.gateway_host)
            if plan is not None and plan.name == name:
                return plan
        return None

    async def ensure_named(self, name: str, *, dry_run: bool = False) -> list[str]:
        """Build the proxy this action names.

        A name with no exposure behind it raises rather than doing
        nothing: a green run that built no proxy would leave the tenant
        without the endpoint they asked for and nobody any the wiser.
        """
        plan = self._plan_named(name)
        if plan is None:
            raise ProxyError(
                f"no private endpoint matches {name!r}; it may have been removed "
                "since the plan was drawn"
            )
        await self.ensure(plan, dry_run=dry_run)
        return [name]

    async def remove_named(self, name: str, *, dry_run: bool = False) -> list[str]:
        """Take away the proxy this action names.

        No exposure is needed: by the time a proxy is removed its
        exposure is already gone, so the name is all there is.
        """
        existing = await self._existing(name)
        if existing is not None and not dry_run:
            await self.client.call_async("destroyVirtualMachine", id=existing["id"], expunge=True)
        return [name]

    async def remove(self, plan: ProxyPlan, *, dry_run: bool = False) -> None:
        """Take a proxy away once its exposure is gone."""
        existing = await self._existing(plan.name)
        if existing is None or dry_run:
            return

        await self.client.call_async(
            "destroyVirtualMachine",
            id=existing["id"],
            # Expunged, not just destroyed: a VM in the recycle bin
            # keeps its NIC on the tenant's tier, which is exactly what
            # removing the endpoint was meant to take away.
            expunge=True,
        )


def _encode(text: str) -> str:
    from aisvc.provision.cloudinit import encode_userdata

    return encode_userdata(text)


async def observe_proxies(client: Any) -> dict[str, str]:
    """Per-tenant proxy VMs that exist, by name.

    Matched on the name prefix, as GPU nodes are: a VM this engine did
    not mint is not ours to reconcile, however much it looks like one.
    """
    machines = await client.call_paged("listVirtualMachines", "virtualmachine", listall=True)
    return {
        str(vm["name"]): str(vm["id"])
        for vm in machines
        if str(vm.get("name", "")).startswith(f"{NAME_PREFIX}-")
    }
