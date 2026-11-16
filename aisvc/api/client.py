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
"""Talking to the engine API (SPEC.md section 10).

The operator CLI goes through this rather than reaching into the engine's
modules, so it exercises the same surface the Java plugin does — if a
response shape breaks the plugin, it breaks `aisvc` too.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["EngineClient", "EngineError"]


class EngineError(Exception):
    """The engine refused or could not answer."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class EngineClient:
    """A synchronous client for the operator CLI."""

    base_url: str
    user_id: str = "cli"
    account: str = "admin"
    domain: str = "/"
    role: str = "Admin"
    client_cert: tuple[str, str] | None = None
    ca_bundle: str | None = None
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> EngineClient | None:
        """Build a client if AISVC_ENGINE_URL is set, else None."""
        base = os.environ.get("AISVC_ENGINE_URL", "")
        if not base:
            return None
        cert = os.environ.get("AISVC_TLS_CERT", "")
        key = os.environ.get("AISVC_TLS_KEY", "")
        return cls(
            base_url=base.rstrip("/"),
            client_cert=(cert, key) if cert and key else None,
            ca_bundle=os.environ.get("AISVC_TLS_CA") or None,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Aisvc-User-Id": self.user_id,
            "X-Aisvc-Account": self.account,
            "X-Aisvc-Domain": self.domain,
            "X-Aisvc-Role": self.role,
        }

    def ssl_context(self) -> ssl.SSLContext | bool:
        """How to verify the engine, and what to present to it.

        An `ssl.SSLContext`, not httpx's `cert=`. That parameter is
        deprecated in httpx 0.28 and **silently sends nothing** — it is
        still in `Client.__init__`'s signature and raises only a
        DeprecationWarning, so `AISVC_TLS_CERT` and `AISVC_TLS_KEY`
        appeared to work while no client certificate was ever
        presented. The engine requires one, so every call failed at the
        handshake with "Server disconnected without sending a
        response", which names neither the certificate nor the cause.

        True where nothing is configured: an engine on loopback with
        TLS unset needs no certificate, and that path still has to
        work.
        """
        if not self.client_cert and not self.ca_bundle:
            return True

        context = (
            ssl.create_default_context(cafile=self.ca_bundle)
            if self.ca_bundle
            else ssl.create_default_context()
        )
        if self.client_cert:
            certfile, keyfile = self.client_cert
            context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        return context

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers=self.headers,
            timeout=self.timeout,
            verify=self.ssl_context(),
        )

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        with self._client() as client:
            try:
                response = client.get(path, params=params or None)
            except httpx.TransportError as exc:
                raise EngineError(f"cannot reach the engine: {exc}") from exc
        return self._unwrap(response)

    def post(self, path: str, **params: Any) -> dict[str, Any]:
        with self._client() as client:
            try:
                response = client.post(path, params=params or None)
            except httpx.TransportError as exc:
                raise EngineError(f"cannot reach the engine: {exc}") from exc
        return self._unwrap(response)

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            # RFC 9457 problem JSON, when the engine answered at all.
            try:
                problem = response.json()
                detail = problem.get("detail") or problem.get("title") or ""
            except ValueError:
                detail = response.text[:200]
            raise EngineError(
                f"engine returned {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        body: dict[str, Any] = response.json()
        return body
