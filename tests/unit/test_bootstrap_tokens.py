"""One-time bootstrap tokens for proxies (SPEC.md section 8).

A proxy needs its TLS material at first boot and has no identity yet.
mTLS cannot solve that: the client key would have to travel in
userdata, which is readable through the CloudStack API — the very
problem the fetch exists to avoid.

So the proxy carries a token instead. The trade is deliberate: a token
in userdata is still a credential in a readable place, but it is
**short-lived, single-use and bound to one certificate**, so a userdata
document read tomorrow yields something already spent and useless for
anything else. A private key read tomorrow is a private key.

Everything below defends that trade.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aisvc.certificates.bootstrap import (
    BOOTSTRAP_TTL,
    BootstrapRejected,
    BootstrapTokens,
    mint_bootstrap_token,
)
from aisvc.db import Base
from aisvc.db.tier2 import CertificateStore


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        yield s


def a_certificate(session: Session, *, account: str = "acme") -> Any:
    from tests.unit.test_certificates import make_cert

    cert, key = make_cert()
    return CertificateStore(session).add(
        account=account,
        domain="/Customers/Acme",
        custom_domain="ai.acme.example",
        certificate=cert,
        private_key=key,
        fingerprint="ff",
        not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=90),
    )


class TestMinting:
    def test_a_token_has_a_plaintext_and_a_stored_form(self) -> None:
        minted = mint_bootstrap_token(certificate_id="c-1", proxy="aisvc-gw-acme-m")

        assert minted.plaintext
        assert minted.hashed

    def test_the_plaintext_is_not_recoverable_from_what_is_stored(self) -> None:
        minted = mint_bootstrap_token(certificate_id="c-1", proxy="p")

        assert minted.secret not in minted.hashed
        assert minted.secret not in minted.salt

    def test_the_secret_resists_guessing(self) -> None:
        """Nothing rate-limits redemption, so the token space is the
        only thing standing between an attacker and the material."""
        minted = mint_bootstrap_token(certificate_id="c-1", proxy="p")

        assert len(minted.secret) >= 32

    def test_two_tokens_never_collide(self) -> None:
        tokens = {mint_bootstrap_token(certificate_id="c", proxy="p").token_id for _ in range(50)}

        assert len(tokens) == 50

    def test_the_ttl_is_short(self) -> None:
        """Long enough for a VM to boot and fetch, no longer: the
        window is the whole point of using a token."""
        assert dt.timedelta(hours=1) >= BOOTSTRAP_TTL

    def test_it_names_the_platform(self) -> None:
        assert mint_bootstrap_token(certificate_id="c", proxy="p").plaintext.startswith(
            "aisvc-boot-"
        )


class TestRedeeming:
    def tokens(self, session: Session) -> BootstrapTokens:
        return BootstrapTokens(session)

    def issue(self, session: Session, **kw: Any) -> tuple[str, Any]:
        record = a_certificate(session, account=kw.pop("account", "acme"))
        minted = self.tokens(session).issue(
            certificate_id=record.id, proxy=kw.pop("proxy", "aisvc-gw-acme-m"), **kw
        )
        return minted.plaintext, record

    def test_a_fresh_token_returns_its_material(self, session: Session) -> None:
        token, record = self.issue(session)

        material = self.tokens(session).redeem(token)

        assert material.certificate == record.certificate
        assert material.private_key == record.private_key

    def test_a_token_works_only_once(self, session: Session) -> None:
        """The property the whole design rests on."""
        token, _ = self.issue(session)
        self.tokens(session).redeem(token)

        with pytest.raises(BootstrapRejected) as caught:
            self.tokens(session).redeem(token)

        assert caught.value.reason == "already_redeemed"

    def test_an_expired_token_is_refused(self, session: Session) -> None:
        token, _ = self.issue(session)
        store = self.tokens(session)
        record = store.record_for(token.split("-")[2])
        record.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)

        with pytest.raises(BootstrapRejected) as caught:
            store.redeem(token)

        assert caught.value.reason == "expired"

    def test_a_forged_token_is_refused(self, session: Session) -> None:
        token, _ = self.issue(session)
        forged = token.rsplit("-", 1)[0] + "-wrong"

        with pytest.raises(BootstrapRejected) as caught:
            self.tokens(session).redeem(forged)

        assert caught.value.reason == "invalid_token"

    def test_an_unknown_token_is_refused_the_same_way(self, session: Session) -> None:
        """A different reason would say which token ids exist."""
        with pytest.raises(BootstrapRejected) as caught:
            self.tokens(session).redeem("aisvc-boot-deadbeef-secret")

        assert caught.value.reason == "invalid_token"

    @pytest.mark.parametrize("bad", ["", "nonsense", "aisvc-boot-", "a-b-c"])
    def test_a_malformed_token_is_refused(self, session: Session, bad: str) -> None:
        with pytest.raises(BootstrapRejected):
            self.tokens(session).redeem(bad)

    def test_a_token_for_a_deleted_certificate_is_refused(self, session: Session) -> None:
        token, record = self.issue(session)
        CertificateStore(session).delete(record)

        with pytest.raises(BootstrapRejected) as caught:
            self.tokens(session).redeem(token)

        assert caught.value.reason == "certificate_not_found"

    def test_redeeming_burns_it_even_though_it_returns_material(self, session: Session) -> None:
        token, _ = self.issue(session)
        store = self.tokens(session)
        store.redeem(token)

        record = store.record_for(token.split("-")[2])
        assert record.redeemed_at is not None


class TestItIsBoundToOneThing:
    def test_a_token_cannot_fetch_another_certificate(self, session: Session) -> None:
        """A stolen token is worth one certificate, not the platform's."""
        mine = a_certificate(session, account="acme")
        theirs = a_certificate(session, account="other")
        store = BootstrapTokens(session)
        minted = store.issue(certificate_id=mine.id, proxy="aisvc-gw-acme-m")

        material = store.redeem(minted.plaintext)

        assert material.certificate == mine.certificate
        assert material.certificate != theirs.certificate

    def test_the_token_records_which_proxy_it_was_for(self, session: Session) -> None:
        """So a redemption can be traced to the VM that should have
        made it."""
        record = a_certificate(session)
        store = BootstrapTokens(session)
        minted = store.issue(certificate_id=record.id, proxy="aisvc-gw-acme-m")

        assert store.record_for(minted.token_id).proxy == "aisvc-gw-acme-m"


class TestNothingLeaks:
    def test_a_refusal_does_not_echo_the_token(self, session: Session) -> None:
        """These reach logs and the proxy's console output."""
        with pytest.raises(BootstrapRejected) as caught:
            BootstrapTokens(session).redeem("aisvc-boot-abc-supersecretvalue")

        assert "supersecretvalue" not in caught.value.detail
        assert "supersecretvalue" not in str(caught.value)

    def test_the_stored_row_holds_no_plaintext(self, session: Session) -> None:
        record = a_certificate(session)
        store = BootstrapTokens(session)
        minted = store.issue(certificate_id=record.id, proxy="p")

        stored = store.record_for(minted.token_id)
        assert minted.secret not in str(vars(stored))


class TestSweeping:
    def test_spent_and_expired_tokens_are_swept(self, session: Session) -> None:
        """A redeemed token is useless but still a row naming a
        certificate; there is no reason to keep it."""
        record = a_certificate(session)
        store = BootstrapTokens(session)
        minted = store.issue(certificate_id=record.id, proxy="p")
        store.redeem(minted.plaintext)

        assert store.sweep() == 1

    def test_a_live_token_is_kept(self, session: Session) -> None:
        record = a_certificate(session)
        store = BootstrapTokens(session)
        store.issue(certificate_id=record.id, proxy="p")

        assert store.sweep() == 0
