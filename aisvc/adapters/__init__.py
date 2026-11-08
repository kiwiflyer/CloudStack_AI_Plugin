"""Multi-LoRA serving (SPEC.md section 8).

Tenants bring adapters; we never train them (CLAUDE.md's non-goals).
Everything here is therefore about accepting someone else's artefact
safely, and the checks mostly exist because the alternative is a
failure the tenant cannot diagnose:

- A rank over the serving profile's `max_lora_rank` does not fail at
  upload, it fails when vLLM tries to load the adapter on the first
  request — which the tenant experiences as the endpoint being broken.
- A `.bin` adapter is a pickle, and a pickle executes on load. CLAUDE.md
  blocks them for base models; a LoRA is no less capable.
- An adapter with no `adapter_config.json` has no declared rank or
  target modules, so PEFT cannot apply it at all.

Serving them is the engine's job and is rendered by `k8s`; this module
decides what may be served, under what name, and to whom.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ADAPTER_SEPARATOR",
    "MAX_ADAPTER_BYTES",
    "AdapterRejected",
    "AdapterRequest",
    "gateway_model_name",
    "may_use",
    "price_with_premium",
    "validate_adapter",
]

#: Separates a base from its adapter in a gateway model name.
ADAPTER_SEPARATOR = ":"

#: A LoRA adapter is tens of megabytes. Anything approaching a gigabyte
#: is a full checkpoint someone has mislabelled, and accepting it would
#: fill the registry and the node-local cache for nothing.
MAX_ADAPTER_BYTES = 2 * 1024 * 1024 * 1024

#: Weight files PEFT will load. safetensors only: everything else in
#: this list would be a pickle.
SAFE_WEIGHT_SUFFIXES = (".safetensors",)
UNSAFE_WEIGHT_SUFFIXES = (".bin", ".pt", ".pth", ".pkl", ".ckpt")

#: PEFT needs this to know the rank and the target modules.
REQUIRED_CONFIG = "adapter_config.json"


class AdapterRejected(Exception):
    """An adapter may not be accepted, with a reason the tenant can act on."""

    status_code = 400

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    """A LoRA adapter a tenant wants served."""

    name: str
    base_model: str
    account: str
    domain: str
    rank: int
    size_bytes: int
    files: tuple[str, ...] = ()
    #: A domain admin may share one across their domain and below.
    shared_with_domain: bool = False


def validate_adapter(
    request: AdapterRequest,
    *,
    bases: Sequence[str],
    max_lora_rank: int,
    max_adapters_per_account: int,
    existing: int,
) -> bool:
    """Check an adapter against the platform, or refuse it with a reason.

    Checked here rather than at load time because every one of these
    failures is silent and late otherwise: the tenant uploads
    something, sees it accepted, and finds out at the first request.
    """
    if ADAPTER_SEPARATOR in request.name:
        # A name containing the separator could address another
        # adapter through the gateway's `<base>:<adapter>` form.
        raise AdapterRejected(
            "invalid_name",
            f"an adapter name may not contain {ADAPTER_SEPARATOR!r}",
        )

    if request.base_model not in bases:
        served = ", ".join(sorted(bases)) or "none"
        raise AdapterRejected(
            "base_not_served",
            f"{request.base_model!r} is not deployed here; adapters can only be "
            f"served on a deployed base. Available: {served}",
        )

    if request.rank < 1:
        raise AdapterRejected("rank_too_high", "an adapter's rank must be at least 1")

    if request.rank > max_lora_rank:
        raise AdapterRejected(
            "rank_too_high",
            f"rank {request.rank} is above this base's max_lora_rank of "
            f"{max_lora_rank}; vLLM would refuse to load it at request time",
        )

    weights = [
        name
        for name in request.files
        if name.endswith(SAFE_WEIGHT_SUFFIXES + UNSAFE_WEIGHT_SUFFIXES)
    ]
    if any(name.endswith(UNSAFE_WEIGHT_SUFFIXES) for name in weights):
        raise AdapterRejected(
            "unsafe_weights",
            "adapter weights must be safetensors; pickle formats execute code "
            "when they are loaded and are blocked",
        )
    if not weights:
        raise AdapterRejected(
            "no_weights", "no adapter weights found; expected a .safetensors file"
        )

    if REQUIRED_CONFIG not in request.files:
        raise AdapterRejected(
            "no_config",
            f"{REQUIRED_CONFIG} is missing; without it the rank and target "
            "modules are unknown and the adapter cannot be applied",
        )

    if request.size_bytes > MAX_ADAPTER_BYTES:
        raise AdapterRejected(
            "adapter_too_large",
            f"{request.size_bytes} bytes is larger than the {MAX_ADAPTER_BYTES} "
            "byte limit; this looks like a full checkpoint rather than a LoRA",
        )

    if existing >= max_adapters_per_account:
        raise AdapterRejected(
            "too_many_adapters",
            f"this account already has {existing} adapters, which is its limit "
            f"of {max_adapters_per_account}",
        )

    return True


def gateway_model_name(base: str, adapter: str | None) -> str:
    """What the tenant addresses at the gateway (SPEC.md section 8)."""
    if not adapter:
        return base
    return f"{base}{ADAPTER_SEPARATOR}{adapter}"


def may_use(adapter: AdapterRequest, *, account: str, domain: str, role: str) -> bool:
    """Whether this caller may serve requests against this adapter.

    An adapter belongs to the account that uploaded it. A domain admin
    may share one down their domain tree — down only: a parent seeing a
    child's adapter would let a reseller read what their customer built.
    """
    if role == "Admin":
        return True

    # Account *and* domain: a CloudStack account name is unique only
    # within its domain, so comparing names alone would let "dev" in
    # one domain read "dev" in another.
    if (adapter.account, adapter.domain) == (account, domain):
        return True

    if not adapter.shared_with_domain:
        return False

    # Shared within the owning domain and anything beneath it.
    owner = adapter.domain.rstrip("/")
    return domain == owner or domain.startswith(f"{owner}/")


def price_with_premium(base_cost: float, *, premium: float) -> float:
    """Base price plus the configured adapter premium (SPEC.md section 8).

    A fraction rather than a rate of its own: the premium follows the
    base model's tier, so re-pricing a model does not leave its
    adapters on a stale number.
    """
    if premium < 0:
        raise ValueError("an adapter premium may not be negative")
    return base_cost * (1 + premium)
