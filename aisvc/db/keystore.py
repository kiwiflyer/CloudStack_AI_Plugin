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
"""Stored scoped keys and metered usage (SPEC.md sections 4 and 7).

Two stores over the tables in `aisvc.db.models`. They are deliberately
thin: the policy lives in `aisvc.gateway` (what a key may do) and
`aisvc.metering` (what usage costs and what a budget means), and these
only decide what is written down and what is read back.

Both are safe to call from the gateway hot path: verification is one
indexed primary-key lookup and one HMAC.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from aisvc.db.models import ApiKeyRecord, BudgetRecord, UsageRow, utcnow
from aisvc.gateway import (
    ApiKey,
    KeyRejected,
    hash_secret,
    key_allows,
    mint_key,
    parse_presented_key,
    verify_secret,
)
from aisvc.metering import (
    DEFAULT_THRESHOLDS,
    BudgetState,
    crossed_thresholds,
    evaluate_budget,
    price_usage,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from aisvc.gateway import KeySecret
    from aisvc.metering import UsageRecord
    from aisvc.spec.models import Budget, PriceTier

__all__ = ["KeyStore", "UsageStore"]


def _domain_variants(domain: str) -> tuple[str, str]:
    """Both spellings of a domain path, so a trailing slash still matches.

    Paths reach the engine from several sources and differ by a
    trailing slash; refusing on that alone would hide a tenant's own
    rows from them.
    """
    bare = domain.rstrip("/")
    return (bare, f"{bare}/")


class KeyStore:
    """Issue, verify and revoke tenant API keys."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def issue(
        self,
        *,
        account: str,
        domain: str,
        name: str,
        allowed_models: Sequence[str] = (),
        cidrs: Sequence[str] = (),
        expires_at: datetime | None = None,
        rate_limit_rpm: int | None = None,
        account_quota_rpm: int | None = None,
        parameters: dict[str, object] | None = None,
    ) -> KeySecret:
        """Mint a key and store everything about it but the secret."""
        minted = mint_key(
            account=account,
            domain=domain,
            name=name,
            allowed_models=allowed_models,
            cidrs=cidrs,
            expires_at=expires_at,
            rate_limit_rpm=rate_limit_rpm,
            account_quota_rpm=account_quota_rpm,
            parameters=parameters,
        )
        self.session.add(
            ApiKeyRecord(
                key_id=minted.key_id,
                account=account,
                domain=domain,
                name=name,
                salt=minted.salt,
                hashed=minted.hashed,
                allowed_models=list(allowed_models),
                cidrs=list(cidrs),
                parameters=dict(parameters or {}),
                rate_limit_rpm=rate_limit_rpm,
                expires_at=expires_at,
            )
        )
        self.session.flush()
        return minted

    def record_for(self, key_id: str) -> ApiKeyRecord:
        record = self.session.get(ApiKeyRecord, key_id)
        if record is None:
            raise KeyError(key_id)
        return record

    def verify(self, presented: str, *, model: str, source_ip: str) -> ApiKey:
        """Resolve a presented key, or refuse it with the reason.

        An unknown key id and a wrong secret refuse identically: telling
        them apart would turn this into an oracle for which ids exist.
        """
        key_id, secret = parse_presented_key(presented)
        record = self.session.get(ApiKeyRecord, key_id)

        if record is None or not verify_secret(secret, salt=record.salt, hashed=record.hashed):
            raise KeyRejected("invalid_key", "the presented API key is not valid")

        if record.revoked_at is not None:
            raise KeyRejected("key_revoked", f"key {key_id} was revoked")

        resolved = self._as_api_key(record)
        key_allows(resolved, model=model, source_ip=source_ip)

        record.last_used_at = utcnow()
        return resolved

    def revoke(self, key_id: str) -> None:
        """Stop a key working. Revoking an already-revoked key is a no-op."""
        record = self.record_for(key_id)
        if record.revoked_at is None:
            record.revoked_at = utcnow()

    def list_for(self, *, account: str, domain: str) -> list[ApiKeyRecord]:
        """Every key an account holds in one domain. Never carries a secret.

        **The domain is required.** A CloudStack account name is unique
        only within its domain, so a query on the name alone returns
        another tenant's keys — and `may_see` is the second line of
        defence, not the first. A default of "any domain" would be the
        defect rather than a convenience.
        """
        return list(
            self.session.scalars(
                select(ApiKeyRecord)
                .where(ApiKeyRecord.account == account)
                .where(ApiKeyRecord.domain.in_(_domain_variants(domain)))
                .order_by(ApiKeyRecord.created_at)
            )
        )

    @staticmethod
    def _as_api_key(record: ApiKeyRecord) -> ApiKey:
        return ApiKey(
            key_id=record.key_id,
            account=record.account,
            domain=record.domain,
            name=record.name,
            allowed_models=tuple(record.allowed_models or ()),
            cidrs=tuple(record.cidrs or ()),
            expires_at=_aware(record.expires_at),
            rate_limit_rpm=record.rate_limit_rpm,
            parameters=dict(record.parameters or {}),
        )

    @staticmethod
    def hash_for(secret: str, *, salt: str) -> str:
        return hash_secret(secret, salt=salt)


class UsageStore:
    """Record what tenants used, and what their budgets say about it."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, usage: UsageRecord, *, tier: PriceTier) -> UsageRow:
        """Price one interval of usage and write it down.

        Priced at write time rather than read time: the catalog's tiers
        change, and a past month's bill must not change with them.
        """
        row = UsageRow(
            account=usage.account,
            domain=usage.domain,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            gpu_hours=usage.gpu_hours,
            cost=price_usage(usage, tier),
            currency=tier.currency,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list_for(
        self, *, account: str, domain: str, since: datetime | None = None
    ) -> list[UsageRow]:
        """Usage rows for one account in one domain.

        The domain is required for the same reason as `KeyStore.list_for`:
        account names collide across domains, and an unscoped query
        bills — or discloses — the wrong tenant.
        """
        query = (
            select(UsageRow)
            .where(UsageRow.account == account)
            .where(UsageRow.domain.in_(_domain_variants(domain)))
        )
        if since is not None:
            query = query.where(UsageRow.recorded_at >= since)
        return list(self.session.scalars(query.order_by(UsageRow.recorded_at)))

    def spent(self, *, account: str, domain: str, since: datetime | None = None) -> float:
        """What an account has spent in the current billing period.

        The window matters: a monthly budget that never resets would
        block a tenant permanently after one expensive month.
        """
        return self._spent(self.list_for(account=account, domain=domain), since=since)

    @staticmethod
    def _spent(rows: list[UsageRow], *, since: datetime | None) -> float:
        window = since if since is not None else _month_start()
        total = 0.0
        for row in rows:
            recorded = _aware(row.recorded_at)
            # recorded_at is not nullable, but a row read back mid-flush
            # can be: count an unstamped row as current rather than
            # comparing against None and failing the budget check.
            if recorded is None or recorded >= window:
                total += row.cost
        return total

    def budget_for(self, account: str, domain: str) -> BudgetRecord | None:
        """The budget for one account in one domain, or None."""
        return self.session.scalars(
            select(BudgetRecord)
            .where(BudgetRecord.account == account)
            .where(BudgetRecord.domain.in_(_domain_variants(domain)))
        ).first()

    def set_budget(self, account: str, domain: str, budget: Budget) -> BudgetRecord:
        """Set or replace a budget, resetting its alerts.

        Keyed on the (account, domain) pair, not the account name:
        names are unique only within a domain, and keying on the name
        alone made two tenants share one limit.
        """
        record = self.budget_for(account, domain)
        if record is None:
            # `or "/"`: rstrip would turn the root domain's only
            # character into nothing, and a caller who asked for "/"
            # was told the budget belonged to "". Lookups match both
            # spellings through _domain_variants either way.
            record = BudgetRecord(
                id=str(uuid.uuid4()),
                account=account,
                domain=domain.rstrip("/") or "/",
            )
            self.session.add(record)
        record.monthly = budget.monthly
        record.currency = budget.currency
        record.on_exceed = budget.on_exceed
        record.notified = []
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def budget_state(self, account: str, domain: str) -> BudgetState | None:
        """How this account stands against its budget, or None if unlimited.

        Spend is counted within `domain`. The budget record itself is
        still keyed on the account name alone, so two accounts
        sharing a name across domains share a limit — a collision
        that needs a schema migration and a change to the published
        budget identifier to close.
        """
        record = self.budget_for(account, domain)
        if record is None:
            return None

        from aisvc.spec.models import Budget

        return evaluate_budget(
            Budget(
                monthly=record.monthly,
                currency=record.currency,
                on_exceed=record.on_exceed,
            ),
            spent=self.spent(account=account, domain=domain),
            already_notified=list(record.notified or ()),
        )

    def take_notifications(self, account: str, domain: str) -> list[int]:
        """Thresholds crossed since the last call, and never repeated.

        Taking rather than reading is deliberate: the caller sends the
        alerts, and a crash between reading and sending should lose an
        alert rather than send it on every request thereafter.
        """
        record = self.budget_for(account, domain)
        if record is None:
            return []

        already = list(record.notified or ())
        newly = crossed_thresholds(
            spent=self.spent(account=account, domain=domain),
            limit=record.monthly,
            already_notified=already,
            thresholds=DEFAULT_THRESHOLDS,
        )
        if newly:
            record.notified = sorted(already + newly)
        return newly


def _month_start(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; Postgres does not."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
