"""Rendering serving workloads (SPEC.md section 4, ADR-0001).

Plain Deployment, Service and PodDisruptionBudget — no KServe CRD.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from aisvc.k8s import (
    DeploymentTarget,
    engine_args,
    render,
    render_yaml,
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
        "color": "blue",
        "weights_uri": "s3://models/meta-llama/Llama-3.3-70B-Instruct/0f3f4a/",
    }
    defaults.update(kw)
    return DeploymentTarget(**defaults)


def objects_by_kind(objs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(o["kind"]): o for o in objs}


class TestRenderedObjects:
    def test_it_renders_a_deployment_service_and_pdb(self) -> None:
        kinds = {o["kind"] for o in render(target())}
        assert kinds == {"Deployment", "Service", "PodDisruptionBudget"}

    def test_it_renders_no_kserve_crd(self) -> None:
        """ADR-0001: plain Kubernetes objects only."""
        for obj in render(target()):
            assert "kserve" not in str(obj.get("apiVersion", "")).lower()

    def test_names_carry_the_model_and_colour_so_blue_and_green_coexist(self) -> None:
        blue = objects_by_kind(render(target(color="blue")))["Deployment"]
        green = objects_by_kind(render(target(color="green")))["Deployment"]
        blue_name = blue["metadata"]["name"]
        green_name = green["metadata"]["name"]
        assert blue_name != green_name
        assert "llama-3.3-70b-instruct" in str(blue_name)
        assert str(blue_name).endswith("-blue")
        assert str(green_name).endswith("-green")

    def test_every_object_is_labelled_for_selection(self) -> None:
        for obj in render(target()):
            labels = obj["metadata"]["labels"]
            assert labels["aisvc.io/model"] == "llama-3.3-70b-instruct"
            assert labels["aisvc.io/colour"] == "blue"
            assert labels["app.kubernetes.io/managed-by"] == "aisvc"

    def test_the_namespace_is_set_on_every_object(self) -> None:
        for obj in render(target(), namespace="aisvc-serving"):
            assert obj["metadata"]["namespace"] == "aisvc-serving"


class TestDeployment:
    def deployment(self, **kw: Any) -> dict[str, Any]:
        return objects_by_kind(render(target(**kw)))["Deployment"]

    def test_replica_count_comes_from_the_target(self) -> None:
        assert self.deployment(replicas=3)["spec"]["replicas"] == 3

    def test_gpu_limit_comes_from_tensor_parallel(self) -> None:
        container = self.deployment()["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["limits"]["nvidia.com/gpu"] == 2

    def test_gpu_request_matches_the_limit(self) -> None:
        """GPUs are not overcommittable; request must equal limit."""
        resources = self.deployment()["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert resources["requests"]["nvidia.com/gpu"] == resources["limits"]["nvidia.com/gpu"]

    def test_pods_are_pinned_to_the_models_node_pool(self) -> None:
        pod = self.deployment()["spec"]["template"]["spec"]
        assert pod["nodeSelector"]["aisvc.io/node-pool"] == "h100-pool"

    def test_replicas_are_spread_across_hosts_by_anti_affinity(self) -> None:
        """SPEC.md section 11: at least 2 replicas, spread across hosts."""
        affinity = self.deployment()["spec"]["template"]["spec"]["affinity"]
        rule = affinity["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
        assert rule["topologyKey"] == "kubernetes.io/hostname"
        assert rule["labelSelector"]["matchLabels"]["aisvc.io/model"] == "llama-3.3-70b-instruct"

    def test_the_weight_cache_is_mounted_from_the_node(self) -> None:
        """SPEC.md section 11: weights pre-staged on node-local NVMe."""
        pod = self.deployment()["spec"]["template"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "weight-cache")
        assert volume["hostPath"]["path"] == "/var/lib/aisvc/weights"
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "weight-cache")
        assert mount["mountPath"] == "/var/lib/aisvc/weights"
        assert mount["readOnly"] is True

    def test_the_container_uses_the_pinned_image(self) -> None:
        assert self.deployment()["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE

    def test_the_image_is_recorded_as_an_annotation_for_drift_checks(self) -> None:
        annotations = self.deployment()["metadata"]["annotations"]
        assert annotations["aisvc.io/engine-image"] == IMAGE
        assert annotations["aisvc.io/engine"] == "vllm"

    def test_the_weights_uri_is_recorded(self) -> None:
        annotations = self.deployment()["metadata"]["annotations"]
        assert annotations["aisvc.io/weights-uri"].startswith("s3://models/")

    def test_inference_pods_get_no_service_account_token(self) -> None:
        """Inference pods have no business talking to the API server."""
        pod = self.deployment()["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False

    def test_the_container_does_not_run_as_root(self) -> None:
        container = self.deployment()["spec"]["template"]["spec"]["containers"][0]
        assert container["securityContext"]["runAsNonRoot"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False


class TestProbes:
    def container(self) -> dict[str, Any]:
        deployment = objects_by_kind(render(target()))["Deployment"]
        container: dict[str, Any] = deployment["spec"]["template"]["spec"]["containers"][0]
        return container

    def test_readiness_and_liveness_use_the_engine_health_endpoint(self) -> None:
        container = self.container()
        assert container["readinessProbe"]["httpGet"]["path"] == "/health"
        assert container["livenessProbe"]["httpGet"]["path"] == "/health"

    def test_a_startup_probe_tolerates_the_cold_start(self) -> None:
        """SPEC.md section 11 targets under three minutes for a 70B FP8."""
        startup = self.container()["startupProbe"]
        budget = startup["periodSeconds"] * startup["failureThreshold"]
        assert budget >= 180

    def test_liveness_does_not_fire_during_a_cold_start(self) -> None:
        """A liveness probe racing the startup probe restart-loops the pod."""
        container = self.container()
        startup = container["startupProbe"]
        assert container["livenessProbe"].get("initialDelaySeconds", 0) == 0
        assert startup["failureThreshold"] * startup["periodSeconds"] >= 180


class TestService:
    def service(self) -> dict[str, Any]:
        return objects_by_kind(render(target()))["Service"]

    def test_it_selects_this_colour_only(self) -> None:
        """Blue and green need separate Services for the gateway to weight."""
        selector = self.service()["spec"]["selector"]
        assert selector["aisvc.io/model"] == "llama-3.3-70b-instruct"
        assert selector["aisvc.io/colour"] == "blue"

    def test_it_exposes_the_engine_port(self) -> None:
        port = self.service()["spec"]["ports"][0]
        assert port["port"] == 8000
        assert port["targetPort"] == 8000


class TestPodDisruptionBudget:
    def test_it_keeps_a_replica_serving_during_disruption(self) -> None:
        pdb = objects_by_kind(render(target(replicas=2)))["PodDisruptionBudget"]
        assert pdb["spec"]["minAvailable"] == 1

    def test_a_single_replica_deployment_gets_no_blocking_budget(self) -> None:
        """minAvailable 1 on one replica would block every drain forever."""
        pdb = objects_by_kind(render(target(replicas=1)))["PodDisruptionBudget"]
        assert pdb["spec"]["minAvailable"] == 0


class TestEngineArgs:
    def test_serving_profile_parameters_become_flags(self) -> None:
        args = engine_args(spec().models[0], spec().serving_profile_for(spec().models[0]))
        assert "--gpu-memory-utilization" in args
        assert "0.92" in args
        assert "--max-num-seqs" in args
        assert "256" in args

    def test_underscores_become_dashes(self) -> None:
        args = engine_args(spec().models[0], spec().serving_profile_for(spec().models[0]))
        assert not any(a.startswith("--") and "_" in a for a in args)

    def test_a_true_boolean_is_a_bare_flag(self) -> None:
        args = engine_args(spec().models[0], spec().serving_profile_for(spec().models[0]))
        assert "--enable-prefix-caching" in args
        assert "true" not in args

    def test_a_false_boolean_is_omitted(self) -> None:
        model = spec().models[0]
        profile = type(spec().serving_profile_for(model))(  # type: ignore[misc]
            name="p", params={"enable_prefix_caching": False}
        )
        assert "--enable-prefix-caching" not in engine_args(model, profile)

    def test_model_fields_are_passed_even_without_a_profile(self) -> None:
        args = engine_args(spec().models[0], None)
        assert "--tensor-parallel-size" in args
        assert "2" in args
        assert "--max-model-len" in args
        assert "32768" in args
        assert "--quantization" in args

    def test_the_served_model_name_is_the_catalog_name(self) -> None:
        """Tenants address the model by its catalog name, not the HF repo."""
        args = engine_args(spec().models[0], None)
        index = args.index("--served-model-name")
        assert args[index + 1] == "llama-3.3-70b-instruct"

    def test_a_profile_cannot_override_tensor_parallel(self) -> None:
        """tensor_parallel is validated against the GPU class; the profile
        must not be able to contradict it."""
        model = spec().models[0]
        profile = type(spec().serving_profile_for(model))(  # type: ignore[misc]
            name="p", params={"tensor_parallel_size": 8}
        )
        args = engine_args(model, profile)
        assert args.count("--tensor-parallel-size") == 1
        index = args.index("--tensor-parallel-size")
        assert args[index + 1] == "2"

    def test_args_are_deterministic(self) -> None:
        model = spec().models[0]
        profile = spec().serving_profile_for(model)
        assert engine_args(model, profile) == engine_args(model, profile)

    def test_an_unknown_engine_is_rejected(self) -> None:
        model = spec().models[0].model_copy(update={"engine": "mystery"})
        with pytest.raises(ValueError, match="mystery"):
            engine_args(model, None)


class TestRenderYaml:
    def test_it_produces_a_multi_document_manifest(self) -> None:
        text = render_yaml(target())
        docs = list(yaml.safe_load_all(text))
        assert len(docs) == 3
        assert {d["kind"] for d in docs} == {"Deployment", "Service", "PodDisruptionBudget"}

    def test_it_is_deterministic(self) -> None:
        assert render_yaml(target()) == render_yaml(target())
