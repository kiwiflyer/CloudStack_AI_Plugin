"""platform.yaml parsing and validation (SPEC.md section 3)."""

from __future__ import annotations

import textwrap
from typing import Any

import pytest
import yaml

from aisvc.spec import PlatformSpec, SpecError, load_spec_from_string

MINIMAL = textwrap.dedent(
    """
    apiVersion: aisvc/v1
    zones:
      - name: zone-east-1
        cloudstack_zone: "East-1"
        cks_cluster: aisvc-east-1
    gpuClasses:
      - name: h100-8x
        host_tag: h100-sxm
        service_offering: "GPU-H100-8x"
        gpus_per_node: 8
        gpu_mem_gb: 80
        mode: passthrough
    nodePools:
      - name: h100-pool
        zone: zone-east-1
        gpu_class: h100-8x
        min_nodes: 1
        max_nodes: 6
        template: "gpu-golden-2026.11"
    huggingface:
      endpoint: https://huggingface.co
      token_secret: aisvc/hf-token
    models:
      - name: llama-3.3-70b-instruct
        source: hf://meta-llama/Llama-3.3-70B-Instruct@0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b
        license: llama3.3
        task: chat
        engine: vllm
        quantization: fp8
        tensor_parallel: 2
        max_model_len: 32768
        node_pool: h100-pool
        replicas: {min: 2, max: 4}
        serving_profile: h100-throughput
        price_tier: standard
        architecture:
          params_b: 70.6
          num_layers: 80
          num_kv_heads: 8
          head_dim: 128
    tuningProfiles:
      serving:
        - name: h100-throughput
          params:
            gpu_memory_utilization: 0.92
            max_num_seqs: 256
            enable_prefix_caching: true
            kv_cache_dtype: fp8
      generation:
        - name: precise
          params:
            temperature: {default: 0.2, min: 0.0, max: 0.5, policy: clamp}
            max_tokens: {default: 1024, max: 4096, policy: clamp}
    assignments:
      - {scope: deployment, target: llama-3.3-70b-instruct, serving_profile: h100-throughput}
    priceTiers:
      - {name: standard, input_per_mtok: 0.60, output_per_mtok: 1.80, currency: USD}
    tenants:
      - name: acme
        cloudstack_domain: "/Customers/Acme"
        cloudstack_account: acme
        models: [llama-3.3-70b-instruct]
        tier: shared
    """
)

LICENSES = ["llama3.3", "apache-2.0", "mit"]


def load(text: str) -> PlatformSpec:
    return load_spec_from_string(text, allowed_licenses=LICENSES)


def mutate(**changes: Any) -> str:
    """Return MINIMAL as YAML text with top-level keys replaced."""
    doc = yaml.safe_load(MINIMAL)
    for key, value in changes.items():
        doc[key] = value
    return yaml.safe_dump(doc)


def problems_of(text: str) -> list[str]:
    with pytest.raises(SpecError) as exc:
        load(text)
    return [str(p) for p in exc.value.problems]


class TestHappyPath:
    def test_minimal_spec_loads(self) -> None:
        spec = load(MINIMAL)
        assert spec.api_version == "aisvc/v1"
        assert [z.name for z in spec.zones] == ["zone-east-1"]
        assert spec.models[0].name == "llama-3.3-70b-instruct"

    def test_model_resolves_its_pool_class_and_zone(self) -> None:
        spec = load(MINIMAL)
        model = spec.models[0]
        pool = spec.node_pool(model.node_pool)
        gpu_class = spec.gpu_class(pool.gpu_class)
        assert gpu_class.gpus_per_node == 8
        assert pool.zone == "zone-east-1"

    def test_source_revision_is_exposed(self) -> None:
        spec = load(MINIMAL)
        model = spec.models[0]
        assert model.repo_id == "meta-llama/Llama-3.3-70B-Instruct"
        assert model.revision == "0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b"


class TestSchema:
    def test_unknown_top_level_key_is_an_error(self) -> None:
        assert any("surprise" in p for p in problems_of(mutate(surprise={"a": 1})))

    def test_unknown_model_key_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["turbo"] = True
        assert any("turbo" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_wrong_api_version_is_an_error(self) -> None:
        assert any("apiVersion" in p for p in problems_of(mutate(apiVersion="aisvc/v2")))

    def test_invalid_yaml_is_reported_not_raised_raw(self) -> None:
        with pytest.raises(SpecError, match="not valid YAML"):
            load("models: [oops")

    def test_non_mapping_document_is_an_error(self) -> None:
        with pytest.raises(SpecError, match="mapping"):
            load("- a\n- b\n")


class TestReferences:
    def test_unknown_node_pool_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["node_pool"] = "ghost-pool"
        assert any("ghost-pool" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_unknown_gpu_class_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["gpu_class"] = "ghost-class"
        assert any("ghost-class" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_unknown_zone_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["zone"] = "ghost-zone"
        assert any("ghost-zone" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_unknown_price_tier_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["price_tier"] = "ghost-tier"
        assert any("ghost-tier" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_unknown_serving_profile_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["serving_profile"] = "ghost-profile"
        assert any("ghost-profile" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_tenant_referencing_unknown_model_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tenants"][0]["models"] = ["ghost-model"]
        assert any("ghost-model" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_assignment_to_unknown_target_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["assignments"][0]["target"] = "ghost-deployment"
        assert any("ghost-deployment" in p for p in problems_of(yaml.safe_dump(doc)))


class TestTensorParallel:
    def test_tensor_parallel_above_gpus_per_node_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["tensor_parallel"] = 16
        found = problems_of(yaml.safe_dump(doc))
        assert any("tensor_parallel" in p and "8" in p for p in found)

    def test_tensor_parallel_must_be_a_power_of_two(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["tensor_parallel"] = 3
        assert any("power of two" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_tensor_parallel_of_one_is_allowed(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["tensor_parallel"] = 1
        doc["models"][0]["max_model_len"] = 4096
        doc["models"][0]["architecture"]["params_b"] = 7.0
        load(yaml.safe_dump(doc))


class TestSupplyChain:
    def test_source_without_a_commit_sha_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["source"] = "hf://meta-llama/Llama-3.3-70B-Instruct"
        assert any("commit SHA" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_source_pinned_to_a_branch_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["source"] = "hf://meta-llama/Llama-3.3-70B-Instruct@main"
        assert any("commit SHA" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_license_outside_the_allow_list_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["license"] = "proprietary-eula"
        assert any("proprietary-eula" in p for p in problems_of(yaml.safe_dump(doc)))


class TestCapacityRules:
    def test_model_that_cannot_fit_is_rejected_with_the_shortfall(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["quantization"] = "bf16"
        doc["models"][0]["tensor_parallel"] = 1
        found = problems_of(yaml.safe_dump(doc))
        assert any("short by" in p and "GiB" in p for p in found)

    def test_capacity_check_is_skipped_without_architecture_facts(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        del doc["models"][0]["architecture"]
        spec = load(yaml.safe_dump(doc))
        assert spec.models[0].architecture is None


class TestServingProfiles:
    def test_parameter_outside_the_engine_allow_list_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tuningProfiles"]["serving"][0]["params"]["make_it_fast"] = True
        assert any("make_it_fast" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_concurrency_beyond_the_kv_budget_is_a_warning_not_an_error(self) -> None:
        """SPEC.md's own example exceeds a literal reading of this rule."""
        spec = load(MINIMAL)
        assert any("max_num_seqs" in w for w in spec.warnings)


class TestGenerationProfiles:
    def test_max_tokens_above_the_context_length_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tuningProfiles"]["generation"][0]["params"]["max_tokens"]["max"] = 999999
        assert any("max_tokens" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_default_outside_min_max_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tuningProfiles"]["generation"][0]["params"]["temperature"]["default"] = 2.0
        assert any("temperature" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_unknown_policy_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tuningProfiles"]["generation"][0]["params"]["temperature"]["policy"] = "nope"
        assert any("policy" in p for p in problems_of(yaml.safe_dump(doc)))


class TestTenants:
    def test_dedicated_tenant_without_its_own_pool_is_an_error(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tenants"][0]["tier"] = "dedicated"
        assert any("dedicated" in p for p in problems_of(yaml.safe_dump(doc)))

    def test_dedicated_tenant_with_its_own_pool_is_allowed(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tenants"][0]["tier"] = "dedicated"
        doc["nodePools"][0]["tenant"] = "acme"
        load(yaml.safe_dump(doc))

    def test_shared_tenant_cannot_use_a_dedicated_pool(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["nodePools"][0]["tenant"] = "someone-else"
        assert any("shared" in p for p in problems_of(yaml.safe_dump(doc)))


class TestErrorReporting:
    def test_every_problem_is_reported_not_just_the_first(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["node_pool"] = "ghost-pool"
        doc["models"][0]["price_tier"] = "ghost-tier"
        found = problems_of(yaml.safe_dump(doc))
        assert any("ghost-pool" in p for p in found)
        assert any("ghost-tier" in p for p in found)

    def test_problems_carry_the_path_that_failed(self) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["price_tier"] = "ghost-tier"
        with pytest.raises(SpecError) as exc:
            load(yaml.safe_dump(doc))
        assert exc.value.problems[0].path.startswith("models[0]")
