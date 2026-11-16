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
"""Usage, pricing and budgets (SPEC.md sections 4 and 7)."""

from __future__ import annotations

import pytest

from aisvc.metering import (
    DEFAULT_THRESHOLDS,
    BudgetState,
    UsageRecord,
    crossed_thresholds,
    evaluate_budget,
    price_usage,
)
from aisvc.spec.models import Budget, PriceTier

STANDARD = PriceTier(name="standard", input_per_mtok=0.60, output_per_mtok=1.80, currency="USD")


def usage(**kw: object) -> UsageRecord:
    defaults: dict[str, object] = {
        "account": "acme",
        "domain": "/Customers/Acme",
        "model": "llama-3.3-70b-instruct",
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "gpu_hours": 0.0,
    }
    defaults.update(kw)
    return UsageRecord(**defaults)  # type: ignore[arg-type]


class TestPricing:
    def test_a_million_tokens_each_way_costs_input_plus_output(self) -> None:
        assert price_usage(usage(), STANDARD) == pytest.approx(2.40)

    def test_input_and_output_are_priced_separately(self) -> None:
        """Output is dearer; charging one rate would misbill every tenant."""
        input_only = price_usage(usage(output_tokens=0), STANDARD)
        output_only = price_usage(usage(input_tokens=0), STANDARD)

        assert input_only == pytest.approx(0.60)
        assert output_only == pytest.approx(1.80)

    def test_pricing_is_per_million_tokens(self) -> None:
        assert price_usage(usage(input_tokens=500_000, output_tokens=0), STANDARD) == pytest.approx(
            0.30
        )

    def test_no_usage_costs_nothing(self) -> None:
        assert price_usage(usage(input_tokens=0, output_tokens=0), STANDARD) == 0.0

    def test_a_free_tier_costs_nothing(self) -> None:
        free = PriceTier(name="free", input_per_mtok=0.0, output_per_mtok=0.0, currency="USD")
        assert price_usage(usage(), free) == 0.0

    def test_fractional_tokens_do_not_round_to_zero(self) -> None:
        """A thousand tokens is a real charge, not a rounding artefact."""
        assert price_usage(usage(input_tokens=1000, output_tokens=0), STANDARD) > 0


class TestUsageRecord:
    def test_total_tokens_is_input_plus_output(self) -> None:
        assert usage(input_tokens=10, output_tokens=5).total_tokens == 15

    def test_records_merge_by_tenant_and_model(self) -> None:
        merged = usage(input_tokens=10, output_tokens=1).merge(
            usage(input_tokens=5, output_tokens=2)
        )

        assert merged.input_tokens == 15
        assert merged.output_tokens == 3

    def test_gpu_hours_accumulate_too(self) -> None:
        merged = usage(gpu_hours=1.5).merge(usage(gpu_hours=2.25))

        assert merged.gpu_hours == pytest.approx(3.75)

    def test_merging_another_tenants_usage_is_refused(self) -> None:
        """Mixing two accounts' usage would misbill both."""
        with pytest.raises(ValueError, match="different"):
            usage(account="acme").merge(usage(account="other"))

    def test_merging_another_models_usage_is_refused(self) -> None:
        with pytest.raises(ValueError, match="different"):
            usage(model="a").merge(usage(model="b"))


class TestThresholds:
    def test_the_spec_defaults_are_fifty_eighty_and_one_hundred(self) -> None:
        assert DEFAULT_THRESHOLDS == (50, 80, 100)

    def test_nothing_is_crossed_below_the_first_threshold(self) -> None:
        assert crossed_thresholds(spent=10, limit=100) == []

    def test_fifty_percent_crosses_the_first(self) -> None:
        assert crossed_thresholds(spent=50, limit=100) == [50]

    def test_every_threshold_below_the_spend_is_crossed(self) -> None:
        assert crossed_thresholds(spent=85, limit=100) == [50, 80]

    def test_being_over_budget_crosses_all_of_them(self) -> None:
        assert crossed_thresholds(spent=150, limit=100) == [50, 80, 100]

    def test_already_notified_thresholds_are_not_repeated(self) -> None:
        """An alert per request at 80% would be unusable."""
        assert crossed_thresholds(spent=85, limit=100, already_notified=[50]) == [80]

    def test_a_zero_limit_is_immediately_exceeded(self) -> None:
        assert crossed_thresholds(spent=1, limit=0) == [50, 80, 100]

    def test_no_spend_against_a_zero_limit_crosses_nothing(self) -> None:
        assert crossed_thresholds(spent=0, limit=0) == []


class TestBudgets:
    def test_under_budget_is_allowed(self) -> None:
        budget = Budget(monthly=100, currency="USD", on_exceed="block")

        state = evaluate_budget(budget, spent=50)

        assert isinstance(state, BudgetState)
        assert not state.should_block
        assert state.percent_used == 50

    def test_a_blocking_budget_blocks_at_one_hundred_percent(self) -> None:
        """SPEC.md section 14: responses stop at 100% after alerts."""
        budget = Budget(monthly=100, currency="USD", on_exceed="block")

        assert evaluate_budget(budget, spent=100).should_block
        assert evaluate_budget(budget, spent=101).should_block

    def test_an_alerting_budget_never_blocks(self) -> None:
        budget = Budget(monthly=100, currency="USD", on_exceed="alert")

        state = evaluate_budget(budget, spent=500)

        assert not state.should_block
        assert state.exceeded

    def test_just_under_the_limit_does_not_block(self) -> None:
        budget = Budget(monthly=100, currency="USD", on_exceed="block")

        assert not evaluate_budget(budget, spent=99.99).should_block

    def test_the_state_reports_which_thresholds_fired(self) -> None:
        budget = Budget(monthly=100, currency="USD", on_exceed="alert")

        state = evaluate_budget(budget, spent=85)

        assert state.newly_crossed == [50, 80]

    def test_thresholds_already_sent_are_not_resent(self) -> None:
        budget = Budget(monthly=100, currency="USD", on_exceed="alert")

        state = evaluate_budget(budget, spent=85, already_notified=[50, 80])

        assert state.newly_crossed == []

    def test_the_currency_comes_from_the_budget(self) -> None:
        budget = Budget(monthly=100, currency="EUR", on_exceed="alert")

        assert evaluate_budget(budget, spent=1).currency == "EUR"

    def test_percent_used_is_reported_for_the_ui(self) -> None:
        budget = Budget(monthly=200, currency="USD", on_exceed="alert")

        assert evaluate_budget(budget, spent=50).percent_used == 25
