"""Filling DeploymentHealth from Prometheus (SPEC.md sections 4 and 11).

Autoscaling has been deciding correctly on data nobody supplied: the
decision logic is driven by the reconciler and tested, but nothing ever
produced a `Metrics` or the durations beside it (PROGRESS.md, milestone
7 gap 2). This is that producer.

The durations are the awkward part. `decide` needs "how long has this
been breaching", and the reconciler keeps no state between runs, so the
question is asked of Prometheus directly rather than remembered here.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from aisvc.observability import (
    SAMPLE_WINDOW,
    HealthObserver,
    PrometheusClient,
    PrometheusError,
)


def responder(values: dict[str, Any]) -> httpx.MockTransport:
    """A Prometheus that answers each query with a chosen scalar."""
    seen: list[str] = []

    def classify(query: str) -> str:
        """Which of the five queries this is.

        Order matters: the duration queries embed the queue and TTFT
        queries, so they have to be recognised first or they would be
        answered with a queue depth.
        """
        if "max_over_time" in query:
            return "breaching" if "> bool" in query else "idle"
        if "num_requests_waiting" in query:
            return "num_requests_waiting"
        if "time_to_first_token" in query:
            return "time_to_first_token"
        if "DCGM_FI_DEV_GPU_UTIL" in query:
            return "DCGM_FI_DEV_GPU_UTIL"
        return ""

    def handle(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        seen.append(query)
        kind = classify(query)
        for fragment, value in values.items():
            matched = fragment == kind if kind else (fragment == "" or fragment in query)
            if matched or (fragment == "" and kind):
                if value is None:
                    return httpx.Response(
                        200,
                        json={"status": "success", "data": {"resultType": "vector", "result": []}},
                    )
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {
                            "resultType": "vector",
                            "result": [{"metric": {}, "value": [0, str(value)]}],
                        },
                    },
                )
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )

    transport = httpx.MockTransport(handle)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def client_for(values: dict[str, Any]) -> PrometheusClient:
    return PrometheusClient("http://prometheus:9090", transport=responder(values))


class TestQuerying:
    async def test_a_scalar_comes_back_as_a_float(self) -> None:
        async with client_for({"up": 3}) as client:
            assert await client.scalar("up") == 3.0

    async def test_an_empty_result_is_none_not_zero(self) -> None:
        """Zero is a real value; "no data" is not the same claim."""
        async with client_for({"up": None}) as client:
            assert await client.scalar("up") is None

    async def test_a_prometheus_error_is_raised_not_swallowed(self) -> None:
        def handle(_: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"status": "error", "error": "parse error at char 1"})

        async with PrometheusClient(
            "http://prometheus:9090", transport=httpx.MockTransport(handle)
        ) as client:
            with pytest.raises(PrometheusError, match="parse error"):
                await client.scalar("not a query")

    async def test_an_unreachable_prometheus_is_an_error(self) -> None:
        def handle(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        async with PrometheusClient(
            "http://prometheus:9090", transport=httpx.MockTransport(handle)
        ) as client:
            with pytest.raises(PrometheusError, match="no route"):
                await client.scalar("up")


class TestObservingOneModel:
    def values(self, **overrides: Any) -> dict[str, Any]:
        values = {
            "num_requests_waiting": 12,
            "time_to_first_token": 0.45,
            "DCGM_FI_DEV_GPU_UTIL": 85,
            "breaching": 300,
            "idle": 0,
        }
        values.update(overrides)
        return values

    async def observe(self, **overrides: Any) -> dict[str, Any]:
        async with client_for(self.values(**overrides)) as client:
            observer = HealthObserver(client)
            return await observer.observe(["llama-3.3-70b-instruct"])

    async def test_a_model_gets_a_health_record(self) -> None:
        health = await self.observe()

        assert "llama-3.3-70b-instruct" in health

    async def test_the_queue_depth_is_read(self) -> None:
        health = await self.observe()

        assert health["llama-3.3-70b-instruct"].metrics.queue_depth == 12

    async def test_ttft_is_converted_from_seconds_to_milliseconds(self) -> None:
        """vLLM reports seconds; `Metrics` is in milliseconds."""
        health = await self.observe(time_to_first_token=0.45)

        assert health["llama-3.3-70b-instruct"].metrics.ttft_p95_ms == pytest.approx(450)

    async def test_gpu_utilisation_is_converted_to_a_fraction(self) -> None:
        """DCGM reports a percentage; `Metrics` is 0.0 to 1.0."""
        health = await self.observe(DCGM_FI_DEV_GPU_UTIL=85)

        assert health["llama-3.3-70b-instruct"].metrics.gpu_utilization == pytest.approx(0.85)

    async def test_the_breach_duration_is_read(self) -> None:
        health = await self.observe(breaching=300)

        assert health["llama-3.3-70b-instruct"].breaching_for_seconds == 300

    async def test_durations_are_whole_seconds(self) -> None:
        health = await self.observe(breaching=42.7)

        assert isinstance(health["llama-3.3-70b-instruct"].breaching_for_seconds, int)

    async def test_a_negative_duration_is_clamped_to_zero(self) -> None:
        """`time() - timestamp(...)` can come back slightly negative."""
        health = await self.observe(breaching=-0.5)

        assert health["llama-3.3-70b-instruct"].breaching_for_seconds == 0

    async def test_a_duration_is_capped_at_the_sample_window(self) -> None:
        """The subquery cannot see further back than its own range, so a
        larger number than the window is an artefact, not a measurement."""
        health = await self.observe(breaching=SAMPLE_WINDOW * 10)

        assert health["llama-3.3-70b-instruct"].breaching_for_seconds == SAMPLE_WINDOW


class TestMissingData:
    async def test_a_model_with_no_metrics_is_left_out(self) -> None:
        """Better absent than a record of zeros.

        A model that reports nothing has not just been deployed and is
        idle — it may not be scraped at all — and a zeroed record would
        read as "idle" and scale it down.
        """
        async with client_for({"anything": None}) as client:
            health = await HealthObserver(client).observe(["llama-3.3-70b-instruct"])

        assert health == {}

    async def test_a_model_missing_only_gpu_utilisation_still_counts(self) -> None:
        """Queue depth and TTFT are what scaling up turns on; DCGM may
        not be installed, and that should not disable scaling entirely."""
        async with client_for(
            {
                "num_requests_waiting": 12,
                "time_to_first_token": 0.45,
                "DCGM_FI_DEV_GPU_UTIL": None,
                "breaching": 300,
                "idle": 0,
            }
        ) as client:
            health = await HealthObserver(client).observe(["llama-3.3-70b-instruct"])

        assert health["llama-3.3-70b-instruct"].metrics.gpu_utilization == 0.0

    async def test_an_unreachable_prometheus_observes_nothing(self) -> None:
        """A monitoring outage must not look like an idle platform."""

        def handle(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        async with PrometheusClient(
            "http://prometheus:9090", transport=httpx.MockTransport(handle)
        ) as client:
            health = await HealthObserver(client).observe(["llama-3.3-70b-instruct"])

        assert health == {}


class TestQueriesAreScopedToTheModel:
    async def test_every_query_filters_by_model(self) -> None:
        """A query without the label would average every model together."""
        transport = responder({"": 1})
        async with PrometheusClient("http://p:9090", transport=transport) as client:
            await HealthObserver(client).observe(["llama-3.3-70b-instruct"])

        assert transport.seen  # type: ignore[attr-defined]
        assert all(
            "llama-3.3-70b-instruct" in query
            for query in transport.seen  # type: ignore[attr-defined]
        )

    async def test_two_models_are_observed_separately(self) -> None:
        transport = responder({"": 1})
        async with PrometheusClient("http://p:9090", transport=transport) as client:
            health = await HealthObserver(client).observe(["one", "two"])

        assert set(health) == {"one", "two"}
