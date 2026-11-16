# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with this
# work for additional information regarding copyright ownership. The ASF
# licenses this file to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""The reconciler driving rollouts and autoscale (SPEC.md sections 4, 6, 7).

Until now `lifecycle` and `autoscale` were decision functions nothing
called. These assert the planner actually uses them: a spec change on a
blue/green model brings up the other colour rather than mutating the
live one, and replica counts follow the metrics rather than the spec's
floor.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import yaml

from aisvc.autoscale import SCALE_DOWN_AFTER_SECONDS, SCALE_UP_AFTER_SECONDS, Metrics
from aisvc.inventory import CapacitySnapshot, ClassCapacity, PoolCapacity
from aisvc.k8s import DeploymentTarget, config_hash, image_for
from aisvc.k8s.client import DeployedModel
from aisvc.reconciler import ActionKind, plan_all
from aisvc.reconciler.state import ActualState, DeploymentHealth
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

SHA = "0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b"
REPO = "meta-llama/Llama-3.3-70B-Instruct"
MODEL = "llama-3.3-70b-instruct"


def spec(**model_changes: Any) -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    doc["nodePools"][0]["min_nodes"] = 2
    if model_changes:
        doc["models"][0].update(model_changes)
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


def registry() -> ModelRegistry:
    store = ModelRegistry(InMemoryObjectStore(), bucket="models")
    entry = store.put_file(REPO, SHA, "config.json", b"{}")
    store.write_manifest(REPO, SHA, [entry], license="llama3.3", task="chat")
    return store


def hash_for(loaded: PlatformSpec, colour: str, replicas: int = 2) -> str:
    model = loaded.models[0]
    pool = loaded.node_pool(model.node_pool)
    return config_hash(
        DeploymentTarget(
            model=model,
            pool=pool,
            gpu_class=loaded.gpu_class(pool.gpu_class),
            serving_profile=loaded.serving_profile_for(model),
            image=image_for(model),
            replicas=replicas,
            weights_uri=f"s3://models/{REPO}/{SHA}/",
            colour=colour,
        )
    )


def deployed(
    colour: str, digest: str, replicas: int = 2, ready: int | None = None
) -> DeployedModel:
    return DeployedModel(
        model=MODEL,
        colour=colour,
        replicas_desired=replicas,
        replicas_ready=replicas if ready is None else ready,
        image="vllm/vllm-openai:v0.11.0",
        spec_hash=digest,
    )


def state(
    *,
    deployments: dict[tuple[str, str], DeployedModel] | None = None,
    weights: dict[str, dict[str, int]] | None = None,
    health: dict[str, DeploymentHealth] | None = None,
) -> ActualState:
    capacity = CapacitySnapshot(
        taken_at=dt.datetime.now(dt.UTC),
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
                nodes=("aisvc-h100-pool-001", "aisvc-h100-pool-002"),
            )
        },
    )
    return ActualState(
        capacity=capacity,
        deployments=deployments or {},
        weights=weights or {},
        health=health or {},
    )


def model_actions(plan: Any) -> list[Any]:
    return [a for a in plan.actions if a.resource_type == "model-deployment"]


class TestLiveColour:
    def test_nothing_deployed_defaults_to_blue(self) -> None:
        assert state().live_colour(MODEL) == "blue"

    def test_the_colour_with_most_traffic_is_live(self) -> None:
        current = state(weights={MODEL: {"blue": 10, "green": 90}})

        assert current.live_colour(MODEL) == "green"

    def test_a_canary_still_counts_the_old_colour_as_live(self) -> None:
        current = state(weights={MODEL: {"blue": 90, "green": 10}})

        assert current.live_colour(MODEL) == "blue"


class TestBlueGreenPlanning:
    def test_a_spec_change_brings_up_the_other_colour(self) -> None:
        """ADR-0001: rollouts are ours, so a change must not mutate live."""
        loaded = spec()
        plan = plan_all(
            loaded,
            state(deployments={(MODEL, "blue"): deployed("blue", "stale")}),
            registry=registry(),
        )

        actions = model_actions(plan)
        assert [a.kind for a in actions] == [ActionKind.CREATE]
        assert actions[0].name == f"{MODEL}-green"

    def test_the_live_colour_is_left_alone_during_a_rollout(self) -> None:
        loaded = spec()
        plan = plan_all(
            loaded,
            state(deployments={(MODEL, "blue"): deployed("blue", "stale")}),
            registry=registry(),
        )

        assert not any(a.name == f"{MODEL}-blue" for a in model_actions(plan))

    def test_a_candidate_already_matching_plans_nothing_more(self) -> None:
        loaded = spec()
        plan = plan_all(
            loaded,
            state(
                deployments={
                    (MODEL, "blue"): deployed("blue", "stale"),
                    (MODEL, "green"): deployed("green", hash_for(loaded, "green")),
                },
                weights={MODEL: {"blue": 90, "green": 10}},
            ),
            registry=registry(),
        )

        assert model_actions(plan) == []

    def test_a_stale_candidate_is_updated(self) -> None:
        loaded = spec()
        plan = plan_all(
            loaded,
            state(
                deployments={
                    (MODEL, "blue"): deployed("blue", "stale"),
                    (MODEL, "green"): deployed("green", "also-stale"),
                },
            ),
            registry=registry(),
        )

        actions = model_actions(plan)
        assert [a.kind for a in actions] == [ActionKind.UPDATE]
        assert actions[0].name == f"{MODEL}-green"

    def test_after_promotion_the_old_colour_is_retired(self) -> None:
        loaded = spec()
        plan = plan_all(
            loaded,
            state(
                deployments={
                    (MODEL, "blue"): deployed("blue", "stale"),
                    (MODEL, "green"): deployed("green", hash_for(loaded, "green")),
                },
                weights={MODEL: {"blue": 0, "green": 100}},
            ),
            registry=registry(),
        )

        deletes = [a for a in model_actions(plan) if a.kind is ActionKind.DELETE]
        assert [a.name for a in deletes] == [f"{MODEL}-blue"]

    def test_a_settled_single_colour_plans_nothing(self) -> None:
        loaded = spec()
        plan = plan_all(
            loaded,
            state(
                deployments={(MODEL, "blue"): deployed("blue", hash_for(loaded, "blue"))},
                weights={MODEL: {"blue": 100, "green": 0}},
            ),
            registry=registry(),
        )

        assert model_actions(plan) == []

    def test_a_recreate_strategy_updates_in_place(self) -> None:
        """Only bluegreen gets a second colour; recreate is a plain update."""
        loaded = spec(rollout={"strategy": "recreate"})
        plan = plan_all(
            loaded,
            state(deployments={(MODEL, "blue"): deployed("blue", "stale")}),
            registry=registry(),
        )

        actions = model_actions(plan)
        assert [a.kind for a in actions] == [ActionKind.UPDATE]
        assert actions[0].name == f"{MODEL}-blue"


class TestAutoscaleDrivesReplicas:
    def health(self, **kw: Any) -> dict[str, DeploymentHealth]:
        defaults: dict[str, Any] = {
            "metrics": Metrics(queue_depth=0.0, ttft_p95_ms=200.0, gpu_utilization=0.5),
            "breaching_for_seconds": 0,
            "idle_for_seconds": 0,
        }
        defaults.update(kw)
        return {MODEL: DeploymentHealth(**defaults)}

    def test_without_metrics_the_replica_floor_is_used(self) -> None:
        loaded = spec()
        plan = plan_all(loaded, state(), registry=registry())

        assert "2 replica" in model_actions(plan)[0].detail

    def test_a_sustained_breach_asks_for_another_replica(self) -> None:
        loaded = spec()
        current = state(
            deployments={(MODEL, "blue"): deployed("blue", hash_for(loaded, "blue"))},
            weights={MODEL: {"blue": 100, "green": 0}},
            health=self.health(
                metrics=Metrics(queue_depth=50.0, ttft_p95_ms=200.0, gpu_utilization=0.9),
                breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
            ),
        )

        plan = plan_all(loaded, current, registry=registry())

        actions = model_actions(plan)
        assert [a.kind for a in actions] == [ActionKind.UPDATE]
        assert "3 replica" in actions[0].detail
        assert "queue" in actions[0].reason

    def test_a_long_idle_period_gives_a_replica_back(self) -> None:
        loaded = spec(replicas={"min": 1, "max": 4})
        current = state(
            deployments={
                (MODEL, "blue"): deployed("blue", hash_for(loaded, "blue", 3), replicas=3)
            },
            weights={MODEL: {"blue": 100, "green": 0}},
            health=self.health(
                metrics=Metrics(queue_depth=0.0, ttft_p95_ms=50.0, gpu_utilization=0.0),
                idle_for_seconds=SCALE_DOWN_AFTER_SECONDS,
            ),
        )

        plan = plan_all(loaded, current, registry=registry())

        assert "2 replica" in model_actions(plan)[0].detail

    def test_autoscale_never_leaves_the_declared_band(self) -> None:
        loaded = spec(replicas={"min": 2, "max": 2})
        current = state(
            deployments={(MODEL, "blue"): deployed("blue", hash_for(loaded, "blue"))},
            weights={MODEL: {"blue": 100, "green": 0}},
            health=self.health(
                metrics=Metrics(queue_depth=99.0, ttft_p95_ms=9000.0, gpu_utilization=1.0),
                breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
            ),
        )

        plan = plan_all(loaded, current, registry=registry())

        assert model_actions(plan) == []

    def test_a_brief_breach_does_not_scale(self) -> None:
        loaded = spec()
        current = state(
            deployments={(MODEL, "blue"): deployed("blue", hash_for(loaded, "blue"))},
            weights={MODEL: {"blue": 100, "green": 0}},
            health=self.health(
                metrics=Metrics(queue_depth=50.0, ttft_p95_ms=200.0, gpu_utilization=0.9),
                breaching_for_seconds=5,
            ),
        )

        plan = plan_all(loaded, current, registry=registry())

        assert model_actions(plan) == []

    def test_scaling_is_not_attempted_mid_rollout(self) -> None:
        """Changing replicas while traffic is shifting confuses both."""
        loaded = spec()
        current = state(
            deployments={
                (MODEL, "blue"): deployed("blue", "stale"),
                (MODEL, "green"): deployed("green", hash_for(loaded, "green")),
            },
            weights={MODEL: {"blue": 90, "green": 10}},
            health=self.health(
                metrics=Metrics(queue_depth=99.0, ttft_p95_ms=9000.0, gpu_utilization=1.0),
                breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
            ),
        )

        plan = plan_all(loaded, current, registry=registry())

        assert model_actions(plan) == []


class TestConvergence:
    @pytest.mark.parametrize("colour", ["blue", "green"])
    def test_a_settled_platform_plans_nothing_whichever_colour_is_live(self, colour: str) -> None:
        loaded = spec()
        current = state(
            deployments={(MODEL, colour): deployed(colour, hash_for(loaded, colour))},
            weights={MODEL: {colour: 100}},
        )

        plan = plan_all(loaded, current, registry=registry())

        assert model_actions(plan) == []
