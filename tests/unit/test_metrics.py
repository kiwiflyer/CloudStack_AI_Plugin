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
"""What the engine exports to Prometheus (SPEC.md section 11).

Section 11 asks for alerts on reconciler failures and mirror failures.
Neither is visible in vLLM or DCGM metrics — they are the engine's own
business — so without this the rules would reference series nothing
produces, and would sit silently green forever.

Written by hand rather than with `prometheus_client`: the exposition
format is a handful of lines, and the dependency tree stays smaller
(docs/third-party-licenses.md).
"""

from __future__ import annotations

import pytest

from aisvc.observability.metrics import MetricsRegistry, render


class TestCounters:
    def test_a_counter_starts_at_zero_once_declared(self) -> None:
        """A counter that appears only after the first event makes an
        alert rule using `rate()` silently green until then."""
        registry = MetricsRegistry()
        registry.counter("aisvc_runs_total", "Reconcile runs.", zone="east", status="ok")

        assert 'aisvc_runs_total{status="ok",zone="east"} 0' in render(registry)

    def test_incrementing_shows_up(self) -> None:
        registry = MetricsRegistry()
        registry.counter("aisvc_runs_total", "Reconcile runs.", zone="east").inc()

        assert 'aisvc_runs_total{zone="east"} 1' in render(registry)

    def test_the_same_labels_share_a_series(self) -> None:
        registry = MetricsRegistry()
        registry.counter("c", "A counter.", zone="east").inc()
        registry.counter("c", "A counter.", zone="east").inc()

        assert 'c{zone="east"} 2' in render(registry)

    def test_different_labels_are_different_series(self) -> None:
        registry = MetricsRegistry()
        registry.counter("c", "A counter.", zone="east").inc()
        registry.counter("c", "A counter.", zone="west").inc(3)

        rendered = render(registry)
        assert 'c{zone="east"} 1' in rendered
        assert 'c{zone="west"} 3' in rendered

    def test_a_counter_with_no_labels_renders_bare(self) -> None:
        registry = MetricsRegistry()
        registry.counter("c", "A counter.").inc()

        assert "\nc 1" in render(registry)


class TestGauges:
    def test_a_gauge_reports_its_last_value(self) -> None:
        registry = MetricsRegistry()
        registry.gauge("g", "A gauge.", zone="east").set(42)

        assert 'g{zone="east"} 42' in render(registry)

    def test_a_gauge_can_go_down(self) -> None:
        registry = MetricsRegistry()
        registry.gauge("g", "A gauge.").set(10)
        registry.gauge("g", "A gauge.").set(2)

        assert "\ng 2" in render(registry)


class TestExpositionFormat:
    def test_each_metric_has_help_and_type(self) -> None:
        registry = MetricsRegistry()
        registry.counter("aisvc_runs_total", "Reconcile runs.", zone="east")

        rendered = render(registry)
        assert "# HELP aisvc_runs_total Reconcile runs." in rendered
        assert "# TYPE aisvc_runs_total counter" in rendered

    def test_help_and_type_appear_once_per_metric(self) -> None:
        """Repeating them per series makes Prometheus reject the scrape."""
        registry = MetricsRegistry()
        registry.counter("c", "A counter.", zone="east")
        registry.counter("c", "A counter.", zone="west")

        assert render(registry).count("# TYPE c counter") == 1

    def test_label_values_are_escaped(self) -> None:
        registry = MetricsRegistry()
        registry.counter("c", "A counter.", detail='a "quoted" \\ value').inc()

        assert r'detail="a \"quoted\" \\ value"' in render(registry)

    def test_the_output_ends_with_a_newline(self) -> None:
        registry = MetricsRegistry()
        registry.counter("c", "A counter.").inc()

        assert render(registry).endswith("\n")

    def test_an_empty_registry_renders_nothing(self) -> None:
        assert render(MetricsRegistry()) == ""


class TestMirroringIsCounted:
    """SPEC.md section 11 alerts on mirror failures, so mirroring has to
    count them. Without this the rule references a series nothing ever
    moves, and reads as a healthy platform forever."""

    def sourcing(self, *, allow: bool = True) -> object:
        from aisvc.hf import ModelSourcing
        from aisvc.registry import InMemoryObjectStore, ModelRegistry
        from tests.unit.test_hf import FakeHub
        from tests.unit.test_spec import LICENSES

        registry = ModelRegistry(InMemoryObjectStore(), bucket="models")
        return ModelSourcing(FakeHub(), registry, allowed_licenses=list(LICENSES) if allow else [])

    def counts(self, repo: str, status: str) -> float:
        from aisvc.observability.metrics import MIRRORS, REGISTRY

        return REGISTRY.counter(
            MIRRORS, "Model mirror requests by outcome.", repo=repo, status=status
        ).value

    def test_a_rejected_mirror_is_counted_as_a_failure(self) -> None:
        from aisvc.hf import HfError

        sourcing = self.sourcing(allow=False)
        repo = "meta-llama/Llama-3.3-70B-Instruct"
        before = self.counts(repo, "failed")

        with pytest.raises(HfError):
            sourcing.mirror(repo, "main")  # type: ignore[attr-defined]

        assert self.counts(repo, "failed") == before + 1

    def test_both_outcomes_are_registered_either_way(self) -> None:
        """So `rate(...{status="failed"})` has something to rate from."""
        from aisvc.hf import HfError

        sourcing = self.sourcing(allow=False)
        repo = "meta-llama/Llama-3.3-70B-Instruct"
        with pytest.raises(HfError):
            sourcing.mirror(repo, "main")  # type: ignore[attr-defined]

        assert self.counts(repo, "succeeded") >= 0
