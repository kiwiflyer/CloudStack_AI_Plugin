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
"""Where a zone's Kubernetes cluster comes from (ADR-0007).

A zone used to name a `cks_cluster` and nothing more, which assumed
every cluster was one CloudStack manages. `addNodesToKubernetesCluster`
only works on such a cluster, so that assumption was about to become
load-bearing; an operator pointing at their own EKS or kubeadm cluster
had nowhere to say so.

A zone now declares a source. The source decides what CloudStack can be
asked about the cluster — never the other way round.
"""

from __future__ import annotations

import pytest

from aisvc.spec import SpecError, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL


def load(text: str):  # type: ignore[no-untyped-def]
    return load_spec_from_string(text, allowed_licenses=LICENSES)


def zone_of(text: str):  # type: ignore[no-untyped-def]
    return load(text).document.zones[0]


def with_zone(*lines: str) -> str:
    """MINIMAL with its single zone replaced by `lines`.

    Everything but the zone is kept, so these exercise the zone block
    against a document that is otherwise known to validate.
    """
    _, _, tail = MINIMAL.partition("gpuClasses:\n")
    zone = "".join(f"{line}\n" for line in lines)
    return "apiVersion: aisvc/v1\nzones:\n" + zone + "gpuClasses:\n" + tail


EAST = ("  - name: zone-east-1", '    cloudstack_zone: "East-1"')


class TestTheOldSpellingStillWorks:
    def test_a_bare_cks_cluster_loads(self) -> None:
        # Every spec written so far says only this, and must keep
        # meaning exactly what it meant.
        assert zone_of(MINIMAL).cks_cluster == "aisvc-east-1"

    def test_it_reads_as_a_cks_cluster_the_engine_may_not_create(self) -> None:
        kubernetes = zone_of(MINIMAL).kubernetes

        assert kubernetes.source == "cks"
        assert kubernetes.cluster == "aisvc-east-1"
        assert kubernetes.create is False


class TestTheExplicitBlock:
    def test_an_external_cluster_can_be_named(self) -> None:
        zone = zone_of(
            with_zone(
                *EAST,
                "    kubernetes:",
                "      source: external",
                "      cluster: our-own-cluster",
            )
        )

        assert zone.kubernetes.source == "external"
        assert zone.kubernetes.cluster == "our-own-cluster"

    def test_cks_cluster_still_reports_the_name(self) -> None:
        # Four call sites read zone.cks_cluster. The block is extra
        # information, not a second place to look for the name.
        zone = zone_of(
            with_zone(
                "      - name: zone-east-1\n"
                '        cloudstack_zone: "East-1"\n'
                "        kubernetes:\n"
                "          source: external\n"
                "          cluster: our-own-cluster\n"
            )
        )

        assert zone.cks_cluster == "our-own-cluster"

    def test_creation_can_be_asked_for(self) -> None:
        zone = zone_of(
            with_zone(
                *EAST,
                "    kubernetes:",
                "      source: cks",
                "      cluster: aisvc-east-1",
                "      create: true",
            )
        )

        assert zone.kubernetes.create is True


class TestWhatIsRefused:
    def test_an_external_cluster_cannot_be_created(self) -> None:
        # The engine can only create a cluster through CKS. Asking it
        # to create one it does not manage describes nothing it could
        # do, so it is refused at load rather than ignored.
        with pytest.raises(SpecError) as raised:
            load(
                with_zone(
                    *EAST,
                    "    kubernetes:",
                    "      source: external",
                    "      cluster: our-own-cluster",
                    "      create: true",
                )
            )

        assert "create" in str(raised.value)

    def test_a_zone_must_name_a_cluster_somehow(self) -> None:
        with pytest.raises(SpecError):
            load(with_zone(*EAST))

    def test_the_two_spellings_may_not_disagree(self) -> None:
        # Silently preferring one would make the other a lie that an
        # operator keeps reading.
        with pytest.raises(SpecError) as raised:
            load(
                with_zone(
                    *EAST,
                    "    cks_cluster: aisvc-east-1",
                    "    kubernetes:",
                    "      source: cks",
                    "      cluster: somewhere-else",
                )
            )

        assert "somewhere-else" in str(raised.value)

    def test_saying_the_same_thing_twice_is_allowed(self) -> None:
        zone = zone_of(
            with_zone(
                *EAST,
                "    cks_cluster: aisvc-east-1",
                "    kubernetes:",
                "      source: cks",
                "      cluster: aisvc-east-1",
            )
        )

        assert zone.cks_cluster == "aisvc-east-1"

    def test_an_unknown_source_is_refused(self) -> None:
        with pytest.raises(SpecError):
            load(
                with_zone(
                    *EAST,
                    "    kubernetes:",
                    "      source: eks",
                    "      cluster: our-own-cluster",
                )
            )


class TestWhatCreationNeeds:
    """A zone that asks for a cluster may also say how to build it.

    Everything here is optional. Leaving it all unset is the point —
    the engine resolves the newest enabled Kubernetes version and the
    smallest offering that clears its minimums, and names both in the
    plan before anything is created.
    """

    def test_everything_defaults(self) -> None:
        kubernetes = zone_of(
            with_zone(
                *EAST,
                "    kubernetes:",
                "      source: cks",
                "      cluster: aisvc-east-1",
                "      create: true",
            )
        ).kubernetes

        assert kubernetes.version == ""
        assert kubernetes.offering == ""
        assert kubernetes.size == 1
        assert kubernetes.network == ""

    def test_a_version_and_offering_can_be_pinned(self) -> None:
        kubernetes = zone_of(
            with_zone(
                *EAST,
                "    kubernetes:",
                "      source: cks",
                "      cluster: aisvc-east-1",
                "      create: true",
                "      version: 1.29.4",
                "      offering: Medium Instance",
                "      size: 3",
                "      network: tier-a",
            )
        ).kubernetes

        assert kubernetes.version == "1.29.4"
        assert kubernetes.offering == "Medium Instance"
        assert kubernetes.size == 3
        assert kubernetes.network == "tier-a"

    def test_a_cluster_needs_at_least_one_worker(self) -> None:
        # The gateway and the system pods run on these. A cluster with
        # none would come up with nowhere to put them.
        with pytest.raises(SpecError):
            load(
                with_zone(
                    *EAST,
                    "    kubernetes:",
                    "      source: cks",
                    "      cluster: aisvc-east-1",
                    "      create: true",
                    "      size: 0",
                )
            )

    def test_build_settings_without_create_are_refused(self) -> None:
        # Pinning a version on a cluster the engine will never build
        # describes nothing, and reads as though it would be applied
        # to the existing one.
        with pytest.raises(SpecError) as raised:
            load(
                with_zone(
                    *EAST,
                    "    kubernetes:",
                    "      source: cks",
                    "      cluster: aisvc-east-1",
                    "      version: 1.29.4",
                )
            )

        assert "create" in str(raised.value)
