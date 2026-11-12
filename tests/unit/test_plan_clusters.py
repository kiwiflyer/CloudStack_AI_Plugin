"""Planning a CKS cluster into existence (ADR-0007, recommendation 3).

A zone with `create: true` whose cluster CloudStack does not have gets
one action, before any GPU node for that zone: a node that boots with
nowhere to join is a billed VM doing nothing.

The plan line names the Kubernetes version and the service offering,
because where the operator pinned neither the engine chose them, and
one of them costs money.
"""

from __future__ import annotations

from aisvc.cloudstack.clusters import ClusterPlanned, ClusterState
from aisvc.reconciler.plan import ActionKind, plan_all, plan_clusters
from aisvc.reconciler.state import ActualState
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_plan import snapshot_with
from tests.unit.test_spec import LICENSES, MINIMAL

PLANNED = ClusterPlanned(
    zone="East-1",
    zone_id="z-east",
    cluster="aisvc-east-1",
    version="1.30.1",
    version_id="v-130",
    offering="Small Instance",
    offering_id="o-small",
    size=1,
)


def plain_spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


def creating_spec() -> PlatformSpec:
    text = MINIMAL.replace(
        "    cks_cluster: aisvc-east-1\n",
        "    kubernetes:\n      source: cks\n      cluster: aisvc-east-1\n      create: true\n",
    )
    return load_spec_from_string(text, allowed_licenses=LICENSES)


class TestWhenAClusterIsMissing:
    def test_it_plans_one(self) -> None:
        actions, _ = plan_clusters(
            creating_spec(),
            ActualState(capacity=snapshot_with(0), cluster_plans={"zone-east-1": PLANNED}),
        )

        assert [a.resource_type for a in actions] == ["kubernetes-cluster"]
        assert actions[0].kind is ActionKind.CREATE

    def test_the_line_names_the_version_and_the_offering(self) -> None:
        actions, _ = plan_clusters(
            creating_spec(),
            ActualState(capacity=snapshot_with(0), cluster_plans={"zone-east-1": PLANNED}),
        )

        assert "1.30.1" in actions[0].detail
        assert "Small Instance" in actions[0].detail

    def test_a_cluster_that_exists_plans_nothing(self) -> None:
        # The idempotence rule: a second apply is an empty plan.
        actions, _ = plan_clusters(
            creating_spec(),
            ActualState(
                capacity=snapshot_with(0),
                clusters=(ClusterState("zone-east-1", "aisvc-east-1", state="Running"),),
            ),
        )

        assert actions == []

    def test_a_zone_that_did_not_ask_plans_nothing(self) -> None:
        actions, _ = plan_clusters(
            plain_spec(),
            ActualState(capacity=snapshot_with(0)),
        )

        assert actions == []


class TestWhenItCannotBeBuilt:
    def test_it_warns_rather_than_planning_something_impossible(self) -> None:
        _, warnings = plan_clusters(
            creating_spec(),
            ActualState(
                capacity=snapshot_with(0),
                cluster_problems={"zone-east-1": "no enabled Kubernetes version"},
            ),
        )

        assert any("no enabled Kubernetes version" in w for w in warnings)

    def test_nothing_is_planned_for_it(self) -> None:
        actions, _ = plan_clusters(
            creating_spec(),
            ActualState(
                capacity=snapshot_with(0),
                cluster_problems={"zone-east-1": "no enabled Kubernetes version"},
            ),
        )

        assert actions == []


class TestOrdering:
    def test_gpu_nodes_wait_for_the_cluster(self) -> None:
        # A node that boots with nowhere to join is a billed VM doing
        # nothing, and nothing later in the plan can fix it.
        plan = plan_all(
            creating_spec(),
            ActualState(capacity=snapshot_with(0), cluster_plans={"zone-east-1": PLANNED}),
            registry=_registry(),
        )

        cluster = next(a for a in plan.actions if a.resource_type == "kubernetes-cluster")
        nodes = [a for a in plan.actions if a.resource_type == "gpu-node"]
        assert nodes
        assert all(cluster.key in node.depends_on for node in nodes)

    def test_the_cluster_comes_first_in_the_plan(self) -> None:
        plan = plan_all(
            creating_spec(),
            ActualState(capacity=snapshot_with(0), cluster_plans={"zone-east-1": PLANNED}),
            registry=_registry(),
        )

        assert plan.actions[0].resource_type == "kubernetes-cluster"

    def test_nodes_do_not_wait_on_a_cluster_that_exists(self) -> None:
        plan = plan_all(
            creating_spec(),
            ActualState(
                capacity=snapshot_with(0),
                clusters=(ClusterState("zone-east-1", "aisvc-east-1", state="Running"),),
            ),
            registry=_registry(),
        )

        nodes = [a for a in plan.actions if a.resource_type == "gpu-node"]
        assert nodes
        assert all(not node.depends_on for node in nodes)


def _registry() -> ModelRegistry:
    return ModelRegistry(InMemoryObjectStore(), bucket="models")
