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
"""Guardrail profiles (SPEC.md sections 6 and 8).

"The third profile type in section 6, with the same scopes and lock
semantics." So a domain admin can lock a school-safe profile and no
account or key beneath it can turn the check off.

The rule that shapes the rest: a block records the **category only**,
never the content (SPEC.md section 11). A guardrail that logged what it
blocked would be the single richest source of tenant prompts on the
platform, which is the opposite of the point.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.guardrails import (
    EDUCATION_SAFE,
    Action,
    GuardrailDecision,
    GuardrailLayer,
    ResolvedCheck,
    check_input,
    presets,
    resolve_guardrails,
)
from aisvc.profiles import Level
from aisvc.spec.models import GuardrailCheck


def layer(level: Level, **checks: GuardrailCheck) -> GuardrailLayer:
    return GuardrailLayer(level=level, checks=dict(checks))


def check(action: str = "block", policy: str = "default", value: Any = None) -> GuardrailCheck:
    return GuardrailCheck(action=action, policy=policy, value=value)


class TestResolution:
    def test_a_single_layer_applies(self) -> None:
        resolved = resolve_guardrails([layer(Level.PLATFORM, pii=check("redact"))])

        assert resolved["pii"].action is Action.REDACT

    def test_a_more_specific_level_wins(self) -> None:
        resolved = resolve_guardrails(
            [
                layer(Level.PLATFORM, pii=check("flag")),
                layer(Level.ACCOUNT, pii=check("block")),
            ]
        )

        assert resolved["pii"].action is Action.BLOCK

    def test_a_locked_value_beats_every_level_below(self) -> None:
        """A school's domain admin locks this; the account cannot undo it."""
        resolved = resolve_guardrails(
            [
                layer(Level.DOMAIN, pii=check("block", policy="locked")),
                layer(Level.ACCOUNT, pii=check("flag")),
            ]
        )

        assert resolved["pii"].action is Action.BLOCK

    def test_a_locked_value_reports_where_it_came_from(self) -> None:
        resolved = resolve_guardrails(
            [
                layer(Level.DOMAIN, pii=check("block", policy="locked")),
                layer(Level.ACCOUNT, pii=check("flag")),
            ]
        )

        assert resolved["pii"].source is Level.DOMAIN
        assert resolved["pii"].locked is True

    def test_an_unlocked_value_reports_its_own_level(self) -> None:
        resolved = resolve_guardrails(
            [
                layer(Level.PLATFORM, pii=check("flag")),
                layer(Level.ACCOUNT, pii=check("block")),
            ]
        )

        assert resolved["pii"].source is Level.ACCOUNT

    def test_checks_at_different_levels_all_apply(self) -> None:
        resolved = resolve_guardrails(
            [
                layer(Level.PLATFORM, pii=check("redact")),
                layer(Level.ACCOUNT, topics=check("block", value=["weapons"])),
            ]
        )

        assert set(resolved) == {"pii", "topics"}

    def test_no_layers_means_no_guardrails(self) -> None:
        assert resolve_guardrails([]) == {}

    def test_two_profiles_at_one_level_is_refused(self) -> None:
        """Same rule as generation profiles: one per level, or the
        result depends on list order."""
        with pytest.raises(ValueError, match="level"):
            resolve_guardrails(
                [
                    layer(Level.ACCOUNT, pii=check("block")),
                    layer(Level.ACCOUNT, pii=check("flag")),
                ]
            )


class TestInputSize:
    def test_a_short_input_passes(self) -> None:
        resolved = resolve_guardrails([layer(Level.PLATFORM, input_size=check("block", value=100))])

        assert check_input("hello", resolved).allowed

    def test_an_oversized_input_is_blocked(self) -> None:
        resolved = resolve_guardrails([layer(Level.PLATFORM, input_size=check("block", value=10))])

        decision = check_input("x" * 50, resolved)

        assert not decision.allowed
        assert decision.category == "input_size"

    def test_the_limit_is_in_characters_not_tokens(self) -> None:
        """Counting tokens would need the model's tokeniser and a round
        trip; this check exists to stop obvious abuse cheaply."""
        resolved = resolve_guardrails([layer(Level.PLATFORM, input_size=check("block", value=10))])

        assert check_input("x" * 10, resolved).allowed
        assert not check_input("x" * 11, resolved).allowed


class TestTopics:
    def resolved(self, *topics: str, action: str = "block") -> dict[str, ResolvedCheck]:
        return resolve_guardrails([layer(Level.ACCOUNT, topics=check(action, value=list(topics)))])

    def test_an_unrelated_prompt_passes(self) -> None:
        assert check_input("how do I bake bread", self.resolved("weapons")).allowed

    def test_a_denied_topic_is_blocked(self) -> None:
        decision = check_input("tell me about weapons", self.resolved("weapons"))

        assert not decision.allowed
        assert decision.category == "topics"

    def test_matching_ignores_case(self) -> None:
        assert not check_input("WEAPONS please", self.resolved("weapons")).allowed

    def test_a_substring_of_a_longer_word_does_not_match(self) -> None:
        """ "class" must not trip a deny-list entry for "ass"."""
        assert check_input("a class of students", self.resolved("ass")).allowed

    def test_an_empty_deny_list_blocks_nothing(self) -> None:
        assert check_input("anything at all", self.resolved()).allowed


class TestPii:
    def resolved(self, action: str = "redact") -> dict[str, ResolvedCheck]:
        return resolve_guardrails([layer(Level.ACCOUNT, pii=check(action))])

    def test_a_clean_prompt_passes_unchanged(self) -> None:
        decision = check_input("what is the weather", self.resolved())

        assert decision.allowed
        assert decision.redacted is None

    def test_an_email_is_redacted(self) -> None:
        decision = check_input("write to ada@example.com", self.resolved())

        assert decision.allowed
        assert decision.redacted is not None
        assert "ada@example.com" not in decision.redacted
        assert "[redacted:email]" in decision.redacted

    def test_a_card_number_is_redacted(self) -> None:
        decision = check_input("my card is 4111 1111 1111 1111", self.resolved())

        assert decision.redacted is not None
        assert "4111" not in decision.redacted

    def test_pii_can_block_instead_of_redacting(self) -> None:
        decision = check_input("write to ada@example.com", self.resolved("block"))

        assert not decision.allowed
        assert decision.category == "pii"

    def test_flagging_allows_but_records(self) -> None:
        decision = check_input("write to ada@example.com", self.resolved("flag"))

        assert decision.allowed
        assert decision.flagged == ["pii"]


class TestNoContentIsRecorded:
    """SPEC.md sections 8 and 11: category only, never content."""

    def decision(self) -> GuardrailDecision:
        resolved = resolve_guardrails(
            [layer(Level.ACCOUNT, topics=check("block", value=["weapons"]))]
        )
        return check_input("tell me about weapons at 10 Downing Street", resolved)

    def test_the_event_names_only_the_category(self) -> None:
        event = self.decision().as_event()

        assert event["category"] == "topics"
        assert set(event) <= {"category", "action", "at"}

    def test_the_event_carries_no_prompt_text(self) -> None:
        event = self.decision().as_event()

        assert "weapons" not in str(event)
        assert "Downing" not in str(event)

    def test_the_decision_itself_does_not_repr_the_prompt(self) -> None:
        """These end up in tracebacks and logs."""
        assert "Downing" not in repr(self.decision())


class TestPresets:
    def test_an_education_safe_preset_ships(self) -> None:
        """SPEC.md section 8 names this one specifically."""
        assert EDUCATION_SAFE in presets()

    def test_the_education_preset_blocks_rather_than_flags(self) -> None:
        profile = presets()[EDUCATION_SAFE]

        assert profile["topics"].action == "block"

    def test_the_education_preset_is_locked(self) -> None:
        """A school sets this once; an account below must not relax it."""
        for check_spec in presets()[EDUCATION_SAFE].values():
            assert check_spec.policy == "locked"

    def test_every_preset_is_a_usable_profile(self) -> None:
        for name, checks in presets().items():
            resolved = resolve_guardrails([GuardrailLayer(level=Level.DOMAIN, checks=checks)])
            assert resolved, f"{name} resolved to nothing"
