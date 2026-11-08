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
