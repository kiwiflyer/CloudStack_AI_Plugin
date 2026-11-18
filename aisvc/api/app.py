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
"""The engine REST API (SPEC.md section 10).

Called only by the Java plugin and the operator CLI, over mTLS, and
never exposed to tenants. The section 9 command map is the authoritative
endpoint list; the FastAPI-generated OpenAPI schema is the contract.

Everything here filters by caller context. Async commands return 202
with a job id the plugin polls at `/v1/jobs/{id}`.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import FastAPI, Query, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from aisvc.adapters import (
    AdapterRejected,
    AdapterRequest,
    gateway_model_name,
    validate_adapter,
)
from aisvc.api.deps import (
    AdminDep,
    CallerDep,
    JobsDep,
    Services,
    ServicesDep,
    SessionDep,
)
from aisvc.api.errors import ApiError, register_error_handlers
from aisvc.batch import BatchRejected, parse_requests
from aisvc.batch.runner import read_output, store_input
from aisvc.certificates import CertificateRejected, validate_certificate
from aisvc.certificates.bootstrap import BootstrapRejected, BootstrapTokens
from aisvc.cloudstack.clusters import ClusterState
from aisvc.cloudstack.gpus import GpuCardCatalogue
from aisvc.db import BudgetRecord, KeyStore, RunLog, UsageStore
from aisvc.db.profiles import (
    AssignmentStore,
    GenerationProfileStore,
    PlacementStore,
    ServingProfileStore,
)
from aisvc.db.tier2 import (
    TERMINAL_BATCH_STATES,
    AdapterStore,
    BatchStore,
    CertificateStore,
    EvaluationStore,
    ExposureStore,
    GuardrailStore,
)
from aisvc.endpoints import (
    EndpointExposure,
    ExposureRejected,
    TlsSource,
    describe_exposure,
    valid_hostname,
    validate_exposure,
)
from aisvc.evaluation import (
    EvaluationRun,
    GateBlocked,
    GateDecision,
    PerformanceResult,
    SuiteResult,
    gate,
    override_gate,
)
from aisvc.guardrails import presets
from aisvc.k8s.client import Kubernetes, observe_deployments, observe_weights
from aisvc.lifecycle import RolloutError, plan_drain, start_rollout
from aisvc.metering import evaluate_budget
from aisvc.observability.metrics import REGISTRY, register_zones, render
from aisvc.playground import SESSION_KEY_TTL, issue_session_key, snippets_for
from aisvc.profiles import Level, ProfileLayer, resolve
from aisvc.reconciler.deployer import ServingDeployer
from aisvc.setup import discover_gpu_classes, plan_self_test, preflight
from aisvc.spec.models import Budget

if TYPE_CHECKING:
    from aisvc.api.deps import Services

__all__ = ["API_VERSION", "create_app"]

API_VERSION = "v1"
#: vLLM's own default, used where no serving profile sets one.
DEFAULT_MAX_LORA_RANK = 32

PREFIX = f"/{API_VERSION}"

DESCRIPTION = """
The CloudStack AI Services engine API.

Called by the `ai-services` CloudStack plugin and by the `aisvc` operator
CLI, over mTLS. Not reachable from tenant networks: tenants use the
OpenAI-compatible gateway instead.

Every request carries a caller context in `X-Aisvc-*` headers, asserted
by the plugin from the CloudStack session, and every response is
filtered by it. Errors are RFC 9457 problem documents.
""".strip()


class KeyRequest(BaseModel):
    """What a tenant asks for when creating a scoped key."""

    name: str = Field(min_length=1, max_length=255)
    allowed_models: list[str] = Field(default_factory=list)
    cidrs: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class PlaygroundRequest(BaseModel):
    model: str = Field(min_length=1)


class BudgetRequest(BaseModel):
    account: str = Field(min_length=1)
    #: Required: a CloudStack account name is unique only within its
    #: domain, so an account alone does not say whose budget this is.
    domain: str = Field(min_length=1)
    monthly: float = Field(ge=0)
    currency: str = "USD"
    on_exceed: Literal["alert", "block"] = "alert"

    def as_budget(self) -> Budget:
        return Budget(monthly=self.monthly, currency=self.currency, on_exceed=self.on_exceed)


class BudgetPatch(BaseModel):
    """Only the fields given are changed."""

    monthly: float | None = Field(default=None, ge=0)
    currency: str | None = None
    on_exceed: Literal["alert", "block"] | None = None

    def applied_to(self, existing: BudgetRecord) -> Budget:
        return Budget(
            monthly=self.monthly if self.monthly is not None else existing.monthly,
            currency=self.currency or existing.currency,
            on_exceed=self.on_exceed or existing.on_exceed,
        )


def _budget_row(record: BudgetRecord, store: UsageStore) -> dict[str, Any]:
    state = store.budget_state(record.account, record.domain)
    return {
        "id": record.id,
        "account": record.account,
        "domain": record.domain,
        "monthly": record.monthly,
        "currency": record.currency,
        "on_exceed": record.on_exceed,
        "spent": state.spent if state else 0.0,
        "percent_used": round(state.percent_used, 1) if state else 0.0,
        "blocked": state.should_block if state else False,
    }


def _all_usage(session: Any) -> list[Any]:
    """Every usage row, newest last."""
    from aisvc.db import UsageRow

    return list(session.scalars(select(UsageRow).order_by(UsageRow.recorded_at)))


class AdapterBody(BaseModel):
    """A LoRA adapter a tenant is registering (SPEC.md section 8)."""

    name: str = Field(min_length=1, max_length=255)
    base_model: str = Field(min_length=1)
    rank: int = Field(ge=1)
    size_bytes: int = Field(ge=0)
    files: list[str] = Field(default_factory=list)
    uri: str = ""
    shared_with_domain: bool = False


class BatchBody(BaseModel):
    model: str = Field(min_length=1)
    #: The JSONL itself. Large files arrive by reference instead.
    input: str = ""
    input_uri: str = ""


class DeploymentBody(BaseModel):
    """A catalogued model being placed in a zone (ADR-0008)."""

    model: str = Field(min_length=1)
    #: The plugin sends the CloudStack zone's *name*, which
    #: `platform.yaml` records as `cloudstack_zone`; a direct caller
    #: may send the spec's own zone name. Either is checked against
    #: the model's node pool rather than believed. Coerced from a
    #: number so a caller that sends CloudStack's internal id gets the
    #: zone refusal rather than a 422 about types.
    zone_id: str = ""

    @field_validator("zone_id", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> Any:
        return "" if value is None else str(value)

    replicas: int | None = None
    serving_profile: str = ""


class DeploymentUpdateBody(BaseModel):
    """A change to an existing placement."""

    replicas: int | None = None
    serving_profile: str | None = None


class ServingProfileBody(BaseModel):
    """A serving profile being created (SPEC.md section 6)."""

    name: str = Field(min_length=1, max_length=255)
    #: The CloudStack command declares this a STRING, so it arrives as
    #: JSON text. A dict is accepted as well, because curl and the CLI
    #: have no reason to stringify an object they already have.
    params: dict[str, Any] | str = Field(default_factory=dict)


class ProfileUpdateBody(BaseModel):
    """New parameters for an existing profile."""

    params: dict[str, Any] | str = Field(default_factory=dict)


class GenerationProfileBody(BaseModel):
    """A generation profile being created (SPEC.md section 6)."""

    name: str = Field(min_length=1, max_length=255)
    params: dict[str, Any] | str = Field(default_factory=dict)


class AssignmentBody(BaseModel):
    """Where a profile binds (SPEC.md section 6).

    One `profile` rather than one field per kind, because that is what
    `assignAiTuningProfile` sends. Which kind it is comes from looking
    the name up, so a caller cannot bind a serving profile into the
    generation slot by mislabelling it.
    """

    scope: str = Field(min_length=1)
    target: str = ""
    profile: str = Field(min_length=1)


class GuardrailBody(BaseModel):
    name: str = Field(min_length=1)
    scope: str = "account"
    target: str = ""
    checks: dict[str, Any] | None = None
    #: Use a shipped profile instead of spelling the checks out.
    preset: str = ""


class GuardrailPatch(BaseModel):
    checks: dict[str, Any] | None = None
    target: str | None = None


class SuiteBody(BaseModel):
    name: str
    score: float
    baseline: float | None = None
    customer: str = ""


class PerformanceBody(BaseModel):
    gpu_class: str
    ttft_p95_ms: float
    tokens_per_second: float


class EvaluationBody(BaseModel):
    revision: str = Field(min_length=1)
    suites: list[SuiteBody] = Field(default_factory=list)
    performance: list[PerformanceBody] = Field(default_factory=list)


class PublishBody(BaseModel):
    price_tier: str = ""
    #: A root admin's recorded reason for publishing past the gate.
    override_reason: str = ""


class PrivateEndpointBody(BaseModel):
    deployment: str = Field(min_length=1)
    network_id: str = ""
    public_enabled: bool = True
    custom_domain: str = ""
    tls: Literal["none", "acme", "uploaded"] = "none"
    certificate_id: str = ""

    @field_validator("custom_domain")
    @classmethod
    def _is_a_hostname(cls, value: str) -> str:
        """Refused here as well as in `validate_exposure`.

        The two guard different doors: this one the HTTP route, that
        one anything reaching the store by another path. The value is
        rendered into the proxy's cloud-init, where a shell and a YAML
        parser both read it.
        """
        if value and not valid_hostname(value):
            raise ValueError("custom_domain must be a hostname such as ai.example.com")
        return value


def _max_lora_rank(services: Services, base_model: str) -> int:
    """The serving profile's max_lora_rank for this base.

    Adapters have to be validated against the same number vLLM was
    started with, or a valid adapter fails to load at request time.
    """
    model = next((m for m in services.spec.document.models if m.name == base_model), None)
    if model is None:
        return DEFAULT_MAX_LORA_RANK
    profile = services.spec.serving_profile_for(model)
    if profile is None:
        return DEFAULT_MAX_LORA_RANK
    raw = profile.params.get("max_lora_rank", DEFAULT_MAX_LORA_RANK)
    return int(raw) if isinstance(raw, int | float | str) else DEFAULT_MAX_LORA_RANK


def _adapter_limit(services: Services, base_model: str) -> int:
    model = next((m for m in services.spec.document.models if m.name == base_model), None)
    if model is None:
        return 20
    return model.lora.max_adapters_per_account


async def _owned_networks(services: Services, caller: Any) -> list[str]:
    """Networks this caller may attach a private endpoint to.

    CloudStack is the authority on who owns a network, and
    `build_services` wires `networks_for` to ask it
    (`aisvc.cloudstack.networks`). The engine keeps no second copy of
    the answer.

    Awaits the lookup when it is a coroutine, so a test can hand in a
    plain function and production can hand in an API call.

    With no lookup configured this returns nothing, which refuses every
    attachment to a network. That is the safe direction, and the reason
    the API refuses rather than guesses.
    """
    lookup = getattr(services, "networks_for", None)
    if lookup is None:
        return []
    found = lookup(caller.account, caller.domain_path)
    if inspect.isawaitable(found):
        found = await found
    return [str(network) for network in found]


def _adapter_row(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "basemodel": record.base_model,
        "servedmodelname": gateway_model_name(record.base_model, record.name),
        "rank": record.rank,
        "sizebytes": record.size_bytes,
        "account": record.account,
        "shared": record.shared_with_domain,
        "created": record.created_at.isoformat() if record.created_at else None,
    }


def _batch_row(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "model": record.model,
        "status": record.state,
        "requestcount": record.request_count,
        "account": record.account,
        "created": record.created_at.isoformat() if record.created_at else None,
        "finished": record.finished_at.isoformat() if record.finished_at else None,
        "inputuri": record.input_uri,
        "outputuri": record.output_uri,
    }


def _guardrail_row(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "scope": record.scope,
        "target": record.target,
        "checks": record.checks or {},
    }


def _evaluation_row(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "model": record.model,
        "revision": record.revision,
        "passed": record.passed,
        "overridden": record.overridden,
        "summary": record.summary,
        "regressions": record.suites or [],
        "performance": record.performance or [],
    }


class BootstrapBody(BaseModel):
    """A proxy redeeming its one-time token."""

    token: str = Field(min_length=1)


class CertificateReplacement(BaseModel):
    """A renewal for an existing certificate. The domain is not here:
    it comes from the record being replaced, so a renewal cannot
    quietly become a different endpoint's certificate."""

    certificate: str = Field(min_length=1)
    private_key: str = Field(min_length=1)


class CertificateBody(BaseModel):
    """An uploaded TLS pair (SPEC.md section 8)."""

    custom_domain: str = Field(min_length=1)
    certificate: str = Field(min_length=1)
    private_key: str = Field(min_length=1)


def _guardrails_above(session: Any, caller: Any, scope: str) -> dict[Any, dict[str, Any]]:
    """The checks already bound above the scope being written.

    Only profiles that cover this caller: a lock at another account's
    scope is above nobody. Used to refuse a check that is locked higher
    up, rather than storing one `resolve_guardrails` would then ignore.
    """
    from aisvc.guardrails.authority import author_level, profile_applies_to

    mine = author_level(scope)
    above: dict[Any, dict[str, Any]] = {}
    for record in GuardrailStore(session).all():
        if not profile_applies_to(record, caller):
            continue
        try:
            level = author_level(record.scope)
        except Exception:
            # A row stored before a scope was renamed cannot bind
            # anything; it is not a reason to refuse this write.
            continue
        if level >= mine:
            continue
        above.setdefault(level, {}).update(record.checks or {})
    return above


def _exposure_row(record: Any) -> dict[str, Any]:
    """One private endpoint, as the UI needs to read it.

    `exposure` is the same phrase the create call returns, rebuilt from
    the stored row rather than stored alongside it: two spellings of
    one fact drift, and this one is shown to a buyer checking that an
    endpoint really is private.
    """
    return {
        "id": record.id,
        "deployment": record.deployment,
        "network": record.network_id,
        "publicenabled": record.public_enabled,
        "customdomain": record.custom_domain,
        "tls": record.tls,
        "certificate": record.certificate_id,
        "account": record.account,
        "created": record.created_at.isoformat() if record.created_at else None,
        "exposure": describe_exposure(
            EndpointExposure(
                deployment=record.deployment,
                account=record.account,
                domain=record.domain,
                public_enabled=record.public_enabled,
                network_id=record.network_id,
                custom_domain=record.custom_domain,
                tls=TlsSource(record.tls),
                certificate_id=record.certificate_id,
            )
        ),
    }


def _certificate_row(record: Any) -> dict[str, Any]:
    """Metadata only: no certificate body, and no key, ever."""
    return {
        "id": record.id,
        "customdomain": record.custom_domain,
        "fingerprint": record.fingerprint,
        "expires": record.not_after.isoformat() if record.not_after else None,
        "serial": record.serial,
        "account": record.account,
        "created": record.created_at.isoformat() if record.created_at else None,
    }


def create_app(services: Services) -> FastAPI:
    """Build the engine app around an assembled `Services`."""
    app = FastAPI(
        title="CloudStack AI Services engine",
        version="1.0.0",
        description=DESCRIPTION,
        openapi_url=f"{PREFIX}/openapi.json",
        docs_url=f"{PREFIX}/docs",
    )
    app.state.services = services

    # Declared up front so a freshly started engine already exports a
    # zero for every zone, and an alert can fire on the first failure.
    register_zones([zone.name for zone in services.spec.document.zones])

    register_error_handlers(app)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    # -- liveness and readiness ------------------------------------------

    @app.get(f"{PREFIX}/health", tags=["engine"], summary="Liveness")
    async def health() -> dict[str, str]:
        """Always 200 while the process is up. No caller context needed."""
        return {"status": "ok"}

    @app.get(f"{PREFIX}/ready", tags=["engine"], summary="Readiness")
    async def ready(services: ServicesDep) -> dict[str, Any]:
        """Whether this replica can serve: the spec loaded and the DB answers."""
        checks = {"spec": services.spec is not None}
        try:
            session = services.session_factory()  # type: ignore[operator]
            session.close()
            checks["database"] = True
        except Exception:
            checks["database"] = False

        if not all(checks.values()):
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "engine is not ready: " + ", ".join(k for k, v in checks.items() if not v),
                code="not_ready",
                extra={"checks": checks},
            )
        return {"status": "ready", "checks": checks}

    # -- async jobs -------------------------------------------------------

    @app.get(f"{PREFIX}/jobs/{{job_id}}", tags=["engine"], summary="Async job status")
    async def get_job(job_id: str, caller: CallerDep, jobs: JobsDep) -> dict[str, Any]:
        """Polled by the plugin until the job leaves the pending state."""
        job = jobs.get(job_id, caller)
        if job is None:
            # Indistinguishable from "not yours", so ids cannot be probed.
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no job {job_id}", code="not_found")
        return job.as_response()

    # -- catalog ----------------------------------------------------------

    @app.get(f"{PREFIX}/models", tags=["catalog"], summary="List models")
    async def list_models(caller: CallerDep, services: ServicesDep) -> dict[str, Any]:
        """The catalog, filtered to what this caller may see."""
        visible = [
            _model_summary(model, services)
            for model in services.spec.document.models
            if _model_visible(model, caller)
        ]
        return {"count": len(visible), "models": visible}

    @app.get(f"{PREFIX}/capacity", tags=["admin"], summary="GPU capacity")
    async def capacity(_: AdminDep, services: ServicesDep) -> dict[str, Any]:
        """Live GPU capacity per zone and class. Admin only."""
        snapshot = services.snapshot()
        if snapshot is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "capacity has not been observed yet",
                code="unavailable",
            )
        return {
            "taken_at": snapshot.taken_at.isoformat(),
            "classes": [
                {
                    "zone": cap.zone,
                    "gpu_class": cap.gpu_class,
                    "nodes_total": cap.nodes_total,
                    "nodes_healthy": cap.nodes_healthy,
                    "nodes_free": cap.nodes_free,
                    "gpus_total": cap.gpus_total,
                    "gpus_free": cap.gpus_free,
                }
                for cap in snapshot.classes.values()
            ],
        }

    @app.get(f"{PREFIX}/runs", tags=["admin"], summary="Reconciler runs")
    async def list_runs(
        _: AdminDep,
        session: SessionDep,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """The activity view: what the reconciler planned and did."""
        runs = RunLog(session).recent_runs(limit=limit)
        return {
            "count": len(runs),
            "runs": [
                {
                    "id": run.id,
                    "zone": run.zone,
                    "status": run.status.value,
                    "dry_run": run.dry_run,
                    "started_at": run.started_at.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "actions": [
                        {
                            "kind": action.kind,
                            "resource_type": action.resource_type,
                            "name": action.name,
                            "outcome": action.outcome.value,
                            "error": action.error,
                        }
                        for action in run.actions
                    ],
                }
                for run in runs
            ],
        }

    # -- deployments ------------------------------------------------------

    @app.get(f"{PREFIX}/deployments", tags=["deployments"], summary="List deployments")
    async def list_deployments(caller: CallerDep, services: ServicesDep) -> dict[str, Any]:
        """What is serving right now, filtered by caller context."""
        cluster = getattr(services, "cluster", None)
        if cluster is None:
            return {"count": 0, "deployments": []}

        by_model = {m.name: m for m in services.spec.document.models}
        visible = []
        for (name, colour), deployed in sorted(observe_deployments(cluster).items()):
            model = by_model.get(name)
            if model is None or not _model_visible(model, caller):
                continue
            visible.append(
                {
                    "model": name,
                    "colour": colour,
                    "replicas_desired": deployed.replicas_desired,
                    "replicas_ready": deployed.replicas_ready,
                    "ready": deployed.fully_ready,
                    "image": deployed.image,
                }
            )
        return {"count": len(visible), "deployments": visible}

    @app.post(
        f"{PREFIX}/deployments",
        tags=["deployments"],
        summary="Deploy a catalogued model",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_deployment(
        caller: CallerDep,
        session: SessionDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        body: DeploymentBody,
    ) -> dict[str, Any]:
        """Place a `placement: api` model in its zone (ADR-0008).

        This writes placement and nothing else. The spec still owns
        what the model is — its source, its licence, its bounds — so a
        count outside those bounds is refused here rather than clamped
        later, and a zone that is not the model's is refused rather
        than stored.
        """
        model, pool = _placeable(services, body.model)
        _check_zone(services, pool, body.zone_id)
        replicas = _placement_replicas(model, body.replicas)

        store = PlacementStore(session)
        if store.for_model(model.name, pool.zone) is not None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{model.name} is already placed in {pool.zone}; "
                f"change it with updateAiModelDeployment",
                code="already_placed",
            )

        job = jobs.create("deployAiModel", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            record = store.add(
                model=model.name,
                zone=pool.zone,
                replicas=replicas,
                serving_profile=body.serving_profile,
                account=caller.account,
                domain=caller.domain_path,
            )
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, _placement_row(record))
        return {"jobid": job.id}

    @app.patch(
        f"{PREFIX}/deployments/{{placement_id}}",
        tags=["deployments"],
        summary="Change a deployment",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def update_deployment(
        placement_id: str,
        caller: CallerDep,
        session: SessionDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        body: DeploymentUpdateBody,
    ) -> dict[str, Any]:
        store = PlacementStore(session)
        record = _own_placement(store, caller, placement_id)
        model, _ = _placeable(services, record.model)
        replicas = _placement_replicas(model, body.replicas) if body.replicas is not None else None

        job = jobs.create("updateAiModelDeployment", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            store.update(record, replicas=replicas, serving_profile=body.serving_profile)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, _placement_row(record))
        return {"jobid": job.id}

    @app.delete(
        f"{PREFIX}/deployments/{{placement_id}}",
        tags=["deployments"],
        summary="Remove a deployment",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_deployment(
        placement_id: str,
        caller: CallerDep,
        session: SessionDep,
        jobs: JobsDep,
        response: Response,
    ) -> dict[str, Any]:
        """Stop running this model here.

        The row going away is what the reconciler reads: a
        `placement: api` model with no row for a zone is not declared
        there, so the next apply tears its Deployment down.
        """
        store = PlacementStore(session)
        record = _own_placement(store, caller, placement_id)

        job = jobs.create("deleteAiModelDeployment", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            store.delete(record)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, {"id": placement_id, "deleted": True})
        return {"jobid": job.id}

    @app.get(f"{PREFIX}/endpoints", tags=["deployments"], summary="List endpoints")
    async def list_endpoints(caller: CallerDep, services: ServicesDep) -> dict[str, Any]:
        """The OpenAI-compatible base URL per model this caller may use."""
        base = getattr(services, "gateway_url", "https://ai.example.invalid")
        visible = [
            {
                "model": model.name,
                "base_url": f"{base}/v1",
                "task": model.task,
                "price_tier": model.price_tier,
            }
            for model in services.spec.document.models
            if _model_visible(model, caller)
        ]
        return {"count": len(visible), "endpoints": visible}

    @app.get(
        "/metrics",
        tags=["admin"],
        summary="Prometheus metrics",
        response_class=PlainTextResponse,
        include_in_schema=False,
    )
    async def metrics() -> PlainTextResponse:
        """The engine's own counters (SPEC.md section 11).

        Outside the versioned prefix and outside the caller-context
        requirement: Prometheus cannot assert one, and there is nothing
        here but counters the engine keeps about itself — no tenant,
        account or request appears in a label.
        """
        return PlainTextResponse(
            render(REGISTRY), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    # -- setup ------------------------------------------------------------

    async def _gpu_cards(services: Services) -> GpuCardCatalogue | None:
        """CloudStack's GPU card catalogue, or nothing.

        `read_cards` already carries a failure rather than raising, so
        this only handles there being no lookup at all.
        """
        lookup = getattr(services, "gpu_cards", None)
        if lookup is None:
            return None
        found = lookup()
        if inspect.isawaitable(found):
            found = await found
        return cast("GpuCardCatalogue", found)

    async def _cluster_states(services: Services) -> list[ClusterState] | None:
        """What CloudStack says about each zone's cluster, or nothing.

        A failure inside `read_clusters` is already recorded per zone,
        so this only has to handle there being no lookup at all.
        """
        lookup = getattr(services, "cluster_states", None)
        if lookup is None:
            return None
        found = lookup()
        if inspect.isawaitable(found):
            found = await found
        return list(found)

    @app.get(f"{PREFIX}/setup/preflight", tags=["admin"], summary="Preflight checks")
    async def setup_preflight(_: AdminDep, services: ServicesDep) -> dict[str, Any]:
        """Everything the wizard checks before a zone is enabled.

        Every check runs even after one fails, so a misconfigured zone
        is fixed in one pass rather than one problem at a time.
        """
        # No snapshot is not an error: a zone being set up has not been
        # observed yet, and the wizard has to work from empty. The
        # capacity checks come back skipped, saying what to run first.
        report = preflight(
            services.spec,
            registry=services.registry,
            snapshot=services.snapshot(),
            # Resolved here rather than inside preflight: the lookup is
            # async and every other check is not.
            clusters=await _cluster_states(services),
            kube=cast("Kubernetes | None", services.kube),
            gpu_cards=await _gpu_cards(services),
        )
        return {
            "ok": report.ok,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status.value,
                    "detail": check.detail,
                    "remedy": check.remedy,
                }
                for check in report.checks
            ],
        }

    @app.get(
        f"{PREFIX}/setup/gpu-discovery",
        tags=["admin"],
        summary="Discover GPU classes",
    )
    async def setup_gpu_discovery(_: AdminDep, services: ServicesDep) -> dict[str, Any]:
        """GPU classes the wizard proposes from the tags on healthy hosts."""
        snapshot = services.snapshot()
        proposals = discover_gpu_classes(snapshot) if snapshot else []
        return {
            "count": len(proposals),
            "classes": [
                {
                    "host_tag": p.host_tag,
                    "hosts": p.hosts,
                    "suggested_name": p.suggested_name,
                }
                for p in proposals
            ],
        }

    @app.post(
        f"{PREFIX}/setup/selftest",
        tags=["admin"],
        summary="Run the setup self-test",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def setup_self_test(
        caller: AdminDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        zone: str = Query(description="The zone to test."),
    ) -> dict[str, Any]:
        """Deploy a tiny model, exercise it, then tear it down.

        Returns 202 with the plan recorded on the job. Running the steps
        needs GPU capacity, so the job carries the plan and the lab runs
        it (SPEC.md section 7).
        """
        # A zone the spec does not name is a bad argument, not a failed
        # run: creating a job for it would fill the run log with typos
        # and hand the caller a 202 for work that was never going to
        # happen. Validate first, as /v1/profiles/effective does.
        try:
            plan = plan_self_test(services.spec, zone=zone)
        except ValueError as exc:
            raise ApiError(status.HTTP_404_NOT_FOUND, str(exc), code="zone-not-found") from exc

        job = jobs.create("runAiSetupSelfTest", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        jobs.succeed(
            job,
            {
                "zone": plan.zone,
                "model": plan.model,
                "steps": [{"name": s.name, "description": s.description} for s in plan.steps],
                "teardown": plan.teardown,
            },
        )
        return {"jobid": job.id}

    # -- rollouts ---------------------------------------------------------

    @app.post(
        f"{PREFIX}/deployments/{{deployment_id}}/rollout/promote",
        tags=["deployments"],
        summary="Promote a rollout",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def promote_rollout(
        deployment_id: str,
        caller: CallerDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
    ) -> dict[str, Any]:
        """Send all traffic to the canary and retire the old colour."""
        return await _rollout_job(
            "promoteAiRollout",
            deployment_id,
            caller,
            services,
            jobs,
            response,
            promote=True,
        )

    @app.post(
        f"{PREFIX}/deployments/{{deployment_id}}/rollout/rollback",
        tags=["deployments"],
        summary="Roll a rollout back",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def rollback_rollout(
        deployment_id: str,
        caller: CallerDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
    ) -> dict[str, Any]:
        """Send all traffic back to the colour that was live."""
        return await _rollout_job(
            "rollbackAiRollout",
            deployment_id,
            caller,
            services,
            jobs,
            response,
            promote=False,
        )

    # -- nodes ------------------------------------------------------------

    @app.post(
        f"{PREFIX}/nodes/{{node_id}}/drain",
        tags=["admin"],
        summary="Drain a GPU node",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def drain_node(
        node_id: str,
        caller: AdminDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        force: bool = Query(
            default=False,
            description="Proceed even without capacity for the replicas elsewhere.",
        ),
    ) -> dict[str, Any]:
        """Move replicas off a node, then destroy it.

        Destroy, not stop: a stopped node keeps its GPUs allocated
        (ADR-0003), stranding capacity nobody is using.
        """
        snapshot = services.snapshot()
        free_elsewhere = snapshot.gpus_free if snapshot else 0
        replicas_on_node = _replicas_on(services, node_id)

        plan = plan_drain(
            node_id,
            replicas_on_node=replicas_on_node,
            free_elsewhere=free_elsewhere,
            force=force,
        )

        job = jobs.create("drainAiNode", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        if not plan.can_proceed:
            jobs.fail(job, plan.reason)
        else:
            jobs.succeed(
                job,
                {
                    "node": plan.node,
                    "action": plan.action,
                    "replicas_moved": plan.replicas_to_move,
                    "forced": plan.forced,
                    "reason": plan.reason,
                },
            )
        return {"jobid": job.id}

    # -- usage and budgets ------------------------------------------------

    @app.get(f"{PREFIX}/usage", tags=["billing"], summary="List usage")
    async def list_usage(caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        """Metered usage, priced from the catalog's tiers."""
        # Read from the store rather than repricing here: rows were
        # priced when they were written, and a later change to the
        # catalog's tiers must not rewrite a past month's bill.
        rows = [
            {
                "account": row.account,
                "model": row.model,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "gpu_hours": row.gpu_hours,
                "cost": row.cost,
                "currency": row.currency,
                "recorded": row.recorded_at.isoformat() if row.recorded_at else None,
            }
            for row in _all_usage(session)
            if caller.may_see(account=row.account, domain_path=row.domain)
        ]
        return {"count": len(rows), "usage": rows}

    @app.get(f"{PREFIX}/budgets", tags=["billing"], summary="List budgets")
    async def list_budgets(
        caller: CallerDep, services: ServicesDep, session: SessionDep
    ) -> dict[str, Any]:
        """Budgets and where each tenant stands against them."""
        store = UsageStore(session)

        # Two places declare a budget: platform.yaml as desired state,
        # and the database as what is in force. The stored one wins,
        # otherwise raising a limit through the API would appear to do
        # nothing while the spec's old number kept being shown.
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for tenant in services.spec.document.tenants:
            if tenant.budget is None:
                continue
            state = evaluate_budget(
                tenant.budget,
                spent=store.spent(
                    account=tenant.cloudstack_account, domain=tenant.cloudstack_domain
                ),
            )
            rows[(tenant.cloudstack_account, tenant.cloudstack_domain.rstrip("/"))] = {
                "id": tenant.name,
                "account": tenant.cloudstack_account,
                "domain": tenant.cloudstack_domain,
                "monthly": state.limit,
                "currency": state.currency,
                "spent": state.spent,
                "on_exceed": state.on_exceed,
                "percent_used": round(state.percent_used, 1),
                "blocked": state.should_block,
                "source": "spec",
            }

        for record in session.scalars(select(BudgetRecord)):
            stored = store.budget_state(record.account, record.domain)
            rows[(record.account, record.domain.rstrip("/"))] = {
                "id": record.id,
                "account": record.account,
                "domain": record.domain,
                "monthly": record.monthly,
                "currency": record.currency,
                "spent": stored.spent if stored else 0.0,
                "on_exceed": record.on_exceed,
                "percent_used": round(stored.percent_used, 1) if stored else 0.0,
                "blocked": stored.should_block if stored else False,
                "source": "api",
            }

        visible = [
            row
            for row in rows.values()
            if caller.may_see(account=row["account"], domain_path=row["domain"])
        ]
        return {"count": len(visible), "budgets": visible}

    # -- scoped keys ------------------------------------------------------

    @app.post(f"{PREFIX}/keys", tags=["billing"], summary="Create a scoped API key")
    async def create_key(
        caller: CallerDep, session: SessionDep, services: ServicesDep, body: KeyRequest
    ) -> dict[str, Any]:
        """Mint a key for the caller's own account (SPEC.md section 7).

        The plaintext is in this response and nowhere else, ever: only a
        per-key salt and hash are stored.
        """
        known = {m.name for m in services.spec.document.models}
        unknown = sorted(set(body.allowed_models) - known)
        if unknown:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such model(s): {', '.join(unknown)}",
                code="model_not_found",
            )

        try:
            minted = KeyStore(session).issue(
                account=caller.account,
                domain=caller.domain_path,
                name=body.name,
                allowed_models=body.allowed_models,
                cidrs=body.cidrs,
                expires_at=body.expires_at,
                rate_limit_rpm=body.rate_limit_rpm,
                parameters=body.parameters,
            )
        except ValueError as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, str(exc), code="invalid_key_scope") from exc

        return {
            "id": minted.key_id,
            "name": body.name,
            "apikey": minted.plaintext,
            "warning": "This key is shown once and cannot be retrieved again.",
        }

    @app.get(f"{PREFIX}/keys", tags=["billing"], summary="List scoped API keys")
    async def list_keys(caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        """A tenant's keys, with no secrets and the last-used time."""
        rows = [
            {
                "id": record.key_id,
                "name": record.name,
                "account": record.account,
                "allowedmodels": list(record.allowed_models or ()),
                "cidrs": list(record.cidrs or ()),
                "ratelimitrpm": record.rate_limit_rpm,
                "created": record.created_at.isoformat() if record.created_at else None,
                "lastused": record.last_used_at.isoformat() if record.last_used_at else None,
                "expires": record.expires_at.isoformat() if record.expires_at else None,
                "revoked": record.revoked_at is not None,
            }
            for record in KeyStore(session).list_for(
                account=caller.account, domain=caller.domain_path
            )
            if caller.may_see(account=record.account, domain_path=record.domain)
        ]
        return {"count": len(rows), "keys": rows}

    @app.delete(f"{PREFIX}/keys/{{key_id}}", tags=["billing"], summary="Revoke a key")
    async def revoke_key(key_id: str, caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        """Revoke a key the caller owns.

        Someone else's key answers 404 rather than 403: key ids are short
        and a 403 would confirm which ones exist.
        """
        store = KeyStore(session)
        try:
            record = store.record_for(key_id)
        except KeyError as exc:
            raise ApiError(
                status.HTTP_404_NOT_FOUND, f"no such key: {key_id}", code="key_not_found"
            ) from exc

        if not caller.may_see(account=record.account, domain_path=record.domain):
            raise ApiError(
                status.HTTP_404_NOT_FOUND, f"no such key: {key_id}", code="key_not_found"
            )

        store.revoke(key_id)
        return {"id": key_id, "revoked": True}

    # -- playground -------------------------------------------------------

    @app.post(
        f"{PREFIX}/playground/sessions",
        tags=["billing"],
        summary="Open a playground session",
    )
    async def create_playground_session(
        caller: CallerDep,
        session: SessionDep,
        services: ServicesDep,
        body: PlaygroundRequest,
    ) -> dict[str, Any]:
        """A 15-minute, model-scoped key plus copy-ready snippets.

        Short-lived and single-model because this key lives in a browser
        (SPEC.md section 7); the tenant's real keys never go there.
        """
        if not any(m.name == body.model for m in services.spec.document.models):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such model: {body.model}",
                code="model_not_found",
            )

        minted = issue_session_key(
            KeyStore(session),
            account=caller.account,
            domain=caller.domain_path,
            model=body.model,
            user_id=caller.user_id,
        )
        # The same base the endpoints list reports, so a snippet copied
        # from the playground points where the tenant's own code will.
        base = getattr(services, "gateway_url", "https://ai.example.invalid")
        base_url = f"{base.rstrip('/')}/v1"
        return {
            "id": minted.key_id,
            "apikey": minted.plaintext,
            "model": body.model,
            # The endpoint to call. Already computed for the snippets,
            # and a session without it leaves the caller holding a key
            # and no address to use it at.
            "base_url": base_url,
            "expires": (datetime.now(UTC) + SESSION_KEY_TTL).isoformat(),
            "snippets": [
                {"name": s.name, "language": s.language, "code": s.code}
                for s in snippets_for(base_url=base_url, model=body.model)
            ],
        }

    @app.post(f"{PREFIX}/budgets", tags=["billing"], summary="Set a budget")
    async def set_budget(_: AdminDep, session: SessionDep, body: BudgetRequest) -> dict[str, Any]:
        """Set or replace an account's monthly budget.

        Admin only: a tenant able to set its own budget could lift the
        limit that is blocking it.
        """
        record = UsageStore(session).set_budget(body.account, body.domain, body.as_budget())
        return _budget_row(record, UsageStore(session))

    @app.patch(f"{PREFIX}/budgets/{{budget_id}}", tags=["billing"], summary="Update a budget")
    async def update_budget(
        budget_id: str, _: AdminDep, session: SessionDep, body: BudgetPatch
    ) -> dict[str, Any]:
        """A budget is identified by its own id.

        Not by the account: names are unique only within a domain, so
        an account alone names two budgets in a platform with two
        tenants called `dev`.
        """
        store = UsageStore(session)
        existing = session.get(BudgetRecord, budget_id)
        if existing is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no budget with id {budget_id}",
                code="budget_not_found",
            )
        record = store.set_budget(existing.account, existing.domain, body.applied_to(existing))
        return _budget_row(record, store)

    @app.delete(f"{PREFIX}/budgets/{{budget_id}}", tags=["billing"], summary="Delete a budget")
    async def delete_budget(budget_id: str, _: AdminDep, session: SessionDep) -> dict[str, Any]:
        """Remove a budget, leaving that account unlimited in its domain."""
        record = session.get(BudgetRecord, budget_id)
        if record is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no budget with id {budget_id}",
                code="budget_not_found",
            )
        account, domain = record.account, record.domain
        session.delete(record)
        session.flush()
        return {"id": budget_id, "account": account, "domain": domain, "deleted": True}

    # -- tier 2: adapters -------------------------------------------------

    @app.post(
        f"{PREFIX}/adapters",
        tags=["adapters"],
        summary="Upload a LoRA adapter",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def upload_adapter(
        caller: CallerDep,
        session: SessionDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        body: AdapterBody,
    ) -> dict[str, Any]:
        """Register a tenant's adapter (SPEC.md section 8).

        Validated here rather than at load time because every one of
        these failures is otherwise silent until the first request.

        202 because section 9 makes `uploadAiAdapter` async, and the
        plugin refuses anything else: a command declared async treats a
        200 as "the engine did not accept this" rather than as success,
        on purpose, since a 200 carries no job to poll. Validation
        still answers 4xx, before any job exists — a caller who sent a
        bad rank wants a parameter error, not a job that failed.
        """
        store = AdapterStore(session)
        request = AdapterRequest(
            name=body.name,
            base_model=body.base_model,
            account=caller.account,
            domain=caller.domain_path,
            rank=body.rank,
            size_bytes=body.size_bytes,
            files=tuple(body.files),
            shared_with_domain=body.shared_with_domain,
        )

        try:
            validate_adapter(
                request,
                bases=[m.name for m in services.spec.document.models],
                max_lora_rank=_max_lora_rank(services, body.base_model),
                max_adapters_per_account=_adapter_limit(services, body.base_model),
                existing=len(store.list_for(account=caller.account, domain=caller.domain_path)),
            )
        except AdapterRejected as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, exc.detail, code=exc.reason) from exc

        job = jobs.create("uploadAiAdapter", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            record = store.add(request, uri=body.uri)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, _adapter_row(record))
        return {"jobid": job.id}

    @app.get(f"{PREFIX}/adapters", tags=["adapters"], summary="List LoRA adapters")
    async def list_adapters(caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        rows = [
            _adapter_row(record)
            for record in AdapterStore(session).visible_to(
                account=caller.account, domain=caller.domain_path, role=caller.role.value
            )
        ]
        return {"count": len(rows), "adapters": rows}

    @app.delete(f"{PREFIX}/adapters/{{adapter_id}}", tags=["adapters"], summary="Delete an adapter")
    async def delete_adapter(
        adapter_id: str, caller: CallerDep, session: SessionDep
    ) -> dict[str, Any]:
        store = AdapterStore(session)
        record = store.get(adapter_id)
        # Someone else's adapter answers as a missing one: the names
        # are guessable and a 403 would confirm which exist.
        if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such adapter: {adapter_id}",
                code="adapter_not_found",
            )
        store.delete(record)
        return {"id": adapter_id, "deleted": True}

    # -- tier 2: batches --------------------------------------------------

    @app.post(
        f"{PREFIX}/batches",
        tags=["batches"],
        summary="Submit a batch",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_batch(
        caller: CallerDep,
        session: SessionDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        body: BatchBody,
    ) -> dict[str, Any]:
        """Accept a JSONL batch (SPEC.md section 8).

        202 because section 9 makes `createAiBatch` async. The refusals
        below stay 4xx: an unparseable input file is the caller's to
        fix, and the plugin maps 400 onto a parameter error that names
        it. Only the accepted batch becomes a job.
        """
        if not any(m.name == body.model for m in services.spec.document.models):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such model: {body.model}",
                code="model_not_found",
            )

        try:
            requests = parse_requests(body.input, model=body.model)
        except BatchRejected as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, exc.detail, code=exc.reason) from exc

        job = jobs.create("createAiBatch", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            record = BatchStore(session).add(
                account=caller.account,
                domain=caller.domain_path,
                model=body.model,
                request_count=len(requests),
                input_uri=body.input_uri,
            )
            if body.input:
                # To the registry, not a column: these run to hundreds of
                # megabytes and the object store is already where large
                # immutable artefacts live.
                store_input(services.registry, record, body.input)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, _batch_row(record))
        return {"jobid": job.id}

    @app.get(
        f"{PREFIX}/batches/{{batch_id}}/results",
        tags=["batches"],
        summary="Download a batch's results",
        response_class=PlainTextResponse,
    )
    async def batch_results(
        batch_id: str, caller: CallerDep, session: SessionDep, services: ServicesDep
    ) -> PlainTextResponse:
        """The JSONL results, one object per line in OpenAI's shape."""
        record = BatchStore(session).get(batch_id)
        if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such batch: {batch_id}",
                code="batch_not_found",
            )

        if not record.output_uri:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"this batch is {record.state}; results are available once it has completed",
                code="results_not_ready",
            )

        try:
            body = read_output(services.registry, record)
        except Exception as exc:
            raise ApiError(
                status.HTTP_410_GONE,
                "these results are no longer stored; they may have passed their retention period",
                code="results_gone",
            ) from exc

        return PlainTextResponse(body, media_type="application/x-ndjson")

    @app.get(f"{PREFIX}/batches", tags=["batches"], summary="List batches")
    async def list_batches(caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        rows = [
            _batch_row(record)
            for record in BatchStore(session).all()
            if caller.may_see(account=record.account, domain_path=record.domain)
        ]
        return {"count": len(rows), "batches": rows}

    @app.post(f"{PREFIX}/batches/{{batch_id}}/cancel", tags=["batches"], summary="Cancel a batch")
    async def cancel_batch(batch_id: str, caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        store = BatchStore(session)
        record = store.get(batch_id)
        if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such batch: {batch_id}",
                code="batch_not_found",
            )
        if record.state in TERMINAL_BATCH_STATES:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"this batch is already {record.state} and cannot be cancelled",
                code="batch_not_cancellable",
            )
        store.cancel(record)
        return _batch_row(record)

    # -- tier 2: guardrail profiles ---------------------------------------

    @app.post(
        f"{PREFIX}/profiles/guardrail",
        tags=["profiles"],
        summary="Create a guardrail profile",
    )
    async def create_guardrail_profile(
        caller: CallerDep, session: SessionDep, body: GuardrailBody
    ) -> dict[str, Any]:
        """Scoped to the caller (SPEC.md sections 6, 8).

        Not admin-only. The objection that made it so — a guardrail an
        account can remove is not one — is answered by the scope rule
        rather than by shutting tenants out: they may write at their
        own level and no higher, so tightening their own account is
        theirs and the platform profile screening them is untouchable.
        """
        from aisvc.guardrails.authority import (
            GuardrailRejected,
            check_may_author,
            check_not_locked_above,
        )

        try:
            check_may_author(caller, scope=body.scope, target=body.target)
            check_not_locked_above(
                body.checks,
                scope=body.scope,
                higher=_guardrails_above(session, caller, body.scope),
            )
        except GuardrailRejected as exc:
            raise ApiError(exc.status_code, exc.detail, code=exc.reason) from exc

        checks = body.checks
        if body.preset:
            available = presets()
            if body.preset not in available:
                raise ApiError(
                    status.HTTP_404_NOT_FOUND,
                    f"no such guardrail preset: {body.preset}",
                    code="preset_not_found",
                )
            checks = {
                name: check.model_dump(exclude_none=True)
                for name, check in available[body.preset].items()
            }

        record = GuardrailStore(session).add(
            name=body.name, scope=body.scope, target=body.target, checks=checks or {}
        )
        return _guardrail_row(record)

    @app.get(f"{PREFIX}/profiles/guardrail", tags=["profiles"], summary="List guardrail profiles")
    async def list_guardrail_profiles(caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        """What screens this caller, plus what is theirs to change.

        Deliberately wider than what they may edit: a tenant who
        cannot read the platform profile cannot explain a block to
        their own customer.
        """
        from aisvc.guardrails.authority import profile_applies_to

        rows = [
            _guardrail_row(record)
            for record in GuardrailStore(session).all()
            if profile_applies_to(record, caller)
        ]
        return {"count": len(rows), "profiles": rows}

    @app.patch(
        f"{PREFIX}/profiles/guardrail/{{profile_id}}",
        tags=["profiles"],
        summary="Update a guardrail profile",
    )
    async def update_guardrail_profile(
        profile_id: str, caller: CallerDep, session: SessionDep, body: GuardrailPatch
    ) -> dict[str, Any]:
        from aisvc.guardrails.authority import (
            GuardrailRejected,
            check_may_author,
            check_not_locked_above,
            profile_applies_to,
        )

        store = GuardrailStore(session)
        record = store.get(profile_id)
        if record is None or not profile_applies_to(record, caller):
            # Not found rather than forbidden for one they cannot see:
            # otherwise this says which profile ids exist.
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such guardrail profile: {profile_id}",
                code="profile_not_found",
            )

        try:
            check_may_author(caller, scope=record.scope, target=record.target)
            if body.target is not None and body.target != record.target:
                # Checked at the new target too, or the account rule
                # would be a formality: create it for yourself, move it.
                check_may_author(caller, scope=record.scope, target=body.target)
            check_not_locked_above(
                body.checks,
                scope=record.scope,
                higher=_guardrails_above(session, caller, record.scope),
            )
        except GuardrailRejected as exc:
            raise ApiError(exc.status_code, exc.detail, code=exc.reason) from exc

        store.update(record, checks=body.checks, target=body.target)
        return _guardrail_row(record)

    @app.delete(
        f"{PREFIX}/profiles/guardrail/{{profile_id}}",
        tags=["profiles"],
        summary="Delete a guardrail profile",
    )
    async def delete_guardrail_profile(
        profile_id: str, caller: CallerDep, session: SessionDep
    ) -> dict[str, Any]:
        from aisvc.guardrails.authority import (
            GuardrailRejected,
            check_may_author,
            profile_applies_to,
        )

        store = GuardrailStore(session)
        record = store.get(profile_id)
        if record is not None and profile_applies_to(record, caller):
            try:
                check_may_author(caller, scope=record.scope, target=record.target)
            except GuardrailRejected as exc:
                raise ApiError(exc.status_code, exc.detail, code=exc.reason) from exc
        if record is None or not profile_applies_to(record, caller):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such guardrail profile: {profile_id}",
                code="profile_not_found",
            )
        store.delete(record)
        return {"id": profile_id, "deleted": True}

    # -- tier 2: evaluations ----------------------------------------------

    @app.post(
        f"{PREFIX}/models/{{model_name}}/evaluations",
        tags=["models"],
        summary="Record an evaluation",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def run_evaluation(
        model_name: str,
        caller: AdminDep,
        session: SessionDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        body: EvaluationBody,
    ) -> dict[str, Any]:
        """Judge a staging evaluation (SPEC.md section 8).

        The suites are produced elsewhere; this applies the gate and
        records the verdict against the catalog entry.

        202 because section 9 makes `runAiModelEvaluation` async. The
        caller was anonymous here — `_: AdminDep` — which was fine
        while nothing needed to know who asked; a job is owned, so it
        is named now.
        """
        if not any(m.name == model_name for m in services.spec.document.models):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such model: {model_name}",
                code="model_not_found",
            )

        job = jobs.create("runAiModelEvaluation", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            decision = gate(
                EvaluationRun(
                    model=model_name,
                    revision=body.revision,
                    suites=tuple(SuiteResult(**suite.model_dump()) for suite in body.suites),
                    performance=tuple(
                        PerformanceResult(**p.model_dump()) for p in body.performance
                    ),
                )
            )
            record = EvaluationStore(session).add(model_name, body.revision, decision)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, _evaluation_row(record))
        return {"jobid": job.id}

    @app.get(
        f"{PREFIX}/models/{{model_name}}/evaluations",
        tags=["models"],
        summary="List evaluations",
    )
    async def list_evaluations(model_name: str, _: AdminDep, session: SessionDep) -> dict[str, Any]:
        rows = [_evaluation_row(r) for r in EvaluationStore(session).for_model(model_name)]
        return {"count": len(rows), "evaluations": rows}

    @app.post(
        f"{PREFIX}/models/{{model_name}}/publish",
        tags=["models"],
        summary="Publish a model to tenants",
    )
    async def publish_model(
        model_name: str,
        caller: AdminDep,
        session: SessionDep,
        services: ServicesDep,
        body: PublishBody,
    ) -> dict[str, Any]:
        """Publish, if the evaluation gate allows it (SPEC.md section 8).

        A model with no evaluation at all is refused: an unevaluated
        revision is exactly what the gate exists to stop, and treating
        "never measured" as "no regression" would defeat it.
        """
        if not any(m.name == model_name for m in services.spec.document.models):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such model: {model_name}",
                code="model_not_found",
            )

        latest = EvaluationStore(session).latest(model_name)
        if latest is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{model_name} has never been evaluated; run an evaluation before publishing it",
                code="not_evaluated",
            )

        decision = GateDecision(
            passed=latest.passed,
            summary=latest.summary,
            overridden=latest.overridden,
        )
        if not decision.passed:
            if not body.override_reason or not body.override_reason.strip():
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    latest.summary,
                    code="evaluation_regressed",
                )
            try:
                decision = override_gate(
                    decision, role=caller.role.value, reason=body.override_reason
                )
            except GateBlocked as exc:
                raise ApiError(status.HTTP_409_CONFLICT, str(exc), code="override_refused") from exc

        return {
            "model": model_name,
            "published": True,
            "pricetier": body.price_tier,
            "overridden": decision.overridden,
            "summary": decision.summary,
        }

    # -- tier 2: certificates ---------------------------------------------

    @app.post(f"{PREFIX}/certificates", tags=["deployments"], summary="Upload a certificate")
    async def upload_certificate(
        caller: CallerDep, session: SessionDep, body: CertificateBody
    ) -> dict[str, Any]:
        """Accept a TLS pair for a custom domain (SPEC.md section 8).

        Validated here because every failure mode is otherwise late and
        unreadable: a mismatched key stops nginx hours later with "key
        values mismatch", and a certificate for the wrong domain is a
        browser warning nobody sees until a customer complains.
        """
        try:
            info = validate_certificate(
                body.certificate, body.private_key, domain=body.custom_domain
            )
        except CertificateRejected as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, exc.detail, code=exc.reason) from exc

        record = CertificateStore(session).add(
            account=caller.account,
            domain=caller.domain_path,
            custom_domain=body.custom_domain,
            certificate=body.certificate,
            private_key=body.private_key,
            fingerprint=info.fingerprint,
            not_after=info.not_after,
        )
        return _certificate_row(record)

    @app.get(f"{PREFIX}/certificates", tags=["deployments"], summary="List certificates")
    async def list_certificates(caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        """Metadata only. Neither the certificate nor the key is here."""
        rows = [
            _certificate_row(record)
            for record in CertificateStore(session).all()
            if caller.may_see(account=record.account, domain_path=record.domain)
        ]
        return {"count": len(rows), "certificates": rows}

    @app.get(
        f"{PREFIX}/certificates/{{certificate_id}}/material",
        tags=["deployments"],
        summary="Fetch certificate material",
        include_in_schema=False,
    )
    async def certificate_material(
        certificate_id: str, _: AdminDep, session: SessionDep
    ) -> dict[str, Any]:
        """The PEM pair, for the proxy that needs it.

        Admin only, and not in the published schema. The engine is
        reachable only over mTLS from the plugin and the proxies, and
        this is the one place key material leaves the platform — a
        tenant has no reason to call it, since they uploaded what it
        returns.
        """
        record = CertificateStore(session).get(certificate_id)
        if record is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such certificate: {certificate_id}",
                code="certificate_not_found",
            )
        return {
            "id": record.id,
            "certificate": record.certificate,
            "privatekey": record.private_key,
        }

    @app.post(
        f"{PREFIX}/certificates/bootstrap",
        tags=["deployments"],
        summary="Redeem a proxy bootstrap token",
        include_in_schema=False,
    )
    async def redeem_bootstrap_token(session: SessionDep, body: BootstrapBody) -> dict[str, Any]:
        """Exchange a one-time token for certificate material.

        **The one endpoint that takes no caller context.** A proxy has
        no identity at first boot — that is why the token exists — so
        this cannot ask for one. The token is the credential, and what
        makes that defensible is in `aisvc.certificates.bootstrap`:
        single use, short-lived, bound to one certificate, and burned
        in the same flush that returns the material.

        Out of the published schema: nothing a tenant or the plugin
        should ever call.
        """
        try:
            material = BootstrapTokens(session).redeem(body.token)
        except BootstrapRejected as exc:
            raise ApiError(status.HTTP_403_FORBIDDEN, exc.detail, code=exc.reason) from exc

        return {
            "id": material.certificate_id,
            "certificate": material.certificate,
            "privatekey": material.private_key,
        }

    @app.patch(
        f"{PREFIX}/certificates/{{certificate_id}}",
        tags=["deployments"],
        summary="Replace a certificate",
    )
    async def replace_certificate(
        certificate_id: str,
        caller: CallerDep,
        session: SessionDep,
        body: CertificateReplacement,
    ) -> dict[str, Any]:
        """Renew in place, keeping the id (SPEC.md section 8).

        The id stays because exposures name it: a new one would mean
        rewriting every exposure by hand, at exactly the moment the old
        certificate is about to stop working.
        """
        store = CertificateStore(session)
        record = store.get(certificate_id)
        if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such certificate: {certificate_id}",
                code="certificate_not_found",
            )

        try:
            info = validate_certificate(
                body.certificate, body.private_key, domain=record.custom_domain
            )
        except CertificateRejected as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, exc.detail, code=exc.reason) from exc

        # A replacement that expires sooner is a mistake every time,
        # and accepting it silently shortens the endpoint's life with
        # nothing to show for it.
        current = record.not_after
        if current is not None:
            aware = current if current.tzinfo is not None else current.replace(tzinfo=UTC)
            if info.not_after <= aware:
                raise ApiError(
                    status.HTTP_400_BAD_REQUEST,
                    f"this certificate expires sooner than the one it would "
                    f"replace ({info.not_after.isoformat()} against "
                    f"{aware.isoformat()})",
                    code="expires_sooner",
                )

        store.replace_material(
            record,
            certificate=body.certificate,
            private_key=body.private_key,
            fingerprint=info.fingerprint,
            not_after=info.not_after,
        )

        row = _certificate_row(record)
        # A running proxy fetched its material once at first boot and
        # has no way to be told. Saying so here beats an operator
        # discovering it when the old certificate stops.
        row["proxyredeploy"] = True
        return row

    @app.delete(
        f"{PREFIX}/certificates/{{certificate_id}}",
        tags=["deployments"],
        summary="Delete a certificate",
    )
    async def delete_certificate(
        certificate_id: str, caller: CallerDep, session: SessionDep
    ) -> dict[str, Any]:
        store = CertificateStore(session)
        record = store.get(certificate_id)
        if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such certificate: {certificate_id}",
                code="certificate_not_found",
            )
        store.delete(record)
        return {"id": certificate_id, "deleted": True}

    # -- tier 2: private endpoints ----------------------------------------

    @app.post(
        f"{PREFIX}/endpoints/private",
        tags=["deployments"],
        summary="Attach a private endpoint",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_private_endpoint(
        caller: CallerDep,
        session: SessionDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        body: PrivateEndpointBody,
    ) -> dict[str, Any]:
        """Record a private endpoint for the reconciler to build.

        202 because section 9 makes `createAiPrivateEndpoint` async,
        and here the job is more than a formality: what this writes is
        desired state, and the proxy VM that serves it is created on a
        later reconcile. The two refusals below stay 4xx — an unknown
        certificate or a network the account does not own is the
        caller's to fix.
        """
        exposure = EndpointExposure(
            deployment=body.deployment,
            account=caller.account,
            domain=caller.domain_path,
            public_enabled=body.public_enabled,
            network_id=body.network_id,
            custom_domain=body.custom_domain,
            tls=TlsSource(body.tls),
            certificate_id=body.certificate_id,
        )

        # A named certificate has to exist and be theirs. Otherwise the
        # proxy boots, cannot fetch one, and serves nothing — with the
        # failure hours away from the action that caused it.
        if body.tls == "uploaded" and body.certificate_id:
            known = CertificateStore(session).get(body.certificate_id)
            if known is None or not caller.may_see(account=known.account, domain_path=known.domain):
                raise ApiError(
                    status.HTTP_400_BAD_REQUEST,
                    f"no such certificate: {body.certificate_id}",
                    code="certificate_not_found",
                )

        try:
            validate_exposure(exposure, owned_networks=await _owned_networks(services, caller))
        except ExposureRejected as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, exc.detail, code=exc.reason) from exc

        job = jobs.create("createAiPrivateEndpoint", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            record = ExposureStore(session).add(exposure)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(
                job,
                {
                    "id": record.id,
                    "deployment": record.deployment,
                    "exposure": describe_exposure(exposure),
                },
            )
        return {"jobid": job.id}

    @app.get(
        f"{PREFIX}/endpoints/private",
        tags=["deployments"],
        summary="List private endpoints",
    )
    async def list_private_endpoints(caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        """Which deployments are exposed, and how.

        Create and delete shipped without this, so a tenant had no way
        to learn the id delete wants, and no screen could show what
        they had. Filtered by the caller like every other list.
        """
        rows = [
            _exposure_row(record)
            for record in ExposureStore(session).all()
            if caller.may_see(account=record.account, domain_path=record.domain)
        ]
        return {"count": len(rows), "endpoints": rows}

    @app.delete(
        f"{PREFIX}/endpoints/private/{{exposure_id}}",
        tags=["deployments"],
        summary="Remove a private endpoint",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_private_endpoint(
        exposure_id: str,
        caller: CallerDep,
        session: SessionDep,
        jobs: JobsDep,
        response: Response,
    ) -> dict[str, Any]:
        """Drop a private endpoint; the reconciler destroys its proxy.

        202 because section 9 makes `deleteAiPrivateEndpoint` async.
        An unknown id stays a 404, before the job: nothing was asked
        for, so there is nothing to report the outcome of.
        """
        store = ExposureStore(session)
        record = store.get(exposure_id)
        if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such private endpoint: {exposure_id}",
                code="exposure_not_found",
            )

        job = jobs.create("deleteAiPrivateEndpoint", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            store.delete(record)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, {"id": exposure_id, "deleted": True})
        return {"jobid": job.id}

    # -- profiles ---------------------------------------------------------

    @app.get(
        f"{PREFIX}/profiles/serving",
        tags=["profiles"],
        summary="List serving profiles",
    )
    async def list_serving_profiles(_: AdminDep, session: SessionDep) -> dict[str, Any]:
        """Root admin's alone: these are the engine's own arguments."""
        rows = [_serving_profile_row(r) for r in ServingProfileStore(session).all()]
        return {"count": len(rows), "profiles": rows}

    @app.post(
        f"{PREFIX}/profiles/serving",
        tags=["profiles"],
        summary="Create a serving profile",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_serving_profile(
        caller: AdminDep,
        session: SessionDep,
        jobs: JobsDep,
        response: Response,
        body: ServingProfileBody,
    ) -> dict[str, Any]:
        """Engine arguments, platform-wide (SPEC.md section 6).

        Admin-only because a serving profile is vLLM's own
        configuration: a tenant setting `gpu_memory_utilization` could
        take a host down for everyone sharing it. Generation profiles
        are the tenant-facing half.
        """
        params = _profile_params(body.params)
        store = ServingProfileStore(session)
        _refuse_duplicate_profile(store.by_name(body.name), body.name)

        job = jobs.create("createAiServingProfile", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            record = store.add(name=body.name, params=params)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, _serving_profile_row(record))
        return {"jobid": job.id}

    @app.patch(
        f"{PREFIX}/profiles/serving/{{profile_id}}",
        tags=["profiles"],
        summary="Update a serving profile",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def update_serving_profile(
        profile_id: str,
        caller: AdminDep,
        session: SessionDep,
        jobs: JobsDep,
        response: Response,
        body: ProfileUpdateBody,
    ) -> dict[str, Any]:
        params = _profile_params(body.params)
        store = ServingProfileStore(session)
        record = store.get(profile_id)
        if record is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such serving profile: {profile_id}",
                code="profile_not_found",
            )

        job = jobs.create("updateAiServingProfile", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            store.update(record, params=params)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, _serving_profile_row(record))
        return {"jobid": job.id}

    @app.delete(
        f"{PREFIX}/profiles/serving/{{profile_id}}",
        tags=["profiles"],
        summary="Delete a serving profile",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_serving_profile(
        profile_id: str,
        caller: AdminDep,
        session: SessionDep,
        jobs: JobsDep,
        response: Response,
    ) -> dict[str, Any]:
        store = ServingProfileStore(session)
        record = store.get(profile_id)
        if record is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such serving profile: {profile_id}",
                code="profile_not_found",
            )

        name = record.name
        job = jobs.create("deleteAiServingProfile", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            AssignmentStore(session).drop_profile(serving_profile=name)
            store.delete(record)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, {"id": profile_id, "deleted": True})
        return {"jobid": job.id}

    @app.get(
        f"{PREFIX}/profiles/generation",
        tags=["profiles"],
        summary="List generation profiles",
    )
    async def list_generation_profiles(caller: CallerDep, session: SessionDep) -> dict[str, Any]:
        """Filtered by caller, like any other tenant-owned row."""
        rows = [
            _generation_profile_row(record)
            for record in GenerationProfileStore(session).all()
            if caller.may_see(account=record.account, domain_path=record.domain)
        ]
        return {"count": len(rows), "profiles": rows}

    @app.post(
        f"{PREFIX}/profiles/generation",
        tags=["profiles"],
        summary="Create a generation profile",
    )
    async def create_generation_profile(
        caller: CallerDep, session: SessionDep, body: GenerationProfileBody
    ) -> dict[str, Any]:
        """A tenant's own tuning (SPEC.md section 6).

        Creating one binds nothing: an assignment does that, and it is
        the assignment the authority rules are checked against. So a
        tenant may describe any parameters they like here and still
        not be able to impose them on anybody.
        """
        params = _generation_params(body.params)
        store = GenerationProfileStore(session)
        _refuse_duplicate_profile(store.by_name(body.name), body.name)

        record = store.add(
            name=body.name,
            params=params,
            account=caller.account,
            domain=caller.domain_path,
        )
        return _generation_profile_row(record)

    @app.patch(
        f"{PREFIX}/profiles/generation/{{profile_id}}",
        tags=["profiles"],
        summary="Update a generation profile",
    )
    async def update_generation_profile(
        profile_id: str,
        caller: CallerDep,
        session: SessionDep,
        body: ProfileUpdateBody,
    ) -> dict[str, Any]:
        params = _generation_params(body.params)
        store = GenerationProfileStore(session)
        record = _own_generation_profile(store, caller, profile_id)
        store.update(record, params=params)
        return _generation_profile_row(record)

    @app.delete(
        f"{PREFIX}/profiles/generation/{{profile_id}}",
        tags=["profiles"],
        summary="Delete a generation profile",
    )
    async def delete_generation_profile(
        profile_id: str, caller: CallerDep, session: SessionDep
    ) -> dict[str, Any]:
        store = GenerationProfileStore(session)
        record = _own_generation_profile(store, caller, profile_id)
        AssignmentStore(session).drop_profile(generation_profile=record.name)
        store.delete(record)
        return {"id": profile_id, "deleted": True}

    @app.post(
        f"{PREFIX}/profiles/assignments",
        tags=["profiles"],
        summary="Assign a tuning profile",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def assign_tuning_profile(
        caller: CallerDep,
        session: SessionDep,
        jobs: JobsDep,
        response: Response,
        body: AssignmentBody,
    ) -> dict[str, Any]:
        """Bind a profile at a scope (SPEC.md section 6).

        This is where authority is checked, not at creation: the rules
        are the same ones guardrail profiles are written under — a
        caller binds at their own level and no higher — because the
        question is identical. Binding is what imposes something on
        somebody.
        """
        from aisvc.guardrails.authority import GuardrailRejected, check_may_author

        serving = ServingProfileStore(session).by_name(body.profile)
        generation = GenerationProfileStore(session).by_name(body.profile)
        if serving is None and generation is None:
            # An assignment naming nothing resolves to nothing, so the
            # operator would see a binding that does not bind.
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such tuning profile: {body.profile}",
                code="profile_not_found",
            )

        try:
            check_may_author(caller, scope=body.scope, target=body.target)
        except GuardrailRejected as exc:
            raise ApiError(exc.status_code, exc.detail, code=exc.reason) from exc

        if serving is not None and not caller.role.is_admin:
            raise ApiError(
                status.HTTP_403_FORBIDDEN,
                "a serving profile is the engine's own configuration and is "
                "bound by root admin only",
                code="not_permitted",
            )

        job = jobs.create("assignAiTuningProfile", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"
        try:
            record = AssignmentStore(session).add(
                scope=body.scope,
                target=body.target,
                serving_profile=serving.name if serving is not None else "",
                generation_profile=generation.name if generation is not None else "",
            )
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(job, _assignment_row(record))
        return {"jobid": job.id}

    @app.get(
        f"{PREFIX}/profiles/effective",
        tags=["profiles"],
        summary="Effective parameters",
    )
    async def effective_parameters(
        caller: CallerDep,
        services: ServicesDep,
        session: SessionDep,
        model: str = Query(description="Deployment to resolve parameters for"),
    ) -> dict[str, Any]:
        """Every parameter's resolved value and the level that set it.

        The same `aisvc.profiles.resolve()` the gateway enforces with, so
        this view can never disagree with what is actually applied.
        """
        declared = {m.name: m for m in services.spec.document.models}
        found = declared.get(model)
        if found is None or not _model_visible(found, caller):
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no model {model!r}", code="not_found")

        layers = _layers_for(services, caller, model, session)
        described = resolve(layers).describe()
        # `count` beside the list, like every other list response, so
        # the plugin's list mapping has the shape it expects.
        return {"model": model, "count": len(described), "parameters": described}

    # -- Hugging Face -----------------------------------------------------

    @app.get(f"{PREFIX}/hf/search", tags=["sourcing"], summary="Search the Hub")
    async def hf_search(
        _: AdminDep,
        services: ServicesDep,
        query: str = Query(min_length=1),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        sourcing = _sourcing(services)
        results = sourcing.search(query, limit=limit)
        return {
            "count": len(results),
            "models": [
                {
                    "repo_id": r.repo_id,
                    "license": r.license,
                    "gated": r.gated,
                    "task": r.pipeline_tag,
                }
                for r in results
            ],
        }

    @app.get(f"{PREFIX}/hf/check", tags=["sourcing"], summary="Compatibility report")
    async def hf_check(
        _: AdminDep,
        services: ServicesDep,
        repo_id: str = Query(min_length=1),
        revision: str = Query(min_length=1),
    ) -> dict[str, Any]:
        """Metadata only: nothing is downloaded, so gating is reported up front."""
        sourcing = _sourcing(services)
        report = sourcing.check(repo_id, revision, gpu_classes=services.spec.document.gpu_classes)
        return {
            "repo_id": report.repo_id,
            "revision": report.revision,
            "compatible": report.compatible,
            "license": report.license,
            "architecture": report.architecture,
            "download_bytes": report.download_bytes,
            "context_length": report.context_length,
            "gated": report.gated,
            "requires_remote_code": report.requires_remote_code,
            "problems": report.problems,
            "warnings": report.warnings,
            "placements": [
                {
                    "gpu_class": p.gpu_class,
                    "tensor_parallel": p.tensor_parallel,
                    "fits": p.fits,
                    "shortfall_gb": p.shortfall_gb,
                }
                for p in report.placements
            ],
        }

    @app.post(
        f"{PREFIX}/models/mirror",
        tags=["sourcing"],
        summary="Mirror a model",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def mirror_model(
        caller: AdminDep,
        services: ServicesDep,
        jobs: JobsDep,
        response: Response,
        repo_id: str = Query(min_length=1),
        revision: str = Query(min_length=1),
    ) -> dict[str, Any]:
        """Start a mirror. Returns 202 with a job id to poll."""
        sourcing = _sourcing(services)
        job = jobs.create("mirrorAiModel", caller)
        response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"

        try:
            manifest = sourcing.mirror(repo_id, revision, domain=caller.domain_path)
        except Exception as exc:
            jobs.fail(job, str(exc))
        else:
            jobs.succeed(
                job,
                {
                    "repo_id": manifest.repo_id,
                    "revision": manifest.revision,
                    "total_bytes": manifest.total_bytes,
                },
            )
        return {"jobid": job.id}


def _replicas_on(services: Services, node_id: str) -> int:
    """How many replicas the node currently carries."""
    cluster = getattr(services, "cluster", None)
    if cluster is None:
        return 0
    return sum(
        deployed.replicas_ready
        for deployed in observe_deployments(cluster).values()
        if node_id in getattr(deployed, "nodes", (node_id,))
    )


async def _rollout_job(
    command: str,
    deployment_id: str,
    caller: Any,
    services: Services,
    jobs: Any,
    response: Response,
    *,
    promote: bool,
) -> dict[str, Any]:
    """Promote or roll back, recording the outcome on a job.

    The weights this produces are what the gateway applies; nothing in
    the serving layer moves traffic (ADR-0001).
    """
    model, _, suffix = deployment_id.rpartition("-")
    if suffix not in {"blue", "green"}:
        model = deployment_id

    # Which colour is live is read from the gateway's weights, never
    # from the id in the URL (CLAUDE.md: it is derived, never stored).
    # Taking it from the URL meant promoting the deployment you named
    # actually promoted the other one, because start_rollout's `active`
    # is the colour currently serving.
    cluster = getattr(services, "cluster", None)
    job = jobs.create(command, caller)
    response.headers["Location"] = f"{PREFIX}/jobs/{job.id}"

    if cluster is None:
        jobs.fail(
            job,
            "the engine has no serving cluster configured, so the traffic split "
            "could not be applied; set AISVC_KUBECONFIG and retry",
        )
        return {"jobid": job.id}

    split = observe_weights(cluster).get(model) or {}
    live = max(split, key=lambda c: split[c]) if split else "blue"

    try:
        rollout = start_rollout(model, active=live)
        settled = rollout.promote() if promote else rollout.rollback()
    except RolloutError as exc:
        jobs.fail(job, str(exc))
        return {"jobid": job.id}

    # Apply the split before reporting it. Milestone 7 recorded the
    # weights on the job and stopped there, so a promotion through the
    # API moved no traffic at all and the next reconcile could not tell
    # a promotion had happened — live colour is derived from these.
    try:
        deployer = ServingDeployer(cluster, services.spec, services.registry)
        await deployer.apply_routes(weights={settled.model: settled.weights()})
    except Exception as exc:
        jobs.fail(job, f"could not apply the traffic split: {exc}")
        return {"jobid": job.id}

    jobs.succeed(
        job,
        {
            "deployment": {
                "id": deployment_id,
                "model": settled.model,
                "colour": settled.live_colour,
                "state": settled.phase.value,
            },
            "weights": settled.weights(),
            "retire": settled.colour_to_remove,
        },
    )
    return {"jobid": job.id}


def _sourcing(services: Services) -> Any:
    if services.sourcing is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Hugging Face sourcing is not configured on this engine",
            code="unavailable",
        )
    return services.sourcing


def _model_visible(model: Any, caller: Any) -> bool:
    """Catalog visibility (SPEC.md section 5, step 8)."""
    if caller.role.is_admin:
        return True
    visibility = model.visibility
    if not visibility.domains and not visibility.accounts:
        return True  # published to all tenants
    if caller.account in visibility.accounts:
        return True
    return any(
        caller.domain_path == domain or caller.domain_path.startswith(f"{domain}/")
        for domain in visibility.domains
    )


def _model_summary(model: Any, services: Services) -> dict[str, Any]:
    mirrored = False
    if model.source_is_pinned:
        try:
            mirrored = services.registry.has(model.repo_id, model.revision or "")
        except Exception:
            mirrored = False
    return {
        "name": model.name,
        "task": model.task,
        "engine": model.engine,
        "license": model.license,
        "price_tier": model.price_tier,
        "max_model_len": model.max_model_len,
        "mirrored": mirrored,
    }


def _placeable(services: Services, name: str) -> tuple[Any, Any]:
    """The catalogued model and its pool, if the API may place it.

    Three refusals, and the middle one is the point of ADR-0008: a
    model the file places is not this command's to move, and saying so
    by naming the field is what tells an operator how to change their
    mind.
    """
    model = next((m for m in services.spec.document.models if m.name == name), None)
    if model is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"no such model: {name}",
            code="model_not_found",
        )
    if model.placement != "api":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{name} has placement: spec, so platform.yaml decides where it runs "
            f"and it is already deployed there. Set placement: api on the model "
            f"to hand that decision to this command.",
            code="placement_is_spec",
        )
    pool = next((p for p in services.spec.document.node_pools if p.name == model.node_pool), None)
    if pool is None:  # pragma: no cover - validation refuses this spec
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{name} names node pool {model.node_pool}, which the spec does not declare",
            code="pool_not_found",
        )
    return model, pool


def _check_zone(services: Services, pool: Any, zone_id: str) -> None:
    """The zone has to be the model's own.

    A node pool already determines it, so a `zoneid` naming another is
    a caller who believes something untrue — and a row carrying it
    would disagree with the spec for as long as it existed. An empty
    one is taken as "the model's zone", since there is only one.

    Two spellings are accepted because two callers use two. The plugin
    resolves CloudStack's zone id to its *name*, which `platform.yaml`
    records as `cloudstack_zone`; a direct caller has the spec's own
    zone name in front of them. Either resolves to the same zone, and
    anything else is refused.
    """
    if not zone_id:
        return

    named = [
        zone
        for zone in services.spec.document.zones
        if zone_id in (zone.name, zone.cloudstack_zone)
    ]
    if named and named[0].name == pool.zone:
        return

    raise ApiError(
        status.HTTP_400_BAD_REQUEST,
        f"this model runs in zone {pool.zone}, because its node pool {pool.name} "
        f"is there; it cannot be placed in {zone_id}",
        code="wrong_zone",
    )


def _placement_replicas(model: Any, wanted: int | None) -> int:
    """A replica count the model's declared bounds allow.

    Refused rather than clamped: a tenant who asks for fifty and is
    quietly given eight has been told nothing, and will ask again.
    """
    bounds = model.replicas
    if wanted is None:
        return int(bounds.min)
    if wanted < bounds.min or wanted > bounds.max:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            f"{model.name} declares replicas between {bounds.min} and {bounds.max}, "
            f"so {wanted} cannot be served; change the bounds in platform.yaml first",
            code="replicas_out_of_bounds",
        )
    return int(wanted)


def _own_placement(store: Any, caller: Any, placement_id: str) -> Any:
    """A placement this caller may change.

    404 rather than 403 for someone else's, as everywhere: an id they
    cannot see is one they should not be able to confirm.
    """
    record = store.get(placement_id)
    if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"no such deployment: {placement_id}",
            code="deployment_not_found",
        )
    return record


def _placement_row(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "model": record.model,
        "zone": record.zone,
        "replicas": record.replicas,
        "serving_profile": record.serving_profile,
        "placement": "api",
    }


def _profile_params(raw: dict[str, Any] | str) -> dict[str, Any]:
    """The parameters a profile was sent, however they were spelled.

    `createAiServingProfile` declares `params` a STRING, so CloudStack
    sends JSON text; everything else that talks to this API has a dict
    already. Both are accepted, and anything that is not an object is
    refused here rather than stored and misread later.
    """
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except ValueError as exc:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            f"params is not valid JSON: {exc}",
            code="invalid_params",
        ) from exc
    if not isinstance(parsed, dict):
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "params must be a JSON object of parameter names to values",
            code="invalid_params",
        )
    return parsed


def _generation_params(raw: dict[str, Any] | str) -> dict[str, Any]:
    """Generation parameters, validated against the spec's own model.

    Every value is a `GenerationValue` — a default, optional bounds and
    a policy of `default`, `clamp` or `locked`. Validating here rather
    than at resolution matters: a policy the resolver has no branch for
    would otherwise be stored happily and then ignored on every single
    request, which is a locked parameter silently not locking.
    """
    from pydantic import ValidationError

    from aisvc.spec.models import GenerationValue

    params = _profile_params(raw)
    checked: dict[str, Any] = {}
    for name, value in params.items():
        try:
            checked[name] = GenerationValue.model_validate(value).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise ApiError(
                status.HTTP_400_BAD_REQUEST,
                f"{name} is not a usable generation parameter: {exc.errors()[0]['msg']}",
                code="invalid_params",
            ) from exc
    return checked


def _refuse_duplicate_profile(existing: Any, name: str) -> None:
    """Names are how assignments bind, so they have to be unique.

    Two profiles sharing a name would make a precedence chain whose
    outcome depends on which row was found first.
    """
    if existing is not None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"a tuning profile called {name!r} already exists",
            code="profile_exists",
        )


def _own_generation_profile(store: Any, caller: Any, profile_id: str) -> Any:
    """A generation profile this caller may change.

    404 rather than 403 for someone else's: an id they cannot see is
    an id they should not be able to confirm the existence of.
    """
    record = store.get(profile_id)
    if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"no such generation profile: {profile_id}",
            code="profile_not_found",
        )
    return record


def _serving_profile_row(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "params": dict(record.params or {}),
        "created": record.created_at.isoformat() if record.created_at else "",
    }


def _generation_profile_row(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "params": dict(record.params or {}),
        "account": record.account,
        "domain": record.domain,
        "created": record.created_at.isoformat() if record.created_at else "",
    }


def _assignment_row(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "scope": record.scope,
        "target": record.target,
        "serving_profile": record.serving_profile,
        "generation_profile": record.generation_profile,
    }


def _layers_for(
    services: Services, caller: Any, model: str, session: Any = None
) -> list[ProfileLayer]:
    """Assemble the precedence chain for this caller and deployment.

    Two sources, one chain. `platform.yaml` declares profiles and
    assignments, and so does the API — a spec profile was written by
    whoever edits the spec, a stored one by a tenant — and both have to
    reach the same `resolve()` the gateway enforces with, or a profile
    would show in the console and change nothing about a request.

    Stored assignments are appended after the spec's, so at the same
    level the stored one wins. That is the right way round: the spec is
    what an operator set as the baseline, and a tenant tightening their
    own account is the later, more specific statement.
    """
    profiles: dict[str, Any] = {
        p.name: p.params for p in services.spec.document.tuning_profiles.generation
    }
    assignments: list[Any] = list(services.spec.document.assignments)

    if session is not None:
        # Back into the spec's own `GenerationValue`, because that is
        # what `resolve()` reads — it asks each value for `.min`,
        # `.policy` and the rest. Stored rows are plain JSON, so a dict
        # handed straight to the resolver fails on the first bound.
        from aisvc.spec.models import GenerationValue

        for record in GenerationProfileStore(session).all():
            if not caller.may_see(account=record.account, domain_path=record.domain):
                continue
            profiles[record.name] = {
                name: GenerationValue.model_validate(value)
                for name, value in (record.params or {}).items()
            }
        assignments.extend(AssignmentStore(session).all())

    layers: list[ProfileLayer] = []
    scope_levels = {
        "platform": (Level.PLATFORM, ""),
        "domain": (Level.DOMAIN, caller.domain_path),
        "account": (Level.ACCOUNT, caller.account),
        "deployment": (Level.DEPLOYMENT, model),
    }
    for assignment in assignments:
        if not assignment.generation_profile:
            continue
        mapped = scope_levels.get(assignment.scope)
        if mapped is None:
            continue
        level, target = mapped
        if level is Level.DOMAIN:
            # A domain binds over everything beneath it, so an exact
            # match would miss /Customers/Acme/Sub under /Customers/Acme.
            from aisvc.guardrails.authority import domain_contains

            if not domain_contains(assignment.target, caller.domain_path):
                continue
        elif level is not Level.PLATFORM and assignment.target != target:
            continue
        params = profiles.get(assignment.generation_profile)
        if params is not None:
            layers.append(
                ProfileLayer(level=level, name=assignment.generation_profile, params=params)
            )
    return layers
