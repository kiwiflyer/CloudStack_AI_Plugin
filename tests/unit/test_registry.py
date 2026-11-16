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
"""Model artifact registry (SPEC.md sections 4, 5 and 11).

Immutable, content-addressed storage with sha256 manifests. Artifacts are
verified before they are ever loaded.
"""

from __future__ import annotations

import hashlib

import pytest

from aisvc.registry import (
    ArtifactEntry,
    InMemoryObjectStore,
    Manifest,
    ModelRegistry,
    RegistryError,
    VerificationResult,
)

SHA = "0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b"
REPO = "meta-llama/Llama-3.3-70B-Instruct"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def registry(store: InMemoryObjectStore) -> ModelRegistry:
    return ModelRegistry(store, bucket="models")


class TestLayout:
    def test_artifacts_live_under_org_repo_sha(self, registry: ModelRegistry) -> None:
        """SPEC.md section 5: s3://models/<org>/<repo>/<sha>/."""
        assert registry.prefix_for(REPO, SHA) == (f"meta-llama/Llama-3.3-70B-Instruct/{SHA}/")

    def test_the_uri_is_addressable(self, registry: ModelRegistry) -> None:
        assert registry.uri_for(REPO, SHA) == (
            f"s3://models/meta-llama/Llama-3.3-70B-Instruct/{SHA}/"
        )

    def test_a_revision_that_is_not_a_sha_is_rejected(self, registry: ModelRegistry) -> None:
        """Mirrors are immutable; a branch name is not."""
        with pytest.raises(RegistryError, match="commit SHA"):
            registry.prefix_for(REPO, "main")


class TestUpload:
    def test_a_file_is_stored_and_digested(self, registry: ModelRegistry) -> None:
        entry = registry.put_file(REPO, SHA, "config.json", b'{"a": 1}')

        assert isinstance(entry, ArtifactEntry)
        assert entry.sha256 == digest(b'{"a": 1}')
        assert entry.size == 8

    def test_the_object_lands_at_the_right_key(
        self, registry: ModelRegistry, store: InMemoryObjectStore
    ) -> None:
        registry.put_file(REPO, SHA, "config.json", b"{}")
        assert f"{registry.prefix_for(REPO, SHA)}config.json" in store.objects

    def test_pickle_weights_are_refused(self, registry: ModelRegistry) -> None:
        """SPEC.md section 5: reject *.bin and *.pt by default."""
        with pytest.raises(RegistryError, match="pickle"):
            registry.put_file(REPO, SHA, "pytorch_model.bin", b"\x80\x04")

    def test_pt_files_are_refused_too(self, registry: ModelRegistry) -> None:
        with pytest.raises(RegistryError, match="pickle"):
            registry.put_file(REPO, SHA, "weights.pt", b"\x80\x04")

    def test_pickle_can_be_allowed_explicitly(self, store: InMemoryObjectStore) -> None:
        relaxed = ModelRegistry(store, bucket="models", block_pickle=False)
        entry = relaxed.put_file(REPO, SHA, "pytorch_model.bin", b"\x80\x04")
        assert entry.path == "pytorch_model.bin"

    def test_a_path_escaping_the_prefix_is_refused(self, registry: ModelRegistry) -> None:
        with pytest.raises(RegistryError, match="path"):
            registry.put_file(REPO, SHA, "../../etc/passwd", b"x")

    def test_an_absolute_path_is_refused(self, registry: ModelRegistry) -> None:
        with pytest.raises(RegistryError, match="path"):
            registry.put_file(REPO, SHA, "/etc/passwd", b"x")


class TestManifest:
    def test_a_manifest_records_every_file(self, registry: ModelRegistry) -> None:
        entries = [
            registry.put_file(REPO, SHA, "config.json", b"{}"),
            registry.put_file(REPO, SHA, "model.safetensors", b"weights"),
        ]
        manifest = registry.write_manifest(REPO, SHA, entries, license="llama3.3", task="chat")

        assert isinstance(manifest, Manifest)
        assert {e.path for e in manifest.files} == {"config.json", "model.safetensors"}

    def test_the_manifest_carries_licence_and_metadata(self, registry: ModelRegistry) -> None:
        entry = registry.put_file(REPO, SHA, "config.json", b"{}")
        manifest = registry.write_manifest(REPO, SHA, [entry], license="llama3.3", task="chat")

        assert manifest.license == "llama3.3"
        assert manifest.task == "chat"
        assert manifest.repo_id == REPO
        assert manifest.revision == SHA

    def test_total_bytes_is_the_sum_of_the_files(self, registry: ModelRegistry) -> None:
        entries = [
            registry.put_file(REPO, SHA, "a.json", b"1234"),
            registry.put_file(REPO, SHA, "b.json", b"123456"),
        ]
        manifest = registry.write_manifest(REPO, SHA, entries, license="mit", task="chat")
        assert manifest.total_bytes == 10

    def test_a_manifest_round_trips_through_json(self) -> None:
        manifest = Manifest(
            repo_id=REPO,
            revision=SHA,
            license="llama3.3",
            task="chat",
            files=(ArtifactEntry(path="config.json", size=2, sha256="ab" * 32),),
        )
        assert Manifest.from_json(manifest.to_json()) == manifest

    def test_the_manifest_can_be_read_back(self, registry: ModelRegistry) -> None:
        entry = registry.put_file(REPO, SHA, "config.json", b"{}")
        registry.write_manifest(REPO, SHA, [entry], license="mit", task="chat")

        assert registry.read_manifest(REPO, SHA).license == "mit"

    def test_reading_a_missing_manifest_is_a_clear_error(self, registry: ModelRegistry) -> None:
        with pytest.raises(RegistryError, match="no manifest"):
            registry.read_manifest(REPO, SHA)


class TestVerification:
    def test_an_intact_mirror_verifies(self, registry: ModelRegistry) -> None:
        entries = [
            registry.put_file(REPO, SHA, "config.json", b"{}"),
            registry.put_file(REPO, SHA, "model.safetensors", b"weights"),
        ]
        registry.write_manifest(REPO, SHA, entries, license="mit", task="chat")

        result = registry.verify(REPO, SHA)

        assert isinstance(result, VerificationResult)
        assert result.ok
        assert result.problems == []

    def test_a_corrupted_file_fails_verification(
        self, registry: ModelRegistry, store: InMemoryObjectStore
    ) -> None:
        """SPEC.md section 11: artifacts verified by sha256 before load."""
        entries = [registry.put_file(REPO, SHA, "model.safetensors", b"weights")]
        registry.write_manifest(REPO, SHA, entries, license="mit", task="chat")

        store.objects[f"{registry.prefix_for(REPO, SHA)}model.safetensors"] = b"tampered"

        result = registry.verify(REPO, SHA)

        assert not result.ok
        assert any("model.safetensors" in p for p in result.problems)
        assert any("sha256" in p for p in result.problems)

    def test_a_missing_file_fails_verification(
        self, registry: ModelRegistry, store: InMemoryObjectStore
    ) -> None:
        entries = [registry.put_file(REPO, SHA, "model.safetensors", b"weights")]
        registry.write_manifest(REPO, SHA, entries, license="mit", task="chat")

        del store.objects[f"{registry.prefix_for(REPO, SHA)}model.safetensors"]

        result = registry.verify(REPO, SHA)

        assert not result.ok
        assert any("missing" in p for p in result.problems)

    def test_a_wrong_size_fails_even_if_the_digest_were_right(
        self, registry: ModelRegistry, store: InMemoryObjectStore
    ) -> None:
        entries = [registry.put_file(REPO, SHA, "a.json", b"1234")]
        registry.write_manifest(REPO, SHA, entries, license="mit", task="chat")
        store.objects[f"{registry.prefix_for(REPO, SHA)}a.json"] = b"12345"

        assert not registry.verify(REPO, SHA).ok


class TestDeduplication:
    def test_an_already_mirrored_revision_is_detected(self, registry: ModelRegistry) -> None:
        """SPEC.md section 5: identical commit SHAs are stored once."""
        assert not registry.has(REPO, SHA)

        entry = registry.put_file(REPO, SHA, "config.json", b"{}")
        registry.write_manifest(REPO, SHA, [entry], license="mit", task="chat")

        assert registry.has(REPO, SHA)

    def test_a_different_revision_is_separate(self, registry: ModelRegistry) -> None:
        other = "a" * 40
        entry = registry.put_file(REPO, SHA, "config.json", b"{}")
        registry.write_manifest(REPO, SHA, [entry], license="mit", task="chat")

        assert registry.has(REPO, SHA)
        assert not registry.has(REPO, other)

    def test_has_is_false_when_only_files_exist_without_a_manifest(
        self, registry: ModelRegistry
    ) -> None:
        """A half-finished mirror must not look complete."""
        registry.put_file(REPO, SHA, "config.json", b"{}")
        assert not registry.has(REPO, SHA)


class TestUsage:
    def test_usage_sums_the_manifests_of_mirrored_models(self, registry: ModelRegistry) -> None:
        """SPEC.md section 5: storage counts against a domain's quota."""
        first = [registry.put_file(REPO, SHA, "a.json", b"1234")]
        registry.write_manifest(
            REPO, SHA, first, license="mit", task="chat", domain="/Customers/Acme"
        )
        other = "b" * 40
        second = [registry.put_file(REPO, other, "a.json", b"123456")]
        registry.write_manifest(
            REPO, other, second, license="mit", task="chat", domain="/Customers/Acme"
        )

        assert registry.domain_usage_bytes("/Customers/Acme") == 10

    def test_another_domains_models_do_not_count(self, registry: ModelRegistry) -> None:
        entry = registry.put_file(REPO, SHA, "a.json", b"1234")
        registry.write_manifest(
            REPO, SHA, [entry], license="mit", task="chat", domain="/Customers/Other"
        )

        assert registry.domain_usage_bytes("/Customers/Acme") == 0

    def test_a_quota_breach_is_reported(self, registry: ModelRegistry) -> None:
        entry = registry.put_file(REPO, SHA, "a.json", b"x" * 100)
        registry.write_manifest(
            REPO, SHA, [entry], license="mit", task="chat", domain="/Customers/Acme"
        )

        assert registry.quota_exceeded("/Customers/Acme", quota_gb=0)
        assert not registry.quota_exceeded("/Customers/Acme", quota_gb=1)

    def test_no_quota_means_no_breach(self, registry: ModelRegistry) -> None:
        assert not registry.quota_exceeded("/Customers/Acme", quota_gb=None)


class TestInMemoryStore:
    def test_it_round_trips_objects(self, store: InMemoryObjectStore) -> None:
        store.put("a/b.json", b"data")
        assert store.get("a/b.json") == b"data"

    def test_listing_is_prefix_scoped(self, store: InMemoryObjectStore) -> None:
        store.put("a/one", b"1")
        store.put("a/two", b"2")
        store.put("b/three", b"3")
        assert sorted(store.list("a/")) == ["a/one", "a/two"]

    def test_getting_a_missing_key_raises(self, store: InMemoryObjectStore) -> None:
        with pytest.raises(KeyError):
            store.get("nope")
