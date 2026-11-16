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
"""Shared fixtures: a CloudStack client backed by recorded responses."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from aisvc.cloudstack import CloudStackClient, Credentials

FIXTURES = Path(__file__).parent / "fixtures" / "cloudstack"

CREDENTIALS = Credentials(
    endpoint="https://cloud.example.com/client/api",
    api_key="apikey-123",
    secret_key="secret-456",
)


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def recorded_handler(
    overrides: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Serve recorded responses, keyed by the `command` query parameter."""
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(httpx.QueryParams(request.url.query.decode()))
        command = params["command"]
        key = command.lower()
        body = overrides.get(command) or overrides.get(key)
        if body is None:
            body = load_fixture(key)
        return httpx.Response(200, json={f"{key}response": body})

    return handler


@pytest.fixture
def recorded_client() -> Iterator[CloudStackClient]:
    """A client whose calls are answered from tests/fixtures/cloudstack."""
    client = CloudStackClient(
        CREDENTIALS,
        transport=httpx.MockTransport(recorded_handler()),
        poll_interval=0.0,
        backoff_base=0.0,
    )
    yield client
