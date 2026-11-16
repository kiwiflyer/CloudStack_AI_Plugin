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
"""The kubeconfig, from CloudStack rather than from an operator.

`KUBECONFIG` was a prerequisite an operator satisfied by hand, which
left cluster creation only half automated: the engine could build a CKS
cluster and then could not talk to it until somebody fetched a file.

`getKubernetesClusterConfig` returns that file's data, and the engine
already holds the CloudStack credentials to ask for it.

**The contents are cluster-admin credentials.** They are never logged,
never in a repr, and the file is written private to the user running
the engine.
"""

from __future__ import annotations

import os
import stat
from typing import Any

import pytest

from aisvc.cloudstack.clusters import (
    ClusterUnbuildable,
    KubeconfigUnavailable,
    fetch_kubeconfig,
    write_kubeconfig,
)

CONFIG = "apiVersion: v1\nkind: Config\nusers:\n- name: admin\n  user:\n    token: s3cret\n"


class FakeClient:
    def __init__(
        self,
        clusters: list[dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
        fail: str = "",
    ) -> None:
        self.clusters = (
            [{"id": "k-1", "name": "aisvc-east-1", "state": "Running"}]
            if clusters is None
            else clusters
        )
        self.config = {"configdata": CONFIG} if config is None else config
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_paged(self, command: str, item_key: str, **params: Any) -> list[Any]:
        self.calls.append((command, params))
        return list(self.clusters)

    async def call(self, command: str, **params: Any) -> dict[str, Any]:
        self.calls.append((command, params))
        if self.fail:
            raise RuntimeError(self.fail)
        return dict(self.config)


class TestFetchingIt:
    async def test_it_returns_the_config_data(self) -> None:
        config = await fetch_kubeconfig(FakeClient(), "aisvc-east-1")

        assert "kind: Config" in config

    async def test_it_asks_by_the_clusters_id(self) -> None:
        # The name is what platform.yaml carries; CloudStack wants the
        # id, so the name is resolved first.
        client = FakeClient()

        await fetch_kubeconfig(client, "aisvc-east-1")

        _, params = next(c for c in client.calls if c[0] == "getKubernetesClusterConfig")
        assert params["id"] == "k-1"

    async def test_an_unknown_cluster_is_refused(self) -> None:
        with pytest.raises(KubeconfigUnavailable) as raised:
            await fetch_kubeconfig(FakeClient(clusters=[]), "aisvc-east-1")

        assert "aisvc-east-1" in str(raised.value)

    async def test_an_empty_config_is_refused_rather_than_written(self) -> None:
        # Writing an empty file would turn a clear failure here into an
        # opaque Kubernetes client error later.
        client = FakeClient(config={"configdata": ""})

        with pytest.raises(KubeconfigUnavailable):
            await fetch_kubeconfig(client, "aisvc-east-1")

    async def test_a_call_failure_says_which_cluster(self) -> None:
        with pytest.raises(KubeconfigUnavailable) as raised:
            await fetch_kubeconfig(FakeClient(fail="boom"), "aisvc-east-1")

        assert "aisvc-east-1" in str(raised.value)

    async def test_the_secret_is_not_in_the_error(self) -> None:
        # An error carrying the token would put cluster-admin
        # credentials into logs and tracebacks.
        client = FakeClient(config={"configdata": ""})

        with pytest.raises(KubeconfigUnavailable) as raised:
            await fetch_kubeconfig(client, "aisvc-east-1")

        assert "s3cret" not in str(raised.value)


class TestWritingIt:
    def test_it_writes_the_contents(self, tmp_path: Any) -> None:
        path = write_kubeconfig(CONFIG, directory=str(tmp_path))

        with open(path) as out:
            assert "kind: Config" in out.read()

    def test_the_file_is_private_to_this_user(self, tmp_path: Any) -> None:
        # It holds cluster-admin credentials, and the engine may run
        # on a host with other accounts on it.
        path = write_kubeconfig(CONFIG, directory=str(tmp_path))

        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_rewriting_replaces_rather_than_appends(self, tmp_path: Any) -> None:
        write_kubeconfig(CONFIG, directory=str(tmp_path))
        path = write_kubeconfig("apiVersion: v1\nkind: Config\n", directory=str(tmp_path))

        with open(path) as out:
            assert "s3cret" not in out.read()


class TestChoosingWhichCluster:
    """One Kubernetes connection, so one cluster has to be chosen."""

    def test_one_cks_zone_is_unambiguous(self) -> None:
        from aisvc.cloudstack.clusters import cluster_for_kubeconfig

        assert cluster_for_kubeconfig([("east", "aisvc-east-1")]) == "aisvc-east-1"

    def test_no_cks_zone_has_nothing_to_fetch(self) -> None:
        from aisvc.cloudstack.clusters import cluster_for_kubeconfig

        assert cluster_for_kubeconfig([]) is None

    def test_two_cks_zones_are_ambiguous(self) -> None:
        # The engine holds one Kubernetes connection. Picking one of
        # two silently would deploy every model into whichever zone
        # happened to sort first.
        from aisvc.cloudstack.clusters import cluster_for_kubeconfig

        with pytest.raises(KubeconfigUnavailable) as raised:
            cluster_for_kubeconfig([("east", "aisvc-east-1"), ("west", "aisvc-west-1")])

        assert "KUBECONFIG" in str(raised.value)

    def test_two_zones_naming_one_cluster_is_fine(self) -> None:
        from aisvc.cloudstack.clusters import cluster_for_kubeconfig

        assert cluster_for_kubeconfig([("east", "shared"), ("west", "shared")]) == "shared"


def test_the_exceptions_do_not_catch_each_other() -> None:
    # A kubeconfig that cannot be fetched is not a cluster that cannot
    # be built; they happen at different times with different fixes.
    # Neither may inherit from the other, or an `except` for one would
    # silently swallow the other.
    assert not issubclass(KubeconfigUnavailable, ClusterUnbuildable)
    assert not issubclass(ClusterUnbuildable, KubeconfigUnavailable)


class TestTheDirectoryIsNotTakenAsFound:
    """The kubeconfig holds cluster-admin credentials.

    The path is predictable — `<tmp>/aisvc-<uid>` — so a local user can
    create the directory, and a symlink inside it, in advance. There is
    no race to win. `chmod 0700` narrows the mode but never changes the
    owner, so an attacker-owned directory stays attacker-readable, and
    an `os.open` that follows symlinks writes wherever they point.
    """

    def test_a_symlink_in_place_of_the_file_is_refused(self, tmp_path: Any) -> None:
        import os

        target = tmp_path / "elsewhere"
        target.write_text("do not clobber me")
        directory = tmp_path / "state"
        directory.mkdir(mode=0o700)
        os.symlink(target, directory / "kubeconfig")

        with pytest.raises(KubeconfigUnavailable):
            write_kubeconfig(CONFIG, directory=str(directory))

        assert target.read_text() == "do not clobber me"

    def test_a_directory_owned_by_someone_else_is_refused(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        # Ownership is what `chmod 0700` cannot fix, so it is the thing
        # worth checking.
        directory = tmp_path / "state"
        directory.mkdir(mode=0o700)
        monkeypatch.setattr("os.geteuid", lambda: 999999)

        with pytest.raises(KubeconfigUnavailable) as raised:
            write_kubeconfig(CONFIG, directory=str(directory))

        assert "owned" in str(raised.value).lower()

    def test_a_world_writable_directory_is_refused(self, tmp_path: Any) -> None:
        import os

        directory = tmp_path / "state"
        directory.mkdir(mode=0o777)
        os.chmod(directory, 0o777)

        with pytest.raises(KubeconfigUnavailable):
            write_kubeconfig(CONFIG, directory=str(directory))

    def test_an_ordinary_write_still_works(self, tmp_path: Any) -> None:
        path = write_kubeconfig(CONFIG, directory=str(tmp_path / "fresh"))

        with open(path) as out:
            assert "kind: Config" in out.read()

    def test_rewriting_over_a_real_file_still_works(self, tmp_path: Any) -> None:
        # The second apply of a run rewrites it; only a symlink is a
        # problem, not a file this code wrote itself.
        directory = str(tmp_path / "fresh")
        write_kubeconfig(CONFIG, directory=directory)
        path = write_kubeconfig("apiVersion: v1\nkind: Config\n", directory=directory)

        with open(path) as out:
            assert "s3cret" not in out.read()
