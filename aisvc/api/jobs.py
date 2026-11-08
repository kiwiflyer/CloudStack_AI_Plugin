"""Async jobs (SPEC.md section 10).

State-changing commands return 202 with a job id, and the plugin polls
`/v1/jobs/{id}` until it leaves the pending state. The three states
mirror CloudStack's own async jobs so the plugin can map them straight
onto a CloudStack async job without translation.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from aisvc.api.context import CallerContext
from aisvc.db.models import Base, JsonColumn, utcnow

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["JobRecord", "JobState", "JobStore"]


class JobState(enum.Enum):
    """Mirrors CloudStack async job status values."""

    PENDING = 0
    SUCCESS = 1
    FAILURE = 2


class JobRecord(Base):
    """One async job, from 202 to its final state."""

    __tablename__ = "aisvc_job"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command: Mapped[str] = mapped_column(String(63), index=True)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="aisvc_job_state", native_enum=False),
        default=JobState.PENDING,
    )

    user_id: Mapped[str] = mapped_column(String(63))
    account: Mapped[str] = mapped_column(String(63), index=True)
    domain_path: Mapped[str] = mapped_column(String(255), index=True)

    result: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def as_response(self) -> dict[str, Any]:
        """The body `/v1/jobs/{id}` returns, in CloudStack's shape."""
        body: dict[str, Any] = {"jobid": self.id, "jobstatus": self.state.value}
        if self.state is JobState.SUCCESS:
            body["jobresult"] = self.result or {}
        elif self.state is JobState.FAILURE:
            body["jobresult"] = {
                "errortext": self.error_text or "",
                "errorcode": self.error_code,
            }
        return body


class JobStore:
    """Creates, finishes and looks up async jobs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, command: str, caller: CallerContext) -> JobRecord:
        job = JobRecord(
            id=str(uuid.uuid4()),
            command=command,
            state=JobState.PENDING,
            user_id=caller.user_id,
            account=caller.account,
            domain_path=caller.domain_path,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def succeed(self, job: JobRecord, result: dict[str, Any] | None = None) -> None:
        job.state = JobState.SUCCESS
        job.result = result or {}
        job.finished_at = utcnow()
        self.session.flush()

    def fail(self, job: JobRecord, error: str, *, error_code: int | None = None) -> None:
        job.state = JobState.FAILURE
        job.error_text = error
        job.error_code = error_code
        job.finished_at = utcnow()
        self.session.flush()

    def get(self, job_id: str, caller: CallerContext) -> JobRecord | None:
        """A job, if this caller may see it.

        A job they may not see is indistinguishable from one that does
        not exist, so job ids cannot be probed.
        """
        job = self.session.get(JobRecord, job_id)
        if job is None:
            return None
        if not caller.may_see(account=job.account, domain_path=job.domain_path):
            return None
        return job

    def recent(self, caller: CallerContext, *, limit: int = 50) -> Sequence[JobRecord]:
        statement = select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit)
        return [
            job
            for job in self.session.scalars(statement)
            if caller.may_see(account=job.account, domain_path=job.domain_path)
        ]
