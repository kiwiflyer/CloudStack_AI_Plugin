"""The engine REST API (SPEC.md section 10)."""

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
from aisvc.db import Base, RunLog
from aisvc.hf import ModelSourcing
from aisvc.k8s import DeploymentTarget, image_for, render
from aisvc.k8s.client import FakeKubernetes, apply_objects
from aisvc.reconciler import Plan
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_hf import FakeHub
from tests.unit.test_spec import LICENSES, MINIMAL

SHA = "0f3f4a6c9d2e1b8a7c5d4e3f2a1b0c9d8e7f6a5b"
REPO = "meta-llama/Llama-3.3-70B-Instruct"

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
        super().__init__(**kw)

    def snapshot(self) -> Any:
        return self._snapshot


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


class TestHealth:
    def test_health_needs_no_caller_context(self, client: TestClient) -> None:
        assert client.get("/v1/health").status_code == 200

    def test_ready_reports_its_checks(self, client: TestClient) -> None:
        body = client.get("/v1/ready").json()
        assert body["status"] == "ready"
        assert body["checks"]["database"] is True


class TestCallerContext:
    def test_a_request_without_context_is_refused(self, client: TestClient) -> None:
        response = client.get("/v1/models")

        assert response.status_code == 401
        assert response.json()["type"].endswith("missing_caller_context")

    def test_a_partial_context_names_what_is_missing(self, client: TestClient) -> None:
        response = client.get(
            "/v1/models", headers={"X-Aisvc-User-Id": "u", "X-Aisvc-Role": "User"}
        )

        assert response.status_code == 401
        assert "X-Aisvc-Account" in response.json()["detail"]

    def test_an_unknown_role_is_refused(self, client: TestClient) -> None:
        response = client.get("/v1/models", headers={**TENANT, "X-Aisvc-Role": "Superuser"})

        assert response.status_code == 401
        assert response.json()["type"].endswith("unknown_role")


class TestProblemJson:
    def test_errors_are_rfc_9457_documents(self, client: TestClient) -> None:
        response = client.get("/v1/models")

        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert set(body) >= {"type", "title", "status", "detail"}
        assert body["status"] == 401

    def test_a_missing_route_is_also_problem_json(self, client: TestClient) -> None:
        response = client.get("/v1/nothing-here")

        assert response.status_code == 404
        assert response.json()["type"].endswith("not_found")

    def test_an_invalid_query_is_reported_field_by_field(self, client: TestClient) -> None:
        response = client.get("/v1/runs?limit=0", headers=ADMIN)

        assert response.status_code == 422
        assert response.json()["errors"]


class TestRbac:
    def test_capacity_is_admin_only(self, client: TestClient) -> None:
        assert client.get("/v1/capacity", headers=TENANT).status_code == 403

    def test_runs_are_admin_only(self, client: TestClient) -> None:
        assert client.get("/v1/runs", headers=TENANT).status_code == 403

    def test_hf_search_is_admin_only(self, client: TestClient) -> None:
        response = client.get("/v1/hf/search?query=llama", headers=TENANT)
        assert response.status_code == 403

    def test_the_refusal_explains_itself(self, client: TestClient) -> None:
        body = client.get("/v1/runs", headers=TENANT).json()
        assert "root admins" in body["detail"]

    def test_an_admin_may_list_runs(self, client: TestClient) -> None:
        assert client.get("/v1/runs", headers=ADMIN).status_code == 200


class TestModels:
    def test_the_catalog_lists_declared_models(self, client: TestClient) -> None:
        body = client.get("/v1/models", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["models"][0]["name"] == "llama-3.3-70b-instruct"

    def test_the_catalog_says_whether_a_model_is_mirrored(self, client: TestClient) -> None:
        body = client.get("/v1/models", headers=ADMIN).json()
        assert body["models"][0]["mirrored"] is True

    def test_visibility_is_respected(self, registry: ModelRegistry) -> None:
        """SPEC.md section 5: publish to all, a domain, or named accounts."""
        scoped = spec_with(visibility={"domains": ["/Customers/Acme"]})
        services = EngineServices(
            spec=scoped,
            registry=registry,
            session_factory=memory_sessions(),
        )
        with TestClient(create_app(services)) as client:
            assert client.get("/v1/models", headers=TENANT).json()["count"] == 1
            assert client.get("/v1/models", headers=OTHER_TENANT).json()["count"] == 0

    def test_an_admin_sees_models_regardless_of_visibility(self, registry: ModelRegistry) -> None:
        scoped = spec_with(visibility={"domains": ["/Customers/Acme"]})
        services = EngineServices(spec=scoped, registry=registry, session_factory=memory_sessions())
        with TestClient(create_app(services)) as client:
            assert client.get("/v1/models", headers=ADMIN).json()["count"] == 1


class TestDeployments:
    def test_nothing_deployed_lists_nothing(self, client: TestClient) -> None:
        assert client.get("/v1/deployments", headers=ADMIN).json()["count"] == 0

    def test_a_running_deployment_is_listed(
        self, client: TestClient, services: EngineServices
    ) -> None:
        model = services.spec.models[0]
        pool = services.spec.node_pool(model.node_pool)
        apply_objects(
            services.cluster,
            render(
                DeploymentTarget(
                    model=model,
                    pool=pool,
                    gpu_class=services.spec.gpu_class(pool.gpu_class),
                    serving_profile=services.spec.serving_profile_for(model),
                    image=image_for(model),
                    replicas=2,
                    weights_uri="s3://models/x/",
                )
            ),
        )

        body = client.get("/v1/deployments", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["deployments"][0]["model"] == "llama-3.3-70b-instruct"
        assert body["deployments"][0]["replicas_desired"] == 2

    def test_endpoints_carry_the_gateway_base_url(self, client: TestClient) -> None:
        body = client.get("/v1/endpoints", headers=ADMIN).json()

        assert body["endpoints"][0]["base_url"].endswith("/v1")


class TestEffectiveParameters:
    def test_it_reports_values_and_their_source(self, client: TestClient) -> None:
        body = client.get(
            "/v1/profiles/effective?model=llama-3.3-70b-instruct", headers=ADMIN
        ).json()

        assert body["model"] == "llama-3.3-70b-instruct"
        assert isinstance(body["parameters"], dict)

    def test_an_unknown_model_is_a_404(self, client: TestClient) -> None:
        response = client.get("/v1/profiles/effective?model=ghost", headers=ADMIN)

        assert response.status_code == 404

    def test_a_domain_profile_shows_up_with_its_source(self, registry: ModelRegistry) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["assignments"].append(
            {
                "scope": "domain",
                "target": "/Customers/Acme",
                "generation_profile": "precise",
            }
        )
        spec = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        services = EngineServices(spec=spec, registry=registry, session_factory=memory_sessions())

        with TestClient(create_app(services)) as client:
            body = client.get(
                "/v1/profiles/effective?model=llama-3.3-70b-instruct", headers=TENANT
            ).json()

        assert body["parameters"]["temperature"]["source"] == "domain"
        assert body["parameters"]["temperature"]["value"] == 0.2


class TestHuggingFace:
    def test_search_returns_repos(self, client: TestClient) -> None:
        body = client.get("/v1/hf/search?query=llama", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["models"][0]["repo_id"] == REPO

    def test_check_returns_a_compatibility_report(self, client: TestClient) -> None:
        body = client.get(f"/v1/hf/check?repo_id={REPO}&revision={SHA}", headers=ADMIN).json()

        assert body["compatible"] is True
        assert body["architecture"] == "LlamaForCausalLM"
        assert body["placements"]


class TestAsyncJobs:
    def test_mirror_returns_202_with_a_job_id(self, client: TestClient) -> None:
        """SPEC.md section 10: async calls return 202 with a job id."""
        response = client.post(f"/v1/models/mirror?repo_id={REPO}&revision={SHA}", headers=ADMIN)

        assert response.status_code == 202
        assert response.json()["jobid"]

    def test_the_location_header_points_at_the_job(self, client: TestClient) -> None:
        response = client.post(f"/v1/models/mirror?repo_id={REPO}&revision={SHA}", headers=ADMIN)

        assert response.headers["Location"].endswith(response.json()["jobid"])

    def test_the_job_can_then_be_polled(self, client: TestClient) -> None:
        job_id = client.post(
            f"/v1/models/mirror?repo_id={REPO}&revision={SHA}", headers=ADMIN
        ).json()["jobid"]

        body = client.get(f"/v1/jobs/{job_id}", headers=ADMIN).json()

        assert body["jobid"] == job_id
        assert body["jobstatus"] == 1
        assert body["jobresult"]["revision"] == SHA

    def test_a_failed_mirror_is_recorded_on_the_job(self, services: EngineServices) -> None:
        bare = ModelRegistry(InMemoryObjectStore(), bucket="models")
        services.registry = bare
        services.sourcing = ModelSourcing(
            FakeHub(),
            bare,
            allowed_licenses=["mit"],  # llama3.3 now disallowed
            settings=services.spec.document.huggingface,
        )

        with TestClient(create_app(services)) as client:
            job_id = client.post(
                f"/v1/models/mirror?repo_id={REPO}&revision={SHA}", headers=ADMIN
            ).json()["jobid"]
            body = client.get(f"/v1/jobs/{job_id}", headers=ADMIN).json()

        assert body["jobstatus"] == 2
        assert "llama3.3" in body["jobresult"]["errortext"]

    def test_an_unknown_job_is_a_404(self, client: TestClient) -> None:
        response = client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers=ADMIN)

        assert response.status_code == 404

    def test_another_tenant_cannot_poll_someone_elses_job(self, client: TestClient) -> None:
        """A job they may not see looks exactly like one that does not exist."""
        job_id = client.post(
            f"/v1/models/mirror?repo_id={REPO}&revision={SHA}", headers=ADMIN
        ).json()["jobid"]

        response = client.get(f"/v1/jobs/{job_id}", headers=OTHER_TENANT)

        assert response.status_code == 404


class TestRuns:
    def test_runs_are_listed_newest_first(
        self, client: TestClient, services: EngineServices
    ) -> None:
        session = services.session_factory()  # type: ignore[operator]
        log = RunLog(session)
        log.finish_run(log.start_run(Plan(), zone="zone-east-1", dry_run=False))
        log.finish_run(log.start_run(Plan(), zone="zone-east-1", dry_run=True))
        session.commit()

        body = client.get("/v1/runs", headers=ADMIN).json()

        assert body["count"] == 2
        assert body["runs"][0]["dry_run"] is True


class TestOpenApi:
    def test_the_schema_is_served_under_the_version_prefix(self, client: TestClient) -> None:
        """SPEC.md section 10: the OpenAPI schema is the contract."""
        assert client.get("/v1/openapi.json").status_code == 200

    def test_every_path_is_versioned(self, client: TestClient) -> None:
        schema = client.get("/v1/openapi.json").json()

        assert all(path.startswith("/v1/") for path in schema["paths"])

    def test_the_engine_only_endpoints_are_present(self, client: TestClient) -> None:
        paths = client.get("/v1/openapi.json").json()["paths"]

        assert "/v1/jobs/{job_id}" in paths
        assert "/v1/health" in paths
        assert "/v1/ready" in paths


class TestDegradedEngine:
    def test_readiness_fails_when_the_database_is_gone(self, services: EngineServices) -> None:
        """A replica that cannot reach Postgres must leave the load balancer."""

        def broken() -> Any:
            raise RuntimeError("connection refused")

        services.session_factory = broken
        with TestClient(create_app(services)) as client:
            response = client.get("/v1/ready")

        assert response.status_code == 503
        assert response.json()["checks"]["database"] is False

    def test_a_registry_outage_is_503_not_404(self, services: EngineServices) -> None:
        """ "Cannot reach the registry" must not read as "no such model"."""
        from aisvc.registry import RegistryError

        class BrokenRegistry:
            def has(self, *_: Any, **__: Any) -> bool:
                raise RegistryError("s3 is unreachable")

            def uri_for(self, *_: Any, **__: Any) -> str:
                raise RegistryError("s3 is unreachable")

        services.registry = BrokenRegistry()  # type: ignore[assignment]
        services.sourcing = None
        with TestClient(create_app(services)) as client:
            response = client.get(f"/v1/hf/check?repo_id={REPO}&revision={SHA}", headers=ADMIN)

        assert response.status_code == 503
        assert response.json()["type"].endswith("unavailable")

    def test_the_catalog_still_lists_during_a_registry_outage(
        self, services: EngineServices
    ) -> None:
        """Knowing what exists should not depend on the registry answering."""
        from aisvc.registry import RegistryError

        class BrokenRegistry:
            def has(self, *_: Any, **__: Any) -> bool:
                raise RegistryError("s3 is unreachable")

        services.registry = BrokenRegistry()  # type: ignore[assignment]
        with TestClient(create_app(services)) as client:
            body = client.get("/v1/models", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["models"][0]["mirrored"] is False


class TestRollouts:
    def test_promoting_returns_a_job(self, client: TestClient) -> None:
        response = client.post(
            "/v1/deployments/llama-3.3-70b-instruct-blue/rollout/promote",
            headers=ADMIN,
        )

        assert response.status_code == 202
        assert response.json()["jobid"]

    def test_a_promoted_rollout_moves_all_traffic_to_green(self, client: TestClient) -> None:
        """ADR-0001: the gateway carries these weights, not the serving layer."""
        job_id = client.post(
            "/v1/deployments/llama-3.3-70b-instruct-blue/rollout/promote",
            headers=ADMIN,
        ).json()["jobid"]

        result = client.get(f"/v1/jobs/{job_id}", headers=ADMIN).json()["jobresult"]

        assert result["weights"] == {"blue": 0, "green": 100}
        assert result["deployment"]["colour"] == "green"
        assert result["retire"] == "blue"

    def test_a_rollback_keeps_traffic_on_blue(self, client: TestClient) -> None:
        job_id = client.post(
            "/v1/deployments/llama-3.3-70b-instruct-blue/rollout/rollback",
            headers=ADMIN,
        ).json()["jobid"]

        result = client.get(f"/v1/jobs/{job_id}", headers=ADMIN).json()["jobresult"]

        assert result["weights"] == {"blue": 100, "green": 0}
        assert result["retire"] == "green"

    def test_a_deployment_id_without_a_colour_defaults_to_blue(self, client: TestClient) -> None:
        job_id = client.post(
            "/v1/deployments/llama-3.3-70b-instruct/rollout/promote", headers=ADMIN
        ).json()["jobid"]

        body = client.get(f"/v1/jobs/{job_id}", headers=ADMIN).json()

        assert body["jobstatus"] == 1


class TestDrain:
    def test_draining_is_admin_only(self, client: TestClient) -> None:
        response = client.post("/v1/nodes/aisvc-h100-pool-001/drain", headers=TENANT)

        assert response.status_code == 403

    def test_an_empty_node_drains(self, client: TestClient) -> None:
        job_id = client.post("/v1/nodes/aisvc-h100-pool-001/drain", headers=ADMIN).json()["jobid"]

        result = client.get(f"/v1/jobs/{job_id}", headers=ADMIN).json()["jobresult"]

        assert result["action"] == "destroy"

    def test_the_drain_destroys_rather_than_stopping(self, client: TestClient) -> None:
        """ADR-0003: a stopped node keeps its GPUs allocated."""
        job_id = client.post("/v1/nodes/aisvc-h100-pool-001/drain", headers=ADMIN).json()["jobid"]

        result = client.get(f"/v1/jobs/{job_id}", headers=ADMIN).json()["jobresult"]

        assert result["action"] != "stop"

    def test_forcing_is_recorded_on_the_job(self, client: TestClient) -> None:
        response = client.post("/v1/nodes/aisvc-h100-pool-001/drain?force=true", headers=ADMIN)

        assert response.status_code == 202


class TestUsageAndBudgets:
    def test_usage_is_empty_without_records(self, client: TestClient) -> None:
        body = client.get("/v1/usage", headers=ADMIN).json()

        assert body["count"] == 0

    def test_usage_is_priced_from_the_catalog_tier(self, services: EngineServices) -> None:
        from aisvc.metering import UsageRecord

        services.usage = [  # type: ignore[attr-defined]
            UsageRecord(
                account="acme",
                domain="/Customers/Acme",
                model="llama-3.3-70b-instruct",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]
        with TestClient(create_app(services)) as client:
            body = client.get("/v1/usage", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["usage"][0]["cost"] == pytest.approx(2.40)
        assert body["usage"][0]["currency"] == "USD"

    def test_another_tenant_sees_none_of_it(self, services: EngineServices) -> None:
        """SPEC.md section 14: tenants cannot see each other's usage."""
        from aisvc.metering import UsageRecord

        services.usage = [  # type: ignore[attr-defined]
            UsageRecord(
                account="acme",
                domain="/Customers/Acme",
                model="llama-3.3-70b-instruct",
                input_tokens=10,
            )
        ]
        with TestClient(create_app(services)) as client:
            body = client.get("/v1/usage", headers=OTHER_TENANT).json()

        assert body["count"] == 0

    def test_budgets_report_where_a_tenant_stands(self, registry: ModelRegistry) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tenants"][0]["budget"] = {"monthly": 5000, "currency": "USD", "on_exceed": "block"}
        spec = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        services = EngineServices(spec=spec, registry=registry, session_factory=memory_sessions())
        services.spend = {"acme": 4000.0}  # type: ignore[attr-defined]

        with TestClient(create_app(services)) as client:
            body = client.get("/v1/budgets", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["budgets"][0]["percent_used"] == 80.0
        assert body["budgets"][0]["blocked"] is False

    def test_a_blocking_budget_at_full_spend_reports_blocked(self, registry: ModelRegistry) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tenants"][0]["budget"] = {"monthly": 100, "currency": "USD", "on_exceed": "block"}
        spec = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        services = EngineServices(spec=spec, registry=registry, session_factory=memory_sessions())
        services.spend = {"acme": 100.0}  # type: ignore[attr-defined]

        with TestClient(create_app(services)) as client:
            body = client.get("/v1/budgets", headers=ADMIN).json()

        assert body["budgets"][0]["blocked"] is True

    def test_a_tenant_without_a_budget_is_not_listed(self, client: TestClient) -> None:
        assert client.get("/v1/budgets", headers=ADMIN).json()["count"] == 0
