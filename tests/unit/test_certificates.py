"""Uploaded TLS certificates (SPEC.md section 8).

A private endpoint with a custom domain needs a certificate, and the
private key that comes with it is the most sensitive thing a tenant
hands us. Two concerns run through all of this:

- **Check it before nginx does.** A key that does not match its
  certificate fails at proxy start with an error an operator cannot
  read, hours after the upload that caused it.
- **Never show it again.** The material is served once, to the proxy,
  over mTLS. It is not in a list, not in an error, not in a repr.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aisvc.certificates import (
    CertificateRejected,
    inspect_certificate,
    validate_certificate,
)


def make_cert(
    *,
    domains: tuple[str, ...] = ("ai.acme.example",),
    not_before: dt.datetime | None = None,
    not_after: dt.datetime | None = None,
) -> tuple[str, str]:
    """A self-signed certificate and its key, as PEM."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - dt.timedelta(days=1))
        .not_valid_after(not_after or now + dt.timedelta(days=90))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


class TestInspecting:
    def test_the_domains_are_read(self) -> None:
        cert, _ = make_cert(domains=("ai.acme.example", "api.acme.example"))

        info = inspect_certificate(cert)

        assert set(info.domains) == {"ai.acme.example", "api.acme.example"}

    def test_the_expiry_is_read(self) -> None:
        cert, _ = make_cert()

        assert inspect_certificate(cert).not_after > dt.datetime.now(dt.UTC)

    def test_a_fingerprint_identifies_it(self) -> None:
        """So an operator can tell two uploads apart without the key."""
        cert, _ = make_cert()
        other, _ = make_cert()

        assert inspect_certificate(cert).fingerprint != inspect_certificate(other).fingerprint

    def test_rubbish_is_refused(self) -> None:
        with pytest.raises(CertificateRejected) as caught:
            inspect_certificate("not a certificate")

        assert caught.value.reason == "unreadable_certificate"


class TestValidating:
    def test_a_matching_pair_for_its_domain_passes(self) -> None:
        cert, key = make_cert()

        assert validate_certificate(cert, key, domain="ai.acme.example")

    def test_a_key_that_does_not_match_is_refused(self) -> None:
        """nginx reports this at start-up, hours later, as "key values
        mismatch" — which tells an operator nothing about the upload."""
        cert, _ = make_cert()
        _, other_key = make_cert()

        with pytest.raises(CertificateRejected) as caught:
            validate_certificate(cert, other_key, domain="ai.acme.example")

        assert caught.value.reason == "key_mismatch"

    def test_a_certificate_for_another_domain_is_refused(self) -> None:
        cert, key = make_cert(domains=("something-else.example",))

        with pytest.raises(CertificateRejected) as caught:
            validate_certificate(cert, key, domain="ai.acme.example")

        assert caught.value.reason == "domain_not_covered"

    def test_a_wildcard_covers_its_subdomains(self) -> None:
        cert, key = make_cert(domains=("*.acme.example",))

        assert validate_certificate(cert, key, domain="ai.acme.example")

    def test_a_wildcard_does_not_cover_the_apex(self) -> None:
        """*.acme.example is not acme.example, as every browser agrees."""
        cert, key = make_cert(domains=("*.acme.example",))

        with pytest.raises(CertificateRejected):
            validate_certificate(cert, key, domain="acme.example")

    def test_a_wildcard_does_not_cover_two_levels(self) -> None:
        cert, key = make_cert(domains=("*.acme.example",))

        with pytest.raises(CertificateRejected):
            validate_certificate(cert, key, domain="a.b.acme.example")

    def test_an_expired_certificate_is_refused(self) -> None:
        now = dt.datetime.now(dt.UTC)
        cert, key = make_cert(
            not_before=now - dt.timedelta(days=10),
            not_after=now - dt.timedelta(days=1),
        )

        with pytest.raises(CertificateRejected) as caught:
            validate_certificate(cert, key, domain="ai.acme.example")

        assert caught.value.reason == "expired"

    def test_one_not_yet_valid_is_refused(self) -> None:
        now = dt.datetime.now(dt.UTC)
        cert, key = make_cert(
            not_before=now + dt.timedelta(days=1),
            not_after=now + dt.timedelta(days=90),
        )

        with pytest.raises(CertificateRejected) as caught:
            validate_certificate(cert, key, domain="ai.acme.example")

        assert caught.value.reason == "not_yet_valid"

    def test_an_unreadable_key_is_refused(self) -> None:
        cert, _ = make_cert()

        with pytest.raises(CertificateRejected) as caught:
            validate_certificate(cert, "not a key", domain="ai.acme.example")

        assert caught.value.reason == "unreadable_key"

    def test_an_encrypted_key_is_refused_with_a_clear_reason(self) -> None:
        """nginx cannot use one without a passphrase it has no way to
        get, so this has to be refused at upload rather than at boot."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        cert, _ = make_cert()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        encrypted = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"hunter2"),
        ).decode()

        with pytest.raises(CertificateRejected) as caught:
            validate_certificate(cert, encrypted, domain="ai.acme.example")

        assert caught.value.reason == "encrypted_key"


class TestNothingLeaks:
    def test_a_refusal_does_not_echo_the_key(self) -> None:
        """These end up in logs, tracebacks and the tenant's console."""
        cert, _ = make_cert()
        _, other_key = make_cert()

        with pytest.raises(CertificateRejected) as caught:
            validate_certificate(cert, other_key, domain="ai.acme.example")

        assert "PRIVATE KEY" not in caught.value.detail
        assert "PRIVATE KEY" not in str(caught.value)

    def test_an_unreadable_key_refusal_does_not_echo_it_either(self) -> None:
        cert, _ = make_cert()

        with pytest.raises(CertificateRejected) as caught:
            validate_certificate(cert, "-----BEGIN PRIVATE KEY-----\nxx", domain="ai.acme.example")

        assert "xx" not in caught.value.detail

    def test_the_inspection_carries_no_key_material(self) -> None:
        cert, key = make_cert()

        info = inspect_certificate(cert)

        assert "PRIVATE" not in repr(info)
        assert key not in repr(info)
