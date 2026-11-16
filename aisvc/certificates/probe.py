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
"""Reading the certificate an endpoint actually serves (SPEC.md section 8).

The renewal sweep watches certificates this platform holds. An ACME
endpoint has none: certbot issues and renews on the proxy itself, so
the database knows nothing about it, and a renewal that quietly fails
leaves nginx on an expired certificate with no alert at all.

So this asks the endpoint instead. Probing rather than having the proxy
report back, for three reasons:

- it needs no credential on the proxy, which is the problem that made
  ACME awkward in the first place;
- it sees what a browser sees, so it also catches nginx never being
  reloaded after a renewal, or DNS pointing somewhere else;
- a proxy that has stopped answering cannot file a report saying so.

**Expiry and trust are read as separate questions.** The connection is
made without verification so the certificate can be read whatever state
it is in — an expired one is exactly what this exists to find, and a
verifying connection would refuse it and report nothing. Trust is then
checked on its own, because "stops working on Friday" and "never
worked" are different problems with different fixes.
"""

from __future__ import annotations

import logging
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from aisvc.observability.alerts import Alert, AlertSeverity

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger("aisvc.certificates.probe")

__all__ = [
    "DEFAULT_TIMEOUT",
    "ProbeResult",
    "expired",
    "probe_alerts",
    "probe_endpoint",
    "sweep_endpoint_certificates",
]

#: Short: this runs over every private endpoint on a reconcile, and a
#: proxy that has gone away should not hold one up.
DEFAULT_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What an endpoint presented, or why nothing could be read."""

    host: str
    port: int
    reachable: bool = False
    #: Whether it also chains to a public root and matches the name.
    #: A separate question from expiry, and a separate problem.
    trusted: bool = False
    not_after: datetime | None = None
    names: tuple[str, ...] = ()
    #: One line, safe for an alert and a tenant's console.
    error: str = ""


def _read_certificate(host: str, port: int, server_name: str, timeout: float) -> tuple[bytes, str]:
    """The DER certificate this endpoint presents, without verifying it."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Deliberately off: an expired or untrusted certificate is what
    # this is looking for, and a verifying handshake would refuse the
    # connection and tell us nothing about it.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with (
        socket.create_connection((host, port), timeout=timeout) as raw,
        context.wrap_socket(raw, server_hostname=server_name) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
        version = tls.version() or ""
    if not der:
        raise ssl.SSLError("the endpoint presented no certificate")
    return der, version


def _is_trusted(host: str, port: int, server_name: str, timeout: float) -> bool:
    """Whether a browser would accept it: public root and right name."""
    context = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as raw,
            context.wrap_socket(raw, server_hostname=server_name),
        ):
            return True
    except (OSError, ssl.SSLError):
        return False


def probe_endpoint(
    host: str,
    *,
    port: int = 443,
    server_name: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ProbeResult:
    """Read the certificate an endpoint is serving right now.

    Never raises: a probe that threw would stop the sweep at the first
    unreachable proxy, and the ones behind it are the ones most likely
    to be broken too.
    """
    sni = server_name or host

    try:
        der, _ = _read_certificate(host, port, sni, timeout)
    except (OSError, ssl.SSLError, ValueError) as exc:
        # One line, no stack: this reaches an alert and a console.
        return ProbeResult(
            host=host, port=port, reachable=False, error=f"{type(exc).__name__}: {exc}"[:180]
        )

    from cryptography import x509

    try:
        certificate = x509.load_der_x509_certificate(der)
    except Exception as exc:
        return ProbeResult(
            host=host,
            port=port,
            reachable=True,
            error=f"the endpoint presented something unreadable: {exc}"[:180],
        )

    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = tuple(san.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        names = ()

    return ProbeResult(
        host=host,
        port=port,
        reachable=True,
        trusted=_is_trusted(host, port, sni, timeout),
        not_after=certificate.not_valid_after_utc,
        names=names,
    )


def expired(result: ProbeResult, *, now: datetime | None = None) -> bool:
    """Whether what is being served has already stopped working."""
    if result.not_after is None:
        return False
    return result.not_after <= (now or datetime.now(UTC))


def probe_alerts(
    result: ProbeResult,
    *,
    domain: str,
    account: str,
    account_domain: str,
    already_notified: Sequence[int] = (),
) -> list[Alert]:
    """Turn what was observed into alerts for the owning tenant.

    Three different failures, deliberately not collapsed into one:
    unreachable means nobody is being served; untrusted means every
    browser refuses it however fresh it is — which is what a failed
    ACME renewal leaving a self-signed certificate looks like; and an
    approaching expiry is the ordinary countdown.
    """
    from aisvc.certificates.renewal import crossed_renewal_thresholds, days_until

    def alert(summary: str, severity: AlertSeverity, **detail: Any) -> Alert:
        return Alert(
            category="endpoint-certificate",
            summary=summary,
            severity=severity,
            account=account,
            domain=account_domain,
            detail={"endpoint": domain, **detail},
        )

    if not result.reachable:
        return [
            alert(
                f"{domain} is not answering on TLS, so nothing is being served "
                f"there ({result.error})",
                AlertSeverity.CRITICAL,
                condition="unreachable",
            )
        ]

    alerts: list[Alert] = []

    if result.names and not _covers(result.names, domain):
        listed = ", ".join(result.names)
        alerts.append(
            alert(
                f"{domain} is serving a certificate for {listed}, which browsers will refuse",
                AlertSeverity.CRITICAL,
                names=list(result.names),
            )
        )

    if not result.trusted:
        alerts.append(
            alert(
                f"the certificate {domain} is serving does not establish trust; "
                "browsers will refuse it even though it has not expired",
                AlertSeverity.CRITICAL,
                condition="untrusted",
            )
        )

    if result.not_after is not None:
        left = days_until(result.not_after)
        crossed = crossed_renewal_thresholds(days_left=left, already_notified=already_notified)
        if crossed:
            summary = (
                f"the certificate {domain} is serving expired {abs(left)} day(s) ago"
                if left < 0
                else f"the certificate {domain} is serving expires in {left} day(s)"
            )
            alerts.append(
                alert(
                    summary,
                    AlertSeverity.CRITICAL if min(crossed) <= 7 else AlertSeverity.WARNING,
                    daysleft=left,
                    thresholds=sorted(crossed),
                )
            )

    return alerts


def conditions_in(result: ProbeResult, *, domain: str) -> set[str]:
    """Standing problems this probe found.

    These stay true until someone fixes them, unlike a threshold, which
    is a moment. Recorded so each is announced once and again only if
    it clears and comes back — a problem fixed and reintroduced should
    not pass unseen.
    """
    found: set[str] = set()
    if not result.reachable:
        found.add("unreachable")
        return found
    if result.names and not _covers(result.names, domain):
        found.add("wrong_domain")
    if not result.trusted:
        found.add("untrusted")
    return found


def _covers(names: tuple[str, ...], domain: str) -> bool:
    from aisvc.certificates import covers_domain

    return covers_domain(names, domain)


async def sweep_endpoint_certificates(session: Any, *, sink: Any, probe: Any = None) -> int:
    """Probe every ACME endpoint and alert on what it is serving.

    Only ACME ones: an uploaded certificate is watched in the database
    by `sweep_renewals`, and probing those as well would alert twice
    for one problem.

    A probe that raises is logged and skipped. Stopping at the first
    unreachable proxy would hide the ones behind it, which are the
    most likely to be broken too.
    """
    from aisvc.db.models import PrivateEndpointRecord

    look = probe or probe_endpoint
    sent = 0

    for record in session.scalars(select(PrivateEndpointRecord)):
        if record.tls != "acme" or not record.custom_domain:
            continue

        try:
            result = look(record.custom_domain)
        except Exception:
            log.exception("could not probe %s", record.custom_domain)
            continue

        remembered = list(record.acme_notified or ())
        # Thresholds are integers and standing conditions are names.
        # They share one column because they answer one question: has
        # this already been said.
        already = [item for item in remembered if isinstance(item, int)]
        announced = {item for item in remembered if isinstance(item, str)}

        alerts = probe_alerts(
            result,
            domain=record.custom_domain,
            account=record.account,
            account_domain=record.domain,
            already_notified=already,
        )

        # A standing condition is dropped if it has already been said,
        # and kept if it is new or has returned since it last cleared.
        # Alerting on every reconcile is how an alert stops being read.
        current = conditions_in(result, domain=record.custom_domain)
        alerts = [
            alert
            for alert in alerts
            if "thresholds" in alert.detail or alert.detail.get("condition") not in announced
        ]

        crossed = [
            int(threshold) for alert in alerts for threshold in alert.detail.get("thresholds", ())
        ]
        record.acme_notified = [*sorted({*already, *crossed}), *sorted(current)]

        if not alerts:
            continue

        for alert in alerts:
            try:
                await sink.send(alert)
            except Exception:
                log.exception("could not deliver an endpoint alert")
            sent += 1

    session.flush()
    return sent
