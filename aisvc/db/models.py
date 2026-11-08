"""Run log schema (SPEC.md section 4).

Every reconcile run, its plan and every action's outcome are recorded so
the audit trail and the UI activity view have a source. Nothing is kept
only in memory.

The schema is dialect-neutral so unit tests can run on SQLite; Alembic
targets Postgres, where JSON columns become JSONB.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "ActionOutcome",
    "ActionRecord",
    "ApiKeyRecord",
    "Base",
    "BudgetRecord",
    "RunRecord",
    "RunStatus",
    "UsageRow",
    "utcnow",
]

#: JSON on SQLite, JSONB on Postgres.
JsonColumn = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RunStatus(enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ActionOutcome(enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunRecord(Base):
    """One reconcile run: the plan it started from and how it ended."""

    __tablename__ = "aisvc_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone: Mapped[str] = mapped_column(String(63), index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="aisvc_run_status", native_enum=False),
        default=RunStatus.RUNNING,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: The plan exactly as it was printed, so the audit trail shows what
    #: the operator saw.
    plan_text: Mapped[str] = mapped_column(Text, default="")
    warnings: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    notes: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    actions: Mapped[list[ActionRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ActionRecord.position",
        lazy="selectin",
    )

    __table_args__ = (Index("ix_aisvc_run_zone_started", "zone", "started_at"),)


class ActionRecord(Base):
    """One planned action and what became of it."""

    __tablename__ = "aisvc_run_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("aisvc_run.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    kind: Mapped[str] = mapped_column(String(16))
    resource_type: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(255))
    count: Mapped[int] = mapped_column(Integer, default=1)
    detail: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")

    outcome: Mapped[ActionOutcome] = mapped_column(
        Enum(ActionOutcome, name="aisvc_action_outcome", native_enum=False),
        default=ActionOutcome.PENDING,
    )
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[RunRecord] = relationship(back_populates="actions")


class ApiKeyRecord(Base):
    """A scoped tenant API key (SPEC.md section 7).

    The secret is not here and cannot be derived from what is: only the
    per-key salt and the hash of the secret are stored, which is what
    "shown once" means in practice.
    """

    __tablename__ = "aisvc_api_key"

    key_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255))
    salt: Mapped[str] = mapped_column(String(64))
    hashed: Mapped[str] = mapped_column(String(128))
    allowed_models: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    cidrs: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    parameters: Mapped[dict[str, object]] = mapped_column(JsonColumn, default=dict)
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: Null until the key is first used, which the UI shows as "never".
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageRow(Base):
    """One metered interval of one tenant's use of one model.

    Rows accumulate rather than being updated in place: a budget asks
    what was spent this month, and overwriting a running total would
    lose the per-model breakdown the usage screen needs.
    """

    __tablename__ = "aisvc_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    model: Mapped[str] = mapped_column(String(255), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    gpu_hours: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class BudgetRecord(Base):
    """A monthly budget for one account (SPEC.md section 7).

    `notified` holds the thresholds already announced, so an alert fires
    once rather than on every request past 80%.
    """

    __tablename__ = "aisvc_budget"

    account: Mapped[str] = mapped_column(String(255), primary_key=True)
    monthly: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    on_exceed: Mapped[str] = mapped_column(String(16), default="alert")
    notified: Mapped[list[int]] = mapped_column(JsonColumn, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
