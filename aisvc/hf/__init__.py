"""Hugging Face sourcing: search, check, mirror, scan (SPEC.md section 5).

Only the mirror worker reaches the internet (SPEC.md section 1), so this
module runs in the isolated `aisvc-mirror` namespace and nowhere else.

The flow the spec lays out is search, check, request, approve, mirror,
scan, publish. This module owns check and mirror, plus the scanning rules
that gate them:

* the revision is resolved to a commit SHA before anything downloads, so
  a mirror is immutable;
* pickle-only repos and repos needing remote code are refused by default;
* the licence must be on the allow-list;
* every file is digested, uploaded and then re-verified.

`Hub` is a narrow protocol over `huggingface_hub` so the rules can be
tested without network access.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from aisvc.capacity import ModelArchitecture, estimate
from aisvc.registry import PICKLE_SUFFIXES, Manifest, ModelRegistry, RegistryError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aisvc.spec.models import GpuClass, HuggingFace

__all__ = [
    "SUPPORTED_ARCHITECTURES",
    "CompatibilityReport",
    "HfError",
    "Hub",
    "HuggingFaceHub",
    "ModelSourcing",
    "Placement",
    "RepoFile",
    "RepoInfo",
    "architecture_from_config",
]

#: Architectures the vLLM/SGLang/TRT-LLM runtimes we ship can serve.
#: Checked against `config.json`'s `architectures` (SPEC.md section 5).
SUPPORTED_ARCHITECTURES: frozenset[str] = frozenset(
    {
        "CohereForCausalLM",
        "DbrxForCausalLM",
        "DeepseekV2ForCausalLM",
        "DeepseekV3ForCausalLM",
        "FalconForCausalLM",
        "Gemma2ForCausalLM",
        "Gemma3ForCausalLM",
        "GemmaForCausalLM",
        "GPTBigCodeForCausalLM",
        "GPTNeoXForCausalLM",
        "LlamaForCausalLM",
        "LlavaForConditionalGeneration",
        "MistralForCausalLM",
        "MixtralForCausalLM",
        "MptForCausalLM",
        "PhiForCausalLM",
        "Phi3ForCausalLM",
        "Qwen2ForCausalLM",
        "Qwen2VLForConditionalGeneration",
        "Qwen3ForCausalLM",
        "StableLmForCausalLM",
        "Starcoder2ForCausalLM",
        "XLMRobertaModel",  # bge-m3 and friends
        "BertModel",
    }
)

SAFETENSORS_SUFFIX = ".safetensors"


class HfError(Exception):
    """A repo could not be checked or mirrored."""


@dataclass(frozen=True, slots=True)
class RepoFile:
    path: str
    size: int


@dataclass(frozen=True, slots=True)
class RepoInfo:
    """What the Hub reports about one repo at one revision."""

    repo_id: str
    revision: str
    license: str
    gated: bool = False
    private: bool = False
    pipeline_tag: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    files: tuple[RepoFile, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def has_safetensors(self) -> bool:
        return any(f.path.endswith(SAFETENSORS_SUFFIX) for f in self.files)

    @property
    def has_pickle_weights(self) -> bool:
        return any(f.path.lower().endswith(PICKLE_SUFFIXES) for f in self.files)

    @property
    def requires_remote_code(self) -> bool:
        """A repo shipping its own modelling code (SPEC.md section 5)."""
        if self.config.get("auto_map"):
            return True
        return any(f.path.endswith(".py") for f in self.files)


class Hub(Protocol):
    """The slice of `huggingface_hub` this module needs."""

    def search(self, **kwargs: Any) -> list[RepoInfo]: ...
    def info(self, repo_id: str, revision: str) -> RepoInfo: ...
    def resolve_revision(self, repo_id: str, revision: str) -> str: ...
    def download(self, repo_id: str, revision: str, path: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class Placement:
    """Whether a GPU class can serve this model, and at what width."""

    gpu_class: str
    tensor_parallel: int
    fits: bool
    shortfall_gb: float = 0.0


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """The answer to "can we serve this, and should we?" (SPEC.md section 5)."""

    repo_id: str
    revision: str
    license: str
    architecture: str
    task: str
    download_bytes: int
    context_length: int
    gated: bool
    requires_remote_code: bool
    has_safetensors: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return not self.problems


def architecture_from_config(config: dict[str, Any], *, params_b: float) -> ModelArchitecture:
    """Read the shape facts `capacity` needs out of `config.json`."""
    layers = config.get("num_hidden_layers") or config.get("n_layer")
    heads = config.get("num_attention_heads") or config.get("n_head")
    hidden = config.get("hidden_size") or config.get("n_embd")

    if not layers or not heads:
        raise HfError(
            "config.json has no num_hidden_layers/num_attention_heads, so the model cannot be sized"
        )

    head_dim = config.get("head_dim") or (hidden // heads if hidden else None)
    if not head_dim:
        raise HfError("config.json has no head_dim and no hidden_size to derive it from")

    kv_heads = config.get("num_key_value_heads") or heads

    return ModelArchitecture(
        params_b=params_b,
        num_layers=int(layers),
        num_kv_heads=int(kv_heads),
        head_dim=int(head_dim),
    )


class ModelSourcing:
    """Checks and mirrors Hugging Face repos into the registry."""

    def __init__(
        self,
        hub: Hub,
        registry: ModelRegistry,
        *,
        allowed_licenses: Sequence[str],
        settings: HuggingFace | None = None,
    ) -> None:
        self.hub = hub
        self.registry = registry
        self.allowed_licenses = {lic.lower() for lic in allowed_licenses}
        self.settings = settings

    @property
    def allow_patterns(self) -> tuple[str, ...]:
        if self.settings and self.settings.allow_patterns:
            return tuple(self.settings.allow_patterns)
        return ("*.safetensors", "*.json", "tokenizer*", "*.model")

    @property
    def block_pickle(self) -> bool:
        return self.settings.block_pickle if self.settings else True

    # -- search -----------------------------------------------------------

    def search(self, query: str, **kwargs: Any) -> list[RepoInfo]:
        """Search the Hub (SPEC.md section 5, step 1)."""
        return self.hub.search(query=query, **kwargs)

    # -- check ------------------------------------------------------------

    def check(
        self,
        repo_id: str,
        revision: str,
        *,
        gpu_classes: Sequence[GpuClass] = (),
        quantization: str = "bf16",
    ) -> CompatibilityReport:
        """Produce the compatibility report (SPEC.md section 5, step 2).

        Reads metadata only; nothing is downloaded, so a gated repo is
        reported as gated rather than failing mid-download.
        """
        info = self.hub.info(repo_id, revision)
        problems: list[str] = []
        warnings: list[str] = []

        architectures = info.config.get("architectures") or []
        architecture = str(architectures[0]) if architectures else ""
        if not architecture:
            problems.append("config.json declares no architecture")
        elif architecture not in SUPPORTED_ARCHITECTURES:
            problems.append(f"architecture {architecture!r} is not supported by any engine we ship")

        if info.license.lower() not in self.allowed_licenses:
            problems.append(f"licence {info.license!r} is not on the allow-list")

        if self.block_pickle and not info.has_safetensors and info.has_pickle_weights:
            problems.append(
                "repo ships pickle weights only (.bin/.pt); safetensors are "
                "required unless a root admin overrides"
            )

        if info.requires_remote_code:
            problems.append(
                "repo requires remote code (trust_remote_code); a root admin "
                "must override to mirror it"
            )

        if info.gated:
            warnings.append(
                "repo is gated: the org token's account must have accepted the "
                "licence on Hugging Face before the mirror will succeed"
            )
        if info.private:
            warnings.append("repo is private to the token's account")

        context_length = int(
            info.config.get("max_position_embeddings") or info.config.get("n_positions") or 0
        )

        return CompatibilityReport(
            repo_id=repo_id,
            revision=revision,
            license=info.license,
            architecture=architecture,
            task=info.pipeline_tag,
            download_bytes=info.total_bytes,
            context_length=context_length,
            gated=info.gated,
            requires_remote_code=info.requires_remote_code,
            has_safetensors=info.has_safetensors,
            problems=problems,
            warnings=warnings,
            placements=self._placements(info, gpu_classes, quantization, context_length),
        )

    def _placements(
        self,
        info: RepoInfo,
        gpu_classes: Sequence[GpuClass],
        quantization: str,
        context_length: int,
    ) -> list[Placement]:
        """Which classes and TP widths can serve this (SPEC.md section 5)."""
        if not gpu_classes:
            return []

        params_b = info.total_bytes / 2e9  # bf16 bytes on the Hub -> parameters
        try:
            arch = architecture_from_config(info.config, params_b=params_b)
        except HfError:
            return []

        context = min(context_length or 8192, 32768)
        placements: list[Placement] = []

        for gpu_class in gpu_classes:
            width = 1
            while width <= gpu_class.gpus_per_node:
                requirement = estimate(
                    arch,
                    quantization=quantization,
                    kv_cache_dtype="fp16",
                    max_model_len=context,
                    tensor_parallel=width,
                    gpu_mem_gb=gpu_class.gpu_mem_gb,
                )
                if requirement.fits:
                    placements.append(
                        Placement(gpu_class=gpu_class.name, tensor_parallel=width, fits=True)
                    )
                    break
                width *= 2
            else:
                final = estimate(
                    arch,
                    quantization=quantization,
                    kv_cache_dtype="fp16",
                    max_model_len=context,
                    tensor_parallel=gpu_class.gpus_per_node,
                    gpu_mem_gb=gpu_class.gpu_mem_gb,
                )
                placements.append(
                    Placement(
                        gpu_class=gpu_class.name,
                        tensor_parallel=gpu_class.gpus_per_node,
                        fits=False,
                        shortfall_gb=final.shortfall_gb,
                    )
                )

        return placements

    # -- mirror -----------------------------------------------------------

    def _wanted(self, info: RepoInfo) -> list[RepoFile]:
        return [
            f
            for f in info.files
            if any(fnmatch.fnmatch(f.path, pattern) for pattern in self.allow_patterns)
        ]

    def mirror(
        self,
        repo_id: str,
        revision: str,
        *,
        domain: str | None = None,
        quota_gb: int | None = None,
        allow_remote_code: bool = False,
    ) -> Manifest:
        """Mirror a pinned revision into the registry (SPEC.md section 5).

        Resolves the revision to a commit SHA first, so what is stored can
        never change underneath us.
        """
        sha = self.hub.resolve_revision(repo_id, revision)

        if self.registry.has(repo_id, sha):
            # Identical SHAs are stored once and shared.
            return self.registry.read_manifest(repo_id, sha)

        report = self.check(repo_id, sha)
        blocking = [
            problem
            for problem in report.problems
            if not (allow_remote_code and "remote code" in problem)
        ]
        if blocking:
            raise HfError(f"{repo_id}@{sha} cannot be mirrored: " + "; ".join(blocking))

        if (
            quota_gb is not None
            and domain
            and self.registry.quota_exceeded(domain, quota_gb=quota_gb)
        ):
            raise HfError(f"domain {domain} is over its model storage quota of {quota_gb} GB")

        info = self.hub.info(repo_id, sha)
        entries = []
        for wanted in self._wanted(info):
            data = self.hub.download(repo_id, sha, wanted.path)
            try:
                entries.append(self.registry.put_file(repo_id, sha, wanted.path, data))
            except RegistryError as exc:
                raise HfError(str(exc)) from exc

        manifest = self.registry.write_manifest(
            repo_id,
            sha,
            entries,
            license=info.license,
            task=info.pipeline_tag or "chat",
            domain=domain,
        )

        result = self.registry.verify(repo_id, sha)
        if not result.ok:
            raise HfError(
                f"{repo_id}@{sha} failed verification after mirroring: "
                + "; ".join(result.problems)
            )

        return manifest


def _card_license(model_info: Any) -> str:
    """The licence from a Hub model card, which may be absent or untyped."""
    card = getattr(model_info, "card_data", None)
    if card is None:
        return ""
    getter = getattr(card, "get", None)
    if getter is None:
        return ""
    return str(getter("license", "") or "")


class HuggingFaceHub:
    """`huggingface_hub` behind the `Hub` protocol.

    Only instantiated inside the mirror Job, which is the one workload
    with egress (SPEC.md section 5).
    """

    def __init__(self, token: str | None = None, endpoint: str | None = None) -> None:
        from huggingface_hub import HfApi

        self.api = HfApi(token=token, endpoint=endpoint)

    def search(self, **kwargs: Any) -> list[RepoInfo]:  # pragma: no cover - network
        models = self.api.list_models(
            search=kwargs.get("query"),
            pipeline_tag=kwargs.get("task"),
            limit=kwargs.get("limit", 25),
            sort=kwargs.get("sort", "downloads"),
        )
        return [
            RepoInfo(
                repo_id=m.id,
                revision=getattr(m, "sha", "") or "",
                license=_card_license(m),
                gated=bool(getattr(m, "gated", False)),
                private=bool(getattr(m, "private", False)),
                pipeline_tag=getattr(m, "pipeline_tag", "") or "",
            )
            for m in models
        ]

    def info(self, repo_id: str, revision: str) -> RepoInfo:  # pragma: no cover - network
        info = self.api.model_info(repo_id, revision=revision, files_metadata=True)
        config = getattr(info, "config", None) or {}
        return RepoInfo(
            repo_id=repo_id,
            revision=getattr(info, "sha", revision) or revision,
            license=_card_license(info),
            gated=bool(getattr(info, "gated", False)),
            private=bool(getattr(info, "private", False)),
            pipeline_tag=getattr(info, "pipeline_tag", "") or "",
            config=dict(config),
            files=tuple(
                RepoFile(path=s.rfilename, size=s.size or 0) for s in (info.siblings or [])
            ),
        )

    def resolve_revision(self, repo_id: str, revision: str) -> str:  # pragma: no cover
        info = self.api.model_info(repo_id, revision=revision)
        sha: str = getattr(info, "sha", "") or ""
        if not sha:
            raise HfError(f"could not resolve {repo_id}@{revision} to a commit SHA")
        return sha

    def download(self, repo_id: str, revision: str, path: str) -> bytes:  # pragma: no cover
        from huggingface_hub import hf_hub_download

        local = hf_hub_download(repo_id=repo_id, revision=revision, filename=path)
        with open(local, "rb") as handle:
            return handle.read()
