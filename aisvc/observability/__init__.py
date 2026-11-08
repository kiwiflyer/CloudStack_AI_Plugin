"""Reading deployment health from Prometheus (SPEC.md sections 4 and 11).

The autoscaler has always decided correctly on data nobody supplied:
`decide()` is driven by the reconciler and well tested, but `Metrics`
and the durations beside it were never filled in. This fills them.

**The durations are the interesting part.** `decide()` asks how long a
deployment has been breaching its SLO, and deliberately does nothing
until that has held for a minute — which is what stops a passing spike
from provisioning a GPU node. But the reconciler keeps nothing between
runs, so there is no "breaching since" to remember.

Rather than add that state, the question is put to Prometheus, which
already has the history:

    time() - max_over_time(timestamp(<condition is false>)[window:step])

That reads as "how long since this was last false". It is zero while the
condition is false, grows while it holds, and saturates at the window.
No state here, and nothing to get out of step with reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx

from aisvc.autoscale import (
    DEFAULT_QUEUE_TARGET,
    DEFAULT_TTFT_P95_MS,
    IDLE_UTILIZATION,
    Metrics,
)
from aisvc.reconciler.state import DeploymentHealth

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "SAMPLE_WINDOW",
    "HealthObserver",
    "PrometheusClient",
    "PrometheusError",
]

#: How far back the duration subqueries look, in seconds. It has to
#: exceed `SCALE_DOWN_AFTER_SECONDS` (15 minutes) or a deployment could
#: never be seen as idle for long enough to give a replica back.
SAMPLE_WINDOW = 30 * 60

#: Subquery resolution. Finer costs Prometheus more for no benefit:
#: the decisions turn on minutes.
SAMPLE_STEP = "30s"

#: vLLM and DCGM metric names. vLLM labels series with `model_name`.
QUEUE_METRIC = "vllm:num_requests_waiting"
TTFT_METRIC = "vllm:time_to_first_token_seconds_bucket"
GPU_METRIC = "DCGM_FI_DEV_GPU_UTIL"


class PrometheusError(Exception):
    """Prometheus could not be reached, or refused the query."""


class PrometheusClient:
    """A read-only Prometheus client, just enough for instant queries."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url, transport=transport, timeout=timeout
        )

    async def __aenter__(self) -> PrometheusClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def scalar(self, query: str) -> float | None:
        """Run an instant query and return its single value.

        `None` means Prometheus answered with no series, which is not
        the same claim as zero: a model that is not being scraped must
        not read as a model with an empty queue.
        """
        try:
            response = await self._client.get("/api/v1/query", params={"query": query})
        except httpx.HTTPError as exc:
            raise PrometheusError(f"querying Prometheus failed: {exc}") from exc

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise PrometheusError(
                f"Prometheus returned a non-JSON response (HTTP {response.status_code})"
            ) from exc

        if body.get("status") != "success":
            raise PrometheusError(
                f"Prometheus refused the query: {body.get('error', 'no reason given')}"
            )

        results = (body.get("data") or {}).get("result") or []
        if not results:
            return None

        value = results[0].get("value")
        if not value or len(value) < 2:
            return None
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class HealthObserver:
    """Fills `DeploymentHealth` for the reconciler."""

    client: PrometheusClient
    window: int = SAMPLE_WINDOW
    step: str = SAMPLE_STEP

    async def observe(self, models: Sequence[str]) -> dict[str, DeploymentHealth]:
        """Read health for each model, skipping any that report nothing.

        A model with no metrics is left out rather than zeroed. Zeros
        read as "idle" to the autoscaler, so a monitoring outage would
        quietly scale the platform down to its floor.
        """
        health: dict[str, DeploymentHealth] = {}
        for model in models:
            observed = await self._observe_one(model)
            if observed is not None:
                health[model] = observed
        return health

    async def _observe_one(self, model: str) -> DeploymentHealth | None:
        try:
            queue = await self.client.scalar(self._queue_query(model))
            ttft = await self.client.scalar(self._ttft_query(model))
            gpu = await self.client.scalar(self._gpu_query(model))
            breaching = await self.client.scalar(self._breaching_query(model))
            idle = await self.client.scalar(self._idle_query(model))
        except PrometheusError:
            # A monitoring outage must not look like an idle platform.
            return None

        if queue is None and ttft is None:
            return None

        return DeploymentHealth(
            metrics=Metrics(
                queue_depth=queue or 0.0,
                # vLLM reports seconds; Metrics is in milliseconds.
                ttft_p95_ms=(ttft or 0.0) * 1000,
                # DCGM reports a percentage; Metrics is a fraction.
                # Absent means DCGM is not installed, which should not
                # disable scaling up — that turns on queue and TTFT.
                gpu_utilization=(gpu or 0.0) / 100,
            ),
            breaching_for_seconds=self._duration(breaching),
            idle_for_seconds=self._duration(idle),
        )

    def _duration(self, seconds: float | None) -> int:
        """Clamp a `time() - timestamp(...)` result into the window.

        It can come back slightly negative from clock skew between the
        scrape and the query, and larger than the window when the
        condition has held throughout — neither is a measurement.
        """
        if seconds is None:
            return 0
        return max(0, min(int(seconds), self.window))

    def _selector(self, metric: str, model: str) -> str:
        return f'{metric}{{model_name="{model}"}}'

    def _queue_query(self, model: str) -> str:
        return f"avg({self._selector(QUEUE_METRIC, model)})"

    def _ttft_query(self, model: str) -> str:
        return (
            "histogram_quantile(0.95, sum by (le) (rate("
            f'{TTFT_METRIC}{{model_name="{model}"}}[5m])))'
        )

    def _gpu_query(self, model: str) -> str:
        # DCGM labels by GPU, not by model; the join is through the pod
        # the exporter annotates, which carries our model label.
        return f'avg({GPU_METRIC}{{model_name="{model}"}})'

    def _since_false(self, condition: str) -> str:
        """How long `condition` has held without a break, in seconds."""
        return (
            f"time() - max_over_time(timestamp(({condition}) == bool 0)"
            f"[{self.window}s:{self.step}])"
        )

    def _breaching_query(self, model: str) -> str:
        queue = self._queue_query(model)
        ttft = self._ttft_query(model)
        breaching = (
            f"(({queue}) > bool {DEFAULT_QUEUE_TARGET}) or "
            f"(({ttft}) > bool {DEFAULT_TTFT_P95_MS / 1000})"
        )
        return self._since_false(breaching)

    def _idle_query(self, model: str) -> str:
        idle = (
            f"(({self._queue_query(model)}) == bool 0) and "
            f"(({self._gpu_query(model)}) < bool {IDLE_UTILIZATION * 100})"
        )
        return self._since_false(idle)
