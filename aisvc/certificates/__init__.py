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
"""Uploaded TLS certificates for private endpoints (SPEC.md section 8).

A custom domain needs a certificate, and the private key that comes
with it is the most sensitive thing a tenant hands this platform. Two
rules run through everything here.

**Check it before nginx does.** A key that does not match its
certificate fails at proxy start with "key values mismatch", hours
after the upload that caused it and with nothing pointing back at it.
Every check in `validate_certificate` exists because its failure mode
is otherwise late, cryptic, or both: a wrong domain is a browser
warning nobody sees until a customer complains, and an encrypted key
simply stops nginx with a passphrase prompt it cannot answer.

**Never echo the key.** Refusals here reach logs, tracebacks and the
tenant's own console, so no error carries key material, and nothing
derived from the key appears in a repr.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

if TYPE_CHECKING:
    pass

__all__ = [
    "CertificateInfo",
    "CertificateRejected",
    "covers_domain",
    "inspect_certificate",
    "validate_certificate",
]


class CertificateRejected(Exception):
    """A certificate or key cannot be accepted.

    `detail` is shown to the tenant, so it never contains key material.
    """

    status_code = 400

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CertificateInfo:
    """What a certificate says about itself.

    Carries nothing derived from the private key: this is shown in
    lists and logged.
    """

    domains: tuple[str, ...]
    not_before: datetime
    not_after: datetime
    fingerprint: str
    subject: str

    @property
    def expired(self) -> bool:
        return self.not_after <= datetime.now(UTC)


def inspect_certificate(cert_pem: str) -> CertificateInfo:
    """Read a PEM certificate, or refuse it."""
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
    except Exception as exc:
        raise CertificateRejected(
            "unreadable_certificate",
            "this does not parse as a PEM certificate",
        ) from exc

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        domains = tuple(san.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        # Pre-SAN certificates put the name in the subject. Browsers
        # stopped honouring that years ago, but reading it gives a
        # better error than "covers no domains".
        domains = tuple(
            attribute.value
            for attribute in cert.subject
            if attribute.oid == x509.oid.NameOID.COMMON_NAME and isinstance(attribute.value, str)
        )

    return CertificateInfo(
        domains=domains,
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
        subject=cert.subject.rfc4514_string(),
    )


def covers_domain(domains: tuple[str, ...], domain: str) -> bool:
    """Whether any of these names covers `domain`.

    A wildcard matches exactly one label, as every browser agrees:
    `*.acme.example` is `ai.acme.example` but neither `acme.example`
    nor `a.b.acme.example`.
    """
    wanted = domain.lower().rstrip(".")
    for name in domains:
        candidate = name.lower().rstrip(".")
        if candidate == wanted:
            return True
        if candidate.startswith("*."):
            suffix = candidate[1:]  # ".acme.example"
            if wanted.endswith(suffix) and "." not in wanted[: -len(suffix)]:
                return True
    return False


def validate_certificate(cert_pem: str, key_pem: str, *, domain: str) -> CertificateInfo:
    """Check an uploaded pair is usable for `domain`, or refuse it."""
    info = inspect_certificate(cert_pem)

    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except TypeError as exc:
        # Raised when the key is encrypted and no password was given.
        raise CertificateRejected(
            "encrypted_key",
            "this private key is encrypted; the proxy has no way to supply a "
            "passphrase, so upload an unencrypted key",
        ) from exc
    except Exception as exc:
        raise CertificateRejected(
            "unreadable_key", "this does not parse as a PEM private key"
        ) from exc

    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    if key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ) != cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ):
        raise CertificateRejected(
            "key_mismatch",
            "this private key does not belong to this certificate; nginx would "
            "refuse to start with it",
        )

    now = datetime.now(UTC)
    if info.not_before > now:
        raise CertificateRejected(
            "not_yet_valid",
            f"this certificate is not valid until {info.not_before.isoformat()}",
        )
    if info.not_after <= now:
        raise CertificateRejected(
            "expired",
            f"this certificate expired on {info.not_after.isoformat()}",
        )

    if domain and not covers_domain(info.domains, domain):
        listed = ", ".join(info.domains) or "no domains"
        raise CertificateRejected(
            "domain_not_covered",
            f"this certificate covers {listed}, not {domain}",
        )

    return info
