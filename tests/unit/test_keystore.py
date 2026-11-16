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
"""Persisted scoped keys and usage (SPEC.md sections 4 and 7).

CLAUDE.md: "Postgres holds runs, actions, usage, budgets, profiles,
hashed keys". Exercised on in-memory SQLite; the schema is
dialect-neutral and Alembic targets Postgres.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aisvc.db import Base, KeyStore, UsageStore
from aisvc.gateway import KeyRejected, KeySecret
from aisvc.metering import UsageMeter, UsageRecord
from aisvc.observability.alerts import Alert, CollectingSink
from aisvc.spec.models import Budget, PriceTier

STANDARD = PriceTier(name="standard", input_per_mtok=0.60, output_per_mtok=1.80, currency="USD")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        yield s


@pytest.fixture
def keys(session: Session) -> KeyStore:
    return KeyStore(session)


@pytest.fixture
def usage(session: Session) -> UsageStore:
    return UsageStore(session)


def issue(keys: KeyStore, **kw: object) -> KeySecret:
    defaults: dict[str, object] = {
        "account": "acme",
        "domain": "/Customers/Acme",
        "name": "ci",
    }
    defaults.update(kw)
    return keys.issue(**defaults)  # type: ignore[arg-type]


def _record_and_price(seen: list[str]) -> Any:
    """Remember which model the tier was looked up for."""

    def tier_for(model: str) -> Any:
        seen.append(model)
        return STANDARD

    return tier_for


class TestIssuing:
    def test_an_issued_key_verifies(self, keys: KeyStore) -> None:
        minted = issue(keys)

        resolved = keys.verify(minted.plaintext, model="any", source_ip="10.0.0.1")

        assert resolved.account == "acme"

    def test_the_plaintext_is_not_stored(self, keys: KeyStore, session: Session) -> None:
        """Shown once: nothing in the database can reproduce it."""
        from aisvc.db import ApiKeyRecord

        minted = issue(keys)
        session.flush()
        rows = session.query(ApiKeyRecord).all()

        assert rows
        assert all(minted.secret not in str(vars(row)) for row in rows)

    def test_a_tampered_secret_is_refused(self, keys: KeyStore) -> None:
        minted = issue(keys)
        forged = f"aisvc-{minted.key_id}-wrong"

        with pytest.raises(KeyRejected) as caught:
            keys.verify(forged, model="any", source_ip="10.0.0.1")

        assert caught.value.reason == "invalid_key"

    def test_an_unknown_key_id_is_refused_the_same_way(self, keys: KeyStore) -> None:
        """A different reason here would say which ids exist."""
        with pytest.raises(KeyRejected) as caught:
            keys.verify("aisvc-deadbeef-secret", model="any", source_ip="10.0.0.1")

        assert caught.value.reason == "invalid_key"


class TestScopeEnforcement:
    def test_a_model_outside_the_scope_is_refused(self, keys: KeyStore) -> None:
        minted = issue(keys, allowed_models=("llama",))

        with pytest.raises(KeyRejected) as caught:
            keys.verify(minted.plaintext, model="gemma", source_ip="10.0.0.1")

        assert caught.value.reason == "model_not_allowed"

    def test_an_ip_outside_the_cidr_is_refused(self, keys: KeyStore) -> None:
        minted = issue(keys, cidrs=("10.0.0.0/8",))

        with pytest.raises(KeyRejected) as caught:
            keys.verify(minted.plaintext, model="any", source_ip="192.168.1.1")

        assert caught.value.reason == "ip_not_allowed"

    def test_an_expired_key_is_refused(self, keys: KeyStore) -> None:
        minted = issue(keys, expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1))
        keys.session.flush()
        record = keys.record_for(minted.key_id)
        record.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)

        with pytest.raises(KeyRejected) as caught:
            keys.verify(minted.plaintext, model="any", source_ip="10.0.0.1")

        assert caught.value.reason == "key_expired"

    def test_the_refusal_says_which_scope_failed(self, keys: KeyStore) -> None:
        """SPEC.md section 7: 403 "with a clear reason"."""
        minted = issue(keys, allowed_models=("llama",))

        with pytest.raises(KeyRejected) as caught:
            keys.verify(minted.plaintext, model="gemma", source_ip="10.0.0.1")

        assert "gemma" in caught.value.detail


class TestLifecycle:
    def test_last_used_is_recorded(self, keys: KeyStore) -> None:
        minted = issue(keys)

        keys.verify(minted.plaintext, model="any", source_ip="10.0.0.1")

        assert keys.record_for(minted.key_id).last_used_at is not None

    def test_an_unused_key_has_no_last_used_time(self, keys: KeyStore) -> None:
        minted = issue(keys)

        assert keys.record_for(minted.key_id).last_used_at is None

    def test_a_revoked_key_stops_working(self, keys: KeyStore) -> None:
        minted = issue(keys)
        keys.revoke(minted.key_id)

        with pytest.raises(KeyRejected) as caught:
            keys.verify(minted.plaintext, model="any", source_ip="10.0.0.1")

        assert caught.value.reason == "key_revoked"

    def test_revoking_twice_is_harmless(self, keys: KeyStore) -> None:
        minted = issue(keys)
        keys.revoke(minted.key_id)
        keys.revoke(minted.key_id)

        assert keys.record_for(minted.key_id).revoked_at is not None

    def test_rotation_overlaps_two_working_keys(self, keys: KeyStore) -> None:
        """SPEC.md section 7: "rotation by overlapping keys"."""
        old = issue(keys, name="old")
        new = issue(keys, name="new")

        assert keys.verify(old.plaintext, model="m", source_ip="10.0.0.1")
        assert keys.verify(new.plaintext, model="m", source_ip="10.0.0.1")

    def test_revoking_the_old_key_leaves_the_new_one(self, keys: KeyStore) -> None:
        old = issue(keys, name="old")
        new = issue(keys, name="new")
        keys.revoke(old.key_id)

        assert keys.verify(new.plaintext, model="m", source_ip="10.0.0.1")

    def test_listing_shows_metadata_but_never_a_secret(self, keys: KeyStore) -> None:
        minted = issue(keys, name="ci")

        listed = keys.list_for(account="acme", domain="/Customers/Acme")

        assert [k.name for k in listed] == ["ci"]
        assert all(minted.secret not in str(vars(k)) for k in listed)

    def test_listing_is_scoped_to_one_account(self, keys: KeyStore) -> None:
        issue(keys, account="acme")
        issue(keys, account="other")

        assert len(keys.list_for(account="acme", domain="/Customers/Acme")) == 1


class TestUsagePersistence:
    def record(self, **kw: object) -> UsageRecord:
        defaults: dict[str, object] = {
            "account": "acme",
            "domain": "/Customers/Acme",
            "model": "llama",
            "input_tokens": 1_000_000,
            "output_tokens": 0,
        }
        defaults.update(kw)
        return UsageRecord(**defaults)  # type: ignore[arg-type]

    def test_a_request_is_recorded_and_priced(self, usage: UsageStore) -> None:
        usage.record(self.record(), tier=STANDARD)

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(0.60)

    def test_usage_accumulates_across_requests(self, usage: UsageStore) -> None:
        usage.record(self.record(), tier=STANDARD)
        usage.record(self.record(), tier=STANDARD)

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(1.20)

    def test_another_accounts_usage_is_not_counted(self, usage: UsageStore) -> None:
        usage.record(self.record(), tier=STANDARD)
        usage.record(self.record(account="other"), tier=STANDARD)

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(0.60)

    def test_zero_usage_spends_nothing(self, usage: UsageStore) -> None:
        assert usage.spent(account="acme", domain="/Customers/Acme") == 0.0

    def test_usage_is_listed_per_model(self, usage: UsageStore) -> None:
        usage.record(self.record(model="llama"), tier=STANDARD)
        usage.record(self.record(model="gemma"), tier=STANDARD)

        assert {r.model for r in usage.list_for(account="acme", domain="/Customers/Acme")} == {
            "llama",
            "gemma",
        }

    def test_last_months_usage_is_not_counted_this_month(self, usage: UsageStore) -> None:
        """A monthly budget that never resets would block forever."""
        usage.record(self.record(), tier=STANDARD)
        row = usage.list_for(account="acme", domain="/Customers/Acme")[0]
        row.recorded_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=60)

        assert usage.spent(account="acme", domain="/Customers/Acme") == 0.0


class TestBudgets:
    def test_a_budget_under_its_limit_does_not_block(self, usage: UsageStore) -> None:
        usage.set_budget("acme", "/d", Budget(monthly=100, currency="USD", on_exceed="block"))

        state = usage.budget_state("acme", "/d")
        assert state is not None
        assert not state.should_block

    def test_an_account_with_no_budget_is_unlimited(self, usage: UsageStore) -> None:
        assert usage.budget_state("acme", "/d") is None

    def test_spending_past_a_blocking_budget_blocks(self, usage: UsageStore) -> None:
        usage.set_budget("acme", "/d", Budget(monthly=1, currency="USD", on_exceed="block"))
        usage.record(
            UsageRecord(
                account="acme",
                domain="/d",
                model="m",
                input_tokens=5_000_000,
                output_tokens=0,
            ),
            tier=STANDARD,
        )

        state = usage.budget_state("acme", "/d")
        assert state is not None
        assert state.should_block

    def test_an_alerting_budget_never_blocks(self, usage: UsageStore) -> None:
        usage.set_budget("acme", "/d", Budget(monthly=1, currency="USD", on_exceed="alert"))
        usage.record(
            UsageRecord(
                account="acme",
                domain="/d",
                model="m",
                input_tokens=5_000_000,
                output_tokens=0,
            ),
            tier=STANDARD,
        )

        state = usage.budget_state("acme", "/d")
        assert state is not None
        assert not state.should_block
        assert state.exceeded

    def test_a_threshold_is_notified_once(self, usage: UsageStore) -> None:
        """An alert per request at 80% would be unusable."""
        usage.set_budget("acme", "/d", Budget(monthly=1, currency="USD", on_exceed="alert"))
        usage.record(
            UsageRecord(
                account="acme",
                domain="/d",
                model="m",
                input_tokens=1_000_000,
                output_tokens=0,
            ),
            tier=STANDARD,
        )

        first = usage.take_notifications("acme", "/d")
        second = usage.take_notifications("acme", "/d")

        assert first == [50]
        assert second == []

    def test_updating_a_budget_replaces_it(self, usage: UsageStore) -> None:
        usage.set_budget("acme", "/d", Budget(monthly=100, currency="USD", on_exceed="alert"))
        usage.set_budget("acme", "/d", Budget(monthly=200, currency="EUR", on_exceed="block"))

        state = usage.budget_state("acme", "/d")
        assert state is not None
        assert state.currency == "EUR"


class TestMeteringRaisesBudgetAlerts:
    """Recording usage is what notices a threshold being crossed.

    Nothing else runs often enough: the reconciler is minutes apart and
    a tenant can cross 100% in seconds.
    """

    def meter(self, usage: UsageStore) -> tuple[UsageMeter, CollectingSink]:
        sink = CollectingSink()
        return UsageMeter(usage, tier_for=lambda _model: STANDARD, sink=sink), sink

    def spend(self, mtok: int = 1) -> UsageRecord:
        return UsageRecord(
            account="acme",
            domain="/Customers/Acme",
            model="llama",
            input_tokens=1_000_000 * mtok,
        )

    async def test_usage_is_recorded(self, usage: UsageStore) -> None:
        meter, _ = self.meter(usage)

        await meter(self.spend())

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(0.60)

    async def test_no_budget_means_no_alert(self, usage: UsageStore) -> None:
        meter, sink = self.meter(usage)

        await meter(self.spend())

        assert sink.alerts == []

    async def test_crossing_a_threshold_alerts(self, usage: UsageStore) -> None:
        usage.set_budget(
            "acme", "/Customers/Acme", Budget(monthly=1, currency="USD", on_exceed="alert")
        )
        meter, sink = self.meter(usage)

        await meter(self.spend())

        assert [a.detail["threshold"] for a in sink.alerts] == [50]

    async def test_a_threshold_alerts_only_once(self, usage: UsageStore) -> None:
        usage.set_budget(
            "acme", "/Customers/Acme", Budget(monthly=10, currency="USD", on_exceed="alert")
        )
        meter, sink = self.meter(usage)

        await meter(self.spend(mtok=9))
        await meter(self.spend(mtok=1))

        thresholds = [a.detail["threshold"] for a in sink.alerts]
        assert thresholds == sorted(set(thresholds))

    async def test_staying_under_alerts_nothing(self, usage: UsageStore) -> None:
        usage.set_budget(
            "acme", "/Customers/Acme", Budget(monthly=1000, currency="USD", on_exceed="alert")
        )
        meter, sink = self.meter(usage)

        await meter(self.spend())

        assert sink.alerts == []

    async def test_a_failing_sink_does_not_lose_the_usage(self, usage: UsageStore) -> None:
        """Billing matters more than the alert: the row must be written."""

        class Broken:
            async def send(self, alert: Alert) -> None:
                raise RuntimeError("the webhook exploded")

        usage.set_budget(
            "acme", "/Customers/Acme", Budget(monthly=1, currency="USD", on_exceed="alert")
        )
        meter = UsageMeter(usage, tier_for=lambda _m: STANDARD, sink=Broken())

        await meter(self.spend())

        assert usage.spent(account="acme", domain="/Customers/Acme") > 0

    async def test_a_model_with_no_tier_is_still_recorded(self, usage: UsageStore) -> None:
        """An uncatalogued model must not drop usage on the floor."""
        meter = UsageMeter(usage, tier_for=lambda _m: None, sink=CollectingSink())

        await meter(self.spend())

        rows = usage.list_for(account="acme", domain="/Customers/Acme")
        assert len(rows) == 1
        assert rows[0].cost == 0.0


class TestMeteringV2Shapes:
    """Billing has to know about adapters and task types.

    Both were decided in v2 modules that nothing on the billing path
    called, so an adapter request was billed at the base rate and an
    embedding model could be billed for output tokens it cannot
    produce.
    """

    def meter(self, usage: UsageStore, **kw: Any) -> tuple[UsageMeter, CollectingSink]:
        sink = CollectingSink()
        defaults: dict[str, Any] = {
            "tier_for": lambda _m: STANDARD,
            "sink": sink,
        }
        defaults.update(kw)
        return UsageMeter(usage, **defaults), sink

    def spend(self, model: str = "llama", **kw: Any) -> UsageRecord:
        defaults: dict[str, Any] = {
            "account": "acme",
            "domain": "/Customers/Acme",
            "model": model,
            "input_tokens": 1_000_000,
            "output_tokens": 0,
        }
        defaults.update(kw)
        return UsageRecord(**defaults)

    async def test_a_plain_model_is_billed_at_its_tier(self, usage: UsageStore) -> None:
        meter, _ = self.meter(usage)

        await meter(self.spend())

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(0.60)

    async def test_an_adapter_adds_its_premium(self, usage: UsageStore) -> None:
        """SPEC.md section 8: base price plus a configurable premium."""
        meter, _ = self.meter(usage, adapter_premium=0.25)

        await meter(self.spend(model="llama:legal-tone"))

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(0.75)

    async def test_no_premium_configured_bills_the_base_rate(self, usage: UsageStore) -> None:
        meter, _ = self.meter(usage)

        await meter(self.spend(model="llama:legal-tone"))

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(0.60)

    async def test_the_premium_does_not_touch_the_bare_base(self, usage: UsageStore) -> None:
        meter, _ = self.meter(usage, adapter_premium=0.25)

        await meter(self.spend(model="llama"))

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(0.60)

    async def test_the_adapter_is_priced_on_its_bases_tier(self, usage: UsageStore) -> None:
        """The tier is looked up for the base, not for `base:adapter`,
        which the catalog has never heard of."""
        asked: list[str] = []
        meter, _ = self.meter(
            usage,
            tier_for=_record_and_price(asked),
            adapter_premium=0.25,
        )

        await meter(self.spend(model="llama:legal-tone"))

        assert asked == ["llama"]

    async def test_an_embedding_billed_for_output_is_refused(self, usage: UsageStore) -> None:
        """A non-generating task reporting output tokens means
        something upstream metered it as chat; billing it would
        overcharge."""
        meter, _ = self.meter(usage, task_for=lambda _m: "embedding")

        with pytest.raises(ValueError, match="embedding"):
            await meter(self.spend(output_tokens=50))

    async def test_an_embedding_with_input_alone_is_billed(self, usage: UsageStore) -> None:
        meter, _ = self.meter(usage, task_for=lambda _m: "embedding")

        await meter(self.spend())

        assert usage.spent(account="acme", domain="/Customers/Acme") == pytest.approx(0.60)

    async def test_without_a_task_lookup_nothing_is_checked(self, usage: UsageStore) -> None:
        meter, _ = self.meter(usage)

        await meter(self.spend(output_tokens=50))

        assert usage.spent(account="acme", domain="/Customers/Acme") > 0


class TestQueriesAreScopedToADomain:
    """A CloudStack account name is unique only within its domain.

    `may_see` is the filter these feed, but a query that returns
    another domain's rows has already leaked them — the filter is the
    second line, not the first. The domain is required rather than
    optional here on purpose: a default of "any domain" is the defect.
    """

    def test_keys_from_another_domain_are_not_listed(self, keys: KeyStore) -> None:
        issue(keys, account="dev", domain="/Customers/AcmeA", name="theirs")
        issue(keys, account="dev", domain="/Customers/AcmeB", name="mine")

        listed = keys.list_for(account="dev", domain="/Customers/AcmeB")

        assert [r.name for r in listed] == ["mine"]

    def test_their_own_keys_are_still_listed(self, keys: KeyStore) -> None:
        issue(keys, account="dev", domain="/Customers/AcmeA", name="one")
        issue(keys, account="dev", domain="/Customers/AcmeA", name="two")

        listed = keys.list_for(account="dev", domain="/Customers/AcmeA")

        assert {r.name for r in listed} == {"one", "two"}

    def test_usage_from_another_domain_is_not_counted(self, usage: UsageStore) -> None:
        for domain in ("/Customers/AcmeA", "/Customers/AcmeB"):
            usage.record(
                UsageRecord(
                    account="dev",
                    domain=domain,
                    model="llama-3-8b",
                    input_tokens=1_000_000,
                    output_tokens=0,
                ),
                tier=STANDARD,
            )

        assert usage.spent(account="dev", domain="/Customers/AcmeA") == pytest.approx(0.60)

    def test_usage_rows_are_scoped_too(self, usage: UsageStore) -> None:
        for domain in ("/Customers/AcmeA", "/Customers/AcmeB"):
            usage.record(
                UsageRecord(
                    account="dev", domain=domain, model="m", input_tokens=10, output_tokens=0
                ),
                tier=STANDARD,
            )

        rows = usage.list_for(account="dev", domain="/Customers/AcmeB")

        assert [r.domain for r in rows] == ["/Customers/AcmeB"]

    def test_a_trailing_slash_still_matches(self, keys: KeyStore) -> None:
        # Domain paths reach the engine from several sources; refusing
        # on a trailing slash would hide a tenant's own rows.
        issue(keys, account="dev", domain="/Customers/AcmeA", name="mine")

        assert keys.list_for(account="dev", domain="/Customers/AcmeA/")


class TestABudgetBelongsToAnAccountInADomain:
    """Budgets were keyed on the account name alone.

    CloudStack account names are unique only within a domain, so `dev`
    in one domain and `dev` in another shared one limit: setting one
    overwrote the other, and the gateway then blocked or failed to
    block the wrong tenant. The siblings — adapters, certificates —
    already carry a surrogate id with (account, domain) beside it.
    """

    def budget(self, monthly: float = 100.0) -> Budget:
        return Budget(monthly=monthly, currency="USD", on_exceed="alert")

    def test_two_domains_keep_separate_budgets(self, usage: UsageStore) -> None:
        usage.set_budget("dev", "/Customers/AcmeA", self.budget(100.0))
        usage.set_budget("dev", "/Customers/AcmeB", self.budget(5.0))

        state = usage.budget_state("dev", "/Customers/AcmeA")

        assert state is not None
        assert state.limit == 100.0

    def test_setting_one_does_not_overwrite_the_other(self, usage: UsageStore) -> None:
        usage.set_budget("dev", "/Customers/AcmeA", self.budget(100.0))
        usage.set_budget("dev", "/Customers/AcmeB", self.budget(5.0))

        other = usage.budget_state("dev", "/Customers/AcmeB")

        assert other is not None
        assert other.limit == 5.0

    def test_setting_it_twice_replaces_rather_than_duplicates(self, usage: UsageStore) -> None:
        first = usage.set_budget("dev", "/Customers/AcmeA", self.budget(100.0))
        second = usage.set_budget("dev", "/Customers/AcmeA", self.budget(50.0))

        assert first.id == second.id
        assert second.monthly == 50.0

    def test_a_budget_carries_an_id_of_its_own(self, usage: UsageStore) -> None:
        # The account name cannot identify it, so the route needs
        # something that can.
        record = usage.set_budget("dev", "/Customers/AcmeA", self.budget())

        assert record.id
        assert record.domain == "/Customers/AcmeA"

    def test_an_unknown_pair_has_no_budget(self, usage: UsageStore) -> None:
        usage.set_budget("dev", "/Customers/AcmeA", self.budget())

        assert usage.budget_state("dev", "/Customers/AcmeB") is None

    def test_spend_is_counted_within_the_domain(self, usage: UsageStore) -> None:
        usage.set_budget("dev", "/Customers/AcmeA", self.budget())
        for domain in ("/Customers/AcmeA", "/Customers/AcmeB"):
            usage.record(
                UsageRecord(
                    account="dev",
                    domain=domain,
                    model="m",
                    input_tokens=1_000_000,
                    output_tokens=0,
                ),
                tier=STANDARD,
            )

        state = usage.budget_state("dev", "/Customers/AcmeA")

        assert state is not None
        assert state.spent == pytest.approx(0.60)
