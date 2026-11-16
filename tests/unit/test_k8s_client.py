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
"""Applying and observing serving workloads (SPEC.md section 4).

Server-side apply, and reading back what is actually running so the
reconciler can diff against it.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.k8s import DeploymentTarget, render, spec_hash
from aisvc.k8s.client import (
    DeployedModel,
    FakeKubernetes,
    KubernetesError,
    apply_objects,
    observe_deployments,
)
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

IMAGE = "vllm/vllm-openai:v0.11.0"


def spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


def target(**kw: Any) -> DeploymentTarget:
    loaded = spec()
    model = loaded.models[0]
    pool = loaded.node_pool(model.node_pool)
    defaults: dict[str, Any] = {
        "model": model,
        "pool": pool,
        "gpu_class": loaded.gpu_class(pool.gpu_class),
        "serving_profile": loaded.serving_profile_for(model),
        "image": IMAGE,
        "replicas": 2,
        "weights_uri": "s3://models/meta-llama/x/0f3f4a/",
    }
    defaults.update(kw)
    return DeploymentTarget(**defaults)


@pytest.fixture
def cluster() -> FakeKubernetes:
    return FakeKubernetes()


class TestSpecHash:
    def test_the_same_target_hashes_the_same(self) -> None:
        assert spec_hash(render(target())) == spec_hash(render(target()))

    def test_a_changed_replica_count_changes_the_hash(self) -> None:
        assert spec_hash(render(target(replicas=2))) != spec_hash(render(target(replicas=3)))

    def test_a_changed_image_changes_the_hash(self) -> None:
        assert spec_hash(render(target())) != spec_hash(
            render(target(image="vllm/vllm-openai:v0.12.0"))
        )

    def test_the_hash_is_short_enough_for_an_annotation(self) -> None:
        assert len(spec_hash(render(target()))) <= 63


class TestApply:
    def test_objects_are_created(self, cluster: FakeKubernetes) -> None:
        apply_objects(cluster, render(target()))

        kinds = {kind for kind, _, _ in cluster.applied}
        assert kinds == {"Deployment", "Service", "PodDisruptionBudget"}

    def test_apply_is_server_side_with_a_field_manager(self, cluster: FakeKubernetes) -> None:
        """Server-side apply, so we own only the fields we set."""
        apply_objects(cluster, render(target()))

        assert all(opts["field_manager"] == "aisvc" for opts in cluster.options)
        assert all(opts["force"] is True for opts in cluster.options)

    def test_applying_twice_is_idempotent(self, cluster: FakeKubernetes) -> None:
        objects = render(target())
        apply_objects(cluster, objects)
        apply_objects(cluster, objects)

        assert len(cluster.objects) == 3

    def test_the_spec_hash_is_recorded_on_the_deployment(self, cluster: FakeKubernetes) -> None:
        objects = render(target())
        apply_objects(cluster, objects)

        deployment = cluster.get("Deployment", "aisvc-serving", "llama-3.3-70b-instruct-blue")
        assert deployment["metadata"]["annotations"]["aisvc.io/spec-hash"] == spec_hash(objects)

    def test_dry_run_applies_nothing(self, cluster: FakeKubernetes) -> None:
        apply_objects(cluster, render(target()), dry_run=True)

        assert cluster.applied == []

    def test_an_api_failure_is_wrapped(self, cluster: FakeKubernetes) -> None:
        cluster.fail_on = "Deployment"

        with pytest.raises(KubernetesError, match="Deployment"):
            apply_objects(cluster, render(target()))


class TestDelete:
    def test_a_deployments_objects_are_removed(self, cluster: FakeKubernetes) -> None:
        from aisvc.k8s.client import delete_deployment

        apply_objects(cluster, render(target()))
        delete_deployment(cluster, "llama-3.3-70b-instruct", "blue", namespace="aisvc-serving")

        assert cluster.objects == {}

    def test_deleting_what_is_absent_is_not_an_error(self, cluster: FakeKubernetes) -> None:
        from aisvc.k8s.client import delete_deployment

        delete_deployment(cluster, "ghost", "blue", namespace="aisvc-serving")

    def test_dry_run_deletes_nothing(self, cluster: FakeKubernetes) -> None:
        from aisvc.k8s.client import delete_deployment

        apply_objects(cluster, render(target()))
        delete_deployment(
            cluster,
            "llama-3.3-70b-instruct",
            "blue",
            namespace="aisvc-serving",
            dry_run=True,
        )

        assert len(cluster.objects) == 3


class TestObserve:
    def test_nothing_deployed_observes_nothing(self, cluster: FakeKubernetes) -> None:
        assert observe_deployments(cluster, namespace="aisvc-serving") == {}

    def test_an_applied_deployment_is_observed(self, cluster: FakeKubernetes) -> None:
        apply_objects(cluster, render(target()))

        observed = observe_deployments(cluster, namespace="aisvc-serving")

        assert ("llama-3.3-70b-instruct", "blue") in observed

    def test_the_observation_carries_what_the_plan_needs(self, cluster: FakeKubernetes) -> None:
        objects = render(target(replicas=3))
        apply_objects(cluster, objects)

        deployed = observe_deployments(cluster, namespace="aisvc-serving")[
            ("llama-3.3-70b-instruct", "blue")
        ]

        assert isinstance(deployed, DeployedModel)
        assert deployed.replicas_desired == 3
        assert deployed.image == IMAGE
        assert deployed.spec_hash == spec_hash(objects)

    def test_readiness_is_reported(self, cluster: FakeKubernetes) -> None:
        apply_objects(cluster, render(target(replicas=2)))
        cluster.set_ready("llama-3.3-70b-instruct-blue", 1)

        deployed = observe_deployments(cluster, namespace="aisvc-serving")[
            ("llama-3.3-70b-instruct", "blue")
        ]

        assert deployed.replicas_ready == 1
        assert not deployed.fully_ready

    def test_a_deployment_is_ready_when_every_replica_is(self, cluster: FakeKubernetes) -> None:
        apply_objects(cluster, render(target(replicas=2)))
        cluster.set_ready("llama-3.3-70b-instruct-blue", 2)

        deployed = observe_deployments(cluster, namespace="aisvc-serving")[
            ("llama-3.3-70b-instruct", "blue")
        ]

        assert deployed.fully_ready

    def test_foreign_deployments_are_ignored(self, cluster: FakeKubernetes) -> None:
        """Only workloads this platform manages are its business."""
        cluster.apply(
            "Deployment",
            "aisvc-serving",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": "someone-elses-app",
                    "namespace": "aisvc-serving",
                    "labels": {"app": "other"},
                },
                "spec": {"replicas": 1},
            },
            field_manager="other",
            force=False,
        )

        assert observe_deployments(cluster, namespace="aisvc-serving") == {}

    def test_both_colours_are_observed_separately(self, cluster: FakeKubernetes) -> None:
        apply_objects(cluster, render(target(colour="blue")))
        apply_objects(cluster, render(target(colour="green")))

        observed = observe_deployments(cluster, namespace="aisvc-serving")

        assert set(observed) == {
            ("llama-3.3-70b-instruct", "blue"),
            ("llama-3.3-70b-instruct", "green"),
        }


class TestObserveWeights:
    """How the planner learns a promotion happened."""

    def configmap(self, cluster: FakeKubernetes, config: str) -> None:
        cluster.apply(
            "ConfigMap",
            "aisvc-serving",
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "aisvc-gateway-config",
                    "namespace": "aisvc-serving",
                },
                "data": {"config.yaml": config},
            },
            field_manager="aisvc",
            force=True,
        )

    def test_no_configmap_means_no_weights(self, cluster: FakeKubernetes) -> None:
        from aisvc.k8s.client import observe_weights

        assert observe_weights(cluster, namespace="aisvc-serving") == {}

    def test_weights_are_read_per_model_and_colour(self, cluster: FakeKubernetes) -> None:
        from aisvc.k8s.client import observe_weights

        self.configmap(
            cluster,
            "model_list:\n"
            "- model_name: m\n"
            "  litellm_params: {weight: 90}\n"
            "  model_info: {colour: blue}\n"
            "- model_name: m\n"
            "  litellm_params: {weight: 10}\n"
            "  model_info: {colour: green}\n",
        )

        assert observe_weights(cluster, namespace="aisvc-serving") == {
            "m": {"blue": 90, "green": 10}
        }

    def test_a_single_entry_defaults_to_all_the_traffic(self, cluster: FakeKubernetes) -> None:
        from aisvc.k8s.client import observe_weights

        self.configmap(
            cluster,
            "model_list:\n- model_name: m\n  litellm_params: {}\n  model_info: {colour: blue}\n",
        )

        assert observe_weights(cluster, namespace="aisvc-serving") == {"m": {"blue": 100}}

    def test_an_entry_without_a_colour_is_skipped(self, cluster: FakeKubernetes) -> None:
        from aisvc.k8s.client import observe_weights

        self.configmap(
            cluster,
            "model_list:\n- model_name: m\n  litellm_params: {weight: 50}\n  model_info: {}\n",
        )

        assert observe_weights(cluster, namespace="aisvc-serving") == {}

    def test_an_empty_config_is_not_an_error(self, cluster: FakeKubernetes) -> None:
        from aisvc.k8s.client import observe_weights

        self.configmap(cluster, "")

        assert observe_weights(cluster, namespace="aisvc-serving") == {}
