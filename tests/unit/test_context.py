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
"""Caller context and visibility (SPEC.md sections 9, 10 and 11).

"Two tenants on a shared pool cannot see each other's endpoints, keys or
usage" — this is where that is decided, so it is tested hard.
"""

from __future__ import annotations

import pytest

from aisvc.api.context import CallerContext, Role


def caller(role: Role, *, account: str = "acme", domain: str = "/Customers/Acme") -> CallerContext:
    return CallerContext(user_id="u-1", account=account, domain_path=domain, role=role)


class TestOwnsDomain:
    def test_a_domain_contains_itself(self) -> None:
        assert caller(Role.DOMAIN_ADMIN).owns_domain("/Customers/Acme")

    def test_a_domain_contains_its_children(self) -> None:
        assert caller(Role.DOMAIN_ADMIN).owns_domain("/Customers/Acme/Engineering")

    def test_a_sibling_domain_is_not_contained(self) -> None:
        assert not caller(Role.DOMAIN_ADMIN).owns_domain("/Customers/Other")

    def test_a_prefix_match_is_not_containment(self) -> None:
        """/Customers/Acme must not contain /Customers/AcmeCorp."""
        assert not caller(Role.DOMAIN_ADMIN).owns_domain("/Customers/AcmeCorp")

    def test_a_parent_domain_is_not_contained(self) -> None:
        assert not caller(Role.DOMAIN_ADMIN).owns_domain("/Customers")

    def test_the_root_domain_contains_everything(self) -> None:
        root = caller(Role.DOMAIN_ADMIN, domain="/")
        assert root.owns_domain("/Customers/Acme")
        assert root.owns_domain("/anything")

    def test_trailing_slashes_do_not_change_the_answer(self) -> None:
        trailing = caller(Role.DOMAIN_ADMIN, domain="/Customers/Acme/")
        assert trailing.owns_domain("/Customers/Acme")
        assert trailing.owns_domain("/Customers/Acme/Sub/")

    def test_an_empty_domain_is_never_contained(self) -> None:
        assert not caller(Role.DOMAIN_ADMIN).owns_domain("")


class TestMaySee:
    def test_an_admin_sees_everything(self) -> None:
        admin = caller(Role.ADMIN)
        assert admin.may_see(account="someone-else", domain_path="/Customers/Other")

    def test_a_user_sees_only_their_own_account(self) -> None:
        user = caller(Role.USER)
        assert user.may_see(account="acme", domain_path="/Customers/Acme")
        assert not user.may_see(account="other", domain_path="/Customers/Acme")

    def test_a_user_in_the_same_domain_cannot_see_another_account(self) -> None:
        """SPEC.md section 14: tenants on a shared pool are isolated."""
        user = caller(Role.USER)
        assert not user.may_see(account="neighbour", domain_path="/Customers/Acme")

    def test_a_domain_admin_sees_their_whole_domain(self) -> None:
        domain_admin = caller(Role.DOMAIN_ADMIN)
        assert domain_admin.may_see(account="anyone", domain_path="/Customers/Acme")
        assert domain_admin.may_see(account="anyone", domain_path="/Customers/Acme/Engineering")

    def test_a_domain_admin_sees_nothing_outside_it(self) -> None:
        domain_admin = caller(Role.DOMAIN_ADMIN)
        assert not domain_admin.may_see(account="anyone", domain_path="/Customers/Other")

    def test_a_domain_admin_is_not_fooled_by_a_prefix(self) -> None:
        domain_admin = caller(Role.DOMAIN_ADMIN)
        assert not domain_admin.may_see(account="anyone", domain_path="/Customers/AcmeCorp")

    def test_an_unowned_resource_is_visible_only_to_admins(self) -> None:
        assert caller(Role.ADMIN).may_see(account=None, domain_path=None)
        assert not caller(Role.USER).may_see(account=None, domain_path=None)
        assert not caller(Role.DOMAIN_ADMIN).may_see(account=None, domain_path=None)


class TestRole:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [(Role.ADMIN, True), (Role.DOMAIN_ADMIN, False), (Role.USER, False)],
    )
    def test_only_the_root_admin_role_is_admin(self, role: Role, expected: bool) -> None:
        assert role.is_admin is expected

    def test_roles_match_cloudstacks_names(self) -> None:
        """The plugin passes CloudStack's own role type through."""
        assert Role("Admin") is Role.ADMIN
        assert Role("DomainAdmin") is Role.DOMAIN_ADMIN
        assert Role("User") is Role.USER


class TestTheDomainIsPartOfIdentity:
    """A CloudStack account name is unique only within its domain.

    `may_see` compared account names alone, so a USER in `dev` under one
    domain passed the filter for `dev` under another — and that filter
    is the only tenant boundary on usage, keys, batches, certificates
    and private endpoints. `adapters.may_use` already compares the pair;
    this is the same rule in the place that guards everything else.
    """

    def test_the_same_name_in_another_domain_is_refused(self) -> None:
        caller = CallerContext(
            user_id="u", account="dev", domain_path="/Customers/AcmeA", role=Role.USER
        )

        assert not caller.may_see(account="dev", domain_path="/Customers/AcmeB")

    def test_their_own_account_is_still_allowed(self) -> None:
        caller = CallerContext(
            user_id="u", account="dev", domain_path="/Customers/AcmeA", role=Role.USER
        )

        assert caller.may_see(account="dev", domain_path="/Customers/AcmeA")

    def test_a_trailing_slash_does_not_change_the_answer(self) -> None:
        # Domain paths reach the engine from several sources and differ
        # by a trailing slash; refusing on that alone would lock a
        # tenant out of their own rows.
        caller = CallerContext(
            user_id="u", account="dev", domain_path="/Customers/AcmeA/", role=Role.USER
        )

        assert caller.may_see(account="dev", domain_path="/Customers/AcmeA")

    def test_a_resource_with_no_domain_is_refused(self) -> None:
        # Failing closed: an unattributed row is not safe to hand to a
        # tenant just because the name matches.
        caller = CallerContext(
            user_id="u", account="dev", domain_path="/Customers/AcmeA", role=Role.USER
        )

        assert not caller.may_see(account="dev", domain_path=None)

    def test_a_sub_domain_is_not_the_same_domain(self) -> None:
        # A USER is scoped to exactly their own domain. Containment is
        # the DOMAIN_ADMIN rule, and it stays theirs.
        caller = CallerContext(
            user_id="u", account="dev", domain_path="/Customers/AcmeA", role=Role.USER
        )

        assert not caller.may_see(account="dev", domain_path="/Customers/AcmeA/Sub")

    def test_an_admin_still_sees_everything(self) -> None:
        caller = CallerContext(user_id="u", account="admin", domain_path="/", role=Role.ADMIN)

        assert caller.may_see(account="dev", domain_path="/Customers/AcmeB")

    def test_a_domain_admin_still_sees_below_them(self) -> None:
        caller = CallerContext(
            user_id="u", account="ops", domain_path="/Customers", role=Role.DOMAIN_ADMIN
        )

        assert caller.may_see(account="dev", domain_path="/Customers/AcmeA")
