"""Talking to the engine API (SPEC.md section 10).

The operator CLI goes through this rather than reaching into the engine's
modules, so it exercises the same surface the Java plugin does — if a
response shape breaks the plugin, it breaks `aisvc` too.
"""

from __future__ import annotations

import os
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

    def _client(self) -> httpx.Client:
        verify: Any = self.ca_bundle if self.ca_bundle else True
        return httpx.Client(
            base_url=self.base_url,
            headers=self.headers,
            timeout=self.timeout,
            cert=self.client_cert,
            verify=verify,
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
