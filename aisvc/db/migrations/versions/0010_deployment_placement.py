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
"""Where a `placement: api` model runs (ADR-0008).

Placement and nothing else. A row names a model that `platform.yaml`
already catalogues, the zone it runs in, how many replicas are wanted
and which serving profile to use. It carries no source, no licence, no
bound and no engine argument, because those are what the spec is for
and a database nobody reviews is the wrong place for a supply-chain
rule.

The zone is stored although a model's node pool already determines it.
The command sends one, so it is checked against the pool's zone and
refused on a mismatch rather than quietly ignored — and keeping it on
the row means a response can say where the deployment is without
resolving the pool again.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aisvc_deployment_placement",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("zone", sa.String(length=255), nullable=False),
        sa.Column("replicas", sa.Integer(), nullable=False),
        sa.Column("serving_profile", sa.String(length=255), nullable=False),
        # Who asked for it. Every role may call `deployAiModel`, so a
        # list has to be filtered by caller like any other
        # tenant-visible row, and an account name is unique only
        # within its domain.
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # One placement per model per zone. A second would make the
        # replica count depend on which row the planner found first.
        sa.UniqueConstraint("model", "zone", name="uq_aisvc_placement_model_zone"),
    )
    op.create_index("ix_aisvc_placement_model", "aisvc_deployment_placement", ["model"])
    op.create_index("ix_aisvc_placement_zone", "aisvc_deployment_placement", ["zone"])


def downgrade() -> None:
    op.drop_table("aisvc_deployment_placement")
