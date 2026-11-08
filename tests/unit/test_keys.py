"""Scoped API keys: minting, verification and rotation (SPEC.md section 7).

"Shown once; last-used time visible; rotation by overlapping keys" and
CLAUDE.md's "API keys stored as salted hashes" are the requirements that
shape this: the engine must never be able to show a key again, and
verifying one must not need the plaintext anywhere.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aisvc.gateway import (
    ApiKey,
    KeyRejected,
    KeySecret,
    hash_secret,
    mint_key,
    parse_presented_key,
    verify_secret,
)


def in_days(days: int) -> dt.datetime:
    return dt.datetime.now(dt.UTC) + dt.timedelta(days=days)


class TestMinting:
    def test_a_minted_key_carries_its_plaintext_once(self) -> None:
        minted = mint_key(account="acme", domain="/Customers/Acme", name="ci")

        assert isinstance(minted, KeySecret)
        assert minted.plaintext

    def test_the_plaintext_is_never_recoverable_from_the_stored_form(self) -> None:
        """Shown once means the engine cannot show it again."""
        minted = mint_key(account="acme", domain="/Customers/Acme", name="ci")

        assert minted.plaintext not in minted.hashed
        assert minted.plaintext not in minted.salt

    def test_every_key_gets_its_own_salt(self) -> None:
        a = mint_key(account="acme", domain="/d", name="one")
        b = mint_key(account="acme", domain="/d", name="two")

        assert a.salt != b.salt

    def test_two_keys_never_collide(self) -> None:
        keys = {mint_key(account="a", domain="/d", name="k").key_id for _ in range(50)}

        assert len(keys) == 50

    def test_the_secret_has_enough_entropy_to_resist_guessing(self) -> None:
        """A fast hash is only safe because the secret is not guessable."""
        minted = mint_key(account="acme", domain="/d", name="ci")

        assert len(minted.secret) >= 32

    def test_the_presented_key_names_the_platform(self) -> None:
        minted = mint_key(account="acme", domain="/d", name="ci")

        assert minted.plaintext.startswith("aisvc-")


class TestParsing:
    def test_a_presented_key_splits_into_id_and_secret(self) -> None:
        minted = mint_key(account="acme", domain="/d", name="ci")

        key_id, secret = parse_presented_key(minted.plaintext)

        assert key_id == minted.key_id
        assert secret == minted.secret

    @pytest.mark.parametrize("bad", ["", "nonsense", "aisvc-", "aisvc-onlyid", "x-y-z"])
    def test_a_malformed_key_is_refused(self, bad: str) -> None:
        with pytest.raises(KeyRejected) as caught:
            parse_presented_key(bad)

        assert caught.value.reason == "malformed_key"

    def test_the_refusal_does_not_echo_the_secret(self) -> None:
        """A rejected key still goes in a log; it must not go there whole."""
        with pytest.raises(KeyRejected) as caught:
            parse_presented_key("aisvc-supersecretvalue")

        assert "supersecretvalue" not in str(caught.value)
        assert "supersecretvalue" not in caught.value.detail


class TestVerification:
    def test_the_right_secret_verifies(self) -> None:
        minted = mint_key(account="acme", domain="/d", name="ci")

        assert verify_secret(minted.secret, salt=minted.salt, hashed=minted.hashed)

    def test_the_wrong_secret_does_not(self) -> None:
        minted = mint_key(account="acme", domain="/d", name="ci")

        assert not verify_secret("wrong", salt=minted.salt, hashed=minted.hashed)

    def test_another_keys_salt_does_not_verify(self) -> None:
        """Per-key salts stop one leaked hash from matching another key."""
        a = mint_key(account="acme", domain="/d", name="one")
        b = mint_key(account="acme", domain="/d", name="two")

        assert not verify_secret(a.secret, salt=b.salt, hashed=a.hashed)

    def test_hashing_is_deterministic_for_one_salt(self) -> None:
        minted = mint_key(account="acme", domain="/d", name="ci")

        assert hash_secret(minted.secret, salt=minted.salt) == minted.hashed


class TestRateLimitScope:
    def test_a_key_may_carry_a_rate_limit(self) -> None:
        key = ApiKey(key_id="k1", account="acme", domain="/d", rate_limit_rpm=60)

        assert key.rate_limit_rpm == 60

    def test_a_key_limit_above_the_account_quota_is_refused(self) -> None:
        """SPEC.md section 7: "up to the account quota", not beyond it."""
        with pytest.raises(ValueError, match="quota"):
            mint_key(
                account="acme",
                domain="/d",
                name="ci",
                rate_limit_rpm=600,
                account_quota_rpm=300,
            )

    def test_a_limit_within_the_quota_is_kept(self) -> None:
        minted = mint_key(
            account="acme",
            domain="/d",
            name="ci",
            rate_limit_rpm=100,
            account_quota_rpm=300,
        )

        assert minted.key.rate_limit_rpm == 100


class TestExpiryAndScopes:
    def test_a_minted_key_carries_its_scopes(self) -> None:
        minted = mint_key(
            account="acme",
            domain="/d",
            name="ci",
            allowed_models=("llama-3.3-70b-instruct",),
            cidrs=("10.0.0.0/8",),
            expires_at=in_days(30),
        )

        assert minted.key.allowed_models == ("llama-3.3-70b-instruct",)
        assert minted.key.cidrs == ("10.0.0.0/8",)
        assert minted.key.expires_at is not None

    def test_an_expiry_in_the_past_is_refused(self) -> None:
        """A key that is born expired is a mistake, not a policy."""
        with pytest.raises(ValueError, match="past"):
            mint_key(account="acme", domain="/d", name="ci", expires_at=in_days(-1))

    def test_key_level_profiles_ride_along(self) -> None:
        minted = mint_key(
            account="acme",
            domain="/d",
            name="ci",
            parameters={"temperature": 0.2},
        )

        assert minted.key.parameters == {"temperature": 0.2}


class TestKeyScopesAndAdapters:
    """A key allowed a base is allowed that base's adapters.

    The alternative is that every existing key stops working the moment
    a tenant uploads an adapter, which would make the feature unusable.
    It is safe because an adapter is the base plus that account's own
    weights, and `may_use` has already established the account owns it.

    A key can still be narrowed to one adapter by naming it in full.
    """

    def key(self, *models: str) -> ApiKey:
        return ApiKey(key_id="k", account="acme", domain="/d", allowed_models=models)

    def allows(self, key: ApiKey, model: str) -> bool:
        from aisvc.gateway import key_allows

        try:
            key_allows(key, model=model, source_ip="10.0.0.1")
        except KeyRejected:
            return False
        return True

    def test_a_base_scoped_key_allows_its_adapters(self) -> None:
        assert self.allows(self.key("llama"), "llama:legal-tone")

    def test_it_does_not_allow_another_bases_adapters(self) -> None:
        assert not self.allows(self.key("llama"), "gemma:legal-tone")

    def test_a_key_may_be_scoped_to_one_adapter(self) -> None:
        key = self.key("llama:legal-tone")

        assert self.allows(key, "llama:legal-tone")
        assert not self.allows(key, "llama:support")

    def test_an_adapter_scoped_key_does_not_allow_the_bare_base(self) -> None:
        """Scoping to an adapter is narrowing, not widening."""
        assert not self.allows(self.key("llama:legal-tone"), "llama")

    def test_an_unscoped_key_still_allows_everything(self) -> None:
        assert self.allows(self.key(), "llama:anything")
