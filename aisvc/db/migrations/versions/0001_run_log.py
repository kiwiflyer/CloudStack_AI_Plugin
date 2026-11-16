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
"""Run log: aisvc_run and aisvc_run_action.

Revision ID: 0001
Revises:
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JsonColumn = sa.JSON().with_variant(JSONB(), "postgresql")

RUN_STATUS = sa.Enum(
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    name="aisvc_run_status",
    native_enum=False,
)
ACTION_OUTCOME = sa.Enum(
    "PENDING",
    "SUCCEEDED",
    "FAILED",
    "SKIPPED",
    name="aisvc_action_outcome",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "aisvc_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("zone", sa.String(length=63), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("status", RUN_STATUS, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_text", sa.Text(), nullable=False),
        sa.Column("warnings", JsonColumn, nullable=False),
        sa.Column("notes", JsonColumn, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_run_zone", "aisvc_run", ["zone"])
    op.create_index("ix_aisvc_run_zone_started", "aisvc_run", ["zone", "started_at"])

    op.create_table(
        "aisvc_run_action",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("resource_type", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("outcome", ACTION_OUTCOME, nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["aisvc_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_run_action_run_id", "aisvc_run_action", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_aisvc_run_action_run_id", table_name="aisvc_run_action")
    op.drop_table("aisvc_run_action")
    op.drop_index("ix_aisvc_run_zone_started", table_name="aisvc_run")
    op.drop_index("ix_aisvc_run_zone", table_name="aisvc_run")
    op.drop_table("aisvc_run")
