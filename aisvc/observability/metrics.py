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
"""The engine's own Prometheus metrics (SPEC.md section 11).

Section 11 asks for alerts on reconciler failures and mirror failures.
Those are the engine's business — nothing in vLLM or DCGM can see them —
so without an exporter here the alert rules would reference series that
nothing produces and would sit green through every outage.

Written by hand rather than pulling in `prometheus_client`: the
exposition format is a few lines and the dependency tree stays smaller
and easier to licence-audit (docs/third-party-licenses.md).

Declaring a counter registers it at zero immediately. That matters:
`rate(x[5m]) > 0` on a series that does not exist yet never fires, so a
counter that only appears on the first failure is an alert that cannot
go off the first time.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "MIRRORS",
    "REGISTRY",
    "RUNS",
    "Counter",
    "Gauge",
    "MetricsRegistry",
    "record_mirror",
    "record_run",
    "register_zones",
    "render",
]

_LABEL_ESCAPES = str.maketrans({"\\": r"\\", '"': r"\"", "\n": r"\n"})


def _label_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{name}="{value.translate(_LABEL_ESCAPES)}"' for name, value in labels)
    return "{" + inner + "}"


@dataclass
class _Series:
    value: float = 0.0


@dataclass
class _Metric:
    name: str
    help_text: str
    kind: str
    series: dict[tuple[tuple[str, str], ...], _Series] = field(default_factory=dict)


class Counter:
    """A handle on one counter series."""

    def __init__(self, series: _Series, lock: threading.Lock) -> None:
        self._series = series
        self._lock = lock

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._series.value += amount

    @property
    def value(self) -> float:
        return self._series.value


class Gauge:
    """A handle on one gauge series."""

    def __init__(self, series: _Series, lock: threading.Lock) -> None:
        self._series = series
        self._lock = lock

    def set(self, value: float) -> None:
        with self._lock:
            self._series.value = float(value)

    @property
    def value(self) -> float:
        return self._series.value


class MetricsRegistry:
    """Everything this engine process exports.

    Safe to touch from the request handlers and the reconciler at once;
    the lock is only held for the arithmetic.
    """

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str, **labels: str) -> Counter:
        return Counter(self._series(name, help_text, "counter", labels), self._lock)

    def gauge(self, name: str, help_text: str, **labels: str) -> Gauge:
        return Gauge(self._series(name, help_text, "gauge", labels), self._lock)

    def _series(self, name: str, help_text: str, kind: str, labels: dict[str, str]) -> _Series:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = _Metric(name=name, help_text=help_text, kind=kind)
                self._metrics[name] = metric
            key = _label_key(labels)
            series = metric.series.get(key)
            if series is None:
                # Registered at zero, so an alert can fire the first
                # time rather than waiting for a second occurrence.
                series = _Series()
                metric.series[key] = series
            return series

    def clear(self) -> None:
        """Forget every series.

        For tests: the process-wide REGISTRY is shared, so one test's
        counters otherwise leak into the next one's assertions.
        """
        with self._lock:
            self._metrics.clear()

    def metrics(self) -> list[_Metric]:
        with self._lock:
            return list(self._metrics.values())


def render(registry: MetricsRegistry) -> str:
    """The Prometheus text exposition format."""
    lines: list[str] = []
    for metric in registry.metrics():
        # HELP and TYPE go once per metric, not once per series:
        # repeating them makes Prometheus reject the whole scrape.
        lines.append(f"# HELP {metric.name} {metric.help_text}")
        lines.append(f"# TYPE {metric.name} {metric.kind}")
        for labels, series in sorted(metric.series.items()):
            value = series.value
            rendered = str(int(value)) if float(value).is_integer() else repr(float(value))
            lines.append(f"{metric.name}{_render_labels(labels)} {rendered}")
    return "\n".join(lines) + "\n" if lines else ""


#: The registry this process exports. Module-level so the reconciler and
#: the API handlers write to the same one without threading it through
#: every call; `/metrics` renders it.
REGISTRY = MetricsRegistry()

RUNS = "aisvc_reconcile_runs_total"
MIRRORS = "aisvc_mirror_requests_total"


def record_run(zone: str, *, failed: bool) -> None:
    """Count a finished reconcile run (SPEC.md section 11: alert on failures)."""
    for status in ("succeeded", "failed"):
        # Both series are registered so `rate(...{status="failed"}[15m])`
        # has something to rate from the first run, not the first failure.
        REGISTRY.counter(RUNS, "Reconcile runs by outcome.", zone=zone, status=status)
    REGISTRY.counter(
        RUNS,
        "Reconcile runs by outcome.",
        zone=zone,
        status="failed" if failed else "succeeded",
    ).inc()


def record_mirror(repo: str, *, failed: bool) -> None:
    """Count a finished mirror request (SPEC.md section 11)."""
    for status in ("succeeded", "failed"):
        REGISTRY.counter(MIRRORS, "Model mirror requests by outcome.", repo=repo, status=status)
    REGISTRY.counter(
        MIRRORS,
        "Model mirror requests by outcome.",
        repo=repo,
        status="failed" if failed else "succeeded",
    ).inc()


def register_zones(zones: Sequence[str]) -> None:
    """Export every zone's run counters at zero, before the first run.

    Without this a fresh engine exports nothing at all, and an alert
    like `increase(...{status="failed"}[30m]) > 0` cannot fire on the
    first failure because there is no series to compare against.
    """
    for zone in zones:
        for status in ("succeeded", "failed"):
            REGISTRY.counter(RUNS, "Reconcile runs by outcome.", zone=zone, status=status)
