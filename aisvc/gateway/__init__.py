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

import hashlib
import hmac
import ipaddress
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aisvc.k8s import DEFAULT_NAMESPACE, ENGINE_PORT
from aisvc.profiles import EffectiveParams, ProfileLayer, resolve

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from aisvc.spec import PlatformSpec

__all__ = [
    "KEY_PREFIX",
    "ApiKey",
    "EnforcedRequest",
    "KeyRejected",
    "KeySecret",
    "RequestContext",
    "build_litellm_config",
    "enforce_request",
    "hash_secret",
    "key_allows",
    "mint_key",
    "parse_presented_key",
    "verify_secret",
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
    #: Key-level profile overrides, merged below the deployment's and
    #: above the request's (SPEC.md section 6's precedence).
    parameters: Mapping[str, Any] = field(default_factory=dict)
    name: str = ""

    @property
    def allows_any_model(self) -> bool:
        return not self.allowed_models


#: How many random bytes back a key's secret. 32 bytes is 256 bits, which
#: is why a single salted hash below is enough: the secret is generated,
#: not chosen, so there is no dictionary to run and nothing to stretch
#: against. Password hashing would cost milliseconds on every gateway
#: request to defend against an attack that does not apply.
SECRET_BYTES = 32

#: Marks our keys in logs and support tickets without revealing anything.
KEY_PREFIX = "aisvc"


@dataclass(frozen=True, slots=True)
class KeySecret:
    """A freshly minted key, the only time its plaintext exists.

    SPEC.md section 7: "shown once". Nothing here is persisted but
    `key` and the salt and hash; the plaintext is handed to the caller
    and then dropped.
    """

    key: ApiKey
    secret: str
    salt: str
    hashed: str

    @property
    def key_id(self) -> str:
        return self.key.key_id

    @property
    def plaintext(self) -> str:
        """What the tenant copies, and what they present on a request."""
        return f"{KEY_PREFIX}-{self.key.key_id}-{self.secret}"


def hash_secret(secret: str, *, salt: str) -> str:
    """Salted hash of a key's secret.

    HMAC-SHA256 rather than a password KDF, deliberately: the secret is
    256 random bits, so stretching buys nothing an attacker could use,
    and the gateway hashes one of these on every single request.
    """
    return hmac.new(salt.encode(), secret.encode(), hashlib.sha256).hexdigest()


def verify_secret(secret: str, *, salt: str, hashed: str) -> bool:
    """Constant-time check of a presented secret against its stored hash."""
    return hmac.compare_digest(hash_secret(secret, salt=salt), hashed)


def parse_presented_key(presented: str) -> tuple[str, str]:
    """Split `aisvc-<key_id>-<secret>` into its parts.

    Raises `KeyRejected` rather than returning None so a malformed key
    refuses with the same shape as an out-of-scope one. The secret is
    never named in the error: these get logged.
    """
    parts = presented.split("-", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX or not parts[1] or not parts[2]:
        raise KeyRejected("malformed_key", "the presented API key is not well formed")
    return parts[1], parts[2]


def mint_key(
    *,
    account: str,
    domain: str,
    name: str,
    allowed_models: Sequence[str] = (),
    cidrs: Sequence[str] = (),
    expires_at: datetime | None = None,
    rate_limit_rpm: int | None = None,
    account_quota_rpm: int | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> KeySecret:
    """Create a scoped key (SPEC.md section 7).

    The scopes are validated here rather than at the gateway: a key that
    cannot be honoured should not exist, and refusing at mint time tells
    the operator while they are still looking at the form.
    """
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise ValueError("a key's expiry is in the past; it would never work")

    if (
        rate_limit_rpm is not None
        and account_quota_rpm is not None
        and rate_limit_rpm > account_quota_rpm
    ):
        raise ValueError(
            f"a key's rate limit ({rate_limit_rpm}/min) may not exceed the "
            f"account quota ({account_quota_rpm}/min)"
        )

    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    salt = secrets.token_hex(16)

    key = ApiKey(
        key_id=key_id,
        account=account,
        domain=domain,
        name=name,
        allowed_models=tuple(allowed_models),
        cidrs=tuple(cidrs),
        expires_at=expires_at,
        rate_limit_rpm=rate_limit_rpm,
        parameters=dict(parameters or {}),
    )
    return KeySecret(key=key, secret=secret, salt=salt, hashed=hash_secret(secret, salt=salt))


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
