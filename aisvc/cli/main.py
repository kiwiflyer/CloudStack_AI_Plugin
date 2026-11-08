"""`aisvc` operator CLI (SPEC.md section 4).

Milestone 1 ships `validate` and `plan`. Both are strictly read-only;
`apply` arrives with milestone 2.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from aisvc.api.client import EngineClient, EngineError
from aisvc.cloudstack import CloudStackClient, CloudStackError, Credentials
from aisvc.db import Base, RunLog, create_session_factory, session_scope
from aisvc.inventory import take_snapshot
from aisvc.k8s import DEFAULT_NAMESPACE
from aisvc.k8s.client import (
    ClusterKubernetes,
    observe_deployments,
    observe_route_hash,
    observe_weights,
)
from aisvc.provision import ClusterJoin, NodeProvisioner
from aisvc.reconciler import ActualState, Executor, Plan, plan_all
from aisvc.reconciler.deployer import ServingDeployer
from aisvc.reconciler.leader import NotLeader, ZoneLeadership
from aisvc.registry import InMemoryObjectStore, ModelRegistry, S3ObjectStore
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


def _registry(*, observe: bool = True) -> ModelRegistry:
    """The artifact registry, from the environment.

    With `--no-observe` nothing should leave the machine, so the plan is
    drawn against an empty registry and every model reads as unmirrored.
    """
    if not observe:
        return ModelRegistry(InMemoryObjectStore(), bucket="models")

    bucket = os.environ.get("AISVC_REGISTRY_BUCKET", "models")
    endpoint = os.environ.get("AISVC_REGISTRY_ENDPOINT") or None
    store = S3ObjectStore(bucket=bucket, endpoint_url=endpoint)
    return ModelRegistry(store, bucket=bucket)


def _cluster() -> ClusterKubernetes:
    return ClusterKubernetes(kubeconfig=os.environ.get("KUBECONFIG") or None)


async def _observe(spec: PlatformSpec) -> ActualState:
    """Read CloudStack capacity and the cluster's serving workloads."""
    credentials = _credentials()
    namespace = os.environ.get("AISVC_NAMESPACE", DEFAULT_NAMESPACE)

    async with CloudStackClient(credentials) as client:
        capacity = await take_snapshot(client, spec)

    cluster = _cluster()
    return ActualState(
        capacity=capacity,
        deployments=observe_deployments(cluster, namespace=namespace),
        route_hash=observe_route_hash(cluster, namespace=namespace),
        weights=observe_weights(cluster, namespace=namespace),
    )


async def _apply(
    spec: PlatformSpec, computed: Plan, *, zone: str, dry_run: bool
) -> tuple[int, str]:
    """Execute a plan, returning the run id and its status."""
    credentials = _credentials()
    join = _cluster_join()
    namespace = os.environ.get("AISVC_NAMESPACE", DEFAULT_NAMESPACE)

    factory = create_session_factory()
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)

    # A dry run writes nothing, so it neither needs the lock nor should
    # take it: holding the zone while printing a plan would stop a real
    # apply from running.
    with ZoneLeadership(engine, zone=zone, required=not dry_run):
        async with CloudStackClient(credentials) as client:
            provisioner = NodeProvisioner(client, spec, join=join)
            deployer = ServingDeployer(_cluster(), spec, _registry(), namespace=namespace)
            with session_scope(factory) as session:
                executor = Executor(provisioner, RunLog(session), deployer=deployer)
                run = await executor.execute(computed, zone=zone, dry_run=dry_run)
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

    state = _observe_or_empty(loaded, observe=observe)
    computed = plan_all(loaded, state, registry=_registry(observe=observe))
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
        run_id, status = asyncio.run(_apply(loaded, computed, zone=zone, dry_run=dry_run))
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
