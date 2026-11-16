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
"""Async job table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JsonColumn = sa.JSON().with_variant(JSONB(), "postgresql")

JOB_STATE = sa.Enum("PENDING", "SUCCESS", "FAILURE", name="aisvc_job_state", native_enum=False)


def upgrade() -> None:
    op.create_table(
        "aisvc_job",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("command", sa.String(length=63), nullable=False),
        sa.Column("state", JOB_STATE, nullable=False),
        sa.Column("user_id", sa.String(length=63), nullable=False),
        sa.Column("account", sa.String(length=63), nullable=False),
        sa.Column("domain_path", sa.String(length=255), nullable=False),
        sa.Column("result", JsonColumn, nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_job_command", "aisvc_job", ["command"])
    op.create_index("ix_aisvc_job_account", "aisvc_job", ["account"])
    op.create_index("ix_aisvc_job_domain_path", "aisvc_job", ["domain_path"])


def downgrade() -> None:
    op.drop_index("ix_aisvc_job_domain_path", table_name="aisvc_job")
    op.drop_index("ix_aisvc_job_account", table_name="aisvc_job")
    op.drop_index("ix_aisvc_job_command", table_name="aisvc_job")
    op.drop_table("aisvc_job")
