"""Where the registry's bucket and credentials come from (ADR-0005).

CloudStack's object store provisions a bucket on a backing MinIO or Ceph
and hands back S3 credentials; it does not proxy object data. So
`S3ObjectStore` is untouched by this — what changes is where three
values come from.

**It resolves, it does not create.** `createBucket` needs a quota in
GiB, mandatory since 4.21, and nothing here knows what a registry's
quota should be: a model is tens to hundreds of GiB and a conservative
guess fails a multi-hour mirror at ninety percent. An operator creates
the bucket in CloudStack, where they can see the pool and set the
quota; the engine finds it and reads the credentials off it.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.registry.bucket import (
    BucketCredentials,
    BucketNotFound,
    resolve_bucket,
)

BUCKET = {
    "id": "b-1",
    "name": "models",
    "url": "https://objects.example/models",
    "accesskey": "AKIAEXAMPLE",
    "usersecretkey": "s3cr3t",
    "objectstore": "minio-zone-1",
    "quota": 2048,
    "size": 12,
}


class FakeClient:
    """Stands in for the signed CloudStack client."""

    def __init__(self, *buckets: dict[str, Any]) -> None:
        self.buckets = list(buckets)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call_paged(self, command: str, item_key: str, **params: Any) -> list[Any]:
        self.calls.append((command, item_key, params))
        return list(self.buckets)


class TestResolving:
    async def test_it_finds_the_bucket_and_reads_its_credentials(self) -> None:
        found = await resolve_bucket(FakeClient(BUCKET), "models")

        assert found.name == "models"
        assert found.endpoint == "https://objects.example/models"
        assert found.access_key == "AKIAEXAMPLE"
        assert found.secret_key == "s3cr3t"

    async def test_it_asks_cloudstack_for_buckets(self) -> None:
        client = FakeClient(BUCKET)

        await resolve_bucket(client, "models")

        command, item_key, _ = client.calls[0]
        assert command == "listBuckets"
        assert item_key == "bucket"

    async def test_it_matches_the_name_exactly(self) -> None:
        # "models" must not resolve to "models-staging": the engine
        # would mirror into the wrong bucket and nothing would say so.
        other = {**BUCKET, "name": "models-staging"}

        with pytest.raises(BucketNotFound):
            await resolve_bucket(FakeClient(other), "models")

    async def test_the_right_one_is_picked_out_of_several(self) -> None:
        found = await resolve_bucket(
            FakeClient({**BUCKET, "name": "other"}, BUCKET, {**BUCKET, "name": "third"}),
            "models",
        )

        assert found.name == "models"


class TestWhenItIsNotThere:
    async def test_a_missing_bucket_raises(self) -> None:
        with pytest.raises(BucketNotFound):
            await resolve_bucket(FakeClient(), "models")

    async def test_the_error_names_the_bucket(self) -> None:
        with pytest.raises(BucketNotFound) as caught:
            await resolve_bucket(FakeClient(), "models")

        assert "models" in str(caught.value)

    async def test_the_error_says_how_to_make_one(self) -> None:
        # An operator who has not created it needs the next step, and
        # the quota is theirs to choose.
        with pytest.raises(BucketNotFound) as caught:
            await resolve_bucket(FakeClient(), "models")

        assert "createBucket" in str(caught.value)
        assert "quota" in str(caught.value).lower()

    async def test_it_does_not_fall_back_to_the_environment(self) -> None:
        # Silently serving a different bucket than the operator asked
        # for is worse than refusing.
        with pytest.raises(BucketNotFound):
            await resolve_bucket(FakeClient({**BUCKET, "name": "elsewhere"}), "models")


class TestABucketWithoutKeys:
    """CloudStack can list a bucket whose keys it will not hand over."""

    async def test_a_bucket_with_no_access_key_is_refused(self) -> None:
        with pytest.raises(BucketNotFound) as caught:
            await resolve_bucket(FakeClient({**BUCKET, "accesskey": ""}), "models")

        assert "credential" in str(caught.value).lower()

    async def test_a_bucket_with_no_secret_is_refused(self) -> None:
        # Returning blank credentials would surface as an opaque S3
        # 403 hours later, on the first mirror.
        with pytest.raises(BucketNotFound):
            await resolve_bucket(FakeClient({**BUCKET, "usersecretkey": ""}), "models")

    async def test_a_bucket_with_no_url_is_refused(self) -> None:
        with pytest.raises(BucketNotFound):
            await resolve_bucket(FakeClient({**BUCKET, "url": ""}), "models")


class TestSecretsStayOut:
    def test_the_secret_is_not_in_the_repr(self) -> None:
        # These reach tracebacks and plan output.
        shown = repr(
            BucketCredentials(
                name="models",
                endpoint="https://objects.example/models",
                access_key="AKIAEXAMPLE",
                secret_key="s3cr3t",
            )
        )

        assert "s3cr3t" not in shown
        assert "models" in shown

    def test_the_access_key_is_not_in_the_repr_either(self) -> None:
        shown = repr(
            BucketCredentials(
                name="models",
                endpoint="https://e",
                access_key="AKIAEXAMPLE",
                secret_key="s",
            )
        )

        assert "AKIAEXAMPLE" not in shown


class TestChoosingTheSource:
    """One switch, explicit, with no silent fallback either way."""

    def test_off_by_default(self) -> None:
        from aisvc.registry.bucket import from_cloudstack_requested

        assert not from_cloudstack_requested({})

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_the_usual_affirmatives_turn_it_on(self, value: str) -> None:
        from aisvc.registry.bucket import from_cloudstack_requested

        assert from_cloudstack_requested({"AISVC_REGISTRY_FROM_CLOUDSTACK": value})

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_the_usual_negatives_leave_it_off(self, value: str) -> None:
        from aisvc.registry.bucket import from_cloudstack_requested

        assert not from_cloudstack_requested({"AISVC_REGISTRY_FROM_CLOUDSTACK": value})

    def test_an_unrecognised_value_is_refused_rather_than_guessed(self) -> None:
        # "maybe" silently meaning off would point the engine at a
        # different bucket than the operator believed they chose.
        from aisvc.registry.bucket import SourceUnclear, from_cloudstack_requested

        with pytest.raises(SourceUnclear):
            from_cloudstack_requested({"AISVC_REGISTRY_FROM_CLOUDSTACK": "maybe"})
