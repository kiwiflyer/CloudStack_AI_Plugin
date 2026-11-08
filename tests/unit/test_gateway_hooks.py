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
