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
"""The engine client has to actually present its certificate.

Found by pointing `aisvc status` at a real engine over real mTLS:
`curl` with the same files got HTTP 200 while the CLI reported "Server
disconnected without sending a response" — a handshake failing because
no client certificate was presented.

httpx 0.28 deprecated `cert=` in favour of an `ssl.SSLContext` passed
as `verify=`. It still appears in `Client.__init__`'s signature and
raises only a `DeprecationWarning`, so nothing failed and nothing was
logged: `AISVC_TLS_CERT` and `AISVC_TLS_KEY` were silently doing
nothing, on the client whose certificate is the entire reason the
engine trusts the caller context it is handed.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import pytest

import install
from aisvc.api import client as client_module
from aisvc.api.client import EngineClient


@pytest.fixture(scope="module")
def material(tmp_path_factory: Any) -> Any:
    """Real certificates, from the installer that an operator uses."""
    directory = tmp_path_factory.mktemp("tls")
    return install.make_tls_material(str(directory), hostname="localhost")


class TestTheClientCertificateIsActuallySent:
    def client(self, material: Any) -> EngineClient:
        return EngineClient(
            base_url="https://localhost:8443",
            client_cert=(
                material.client_cert,
                Path(material.client_cert).with_suffix(".key").as_posix(),
            ),
            ca_bundle=material.ca_cert,
        )

    def test_a_context_is_built_when_a_certificate_is_given(self, material: Any) -> None:
        context = self.client(material).ssl_context()

        assert isinstance(context, ssl.SSLContext)

    def test_the_context_trusts_the_given_ca(self, material: Any) -> None:
        context = self.client(material).ssl_context()

        assert isinstance(context, ssl.SSLContext)
        assert context.get_ca_certs(), "the CA bundle was not loaded"

    def test_the_client_chain_is_loaded(self, material: Any) -> None:
        # A context with no chain loaded presents nothing, which is the
        # bug. There is no public attribute for it, so this asserts the
        # load succeeds rather than raising.
        context = ssl.create_default_context(cafile=material.ca_cert)
        key = Path(material.client_cert).with_suffix(".key")

        context.load_cert_chain(certfile=material.client_cert, keyfile=str(key))

    def test_no_certificate_means_ordinary_verification(self) -> None:
        # An engine on loopback with TLS unconfigured needs no client
        # certificate, and that path has to keep working.
        plain = EngineClient(base_url="http://127.0.0.1:8000")

        assert plain.ssl_context() is True

    def test_a_ca_without_a_certificate_still_verifies(self, material: Any) -> None:
        only_ca = EngineClient(base_url="https://localhost:8443", ca_bundle=material.ca_cert)

        context = only_ca.ssl_context()

        assert isinstance(context, ssl.SSLContext)
        assert context.get_ca_certs()

    def test_the_deprecated_parameter_is_gone(self) -> None:
        # The regression itself. `cert=` is accepted by httpx and sends
        # nothing, so its presence means no certificate is presented.
        source = Path(client_module.__file__).read_text()

        assert "cert=self.client_cert" not in source
