"""Request-scoped dependencies (SPEC.md section 10).

The engine is called only by the Java plugin and the operator CLI, over
mTLS, and is unreachable from tenant networks. The caller context is
therefore *asserted* by the plugin in headers rather than authenticated
here — the transport is what proves who is calling.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, Request, status
from sqlalchemy.orm import Session

from aisvc.api.context import CallerContext, Role
from aisvc.api.errors import ApiError
from aisvc.api.jobs import JobStore

if TYPE_CHECKING:
    from aisvc.inventory import CapacitySnapshot
    from aisvc.registry import ModelRegistry
    from aisvc.spec import PlatformSpec

__all__ = [
    "CallerDep",
    "Services",
    "SessionDep",
    "caller_context",
    "require_admin",
]

USER_HEADER = "X-Aisvc-User-Id"
ACCOUNT_HEADER = "X-Aisvc-Account"
DOMAIN_HEADER = "X-Aisvc-Domain"
ROLE_HEADER = "X-Aisvc-Role"


@dataclass
class Services:
    """Everything the handlers need, assembled once at startup.

    Held on `app.state` rather than imported, so tests can substitute
    fakes without patching modules.
    """

    spec: PlatformSpec
    registry: ModelRegistry
    session_factory: object
    sourcing: object | None = None
    observe: object | None = None

    def snapshot(self) -> CapacitySnapshot | None:
        return None


def caller_context(
    user_id: Annotated[str | None, Header(alias=USER_HEADER)] = None,
    account: Annotated[str | None, Header(alias=ACCOUNT_HEADER)] = None,
    domain: Annotated[str | None, Header(alias=DOMAIN_HEADER)] = None,
    role: Annotated[str | None, Header(alias=ROLE_HEADER)] = None,
) -> CallerContext:
    """Read the caller the plugin asserted, or refuse the request."""
    missing = [
        name
        for name, value in (
            (USER_HEADER, user_id),
            (ACCOUNT_HEADER, account),
            (DOMAIN_HEADER, domain),
            (ROLE_HEADER, role),
        )
        if not value
    ]
    if missing:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "caller context is incomplete: missing " + ", ".join(missing),
            code="missing_caller_context",
        )

    try:
        parsed = Role(str(role))
    except ValueError as exc:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            f"{role!r} is not a known role",
            code="unknown_role",
        ) from exc

    return CallerContext(
        user_id=str(user_id),
        account=str(account),
        domain_path=str(domain),
        role=parsed,
    )


CallerDep = Annotated[CallerContext, Depends(caller_context)]


def require_admin(caller: CallerDep) -> CallerContext:
    """Guard admin-only commands (SPEC.md section 9).

    "Admin-only commands are never granted to tenant roles."
    """
    if not caller.role.is_admin:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "this command is available to root admins only",
            code="forbidden",
        )
    return caller


AdminDep = Annotated[CallerContext, Depends(require_admin)]


def services(request: Request) -> Services:
    found = getattr(request.app.state, "services", None)
    if found is None:  # pragma: no cover - misconfiguration
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the engine is not configured",
            code="unavailable",
        )
    return found  # type: ignore[no-any-return]


ServicesDep = Annotated[Services, Depends(services)]


def db_session(request: Request) -> Iterator[Session]:
    """A transaction per request, committed on success."""
    factory = request.app.state.services.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(db_session)]


def job_store(session: SessionDep) -> JobStore:
    return JobStore(session)


JobsDep = Annotated[JobStore, Depends(job_store)]
