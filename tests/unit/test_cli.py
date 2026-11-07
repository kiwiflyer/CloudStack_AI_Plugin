"""Operator CLI (SPEC.md section 4)."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from aisvc.cli.main import app
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
        assert result.exit_code == 0, result.output
        assert "valid" in result.output.lower()

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
        assert "ghost-tier" in result.output

    def test_missing_file_is_reported_cleanly(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["validate", "--spec", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 1
        assert "nope.yaml" in result.output

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
        assert "max_num_seqs" in result.output


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
        assert result.exit_code == 0, result.output
        assert "gpu-node" in result.output
        assert "h100-pool" in result.output

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
        assert "read-only" in result.output.lower()

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
        assert "ghost-pool" in result.output

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
        assert "CLOUDSTACK_API_URL" in result.output


class TestExampleSpec:
    def test_the_shipped_example_validates(self) -> None:
        example = Path(__file__).parents[2] / "examples" / "platform.yaml"
        result = runner.invoke(app, ["validate", "--spec", str(example)])
        assert result.exit_code == 0, result.output
