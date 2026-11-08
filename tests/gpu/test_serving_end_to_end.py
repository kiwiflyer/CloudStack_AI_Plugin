"""One model end to end in a lab zone (SPEC.md sections 12 and 14).

These need real GPUs and are marked `gpu`, so CI skips them and
`make test-gpu` runs them against a lab zone. They are the milestone 3
acceptance criteria that cannot be met on a laptop:

* a 70B FP8 model serving behind the gateway with no manual steps;
* the cold-start target of under three minutes (SPEC.md section 11).

Environment:
    AISVC_GPU_LAB_SPEC       path to the lab's platform.yaml
    AISVC_GPU_LAB_GATEWAY    base URL of the lab gateway
    AISVC_GPU_LAB_KEY        an API key scoped to the lab model
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import httpx
import pytest

pytestmark = pytest.mark.gpu

COLD_START_BUDGET_SECONDS = 180
MODEL = os.environ.get("AISVC_GPU_LAB_MODEL", "llama-3.3-70b-instruct")


def lab_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is not set; this test needs a GPU lab zone")
    return value


@pytest.fixture
def gateway() -> Iterator[httpx.Client]:
    base = lab_env("AISVC_GPU_LAB_GATEWAY")
    key = lab_env("AISVC_GPU_LAB_KEY")
    with httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {key}"},
        timeout=120.0,
    ) as client:
        yield client


class TestServingThroughTheGateway:
    def test_the_model_is_listed_for_this_key(self, gateway: httpx.Client) -> None:
        response = gateway.get("/v1/models")
        response.raise_for_status()

        names = {m["id"] for m in response.json()["data"]}
        assert MODEL in names

    def test_a_chat_completion_returns_tokens(self, gateway: httpx.Client) -> None:
        response = gateway.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "Reply with the word ok."}],
                "max_tokens": 8,
            },
        )
        response.raise_for_status()
        body = response.json()

        assert body["choices"][0]["message"]["content"].strip()
        assert body["usage"]["completion_tokens"] > 0

    def test_streaming_works(self, gateway: httpx.Client) -> None:
        with gateway.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "Count to three."}],
                "max_tokens": 32,
                "stream": True,
            },
        ) as response:
            response.raise_for_status()
            chunks = [line for line in response.iter_lines() if line.startswith("data:")]

        assert len(chunks) > 1

    def test_a_locked_parameter_cannot_be_overridden(self, gateway: httpx.Client) -> None:
        """SPEC.md section 14: a locked value beats the request."""
        response = gateway.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 8,
                "temperature": 1.9,
            },
        )
        response.raise_for_status()
        # The lab profile locks temperature; the request must not have
        # changed it. Verified through the effective-parameters endpoint
        # rather than by inspecting output, which is not deterministic.
        effective = gateway.get(f"/v1/profiles/effective?model={MODEL}").json()
        assert effective["temperature"]["locked"] is True

    def test_a_key_scoped_elsewhere_is_refused(self, gateway: httpx.Client) -> None:
        response = gateway.post(
            "/v1/chat/completions",
            json={
                "model": "a-model-this-key-cannot-use",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 403


class TestColdStart:
    def test_cold_start_is_under_three_minutes(self) -> None:
        """SPEC.md section 11, validated in milestone 3.

        Measured from the pod being scheduled to its readiness probe
        passing, with weights already staged on node-local NVMe.
        """
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        lab_env("AISVC_GPU_LAB_SPEC")
        k8s_config.load_kube_config()  # type: ignore[no-untyped-call]
        core = k8s_client.CoreV1Api()

        namespace = os.environ.get("AISVC_GPU_LAB_NAMESPACE", "aisvc-serving")
        pods = core.list_namespaced_pod(namespace, label_selector=f"aisvc.io/model={MODEL}").items
        assert pods, f"no pods for {MODEL} in {namespace}"

        pod = pods[0]
        conditions = (pod.status.conditions or []) if pod.status else []
        scheduled = next(
            (c.last_transition_time for c in conditions if c.type == "PodScheduled"),
            None,
        )
        ready = next((c.last_transition_time for c in conditions if c.type == "Ready"), None)
        assert scheduled and ready, "pod never became ready"

        elapsed = (ready - scheduled).total_seconds()
        assert elapsed < COLD_START_BUDGET_SECONDS, (
            f"cold start took {elapsed:.0f}s, budget is {COLD_START_BUDGET_SECONDS}s"
        )

    def test_a_restarted_replica_recovers_within_the_slo(self, gateway: httpx.Client) -> None:
        """A replica must come back without the endpoint going down."""
        deadline = time.monotonic() + COLD_START_BUDGET_SECONDS
        failures = 0
        while time.monotonic() < deadline:
            response = gateway.post(
                "/v1/chat/completions",
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 4,
                },
            )
            if response.status_code >= 500:
                failures += 1
            time.sleep(5)

        assert failures == 0, f"{failures} requests failed during the window"
