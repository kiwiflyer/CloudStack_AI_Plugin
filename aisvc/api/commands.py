"""The CloudStack command map (SPEC.md section 9).

Section 9's table is "the authoritative list of endpoints". It is
encoded here once, as data, so three things that would otherwise drift
apart stay in step:

* the Java plugin's `PluggableService.getCommands()` and its default
  role permissions, generated from this rather than hand-maintained;
* the engine's `/v1` routes, which every command maps onto;
* the contract tests, which check the two against each other.

Keeping it on the Python side is deliberate. The engine is the thing
with a test suite that runs in seconds, and a command whose endpoint
does not exist should fail there rather than in a Marvin run.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aisvc.api.context import Role

__all__ = [
    "COMMANDS",
    "Command",
    "Release",
    "commands_for_release",
    "java_command_classes",
    "role_permissions",
    "role_permissions_rules",
]


class Release(StrEnum):
    V1 = "v1"
    V2 = "v2"


@dataclass(frozen=True, slots=True)
class Command:
    """One CloudStack API command and the engine endpoint behind it."""

    name: str
    method: str
    path: str
    roles: frozenset[Role]
    release: Release = Release.V1
    #: Async commands extend BaseAsyncCmd and poll /v1/jobs/{id}.
    is_async: bool = False
    #: True where section 9 says "Admin; domain admin if enabled",
    #: gated per domain by ai.services.hf.require.approval.
    domain_admin_if_enabled: bool = False

    @property
    def admin_only(self) -> bool:
        return self.roles == frozenset({Role.ADMIN})

    @property
    def base_class(self) -> str:
        """The CloudStack base class this command extends."""
        return "BaseAsyncCmd" if self.is_async else "BaseCmd"


ALL = frozenset({Role.ADMIN, Role.DOMAIN_ADMIN, Role.USER})
ADMINS = frozenset({Role.ADMIN, Role.DOMAIN_ADMIN})
ROOT = frozenset({Role.ADMIN})


def _c(
    name: str,
    method: str,
    path: str,
    roles: frozenset[Role],
    *,
    is_async: bool = False,
    release: Release = Release.V1,
    domain_admin_if_enabled: bool = False,
) -> Command:
    return Command(
        name=name,
        method=method,
        path=path,
        roles=roles,
        release=release,
        is_async=is_async,
        domain_admin_if_enabled=domain_admin_if_enabled,
    )


#: SPEC.md section 9's command map, verbatim. Path placeholders use the
#: engine's own parameter names, so `test_every_engine_route_belongs_to_a_command`
#: compares like with like rather than tripping over a rename.
COMMANDS: tuple[Command, ...] = (
    # -- sourcing ---------------------------------------------------------
    _c("searchAiHfModels", "GET", "/v1/hf/search", ROOT, domain_admin_if_enabled=True),
    _c("checkAiHfModel", "GET", "/v1/hf/check", ROOT, domain_admin_if_enabled=True),
    _c(
        "mirrorAiModel",
        "POST",
        "/v1/models/mirror",
        ROOT,
        is_async=True,
        domain_admin_if_enabled=True,
    ),
    _c("approveAiModel", "POST", "/v1/models/{id}/approve", ROOT),
    _c("publishAiModel", "POST", "/v1/models/{id}/publish", ADMINS),
    _c("listAiModels", "GET", "/v1/models", ALL),
    # -- deployments ------------------------------------------------------
    _c("deployAiModel", "POST", "/v1/deployments", ALL, is_async=True),
    _c("listAiModelDeployments", "GET", "/v1/deployments", ALL),
    _c("updateAiModelDeployment", "PATCH", "/v1/deployments/{id}", ALL, is_async=True),
    _c("deleteAiModelDeployment", "DELETE", "/v1/deployments/{id}", ALL, is_async=True),
    _c(
        "promoteAiRollout",
        "POST",
        "/v1/deployments/{deployment_id}/rollout/promote",
        ALL,
        is_async=True,
    ),
    _c(
        "rollbackAiRollout",
        "POST",
        "/v1/deployments/{deployment_id}/rollout/rollback",
        ALL,
        is_async=True,
    ),
    _c("listAiEndpoints", "GET", "/v1/endpoints", ALL),
    # -- keys -------------------------------------------------------------
    _c("createAiApiKey", "POST", "/v1/keys", ALL),
    _c("revokeAiApiKey", "DELETE", "/v1/keys/{id}", ALL),
    # -- profiles ---------------------------------------------------------
    _c("listAiServingProfiles", "GET", "/v1/profiles/serving", ROOT),
    _c("createAiServingProfile", "POST", "/v1/profiles/serving", ROOT, is_async=True),
    _c(
        "updateAiServingProfile",
        "PATCH",
        "/v1/profiles/serving/{id}",
        ROOT,
        is_async=True,
    ),
    _c(
        "deleteAiServingProfile",
        "DELETE",
        "/v1/profiles/serving/{id}",
        ROOT,
        is_async=True,
    ),
    _c("listAiGenerationProfiles", "GET", "/v1/profiles/generation", ALL),
    _c("createAiGenerationProfile", "POST", "/v1/profiles/generation", ALL),
    _c("updateAiGenerationProfile", "PATCH", "/v1/profiles/generation/{id}", ALL),
    _c("deleteAiGenerationProfile", "DELETE", "/v1/profiles/generation/{id}", ALL),
    _c("assignAiTuningProfile", "POST", "/v1/profiles/assignments", ALL, is_async=True),
    _c("getAiEffectiveParameters", "GET", "/v1/profiles/effective", ALL),
    # -- usage and budgets ------------------------------------------------
    _c("listAiUsage", "GET", "/v1/usage", ALL),
    _c("listAiBudgets", "GET", "/v1/budgets", ALL),
    _c("createAiBudget", "POST", "/v1/budgets", ALL),
    _c("updateAiBudget", "PATCH", "/v1/budgets/{id}", ALL),
    _c("deleteAiBudget", "DELETE", "/v1/budgets/{id}", ALL),
    # -- tenant experience ------------------------------------------------
    _c("createAiPlaygroundSession", "POST", "/v1/playground/sessions", ALL),
    # -- operations -------------------------------------------------------
    # SPEC.md section 9's table lists only the self-test, but section 7
    # describes a wizard whose steps "validate live" and whose GPU
    # discovery "reads host tags". Those need commands of their own, so
    # they are added here rather than left unreachable from the UI.
    _c("listAiSetupChecks", "GET", "/v1/setup/preflight", ROOT),
    _c("discoverAiGpuClasses", "GET", "/v1/setup/gpu-discovery", ROOT),
    _c("runAiSetupSelfTest", "POST", "/v1/setup/selftest", ROOT, is_async=True),
    _c("listAiGpuCapacity", "GET", "/v1/capacity", ROOT),
    _c("drainAiNode", "POST", "/v1/nodes/{node_id}/drain", ROOT, is_async=True),
    _c("listAiReconcilerRuns", "GET", "/v1/runs", ROOT),
    # -- v2 ---------------------------------------------------------------
    _c("listAiAdapters", "GET", "/v1/adapters", ALL, release=Release.V2),
    _c(
        "uploadAiAdapter",
        "POST",
        "/v1/adapters",
        ALL,
        is_async=True,
        release=Release.V2,
    ),
    _c("deleteAiAdapter", "DELETE", "/v1/adapters/{id}", ALL, release=Release.V2),
    _c("createAiBatch", "POST", "/v1/batches", ALL, is_async=True, release=Release.V2),
    _c("listAiBatches", "GET", "/v1/batches", ALL, release=Release.V2),
    _c(
        "cancelAiBatch",
        "POST",
        "/v1/batches/{id}/cancel",
        ALL,
        is_async=True,
        release=Release.V2,
    ),
    _c("listAiGuardrailProfiles", "GET", "/v1/profiles/guardrail", ALL, release=Release.V2),
    _c("createAiGuardrailProfile", "POST", "/v1/profiles/guardrail", ALL, release=Release.V2),
    _c(
        "updateAiGuardrailProfile",
        "PATCH",
        "/v1/profiles/guardrail/{id}",
        ALL,
        release=Release.V2,
    ),
    _c(
        "deleteAiGuardrailProfile",
        "DELETE",
        "/v1/profiles/guardrail/{id}",
        ALL,
        release=Release.V2,
    ),
    _c(
        "runAiModelEvaluation",
        "POST",
        "/v1/models/{id}/evaluations",
        ROOT,
        is_async=True,
        release=Release.V2,
    ),
    _c("listAiEvaluations", "GET", "/v1/models/{id}/evaluations", ROOT, release=Release.V2),
    _c(
        "createAiPrivateEndpoint",
        "POST",
        "/v1/endpoints/private",
        ALL,
        is_async=True,
        release=Release.V2,
    ),
    _c(
        "deleteAiPrivateEndpoint",
        "DELETE",
        "/v1/endpoints/private/{id}",
        ALL,
        is_async=True,
        release=Release.V2,
    ),
)


def commands_for_release(release: Release) -> tuple[Command, ...]:
    return tuple(c for c in COMMANDS if c.release is release)


def role_permissions(release: Release = Release.V1) -> dict[Role, list[str]]:
    """Which commands each default role may call (SPEC.md section 9).

    "Admin-only commands are never granted to tenant roles", so this is
    the input to the plugin's default role permission rules rather than
    something maintained by hand beside them.
    """
    granted: dict[Role, list[str]] = {role: [] for role in Role}
    for command in commands_for_release(release):
        for role in command.roles:
            granted[role].append(command.name)
    return {role: sorted(names) for role, names in granted.items()}


def java_command_classes(release: Release = Release.V1) -> list[str]:
    """Fully-qualified command classes for `PluggableService.getCommands()`.

    Generated rather than hand-listed, so a command cannot exist in the
    map and be missing from the plugin's registration.
    """
    package = "org.apache.cloudstack.api.command.admin.ai"
    return [
        f"{package}.{command.name[0].upper()}{command.name[1:]}Cmd"
        for command in commands_for_release(release)
    ]


def role_permissions_rules(release: Release = Release.V1) -> list[str]:
    """Default role permission rules, in `commands.properties` form.

    One line per command, listing the roles allowed to call it. Admin
    is implicit in CloudStack but written out so the file reads the same
    way the command map does.
    """
    order = (Role.ADMIN, Role.DOMAIN_ADMIN, Role.USER)
    lines = []
    for command in sorted(commands_for_release(release), key=lambda c: c.name):
        allowed = ",".join(role.value for role in order if role in command.roles)
        lines.append(f"{command.name}={allowed}")
    return lines
