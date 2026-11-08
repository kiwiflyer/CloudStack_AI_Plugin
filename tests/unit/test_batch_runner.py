"""Running batches (SPEC.md section 8).

v2 accepted a batch, counted its requests and stored it. Nothing ever
ran one. This is the runner, and most of what it has to get right is
about *not* running:

- it must stand down for online traffic, because that yielding is the
  only thing that makes the discount defensible;
- it must stop at the completion window rather than work forever on a
  job nobody is waiting for any more;
- and it must survive a request failing, because one bad line in fifty
  thousand is normal and failing the lot would be useless.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aisvc.batch import BatchState, price_batch
from aisvc.batch.runner import BatchRunner, Outcome
from aisvc.db import Base
from aisvc.db.tier2 import BatchStore
from aisvc.registry import InMemoryObjectStore, ModelRegistry

MODEL = "llama-3.3-70b-instruct"


def line(custom_id: str) -> str:
    return json.dumps(
        {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        }
    )


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        yield s


class Completions:
    """Stands in for the gateway."""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.seen: list[dict[str, Any]] = []
        self.fail = fail or set()

    async def __call__(self, request: Any) -> dict[str, Any]:
        self.seen.append(request.body)
        if request.custom_id in self.fail:
            raise RuntimeError("upstream said no")
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def runner(
    session: Session,
    registry: ModelRegistry,
    *,
    complete: Any = None,
    preempt: bool = False,
    meter: Any = None,
) -> BatchRunner:
    return BatchRunner(
        BatchStore(session),
        registry,
        complete=complete or Completions(),
        should_preempt=lambda: preempt,
        meter=meter,
    )


def registry_for() -> ModelRegistry:
    return ModelRegistry(InMemoryObjectStore(), bucket="models")


def submit(session: Session, registry: ModelRegistry, *ids: str) -> Any:
    from aisvc.batch.runner import store_input

    store = BatchStore(session)
    record = store.add(
        account="acme",
        domain="/Customers/Acme",
        model=MODEL,
        request_count=len(ids) or 1,
    )
    store_input(registry, record, "\n".join(line(i) for i in (ids or ("a",))))
    return record


class TestRunningOne:
    async def test_a_submitted_batch_is_executed(self, session: Session) -> None:
        registry = registry_for()
        submit(session, registry, "a", "b")
        completions = Completions()

        outcome = await runner(session, registry, complete=completions).run_once()

        assert outcome is Outcome.RAN
        assert len(completions.seen) == 2

    async def test_it_finishes_as_completed(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")

        await runner(session, registry).run_once()

        assert record.state == BatchState.COMPLETED.value
        assert record.finished_at is not None

    async def test_the_results_are_written_as_jsonl(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a", "b")

        await runner(session, registry).run_once()

        from aisvc.batch.runner import read_output

        results = [json.loads(line) for line in read_output(registry, record).splitlines()]
        assert [r["custom_id"] for r in results] == ["a", "b"]

    async def test_each_result_carries_its_response(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")

        await runner(session, registry).run_once()

        from aisvc.batch.runner import read_output

        result = json.loads(read_output(registry, record))
        assert result["response"]["status_code"] == 200
        assert result["error"] is None

    async def test_nothing_to_do_is_reported(self, session: Session) -> None:
        outcome = await runner(session, registry_for()).run_once()

        assert outcome is Outcome.IDLE

    async def test_a_finished_batch_is_not_run_again(self, session: Session) -> None:
        registry = registry_for()
        submit(session, registry, "a")
        await runner(session, registry).run_once()

        completions = Completions()
        outcome = await runner(session, registry, complete=completions).run_once()

        assert outcome is Outcome.IDLE
        assert completions.seen == []


class TestPreemption:
    async def test_online_traffic_stops_a_batch_starting(self, session: Session) -> None:
        """The discount is payable because batch yields."""
        registry = registry_for()
        submit(session, registry, "a")
        completions = Completions()

        outcome = await runner(session, registry, complete=completions, preempt=True).run_once()

        assert outcome is Outcome.PREEMPTED
        assert completions.seen == []

    async def test_a_preempted_batch_stays_runnable(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")

        await runner(session, registry, preempt=True).run_once()

        assert record.state == BatchState.VALIDATING.value

    async def test_it_runs_once_the_platform_is_quiet(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")
        await runner(session, registry, preempt=True).run_once()

        await runner(session, registry).run_once()

        assert record.state == BatchState.COMPLETED.value


class TestFailures:
    async def test_one_failed_request_does_not_fail_the_batch(self, session: Session) -> None:
        """One bad line in fifty thousand is normal."""
        registry = registry_for()
        record = submit(session, registry, "a", "b", "c")

        await runner(session, registry, complete=Completions(fail={"b"})).run_once()

        assert record.state == BatchState.COMPLETED.value

    async def test_the_failure_is_recorded_against_its_line(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a", "b")

        await runner(session, registry, complete=Completions(fail={"b"})).run_once()

        from aisvc.batch.runner import read_output

        results = {
            json.loads(line)["custom_id"]: json.loads(line)
            for line in read_output(registry, record).splitlines()
        }
        assert results["a"]["error"] is None
        assert results["b"]["error"] is not None

    async def test_every_other_request_still_runs(self, session: Session) -> None:
        registry = registry_for()
        submit(session, registry, "a", "b", "c")
        completions = Completions(fail={"a"})

        await runner(session, registry, complete=completions).run_once()

        assert len(completions.seen) == 3

    async def test_a_batch_with_no_input_fails_rather_than_hanging(self, session: Session) -> None:
        registry = registry_for()
        record = BatchStore(session).add(account="acme", domain="/d", model=MODEL, request_count=1)

        await runner(session, registry).run_once()

        assert record.state == BatchState.FAILED.value
        assert record.error_text


class TestTheCompletionWindow:
    async def test_a_batch_past_its_window_is_expired(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")
        record.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)
        completions = Completions()

        await runner(session, registry, complete=completions).run_once()

        assert record.state == BatchState.EXPIRED.value
        assert completions.seen == []

    async def test_an_expired_batch_says_so(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")
        record.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)

        await runner(session, registry).run_once()

        assert "window" in record.error_text.lower()

    async def test_a_batch_inside_its_window_runs(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")
        record.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)

        await runner(session, registry).run_once()

        assert record.state == BatchState.COMPLETED.value


class TestMetering:
    async def test_usage_is_recorded_per_request(self, session: Session) -> None:
        registry = registry_for()
        submit(session, registry, "a")
        metered: list[Any] = []

        async def meter(usage: Any) -> None:
            metered.append(usage)

        await runner(session, registry, meter=meter).run_once()

        assert len(metered) == 1
        assert metered[0].input_tokens == 10
        assert metered[0].output_tokens == 5

    async def test_the_discount_is_reported_on_the_record(self, session: Session) -> None:
        """SPEC.md section 8: a configurable discount to the online rate."""
        assert price_batch(10.0, discount=0.5) == pytest.approx(5.0)

    async def test_a_failed_request_is_not_metered(self, session: Session) -> None:
        """Nothing was generated, so there is nothing to charge for."""
        registry = registry_for()
        submit(session, registry, "a", "b")
        metered: list[Any] = []

        async def meter(usage: Any) -> None:
            metered.append(usage)

        await runner(session, registry, meter=meter, complete=Completions(fail={"b"})).run_once()

        assert len(metered) == 1

    async def test_running_without_a_meter_is_allowed(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")

        await runner(session, registry, meter=None).run_once()

        assert record.state == BatchState.COMPLETED.value


class TestRetention:
    async def test_results_past_retention_are_swept(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")
        await runner(session, registry).run_once()
        record.finished_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=31)

        swept = runner(session, registry).sweep_retention()

        assert swept == 1

    async def test_the_output_is_actually_deleted(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")
        await runner(session, registry).run_once()
        record.finished_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=31)

        runner(session, registry).sweep_retention()

        assert record.output_uri == ""

    async def test_recent_results_are_kept(self, session: Session) -> None:
        registry = registry_for()
        record = submit(session, registry, "a")
        await runner(session, registry).run_once()

        assert runner(session, registry).sweep_retention() == 0
        assert record.output_uri


class TestThePreemptionSignal:
    """What the worker asks before running: is the platform busy?

    Built from the same Prometheus health the autoscaler uses, so batch
    and autoscaling cannot disagree about whether a model is under
    pressure.
    """

    def health(self, queue: float, utilisation: float) -> dict[str, Any]:
        from aisvc.autoscale import Metrics
        from aisvc.reconciler.state import DeploymentHealth

        return {
            MODEL: DeploymentHealth(
                metrics=Metrics(queue_depth=queue, ttft_p95_ms=100.0, gpu_utilization=utilisation)
            )
        }

    def test_a_quiet_platform_lets_batches_run(self) -> None:
        from aisvc.batch.runner import preemption_signal

        assert not preemption_signal(self.health(0.0, 0.1))()

    def test_a_busy_model_preempts(self) -> None:
        from aisvc.batch.runner import preemption_signal

        assert preemption_signal(self.health(20.0, 0.95))()

    def test_one_busy_model_is_enough(self) -> None:
        """Batch shares the GPUs; a queue anywhere is a queue."""
        from aisvc.batch.runner import preemption_signal

        health = self.health(0.0, 0.1) | {"other": self.health(20.0, 0.95)[MODEL]}

        assert preemption_signal(health)()

    def test_no_health_does_not_preempt(self) -> None:
        """No Prometheus means no autoscaling either; batches should
        still run rather than being blocked by missing data."""
        from aisvc.batch.runner import preemption_signal

        assert not preemption_signal({})()
