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
"""Who may author a guardrail profile, and at what scope (SPEC.md 6, 8).

Guardrails are "the third profile type in section 6, with the same
scopes and lock semantics", and section 9's UI table puts them in front
of tenants: *stricter account rules*. The routes were admin-only, which
made both of those untrue — and left the command map, which grants them
to every role, promising a call the engine would refuse.

Two rules do the work here, and they are different:

- **Authority** is about the scope. A caller may write at their own
  level and no higher, so a tenant can tighten their own account and
  can never touch the platform profile screening them.
- **Locks** are about the check. A higher scope that locked a check
  has settled it, and `resolve_guardrails` already ignores anything
  below. Authoring refuses it outright instead, because a profile that
  is silently ignored is worse than one that is refused: the tenant
  believes it took effect.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from aisvc.api.context import CallerContext, Role
from aisvc.guardrails.authority import (
    GuardrailRejected,
    author_level,
    check_may_author,
    check_not_locked_above,
    domain_contains,
    may_author,
    profile_applies_to,
)
from aisvc.profiles import Level


def caller(role: Role, account: str = "acme", domain: str = "/Customers/Acme") -> CallerContext:
    return CallerContext(user_id="u", account=account, domain_path=domain, role=role)


ADMIN = caller(Role.ADMIN, account="admin", domain="/")
DOMAIN_ADMIN = caller(Role.DOMAIN_ADMIN)
TENANT = caller(Role.USER)


class TestScopeLevels:
    def test_every_scope_maps_onto_the_precedence_chain(self) -> None:
        assert author_level("platform") is Level.PLATFORM
        assert author_level("domain") is Level.DOMAIN
        assert author_level("account") is Level.ACCOUNT
        assert author_level("deployment") is Level.DEPLOYMENT

    def test_an_unknown_scope_is_refused_rather_than_defaulted(self) -> None:
        # Defaulting would silently bind a profile at the wrong level.
        with pytest.raises(GuardrailRejected) as caught:
            author_level("universe")

        assert caught.value.reason == "unknown_scope"


class TestDomainContainment:
    def test_a_domain_contains_itself(self) -> None:
        assert domain_contains("/Customers/Acme", "/Customers/Acme")

    def test_a_domain_contains_its_children(self) -> None:
        assert domain_contains("/Customers", "/Customers/Acme")

    def test_the_separator_matters(self) -> None:
        # /Customers/Acme must not contain /Customers/AcmeCorp.
        assert not domain_contains("/Customers/Acme", "/Customers/AcmeCorp")

    def test_the_root_contains_everything(self) -> None:
        assert domain_contains("/", "/Customers/Acme")

    def test_a_child_does_not_contain_its_parent(self) -> None:
        assert not domain_contains("/Customers/Acme", "/Customers")


class TestWhoMayAuthor:
    def test_an_admin_may_write_at_any_scope(self) -> None:
        for scope in ("platform", "domain", "account", "deployment"):
            assert may_author(ADMIN, scope=scope, target="anything")

    def test_nobody_else_may_touch_the_platform_profile(self) -> None:
        # The one a tenant is most likely to want gone.
        assert not may_author(DOMAIN_ADMIN, scope="platform", target="")
        assert not may_author(TENANT, scope="platform", target="")

    def test_a_domain_admin_may_write_in_their_own_domain(self) -> None:
        assert may_author(DOMAIN_ADMIN, scope="domain", target="/Customers/Acme")
        assert may_author(DOMAIN_ADMIN, scope="domain", target="/Customers/Acme/Sub")

    def test_a_domain_admin_may_not_write_in_another_domain(self) -> None:
        assert not may_author(DOMAIN_ADMIN, scope="domain", target="/Customers/Other")

    def test_a_domain_admin_may_not_write_above_their_own_domain(self) -> None:
        # Writing at /Customers would bind a profile over every other
        # reseller in it.
        assert not may_author(DOMAIN_ADMIN, scope="domain", target="/Customers")

    def test_a_tenant_may_tighten_their_own_account(self) -> None:
        assert may_author(TENANT, scope="account", target="acme")

    def test_a_tenant_may_not_write_for_another_account(self) -> None:
        assert not may_author(TENANT, scope="account", target="someone-else")

    def test_a_tenant_may_not_write_a_domain_profile(self) -> None:
        assert not may_author(TENANT, scope="domain", target="/Customers/Acme")

    def test_deployment_scope_is_root_admin_only(self) -> None:
        # A deployment name does not say who owns it, and the engine
        # has no ownership lookup for one. Until it does, a tenant
        # could bind a weaker profile over someone else's deployment.
        assert not may_author(TENANT, scope="deployment", target="llama-prod")
        assert not may_author(DOMAIN_ADMIN, scope="deployment", target="llama-prod")


class TestTheRefusalExplainsItself:
    def test_it_names_the_scope_and_what_to_do_instead(self) -> None:
        with pytest.raises(GuardrailRejected) as caught:
            check_may_author(TENANT, scope="platform", target="")

        assert caught.value.status_code == 403
        assert "platform" in caught.value.detail
        assert "account" in caught.value.detail

    def test_writing_for_another_account_says_so(self) -> None:
        with pytest.raises(GuardrailRejected) as caught:
            check_may_author(TENANT, scope="account", target="someone-else")

        assert "someone-else" in caught.value.detail

    def test_an_allowed_write_raises_nothing(self) -> None:
        check_may_author(TENANT, scope="account", target="acme")


class TestLockedChecksAbove:
    """A locked check above has settled the matter.

    `resolve_guardrails` already ignores a lower layer that touches
    one, so nothing unsafe gets through either way. The point of
    refusing here is honesty: a tenant whose profile was accepted and
    then ignored believes their rule is in force.
    """

    PLATFORM: ClassVar[dict[str, dict[str, str]]] = {
        "pii": {"action": "redact", "policy": "locked"}
    }

    def test_a_tenant_may_not_touch_a_locked_check(self) -> None:
        with pytest.raises(GuardrailRejected) as caught:
            check_not_locked_above(
                {"pii": {"action": "flag"}}, scope="account", higher={Level.PLATFORM: self.PLATFORM}
            )

        assert caught.value.reason == "locked_above"
        assert "pii" in caught.value.detail
        assert "platform" in caught.value.detail

    def test_even_a_stricter_value_is_refused(self) -> None:
        # Locked means settled, not "settled unless you are stricter".
        # Letting a tenant tighten it would make the lock's meaning
        # depend on which direction they moved it.
        with pytest.raises(GuardrailRejected):
            check_not_locked_above(
                {"pii": {"action": "block"}},
                scope="account",
                higher={Level.PLATFORM: self.PLATFORM},
            )

    def test_an_unrelated_check_is_fine(self) -> None:
        check_not_locked_above(
            {"topics": {"action": "block", "value": ["self-harm"]}},
            scope="account",
            higher={Level.PLATFORM: self.PLATFORM},
        )

    def test_an_unlocked_check_above_may_be_overridden(self) -> None:
        # That is what precedence is for; only `locked` stops it.
        check_not_locked_above(
            {"pii": {"action": "block"}},
            scope="account",
            higher={Level.PLATFORM: {"pii": {"action": "flag", "policy": "default"}}},
        )

    def test_a_lock_below_does_not_bind_a_scope_above(self) -> None:
        # An account cannot lock the platform out of its own profile.
        check_not_locked_above(
            {"pii": {"action": "flag"}},
            scope="platform",
            higher={Level.ACCOUNT: self.PLATFORM},
        )


class TestWhatACallerSees:
    """Listing is wider than editing on purpose.

    A tenant should be able to read the platform profile screening
    their traffic — they cannot change it, and not showing it only
    means they cannot explain a block to their own customer.
    """

    def row(self, scope: str, target: str) -> object:
        class Row:
            pass

        row = Row()
        row.scope = scope  # type: ignore[attr-defined]
        row.target = target  # type: ignore[attr-defined]
        return row

    def test_an_admin_sees_everything(self) -> None:
        assert profile_applies_to(self.row("account", "someone-else"), ADMIN)

    def test_a_tenant_sees_the_platform_profile(self) -> None:
        assert profile_applies_to(self.row("platform", ""), TENANT)

    def test_a_tenant_sees_a_domain_profile_over_them(self) -> None:
        assert profile_applies_to(self.row("domain", "/Customers"), TENANT)

    def test_a_tenant_sees_their_own_account_profile(self) -> None:
        assert profile_applies_to(self.row("account", "acme"), TENANT)

    def test_a_tenant_does_not_see_another_account(self) -> None:
        assert not profile_applies_to(self.row("account", "someone-else"), TENANT)

    def test_a_tenant_does_not_see_an_unrelated_domain(self) -> None:
        assert not profile_applies_to(self.row("domain", "/Customers/Other"), TENANT)

    def test_a_domain_admin_sees_their_own_domain(self) -> None:
        assert profile_applies_to(self.row("domain", "/Customers/Acme/Sub"), DOMAIN_ADMIN)
