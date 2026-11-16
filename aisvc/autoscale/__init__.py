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
"""Replica scaling (SPEC.md section 4).

Scales on the three signals the spec names — `vllm:num_requests_waiting`,
TTFT p95 and DCGM utilisation — "up in 1 min, down after 15 min".

The asymmetry is the point. Adding a replica means loading weights,
which SPEC.md section 11 budgets at three minutes even with the NVMe
cache, so scaling down eagerly and back up again costs far more than
carrying a replica for a quarter of an hour. One replica at a time, for
the same reason: adding three at once just queues three cold starts
behind the same bottleneck.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "IDLE_UTILIZATION",
    "SCALE_DOWN_AFTER_SECONDS",
    "SCALE_UP_AFTER_SECONDS",
    "Metrics",
    "ScaleDecision",
    "decide",
]

SCALE_UP_AFTER_SECONDS = 60
SCALE_DOWN_AFTER_SECONDS = 15 * 60

#: Below this GPU utilisation, with an empty queue, a replica is idle.
IDLE_UTILIZATION = 0.15

DEFAULT_QUEUE_TARGET = 8
DEFAULT_TTFT_P95_MS = 800.0


@dataclass(frozen=True, slots=True)
class Metrics:
    """What Prometheus reports for one deployment."""

    #: vllm:num_requests_waiting, averaged across replicas.
    queue_depth: float
    ttft_p95_ms: float
    #: DCGM utilisation, 0.0 to 1.0.
    gpu_utilization: float


@dataclass(frozen=True, slots=True)
class ScaleDecision:
    """What to do with a deployment's replica count, and why."""

    replicas: int
    previous: int
    reason: str

    @property
    def changed(self) -> bool:
        return self.replicas != self.previous


def decide(
    metrics: Metrics,
    *,
    slo: Mapping[str, object],
    replicas: int,
    minimum: int,
    maximum: int,
    breaching_for_seconds: int = 0,
    idle_for_seconds: int = 0,
) -> ScaleDecision:
    """Decide the replica count for one deployment.

    `breaching_for_seconds` and `idle_for_seconds` are how long the
    condition has held, which is what keeps a passing spike from adding
    a GPU node and a quiet minute from removing one.
    """
    if maximum < minimum:
        raise ValueError(f"maximum {maximum} is below minimum {minimum}")

    current = max(minimum, min(replicas, maximum))
    queue_target = _as_number(slo.get("queue_depth_target"), DEFAULT_QUEUE_TARGET)
    ttft_target = _as_number(slo.get("ttft_p95_ms"), DEFAULT_TTFT_P95_MS)

    breaches = []
    if metrics.queue_depth > queue_target:
        breaches.append(f"queue depth {metrics.queue_depth:.0f} over target {queue_target:.0f}")
    if metrics.ttft_p95_ms > ttft_target:
        breaches.append(f"TTFT p95 {metrics.ttft_p95_ms:.0f}ms over SLO {ttft_target:.0f}ms")

    # A breach outranks idleness: being over SLO is a tenant-visible
    # problem, while carrying a spare replica is only a cost.
    if breaches and breaching_for_seconds >= SCALE_UP_AFTER_SECONDS:
        if current >= maximum:
            return ScaleDecision(
                replicas=current,
                previous=replicas,
                reason=(f"{'; '.join(breaches)}, but already at the maximum of {maximum}"),
            )
        return ScaleDecision(
            replicas=current + 1,
            previous=replicas,
            reason=f"{'; '.join(breaches)}; adding one replica",
        )

    if breaches:
        return ScaleDecision(
            replicas=current,
            previous=replicas,
            reason=(
                f"{'; '.join(breaches)} for {breaching_for_seconds}s; waiting for "
                f"{SCALE_UP_AFTER_SECONDS}s before scaling up"
            ),
        )

    idle = metrics.queue_depth == 0 and metrics.gpu_utilization < IDLE_UTILIZATION
    if idle and idle_for_seconds >= SCALE_DOWN_AFTER_SECONDS:
        if current <= minimum:
            return ScaleDecision(
                replicas=current,
                previous=replicas,
                reason=f"idle, but already at the minimum of {minimum}",
            )
        return ScaleDecision(
            replicas=current - 1,
            previous=replicas,
            reason=(
                f"idle for {idle_for_seconds // 60}m at "
                f"{metrics.gpu_utilization:.0%} utilisation; removing one replica"
            ),
        )

    if idle:
        return ScaleDecision(
            replicas=current,
            previous=replicas,
            reason=(
                f"idle for {idle_for_seconds}s; waiting for "
                f"{SCALE_DOWN_AFTER_SECONDS}s before scaling down"
            ),
        )

    return ScaleDecision(
        replicas=current,
        previous=replicas,
        reason="within SLO and not idle",
    )


def _as_number(value: object, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return float(value)
