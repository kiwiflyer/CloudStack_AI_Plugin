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

    def test_it_uses_mtls(self) -> None:
        text = source(self.SCRIPT)

        assert "--cert" in text
        assert "--key" in text
        assert "--cacert" in text

    def test_it_fetches_by_id_rather_than_taking_material(self) -> None:
        assert "/v1/certificates/" in source(self.SCRIPT)
        assert "/material" in source(self.SCRIPT)

    def test_it_refuses_to_write_an_empty_key(self) -> None:
        """An empty file makes nginx blame the file rather than the
        fetch that produced it."""
        assert "no %s" in source(self.SCRIPT) or "returned no" in source(self.SCRIPT)

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
