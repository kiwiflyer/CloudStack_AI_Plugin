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
"""Preflight for the GPU device plugin (SPEC.md section 7).

Kubernetes does not know a GPU exists until a device plugin advertises
it as an extended resource, and nothing in this project installs one.
Without it the engine's reconcile succeeds, the Deployment exists, and
every pod sits Pending for ever — the reason visible only in `kubectl
describe pod`. This check is what turns that into a named failure
carrying its remedy.

Two states matter as much as the failure. A cluster whose GPU nodes
have not joined yet must not be called broken: that is the normal state
of a fresh install, and a preflight that cries wolf there is one an
operator learns to ignore. And a cluster this engine cannot read must
not be called broken either — the plugin may be perfectly fine.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.setup import CheckStatus, check_gpu_device_plugin, preflight
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_amd_gpus import AMD_SPEC
from tests.unit.test_spec import LICENSES, MINIMAL


def spec(text: str = MINIMAL) -> PlatformSpec:
    return load_spec_from_string(text, allowed_licenses=LICENSES)


class FakeNodes:
    """Just enough of the Kubernetes protocol to answer for Nodes."""

    def __init__(self, nodes: list[dict[str, Any]] | None = None, fail: str = "") -> None:
        self.nodes = nodes or []
        self.fail = fail
        self.asked: list[str] = []

    def list(self, kind: str, namespace: str) -> list[dict[str, Any]]:
        self.asked.append(kind)
        if self.fail:
            raise RuntimeError(self.fail)
        return self.nodes if kind == "Node" else []

    def apply(
        self,
        kind: str,
        namespace: str,
        obj: dict[str, Any],
        *,
        field_manager: str,
        force: bool,
    ) -> None:
        raise AssertionError("a preflight check must not write to the cluster")

    def delete(self, kind: str, namespace: str, name: str) -> None:
        raise AssertionError("a preflight check must not write to the cluster")


def node(*, gpu_class: str | None = "h100-8x", advertises: dict[str, str] | None = None) -> Any:
    labels = {"aisvc.io/gpu-class": gpu_class} if gpu_class else {}
    return {
        "metadata": {"name": "aisvc-h100-pool-0", "labels": labels},
        "status": {"allocatable": {"cpu": "32", "memory": "256Gi", **(advertises or {})}},
    }


def only(results: list[Any]) -> Any:
    assert len(results) == 1, f"expected one check, got {[r.name for r in results]}"
    return results[0]


class TestItReadsTheClusterCarefully:
    def test_without_a_kubernetes_client_it_skips(self) -> None:
        result = only(check_gpu_device_plugin(spec(), None))
        assert result.status is CheckStatus.SKIPPED
        assert "KUBECONFIG" in result.remedy

    def test_a_cluster_it_cannot_read_is_not_called_broken(self) -> None:
        """The plugin may be fine; an unreachable API says nothing about it."""
        result = only(check_gpu_device_plugin(spec(), FakeNodes(fail="connection refused")))
        assert result.status is CheckStatus.WARN
        assert "connection refused" in result.detail

    def test_it_asks_for_nodes(self) -> None:
        kube = FakeNodes()
        check_gpu_device_plugin(spec(), kube)
        assert "Node" in kube.asked


class TestTheThreeAnswersAboutAVendor:
    def test_a_node_advertising_the_resource_passes(self) -> None:
        kube = FakeNodes([node(advertises={"nvidia.com/gpu": "8"})])
        result = only(check_gpu_device_plugin(spec(), kube))
        assert result.status is CheckStatus.PASS
        assert "nvidia.com/gpu" in result.detail

    def test_a_gpu_node_that_advertises_nothing_fails_with_the_remedy(self) -> None:
        kube = FakeNodes([node(advertises=None)])
        result = only(check_gpu_device_plugin(spec(), kube))
        assert result.status is CheckStatus.FAIL
        assert "nvidia.com/gpu" in result.detail
        assert "device plugin" in result.remedy.lower()

    def test_no_gpu_node_yet_is_a_warning_not_a_failure(self) -> None:
        """The normal state of a fresh install, before any node is
        provisioned. Failing here teaches operators to ignore preflight."""
        result = only(check_gpu_device_plugin(spec(), FakeNodes([])))
        assert result.status is CheckStatus.WARN
        assert "no GPU node" in result.detail


class TestItFollowsTheVendor:
    def test_an_amd_class_wants_the_amd_resource(self) -> None:
        kube = FakeNodes([node(advertises={"amd.com/gpu": "8"})])
        result = only(check_gpu_device_plugin(spec(AMD_SPEC), kube))
        assert result.status is CheckStatus.PASS
        assert "amd.com/gpu" in result.detail

    def test_the_wrong_vendors_plugin_fails_like_a_missing_one(self) -> None:
        """The mode to watch for on a mixed fleet: a plugin is installed,
        the node advertises a resource, and it is the other vendor's."""
        kube = FakeNodes([node(advertises={"nvidia.com/gpu": "8"})])
        result = only(check_gpu_device_plugin(spec(AMD_SPEC), kube))
        assert result.status is CheckStatus.FAIL
        assert "amd.com/gpu" in result.detail

    def test_one_check_per_distinct_vendor_not_per_class(self) -> None:
        """The plugin is installed per cluster, not per GPU class."""
        two_classes = MINIMAL.replace(
            "    gpu_mem_gb: 80\n    mode: passthrough\n",
            "    gpu_mem_gb: 80\n    mode: passthrough\n"
            "  - name: mi300x-8x\n"
            "    host_tag: mi300x\n"
            '    service_offering: "GPU-MI300X-8x"\n'
            "    gpus_per_node: 8\n"
            "    gpu_mem_gb: 192\n"
            "    mode: passthrough\n"
            "    vendor: amd\n",
        )
        results = check_gpu_device_plugin(spec(two_classes), FakeNodes([]))
        assert len(results) == 2
        assert {r.name for r in results} == {
            "GPU device plugin for nvidia",
            "GPU device plugin for amd",
        }


class TestItIsPartOfPreflight:
    def test_preflight_runs_it(self) -> None:
        from aisvc.registry import InMemoryObjectStore, ModelRegistry

        report = preflight(
            spec(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            snapshot=None,
            kube=FakeNodes([node(advertises={"nvidia.com/gpu": "8"})]),
        )
        names = [c.name for c in report.checks]
        assert "GPU device plugin for nvidia" in names

    @pytest.mark.parametrize("status", [CheckStatus.FAIL, CheckStatus.WARN])
    def test_every_non_passing_answer_carries_a_remedy(self, status: CheckStatus) -> None:
        cases = {
            CheckStatus.FAIL: FakeNodes([node(advertises=None)]),
            CheckStatus.WARN: FakeNodes([]),
        }
        result = only(check_gpu_device_plugin(spec(), cases[status]))
        assert result.status is status
        assert result.remedy


class TestTheFakeClusterCanAnswerForNodes:
    """`FakeKubernetes` used to stamp `readyReplicas` onto every listed
    object's status, which would have wiped the allocatable resources
    this check reads — a test double agreeing with a bug, the way
    `refusal()` once did."""

    def test_a_listed_node_keeps_its_allocatable(self) -> None:
        from aisvc.k8s.client import FakeKubernetes

        kube = FakeKubernetes()
        kube.objects[("Node", "", "n1")] = node(advertises={"nvidia.com/gpu": "8"})
        listed = kube.list("Node", "")
        assert listed[0]["status"]["allocatable"]["nvidia.com/gpu"] == "8"

    def test_the_check_passes_against_it(self) -> None:
        from aisvc.k8s.client import FakeKubernetes

        kube = FakeKubernetes()
        kube.objects[("Node", "", "n1")] = node(advertises={"nvidia.com/gpu": "8"})
        assert only(check_gpu_device_plugin(spec(), kube)).status is CheckStatus.PASS
