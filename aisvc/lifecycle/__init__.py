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
"""Rollouts and maintenance (SPEC.md sections 4, 7 and 11).

Blue/green with canary weights, and draining a node before it is taken
away.

Two decisions from earlier ADRs land here.
[ADR-0001](../../docs/adr/0001-serving-layer.md) kept canary weighting
out of the serving layer, so this module decides the split and the
gateway carries it — which is why a rollout is a small state machine
producing weights rather than anything that touches Kubernetes.
[ADR-0003](../../docs/adr/0003-cloudstack-release.md) found that
`gpu.detach.on.stop` defaults to false, so a drain **destroys** a node
rather than stopping it: a stopped node keeps its GPUs allocated and
strands capacity nobody is using.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

__all__ = [
    "COLOURS",
    "DrainPlan",
    "Rollout",
    "RolloutError",
    "RolloutPhase",
    "plan_drain",
    "start_rollout",
]

COLOURS = ("blue", "green")


class RolloutError(Exception):
    """A rollout was asked for something it cannot do."""


class RolloutPhase(Enum):
    """Where a blue/green rollout has got to."""

    CANARY = "canary"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


def _other(colour: str) -> str:
    if colour not in COLOURS:
        raise RolloutError(f"{colour!r} is not a colour; expected blue or green")
    return "green" if colour == "blue" else "blue"


@dataclass(frozen=True, slots=True)
class Rollout:
    """One blue/green rollout (SPEC.md sections 3 and 7).

    `active` is the colour that was live when the rollout started and
    `candidate` is the new one; neither changes as the rollout settles.
    Swapping them on promote would make the pair ambiguous — the same
    two fields would mean different things depending on how the rollout
    got there — so `live_colour` answers "what is serving now?" instead.

    Immutable: `promote()` and `rollback()` return a new rollout, so a
    caller cannot half-apply a transition and leave the weights and the
    phase disagreeing.
    """

    model: str
    active: str
    candidate: str
    canary_percent: int
    phase: RolloutPhase = RolloutPhase.CANARY

    def weights(self) -> dict[str, int]:
        """The traffic split the gateway should apply.

        Always sums to 100, so a misconfigured split cannot silently
        drop or duplicate a share of traffic.
        """
        if self.phase is RolloutPhase.PROMOTED:
            return _split(self.candidate, 100, self.active)
        if self.phase is RolloutPhase.ROLLED_BACK:
            return _split(self.active, 100, self.candidate)
        return _split(self.candidate, self.canary_percent, self.active)

    @property
    def live_colour(self) -> str:
        """Which colour is serving now."""
        return self.candidate if self.phase is RolloutPhase.PROMOTED else self.active

    @property
    def is_finished(self) -> bool:
        return self.phase is not RolloutPhase.CANARY

    @property
    def colour_to_remove(self) -> str | None:
        """The colour whose deployment can be torn down, once settled."""
        if self.phase is RolloutPhase.PROMOTED:
            return self.active  # the colour that was replaced
        if self.phase is RolloutPhase.ROLLED_BACK:
            return self.candidate  # the colour that failed
        return None

    def promote(self) -> Rollout:
        """Send all traffic to the candidate and make it the active colour."""
        if self.is_finished:
            raise RolloutError(
                f"{self.model}: this rollout is already {self.phase.value}; "
                "start a new one to change the active colour again"
            )
        return replace(self, phase=RolloutPhase.PROMOTED)

    def rollback(self) -> Rollout:
        """Send all traffic back to the active colour."""
        if self.phase is RolloutPhase.PROMOTED:
            raise RolloutError(
                f"{self.model}: this rollout was already promoted; rolling back "
                "now is a new rollout in the other direction"
            )
        if self.phase is RolloutPhase.ROLLED_BACK:
            raise RolloutError(f"{self.model}: this rollout was already rolled back")
        return replace(self, phase=RolloutPhase.ROLLED_BACK)


def _split(to_colour: str, percent: int, other_colour: str) -> dict[str, int]:
    weights = {colour: 0 for colour in COLOURS}
    weights[to_colour] = percent
    weights[other_colour] = 100 - percent
    return weights


def start_rollout(model: str, *, active: str, canary_percent: int = 10) -> Rollout:
    """Begin a blue/green rollout against whichever colour is not live."""
    if not 0 <= canary_percent <= 100:
        raise RolloutError(f"canary_percent must be between 0 and 100, got {canary_percent}")
    return Rollout(
        model=model,
        active=active,
        candidate=_other(active),
        canary_percent=canary_percent,
    )


@dataclass(frozen=True, slots=True)
class DrainPlan:
    """What draining one node would involve (SPEC.md section 7)."""

    node: str
    replicas_to_move: int
    free_elsewhere: int
    can_proceed: bool
    reason: str
    forced: bool = False
    #: Always "destroy". A stopped node keeps its GPUs allocated
    #: (ADR-0003), so stopping strands capacity nobody is using.
    action: str = "destroy"


def plan_drain(
    node: str,
    *,
    replicas_on_node: int,
    free_elsewhere: int,
    force: bool = False,
) -> DrainPlan:
    """Decide whether a node can be drained now.

    The replicas it carries have to land somewhere first. Where they
    cannot, SPEC.md section 7 says the admin must confirm a forced
    drain rather than the platform quietly reducing capacity.
    """
    enough_room = free_elsewhere >= replicas_on_node

    if enough_room:
        reason = (
            f"{replicas_on_node} replica(s) move to {free_elsewhere} free slot(s), "
            f"then {node} is destroyed"
        )
    elif force:
        reason = (
            f"forced: {replicas_on_node} replica(s) on {node} but only "
            f"{free_elsewhere} free slot(s) elsewhere, so the model will run "
            "below its replica count until capacity returns"
        )
    else:
        reason = (
            f"not enough capacity: {replicas_on_node} replica(s) on {node} but "
            f"only {free_elsewhere} free slot(s) elsewhere; an admin must "
            "confirm a forced drain"
        )

    return DrainPlan(
        node=node,
        replicas_to_move=replicas_on_node,
        free_elsewhere=free_elsewhere,
        can_proceed=enough_room or force,
        reason=reason,
        forced=force and not enough_room,
    )
