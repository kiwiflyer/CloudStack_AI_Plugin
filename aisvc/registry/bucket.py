"""The registry's bucket, from CloudStack's object store (ADR-0005).

CloudStack's object store framework provisions a bucket on a backing
MinIO or Ceph and hands back S3 credentials; it does not proxy object
data. `createBucket` returns `accesskey`, `usersecretkey` and `url`, and
CloudStack's own object browser reads objects with exactly those.

So this does not replace `S3ObjectStore`, and boto3 still talks S3 to
the same kind of endpoint. What it replaces is where three values come
from: a bucket name, an endpoint and a credential that an operator
would otherwise set by hand and keep in step.

**It resolves; it does not create.** `createBucket` needs a quota in
GiB — mandatory since 4.21 — and nothing here knows what a registry's
quota should be. A model is tens to hundreds of GiB, and a conservative
guess fails a multi-hour mirror at ninety percent, long after the
decision that caused it. An operator creates the bucket in CloudStack,
where they can see the pool and choose the number; this finds it.

**A bucket that is not there is an error, never a fallback.** Quietly
using a different bucket than the operator named would mirror models
somewhere nobody is looking, and nothing downstream would notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "BucketCredentials",
    "BucketNotFound",
    "SourceUnclear",
    "from_cloudstack_requested",
    "resolve_bucket",
]

#: Accepted spellings for the switch. An unrecognised value is an
#: error: treating "maybe" as off would point the engine at a different
#: bucket than the operator believed they had chosen.
_AFFIRMATIVE = frozenset({"1", "true", "yes", "on"})
_NEGATIVE = frozenset({"", "0", "false", "no", "off"})

SOURCE_VAR = "AISVC_REGISTRY_FROM_CLOUDSTACK"

#: CloudStack's list item key for a bucket.
ITEM_KEY = "bucket"


class BucketNotFound(Exception):
    """The named bucket is not usable, with a reason an operator can act on."""


class SourceUnclear(Exception):
    """The switch was set to something that is neither on nor off."""


def from_cloudstack_requested(environ: Mapping[str, str]) -> bool:
    """Whether to resolve the bucket through CloudStack's object store."""
    raw = environ.get(SOURCE_VAR, "").strip().lower()
    if raw in _AFFIRMATIVE:
        return True
    if raw in _NEGATIVE:
        return False
    raise SourceUnclear(
        f"{SOURCE_VAR}={environ.get(SOURCE_VAR)!r} is neither on nor off. "
        f"Use one of {', '.join(sorted(_AFFIRMATIVE))} to resolve the bucket "
        f"through CloudStack, or leave it unset to configure the endpoint directly."
    )


@dataclass(frozen=True)
class BucketCredentials:
    """What is needed to talk S3 to a CloudStack-provisioned bucket.

    Both keys are secret, and this ends up in tracebacks and plan
    output, so neither appears in the repr.
    """

    name: str
    endpoint: str
    access_key: str
    secret_key: str

    def __repr__(self) -> str:
        return f"BucketCredentials(name={self.name!r}, endpoint={self.endpoint!r})"


async def resolve_bucket(client: Any, name: str) -> BucketCredentials:
    """Find `name` in CloudStack's object store and read its credentials.

    Raises `BucketNotFound` when it is absent or unusable — never falls
    back to another bucket or to the environment.
    """
    listed = await client.call_paged("listBuckets", ITEM_KEY)

    for bucket in listed:
        if str(bucket.get("name", "")) != name:
            continue
        return _credentials_from(bucket, name)

    raise BucketNotFound(
        f"no object store bucket named {name!r} in CloudStack. Create one with "
        f"createBucket, choosing an objectstorageid and a quota in GiB large "
        f"enough for the models you intend to mirror, or unset "
        f"AISVC_REGISTRY_FROM_CLOUDSTACK to configure the endpoint directly."
    )


def _credentials_from(bucket: Mapping[str, Any], name: str) -> BucketCredentials:
    """Read a listed bucket, or say precisely what it is missing.

    A bucket CloudStack lists without credentials would otherwise
    surface as an opaque S3 403 on the first mirror, hours later.
    """
    endpoint = str(bucket.get("url", ""))
    access_key = str(bucket.get("accesskey", ""))
    secret_key = str(bucket.get("usersecretkey", ""))

    missing = [
        label
        for label, value in (
            ("url", endpoint),
            ("access key", access_key),
            ("secret key", secret_key),
        )
        if not value
    ]
    if missing:
        raise BucketNotFound(
            f"the bucket {name!r} exists but CloudStack returned no "
            f"{', '.join(missing)} for it, so the engine has no usable "
            f"credential. Check the object store pool it belongs to."
        )

    return BucketCredentials(
        name=name,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
    )
