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
"""How an endpoint is exposed (SPEC.md section 8).

A deployment can be attached to a tenant's own VPC tier through a
per-tenant gateway proxy, and its public address taken away.

This module decides whether a requested exposure is coherent and
permitted. Creating the proxy on the tier is CloudStack work and
belongs in `provision`; what is here is the part that has to be right
before anything is built, because each of these mistakes is expensive
to discover afterwards:

- Turning the public address off with nothing private attached leaves a
  deployment nobody can reach, including the tenant who asked for it.
- A private endpoint is a control a regulated buyer is paying for, so
  "attached" has to mean attached to a network they own, not one they
  named.
- ACME has to reach the host over the internet to answer its challenge.
  Issuing a private-only endpoint an ACME certificate would simply
  fail, at renewal if not at issue, long after anyone was watching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "MAX_HOSTNAME",
    "EndpointExposure",
    "ExposureRejected",
    "TlsSource",
    "describe_exposure",
    "valid_hostname",
    "validate_exposure",
]


class TlsSource(StrEnum):
    """Where a custom domain's certificate comes from."""

    NONE = "none"
    ACME = "acme"
    UPLOADED = "uploaded"


class ExposureRejected(Exception):
    """An exposure cannot be applied, with a reason the tenant can act on."""

    status_code = 400

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class EndpointExposure:
    """How one deployment should be reachable."""

    deployment: str
    account: str
    domain: str
    public_enabled: bool = True
    #: A CloudStack network (VPC tier) id, when attached privately.
    network_id: str = ""
    custom_domain: str = ""
    tls: TlsSource = TlsSource.NONE
    certificate_id: str = ""
    #: The certificate serial this exposure's proxy was built from.
    #: Zero until one has been built. A difference from the
    #: certificate's current serial means the proxy is serving
    #: superseded material (SPEC.md section 8).
    proxy_certificate_serial: int = 0


#: One label of a hostname: alphanumeric, inner hyphens allowed, at
#: most 63 characters.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")

#: The DNS limit. A longer name cannot resolve, and length is the one
#: thing a per-label pattern does not bound.
MAX_HOSTNAME = 253


def valid_hostname(name: str) -> bool:
    """Whether `name` is a hostname and nothing else.

    This is a security boundary, not a formatting nicety. A custom
    domain is interpolated into the proxy's cloud-init — into a
    `runcmd` string entry, which cloud-init runs through a shell, and
    into the nginx `server_name` — so a value carrying `;`, a newline
    or `$(...)` escapes into one or both. The command that sets it is
    granted to every role.

    An allow-list rather than an escape: the set of things that are
    hostnames is small and known, while the set of things that are
    dangerous in a shell and in YAML and in an nginx config is neither.
    """
    if not name or len(name) > MAX_HOSTNAME:
        return False
    return _HOSTNAME.match(name.lower()) is not None


def validate_exposure(exposure: EndpointExposure, *, owned_networks: Sequence[str]) -> bool:
    """Check an exposure is coherent and permitted, or refuse it."""
    if not exposure.public_enabled and not exposure.network_id:
        raise ExposureRejected(
            "unreachable",
            "disabling the public endpoint without attaching a private network "
            "would leave this deployment with no address anyone can reach, "
            "including you",
        )

    if exposure.network_id and exposure.network_id not in owned_networks:
        # One message for "not yours" and "does not exist" alike:
        # telling them apart would enumerate other tenants' networks.
        raise ExposureRejected(
            "network_not_owned",
            f"network {exposure.network_id} is not available to this account",
        )

    if exposure.custom_domain:
        if not valid_hostname(exposure.custom_domain):
            raise ExposureRejected(
                "custom_domain_invalid",
                "custom_domain must be a hostname such as ai.example.com; it is "
                "rendered into the proxy's configuration, so anything else is "
                "refused rather than escaped",
            )

        if exposure.tls is TlsSource.NONE:
            raise ExposureRejected(
                "no_tls_source",
                f"{exposure.custom_domain} needs a certificate; choose ACME or upload one",
            )

        if exposure.tls is TlsSource.UPLOADED and not exposure.certificate_id:
            raise ExposureRejected(
                "no_certificate",
                "an uploaded certificate was chosen but none was given",
            )

        if exposure.tls is TlsSource.ACME and not exposure.public_enabled:
            raise ExposureRejected(
                "acme_needs_public",
                "ACME has to reach this endpoint over the internet to answer its "
                "challenge, and this one is private only; upload a certificate "
                "instead",
            )

    return True


def describe_exposure(exposure: EndpointExposure) -> str:
    """A phrase for the UI and for events.

    Plain words rather than a flag, because "private" is the thing a
    regulated buyer is checking for and it should be legible at a
    glance.
    """
    parts = []
    if exposure.public_enabled:
        parts.append("public")
    if exposure.network_id:
        parts.append(f"private on {exposure.network_id}")

    described = " and ".join(parts) or "unreachable"
    if exposure.custom_domain:
        described = f"{described} as {exposure.custom_domain}"
    return described
