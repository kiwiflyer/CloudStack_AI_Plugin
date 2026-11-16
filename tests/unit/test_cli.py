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
"""Operator CLI (SPEC.md section 4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from aisvc.cli.main import app
from tests.unit.cli_output import plain
from tests.unit.test_spec import MINIMAL

runner = CliRunner()


def write_spec(tmp_path: Path, **model_changes: object) -> Path:
    doc = yaml.safe_load(MINIMAL)
    if model_changes:
        doc["models"][0].update(model_changes)
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def licenses_file(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "licenses.yaml"
    path.write_text(yaml.safe_dump({"allowed": list(names)}), encoding="utf-8")
    return path


class TestValidate:
    def test_valid_spec_exits_zero(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "validate",
                "--spec",
                str(write_spec(tmp_path)),
                "--licenses",
                str(licenses_file(tmp_path, "llama3.3")),
            ],
        )
        assert result.exit_code == 0, plain(result)
        assert "valid" in plain(result).lower()

    def test_invalid_spec_exits_nonzero_and_lists_problems(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "validate",
                "--spec",
                str(write_spec(tmp_path, price_tier="ghost-tier")),
                "--licenses",
                str(licenses_file(tmp_path, "llama3.3")),
            ],
        )
        assert result.exit_code == 1
        assert "ghost-tier" in plain(result)

    def test_missing_file_is_reported_cleanly(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["validate", "--spec", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 1
        assert "nope.yaml" in plain(result)

    def test_warnings_are_shown_without_failing(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "validate",
                "--spec",
                str(write_spec(tmp_path)),
                "--licenses",
                str(licenses_file(tmp_path, "llama3.3")),
            ],
        )
        assert result.exit_code == 0
        assert "max_num_seqs" in plain(result)


class TestPlan:
    def test_plan_without_observing_renders_a_plan(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "plan",
                "--spec",
                str(write_spec(tmp_path)),
                "--licenses",
                str(licenses_file(tmp_path, "llama3.3")),
                "--no-observe",
            ],
        )
        assert result.exit_code == 0, plain(result)
        assert "gpu-node" in plain(result)
        assert "h100-pool" in plain(result)

    def test_plan_makes_no_api_writes(self, tmp_path: Path) -> None:
        """SPEC.md section 12: plan and --dry-run never write."""
        result = runner.invoke(
            app,
            [
                "plan",
                "--spec",
                str(write_spec(tmp_path)),
                "--licenses",
                str(licenses_file(tmp_path, "llama3.3")),
                "--no-observe",
            ],
        )
        assert result.exit_code == 0
        assert "read-only" in plain(result).lower()

    def test_plan_on_an_invalid_spec_aborts_before_observing(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "plan",
                "--spec",
                str(write_spec(tmp_path, node_pool="ghost-pool")),
                "--licenses",
                str(licenses_file(tmp_path, "llama3.3")),
                "--no-observe",
            ],
        )
        assert result.exit_code == 1
        assert "ghost-pool" in plain(result)

    def test_observing_without_credentials_is_a_clear_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "plan",
                "--spec",
                str(write_spec(tmp_path)),
                "--licenses",
                str(licenses_file(tmp_path, "llama3.3")),
            ],
            env={
                "CLOUDSTACK_API_URL": "",
                "CLOUDSTACK_API_KEY": "",
                "CLOUDSTACK_SECRET_KEY": "",
            },
        )
        assert result.exit_code == 1
        assert "CLOUDSTACK_API_URL" in plain(result)


class TestExampleSpec:
    def test_the_shipped_example_validates(self) -> None:
        example = Path(__file__).parents[2] / "examples" / "platform.yaml"
        result = runner.invoke(app, ["validate", "--spec", str(example)])
        assert result.exit_code == 0, plain(result)


class TestWhenThereIsNoKubernetesCluster:
    """The engine reached CloudStack, then fell over on Kubernetes.

    Found by the first run against a real management server: with
    CloudStack configured and no cluster, `plan` printed a clear
    warning that it could not fetch a kubeconfig and then dumped a
    forty-line Python traceback from the Kubernetes client's
    `load_incluster_config`. Missing CloudStack credentials, by
    contrast, produce one sentence naming the variable.

    No unit test caught it because they all use `FakeKubernetes`;
    `ClusterKubernetes.__init__` is marked `pragma: no cover - needs a
    real cluster`, which was exactly true.
    """

    def refuse_a_cluster(self, monkeypatch: Any) -> None:
        from kubernetes.config.config_exception import ConfigException

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise ConfigException("Service host/port is not set.")

        monkeypatch.setattr("aisvc.cli.main.ClusterKubernetes", refuse)
        monkeypatch.delenv("KUBECONFIG", raising=False)

    def with_cloudstack(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CLOUDSTACK_API_URL", "http://localhost:8080/client/api")
        monkeypatch.setenv("CLOUDSTACK_API_KEY", "k")
        monkeypatch.setenv("CLOUDSTACK_SECRET_KEY", "s")

    def plan(self, tmp_path: Path, *extra: str) -> Any:
        return runner.invoke(
            app,
            [
                "plan",
                "--spec",
                str(write_spec(tmp_path)),
                "--licenses",
                str(licenses_file(tmp_path, "llama3.3")),
                *extra,
            ],
        )

    def test_it_says_what_to_set_rather_than_raising(self, monkeypatch: Any, capsys: Any) -> None:
        # `_cluster` directly, not through `plan`: reaching Kubernetes
        # means getting past CloudStack first, and a unit test must not
        # call out to one.
        import typer

        from aisvc.cli import main

        self.refuse_a_cluster(monkeypatch)

        with pytest.raises(typer.Exit) as raised:
            main._cluster()

        assert raised.value.exit_code == 1
        assert "KUBECONFIG" in capsys.readouterr().err

    def test_the_message_offers_the_way_out(self, monkeypatch: Any, capsys: Any) -> None:
        import typer

        from aisvc.cli import main

        self.refuse_a_cluster(monkeypatch)

        with pytest.raises(typer.Exit):
            main._cluster()

        assert "--no-observe" in capsys.readouterr().err

    def test_a_working_cluster_is_not_swallowed(self, monkeypatch: Any) -> None:
        # The guard must only catch a failure to build one, not turn
        # every call into an error.
        from aisvc.cli import main

        built: list[dict[str, Any]] = []

        def record(**kw: Any) -> object:
            built.append(kw)
            return object()

        monkeypatch.setattr("aisvc.cli.main.ClusterKubernetes", record)

        assert main._cluster() is not None
        assert built, "the real client was never constructed"

    def test_no_observe_still_needs_no_cluster(self, tmp_path: Path, monkeypatch: Any) -> None:
        # The documented way out has to keep working.
        self.refuse_a_cluster(monkeypatch)

        result = self.plan(tmp_path, "--no-observe")

        assert result.exit_code == 0, plain(result)
