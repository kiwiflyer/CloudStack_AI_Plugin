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
"""Delivering alerts (SPEC.md sections 7 and 11).

Budget thresholds were being computed and then dropped: `UsageStore`
returns each newly crossed threshold exactly once, and nothing acted on
the answer.

SPEC names three channels. Two of them are CloudStack's rather than
ours — events and email are raised by the plugin, which holds the
session, the account and CloudStack's own alert machinery — so what
belongs here is the HMAC-signed webhook, and a log sink so an operator
with neither still sees something.

**Alerts never fail the thing that produced them.** A budget alert is
raised while serving a tenant's request; an unreachable webhook is not
that tenant's problem, so every sink swallows its own failures and says
so in the log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aisvc.metering import BudgetState

__all__ = [
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "Alert",
    "AlertSeverity",
    "AlertSink",
    "CollectingSink",
    "CompositeSink",
    "LogSink",
    "WebhookSink",
    "budget_alerts",
]

log = logging.getLogger("aisvc.alerts")

#: `sha256=<hex>` over `<timestamp>.<body>`.
SIGNATURE_HEADER = "X-Aisvc-Signature"
#: Unix seconds, inside the signature so an alert cannot be replayed.
TIMESTAMP_HEADER = "X-Aisvc-Timestamp"


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing an operator or tenant should be told about.

    Carries no prompt or completion text, and nothing that would
    identify a request (SPEC.md section 11).
    """

    category: str
    summary: str
    severity: AlertSeverity = AlertSeverity.INFO
    account: str = ""
    domain: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_payload(self) -> dict[str, Any]:
        """What a webhook receives."""
        return {
            "category": self.category,
            "summary": self.summary,
            "severity": self.severity.value,
            "account": self.account,
            "domain": self.domain,
            "detail": self.detail,
            "at": self.at.isoformat(),
        }


class AlertSink(Protocol):
    """Somewhere alerts go."""

    async def send(self, alert: Alert) -> None: ...


class LogSink:
    """The sink every deployment has."""

    async def send(self, alert: Alert) -> None:
        log.warning("[%s] %s", alert.category, alert.summary)


class CollectingSink:
    """Keeps alerts in memory, for tests and for the API to show."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class CompositeSink:
    """Fans an alert out, and does not let one sink stop the others."""

    def __init__(self, sinks: Sequence[AlertSink]) -> None:
        self.sinks = list(sinks)

    async def send(self, alert: Alert) -> None:
        for sink in self.sinks:
            try:
                await sink.send(alert)
            except Exception:
                log.exception("an alert sink failed; continuing with the others")


class WebhookSink:
    """Posts signed alerts to an operator's endpoint.

    The signature covers `<timestamp>.<body>` rather than the body
    alone, so a captured alert cannot be replayed later and still
    verify. Receivers should reject a timestamp that is too old.
    """

    def __init__(
        self,
        url: str,
        *,
        secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.secret = secret
        self._transport = transport
        self._timeout = timeout

    async def send(self, alert: Alert) -> None:
        body = json.dumps(alert.as_payload(), separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.post(
                    self.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        SIGNATURE_HEADER: f"sha256={signature}",
                        TIMESTAMP_HEADER: timestamp,
                    },
                )
            if response.status_code >= 400:
                log.warning("alert webhook %s answered HTTP %d", self.url, response.status_code)
        except httpx.HTTPError as exc:
            # The tenant's request is not the webhook's problem.
            log.warning("alert webhook %s is unreachable: %s", self.url, exc)


def budget_alerts(account: str, domain: str, state: BudgetState) -> list[Alert]:
    """One alert per threshold this spend newly crossed.

    One each rather than one combined: they are delivered once and
    never repeated, so folding 50% into the 80% message would mean the
    50% notice was never sent at all.
    """
    alerts: list[Alert] = []
    for threshold in state.newly_crossed:
        at_limit = threshold >= 100
        blocking = at_limit and state.on_exceed == "block"

        summary = (
            f"{account} has used {threshold}% of its "
            f"{state.limit:.2f} {state.currency} monthly budget "
            f"({state.spent:.2f} spent)"
        )
        if blocking:
            summary += "; further requests will be blocked"

        alerts.append(
            Alert(
                category="budget",
                summary=summary,
                severity=AlertSeverity.CRITICAL if at_limit else AlertSeverity.WARNING,
                account=account,
                domain=domain,
                detail={
                    "threshold": threshold,
                    "limit": state.limit,
                    "spent": state.spent,
                    "currency": state.currency,
                    "on_exceed": state.on_exceed,
                },
            )
        )
    return alerts
