"""Converging desired state onto actual state (SPEC.md section 4).

`observe() -> diff() -> plan() -> execute()`, ordered: capacity, models,
profiles, routes. Every run is recorded; a failed action stops only its
dependants.
"""

from __future__ import annotations

from aisvc.reconciler.executor import Executor, ExecutorError
from aisvc.reconciler.plan import (
    ACTIVE_COLOUR,
    Action,
    ActionKind,
    Plan,
    plan_all,
    plan_capacity,
    plan_models,
    plan_routes,
)
from aisvc.reconciler.state import ActualState

__all__ = [
    "ACTIVE_COLOUR",
    "Action",
    "ActionKind",
    "ActualState",
    "Executor",
    "ExecutorError",
    "Plan",
    "plan_all",
    "plan_capacity",
    "plan_models",
    "plan_routes",
]
