"""Applying model deployments and routes (SPEC.md section 4).

The executor's counterpart to `plan_models` and `plan_routes`: it turns
a planned action into server-side applies against the cluster.

The LiteLLM config is written as a ConfigMap rather than a file, so the
gateway Deployment can mount it and the reconciler can converge it like
anything else.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import yaml

from aisvc.gateway import build_litellm_config
from aisvc.k8s import (
    DEFAULT_NAMESPACE,
    DeploymentTarget,
    config_hash,
    image_for,
    render,
)
from aisvc.k8s.client import Kubernetes, apply_objects, delete_deployment, observe_weights
from aisvc.reconciler.plan import route_config_hash

if TYPE_CHECKING:
    from aisvc.registry import ModelRegistry
    from aisvc.spec import PlatformSpec

__all__ = [
    "GATEWAY_CONFIGMAP",
    "ROUTE_HASH_ANNOTATION",
    "DeployerError",
    "ServingDeployer",
]

GATEWAY_CONFIGMAP = "aisvc-gateway-config"
ROUTE_HASH_ANNOTATION = "aisvc.io/route-hash"


class DeployerError(Exception):
    """A model deployment or route could not be applied."""


class ServingDeployer:
    """Applies what `plan_models` and `plan_routes` decided."""

    def __init__(
        self,
        cluster: Kubernetes,
        spec: PlatformSpec,
        registry: ModelRegistry,
        *,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        self.cluster = cluster
        self.spec = spec
        self.registry = registry
        self.namespace = namespace

    def _target_for(self, name: str) -> DeploymentTarget:
        """Resolve an action name like `my-model-blue` back to a target."""
        for model in self.spec.document.models:
            for colour in ("blue", "green"):
                if name != f"{model.name}-{colour}":
                    continue
                pool = next(
                    (p for p in self.spec.document.node_pools if p.name == model.node_pool),
                    None,
                )
                if pool is None:
                    raise DeployerError(f"{model.name}: node pool {model.node_pool!r} is gone")
                revision = model.revision or ""
                return DeploymentTarget(
                    model=model,
                    pool=pool,
                    gpu_class=self.spec.gpu_class(pool.gpu_class),
                    serving_profile=self.spec.serving_profile_for(model),
                    image=image_for(model),
                    replicas=model.replicas.min,
                    weights_uri=self.registry.uri_for(model.repo_id, revision),
                    colour=colour,
                )
        raise DeployerError(f"no model in the spec matches deployment {name!r}")

    async def apply_model(self, name: str, *, dry_run: bool = False) -> list[str]:
        """Create or update one colour of one model."""
        target = self._target_for(name)
        objects = render(target, namespace=self.namespace)
        # Stamped with the replica-independent hash, so a scale does not
        # read as a spec change on the next plan.
        apply_objects(
            self.cluster,
            objects,
            dry_run=dry_run,
            digest=config_hash(target, namespace=self.namespace),
        )
        return [f"{obj['kind']}/{obj['metadata']['name']}" for obj in objects]

    async def delete_model(self, name: str, *, dry_run: bool = False) -> list[str]:
        """Remove one colour of one model, whatever the spec now says.

        Resolved from the name rather than the spec, since the whole
        reason to delete is that the spec no longer mentions it.
        """
        model, _, colour = name.rpartition("-")
        if not model or colour not in {"blue", "green"}:
            raise DeployerError(f"{name!r} is not a <model>-<colour> deployment name")
        delete_deployment(self.cluster, model, colour, namespace=self.namespace, dry_run=dry_run)
        return [name]

    async def apply_routes(
        self,
        _name: str = "litellm",
        *,
        weights: Mapping[str, Mapping[str, int]] | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        """Write the gateway's routing config as a ConfigMap.

        `weights` given explicitly is a rollout moving traffic. Left
        out — which is what the executor does on every ordinary run —
        it means "keep the split that is in force", read back off the
        ConfigMap written last time.

        That default is the important one. The split is runtime state
        that only a rollout changes, so if an ordinary reconcile
        rendered at a fixed split it would send every model back to
        blue and silently undo a promotion.
        """
        if weights is None:
            weights = observe_weights(self.cluster, namespace=self.namespace)
        config = build_litellm_config(self.spec, weights=weights, namespace=self.namespace)
        configmap: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": GATEWAY_CONFIGMAP,
                "namespace": self.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "aisvc",
                    "app.kubernetes.io/component": "gateway",
                },
                # The digest of the config rendered at a normalised
                # split, not of the one written below. Read back by
                # observe_route_hash and compared against what
                # plan_routes computes, so a promotion — which changes
                # the written config but not the spec — does not look
                # like drift and get reverted on the next run.
                "annotations": {
                    ROUTE_HASH_ANNOTATION: route_config_hash(
                        build_litellm_config(self.spec, weights=None, namespace=self.namespace)
                    )
                },
            },
            "data": {"config.yaml": yaml.safe_dump(config, sort_keys=False)},
        }
        apply_objects(self.cluster, [configmap], dry_run=dry_run)
        return [f"ConfigMap/{GATEWAY_CONFIGMAP}"]
