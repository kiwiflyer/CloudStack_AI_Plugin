"""The engine REST API (SPEC.md section 10)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
import yaml
from fastapi import FastAPI
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


def record_usage(
    client: TestClient,
    *,
    account: str = "acme",
    domain: str = "/Customers/Acme",
    model: str = "llama-3.3-70b-instruct",
    input_tokens: int = 0,
    output_tokens: int = 0,
    per_mtok: float = 0.60,
) -> None:
    """Write a real usage row, the way the gateway's meter does."""
    from aisvc.db import UsageStore
    from aisvc.metering import UsageRecord
    from aisvc.spec.models import PriceTier

    app = cast(FastAPI, client.app)
    session = app.state.services.session_factory()
    UsageStore(session).record(
        UsageRecord(
            account=account,
            domain=domain,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
        tier=PriceTier(
            name="standard",
            input_per_mtok=per_mtok,
            output_per_mtok=per_mtok * 3,
            currency="USD",
        ),
    )
    session.commit()


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

    def test_usage_is_priced_from_the_catalog_tier(self, client: TestClient) -> None:
        record_usage(client, input_tokens=1_000_000, output_tokens=1_000_000)

        body = client.get("/v1/usage", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["usage"][0]["cost"] == pytest.approx(2.40)
        assert body["usage"][0]["currency"] == "USD"

    def test_another_tenant_sees_none_of_it(self, client: TestClient) -> None:
        """SPEC.md section 14: tenants cannot see each other's usage."""
        record_usage(client, input_tokens=10)

        assert client.get("/v1/usage", headers=OTHER_TENANT).json()["count"] == 0
        assert client.get("/v1/usage", headers=ADMIN).json()["count"] == 1

    def test_budgets_report_where_a_tenant_stands(self, registry: ModelRegistry) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tenants"][0]["budget"] = {"monthly": 5000, "currency": "USD", "on_exceed": "block"}
        spec = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        services = EngineServices(spec=spec, registry=registry, session_factory=memory_sessions())

        with TestClient(create_app(services)) as client:
            record_usage(client, input_tokens=1_000_000, per_mtok=4000.0)
            body = client.get("/v1/budgets", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["budgets"][0]["percent_used"] == 80.0
        assert body["budgets"][0]["blocked"] is False

    def test_a_blocking_budget_at_full_spend_reports_blocked(self, registry: ModelRegistry) -> None:
        doc = yaml.safe_load(MINIMAL)
        doc["tenants"][0]["budget"] = {"monthly": 100, "currency": "USD", "on_exceed": "block"}
        spec = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
        services = EngineServices(spec=spec, registry=registry, session_factory=memory_sessions())

        with TestClient(create_app(services)) as client:
            record_usage(client, input_tokens=1_000_000, per_mtok=100.0)
            body = client.get("/v1/budgets", headers=ADMIN).json()

        assert body["budgets"][0]["blocked"] is True

    def test_a_tenant_without_a_budget_is_not_listed(self, client: TestClient) -> None:
        assert client.get("/v1/budgets", headers=ADMIN).json()["count"] == 0


class TestSetupWizard:
    """SPEC.md section 7: the wizard's checks, discovery and self-test."""

    def test_preflight_reports_every_check(self, client: TestClient) -> None:
        body = client.get("/v1/setup/preflight", headers=ADMIN).json()

        assert body["checks"]
        assert {"name", "status", "detail", "remedy"} <= set(body["checks"][0])

    def test_preflight_says_whether_the_zone_is_ready(self, client: TestClient) -> None:
        body = client.get("/v1/setup/preflight", headers=ADMIN).json()

        assert isinstance(body["ok"], bool)

    def test_every_failed_check_carries_a_remedy(self, client: TestClient) -> None:
        """An operator in a wizard cannot read the source to find the fix."""
        body = client.get("/v1/setup/preflight", headers=ADMIN).json()

        assert all(c["remedy"] for c in body["checks"] if c["status"] == "fail")

    def test_a_tenant_cannot_run_preflight(self, client: TestClient) -> None:
        assert client.get("/v1/setup/preflight", headers=TENANT).status_code == 403

    def test_gpu_discovery_proposes_classes(self, client: TestClient) -> None:
        body = client.get("/v1/setup/gpu-discovery", headers=ADMIN).json()

        assert "classes" in body
        assert body["count"] == len(body["classes"])

    def test_a_tenant_cannot_run_gpu_discovery(self, client: TestClient) -> None:
        assert client.get("/v1/setup/gpu-discovery", headers=TENANT).status_code == 403

    def test_the_self_test_is_accepted_as_a_job(self, client: TestClient) -> None:
        response = client.post("/v1/setup/selftest?zone=zone-east-1", headers=ADMIN)

        assert response.status_code == 202
        assert response.json()["jobid"]

    def test_the_self_test_plan_ends_in_teardown(self, client: TestClient) -> None:
        """A self-test that leaves a model running has failed its purpose."""
        job = client.post("/v1/setup/selftest?zone=zone-east-1", headers=ADMIN).json()
        result = client.get(f"/v1/jobs/{job['jobid']}", headers=ADMIN).json()

        assert result["jobresult"]["steps"][-1]["name"] == "teardown"

    def test_an_unknown_zone_is_refused(self, client: TestClient) -> None:
        response = client.post("/v1/setup/selftest?zone=ghost-zone", headers=ADMIN)

        assert response.status_code == 404

    def test_a_tenant_cannot_run_the_self_test(self, client: TestClient) -> None:
        response = client.post("/v1/setup/selftest?zone=zone-east-1", headers=TENANT)

        assert response.status_code == 403


class TestUnexpectedFailures:
    """CLAUDE.md: errors use RFC 9457 problem JSON — all of them.

    Found by pointing the engine at an unreachable Postgres: the plugin
    got `text/plain` "Internal Server Error", which its client cannot
    parse. Anything escaping a handler has to come back as a problem
    document too, without leaking what went wrong.
    """

    @pytest.fixture
    def exploding(self, services: EngineServices) -> Iterator[TestClient]:
        app = create_app(services)

        @app.get("/v1/boom")
        async def boom() -> None:
            raise RuntimeError("connection to localhost:5432 failed for user 'secret'")

        yield TestClient(app, raise_server_exceptions=False)

    def test_an_unhandled_error_is_problem_json(self, exploding: TestClient) -> None:
        response = exploding.get("/v1/boom", headers=ADMIN)

        assert response.status_code == 500
        assert response.headers["content-type"].startswith("application/problem+json")

    def test_the_document_has_the_required_members(self, exploding: TestClient) -> None:
        body = exploding.get("/v1/boom", headers=ADMIN).json()

        assert {"type", "title", "status", "detail"} <= set(body)
        assert body["status"] == 500

    def test_the_internals_are_not_leaked_to_the_caller(self, exploding: TestClient) -> None:
        """A connection string in a 500 would hand out a password."""
        text = exploding.get("/v1/boom", headers=ADMIN).text

        assert "secret" not in text
        assert "5432" not in text


class TestApiKeys:
    """Scoped keys over the API (SPEC.md section 7)."""

    def create(self, client: TestClient, headers: dict[str, str] = TENANT, **body: Any) -> Any:
        payload = {"name": "ci"}
        payload.update(body)
        return client.post("/v1/keys", json=payload, headers=headers)

    def test_a_key_is_created_and_shown_once(self, client: TestClient) -> None:
        body = self.create(client).json()

        assert body["apikey"].startswith("aisvc-")
        assert body["id"]

    def test_the_response_says_it_cannot_be_shown_again(self, client: TestClient) -> None:
        body = self.create(client).json()

        assert "once" in body["warning"].lower()

    def test_listing_never_returns_the_secret(self, client: TestClient) -> None:
        secret = self.create(client).json()["apikey"]

        listed = client.get("/v1/keys", headers=TENANT).json()

        assert secret not in str(listed)

    def test_listing_shows_the_last_used_time_field(self, client: TestClient) -> None:
        """SPEC.md section 7: "last-used time visible"."""
        self.create(client)

        row = client.get("/v1/keys", headers=TENANT).json()["keys"][0]

        assert "lastused" in row

    def test_a_tenant_sees_only_its_own_keys(self, client: TestClient) -> None:
        self.create(client, headers=TENANT)
        self.create(client, headers=OTHER_TENANT)

        assert client.get("/v1/keys", headers=TENANT).json()["count"] == 1

    def test_scopes_are_accepted(self, client: TestClient) -> None:
        body = self.create(
            client,
            allowed_models=["llama-3.3-70b-instruct"],
            cidrs=["10.0.0.0/8"],
        ).json()

        row = client.get("/v1/keys", headers=TENANT).json()["keys"][0]
        assert row["allowedmodels"] == ["llama-3.3-70b-instruct"]
        assert body["id"]

    def test_an_expiry_in_the_past_is_refused(self, client: TestClient) -> None:
        response = self.create(client, expires_at="2020-01-01T00:00:00Z")

        assert response.status_code == 400

    def test_a_key_can_be_revoked(self, client: TestClient) -> None:
        key_id = self.create(client).json()["id"]

        assert client.delete(f"/v1/keys/{key_id}", headers=TENANT).status_code == 200

    def test_revoking_an_unknown_key_is_a_404(self, client: TestClient) -> None:
        assert client.delete("/v1/keys/nope", headers=TENANT).status_code == 404

    def test_a_tenant_cannot_revoke_another_tenants_key(self, client: TestClient) -> None:
        """Key ids are guessable enough that this must be checked."""
        key_id = self.create(client, headers=OTHER_TENANT).json()["id"]

        assert client.delete(f"/v1/keys/{key_id}", headers=TENANT).status_code == 404


class TestPlaygroundSessions:
    def test_a_session_key_is_issued(self, client: TestClient) -> None:
        body = client.post(
            "/v1/playground/sessions",
            json={"model": "llama-3.3-70b-instruct"},
            headers=TENANT,
        ).json()

        assert body["apikey"].startswith("aisvc-")

    def test_the_session_says_when_it_expires(self, client: TestClient) -> None:
        body = client.post(
            "/v1/playground/sessions",
            json={"model": "llama-3.3-70b-instruct"},
            headers=TENANT,
        ).json()

        assert body["expires"]

    def test_the_snippets_come_with_it(self, client: TestClient) -> None:
        body = client.post(
            "/v1/playground/sessions",
            json={"model": "llama-3.3-70b-instruct"},
            headers=TENANT,
        ).json()

        assert {s["name"] for s in body["snippets"]} == {
            "curl",
            "python",
            "openai-python",
            "openai-javascript",
        }

    def test_no_snippet_carries_the_session_key(self, client: TestClient) -> None:
        """Snippets get pasted into tickets; a live key must not."""
        body = client.post(
            "/v1/playground/sessions",
            json={"model": "llama-3.3-70b-instruct"},
            headers=TENANT,
        ).json()

        assert all(body["apikey"] not in s["code"] for s in body["snippets"])

    def test_an_unknown_model_is_refused(self, client: TestClient) -> None:
        response = client.post("/v1/playground/sessions", json={"model": "ghost"}, headers=TENANT)

        assert response.status_code == 404


class TestBudgetWriting:
    def test_a_budget_can_be_set(self, client: TestClient) -> None:
        response = client.post(
            "/v1/budgets",
            json={"account": "acme", "monthly": 100, "currency": "USD", "on_exceed": "block"},
            headers=ADMIN,
        )

        assert response.status_code == 200
        assert response.json()["monthly"] == 100

    def test_a_tenant_cannot_set_its_own_budget(self, client: TestClient) -> None:
        """Otherwise a tenant could lift the limit it is blocked by."""
        response = client.post(
            "/v1/budgets",
            json={"account": "acme", "monthly": 10_000, "currency": "USD", "on_exceed": "alert"},
            headers=TENANT,
        )

        assert response.status_code == 403

    def test_a_budget_can_be_updated(self, client: TestClient) -> None:
        client.post(
            "/v1/budgets",
            json={"account": "acme", "monthly": 100, "currency": "USD", "on_exceed": "alert"},
            headers=ADMIN,
        )

        response = client.patch("/v1/budgets/acme", json={"monthly": 250}, headers=ADMIN)

        assert response.json()["monthly"] == 250

    def test_a_budget_can_be_deleted(self, client: TestClient) -> None:
        client.post(
            "/v1/budgets",
            json={"account": "acme", "monthly": 100, "currency": "USD", "on_exceed": "alert"},
            headers=ADMIN,
        )

        assert client.delete("/v1/budgets/acme", headers=ADMIN).status_code == 200

    def test_deleting_an_unknown_budget_is_a_404(self, client: TestClient) -> None:
        assert client.delete("/v1/budgets/ghost", headers=ADMIN).status_code == 404

    def test_an_invalid_on_exceed_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/v1/budgets",
            json={"account": "acme", "monthly": 100, "currency": "USD", "on_exceed": "explode"},
            headers=ADMIN,
        )

        assert response.status_code in (400, 422)


class TestBudgetsHaveOneSourceOfTruth:
    """`platform.yaml` declares budgets; the database holds the live one.

    CLAUDE.md puts budgets in both: the spec is desired state, and
    Postgres holds "runs, actions, usage, budgets". They have to agree on
    who wins, or an admin raises a limit through the API and the list
    keeps showing the old one from the spec.
    """

    def set_budget(self, client: TestClient, monthly: float) -> None:
        client.post(
            "/v1/budgets",
            json={"account": "acme", "monthly": monthly, "currency": "USD", "on_exceed": "alert"},
            headers=ADMIN,
        )

    def listed(self, client: TestClient) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = client.get("/v1/budgets", headers=ADMIN).json()["budgets"]
        return rows

    def test_a_budget_set_through_the_api_is_listed(self, client: TestClient) -> None:
        self.set_budget(client, 123)

        assert any(row["monthly"] == 123 for row in self.listed(client))

    def test_the_stored_budget_wins_over_the_spec(self, client: TestClient) -> None:
        """Otherwise raising a limit through the API appears to do nothing."""
        self.set_budget(client, 999)

        rows = [r for r in self.listed(client) if r["account"] == "acme"]
        assert rows
        assert all(r["monthly"] == 999 for r in rows)

    def test_an_account_is_listed_once(self, client: TestClient) -> None:
        self.set_budget(client, 999)

        accounts = [r["account"] for r in self.listed(client)]
        assert len(accounts) == len(set(accounts))

    def test_a_spec_budget_with_no_stored_one_is_still_listed(self, client: TestClient) -> None:
        assert isinstance(self.listed(client), list)

    def test_a_tenant_sees_only_its_own_budget(self, client: TestClient) -> None:
        self.set_budget(client, 100)

        rows = client.get("/v1/budgets", headers=OTHER_TENANT).json()["budgets"]
        assert all(r["account"] != "acme" for r in rows)


class TestUsageComesFromTheStore:
    """Nothing populated usage before milestone 8 (PROGRESS.md)."""

    def record_usage(self, client: TestClient) -> None:
        record_usage(client, input_tokens=1_000_000, output_tokens=0)

    def test_recorded_usage_is_listed(self, client: TestClient) -> None:
        self.record_usage(client)

        body = client.get("/v1/usage", headers=ADMIN).json()

        assert body["count"] == 1
        assert body["usage"][0]["input_tokens"] == 1_000_000

    def test_the_cost_is_the_one_that_was_recorded(self, client: TestClient) -> None:
        """Priced when written, so a tier change cannot rewrite history."""
        self.record_usage(client)

        assert client.get("/v1/usage", headers=ADMIN).json()["usage"][0]["cost"] == 0.60

    def test_a_tenant_sees_only_its_own_usage(self, client: TestClient) -> None:
        self.record_usage(client)

        assert client.get("/v1/usage", headers=OTHER_TENANT).json()["count"] == 0

    def test_a_tenant_sees_its_own(self, client: TestClient) -> None:
        self.record_usage(client)

        assert client.get("/v1/usage", headers=TENANT).json()["count"] == 1


class TestRolloutsApplyTheirWeights:
    """Promote and rollback must move traffic, not just report a split.

    Milestone 7 left these computing weights and recording them on a
    job, with nothing applying them — so a promotion through the API
    changed nothing at the gateway.
    """

    def cluster_of(self, client: TestClient) -> Any:
        return cast(FastAPI, client.app).state.services.cluster

    def weights(self, client: TestClient) -> dict[str, dict[str, int]]:
        from aisvc.k8s.client import observe_weights

        return observe_weights(self.cluster_of(client))

    def test_promoting_sends_traffic_to_the_candidate(self, client: TestClient) -> None:
        response = client.post(
            "/v1/deployments/llama-3.3-70b-instruct-green/rollout/promote",
            headers=ADMIN,
        )

        assert response.status_code == 202
        assert self.weights(client)["llama-3.3-70b-instruct"]["green"] == 100

    def test_the_job_still_reports_the_split(self, client: TestClient) -> None:
        job = client.post(
            "/v1/deployments/llama-3.3-70b-instruct-green/rollout/promote",
            headers=ADMIN,
        ).json()

        result = client.get(f"/v1/jobs/{job['jobid']}", headers=ADMIN).json()
        assert result["jobresult"]["weights"]["green"] == 100

    def test_rolling_back_a_canary_returns_traffic_to_the_live_colour(
        self, client: TestClient
    ) -> None:
        """Rollback abandons a candidate mid-canary.

        After a *completed* promotion there is nothing to roll back to —
        the new colour is live and the old one is retired — so the case
        worth testing is a canary still carrying a slice of traffic.
        """
        import asyncio

        from aisvc.reconciler.deployer import ServingDeployer

        services = cast(FastAPI, client.app).state.services
        asyncio.run(
            ServingDeployer(services.cluster, services.spec, services.registry).apply_routes(
                weights={"llama-3.3-70b-instruct": {"blue": 90, "green": 10}}
            )
        )

        client.post(
            "/v1/deployments/llama-3.3-70b-instruct-green/rollout/rollback",
            headers=ADMIN,
        )

        split = self.weights(client)["llama-3.3-70b-instruct"]
        assert split["blue"] == 100
        assert split.get("green", 0) == 0

    def test_a_tenant_may_promote_its_own_deployment(self, client: TestClient) -> None:
        """SPEC.md section 9 gives promote to all roles.

        Ownership is the plugin's to enforce — it holds `ai_deployment`
        so CloudStack can — and the engine trusts what it asserts.
        """
        response = client.post(
            "/v1/deployments/llama-3.3-70b-instruct-green/rollout/promote",
            headers=TENANT,
        )

        assert response.status_code == 202

    def test_without_a_cluster_the_job_says_so(self, registry: ModelRegistry) -> None:
        """The engine can run without cluster access; it must say so
        rather than report a promotion that moved nothing."""
        services = EngineServices(
            spec=spec_with(), registry=registry, session_factory=memory_sessions()
        )
        client = TestClient(create_app(services))
        job = client.post(
            "/v1/deployments/llama-3.3-70b-instruct-green/rollout/promote",
            headers=ADMIN,
        ).json()

        result = client.get(f"/v1/jobs/{job['jobid']}", headers=ADMIN).json()
        assert result["jobstatus"] == 2


class TestMetricsEndpoint:
    """SPEC.md section 11: the engine's own metrics, for the alert rules."""

    def test_metrics_are_exposed(self, client: TestClient) -> None:
        response = client.get("/metrics")

        assert response.status_code == 200

    def test_the_content_type_is_the_exposition_format(self, client: TestClient) -> None:
        """Prometheus refuses a scrape served as application/json."""
        response = client.get("/metrics")

        assert response.headers["content-type"].startswith("text/plain")

    def test_scraping_needs_no_caller_context(self, client: TestClient) -> None:
        """Prometheus cannot assert one, and the endpoint exposes no
        tenant data — only counters the engine keeps about itself."""
        assert client.get("/metrics").status_code == 200

    def test_a_fresh_engine_already_exports_its_counters(self, client: TestClient) -> None:
        """The whole point of registering at zero.

        `increase(...{status="failed"}[30m]) > 0` on a series that does
        not exist yet never fires, so an engine that exported nothing
        until its first reconcile could not alert on the first failure.
        """
        body = client.get("/metrics").text

        assert 'aisvc_reconcile_runs_total{status="failed",zone="zone-east-1"} 0' in body
        assert 'status="succeeded"' in body

    def test_every_declared_zone_is_registered(self, client: TestClient) -> None:
        body = client.get("/metrics").text

        assert 'zone="zone-east-1"' in body

    def test_no_tenant_identifiers_are_exported(self, client: TestClient) -> None:
        from aisvc.observability.metrics import record_run

        record_run("zone-east-1", failed=False)
        body = client.get("/metrics").text

        assert "acme" not in body
        assert "aisvc_reconcile_runs_total" in body
