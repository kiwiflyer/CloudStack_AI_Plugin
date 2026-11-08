"""Blue/green rollouts and draining (SPEC.md sections 4, 7 and 11).

[ADR-0001](../../docs/adr/0001-serving-layer.md) kept canary weighting
out of the serving layer, so the rollout state machine lives here and
the gateway carries the weights.
"""

from __future__ import annotations

import pytest

from aisvc.lifecycle import (
    DrainPlan,
    Rollout,
    RolloutError,
    RolloutPhase,
    plan_drain,
    start_rollout,
)


def rollout(**kw: object) -> Rollout:
    defaults: dict[str, object] = {
        "model": "llama-3.3-70b-instruct",
        "active": "blue",
        "candidate": "green",
        "canary_percent": 10,
        "phase": RolloutPhase.CANARY,
    }
    defaults.update(kw)
    return Rollout(**defaults)  # type: ignore[arg-type]


class TestStartRollout:
    def test_a_rollout_targets_the_other_colour(self) -> None:
        started = start_rollout("llama", active="blue", canary_percent=10)

        assert started.active == "blue"
        assert started.candidate == "green"

    def test_rolling_out_over_green_targets_blue(self) -> None:
        started = start_rollout("llama", active="green", canary_percent=10)

        assert started.candidate == "blue"

    def test_a_new_rollout_starts_in_canary(self) -> None:
        assert start_rollout("llama", active="blue").phase is RolloutPhase.CANARY

    def test_the_canary_percent_is_carried(self) -> None:
        assert start_rollout("llama", active="blue", canary_percent=25).canary_percent == 25

    def test_a_canary_percent_outside_0_to_100_is_rejected(self) -> None:
        with pytest.raises(RolloutError, match="canary"):
            start_rollout("llama", active="blue", canary_percent=140)

    def test_an_unknown_active_colour_is_rejected(self) -> None:
        with pytest.raises(RolloutError, match="purple"):
            start_rollout("llama", active="purple")


class TestWeights:
    def test_canary_splits_traffic_by_the_canary_percent(self) -> None:
        weights = rollout(canary_percent=10).weights()

        assert weights == {"blue": 90, "green": 10}

    def test_a_zero_percent_canary_sends_nothing_to_the_candidate(self) -> None:
        """Lets a candidate warm up before it takes any traffic."""
        assert rollout(canary_percent=0).weights() == {"blue": 100, "green": 0}

    def test_promoting_sends_everything_to_the_candidate(self) -> None:
        weights = rollout(phase=RolloutPhase.PROMOTED).weights()

        assert weights == {"blue": 0, "green": 100}

    def test_rolling_back_sends_everything_to_the_active_colour(self) -> None:
        weights = rollout(phase=RolloutPhase.ROLLED_BACK).weights()

        assert weights == {"blue": 100, "green": 0}

    def test_weights_always_sum_to_one_hundred(self) -> None:
        for percent in (0, 1, 10, 50, 99, 100):
            weights = rollout(canary_percent=percent).weights()
            assert sum(weights.values()) == 100


class TestTransitions:
    def test_promote_moves_to_promoted(self) -> None:
        promoted = rollout().promote()

        assert promoted.phase is RolloutPhase.PROMOTED

    def test_promote_makes_the_candidate_live(self) -> None:
        promoted = rollout().promote()

        assert promoted.live_colour == "green"
        # The pair keeps its meaning: active is what was live before.
        assert promoted.active == "blue"
        assert promoted.candidate == "green"

    def test_during_canary_the_active_colour_is_still_live(self) -> None:
        assert rollout().live_colour == "blue"

    def test_after_rollback_the_active_colour_is_still_live(self) -> None:
        assert rollout().rollback().live_colour == "blue"

    def test_rollback_moves_to_rolled_back(self) -> None:
        assert rollout().rollback().phase is RolloutPhase.ROLLED_BACK

    def test_rollback_leaves_the_active_colour_alone(self) -> None:
        rolled = rollout().rollback()

        assert rolled.active == "blue"

    def test_promoting_twice_is_refused(self) -> None:
        with pytest.raises(RolloutError, match="already"):
            rollout().promote().promote()

    def test_rolling_back_a_promoted_rollout_is_refused(self) -> None:
        """Once traffic has moved, going back is a new rollout."""
        with pytest.raises(RolloutError, match="promoted"):
            rollout().promote().rollback()

    def test_a_rolled_back_rollout_cannot_be_promoted(self) -> None:
        with pytest.raises(RolloutError):
            rollout().rollback().promote()

    def test_transitions_do_not_mutate_the_original(self) -> None:
        original = rollout()

        original.promote()

        assert original.phase is RolloutPhase.CANARY


class TestFinished:
    def test_a_canary_is_not_finished(self) -> None:
        assert not rollout().is_finished

    @pytest.mark.parametrize("phase", [RolloutPhase.PROMOTED, RolloutPhase.ROLLED_BACK])
    def test_a_settled_rollout_is_finished(self, phase: RolloutPhase) -> None:
        assert rollout(phase=phase).is_finished

    def test_the_colour_to_tear_down_is_the_loser(self) -> None:
        assert rollout().promote().colour_to_remove == "blue"
        assert rollout().rollback().colour_to_remove == "green"

    def test_nothing_is_torn_down_mid_canary(self) -> None:
        assert rollout().colour_to_remove is None


class TestDrain:
    def test_a_drain_moves_replicas_before_removing_the_node(self) -> None:
        plan = plan_drain("aisvc-h100-pool-003", replicas_on_node=2, free_elsewhere=4)

        assert isinstance(plan, DrainPlan)
        assert plan.can_proceed
        assert plan.replicas_to_move == 2

    def test_a_drain_destroys_rather_than_stopping(self) -> None:
        """ADR-0003: gpu.detach.on.stop is false, so a stopped node holds
        its GPUs. Stopping would strand them."""
        plan = plan_drain("aisvc-h100-pool-003", replicas_on_node=1, free_elsewhere=4)

        assert plan.action == "destroy"
        assert plan.action != "stop"

    def test_too_little_capacity_elsewhere_blocks_the_drain(self) -> None:
        """SPEC.md section 7: the admin must confirm a forced drain."""
        plan = plan_drain("aisvc-h100-pool-003", replicas_on_node=3, free_elsewhere=1)

        assert not plan.can_proceed
        assert "capacity" in plan.reason

    def test_a_forced_drain_proceeds_anyway(self) -> None:
        plan = plan_drain("aisvc-h100-pool-003", replicas_on_node=3, free_elsewhere=1, force=True)

        assert plan.can_proceed
        assert plan.forced

    def test_an_empty_node_drains_immediately(self) -> None:
        plan = plan_drain("aisvc-h100-pool-003", replicas_on_node=0, free_elsewhere=0)

        assert plan.can_proceed
        assert plan.replicas_to_move == 0

    def test_a_forced_drain_says_what_it_risks(self) -> None:
        plan = plan_drain("aisvc-h100-pool-003", replicas_on_node=3, free_elsewhere=0, force=True)

        assert "3 replica" in plan.reason
