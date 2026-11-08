"""Planning model deployments and routes (SPEC.md section 4).

The reconciler runs ordered: capacity, models, profiles, routes. These
cover the models and routes stages, and the dependency edges between
them and the capacity stage.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from aisvc.inventory import CapacitySnapshot, ClassCapacity, PoolCapacity
from aisvc.k8s import DeploymentTarget, image_for, render, spec_hash
from aisvc.k8s.client import DeployedModel
from aisvc.reconciler import ActionKind, plan_all
from aisvc.reconciler.state import ActualState
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

SHA = "0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b"
REPO = "meta-llama/Llama-3.3-70B-Instruct"


def spec(**model_changes: Any) -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    doc["nodePools"][0]["min_nodes"] = 2
    if model_changes:
        doc["models"][0].update(model_changes)
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


def mirrored_registry() -> ModelRegistry:
    registry = ModelRegistry(InMemoryObjectStore(), bucket="models")
    entry = registry.put_file(REPO, SHA, "config.json", b"{}")
    registry.write_manifest(REPO, SHA, [entry], license="llama3.3", task="chat")
    return registry


def empty_registry() -> ModelRegistry:
    return ModelRegistry(InMemoryObjectStore(), bucket="models")


def state(
    *,
    pool_nodes: int = 2,
    deployments: dict[tuple[str, str], DeployedModel] | None = None,
    route_hash: str = "",
) -> ActualState:
    capacity = CapacitySnapshot(
        taken_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        classes={
            ("zone-east-1", "h100-8x"): ClassCapacity(
                zone="zone-east-1",
                gpu_class="h100-8x",
                gpus_per_node=8,
                nodes_total=8,
                nodes_healthy=8,
                gpus_total=64,
                free_by_host={f"host-{i}": 8 for i in range(8)},
                host_ids=tuple(f"host-{i}" for i in range(8)),
            )
        },
        pools={
            "h100-pool": PoolCapacity(
                pool="h100-pool",
                zone="zone-east-1",
                gpu_class="h100-8x",
                nodes=tuple(f"aisvc-h100-pool-{i:03d}" for i in range(1, pool_nodes + 1)),
            )
        },
    )
    return ActualState(capacity=capacity, deployments=deployments or {}, route_hash=route_hash)


def expected_target(loaded: PlatformSpec, replicas: int = 2) -> DeploymentTarget:
    model = loaded.models[0]
    pool = loaded.node_pool(model.node_pool)
    return DeploymentTarget(
        model=model,
        pool=pool,
        gpu_class=loaded.gpu_class(pool.gpu_class),
        serving_profile=loaded.serving_profile_for(model),
        image=image_for(model),
        replicas=replicas,
        weights_uri=f"s3://models/{REPO}/{SHA}/",
    )


def deployed(loaded: PlatformSpec, **kw: Any) -> DeployedModel:
    digest = spec_hash(render(expected_target(loaded)))
    defaults: dict[str, Any] = {
        "model": "llama-3.3-70b-instruct",
        "colour": "blue",
        "replicas_desired": 2,
        "replicas_ready": 2,
        "image": image_for(loaded.models[0]),
        "spec_hash": digest,
    }
    defaults.update(kw)
    return DeployedModel(**defaults)


def actions_of(plan: Any, resource_type: str) -> list[Any]:
    return [a for a in plan.actions if a.resource_type == resource_type]


class TestModelPlanning:
    def test_a_mirrored_model_with_no_deployment_is_created(self) -> None:
        plan = plan_all(spec(), state(), registry=mirrored_registry())

        creates = actions_of(plan, "model-deployment")
        assert len(creates) == 1
        assert creates[0].kind is ActionKind.CREATE
        assert creates[0].name == "llama-3.3-70b-instruct-blue"

    def test_an_unmirrored_model_is_not_deployed(self) -> None:
        """Nothing is served from weights that were never mirrored."""
        plan = plan_all(spec(), state(), registry=empty_registry())

        assert actions_of(plan, "model-deployment") == []
        assert any("not mirrored" in w for w in plan.warnings)

    def test_an_unchanged_deployment_plans_nothing(self) -> None:
        loaded = spec()
        plan = plan_all(
            loaded,
            state(deployments={("llama-3.3-70b-instruct", "blue"): deployed(loaded)}),
            registry=mirrored_registry(),
        )

        assert actions_of(plan, "model-deployment") == []

    def test_a_changed_spec_plans_an_update(self) -> None:
        loaded = spec()
        stale = deployed(loaded, spec_hash="something-else")
        plan = plan_all(
            loaded,
            state(deployments={("llama-3.3-70b-instruct", "blue"): stale}),
            registry=mirrored_registry(),
        )

        updates = actions_of(plan, "model-deployment")
        assert len(updates) == 1
        assert updates[0].kind is ActionKind.UPDATE

    def test_the_update_says_what_changed(self) -> None:
        loaded = spec()
        stale = deployed(loaded, spec_hash="old", image="vllm/vllm-openai:v0.9.0")
        plan = plan_all(
            loaded,
            state(deployments={("llama-3.3-70b-instruct", "blue"): stale}),
            registry=mirrored_registry(),
        )

        assert "spec changed" in actions_of(plan, "model-deployment")[0].reason

    def test_a_deployment_for_an_unknown_model_is_removed(self) -> None:
        orphan = DeployedModel(
            model="retired-model",
            colour="blue",
            replicas_desired=1,
            replicas_ready=1,
            image="x",
            spec_hash="y",
        )
        plan = plan_all(
            spec(),
            state(deployments={("retired-model", "blue"): orphan}),
            registry=mirrored_registry(),
        )

        deletes = [a for a in actions_of(plan, "model-deployment") if a.kind is ActionKind.DELETE]
        assert len(deletes) == 1
        assert deletes[0].name == "retired-model-blue"

    def test_replica_count_comes_from_the_spec_minimum(self) -> None:
        loaded = spec(replicas={"min": 3, "max": 5})
        plan = plan_all(loaded, state(), registry=mirrored_registry())

        assert "3 replica" in actions_of(plan, "model-deployment")[0].detail

    def test_an_unpinned_model_is_skipped_with_a_warning(self) -> None:
        """A source without a commit SHA cannot resolve to a mirror."""
        loaded = spec()
        registry = empty_registry()
        plan = plan_all(loaded, state(), registry=registry)

        assert any("llama-3.3-70b-instruct" in w for w in plan.warnings)


class TestOrdering:
    def test_a_deployment_waits_for_its_pools_nodes(self) -> None:
        """Capacity before models (SPEC.md section 4)."""
        plan = plan_all(spec(), state(pool_nodes=0), registry=mirrored_registry())

        node_action = next(a for a in plan.actions if a.resource_type == "gpu-node")
        model_action = next(a for a in plan.actions if a.resource_type == "model-deployment")
        assert node_action.key in model_action.depends_on

    def test_no_dependency_when_the_pool_is_already_built(self) -> None:
        plan = plan_all(spec(), state(pool_nodes=2), registry=mirrored_registry())

        model_action = next(a for a in plan.actions if a.resource_type == "model-deployment")
        assert model_action.depends_on == ()

    def test_routes_wait_for_the_deployments_they_point_at(self) -> None:
        plan = plan_all(spec(), state(), registry=mirrored_registry())

        route = next(a for a in plan.actions if a.resource_type == "gateway-config")
        model_action = next(a for a in plan.actions if a.resource_type == "model-deployment")
        assert model_action.key in route.depends_on

    def test_actions_are_listed_capacity_then_models_then_routes(self) -> None:
        plan = plan_all(spec(), state(pool_nodes=0), registry=mirrored_registry())

        order = [a.resource_type for a in plan.actions]
        assert order.index("gpu-node") < order.index("model-deployment")
        assert order.index("model-deployment") < order.index("gateway-config")


class TestRoutePlanning:
    def test_routes_are_planned_when_the_config_is_new(self) -> None:
        plan = plan_all(spec(), state(), registry=mirrored_registry())

        assert len(actions_of(plan, "gateway-config")) == 1

    def test_an_unchanged_route_config_plans_nothing(self) -> None:
        loaded = spec()
        first = plan_all(loaded, state(), registry=mirrored_registry())
        digest = next(
            a for a in first.actions if a.resource_type == "gateway-config"
        ).detail.split()[-1]

        settled = plan_all(
            loaded,
            state(
                deployments={("llama-3.3-70b-instruct", "blue"): deployed(loaded)},
                route_hash=digest,
            ),
            registry=mirrored_registry(),
        )

        assert actions_of(settled, "gateway-config") == []

    def test_no_models_means_no_route_action(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"] = []
        doc["tenants"][0]["models"] = []
        doc["assignments"] = []
        empty = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)

        plan = plan_all(empty, ActualState.empty_for(empty), registry=empty_registry())

        assert actions_of(plan, "gateway-config") == []


class TestConvergence:
    def test_a_fully_built_platform_plans_nothing(self) -> None:
        loaded = spec()
        first = plan_all(loaded, state(), registry=mirrored_registry())
        digest = next(
            a for a in first.actions if a.resource_type == "gateway-config"
        ).detail.split()[-1]

        settled = plan_all(
            loaded,
            state(
                deployments={("llama-3.3-70b-instruct", "blue"): deployed(loaded)},
                route_hash=digest,
            ),
            registry=mirrored_registry(),
        )

        assert not settled.has_changes

    def test_planning_from_nothing_covers_every_stage(self) -> None:
        plan = plan_all(spec(), ActualState.empty_for(spec()), registry=mirrored_registry())

        kinds = {a.resource_type for a in plan.actions}
        assert kinds == {"gpu-node", "model-deployment", "gateway-config"}


class TestActualState:
    def test_empty_for_has_no_deployments(self) -> None:
        assert ActualState.empty_for(spec()).deployments == {}

    def test_deployment_lookup(self) -> None:
        loaded = spec()
        current = state(deployments={("llama-3.3-70b-instruct", "blue"): deployed(loaded)})

        assert current.deployment("llama-3.3-70b-instruct", "blue") is not None
        assert current.deployment("llama-3.3-70b-instruct", "green") is None

    def test_colours_of_lists_what_is_deployed(self) -> None:
        loaded = spec()
        current = state(
            deployments={
                ("llama-3.3-70b-instruct", "blue"): deployed(loaded),
                ("llama-3.3-70b-instruct", "green"): deployed(loaded, colour="green"),
            }
        )

        assert current.colours_of("llama-3.3-70b-instruct") == ["blue", "green"]


class TestUnmirroredIsNotFatal:
    def test_capacity_still_plans_when_a_model_is_unmirrored(self) -> None:
        plan = plan_all(spec(), state(pool_nodes=0), registry=empty_registry())

        assert actions_of(plan, "gpu-node")

    @pytest.mark.parametrize("pool_nodes", [0, 1, 2, 5])
    def test_plans_are_stable_whatever_the_pool_size(self, pool_nodes: int) -> None:
        plan = plan_all(spec(), state(pool_nodes=pool_nodes), registry=mirrored_registry())
        again = plan_all(spec(), state(pool_nodes=pool_nodes), registry=mirrored_registry())

        assert [a.key for a in plan.actions] == [a.key for a in again.actions]
