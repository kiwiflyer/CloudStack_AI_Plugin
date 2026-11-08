"""GPU sizing for models, serving profiles and pools (SPEC.md sections 3 and 4).

All memory figures are GiB (2**30 bytes), which is how GPU VRAM is quoted:
an "80 GB" H100 is 80 GiB. Parameter counts are decimal billions.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_UTILIZATION",
    "HEADROOM_FRACTION",
    "MIN_BLOCK_TOKENS",
    "GpuRequirement",
    "KvBudget",
    "ModelArchitecture",
    "bytes_per_param",
    "estimate",
    "kv_budget",
]

GIB = float(1 << 30)

#: Fraction of GPU memory the engine is allowed to use (SPEC.md section 3).
DEFAULT_UTILIZATION = 0.9

#: Safety margin on top of weights and KV cache (SPEC.md section 4).
HEADROOM_FRACTION = 0.10

#: Paged KV cache block size, in tokens. A sequence costs at least one block,
#: so this is the floor on what any concurrency cap can consume.
MIN_BLOCK_TOKENS = 16

_BYTES_PER_PARAM: dict[str, float] = {
    "fp32": 4.0,
    "float32": 4.0,
    "bf16": 2.0,
    "fp16": 2.0,
    "float16": 2.0,
    "none": 2.0,
    "fp8": 1.0,
    "int8": 1.0,
    "w8a8": 1.0,
    "int4": 0.5,
    "awq": 0.5,
    "gptq": 0.5,
}


def bytes_per_param(quantization: str) -> float:
    """Bytes each parameter occupies under `quantization`."""
    try:
        return _BYTES_PER_PARAM[quantization.lower()]
    except KeyError:
        known = ", ".join(sorted(_BYTES_PER_PARAM))
        raise ValueError(f"unknown quantization {quantization!r}; known values: {known}") from None


@dataclass(frozen=True, slots=True)
class ModelArchitecture:
    """The shape facts sizing needs, read from the repo's `config.json`.

    Milestone 3 populates this from mirrored metadata; until then callers
    supply it explicitly.
    """

    params_b: float
    num_layers: int
    num_kv_heads: int
    head_dim: int

    def __post_init__(self) -> None:
        for field in ("params_b", "num_layers", "num_kv_heads", "head_dim"):
            if getattr(self, field) <= 0:
                raise ValueError(f"{field} must be positive")

    def kv_bytes_per_token(self, kv_cache_dtype: str) -> float:
        """Key and value bytes one token of context costs across all layers."""
        return (
            2  # one key tensor and one value tensor
            * self.num_layers
            * self.num_kv_heads
            * self.head_dim
            * bytes_per_param(kv_cache_dtype)
        )

    def weights_gb(self, quantization: str) -> float:
        return self.params_b * 1e9 * bytes_per_param(quantization) / GIB


@dataclass(frozen=True, slots=True)
class GpuRequirement:
    """What one replica needs, against what the GPU class offers."""

    weights_gb: float
    kv_cache_gb: float
    headroom_gb: float
    total_gb: float
    available_gb: float
    fits: bool
    shortfall_gb: float

    def describe(self) -> str:
        if self.fits:
            return (
                f"{self.total_gb:.1f} GiB of {self.available_gb:.1f} GiB "
                f"(weights {self.weights_gb:.1f}, KV {self.kv_cache_gb:.1f})"
            )
        return (
            f"needs {self.total_gb:.1f} GiB but only {self.available_gb:.1f} GiB "
            f"is usable: short by {self.shortfall_gb:.1f} GiB "
            f"(weights {self.weights_gb:.1f}, KV {self.kv_cache_gb:.1f})"
        )


@dataclass(frozen=True, slots=True)
class KvBudget:
    """KV cache left for concurrency once weights and headroom are paid for."""

    kv_cache_gb: float
    kv_bytes_per_token: float

    def max_tokens(self) -> int:
        return int(self.kv_cache_gb * GIB // self.kv_bytes_per_token)

    def max_full_context_seqs(self, max_model_len: int) -> int:
        """How many sequences at the full context length the budget holds."""
        if max_model_len <= 0:
            raise ValueError("max_model_len must be positive")
        return self.max_tokens() // max_model_len

    def context_for_seqs(self, seqs: int) -> int:
        """The average context length at which `seqs` sequences fit at once.

        The inverse of `max_full_context_seqs`: answers "how short do
        requests have to run for this concurrency cap to be reachable?"
        """
        if seqs <= 0:
            raise ValueError("seqs must be positive")
        return self.max_tokens() // seqs


def _available_gb(tensor_parallel: int, gpu_mem_gb: float, utilization: float) -> float:
    if tensor_parallel <= 0:
        raise ValueError("tensor_parallel must be positive")
    if gpu_mem_gb <= 0:
        raise ValueError("gpu_mem_gb must be positive")
    if not 0 < utilization <= 1:
        raise ValueError("utilization must be in (0, 1]")
    return tensor_parallel * gpu_mem_gb * utilization


def estimate(
    arch: ModelArchitecture,
    *,
    quantization: str,
    kv_cache_dtype: str,
    max_model_len: int,
    tensor_parallel: int,
    gpu_mem_gb: float,
    utilization: float = DEFAULT_UTILIZATION,
) -> GpuRequirement:
    """Size one replica: weights + KV cache for one full-length sequence + 10%.

    This is the deploy-feasibility gate of SPEC.md section 3: a model that
    cannot hold a single `max_model_len` sequence cannot serve at all.
    Concurrency is sized separately by `kv_budget`.
    """
    if max_model_len <= 0:
        raise ValueError("max_model_len must be positive")

    weights = arch.weights_gb(quantization)
    kv = arch.kv_bytes_per_token(kv_cache_dtype) * max_model_len / GIB
    headroom = HEADROOM_FRACTION * (weights + kv)
    total = weights + kv + headroom
    available = _available_gb(tensor_parallel, gpu_mem_gb, utilization)
    shortfall = max(0.0, total - available)

    return GpuRequirement(
        weights_gb=weights,
        kv_cache_gb=kv,
        headroom_gb=headroom,
        total_gb=total,
        available_gb=available,
        fits=shortfall == 0.0,
        shortfall_gb=shortfall,
    )


def kv_budget(
    arch: ModelArchitecture,
    *,
    quantization: str,
    kv_cache_dtype: str,
    tensor_parallel: int,
    gpu_mem_gb: float,
    utilization: float = DEFAULT_UTILIZATION,
) -> KvBudget:
    """KV cache available for concurrent sequences after weights and headroom.

    Inverts the headroom rule used by `estimate`: total = 1.1 x (weights + kv)
    must fit, so kv <= available / 1.1 - weights.
    """
    weights = arch.weights_gb(quantization)
    available = _available_gb(tensor_parallel, gpu_mem_gb, utilization)
    kv = max(0.0, available / (1.0 + HEADROOM_FRACTION) - weights)
    return KvBudget(
        kv_cache_gb=kv,
        kv_bytes_per_token=arch.kv_bytes_per_token(kv_cache_dtype),
    )
