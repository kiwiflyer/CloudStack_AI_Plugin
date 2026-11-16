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
"""`aisvc apply` (SPEC.md sections 4 and 13)."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from aisvc.cli.main import app
from tests.unit.test_spec import MINIMAL

runner = CliRunner()


def write_spec(tmp_path: Path) -> Path:
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(yaml.safe_load(MINIMAL)), encoding="utf-8")
    return path


def licenses_file(tmp_path: Path) -> Path:
    path = tmp_path / "licenses.yaml"
    path.write_text(yaml.safe_dump({"allowed": ["llama3.3"]}), encoding="utf-8")
    return path


def base_args(tmp_path: Path) -> list[str]:
    return [
        "--spec",
        str(write_spec(tmp_path)),
        "--licenses",
        str(licenses_file(tmp_path)),
    ]


CREDS = {
    "CLOUDSTACK_API_URL": "https://cloud.example.com/client/api",
    "CLOUDSTACK_API_KEY": "k",
    "CLOUDSTACK_SECRET_KEY": "s",
    "AISVC_DATABASE_URL": "sqlite+pysqlite:///:memory:",
    "AISVC_CKS_JOIN_ENDPOINT": "10.0.0.10:6443",
    "AISVC_CKS_JOIN_TOKEN": "abcdef.0123456789abcdef",
    "AISVC_CKS_CA_CERT_HASH": "sha256:deadbeef",
}


class TestGuards:
    def test_apply_on_an_invalid_spec_aborts(self, tmp_path: Path) -> None:
        path = tmp_path / "platform.yaml"
        doc = yaml.safe_load(MINIMAL)
        doc["models"][0]["price_tier"] = "ghost-tier"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")

        result = runner.invoke(
            app,
            ["apply", "--spec", str(path), "--licenses", str(licenses_file(tmp_path))],
            env=CREDS,
        )

        assert result.exit_code == 1
        assert "ghost-tier" in result.output

    def test_apply_without_credentials_is_a_clear_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["apply", *base_args(tmp_path)],
            env={**CREDS, "CLOUDSTACK_API_URL": ""},
        )

        assert result.exit_code == 1
        assert "CLOUDSTACK_API_URL" in result.output

    def test_apply_without_cluster_join_details_is_a_clear_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["apply", *base_args(tmp_path)],
            env={**CREDS, "AISVC_CKS_JOIN_TOKEN": ""},
        )

        assert result.exit_code == 1
        assert "AISVC_CKS_JOIN_TOKEN" in result.output

    def test_apply_requires_confirmation_without_yes(self, tmp_path: Path) -> None:
        """A write that reaches CloudStack should not happen by accident."""
        result = runner.invoke(
            app,
            ["apply", *base_args(tmp_path), "--no-observe"],
            env=CREDS,
            input="n\n",
        )

        assert result.exit_code == 1
        assert "aborted" in result.output.lower()

    def test_declining_contacts_cloudstack_for_nothing(self, tmp_path: Path) -> None:
        """Answering no must abort before any write is attempted."""
        result = runner.invoke(
            app,
            ["apply", *base_args(tmp_path), "--no-observe"],
            env=CREDS,
            input="n\n",
        )

        assert "h100-pool" in result.output
        assert "Run " not in result.output

    def test_the_plan_is_shown_before_the_prompt(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["apply", *base_args(tmp_path), "--no-observe"],
            env=CREDS,
            input="n\n",
        )

        assert result.output.index("Planned actions") < result.output.index("Apply these")


class TestHelp:
    def test_apply_is_listed(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert "apply" in result.output

    def test_apply_help_mentions_dry_run(self) -> None:
        result = runner.invoke(app, ["apply", "--help"])
        assert "--dry-run" in result.output
