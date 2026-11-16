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
"""One-time bootstrap tokens for gateway proxies.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aisvc_bootstrap_token",
        sa.Column("token_id", sa.String(length=32), nullable=False),
        sa.Column("certificate_id", sa.String(length=36), nullable=False),
        sa.Column("proxy", sa.String(length=255), nullable=False),
        # The token itself is never stored: only a per-token salt and
        # the HMAC of the secret under it.
        sa.Column("salt", sa.String(length=64), nullable=False),
        sa.Column("hashed", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token_id"),
    )
    op.create_index(
        "ix_aisvc_bootstrap_token_certificate_id",
        "aisvc_bootstrap_token",
        ["certificate_id"],
    )
    op.create_index("ix_aisvc_bootstrap_token_proxy", "aisvc_bootstrap_token", ["proxy"])


def downgrade() -> None:
    op.drop_index("ix_aisvc_bootstrap_token_proxy", table_name="aisvc_bootstrap_token")
    op.drop_index("ix_aisvc_bootstrap_token_certificate_id", table_name="aisvc_bootstrap_token")
    op.drop_table("aisvc_bootstrap_token")
