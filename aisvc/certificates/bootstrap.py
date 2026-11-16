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
"""One-time bootstrap tokens for gateway proxies (SPEC.md section 8).

A proxy needs its TLS material at first boot and has no identity yet.
mTLS cannot solve that: the client key would have to travel in
cloud-init userdata, which is readable through the CloudStack API —
precisely the problem the fetch exists to avoid. A key there would be
worse than the key it protects, because it would open every
certificate rather than one.

So the proxy carries a token. **The trade is deliberate and worth
stating plainly:** a token in userdata is still a credential in a
readable place. What makes it acceptable is that it is short-lived,
single-use, and bound to one certificate. A userdata document read
tomorrow yields something already spent and useless for anything else;
a private key read tomorrow is a private key.

Every property below exists to hold that trade up, and the first two
are the load-bearing ones:

- **Single use.** Redeeming burns it, in the same transaction that
  returns the material.
- **Bound to one certificate.** A stolen token is worth that one
  certificate, never the platform's.
- Short TTL, long enough to boot and fetch.
- Stored as a salted hash, so a database read yields nothing usable.
- Never echoed in an error: refusals reach logs and console output.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from aisvc.db.models import BootstrapTokenRecord, CertificateRecord, utcnow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "BOOTSTRAP_TTL",
    "TOKEN_PREFIX",
    "BootstrapRejected",
    "BootstrapTokens",
    "CertificateMaterial",
    "MintedToken",
    "mint_bootstrap_token",
]

log = logging.getLogger("aisvc.certificates.bootstrap")

#: Long enough for a VM to boot, install and fetch; no longer. The
#: window is the point of using a token rather than a key.
BOOTSTRAP_TTL = timedelta(minutes=30)

#: Marks these in logs without revealing anything.
TOKEN_PREFIX = "aisvc-boot"

#: 256 bits. Nothing rate-limits redemption, so the token space is the
#: only thing between an attacker and one certificate.
SECRET_BYTES = 32


class BootstrapRejected(Exception):
    """A token may not be redeemed, with a reason that is safe to log."""

    status_code = 403

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A freshly minted token. Its plaintext exists only here."""

    token_id: str
    secret: str
    salt: str
    hashed: str
    expires_at: datetime

    @property
    def plaintext(self) -> str:
        """What goes into the proxy's userdata."""
        return f"{TOKEN_PREFIX}-{self.token_id}-{self.secret}"


@dataclass(frozen=True)
class CertificateMaterial:
    """What a redeemed token buys. Never logged, never repr'd."""

    certificate_id: str
    certificate: str
    private_key: str = ""

    def __repr__(self) -> str:
        # These end up in tracebacks; the key must not.
        return f"CertificateMaterial(certificate_id={self.certificate_id!r})"


def _hash(secret: str, *, salt: str) -> str:
    """Salted HMAC, as API keys use.

    The secret is 256 random bits, so stretching defends against an
    attack that does not apply, and this runs on a boot path.
    """
    return hmac.new(salt.encode(), secret.encode(), hashlib.sha256).hexdigest()


def mint_bootstrap_token(
    *, certificate_id: str, proxy: str, ttl: timedelta = BOOTSTRAP_TTL
) -> MintedToken:
    """Create a token for one proxy and one certificate."""
    _ = certificate_id, proxy  # bound by the caller when it is stored
    return MintedToken(
        token_id=secrets.token_hex(8),
        secret=(secret := secrets.token_urlsafe(SECRET_BYTES)),
        salt=(salt := secrets.token_hex(16)),
        hashed=_hash(secret, salt=salt),
        expires_at=datetime.now(UTC) + ttl,
    )


def _parse(presented: str) -> tuple[str, str]:
    """Split `aisvc-boot-<id>-<secret>`.

    The secret is never named in the error: these get logged.
    """
    parts = presented.split("-", 3)
    if len(parts) != 4 or f"{parts[0]}-{parts[1]}" != TOKEN_PREFIX or not parts[2] or not parts[3]:
        raise BootstrapRejected("invalid_token", "the presented bootstrap token is not well formed")
    return parts[2], parts[3]


class BootstrapTokens:
    """Issues and redeems proxy bootstrap tokens."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def issue(
        self, *, certificate_id: str, proxy: str, ttl: timedelta = BOOTSTRAP_TTL
    ) -> MintedToken:
        minted = mint_bootstrap_token(certificate_id=certificate_id, proxy=proxy, ttl=ttl)
        self.session.add(
            BootstrapTokenRecord(
                token_id=minted.token_id,
                certificate_id=certificate_id,
                proxy=proxy,
                salt=minted.salt,
                hashed=minted.hashed,
                expires_at=minted.expires_at,
            )
        )
        self.session.flush()
        return minted

    def record_for(self, token_id: str) -> BootstrapTokenRecord:
        record = self.session.get(BootstrapTokenRecord, token_id)
        if record is None:
            raise KeyError(token_id)
        return record

    def redeem(self, presented: str) -> CertificateMaterial:
        """Exchange a token for its certificate, and burn it.

        An unknown id and a wrong secret refuse identically: telling
        them apart would make this an oracle for which tokens exist.
        """
        token_id, secret = _parse(presented)
        record = self.session.get(BootstrapTokenRecord, token_id)

        if record is None or not hmac.compare_digest(
            _hash(secret, salt=record.salt), record.hashed
        ):
            raise BootstrapRejected("invalid_token", "the presented bootstrap token is not valid")

        if record.redeemed_at is not None:
            # The property the design rests on. Worth an alarm: a
            # second attempt means either a retry after a crash or
            # someone reading userdata.
            log.warning("bootstrap token for proxy %s was presented twice", record.proxy)
            raise BootstrapRejected(
                "already_redeemed",
                "this bootstrap token has already been used; redeploy the proxy to get a new one",
            )

        if _aware(record.expires_at) <= datetime.now(UTC):
            raise BootstrapRejected(
                "expired",
                "this bootstrap token has expired; redeploy the proxy to get a new one",
            )

        certificate = self.session.get(CertificateRecord, record.certificate_id)
        if certificate is None:
            raise BootstrapRejected(
                "certificate_not_found",
                "the certificate this token was issued for no longer exists",
            )

        # Burned before the material is handed back, and flushed with
        # it: a crash between the two must not leave a live token.
        record.redeemed_at = utcnow()
        self.session.flush()

        return CertificateMaterial(
            certificate_id=certificate.id,
            certificate=certificate.certificate,
            private_key=certificate.private_key,
        )

    def sweep(self) -> int:
        """Delete spent and expired tokens.

        A redeemed token is useless but still a row naming a
        certificate, and there is no reason to keep it.
        """
        now = datetime.now(UTC)
        removed = 0
        for record in self.session.scalars(select(BootstrapTokenRecord)):
            spent = record.redeemed_at is not None
            stale = _aware(record.expires_at) <= now
            if spent or stale:
                self.session.delete(record)
                removed += 1
        self.session.flush()
        return removed


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres does not."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
