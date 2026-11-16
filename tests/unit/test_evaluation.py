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
"""The pre-publish evaluation gate (SPEC.md section 8).

A new revision or quantization is evaluated in staging before it can be
published, and a regression past the threshold blocks it. The gate's
job is to make a quantization that quietly lost two points impossible
to ship by accident — which is the failure mode it exists for, since
nobody notices a model getting slightly worse.

A root admin may override, but only with a reason, and the reason is
recorded: an override nobody can explain later is how the gate stops
meaning anything.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.evaluation import (
    DEFAULT_REGRESSION_THRESHOLD,
    EvaluationRun,
    GateBlocked,
    GateDecision,
    PerformanceResult,
    SuiteResult,
    gate,
    override_gate,
)


def suite(name: str = "mmlu", score: float = 70.0, baseline: float | None = 70.0) -> SuiteResult:
    return SuiteResult(name=name, score=score, baseline=baseline)


def run(*suites: SuiteResult, **kw: Any) -> EvaluationRun:
    defaults: dict[str, Any] = {
        "model": "llama-3.3-70b-instruct",
        "revision": "0f3f4a6c",
        "suites": suites or (suite(),),
    }
    defaults.update(kw)
    return EvaluationRun(**defaults)


class TestTheThreshold:
    def test_the_documented_default_is_two_points(self) -> None:
        assert DEFAULT_REGRESSION_THRESHOLD == 2.0

    def test_an_unchanged_score_passes(self) -> None:
        assert gate(run(suite(score=70, baseline=70))).passed

    def test_an_improvement_passes(self) -> None:
        assert gate(run(suite(score=75, baseline=70))).passed

    def test_a_small_regression_passes(self) -> None:
        """Benchmarks are noisy; blocking on noise would make the gate
        something people learn to override."""
        assert gate(run(suite(score=68.5, baseline=70))).passed

    def test_a_regression_at_the_threshold_passes(self) -> None:
        assert gate(run(suite(score=68, baseline=70))).passed

    def test_a_regression_past_the_threshold_blocks(self) -> None:
        decision = gate(run(suite(score=67.9, baseline=70)))

        assert not decision.passed

    def test_the_threshold_can_be_tightened(self) -> None:
        assert not gate(run(suite(score=69.5, baseline=70)), threshold=0.25).passed

    def test_any_one_suite_regressing_blocks_the_lot(self) -> None:
        """SPEC.md section 8: "if any suite regresses"."""
        decision = gate(run(suite("mmlu", 70, 70), suite("gsm8k", 60, 70)))

        assert not decision.passed

    def test_the_decision_names_what_regressed(self) -> None:
        decision = gate(run(suite("mmlu", 70, 70), suite("gsm8k", 60, 70)))

        assert [r.name for r in decision.regressions] == ["gsm8k"]

    def test_the_decision_says_by_how_much(self) -> None:
        decision = gate(run(suite("gsm8k", 60, 70)))

        assert decision.regressions[0].delta == pytest.approx(-10.0)


class TestNoBaseline:
    def test_a_first_evaluation_has_nothing_to_regress_against(self) -> None:
        """A brand-new model has no previous version; it cannot have
        got worse, so the gate must not invent a failure."""
        assert gate(run(suite(score=70, baseline=None))).passed

    def test_a_first_evaluation_is_recorded_as_a_baseline(self) -> None:
        decision = gate(run(suite(score=70, baseline=None)))

        assert decision.regressions == []

    def test_an_empty_evaluation_does_not_silently_pass(self) -> None:
        """Publishing because nothing ran is the worst outcome: it looks
        exactly like publishing because everything passed."""
        decision = gate(run(suites=()))

        assert not decision.passed
        assert "no suites" in decision.summary.lower()


class TestCustomerGoldenSets:
    def test_a_customer_set_counts_like_any_other_suite(self) -> None:
        decision = gate(
            run(
                suite("mmlu", 70, 70),
                SuiteResult(name="acme-golden", score=55, baseline=70, customer="acme"),
            )
        )

        assert not decision.passed

    def test_the_decision_says_whose_set_regressed(self) -> None:
        decision = gate(
            run(SuiteResult(name="acme-golden", score=55, baseline=70, customer="acme"))
        )

        assert decision.regressions[0].customer == "acme"


class TestOverride:
    def blocked(self) -> GateDecision:
        return gate(run(suite("gsm8k", 60, 70)))

    def test_a_root_admin_may_override_with_a_reason(self) -> None:
        overridden = override_gate(
            self.blocked(), role="Admin", reason="known benchmark contamination"
        )

        assert overridden.passed

    def test_the_reason_is_kept(self) -> None:
        overridden = override_gate(
            self.blocked(), role="Admin", reason="known benchmark contamination"
        )

        assert "contamination" in overridden.summary

    def test_an_override_is_marked_as_one(self) -> None:
        """A published model nobody can tell was forced through is how
        the gate stops meaning anything."""
        overridden = override_gate(self.blocked(), role="Admin", reason="r")

        assert overridden.overridden is True

    def test_an_override_without_a_reason_is_refused(self) -> None:
        with pytest.raises(GateBlocked, match="reason"):
            override_gate(self.blocked(), role="Admin", reason="  ")

    def test_a_domain_admin_may_not_override(self) -> None:
        """SPEC.md section 8: a root admin, specifically."""
        with pytest.raises(GateBlocked, match="root admin"):
            override_gate(self.blocked(), role="DomainAdmin", reason="please")

    def test_a_tenant_may_not_override(self) -> None:
        with pytest.raises(GateBlocked, match="root admin"):
            override_gate(self.blocked(), role="User", reason="please")

    def test_overriding_a_passing_gate_changes_nothing(self) -> None:
        passing = gate(run(suite(score=70, baseline=70)))

        assert override_gate(passing, role="Admin", reason="r").overridden is False


class TestPerformanceResults:
    def test_performance_is_recorded_per_gpu_class(self) -> None:
        """SPEC.md section 8: these feed `capacity`, which sizes from
        them, so they are per class or they are useless."""
        evaluated = run(
            performance=(
                PerformanceResult(gpu_class="h100-8x", ttft_p95_ms=320, tokens_per_second=1800),
                PerformanceResult(gpu_class="l40s-1x", ttft_p95_ms=900, tokens_per_second=240),
            )
        )

        decision = gate(evaluated)

        assert {p.gpu_class for p in decision.performance} == {"h100-8x", "l40s-1x"}

    def test_performance_does_not_affect_the_gate(self) -> None:
        """Being slower is a capacity question, not a correctness one."""
        evaluated = run(
            suite(score=70, baseline=70),
            performance=(
                PerformanceResult(gpu_class="h100-8x", ttft_p95_ms=9000, tokens_per_second=1),
            ),
        )

        assert gate(evaluated).passed

    def test_an_evaluation_without_performance_still_gates(self) -> None:
        assert gate(run(suite(score=70, baseline=70))).passed
