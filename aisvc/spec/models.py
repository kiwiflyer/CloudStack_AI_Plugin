"""Pydantic models for platform.yaml (SPEC.md section 3).

Every model forbids unknown keys: "unknown keys are errors".
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "aisvc/v1"

GpuMode = Literal["passthrough", "vgpu", "mig"]
Task = Literal["chat", "embedding", "rerank", "vision-chat"]
Engine = Literal["vllm", "sglang", "trtllm"]
RolloutStrategy = Literal["bluegreen", "canary", "recreate"]
Tier = Literal["shared", "dedicated"]
Policy = Literal["default", "clamp", "locked"]
AssignmentScope = Literal["domain", "account", "deployment", "key"]
OnExceed = Literal["alert", "block"]

#: hf://<org>/<repo>@<40-hex-commit-sha>
_SOURCE_RE = re.compile(r"^hf://(?P<repo>[^@\s]+/[^@\s]+)@(?P<rev>[0-9a-f]{40})$")
_SOURCE_SHAPE_RE = re.compile(r"^hf://(?P<repo>[^@\s]+/[^@\s]+)(@(?P<rev>\S+))?$")

Name = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Zone(Base):
    name: Name
    cloudstack_zone: str
    cks_cluster: str


class GpuClass(Base):
    name: Name
    host_tag: str
    service_offering: str
    gpus_per_node: int = Field(gt=0, le=8)
    gpu_mem_gb: float = Field(gt=0)
    mode: GpuMode = "passthrough"


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
    value: float | int | str | None = None


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
