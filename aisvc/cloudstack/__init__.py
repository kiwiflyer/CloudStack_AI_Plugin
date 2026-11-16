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
"""Signed CloudStack API client with async job polling (SPEC.md section 4)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

__all__ = [
    "AsyncJobFailed",
    "CloudStackClient",
    "CloudStackError",
    "Credentials",
    "canonical_query",
    "sign",
]

#: CloudStack async job states.
JOB_PENDING = 0
JOB_SUCCESS = 1
JOB_FAILURE = 2


class CloudStackError(Exception):
    """The management server rejected a call, or answered unusably."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class AsyncJobFailed(CloudStackError):
    """An async job finished in the failure state."""

    def __init__(self, job_id: str, message: str, *, error_code: int | None = None) -> None:
        super().__init__(f"job {job_id} failed: {message}", error_code=error_code)
        self.job_id = job_id


@dataclass(frozen=True, slots=True)
class Credentials:
    endpoint: str
    api_key: str
    secret_key: str


def _render(value: object) -> str | None:
    """Render one parameter value the way CloudStack expects it."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return ",".join(str(item) for item in value)
    return str(value)


def canonical_query(params: Mapping[str, object]) -> str:
    """The string CloudStack signs: sorted, URL-encoded, wholly lower-cased.

    `signature` itself is never part of it, and unset values are dropped.
    """
    pairs: list[tuple[str, str]] = []
    for key, raw in params.items():
        if key.lower() == "signature":
            continue
        rendered = _render(raw)
        if rendered is None:
            continue
        # `quote`, not `quote_plus`: a space must be %20. CloudStack's
        # servlet decodes a `+` back to a space and re-encodes it as
        # %20 when it rebuilds the string to verify, so a `+` here can
        # never match whatever we send. Found against a real 4.23.0.0
        # server, where every call carrying a space in a value was
        # rejected as an invalid signature.
        pairs.append((key.lower(), quote(rendered, safe="")))
    pairs.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in pairs).lower()


def sign(params: Mapping[str, object], secret_key: str) -> str:
    """Base64 HMAC-SHA1 of the canonical query."""
    digest = hmac.new(
        secret_key.encode("utf-8"),
        canonical_query(params).encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


class CloudStackClient:
    """Async CloudStack API client.

    Retries transport errors and 5xx responses with exponential backoff;
    4xx responses carry a CloudStack error and are returned to the caller
    immediately.
    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        poll_interval: float = 2.0,
        job_timeout: float = 1800.0,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool | str = True,
    ) -> None:
        self.credentials = credentials
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.poll_interval = poll_interval
        self.job_timeout = job_timeout
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            verify=verify,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- request plumbing -------------------------------------------------

    def _signed_params(self, command: str, params: Mapping[str, object]) -> dict[str, str]:
        base: dict[str, object] = {
            **params,
            "command": command,
            "apikey": self.credentials.api_key,
            "response": "json",
        }
        rendered = {key: value for key, raw in base.items() if (value := _render(raw)) is not None}
        rendered["signature"] = sign(rendered, self.credentials.secret_key)
        return rendered

    @staticmethod
    def _unwrap(command: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CloudStackError(
                f"{command}: expected a JSON object, got {type(payload).__name__}"
            )
        key = f"{command.lower()}response"
        body = payload.get(key)
        if body is None and len(payload) == 1:
            body = next(iter(payload.values()))
        if not isinstance(body, dict):
            raise CloudStackError(f"{command}: no {key!r} object in the response")
        return body

    @staticmethod
    def _raise_for_api_error(command: str, body: Mapping[str, Any]) -> None:
        if "errorcode" in body:
            raise CloudStackError(
                f"{command}: {body.get('errortext', 'unknown error')}",
                error_code=int(body["errorcode"]),
            )

    async def call(self, command: str, **params: object) -> dict[str, Any]:
        """Make one signed call and return its unwrapped response body."""
        query = self._signed_params(command, params)
        last: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(self.credentials.endpoint, params=query)
            except httpx.TransportError as exc:
                last = exc
            else:
                if response.status_code < 400:
                    return self._unwrap(command, response.json())

                # CloudStack answers API errors with its own status codes,
                # several of which are 5xx (530, 531, 533, 534...). A body
                # carrying errorcode/errortext is a definitive answer, so it
                # is raised rather than retried; only a bare 5xx with no
                # CloudStack error in it is treated as transient.
                body = self._error_body(command, response)
                self._raise_for_api_error(command, body)

                if response.status_code < 500:
                    raise CloudStackError(
                        f"{command}: HTTP {response.status_code}",
                        error_code=response.status_code,
                    )

                last = CloudStackError(
                    f"{command}: HTTP {response.status_code}",
                    error_code=response.status_code,
                )

            if attempt < self.max_retries:
                await asyncio.sleep(self.backoff_base * (2**attempt))

        raise CloudStackError(
            f"{command}: giving up after {self.max_retries} retries ({last})"
        ) from last

    @staticmethod
    def _error_body(command: str, response: httpx.Response) -> dict[str, Any]:
        try:
            return CloudStackClient._unwrap(command, response.json())
        except (ValueError, CloudStackError):
            return {}

    async def call_paged(
        self,
        command: str,
        item_key: str,
        *,
        page_size: int = 500,
        **params: object,
    ) -> list[dict[str, Any]]:
        """Follow `page`/`pagesize` until the server runs out of items."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self.call(command, page=page, pagesize=page_size, **params)
            batch = body.get(item_key) or []
            if not isinstance(batch, list):
                raise CloudStackError(f"{command}: {item_key!r} is not a list")
            items.extend(batch)
            if len(batch) < page_size:
                return items
            page += 1

    # -- async jobs -------------------------------------------------------

    async def wait_job(self, job_id: str, *, job_timeout: float | None = None) -> dict[str, Any]:
        """Poll `queryAsyncJobResult` until the job leaves the pending state."""
        deadline = time.monotonic() + (job_timeout if job_timeout is not None else self.job_timeout)

        while True:
            body = await self.call("queryAsyncJobResult", jobid=job_id)
            status = int(body.get("jobstatus", JOB_PENDING))

            if status == JOB_SUCCESS:
                result = body.get("jobresult")
                return result if isinstance(result, dict) else {}

            if status == JOB_FAILURE:
                result = body.get("jobresult") or {}
                raise AsyncJobFailed(
                    job_id,
                    str(result.get("errortext", "no error text")),
                    error_code=result.get("errorcode"),
                )

            if time.monotonic() >= deadline:
                raise TimeoutError(f"async job {job_id} did not finish in time")

            await asyncio.sleep(self.poll_interval)

    async def call_async(
        self, command: str, *, job_timeout: float | None = None, **params: object
    ) -> dict[str, Any]:
        """Make a call that returns a job id, then wait for the job."""
        body = await self.call(command, **params)
        job_id = body.get("jobid")
        if not job_id:
            raise CloudStackError(f"{command}: response carried no jobid")
        return await self.wait_job(str(job_id), job_timeout=job_timeout)
