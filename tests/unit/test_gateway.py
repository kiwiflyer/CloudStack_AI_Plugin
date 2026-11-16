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
"""Gateway routing, key scopes and enforcement (SPEC.md sections 6, 7, 10).

ADR-0002 chose LiteLLM precisely so the hook can call the same
`aisvc.profiles.resolve()` the engine answers
`getAiEffectiveParameters` with. The decisive test here is
`test_the_hook_and_the_effective_parameters_view_agree`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aisvc.gateway import (
    ApiKey,
    KeyRejected,
    RequestContext,
    build_litellm_config,
    enforce_request,
    key_allows,
)
from aisvc.profiles import Level, ParameterRejected, ProfileLayer, resolve
from aisvc.spec import PlatformSpec, load_spec_from_string
from aisvc.spec.models import GenerationValue
from tests.unit.test_spec import LICENSES, MINIMAL


def spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


def key(**kw: object) -> ApiKey:
    defaults: dict[str, object] = {
        "key_id": "k-1",
        "account": "acme",
        "domain": "/Customers/Acme",
        "allowed_models": ("llama-3.3-70b-instruct",),
        "expires_at": None,
        "cidrs": (),
    }
    defaults.update(kw)
    return ApiKey(**defaults)  # type: ignore[arg-type]


def layer(level: Level, **params: GenerationValue) -> ProfileLayer:
    return ProfileLayer(level=level, params=params)


class TestKeyScopes:
    def test_an_allowed_model_passes(self) -> None:
        key_allows(key(), model="llama-3.3-70b-instruct", source_ip="10.0.0.1")

    def test_scoped_key_rejects_other_model(self) -> None:
        """SPEC.md section 12, named test (model half)."""
        with pytest.raises(KeyRejected) as exc:
            key_allows(key(), model="some-other-model", source_ip="10.0.0.1")

        assert "some-other-model" in str(exc.value)
        assert exc.value.reason == "model_not_allowed"

    def test_scoped_key_rejects_other_ip(self) -> None:
        """SPEC.md section 12, named test (CIDR half)."""
        scoped = key(cidrs=("10.0.0.0/24",))

        key_allows(scoped, model="llama-3.3-70b-instruct", source_ip="10.0.0.9")
        with pytest.raises(KeyRejected) as exc:
            key_allows(scoped, model="llama-3.3-70b-instruct", source_ip="192.168.1.1")

        assert exc.value.reason == "ip_not_allowed"

    def test_an_empty_cidr_list_allows_any_address(self) -> None:
        key_allows(key(cidrs=()), model="llama-3.3-70b-instruct", source_ip="8.8.8.8")

    def test_several_cidrs_are_all_considered(self) -> None:
        scoped = key(cidrs=("10.0.0.0/24", "192.168.0.0/16"))
        key_allows(scoped, model="llama-3.3-70b-instruct", source_ip="192.168.5.5")

    def test_a_malformed_source_address_is_rejected(self) -> None:
        with pytest.raises(KeyRejected):
            key_allows(
                key(cidrs=("10.0.0.0/24",)),
                model="llama-3.3-70b-instruct",
                source_ip="not-an-ip",
            )

    def test_an_expired_key_is_rejected(self) -> None:
        stale = key(expires_at=datetime.now(UTC) - timedelta(minutes=1))

        with pytest.raises(KeyRejected) as exc:
            key_allows(stale, model="llama-3.3-70b-instruct", source_ip="10.0.0.1")

        assert exc.value.reason == "key_expired"

    def test_a_key_expiring_later_is_fine(self) -> None:
        fresh = key(expires_at=datetime.now(UTC) + timedelta(hours=1))
        key_allows(fresh, model="llama-3.3-70b-instruct", source_ip="10.0.0.1")

    def test_a_wildcard_key_allows_any_model(self) -> None:
        key_allows(key(allowed_models=()), model="anything", source_ip="10.0.0.1")

    def test_the_rejection_explains_itself(self) -> None:
        """SPEC.md section 7: 403 with a clear reason."""
        with pytest.raises(KeyRejected) as exc:
            key_allows(key(), model="nope", source_ip="10.0.0.1")

        assert exc.value.status_code == 403
        assert "nope" in exc.value.detail


class TestEnforceRequest:
    def context(self, **kw: object) -> RequestContext:
        defaults: dict[str, object] = {
            "key": key(),
            "model": "llama-3.3-70b-instruct",
            "source_ip": "10.0.0.1",
            "layers": [
                layer(Level.DOMAIN, temperature=GenerationValue(value=0.2, policy="locked")),
                layer(
                    Level.ACCOUNT,
                    max_tokens=GenerationValue(default=1024, max=4096, policy="clamp"),
                ),
            ],
        }
        defaults.update(kw)
        return RequestContext(**defaults)  # type: ignore[arg-type]

    def test_the_locked_value_is_applied_to_the_request(self) -> None:
        body = {"model": "llama-3.3-70b-instruct", "temperature": 1.9, "messages": []}

        result = enforce_request(self.context(), body)

        assert result.body["temperature"] == 0.2

    def test_a_clamped_value_is_bounded(self) -> None:
        body = {"model": "llama-3.3-70b-instruct", "max_tokens": 99999, "messages": []}

        result = enforce_request(self.context(), body)

        assert result.body["max_tokens"] == 4096

    def test_untouched_request_fields_survive(self) -> None:
        body = {
            "model": "llama-3.3-70b-instruct",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }

        result = enforce_request(self.context(), body)

        assert result.body["messages"] == [{"role": "user", "content": "hi"}]
        assert result.body["stream"] is True

    def test_an_out_of_scope_model_is_rejected_before_profiles_run(self) -> None:
        body = {"model": "other-model", "messages": []}

        with pytest.raises(KeyRejected):
            enforce_request(self.context(model="other-model"), body)

    def test_the_effective_parameters_are_returned_for_metering(self) -> None:
        """SPEC.md section 6: the gateway records effective parameters."""
        body = {"model": "llama-3.3-70b-instruct", "messages": []}

        result = enforce_request(self.context(), body)

        assert result.effective["temperature"].value == 0.2
        assert result.effective["temperature"].source is Level.DOMAIN

    def test_no_prompt_content_reaches_the_metering_record(self) -> None:
        """SPEC.md section 11: prompts are never logged by default."""
        body = {
            "model": "llama-3.3-70b-instruct",
            "messages": [{"role": "user", "content": "my social security number is"}],
        }

        record = enforce_request(self.context(), body).metering_record()

        serialised = repr(record)
        assert "social security" not in serialised
        assert "messages" not in record

    def test_the_metering_record_identifies_the_tenant_and_model(self) -> None:
        record = enforce_request(
            self.context(), {"model": "llama-3.3-70b-instruct", "messages": []}
        ).metering_record()

        assert record["account"] == "acme"
        assert record["domain"] == "/Customers/Acme"
        assert record["model"] == "llama-3.3-70b-instruct"
        assert record["key_id"] == "k-1"

    def test_the_metering_record_carries_effective_parameters(self) -> None:
        record = enforce_request(
            self.context(), {"model": "llama-3.3-70b-instruct", "messages": []}
        ).metering_record()

        assert record["effective_parameters"]["temperature"]["value"] == 0.2
        assert record["effective_parameters"]["temperature"]["source"] == "domain"

    def test_a_rejected_parameter_surfaces_as_a_client_error(self) -> None:
        context = self.context(
            layers=[
                layer(
                    Level.ACCOUNT,
                    temperature=GenerationValue(default=0.3, min=0.0, max=1.0),
                )
            ]
        )
        body = {"model": "llama-3.3-70b-instruct", "temperature": 1.9, "messages": []}

        with pytest.raises(ParameterRejected):
            enforce_request(context, body)


class TestOneImplementation:
    def test_the_hook_and_the_effective_parameters_view_agree(self) -> None:
        """ADR-0002's whole reason for choosing LiteLLM.

        What the gateway enforces and what getAiEffectiveParameters
        reports come from the same call, so they cannot drift.
        """
        layers = [
            layer(Level.PLATFORM, temperature=GenerationValue(default=0.7)),
            layer(Level.DOMAIN, max_tokens=GenerationValue(value=512, policy="locked")),
            layer(
                Level.ACCOUNT,
                temperature=GenerationValue(default=0.4, min=0.0, max=1.0),
            ),
        ]
        body = {"model": "llama-3.3-70b-instruct", "temperature": 0.9, "messages": []}

        enforced = enforce_request(
            RequestContext(
                key=key(),
                model="llama-3.3-70b-instruct",
                source_ip="10.0.0.1",
                layers=layers,
            ),
            body,
        )
        reported = resolve(layers, request={"temperature": 0.9})

        assert enforced.effective.describe() == reported.describe()
        assert enforced.body["temperature"] == reported["temperature"].value
        assert enforced.body["max_tokens"] == reported["max_tokens"].value


class TestLiteLlmConfig:
    def test_every_model_becomes_a_model_group(self) -> None:
        config = build_litellm_config(spec(), weights={})

        names = {entry["model_name"] for entry in config["model_list"]}
        assert "llama-3.3-70b-instruct" in names

    def test_a_deployment_points_at_the_colour_service(self) -> None:
        config = build_litellm_config(spec(), weights={})

        entry = config["model_list"][0]
        assert "llama-3.3-70b-instruct-blue" in entry["litellm_params"]["api_base"]

    def test_canary_weights_produce_two_weighted_entries(self) -> None:
        """ADR-0001: blue/green canary weighting lives at the gateway."""
        config = build_litellm_config(
            spec(), weights={"llama-3.3-70b-instruct": {"blue": 90, "green": 10}}
        )

        entries = [e for e in config["model_list"] if e["model_name"] == "llama-3.3-70b-instruct"]
        assert len(entries) == 2
        assert {e["litellm_params"]["weight"] for e in entries} == {90, 10}

    def test_a_zero_weight_colour_is_omitted(self) -> None:
        config = build_litellm_config(
            spec(), weights={"llama-3.3-70b-instruct": {"blue": 100, "green": 0}}
        )

        entries = [e for e in config["model_list"] if e["model_name"] == "llama-3.3-70b-instruct"]
        assert len(entries) == 1

    def test_content_logging_is_off(self) -> None:
        """SPEC.md section 11: prompts and outputs are never logged by default."""
        config = build_litellm_config(spec(), weights={})

        assert config["litellm_settings"]["turn_off_message_logging"] is True

    def test_the_enforcement_hook_is_registered(self) -> None:
        config = build_litellm_config(spec(), weights={})

        assert config["litellm_settings"]["callbacks"]

    def test_the_config_is_deterministic(self) -> None:
        assert build_litellm_config(spec(), weights={}) == build_litellm_config(spec(), weights={})
