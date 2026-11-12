"""`aisvc` operator CLI (SPEC.md section 4).

Milestone 1 ships `validate` and `plan`. Both are strictly read-only;
`apply` arrives with milestone 2.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console

from aisvc.api.client import EngineClient, EngineError
from aisvc.batch import DEFAULT_DISCOUNT
from aisvc.batch.runner import (
    BatchRunner,
    Outcome,
    batch_tier,
    preemption_signal,
)
from aisvc.certificates.bootstrap import BootstrapTokens
from aisvc.certificates.probe import sweep_endpoint_certificates
from aisvc.certificates.renewal import sweep_renewals
from aisvc.cloudstack import CloudStackClient, CloudStackError, Credentials
from aisvc.cloudstack.clusters import (
    ClusterBuilder,
    ClusterPlanned,
    ClusterState,
    ClusterUnbuildable,
    KubeconfigUnavailable,
    cluster_for_kubeconfig,
    fetch_kubeconfig,
    read_clusters,
    write_kubeconfig,
)
from aisvc.db import RunLog, UsageStore, create_session_factory, session_scope
from aisvc.db.migrate import upgrade_to_head
from aisvc.db.session import database_url
from aisvc.db.tier2 import (
    AdapterStore,
    BatchStore,
    CertificateStore,
    ExposureStore,
)
from aisvc.endpoints.proxy import ProxyProvisioner, observe_proxies
from aisvc.inventory import take_snapshot
from aisvc.k8s import DEFAULT_NAMESPACE
from aisvc.k8s.client import (
    ClusterKubernetes,
    observe_deployments,
    observe_route_hash,
    observe_weights,
)
from aisvc.metering import UsageMeter
from aisvc.observability import HealthObserver, PrometheusClient
from aisvc.observability.alerts import LogSink
from aisvc.provision import ClusterJoin, NodeProvisioner
from aisvc.reconciler import ActualState, Executor, Plan, plan_all
from aisvc.reconciler.deployer import ServingDeployer
from aisvc.reconciler.leader import NotLeader, ZoneLeadership
from aisvc.reconciler.state import DeploymentHealth
from aisvc.registry import InMemoryObjectStore, ModelRegistry, S3ObjectStore
from aisvc.registry.bucket import (
    BucketCredentials,
    SourceUnclear,
    from_cloudstack_requested,
    resolve_bucket,
)
from aisvc.spec import PlatformSpec, SpecError, load_spec

app = typer.Typer(
    name="aisvc",
    help="CloudStack AI Services control plane.",
    no_args_is_help=True,
    add_completion=False,
)

out = Console()
err = Console(stderr=True)

SpecOption = Annotated[
    Path,
    typer.Option("--spec", "-s", help="Path to platform.yaml."),
]
LicensesOption = Annotated[
    Path | None,
    typer.Option("--licenses", help="Path to licenses.yaml; built-in list if omitted."),
]


def _load(spec_path: Path, licenses_path: Path | None) -> PlatformSpec:
    """Load and validate, turning any SpecError into a clean exit."""
    try:
        return load_spec(spec_path, licenses_path=licenses_path)
    except SpecError as exc:
        err.print(f"[bold red]{spec_path} is not valid:[/bold red]")
        for problem in exc.problems:
            err.print(f"  - {problem}")
        raise typer.Exit(code=1) from exc


def _show_findings(spec: PlatformSpec) -> None:
    for warning in spec.warnings:
        err.print(f"[yellow]warning:[/yellow] {warning}")
    for note in spec.notes:
        err.print(f"[dim]note:[/dim] {note}")


def _credentials() -> Credentials:
    endpoint = os.environ.get("CLOUDSTACK_API_URL", "")
    api_key = os.environ.get("CLOUDSTACK_API_KEY", "")
    secret_key = os.environ.get("CLOUDSTACK_SECRET_KEY", "")
    missing = [
        name
        for name, value in (
            ("CLOUDSTACK_API_URL", endpoint),
            ("CLOUDSTACK_API_KEY", api_key),
            ("CLOUDSTACK_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        err.print(
            "[bold red]Cannot reach CloudStack:[/bold red] set "
            + ", ".join(missing)
            + ", or pass --no-observe to plan against an empty snapshot."
        )
        raise typer.Exit(code=1)
    return Credentials(endpoint=endpoint, api_key=api_key, secret_key=secret_key)


def _required_env(*names: str) -> dict[str, str]:
    """Read required environment variables, naming every one that is unset."""
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        err.print("[bold red]Missing configuration:[/bold red] " + ", ".join(missing))
        raise typer.Exit(code=1)
    return values


def _cluster_join() -> ClusterJoin:
    """Cluster join details come from the environment, never the spec file."""
    env = _required_env(
        "AISVC_CKS_JOIN_ENDPOINT",
        "AISVC_CKS_JOIN_TOKEN",
        "AISVC_CKS_CA_CERT_HASH",
    )
    return ClusterJoin(
        endpoint=env["AISVC_CKS_JOIN_ENDPOINT"],
        token=env["AISVC_CKS_JOIN_TOKEN"],
        ca_cert_hash=env["AISVC_CKS_CA_CERT_HASH"],
    )


def _registry_from_cloudstack() -> bool:
    """Whether to resolve the bucket through CloudStack, reported cleanly.

    `from_cloudstack_requested` raises on a value that is neither on nor
    off; a traceback is the wrong way to tell an operator they typed the
    switch wrong.
    """
    try:
        return from_cloudstack_requested(os.environ)
    except SourceUnclear as exc:
        err.print(f"[bold red]Registry source is unclear:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


def _registry(*, observe: bool = True) -> ModelRegistry:
    """The artifact registry (ADR-0005).

    With `--no-observe` nothing should leave the machine, so the plan is
    drawn against an empty registry and every model reads as unmirrored.

    With `AISVC_REGISTRY_FROM_CLOUDSTACK` on, the bucket, endpoint and
    credentials come from CloudStack's object store instead of from the
    environment. CloudStack provisions the bucket on a backing MinIO or
    Ceph and hands back S3 credentials; it does not proxy object data,
    so the store below is the same `S3ObjectStore` either way.

    **Called only from a synchronous command body**, before the event
    loop starts: resolving the bucket is itself an async call, and the
    commands pass the result down rather than building a registry
    inside a loop.
    """
    if not observe:
        return ModelRegistry(InMemoryObjectStore(), bucket="models")

    bucket = os.environ.get("AISVC_REGISTRY_BUCKET", "models")

    if _registry_from_cloudstack():
        found = asyncio.run(_bucket_from_cloudstack(bucket))
        store: Any = S3ObjectStore(
            bucket=found.name,
            endpoint_url=found.endpoint,
            aws_access_key_id=found.access_key,
            aws_secret_access_key=found.secret_key,
        )
        return ModelRegistry(store, bucket=found.name)

    endpoint = os.environ.get("AISVC_REGISTRY_ENDPOINT") or None
    store = S3ObjectStore(bucket=bucket, endpoint_url=endpoint)
    return ModelRegistry(store, bucket=bucket)


async def _bucket_from_cloudstack(bucket: str) -> BucketCredentials:
    """Read the bucket's endpoint and credentials out of CloudStack."""
    async with CloudStackClient(_credentials()) as client:
        return await resolve_bucket(client, bucket)


def _cluster(kubeconfig: str = "") -> ClusterKubernetes:
    """The engine's Kubernetes connection.

    An explicit `KUBECONFIG` always wins: an operator who set it meant
    it, and overriding them with something fetched from CloudStack
    would be the engine deciding which cluster they serve.
    """
    return ClusterKubernetes(kubeconfig=os.environ.get("KUBECONFIG") or kubeconfig or None)


def _kubeconfig_dir() -> str:
    """Where a fetched kubeconfig is kept, private to this user."""
    return os.environ.get("AISVC_KUBECONFIG_DIR") or os.path.join(
        tempfile.gettempdir(), f"aisvc-{os.getuid()}"
    )


async def _kubeconfig_from_cloudstack(client: Any, spec: PlatformSpec) -> str:
    """Fetch the cluster's kubeconfig, or return "" to fall back.

    This is what makes `create: true` end to end. Without it an
    operator had to fetch a kubeconfig by hand in between the engine
    building a cluster and the engine using it, which left cluster
    creation only half automated.

    Nothing here is fatal. A failure means the engine carries on with
    whatever `KUBECONFIG` says, including nothing — the Kubernetes
    client's own error is clearer than a second one wrapped around it.
    """
    if os.environ.get("KUBECONFIG"):
        return ""

    try:
        cluster = cluster_for_kubeconfig(_cks_zones(spec))
        if cluster is None:
            return ""
        data = await fetch_kubeconfig(client, cluster)
    except KubeconfigUnavailable as unavailable:
        err.print(f"[yellow]Could not fetch a kubeconfig:[/yellow] {unavailable}")
        return ""

    # The contents are never printed, here or anywhere: they are
    # cluster-admin credentials.
    return write_kubeconfig(data, directory=_kubeconfig_dir())


def _certificate_serials() -> dict[str, int]:
    """Each certificate's current serial.

    The planner compares these against what each proxy was built with,
    which is how a renewal becomes a proxy replacement.
    """
    factory = create_session_factory()
    with session_scope(factory) as session:
        return {record.id: record.serial or 1 for record in CertificateStore(session).all()}


def _stored_exposures() -> list[Any]:
    """Private endpoints, from the database.

    Read here rather than passed in because `observe` is where every
    other piece of actual state is gathered, and a plan drawn without
    them would re-publish every private deployment.
    """
    try:
        factory = create_session_factory()
        with session_scope(factory) as session:
            return list(ExposureStore(session).all())
    except Exception as exc:
        # Deliberately not "assume none". Without the exposures the
        # plan would propose re-publishing every private deployment,
        # which is the opposite of what the operator asked for and
        # would read as a routine route change.
        err.print(
            "[bold red]Cannot read private endpoints:[/bold red] "
            f"{exc}\nObserving needs AISVC_DATABASE_URL; without it a plan "
            "would wrongly re-publish private deployments."
        )
        raise typer.Exit(code=1) from exc


async def _observe(spec: PlatformSpec) -> ActualState:
    """Read CloudStack capacity and the cluster's serving workloads."""
    credentials = _credentials()
    namespace = os.environ.get("AISVC_NAMESPACE", DEFAULT_NAMESPACE)

    async with CloudStackClient(credentials) as client:
        capacity = await take_snapshot(client, spec)
        clusters = await read_clusters(client, _cks_zones(spec))
        plans, problems = await _cluster_plans(client, spec, clusters)
        kubeconfig = await _kubeconfig_from_cloudstack(client, spec)

    cluster = _cluster(kubeconfig)
    deployments = observe_deployments(cluster, namespace=namespace)
    return ActualState(
        capacity=capacity,
        deployments=deployments,
        route_hash=observe_route_hash(cluster, namespace=namespace),
        weights=observe_weights(cluster, namespace=namespace),
        health=await _observe_health({model for model, _ in deployments}),
        exposures=tuple(_stored_exposures()),
        proxies=await observe_proxies(client),
        certificate_serials=_certificate_serials(),
        clusters=tuple(clusters),
        cluster_plans=plans,
        cluster_problems=problems,
    )


def _cks_zones(spec: PlatformSpec) -> list[tuple[str, str]]:
    """Zones whose cluster CloudStack is meant to know about (ADR-0007)."""
    return [
        (zone.name, zone.cks_cluster)
        for zone in spec.document.zones
        if zone.kubernetes.source == "cks"
    ]


async def _cluster_plans(
    client: Any, spec: PlatformSpec, clusters: list[ClusterState]
) -> tuple[dict[str, ClusterPlanned], dict[str, str]]:
    """What would be built for each zone asking for a cluster it lacks.

    Resolution is read-only and happens here, at observe time, so the
    plan line can name the Kubernetes version and the service offering
    before an operator confirms. Where the operator pinned neither, the
    engine chose them, and one of them is billable.

    A zone that cannot be built becomes a warning rather than an error:
    every other zone in the spec can still be planned.
    """
    have = {found.zone for found in clusters if found.state is not None}
    builder = ClusterBuilder(client)
    plans: dict[str, ClusterPlanned] = {}
    problems: dict[str, str] = {}

    for zone in spec.document.zones:
        if not zone.kubernetes.create or zone.name in have:
            continue
        try:
            plans[zone.name] = await builder.resolve(
                zone=zone.cloudstack_zone,
                cluster=zone.cks_cluster,
                version=zone.kubernetes.version,
                offering=zone.kubernetes.offering,
                size=zone.kubernetes.size,
                network_id=zone.kubernetes.network,
            )
        except ClusterUnbuildable as refused:
            problems[zone.name] = str(refused)

    return plans, problems


async def _proxy_provisioner(client: Any, spec: PlatformSpec, session: Any) -> Any:
    """The per-tenant proxy provisioner, where the zone configures one.

    Returns None when no zone sets `proxy_template` and
    `proxy_offering`: the planner warns about that and plans nothing,
    so there is nothing for a provisioner to do.
    """
    zone = spec.document.zones[0] if spec.document.zones else None
    if zone is None or not zone.proxy_template or not zone.proxy_offering:
        return None

    zones = await client.call_paged("listZones", "zone", name=zone.cloudstack_zone)
    templates = await client.call_paged(
        "listTemplates",
        "template",
        templatefilter="executable",
        name=zone.proxy_template,
    )
    offerings = await client.call_paged(
        "listServiceOfferings", "serviceoffering", name=zone.proxy_offering
    )
    if not (zones and templates and offerings):
        err.print(
            "[yellow]Private endpoints:[/yellow] the zone's proxy_template or "
            "proxy_offering was not found in CloudStack; proxies will not be built."
        )
        return None

    return ProxyProvisioner(
        client,
        zone_id=str(zones[0]["id"]),
        template_id=str(templates[0]["id"]),
        offering_id=str(offerings[0]["id"]),
        gateway_host=os.environ.get(
            "AISVC_GATEWAY_SERVICE", "aisvc-gateway.aisvc-serving.svc.cluster.local"
        ),
        engine_url=os.environ.get("AISVC_ENGINE_URL", ""),
        exposures_for=_exposures_for(session),
        mint_token=_token_minter(session),
        record_serial=_serial_recorder(session),
    )


def _serial_recorder(session: Any) -> Callable[[str, str, str], None]:
    """Remember which certificate serial a proxy was built from.

    Without it every proxy reads as current forever and a renewal
    never replaces one.
    """
    certificates = CertificateStore(session)
    exposures = ExposureStore(session)

    def record(deployment: str, account: str, certificate_id: str) -> None:
        certificate = certificates.get(certificate_id)
        if certificate is None:
            return
        for exposure in exposures.all():
            if (exposure.deployment, exposure.account) == (deployment, account):
                exposure.proxy_certificate_serial = certificate.serial or 1
        session.flush()

    return record


def _token_minter(session: Any) -> Callable[[str, str], str]:
    """Mints a proxy's one-time bootstrap token (SPEC.md section 8).

    Given to the provisioner rather than called here, so the token is
    minted as the VM is deployed: its half-hour starts at mint time,
    and one issued while a plan was still being reviewed would be spent
    before the VM booted.
    """
    tokens = BootstrapTokens(session)

    def mint(certificate_id: str, proxy: str) -> str:
        return tokens.issue(certificate_id=certificate_id, proxy=proxy).plaintext

    return mint


def _exposures_for(session: Any) -> Callable[[], list[Any]]:
    """How each deployment is exposed (SPEC.md section 8).

    Without this every deployment stays in the public gateway, so a
    tenant could switch public off and still be reachable.
    """
    store = ExposureStore(session)

    def lookup() -> list[Any]:
        return store.all()

    return lookup


def _adapters_for(session: Any) -> Callable[[str], list[dict[str, str]]]:
    """Which adapters each base should load (SPEC.md section 8)."""
    store = AdapterStore(session)

    def lookup(base_model: str) -> list[dict[str, str]]:
        return [
            {"name": record.name, "uri": record.uri}
            for record in store.all()
            if record.base_model == base_model and record.uri
        ]

    return lookup


async def _observe_health(models: set[str]) -> dict[str, DeploymentHealth]:
    """Read autoscaling signals, where Prometheus is configured.

    Without AISVC_PROMETHEUS_URL this returns nothing and the planner
    holds every deployment at its declared replica floor — which is what
    it did before anything filled this in. Autoscaling is opt-in on
    having somewhere to read the metrics from.
    """
    url = os.environ.get("AISVC_PROMETHEUS_URL")
    if not url or not models:
        return {}

    async with PrometheusClient(url) as prometheus:
        return await HealthObserver(prometheus).observe(sorted(models))


async def _apply(
    spec: PlatformSpec,
    computed: Plan,
    *,
    zone: str,
    dry_run: bool,
    registry: ModelRegistry,
    cluster_plans: dict[str, ClusterPlanned] | None = None,
) -> tuple[int, str]:
    """Execute a plan, returning the run id and its status."""
    credentials = _credentials()
    join = _cluster_join()
    namespace = os.environ.get("AISVC_NAMESPACE", DEFAULT_NAMESPACE)

    upgrade_to_head(database_url())
    factory = create_session_factory()
    engine = factory.kw["bind"]

    # A dry run writes nothing, so it neither needs the lock nor should
    # take it: holding the zone while printing a plan would stop a real
    # apply from running.
    with ZoneLeadership(engine, zone=zone, required=not dry_run):
        async with CloudStackClient(credentials) as client:
            provisioner = NodeProvisioner(client, spec, join=join)
            # Fetched again here rather than carried from observe: a
            # cluster this run is about to create has no kubeconfig at
            # observe time, and this is where it first does.
            kubeconfig = await _kubeconfig_from_cloudstack(client, spec)
            with session_scope(factory) as session:
                # Adapters come from the database, so the deployer is
                # built inside the session: without this the LoRA args
                # are never rendered and an uploaded adapter would be
                # routed to a process that never loaded it.
                deployer = ServingDeployer(
                    _cluster(kubeconfig),
                    spec,
                    registry,
                    namespace=namespace,
                    adapters_for=_adapters_for(session),
                    exposures_for=_exposures_for(session),
                )
                executor = Executor(
                    provisioner,
                    RunLog(session),
                    deployer=deployer,
                    proxies=await _proxy_provisioner(client, spec, session),
                    clusters=ClusterBuilder(client),
                )
                # What observe resolved, so execution builds exactly
                # the line the operator confirmed.
                executor.cluster_plans = {
                    planned.cluster: planned for planned in (cluster_plans or {}).values()
                }
                run = await executor.execute(computed, zone=zone, dry_run=dry_run)
                if not dry_run:
                    # Spent and expired tokens are useless but still
                    # rows naming a certificate; there is no reason to
                    # keep them, and this is the run that created them.
                    BootstrapTokens(session).sweep()
                    # Nothing else runs often enough to notice an
                    # expiry: a certificate does not degrade, it stops.
                    await sweep_renewals(session, sink=LogSink())
                    # ACME certificates have no record here — certbot
                    # issues and renews them on the proxy — so the only
                    # way to know a renewal failed is to look at what
                    # the endpoint is serving.
                    await sweep_endpoint_certificates(session, sink=LogSink())
                return run.id, run.status.value


def _observe_or_empty(spec: PlatformSpec, *, observe: bool) -> ActualState:
    """Observe the world, or stand in an empty one for --no-observe."""
    if not observe:
        out.print("[dim]--no-observe: planning against an empty platform.[/dim]\n")
        return ActualState.empty_for(spec)
    try:
        return asyncio.run(_observe(spec))
    except CloudStackError as exc:
        err.print(f"[bold red]CloudStack call failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command()
def validate(spec: SpecOption = Path("platform.yaml"), licenses: LicensesOption = None) -> None:
    """Check platform.yaml against every rule in SPEC.md section 3."""
    loaded = _load(spec, licenses)
    _show_findings(loaded)
    out.print(
        f"[green]{spec} is valid[/green]: "
        f"{len(loaded.document.zones)} zone(s), "
        f"{len(loaded.document.gpu_classes)} GPU class(es), "
        f"{len(loaded.document.node_pools)} node pool(s), "
        f"{len(loaded.document.models)} model(s), "
        f"{len(loaded.document.tenants)} tenant(s)."
    )


@app.command()
def plan(
    spec: SpecOption = Path("platform.yaml"),
    licenses: LicensesOption = None,
    observe: Annotated[
        bool,
        typer.Option(
            "--observe/--no-observe",
            help="Read live CloudStack capacity; --no-observe plans against nothing.",
        ),
    ] = True,
) -> None:
    """Print the plan to converge CloudStack onto platform.yaml."""
    loaded = _load(spec, licenses)

    state = _observe_or_empty(loaded, observe=observe)
    computed: Plan = plan_all(loaded, state, registry=_registry(observe=observe))
    out.print(computed.render())
    out.print("\n[dim]plan is read-only; nothing was changed.[/dim]")


@app.command()
def apply(
    spec: SpecOption = Path("platform.yaml"),
    licenses: LicensesOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan and record the run, but write nothing."),
    ] = False,
    observe: Annotated[
        bool,
        typer.Option("--observe/--no-observe", help="Read live CloudStack capacity."),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Do not ask before making changes."),
    ] = False,
) -> None:
    """Converge CloudStack onto platform.yaml."""
    loaded = _load(spec, licenses)
    _show_findings(loaded)

    # Fail on missing configuration before planning, so an operator is never
    # asked to confirm changes that could not have been applied anyway.
    _credentials()
    _cluster_join()
    # A typo in the registry switch should fail here rather than after
    # the plan has been printed and confirmed.
    _registry_from_cloudstack()

    state = _observe_or_empty(loaded, observe=observe)
    # Resolved once, here in the synchronous body: with
    # AISVC_REGISTRY_FROM_CLOUDSTACK on this makes an API call, and the
    # execution below runs inside an event loop that cannot start another.
    registry = _registry(observe=observe)
    computed = plan_all(loaded, state, registry=registry)
    out.print(computed.render())

    if not computed.has_changes:
        out.print("\n[green]Nothing to do.[/green]")
        return

    if dry_run:
        out.print("\n[yellow]Dry run:[/yellow] nothing will be written.")
    elif not yes and not typer.confirm("\nApply these changes?"):
        err.print("Aborted; nothing was changed.")
        raise typer.Exit(code=1)

    zone = loaded.document.zones[0].name if loaded.document.zones else "unknown"
    try:
        run_id, status = asyncio.run(
            _apply(
                loaded,
                computed,
                zone=zone,
                dry_run=dry_run,
                registry=registry,
                cluster_plans=state.cluster_plans,
            )
        )
    except NotLeader as exc:
        # Not an error in the zone, just someone else's turn. Saying so
        # plainly stops an operator debugging a problem that is not one.
        err.print(
            f"[bold yellow]Another engine replica is reconciling {zone}.[/bold yellow]\n"
            "Nothing was changed. Try again once its run finishes."
        )
        raise typer.Exit(code=1) from exc
    except CloudStackError as exc:
        err.print(f"[bold red]CloudStack call failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    colour = "green" if status == "succeeded" else "red"
    out.print(f"\n[{colour}]Run {run_id} {status}.[/{colour}]")
    if status != "succeeded":
        raise typer.Exit(code=1)


def _engine() -> EngineClient:
    """The engine client, or a clear error if it is not configured."""
    client = EngineClient.from_env()
    if client is None:
        err.print(
            "[bold red]No engine configured:[/bold red] set AISVC_ENGINE_URL "
            "to the engine's base URL (and AISVC_TLS_CERT / AISVC_TLS_KEY for mTLS)."
        )
        raise typer.Exit(code=1)
    return client


@app.command()
def batch(
    once: Annotated[
        bool,
        typer.Option("--once", help="Run one pass and exit, rather than looping."),
    ] = False,
    interval: Annotated[
        int,
        typer.Option("--interval", help="Seconds to wait between passes."),
    ] = 30,
) -> None:
    """Run queued batches on spare capacity (SPEC.md section 8).

    Stands down whenever online traffic is waiting: that yielding is
    what makes the batch discount defensible, so it is checked before
    every pass rather than once at startup.

    Needs AISVC_DATABASE_URL, the registry settings and
    AISVC_GATEWAY_URL. Without AISVC_PROMETHEUS_URL it cannot see
    online load and runs batches unconditionally, which is the wrong
    thing on a busy platform — it says so and carries on, because a
    worker that silently refused to work would look broken.
    """
    gateway = os.environ.get("AISVC_GATEWAY_URL")
    if not gateway:
        err.print(
            "[bold red]No gateway configured:[/bold red] set AISVC_GATEWAY_URL "
            "to the OpenAI-compatible base URL batches should be sent to."
        )
        raise typer.Exit(code=1)

    if not os.environ.get("AISVC_PROMETHEUS_URL"):
        out.print(
            "[yellow]No AISVC_PROMETHEUS_URL:[/yellow] online load cannot be seen, "
            "so batches will not stand down for tenant traffic.\n"
        )

    loaded = _load(Path(os.environ.get("AISVC_SPEC", "platform.yaml")), None)
    asyncio.run(_batch_worker(gateway, loaded, once=once, interval=interval, registry=_registry()))


def _batch_meter(session: Any, spec: PlatformSpec) -> Any:
    """A meter that bills at the batch rate (SPEC.md section 8)."""
    tiers = {t.name: t for t in spec.document.price_tiers}
    discount = next(
        (m.batch.discount for m in spec.document.models if m.batch.enabled),
        DEFAULT_DISCOUNT,
    )

    def tier_for(model_name: str) -> Any:
        model = next((m for m in spec.document.models if m.name == model_name), None)
        tier = tiers.get(model.price_tier) if model and model.price_tier else None
        return batch_tier(tier, discount=discount) if tier else None

    return UsageMeter(UsageStore(session), tier_for=tier_for, sink=LogSink())


async def _batch_worker(
    gateway: str,
    spec: PlatformSpec,
    *,
    once: bool,
    interval: int,
    registry: ModelRegistry,
) -> None:
    """One pass, or a loop of them."""
    upgrade_to_head(database_url())
    factory = create_session_factory()

    while True:
        health = await _observe_health(set())
        with session_scope(factory) as session:
            runner = BatchRunner(
                BatchStore(session),
                registry,
                complete=_gateway_completion(gateway),
                should_preempt=preemption_signal(health),
                meter=_batch_meter(session, spec),
            )
            outcome = await runner.run_once()
            swept = runner.sweep_retention()

        if outcome is not Outcome.IDLE or swept:
            out.print(f"batch pass: {outcome.value}, {swept} result set(s) swept")

        if once:
            return
        await asyncio.sleep(interval)


def _gateway_completion(base_url: str) -> Any:
    """Send one batch request through the gateway.

    Through the gateway rather than straight to a model: a batch is
    still a tenant's traffic and has to meet the same profiles,
    guardrails and key scopes as anything else.
    """

    async def complete(request: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}{request.url}",
                json=request.body,
                headers={"Authorization": f"Bearer {os.environ.get('AISVC_BATCH_KEY', '')}"},
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            return body

    return complete


@app.command()
def status() -> None:
    """Show what the engine is serving, through its API.

    Goes through the same REST surface the Java plugin uses, so a
    response shape that would break the plugin breaks this too.
    """
    engine = _engine()
    try:
        health = engine.get("/v1/health")
        models = engine.get("/v1/models")
        deployments = engine.get("/v1/deployments")
    except EngineError as exc:
        err.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc

    out.print(f"engine: [green]{health.get('status', 'unknown')}[/green]")
    out.print(f"catalog: {models['count']} model(s)")

    if not deployments["count"]:
        out.print("deployments: [yellow]none running[/yellow]")
        return

    out.print("deployments:")
    for deployment in deployments["deployments"]:
        state = "[green]ready[/green]" if deployment["ready"] else "[yellow]starting[/yellow]"
        out.print(
            f"  {deployment['model']} ({deployment['colour']}) "
            f"{deployment['replicas_ready']}/{deployment['replicas_desired']} {state}"
        )


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to bind.")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8443,
) -> None:
    """Run the engine REST API.

    Without AISVC_TLS_* set it binds loopback only: the engine trusts the
    caller context its clients assert, and mTLS is what earns that trust.
    """
    from aisvc.api.server import TlsConfigError, run

    try:
        run(host=host, port=port)
    except TlsConfigError as exc:
        err.print(f"[bold red]TLS is misconfigured:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":  # pragma: no cover
    app()
