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
"""`aisvc status` against the engine API (SPEC.md section 10, milestone 4).

The CLI goes through the REST surface rather than the engine's modules,
so these drive it against a real app over an httpx transport.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from aisvc.api.app import create_app
from aisvc.api.client import EngineClient, EngineError
from aisvc.api.deps import Services
from aisvc.cli.main import app as cli
from aisvc.db import Base
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

runner = CliRunner()


@pytest.fixture
def engine_app() -> Any:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_app(
        Services(
            spec=load_spec_from_string(MINIMAL, allowed_licenses=LICENSES),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            session_factory=sessionmaker(engine),
        )
    )


@pytest.fixture
def client(engine_app: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[EngineClient]:
    """An EngineClient wired straight to the app, no socket involved.

    Starlette's TestClient is a sync httpx.Client that bridges to ASGI,
    which a bare ASGITransport cannot do from a synchronous client.
    """
    built = EngineClient(base_url="http://engine.invalid")

    def fake_client(self: EngineClient) -> Any:
        # TestClient subclasses httpx.Client and bridges to ASGI; the
        # return is Any because starlette does not re-export that fact.
        return TestClient(engine_app, base_url=self.base_url, headers=self.headers)

    monkeypatch.setattr(EngineClient, "_client", fake_client)
    yield built


class TestEngineClient:
    def test_it_sends_the_caller_context(self, client: EngineClient) -> None:
        body = client.get("/v1/models")
        assert body["count"] == 1

    def test_an_error_carries_the_problem_detail(self, client: EngineClient) -> None:
        with pytest.raises(EngineError) as exc:
            client.get("/v1/profiles/effective", model="ghost")

        assert exc.value.status_code == 404
        assert "ghost" in str(exc.value)

    def test_an_unreachable_engine_says_so(self) -> None:
        unreachable = EngineClient(base_url="http://127.0.0.1:1")

        with pytest.raises(EngineError, match="cannot reach"):
            unreachable.get("/v1/health")

    def test_from_env_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AISVC_ENGINE_URL", raising=False)
        assert EngineClient.from_env() is None

    def test_from_env_picks_up_client_certificates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AISVC_ENGINE_URL", "https://engine.internal:8443/")
        monkeypatch.setenv("AISVC_TLS_CERT", "/tmp/c.crt")
        monkeypatch.setenv("AISVC_TLS_KEY", "/tmp/c.key")

        built = EngineClient.from_env()

        assert built is not None
        assert built.base_url == "https://engine.internal:8443"
        assert built.client_cert == ("/tmp/c.crt", "/tmp/c.key")


class TestStatusCommand:
    def test_it_reports_the_catalog(
        self, client: EngineClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AISVC_ENGINE_URL", "http://engine.invalid")

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0, result.output
        assert "1 model(s)" in result.output

    def test_it_says_when_nothing_is_running(
        self, client: EngineClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AISVC_ENGINE_URL", "http://engine.invalid")

        result = runner.invoke(cli, ["status"])

        assert "none running" in result.output

    def test_without_an_engine_url_it_explains_what_to_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AISVC_ENGINE_URL", raising=False)

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 1
        assert "AISVC_ENGINE_URL" in result.output

    def test_an_unreachable_engine_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AISVC_ENGINE_URL", "http://127.0.0.1:1")

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 1
        assert "cannot reach" in result.output
