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
"""Serving profiles, generation profiles and their assignments.

Section 6's precedence logic has existed since milestone 1 and had
nowhere to read a profile from but `platform.yaml`, so the nine
commands section 9 declares for profiles answered 404: the engine had
no route, because it had no table.

Three tables rather than two, because a profile and its binding are
different things. `platform.yaml` already separates them — a profile
is a name and its parameters, an assignment says where it applies —
and the same profile may bind at more than one scope. Folding the
scope into the profile, as `aisvc_guardrail_profile` does, would make
that impossible to express and would disagree with the spec file the
resolver reads in the same breath.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JsonColumn = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "aisvc_serving_profile",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("params", JsonColumn, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_serving_profile_name", "aisvc_serving_profile", ["name"])

    op.create_table(
        "aisvc_generation_profile",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("params", JsonColumn, nullable=False),
        # Every role may create one, so a list is filtered by caller
        # like any other tenant-owned row — and an account name is
        # unique only within its domain, so both are needed.
        sa.Column("account", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_generation_profile_name", "aisvc_generation_profile", ["name"])
    op.create_index("ix_aisvc_generation_profile_account", "aisvc_generation_profile", ["account"])
    op.create_index("ix_aisvc_generation_profile_domain", "aisvc_generation_profile", ["domain"])

    op.create_table(
        "aisvc_profile_assignment",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("serving_profile", sa.String(length=255), nullable=False),
        sa.Column("generation_profile", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_aisvc_profile_assignment_scope", "aisvc_profile_assignment", ["scope"])
    op.create_index("ix_aisvc_profile_assignment_target", "aisvc_profile_assignment", ["target"])


def downgrade() -> None:
    op.drop_table("aisvc_profile_assignment")
    op.drop_table("aisvc_generation_profile")
    op.drop_table("aisvc_serving_profile")
