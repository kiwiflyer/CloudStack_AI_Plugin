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
"""Running the engine API (SPEC.md sections 7, 10 and 11).

"Engine and plugin communicate over mTLS; the engine is not reachable
from tenant networks." The engine trusts the caller context the plugin
asserts in headers, so that trust has to be established by the
transport: client certificates are *required*, not optional, and a
misconfiguration must fail to start rather than quietly serve plaintext.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TlsConfig", "TlsConfigError", "run", "uvicorn_options"]


class TlsConfigError(Exception):
    """mTLS is misconfigured; the engine must not start."""


@dataclass(frozen=True, slots=True)
class TlsConfig:
    """Paths to the engine's certificate and the CA that signs clients."""

    certfile: Path
    keyfile: Path
    #: The CA that signed the plugin's client certificates. Without it
    #: there is nothing to verify callers against.
    client_ca: Path

    @classmethod
    def from_env(cls) -> TlsConfig | None:
        """Read mTLS paths from the environment, or None if unset."""
        cert = os.environ.get("AISVC_TLS_CERT", "")
        key = os.environ.get("AISVC_TLS_KEY", "")
        ca = os.environ.get("AISVC_TLS_CLIENT_CA", "")
        if not any((cert, key, ca)):
            return None
        if not all((cert, key, ca)):
            missing = [
                name
                for name, value in (
                    ("AISVC_TLS_CERT", cert),
                    ("AISVC_TLS_KEY", key),
                    ("AISVC_TLS_CLIENT_CA", ca),
                )
                if not value
            ]
            raise TlsConfigError(
                "mTLS is partly configured, which would serve without verifying "
                "callers; set " + ", ".join(missing)
            )
        return cls(certfile=Path(cert), keyfile=Path(key), client_ca=Path(ca))

    def validate(self) -> None:
        """Fail before binding a port, not on the first request."""
        for label, path in (
            ("certificate", self.certfile),
            ("key", self.keyfile),
            ("client CA", self.client_ca),
        ):
            if not path.is_file():
                raise TlsConfigError(f"TLS {label} not found at {path}")


def uvicorn_options(tls: TlsConfig | None, *, host: str, port: int) -> dict[str, object]:
    """Uvicorn keyword arguments for this TLS configuration.

    With no TLS config the engine binds loopback only: an engine
    reachable on every interface without mTLS is exactly what SPEC.md
    section 11 forbids.
    """
    if tls is None:
        return {"host": "127.0.0.1", "port": port}

    tls.validate()
    return {
        "host": host,
        "port": port,
        "ssl_certfile": str(tls.certfile),
        "ssl_keyfile": str(tls.keyfile),
        "ssl_ca_certs": str(tls.client_ca),
        # CERT_REQUIRED, not CERT_OPTIONAL: an unauthenticated caller
        # could otherwise assert any caller context it liked.
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }


def run(host: str = "0.0.0.0", port: int = 8443) -> None:  # pragma: no cover
    """Serve the engine API. Entry point for the container."""
    import uvicorn

    from aisvc.api.bootstrap import build_services

    app_options = uvicorn_options(TlsConfig.from_env(), host=host, port=port)
    uvicorn.run(create_configured_app(build_services()), **app_options)  # type: ignore[arg-type]


def create_configured_app(services: object) -> object:  # pragma: no cover
    from aisvc.api.app import create_app

    return create_app(services)  # type: ignore[arg-type]
