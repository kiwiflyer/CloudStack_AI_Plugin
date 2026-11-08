"""The engine REST API (SPEC.md section 10).

Called only by the Java plugin and the operator CLI, over mTLS, and
never exposed to tenants. The section 9 command map is the authoritative
endpoint list; the FastAPI-generated OpenAPI schema is the contract.

Everything here filters by caller context. Async commands return 202
with a job id the plugin polls at `/v1/jobs/{id}`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Query, Response, status

from aisvc.api.deps import AdminDep, CallerDep, JobsDep, ServicesDep, SessionDep
from aisvc.api.errors import ApiError, register_error_handlers
from aisvc.db import RunLog
from aisvc.k8s.client import observe_deployments
from aisvc.profiles import Level, ProfileLayer, resolve

if TYPE_CHECKING:
    from aisvc.api.deps import Services

__all__ = ["API_VERSION", "create_app"]

API_VERSION = "v1"
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
