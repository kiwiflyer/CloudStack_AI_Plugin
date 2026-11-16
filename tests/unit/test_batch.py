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
out within a completion window. It runs on spare capacity and is
preempted by online traffic, which is what makes the discount payable —
a batch that competed with online requests would just be cheap traffic
degrading the SLO everyone else pays for.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aisvc.batch import (
    DEFAULT_COMPLETION_WINDOW,
    DEFAULT_RETENTION,
    BatchJob,
    BatchRejected,
    BatchState,
    expired,
    parse_requests,
    price_batch,
    should_preempt,
)

LINE = (
    '{"custom_id":"a","method":"POST","url":"/v1/chat/completions",'
    '"body":{"model":"llama","messages":[{"role":"user","content":"hi"}]}}'
)


class TestParsingTheInputFile:
    def test_a_well_formed_file_parses(self) -> None:
        assert len(parse_requests(LINE)) == 1

    def test_every_line_is_one_request(self) -> None:
        lines = [LINE.replace('"custom_id":"a"', f'"custom_id":"r{i}"') for i in range(3)]

        assert len(parse_requests("\n".join(lines))) == 3

    def test_blank_lines_are_ignored(self) -> None:
        """Files written by shell loops end with a newline."""
        assert len(parse_requests(f"{LINE}\n\n")) == 1

    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(BatchRejected) as caught:
            parse_requests("")

        assert caught.value.reason == "empty_batch"

    def test_malformed_json_names_the_line(self) -> None:
        """A tenant with a 50,000-line file needs the line number."""
        with pytest.raises(BatchRejected) as caught:
            parse_requests(f"{LINE}\nnot json\n{LINE}")

        assert "line 2" in caught.value.detail

    def test_a_request_without_a_custom_id_is_refused(self) -> None:
        """Results come back unordered; custom_id is how a tenant
        matches a result to its request."""
        with pytest.raises(BatchRejected) as caught:
            parse_requests('{"method":"POST","url":"/v1/chat/completions","body":{}}')

        assert caught.value.reason == "missing_custom_id"

    def test_duplicate_custom_ids_are_refused(self) -> None:
        with pytest.raises(BatchRejected) as caught:
            parse_requests("\n".join([LINE, LINE]))

        assert caught.value.reason == "duplicate_custom_id"

    def test_an_unsupported_endpoint_is_refused(self) -> None:
        line = '{"custom_id":"a","method":"POST","url":"/v1/images","body":{}}'

        with pytest.raises(BatchRejected) as caught:
            parse_requests(line)

        assert caught.value.reason == "unsupported_endpoint"

    def test_embeddings_are_supported_too(self) -> None:
        line = (
            '{"custom_id":"a","method":"POST","url":"/v1/embeddings",'
            '"body":{"model":"bge","input":"x"}}'
        )

        assert len(parse_requests(line)) == 1


class TestTheCompletionWindow:
    def test_the_documented_default_is_twenty_four_hours(self) -> None:
        assert dt.timedelta(hours=24) == DEFAULT_COMPLETION_WINDOW

    def test_a_job_inside_its_window_is_not_expired(self) -> None:
        job = BatchJob(
            id="b-1",
            account="acme",
            domain="/d",
            model="llama",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
        )

        assert not expired(job)

    def test_a_job_past_its_window_is_expired(self) -> None:
        job = BatchJob(
            id="b-1",
            account="acme",
            domain="/d",
            model="llama",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=25),
        )

        assert expired(job)

    def test_a_finished_job_does_not_expire(self) -> None:
        """Expiry is about work never done, not results going stale —
        retention covers those."""
        job = BatchJob(
            id="b-1",
            account="acme",
            domain="/d",
            model="llama",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=25),
            state=BatchState.COMPLETED,
        )

        assert not expired(job)

    def test_the_window_can_be_shortened(self) -> None:
        job = BatchJob(
            id="b-1",
            account="acme",
            domain="/d",
            model="llama",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
            completion_window=dt.timedelta(hours=1),
        )

        assert expired(job)


class TestPreemption:
    def test_a_quiet_platform_runs_batches(self) -> None:
        assert not should_preempt(online_queue_depth=0, gpu_utilization=0.2)

    def test_online_traffic_preempts_a_batch(self) -> None:
        """The discount is payable because batch yields; if it did not,
        it would be cheap traffic degrading everyone's SLO."""
        assert should_preempt(online_queue_depth=12, gpu_utilization=0.9)

    def test_a_queue_alone_preempts(self) -> None:
        assert should_preempt(online_queue_depth=12, gpu_utilization=0.1)

    def test_high_utilisation_alone_preempts(self) -> None:
        assert should_preempt(online_queue_depth=0, gpu_utilization=0.95)

    def test_moderate_load_does_not_preempt(self) -> None:
        """Yielding at the first sign of traffic means batches never
        finish on a platform that is doing anything at all."""
        assert not should_preempt(online_queue_depth=1, gpu_utilization=0.5)


class TestPricing:
    def test_a_batch_is_cheaper_than_online(self) -> None:
        assert price_batch(10.0, discount=0.5) == pytest.approx(5.0)

    def test_the_documented_default_discount_applies(self) -> None:
        assert price_batch(10.0) == pytest.approx(5.0)

    def test_no_discount_costs_the_online_rate(self) -> None:
        assert price_batch(10.0, discount=1.0) == pytest.approx(10.0)

    def test_a_free_batch_tier_is_allowed(self) -> None:
        assert price_batch(10.0, discount=0.0) == 0.0

    def test_a_discount_above_one_is_refused(self) -> None:
        """That would charge more for batch than for online."""
        with pytest.raises(ValueError, match="discount"):
            price_batch(10.0, discount=1.5)

    def test_a_negative_discount_is_refused(self) -> None:
        with pytest.raises(ValueError, match="discount"):
            price_batch(10.0, discount=-0.1)


class TestRetention:
    def test_the_documented_default_is_thirty_days(self) -> None:
        assert dt.timedelta(days=30) == DEFAULT_RETENTION

    def test_results_within_retention_are_kept(self) -> None:
        job = BatchJob(
            id="b-1",
            account="acme",
            domain="/d",
            model="llama",
            state=BatchState.COMPLETED,
            finished_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
        )

        assert not job.results_expired()

    def test_results_past_retention_are_not(self) -> None:
        job = BatchJob(
            id="b-1",
            account="acme",
            domain="/d",
            model="llama",
            state=BatchState.COMPLETED,
            finished_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=31),
        )

        assert job.results_expired()

    def test_an_unfinished_job_has_no_results_to_expire(self) -> None:
        job = BatchJob(id="b-1", account="acme", domain="/d", model="llama")

        assert not job.results_expired()


class TestStates:
    def test_a_new_job_is_validating(self) -> None:
        job = BatchJob(id="b-1", account="acme", domain="/d", model="llama")

        assert job.state is BatchState.VALIDATING

    def test_the_states_match_the_openai_batch_api(self) -> None:
        """SPEC.md section 8 says OpenAI-compatible, and a tenant's
        existing client reads these strings."""
        assert {s.value for s in BatchState} == {
            "validating",
            "in_progress",
            "completed",
            "failed",
            "expired",
            "cancelled",
        }

    def test_a_terminal_job_is_not_running(self) -> None:
        for state in (BatchState.COMPLETED, BatchState.FAILED, BatchState.CANCELLED):
            job = BatchJob(id="b", account="a", domain="/d", model="m", state=state)
            assert job.terminal

    def test_a_running_job_is_not_terminal(self) -> None:
        job = BatchJob(id="b", account="a", domain="/d", model="m", state=BatchState.IN_PROGRESS)

        assert not job.terminal
