"""Noticing that a certificate is about to expire (SPEC.md section 8).

Expiry was stored and nothing watched it, which is the failure mode
that matters here: a certificate does not degrade, it stops. The first
sign is a tenant's own customers seeing a browser warning, and by then
the fix is hours away.

The thresholds fire once each, like budget ones and for the same
reason: a warning on every reconcile is a warning nobody reads, and the
one that matters — a day out — would arrive looking exactly like the
two hundred before it.

Alerts go to the owning **tenant**, not the operator's pager. They are
the only ones who can upload a replacement.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from aisvc.observability.alerts import Alert, AlertSeverity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aisvc.db.models import CertificateRecord

log = logging.getLogger("aisvc.certificates.renewal")

__all__ = [
    "RENEWAL_THRESHOLDS",
    "crossed_renewal_thresholds",
    "days_until",
    "renewal_alerts",
    "sweep_renewals",
]

#: Days before expiry at which to say something. Far enough out to buy
#: a replacement comfortably, then increasingly insistent.
RENEWAL_THRESHOLDS: tuple[int, ...] = (30, 14, 7, 1)

#: Past this one, the matter is urgent rather than a reminder.
CRITICAL_BELOW_DAYS = 7


def days_until(moment: datetime, *, now: datetime | None = None) -> int:
    """Whole days until `moment`, negative once it has passed."""
    # SQLite hands timestamps back without a timezone; Postgres does not.
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    delta = aware - (now or datetime.now(UTC))
    return int(delta.total_seconds() // 86400)


def crossed_renewal_thresholds(
    *,
    days_left: int,
    already_notified: Sequence[int] = (),
    thresholds: Sequence[int] = RENEWAL_THRESHOLDS,
) -> list[int]:
    """Which warning points this expiry has newly passed."""
    seen = set(already_notified)
    return [
        threshold
        for threshold in sorted(thresholds, reverse=True)
        if days_left <= threshold and threshold not in seen
    ]


def renewal_alerts(record: CertificateRecord, already_notified: Sequence[int] = ()) -> list[Alert]:
    """One alert per newly crossed threshold.

    Carries the domain and the days left and nothing else: an alert
    reaches logs and webhooks, and certificate material has no business
    in either.
    """
    left = days_until(record.not_after)
    alerts: list[Alert] = []

    for threshold in crossed_renewal_thresholds(days_left=left, already_notified=already_notified):
        if left < 0:
            summary = (
                f"the TLS certificate for {record.custom_domain} expired "
                f"{abs(left)} day(s) ago; its endpoint is serving a certificate "
                "browsers will refuse"
            )
        else:
            summary = (
                f"the TLS certificate for {record.custom_domain} expires in "
                f"{left} day(s); upload a replacement before it does"
            )

        alerts.append(
            Alert(
                category="certificate",
                summary=summary,
                severity=(
                    AlertSeverity.CRITICAL
                    if threshold <= CRITICAL_BELOW_DAYS
                    else AlertSeverity.WARNING
                ),
                account=record.account,
                domain=record.domain,
                detail={
                    "certificate": record.id,
                    "customdomain": record.custom_domain,
                    "daysleft": left,
                    "threshold": threshold,
                    "expires": record.not_after.isoformat() if record.not_after else None,
                },
            )
        )

    return alerts


async def sweep_renewals(session: Any, *, sink: Any) -> int:
    """Alert on every certificate that has newly crossed a threshold.

    Run from the apply path, which already runs on a schedule and
    holds a session. Each threshold fires once: the record remembers
    which, and a replacement resets them because a new expiry is a new
    countdown.
    """
    from aisvc.db.models import CertificateRecord

    sent = 0
    for record in session.scalars(select(CertificateRecord)):
        already = list(record.notified or ())
        alerts = renewal_alerts(record, already_notified=already)
        if not alerts:
            continue

        record.notified = sorted(already + [int(alert.detail["threshold"]) for alert in alerts])
        for alert in alerts:
            try:
                await sink.send(alert)
            except Exception:
                log.exception("could not deliver a renewal alert for %s", record.custom_domain)
            sent += 1

    session.flush()
    return sent
