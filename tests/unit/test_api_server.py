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
"""mTLS configuration for the engine API (SPEC.md sections 10 and 11)."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from aisvc.api.server import TlsConfig, TlsConfigError, uvicorn_options


@pytest.fixture
def certs(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "cert": tmp_path / "engine.crt",
        "key": tmp_path / "engine.key",
        "ca": tmp_path / "plugin-ca.crt",
    }
    for path in paths.values():
        path.write_text("not a real certificate", encoding="utf-8")
    return paths


class TestFromEnv:
    def test_nothing_set_means_no_tls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("AISVC_TLS_CERT", "AISVC_TLS_KEY", "AISVC_TLS_CLIENT_CA"):
            monkeypatch.delenv(name, raising=False)

        assert TlsConfig.from_env() is None

    def test_all_three_set_gives_a_config(
        self, monkeypatch: pytest.MonkeyPatch, certs: dict[str, Path]
    ) -> None:
        monkeypatch.setenv("AISVC_TLS_CERT", str(certs["cert"]))
        monkeypatch.setenv("AISVC_TLS_KEY", str(certs["key"]))
        monkeypatch.setenv("AISVC_TLS_CLIENT_CA", str(certs["ca"]))

        config = TlsConfig.from_env()

        assert config is not None
        assert config.client_ca == certs["ca"]

    def test_a_partial_configuration_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, certs: dict[str, Path]
    ) -> None:
        """Serving TLS without a client CA verifies nobody."""
        monkeypatch.setenv("AISVC_TLS_CERT", str(certs["cert"]))
        monkeypatch.setenv("AISVC_TLS_KEY", str(certs["key"]))
        monkeypatch.delenv("AISVC_TLS_CLIENT_CA", raising=False)

        with pytest.raises(TlsConfigError, match="AISVC_TLS_CLIENT_CA"):
            TlsConfig.from_env()


class TestValidate:
    def test_existing_files_validate(self, certs: dict[str, Path]) -> None:
        TlsConfig(certfile=certs["cert"], keyfile=certs["key"], client_ca=certs["ca"]).validate()

    def test_a_missing_file_fails_before_binding(
        self, certs: dict[str, Path], tmp_path: Path
    ) -> None:
        config = TlsConfig(
            certfile=certs["cert"],
            keyfile=certs["key"],
            client_ca=tmp_path / "absent.crt",
        )

        with pytest.raises(TlsConfigError, match="client CA"):
            config.validate()


class TestUvicornOptions:
    def test_client_certificates_are_required_not_optional(self, certs: dict[str, Path]) -> None:
        """The engine trusts asserted caller context; mTLS is what earns it."""
        options = uvicorn_options(
            TlsConfig(certfile=certs["cert"], keyfile=certs["key"], client_ca=certs["ca"]),
            host="0.0.0.0",
            port=8443,
        )

        assert options["ssl_cert_reqs"] == ssl.CERT_REQUIRED
        assert options["ssl_ca_certs"] == str(certs["ca"])

    def test_without_tls_the_engine_binds_loopback_only(self) -> None:
        """An engine on every interface without mTLS is what section 11 forbids."""
        options = uvicorn_options(None, host="0.0.0.0", port=8443)

        assert options["host"] == "127.0.0.1"
        assert "ssl_certfile" not in options

    def test_the_requested_host_is_used_when_tls_is_configured(
        self, certs: dict[str, Path]
    ) -> None:
        options = uvicorn_options(
            TlsConfig(certfile=certs["cert"], keyfile=certs["key"], client_ca=certs["ca"]),
            host="0.0.0.0",
            port=9443,
        )

        assert options["host"] == "0.0.0.0"
        assert options["port"] == 9443

    def test_missing_files_fail_before_any_port_is_bound(self, tmp_path: Path) -> None:
        config = TlsConfig(
            certfile=tmp_path / "a", keyfile=tmp_path / "b", client_ca=tmp_path / "c"
        )

        with pytest.raises(TlsConfigError):
            uvicorn_options(config, host="0.0.0.0", port=8443)
