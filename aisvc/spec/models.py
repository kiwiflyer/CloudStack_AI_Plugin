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
"""Pydantic models for platform.yaml (SPEC.md section 3).

Every model forbids unknown keys: "unknown keys are errors".
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

API_VERSION = "aisvc/v1"

GpuMode = Literal["passthrough", "vgpu", "mig"]
Task = Literal["chat", "embedding", "rerank", "vision-chat"]
Engine = Literal["vllm", "sglang", "trtllm"]
RolloutStrategy = Literal["bluegreen", "canary", "recreate"]
Tier = Literal["shared", "dedicated"]
#: Who owns a zone's Kubernetes cluster (ADR-0007). "cks" means
#: CloudStack manages it, so it can be asked about and nodes can be
#: handed to it; "external" means CloudStack knows nothing about it.
ClusterSource = Literal["cks", "external"]
Policy = Literal["default", "clamp", "locked"]
AssignmentScope = Literal["domain", "account", "deployment", "key"]
OnExceed = Literal["alert", "block"]

#: hf://<org>/<repo>@<40-hex-commit-sha>
_SOURCE_RE = re.compile(r"^hf://(?P<repo>[^@\s]+/[^@\s]+)@(?P<rev>[0-9a-f]{40})$")
_SOURCE_SHAPE_RE = re.compile(r"^hf://(?P<repo>[^@\s]+/[^@\s]+)(@(?P<rev>\S+))?$")

Name = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KubernetesSource(Base):
    """Where a zone's Kubernetes cluster comes from (ADR-0007).

    The source decides what may be asked of CloudStack about this
    cluster, and therefore which join mechanisms are available to its
    GPU nodes. It is never decided the other way round.
    """

    source: ClusterSource = "cks"
    cluster: str = Field(min_length=1)
    #: Whether the engine may build the cluster if CloudStack has none.
    #: A separate permission from naming one, because it is a separate
    #: thing to grant.
    create: bool = False
    #: How to build it. Every one of these is optional, and leaving
    #: them unset is the intended way: the engine takes the newest
    #: enabled Kubernetes version and the smallest service offering
    #: that clears its minimums, and names both in the plan before
    #: anything is created.
    version: str = ""
    offering: str = ""
    #: Worker nodes for the cluster's own workload — the gateway and
    #: the system pods. GPU nodes are not these; they are provisioned
    #: separately and join afterwards.
    size: int = Field(default=1, gt=0)
    network: str = ""

    @model_validator(mode="after")
    def _creatable(self) -> KubernetesSource:
        """Only CKS clusters can be created, because only they can be.

        Refused at load rather than ignored: `create: true` on an
        external cluster describes nothing the engine could do, and
        silently dropping it would leave an operator believing a
        cluster would appear.
        """
        if self.create and self.source != "cks":
            raise ValueError(
                f"create is only possible when source is cks; the engine cannot "
                f"create a {self.source} cluster, because CloudStack does not "
                f"manage one"
            )

        if not self.create:
            # Settings that only apply while building would otherwise
            # read as though they described, or would be applied to,
            # the cluster that already exists.
            building = [
                name
                for name, value in (
                    ("version", self.version),
                    ("offering", self.offering),
                    ("network", self.network),
                )
                if value
            ]
            if building:
                raise ValueError(
                    f"{', '.join(building)} only applies while creating a cluster; "
                    f"set create: true, or remove it"
                )
        return self


class Zone(Base):
    name: Name
    cloudstack_zone: str
    #: The cluster's name, kept as a field because four call sites read
    #: it and its meaning has not changed. `kubernetes` carries what is
    #: new; the two are normalised to agree.
    cks_cluster: str = ""
    kubernetes: KubernetesSource
    #: Template and offering for per-tenant gateway proxies (SPEC.md
    #: section 8). Optional: a platform with no private endpoints needs
    #: neither, and planning one without them warns rather than failing
    #: at execute time with a CloudStack error nobody can act on.
    proxy_template: str = ""
    proxy_offering: str = ""

    @model_validator(mode="before")
    @classmethod
    def _one_cluster_two_spellings(cls, data: Any) -> Any:
        """Reconcile `cks_cluster` with the `kubernetes` block.

        Every spec written before ADR-0007 says only `cks_cluster`, and
        has to keep meaning exactly what it meant: a CKS cluster the
        engine may not create. So the short spelling is expanded rather
        than deprecated.

        Where both are given they must agree. Preferring one silently
        would leave the other in the file as a lie an operator keeps
        reading.
        """
        if not isinstance(data, dict):
            return data

        named = str(data.get("cks_cluster") or "")
        block = data.get("kubernetes")

        if block is None:
            if not named:
                raise ValueError(
                    "a zone must name its Kubernetes cluster: set cks_cluster, or "
                    "a kubernetes block with a cluster and a source"
                )
            return {**data, "kubernetes": {"source": "cks", "cluster": named}}

        if not isinstance(block, dict):
            return data

        in_block = str(block.get("cluster") or "")
        if named and in_block and named != in_block:
            raise ValueError(
                f"cks_cluster is {named!r} but kubernetes.cluster is {in_block!r}; "
                f"they name one cluster and must agree"
            )
        cluster = in_block or named
        return {**data, "kubernetes": {**block, "cluster": cluster}, "cks_cluster": cluster}


class GpuClass(Base):
    name: Name
    host_tag: str
    service_offering: str
    gpus_per_node: int = Field(gt=0, le=8)
    gpu_mem_gb: float = Field(gt=0)
    mode: GpuMode = "passthrough"
    #: CloudStack's `gpucardname`, to narrow capacity counting on hosts
    #: carrying more than one kind of GPU. Unset counts every GPU on a
    #: host the tag selects.
    gpu_card: str | None = None


class NodePool(Base):
    name: Name
    zone: str
    gpu_class: str
    min_nodes: int = Field(ge=0)
    max_nodes: int = Field(gt=0)
    template: str
    #: Set to a tenant name to reserve this pool for a dedicated tenant.
    tenant: str | None = None


class HuggingFace(Base):
    endpoint: str = "https://huggingface.co"
    token_secret: str
    allow_patterns: list[str] = Field(
        default_factory=lambda: ["*.safetensors", "*.json", "tokenizer*", "*.model"]
    )
    block_pickle: bool = True
    trust_remote_code: bool = False
    require_approval: bool = True
    storage_quota_gb_per_domain: int | None = None


class Architecture(Base):
    """Shape facts from the repo's config.json, filled in at mirror time."""

    params_b: float = Field(gt=0)
    num_layers: int = Field(gt=0)
    num_kv_heads: int = Field(gt=0)
    head_dim: int = Field(gt=0)


class Concurrency(Base):
    """The workload assumption concurrency is checked against.

    `max_num_seqs` is a scheduler cap, not a reservation: a paged engine
    allocates KV cache per token as sequences grow. Declaring the context
    length requests actually run at makes the cap checkable; leaving it
    unset means the spec only reports what is reachable.
    """

    working_context_tokens: int | None = Field(default=None, gt=0)


class Replicas(Base):
    min: int = Field(ge=0)
    max: int = Field(gt=0)


class Slo(Base):
    ttft_p95_ms: int | None = Field(default=None, gt=0)
    queue_depth_target: int | None = Field(default=None, gt=0)


class Rollout(Base):
    strategy: RolloutStrategy = "bluegreen"
    canary_percent: int = Field(default=10, ge=0, le=100)


class Visibility(Base):
    domains: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)


class LoraSettings(Base):
    enabled: bool = False
    max_adapters_per_account: int = Field(default=20, gt=0)


class BatchSettings(Base):
    enabled: bool = False
    discount: float = Field(default=0.5, gt=0, le=1)


class Model(Base):
    name: Name
    source: str
    license: str
    task: Task = "chat"
    engine: Engine = "vllm"
    quantization: str = "bf16"
    tensor_parallel: int = Field(default=1, gt=0)
    max_model_len: int = Field(gt=0)
    node_pool: str
    replicas: Replicas
    slo: Slo = Field(default_factory=Slo)
    rollout: Rollout = Field(default_factory=Rollout)
    serving_profile: str | None = None
    visibility: Visibility = Field(default_factory=Visibility)
    price_tier: str | None = None
    architecture: Architecture | None = None
    #: Engine container image. Defaults per engine in aisvc.k8s when unset,
    #: so an operator can pin one without editing code.
    image: str | None = None
    concurrency: Concurrency = Field(default_factory=Concurrency)
    lora: LoraSettings = Field(default_factory=LoraSettings)
    batch: BatchSettings = Field(default_factory=BatchSettings)

    @property
    def repo_id(self) -> str:
        match = _SOURCE_SHAPE_RE.match(self.source)
        return match.group("repo") if match else ""

    @property
    def revision(self) -> str | None:
        match = _SOURCE_SHAPE_RE.match(self.source)
        return match.group("rev") if match else None

    @property
    def source_is_pinned(self) -> bool:
        return _SOURCE_RE.match(self.source) is not None


class ServingProfile(Base):
    name: Name
    params: dict[str, object] = Field(default_factory=dict)


class GenerationValue(Base):
    """One tunable generation parameter with its policy (SPEC.md section 6)."""

    default: float | int | str | bool | list[str] | None = None
    value: float | int | str | bool | list[str] | None = None
    min: float | None = None
    max: float | None = None
    policy: Policy = "default"


class GenerationProfile(Base):
    name: Name
    params: dict[str, GenerationValue] = Field(default_factory=dict)


class GuardrailCheck(Base):
    action: Literal["block", "redact", "flag"] | None = None
    policy: Policy = "default"
    #: A threshold for a sized check, or the entries of a topic
    #: deny-list (SPEC.md section 8), which is why a list belongs here.
    value: float | int | str | list[str] | None = None


class GuardrailProfile(Base):
    name: Name
    checks: dict[str, GuardrailCheck] = Field(default_factory=dict)


class TuningProfiles(Base):
    serving: list[ServingProfile] = Field(default_factory=list)
    generation: list[GenerationProfile] = Field(default_factory=list)
    guardrail: list[GuardrailProfile] = Field(default_factory=list)


class Assignment(Base):
    scope: AssignmentScope
    target: str
    serving_profile: str | None = None
    generation_profile: str | None = None
    guardrail_profile: str | None = None


class PriceTier(Base):
    name: Name
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


class Quota(Base):
    tpm: int | None = Field(default=None, gt=0)
    rpm: int | None = Field(default=None, gt=0)
    gpu_hours_month: int | None = Field(default=None, gt=0)


class Budget(Base):
    monthly: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    on_exceed: OnExceed = "alert"


class Tenant(Base):
    name: Name
    cloudstack_domain: str
    cloudstack_account: str
    models: list[str] = Field(default_factory=list)
    quota: Quota = Field(default_factory=Quota)
    budget: Budget | None = None
    tier: Tier = "shared"


class Evaluation(Base):
    suites: list[str] = Field(default_factory=list)
    max_regression_points: float = Field(default=2, ge=0)


class PlatformDocument(Base):
    """The YAML document, parsed but not yet cross-validated."""

    api_version: str = Field(alias="apiVersion")
    zones: list[Zone] = Field(default_factory=list)
    gpu_classes: list[GpuClass] = Field(default_factory=list, alias="gpuClasses")
    node_pools: list[NodePool] = Field(default_factory=list, alias="nodePools")
    huggingface: HuggingFace | None = None
    models: list[Model] = Field(default_factory=list)
    tuning_profiles: TuningProfiles = Field(default_factory=TuningProfiles, alias="tuningProfiles")
    assignments: list[Assignment] = Field(default_factory=list)
    price_tiers: list[PriceTier] = Field(default_factory=list, alias="priceTiers")
    tenants: list[Tenant] = Field(default_factory=list)
    evaluation: Evaluation | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
