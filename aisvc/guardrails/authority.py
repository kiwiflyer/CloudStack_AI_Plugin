"""Who may author a guardrail profile, and at what scope (SPEC.md 6, 8).

Guardrails are "the third profile type in section 6, with the same
scopes and lock semantics", and section 9's UI table puts them in front
of a tenant: *stricter account rules*. The routes were admin-only, so
neither was true, and the command map — which grants these to every
role — promised a call the engine would refuse with a 403 and nothing
to explain it.

The objection the old comment recorded is the right one: *a guardrail
an account can remove is not one*. This module is the answer to it
rather than a reversal of it. A caller may write at their own level and
no higher, so tightening their own account is theirs to do and the
platform profile screening them is not — they cannot reach it, loosen
it, or delete it.

Two rules, deliberately separate:

- **Authority** is about the scope, and is settled here.
- **Locks** are about one check, and `resolve_guardrails` already
  enforces them when a request is screened. Authoring refuses a locked
  check as well, which protects nothing extra but is the difference
  between a tenant being told no and a tenant believing a rule took
  effect when it was silently dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aisvc.profiles import Level

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aisvc.api.context import CallerContext

__all__ = [
    "SCOPES",
    "GuardrailRejected",
    "author_level",
    "check_may_author",
    "check_not_locked_above",
    "domain_contains",
    "may_author",
    "profile_applies_to",
]

#: The scopes a profile may be bound at, onto section 6's chain. There
#: is no key or request scope: those are per-call, not stored policy.
SCOPES: dict[str, Level] = {
    "platform": Level.PLATFORM,
    "domain": Level.DOMAIN,
    "account": Level.ACCOUNT,
    "deployment": Level.DEPLOYMENT,
}


class GuardrailRejected(Exception):
    """A guardrail write is not this caller's to make.

    `detail` reaches the tenant's own console, so it says what they may
    do instead rather than only what they may not.
    """

    def __init__(self, reason: str, detail: str, *, status_code: int = 403) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code


def author_level(scope: str) -> Level:
    """The precedence level a scope binds at."""
    level = SCOPES.get(scope)
    if level is None:
        known = ", ".join(SCOPES)
        raise GuardrailRejected(
            "unknown_scope",
            f"{scope!r} is not a guardrail scope; use one of {known}",
            status_code=400,
        )
    return level


def domain_contains(parent: str, child: str) -> bool:
    """Whether `parent` is `child` or an ancestor of it.

    The separator matters: /Customers/Acme contains /Customers/Acme/Sub
    but not /Customers/AcmeCorp.
    """
    if not parent or not child:
        return False
    above = parent.rstrip("/") or "/"
    below = child.rstrip("/") or "/"
    if above == "/":
        return True
    return below == above or below.startswith(f"{above}/")


def may_author(caller: CallerContext, *, scope: str, target: str) -> bool:
    """Whether this caller may create, change or delete such a profile.

    Create and edit are one question. A caller who could edit a profile
    they could not have created would be loosening a rule set over
    them, which is the whole thing this prevents.
    """
    level = author_level(scope)

    if caller.role.is_admin:
        return True

    if level is Level.PLATFORM:
        # The profile that screens everyone. Root admin's alone.
        return False

    if level is Level.DOMAIN:
        # A domain admin writes inside their own domain, never at or
        # above it: /Customers would bind over every other reseller.
        from aisvc.api.context import Role

        if caller.role is not Role.DOMAIN_ADMIN:
            return False
        return domain_contains(caller.domain_path, target) and target.rstrip("/") != "/"

    if level is Level.ACCOUNT:
        # Their own, and only their own. A domain admin constraining a
        # sub-account sets a domain profile, which covers all of them —
        # the engine cannot tell which domain an account name is in.
        return target == caller.account

    # Deployment scope. A deployment name does not say who owns it and
    # the engine has no ownership lookup for one, so a tenant could
    # otherwise bind a weaker profile over someone else's deployment.
    # Root admin until there is a lookup to check against.
    return False


def check_may_author(caller: CallerContext, *, scope: str, target: str) -> None:
    """Raise unless this caller may write that profile."""
    if may_author(caller, scope=scope, target=target):
        return

    level = author_level(scope)
    if level is Level.PLATFORM:
        detail = (
            "the platform guardrail profile is set by a root admin and applies to "
            "everyone; to tighten the rules on your own traffic, create a profile "
            "at the account scope instead"
        )
    elif level is Level.DOMAIN:
        detail = (
            f"{target or 'that domain'} is not inside your own domain, so a guardrail "
            "profile there is not yours to set; use the account scope for your own "
            "traffic"
        )
    elif level is Level.ACCOUNT:
        detail = (
            f"a guardrail profile at the account scope applies to {target!r}, which is "
            f"not your account; you may set one for {caller.account!r}"
        )
    else:
        detail = (
            "guardrail profiles at the deployment scope are set by a root admin, "
            "because a deployment name does not say who owns it; use the account "
            "scope to tighten the rules on your own traffic"
        )
    raise GuardrailRejected("not_your_scope", detail)


def check_not_locked_above(
    checks: Mapping[str, Any] | None,
    *,
    scope: str,
    higher: Mapping[Level, Mapping[str, Any]],
) -> None:
    """Raise if any submitted check is locked at a higher scope.

    `higher` maps a level to the checks bound there. Only levels above
    the one being written matter: an account cannot lock the platform
    out of its own profile.
    """
    if not checks:
        return

    mine = author_level(scope)
    for level, bound in sorted(higher.items()):
        if level >= mine:
            continue
        for name, spec in bound.items():
            if name not in checks:
                continue
            if _policy_of(spec) != "locked":
                continue
            raise GuardrailRejected(
                "locked_above",
                f"the {name!r} check is locked at the {level.label} scope, so it "
                "cannot be set here — including to a stricter value. Ask whoever "
                "owns that profile to change it.",
                status_code=409,
            )


def _policy_of(spec: Any) -> str:
    """The policy on a check, however it was stored."""
    if isinstance(spec, dict):
        return str(spec.get("policy", "default"))
    return str(getattr(spec, "policy", "default"))


def profile_applies_to(record: Any, caller: CallerContext) -> bool:
    """Whether this caller should see a profile in a listing.

    Wider than `may_author` on purpose: a tenant can read the platform
    profile screening their traffic but not touch it. Hiding it would
    only leave them unable to explain a block to their own customer.
    """
    if caller.role.is_admin:
        return True

    scope = getattr(record, "scope", "")
    target = getattr(record, "target", "") or ""

    if scope == "platform":
        return True
    if scope == "domain":
        # Either it is bound over them, or it is inside their domain
        # and therefore theirs to see.
        return domain_contains(target, caller.domain_path) or domain_contains(
            caller.domain_path, target
        )
    if scope == "account":
        from aisvc.api.context import Role

        if caller.role is Role.DOMAIN_ADMIN:
            # The engine cannot tell which domain an account is in, so
            # a domain admin sees their own account's and no more.
            return target == caller.account
        return target == caller.account

    # Deployment scope: root admin's to set, and nothing here can say
    # whose deployment it is.
    return False
