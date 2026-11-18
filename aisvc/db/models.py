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
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "ActionOutcome",
    "ActionRecord",
    "AdapterRecord",
    "ApiKeyRecord",
    "Base",
    "BatchRecord",
    "BootstrapTokenRecord",
    "BudgetRecord",
    "CertificateRecord",
    "EvaluationRecord",
    "GuardrailProfileRecord",
    "PrivateEndpointRecord",
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
    """A monthly budget for one account in one domain (SPEC.md section 7).

    Owned by an (account, domain) pair, not an account name: CloudStack
    account names are unique only within a domain, so keying on the
    name alone made `dev` in one domain share a limit with `dev` in
    another — one overwrote the other, and the gateway then blocked or
    failed to block the wrong tenant.

    The surrogate id is what the API identifies a budget by, since the
    account name cannot. Adapters and certificates are shaped the same
    way.

    `notified` holds the thresholds already announced, so an alert fires
    once rather than on every request past 80%.
    """

    __tablename__ = "aisvc_budget"
    __table_args__ = (UniqueConstraint("account", "domain", name="uq_aisvc_budget_owner"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    monthly: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    on_exceed: Mapped[str] = mapped_column(String(16), default="alert")
    notified: Mapped[list[int]] = mapped_column(JsonColumn, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdapterRecord(Base):
    """A tenant's LoRA adapter (SPEC.md section 8).

    Owned by an (account, domain) pair, not an account name: CloudStack
    account names are unique only within a domain.
    """

    __tablename__ = "aisvc_adapter"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    base_model: Mapped[str] = mapped_column(String(255), index=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    uri: Mapped[str] = mapped_column(String(1024))
    shared_with_domain: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BatchRecord(Base):
    """A submitted batch job (SPEC.md section 8)."""

    __tablename__ = "aisvc_batch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    model: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16), default="validating", index=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    input_uri: Mapped[str] = mapped_column(String(1024), default="")
    output_uri: Mapped[str] = mapped_column(String(1024), default="")
    error_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationRecord(Base):
    """One pre-publish evaluation of a model revision (SPEC.md section 8)."""

    __tablename__ = "aisvc_evaluation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model: Mapped[str] = mapped_column(String(255), index=True)
    revision: Mapped[str] = mapped_column(String(64), index=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    suites: Mapped[list[dict[str, object]]] = mapped_column(JsonColumn, default=list)
    performance: Mapped[list[dict[str, object]]] = mapped_column(JsonColumn, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ServingProfileRecord(Base):
    """Engine arguments for a deployment (SPEC.md section 6).

    Platform-level and root admin's alone, because these are vLLM's own
    arguments: a tenant able to set `gpu_memory_utilization` could take
    a host down for everyone sharing it. There is no scope or target
    column for that reason — a serving profile is bound by an
    assignment, never by itself.
    """

    __tablename__ = "aisvc_serving_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    params: Mapped[dict[str, object]] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GenerationProfileRecord(Base):
    """Tunable generation parameters and their policies (SPEC.md section 6).

    Carries its author's account and domain because every role may
    create one, so a list has to be filtered by caller like any other
    tenant-owned row. The precedence *level* a profile binds at is not
    here: that is the assignment's business, and the same profile may
    be bound at more than one.
    """

    __tablename__ = "aisvc_generation_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    params: Mapped[dict[str, object]] = mapped_column(JsonColumn, default=dict)
    account: Mapped[str] = mapped_column(String(255), default="", index=True)
    domain: Mapped[str] = mapped_column(String(255), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProfileAssignmentRecord(Base):
    """Which profile binds at which scope (SPEC.md section 6).

    The same shape as `platform.yaml`'s `assignments`, deliberately:
    resolution merges the two, and a row here that could not be written
    in the spec would be a precedence chain no operator could read.
    """

    __tablename__ = "aisvc_profile_assignment"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), index=True)
    target: Mapped[str] = mapped_column(String(255), default="", index=True)
    serving_profile: Mapped[str] = mapped_column(String(255), default="")
    generation_profile: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GuardrailProfileRecord(Base):
    """A guardrail profile and where it is bound (SPEC.md sections 6, 8)."""

    __tablename__ = "aisvc_guardrail_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    scope: Mapped[str] = mapped_column(String(32), default="account")
    target: Mapped[str] = mapped_column(String(255), default="", index=True)
    checks: Mapped[dict[str, object]] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PrivateEndpointRecord(Base):
    """How one deployment is exposed (SPEC.md section 8)."""

    __tablename__ = "aisvc_private_endpoint"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    deployment: Mapped[str] = mapped_column(String(255), index=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    network_id: Mapped[str] = mapped_column(String(36), default="")
    public_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    custom_domain: Mapped[str] = mapped_column(String(255), default="")
    tls: Mapped[str] = mapped_column(String(16), default="none")
    certificate_id: Mapped[str] = mapped_column(String(36), default="")
    #: Which certificate serial this exposure's proxy was built with.
    #: A proxy fetched once at boot, so a difference from the
    #: certificate's current serial means it is serving superseded
    #: material and has to be replaced.
    proxy_certificate_serial: Mapped[int] = mapped_column(Integer, default=0)
    #: Renewal thresholds already announced for what this endpoint is
    #: observed to be serving. ACME certificates have no record of
    #: their own, so the countdown is remembered here.
    acme_notified: Mapped[list[int]] = mapped_column(JsonColumn, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CertificateRecord(Base):
    """An uploaded TLS certificate and its key (SPEC.md section 8).

    The key is here because the proxy is a VM outside the cluster and
    cannot read a Kubernetes Secret. It leaves exactly once, to that
    proxy, over mTLS; nothing else returns it, and it is never logged.
    """

    __tablename__ = "aisvc_certificate"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    custom_domain: Mapped[str] = mapped_column(String(255), index=True)
    certificate: Mapped[str] = mapped_column(Text)
    private_key: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Bumped on every replacement. A proxy fetched its material once
    #: at boot, so this is how the planner tells one is serving
    #: superseded material and needs redeploying.
    serial: Mapped[int] = mapped_column(Integer, default=1)
    #: Renewal thresholds already announced, so each fires once.
    notified: Mapped[list[int]] = mapped_column(JsonColumn, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BootstrapTokenRecord(Base):
    """A one-time token letting a proxy fetch its TLS material.

    Stored as a salted hash, like an API key: a database read yields
    nothing usable. Short-lived and single-use, because the plaintext
    travels in cloud-init userdata, which is readable through the
    CloudStack API.
    """

    __tablename__ = "aisvc_bootstrap_token"

    token_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    certificate_id: Mapped[str] = mapped_column(String(36), index=True)
    #: The proxy it was minted for, so a redemption can be traced back.
    proxy: Mapped[str] = mapped_column(String(255), index=True)
    salt: Mapped[str] = mapped_column(String(64))
    hashed: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
