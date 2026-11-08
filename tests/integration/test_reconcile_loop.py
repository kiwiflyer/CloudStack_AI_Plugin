"""The reconcile loop end to end (SPEC.md section 12).

Spec -> plan -> execute -> observe -> plan again, against a CloudStack
whose state actually changes when the executor writes to it. Covers the
two named cases: `test_second_apply_produces_empty_plan` and
`test_dry_run_makes_no_api_writes`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aisvc.cloudstack import CloudStackClient
from aisvc.db import Base, RunLog, RunStatus
from aisvc.inventory import take_snapshot
from aisvc.provision import ClusterJoin, NodeProvisioner
from aisvc.reconciler import Executor, plan_capacity
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.conftest import CREDENTIALS
from tests.integration.fake_cloudstack import FakeCloudStack
from tests.unit.test_spec import LICENSES, MINIMAL

JOIN = ClusterJoin(
    endpoint="10.0.0.10:6443",
    token="abcdef.0123456789abcdef",
    ca_cert_hash="sha256:deadbeef",
)

WRITE_COMMANDS = frozenset(
    {
        "deployVirtualMachine",
        "destroyVirtualMachine",
        "createAffinityGroup",
        "scaleKubernetesCluster",
    }
)


def spec_with(min_nodes: int = 2, max_nodes: int = 6) -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    doc["nodePools"][0]["min_nodes"] = min_nodes
    doc["nodePools"][0]["max_nodes"] = max_nodes
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        yield s


@pytest.fixture
def fake() -> FakeCloudStack:
    return FakeCloudStack()


def client_for(fake: FakeCloudStack) -> CloudStackClient:
    return CloudStackClient(
        CREDENTIALS,
        transport=fake.transport(),
        poll_interval=0.0,
        backoff_base=0.0,
    )


async def reconcile(
    fake: FakeCloudStack,
    session: Session,
    spec: PlatformSpec,
    *,
    dry_run: bool = False,
) -> tuple[object, int]:
    """One full observe -> plan -> execute cycle. Returns (run, actions planned)."""
    async with client_for(fake) as client:
        snapshot = await take_snapshot(client, spec)
        plan = plan_capacity(spec, snapshot)
        executor = Executor(NodeProvisioner(client, spec, join=JOIN), RunLog(session))
        run = await executor.execute(plan, zone="zone-east-1", dry_run=dry_run)
        return run, len(plan.actions)


class TestConvergence:
    async def test_apply_builds_the_pool_the_spec_declares(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        spec = spec_with(min_nodes=2)

        run, planned = await reconcile(fake, session, spec)

        assert planned == 1
        assert run.status is RunStatus.SUCCEEDED  # type: ignore[attr-defined]
        assert sorted(vm["name"] for vm in fake.gpu_vms()) == [
            "aisvc-h100-pool-001",
            "aisvc-h100-pool-002",
        ]

    async def test_second_apply_produces_empty_plan(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        """The reconciler is idempotent (SPEC.md section 12)."""
        spec = spec_with(min_nodes=2)
        await reconcile(fake, session, spec)

        fake.calls.clear()
        _, planned = await reconcile(fake, session, spec)

        assert planned == 0
        assert WRITE_COMMANDS.isdisjoint(fake.commands())

    async def test_a_third_apply_is_still_empty(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        spec = spec_with(min_nodes=3)
        await reconcile(fake, session, spec)
        await reconcile(fake, session, spec)

        _, planned = await reconcile(fake, session, spec)

        assert planned == 0
        assert len(fake.gpu_vms()) == 3

    async def test_raising_the_minimum_adds_only_the_missing_nodes(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        await reconcile(fake, session, spec_with(min_nodes=2))

        await reconcile(fake, session, spec_with(min_nodes=4))

        assert len(fake.gpu_vms()) == 4
        assert sorted(vm["name"] for vm in fake.gpu_vms())[-1] == "aisvc-h100-pool-004"

    async def test_lowering_the_maximum_removes_the_excess(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        await reconcile(fake, session, spec_with(min_nodes=4))

        await reconcile(fake, session, spec_with(min_nodes=1, max_nodes=2))

        assert len(fake.gpu_vms()) == 2

    async def test_converging_down_then_up_settles(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        await reconcile(fake, session, spec_with(min_nodes=4))
        await reconcile(fake, session, spec_with(min_nodes=1, max_nodes=2))
        await reconcile(fake, session, spec_with(min_nodes=3))

        _, planned = await reconcile(fake, session, spec_with(min_nodes=3))

        assert planned == 0
        assert len(fake.gpu_vms()) == 3


class TestDryRun:
    async def test_dry_run_makes_no_api_writes(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        """--dry-run is safe (SPEC.md section 12)."""
        spec = spec_with(min_nodes=3)

        run, planned = await reconcile(fake, session, spec, dry_run=True)

        assert planned == 1
        assert run.status is RunStatus.SUCCEEDED  # type: ignore[attr-defined]
        assert WRITE_COMMANDS.isdisjoint(fake.commands())
        assert fake.gpu_vms() == []

    async def test_a_dry_run_leaves_the_next_plan_unchanged(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        spec = spec_with(min_nodes=3)
        _, first = await reconcile(fake, session, spec, dry_run=True)

        _, second = await reconcile(fake, session, spec, dry_run=True)

        assert first == second == 1

    async def test_a_dry_run_still_reads_live_state(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        """Dry run is worthless if it plans against nothing."""
        spec = spec_with(min_nodes=3)

        await reconcile(fake, session, spec, dry_run=True)

        assert "listVirtualMachines" in fake.commands()

    async def test_a_dry_run_is_recorded_in_the_run_log(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        spec = spec_with(min_nodes=3)
        await reconcile(fake, session, spec, dry_run=True)

        runs = RunLog(session).recent_runs()

        assert len(runs) == 1
        assert runs[0].dry_run is True
        assert "h100-pool" in runs[0].plan_text


class TestAuditTrail:
    async def test_every_run_is_recorded_with_its_plan(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        spec = spec_with(min_nodes=2)
        await reconcile(fake, session, spec)
        await reconcile(fake, session, spec)

        runs = RunLog(session).recent_runs()

        assert len(runs) == 2
        assert runs[1].actions[0].result  # the first run created nodes
        assert runs[0].actions == []  # the second had nothing to do

    async def test_a_failed_deploy_is_recorded_against_the_action(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        fake.fail_on = "deployVirtualMachine"

        run, _ = await reconcile(fake, session, spec_with(min_nodes=2))

        assert run.status is RunStatus.FAILED  # type: ignore[attr-defined]
        assert "scripted failure" in (run.actions[0].error or "")  # type: ignore[attr-defined]

    async def test_a_failed_run_leaves_the_next_plan_asking_for_the_same_work(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        fake.fail_on = "deployVirtualMachine"
        await reconcile(fake, session, spec_with(min_nodes=2))

        fake.fail_on = None
        run, planned = await reconcile(fake, session, spec_with(min_nodes=2))

        assert planned == 1
        assert run.status is RunStatus.SUCCEEDED  # type: ignore[attr-defined]
        assert len(fake.gpu_vms()) == 2


class TestAntiAffinity:
    async def test_the_pool_gets_one_anti_affinity_group(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        await reconcile(fake, session, spec_with(min_nodes=3))

        assert [g["name"] for g in fake.affinity_groups] == ["aisvc-h100-pool-anti-affinity"]

    async def test_the_group_is_not_recreated_on_later_runs(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        await reconcile(fake, session, spec_with(min_nodes=2))
        await reconcile(fake, session, spec_with(min_nodes=4))

        assert fake.commands().count("createAffinityGroup") == 1

    async def test_every_node_is_deployed_into_the_group(
        self, fake: FakeCloudStack, session: Session
    ) -> None:
        await reconcile(fake, session, spec_with(min_nodes=3))

        groups = {p["affinitygroupids"] for p in fake.params_for("deployVirtualMachine")}
        assert groups == {"ag-1"}
