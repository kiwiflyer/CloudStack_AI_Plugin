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
"""The LiteLLM pre-call hook (SPEC.md sections 6 and 7).

LiteLLM is not a dependency of the engine: the proxy runs as its own
service, and this module is mounted into it. The base class is imported
lazily so the adapter can be unit-tested without installing LiteLLM.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from aisvc.adapters import ADAPTER_SEPARATOR, may_use
from aisvc.gateway import KeyRejected, RequestContext, enforce_request
from aisvc.guardrails import check_input
from aisvc.metering import UsageRecord
from aisvc.profiles import ParameterRejected

try:  # pragma: no cover - exercised only where LiteLLM is installed
    from litellm.integrations.custom_logger import CustomLogger as _Base
except ImportError:  # pragma: no cover - the engine's test environment

    class _Base:  # type: ignore[no-redef]
        """Stand-in so the adapter imports without LiteLLM present."""


log = logging.getLogger("aisvc.gateway.hooks")

__all__ = [
    "AisvcEnforcement",
    "BudgetExceeded",
    "GuardrailBlocked",
    "HttpError",
    "to_http_error",
]


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


class BudgetExceeded(Exception):
    """The account is over a budget whose action is `block`.

    402 rather than 429: this is not a rate the tenant can wait out,
    it is a spending limit someone has to raise.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class GuardrailBlocked(Exception):
    """A guardrail refused the request.

    Carries the category and nothing else. The detail reaches logs,
    traces and the tenant's own error handler, and a guardrail sees
    exactly the prompts least suited to appearing in any of them
    (SPEC.md section 11).
    """

    def __init__(self, category: str) -> None:
        super().__init__(f"blocked by the {category} guardrail")
        self.category = category


def to_http_error(exc: Exception) -> HttpError:
    """Map an enforcement failure onto a tenant-visible error."""
    if isinstance(exc, KeyRejected):
        # "You may not see this" and "this does not exist" answer the
        # same way. A 403 here would confirm which adapters other
        # accounts hold, and adapter names are guessable.
        status = 404 if exc.reason == "model_not_found" else exc.status_code
        return HttpError(status, exc.detail, code=exc.reason)
    if isinstance(exc, GuardrailBlocked):
        return HttpError(
            403,
            f"this request was blocked by the {exc.category} guardrail on your account",
            code=f"guardrail_{exc.category}",
        )
    if isinstance(exc, BudgetExceeded):
        return HttpError(402, exc.detail, code="budget_exceeded")
    if isinstance(exc, ParameterRejected):
        return HttpError(400, str(exc), code="parameter_rejected")
    raise exc


class AisvcEnforcement(_Base):  # type: ignore[misc]  # base is Any without LiteLLM
    """Applies key scopes and generation profiles to every request.

    `resolve_context` is supplied by the proxy's wiring: it turns
    LiteLLM's user_api_key_dict into a `RequestContext`, including the
    profile layers for this domain, account, deployment and key.

    `budget` resolves an account to its `BudgetState`, or None where
    the account has no budget. `meter` receives a `UsageRecord` once a
    request has been served: it is the only thing that writes usage,
    so a deployment without it meters nothing at all.
    """

    def __init__(
        self,
        resolve_context: Any = None,
        *,
        budget: Any = None,
        meter: Any = None,
        guardrails: Any = None,
        on_guardrail: Any = None,
        lookup_adapter: Any = None,
    ) -> None:
        self.resolve_context = resolve_context
        self.budget = budget
        self.meter = meter
        self.guardrails = guardrails
        self.on_guardrail = on_guardrail
        self.lookup_adapter = lookup_adapter
        self.last_record: dict[str, Any] | None = None
        self._last_context: RequestContext | None = None

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
            # Scopes first, then the budget: a key that may not use this
            # model should hear that, not a billing message it cannot act on.
            self._check_adapter(context)
            enforced = enforce_request(context, data)
            self._check_budget(context)
            self._check_guardrails(context, enforced.body)
        except (
            KeyRejected,
            ParameterRejected,
            BudgetExceeded,
            GuardrailBlocked,
        ) as exc:
            raise to_http_error(exc) from exc

        # Effective parameters are metered; prompt content never is.
        self.last_record = enforced.metering_record()
        self._last_context = context
        return enforced.body

    def _check_adapter(self, context: RequestContext) -> None:
        """Refuse an adapter this caller may not see (SPEC.md section 8).

        The gateway config publishes every adapter as
        `<base>:<adapter>`, so without this a tenant could address
        another account's by name — and the names are guessable, being
        things like "legal" and "support".

        An adapter they may not see answers exactly as one that does
        not exist: a 403 would confirm which adapters other accounts
        have.
        """
        if self.lookup_adapter is None or ADAPTER_SEPARATOR not in context.model:
            return

        _, _, name = context.model.partition(ADAPTER_SEPARATOR)
        adapter = self.lookup_adapter(name)
        allowed = adapter is not None and may_use(
            adapter,
            account=context.key.account,
            domain=context.key.domain,
            role="User",
        )
        if not allowed:
            raise KeyRejected("model_not_found", f"no such model: {context.model}")

    def _check_budget(self, context: RequestContext) -> None:
        if self.budget is None:
            return
        state = self.budget(context.key.account)
        if state is not None and state.should_block:
            raise BudgetExceeded(
                f"account {context.key.account} has spent "
                f"{state.spent:.2f} of its {state.limit:.2f} {state.currency} "
                "monthly budget; ask an administrator to raise it"
            )

    def _check_guardrails(self, context: RequestContext, body: dict[str, Any]) -> None:
        """Apply the account's guardrails, rewriting redactions in place.

        After the key's scopes, so a key that may not use this model
        hears that rather than a guardrail message about a request it
        was never allowed to make.
        """
        if self.guardrails is None:
            return

        checks = self.guardrails(context.key.account)
        if not checks:
            return

        for message in body.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, str):
                # Vision parts are a list; nothing to scan as text.
                continue

            decision = check_input(content, checks)
            if not decision.allowed:
                if self.on_guardrail is not None:
                    self.on_guardrail(decision.as_event())
                raise GuardrailBlocked(decision.category)

            if decision.redacted is not None:
                message["content"] = decision.redacted

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Write the usage record for a served request.

        This is the metering driver: nothing else produces usage. It is
        `async_log_success_event` rather than a post-call hook because
        that one fires for streaming responses too, and the playground
        streams.

        Token counts only. The response carries the completion text and
        none of it is read (SPEC.md section 11).
        """
        if self.meter is None or self._last_context is None:
            return

        counts = _token_counts(response_obj)
        if counts is None:
            # No usage block means we do not know what was used. A zero
            # record would be a claim; absence is the truth.
            return

        context = self._last_context
        self._last_context = None
        record = UsageRecord(
            account=context.key.account,
            domain=context.key.domain,
            model=context.model,
            input_tokens=counts[0],
            output_tokens=counts[1],
        )

        try:
            # The meter writes to Postgres and may raise budget alerts,
            # so it is allowed to be a coroutine rather than blocking
            # the proxy's event loop on a write per request.
            result = self.meter(record)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - the response has already been sent
            log.exception("failed to meter a served request")

    def _context_for(self, user_api_key_dict: Any, data: dict[str, Any]) -> RequestContext:
        if self.resolve_context is None:
            raise RuntimeError(
                "AisvcEnforcement needs a resolve_context callable; the proxy wiring supplies one"
            )
        context = self.resolve_context(user_api_key_dict, data)
        if not isinstance(context, RequestContext):
            raise TypeError("resolve_context must return a RequestContext")
        return context


def _token_counts(response_obj: Any) -> tuple[int, int] | None:
    """Pull prompt and completion counts out of a LiteLLM response.

    Handles both the mapping shape used in tests and the pydantic model
    LiteLLM actually returns.
    """
    usage = (
        response_obj.get("usage")
        if isinstance(response_obj, dict)
        else getattr(response_obj, "usage", None)
    )
    if usage is None:
        return None

    def count(field: str) -> int | None:
        value = (
            usage.get(field) if isinstance(usage, dict) else getattr(usage, field, None)
        )
        return int(value) if value is not None else None

    prompt, completion = count("prompt_tokens"), count("completion_tokens")
    if prompt is None and completion is None:
        return None
    return prompt or 0, completion or 0
