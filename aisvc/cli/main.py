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

from aisvc.cloudstack import CloudStackClient, CloudStackError, Credentials
from aisvc.db import Base, RunLog, create_session_factory, session_scope
from aisvc.inventory import CapacitySnapshot, take_snapshot
from aisvc.provision import ClusterJoin, NodeProvisioner
from aisvc.reconciler import Executor, Plan, plan_capacity
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


async def _observe(spec: PlatformSpec) -> CapacitySnapshot:
    credentials = _credentials()
    async with CloudStackClient(credentials) as client:
        return await take_snapshot(client, spec)


async def _apply(
    spec: PlatformSpec, computed: Plan, *, zone: str, dry_run: bool
) -> tuple[int, str]:
    """Execute a plan, returning the run id and its status."""
    credentials = _credentials()
    join = _cluster_join()

    factory = create_session_factory()
    Base.metadata.create_all(factory.kw["bind"])

    async with CloudStackClient(credentials) as client:
        provisioner = NodeProvisioner(client, spec, join=join)
        with session_scope(factory) as session:
            executor = Executor(provisioner, RunLog(session))
            run = await executor.execute(computed, zone=zone, dry_run=dry_run)
            return run.id, run.status.value


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

    if observe:
        try:
            snapshot = asyncio.run(_observe(loaded))
        except CloudStackError as exc:
            err.print(f"[bold red]CloudStack call failed:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc
    else:
        snapshot = CapacitySnapshot.empty_for(loaded)
        out.print("[dim]--no-observe: planning against an empty platform.[/dim]\n")

    computed: Plan = plan_capacity(loaded, snapshot)
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

    if observe:
        try:
            snapshot = asyncio.run(_observe(loaded))
        except CloudStackError as exc:
            err.print(f"[bold red]CloudStack call failed:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc
    else:
        snapshot = CapacitySnapshot.empty_for(loaded)

    computed = plan_capacity(loaded, snapshot)
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
    except CloudStackError as exc:
        err.print(f"[bold red]CloudStack call failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    colour = "green" if status == "succeeded" else "red"
    out.print(f"\n[{colour}]Run {run_id} {status}.[/{colour}]")
    if status != "succeeded":
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
