"""Typed diff between desired and actual capacity (SPEC.md section 4)."""

from __future__ import annotations

from datetime import UTC, datetime

import yaml

from aisvc.inventory import CapacitySnapshot, ClassCapacity
from aisvc.reconciler import Action, ActionKind, Plan, plan_capacity
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL


def spec_with(**pool_changes: object) -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    doc["nodePools"][0].update(pool_changes)
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


def snapshot_with(nodes_total: int, nodes_allocated: int = 0) -> CapacitySnapshot:
    return CapacitySnapshot(
        taken_at=datetime.now(UTC),
        classes={
            ("zone-east-1", "h100-8x"): ClassCapacity(
                zone="zone-east-1",
                gpu_class="h100-8x",
                gpus_per_node=8,
                nodes_total=nodes_total,
                nodes_healthy=nodes_total,
                nodes_allocated=nodes_allocated,
            )
        },
    )


class TestPlanCapacity:
    def test_pool_below_its_minimum_plans_node_creation(self) -> None:
        plan = plan_capacity(spec_with(min_nodes=3), snapshot_with(nodes_total=1))

        creates = [a for a in plan.actions if a.kind is ActionKind.CREATE]
        assert len(creates) == 1
        assert creates[0].count == 2
        assert creates[0].resource_type == "gpu-node"
        assert "h100-pool" in creates[0].name

    def test_pool_at_its_minimum_plans_nothing(self) -> None:
        plan = plan_capacity(spec_with(min_nodes=2), snapshot_with(nodes_total=2))
        assert plan.actions == []
        assert not plan.has_changes

    def test_pool_between_min_and_max_plans_nothing(self) -> None:
        plan = plan_capacity(spec_with(min_nodes=1, max_nodes=6), snapshot_with(nodes_total=4))
        assert not plan.has_changes

    def test_pool_above_its_maximum_plans_node_removal(self) -> None:
        plan = plan_capacity(spec_with(min_nodes=1, max_nodes=2), snapshot_with(nodes_total=5))

        deletes = [a for a in plan.actions if a.kind is ActionKind.DELETE]
        assert len(deletes) == 1
        assert deletes[0].count == 3

    def test_unobserved_class_is_reported_as_a_problem_not_a_create(self) -> None:
        spec = spec_with(min_nodes=1)
        empty = CapacitySnapshot(taken_at=datetime.now(UTC), classes={})
        plan = plan_capacity(spec, empty)

        assert any("not observed" in w for w in plan.warnings)
        assert not plan.has_changes

    def test_plan_carries_the_spec_notes(self) -> None:
        spec = spec_with(min_nodes=2)
        plan = plan_capacity(spec, snapshot_with(nodes_total=2))
        assert any("max_num_seqs" in n for n in plan.notes)

    def test_second_plan_against_the_resulting_state_is_empty(self) -> None:
        """The reconciler must converge: SPEC.md section 12."""
        spec = spec_with(min_nodes=3)
        first = plan_capacity(spec, snapshot_with(nodes_total=1))
        applied = sum(a.count for a in first.actions if a.kind is ActionKind.CREATE)

        second = plan_capacity(spec, snapshot_with(nodes_total=1 + applied))
        assert not second.has_changes


class TestPlanRendering:
    def test_empty_plan_says_so(self) -> None:
        plan = Plan(actions=[], warnings=[])
        assert "No changes" in plan.render()

    def test_rendered_plan_lists_every_action(self) -> None:
        plan = Plan(
            actions=[
                Action(
                    kind=ActionKind.CREATE,
                    resource_type="gpu-node",
                    name="h100-pool",
                    count=2,
                    detail="service offering GPU-H100-8x",
                ),
                Action(
                    kind=ActionKind.DELETE,
                    resource_type="gpu-node",
                    name="l40s-pool",
                    count=1,
                ),
            ],
            warnings=["something to know"],
        )
        text = plan.render()

        assert "+ gpu-node h100-pool x2" in text
        assert "- gpu-node l40s-pool x1" in text
        assert "service offering GPU-H100-8x" in text
        assert "something to know" in text
        assert "2 to add, 1 to remove" in text

    def test_summary_counts_actions_not_resources(self) -> None:
        plan = Plan(
            actions=[
                Action(ActionKind.CREATE, "gpu-node", "a", count=3),
                Action(ActionKind.CREATE, "gpu-node", "b", count=1),
            ],
            warnings=[],
        )
        assert "4 to add, 0 to remove" in plan.render()
