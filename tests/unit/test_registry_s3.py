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
"""The boto3-backed object store (SPEC.md section 4).

Driven through a scripted client so the real paging, key handling and
error translation run without S3.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.registry import ModelRegistry, S3ObjectStore


def _client_error(code: str, status: int) -> Exception:
    """An exception shaped like botocore's ClientError."""
    error = Exception(code)
    error.response = {  # type: ignore[attr-defined]
        "Error": {"Code": code},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return error


class FakeS3Client:
    """Enough of the boto3 S3 client for the store to exercise."""

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, bytes] = {}
        self.page_size = page_size
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        key = kwargs["Key"]
        if key not in self.objects:
            raise _client_error("NoSuchKey", 404)

        class Body:
            def __init__(self, data: bytes) -> None:
                self.data = data

            def read(self) -> bytes:
                return self.data

        return {"Body": Body(self.objects[key])}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_objects_v2", kwargs))
        keys = sorted(k for k in self.objects if k.startswith(kwargs.get("Prefix", "")))
        start = 0
        if token := kwargs.get("ContinuationToken"):
            start = keys.index(token)
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        body: dict[str, Any] = {"Contents": [{"Key": k} for k in page]}
        if truncated:
            body["IsTruncated"] = True
            body["NextContinuationToken"] = keys[start + self.page_size]
        return body


@pytest.fixture
def client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture
def store(client: FakeS3Client) -> S3ObjectStore:
    return S3ObjectStore(bucket="models", client=client)


class TestS3ObjectStore:
    def test_put_targets_the_bucket_and_key(
        self, store: S3ObjectStore, client: FakeS3Client
    ) -> None:
        store.put("a/b.json", b"data")

        name, kwargs = client.calls[0]
        assert name == "put_object"
        assert kwargs["Bucket"] == "models"
        assert kwargs["Key"] == "a/b.json"
        assert kwargs["Body"] == b"data"

    def test_get_returns_the_body(self, store: S3ObjectStore) -> None:
        store.put("a/b.json", b"data")
        assert store.get("a/b.json") == b"data"

    def test_a_missing_key_raises_keyerror(self, store: S3ObjectStore) -> None:
        """The registry catches KeyError, not botocore's own exceptions."""
        with pytest.raises(KeyError):
            store.get("nope")

    def test_listing_is_prefix_scoped(self, store: S3ObjectStore) -> None:
        store.put("a/one", b"1")
        store.put("a/two", b"2")
        store.put("b/three", b"3")

        assert sorted(store.list("a/")) == ["a/one", "a/two"]

    def test_listing_follows_continuation_tokens(self, client: FakeS3Client) -> None:
        """A large mirror spans more than one page of results."""
        client.page_size = 2
        store = S3ObjectStore(bucket="models", client=client)
        for i in range(5):
            store.put(f"a/{i}", b"x")

        assert len(store.list("a/")) == 5

    def test_an_empty_prefix_lists_nothing(self, store: S3ObjectStore) -> None:
        assert store.list("nothing/") == []


class TestRegistryOverS3:
    def test_a_mirror_round_trips_and_verifies(self, client: FakeS3Client) -> None:
        registry = ModelRegistry(S3ObjectStore(bucket="models", client=client), bucket="models")
        sha = "0" * 40
        entries = [
            registry.put_file("org/repo", sha, "config.json", b"{}"),
            registry.put_file("org/repo", sha, "model.safetensors", b"weights"),
        ]
        registry.write_manifest("org/repo", sha, entries, license="mit", task="chat")

        assert registry.has("org/repo", sha)
        assert registry.verify("org/repo", sha).ok

    def test_corruption_in_the_bucket_is_detected(self, client: FakeS3Client) -> None:
        registry = ModelRegistry(S3ObjectStore(bucket="models", client=client), bucket="models")
        sha = "0" * 40
        entries = [registry.put_file("org/repo", sha, "model.safetensors", b"weights")]
        registry.write_manifest("org/repo", sha, entries, license="mit", task="chat")

        client.objects[f"org/repo/{sha}/model.safetensors"] = b"tampered"

        assert not registry.verify("org/repo", sha).ok


class TestErrorDiscrimination:
    """A missing object and an unreachable registry are different things."""

    def test_a_missing_key_is_a_keyerror(self, store: S3ObjectStore) -> None:
        with pytest.raises(KeyError):
            store.get("absent")

    def test_an_outage_is_not_mistaken_for_a_missing_object(self) -> None:
        """Otherwise every model would read as unmirrored during an outage."""

        class BrokenClient:
            def get_object(self, **_: Any) -> dict[str, Any]:
                raise ConnectionError("connection refused")

        from aisvc.registry import RegistryError

        store = S3ObjectStore(bucket="models", client=BrokenClient())
        with pytest.raises(RegistryError, match="could not read"):
            store.get("anything")

    def test_a_404_response_is_a_keyerror(self) -> None:
        class NotFoundClient:
            def get_object(self, **_: Any) -> dict[str, Any]:
                raise _client_error("NoSuchKey", 404)

        store = S3ObjectStore(bucket="models", client=NotFoundClient())
        with pytest.raises(KeyError):
            store.get("anything")

    def test_an_outage_does_not_make_a_model_look_unmirrored(self) -> None:
        class BrokenClient:
            def get_object(self, **_: Any) -> dict[str, Any]:
                raise ConnectionError("connection refused")

        from aisvc.registry import RegistryError

        registry = ModelRegistry(
            S3ObjectStore(bucket="models", client=BrokenClient()), bucket="models"
        )
        with pytest.raises(RegistryError):
            registry.has("org/repo", "0" * 40)
