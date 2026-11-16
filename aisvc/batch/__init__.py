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
"""Batch inference (SPEC.md section 8).

OpenAI-compatible: a JSONL file of requests in, a JSONL file of results
out within a completion window.

**The discount is payable because batch yields.** A batch that competed
with online traffic would be cheap traffic degrading the SLO that
everyone else is paying full rate for, so preemption is not a nicety
here — it is the thing that makes the price defensible.

The states are OpenAI's by name, because a tenant's existing batch
client reads those strings and the whole point of being compatible is
that they do not have to care whose platform it is.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

__all__ = [
    "DEFAULT_COMPLETION_WINDOW",
    "DEFAULT_DISCOUNT",
    "DEFAULT_RETENTION",
    "SUPPORTED_ENDPOINTS",
    "BatchJob",
    "BatchRejected",
    "BatchRequest",
    "BatchState",
    "expired",
    "parse_requests",
    "price_batch",
    "should_preempt",
]

#: SPEC.md section 8's default window.
DEFAULT_COMPLETION_WINDOW = dt.timedelta(hours=24)

#: How long finished results stay downloadable.
DEFAULT_RETENTION = dt.timedelta(days=30)

#: Half the online rate unless configured otherwise.
DEFAULT_DISCOUNT = 0.5

#: What a batch line may target. Chat and embeddings only: a rerank or
#: a vision request has no batch semantics worth the surface yet.
SUPPORTED_ENDPOINTS = ("/v1/chat/completions", "/v1/embeddings")

#: Online load past which batches stand down. Set so an idle platform
#: runs them and a busy one does not — yielding at the first sign of
#: traffic would mean batches never finish on a platform doing anything.
PREEMPT_QUEUE_DEPTH = 8
PREEMPT_GPU_UTILIZATION = 0.9


class BatchState(StrEnum):
    """OpenAI's batch states, by their names."""

    VALIDATING = "validating"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {BatchState.COMPLETED, BatchState.FAILED, BatchState.EXPIRED, BatchState.CANCELLED}
)


class BatchRejected(Exception):
    """An input file cannot be accepted, with a reason and a location."""

    status_code = 400

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """One line of the input file."""

    custom_id: str
    method: str
    url: str
    body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchJob:
    """A submitted batch."""

    id: str
    account: str
    domain: str
    model: str
    state: BatchState = BatchState.VALIDATING
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    finished_at: dt.datetime | None = None
    completion_window: dt.timedelta = DEFAULT_COMPLETION_WINDOW
    retention: dt.timedelta = DEFAULT_RETENTION
    request_count: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def results_expired(self) -> bool:
        """Whether the results have passed their retention period.

        Separate from the completion window: that one is about work
        never done, this one about finished output going stale.
        """
        if self.finished_at is None:
            return False
        return dt.datetime.now(dt.UTC) - self.finished_at > self.retention


def parse_requests(jsonl: str, *, model: str) -> list[BatchRequest]:
    """Parse and validate a batch input file against its declared model.

    Errors name the line. A tenant debugging a 50,000-line file with
    "invalid JSON" and nothing else has no way in.

    **Every line must serve `model`.** The runner posts each body
    verbatim under one shared operator key, so by the time the gateway
    sees a request the submitting account is gone and every scope,
    budget and guardrail check runs against that key instead. A line
    naming a different model would reach the gateway with no
    tenant-level check at all, while metering recorded the cost
    against the model the batch declared. `model` is required rather
    than optional because a check that can be omitted is one that will
    be.
    """
    requests: list[BatchRequest] = []
    seen: set[str] = set()

    for number, raw in enumerate(jsonl.splitlines(), start=1):
        line = raw.strip()
        if not line:
            # Files written by shell loops end with a newline.
            continue

        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BatchRejected(
                "invalid_json", f"line {number} is not valid JSON: {exc.msg}"
            ) from exc

        custom_id = parsed.get("custom_id")
        if not custom_id:
            raise BatchRejected(
                "missing_custom_id",
                f"line {number} has no custom_id; results come back unordered "
                "and that is how a result is matched to its request",
            )
        if custom_id in seen:
            raise BatchRejected(
                "duplicate_custom_id",
                f"line {number} repeats custom_id {custom_id!r}, so two results "
                "would be indistinguishable",
            )
        seen.add(custom_id)

        url = parsed.get("url", "")
        if url not in SUPPORTED_ENDPOINTS:
            supported = ", ".join(SUPPORTED_ENDPOINTS)
            raise BatchRejected(
                "unsupported_endpoint",
                f"line {number} targets {url!r}; batches support {supported}",
            )

        body = dict(parsed.get("body") or {})
        named = body.get("model")
        if named is None or named == "":
            # Omitting it is ordinary, and the declared model is what
            # metering already bills, so it is filled in rather than
            # refused.
            body["model"] = model
        elif named != model:
            raise BatchRejected(
                "model_mismatch",
                f"line {number} asks for model {named!r} but the batch declares "
                f"{model!r}; a batch serves one model, and its requests are not "
                f"checked against the submitting account's scopes",
            )

        requests.append(
            BatchRequest(
                custom_id=custom_id,
                method=parsed.get("method", "POST"),
                url=url,
                body=body,
            )
        )

    if not requests:
        raise BatchRejected("empty_batch", "the batch input file has no requests")

    return requests


def expired(job: BatchJob, *, now: dt.datetime | None = None) -> bool:
    """Whether a job missed its completion window.

    A finished job never expires: the window is a promise about work
    being done, and it was.
    """
    if job.terminal:
        return False
    moment = now or dt.datetime.now(dt.UTC)
    return moment - job.created_at > job.completion_window


def should_preempt(*, online_queue_depth: float, gpu_utilization: float) -> bool:
    """Whether batch work should stand down for online traffic.

    Either signal alone is enough: a queue means requests are already
    waiting, and high utilisation means the next one will.
    """
    return online_queue_depth >= PREEMPT_QUEUE_DEPTH or gpu_utilization >= PREEMPT_GPU_UTILIZATION


def price_batch(online_cost: float, *, discount: float = DEFAULT_DISCOUNT) -> float:
    """What a batch costs, as a fraction of the online rate.

    A fraction rather than its own tier, so a model's batch price
    follows its online price instead of going stale when that changes.
    """
    if not 0 <= discount <= 1:
        raise ValueError(
            f"a batch discount is a fraction of the online rate, between 0 and 1; got {discount}"
        )
    return online_cost * discount
