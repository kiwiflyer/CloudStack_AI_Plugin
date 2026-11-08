"""Certificate renewal (SPEC.md section 8).

Expiry was stored and nothing watched it, which is the failure mode
that matters: a certificate does not degrade, it stops, and the first
sign is a tenant's customers seeing a browser warning.

Two halves. Noticing — thresholds that fire once each, like budget
ones, because an alert per reconcile is an alert nobody reads. And
acting — a replacement is uploaded in place, keeping the id, so an
exposure that names it does not have to be rewritten.

A running proxy fetched its material once at first boot and has no way
to be told about a new one. Replacing a certificate therefore marks
its proxies stale and the next apply redeploys them, which is downtime.
That is the honest cost of the one-time-token design and is tested as
such rather than hidden.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from aisvc.certificates.renewal import (
    RENEWAL_THRESHOLDS,
    crossed_renewal_thresholds,
    days_until,
    renewal_alerts,
)


def expiring_in(days: float) -> dt.datetime:
    return dt.datetime.now(dt.UTC) + dt.timedelta(days=days)


class TestCountingDown:
    def test_it_counts_whole_days(self) -> None:
        assert days_until(expiring_in(30)) in (29, 30)

    def test_it_never_overstates_the_time_left(self) -> None:
        """Flooring is the safe direction: claiming a day that is not
        there is how a certificate expires over a weekend."""
        for days in (1, 7, 14, 30, 90):
            assert days_until(expiring_in(days)) <= days

    def test_an_expired_certificate_is_negative(self) -> None:
        assert days_until(expiring_in(-2)) < 0

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """SQLite hands these back without a timezone."""
        naive = (dt.datetime.now(dt.UTC) + dt.timedelta(days=10)).replace(tzinfo=None)

        assert days_until(naive) in (9, 10)


class TestThresholds:
    def test_the_documented_points(self) -> None:
        """Far enough out to act, then increasingly insistent."""
        assert RENEWAL_THRESHOLDS == (30, 14, 7, 1)

    def test_nothing_is_crossed_well_before_expiry(self) -> None:
        assert crossed_renewal_thresholds(days_left=60) == []

    def test_thirty_days_crosses_the_first(self) -> None:
        assert crossed_renewal_thresholds(days_left=30) == [30]

    def test_every_threshold_passed_is_crossed(self) -> None:
        assert crossed_renewal_thresholds(days_left=5) == [30, 14, 7]

    def test_an_expired_certificate_crosses_them_all(self) -> None:
        assert crossed_renewal_thresholds(days_left=-1) == [30, 14, 7, 1]

    def test_already_notified_points_are_not_repeated(self) -> None:
        """An alert per reconcile is an alert nobody reads."""
        assert crossed_renewal_thresholds(days_left=5, already_notified=[30, 14]) == [7]

    def test_nothing_left_to_cross_is_empty(self) -> None:
        assert crossed_renewal_thresholds(days_left=-1, already_notified=[30, 14, 7, 1]) == []


class TestAlerts:
    def record(self, **kw: Any) -> Any:
        from aisvc.db.models import CertificateRecord

        defaults: dict[str, Any] = {
            "id": "c-1",
            "account": "acme",
            "domain": "/Customers/Acme",
            "custom_domain": "ai.acme.example",
            "certificate": "",
            "private_key": "",
            "fingerprint": "ff",
            "not_after": expiring_in(10),
        }
        defaults.update(kw)
        return CertificateRecord(**defaults)

    def test_an_approaching_expiry_alerts(self) -> None:
        alerts = renewal_alerts(self.record(), already_notified=[])

        assert alerts

    def test_a_distant_expiry_does_not(self) -> None:
        assert renewal_alerts(self.record(not_after=expiring_in(200))) == []

    def test_the_alert_names_the_domain(self) -> None:
        """An operator with fifty certificates needs to know which."""
        alerts = renewal_alerts(self.record())

        assert "ai.acme.example" in alerts[0].summary

    def test_it_says_how_long_is_left(self) -> None:
        alerts = renewal_alerts(self.record(not_after=expiring_in(10.5)))

        assert "10 day" in alerts[0].summary

    def test_an_expired_one_says_so_rather_than_counting_down(self) -> None:
        alerts = renewal_alerts(self.record(not_after=expiring_in(-3)))

        assert "expired" in alerts[0].summary.lower()

    def test_the_last_warning_is_critical(self) -> None:
        from aisvc.observability.alerts import AlertSeverity

        alerts = renewal_alerts(self.record(not_after=expiring_in(0.5)))

        assert alerts[-1].severity is AlertSeverity.CRITICAL

    def test_an_early_warning_is_not(self) -> None:
        from aisvc.observability.alerts import AlertSeverity

        alerts = renewal_alerts(self.record(not_after=expiring_in(30)))

        assert alerts[0].severity is AlertSeverity.WARNING

    def test_it_carries_no_certificate_material(self) -> None:
        alerts = renewal_alerts(self.record(certificate="-----BEGIN CERTIFICATE-----"))

        assert "BEGIN CERTIFICATE" not in str(alerts[0].as_payload())

    def test_it_reaches_the_owning_tenant(self) -> None:
        """They are the ones who have to upload a replacement."""
        alerts = renewal_alerts(self.record())

        assert alerts[0].account == "acme"


class TestTheSweep:
    """Something has to run the thresholds. The apply path does, since
    it already runs on a schedule and holds a session."""

    def store(self) -> Any:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from aisvc.db import Base

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        return sessionmaker(engine)()

    def certificate(self, session: Any, *, days: float) -> Any:
        from aisvc.db.tier2 import CertificateStore

        return CertificateStore(session).add(
            account="acme",
            domain="/Customers/Acme",
            custom_domain="ai.acme.example",
            certificate="",
            private_key="",
            fingerprint="ff",
            not_after=expiring_in(days),
        )

    async def sweep(self, session: Any) -> list[Any]:
        from aisvc.certificates.renewal import sweep_renewals
        from aisvc.observability.alerts import CollectingSink

        sink = CollectingSink()
        await sweep_renewals(session, sink=sink)
        return sink.alerts

    async def test_an_expiring_certificate_alerts(self) -> None:
        session = self.store()
        self.certificate(session, days=10)

        assert await self.sweep(session)

    async def test_a_healthy_one_does_not(self) -> None:
        session = self.store()
        self.certificate(session, days=200)

        assert await self.sweep(session) == []

    async def test_each_threshold_alerts_once(self) -> None:
        """The property that makes the last warning readable."""
        session = self.store()
        self.certificate(session, days=10)

        first = await self.sweep(session)
        second = await self.sweep(session)

        assert first
        assert second == []

    async def test_a_further_threshold_alerts_again(self) -> None:
        session = self.store()
        record = self.certificate(session, days=10)
        await self.sweep(session)

        record.not_after = expiring_in(0.5)
        later = await self.sweep(session)

        assert later

    async def test_a_renewal_resets_the_countdown(self) -> None:
        """A replaced certificate is a new expiry, so its thresholds
        start again — otherwise the next renewal passes unannounced."""
        from aisvc.db.tier2 import CertificateStore

        session = self.store()
        record = self.certificate(session, days=10)
        await self.sweep(session)

        CertificateStore(session).replace_material(
            record,
            certificate="",
            private_key="",
            fingerprint="gg",
            not_after=expiring_in(10),
        )

        assert await self.sweep(session)

    async def test_a_failing_sink_does_not_stop_the_sweep(self) -> None:
        from aisvc.certificates.renewal import sweep_renewals

        class Broken:
            async def send(self, alert: Any) -> None:
                raise RuntimeError("the webhook exploded")

        session = self.store()
        self.certificate(session, days=10)

        await sweep_renewals(session, sink=Broken())


class TestOneAlertPerSweep:
    """Several thresholds crossing at once must not become several
    near-identical messages.

    They do cross together — a certificate first seen ten days out
    passes 30 and 14 in the same sweep — and three alerts reading
    "expires in 9 days" is exactly the noise the thresholds exist to
    prevent. One alert, at the most urgent severity, and every crossed
    point recorded so none fires again.
    """

    def record(self, days: float) -> Any:
        from aisvc.db.models import CertificateRecord

        return CertificateRecord(
            id="c-1",
            account="acme",
            domain="/Customers/Acme",
            custom_domain="ai.acme.example",
            certificate="",
            private_key="",
            fingerprint="ff",
            not_after=expiring_in(days),
        )

    def test_two_thresholds_crossing_make_one_alert(self) -> None:
        alerts = renewal_alerts(self.record(10))

        assert len(alerts) == 1

    def test_it_takes_the_most_urgent_severity(self) -> None:
        from aisvc.observability.alerts import AlertSeverity

        # 30, 14 and 7 all cross at five days left.
        alerts = renewal_alerts(self.record(5))

        assert alerts[0].severity is AlertSeverity.CRITICAL

    def test_every_crossed_threshold_is_recorded(self) -> None:
        """Or the ones skipped would fire on the next sweep."""
        alerts = renewal_alerts(self.record(5))

        assert sorted(alerts[0].detail["thresholds"]) == [7, 14, 30]

    def test_a_single_crossing_is_unchanged(self) -> None:
        alerts = renewal_alerts(self.record(20))

        assert len(alerts) == 1
        assert alerts[0].detail["thresholds"] == [30]
