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
"""The CloudStack command map (SPEC.md section 9).

Section 9's table is the authoritative endpoint list. Encoding it as
data lets the plugin's commands and role permissions be generated from
one source, and lets drift against the engine's routes fail here rather
than in a Marvin run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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

        Milestone 7 added rollouts, drain, usage and budgets; milestone 8
        added the setup wizard, scoped keys, budget writing and the
        playground; milestone 9 added the whole v2 surface. What is
        left is milestone 3's: mirroring, approval, and writing serving
        and generation profiles through the API rather than the spec.
        """
        paths = self.engine_paths()
        missing = sorted(c.name for c in V1 if c.path not in paths)

        assert len(missing) == 12, (
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


class TestTheGuardIsMethodAware:
    """A path and a method together make an endpoint.

    Comparing paths alone let `PATCH /v1/certificates/{id}` ship with no
    command: `DELETE` on the same path already had one, so the set of
    paths was unchanged and the guard saw nothing.
    """

    def engine_operations(self) -> set[tuple[str, str]]:
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
            paths = client.get("/v1/openapi.json").json()["paths"]

        return {(method.upper(), path) for path, methods in paths.items() for method in methods}

    def test_every_operation_has_a_command(self) -> None:
        engine_only = {
            ("GET", "/v1/health"),
            ("GET", "/v1/ready"),
            ("GET", "/v1/jobs/{job_id}"),
        }
        mapped = {(c.method.upper(), c.path) for c in COMMANDS}

        orphans = self.engine_operations() - engine_only - mapped

        assert not orphans, f"engine operations with no command: {sorted(orphans)}"

    def test_it_would_notice_a_new_method_on_a_known_path(self) -> None:
        """The failure the path-only guard missed."""
        mapped = {(c.method.upper(), c.path) for c in COMMANDS}

        assert ("PATCH", "/v1/certificates/{certificate_id}") in mapped
        assert ("DELETE", "/v1/certificates/{certificate_id}") in mapped


# --------------------------------------------------------------------------
# The Java generator's own table
# --------------------------------------------------------------------------
#
# `java-plugin/tools/generate_commands.py` carries a second description
# of the same commands, because it needs things this module does not
# (parameter types, response classes, event names). It is a copy, and a
# copy nobody compares is a copy that drifts: a v2 command added here
# would simply never reach CloudStack, silently and with every test
# still green. So it is compared, here, where both sides can be
# imported.
#
# Two commands are deliberately hand-written rather than generated —
# `deployAiModel` and `deleteAiModelDeployment` own the `ai_deployment`
# row CloudStack uses for ownership and cleanup, which the generator
# does not model. They are listed, so adding a third by accident fails.

HAND_WRITTEN = frozenset({"deployAiModel", "deleteAiModelDeployment"})

#: `/v1/models/{model_name}` against `/v1/models/{modelId}`: the
#: generator substitutes the Java field, so the name inside the braces
#: is its business and not drift.
PLACEHOLDER = re.compile(r"\{[^}]*\}")


def generator_commands() -> dict[str, Any]:
    """Import the generator without running it."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "java-plugin" / "tools" / "generate_commands.py"
    spec = importlib.util.spec_from_file_location("aisvc_java_generator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses resolve annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return {command.name: command for command in module.COMMANDS}


def drift(engine: Mapping[str, Any], java: Mapping[str, Any]) -> list[str]:
    """Every disagreement between the two tables, in plain words."""
    problems = []

    for name in sorted(set(engine) - set(java) - HAND_WRITTEN):
        problems.append(f"{name}: in commands.py but the generator emits no class for it")
    for name in sorted(set(java) - set(engine)):
        problems.append(f"{name}: generated, but commands.py does not have it")

    for name in sorted(set(engine) & set(java)):
        one, other = engine[name], java[name]
        if one.method.upper() != other.method.upper():
            problems.append(f"{name}: method {one.method} here, {other.method} in the generator")
        if one.is_async != other.is_async:
            problems.append(f"{name}: async {one.is_async} here, {other.is_async} in the generator")
        if one.release.value != other.release:
            problems.append(f"{name}: {one.release.value} here, {other.release} in the generator")
        here = PLACEHOLDER.sub("{}", one.path)
        there = PLACEHOLDER.sub("{}", other.path)
        if here != there:
            problems.append(f"{name}: {one.path} here, {other.path} in the generator")

    return problems


class TestTheGeneratorAgrees:
    """The plugin's surface is the command map, or it is a bug."""

    def test_the_two_tables_describe_the_same_commands(self) -> None:
        found = drift({c.name: c for c in COMMANDS}, generator_commands())

        assert not found, "the generator has drifted from commands.py:\n  " + "\n  ".join(found)

    def test_every_v2_command_is_generated(self) -> None:
        """What this guard was written for: v2 reaching CloudStack at all."""
        generated = generator_commands()

        missing = [c.name for c in V2 if c.name not in generated]

        assert not missing, f"v2 commands with no CloudStack class: {missing}"

    def test_the_hand_written_ones_really_are_in_the_map(self) -> None:
        # Otherwise the exemption would quietly excuse a typo.
        names = {c.name for c in COMMANDS}

        assert names >= HAND_WRITTEN

    def test_it_would_notice_a_command_the_generator_forgot(self) -> None:
        engine = {c.name: c for c in COMMANDS}
        java = generator_commands()
        java.pop("listAiAdapters")

        assert any("listAiAdapters" in problem for problem in drift(engine, java))

    def test_it_would_notice_a_release_that_disagreed(self) -> None:
        import dataclasses

        engine = {c.name: c for c in COMMANDS}
        java = generator_commands()
        java["listAiAdapters"] = dataclasses.replace(java["listAiAdapters"], release="v1")

        assert any("listAiAdapters" in problem for problem in drift(engine, java))

    def test_it_would_notice_a_path_that_disagreed(self) -> None:
        import dataclasses

        engine = {c.name: c for c in COMMANDS}
        java = generator_commands()
        java["listAiAdapters"] = dataclasses.replace(
            java["listAiAdapters"], path="/v1/adapters/elsewhere"
        )

        assert any("listAiAdapters" in problem for problem in drift(engine, java))


# --------------------------------------------------------------------------
# SPEC.md section 9's own table
# --------------------------------------------------------------------------
#
# The table is what a reader and a reviewer go by, and it had fallen
# behind twice — once on v1 (three commands missing) and again on v2
# (`getAiBatchResults` and all four certificate commands), both found by
# hand. Parsing it makes the omission fail a test instead.
#
# Rows name their commands in full, comma-separated. The old
# `list/create/update/deleteAiThing` shorthand is what let them hide: it
# cannot spell `listAiThings`, so a row that looked complete was not.

SPEC_PATH = "SPEC.md"


def spec_command_names() -> set[str]:
    """Every command named in section 9's table, groups expanded."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / SPEC_PATH).read_text()
    section = text.split("## 9.")[1].split("## 10.")[0]

    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cell = line.split("|")[1].strip()
        names.update(re.findall(r"`([^`]+)`", cell))
    return names


class TestTheSpecTable:
    """Section 9's table is the published surface; it has to be whole."""

    def test_the_table_names_every_command(self) -> None:
        missing = sorted({c.name for c in COMMANDS} - spec_command_names())

        assert not missing, f"commands absent from SPEC.md section 9's table: {missing}"

    def test_the_table_invents_nothing(self) -> None:
        extra = sorted(spec_command_names() - {c.name for c in COMMANDS})

        assert not extra, f"SPEC.md section 9 names commands that do not exist: {extra}"

    def test_it_reads_a_multi_command_row(self) -> None:
        # The parsing this guard rests on, stated outright.
        names = spec_command_names()

        assert {"listAiServingProfiles", "createAiServingProfile"} <= names
        assert "deleteAiGenerationProfile" in names

    def test_it_would_notice_a_command_left_out_of_the_table(self) -> None:
        assert "listAiAdapters" in spec_command_names()
        assert "getAiBatchResults" in spec_command_names()
        assert "replaceAiCertificate" in spec_command_names()


# --------------------------------------------------------------------------
# The grant against what the engine actually enforces
# --------------------------------------------------------------------------
#
# The map says who CloudStack lets through; the engine's route decides
# who it serves. Where the map is the more generous of the two, a tenant
# gets a 403 from the engine with nothing to explain it — a button that
# fails for a reason no screen can show.
#
# Not a hole: the engine is the authority and it is the stricter side.
# But each one is a promise CloudStack makes and does not keep, so they
# are counted here and the list only shrinks.

ADMIN_DEP = re.compile(
    r"@app\.(get|post|patch|delete|put)\(\s*(.*?)\)\s*\n\s*async def (\w+)\((.*?)\)\s*->",
    re.S,
)

#: Where the map is knowingly wider than the engine.
#:
#: `createAiBudget` is granted to every role and served admin-only, and
#: here the **engine** is right: a tenant who could set their own budget
#: could lift the limit that is blocking them. The map is the side to
#: narrow, which changes a published grant — an open decision, recorded
#: rather than made quietly.
KNOWN_WIDER_THAN_THE_ENGINE = {"createAiBudget"}


def admin_only_routes() -> set[tuple[str, str]]:
    """Every engine route served behind AdminDep."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "aisvc" / "api" / "app.py").read_text()

    found = set()
    for verb, args, _name, params in ADMIN_DEP.findall(source):
        path = re.search(r'f?"\{?PREFIX\}?([^"]*)"', args)
        if path and "AdminDep" in params:
            found.add((verb.upper(), "/v1" + path.group(1)))
    return found


class TestTheGrantMatchesTheEngine:
    def test_the_routes_were_read(self) -> None:
        # An empty set would make the check below vacuous.
        assert len(admin_only_routes()) > 5

    def test_no_new_command_promises_more_than_the_engine_serves(self) -> None:
        admin_only = admin_only_routes()

        wider = {
            command.name
            for command in COMMANDS
            if Role.USER in command.roles and (command.method.upper(), command.path) in admin_only
        }

        assert wider == KNOWN_WIDER_THAN_THE_ENGINE, (
            "the map and the engine disagree about who may call: "
            f"{sorted(wider - KNOWN_WIDER_THAN_THE_ENGINE)}"
        )

    def test_guardrails_are_no_longer_among_them(self) -> None:
        """What this milestone changed, stated so it cannot slip back."""
        admin_only = admin_only_routes()

        for name in (
            "listAiGuardrailProfiles",
            "createAiGuardrailProfile",
            "updateAiGuardrailProfile",
            "deleteAiGuardrailProfile",
        ):
            command = next(c for c in COMMANDS if c.name == name)
            assert (command.method.upper(), command.path) not in admin_only, (
                f"{name} is admin-only again; tenants are meant to be able to "
                "tighten their own account"
            )


class TestEveryCommandDeclaresWhoMayCallIt:
    """A tenant was denied every AI command on a real management server.

    Found end to end: a test account in a live 4.23.0.0 got

        The API [listAiModels] does not exist or is not available
        for the account aisvc-tenant.

    for all thirty-eight, `listAiModels` and `listAiEndpoints`
    included, which this map grants to every role. Root admin worked
    only because `DynamicRoleBasedAPIAccessChecker` short-circuits for
    role id 1.

    The platform's answer, read from that checker's `start()`: it
    walks every `PluggableService`'s commands, reads
    `@APICommand.authorized()`, and consults that map whenever no
    explicit `role_permissions` rule matches. So a plugin declares its
    default role access on the annotation — no schema seeding, no
    operator step, and an explicit DENY still wins because a matching
    rule returns before the fallback is reached.

    Our `Role` values are CloudStack's own `RoleType` names, so the
    mapping is one for one. `ResourceAdmin` is deliberately not
    granted anything: the engine does not model it, and inventing a
    grant is worse than leaving it to an operator who wants one.
    """

    def generated(self) -> dict[str, str]:
        """Each generated command class, by API name."""
        root = Path(__file__).resolve().parents[2] / "java-plugin" / "src" / "main"
        out = {}
        for path in (root / "java/org/apache/cloudstack/ai/api/command").rglob("*Cmd.java"):
            text = path.read_text(encoding="utf-8")
            match = re.search(r'APINAME\s*=\s*"([A-Za-z]+)"', text)
            if match:
                out[match.group(1)] = text
        return out

    def test_the_generated_classes_were_found(self) -> None:
        assert len(self.generated()) >= 38

    def test_every_command_carries_an_authorized_list(self) -> None:
        missing = [name for name, text in self.generated().items() if "authorized" not in text]

        assert not missing, (
            f"{len(missing)} command(s) declare no authorized roles, so every "
            f"role but root admin is denied them: {sorted(missing)[:5]}"
        )

    def test_the_annotation_matches_the_command_map(self) -> None:
        from aisvc.api.commands import COMMANDS

        by_name = {c.name: c for c in COMMANDS}
        wrong = []
        for name, text in self.generated().items():
            command = by_name.get(name)
            if command is None:
                continue
            found = re.search(r"authorized\s*=\s*\{([^}]*)\}", text)
            declared = set(re.findall(r"RoleType\.(\w+)", found.group(1))) if found else set()
            expected = {role.value for role in command.roles}
            if declared != expected:
                wrong.append(f"{name}: annotation {sorted(declared)} vs map {sorted(expected)}")

        assert not wrong, "\n".join(wrong)

    def test_a_tenant_facing_command_actually_names_the_user_role(self) -> None:
        # The specific failure that started this: the two commands a
        # tenant most obviously needs.
        for name in ("listAiModels", "listAiEndpoints"):
            text = self.generated()[name]
            assert "RoleType.User" in text, f"{name} would still deny every tenant"

    def test_an_admin_only_command_does_not_name_the_user_role(self) -> None:
        text = self.generated()["listAiGpuCapacity"]

        assert "RoleType.User" not in text
        assert "RoleType.Admin" in text
