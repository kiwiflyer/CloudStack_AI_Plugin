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
"""Generation profile resolution (SPEC.md section 6).

Precedence, most specific wins, and a lock beats every level below it:
platform -> model -> domain -> account -> deployment -> key -> request.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.profiles import (
    EffectiveParams,
    Level,
    ParameterRejected,
    ProfileLayer,
    resolve,
)
from aisvc.spec.models import GenerationValue


def layer(level: Level, **params: GenerationValue) -> ProfileLayer:
    return ProfileLayer(level=level, name=f"{level.name.lower()}-profile", params=params)


def value(**kw: Any) -> GenerationValue:
    return GenerationValue(**kw)


class TestSingleLayer:
    def test_a_default_is_resolved(self) -> None:
        effective = resolve([layer(Level.PLATFORM, temperature=value(default=0.7))])
        assert effective["temperature"].value == 0.7

    def test_the_source_level_is_reported(self) -> None:
        effective = resolve([layer(Level.DOMAIN, temperature=value(default=0.2))])
        assert effective["temperature"].source is Level.DOMAIN

    def test_an_unset_parameter_is_absent(self) -> None:
        effective = resolve([layer(Level.PLATFORM, temperature=value(default=0.7))])
        assert "top_p" not in effective

    def test_a_locked_value_uses_value_not_default(self) -> None:
        effective = resolve(
            [layer(Level.DOMAIN, system_prompt_prefix=value(value="Be brief.", policy="locked"))]
        )
        assert effective["system_prompt_prefix"].value == "Be brief."


class TestPrecedence:
    def test_a_more_specific_level_wins(self) -> None:
        effective = resolve(
            [
                layer(Level.PLATFORM, temperature=value(default=0.7)),
                layer(Level.ACCOUNT, temperature=value(default=0.3)),
            ]
        )
        assert effective["temperature"].value == 0.3
        assert effective["temperature"].source is Level.ACCOUNT

    def test_layers_apply_in_precedence_order_not_list_order(self) -> None:
        effective = resolve(
            [
                layer(Level.KEY, temperature=value(default=0.1)),
                layer(Level.PLATFORM, temperature=value(default=0.7)),
            ]
        )
        assert effective["temperature"].value == 0.1

    def test_unset_values_fall_through_to_the_next_level(self) -> None:
        effective = resolve(
            [
                layer(Level.PLATFORM, temperature=value(default=0.7), top_p=value(default=0.9)),
                layer(Level.ACCOUNT, temperature=value(default=0.3)),
            ]
        )
        assert effective["temperature"].value == 0.3
        assert effective["top_p"].value == 0.9
        assert effective["top_p"].source is Level.PLATFORM

    def test_every_level_in_the_chain_resolves(self) -> None:
        layers = [
            layer(Level.PLATFORM, temperature=value(default=0.1)),
            layer(Level.MODEL, temperature=value(default=0.2)),
            layer(Level.DOMAIN, temperature=value(default=0.3)),
            layer(Level.ACCOUNT, temperature=value(default=0.4)),
            layer(Level.DEPLOYMENT, temperature=value(default=0.5)),
            layer(Level.KEY, temperature=value(default=0.6)),
        ]
        effective = resolve(layers)
        assert effective["temperature"].value == 0.6
        assert effective["temperature"].source is Level.KEY


class TestLocking:
    def test_locked_domain_param_beats_account_key_and_request(self) -> None:
        """SPEC.md section 12, named test."""
        effective = resolve(
            [
                layer(Level.DOMAIN, temperature=value(value=0.2, policy="locked")),
                layer(Level.ACCOUNT, temperature=value(default=0.9)),
                layer(Level.KEY, temperature=value(default=1.2)),
            ],
            request={"temperature": 1.5},
        )
        assert effective["temperature"].value == 0.2
        assert effective["temperature"].source is Level.DOMAIN
        assert effective["temperature"].locked_at is Level.DOMAIN

    def test_a_lock_is_reported_so_the_ui_can_grey_it_out(self) -> None:
        effective = resolve([layer(Level.DOMAIN, temperature=value(value=0.2, policy="locked"))])
        assert effective["temperature"].is_locked
        assert effective["temperature"].locked_at is Level.DOMAIN

    def test_a_more_specific_lock_wins_over_a_less_specific_one(self) -> None:
        effective = resolve(
            [
                layer(Level.DOMAIN, temperature=value(value=0.2, policy="locked")),
                layer(Level.ACCOUNT, temperature=value(value=0.4, policy="locked")),
            ]
        )
        # A lock binds everything below it, so the domain's lock holds.
        assert effective["temperature"].value == 0.2
        assert effective["temperature"].locked_at is Level.DOMAIN

    def test_a_lock_does_not_affect_other_parameters(self) -> None:
        effective = resolve(
            [
                layer(Level.DOMAIN, temperature=value(value=0.2, policy="locked")),
                layer(Level.ACCOUNT, top_p=value(default=0.5)),
            ],
            request={"top_p": 0.8},
        )
        assert effective["temperature"].value == 0.2
        assert effective["top_p"].value == 0.8

    def test_a_locked_value_ignores_the_request_silently(self) -> None:
        """A lock is policy, not a client error."""
        effective = resolve(
            [layer(Level.DOMAIN, temperature=value(value=0.2, policy="locked"))],
            request={"temperature": 1.9},
        )
        assert effective["temperature"].value == 0.2


class TestBounds:
    def test_bounds_narrow_as_levels_get_more_specific(self) -> None:
        effective = resolve(
            [
                layer(Level.PLATFORM, temperature=value(default=0.5, min=0.0, max=2.0)),
                layer(Level.DOMAIN, temperature=value(min=0.1, max=1.0)),
            ]
        )
        assert effective["temperature"].min == 0.1
        assert effective["temperature"].max == 1.0

    def test_a_specific_level_cannot_widen_an_inherited_bound(self) -> None:
        """Otherwise a tenant could escape a bound an admin set."""
        effective = resolve(
            [
                layer(Level.DOMAIN, temperature=value(default=0.5, min=0.1, max=1.0)),
                layer(Level.ACCOUNT, temperature=value(min=0.0, max=2.0)),
            ]
        )
        assert effective["temperature"].min == 0.1
        assert effective["temperature"].max == 1.0

    def test_bounds_survive_even_when_the_value_is_overridden(self) -> None:
        effective = resolve(
            [
                layer(Level.PLATFORM, max_tokens=value(default=1024, max=4096)),
                layer(Level.ACCOUNT, max_tokens=value(default=2048)),
            ]
        )
        assert effective["max_tokens"].value == 2048
        assert effective["max_tokens"].max == 4096


class TestRequestOverrides:
    def test_a_request_may_override_within_bounds(self) -> None:
        effective = resolve(
            [layer(Level.ACCOUNT, temperature=value(default=0.3, min=0.0, max=1.0))],
            request={"temperature": 0.8},
        )
        assert effective["temperature"].value == 0.8
        assert effective["temperature"].source is Level.REQUEST

    def test_clamp_policy_bounds_temperature(self) -> None:
        """SPEC.md section 12, named test."""
        effective = resolve(
            [
                layer(
                    Level.DOMAIN,
                    temperature=value(default=0.2, min=0.0, max=0.5, policy="clamp"),
                )
            ],
            request={"temperature": 1.9},
        )
        assert effective["temperature"].value == 0.5
        assert effective["temperature"].clamped_from == 1.9

    def test_clamp_also_raises_a_value_below_the_minimum(self) -> None:
        effective = resolve(
            [layer(Level.DOMAIN, temperature=value(min=0.2, max=0.8, policy="clamp"))],
            request={"temperature": 0.0},
        )
        assert effective["temperature"].value == 0.2

    def test_default_policy_rejects_a_value_outside_the_bounds(self) -> None:
        """`default` permits override *within* min/max; outside is an error."""
        with pytest.raises(ParameterRejected) as exc:
            resolve(
                [layer(Level.ACCOUNT, temperature=value(default=0.3, min=0.0, max=1.0))],
                request={"temperature": 1.9},
            )
        assert "temperature" in str(exc.value)
        assert exc.value.parameter == "temperature"

    def test_the_rejection_names_the_bound_and_the_level_that_set_it(self) -> None:
        with pytest.raises(ParameterRejected) as exc:
            resolve(
                [layer(Level.DOMAIN, max_tokens=value(default=1024, max=4096))],
                request={"max_tokens": 99999},
            )
        assert "4096" in str(exc.value)
        assert exc.value.source is Level.DOMAIN

    def test_a_request_parameter_nobody_declared_is_passed_through(self) -> None:
        """The gateway governs what profiles cover; it is not an allow-list."""
        effective = resolve([], request={"seed": 42})
        assert effective["seed"].value == 42
        assert effective["seed"].source is Level.REQUEST

    def test_a_none_in_the_request_is_ignored_not_treated_as_a_value(self) -> None:
        effective = resolve(
            [layer(Level.ACCOUNT, temperature=value(default=0.3))],
            request={"temperature": None},
        )
        assert effective["temperature"].value == 0.3
        assert effective["temperature"].source is Level.ACCOUNT

    def test_clamping_a_non_numeric_request_value_is_rejected(self) -> None:
        with pytest.raises(ParameterRejected, match="numeric"):
            resolve(
                [layer(Level.DOMAIN, temperature=value(min=0.0, max=1.0, policy="clamp"))],
                request={"temperature": "hot"},
            )


class TestEffectiveParams:
    def test_it_renders_the_overrides_to_apply(self) -> None:
        effective = resolve(
            [
                layer(Level.PLATFORM, temperature=value(default=0.7), top_p=value(default=0.9)),
                layer(Level.DOMAIN, max_tokens=value(value=512, policy="locked")),
            ]
        )
        assert effective.to_overrides() == {
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 512,
        }

    def test_it_reports_every_parameter_with_its_source(self) -> None:
        """Backs getAiEffectiveParameters (SPEC.md section 6)."""
        effective = resolve(
            [
                layer(Level.PLATFORM, temperature=value(default=0.7)),
                layer(Level.DOMAIN, top_p=value(value=0.5, policy="locked")),
            ]
        )
        # A list, each entry naming itself: section 9 declares
        # getAiEffectiveParameters a list command, so the plugin reads
        # `parameters` as an array. `by_name()` is the keyed form, kept
        # for the metering record.
        report = {entry["name"]: entry for entry in effective.describe()}
        assert report["temperature"]["source"] == "platform"
        assert report["top_p"]["source"] == "domain"
        assert report["top_p"]["locked"] is True
        assert report["temperature"]["locked"] is False
        assert effective.by_name()["top_p"]["locked"] is True

    def test_it_is_iterable_and_sized(self) -> None:
        effective = resolve([layer(Level.PLATFORM, a=value(default=1), b=value(default=2))])
        assert isinstance(effective, EffectiveParams)
        assert len(effective) == 2
        assert sorted(effective) == ["a", "b"]

    def test_a_missing_parameter_raises_keyerror(self) -> None:
        effective = resolve([])
        with pytest.raises(KeyError):
            effective["nope"]


class TestSystemPromptPrefix:
    def test_a_locked_prefix_is_applied(self) -> None:
        effective = resolve(
            [
                layer(
                    Level.DOMAIN,
                    system_prompt_prefix=value(value="Answer concisely.", policy="locked"),
                )
            ]
        )
        assert effective["system_prompt_prefix"].value == "Answer concisely."
        assert effective["system_prompt_prefix"].is_locked


class TestOrderingGuarantees:
    def test_two_layers_at_the_same_level_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="more than one"):
            resolve(
                [
                    layer(Level.ACCOUNT, temperature=value(default=0.1)),
                    layer(Level.ACCOUNT, temperature=value(default=0.2)),
                ]
            )

    def test_resolution_does_not_mutate_the_layers(self) -> None:
        original = layer(Level.PLATFORM, temperature=value(default=0.7, max=1.0))
        resolve([original], request={"temperature": 0.9})
        assert original.params["temperature"].default == 0.7
        assert original.params["temperature"].max == 1.0
