"""The LiteLLM pre-call hook (SPEC.md sections 6 and 7).

LiteLLM is not a dependency of the engine: the proxy runs as its own
service, and this module is mounted into it. The base class is imported
lazily so the adapter can be unit-tested without installing LiteLLM.
"""

from __future__ import annotations

from typing import Any

from aisvc.gateway import KeyRejected, RequestContext, enforce_request
from aisvc.profiles import ParameterRejected

try:  # pragma: no cover - exercised only where LiteLLM is installed
    from litellm.integrations.custom_logger import CustomLogger as _Base
except ImportError:  # pragma: no cover - the engine's test environment

    class _Base:  # type: ignore[no-redef]
        """Stand-in so the adapter imports without LiteLLM present."""


__all__ = ["AisvcEnforcement", "HttpError", "to_http_error"]


class HttpError(Exception):
    """What the proxy should return to the tenant."""

    def __init__(self, status_code: int, detail: str, *, code: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code

    def as_problem(self) -> dict[str, Any]:
        """RFC 9457 problem JSON (SPEC.md section 10)."""
        return {
            "type": f"https://cloudstack.apache.org/aisvc/errors/{self.code}",
            "title": self.code.replace("_", " "),
            "status": self.status_code,
            "detail": self.detail,
        }


def to_http_error(exc: Exception) -> HttpError:
    """Map an enforcement failure onto a tenant-visible error."""
    if isinstance(exc, KeyRejected):
        return HttpError(exc.status_code, exc.detail, code=exc.reason)
    if isinstance(exc, ParameterRejected):
        return HttpError(400, str(exc), code="parameter_rejected")
    raise exc


class AisvcEnforcement(_Base):  # type: ignore[misc]  # base is Any without LiteLLM
    """Applies key scopes and generation profiles to every request.

    `resolve_context` is supplied by the proxy's wiring: it turns
    LiteLLM's user_api_key_dict into a `RequestContext`, including the
    profile layers for this domain, account, deployment and key.
    """

    def __init__(self, resolve_context: Any = None) -> None:
        self.resolve_context = resolve_context
        self.last_record: dict[str, Any] | None = None

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any]:
        """Rewrite the request to match the resolved profiles."""
        context = self._context_for(user_api_key_dict, data)
        try:
            enforced = enforce_request(context, data)
        except (KeyRejected, ParameterRejected) as exc:
            raise to_http_error(exc) from exc

        # Effective parameters are metered; prompt content never is.
        self.last_record = enforced.metering_record()
        return enforced.body

    def _context_for(self, user_api_key_dict: Any, data: dict[str, Any]) -> RequestContext:
        if self.resolve_context is None:
            raise RuntimeError(
                "AisvcEnforcement needs a resolve_context callable; the proxy wiring supplies one"
            )
        context = self.resolve_context(user_api_key_dict, data)
        if not isinstance(context, RequestContext):
            raise TypeError("resolve_context must return a RequestContext")
        return context
