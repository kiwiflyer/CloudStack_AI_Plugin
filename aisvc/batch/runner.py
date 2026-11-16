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
"""Running batches (SPEC.md section 8).

v2 accepted a batch and stored it; nothing ever ran one. This does.

Most of what it has to get right is about *not* running:

- **Yielding.** Batch stands down whenever online traffic is waiting,
  and that is the only thing that makes the discount defensible. A
  batch that competed for GPUs would be cheap traffic degrading the SLO
  everyone else pays full rate for.
- **Stopping.** A job past its completion window is expired rather than
  worked on. The window is a promise, and once it is broken nobody is
  waiting for the answer any more.
- **Carrying on.** One failed request does not fail the batch. A bad
  line in fifty thousand is normal, and refusing the whole file would
  make the feature useless at the sizes it exists for.

Results are written as JSONL in OpenAI's shape — one object per line
carrying `custom_id`, `response` and `error` — because a tenant's
existing batch client reads exactly that.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from aisvc.batch import (
    DEFAULT_DISCOUNT,
    BatchState,
    expired,
    parse_requests,
    price_batch,
    should_preempt,
)
from aisvc.db.models import utcnow
from aisvc.metering import UsageRecord
from aisvc.spec.models import PriceTier

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from aisvc.db.models import BatchRecord
    from aisvc.db.tier2 import BatchStore
    from aisvc.registry import ModelRegistry

__all__ = [
    "BatchRunner",
    "Outcome",
    "batch_tier",
    "input_key",
    "output_key",
    "preemption_signal",
    "read_output",
    "store_input",
]

log = logging.getLogger("aisvc.batch")


class Outcome(StrEnum):
    """What one pass of the runner did."""

    IDLE = "idle"
    RAN = "ran"
    PREEMPTED = "preempted"


def input_key(record: BatchRecord) -> str:
    return f"batches/{record.id}/input.jsonl"


def output_key(record: BatchRecord) -> str:
    return f"batches/{record.id}/output.jsonl"


def store_input(registry: ModelRegistry, record: BatchRecord, jsonl: str) -> str:
    """Put a batch's input file in the registry.

    In the registry rather than a database column: these run to
    hundreds of megabytes, and the object store is already the place
    for large immutable artefacts.
    """
    key = input_key(record)
    registry.store.put(key, jsonl.encode("utf-8"))
    record.input_uri = key
    return key


def read_output(registry: ModelRegistry, record: BatchRecord) -> str:
    return registry.store.get(output_key(record)).decode("utf-8")


class BatchRunner:
    """Executes one batch per pass, if the platform has room for it."""

    def __init__(
        self,
        store: BatchStore,
        registry: ModelRegistry,
        *,
        complete: Callable[[Any], Any],
        should_preempt: Callable[[], bool],
        meter: Callable[[UsageRecord], Any] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.complete = complete
        self.should_preempt = should_preempt
        # Supplied already priced at the batch rate: see
        # `batch_tier`. The runner records usage, it does not price.
        self.meter = meter

    async def run_once(self) -> Outcome:
        """Expire what is stale, then run one batch if there is room."""
        self._expire_stale()

        record = self._next_runnable()
        if record is None:
            return Outcome.IDLE

        # Checked before starting rather than mid-flight: a batch that
        # began and stopped halfway leaves partial results nobody asked
        # for, and the next pass will pick it up anyway.
        if self.should_preempt():
            log.debug("standing down from batch %s: online traffic", record.id)
            return Outcome.PREEMPTED

        await self._run(record)
        return Outcome.RAN

    def _next_runnable(self) -> BatchRecord | None:
        for record in self.store.all():
            if record.state in {
                BatchState.VALIDATING.value,
                BatchState.IN_PROGRESS.value,
            }:
                return record
        return None

    def _expire_stale(self) -> None:
        """Mark anything past its completion window."""
        for record in self.store.all():
            if record.state not in {
                BatchState.VALIDATING.value,
                BatchState.IN_PROGRESS.value,
            }:
                continue
            if expired(_as_job(record)):
                record.state = BatchState.EXPIRED.value
                record.error_text = "this batch did not finish inside its completion window"
                record.finished_at = utcnow()
                log.info("batch %s expired", record.id)
        self.store.session.flush()

    async def _run(self, record: BatchRecord) -> None:
        record.state = BatchState.IN_PROGRESS.value
        self.store.session.flush()

        try:
            raw = self.registry.store.get(input_key(record)).decode("utf-8")
            requests = parse_requests(raw, model=record.model)
        except Exception as exc:
            # Without an input file there is nothing to run and no way
            # to recover; failing beats sitting in progress forever.
            record.state = BatchState.FAILED.value
            record.error_text = f"could not read this batch's input: {exc}"
            record.finished_at = utcnow()
            self.store.session.flush()
            log.warning("batch %s has no readable input: %s", record.id, exc)
            return

        lines: list[str] = []
        for request in requests:
            lines.append(json.dumps(await self._one(record, request)))

        self.registry.store.put(output_key(record), "\n".join(lines).encode("utf-8"))
        record.output_uri = output_key(record)
        record.state = BatchState.COMPLETED.value
        record.finished_at = utcnow()
        self.store.session.flush()
        log.info("batch %s completed with %d result(s)", record.id, len(lines))

    async def _one(self, record: BatchRecord, request: Any) -> dict[str, Any]:
        """Run one request, in OpenAI's result shape.

        A failure is recorded against its own line and the batch keeps
        going: one bad request in fifty thousand is normal, and failing
        the file would make batches useless at the size they exist for.
        """
        try:
            response = await self.complete(request)
        except Exception as exc:
            return {
                "id": f"{record.id}-{request.custom_id}",
                "custom_id": request.custom_id,
                "response": None,
                "error": {"message": str(exc)},
            }

        await self._meter(record, response)
        return {
            "id": f"{record.id}-{request.custom_id}",
            "custom_id": request.custom_id,
            "response": {"status_code": 200, "body": response},
            "error": None,
        }

    async def _meter(self, record: BatchRecord, response: Any) -> None:
        """Record what this request used.

        Only on success: a request that produced nothing has nothing to
        charge for. The discount lives in the tier the meter was built
        with (`batch_tier`), so there is one pricing path rather than a
        second one here that could disagree with it.
        """
        if self.meter is None:
            return

        usage = (response or {}).get("usage") or {}
        result = self.meter(
            UsageRecord(
                account=record.account,
                domain=record.domain,
                model=record.model,
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            )
        )
        if hasattr(result, "__await__"):
            await result

    def sweep_retention(self) -> int:
        """Delete results past their retention period (SPEC.md section 8).

        Separate from expiry: that is about work never done, this about
        finished output nobody is entitled to keep forever.
        """
        swept = 0
        for record in self.store.all():
            if not record.output_uri or not _as_job(record).results_expired():
                continue
            try:
                self.registry.store.put(output_key(record), b"")
            except Exception:
                log.exception("could not delete results for batch %s", record.id)
            record.output_uri = ""
            swept += 1
        self.store.session.flush()
        return swept


def _as_job(record: BatchRecord) -> Any:
    """The record as the decision functions in `aisvc.batch` expect it.

    They were written against a frozen dataclass so they could be
    tested without a database; this adapts rather than duplicating the
    window and retention rules.
    """
    from aisvc.batch import BatchJob

    return BatchJob(
        id=record.id,
        account=record.account,
        domain=record.domain,
        model=record.model,
        state=BatchState(record.state),
        created_at=_aware(record.created_at),
        finished_at=_aware(record.finished_at),
        request_count=record.request_count,
    )


def _aware(value: Any) -> Any:
    """SQLite hands back naive datetimes; Postgres does not."""
    if value is None:
        return None
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def preemption_signal(
    health: Mapping[str, Any],
) -> Callable[[], bool]:
    """Whether online traffic needs the GPUs right now.

    Built from the same `DeploymentHealth` the autoscaler reads, so the
    two cannot disagree about whether a model is under pressure. One
    busy model is enough: batch shares the same GPUs, and a queue
    anywhere is a queue.

    No health at all does not preempt. Missing Prometheus already
    disables autoscaling; it should not also silently stop every batch,
    which would look like the feature being broken.
    """

    def busy() -> bool:
        return any(
            should_preempt(
                online_queue_depth=entry.metrics.queue_depth,
                gpu_utilization=entry.metrics.gpu_utilization,
            )
            for entry in health.values()
        )

    return busy


def batch_tier(tier: PriceTier, *, discount: float = DEFAULT_DISCOUNT) -> PriceTier:
    """A price tier at the batch rate (SPEC.md section 8).

    A fraction of the online tier rather than a tier of its own, so a
    model's batch price follows its online price instead of going stale
    when that changes.
    """
    return tier.model_copy(
        update={
            "input_per_mtok": price_batch(tier.input_per_mtok, discount=discount),
            "output_per_mtok": price_batch(tier.output_per_mtok, discount=discount),
        }
    )
