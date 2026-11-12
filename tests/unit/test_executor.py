"""Executing a plan (SPEC.md section 4).

"Execute actions in dependency order with per-action timeouts; a failed
action stops its dependants only." Every run is recorded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aisvc.db import ActionOutcome, Base, RunLog, RunStatus
from aisvc.provision import GpuNode, ProvisionError
from aisvc.reconciler import Action, ActionKind, Executor, Plan


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        yield s


@pytest.fixture
def log(session: Session) -> RunLog:
    return RunLog(session)


class FakeProvisioner:
    """Records calls; can be told to fail or hang for a given pool."""

    def __init__(self) -> None:
        self.created: list[tuple[str, int, bool]] = []
        self.destroyed: list[tuple[str, int, bool]] = []
        self.fail_pools: set[str] = set()
        self.hang_pools: set[str] = set()

    async def create_nodes(self, pool: str, count: int, *, dry_run: bool = False) -> list[GpuNode]:
        if pool in self.hang_pools:
            await asyncio.sleep(10)
        if pool in self.fail_pools:
            raise ProvisionError(f"could not deploy into {pool}")
        self.created.append((pool, count, dry_run))
        return [
            GpuNode(id=f"vm-{i}", name=f"aisvc-{pool}-{i:03d}", pool=pool, ordinal=i)
            for i in range(1, count + 1)
        ]

    async def destroy_nodes(self, pool: str, count: int, *, dry_run: bool = False) -> list[GpuNode]:
        if pool in self.fail_pools:
            raise ProvisionError(f"could not destroy in {pool}")
        self.destroyed.append((pool, count, dry_run))
        return []


@pytest.fixture
def provisioner() -> FakeProvisioner:
    return FakeProvisioner()


def executor(provisioner: FakeProvisioner, log: RunLog, **kw: object) -> Executor:
    return Executor(provisioner, log, **kw)  # type: ignore[arg-type]


def create(pool: str, count: int = 1, **kw: object) -> Action:
    return Action(ActionKind.CREATE, "gpu-node", pool, count=count, **kw)  # type: ignore[arg-type]


class TestSuccessfulRun:
    async def test_a_create_action_provisions_nodes(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        plan = Plan(actions=[create("h100-pool", 2)])

        run = await executor(provisioner, log).execute(plan, zone="zone-east-1")

        assert provisioner.created == [("h100-pool", 2, False)]
        assert run.status is RunStatus.SUCCEEDED

    async def test_a_delete_action_removes_nodes(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        plan = Plan(actions=[Action(ActionKind.DELETE, "gpu-node", "l40s-pool", count=3)])

        await executor(provisioner, log).execute(plan, zone="z")

        assert provisioner.destroyed == [("l40s-pool", 3, False)]

    async def test_the_run_records_what_each_action_did(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        plan = Plan(actions=[create("h100-pool", 2)])

        run = await executor(provisioner, log).execute(plan, zone="z")

        assert run.actions[0].outcome is ActionOutcome.SUCCEEDED
        assert "aisvc-h100-pool-001" in (run.actions[0].result or "")

    async def test_an_empty_plan_succeeds_without_calling_anything(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        run = await executor(provisioner, log).execute(Plan(), zone="z")

        assert run.status is RunStatus.SUCCEEDED
        assert provisioner.created == []


class TestDryRun:
    async def test_dry_run_passes_the_flag_down(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        plan = Plan(actions=[create("h100-pool", 2)])

        await executor(provisioner, log).execute(plan, zone="z", dry_run=True)

        assert provisioner.created == [("h100-pool", 2, True)]

    async def test_the_run_is_marked_as_a_dry_run(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        run = await executor(provisioner, log).execute(
            Plan(actions=[create("h100-pool")]), zone="z", dry_run=True
        )

        assert run.dry_run is True
        assert run.status is RunStatus.SUCCEEDED


class TestFailures:
    async def test_a_failed_action_fails_the_run(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        provisioner.fail_pools = {"h100-pool"}
        plan = Plan(actions=[create("h100-pool")])

        run = await executor(provisioner, log).execute(plan, zone="z")

        assert run.status is RunStatus.FAILED
        assert run.actions[0].outcome is ActionOutcome.FAILED
        assert "could not deploy" in (run.actions[0].error or "")

    async def test_an_independent_action_still_runs_after_a_failure(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        """A failed action stops its dependants only."""
        provisioner.fail_pools = {"bad-pool"}
        plan = Plan(actions=[create("bad-pool"), create("good-pool", 2)])

        run = await executor(provisioner, log).execute(plan, zone="z")

        assert provisioner.created == [("good-pool", 2, False)]
        assert run.actions[0].outcome is ActionOutcome.FAILED
        assert run.actions[1].outcome is ActionOutcome.SUCCEEDED

    async def test_a_dependant_of_a_failure_is_skipped(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        provisioner.fail_pools = {"bad-pool"}
        first = create("bad-pool")
        second = create("downstream-pool", depends_on=(first.key,))
        plan = Plan(actions=[first, second])

        run = await executor(provisioner, log).execute(plan, zone="z")

        assert provisioner.created == []
        assert run.actions[1].outcome is ActionOutcome.SKIPPED
        assert "bad-pool" in (run.actions[1].error or "")

    async def test_skipping_cascades_down_the_chain(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        provisioner.fail_pools = {"a"}
        first = create("a")
        second = create("b", depends_on=(first.key,))
        third = create("c", depends_on=(second.key,))
        plan = Plan(actions=[first, second, third])

        run = await executor(provisioner, log).execute(plan, zone="z")

        assert [a.outcome for a in run.actions] == [
            ActionOutcome.FAILED,
            ActionOutcome.SKIPPED,
            ActionOutcome.SKIPPED,
        ]

    async def test_an_action_that_overruns_its_timeout_fails(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        provisioner.hang_pools = {"slow-pool"}
        plan = Plan(actions=[create("slow-pool")])

        run = await executor(provisioner, log, action_timeout=0.05).execute(plan, zone="z")

        assert run.actions[0].outcome is ActionOutcome.FAILED
        assert "timed out" in (run.actions[0].error or "")

    async def test_a_timeout_does_not_stop_independent_actions(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        provisioner.hang_pools = {"slow-pool"}
        plan = Plan(actions=[create("slow-pool"), create("quick-pool")])

        await executor(provisioner, log, action_timeout=0.05).execute(plan, zone="z")

        assert provisioner.created == [("quick-pool", 1, False)]

    async def test_an_unknown_resource_type_fails_that_action_only(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        plan = Plan(
            actions=[
                Action(ActionKind.CREATE, "mystery-resource", "x"),
                create("good-pool"),
            ]
        )

        run = await executor(provisioner, log).execute(plan, zone="z")

        assert run.actions[0].outcome is ActionOutcome.FAILED
        assert "mystery-resource" in (run.actions[0].error or "")
        assert run.actions[1].outcome is ActionOutcome.SUCCEEDED


class TestOrdering:
    async def test_dependencies_run_before_their_dependants(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        first = create("first")
        second = create("second", depends_on=(first.key,))
        # Listed out of order on purpose.
        plan = Plan(actions=[second, first])

        await executor(provisioner, log).execute(plan, zone="z")

        assert [pool for pool, _, _ in provisioner.created] == ["first", "second"]

    async def test_a_dependency_cycle_is_reported_not_hung(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        a = Action(ActionKind.CREATE, "gpu-node", "a", depends_on=("gpu-node:b:create",))
        b = Action(ActionKind.CREATE, "gpu-node", "b", depends_on=("gpu-node:a:create",))

        run = await executor(provisioner, log).execute(Plan(actions=[a, b]), zone="z")

        assert run.status is RunStatus.FAILED
        assert "cycle" in (run.error or "").lower()

    async def test_a_dependency_on_a_missing_action_is_reported(
        self, provisioner: FakeProvisioner, log: RunLog
    ) -> None:
        plan = Plan(actions=[create("a", depends_on=("gpu-node:ghost:create",))])

        run = await executor(provisioner, log).execute(plan, zone="z")

        assert run.status is RunStatus.FAILED
        assert "ghost" in (run.error or "")


class TestActionKey:
    def test_the_key_identifies_the_action(self) -> None:
        assert create("h100-pool").key == "gpu-node:h100-pool:create"

    def test_create_and_delete_of_one_resource_are_different_actions(self) -> None:
        a = Action(ActionKind.CREATE, "gpu-node", "p")
        b = Action(ActionKind.DELETE, "gpu-node", "p")
        assert a.key != b.key


class TestCreatingAKubernetesCluster:
    """ADR-0007: a zone that asks for a cluster gets one, once.

    The handler is given what the planner resolved, so execution does
    not re-choose a version or an offering between plan and apply —
    an operator confirmed a specific line, and getting something else
    would make the plan a suggestion rather than a contract.
    """

    async def test_it_creates_what_the_plan_said(self, log: RunLog) -> None:
        builder = FakeClusterBuilder()
        record = await Executor(None, log, clusters=builder).execute(
            _cluster_plan(), zone="zone-east-1"
        )

        assert builder.created == [("aisvc-east-1", "1.30.1", "Small Instance")]
        assert record.status is RunStatus.SUCCEEDED

    async def test_a_dry_run_creates_nothing(self, log: RunLog) -> None:
        builder = FakeClusterBuilder()
        await Executor(None, log, clusters=builder).execute(
            _cluster_plan(), zone="zone-east-1", dry_run=True
        )

        assert builder.created == []

    async def test_without_a_builder_the_action_fails_rather_than_passing(
        self, log: RunLog
    ) -> None:
        # A plan that prints a cluster and quietly does nothing is
        # worse than one that stops: every GPU node after it depends
        # on the cluster, and the operator would believe it was built.
        record = await Executor(None, log).execute(_cluster_plan(), zone="zone-east-1")

        assert record.status is not RunStatus.SUCCEEDED


def _cluster_plan() -> Plan:
    from aisvc.cloudstack.clusters import ClusterPlanned

    planned = ClusterPlanned(
        zone="East-1",
        zone_id="z-east",
        cluster="aisvc-east-1",
        version="1.30.1",
        version_id="v-130",
        offering="Small Instance",
        offering_id="o-small",
    )
    return Plan(
        actions=[
            Action(
                kind=ActionKind.CREATE,
                resource_type="kubernetes-cluster",
                name="aisvc-east-1",
                detail=planned.detail,
            )
        ],
        notes=[],
    )


class FakeClusterBuilder:
    """Stands in for ClusterBuilder, recording what it was asked to build."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []

    async def resolve(self, **kwargs: object):  # type: ignore[no-untyped-def]
        from aisvc.cloudstack.clusters import ClusterPlanned

        return ClusterPlanned(
            zone="East-1",
            zone_id="z-east",
            cluster=str(kwargs["cluster"]),
            version="1.30.1",
            version_id="v-130",
            offering="Small Instance",
            offering_id="o-small",
        )

    async def create(self, planned: object, *, dry_run: bool) -> str:
        if dry_run:
            return ""
        self.created.append(
            (planned.cluster, planned.version, planned.offering)  # type: ignore[attr-defined]
        )
        return str(planned.cluster)  # type: ignore[attr-defined]
