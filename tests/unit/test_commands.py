"""The CloudStack command map (SPEC.md section 9).

Section 9's table is the authoritative endpoint list. Encoding it as
data lets the plugin's commands and role permissions be generated from
one source, and lets drift against the engine's routes fail here rather
than in a Marvin run.
"""

from __future__ import annotations

import re

import pytest

from aisvc.api.commands import (
    COMMANDS,
    Command,
    Release,
    commands_for_release,
    role_permissions,
)
from aisvc.api.context import Role

V1 = commands_for_release(Release.V1)
V2 = commands_for_release(Release.V2)

#: CloudStack API commands are lowerCamelCase verbs.
NAME_RE = re.compile(r"^[a-z][A-Za-z0-9]+$")


class TestShape:
    def test_both_releases_have_commands(self) -> None:
        assert len(V1) > 25
        assert len(V2) > 10

    def test_every_command_name_is_unique(self) -> None:
        names = [c.name for c in COMMANDS]
        assert len(names) == len(set(names))

    def test_names_follow_cloudstacks_convention(self) -> None:
        bad = [c.name for c in COMMANDS if not NAME_RE.match(c.name)]
        assert not bad, f"names that are not lowerCamelCase: {bad}"

    def test_every_command_names_an_ai_resource(self) -> None:
        """Commands share CloudStack's namespace; ours are all Ai-prefixed."""
        bad = [c.name for c in COMMANDS if "Ai" not in c.name]
        assert not bad, f"commands without an Ai marker: {bad}"

    def test_every_path_is_under_the_version_prefix(self) -> None:
        assert all(c.path.startswith("/v1/") for c in COMMANDS)

    def test_every_command_grants_at_least_one_role(self) -> None:
        assert all(c.roles for c in COMMANDS)


class TestAsyncConvention:
    def test_async_commands_are_never_reads(self) -> None:
        """A GET that returns a job id would be a surprising contract."""
        bad = [c.name for c in COMMANDS if c.is_async and c.method == "GET"]
        assert not bad, f"async GET commands: {bad}"

    def test_async_commands_extend_the_async_base_class(self) -> None:
        for command in COMMANDS:
            expected = "BaseAsyncCmd" if command.is_async else "BaseCmd"
            assert command.base_class == expected

    def test_the_named_async_commands_from_the_spec_are_async(self) -> None:
        by_name = {c.name: c for c in COMMANDS}
        for name in (
            "mirrorAiModel",
            "deployAiModel",
            "updateAiModelDeployment",
            "deleteAiModelDeployment",
            "promoteAiRollout",
            "rollbackAiRollout",
            "drainAiNode",
            "runAiSetupSelfTest",
        ):
            assert by_name[name].is_async, f"{name} should be async"

    def test_reads_are_not_async(self) -> None:
        by_name = {c.name: c for c in COMMANDS}
        for name in ("listAiModels", "listAiUsage", "getAiEffectiveParameters"):
            assert not by_name[name].is_async


class TestRbac:
    def test_admin_only_commands_are_never_granted_to_tenants(self) -> None:
        """SPEC.md section 9, stated as a rule."""
        granted = role_permissions(Release.V1)
        admin_only = {c.name for c in V1 if c.admin_only}

        assert not admin_only & set(granted[Role.USER])
        assert not admin_only & set(granted[Role.DOMAIN_ADMIN])

    def test_the_operational_commands_are_root_admin_only(self) -> None:
        by_name = {c.name: c for c in COMMANDS}
        for name in (
            "approveAiModel",
            "listAiGpuCapacity",
            "drainAiNode",
            "listAiReconcilerRuns",
            "runAiSetupSelfTest",
        ):
            assert by_name[name].admin_only, f"{name} should be root admin only"

    def test_serving_profiles_are_root_admin_only(self) -> None:
        """SPEC.md section 6: serving profiles are edited by root admins."""
        serving = [c for c in COMMANDS if "ServingProfile" in c.name]
        assert serving
        assert all(c.admin_only for c in serving)

    def test_generation_profiles_are_open_to_tenants(self) -> None:
        """Section 6: users may edit them for their own account, within bounds."""
        generation = [c for c in COMMANDS if "GenerationProfile" in c.name]
        assert generation
        assert all(Role.USER in c.roles for c in generation)

    def test_an_admin_may_call_everything(self) -> None:
        granted = role_permissions(Release.V1)
        assert len(granted[Role.ADMIN]) == len(V1)

    def test_tenant_commands_are_the_ones_tenants_need(self) -> None:
        granted = set(role_permissions(Release.V1)[Role.USER])
        for name in (
            "listAiModels",
            "deployAiModel",
            "listAiEndpoints",
            "createAiApiKey",
            "listAiUsage",
            "createAiPlaygroundSession",
            "getAiEffectiveParameters",
        ):
            assert name in granted, f"a tenant cannot call {name}"

    def test_permissions_are_sorted_for_a_stable_diff(self) -> None:
        for names in role_permissions().values():
            assert names == sorted(names)

    def test_v2_commands_are_excluded_from_the_v1_grant(self) -> None:
        granted = set(role_permissions(Release.V1)[Role.ADMIN])
        assert not granted & {c.name for c in V2}


class TestDomainAdminGating:
    def test_the_hub_commands_are_gated_per_domain(self) -> None:
        """ "Admin; domain admin if enabled" — ai.services.hf.require.approval."""
        by_name = {c.name: c for c in COMMANDS}
        for name in ("searchAiHfModels", "checkAiHfModel", "mirrorAiModel"):
            assert by_name[name].domain_admin_if_enabled, name

    def test_nothing_else_is_conditionally_gated(self) -> None:
        gated = {c.name for c in COMMANDS if c.domain_admin_if_enabled}
        assert gated == {"searchAiHfModels", "checkAiHfModel", "mirrorAiModel"}


class TestAgainstTheEngine:
    """The command map and the engine's routes must not drift apart."""

    def engine_paths(self) -> set[str]:
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from aisvc.api.app import create_app
        from aisvc.api.deps import Services
        from aisvc.db import Base
        from aisvc.registry import InMemoryObjectStore, ModelRegistry
        from aisvc.spec import load_spec_from_string
        from tests.unit.test_spec import LICENSES, MINIMAL

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        app = create_app(
            Services(
                spec=load_spec_from_string(MINIMAL, allowed_licenses=LICENSES),
                registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
                session_factory=sessionmaker(engine),
            )
        )
        with TestClient(app) as client:
            return set(client.get("/v1/openapi.json").json()["paths"])

    def test_every_engine_route_belongs_to_a_command(self) -> None:
        """An endpoint no command reaches is unreachable from CloudStack."""
        engine_only = {"/v1/health", "/v1/ready", "/v1/jobs/{job_id}"}
        mapped = {c.path for c in COMMANDS}

        orphans = {path for path in self.engine_paths() - engine_only if path not in mapped}

        assert not orphans, f"engine routes with no CloudStack command: {sorted(orphans)}"

    def test_the_implemented_commands_point_at_real_routes(self) -> None:
        paths = self.engine_paths()
        implemented = [c for c in V1 if c.path in paths]

        assert len(implemented) >= 8, "expected the milestone 3-4 surface to be mapped"

    def test_the_unimplemented_commands_are_known(self) -> None:
        """Fails when an endpoint lands without the count being updated.

        Milestone 4 shipped read endpoints and mirror; the rest of
        section 9 arrives with milestones 7 to 9.
        """
        paths = self.engine_paths()
        missing = sorted(c.name for c in V1 if c.path not in paths)

        assert len(missing) == 25, (
            f"{len(missing)} v1 commands still have no engine route: {missing}"
        )


class TestGeneratedArtifacts:
    """The plugin's registration is generated, not hand-maintained."""

    def test_every_v1_command_gets_a_class(self) -> None:
        from aisvc.api.commands import java_command_classes

        classes = java_command_classes(Release.V1)
        assert len(classes) == len(V1)

    def test_class_names_are_upper_camel_with_a_cmd_suffix(self) -> None:
        from aisvc.api.commands import java_command_classes

        for name in java_command_classes(Release.V1):
            simple = name.rsplit(".", 1)[-1]
            assert simple.endswith("Cmd")
            assert simple[0].isupper()

    def test_a_known_command_maps_to_its_class(self) -> None:
        from aisvc.api.commands import java_command_classes

        assert any(n.endswith(".DeployAiModelCmd") for n in java_command_classes(Release.V1))

    def test_permission_rules_cover_every_command(self) -> None:
        from aisvc.api.commands import role_permissions_rules

        rules = role_permissions_rules(Release.V1)
        assert len(rules) == len(V1)
        assert all("=" in rule for rule in rules)

    def test_an_admin_only_rule_names_only_admin(self) -> None:
        from aisvc.api.commands import role_permissions_rules

        rules = dict(r.split("=", 1) for r in role_permissions_rules(Release.V1))
        assert rules["drainAiNode"] == "Admin"

    def test_a_tenant_rule_names_every_role(self) -> None:
        from aisvc.api.commands import role_permissions_rules

        rules = dict(r.split("=", 1) for r in role_permissions_rules(Release.V1))
        assert rules["listAiModels"] == "Admin,DomainAdmin,User"

    def test_rules_are_sorted_for_a_stable_diff(self) -> None:
        from aisvc.api.commands import role_permissions_rules

        rules = role_permissions_rules(Release.V1)
        assert rules == sorted(rules)


class TestCommand:
    def test_admin_only_is_exactly_the_root_role(self) -> None:
        assert Command(
            name="x", method="GET", path="/v1/x", roles=frozenset({Role.ADMIN})
        ).admin_only

    def test_a_command_open_to_tenants_is_not_admin_only(self) -> None:
        assert not Command(
            name="x",
            method="GET",
            path="/v1/x",
            roles=frozenset({Role.ADMIN, Role.USER}),
        ).admin_only


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c.name)
def test_every_command_is_well_formed(command: Command) -> None:
    assert command.method in {"GET", "POST", "PATCH", "DELETE"}
    assert command.path.startswith("/v1/")
    assert command.roles <= {Role.ADMIN, Role.DOMAIN_ADMIN, Role.USER}
