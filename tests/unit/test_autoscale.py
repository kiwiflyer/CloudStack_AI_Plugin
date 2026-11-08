"""Replica and node scaling (SPEC.md section 4).

"Prometheus: vllm:num_requests_waiting, TTFT p95, DCGM utilization; up
in 1 min, down after 15 min."
"""

from __future__ import annotations

import pytest

from aisvc.autoscale import (
    SCALE_DOWN_AFTER_SECONDS,
    SCALE_UP_AFTER_SECONDS,
    Metrics,
    ScaleDecision,
    decide,
)


def metrics(**kw: object) -> Metrics:
    defaults: dict[str, object] = {
        "queue_depth": 0.0,
        "ttft_p95_ms": 200.0,
        "gpu_utilization": 0.4,
    }
    defaults.update(kw)
    return Metrics(**defaults)  # type: ignore[arg-type]


def slo(**kw: object) -> dict[str, object]:
    defaults: dict[str, object] = {"ttft_p95_ms": 800, "queue_depth_target": 8}
    defaults.update(kw)
    return defaults


class TestScaleUp:
    def test_a_deep_queue_scales_up(self) -> None:
        decision = decide(
            metrics(queue_depth=20.0),
            slo=slo(),
            replicas=2,
            minimum=2,
            maximum=6,
            breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
        )

        assert decision.replicas == 3
        assert "queue" in decision.reason

    def test_breaching_ttft_scales_up(self) -> None:
        decision = decide(
            metrics(ttft_p95_ms=1500.0),
            slo=slo(),
            replicas=2,
            minimum=2,
            maximum=6,
            breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
        )

        assert decision.replicas == 3
        assert "ttft" in decision.reason.lower()

    def test_scaling_up_waits_a_minute(self) -> None:
        """A one-off spike should not add a GPU node."""
        decision = decide(
            metrics(queue_depth=20.0),
            slo=slo(),
            replicas=2,
            minimum=2,
            maximum=6,
            breaching_for_seconds=10,
        )

        assert decision.replicas == 2
        assert not decision.changed

    def test_scaling_up_stops_at_the_maximum(self) -> None:
        decision = decide(
            metrics(queue_depth=99.0),
            slo=slo(),
            replicas=4,
            minimum=2,
            maximum=4,
            breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
        )

        assert decision.replicas == 4
        assert "maximum" in decision.reason

    def test_scale_up_adds_one_at_a_time(self) -> None:
        """Adding several at once outruns the three-minute cold start."""
        decision = decide(
            metrics(queue_depth=500.0),
            slo=slo(),
            replicas=2,
            minimum=1,
            maximum=10,
            breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
        )

        assert decision.replicas == 3


class TestScaleDown:
    def test_a_quiet_deployment_scales_down(self) -> None:
        decision = decide(
            metrics(queue_depth=0.0, gpu_utilization=0.05),
            slo=slo(),
            replicas=4,
            minimum=2,
            maximum=6,
            idle_for_seconds=SCALE_DOWN_AFTER_SECONDS,
        )

        assert decision.replicas == 3

    def test_scaling_down_waits_fifteen_minutes(self) -> None:
        """Weights take minutes to load; flapping is expensive."""
        decision = decide(
            metrics(queue_depth=0.0, gpu_utilization=0.05),
            slo=slo(),
            replicas=4,
            minimum=2,
            maximum=6,
            idle_for_seconds=60,
        )

        assert decision.replicas == 4
        assert not decision.changed

    def test_scaling_down_stops_at_the_minimum(self) -> None:
        decision = decide(
            metrics(queue_depth=0.0, gpu_utilization=0.0),
            slo=slo(),
            replicas=2,
            minimum=2,
            maximum=6,
            idle_for_seconds=SCALE_DOWN_AFTER_SECONDS,
        )

        assert decision.replicas == 2
        assert "minimum" in decision.reason

    def test_a_busy_deployment_does_not_scale_down(self) -> None:
        decision = decide(
            metrics(queue_depth=4.0, gpu_utilization=0.7),
            slo=slo(),
            replicas=4,
            minimum=2,
            maximum=6,
            idle_for_seconds=SCALE_DOWN_AFTER_SECONDS,
        )

        assert decision.replicas == 4

    def test_scale_down_removes_one_at_a_time(self) -> None:
        decision = decide(
            metrics(queue_depth=0.0, gpu_utilization=0.0),
            slo=slo(),
            replicas=8,
            minimum=1,
            maximum=10,
            idle_for_seconds=SCALE_DOWN_AFTER_SECONDS,
        )

        assert decision.replicas == 7


class TestPrecedence:
    def test_scaling_up_beats_scaling_down(self) -> None:
        """A breach matters more than idleness, whatever the timers say."""
        decision = decide(
            metrics(queue_depth=50.0, gpu_utilization=0.0),
            slo=slo(),
            replicas=4,
            minimum=2,
            maximum=6,
            breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
            idle_for_seconds=SCALE_DOWN_AFTER_SECONDS,
        )

        assert decision.replicas == 5

    def test_a_steady_deployment_is_left_alone(self) -> None:
        decision = decide(
            metrics(queue_depth=4.0, ttft_p95_ms=400.0, gpu_utilization=0.6),
            slo=slo(),
            replicas=3,
            minimum=2,
            maximum=6,
        )

        assert not decision.changed
        assert decision.replicas == 3


class TestDecision:
    def test_a_decision_says_what_it_did_and_why(self) -> None:
        decision = decide(
            metrics(queue_depth=20.0),
            slo=slo(),
            replicas=2,
            minimum=2,
            maximum=6,
            breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
        )

        assert isinstance(decision, ScaleDecision)
        assert decision.changed
        assert decision.previous == 2
        assert decision.reason

    def test_replicas_never_leave_the_declared_band(self) -> None:
        for replicas in (0, 1, 5, 20):
            for depth in (0.0, 100.0):
                decision = decide(
                    metrics(queue_depth=depth),
                    slo=slo(),
                    replicas=replicas,
                    minimum=2,
                    maximum=6,
                    breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
                    idle_for_seconds=SCALE_DOWN_AFTER_SECONDS,
                )
                assert 2 <= decision.replicas <= 6

    def test_an_slo_without_targets_falls_back_to_defaults(self) -> None:
        decision = decide(
            metrics(queue_depth=50.0),
            slo={},
            replicas=2,
            minimum=1,
            maximum=4,
            breaching_for_seconds=SCALE_UP_AFTER_SECONDS,
        )

        assert decision.changed

    def test_a_maximum_below_the_minimum_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="maximum"):
            decide(metrics(), slo=slo(), replicas=2, minimum=4, maximum=2)
