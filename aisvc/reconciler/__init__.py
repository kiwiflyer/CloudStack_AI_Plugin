"""Converging desired state onto actual state (SPEC.md section 4).

`observe() -> diff() -> plan() -> execute()`, ordered: capacity, models,
profiles, routes. Every run is recorded; a failed action stops only its
dependants.
"""

from __future__ import annotations

from aisvc.reconciler.executor import Executor, ExecutorError
from aisvc.reconciler.plan import Action, ActionKind, Plan, plan_capacity

__all__ = [
    "Action",
    "ActionKind",
    "Executor",
    "ExecutorError",
    "Plan",
    "plan_capacity",
]
