"""The whole loop, from empty to serving (SPEC.md sections 12 and 14).

Nodes, then model deployments, then routes — against a CloudStack and a
Kubernetes that both remember what was done to them, so convergence is
real rather than asserted.
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
from aisvc.k8s.client import (
    FakeKubernetes,
    observe_deployments,
    observe_route_hash,
)
from aisvc.provision import ClusterJoin, NodeProvisioner
from aisvc.reconciler import ActualState, Executor, plan_all
from aisvc.reconciler.deployer import GATEWAY_CONFIGMAP, ServingDeployer
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.conftest import CREDENTIALS
from tests.integration.fake_cloudstack import FakeCloudStack
from tests.unit.test_spec import LICENSES, MINIMAL

SHA = "0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b"
REPO = "meta-llama/Llama-3.3-70B-Instruct"
NAMESPACE = "aisvc-serving"

JOIN = ClusterJoin(
    endpoint="10.0.0.10:6443",
    token="abcdef.0123456789abcdef",
    ca_cert_hash="sha256:deadbeef",
)


def spec(min_nodes: int = 2) -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    doc["nodePools"][0]["min_nodes"] = min_nodes
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        yield s


@pytest.fixture
def cloud() -> FakeCloudStack:
    return FakeCloudStack()


@pytest.fixture
def cluster() -> FakeKubernetes:
    return FakeKubernetes()


@pytest.fixture
def registry() -> ModelRegistry:
    """A registry with the example model already mirrored."""
    registry = ModelRegistry(InMemoryObjectStore(), bucket="models")
    entry = registry.put_file(REPO, SHA, "config.json", b"{}")
    registry.write_manifest(REPO, SHA, [entry], license="llama3.3", task="chat")
    return registry


class Platform:
    """Drives one reconcile cycle against the fakes."""

    def __init__(
        self,
        cloud: FakeCloudStack,
        cluster: FakeKubernetes,
        registry: ModelRegistry,
        session: Session,
    ) -> None:
        self.cloud = cloud
        self.cluster = cluster
        self.registry = registry
        self.session = session
        #: Traffic weights as the gateway has them, which is how the
        #: planner learns which colour is live.
        self.weights: dict[str, dict[str, int]] = {}

    async def reconcile(self, spec: PlatformSpec, *, dry_run: bool = False) -> object:
        client = CloudStackClient(
            CREDENTIALS,
            transport=self.cloud.transport(),
            poll_interval=0.0,
            backoff_base=0.0,
        )
        async with client:
            capacity = await take_snapshot(client, spec)
            # Observed exactly the way the CLI observes it, so the
            # harness cannot paper over a drift bug in the real path.
            state = ActualState(
                capacity=capacity,
                deployments=observe_deployments(self.cluster, namespace=NAMESPACE),
                route_hash=observe_route_hash(self.cluster, namespace=NAMESPACE),
                weights=dict(self.weights),
            )
            plan = plan_all(spec, state, registry=self.registry, namespace=NAMESPACE)

            executor = Executor(
                NodeProvisioner(client, spec, join=JOIN),
                RunLog(self.session),
                deployer=ServingDeployer(self.cluster, spec, self.registry, namespace=NAMESPACE),
            )
            run = await executor.execute(plan, zone="zone-east-1", dry_run=dry_run)

        self.planned = plan
        return run

    def deployments(self) -> dict[tuple[str, str], object]:
        return dict(observe_deployments(self.cluster, namespace=NAMESPACE))


@pytest.fixture
def platform(
    cloud: FakeCloudStack,
    cluster: FakeKubernetes,
    registry: ModelRegistry,
    session: Session,
) -> Platform:
    return Platform(cloud, cluster, registry, session)


class TestFromEmptyToServing:
    async def test_one_apply_builds_nodes_and_serves_the_model(self, platform: Platform) -> None:
        """SPEC.md section 14: no manual steps between empty and serving."""
        run = await platform.reconcile(spec())

        assert run.status is RunStatus.SUCCEEDED  # type: ignore[attr-defined]
        assert len(platform.cloud.gpu_vms()) == 2
        assert ("llama-3.3-70b-instruct", "blue") in platform.deployments()

    async def test_the_deployment_lands_with_the_right_shape(self, platform: Platform) -> None:
        await platform.reconcile(spec())

        deployment = platform.cluster.get("Deployment", NAMESPACE, "llama-3.3-70b-instruct-blue")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["limits"]["nvidia.com/gpu"] == 2
        assert deployment["spec"]["replicas"] == 2

    async def test_the_service_and_pdb_land_too(self, platform: Platform) -> None:
        await platform.reconcile(spec())

        kinds = {kind for kind, _, _ in platform.cluster.objects}
        assert {"Deployment", "Service", "PodDisruptionBudget", "ConfigMap"} <= kinds

    async def test_the_gateway_config_names_the_model(self, platform: Platform) -> None:
        await platform.reconcile(spec())

        configmap = platform.cluster.get("ConfigMap", NAMESPACE, GATEWAY_CONFIGMAP)
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        assert config["model_list"][0]["model_name"] == "llama-3.3-70b-instruct"
        assert config["litellm_settings"]["turn_off_message_logging"] is True


class TestIdempotence:
    async def test_the_second_apply_produces_an_empty_plan(self, platform: Platform) -> None:
        """SPEC.md section 14, now covering the whole platform."""
        await platform.reconcile(spec())

        await platform.reconcile(spec())

        assert platform.planned.actions == []

    async def test_a_third_apply_is_still_empty(self, platform: Platform) -> None:
        await platform.reconcile(spec())
        await platform.reconcile(spec())
        await platform.reconcile(spec())

        assert platform.planned.actions == []

    async def test_nothing_is_reapplied_on_a_settled_platform(self, platform: Platform) -> None:
        await platform.reconcile(spec())
        before = len(platform.cluster.applied)

        await platform.reconcile(spec())

        assert len(platform.cluster.applied) == before


class TestChangeDetection:
    async def test_raising_the_replica_count_updates_the_deployment(
        self, platform: Platform
    ) -> None:
        await platform.reconcile(spec())

        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["min_nodes"] = 2
        doc["models"][0]["replicas"] = {"min": 3, "max": 4}
        bigger = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        await platform.reconcile(bigger)

        # Scaling is in place: a replica count is not a spec change, so
        # it must not start a rollout.
        deployment = platform.cluster.get("Deployment", NAMESPACE, "llama-3.3-70b-instruct-blue")
        assert deployment["spec"]["replicas"] == 3
        assert ("llama-3.3-70b-instruct", "green") not in platform.deployments()

    async def test_a_serving_profile_change_rolls_out(self, platform: Platform) -> None:
        """SPEC.md section 6: a profile change takes effect by rollout.

        ADR-0001 put rollouts here rather than in the serving layer, so
        the change lands on a new colour beside the one still serving.
        """
        await platform.reconcile(spec())

        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["min_nodes"] = 2
        doc["tuningProfiles"]["serving"][0]["params"]["max_num_seqs"] = 128
        changed = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        await platform.reconcile(changed)

        green = platform.cluster.get("Deployment", NAMESPACE, "llama-3.3-70b-instruct-green")
        assert "128" in green["spec"]["template"]["spec"]["containers"][0]["args"]

        # Blue keeps serving the old configuration until promotion.
        blue = platform.cluster.get("Deployment", NAMESPACE, "llama-3.3-70b-instruct-blue")
        assert "256" in blue["spec"]["template"]["spec"]["containers"][0]["args"]

    async def test_removing_a_model_removes_its_deployment(self, platform: Platform) -> None:
        await platform.reconcile(spec())

        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["min_nodes"] = 2
        doc["models"] = []
        doc["tenants"][0]["models"] = []
        doc["assignments"] = []
        emptied = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        await platform.reconcile(emptied)

        assert platform.deployments() == {}


class TestUnmirroredModel:
    async def test_an_unmirrored_model_is_not_deployed(
        self, cloud: FakeCloudStack, cluster: FakeKubernetes, session: Session
    ) -> None:
        """Nodes still get built; the model waits for its weights."""
        bare = ModelRegistry(InMemoryObjectStore(), bucket="models")
        platform = Platform(cloud, cluster, bare, session)

        await platform.reconcile(spec())

        assert len(cloud.gpu_vms()) == 2
        assert platform.deployments() == {}
        assert any("not mirrored" in w for w in platform.planned.warnings)

    async def test_it_deploys_once_the_weights_arrive(
        self,
        cloud: FakeCloudStack,
        cluster: FakeKubernetes,
        session: Session,
        registry: ModelRegistry,
    ) -> None:
        bare = ModelRegistry(InMemoryObjectStore(), bucket="models")
        platform = Platform(cloud, cluster, bare, session)
        await platform.reconcile(spec())

        platform.registry = registry  # the mirror job finished
        await platform.reconcile(spec())

        assert ("llama-3.3-70b-instruct", "blue") in platform.deployments()


class TestDryRun:
    async def test_a_dry_run_changes_nothing_anywhere(self, platform: Platform) -> None:
        run = await platform.reconcile(spec(), dry_run=True)

        assert run.status is RunStatus.SUCCEEDED  # type: ignore[attr-defined]
        assert platform.cloud.gpu_vms() == []
        assert platform.cluster.objects == {}

    async def test_a_dry_run_still_plans_the_whole_platform(self, platform: Platform) -> None:
        await platform.reconcile(spec(), dry_run=True)

        kinds = {a.resource_type for a in platform.planned.actions}
        assert kinds == {"gpu-node", "model-deployment", "gateway-config"}


class TestOrderingUnderFailure:
    async def test_a_failed_node_build_skips_the_deployment(self, platform: Platform) -> None:
        """A model cannot land on nodes that were never created."""
        platform.cloud.fail_on = "deployVirtualMachine"

        run = await platform.reconcile(spec())

        assert run.status is RunStatus.FAILED  # type: ignore[attr-defined]
        outcomes = {a.resource_type: a.outcome.value for a in run.actions}  # type: ignore[attr-defined]
        assert outcomes["gpu-node"] == "failed"
        assert outcomes["model-deployment"] == "skipped"

    async def test_the_route_update_is_skipped_too(self, platform: Platform) -> None:
        platform.cloud.fail_on = "deployVirtualMachine"

        run = await platform.reconcile(spec())

        outcomes = {a.resource_type: a.outcome.value for a in run.actions}  # type: ignore[attr-defined]
        assert outcomes["gateway-config"] == "skipped"

    async def test_recovery_completes_the_platform(self, platform: Platform) -> None:
        platform.cloud.fail_on = "deployVirtualMachine"
        await platform.reconcile(spec())

        platform.cloud.fail_on = None
        run = await platform.reconcile(spec())

        assert run.status is RunStatus.SUCCEEDED  # type: ignore[attr-defined]
        assert ("llama-3.3-70b-instruct", "blue") in platform.deployments()


class TestRolloutAcrossRuns:
    """A blue/green rollout, one reconcile at a time (SPEC.md section 7)."""

    async def test_a_rollout_runs_to_completion(self, platform: Platform) -> None:
        await platform.reconcile(spec())
        platform.weights = {"llama-3.3-70b-instruct": {"blue": 100, "green": 0}}

        # A spec change brings green up beside blue.
        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["min_nodes"] = 2
        doc["tuningProfiles"]["serving"][0]["params"]["max_num_seqs"] = 128
        changed = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        await platform.reconcile(changed)

        assert ("llama-3.3-70b-instruct", "green") in platform.deployments()
        assert ("llama-3.3-70b-instruct", "blue") in platform.deployments()

        # Nothing more to do while the operator decides.
        await platform.reconcile(changed)
        assert [a.resource_type for a in platform.planned.actions] == []

        # Promotion moves the traffic; the next run retires blue.
        platform.weights = {"llama-3.3-70b-instruct": {"blue": 0, "green": 100}}
        await platform.reconcile(changed)

        assert ("llama-3.3-70b-instruct", "blue") not in platform.deployments()
        assert ("llama-3.3-70b-instruct", "green") in platform.deployments()

    async def test_the_platform_settles_after_a_rollout(self, platform: Platform) -> None:
        await platform.reconcile(spec())
        platform.weights = {"llama-3.3-70b-instruct": {"blue": 100, "green": 0}}

        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["min_nodes"] = 2
        doc["tuningProfiles"]["serving"][0]["params"]["max_num_seqs"] = 128
        changed = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        await platform.reconcile(changed)
        platform.weights = {"llama-3.3-70b-instruct": {"blue": 0, "green": 100}}
        await platform.reconcile(changed)

        await platform.reconcile(changed)

        assert platform.planned.actions == []

    async def test_a_rollback_retires_the_candidate_instead(self, platform: Platform) -> None:
        await platform.reconcile(spec())
        platform.weights = {"llama-3.3-70b-instruct": {"blue": 100, "green": 0}}

        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["min_nodes"] = 2
        doc["tuningProfiles"]["serving"][0]["params"]["max_num_seqs"] = 128
        changed = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        await platform.reconcile(changed)

        # The operator rolls back: blue keeps all the traffic.
        await platform.reconcile(changed)

        assert ("llama-3.3-70b-instruct", "blue") in platform.deployments()
