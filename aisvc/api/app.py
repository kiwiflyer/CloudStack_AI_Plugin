"""The engine REST API (SPEC.md section 10).

Called only by the Java plugin and the operator CLI, over mTLS, and
never exposed to tenants. The section 9 command map is the authoritative
endpoint list; the FastAPI-generated OpenAPI schema is the contract.

Everything here filters by caller context. Async commands return 202
with a job id the plugin polls at `/v1/jobs/{id}`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, Query, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
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
from aisvc.db import BudgetRecord, KeyStore, RunLog, UsageStore
from aisvc.db.tier2 import (
    TERMINAL_BATCH_STATES,
    AdapterStore,
    BatchStore,
    EvaluationStore,
    ExposureStore,
    GuardrailStore,
)
from aisvc.endpoints import (
    EndpointExposure,
    ExposureRejected,
    TlsSource,
    describe_exposure,
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
from aisvc.k8s.client import observe_deployments, observe_weights
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
    state = store.budget_state(record.account)
    return {
        "account": record.account,
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


def _owned_networks(services: Services, caller: Any) -> list[str]:
    """Networks this caller may attach to.

    Supplied by the plugin, which asked CloudStack: the engine has no
    view of a tenant's VPC tiers and must not guess at one.
    """
    lookup = getattr(services, "networks_for", None)
    if lookup is None:
        return []
    networks: list[str] = list(lookup(caller.account, caller.domain_path))
    return networks


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
        rows: dict[str, dict[str, Any]] = {}
        for tenant in services.spec.document.tenants:
            if tenant.budget is None:
                continue
            state = evaluate_budget(
                tenant.budget, spent=store.spent(account=tenant.cloudstack_account)
            )
            rows[tenant.cloudstack_account] = {
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
            stored = store.budget_state(record.account)
            existing = rows.get(record.account, {})
            rows[record.account] = {
                "id": existing.get("id", record.account),
                "account": record.account,
                "domain": existing.get("domain", "/"),
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
            for record in KeyStore(session).list_for(account=caller.account)
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
        record = UsageStore(session).set_budget(body.account, body.as_budget())
        return _budget_row(record, UsageStore(session))

    @app.patch(f"{PREFIX}/budgets/{{budget_id}}", tags=["billing"], summary="Update a budget")
    async def update_budget(
        budget_id: str, _: AdminDep, session: SessionDep, body: BudgetPatch
    ) -> dict[str, Any]:
        """A budget is identified by the account it belongs to."""
        account = budget_id
        store = UsageStore(session)
        existing = session.get(BudgetRecord, account)
        if existing is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no budget for account {account}",
                code="budget_not_found",
            )
        record = store.set_budget(account, body.applied_to(existing))
        return _budget_row(record, store)

    @app.delete(f"{PREFIX}/budgets/{{budget_id}}", tags=["billing"], summary="Delete a budget")
    async def delete_budget(budget_id: str, _: AdminDep, session: SessionDep) -> dict[str, Any]:
        """Remove a budget, leaving the account unlimited."""
        account = budget_id
        record = session.get(BudgetRecord, account)
        if record is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no budget for account {account}",
                code="budget_not_found",
            )
        session.delete(record)
        session.flush()
        return {"account": account, "deleted": True}

    # -- tier 2: adapters -------------------------------------------------

    @app.post(f"{PREFIX}/adapters", tags=["adapters"], summary="Upload a LoRA adapter")
    async def upload_adapter(
        caller: CallerDep, session: SessionDep, services: ServicesDep, body: AdapterBody
    ) -> dict[str, Any]:
        """Register a tenant's adapter (SPEC.md section 8).

        Validated here rather than at load time because every one of
        these failures is otherwise silent until the first request.
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

        record = store.add(request, uri=body.uri)
        return _adapter_row(record)

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

    @app.post(f"{PREFIX}/batches", tags=["batches"], summary="Submit a batch")
    async def create_batch(
        caller: CallerDep, session: SessionDep, services: ServicesDep, body: BatchBody
    ) -> dict[str, Any]:
        """Accept a JSONL batch (SPEC.md section 8)."""
        if not any(m.name == body.model for m in services.spec.document.models):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such model: {body.model}",
                code="model_not_found",
            )

        try:
            requests = parse_requests(body.input)
        except BatchRejected as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, exc.detail, code=exc.reason) from exc

        record = BatchStore(session).add(
            account=caller.account,
            domain=caller.domain_path,
            model=body.model,
            request_count=len(requests),
            input_uri=body.input_uri,
        )
        return _batch_row(record)

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
        _: AdminDep, session: SessionDep, body: GuardrailBody
    ) -> dict[str, Any]:
        """Admin only: a guardrail an account can remove is not one."""
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
    async def list_guardrail_profiles(_: AdminDep, session: SessionDep) -> dict[str, Any]:
        rows = [_guardrail_row(r) for r in GuardrailStore(session).all()]
        return {"count": len(rows), "profiles": rows}

    @app.patch(
        f"{PREFIX}/profiles/guardrail/{{profile_id}}",
        tags=["profiles"],
        summary="Update a guardrail profile",
    )
    async def update_guardrail_profile(
        profile_id: str, _: AdminDep, session: SessionDep, body: GuardrailPatch
    ) -> dict[str, Any]:
        store = GuardrailStore(session)
        record = store.get(profile_id)
        if record is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such guardrail profile: {profile_id}",
                code="profile_not_found",
            )
        store.update(record, checks=body.checks, target=body.target)
        return _guardrail_row(record)

    @app.delete(
        f"{PREFIX}/profiles/guardrail/{{profile_id}}",
        tags=["profiles"],
        summary="Delete a guardrail profile",
    )
    async def delete_guardrail_profile(
        profile_id: str, _: AdminDep, session: SessionDep
    ) -> dict[str, Any]:
        store = GuardrailStore(session)
        record = store.get(profile_id)
        if record is None:
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
    )
    async def run_evaluation(
        model_name: str,
        _: AdminDep,
        session: SessionDep,
        services: ServicesDep,
        body: EvaluationBody,
    ) -> dict[str, Any]:
        """Judge a staging evaluation (SPEC.md section 8).

        The suites are produced elsewhere; this applies the gate and
        records the verdict against the catalog entry.
        """
        if not any(m.name == model_name for m in services.spec.document.models):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such model: {model_name}",
                code="model_not_found",
            )

        decision = gate(
            EvaluationRun(
                model=model_name,
                revision=body.revision,
                suites=tuple(SuiteResult(**suite.model_dump()) for suite in body.suites),
                performance=tuple(PerformanceResult(**p.model_dump()) for p in body.performance),
            )
        )
        record = EvaluationStore(session).add(model_name, body.revision, decision)
        return _evaluation_row(record)

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

    # -- tier 2: private endpoints ----------------------------------------

    @app.post(
        f"{PREFIX}/endpoints/private",
        tags=["deployments"],
        summary="Attach a private endpoint",
    )
    async def create_private_endpoint(
        caller: CallerDep,
        session: SessionDep,
        services: ServicesDep,
        body: PrivateEndpointBody,
    ) -> dict[str, Any]:
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

        try:
            validate_exposure(exposure, owned_networks=_owned_networks(services, caller))
        except ExposureRejected as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, exc.detail, code=exc.reason) from exc

        record = ExposureStore(session).add(exposure)
        return {
            "id": record.id,
            "deployment": record.deployment,
            "exposure": describe_exposure(exposure),
        }

    @app.delete(
        f"{PREFIX}/endpoints/private/{{exposure_id}}",
        tags=["deployments"],
        summary="Remove a private endpoint",
    )
    async def delete_private_endpoint(
        exposure_id: str, caller: CallerDep, session: SessionDep
    ) -> dict[str, Any]:
        store = ExposureStore(session)
        record = store.get(exposure_id)
        if record is None or not caller.may_see(account=record.account, domain_path=record.domain):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no such private endpoint: {exposure_id}",
                code="exposure_not_found",
            )
        store.delete(record)
        return {"id": exposure_id, "deleted": True}

    # -- profiles ---------------------------------------------------------

    @app.get(
        f"{PREFIX}/profiles/effective",
        tags=["profiles"],
        summary="Effective parameters",
    )
    async def effective_parameters(
        caller: CallerDep,
        services: ServicesDep,
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

        layers = _layers_for(services, caller, model)
        return {"model": model, "parameters": resolve(layers).describe()}

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


def _layers_for(services: Services, caller: Any, model: str) -> list[ProfileLayer]:
    """Assemble the precedence chain for this caller and deployment."""
    profiles = {p.name: p for p in services.spec.document.tuning_profiles.generation}
    layers: list[ProfileLayer] = []

    scope_levels = {
        "domain": (Level.DOMAIN, caller.domain_path),
        "account": (Level.ACCOUNT, caller.account),
        "deployment": (Level.DEPLOYMENT, model),
    }
    for assignment in services.spec.document.assignments:
        if not assignment.generation_profile:
            continue
        mapped = scope_levels.get(assignment.scope)
        if mapped is None:
            continue
        level, target = mapped
        if assignment.target != target:
            continue
        profile = profiles.get(assignment.generation_profile)
        if profile is not None:
            layers.append(ProfileLayer(level=level, name=profile.name, params=profile.params))
    return layers
