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

from aisvc.autoscale import decide
from aisvc.k8s import (
    DEFAULT_NAMESPACE,
    DeploymentTarget,
    config_hash,
    image_for,
)

if TYPE_CHECKING:
    from typing import Any

    from aisvc.inventory import CapacitySnapshot
    from aisvc.reconciler.state import ActualState
    from aisvc.registry import ModelRegistry
    from aisvc.spec import NodePool, PlatformSpec
    from aisvc.spec.models import Model

__all__ = [
    "ACTIVE_COLOUR",
    "Action",
    "ActionKind",
    "Plan",
    "plan_all",
    "plan_capacity",
    "plan_clusters",
    "route_config_hash",
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
    #: The name CloudStack knows the zone by, where the action needs
    #: one to be carried out. Without it the executor could not
    #: resolve a cluster it had not planned itself, and passed an
    #: empty zone that CloudStack answers with "has no zone named ''".
    zone: str = ""
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


def plan_clusters(spec: PlatformSpec, state: ActualState) -> tuple[list[Action], list[str]]:
    """One action per zone that asked for a cluster and has none.

    Resolution happened at observe time, which is why the line can name
    the Kubernetes version and the service offering: where the operator
    pinned neither, the engine chose them, and one of them is billable.
    An operator should read that in the plan rather than on an invoice.

    A zone that cannot be built warns instead. There is no action to
    take, and failing the whole plan would stop every other zone.
    """
    actions: list[Action] = []
    warnings: list[str] = []
    existing = {found.zone for found in state.clusters if found.state is not None}

    for zone in spec.document.zones:
        if not zone.kubernetes.create or zone.name in existing:
            continue

        problem = state.cluster_problems.get(zone.name)
        if problem:
            warnings.append(f"zones.{zone.name}: cannot create {zone.cks_cluster}: {problem}")
            continue

        planned = state.cluster_plans.get(zone.name)
        if planned is None:
            continue

        actions.append(
            Action(
                kind=ActionKind.CREATE,
                resource_type="kubernetes-cluster",
                name=planned.cluster,
                zone=planned.zone,
                detail=planned.detail,
                reason=f"zone {zone.name} asks for a cluster and CloudStack has none",
            )
        )

    return actions, warnings


def _cluster_action_for(spec: PlatformSpec, cluster_actions: list[Action]) -> dict[str, str]:
    """Pool name to the key of the cluster action it must wait for.

    A GPU node that boots with nowhere to join is a billed VM doing
    nothing, and nothing later in the plan can repair it.
    """
    by_cluster = {action.name: action.key for action in cluster_actions}
    zones = {zone.name: zone.cks_cluster for zone in spec.document.zones}
    return {
        pool.name: by_cluster[zones[pool.zone]]
        for pool in spec.document.node_pools
        if pool.zone in zones and zones[pool.zone] in by_cluster
    }


def plan_capacity(
    spec: PlatformSpec,
    snapshot: CapacitySnapshot,
    *,
    waits_for: dict[str, str] | None = None,
) -> Plan:
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
    waits = waits_for or {}

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
                    depends_on=(waits[pool.name],) if pool.name in waits else (),
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
    placements: Sequence[Any] = (),
) -> tuple[list[Action], list[str]]:
    """Diff declared models against what the cluster is serving.

    A model is only deployed once its weights are in the registry: a
    deployment pointing at an unmirrored revision would start, fail to
    load and crash-loop, which is worse than not starting.

    `placements` carries the rows behind `placement: api` models
    (ADR-0008). A model the spec places is planned from the spec, as
    it always was. A model that hands placement to the API is planned
    only where a row says so — and **not being declared is the point**:
    that absence is what `deleteAiModelDeployment` leaves behind, and
    what makes the sweep below tear its Deployment down.
    """
    actions: list[Action] = []
    warnings: list[str] = []

    placed_in = {(record.model, record.zone): record for record in placements}

    node_action_for = {
        action.name: action
        for action in capacity_actions
        if action.resource_type == "gpu-node" and action.kind is ActionKind.CREATE
    }
    declared: set[tuple[str, str]] = set()

    for model in spec.document.models:
        if not model.source_is_pinned:
            warnings.append(
                f"models.{model.name}: source does not pin a commit SHA, so it cannot be deployed"
            )
            declared.add((model.name, ACTIVE_COLOUR))
            continue

        revision = model.revision or ""
        if not registry.has(model.repo_id, revision):
            warnings.append(
                f"models.{model.name}: {model.repo_id}@{revision[:12]} is not "
                "mirrored yet, so it will not be deployed"
            )
            declared.add((model.name, ACTIVE_COLOUR))
            continue

        pool = next((p for p in spec.document.node_pools if p.name == model.node_pool), None)
        if pool is None:  # already reported by validation
            continue

        placement = placed_in.get((model.name, pool.zone))
        if model.placement == "api" and placement is None:
            # Catalogued, and placed nowhere. Deliberately left out of
            # `declared`, so anything already serving it is swept.
            continue

        live = state.live_colour(model.name)
        candidate = _other_colour(live)
        blue_green = model.rollout.strategy == "bluegreen"

        # Both colours are declared during a rollout, so neither is
        # mistaken for an orphan and deleted out from under it.
        declared.add((model.name, live))

        deployed_live = state.deployment(model.name, live)
        deployed_candidate = state.deployment(model.name, candidate)
        rolling = deployed_live is not None and deployed_candidate is not None

        replicas = _replicas_for(model, state, deployed_live, rolling=rolling)
        if placement is not None and deployed_live is None:
            # What the caller asked for, within the bounds the API
            # already checked against the spec. Once it is running the
            # autoscaler owns the count, exactly as for a spec model.
            replicas = int(placement.replicas)
        depends_on = _depends_on(node_action_for, pool.name)

        live_hash = _target_hash(spec, model, pool, registry, replicas, live, namespace)

        if deployed_live is None:
            actions.append(
                _deployment_action(
                    ActionKind.CREATE,
                    model,
                    pool,
                    replicas,
                    live,
                    reason="not deployed",
                    depends_on=depends_on,
                    registry=registry,
                )
            )
            continue

        spec_unchanged = deployed_live.spec_hash == live_hash

        if spec_unchanged and rolling and _rollout_finished(state, model.name, candidate):
            # The candidate carries no traffic: a promote or rollback
            # settled, and the loser is swept up below.
            continue

        if spec_unchanged and not rolling:
            if deployed_live.replicas_desired != replicas:
                # Scaling, not a spec change: in place, no rollout.
                actions.append(
                    _deployment_action(
                        ActionKind.UPDATE,
                        model,
                        pool,
                        replicas,
                        live,
                        reason=_scale_reason(model, state)
                        or f"replica count is {deployed_live.replicas_desired}, want {replicas}",
                        depends_on=depends_on,
                        registry=registry,
                    )
                )
            continue

        if not blue_green:
            if not spec_unchanged or deployed_live.replicas_desired != replicas:
                actions.append(
                    _deployment_action(
                        ActionKind.UPDATE,
                        model,
                        pool,
                        replicas,
                        live,
                        reason=_changed_reason(deployed_live, live_hash),
                        depends_on=depends_on,
                        registry=registry,
                    )
                )
            continue

        # Blue/green: bring the other colour up to the new spec and leave
        # the live one serving until an operator promotes
        # (ADR-0001 put the weights at the gateway, not here).
        declared.add((model.name, candidate))
        candidate_hash = _target_hash(spec, model, pool, registry, replicas, candidate, namespace)

        if deployed_candidate is None:
            actions.append(
                _deployment_action(
                    ActionKind.CREATE,
                    model,
                    pool,
                    replicas,
                    candidate,
                    reason=(
                        f"spec changed and rollout.strategy is bluegreen, so "
                        f"{candidate} is brought up beside {live}"
                    ),
                    depends_on=depends_on,
                    registry=registry,
                )
            )
        elif deployed_candidate.spec_hash != candidate_hash:
            actions.append(
                _deployment_action(
                    ActionKind.UPDATE,
                    model,
                    pool,
                    replicas,
                    candidate,
                    reason=_changed_reason(deployed_candidate, candidate_hash),
                    depends_on=depends_on,
                    registry=registry,
                )
            )
        # Otherwise the candidate matches: waiting on promote or rollback,
        # which are operator actions through the API.

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
                reason=_retire_reason(name, colour, state),
            )
        )

    return actions, warnings


def plan_routes(
    spec: PlatformSpec,
    state: ActualState,
    *,
    model_actions: Sequence[Action] = (),
    proxy_actions: Sequence[Action] = (),
    namespace: str = DEFAULT_NAMESPACE,
) -> list[Action]:
    """Diff the gateway's routing config against what the spec implies."""
    from aisvc.gateway import build_litellm_config

    servable = [m for m in spec.document.models]
    if not servable:
        return []

    config = build_litellm_config(spec, weights=None, namespace=namespace)
    digest = route_config_hash(config)
    if state.route_hash == digest:
        return []

    # Creates only. A model leaving the public gateway must wait for
    # its private route to exist, or the tenant has no route at all for
    # a whole reconcile. A *delete* re-publishes the model, which
    # should not wait on tearing a VM down.
    proxy_dependencies = tuple(
        action.key for action in proxy_actions if action.kind is ActionKind.CREATE
    )
    depends_on = (
        tuple(
            action.key
            for action in model_actions
            if action.resource_type == "model-deployment" and action.kind is not ActionKind.DELETE
        )
        + proxy_dependencies
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


def route_config_hash(config: dict[str, object]) -> str:
    """Digest of a rendered gateway config, used to detect drift.

    Always given the config rendered at a *normalised* traffic split
    (`weights=None`), never the one actually written. The split is
    runtime state — a promotion sets it, the spec never does — so a
    digest that moved with it would make every promotion look like a
    spec change, rewrite the routes and revert the promotion. Same
    reason `config_hash` digests at a normalised replica count
    (CLAUDE.md). `ServingDeployer.apply_routes` is what holds to this.
    """
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def plan_proxies(spec: PlatformSpec, state: ActualState) -> list[Action]:
    """Diff the per-tenant gateway proxies against the exposures.

    A proxy is created for every exposure attached to a network, and
    destroyed once its exposure is gone — a VM left behind keeps a NIC
    on the tenant's tier, which is precisely what removing the endpoint
    was meant to take away.

    Only names this engine would have minted are ours to remove. A VM
    on the same network that someone else created is not ours to
    expunge, however much it looks like one.
    """
    from aisvc.endpoints.proxy import NAME_PREFIX, plan_proxy

    actions: list[Action] = []
    wanted: dict[str, Any] = {}

    for exposure in state.exposures:
        plan = plan_proxy(exposure, zone_id="", gateway_host="")
        if plan is None:
            continue

        zone = _zone_for(spec, exposure)
        if zone is None or not zone.proxy_template or not zone.proxy_offering:
            # Warned about in plan_all; planning a VM with no template
            # would fail at execute time with a CloudStack error an
            # operator cannot act on.
            continue

        wanted[plan.name] = exposure
        if plan.name in state.proxies:
            if _superseded(exposure, state):
                # A proxy fetched its material once at first boot and
                # cannot be told about a replacement, so renewing means
                # replacing the VM. Downtime, and better than a proxy
                # quietly serving a certificate that has expired.
                delete = Action(
                    kind=ActionKind.DELETE,
                    resource_type="endpoint-proxy",
                    name=plan.name,
                    reason="its certificate has been renewed",
                )
                actions.append(delete)
                actions.append(
                    Action(
                        kind=ActionKind.CREATE,
                        resource_type="endpoint-proxy",
                        name=plan.name,
                        detail=f"on network {exposure.network_id}",
                        reason="to serve the renewed certificate",
                        # CloudStack will not hold two VMs of one name.
                        depends_on=(delete.key,),
                    )
                )
            continue

        actions.append(
            Action(
                kind=ActionKind.CREATE,
                resource_type="endpoint-proxy",
                name=plan.name,
                detail=f"on network {exposure.network_id}",
                reason=(f"{exposure.account} has a private endpoint for {exposure.deployment}"),
            )
        )

    for name in sorted(state.proxies):
        if name in wanted or not name.startswith(NAME_PREFIX):
            continue
        actions.append(
            Action(
                kind=ActionKind.DELETE,
                resource_type="endpoint-proxy",
                name=name,
                reason="its private endpoint has been removed",
            )
        )

    return actions


def _superseded(exposure: Any, state: ActualState) -> bool:
    """Whether this exposure's proxy holds out-of-date material.

    An unknown certificate does not count: the engine not seeing one
    must not make the planner replace a working proxy on every run.
    """
    certificate_id = getattr(exposure, "certificate_id", "")
    if not certificate_id:
        return False

    current = state.certificate_serials.get(certificate_id)
    if current is None:
        return False

    built_with = getattr(exposure, "proxy_certificate_serial", 0) or 0
    return bool(built_with) and built_with != current


def _zone_for(spec: PlatformSpec, exposure: Any) -> Any:
    """The zone a deployment's proxy belongs in.

    One zone per platform today, so this picks the first; it exists so
    the multi-zone case has somewhere obvious to go.
    """
    _ = exposure
    zones = spec.document.zones
    return zones[0] if zones else None


def proxy_warnings(spec: PlatformSpec, state: ActualState) -> list[str]:
    """Exposures that cannot be built for want of zone configuration."""
    warnings: list[str] = []
    for exposure in state.exposures:
        if not exposure.network_id:
            continue
        zone = _zone_for(spec, exposure)
        if zone is None or not zone.proxy_template or not zone.proxy_offering:
            warnings.append(
                f"{exposure.deployment} has a private endpoint for "
                f"{exposure.account}, but its zone sets no proxy_template and "
                "proxy_offering, so no proxy can be built for it"
            )
    return warnings


def plan_all(
    spec: PlatformSpec,
    state: ActualState,
    *,
    registry: ModelRegistry,
    namespace: str = DEFAULT_NAMESPACE,
    placements: Sequence[Any] = (),
) -> Plan:
    """The whole plan, in dependency order: capacity, models, routes.

    Profiles sit between models and routes in SPEC.md section 4; they are
    rendered into the deployments and the gateway config rather than
    being a stage of their own, so there is nothing separate to plan.
    """
    clusters, cluster_warnings = plan_clusters(spec, state)
    capacity = plan_capacity(spec, state.capacity, waits_for=_cluster_action_for(spec, clusters))
    models, model_warnings = plan_models(
        spec,
        state,
        registry=registry,
        namespace=namespace,
        capacity_actions=capacity.actions,
        placements=placements,
    )
    proxies = plan_proxies(spec, state)
    routes = plan_routes(
        spec, state, model_actions=models, proxy_actions=proxies, namespace=namespace
    )

    return Plan(
        actions=[*clusters, *capacity.actions, *models, *proxies, *routes],
        warnings=[
            *cluster_warnings,
            *capacity.warnings,
            *model_warnings,
            *proxy_warnings(spec, state),
        ],
        notes=capacity.notes,
    )


def _other_colour(colour: str) -> str:
    return "green" if colour == "blue" else "blue"


def _depends_on(node_action_for: dict[str, Action], pool_name: str) -> tuple[str, ...]:
    action = node_action_for.get(pool_name)
    return (action.key,) if action is not None else ()


def _changed_reason(deployed: Any, desired: str) -> str:
    return f"spec changed: running {deployed.spec_hash[:12] or 'unknown'}, want {desired[:12]}"


def _retire_reason(name: str, colour: str, state: ActualState) -> str:
    """Why a deployment is being removed.

    A colour left over from a finished rollout reads very differently
    from a model somebody deleted, and an operator should be able to
    tell which they are looking at.
    """
    if state.weights.get(name, {}).get(colour) == 0:
        return f"rollout finished; {colour} no longer carries traffic"
    return "no longer declared in the spec"


def _replicas_for(
    model: Model,
    state: ActualState,
    deployed: Any,
    *,
    rolling: bool,
) -> int:
    """How many replicas this model should run.

    The spec's minimum is a floor, not a target: once metrics exist,
    `autoscale` decides within the declared band. Scaling is skipped
    mid-rollout, because moving replicas while traffic is shifting makes
    both harder to reason about.
    """
    floor = model.replicas.min
    if deployed is None or rolling:
        return floor

    ceiling = model.replicas.max
    health = state.health.get(model.name)
    if health is None:
        # No metrics: keep what is running, but inside the declared band.
        # A raised floor must still take effect, or a spec change to
        # replicas would be silently ignored.
        return max(floor, min(deployed.replicas_desired or floor, ceiling))

    decision = decide(
        health.metrics,
        slo={
            "ttft_p95_ms": model.slo.ttft_p95_ms,
            "queue_depth_target": model.slo.queue_depth_target,
        },
        replicas=deployed.replicas_desired or floor,
        minimum=floor,
        maximum=model.replicas.max,
        breaching_for_seconds=health.breaching_for_seconds,
        idle_for_seconds=health.idle_for_seconds,
    )
    return decision.replicas


def _scale_reason(model: Model, state: ActualState) -> str:
    health = state.health.get(model.name)
    if health is None:
        return ""
    deployed = state.deployment(model.name, state.live_colour(model.name))
    decision = decide(
        health.metrics,
        slo={
            "ttft_p95_ms": model.slo.ttft_p95_ms,
            "queue_depth_target": model.slo.queue_depth_target,
        },
        replicas=(deployed.replicas_desired if deployed else model.replicas.min),
        minimum=model.replicas.min,
        maximum=model.replicas.max,
        breaching_for_seconds=health.breaching_for_seconds,
        idle_for_seconds=health.idle_for_seconds,
    )
    return decision.reason


def _rollout_finished(state: ActualState, model: str, candidate: str) -> bool:
    """Whether the candidate has been taken out of the traffic split."""
    return state.weights.get(model, {}).get(candidate) == 0


def _target_hash(
    spec: PlatformSpec,
    model: Model,
    pool: NodePool,
    registry: ModelRegistry,
    replicas: int,
    colour: str,
    namespace: str,
) -> str:
    """The replica-independent identity of a deployment's configuration."""
    target = DeploymentTarget(
        model=model,
        pool=pool,
        gpu_class=spec.gpu_class(pool.gpu_class),
        serving_profile=spec.serving_profile_for(model),
        image=image_for(model),
        replicas=replicas,
        weights_uri=registry.uri_for(model.repo_id, model.revision or ""),
        colour=colour,
    )
    return config_hash(target, namespace=namespace)


def _deployment_action(
    kind: ActionKind,
    model: Model,
    pool: NodePool,
    replicas: int,
    colour: str,
    *,
    reason: str,
    depends_on: tuple[str, ...],
    registry: ModelRegistry,
) -> Action:
    """One create or update of a model deployment."""
    return Action(
        kind=kind,
        resource_type="model-deployment",
        name=f"{model.name}-{colour}",
        count=1,
        detail=(
            f"{replicas} replica(s) of {image_for(model)} on {pool.name}, "
            f"weights {registry.uri_for(model.repo_id, model.revision or '')}, "
            # Two sources feed one reconcile since ADR-0008, so a plan
            # that does not say which leaves an operator unable to
            # explain why a model appeared.
            f"placement: {model.placement}"
        ),
        reason=reason,
        depends_on=depends_on,
    )
