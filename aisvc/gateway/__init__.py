"""Gateway routing, key scopes and enforcement (SPEC.md sections 6, 7, 10).

[ADR-0002](../../docs/adr/0002-gateway.md) chose LiteLLM so that this
module can call `aisvc.profiles.resolve()` directly. That is the whole
point: the value the gateway enforces and the value
`getAiEffectiveParameters` reports come from one function, so they
cannot drift.

What happens to a request, in order:

1. the key's scopes are checked — model, expiry, source address;
2. generation parameters are resolved across the precedence chain and
   written into the request body;
3. a metering record is built carrying the effective parameters and the
   tenant, and **never** prompt or completion content (section 11).

Canary weighting between blue and green lives here too, since
[ADR-0001](../../docs/adr/0001-serving-layer.md) kept it out of the
serving layer.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aisvc.k8s import DEFAULT_NAMESPACE, ENGINE_PORT
from aisvc.profiles import EffectiveParams, ProfileLayer, resolve

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from aisvc.spec import PlatformSpec

__all__ = [
    "ApiKey",
    "EnforcedRequest",
    "KeyRejected",
    "RequestContext",
    "build_litellm_config",
    "enforce_request",
    "key_allows",
]

#: Request fields the gateway governs; everything else passes through.
#: `model`, `messages`, `stream` and friends are the client's business.
_RESERVED_FIELDS = frozenset({"model", "messages", "prompt", "input", "stream"})


class KeyRejected(Exception):
    """A key may not make this request (SPEC.md section 7).

    Carries a machine-readable `reason` and a human-readable `detail`, so
    the 403 can say which scope failed rather than just refusing.
    """

    status_code = 403

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A scoped API key (SPEC.md section 7).

    The secret is never here: keys are stored as salted hashes, and this
    is what a verified key resolves to.
    """

    key_id: str
    account: str
    domain: str
    allowed_models: tuple[str, ...] = ()
    expires_at: datetime | None = None
    cidrs: tuple[str, ...] = ()
    rate_limit_rpm: int | None = None

    @property
    def allows_any_model(self) -> bool:
        return not self.allowed_models


def key_allows(api_key: ApiKey, *, model: str, source_ip: str) -> None:
    """Check a key's scopes, raising `KeyRejected` with the reason."""
    if api_key.expires_at is not None and api_key.expires_at <= datetime.now(UTC):
        raise KeyRejected(
            "key_expired",
            f"key {api_key.key_id} expired at {api_key.expires_at.isoformat()}",
        )

    if not api_key.allows_any_model and model not in api_key.allowed_models:
        allowed = ", ".join(api_key.allowed_models) or "none"
        raise KeyRejected(
            "model_not_allowed",
            f"key {api_key.key_id} may not use model {model!r}; allowed: {allowed}",
        )

    if api_key.cidrs:
        try:
            address = ipaddress.ip_address(source_ip)
        except ValueError as exc:
            raise KeyRejected("ip_not_allowed", f"{source_ip!r} is not a valid IP address") from exc
        permitted = any(
            address in ipaddress.ip_network(cidr, strict=False) for cidr in api_key.cidrs
        )
        if not permitted:
            raise KeyRejected(
                "ip_not_allowed",
                f"key {api_key.key_id} may not be used from {source_ip}",
            )


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who is asking, for what, under which profiles."""

    key: ApiKey
    model: str
    source_ip: str
    layers: Sequence[ProfileLayer] = ()


@dataclass(frozen=True, slots=True)
class EnforcedRequest:
    """The request as it should go upstream, plus what to meter."""

    body: dict[str, Any]
    effective: EffectiveParams
    context: RequestContext = field(repr=False)

    def metering_record(self) -> dict[str, Any]:
        """What metering stores for this request.

        Carries the effective parameters and the tenant, and no prompt or
        completion content at all (SPEC.md section 11). Token counts are
        added by the post-call hook once the response is known.
        """
        return {
            "key_id": self.context.key.key_id,
            "account": self.context.key.account,
            "domain": self.context.key.domain,
            "model": self.context.model,
            "effective_parameters": self.effective.describe(),
        }


def enforce_request(context: RequestContext, body: Mapping[str, Any]) -> EnforcedRequest:
    """Apply key scopes and generation profiles to one request.

    This is what the LiteLLM `async_pre_call_hook` calls.
    """
    key_allows(context.key, model=context.model, source_ip=context.source_ip)

    requested = {
        name: value
        for name, value in body.items()
        if name not in _RESERVED_FIELDS and value is not None
    }
    effective = resolve(context.layers, request=requested)

    updated = dict(body)
    updated.update(effective.to_overrides())

    return EnforcedRequest(body=updated, effective=effective, context=context)


def _service_base(model: str, colour: str, namespace: str) -> str:
    host = f"{model}-{colour}.{namespace}.svc.cluster.local"
    return f"http://{host}:{ENGINE_PORT}/v1"


def build_litellm_config(
    spec: PlatformSpec,
    *,
    weights: Mapping[str, Mapping[str, int]] | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> dict[str, Any]:
    """Render the LiteLLM proxy config from `platform.yaml`.

    `weights` carries the canary split per model, as
    `{"model": {"blue": 90, "green": 10}}`. A colour at weight zero is
    left out entirely rather than registered and never chosen.
    """
    weights = weights or {}
    model_list: list[dict[str, Any]] = []

    for model in spec.document.models:
        split = weights.get(model.name) or {"blue": 100}
        for colour in ("blue", "green"):
            weight = split.get(colour, 0)
            if weight <= 0:
                continue
            params: dict[str, Any] = {
                "model": f"openai/{model.name}",
                "api_base": _service_base(model.name, colour, namespace),
                "api_key": "none",  # cluster-internal; the gateway is the guard
            }
            if len(split) > 1 or weight != 100:
                params["weight"] = weight
            model_list.append(
                {
                    "model_name": model.name,
                    "litellm_params": params,
                    "model_info": {
                        "colour": colour,
                        "task": model.task,
                        "price_tier": model.price_tier,
                    },
                }
            )

    return {
        "model_list": model_list,
        "litellm_settings": {
            # SPEC.md section 11: prompts and outputs are never logged by
            # default. Tenant opt-in logging is a separate setting.
            "turn_off_message_logging": True,
            "callbacks": ["gateway_hooks.enforcement.AisvcEnforcement"],
            "drop_params": False,
        },
        "general_settings": {
            "master_key": "os.environ/AISVC_GATEWAY_MASTER_KEY",
            "database_url": "os.environ/AISVC_DATABASE_URL",
        },
    }
