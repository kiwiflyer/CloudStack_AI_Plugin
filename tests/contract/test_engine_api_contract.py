"""The engine API against its own OpenAPI schema (SPEC.md sections 10, 12).

"The section 9 command map is the authoritative list of endpoints, and
the FastAPI-generated OpenAPI schema is the contract." These tests hold
the schema to the conventions the plugin relies on, so a change that
would break the Java client fails here rather than in integration.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from aisvc.api.app import API_VERSION, create_app
from aisvc.api.deps import Services
from aisvc.db import Base
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

ADMIN = {
    "X-Aisvc-User-Id": "root",
    "X-Aisvc-Account": "admin",
    "X-Aisvc-Domain": "/",
    "X-Aisvc-Role": "Admin",
}

#: Endpoints SPEC.md section 10 lists as engine-only, plus the v1
#: commands whose modules exist today. Milestones 5 and 7-9 add the rest.
REQUIRED_PATHS = {
    "/v1/health",
    "/v1/ready",
    "/v1/jobs/{job_id}",
    "/v1/models",
    "/v1/models/mirror",
    "/v1/deployments",
    "/v1/endpoints",
    "/v1/capacity",
    "/v1/runs",
    "/v1/profiles/effective",
    "/v1/hf/search",
    "/v1/hf/check",
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    services = Services(
        spec=load_spec_from_string(MINIMAL, allowed_licenses=LICENSES),
        registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
        session_factory=sessionmaker(engine),
    )
    with TestClient(create_app(services)) as c:
        yield c


@pytest.fixture
def schema(client: TestClient) -> dict[str, Any]:
    response = client.get(f"/{API_VERSION}/openapi.json")
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


class TestSchemaShape:
    def test_the_schema_is_valid_openapi_3(self, schema: dict[str, Any]) -> None:
        assert schema["openapi"].startswith("3.")
        assert schema["info"]["title"]
        assert schema["info"]["version"]

    def test_every_path_is_under_the_version_prefix(self, schema: dict[str, Any]) -> None:
        """Breaking changes require a new version prefix, so everything is in one."""
        assert all(path.startswith(f"/{API_VERSION}/") for path in schema["paths"])

    def test_the_commands_with_modules_today_are_all_present(self, schema: dict[str, Any]) -> None:
        missing = REQUIRED_PATHS - set(schema["paths"])
        assert not missing, f"missing endpoints: {sorted(missing)}"

    def test_every_operation_has_a_summary(self, schema: dict[str, Any]) -> None:
        """The summary is what the generated Java client's method is named from."""
        missing = [
            f"{method.upper()} {path}"
            for path, operations in schema["paths"].items()
            for method, operation in operations.items()
            if not operation.get("summary")
        ]
        assert not missing, f"operations without a summary: {missing}"

    def test_every_operation_is_tagged(self, schema: dict[str, Any]) -> None:
        untagged = [
            f"{method.upper()} {path}"
            for path, operations in schema["paths"].items()
            for method, operation in operations.items()
            if not operation.get("tags")
        ]
        assert not untagged, f"untagged operations: {untagged}"


class TestCallerContextIsDocumented:
    @pytest.mark.parametrize("path", ["/v1/models", "/v1/deployments", "/v1/runs", "/v1/endpoints"])
    def test_the_caller_headers_are_declared(self, schema: dict[str, Any], path: str) -> None:
        """The plugin generates its client from this; the headers must be in it."""
        parameters = schema["paths"][path]["get"].get("parameters", [])
        headers = {p["name"] for p in parameters if p.get("in") == "header"}
        assert {
            "X-Aisvc-User-Id",
            "X-Aisvc-Account",
            "X-Aisvc-Domain",
            "X-Aisvc-Role",
        } <= headers

    def test_health_does_not_require_caller_context(self, schema: dict[str, Any]) -> None:
        """Load balancers probe it; they have no CloudStack session."""
        parameters = schema["paths"]["/v1/health"]["get"].get("parameters", [])
        assert parameters == []


class TestAsyncConvention:
    def test_async_commands_declare_202(self, schema: dict[str, Any]) -> None:
        """SPEC.md section 10: async calls return 202 with a job id."""
        responses = schema["paths"]["/v1/models/mirror"]["post"]["responses"]
        assert "202" in responses

    def test_async_commands_do_not_also_declare_200(self, schema: dict[str, Any]) -> None:
        responses = schema["paths"]["/v1/models/mirror"]["post"]["responses"]
        assert "200" not in responses


class TestLiveResponsesMatchTheSchema:
    @pytest.mark.parametrize(
        "path",
        ["/v1/models", "/v1/deployments", "/v1/endpoints", "/v1/runs"],
    )
    def test_documented_endpoints_answer(self, client: TestClient, path: str) -> None:
        response = client.get(path, headers=ADMIN)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")

    def test_health_and_ready_answer_without_headers(self, client: TestClient) -> None:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/ready").status_code == 200


class TestErrorContract:
    @pytest.mark.parametrize(
        ("path", "headers", "expected"),
        [
            ("/v1/models", {}, 401),
            ("/v1/runs", {**ADMIN, "X-Aisvc-Role": "User"}, 403),
            ("/v1/jobs/does-not-exist", ADMIN, 404),
            ("/v1/profiles/effective?model=ghost", ADMIN, 404),
        ],
    )
    def test_errors_are_problem_json_with_the_right_status(
        self, client: TestClient, path: str, headers: dict[str, str], expected: int
    ) -> None:
        response = client.get(path, headers=headers)

        assert response.status_code == expected
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["status"] == expected
        assert body["type"].startswith("https://")
        assert body["detail"]

    def test_a_problem_type_is_stable_and_machine_readable(self, client: TestClient) -> None:
        body = client.get("/v1/models").json()
        assert body["type"].endswith("/missing_caller_context")
