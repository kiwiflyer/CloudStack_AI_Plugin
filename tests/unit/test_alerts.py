"""Delivering alerts (SPEC.md sections 7 and 11).

Budget thresholds were computed and never sent anywhere: `UsageStore`
returns the newly crossed ones exactly once and nothing acted on them
(PROGRESS.md, milestone 7 gap 4).

SPEC names three channels. Two are CloudStack's — events and email are
raised by the plugin, which has the session and the account — so the
engine's share is the HMAC-signed webhook, plus the log every operator
already has.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from aisvc.metering import BudgetState
from aisvc.observability.alerts import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    Alert,
    AlertSeverity,
    CollectingSink,
    CompositeSink,
    WebhookSink,
    budget_alerts,
)

SECRET = "shared-secret"


def alert(**kw: object) -> Alert:
    defaults: dict[str, object] = {
        "category": "budget",
        "summary": "acme has used 80% of its monthly budget",
        "severity": AlertSeverity.WARNING,
        "account": "acme",
        "domain": "/Customers/Acme",
    }
    defaults.update(kw)
    return Alert(**defaults)  # type: ignore[arg-type]


class TestBudgetAlerts:
    def state(self, **kw: object) -> BudgetState:
        defaults: dict[str, object] = {
            "limit": 100.0,
            "spent": 80.0,
            "currency": "USD",
            "on_exceed": "alert",
            "newly_crossed": [80],
        }
        defaults.update(kw)
        return BudgetState(**defaults)  # type: ignore[arg-type]

    def test_a_crossed_threshold_becomes_an_alert(self) -> None:
        alerts = budget_alerts("acme", "/Customers/Acme", self.state())

        assert len(alerts) == 1
        assert "80" in alerts[0].summary

    def test_nothing_crossed_means_no_alert(self) -> None:
        assert budget_alerts("acme", "/d", self.state(newly_crossed=[])) == []

    def test_each_threshold_gets_its_own_alert(self) -> None:
        """They are sent once each; batching them would lose the 50%."""
        alerts = budget_alerts("acme", "/d", self.state(newly_crossed=[50, 80]))

        assert len(alerts) == 2

    def test_the_hundred_percent_alert_is_more_severe(self) -> None:
        alerts = budget_alerts("acme", "/d", self.state(spent=100, newly_crossed=[100]))

        assert alerts[0].severity is AlertSeverity.CRITICAL

    def test_a_blocking_budget_says_requests_are_stopping(self) -> None:
        """The tenant needs to know service stops, not just that they spent."""
        alerts = budget_alerts(
            "acme",
            "/d",
            self.state(spent=100, on_exceed="block", newly_crossed=[100]),
        )

        assert "block" in alerts[0].summary.lower()

    def test_an_alerting_budget_does_not_claim_to_block(self) -> None:
        alerts = budget_alerts(
            "acme",
            "/d",
            self.state(spent=100, on_exceed="alert", newly_crossed=[100]),
        )

        assert "block" not in alerts[0].summary.lower()

    def test_the_alert_carries_the_tenant(self) -> None:
        alerts = budget_alerts("acme", "/Customers/Acme", self.state())

        assert alerts[0].account == "acme"
        assert alerts[0].domain == "/Customers/Acme"

    def test_the_amounts_are_in_the_alert(self) -> None:
        alerts = budget_alerts("acme", "/d", self.state())

        assert "USD" in alerts[0].summary


class TestWebhookSink:
    def sink(self, handler: httpx.MockTransport) -> WebhookSink:
        return WebhookSink("https://hooks.example.com/aisvc", secret=SECRET, transport=handler)

    def capture(self) -> tuple[httpx.MockTransport, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        return httpx.MockTransport(handle), seen

    async def test_an_alert_is_posted(self) -> None:
        transport, seen = self.capture()

        await self.sink(transport).send(alert())

        assert len(seen) == 1
        assert seen[0].method == "POST"

    async def test_the_body_carries_the_alert(self) -> None:
        transport, seen = self.capture()

        await self.sink(transport).send(alert())

        body = json.loads(seen[0].content)
        assert body["category"] == "budget"
        assert body["account"] == "acme"

    async def test_the_body_is_signed(self) -> None:
        transport, seen = self.capture()

        await self.sink(transport).send(alert())

        signature = seen[0].headers[SIGNATURE_HEADER]
        timestamp = seen[0].headers[TIMESTAMP_HEADER]
        expected = hmac.new(
            SECRET.encode(),
            timestamp.encode() + b"." + seen[0].content,
            hashlib.sha256,
        ).hexdigest()
        assert signature == f"sha256={expected}"

    async def test_the_timestamp_is_inside_the_signature(self) -> None:
        """Signing the body alone would let an old alert be replayed."""
        transport, seen = self.capture()

        await self.sink(transport).send(alert())

        tampered = hmac.new(SECRET.encode(), seen[0].content, hashlib.sha256).hexdigest()
        assert seen[0].headers[SIGNATURE_HEADER] != f"sha256={tampered}"

    async def test_a_different_secret_produces_a_different_signature(self) -> None:
        transport, seen = self.capture()

        await self.sink(transport).send(alert())
        await WebhookSink(
            "https://hooks.example.com/aisvc", secret="other", transport=transport
        ).send(alert())

        assert seen[0].headers[SIGNATURE_HEADER] != seen[1].headers[SIGNATURE_HEADER]

    async def test_no_prompt_or_completion_text_is_sent(self) -> None:
        """SPEC.md section 11: prompts and outputs are never logged."""
        transport, seen = self.capture()

        await self.sink(transport).send(alert())

        assert set(json.loads(seen[0].content)) <= {
            "category",
            "summary",
            "severity",
            "account",
            "domain",
            "detail",
            "at",
        }

    async def test_a_failing_webhook_does_not_raise(self) -> None:
        """An alert that cannot be delivered must not fail the request
        that produced it: the tenant's call is not the webhook's problem."""

        def handle(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        await self.sink(httpx.MockTransport(handle)).send(alert())

    async def test_a_rejecting_webhook_does_not_raise(self) -> None:
        def handle(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        await self.sink(httpx.MockTransport(handle)).send(alert())


class TestCompositeSink:
    async def test_every_sink_receives_the_alert(self) -> None:
        one, two = CollectingSink(), CollectingSink()

        await CompositeSink([one, two]).send(alert())

        assert len(one.alerts) == 1
        assert len(two.alerts) == 1

    async def test_one_failing_sink_does_not_stop_the_others(self) -> None:
        class Broken(CollectingSink):
            async def send(self, alert: Alert) -> None:
                raise RuntimeError("this sink is broken")

        working = CollectingSink()

        await CompositeSink([Broken(), working]).send(alert())

        assert len(working.alerts) == 1

    async def test_no_sinks_is_not_an_error(self) -> None:
        await CompositeSink([]).send(alert())
