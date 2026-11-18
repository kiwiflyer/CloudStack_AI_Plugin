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
"""What the reconciler does with `placement` (ADR-0008).

The API half is only half. A placement row that the planner never
reads is a record of an intention nobody acts on, and the failure it
risks is specific: an `api` model whose row the planner cannot see
runs nowhere, silently, while `listAiModelDeployments` shows the row
that was supposed to deploy it.

So both values are covered here, and so is the one that deletes: a
`placement: api` model with no row must not be declared, because that
absence is exactly what `deleteAiModelDeployment` leaves behind and
what tears the Deployment down.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml

from aisvc.inventory import CapacitySnapshot
from aisvc.reconciler.plan import plan_models
from aisvc.reconciler.state import ActualState
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL


def nothing_deployed() -> ActualState:
    """An empty cluster: every model is a create, so the branch shows."""
    return ActualState(capacity=CapacitySnapshot(taken_at=datetime.now(UTC)))


def spec_with(**model_kw: Any) -> Any:
    doc = yaml.safe_load(MINIMAL)
    doc["models"][0].update(model_kw)
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


def mirrored_registry(spec: Any) -> ModelRegistry:
    """A registry holding the catalogue's weights, so nothing is held back."""
    registry = ModelRegistry(InMemoryObjectStore(), bucket="models")
    for model in spec.document.models:
        revision = model.revision or ""
        entry = registry.put_file(model.repo_id, revision, "config.json", b"{}")
        registry.write_manifest(
            model.repo_id, revision, [entry], license=model.license, task=model.task
        )
    return registry


class Placement:
    """A stand-in for a `DeploymentPlacementRecord`."""

    def __init__(self, model: str, zone: str, replicas: int) -> None:
        self.model = model
        self.zone = zone
        self.replicas = replicas
        self.serving_profile = ""


def model_names(actions: Any) -> list[str]:
    return [a.name for a in actions if a.resource_type == "model-deployment"]


def one_deployment(actions: Any) -> Any:
    return next(a for a in actions if a.resource_type == "model-deployment")


class TestSpecPlacedModelsAreUnchanged:
    """The default must behave exactly as it did before ADR-0008."""

    def test_a_spec_model_is_planned_without_any_placement(self) -> None:
        spec = spec_with(placement="spec")
        actions, _ = plan_models(
            spec, nothing_deployed(), registry=mirrored_registry(spec), placements=()
        )

        assert model_names(actions)

    def test_a_model_that_never_heard_of_placement_is_planned(self) -> None:
        # No `placement` key at all, which is every spec written before
        # this field existed.
        spec = spec_with()
        actions, _ = plan_models(spec, nothing_deployed(), registry=mirrored_registry(spec))

        assert model_names(actions)


class TestApiPlacedModels:
    def test_one_with_no_placement_is_not_planned(self) -> None:
        # And this is what deleteAiModelDeployment leaves behind, so
        # the Deployment is swept rather than left orphaned.
        spec = spec_with(placement="api")
        actions, _ = plan_models(
            spec, nothing_deployed(), registry=mirrored_registry(spec), placements=()
        )

        assert model_names(actions) == []

    def test_one_with_a_placement_is_planned(self) -> None:
        spec = spec_with(placement="api")
        zone = spec.document.node_pools[0].zone
        placements = (Placement(spec.document.models[0].name, zone, 2),)

        actions, _ = plan_models(
            spec, nothing_deployed(), registry=mirrored_registry(spec), placements=placements
        )

        assert model_names(actions)

    def test_the_placement_sets_the_replica_count(self) -> None:
        spec = spec_with(placement="api", replicas={"min": 1, "max": 8})
        zone = spec.document.node_pools[0].zone
        placements = (Placement(spec.document.models[0].name, zone, 5),)

        actions, _ = plan_models(
            spec, nothing_deployed(), registry=mirrored_registry(spec), placements=placements
        )

        assert "5 replica(s)" in one_deployment(actions).detail

    def test_a_placement_in_another_zone_does_not_deploy_it(self) -> None:
        # The API refuses this, so a row like it can only come from a
        # pool that moved zone afterwards. Deploying it anyway would
        # put the model somewhere the spec does not describe.
        spec = spec_with(placement="api")
        placements = (Placement(spec.document.models[0].name, "elsewhere", 2),)

        actions, _ = plan_models(
            spec, nothing_deployed(), registry=mirrored_registry(spec), placements=placements
        )

        assert model_names(actions) == []

    def test_a_placement_for_an_uncatalogued_model_is_ignored(self) -> None:
        spec = spec_with(placement="api")
        placements = (Placement("ghost", spec.document.node_pools[0].zone, 2),)

        actions, _ = plan_models(
            spec, nothing_deployed(), registry=mirrored_registry(spec), placements=placements
        )

        assert model_names(actions) == []


class TestThePlanSaysWhereADeploymentCameFrom:
    """Two sources feed one reconcile now, so a plan that does not say
    which leaves an operator unable to explain why a model appeared."""

    def test_a_spec_placed_deployment_says_so(self) -> None:
        spec = spec_with(placement="spec")
        actions, _ = plan_models(spec, nothing_deployed(), registry=mirrored_registry(spec))

        assert "placement: spec" in one_deployment(actions).detail

    def test_an_api_placed_deployment_says_so(self) -> None:
        spec = spec_with(placement="api")
        zone = spec.document.node_pools[0].zone
        placements = (Placement(spec.document.models[0].name, zone, 2),)

        actions, _ = plan_models(
            spec, nothing_deployed(), registry=mirrored_registry(spec), placements=placements
        )

        assert "placement: api" in one_deployment(actions).detail
