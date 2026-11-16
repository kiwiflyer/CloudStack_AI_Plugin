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
"""A budget belongs to an account in a domain, not to a name.

CloudStack account names are unique only within a domain, so keying
`aisvc_budget` on the account alone made `dev` in one domain share a
limit with `dev` in another: setting one overwrote the other, and the
gateway then blocked, or failed to block, the wrong tenant.

The table is rebuilt rather than altered in place. It gains a
surrogate id — what the API identifies a budget by, since the account
name cannot — and a domain beside the account, unique together. Rows
that exist keep their limits and are attributed to the root domain,
which is the only domain their account name can be resolved to without
guessing; an operator with tenants in several domains should check them
afterwards.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Where a pre-existing budget is attributed. It cannot be derived:
#: the old table recorded no domain at all.
UNKNOWN_DOMAIN = "/"


def _existing() -> list[dict[str, object]]:
    """Rows to carry across, or nothing when rendering SQL offline.

    `alembic upgrade --sql` renders against a mock connection, so a
    data-copying migration says what it would do structurally and
    leaves the rows to the online run.
    """
    found = op.get_bind().execute(
        sa.text(
            "SELECT account, monthly, currency, on_exceed, notified, updated_at FROM aisvc_budget"
        )
    )
    if found is None:
        # `alembic upgrade --sql` renders against a mock connection
        # that returns nothing, so there are no rows to carry and the
        # structural change is all this can emit.
        return []
    return [dict(row._mapping) for row in found]


def upgrade() -> None:
    existing = _existing()

    op.drop_table("aisvc_budget")
    op.create_table(
        "aisvc_budget",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account", sa.String(255), nullable=False, index=True),
        sa.Column("domain", sa.String(255), nullable=False, index=True),
        sa.Column("monthly", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("on_exceed", sa.String(16), nullable=False, server_default="alert"),
        sa.Column("notified", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("account", "domain", name="uq_aisvc_budget_owner"),
    )

    for row in existing:
        op.get_bind().execute(
            sa.text(
                "INSERT INTO aisvc_budget "
                "(id, account, domain, monthly, currency, on_exceed, notified, updated_at) "
                "VALUES (:id, :account, :domain, :monthly, :currency, :on_exceed, "
                ":notified, :updated_at)"
            ),
            {**row, "id": str(uuid.uuid4()), "domain": UNKNOWN_DOMAIN},
        )


def downgrade() -> None:
    # Going back loses the domain, so two budgets that differ only by
    # it would collide on the account key. The first one wins, which is
    # the same ambiguity the old schema always had.
    existing = _existing()

    op.drop_table("aisvc_budget")
    op.create_table(
        "aisvc_budget",
        sa.Column("account", sa.String(255), primary_key=True),
        sa.Column("monthly", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("on_exceed", sa.String(16), nullable=False, server_default="alert"),
        sa.Column("notified", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    seen: set[str] = set()
    for row in existing:
        account = str(row["account"])
        if account in seen:
            continue
        seen.add(account)
        op.get_bind().execute(
            sa.text(
                "INSERT INTO aisvc_budget "
                "(account, monthly, currency, on_exceed, notified, updated_at) "
                "VALUES (:account, :monthly, :currency, :on_exceed, :notified, :updated_at)"
            ),
            row,
        )
