"""Writing and reading the run log (SPEC.md section 4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from aisvc.db.models import (
    ActionOutcome,
    ActionRecord,
    RunRecord,
    RunStatus,
    utcnow,
)

if TYPE_CHECKING:
    from aisvc.reconciler import Plan

__all__ = ["RunLog"]


class RunLog:
    """Records reconcile runs so every action has an audit trail."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def start_run(self, plan: Plan, *, zone: str, dry_run: bool) -> RunRecord:
        """Open a run and write its plan before anything executes."""
        run = RunRecord(
            zone=zone,
            dry_run=dry_run,
            status=RunStatus.RUNNING,
            plan_text=plan.render(),
            warnings=list(plan.warnings),
            notes=list(plan.notes),
            actions=[
                ActionRecord(
                    position=index,
                    kind=action.kind.value,
                    resource_type=action.resource_type,
                    name=action.name,
                    count=action.count,
                    detail=action.detail,
                    reason=action.reason,
                    outcome=ActionOutcome.PENDING,
                )
                for index, action in enumerate(plan.actions)
            ],
        )
        self.session.add(run)
        self.session.flush()
        return run

    def action_started(self, action: ActionRecord) -> None:
        action.started_at = utcnow()
        self.session.flush()

    def action_succeeded(self, action: ActionRecord, *, detail: str = "") -> None:
        action.outcome = ActionOutcome.SUCCEEDED
        action.result = detail or None
        action.finished_at = utcnow()
        self.session.flush()

    def action_failed(self, action: ActionRecord, *, error: str) -> None:
        action.outcome = ActionOutcome.FAILED
        action.error = error
        action.finished_at = utcnow()
        self.session.flush()

    def action_skipped(self, action: ActionRecord, *, reason: str) -> None:
        action.outcome = ActionOutcome.SKIPPED
        action.error = reason
        action.finished_at = utcnow()
        self.session.flush()

    def finish_run(self, run: RunRecord, *, error: str | None = None) -> None:
        """Close a run; any failed action makes the whole run a failure."""
        failed = any(a.outcome is ActionOutcome.FAILED for a in run.actions)
        run.status = RunStatus.FAILED if (failed or error) else RunStatus.SUCCEEDED
        run.error = error
        run.finished_at = utcnow()
        self.session.flush()

    def recent_runs(self, *, zone: str | None = None, limit: int = 50) -> list[RunRecord]:
        """Runs newest first, for the activity view."""
        statement = select(RunRecord).order_by(RunRecord.id.desc()).limit(limit)
        if zone is not None:
            statement = statement.filter(RunRecord.zone == zone)
        return list(self.session.scalars(statement))
