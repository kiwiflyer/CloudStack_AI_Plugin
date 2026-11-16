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
