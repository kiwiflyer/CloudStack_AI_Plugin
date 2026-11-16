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
"""Hugging Face sourcing: search, check, mirror, scan (SPEC.md section 5)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from aisvc.capacity import ModelArchitecture
from aisvc.hf import (
    CompatibilityReport,
    HfError,
    ModelSourcing,
    RepoFile,
    RepoInfo,
    architecture_from_config,
)
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

SHA = "0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b"
REPO = "meta-llama/Llama-3.3-70B-Instruct"

LLAMA_CONFIG: dict[str, Any] = {
    "architectures": ["LlamaForCausalLM"],
    "num_hidden_layers": 80,
    "num_attention_heads": 64,
    "num_key_value_heads": 8,
    "hidden_size": 8192,
    "max_position_embeddings": 131072,
    "torch_dtype": "bfloat16",
}


def repo(**kw: Any) -> RepoInfo:
    defaults: dict[str, Any] = {
        "repo_id": REPO,
        "revision": SHA,
        "license": "llama3.3",
        "gated": False,
        "private": False,
        "pipeline_tag": "text-generation",
        "config": LLAMA_CONFIG,
        "files": (
            RepoFile(path="config.json", size=1024),
            RepoFile(path="model-00001-of-00002.safetensors", size=70_000_000_000),
            RepoFile(path="tokenizer.json", size=2048),
        ),
    }
    defaults.update(kw)
    return RepoInfo(**defaults)


class FakeHub:
    """Stands in for huggingface_hub; records what was asked for."""

    def __init__(self, info: RepoInfo | None = None) -> None:
        self.repo_info = info or repo()
        self.searches: list[dict[str, Any]] = []
        self.downloads: list[tuple[str, str]] = []
        self.blobs: dict[str, bytes] = {
            "config.json": b'{"architectures": ["LlamaForCausalLM"]}',
            "model-00001-of-00002.safetensors": b"safetensor-bytes",
            "tokenizer.json": b"{}",
        }

    def search(self, **kw: Any) -> list[RepoInfo]:
        self.searches.append(kw)
        return [self.repo_info]

    def info(self, repo_id: str, revision: str) -> RepoInfo:
        return self.repo_info

    def resolve_revision(self, repo_id: str, revision: str) -> str:
        """Any ref resolves to whatever revision this repo is pinned at."""
        return self.repo_info.revision

    def download(self, repo_id: str, revision: str, path: str) -> bytes:
        self.downloads.append((path, revision))
        return self.blobs[path]


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


@pytest.fixture
def registry() -> ModelRegistry:
    return ModelRegistry(InMemoryObjectStore(), bucket="models")


def sourcing(hub: FakeHub, registry: ModelRegistry, **kw: Any) -> ModelSourcing:
    defaults: dict[str, Any] = {
        "hub": hub,
        "registry": registry,
        "allowed_licenses": LICENSES,
        "settings": load_spec_from_string(MINIMAL, allowed_licenses=LICENSES).document.huggingface,
    }
    defaults.update(kw)
    return ModelSourcing(**defaults)


class TestArchitectureFromConfig:
    def test_it_reads_layers_heads_and_dim(self) -> None:
        arch = architecture_from_config(LLAMA_CONFIG, params_b=70.6)
        assert isinstance(arch, ModelArchitecture)
        assert arch.num_layers == 80
        assert arch.num_kv_heads == 8
        assert arch.head_dim == 8192 // 64

    def test_head_dim_is_taken_directly_when_present(self) -> None:
        arch = architecture_from_config({**LLAMA_CONFIG, "head_dim": 128}, params_b=70.6)
        assert arch.head_dim == 128

    def test_multi_query_falls_back_to_attention_heads(self) -> None:
        config = {k: v for k, v in LLAMA_CONFIG.items() if k != "num_key_value_heads"}
        arch = architecture_from_config(config, params_b=7.0)
        assert arch.num_kv_heads == 64

    def test_a_config_without_shape_fields_is_an_error(self) -> None:
        with pytest.raises(HfError, match=re.escape("config.json")):
            architecture_from_config({"architectures": ["X"]}, params_b=7.0)


class TestCompatibilityCheck:
    def test_a_supported_model_is_compatible(self, hub: FakeHub, registry: ModelRegistry) -> None:
        report = sourcing(hub, registry).check(REPO, SHA)

        assert isinstance(report, CompatibilityReport)
        assert report.compatible
        assert report.problems == []

    def test_the_report_names_the_architecture_and_size(
        self, hub: FakeHub, registry: ModelRegistry
    ) -> None:
        report = sourcing(hub, registry).check(REPO, SHA)

        assert report.architecture == "LlamaForCausalLM"
        assert report.download_bytes == 70_000_003_072
        assert report.context_length == 131072

    def test_an_unsupported_architecture_is_flagged(self, registry: ModelRegistry) -> None:
        hub = FakeHub(repo(config={**LLAMA_CONFIG, "architectures": ["MambaLMHeadModel"]}))
        report = sourcing(hub, registry).check(REPO, SHA)

        assert not report.compatible
        assert any("MambaLMHeadModel" in p for p in report.problems)

    def test_a_licence_off_the_allow_list_is_flagged(self, registry: ModelRegistry) -> None:
        hub = FakeHub(repo(license="proprietary-eula"))
        report = sourcing(hub, registry).check(REPO, SHA)

        assert not report.compatible
        assert any("proprietary-eula" in p for p in report.problems)

    def test_a_gated_repo_is_reported_before_any_download(self, registry: ModelRegistry) -> None:
        """SPEC.md section 5: the check step reports gating up front."""
        hub = FakeHub(repo(gated=True))
        report = sourcing(hub, registry).check(REPO, SHA)

        assert report.gated
        assert any("gated" in p.lower() for p in report.warnings)
        assert hub.downloads == []

    def test_remote_code_is_flagged(self, registry: ModelRegistry) -> None:
        hub = FakeHub(
            repo(
                config={**LLAMA_CONFIG, "auto_map": {"AutoModel": "modeling_x.MyModel"}},
                files=(RepoFile(path="modeling_x.py", size=100),),
            )
        )
        report = sourcing(hub, registry).check(REPO, SHA)

        assert report.requires_remote_code
        assert not report.compatible

    def test_pickle_only_repos_are_flagged(self, registry: ModelRegistry) -> None:
        """SPEC.md section 14: pickle-only repos are rejected by default."""
        hub = FakeHub(
            repo(
                files=(
                    RepoFile(path="config.json", size=1024),
                    RepoFile(path="pytorch_model.bin", size=70_000_000_000),
                )
            )
        )
        report = sourcing(hub, registry).check(REPO, SHA)

        assert not report.compatible
        assert any("pickle" in p.lower() for p in report.problems)

    def test_a_repo_with_both_formats_is_compatible(self, registry: ModelRegistry) -> None:
        """Only pickle-*only* repos are refused; safetensors wins."""
        hub = FakeHub(
            repo(
                files=(
                    RepoFile(path="config.json", size=1024),
                    RepoFile(path="model.safetensors", size=70_000_000_000),
                    RepoFile(path="pytorch_model.bin", size=70_000_000_000),
                )
            )
        )
        report = sourcing(hub, registry).check(REPO, SHA)

        assert report.compatible

    def test_the_report_says_which_gpu_classes_can_serve_it(
        self, hub: FakeHub, registry: ModelRegistry
    ) -> None:
        """SPEC.md section 5: `capacity` adds which classes and TP sizes fit."""
        spec = load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)
        report = sourcing(hub, registry).check(
            REPO, SHA, gpu_classes=spec.document.gpu_classes, quantization="fp8"
        )

        assert report.placements
        assert any(p.gpu_class == "h100-8x" and p.fits for p in report.placements)

    def test_placements_report_the_smallest_workable_tensor_parallel(
        self, hub: FakeHub, registry: ModelRegistry
    ) -> None:
        spec = load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)
        report = sourcing(hub, registry).check(
            REPO, SHA, gpu_classes=spec.document.gpu_classes, quantization="fp8"
        )

        fitting = [p for p in report.placements if p.fits]
        assert fitting
        assert all(p.tensor_parallel & (p.tensor_parallel - 1) == 0 for p in fitting)


class TestMirror:
    def test_the_revision_is_resolved_to_a_commit_sha(
        self, hub: FakeHub, registry: ModelRegistry
    ) -> None:
        """SPEC.md section 5: the engine resolves to a SHA so mirrors are immutable."""
        manifest = sourcing(hub, registry).mirror(REPO, "main")

        assert manifest.revision == SHA
        assert all(revision == SHA for _, revision in hub.downloads)

    def test_only_allow_listed_patterns_are_downloaded(
        self, hub: FakeHub, registry: ModelRegistry
    ) -> None:
        hub.repo_info = repo(
            files=(
                RepoFile(path="config.json", size=10),
                RepoFile(path="model-00001-of-00002.safetensors", size=10),
                RepoFile(path="tokenizer.json", size=10),
                RepoFile(path="README.md", size=10),
            )
        )
        hub.blobs["README.md"] = b"docs"
        sourcing(hub, registry).mirror(REPO, SHA)

        assert "README.md" not in [path for path, _ in hub.downloads]

    def test_every_file_is_digested_into_the_manifest(
        self, hub: FakeHub, registry: ModelRegistry
    ) -> None:
        manifest = sourcing(hub, registry).mirror(REPO, SHA)

        assert {e.path for e in manifest.files} == {
            "config.json",
            "model-00001-of-00002.safetensors",
            "tokenizer.json",
        }
        assert all(len(e.sha256) == 64 for e in manifest.files)

    def test_the_mirror_verifies_after_upload(self, hub: FakeHub, registry: ModelRegistry) -> None:
        sourcing(hub, registry).mirror(REPO, SHA)
        assert registry.verify(REPO, SHA).ok

    def test_the_licence_is_recorded_on_the_manifest(
        self, hub: FakeHub, registry: ModelRegistry
    ) -> None:
        manifest = sourcing(hub, registry).mirror(REPO, SHA, domain="/Customers/Acme")

        assert manifest.license == "llama3.3"
        assert manifest.domain == "/Customers/Acme"

    def test_mirror_rejects_pickle_weights(self, registry: ModelRegistry) -> None:
        """SPEC.md section 12, named test."""
        hub = FakeHub(
            repo(
                files=(
                    RepoFile(path="config.json", size=10),
                    RepoFile(path="pytorch_model.bin", size=10),
                )
            )
        )
        with pytest.raises(HfError, match="pickle"):
            sourcing(hub, registry).mirror(REPO, SHA)

    def test_mirror_refuses_an_incompatible_model(self, registry: ModelRegistry) -> None:
        hub = FakeHub(repo(license="proprietary-eula"))
        with pytest.raises(HfError, match="proprietary-eula"):
            sourcing(hub, registry).mirror(REPO, SHA)

    def test_an_already_mirrored_revision_is_not_downloaded_again(
        self, hub: FakeHub, registry: ModelRegistry
    ) -> None:
        """SPEC.md section 5: identical commit SHAs are stored once."""
        source = sourcing(hub, registry)
        source.mirror(REPO, SHA)
        hub.downloads.clear()

        source.mirror(REPO, SHA)

        assert hub.downloads == []

    def test_remote_code_requires_an_override(self, registry: ModelRegistry) -> None:
        hub = FakeHub(repo(config={**LLAMA_CONFIG, "auto_map": {"AutoModel": "mod.M"}}))
        with pytest.raises(HfError, match="remote code"):
            sourcing(hub, registry).mirror(REPO, SHA)

    def test_a_root_admin_can_override_remote_code(self, registry: ModelRegistry) -> None:
        hub = FakeHub(repo(config={**LLAMA_CONFIG, "auto_map": {"AutoModel": "mod.M"}}))
        manifest = sourcing(hub, registry).mirror(REPO, SHA, allow_remote_code=True)
        assert manifest.revision == SHA

    def test_a_quota_breach_blocks_the_mirror(self, registry: ModelRegistry) -> None:
        hub = FakeHub()
        source = sourcing(hub, registry)
        source.mirror(REPO, SHA, domain="/Customers/Acme")

        other = FakeHub(repo(revision="b" * 40))
        with pytest.raises(HfError, match="quota"):
            sourcing(other, registry).mirror(REPO, "b" * 40, domain="/Customers/Acme", quota_gb=0)


class TestSearch:
    def test_search_passes_the_query_through(self, hub: FakeHub, registry: ModelRegistry) -> None:
        sourcing(hub, registry).search("llama", task="text-generation", limit=10)

        assert hub.searches[0]["query"] == "llama"
        assert hub.searches[0]["task"] == "text-generation"
        assert hub.searches[0]["limit"] == 10

    def test_results_come_back_as_repo_info(self, hub: FakeHub, registry: ModelRegistry) -> None:
        results = sourcing(hub, registry).search("llama")
        assert results[0].repo_id == REPO
