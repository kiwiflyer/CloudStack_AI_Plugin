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
"""Promotion has to survive reconciliation (SPEC.md section 4; ADR-0001).

Which colour is live is *derived* from the gateway's weights — there is
no rollout state (CLAUDE.md). That only works if the weights in the
ConfigMap are the ones a promotion wrote and a reconcile leaves them
alone. Rendering routes at a fixed weight instead would send traffic
back to blue on the next run and nothing would record that it happened.

The weights are excluded from the route hash for the same reason
replicas are excluded from the spec hash: they are runtime state, not
desired state, and folding them in would make every promotion look like
a config change.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from aisvc.gateway import build_litellm_config
from aisvc.inventory import CapacitySnapshot
from aisvc.k8s import DEFAULT_NAMESPACE
from aisvc.k8s.client import FakeKubernetes, observe_weights
from aisvc.reconciler.deployer import GATEWAY_CONFIGMAP, ROUTE_HASH_ANNOTATION, ServingDeployer
from aisvc.reconciler.plan import plan_routes, route_config_hash
from aisvc.reconciler.state import ActualState
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

PROMOTED = {"llama-3.3-70b-instruct": {"blue": 0, "green": 100}}


def spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


def deployer(cluster: FakeKubernetes) -> ServingDeployer:
    return ServingDeployer(cluster, spec(), ModelRegistry(InMemoryObjectStore(), bucket="models"))


def configmap_of(cluster: FakeKubernetes) -> dict[str, Any]:
    for obj in cluster.list("ConfigMap", DEFAULT_NAMESPACE):
        if (obj.get("metadata") or {}).get("name") == GATEWAY_CONFIGMAP:
            return obj
    raise AssertionError("the gateway ConfigMap was never written")


class TestWeightsAreNotInTheRouteHash:
    def test_the_hash_is_taken_at_a_normalised_split(self) -> None:
        """Otherwise every promotion looks like a spec change.

        The digest is of the config rendered at `weights=None`, which
        both the planner and the deployer's annotation use, so a
        promotion changes the written config without changing the
        digest the two compare.
        """
        loaded = spec()
        normalised = build_litellm_config(loaded, weights=None)
        promoted = build_litellm_config(loaded, weights=PROMOTED)

        assert promoted != normalised
        assert route_config_hash(normalised) == route_config_hash(
            build_litellm_config(loaded, weights=None)
        )

    def test_a_real_spec_change_still_changes_the_hash(self) -> None:
        import copy

        loaded = spec()
        config = build_litellm_config(loaded, weights=None)
        changed = copy.deepcopy(config)
        changed["model_list"][0]["model_name"] = "something-else"

        assert route_config_hash(config) != route_config_hash(changed)


class TestApplyingRoutesKeepsTheWeights:
    async def test_apply_routes_writes_the_weights_it_is_given(self) -> None:
        cluster = FakeKubernetes()

        await deployer(cluster).apply_routes(weights=PROMOTED)

        assert observe_weights(cluster)["llama-3.3-70b-instruct"]["green"] == 100

    async def test_reconciling_again_does_not_revert_a_promotion(self) -> None:
        """The bug this file exists for.

        The executor calls `apply_routes` with no weights on every run.
        If that meant "the default split", each reconcile would send
        every model back to blue, silently undoing a promotion an
        operator made deliberately. It means "keep what is in force".
        """
        cluster = FakeKubernetes()
        await deployer(cluster).apply_routes(weights=PROMOTED)

        await deployer(cluster).apply_routes()

        assert observe_weights(cluster)["llama-3.3-70b-instruct"]["green"] == 100

    async def test_an_explicit_split_overrides_what_is_in_force(self) -> None:
        """A rollback has to be able to move traffic back."""
        cluster = FakeKubernetes()
        await deployer(cluster).apply_routes(weights=PROMOTED)

        await deployer(cluster).apply_routes(
            weights={"llama-3.3-70b-instruct": {"blue": 100, "green": 0}}
        )

        assert observe_weights(cluster)["llama-3.3-70b-instruct"] == {"blue": 100}

    async def test_no_weights_means_the_default_split(self) -> None:
        cluster = FakeKubernetes()

        await deployer(cluster).apply_routes()

        assert observe_weights(cluster)["llama-3.3-70b-instruct"] == {"blue": 100}

    async def test_the_route_hash_annotation_ignores_the_weights(self) -> None:
        """So a settled platform does not re-apply its routes every run."""
        plain, promoted = FakeKubernetes(), FakeKubernetes()

        await deployer(plain).apply_routes()
        await deployer(promoted).apply_routes(weights=PROMOTED)

        annotations = [
            configmap_of(c)["metadata"]["annotations"][ROUTE_HASH_ANNOTATION]
            for c in (plain, promoted)
        ]
        assert annotations[0] == annotations[1]

    async def test_a_dry_run_writes_nothing(self) -> None:
        cluster = FakeKubernetes()

        await deployer(cluster).apply_routes(weights=PROMOTED, dry_run=True)

        assert not list(cluster.list("ConfigMap", DEFAULT_NAMESPACE))


class TestPlanningAfterAPromotion:
    def state_with(self, weights: dict[str, dict[str, int]]) -> ActualState:
        loaded = spec()
        return ActualState(
            capacity=CapacitySnapshot(taken_at=dt.datetime.now(dt.UTC)),
            route_hash=route_config_hash(build_litellm_config(loaded, weights=None)),
            weights=weights,
        )

    def test_a_promoted_platform_plans_no_route_change(self) -> None:
        """A promotion must not leave the planner wanting to rewrite routes."""
        assert plan_routes(spec(), self.state_with(PROMOTED)) == []

    def test_an_unpromoted_platform_plans_no_route_change_either(self) -> None:
        assert plan_routes(spec(), self.state_with({})) == []
