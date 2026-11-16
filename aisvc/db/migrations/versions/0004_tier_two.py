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
"""Tier 2 tables: adapters, batches, evaluations, guardrails, endpoints.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JsonColumn = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "aisvc_adapter",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_model", sa.String(length=255), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uri", sa.String(length=1024), nullable=False),
        sa.Column("shared_with_domain", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_adapter_name", "aisvc_adapter", ["name"])
    op.create_index("ix_aisvc_adapter_base_model", "aisvc_adapter", ["base_model"])
    op.create_index("ix_aisvc_adapter_account", "aisvc_adapter", ["account"])
    op.create_index("ix_aisvc_adapter_domain", "aisvc_adapter", ["domain"])

    op.create_table(
        "aisvc_batch",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_uri", sa.String(length=1024), nullable=False),
        sa.Column("output_uri", sa.String(length=1024), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_batch_account", "aisvc_batch", ["account"])
    op.create_index("ix_aisvc_batch_domain", "aisvc_batch", ["domain"])
    op.create_index("ix_aisvc_batch_state", "aisvc_batch", ["state"])

    op.create_table(
        "aisvc_evaluation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("overridden", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("suites", JsonColumn, nullable=True),
        sa.Column("performance", JsonColumn, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_evaluation_model", "aisvc_evaluation", ["model"])
    op.create_index("ix_aisvc_evaluation_revision", "aisvc_evaluation", ["revision"])

    op.create_table(
        "aisvc_guardrail_profile",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("checks", JsonColumn, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_guardrail_profile_name", "aisvc_guardrail_profile", ["name"])
    op.create_index("ix_aisvc_guardrail_profile_target", "aisvc_guardrail_profile", ["target"])

    op.create_table(
        "aisvc_private_endpoint",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("deployment", sa.String(length=255), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("network_id", sa.String(length=36), nullable=False),
        sa.Column("public_enabled", sa.Boolean(), nullable=False),
        sa.Column("custom_domain", sa.String(length=255), nullable=False),
        sa.Column("tls", sa.String(length=16), nullable=False),
        sa.Column("certificate_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_aisvc_private_endpoint_deployment", "aisvc_private_endpoint", ["deployment"]
    )
    op.create_index("ix_aisvc_private_endpoint_account", "aisvc_private_endpoint", ["account"])
    op.create_index("ix_aisvc_private_endpoint_domain", "aisvc_private_endpoint", ["domain"])


def downgrade() -> None:
    op.drop_index("ix_aisvc_private_endpoint_domain", table_name="aisvc_private_endpoint")
    op.drop_index("ix_aisvc_private_endpoint_account", table_name="aisvc_private_endpoint")
    op.drop_index("ix_aisvc_private_endpoint_deployment", table_name="aisvc_private_endpoint")
    op.drop_table("aisvc_private_endpoint")
    op.drop_index("ix_aisvc_guardrail_profile_target", table_name="aisvc_guardrail_profile")
    op.drop_index("ix_aisvc_guardrail_profile_name", table_name="aisvc_guardrail_profile")
    op.drop_table("aisvc_guardrail_profile")
    op.drop_index("ix_aisvc_evaluation_revision", table_name="aisvc_evaluation")
    op.drop_index("ix_aisvc_evaluation_model", table_name="aisvc_evaluation")
    op.drop_table("aisvc_evaluation")
    op.drop_index("ix_aisvc_batch_state", table_name="aisvc_batch")
    op.drop_index("ix_aisvc_batch_domain", table_name="aisvc_batch")
    op.drop_index("ix_aisvc_batch_account", table_name="aisvc_batch")
    op.drop_table("aisvc_batch")
    op.drop_index("ix_aisvc_adapter_domain", table_name="aisvc_adapter")
    op.drop_index("ix_aisvc_adapter_account", table_name="aisvc_adapter")
    op.drop_index("ix_aisvc_adapter_base_model", table_name="aisvc_adapter")
    op.drop_index("ix_aisvc_adapter_name", table_name="aisvc_adapter")
    op.drop_table("aisvc_adapter")
