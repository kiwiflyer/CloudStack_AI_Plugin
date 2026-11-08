"""Scoped API keys, metered usage and budgets.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JsonColumn = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "aisvc_api_key",
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        # The secret itself is never stored: only its per-key salt and
        # the HMAC of the secret under that salt.
        sa.Column("salt", sa.String(length=64), nullable=False),
        sa.Column("hashed", sa.String(length=128), nullable=False),
        sa.Column("allowed_models", JsonColumn, nullable=True),
        sa.Column("cidrs", JsonColumn, nullable=True),
        sa.Column("parameters", JsonColumn, nullable=True),
        sa.Column("rate_limit_rpm", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key_id"),
    )
    op.create_index("ix_aisvc_api_key_account", "aisvc_api_key", ["account"])
    op.create_index("ix_aisvc_api_key_domain", "aisvc_api_key", ["domain"])

    op.create_table(
        "aisvc_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("gpu_hours", sa.Float(), nullable=False),
        # Priced when written, not when read: a past month's bill must
        # not move when the catalog's price tiers change.
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_usage_account", "aisvc_usage", ["account"])
    op.create_index("ix_aisvc_usage_domain", "aisvc_usage", ["domain"])
    op.create_index("ix_aisvc_usage_model", "aisvc_usage", ["model"])
    op.create_index("ix_aisvc_usage_recorded_at", "aisvc_usage", ["recorded_at"])

    op.create_table(
        "aisvc_budget",
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("monthly", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("on_exceed", sa.String(length=16), nullable=False),
        sa.Column("notified", JsonColumn, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("account"),
    )


def downgrade() -> None:
    op.drop_table("aisvc_budget")
    op.drop_index("ix_aisvc_usage_recorded_at", table_name="aisvc_usage")
    op.drop_index("ix_aisvc_usage_model", table_name="aisvc_usage")
    op.drop_index("ix_aisvc_usage_domain", table_name="aisvc_usage")
    op.drop_index("ix_aisvc_usage_account", table_name="aisvc_usage")
    op.drop_table("aisvc_usage")
    op.drop_index("ix_aisvc_api_key_domain", table_name="aisvc_api_key")
    op.drop_index("ix_aisvc_api_key_account", table_name="aisvc_api_key")
    op.drop_table("aisvc_api_key")
