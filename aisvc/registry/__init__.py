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

Mirrors land at `s3://<bucket>/<org>/<repo>/<commit-sha>/`, which makes
them immutable and content-addressed: the same commit SHA is the same
bytes, so it is stored once and shared.

Every file is digested on the way in and the digests are written to a
manifest. `verify()` re-reads and re-digests before anything is loaded —
SPEC.md section 11 requires artifacts to be verified by sha256 before
load, and pickle weights to be refused by default.

The store is a narrow protocol so the registry can be exercised without
S3; `S3ObjectStore` is the boto3 implementation used against Ceph RGW or
MinIO.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "MANIFEST_NAME",
    "ArtifactEntry",
    "InMemoryObjectStore",
    "Manifest",
    "ManifestNotFound",
    "ModelRegistry",
    "ObjectStore",
    "RegistryError",
    "S3ObjectStore",
    "VerificationResult",
]

MANIFEST_NAME = "aisvc-manifest.json"
GIB = 1 << 30

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Pickle-backed weight formats, refused by default (SPEC.md section 5).
PICKLE_SUFFIXES = (".bin", ".pt", ".pth", ".ckpt", ".pkl")


class RegistryError(Exception):
    """An artifact could not be stored, found or trusted."""


class ManifestNotFound(RegistryError):
    """This revision has not been mirrored.

    Distinct from every other RegistryError on purpose: "not mirrored" is
    an ordinary state the planner acts on, while a registry it cannot
    reach must stop the run rather than look like an empty registry.
    """


class ObjectStore(Protocol):
    """The slice of object storage the registry needs."""

    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str) -> Iterable[str]: ...


@dataclass
class InMemoryObjectStore:
    """An object store that lives in a dict. For tests and `--dry-run`."""

    objects: dict[str, bytes] = field(default_factory=dict)

    def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def list(self, prefix: str) -> builtins.list[str]:
        return [k for k in self.objects if k.startswith(prefix)]

    def keys(self) -> builtins.list[str]:
        return sorted(self.objects)


def _is_not_found(exc: Exception) -> bool:
    """Whether a botocore error means the object simply is not there."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = str(error.get("Code", ""))
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return code in {"NoSuchKey", "404", "NotFound"} or status == 404
    return exc.__class__.__name__ in {"NoSuchKey", "404"}


class S3ObjectStore:
    """boto3 against Ceph RGW, MinIO or S3 (SPEC.md section 4)."""

    def __init__(self, bucket: str, client: Any = None, **client_kwargs: Any) -> None:
        if client is None:  # pragma: no cover - needs real credentials
            import boto3

            # endpoint_url points at Ceph RGW or MinIO; None means AWS S3.
            client = boto3.client("s3", **{k: v for k, v in client_kwargs.items() if v is not None})
        self.bucket = bucket
        self.client = client

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            # "Not there" and "cannot reach the registry" must not look the
            # same: the first means a model is unmirrored, the second would
            # otherwise silently skip every deployment during an outage.
            if _is_not_found(exc):
                raise KeyError(key) from exc
            raise RegistryError(f"could not read {key} from s3://{self.bucket}: {exc}") from exc
        body: bytes = response["Body"].read()
        return body

    def list(self, prefix: str) -> builtins.list[str]:
        keys: builtins.list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return keys
            token = page.get("NextContinuationToken")


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    """One stored file, with the digest it must still have."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """What a mirrored revision contains, and what it is licensed under."""

    repo_id: str
    revision: str
    license: str
    task: str
    files: tuple[ArtifactEntry, ...] = ()
    domain: str | None = None
    mirrored_at: str = ""

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.files)

    def to_json(self) -> str:
        return json.dumps(
            {
                "repo_id": self.repo_id,
                "revision": self.revision,
                "license": self.license,
                "task": self.task,
                "domain": self.domain,
                "mirrored_at": self.mirrored_at,
                "files": [
                    {"path": e.path, "size": e.size, "sha256": e.sha256}
                    for e in sorted(self.files, key=lambda e: e.path)
                ],
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        raw = json.loads(text)
        return cls(
            repo_id=raw["repo_id"],
            revision=raw["revision"],
            license=raw["license"],
            task=raw["task"],
            domain=raw.get("domain"),
            mirrored_at=raw.get("mirrored_at", ""),
            files=tuple(
                ArtifactEntry(path=f["path"], size=f["size"], sha256=f["sha256"])
                for f in raw.get("files", [])
            ),
        )


@dataclass(frozen=True, slots=True)
class VerificationResult:
    repo_id: str
    revision: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ModelRegistry:
    """Stores and verifies mirrored model artifacts."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        bucket: str = "models",
        block_pickle: bool = True,
    ) -> None:
        self.store = store
        self.bucket = bucket
        self.block_pickle = block_pickle

    # -- layout -----------------------------------------------------------

    def prefix_for(self, repo_id: str, revision: str) -> str:
        if not _SHA_RE.match(revision):
            raise RegistryError(
                f"{revision!r} is not a 40-character commit SHA; mirrors must "
                "pin an immutable revision"
            )
        return f"{repo_id}/{revision}/"

    def uri_for(self, repo_id: str, revision: str) -> str:
        return f"s3://{self.bucket}/{self.prefix_for(repo_id, revision)}"

    def _check_path(self, path: str) -> None:
        if path.startswith("/") or ".." in path.split("/"):
            raise RegistryError(f"unsafe artifact path {path!r}")
        if posixpath.normpath(path) != path.strip("/"):
            raise RegistryError(f"unsafe artifact path {path!r}")
        if self.block_pickle and path.lower().endswith(PICKLE_SUFFIXES):
            raise RegistryError(
                f"{path!r} is a pickle-backed weight format; these are refused "
                "by default (SPEC.md section 5). Mirror safetensors instead."
            )

    # -- writing ----------------------------------------------------------

    def put_file(self, repo_id: str, revision: str, path: str, data: bytes) -> ArtifactEntry:
        """Store one artifact and return its manifest entry."""
        prefix = self.prefix_for(repo_id, revision)
        self._check_path(path)
        self.store.put(f"{prefix}{path}", data)
        return ArtifactEntry(path=path, size=len(data), sha256=_sha256(data))

    def write_manifest(
        self,
        repo_id: str,
        revision: str,
        entries: Sequence[ArtifactEntry],
        *,
        license: str,
        task: str,
        domain: str | None = None,
    ) -> Manifest:
        """Seal a mirror: its file list, digests, licence and owner."""
        manifest = Manifest(
            repo_id=repo_id,
            revision=revision,
            license=license,
            task=task,
            domain=domain,
            mirrored_at=datetime.now(UTC).isoformat(),
            files=tuple(sorted(entries, key=lambda e: e.path)),
        )
        prefix = self.prefix_for(repo_id, revision)
        self.store.put(f"{prefix}{MANIFEST_NAME}", manifest.to_json().encode("utf-8"))
        return manifest

    # -- reading ----------------------------------------------------------

    def read_manifest(self, repo_id: str, revision: str) -> Manifest:
        prefix = self.prefix_for(repo_id, revision)
        try:
            raw = self.store.get(f"{prefix}{MANIFEST_NAME}")
        except KeyError as exc:
            raise ManifestNotFound(
                f"no manifest for {repo_id}@{revision}; it was never mirrored, "
                "or the mirror did not finish"
            ) from exc
        return Manifest.from_json(raw.decode("utf-8"))

    def has(self, repo_id: str, revision: str) -> bool:
        """Whether a *complete* mirror exists, manifest included.

        Only a genuine absence answers False; a registry that cannot be
        reached raises, so an outage never reads as "nothing is mirrored".
        """
        try:
            self.read_manifest(repo_id, revision)
        except ManifestNotFound:
            return False
        return True

    def verify(self, repo_id: str, revision: str) -> VerificationResult:
        """Re-digest every file against the manifest."""
        manifest = self.read_manifest(repo_id, revision)
        prefix = self.prefix_for(repo_id, revision)
        problems: list[str] = []

        for entry in manifest.files:
            try:
                data = self.store.get(f"{prefix}{entry.path}")
            except KeyError:
                problems.append(f"{entry.path}: missing from the registry")
                continue
            # Both checks run: a size mismatch already proves corruption, but
            # the digest is what an operator will want in the incident report.
            if len(data) != entry.size:
                problems.append(
                    f"{entry.path}: size is {len(data)} bytes, manifest says {entry.size}"
                )
            actual = _sha256(data)
            if actual != entry.sha256:
                problems.append(f"{entry.path}: sha256 is {actual}, manifest says {entry.sha256}")

        return VerificationResult(repo_id=repo_id, revision=revision, problems=problems)

    # -- quotas -----------------------------------------------------------

    def manifests(self) -> list[Manifest]:
        """Every complete mirror in the registry."""
        found: list[Manifest] = []
        for key in self.store.list(""):
            if not key.endswith(MANIFEST_NAME):
                continue
            try:
                found.append(Manifest.from_json(self.store.get(key).decode("utf-8")))
            except (KeyError, json.JSONDecodeError, TypeError):  # pragma: no cover
                continue
        return found

    def domain_usage_bytes(self, domain: str) -> int:
        """Bytes mirrored on behalf of `domain` (SPEC.md section 5)."""
        return sum(m.total_bytes for m in self.manifests() if m.domain == domain)

    def quota_exceeded(self, domain: str, *, quota_gb: int | None) -> bool:
        if quota_gb is None:
            return False
        return self.domain_usage_bytes(domain) > quota_gb * GIB
