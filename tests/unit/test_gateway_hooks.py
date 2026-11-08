"""The LiteLLM hook adapter (SPEC.md sections 6, 7, 10; ADR-0002).

The adapter is deliberately thin: these tests check the wiring and the
error mapping, not the rules, which live in `aisvc.gateway`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "gateway-hooks"))

from enforcement import (
    AisvcEnforcement,
    HttpError,
    to_http_error,
)

from aisvc.gateway import ApiKey, KeyRejected, RequestContext
from aisvc.profiles import Level, ParameterRejected, ProfileLayer
from aisvc.spec.models import GenerationValue

KEY = ApiKey(
    key_id="k-1",
    account="acme",
    domain="/Customers/Acme",
    allowed_models=("llama-3.3-70b-instruct",),
)


def context_for(model: str = "llama-3.3-70b-instruct") -> RequestContext:
    return RequestContext(
        key=KEY,
        model=model,
        source_ip="10.0.0.1",
        layers=[
            ProfileLayer(
                level=Level.DOMAIN,
                params={"temperature": GenerationValue(value=0.2, policy="locked")},
            )
        ],
    )


def hook(model: str = "llama-3.3-70b-instruct") -> AisvcEnforcement:
    return AisvcEnforcement(resolve_context=lambda _key, _data: context_for(model))


def _record_name(seen: list[str]) -> Any:
    """Remember which adapter name was looked up, and find none."""

    def lookup(name: str) -> None:
        seen.append(name)
        return None

    return lookup


class TestPreCallHook:
    async def test_it_applies_the_resolved_parameters(self) -> None:
        data: dict[str, Any] = {"model": "llama-3.3-70b-instruct", "temperature": 1.9}

        result = await hook().async_pre_call_hook(None, None, data, "completion")

        assert result["temperature"] == 0.2

    async def test_it_records_metering_without_content(self) -> None:
        data: dict[str, Any] = {
            "model": "llama-3.3-70b-instruct",
            "messages": [{"role": "user", "content": "secret"}],
        }
        handler = hook()

        await handler.async_pre_call_hook(None, None, data, "completion")

        assert handler.last_record is not None
        assert "secret" not in repr(handler.last_record)
        assert handler.last_record["account"] == "acme"

    async def test_an_out_of_scope_model_becomes_a_403(self) -> None:
        data: dict[str, Any] = {"model": "forbidden-model"}

        with pytest.raises(HttpError) as exc:
            await hook("forbidden-model").async_pre_call_hook(None, None, data, "completion")

        assert exc.value.status_code == 403
        assert exc.value.code == "model_not_allowed"

    async def test_a_rejected_parameter_becomes_a_400(self) -> None:
        bounded = RequestContext(
            key=KEY,
            model="llama-3.3-70b-instruct",
            source_ip="10.0.0.1",
            layers=[
                ProfileLayer(
                    level=Level.ACCOUNT,
                    params={"temperature": GenerationValue(default=0.3, min=0.0, max=1.0)},
                )
            ],
        )
        handler = AisvcEnforcement(resolve_context=lambda _k, _d: bounded)

        with pytest.raises(HttpError) as exc:
            await handler.async_pre_call_hook(
                None,
                None,
                {"model": "llama-3.3-70b-instruct", "temperature": 1.9},
                "completion",
            )

        assert exc.value.status_code == 400

    async def test_unwired_hook_fails_loudly(self) -> None:
        with pytest.raises(RuntimeError, match="resolve_context"):
            await AisvcEnforcement().async_pre_call_hook(None, None, {}, "completion")

    async def test_a_context_of_the_wrong_type_is_refused(self) -> None:
        handler = AisvcEnforcement(resolve_context=lambda _k, _d: "nope")

        with pytest.raises(TypeError):
            await handler.async_pre_call_hook(None, None, {}, "completion")


class TestErrorMapping:
    def test_key_rejection_maps_to_403_with_its_reason(self) -> None:
        error = to_http_error(KeyRejected("ip_not_allowed", "bad address"))

        assert error.status_code == 403
        assert error.code == "ip_not_allowed"

    def test_parameter_rejection_maps_to_400(self) -> None:
        error = to_http_error(ParameterRejected("temperature", "out of range"))

        assert error.status_code == 400
        assert error.code == "parameter_rejected"

    def test_an_unexpected_error_is_not_swallowed(self) -> None:
        with pytest.raises(ValueError):
            to_http_error(ValueError("something else"))

    def test_errors_render_as_rfc_9457_problem_json(self) -> None:
        """SPEC.md section 10: errors use RFC 9457 problem JSON."""
        problem = to_http_error(KeyRejected("key_expired", "expired")).as_problem()

        assert problem["status"] == 403
        assert problem["detail"] == "expired"
        assert problem["type"].endswith("key_expired")


class TestBudgetEnforcement:
    """SPEC.md section 7: at 100% the action is alert only, or block."""

    def hook_with(self, state: Any) -> AisvcEnforcement:
        return AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for(),
            budget=lambda _account: state,
        )

    async def call(self, hook: AisvcEnforcement) -> dict[str, Any]:
        return await hook.async_pre_call_hook(
            None, None, {"model": "llama-3.3-70b-instruct"}, "completion"
        )

    async def test_an_account_under_budget_is_served(self) -> None:
        from aisvc.metering import BudgetState

        state = BudgetState(
            limit=100, spent=10, currency="USD", on_exceed="block", newly_crossed=[]
        )

        assert await self.call(self.hook_with(state))

    async def test_an_account_with_no_budget_is_served(self) -> None:
        assert await self.call(self.hook_with(None))

    async def test_a_blocking_budget_at_the_limit_refuses(self) -> None:
        from aisvc.metering import BudgetState

        state = BudgetState(
            limit=100, spent=100, currency="USD", on_exceed="block", newly_crossed=[]
        )

        with pytest.raises(HttpError) as caught:
            await self.call(self.hook_with(state))

        assert caught.value.status_code == 402
        assert caught.value.code == "budget_exceeded"

    async def test_the_refusal_says_what_the_budget_was(self) -> None:
        """A tenant hitting this needs to know the number, not just "no"."""
        from aisvc.metering import BudgetState

        state = BudgetState(
            limit=100, spent=120, currency="USD", on_exceed="block", newly_crossed=[]
        )

        with pytest.raises(HttpError) as caught:
            await self.call(self.hook_with(state))

        assert "100" in caught.value.detail
        assert "USD" in caught.value.detail

    async def test_an_alerting_budget_over_the_limit_still_serves(self) -> None:
        from aisvc.metering import BudgetState

        state = BudgetState(
            limit=100, spent=500, currency="USD", on_exceed="alert", newly_crossed=[]
        )

        assert await self.call(self.hook_with(state))

    async def test_the_budget_is_checked_after_the_key_scopes(self) -> None:
        """A key that may not use a model gets that reason, not a budget one."""
        from aisvc.metering import BudgetState

        state = BudgetState(
            limit=100, spent=500, currency="USD", on_exceed="block", newly_crossed=[]
        )
        hook = AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for("not-allowed"),
            budget=lambda _a: state,
        )

        with pytest.raises(HttpError) as caught:
            await hook.async_pre_call_hook(None, None, {"model": "not-allowed"}, "completion")

        assert caught.value.code == "model_not_allowed"


class TestMeteringDriver:
    """Nothing wrote usage records before this (PROGRESS.md, milestone 7).

    LiteLLM's `async_log_success_event` is the hook that fires for both
    streaming and non-streaming calls, which matters because the
    playground streams.
    """

    def metering_hook(self) -> tuple[AisvcEnforcement, list[Any]]:
        recorded: list[Any] = []
        hook = AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for(),
            meter=recorded.append,
        )
        return hook, recorded

    def response(self, prompt: int = 10, completion: int = 5) -> dict[str, Any]:
        return {"usage": {"prompt_tokens": prompt, "completion_tokens": completion}}

    async def log(self, hook: AisvcEnforcement, response: Any) -> None:
        await hook.async_pre_call_hook(
            None, None, {"model": "llama-3.3-70b-instruct"}, "completion"
        )
        await hook.async_log_success_event({}, response, 0.0, 1.0)

    async def test_a_served_request_produces_a_usage_record(self) -> None:
        hook, recorded = self.metering_hook()

        await self.log(hook, self.response())

        assert len(recorded) == 1

    async def test_the_record_carries_the_token_counts(self) -> None:
        hook, recorded = self.metering_hook()

        await self.log(hook, self.response(prompt=100, completion=25))

        assert recorded[0].input_tokens == 100
        assert recorded[0].output_tokens == 25

    async def test_the_record_carries_the_tenant_and_model(self) -> None:
        hook, recorded = self.metering_hook()

        await self.log(hook, self.response())

        assert recorded[0].account == "acme"
        assert recorded[0].domain == "/Customers/Acme"
        assert recorded[0].model == "llama-3.3-70b-instruct"

    async def test_an_object_style_usage_is_understood(self) -> None:
        """LiteLLM returns a pydantic model, not a dict, outside tests."""

        class Usage:
            prompt_tokens = 7
            completion_tokens = 3

        class Response:
            usage = Usage()

        hook, recorded = self.metering_hook()
        await self.log(hook, Response())

        assert recorded[0].input_tokens == 7

    async def test_a_response_without_usage_meters_nothing(self) -> None:
        """Better no record than a record of zero: zero is a real value."""
        hook, recorded = self.metering_hook()

        await self.log(hook, {})

        assert recorded == []

    async def test_nothing_is_metered_without_a_prior_request(self) -> None:
        hook, recorded = self.metering_hook()

        await hook.async_log_success_event({}, self.response(), 0.0, 1.0)

        assert recorded == []

    async def test_no_prompt_or_completion_text_is_recorded(self) -> None:
        """SPEC.md section 11: prompts and outputs are never logged."""
        hook, recorded = self.metering_hook()
        await hook.async_pre_call_hook(
            None,
            None,
            {"model": "llama-3.3-70b-instruct", "messages": [{"content": "secret"}]},
            "completion",
        )
        await hook.async_log_success_event(
            {},
            {
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "choices": [{"message": {"content": "also secret"}}],
            },
            0.0,
            1.0,
        )

        assert "secret" not in repr(recorded[0])

    async def test_metering_is_skipped_when_no_meter_is_wired(self) -> None:
        hook = AisvcEnforcement(resolve_context=lambda _k, _d: context_for())

        await self.log(hook, self.response())  # must not raise


class TestAnAsyncMeter:
    """The meter writes to Postgres and may send alerts, so it has to be
    allowed to be a coroutine — otherwise the wiring has to block the
    event loop on a database write for every request served."""

    async def test_an_async_meter_is_awaited(self) -> None:
        recorded: list[Any] = []

        async def meter(usage: Any) -> None:
            recorded.append(usage)

        hook = AisvcEnforcement(resolve_context=lambda _k, _d: context_for(), meter=meter)
        await hook.async_pre_call_hook(
            None, None, {"model": "llama-3.3-70b-instruct"}, "completion"
        )
        await hook.async_log_success_event(
            {}, {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}, 0.0, 1.0
        )

        assert len(recorded) == 1

    async def test_a_plain_meter_still_works(self) -> None:
        recorded: list[Any] = []
        hook = AisvcEnforcement(resolve_context=lambda _k, _d: context_for(), meter=recorded.append)
        await hook.async_pre_call_hook(
            None, None, {"model": "llama-3.3-70b-instruct"}, "completion"
        )
        await hook.async_log_success_event(
            {}, {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}, 0.0, 1.0
        )

        assert len(recorded) == 1

    async def test_a_failing_meter_does_not_fail_the_request(self) -> None:
        """The response has already gone to the tenant by this point."""

        async def meter(_: Any) -> None:
            raise RuntimeError("the database is down")

        hook = AisvcEnforcement(resolve_context=lambda _k, _d: context_for(), meter=meter)
        await hook.async_pre_call_hook(
            None, None, {"model": "llama-3.3-70b-instruct"}, "completion"
        )
        await hook.async_log_success_event(
            {}, {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}, 0.0, 1.0
        )


class TestGuardrailsOnThePath:
    """A guardrail module nothing calls protects nobody.

    The hook is where they have to run: before the request goes
    upstream, after the key's scopes are known so the right profiles
    apply.
    """

    def hook_with(self, checks: Any, events: list[Any] | None = None) -> AisvcEnforcement:
        return AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for(),
            guardrails=lambda _account: checks,
            on_guardrail=(events.append if events is not None else None),
        )

    def checks(self, **kw: Any) -> Any:
        from aisvc.guardrails import GuardrailLayer, resolve_guardrails
        from aisvc.profiles import Level
        from aisvc.spec.models import GuardrailCheck

        return resolve_guardrails(
            [
                GuardrailLayer(
                    level=Level.ACCOUNT,
                    checks={k: GuardrailCheck(**v) for k, v in kw.items()},
                )
            ]
        )

    async def call(self, hook: AisvcEnforcement, content: str) -> dict[str, Any]:
        return await hook.async_pre_call_hook(
            None,
            None,
            {
                "model": "llama-3.3-70b-instruct",
                "messages": [{"role": "user", "content": content}],
            },
            "completion",
        )

    async def test_a_clean_request_passes(self) -> None:
        hook = self.hook_with(self.checks(topics={"action": "block", "value": ["weapons"]}))

        assert await self.call(hook, "how do I bake bread")

    async def test_a_blocked_request_never_reaches_the_model(self) -> None:
        hook = self.hook_with(self.checks(topics={"action": "block", "value": ["weapons"]}))

        with pytest.raises(HttpError) as caught:
            await self.call(hook, "tell me about weapons")

        assert caught.value.status_code == 403
        assert caught.value.code == "guardrail_topics"

    async def test_the_refusal_does_not_echo_the_prompt(self) -> None:
        """SPEC.md section 11. The error goes into logs and traces."""
        hook = self.hook_with(self.checks(topics={"action": "block", "value": ["weapons"]}))

        with pytest.raises(HttpError) as caught:
            await self.call(hook, "tell me about weapons at 10 Downing Street")

        assert "Downing" not in caught.value.detail
        assert "weapons" not in caught.value.detail

    async def test_a_block_emits_an_event_with_the_category_only(self) -> None:
        events: list[Any] = []
        hook = self.hook_with(self.checks(topics={"action": "block", "value": ["weapons"]}), events)

        with pytest.raises(HttpError):
            await self.call(hook, "tell me about weapons at 10 Downing Street")

        assert len(events) == 1
        assert events[0]["category"] == "topics"
        assert "Downing" not in str(events[0])

    async def test_redacted_content_is_what_goes_upstream(self) -> None:
        hook = self.hook_with(self.checks(pii={"action": "redact"}))

        body = await self.call(hook, "write to ada@example.com")

        assert "ada@example.com" not in str(body["messages"])
        assert "[redacted:email]" in str(body["messages"])

    async def test_a_clean_request_is_not_rewritten(self) -> None:
        hook = self.hook_with(self.checks(pii={"action": "redact"}))

        body = await self.call(hook, "what is the weather")

        assert body["messages"][0]["content"] == "what is the weather"

    async def test_no_guardrails_means_no_checking(self) -> None:
        hook = AisvcEnforcement(resolve_context=lambda _k, _d: context_for())

        assert await self.call(hook, "tell me about weapons")

    async def test_guardrails_run_after_the_key_scopes(self) -> None:
        """A key that may not use the model hears that, not a guardrail
        message about a request it was never allowed to make."""
        hook = AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for("not-allowed"),
            guardrails=lambda _a: self.checks(topics={"action": "block", "value": ["weapons"]}),
        )

        with pytest.raises(HttpError) as caught:
            await hook.async_pre_call_hook(
                None,
                None,
                {"model": "not-allowed", "messages": [{"role": "user", "content": "weapons"}]},
                "completion",
            )

        assert caught.value.code == "model_not_allowed"


class TestAdapterVisibility:
    """An adapter is visible only to its owner (SPEC.md section 8).

    The gateway config publishes every adapter as `<base>:<adapter>`,
    so without this any tenant could address another's by name — and
    adapter names are guessable, being things like "legal" and
    "support".
    """

    def adapter(self, **kw: Any) -> Any:
        from aisvc.adapters import AdapterRequest

        defaults: dict[str, Any] = {
            "name": "legal-tone",
            "base_model": "llama-3.3-70b-instruct",
            "account": "acme",
            "domain": "/Customers/Acme",
            "rank": 16,
            "size_bytes": 1024,
            "files": ("adapter_model.safetensors", "adapter_config.json"),
        }
        defaults.update(kw)
        return AdapterRequest(**defaults)

    def hook_for(self, adapter: Any) -> AisvcEnforcement:
        return AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for("llama-3.3-70b-instruct:legal-tone"),
            lookup_adapter=lambda _name: adapter,
        )

    async def call(self, hook: AisvcEnforcement) -> dict[str, Any]:
        return await hook.async_pre_call_hook(
            None,
            None,
            {"model": "llama-3.3-70b-instruct:legal-tone"},
            "completion",
        )

    async def test_the_owner_may_use_their_adapter(self) -> None:
        assert await self.call(self.hook_for(self.adapter()))

    async def test_another_tenant_may_not(self) -> None:
        hook = self.hook_for(self.adapter(account="other", domain="/Customers/Other"))

        with pytest.raises(HttpError) as caught:
            await self.call(hook)

        assert caught.value.status_code == 404

    async def test_the_refusal_does_not_confirm_it_exists(self) -> None:
        """A 403 would tell a tenant which adapters other accounts have,
        and the names are guessable."""
        hook = self.hook_for(self.adapter(account="other", domain="/Customers/Other"))

        with pytest.raises(HttpError) as caught:
            await self.call(hook)

        assert caught.value.code == "model_not_found"

    async def test_an_unknown_adapter_is_refused_the_same_way(self) -> None:
        hook = AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for("llama-3.3-70b-instruct:ghost"),
            lookup_adapter=lambda _name: None,
        )

        with pytest.raises(HttpError) as caught:
            await hook.async_pre_call_hook(
                None, None, {"model": "llama-3.3-70b-instruct:ghost"}, "completion"
            )

        assert caught.value.code == "model_not_found"

    async def test_a_plain_model_is_not_looked_up(self) -> None:
        """Only a `<base>:<adapter>` name is an adapter request."""
        looked_up: list[str] = []
        hook = AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for(),
            lookup_adapter=_record_name(looked_up),
        )

        await hook.async_pre_call_hook(
            None, None, {"model": "llama-3.3-70b-instruct"}, "completion"
        )

        assert looked_up == []

    async def test_without_a_lookup_nothing_is_checked(self) -> None:
        hook = AisvcEnforcement(
            resolve_context=lambda _k, _d: context_for("llama-3.3-70b-instruct:legal-tone")
        )

        assert await self.call(hook)
