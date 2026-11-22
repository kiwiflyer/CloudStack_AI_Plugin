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
"""AMD (ROCm) GPU classes.

CloudStack discovers GPUs by PCI class and not by vendor — an MI300X
reports `Processing accelerators [1200]`, which `gpudiscovery.sh` matches
— so a zone can hand the engine AMD capacity. Everything the engine then
renders differently is pinned here: the Kubernetes extended resource, the
toleration, the default engine image and the node's own driver check.

The last group is the one that matters most to an existing install: an
`nvidia` class must render byte-for-byte as it did before the vendor
field existed, because `config_hash` drives rollouts and a changed hash
would restart every running deployment.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from aisvc.k8s import DeploymentTarget, config_hash, image_for, render
from aisvc.provision.cloudinit import GpuNodeUserdata, render_gpu_node_userdata
from aisvc.spec import PlatformSpec, SpecError, load_spec_from_string
from tests.unit.test_k8s_render import objects_by_kind
from tests.unit.test_k8s_render import target as nvidia_target
from tests.unit.test_spec import LICENSES, MINIMAL

#: What `config_hash` produced for the MINIMAL spec's one NVIDIA model
#: before `vendor` existed. Recorded, not computed: the whole point is
#: that adding AMD support cannot move it.
NVIDIA_CONFIG_HASH = "dbba8fcd3e085f4162e1c2801a77d521"


def _swap(text: str, old: str, new: str) -> str:
    """`str.replace` that refuses to match nothing.

    MINIMAL is a dedented block, so a fixture built by eyeballing the
    source's indentation silently produces the original string and the
    test then passes against the wrong spec.
    """
    if old not in text:
        raise AssertionError(f"fixture no longer contains {old!r}")
    return text.replace(old, new)


AMD_SPEC = _swap(
    MINIMAL,
    "    gpu_mem_gb: 80\n    mode: passthrough\n",
    "    gpu_mem_gb: 192\n    mode: passthrough\n    vendor: amd\n",
)


def amd_spec() -> PlatformSpec:
    return load_spec_from_string(AMD_SPEC, allowed_licenses=LICENSES)


def amd_target(**kw: Any) -> DeploymentTarget:
    loaded = amd_spec()
    model = loaded.models[0]
    pool = loaded.node_pool(model.node_pool)
    defaults: dict[str, Any] = {
        "model": model,
        "pool": pool,
        "gpu_class": loaded.gpu_class(pool.gpu_class),
        "serving_profile": loaded.serving_profile_for(model),
        "image": "rocm/vllm:test",
        "replicas": 2,
        "colour": "blue",
        "weights_uri": "s3://models/meta-llama/Llama-3.3-70B-Instruct/0f3f4a/",
    }
    defaults.update(kw)
    return DeploymentTarget(**defaults)


def userdata(**kw: Any) -> GpuNodeUserdata:
    defaults: dict[str, Any] = {
        "hostname": "aisvc-h100-pool-0",
        "pool": "h100-pool",
        "gpu_class": "h100-8x",
        "zone": "zone-east-1",
        "cks_cluster": "aisvc-east-1",
        "join_endpoint": "10.0.0.1:6443",
        "join_token": "abcdef.0123456789abcdef",
        "ca_cert_hash": "sha256:" + "0" * 64,
    }
    defaults.update(kw)
    return GpuNodeUserdata(**defaults)


class TestAGpuClassDeclaresItsVendor:
    def test_it_defaults_to_nvidia_so_every_existing_spec_is_unchanged(self) -> None:
        loaded = load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)
        assert loaded.gpu_class("h100-8x").vendor == "nvidia"

    def test_it_accepts_amd(self) -> None:
        assert amd_spec().gpu_class("h100-8x").vendor == "amd"

    def test_it_refuses_a_vendor_nothing_can_render(self) -> None:
        with pytest.raises(SpecError):
            load_spec_from_string(
                _swap(AMD_SPEC, "vendor: amd", "vendor: intel"),
                allowed_licenses=LICENSES,
            )

    def test_mig_is_refused_on_amd_because_mig_is_nvidias(self) -> None:
        """MIG is an NVIDIA partitioning feature; AMD's equivalent is not it."""
        with pytest.raises(SpecError) as caught:
            load_spec_from_string(
                _swap(AMD_SPEC, "mode: passthrough", "mode: mig"),
                allowed_licenses=LICENSES,
            )
        assert "mig" in str(caught.value).lower()


class TestRenderingFollowsTheVendor:
    def test_an_amd_class_asks_for_amd_gpus(self) -> None:
        deployment = objects_by_kind(render(amd_target()))["Deployment"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert "amd.com/gpu" in container["resources"]["limits"]
        assert "amd.com/gpu" in container["resources"]["requests"]
        assert "nvidia.com/gpu" not in container["resources"]["limits"]

    def test_the_count_is_still_the_models_tensor_parallel(self) -> None:
        target = amd_target()
        deployment = objects_by_kind(render(target))["Deployment"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["limits"]["amd.com/gpu"] == target.model.tensor_parallel

    def test_an_amd_pod_tolerates_the_amd_taint_not_nvidias(self) -> None:
        deployment = objects_by_kind(render(amd_target()))["Deployment"]
        keys = {t["key"] for t in deployment["spec"]["template"]["spec"]["tolerations"]}
        assert "amd.com/gpu" in keys
        assert "nvidia.com/gpu" not in keys

    def test_an_nvidia_class_still_asks_for_nvidia_gpus(self) -> None:
        deployment = objects_by_kind(render(nvidia_target()))["Deployment"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert "nvidia.com/gpu" in container["resources"]["limits"]


class TestTheNvidiaRenderDidNotMove:
    """A changed hash restarts every running deployment (see `config_hash`)."""

    def test_the_recorded_nvidia_config_hash_is_unchanged(self) -> None:
        assert config_hash(nvidia_target()) == NVIDIA_CONFIG_HASH

    def test_an_amd_class_hashes_differently(self) -> None:
        assert config_hash(amd_target()) != NVIDIA_CONFIG_HASH


class TestDefaultImagesAreVendorSpecific:
    def test_nvidia_vllm_is_the_image_it_always_was(self) -> None:
        model = load_spec_from_string(MINIMAL, allowed_licenses=LICENSES).models[0]
        assert image_for(model, "nvidia") == "vllm/vllm-openai:v0.11.0"

    def test_the_vendor_defaults_to_nvidia(self) -> None:
        model = load_spec_from_string(MINIMAL, allowed_licenses=LICENSES).models[0]
        assert image_for(model) == image_for(model, "nvidia")

    def test_amd_vllm_gets_a_rocm_image(self) -> None:
        model = amd_spec().models[0]
        assert image_for(model, "amd").startswith("rocm/vllm:")

    def test_a_model_pinning_its_own_image_wins_for_either_vendor(self) -> None:
        pinned = _swap(
            AMD_SPEC,
            "    engine: vllm\n",
            "    engine: vllm\n    image: example.invalid/mine:1\n",
        )
        model = load_spec_from_string(pinned, allowed_licenses=LICENSES).models[0]
        assert image_for(model, "amd") == "example.invalid/mine:1"
        assert image_for(model, "nvidia") == "example.invalid/mine:1"

    def test_trtllm_on_amd_is_refused_by_name(self) -> None:
        """TensorRT-LLM is NVIDIA-only; there is no ROCm build to default to."""
        model = amd_spec().models[0].model_copy(update={"engine": "trtllm"})
        with pytest.raises(ValueError) as caught:
            image_for(model, "amd")
        assert "trtllm" in str(caught.value)

    def test_sglang_on_amd_is_refused_and_says_to_pin_an_image(self) -> None:
        """SGLang publishes per-silicon ROCm tags (mi30x, mi35x), so the
        vendor alone cannot choose one. Refuse loudly rather than guess."""
        model = amd_spec().models[0].model_copy(update={"engine": "sglang"})
        with pytest.raises(ValueError) as caught:
            image_for(model, "amd")
        assert "image" in str(caught.value).lower()


class TestANodeChecksTheDriverItActuallyHas:
    def test_an_nvidia_node_is_unchanged(self) -> None:
        rendered = render_gpu_node_userdata(userdata())
        assert "nvidia-smi -L" in rendered
        assert "nvidia-ctk runtime configure" in rendered
        assert "nvidia.com/gpu.present=true" in rendered

    def test_an_amd_node_does_not_run_nvidia_tools(self) -> None:
        rendered = render_gpu_node_userdata(userdata(vendor="amd"))
        assert "nvidia-smi" not in rendered
        assert "nvidia-ctk" not in rendered

    def test_an_amd_node_proves_its_gpus_are_visible(self) -> None:
        """Same contract as the NVIDIA gate: a node that joins without a
        working GPU still gets pods, so the check must fail the boot."""
        rendered = render_gpu_node_userdata(userdata(vendor="amd"))
        assert "amd-smi" in rendered
        assert "no GPU visible" in rendered

    def test_an_amd_node_labels_itself_for_the_amd_device_plugin(self) -> None:
        rendered = render_gpu_node_userdata(userdata(vendor="amd"))
        assert "amd.com/gpu.present=true" in rendered
        assert "nvidia.com/gpu.present" not in rendered

    @pytest.mark.parametrize("vendor", ["nvidia", "amd"])
    def test_the_document_still_parses_as_yaml(self, vendor: str) -> None:
        """The driver block is a Jinja conditional inside a YAML list,
        which is a good way to emit something that renders but will not
        load on the node, where nobody is watching."""
        doc = yaml.safe_load(
            render_gpu_node_userdata(userdata(vendor=vendor, weight_cache_device="/dev/nvme1n1"))
        )
        assert isinstance(doc, dict)
        assert doc["runcmd"]

    @pytest.mark.parametrize("vendor", ["nvidia", "amd"])
    def test_the_node_still_joins_the_cluster_last(self, vendor: str) -> None:
        doc = yaml.safe_load(render_gpu_node_userdata(userdata(vendor=vendor)))
        assert "kubeadm join" in doc["runcmd"][-1][2]
