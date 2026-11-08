"""Multi-LoRA serving (SPEC.md section 8).

Tenants bring adapters; we never train them (CLAUDE.md's non-goals).
So everything here is about accepting someone else's artefact safely:
it has to match a base we actually serve, fit the serving profile the
base runs under, stay inside the account's limits, and be safetensors
rather than a pickle.

The naming matters as much as the validation. An adapter is exposed as
`<base>:<adapter>`, and two accounts may both have one called "legal" —
so the gateway name has to be unambiguous without leaking who else
exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.adapters import (
    MAX_ADAPTER_BYTES,
    AdapterRejected,
    AdapterRequest,
    gateway_model_name,
    may_use,
    price_with_premium,
    validate_adapter,
)


def request(**kw: Any) -> AdapterRequest:
    defaults: dict[str, Any] = {
        "name": "legal-tone",
        "base_model": "llama-3.3-70b-instruct",
        "account": "acme",
        "domain": "/Customers/Acme",
        "rank": 16,
        "size_bytes": 50 * 1024 * 1024,
        "files": ("adapter_model.safetensors", "adapter_config.json"),
    }
    defaults.update(kw)
    return AdapterRequest(**defaults)


def served(bases: tuple[str, ...] = ("llama-3.3-70b-instruct",)) -> dict[str, Any]:
    return {
        "bases": bases,
        "max_lora_rank": 32,
        "max_adapters_per_account": 20,
        "existing": 0,
    }


class TestBaseModel:
    def test_an_adapter_for_a_served_base_is_accepted(self) -> None:
        assert validate_adapter(request(), **served())

    def test_an_adapter_for_an_unknown_base_is_refused(self) -> None:
        """A LoRA is meaningless away from the base it was trained on."""
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(base_model="mistral-7b"), **served())

        assert caught.value.reason == "base_not_served"

    def test_the_refusal_names_the_bases_that_are_served(self) -> None:
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(base_model="mistral-7b"), **served())

        assert "llama-3.3-70b-instruct" in caught.value.detail


class TestRank:
    def test_a_rank_within_the_profile_is_accepted(self) -> None:
        assert validate_adapter(request(rank=32), **served())

    def test_a_rank_over_the_profile_is_refused(self) -> None:
        """vLLM is started with max_lora_rank; a larger adapter fails to
        load at request time, which looks like an outage to the tenant."""
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(rank=64), **served())

        assert caught.value.reason == "rank_too_high"

    def test_the_refusal_says_what_the_limit_is(self) -> None:
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(rank=64), **served())

        assert "32" in caught.value.detail

    def test_a_nonsense_rank_is_refused(self) -> None:
        with pytest.raises(AdapterRejected):
            validate_adapter(request(rank=0), **served())


class TestFileSafety:
    def test_safetensors_are_accepted(self) -> None:
        assert validate_adapter(request(), **served())

    @pytest.mark.parametrize("bad", ["adapter_model.bin", "adapter_model.pt", "x.pkl"])
    def test_pickle_weights_are_refused(self, bad: str) -> None:
        """CLAUDE.md: pickle weights are blocked by default, and a LoRA
        is no less able to execute on load than a base model."""
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(files=(bad, "adapter_config.json")), **served())

        assert caught.value.reason == "unsafe_weights"

    def test_an_adapter_without_weights_is_refused(self) -> None:
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(files=("adapter_config.json",)), **served())

        assert caught.value.reason == "no_weights"

    def test_an_adapter_without_its_config_is_refused(self) -> None:
        """PEFT needs adapter_config.json to know the rank and targets."""
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(files=("adapter_model.safetensors",)), **served())

        assert caught.value.reason == "no_config"


class TestLimits:
    def test_an_account_within_its_count_is_accepted(self) -> None:
        assert validate_adapter(request(), **served() | {"existing": 19})

    def test_an_account_at_its_count_is_refused(self) -> None:
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(), **served() | {"existing": 20})

        assert caught.value.reason == "too_many_adapters"

    def test_an_oversized_adapter_is_refused(self) -> None:
        """An adapter is tens of megabytes; a multi-gigabyte one is a
        full checkpoint someone has mislabelled."""
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(size_bytes=MAX_ADAPTER_BYTES + 1), **served())

        assert caught.value.reason == "adapter_too_large"


class TestGatewayNaming:
    def test_an_adapter_is_exposed_under_its_base(self) -> None:
        assert gateway_model_name("llama-3.3-70b-instruct", "legal-tone") == (
            "llama-3.3-70b-instruct:legal-tone"
        )

    def test_the_base_alone_keeps_its_own_name(self) -> None:
        assert gateway_model_name("llama-3.3-70b-instruct", None) == ("llama-3.3-70b-instruct")

    def test_an_adapter_name_may_not_contain_the_separator(self) -> None:
        """Otherwise a tenant could name one `x:y` and address another
        adapter through it."""
        with pytest.raises(AdapterRejected) as caught:
            validate_adapter(request(name="legal:tone"), **served())

        assert caught.value.reason == "invalid_name"


class TestVisibility:
    def adapter(self, **kw: Any) -> AdapterRequest:
        return request(**kw)

    def test_the_owning_account_may_use_it(self) -> None:
        assert may_use(self.adapter(), account="acme", domain="/Customers/Acme", role="User")

    def test_another_account_may_not(self) -> None:
        """SPEC.md section 8: visible only to the owning account."""
        assert not may_use(self.adapter(), account="other", domain="/Customers/Other", role="User")

    def test_a_domain_shared_adapter_is_visible_in_that_domain(self) -> None:
        shared = self.adapter(shared_with_domain=True)

        assert may_use(shared, account="sibling", domain="/Customers/Acme", role="User")

    def test_a_domain_shared_adapter_is_not_visible_elsewhere(self) -> None:
        shared = self.adapter(shared_with_domain=True)

        assert not may_use(shared, account="other", domain="/Customers/Other", role="User")

    def test_a_sub_domain_sees_a_shared_adapter(self) -> None:
        shared = self.adapter(shared_with_domain=True)

        assert may_use(shared, account="child", domain="/Customers/Acme/Dev", role="User")

    def test_a_parent_domain_does_not_see_a_childs_adapter(self) -> None:
        """Sharing goes down a domain tree, never up: a reseller must
        not read what their customer built."""
        shared = self.adapter(
            account="dev-team", domain="/Customers/Acme/Dev", shared_with_domain=True
        )

        assert not may_use(shared, account="acme", domain="/Customers/Acme", role="DomainAdmin")

    def test_the_same_account_name_in_another_domain_is_a_different_account(self) -> None:
        """CloudStack account names are unique only within a domain."""
        mine = self.adapter(account="dev", domain="/Customers/Acme")

        assert not may_use(mine, account="dev", domain="/Customers/Other", role="User")

    def test_an_admin_sees_everything(self) -> None:
        assert may_use(self.adapter(), account="admin", domain="/", role="Admin")


class TestPricing:
    def test_an_adapter_costs_the_base_plus_a_premium(self) -> None:
        """SPEC.md section 8: metered at the base price plus a
        configurable adapter premium."""
        assert price_with_premium(10.0, premium=0.2) == pytest.approx(12.0)

    def test_no_premium_costs_the_base_rate(self) -> None:
        assert price_with_premium(10.0, premium=0.0) == pytest.approx(10.0)

    def test_a_negative_premium_is_refused(self) -> None:
        """Charging less than the base for extra work is a typo."""
        with pytest.raises(ValueError, match="premium"):
            price_with_premium(10.0, premium=-0.1)
