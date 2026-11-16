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
"""Uploaded TLS certificates for private endpoints.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aisvc_certificate",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("custom_domain", sa.String(length=255), nullable=False),
        sa.Column("certificate", sa.Text(), nullable=False),
        # The key is here because the proxy is a VM outside the cluster
        # and cannot read a Kubernetes Secret. It is served once, to
        # that proxy, over mTLS, and never logged or listed.
        sa.Column("private_key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_certificate_account", "aisvc_certificate", ["account"])
    op.create_index("ix_aisvc_certificate_domain", "aisvc_certificate", ["domain"])
    op.create_index("ix_aisvc_certificate_custom_domain", "aisvc_certificate", ["custom_domain"])
    op.create_index("ix_aisvc_certificate_fingerprint", "aisvc_certificate", ["fingerprint"])


def downgrade() -> None:
    op.drop_index("ix_aisvc_certificate_fingerprint", table_name="aisvc_certificate")
    op.drop_index("ix_aisvc_certificate_custom_domain", table_name="aisvc_certificate")
    op.drop_index("ix_aisvc_certificate_domain", table_name="aisvc_certificate")
    op.drop_index("ix_aisvc_certificate_account", table_name="aisvc_certificate")
    op.drop_table("aisvc_certificate")
