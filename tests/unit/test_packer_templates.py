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
"""The Packer templates (SPEC.md sections 7, 8 and 13).

`packer validate` is not run here — it needs the binary and the
CloudStack plugin — so these check the things that break a build
without it: a provisioner pointing at a file nobody committed, a
template that no longer declares the variable the spec passes it, and
braces that do not balance.

Weaker than a real validate, and it catches the mistakes that have
actually happened.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKER = Path(__file__).resolve().parents[2] / "templates" / "packer"
TEMPLATES = sorted(PACKER.glob("*.pkr.hcl"))


def source(path: Path) -> str:
    return path.read_text()


class TestEveryTemplate:
    def test_there_are_some(self) -> None:
        assert TEMPLATES

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_braces_balance(self, path: Path) -> None:
        text = re.sub(r"//[^\n]*", "", source(path))
        assert text.count("{") == text.count("}")

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_it_declares_a_build(self, path: Path) -> None:
        assert re.search(r"^build\s*\{", source(path), re.M)

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_it_requires_the_cloudstack_plugin(self, path: Path) -> None:
        assert "github.com/hashicorp/cloudstack" in source(path)

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_credentials_are_marked_sensitive(self, path: Path) -> None:
        """Or Packer prints them in its build log."""
        text = source(path)
        for name in ("cloudstack_api_key", "cloudstack_secret_key"):
            block = re.search(rf'variable "{name}" \{{(.*?)\}}', text, re.S)
            assert block, f"{path.name} does not declare {name}"
            assert "sensitive = true" in block.group(1)

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_it_takes_an_image_version(self, path: Path) -> None:
        """`template` and `proxy_template` in platform.yaml name one."""
        assert 'variable "image_version"' in source(path)

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_every_provisioned_file_exists(self, path: Path) -> None:
        """A file provisioner pointing at nothing fails the build long
        after it started, with the upload as the error."""
        # Only `provisioner "file"` blocks; `source` also names the
        # plugin registry and the build's own source block.
        for block in re.findall(r'provisioner\s+"file"\s*\{(.*?)\}', source(path), re.S):
            for reference in re.findall(r'source\s*=\s*"([^"]+)"', block):
                assert (PACKER / reference).exists(), f"{path.name}: {reference} is missing"

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_the_template_is_not_published(self, path: Path) -> None:
        """A public template is visible to every account in the zone."""
        assert "template_public       = false" in source(path)


class TestTheProxyTemplate:
    PROXY = PACKER / "proxy.pkr.hcl"

    def test_it_exists(self) -> None:
        """zones[].proxy_template names an image something has to build."""
        assert self.PROXY.exists()

    def test_it_installs_nginx(self) -> None:
        assert "nginx" in source(self.PROXY)

    def test_it_bakes_in_the_certificate_fetcher(self) -> None:
        """It cannot arrive by cloud-init: userdata is readable through
        the CloudStack API, which is the reason the fetcher exists."""
        assert "aisvc-fetch-cert" in source(self.PROXY)
        assert (PACKER / "files" / "aisvc-fetch-cert").exists()

    def test_the_fetcher_is_executable(self) -> None:
        import os

        assert os.access(PACKER / "files" / "aisvc-fetch-cert", os.X_OK)

    def test_certbot_is_baked_in(self) -> None:
        """So an ACME endpoint does not wait on a package download at
        first boot."""
        assert "certbot" in source(self.PROXY)

    def test_the_default_site_is_removed(self) -> None:
        """Otherwise nginx answers on the tenant's network with its own
        welcome page."""
        assert "sites-enabled/default" in source(self.PROXY)

    def test_it_records_what_image_it_is(self) -> None:
        assert "image.json" in source(self.PROXY)


class TestTheCertificateFetcher:
    SCRIPT = PACKER / "files" / "aisvc-fetch-cert"

    def test_it_redeems_a_token(self) -> None:
        """Not mTLS: the client key would have to travel in the same
        readable userdata the material is kept out of."""
        text = source(self.SCRIPT)

        assert "--token" in text
        assert "/v1/certificates/bootstrap" in text

    def test_the_token_goes_in_the_body_not_the_url(self) -> None:
        """Paths reach access logs and proxies; bodies do not."""
        text = source(self.SCRIPT)

        assert "--data" in text
        assert "bootstrap?" not in text

    def test_it_fails_on_an_http_error(self) -> None:
        """Without --fail, curl writes the error page out as a
        certificate and nginx blames the file."""
        assert "--fail" in source(self.SCRIPT)

    def test_it_says_what_to_do_when_a_token_is_spent(self) -> None:
        assert "redeploy" in source(self.SCRIPT).lower()

    def test_it_writes_both_files_or_neither(self) -> None:
        """A half-written pair makes nginx blame the file rather than
        the fetch, and by then the token is spent."""
        assert "returned no" in source(self.SCRIPT)

    def test_the_key_is_written_private(self) -> None:
        assert "0o600" in source(self.SCRIPT)
        assert "umask 077" in source(self.SCRIPT)

    def test_it_stops_on_the_first_error(self) -> None:
        assert "set -euo pipefail" in source(self.SCRIPT)

    def test_it_fails_rather_than_guessing_an_engine(self) -> None:
        assert "no engine URL" in source(self.SCRIPT)


class TestPackerItself:
    """The real thing, when the binary is there.

    Skipped otherwise rather than failing, as the Terraform and
    Playwright acceptance tests are: a contributor without Packer
    should still get a green suite, and the structural checks above
    still run.

    `packer init` is what caught the version constraint that could
    never resolve, so it is part of this rather than assumed.
    """

    @staticmethod
    def packer() -> str | None:
        import shutil

        return shutil.which("packer")

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_the_plugin_constraint_resolves(self, path: Path) -> None:
        """A constraint matching no release refuses the template
        outright, which is how `>= 1.1.0` sat here unbuildable."""
        import subprocess

        binary = self.packer()
        if binary is None:
            pytest.skip("packer is not installed")

        result = subprocess.run(
            [binary, "init", path.name],
            cwd=PACKER,
            capture_output=True,
            text=True,
            timeout=300,
        )

        # The message goes to stdout, not stderr, and `packer validate`
        # on its own does not catch this at all — it prints "Did you
        # run packer init?" and carries on. So this asserts on init's
        # own exit code and its whole output.
        output = (result.stdout + result.stderr).strip()
        assert result.returncode == 0, output
        assert "no matching version" not in output, output

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_packer_validates_it(self, path: Path) -> None:
        import subprocess

        binary = self.packer()
        if binary is None:
            pytest.skip("packer is not installed")

        result = subprocess.run(
            [
                binary,
                "validate",
                # Values that only have to be well formed: validate
                # does not reach CloudStack.
                "-var",
                "image_version=validate-check",
                "-var",
                "cloudstack_api_url=https://cloudstack.invalid/client/api",
                "-var",
                "cloudstack_api_key=validate-only-key",
                "-var",
                "cloudstack_secret_key=validate-only-secret",
                "-var",
                "zone=validate-zone",
                "-var",
                "network=validate-network",
                path.name,
            ],
            cwd=PACKER,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 0, (result.stdout + result.stderr).strip()
