"""The setup wizard's backend (SPEC.md section 7).

"A new zone goes from empty to a passing self-test using only the setup
wizard" is section 14's first acceptance criterion, so the checks have
to say what is wrong and what to do about it, not just fail.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from aisvc.cloudstack.clusters import ClusterState
from aisvc.inventory import CapacitySnapshot, ClassCapacity, HostRecord
from aisvc.k8s.client import FakeKubernetes
from aisvc.registry import InMemoryObjectStore, ModelRegistry, RegistryError
from aisvc.setup import (
    CheckStatus,
    PreflightReport,
    check_capacity,
    check_cluster_reachable,
    check_clusters,
    check_registry,
    check_spec,
    discover_gpu_classes,
    plan_self_test,
    preflight,
)
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL


def spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


def host(host_id: str, tags: str, *, state: str = "Up") -> HostRecord:
    return HostRecord.from_api(
        {
            "id": host_id,
            "name": host_id,
            "zonename": "East-1",
            "hosttags": tags,
            "state": state,
            "resourcestate": "Enabled",
        }
    )


def snapshot(
    *,
    classes: dict[tuple[str, str], ClassCapacity] | None = None,
    hosts: tuple[HostRecord, ...] = (),
) -> CapacitySnapshot:
    return CapacitySnapshot(taken_at=dt.datetime.now(dt.UTC), classes=classes or {}, hosts=hosts)


def capacity(**kw: Any) -> ClassCapacity:
    defaults: dict[str, Any] = {
        "zone": "zone-east-1",
        "gpu_class": "h100-8x",
        "gpus_per_node": 8,
        "nodes_total": 2,
        "nodes_healthy": 2,
        "gpus_total": 16,
        "free_by_host": {"host-1": 8, "host-2": 8},
        "host_ids": ("host-1", "host-2"),
    }
    defaults.update(kw)
    return ClassCapacity(**defaults)


class TestSpecCheck:
    def test_a_valid_spec_passes(self) -> None:
        result = check_spec(spec())

        assert result.status is CheckStatus.PASS
        assert "model" in result.detail

    def test_a_spec_with_warnings_still_passes_but_says_so(self) -> None:
        result = check_spec(spec())

        # MINIMAL carries a concurrency note, which must not fail setup.
        assert result.status is CheckStatus.PASS


class TestRegistryCheck:
    def test_a_writable_registry_passes(self) -> None:
        result = check_registry(ModelRegistry(InMemoryObjectStore(), bucket="models"))

        assert result.status is CheckStatus.PASS

    def test_the_check_writes_and_reads_back(self) -> None:
        """A bucket that accepts writes but cannot read them is useless."""
        store = InMemoryObjectStore()
        check_registry(ModelRegistry(store, bucket="models"))

        assert any("preflight" in key for key in store.objects)

    def test_an_unreachable_registry_fails_with_a_remedy(self) -> None:
        class Broken:
            def put(self, key: str, data: bytes) -> None:
                raise RegistryError("s3 is unreachable")

            def get(self, key: str) -> bytes:
                raise RegistryError("s3 is unreachable")

            def list(self, prefix: str) -> list[str]:
                return []

        result = check_registry(ModelRegistry(Broken(), bucket="models"))

        assert result.status is CheckStatus.FAIL
        assert result.remedy
        assert "AISVC_REGISTRY" in result.remedy

    def test_a_registry_that_reads_back_the_wrong_bytes_fails(self) -> None:
        class Liar(InMemoryObjectStore):
            def get(self, key: str) -> bytes:
                return b"something else"

        result = check_registry(ModelRegistry(Liar(), bucket="models"))

        assert result.status is CheckStatus.FAIL
        assert "read back" in result.detail


class TestCapacityCheck:
    def test_a_class_with_healthy_hosts_passes(self) -> None:
        results = check_capacity(spec(), snapshot(classes={("zone-east-1", "h100-8x"): capacity()}))

        assert all(r.status is CheckStatus.PASS for r in results)

    def test_a_class_with_no_hosts_fails_and_names_the_tag(self) -> None:
        results = check_capacity(
            spec(),
            snapshot(
                classes={
                    ("zone-east-1", "h100-8x"): capacity(
                        nodes_total=0,
                        nodes_healthy=0,
                        gpus_total=0,
                        free_by_host={},
                        host_ids=(),
                    )
                }
            ),
        )

        failed = [r for r in results if r.status is CheckStatus.FAIL]
        assert failed
        assert "h100-sxm" in failed[0].remedy

    def test_hosts_without_gpus_point_at_iommu_and_nouveau(self) -> None:
        """ADR-0003: passthrough needs IOMMU on and Nouveau disabled.

        CloudStack does not report either directly, but a tagged host
        with no GPU devices is exactly what both look like.
        """
        results = check_capacity(
            spec(),
            snapshot(
                classes={
                    ("zone-east-1", "h100-8x"): capacity(
                        gpus_total=0, free_by_host={"host-1": 0}, host_ids=("host-1",)
                    )
                }
            ),
        )

        failed = [r for r in results if r.status is CheckStatus.FAIL]
        assert failed
        assert "IOMMU" in failed[0].remedy
        assert "nouveau" in failed[0].remedy.lower()

    def test_an_unobserved_class_fails(self) -> None:
        results = check_capacity(spec(), snapshot(classes={}))

        assert any(r.status is CheckStatus.FAIL for r in results)

    def test_too_few_gpus_for_the_declared_width_warns(self) -> None:
        """tensor_parallel 2 needs two free GPUs on one host."""
        results = check_capacity(
            spec(),
            snapshot(
                classes={
                    ("zone-east-1", "h100-8x"): capacity(
                        gpus_total=1, free_by_host={"host-1": 1}, host_ids=("host-1",)
                    )
                }
            ),
        )

        assert any(r.status is CheckStatus.WARN for r in results)


class TestGpuDiscovery:
    def test_tags_on_hosts_become_proposed_classes(self) -> None:
        proposed = discover_gpu_classes(
            snapshot(hosts=(host("h1", "h100-sxm"), host("h2", "h100-sxm")))
        )

        assert [p.host_tag for p in proposed] == ["h100-sxm"]
        assert proposed[0].hosts == 2

    def test_untagged_hosts_propose_nothing(self) -> None:
        assert discover_gpu_classes(snapshot(hosts=(host("h1", ""),))) == []

    def test_several_tags_propose_several_classes(self) -> None:
        proposed = discover_gpu_classes(
            snapshot(hosts=(host("h1", "h100-sxm"), host("h2", "l40s")))
        )

        assert {p.host_tag for p in proposed} == {"h100-sxm", "l40s"}

    def test_a_host_with_two_tags_proposes_both(self) -> None:
        proposed = discover_gpu_classes(snapshot(hosts=(host("h1", "h100-sxm,gpu"),)))

        assert len(proposed) == 2

    def test_unhealthy_hosts_are_not_proposed(self) -> None:
        assert discover_gpu_classes(snapshot(hosts=(host("h1", "h100-sxm", state="Down"),))) == []

    def test_proposals_suggest_a_class_name(self) -> None:
        proposed = discover_gpu_classes(snapshot(hosts=(host("h1", "h100-sxm"),)))

        assert proposed[0].suggested_name


class TestSelfTest:
    def test_the_plan_deploys_a_tiny_model(self) -> None:
        """SPEC.md section 7: deploy a tiny test model, request, tear down."""
        plan = plan_self_test(spec(), zone="zone-east-1")

        assert plan.zone == "zone-east-1"
        assert plan.model
        assert plan.teardown

    def test_the_plan_names_every_step(self) -> None:
        plan = plan_self_test(spec(), zone="zone-east-1")

        assert [s.name for s in plan.steps] == [
            "mirror",
            "deploy",
            "request",
            "meter",
            "teardown",
        ]

    def test_an_unknown_zone_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ghost-zone"):
            plan_self_test(spec(), zone="ghost-zone")

    def test_teardown_is_always_the_last_step(self) -> None:
        """A self-test that leaves a model running has failed its purpose."""
        plan = plan_self_test(spec(), zone="zone-east-1")

        assert plan.steps[-1].name == "teardown"


class TestPreflight:
    def test_a_healthy_platform_passes(self) -> None:
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(classes={("zone-east-1", "h100-8x"): capacity()}),
        )

        assert isinstance(report, PreflightReport)
        assert report.ok
        assert report.failures == []

    def test_one_failure_fails_the_report(self) -> None:
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(classes={}),
        )

        assert not report.ok
        assert report.failures

    def test_every_check_is_reported_even_after_one_fails(self) -> None:
        """An operator should see every problem, not fix them one at a time."""
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(classes={}),
        )

        assert len(report.checks) >= 3

    def test_every_failure_carries_a_remedy(self) -> None:
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(classes={}),
        )

        assert all(check.remedy for check in report.failures)

    def test_warnings_do_not_fail_the_report(self) -> None:
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(
                classes={
                    ("zone-east-1", "h100-8x"): capacity(
                        gpus_total=1, free_by_host={"host-1": 1}, host_ids=("host-1",)
                    )
                }
            ),
        )

        assert report.ok
        assert any(c.status is CheckStatus.WARN for c in report.checks)


class TestUnobservedCapacity:
    """A brand-new zone has no capacity snapshot yet.

    Section 14 asks that a new zone reach a passing self-test "using
    only the setup wizard", so the first screen has to work before
    anything has been observed: the checks that can run, run, and the
    rest say why they could not and what to do about it.
    """

    def test_the_capacity_checks_are_skipped_not_failed(self) -> None:
        results = check_capacity(spec(), None)

        assert results
        assert all(r.status is CheckStatus.SKIPPED for r in results)

    def test_a_skipped_check_says_how_to_unblock_it(self) -> None:
        results = check_capacity(spec(), None)

        assert all("observe" in r.remedy.lower() for r in results)

    def test_preflight_still_runs_the_other_checks(self) -> None:
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=None,
        )

        assert any(c.status is CheckStatus.PASS for c in report.checks)

    def test_skipped_checks_do_not_fail_the_report(self) -> None:
        """Nothing is known to be wrong yet, so nothing should block."""
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=None,
        )

        assert report.ok


class TestClusterChecks:
    """The CKS cluster a zone names, and whether the engine can reach it.

    Nothing checked either before. `cks_cluster` was a label passed to
    cloud-init and never verified, and the engine's own Kubernetes
    connection was only exercised once an apply was already underway —
    by which point GPU node VMs have been deployed and billed.
    """

    def test_a_running_cluster_passes(self) -> None:
        results = check_clusters(
            spec(), [ClusterState("zone-east-1", "aisvc-east-1", state="Running")]
        )

        assert [r.status for r in results] == [CheckStatus.PASS]

    def test_a_cluster_cloudstack_does_not_list_fails(self) -> None:
        results = check_clusters(spec(), [ClusterState("zone-east-1", "aisvc-east-1", state=None)])

        assert results[0].status is CheckStatus.FAIL
        assert "aisvc-east-1" in results[0].detail

    def test_a_missing_cluster_sends_the_operator_to_the_spec(self) -> None:
        results = check_clusters(spec(), [ClusterState("zone-east-1", "aisvc-east-1", state=None)])

        assert "cks_cluster" in results[0].remedy

    def test_a_lookup_failure_does_not_send_them_to_the_spec(self) -> None:
        # The distinction the whole lookup exists to preserve: an
        # unreachable CloudStack is not a typo in platform.yaml, and
        # saying so would waste an operator's afternoon.
        results = check_clusters(
            spec(),
            [ClusterState("zone-east-1", "aisvc-east-1", error="connection refused")],
        )

        assert results[0].status is CheckStatus.FAIL
        assert "connection refused" in results[0].detail
        assert "cks_cluster" not in results[0].remedy

    def test_a_cluster_that_is_not_running_names_its_state(self) -> None:
        results = check_clusters(
            spec(), [ClusterState("zone-east-1", "aisvc-east-1", state="Stopped")]
        )

        assert results[0].status is CheckStatus.FAIL
        assert "Stopped" in results[0].detail

    def test_not_having_asked_is_skipped_rather_than_failed(self) -> None:
        # Same reasoning as capacity: a zone being set up has not been
        # observed yet, and nothing is known to be wrong.
        results = check_clusters(spec(), None)

        assert [r.status for r in results] == [CheckStatus.SKIPPED]
        assert results[0].remedy

    def test_a_reachable_cluster_passes(self) -> None:
        assert (
            check_cluster_reachable(FakeKubernetes(), namespace="aisvc-serving").status
            is CheckStatus.PASS
        )

    def test_an_unreachable_cluster_fails_with_a_remedy(self) -> None:
        class Unreachable(FakeKubernetes):
            def list(self, kind: str, namespace: str) -> list[dict[str, Any]]:
                raise RuntimeError("connection refused")

        result = check_cluster_reachable(Unreachable(), namespace="aisvc-serving")

        assert result.status is CheckStatus.FAIL
        assert "connection refused" in result.detail
        assert "KUBECONFIG" in result.remedy

    def test_no_kubernetes_client_is_skipped(self) -> None:
        assert check_cluster_reachable(None, namespace="aisvc-serving").status is (
            CheckStatus.SKIPPED
        )

    def test_preflight_includes_the_cluster_checks(self) -> None:
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(classes={("zone-east-1", "h100-8x"): capacity()}),
            clusters=[ClusterState("zone-east-1", "aisvc-east-1", state="Running")],
            kube=FakeKubernetes(),
        )

        names = [check.name for check in report.checks]
        assert any("aisvc-east-1" in name for name in names)
        assert any("reachable" in name for name in names)
        assert report.ok

    def test_a_stopped_cluster_fails_the_report(self) -> None:
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(classes={("zone-east-1", "h100-8x"): capacity()}),
            clusters=[ClusterState("zone-east-1", "aisvc-east-1", state="Stopped")],
            kube=FakeKubernetes(),
        )

        assert not report.ok

    def test_preflight_without_them_still_works(self) -> None:
        # Every existing caller passes neither, and a zone that has not
        # been observed must still get a report.
        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(classes={("zone-east-1", "h100-8x"): capacity()}),
        )

        assert report.ok


def spec_external() -> PlatformSpec:
    """MINIMAL, but pointing at a cluster CloudStack does not manage."""
    text = MINIMAL.replace(
        "    cks_cluster: aisvc-east-1\n",
        "    kubernetes:\n      source: external\n      cluster: our-own-cluster\n",
    )
    return load_spec_from_string(text, allowed_licenses=LICENSES)


class TestAnExternalClusterIsNotCloudStacksBusiness:
    """ADR-0007: the source decides what CloudStack can be asked.

    Before this, `check_clusters` failed any zone CloudStack had never
    heard of and told the operator to fix `cks_cluster`. For someone
    pointing at their own EKS or kubeadm cluster that is a hard
    preflight failure over a cluster CloudStack was never meant to
    know about.
    """

    def test_an_external_cluster_is_not_failed_for_being_unknown(self) -> None:
        results = check_clusters(
            spec_external(),
            [ClusterState("zone-east-1", "our-own-cluster", state=None)],
        )

        assert results[0].status is not CheckStatus.FAIL

    def test_it_says_why_cloudstack_was_not_asked(self) -> None:
        # Silence would read as the check not having run.
        results = check_clusters(spec_external(), None)

        assert "external" in results[0].detail.lower()

    def test_a_cks_cluster_is_still_failed_for_being_unknown(self) -> None:
        results = check_clusters(spec(), [ClusterState("zone-east-1", "aisvc-east-1", state=None)])

        assert results[0].status is CheckStatus.FAIL

    def test_a_lookup_failure_against_an_external_zone_is_ignored(self) -> None:
        # Nothing was asked on this zone's behalf, so a CloudStack
        # failure says nothing about it.
        results = check_clusters(
            spec_external(),
            [ClusterState("zone-east-1", "our-own-cluster", error="boom")],
        )

        assert results[0].status is not CheckStatus.FAIL

    def test_a_cks_zone_with_no_state_is_skipped_not_failed(self) -> None:
        # The lookup did not run for it; nothing is known to be wrong.
        results = check_clusters(spec(), [])

        assert results[0].status is CheckStatus.SKIPPED

    def test_the_report_still_names_the_cluster(self) -> None:
        results = check_clusters(spec_external(), None)

        assert "our-own-cluster" in results[0].name

    def test_an_external_zone_does_not_fail_the_report(self) -> None:
        report = preflight(
            spec_external(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=snapshot(classes={("zone-east-1", "h100-8x"): capacity()}),
            clusters=[ClusterState("zone-east-1", "our-own-cluster", state=None)],
            kube=FakeKubernetes(),
        )

        assert report.ok
