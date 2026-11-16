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
"""Watching what an endpoint actually serves (SPEC.md section 8).

The renewal sweep watches certificates we hold. An ACME endpoint has
none: certbot issues and renews on the proxy itself, so the engine's
database knows nothing about it, and a renewal that quietly fails
leaves nginx on an expired certificate with no alert at all.

So this asks the endpoint. Probing rather than reporting, for three
reasons:

- it needs no credential on the proxy, which is the problem that made
  ACME awkward in the first place;
- it sees what a browser sees, so it also catches nginx never being
  reloaded after a renewal, or DNS pointing somewhere else entirely;
- a proxy that has stopped answering cannot file a report saying so.

These run against a real TLS server on localhost, not a mock: the
point is to read a certificate off a socket, and a mocked socket would
prove nothing about that.
"""

from __future__ import annotations

import datetime as dt
import socket
import ssl
import threading
from pathlib import Path
from typing import Any

import pytest

from aisvc.certificates.probe import ProbeResult, probe_endpoint


def serve_tls(cert_pem: str, key_pem: str, tmp_path: Path) -> tuple[str, int, Any]:
    """A TLS server on localhost presenting this certificate."""
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text(cert_pem)
    key_file.write_text(key_pem)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]

    stop = threading.Event()

    def run() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            try:
                with context.wrap_socket(raw, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass
            finally:
                raw.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    class Server:
        def close(self) -> None:
            stop.set()
            thread.join(timeout=2)
            listener.close()

    return "127.0.0.1", port, Server()


@pytest.fixture
def certificate() -> Any:
    from tests.unit.test_certificates import make_cert

    return make_cert


class TestReadingWhatIsServed:
    def test_the_expiry_is_read_off_the_socket(self, certificate: Any, tmp_path: Path) -> None:
        now = dt.datetime.now(dt.UTC)
        cert, key = certificate(
            domains=("ai.acme.example",),
            not_before=now - dt.timedelta(days=1),
            not_after=now + dt.timedelta(days=42),
        )
        host, port, server = serve_tls(cert, key, tmp_path)
        try:
            result = probe_endpoint(host, port=port, server_name="ai.acme.example")
        finally:
            server.close()

        assert isinstance(result, ProbeResult)
        assert result.reachable
        assert result.not_after is not None
        assert 40 <= (result.not_after - now).days <= 42

    def test_the_names_are_read(self, certificate: Any, tmp_path: Path) -> None:
        cert, key = certificate(domains=("ai.acme.example", "api.acme.example"))
        host, port, server = serve_tls(cert, key, tmp_path)
        try:
            result = probe_endpoint(host, port=port, server_name="ai.acme.example")
        finally:
            server.close()

        assert "ai.acme.example" in result.names

    def test_a_self_signed_certificate_is_read_but_not_trusted(
        self, certificate: Any, tmp_path: Path
    ) -> None:
        """Expiry and trust are different questions. One says when it
        stops working; the other says it never did."""
        cert, key = certificate(domains=("ai.acme.example",))
        host, port, server = serve_tls(cert, key, tmp_path)
        try:
            result = probe_endpoint(host, port=port, server_name="ai.acme.example")
        finally:
            server.close()

        assert result.reachable
        assert result.not_after is not None
        assert not result.trusted

    def test_an_expired_certificate_is_still_read(self, certificate: Any, tmp_path: Path) -> None:
        """The whole point: an endpoint serving an expired certificate
        has to be visible, not a connection error."""
        now = dt.datetime.now(dt.UTC)
        cert, key = certificate(
            domains=("ai.acme.example",),
            not_before=now - dt.timedelta(days=100),
            not_after=now - dt.timedelta(days=3),
        )
        host, port, server = serve_tls(cert, key, tmp_path)
        try:
            result = probe_endpoint(host, port=port, server_name="ai.acme.example")
        finally:
            server.close()

        assert result.reachable
        assert result.not_after is not None
        assert result.not_after < now
        assert not result.trusted


class TestWhenItCannotLook:
    def test_a_closed_port_is_not_reachable(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()

        result = probe_endpoint("127.0.0.1", port=port, timeout=1.0)

        assert not result.reachable
        assert result.not_after is None
        assert result.error

    def test_an_unresolvable_host_is_not_reachable(self) -> None:
        result = probe_endpoint("no-such-host.invalid", port=443, timeout=1.0)

        assert not result.reachable
        assert result.error

    def test_a_plain_http_port_is_not_reachable(self, tmp_path: Path) -> None:
        """A proxy serving HTTP where TLS was expected is a failure
        worth seeing, not a silent pass."""
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def accept_and_hang() -> None:
            try:
                raw, _ = listener.accept()
                raw.recv(16)
                raw.close()
            except OSError:
                pass

        thread = threading.Thread(target=accept_and_hang, daemon=True)
        thread.start()
        try:
            result = probe_endpoint("127.0.0.1", port=port, timeout=2.0)
        finally:
            listener.close()

        assert not result.reachable

    def test_an_error_carries_no_stack(self) -> None:
        """It reaches an alert and a tenant's console."""
        result = probe_endpoint("no-such-host.invalid", port=443, timeout=1.0)

        assert "Traceback" not in result.error
        assert len(result.error) < 200


class TestAlertsFromAProbe:
    """What the probe sees becomes the same kind of alert an uploaded
    certificate's expiry does, with a category that says it was
    observed on the wire rather than read from our own database."""

    def result(self, **kw: Any) -> ProbeResult:
        defaults: dict[str, Any] = {
            "host": "ai.acme.example",
            "port": 443,
            "reachable": True,
            "trusted": True,
            "not_after": dt.datetime.now(dt.UTC) + dt.timedelta(days=60),
            "names": ("ai.acme.example",),
        }
        defaults.update(kw)
        return ProbeResult(**defaults)

    def alerts(self, result: ProbeResult, **kw: Any) -> list[Any]:
        from aisvc.certificates.probe import probe_alerts

        return probe_alerts(
            result,
            domain="ai.acme.example",
            account="acme",
            account_domain="/Customers/Acme",
            **kw,
        )

    def test_a_healthy_endpoint_says_nothing(self) -> None:
        assert self.alerts(self.result()) == []

    def test_an_unreachable_endpoint_is_critical(self) -> None:
        """Nobody is being served at all."""
        from aisvc.observability.alerts import AlertSeverity

        alerts = self.alerts(self.result(reachable=False, error="timed out"))

        assert alerts
        assert alerts[0].severity is AlertSeverity.CRITICAL

    def test_an_expiring_certificate_crosses_thresholds(self) -> None:
        alerts = self.alerts(self.result(not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=10)))

        assert alerts

    def test_each_threshold_fires_once(self) -> None:
        soon = self.result(not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=10))

        assert self.alerts(soon, already_notified=[30, 14]) == []

    def test_an_expired_certificate_is_critical(self) -> None:
        from aisvc.observability.alerts import AlertSeverity

        alerts = self.alerts(
            self.result(
                trusted=False,
                not_after=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
            )
        )

        assert any(a.severity is AlertSeverity.CRITICAL for a in alerts)

    def test_an_untrusted_certificate_is_reported_even_if_it_is_fresh(self) -> None:
        """ACME failing over and leaving a self-signed certificate in
        place looks fine by expiry and is refused by every browser."""
        alerts = self.alerts(self.result(trusted=False))

        assert alerts
        assert "trust" in alerts[0].summary.lower()

    def test_serving_the_wrong_domain_is_reported(self) -> None:
        alerts = self.alerts(self.result(names=("somewhere-else.example",)))

        assert alerts
        assert any("ai.acme.example" in a.summary for a in alerts)

    def test_the_alert_reaches_the_owning_tenant(self) -> None:
        alerts = self.alerts(self.result(reachable=False, error="x"))

        assert alerts[0].account == "acme"

    def test_the_category_says_it_was_observed(self) -> None:
        """So an operator can tell this from our stored copy's expiry."""
        alerts = self.alerts(self.result(reachable=False, error="x"))

        assert alerts[0].category == "endpoint-certificate"

    def test_no_certificate_material_is_carried(self) -> None:
        alerts = self.alerts(self.result(reachable=False, error="x"))

        assert "BEGIN" not in str(alerts[0].as_payload())


class TestTheSweep:
    """Over ACME exposures, from the apply path.

    Uploaded certificates are watched in the database; these are the
    ones that have no database entry to watch.
    """

    def session(self) -> Any:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from aisvc.db import Base

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        return sessionmaker(engine)()

    def exposure(self, session: Any, *, tls: str = "acme", domain: str = "ai.acme.example") -> Any:
        from aisvc.db.tier2 import ExposureStore
        from aisvc.endpoints import EndpointExposure, TlsSource

        return ExposureStore(session).add(
            EndpointExposure(
                deployment="llama-3.3-70b-instruct",
                account="acme",
                domain="/Customers/Acme",
                public_enabled=True,
                network_id="net-1",
                custom_domain=domain,
                tls=TlsSource(tls),
            )
        )

    async def sweep(self, session: Any, result: ProbeResult) -> list[Any]:
        from aisvc.certificates.probe import sweep_endpoint_certificates
        from aisvc.observability.alerts import CollectingSink

        sink = CollectingSink()
        await sweep_endpoint_certificates(session, sink=sink, probe=lambda host, **kw: result)
        return sink.alerts

    def healthy(self, **kw: Any) -> ProbeResult:
        defaults: dict[str, Any] = {
            "host": "ai.acme.example",
            "port": 443,
            "reachable": True,
            "trusted": True,
            "names": ("ai.acme.example",),
            "not_after": dt.datetime.now(dt.UTC) + dt.timedelta(days=60),
        }
        defaults.update(kw)
        return ProbeResult(**defaults)

    async def test_a_healthy_acme_endpoint_is_quiet(self) -> None:
        session = self.session()
        self.exposure(session)

        assert await self.sweep(session, self.healthy()) == []

    async def test_a_failed_renewal_is_caught(self) -> None:
        """The gap this exists for: certbot's timer failed, nobody
        noticed, and the certificate is days from stopping."""
        session = self.session()
        self.exposure(session)

        alerts = await self.sweep(
            session,
            self.healthy(not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=5)),
        )

        assert alerts

    async def test_an_unreachable_proxy_is_caught(self) -> None:
        session = self.session()
        self.exposure(session)

        alerts = await self.sweep(
            session, ProbeResult(host="h", port=443, reachable=False, error="refused")
        )

        assert alerts

    async def test_each_threshold_fires_once(self) -> None:
        session = self.session()
        self.exposure(session)
        soon = self.healthy(not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=10))

        first = await self.sweep(session, soon)
        second = await self.sweep(session, soon)

        assert first
        assert second == []

    async def test_an_uploaded_exposure_is_not_probed(self) -> None:
        """Those are watched in the database; probing them too would
        alert twice for one problem."""
        session = self.session()
        self.exposure(session, tls="uploaded")

        assert await self.sweep(session, self.healthy()) == []

    async def test_an_exposure_with_no_custom_domain_is_not_probed(self) -> None:
        """Plain HTTP on a tenant's own network serves no certificate."""
        session = self.session()
        self.exposure(session, tls="none", domain="")

        assert await self.sweep(session, self.healthy()) == []

    async def test_a_failing_probe_does_not_stop_the_sweep(self) -> None:
        from aisvc.certificates.probe import sweep_endpoint_certificates
        from aisvc.observability.alerts import CollectingSink

        session = self.session()
        self.exposure(session)

        def explode(host: str, **kw: Any) -> ProbeResult:
            raise RuntimeError("the socket layer gave up")

        await sweep_endpoint_certificates(session, sink=CollectingSink(), probe=explode)


class TestStandingConditionsDoNotRepeat:
    """Untrusted, unreachable and wrong-domain are conditions rather
    than moments: they stay true until someone fixes them.

    Alerting on every reconcile would be the "alert nobody reads"
    problem, so each is announced once and again only if it clears and
    comes back.
    """

    def session(self) -> Any:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from aisvc.db import Base

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        return sessionmaker(engine)()

    def exposure(self, session: Any) -> Any:
        from aisvc.db.tier2 import ExposureStore
        from aisvc.endpoints import EndpointExposure, TlsSource

        return ExposureStore(session).add(
            EndpointExposure(
                deployment="m",
                account="acme",
                domain="/Customers/Acme",
                public_enabled=True,
                network_id="net-1",
                custom_domain="ai.acme.example",
                tls=TlsSource.ACME,
            )
        )

    async def sweep(self, session: Any, result: ProbeResult) -> list[Any]:
        from aisvc.certificates.probe import sweep_endpoint_certificates
        from aisvc.observability.alerts import CollectingSink

        sink = CollectingSink()
        await sweep_endpoint_certificates(session, sink=sink, probe=lambda host, **kw: result)
        return sink.alerts

    def untrusted(self) -> ProbeResult:
        return ProbeResult(
            host="ai.acme.example",
            port=443,
            reachable=True,
            trusted=False,
            names=("ai.acme.example",),
            not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=60),
        )

    def healthy(self) -> ProbeResult:
        return ProbeResult(
            host="ai.acme.example",
            port=443,
            reachable=True,
            trusted=True,
            names=("ai.acme.example",),
            not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=60),
        )

    async def test_an_untrusted_endpoint_alerts_once(self) -> None:
        session = self.session()
        self.exposure(session)

        first = await self.sweep(session, self.untrusted())
        second = await self.sweep(session, self.untrusted())

        assert first
        assert second == []

    async def test_it_alerts_again_after_it_clears_and_returns(self) -> None:
        """Otherwise a problem fixed and reintroduced passes unseen."""
        session = self.session()
        self.exposure(session)
        await self.sweep(session, self.untrusted())
        await self.sweep(session, self.healthy())

        assert await self.sweep(session, self.untrusted())

    async def test_an_unreachable_endpoint_alerts_once(self) -> None:
        session = self.session()
        self.exposure(session)
        down = ProbeResult(host="h", port=443, reachable=False, error="refused")

        assert await self.sweep(session, down)
        assert await self.sweep(session, down) == []

    async def test_recovery_is_not_announced_as_a_problem(self) -> None:
        session = self.session()
        self.exposure(session)
        await self.sweep(session, self.untrusted())

        assert await self.sweep(session, self.healthy()) == []

    async def test_several_thresholds_make_one_alert(self) -> None:
        session = self.session()
        self.exposure(session)
        soon = ProbeResult(
            host="ai.acme.example",
            port=443,
            reachable=True,
            trusted=True,
            names=("ai.acme.example",),
            not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=5),
        )

        alerts = await self.sweep(session, soon)

        assert len([a for a in alerts if "thresholds" in a.detail]) == 1
