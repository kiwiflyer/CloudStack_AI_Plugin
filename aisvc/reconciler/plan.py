"""The typed diff between desired and actual state (SPEC.md section 4).

`observe() -> diff() -> plan()`. Nothing here writes; execution is the
executor's job.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from aisvc.k8s import DEFAULT_NAMESPACE, DeploymentTarget, image_for, render, spec_hash

if TYPE_CHECKING:
    from aisvc.inventory import CapacitySnapshot
    from aisvc.reconciler.state import ActualState
    from aisvc.registry import ModelRegistry
    from aisvc.spec import NodePool, PlatformSpec

__all__ = [
    "ACTIVE_COLOUR",
    "Action",
    "ActionKind",
    "Plan",
    "config_hash",
    "plan_all",
    "plan_capacity",
]

#: v1 serves one colour; blue/green rollouts arrive with `lifecycle`
#: in milestone 7, which is what introduces green.
ACTIVE_COLOUR = "blue"


class ActionKind(Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"

    @property
    def sigil(self) -> str:
        return {"create": "+", "update": "~", "delete": "-"}[self.value]


@dataclass(frozen=True, slots=True)
class Action:
    """One intended change, printable as a plan line."""

    kind: ActionKind
    resource_type: str
    name: str
    count: int = 1
    detail: str = ""
    reason: str = ""
    #: Keys of actions that must succeed before this one may run.
    depends_on: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Identifies this action within a plan, for dependency edges."""
        return f"{self.resource_type}:{self.name}:{self.kind.value}"

    def render(self) -> str:
        line = f"{self.kind.sigil} {self.resource_type} {self.name} x{self.count}"
        if self.detail:
            line += f"  ({self.detail})"
        if self.reason:
            line += f"\n    reason: {self.reason}"
        return line


@dataclass(frozen=True)
class Plan:
    """A typed diff, printed like `terraform plan` before anything runs."""

    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.actions)

    @property
    def to_add(self) -> int:
        return sum(a.count for a in self.actions if a.kind is ActionKind.CREATE)

    @property
    def to_remove(self) -> int:
        return sum(a.count for a in self.actions if a.kind is ActionKind.DELETE)

    def render(self) -> str:
        lines: list[str] = []
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"  ! {w}" for w in self.warnings)
            lines.append("")
        if self.notes:
            lines.append("Notes:")
            lines.extend(f"  - {n}" for n in self.notes)
            lines.append("")

        if not self.actions:
            lines.append("No changes. Desired state matches actual state.")
            return "\n".join(lines)

        lines.append("Planned actions:")
        lines.extend(f"  {a.render()}" for a in self.actions)
        lines.append("")
        lines.append(f"Plan: {self.to_add} to add, {self.to_remove} to remove.")
        return "\n".join(lines)


def plan_capacity(spec: PlatformSpec, snapshot: CapacitySnapshot) -> Plan:
    """Diff declared node pools against the GPU nodes that exist.

    A pool is sized in node VMs: below `min_nodes` plans creation, above
    `max_nodes` plans removal, and anything in the band is left alone so
    autoscaling can own it (SPEC.md section 4). Whether the hosts to place
    those nodes on exist is a separate question, answered by the class
    capacity and reported as a warning rather than silently planned.
    """
    actions: list[Action] = []
    warnings: list[str] = list(spec.warnings)
    notes: list[str] = list(spec.notes)

    classes = {c.name: c for c in spec.document.gpu_classes}

    for pool in spec.document.node_pools:
        observed = snapshot.for_pool(pool.name)
        if observed is None:
            warnings.append(f"nodePools.{pool.name}: not observed, so its size is unknown")
            continue

        gpu_class = classes.get(pool.gpu_class)
        offering = gpu_class.service_offering if gpu_class else "unknown offering"
        count = observed.node_count

        if count < pool.min_nodes:
            wanted = pool.min_nodes - count
            _warn_if_no_room(spec, snapshot, pool, wanted, warnings)
            actions.append(
                Action(
                    kind=ActionKind.CREATE,
                    resource_type="gpu-node",
                    name=pool.name,
                    count=wanted,
                    detail=f"service offering {offering}, template {pool.template}",
                    reason=(f"{count} node(s) in the pool, min_nodes is {pool.min_nodes}"),
                )
            )
        elif count > pool.max_nodes:
            actions.append(
                Action(
                    kind=ActionKind.DELETE,
                    resource_type="gpu-node",
                    name=pool.name,
                    count=count - pool.max_nodes,
                    detail=f"service offering {offering}",
                    reason=(f"{count} node(s) in the pool, max_nodes is {pool.max_nodes}"),
                )
            )

    return Plan(actions=actions, warnings=warnings, notes=notes)


def _warn_if_no_room(
    spec: PlatformSpec,
    snapshot: CapacitySnapshot,
    pool: NodePool,
    wanted: int,
    warnings: list[str],
) -> None:
    """Note when the zone has fewer free GPU hosts than the pool needs."""
    capacity = snapshot.for_class(pool.zone, pool.gpu_class)
    if capacity is None:
        warnings.append(
            f"nodePools.{pool.name}: GPU class {pool.gpu_class!r} in zone "
            f"{pool.zone!r} was not observed, so placement cannot be checked"
        )
        return
    if capacity.nodes_free < wanted:
        warnings.append(
            f"nodePools.{pool.name}: {wanted} node(s) to add but only "
            f"{capacity.nodes_free} free {pool.gpu_class} host(s) in "
            f"{pool.zone}; some deployments may fail for want of capacity"
        )


def plan_models(
    spec: PlatformSpec,
    state: ActualState,
    *,
    registry: ModelRegistry,
    namespace: str = DEFAULT_NAMESPACE,
    capacity_actions: Sequence[Action] = (),
) -> tuple[list[Action], list[str]]:
    """Diff declared models against what the cluster is serving.

    A model is only deployed once its weights are in the registry: a
    deployment pointing at an unmirrored revision would start, fail to
    load and crash-loop, which is worse than not starting.
    """
    actions: list[Action] = []
    warnings: list[str] = []

    node_action_for = {
        action.name: action
        for action in capacity_actions
        if action.resource_type == "gpu-node" and action.kind is ActionKind.CREATE
    }
    declared: set[tuple[str, str]] = set()

    for model in spec.document.models:
        declared.add((model.name, ACTIVE_COLOUR))

        if not model.source_is_pinned:
            warnings.append(
                f"models.{model.name}: source does not pin a commit SHA, so it cannot be deployed"
            )
            continue

        revision = model.revision or ""
        if not registry.has(model.repo_id, revision):
            warnings.append(
                f"models.{model.name}: {model.repo_id}@{revision[:12]} is not "
                "mirrored yet, so it will not be deployed"
            )
            continue

        pool = next((p for p in spec.document.node_pools if p.name == model.node_pool), None)
        if pool is None:  # already reported by validation
            continue

        target = DeploymentTarget(
            model=model,
            pool=pool,
            gpu_class=spec.gpu_class(pool.gpu_class),
            serving_profile=spec.serving_profile_for(model),
            image=image_for(model),
            replicas=model.replicas.min,
            weights_uri=registry.uri_for(model.repo_id, revision),
            colour=ACTIVE_COLOUR,
        )
        desired_hash = spec_hash(render(target, namespace=namespace))
        current = state.deployment(model.name, ACTIVE_COLOUR)

        depends_on: tuple[str, ...] = ()
        if (node_action := node_action_for.get(pool.name)) is not None:
            depends_on = (node_action.key,)

        detail = (
            f"{model.replicas.min} replica(s) of {target.image} "
            f"on {pool.name}, weights {target.weights_uri}"
        )

        if current is None:
            actions.append(
                Action(
                    kind=ActionKind.CREATE,
                    resource_type="model-deployment",
                    name=target.name,
                    count=1,
                    detail=detail,
                    reason="not deployed",
                    depends_on=depends_on,
                )
            )
        elif current.spec_hash != desired_hash:
            actions.append(
                Action(
                    kind=ActionKind.UPDATE,
                    resource_type="model-deployment",
                    name=target.name,
                    count=1,
                    detail=detail,
                    reason=(
                        f"spec changed: running {current.spec_hash[:12] or 'unknown'}, "
                        f"want {desired_hash[:12]}"
                    ),
                    depends_on=depends_on,
                )
            )

    for (name, colour), deployed in sorted(state.deployments.items()):
        if (name, colour) in declared:
            continue
        actions.append(
            Action(
                kind=ActionKind.DELETE,
                resource_type="model-deployment",
                name=f"{name}-{colour}",
                count=1,
                detail=f"{deployed.replicas_desired} replica(s)",
                reason="no longer declared in the spec",
            )
        )

    return actions, warnings


def plan_routes(
    spec: PlatformSpec,
    state: ActualState,
    *,
    model_actions: Sequence[Action] = (),
    namespace: str = DEFAULT_NAMESPACE,
) -> list[Action]:
    """Diff the gateway's routing config against what the spec implies."""
    from aisvc.gateway import build_litellm_config

    servable = [m for m in spec.document.models]
    if not servable:
        return []

    config = build_litellm_config(spec, weights=None, namespace=namespace)
    digest = config_hash(config)
    if state.route_hash == digest:
        return []

    depends_on = tuple(
        action.key
        for action in model_actions
        if action.resource_type == "model-deployment" and action.kind is not ActionKind.DELETE
    )

    return [
        Action(
            kind=ActionKind.UPDATE,
            resource_type="gateway-config",
            name="litellm",
            count=1,
            detail=f"{len(config['model_list'])} route(s), digest {digest}",
            reason="routing config differs from what the gateway has",
            depends_on=depends_on,
        )
    ]


def config_hash(config: dict[str, object]) -> str:
    """Digest of a rendered gateway config, used to detect drift."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def plan_all(
    spec: PlatformSpec,
    state: ActualState,
    *,
    registry: ModelRegistry,
    namespace: str = DEFAULT_NAMESPACE,
) -> Plan:
    """The whole plan, in dependency order: capacity, models, routes.

    Profiles sit between models and routes in SPEC.md section 4; they are
    rendered into the deployments and the gateway config rather than
    being a stage of their own, so there is nothing separate to plan.
    """
    capacity = plan_capacity(spec, state.capacity)
    models, model_warnings = plan_models(
        spec,
        state,
        registry=registry,
        namespace=namespace,
        capacity_actions=capacity.actions,
    )
    routes = plan_routes(spec, state, model_actions=models, namespace=namespace)

    return Plan(
        actions=[*capacity.actions, *models, *routes],
        warnings=[*capacity.warnings, *model_warnings],
        notes=capacity.notes,
    )
