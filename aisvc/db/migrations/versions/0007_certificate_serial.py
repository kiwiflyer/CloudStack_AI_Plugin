"""Certificate renewal: serial and notified thresholds.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JsonColumn = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # Existing rows are serial 1: whatever a proxy fetched is current.
    op.add_column(
        "aisvc_certificate",
        sa.Column("serial", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("aisvc_certificate", sa.Column("notified", JsonColumn, nullable=True))
    # Zero for existing rows: no proxy was built from a serial, so
    # nothing is treated as superseded on the first run after upgrade.
    op.add_column(
        "aisvc_private_endpoint",
        sa.Column("proxy_certificate_serial", sa.Integer(), nullable=False, server_default="0"),
    )
    # ACME certificates have no record of their own, so an endpoint's
    # countdown is remembered against its exposure.
    op.add_column("aisvc_private_endpoint", sa.Column("acme_notified", JsonColumn, nullable=True))


def downgrade() -> None:
    op.drop_column("aisvc_private_endpoint", "acme_notified")
    op.drop_column("aisvc_private_endpoint", "proxy_certificate_serial")
    op.drop_column("aisvc_certificate", "notified")
    op.drop_column("aisvc_certificate", "serial")
