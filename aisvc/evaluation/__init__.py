"""The pre-publish evaluation gate (SPEC.md section 8).

A new model, revision or quantization is evaluated in a staging
deployment before it can be published, and a regression past the
threshold blocks it.

The failure this exists to prevent is quiet: a quantization that saves
memory and loses two points on reasoning looks identical to one that
loses nothing, until a tenant notices months later. Nobody catches that
by looking.

Two judgements are built in, and both cut against making the gate
stricter:

- The default threshold is two points because benchmarks are noisy.
  A gate that fires on noise is a gate people learn to override, and an
  override reflex is worse than no gate.
- Performance does not gate. A revision being slower on one GPU class
  is a capacity question — `capacity` sizes from these numbers — not a
  reason to refuse to publish a model that answers correctly.

An empty evaluation does **not** pass. Publishing because nothing ran
looks exactly like publishing because everything passed, which is the
one outcome worth being paranoid about.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

__all__ = [
    "DEFAULT_REGRESSION_THRESHOLD",
    "EvaluationRun",
    "GateBlocked",
    "GateDecision",
    "PerformanceResult",
    "Regression",
    "SuiteResult",
    "gate",
    "override_gate",
]

#: Points a suite may drop before publishing is blocked.
DEFAULT_REGRESSION_THRESHOLD = 2.0

#: Only this role may override a blocked gate (SPEC.md section 8).
ROOT_ADMIN = "Admin"


class GateBlocked(Exception):
    """An override was refused."""


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """One benchmark or customer golden set.

    `baseline` is None for a model's first evaluation: there is no
    previous version, so it cannot have got worse.
    """

    name: str
    score: float
    baseline: float | None = None
    #: Set when this is a customer's own golden set rather than a
    #: standard benchmark, so a block can say whose expectations moved.
    customer: str = ""


@dataclass(frozen=True, slots=True)
class PerformanceResult:
    """TTFT and throughput on one GPU class (SPEC.md section 8).

    Per class because that is how `capacity` consumes them; a single
    averaged number would be true of no hardware anyone runs.
    """

    gpu_class: str
    ttft_p95_ms: float
    tokens_per_second: float


@dataclass(frozen=True, slots=True)
class Regression:
    """One suite that dropped too far."""

    name: str
    score: float
    baseline: float
    delta: float
    customer: str = ""


@dataclass(frozen=True)
class EvaluationRun:
    """What a staging deployment produced."""

    model: str
    revision: str
    suites: tuple[SuiteResult, ...] = ()
    performance: tuple[PerformanceResult, ...] = ()


@dataclass(frozen=True)
class GateDecision:
    """Whether this may be published, and why."""

    passed: bool
    summary: str
    regressions: list[Regression] = field(default_factory=list)
    performance: tuple[PerformanceResult, ...] = ()
    overridden: bool = False


def gate(run: EvaluationRun, *, threshold: float = DEFAULT_REGRESSION_THRESHOLD) -> GateDecision:
    """Decide whether an evaluated revision may be published."""
    if not run.suites:
        return GateDecision(
            passed=False,
            summary=(
                f"{run.model}@{run.revision} ran no suites, so nothing is known "
                "about it; an empty evaluation is not a passing one"
            ),
            performance=run.performance,
        )

    regressions = []
    for suite in run.suites:
        if suite.baseline is None:
            # First evaluation of this model: a baseline, not a
            # comparison. Nothing to regress against.
            continue
        delta = suite.score - suite.baseline
        if delta < -threshold:
            regressions.append(
                Regression(
                    name=suite.name,
                    score=suite.score,
                    baseline=suite.baseline,
                    delta=delta,
                    customer=suite.customer,
                )
            )

    if regressions:
        worst = ", ".join(
            f"{r.name} {r.delta:+.1f}" + (f" ({r.customer})" if r.customer else "")
            for r in regressions
        )
        return GateDecision(
            passed=False,
            summary=(f"{run.model}@{run.revision} regressed past {threshold:g} points: {worst}"),
            regressions=regressions,
            performance=run.performance,
        )

    return GateDecision(
        passed=True,
        summary=(
            f"{run.model}@{run.revision} passed {len(run.suites)} suite(s) within "
            f"{threshold:g} points"
        ),
        performance=run.performance,
    )


def override_gate(decision: GateDecision, *, role: str, reason: str) -> GateDecision:
    """Publish anyway, on a root admin's recorded say-so.

    The reason is mandatory and kept in the summary. An override nobody
    can account for afterwards is how a gate stops meaning anything —
    the record is the thing that keeps it honest, not the permission.
    """
    if decision.passed:
        # Nothing to override; saying so beats marking a clean result
        # as forced through.
        return decision

    if role != ROOT_ADMIN:
        raise GateBlocked(
            f"only a root admin may override a blocked evaluation gate; {role} may not"
        )

    if not reason.strip():
        raise GateBlocked("an override needs a reason, and it is recorded")

    return replace(
        decision,
        passed=True,
        overridden=True,
        summary=f"{decision.summary} — overridden by a root admin: {reason.strip()}",
    )
