"""The setup wizard's backend (SPEC.md section 7).

"A new zone goes from empty to a passing self-test using only the setup
wizard" is section 14's first acceptance criterion, which sets the bar
for these checks: every one that fails has to say what to do about it.
An operator working through a wizard cannot read the source.

Two choices follow from that. Every check runs even after one fails, so
a misconfigured zone is fixed in one pass rather than one problem at a
time. And the GPU checks name IOMMU and the nouveau driver by name:
CloudStack reports neither directly, but a tagged host with no GPU
devices is exactly what both look like, and those are the two things
that are actually wrong
([ADR-0003](../../docs/adr/0003-cloudstack-release.md)).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from aisvc.registry import ModelRegistry

if TYPE_CHECKING:
    from aisvc.inventory import CapacitySnapshot
    from aisvc.spec import PlatformSpec

__all__ = [
    "CheckResult",
    "CheckStatus",
    "PreflightReport",
    "ProposedGpuClass",
    "SelfTestPlan",
    "SelfTestStep",
    "check_capacity",
    "check_registry",
    "check_spec",
    "discover_gpu_classes",
    "plan_self_test",
    "preflight",
]

#: The model the self-test deploys: small enough to load quickly, real
#: enough that serving it proves the path works end to end.
SELF_TEST_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One preflight check, and what to do if it failed."""

    name: str
    status: CheckStatus
    detail: str
    remedy: str = ""

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAIL


@dataclass(frozen=True)
class PreflightReport:
    """Everything the wizard checked before letting a zone be enabled."""

    checks: tuple[CheckResult, ...] = ()

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if check.failed]

    @property
    def warnings(self) -> list[CheckResult]:
        return [check for check in self.checks if check.status is CheckStatus.WARN]

    @property
    def ok(self) -> bool:
        """Warnings do not block: they are worth saying, not worth stopping for."""
        return not self.failures


def check_spec(spec: PlatformSpec) -> CheckResult:
    """The spec loaded and validated.

    Reaching here means it already passed validation, so this reports
    its shape rather than re-checking it.
    """
    document = spec.document
    return CheckResult(
        name="platform spec",
        status=CheckStatus.PASS,
        detail=(
            f"{len(document.zones)} zone(s), {len(document.gpu_classes)} GPU class(es), "
            f"{len(document.models)} model(s), {len(document.tenants)} tenant(s)"
            + (f"; {len(spec.warnings)} warning(s)" if spec.warnings else "")
        ),
    )


def check_registry(registry: ModelRegistry) -> CheckResult:
    """Write an object and read it back.

    A bucket that accepts writes but cannot return them would fail only
    when the first model was deployed, hours later.
    """
    key = f"preflight/{uuid.uuid4()}"
    payload = b"aisvc preflight"

    try:
        registry.store.put(key, payload)
        echoed = registry.store.get(key)
    except Exception as failure:
        return CheckResult(
            name="model registry",
            status=CheckStatus.FAIL,
            detail=f"could not write to s3://{registry.bucket}: {failure}",
            remedy=(
                "Check AISVC_REGISTRY_BUCKET and AISVC_REGISTRY_ENDPOINT, and that "
                "the engine's credentials can write to that bucket."
            ),
        )

    if echoed != payload:
        return CheckResult(
            name="model registry",
            status=CheckStatus.FAIL,
            detail=(f"s3://{registry.bucket} accepted a write but read back different bytes"),
            remedy=(
                "Check AISVC_REGISTRY_ENDPOINT points at the intended object store "
                "and that nothing is rewriting objects in transit."
            ),
        )

    return CheckResult(
        name="model registry",
        status=CheckStatus.PASS,
        detail=f"s3://{registry.bucket} is writable and reads back correctly",
    )


def check_capacity(spec: PlatformSpec, snapshot: CapacitySnapshot | None) -> list[CheckResult]:
    """One check per declared GPU class.

    A new zone has not been observed yet, and section 14 asks the wizard
    to work from empty. So no snapshot means skipped, not failed:
    nothing is known to be wrong, and the remedy says what to do first.
    """
    results: list[CheckResult] = []

    if snapshot is None:
        return [
            CheckResult(
                name=f"GPU class {gpu_class.name} in {zone.name}",
                status=CheckStatus.SKIPPED,
                detail="CloudStack capacity has not been observed yet",
                remedy=(
                    "Run `aisvc plan --observe` (or let the reconciler run once) "
                    "so the engine can read this zone's GPU hosts."
                ),
            )
            for zone in spec.document.zones
            for gpu_class in spec.document.gpu_classes
        ]

    widths: dict[str, int] = {}
    for model in spec.document.models:
        pool = next((p for p in spec.document.node_pools if p.name == model.node_pool), None)
        if pool is not None:
            widths[pool.gpu_class] = max(widths.get(pool.gpu_class, 0), model.tensor_parallel)

    for zone in spec.document.zones:
        for gpu_class in spec.document.gpu_classes:
            name = f"GPU class {gpu_class.name} in {zone.name}"
            observed = snapshot.for_class(zone.name, gpu_class.name)

            if observed is None:
                results.append(
                    CheckResult(
                        name=name,
                        status=CheckStatus.FAIL,
                        detail="not observed in CloudStack",
                        remedy=(
                            f"Check that zone {zone.cloudstack_zone!r} exists and "
                            "that the engine's CloudStack credentials can see it."
                        ),
                    )
                )
                continue

            if observed.nodes_healthy == 0:
                results.append(
                    CheckResult(
                        name=name,
                        status=CheckStatus.FAIL,
                        detail="no healthy hosts carry this class's tag",
                        remedy=(
                            f"Tag the GPU hosts with {gpu_class.host_tag!r} and make "
                            "sure they are Up and Enabled."
                        ),
                    )
                )
                continue

            if observed.gpus_total == 0:
                results.append(
                    CheckResult(
                        name=name,
                        status=CheckStatus.FAIL,
                        detail=(
                            f"{observed.nodes_healthy} tagged host(s), but CloudStack "
                            "has discovered no GPU devices on them"
                        ),
                        remedy=(
                            "Enable IOMMU in the BIOS and on the kernel command line "
                            "(intel_iommu=on or amd_iommu=on), blacklist the nouveau "
                            "driver, and reconnect the host so CloudStack rescans it."
                        ),
                    )
                )
                continue

            needed = widths.get(gpu_class.name, 1)
            if observed.largest_free_block() < needed:
                results.append(
                    CheckResult(
                        name=name,
                        status=CheckStatus.WARN,
                        detail=(
                            f"{observed.gpus_free} GPU(s) free, but the widest model "
                            f"needs {needed} on one host and the largest free block "
                            f"is {observed.largest_free_block()}"
                        ),
                        remedy=(
                            "Add GPU hosts, or lower tensor_parallel for the models on this class."
                        ),
                    )
                )
                continue

            results.append(
                CheckResult(
                    name=name,
                    status=CheckStatus.PASS,
                    detail=(
                        f"{observed.nodes_healthy} healthy host(s), "
                        f"{observed.gpus_total} GPU(s), {observed.gpus_free} free"
                    ),
                )
            )

    return results


@dataclass(frozen=True, slots=True)
class ProposedGpuClass:
    """A GPU class the wizard suggests, from what CloudStack reports."""

    host_tag: str
    hosts: int
    suggested_name: str


def discover_gpu_classes(snapshot: CapacitySnapshot) -> list[ProposedGpuClass]:
    """Propose GPU classes from the tags on healthy hosts (SPEC.md section 7).

    A proposal, not a decision: the operator maps these onto service
    offerings, and only they know which offering corresponds to which
    tag.
    """
    counts: dict[str, int] = {}
    for host in snapshot.hosts:
        if not host.healthy:
            continue
        for tag in host.tags:
            counts[tag] = counts.get(tag, 0) + 1

    return [
        ProposedGpuClass(
            host_tag=tag,
            hosts=count,
            suggested_name=tag.replace("_", "-").lower(),
        )
        for tag, count in sorted(counts.items())
    ]


@dataclass(frozen=True, slots=True)
class SelfTestStep:
    """One step of the self-test, named so the report reads plainly."""

    name: str
    description: str


@dataclass(frozen=True)
class SelfTestPlan:
    """What the self-test will do (SPEC.md section 7).

    Teardown is part of the plan rather than a caller's responsibility:
    a self-test that leaves a model running has failed at its job, which
    is to prove the zone works and then leave it as it was.
    """

    zone: str
    model: str
    steps: tuple[SelfTestStep, ...] = ()
    teardown: bool = True

    @property
    def step_names(self) -> list[str]:
        return [step.name for step in self.steps]


def plan_self_test(spec: PlatformSpec, *, zone: str) -> SelfTestPlan:
    """Plan an end-to-end check of one zone."""
    if not any(z.name == zone for z in spec.document.zones):
        raise ValueError(f"no zone named {zone!r} in the spec")

    return SelfTestPlan(
        zone=zone,
        model=SELF_TEST_MODEL,
        steps=(
            SelfTestStep("mirror", f"mirror {SELF_TEST_MODEL} into the registry"),
            SelfTestStep("deploy", "deploy it on the smallest GPU class in the zone"),
            SelfTestStep("request", "send a chat completion through the gateway"),
            SelfTestStep("meter", "check the request produced a usage record"),
            SelfTestStep("teardown", "remove the deployment and leave the zone as found"),
        ),
    )


def preflight(
    spec: PlatformSpec,
    *,
    registry: ModelRegistry,
    snapshot: CapacitySnapshot | None,
) -> PreflightReport:
    """Run every check, so one pass surfaces every problem."""
    checks: list[CheckResult] = [check_spec(spec), check_registry(registry)]
    checks.extend(check_capacity(spec, snapshot))
    return PreflightReport(checks=tuple(checks))
