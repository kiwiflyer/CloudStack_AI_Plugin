"""Fixtures shared across the unit tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from aisvc.api.app import create_app
from aisvc.api.deps import Services
from aisvc.db import Base, KeyStore, UsageStore
from aisvc.hf import ModelSourcing
from aisvc.k8s.client import FakeKubernetes
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_hf import FakeHub
from tests.unit.test_spec import LICENSES, MINIMAL

SHA = "0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b"
REPO = "meta-llama/Llama-3.3-70B-Instruct"


@pytest.fixture
def keys() -> Iterator[KeyStore]:
    """A KeyStore over in-memory SQLite, for anything that issues keys."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        yield KeyStore(session)


@pytest.fixture
def usage_store() -> Iterator[UsageStore]:
    """A UsageStore over in-memory SQLite."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        yield UsageStore(session)


ADMIN = {
    "X-Aisvc-User-Id": "root",
    "X-Aisvc-Account": "admin",
    "X-Aisvc-Domain": "/",
    "X-Aisvc-Role": "Admin",
}
TENANT = {
    "X-Aisvc-User-Id": "u-1",
    "X-Aisvc-Account": "acme",
    "X-Aisvc-Domain": "/Customers/Acme",
    "X-Aisvc-Role": "User",
}
OTHER_TENANT = {
    "X-Aisvc-User-Id": "u-2",
    "X-Aisvc-Account": "other",
    "X-Aisvc-Domain": "/Customers/Other",
    "X-Aisvc-Role": "User",
}


def memory_sessions() -> Any:
    """One shared in-memory database across TestClient's worker threads."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


def spec_with(**model_changes: Any) -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    if model_changes:
        doc["models"][0].update(model_changes)
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


class EngineServices(Services):
    """Services with the extras the handlers look for."""

    def __init__(self, **kw: Any) -> None:
        self.cluster = kw.pop("cluster", None)
        self.gateway_url = kw.pop("gateway_url", "https://ai.example.invalid")
        self._snapshot = kw.pop("snapshot", None)
        self._networks = kw.pop("networks", ("net-1", "net-2"))
        super().__init__(**kw)

    def snapshot(self) -> Any:
        return self._snapshot

    def networks_for(self, account: str, domain: str) -> Any:
        """Which VPC tiers this caller owns.

        Supplied by the plugin in production: the engine has no view of
        a tenant's networks and must not guess at one.
        """
        return self._networks


@pytest.fixture
def registry() -> ModelRegistry:
    registry = ModelRegistry(InMemoryObjectStore(), bucket="models")
    entry = registry.put_file(REPO, SHA, "config.json", b"{}")
    registry.write_manifest(REPO, SHA, [entry], license="llama3.3", task="chat")
    return registry


@pytest.fixture
def services(registry: ModelRegistry) -> EngineServices:
    spec = spec_with()
    return EngineServices(
        spec=spec,
        registry=registry,
        session_factory=memory_sessions(),
        sourcing=ModelSourcing(
            FakeHub(),
            registry,
            allowed_licenses=LICENSES,
            settings=spec.document.huggingface,
        ),
        cluster=FakeKubernetes(),
    )


@pytest.fixture
def client(services: EngineServices) -> Iterator[TestClient]:
    with TestClient(create_app(services)) as c:
        yield c
