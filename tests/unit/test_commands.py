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

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

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

        This counts *paths*, so a command whose path is served by a
        sibling verb counts as implemented — the method-aware check in
        `TestTheGuardIsMethodAware` is the one that catches a missing
        PATCH or DELETE. Both are kept: this one notices a whole
        resource arriving, that one notices a verb.

        One remains. `approveAiModel` waits on SPEC section 14's open
        question about whether reseller domain admins mirror from
        Hugging Face directly or only request it: what approval
        *means* depends on the answer, and `mirrorAiModel` already
        works, so with direct mirroring there is no request to approve.

        The deployment three are served now, by ADR-0008's `placement`
        field. They were never missing endpoints — a spec model's zone
        comes from its node pool and its replicas are a declared
        range, so until the file said who owned placement there was
        nothing for them to decide.
        """
        paths = self.engine_paths()
        missing = sorted(c.name for c in V1 if c.path not in paths)

        assert missing == ["approveAiModel"], f"v1 commands with no engine route: {missing}"


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


class TestAWriteCommandSendsWhatItWasGiven:
    """Three commands took parameters and posted an empty body.

    `createAiGuardrailProfile` declared name, scope, target and preset
    as `@Parameter`s, registered them, documented them — and sent none
    of them, because the generator builds a body only from parameters
    carrying a `wire=` name and none of those had one. A live 4.23.0.0
    answered:

        the AI Services engine returned 422 for /v1/profiles/guardrail:
        the request body or query is not valid

    Two of the three fail that loudly, because the engine requires a
    field they never send. `updateAiGuardrailProfile` does not:
    `GuardrailPatch` is all-optional, so a PATCH with an empty body is
    a clean 200 that changes nothing, and the operator is told their
    guardrail was updated when it was not.

    A parameter that is neither in the path nor in the query string
    has nowhere to travel except the body. If none of them has a
    `wire=`, the command cannot do its job.
    """

    def generator(self) -> Any:
        import importlib.util
        import sys as _sys

        root = Path(__file__).resolve().parents[2]
        path = root / "java-plugin" / "tools" / "generate_commands.py"
        spec = importlib.util.spec_from_file_location("aisvc_java_generator_body", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def body_params(self, command: Any) -> list[Any]:
        """Parameters with nowhere to go but the body."""
        return [
            param
            for param in command.params
            if "{" + param.java + "}" not in command.path and param.java not in command.query
        ]

    def test_the_generator_was_loaded(self) -> None:
        assert len(self.generator().COMMANDS) > 50

    def generated(self, name: str) -> str:
        root = Path(__file__).resolve().parents[2] / "java-plugin" / "src" / "main"
        cls = name[0].upper() + name[1:] + "Cmd.java"
        return (root / "java/org/apache/cloudstack/ai/api/command" / cls).read_text()

    def test_every_write_command_sends_its_parameters(self) -> None:
        """Asserted on the generated Java, not on the table.

        The table now defaults a parameter's wire name, so a check
        there would pass whatever the generator did with it. This
        reads what the plugin will actually compile.
        """
        generator = self.generator()
        silent = []
        for command in generator.COMMANDS:
            if command.method not in ("POST", "PATCH", "PUT"):
                continue
            params = self.body_params(command)
            if not params:
                continue
            java = self.generated(command.name)
            if "engineBody" not in java:
                silent.append(
                    f"{command.method} {command.name}: builds no body, so "
                    f"{[p.java for p in params]} are never sent"
                )
                continue
            for param in params:
                field = generator.wire_name(param)
                # A list parameter is built as a JsonArray and added
                # with `body.add`, not `addProperty`.
                sent = f'body.addProperty("{field}"' in java or f'body.add("{field}"' in java
                if not sent:
                    silent.append(f"{command.name}: {param.java} is not put in the body")

        assert not silent, "\n".join(silent)

    def test_a_renamed_field_travels_under_the_engines_name(self) -> None:
        # The five deliberate renames, which the snake_case default
        # must not quietly undo.
        assert 'body.addProperty("base_model"' in self.generated("uploadAiAdapter")
        assert 'body.addProperty("custom_domain"' in self.generated("uploadAiCertificate")
        # A list travels as an array, under the same renamed field.
        assert 'body.add("allowed_models"' in self.generated("createAiApiKey")

    def test_a_name_needing_no_translation_still_travels(self) -> None:
        # The exact class of parameter that was dropped.
        java = self.generated("createAiGuardrailProfile")
        for field in ("name", "scope", "target", "preset"):
            assert f'body.addProperty("{field}"' in java, field

    def test_a_get_needs_no_body(self) -> None:
        gets = [c for c in self.generator().COMMANDS if c.method == "GET"]
        assert gets, "no GET commands found, so this guard proves nothing"


class TestAParameterCarryingABlobSaysSo:
    """`uploadAiCertificate` could never accept a certificate.

    `@Parameter`'s `length()` defaults to 255 and a PEM certificate is
    well over a thousand characters, so a live 4.23.0.0 refused the
    only thing the command exists to take:

        Value greater than max allowed length 255 for param: certificate

    CloudStack's own `UploadCustomCertificateCmd` declares
    `length = 65535` for exactly this reason. Nothing in the generator
    emitted one, so every PEM parameter was capped at 255 — a limit
    no real input can meet, on four parameters across two commands.
    """

    def generated(self, name: str) -> str:
        root = Path(__file__).resolve().parents[2] / "java-plugin" / "src" / "main"
        cls = name[0].upper() + name[1:] + "Cmd.java"
        return (root / "java/org/apache/cloudstack/ai/api/command" / cls).read_text()

    def generator(self) -> Any:
        import importlib.util
        import sys as _sys

        root = Path(__file__).resolve().parents[2]
        path = root / "java-plugin" / "tools" / "generate_commands.py"
        spec = importlib.util.spec_from_file_location("aisvc_java_generator_len", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_every_pem_parameter_raises_the_limit(self) -> None:
        capped = []
        for command in self.generator().COMMANDS:
            for param in command.params:
                if "PEM" not in param.doc:
                    continue
                java = self.generated(command.name)
                declaration = java.split(f'name = "{param.name}"')[1].split(";")[0]
                if "length" not in declaration:
                    capped.append(f"{command.name}.{param.name} carries a PEM but is capped at 255")

        assert not capped, "\n".join(capped)

    def test_the_certificate_commands_in_particular(self) -> None:
        for name in ("uploadAiCertificate", "replaceAiCertificate"):
            java = self.generated(name)
            assert "length = 65535" in java, f"{name} caps its PEM at 255"


class TestTheMarvinExpectationsMatchTheMap:
    """Marvin cannot import `aisvc`, so it carries a copy.

    `test/integration/plugins/ai-services/role_permissions.json` is
    generated from `COMMANDS` and read by the Marvin tests, which run
    under CloudStack's own Python inside a management server. This is
    the guard that keeps the copy honest — the same arrangement as the
    Java generator's command table.
    """

    def expectations(self) -> dict[str, list[str]]:
        path = (
            Path(__file__).resolve().parents[2]
            / "test/integration/plugins/ai-services/role_permissions.json"
        )
        generated: dict[str, Any] = json.loads(path.read_text())
        roles: dict[str, list[str]] = generated["roles"]
        return roles

    def test_it_exists_and_is_generated(self) -> None:
        path = (
            Path(__file__).resolve().parents[2]
            / "test/integration/plugins/ai-services/role_permissions.json"
        )
        assert "Do not edit by hand" in path.read_text()

    def test_every_role_and_release_is_covered(self) -> None:
        expected = {
            f"{release.value}.{role.value}"
            for release in (Release.V1, Release.V2)
            for role in (Role.ADMIN, Role.DOMAIN_ADMIN, Role.USER)
        }

        assert set(self.expectations()) == expected

    def test_each_list_matches_the_command_map(self) -> None:
        wrong = []
        for release in (Release.V1, Release.V2):
            for role in (Role.ADMIN, Role.DOMAIN_ADMIN, Role.USER):
                key = f"{release.value}.{role.value}"
                here = sorted(c.name for c in COMMANDS if c.release is release and role in c.roles)
                there = self.expectations()[key]
                if here != there:
                    missing = sorted(set(here) - set(there))
                    extra = sorted(set(there) - set(here))
                    wrong.append(f"{key}: missing {missing}, stale {extra}")

        assert not wrong, (
            "\n".join(wrong) + "\n\nRegenerate role_permissions.json from aisvc/api/commands.py."
        )


class TestTheTerraformProviderSendsRealParameters:
    """CloudStack ignores a parameter it does not know, in silence.

    `cloudstack_ai_api_key` sent `allowedmodels`, `cidrs`, `expiresat`
    and `ratelimitrpm`. The command takes `models`, `cidrlist` and
    `expires`, and has no rate-limit parameter at all — so every one
    of them was dropped on the floor and the key was created with a
    name and nothing else.

    That is not a wiring bug but a security one: an operator writing

        resource "cloudstack_ai_api_key" "x" {
          allowed_models = ["llama-3.3-70b-instruct"]
          cidrs          = ["10.0.0.0/8"]
          expires_at     = "2026-01-01T00:00:00Z"
        }

    got an unrestricted key that never expires, and nothing said so.
    Found by the first acceptance-test run against a live management
    server; the unit tests pass against a fake that accepts whatever
    it is sent.
    """

    def provider_calls(self) -> list[tuple[str, set[str]]]:
        """Each `Call("name", …)` in the provider, with its parameters."""
        root = Path(__file__).resolve().parents[2] / "terraform/internal/provider"
        found = []
        for path in sorted(root.glob("*.go")):
            if path.name.endswith("_test.go"):
                continue
            source = path.read_text()
            for match in re.finditer(
                r'Call\(\s*"(\w+)",\s*map\[string\]string\{([^}]*)\}', source, re.S
            ):
                found.append((match.group(1), set(re.findall(r'"(\w+)":', match.group(2)))))
            for match in re.finditer(
                r"params\s*:=\s*map\[string\]string\{(.*?)\n\t\}(.*?)"
                r'Call\(\s*"(\w+)",\s*params',
                source,
                re.S,
            ):
                keys = set(re.findall(r'"(\w+)":', match.group(1)))
                keys |= set(re.findall(r'params\["(\w+)"\]\s*=', match.group(2)))
                found.append((match.group(3), keys))
        return found

    def command_parameters(self) -> dict[str, set[str]]:
        import importlib.util
        import sys as _sys

        root = Path(__file__).resolve().parents[2]
        path = root / "java-plugin" / "tools" / "generate_commands.py"
        spec = importlib.util.spec_from_file_location("aisvc_java_generator_tf", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return {c.name: {p.name for p in c.params} for c in module.COMMANDS}

    def test_the_provider_was_read(self) -> None:
        assert len(self.provider_calls()) >= 8

    def test_every_parameter_the_provider_sends_exists(self) -> None:
        known = self.command_parameters()
        ignored = []
        for command, sent in self.provider_calls():
            if command not in known:
                continue
            for name in sorted(sent - known[command]):
                ignored.append(
                    f"{command}: sends {name!r}, which the command does not take "
                    f"(it takes {sorted(known[command])}) — CloudStack drops it silently"
                )

        assert not ignored, "\n".join(ignored)


class TestEveryWriteRecordsAnEvent:
    """A write CloudStack cannot report on is a write nobody can audit.

    Until the event path was fixed, every AI event in `listEvents` read
    `Scheduled` and nothing followed it — the interceptor that writes
    Completed and Failed is a Spring AOP advisor, so it never saw the
    `@ActionEvent` on a command's own `execute()`. The commands now
    route their work through `AiServicesService.recordEvent`, which is
    a method on a bean, and the gate on whether anything is recorded is
    simply whether the command declares an event type.

    So that declaration is the thing worth guarding: a new write added
    without one would compile, deploy, work, and quietly leave no trace.
    """

    def _table(self) -> list[Any]:
        # `generator_commands` already imports it the one way that
        # works: its dataclasses resolve annotations through
        # sys.modules, so a loader that skips that registration fails
        # inside `dataclasses` rather than here.
        return list(generator_commands().values())

    def test_every_write_command_names_an_event(self) -> None:
        table = self._table()
        silent = sorted(
            command.name
            for command in table
            if command.method in ("POST", "PATCH", "DELETE") and not command.event
        )

        assert silent == [], (
            "these write commands would complete without recording anything: " + ", ".join(silent)
        )

    def test_no_read_command_names_an_event(self) -> None:
        # A read is not an action. `getEventType()` returning null is
        # what keeps list commands out of the operator's log, so an
        # event name on a GET would put them in it.
        table = self._table()
        noisy = sorted(
            command.name for command in table if command.method == "GET" and command.event
        )

        assert noisy == []

    def test_every_event_a_command_names_is_defined_and_listed(self) -> None:
        # `AiServicesEvents.all()` is what the convention and
        # uniqueness checks walk, so a constant missing from it is a
        # constant nothing verifies.
        table = self._table()
        source = (
            Path(__file__).resolve().parents[2]
            / "java-plugin"
            / "src"
            / "main"
            / "java"
            / "org"
            / "apache"
            / "cloudstack"
            / "ai"
            / "AiServicesEvents.java"
        ).read_text(encoding="utf-8")

        declared = set(re.findall(r"String (\w+) = \"AI\.", source))
        listed = set(re.findall(r"\b([A-Z][A-Z_]+)\b", source.split("return new String[]")[1]))

        for command in table:
            if not command.event:
                continue
            assert command.event in declared, (
                f"{command.name} names {command.event}, which AiServicesEvents does not define"
            )
            assert command.event in listed, (
                f"{command.event} is defined but missing from AiServicesEvents.all()"
            )


class TestEveryListScreenCanReachItsDetailView:
    """A column called `name` that renders nothing links nothing.

    `ListView.vue` renders the way into a record's detail view only
    where `['name', 'provider'].includes(column.key)`. So a screen
    whose first column is a bare `'name'` depends on the response
    actually having a `name` field — and when it does not, the column
    draws blank, the row cannot be clicked, and every `dataView`
    action on that screen is unreachable however correctly declared.

    That was already found once and fixed on six screens by giving
    each a synthesised column:

        { field: 'name', customTitle: 'ai.model', name: (r) => r.model }

    Two were missed. `aiendpoint` and `airollout` kept bare `'name'`
    columns while `AiEndpointResponse` and `AiDeploymentResponse`
    carry no such field, so the endpoints detail view — with its API
    keys tab, its snippets tab and three actions — and both rollout
    actions had no route to them at all.

    This compares the two sides, which is the only way to see it: the
    section config alone looks correct, and so does each response.
    """

    def _section(self) -> str:
        return (
            Path(__file__).resolve().parents[2]
            / "ui"
            / "src"
            / "config"
            / "section"
            / "aiservices.js"
        ).read_text(encoding="utf-8")

    def _responses_with_a_name(self) -> set[str]:
        """Every response class declaring a CloudStack `name` field."""
        root = (
            Path(__file__).resolve().parents[2]
            / "java-plugin"
            / "src"
            / "main"
            / "java"
            / "org"
            / "apache"
            / "cloudstack"
            / "ai"
            / "api"
            / "response"
        )
        named = set()
        for java in root.glob("*.java"):
            if '@SerializedName("name")' in java.read_text(encoding="utf-8"):
                named.add(java.stem)
        return named

    #: Which response each screen's rows come from. Written down rather
    #: than parsed, because the mapping runs through a command name and
    #: a response class and reads worse derived than stated.
    SCREEN_RESPONSES: ClassVar[dict[str, str]] = {
        "aimodelcatalog": "AiModelResponse",
        "aiendpoint": "AiEndpointResponse",
        "aiadapter": "AiAdapterResponse",
        "airegistry": "AiModelResponse",
        "airollout": "AiDeploymentResponse",
        "aiservingprofile": "AiProfileResponse",
    }

    def test_a_bare_name_column_has_a_name_to_render(self) -> None:
        section = self._section()
        named = self._responses_with_a_name()

        blank = []
        for screen, response in self.SCREEN_RESPONSES.items():
            block = section.split(f"name: '{screen}'", 1)
            if len(block) < 2:
                continue
            columns = block[1].split("details:", 1)[0]
            bare = re.search(r"^\s*'name',", columns, re.M)
            if bare and response not in named:
                blank.append(f"{screen} ({response} has no name field)")

        assert blank == [], (
            "these screens draw a blank name column, so the row cannot be "
            "clicked and every dataView action on them is unreachable: "
            + "; ".join(blank)
            + ". Render one from a field the response does have, as the "
            "other screens do."
        )

    def test_the_screen_response_map_names_real_classes(self) -> None:
        # So the test above cannot quietly pass by naming a class that
        # does not exist.
        root = (
            Path(__file__).resolve().parents[2]
            / "java-plugin"
            / "src"
            / "main"
            / "java"
            / "org"
            / "apache"
            / "cloudstack"
            / "ai"
            / "api"
            / "response"
        )
        existing = {java.stem for java in root.glob("*.java")}

        missing = sorted(set(self.SCREEN_RESPONSES.values()) - existing)
        assert missing == [], f"no such response classes: {missing}"


# ---------------------------------------------------------------------------
# The UI reads what CloudStack serialises, which is not what the Java reads
# from the engine. Those are two different names on the same command and the
# generator holds both: `items_key` is the key in the engine's own JSON, and
# the Response's `object_name` is what CloudStack puts the list under.
#
# The setup wizard read `checks` — the engine's key — so on a live management
# server it drew "No setup checks have been run yet." while the API was
# answering with all eight, two of them failures naming their remedy. The
# evaluations tab had the same bug for the same reason. Neither could be
# caught by a rendering test, which passes the array in as a prop, nor by a
# fetching test, which only asserts the command was called.
#
# Same family as the playground session reading `session_key` for `apikey`:
# only the far side can say what the far side sends.


def generator_module() -> Any:
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "java-plugin" / "tools" / "generate_commands.py"
    spec = importlib.util.spec_from_file_location("aisvc_java_generator_responses", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


UNWRAP = re.compile(r"json\?\.([a-z0-9]+response)\?\.([A-Za-z_][A-Za-z0-9_]*)")


class TestTheUiUnwrapsWhatCloudStackSends:
    def _views(self) -> list[Path]:
        root = Path(__file__).resolve().parents[2]
        return sorted((root / "ui" / "src" / "views" / "aiservices").glob("*.vue"))

    def test_every_view_reads_the_name_cloudstack_serialises(self) -> None:
        module = generator_module()
        serialised = {r.name: r.object_name for r in module.RESPONSES}
        by_lowered = {
            f"{command.name.lower()}response": command
            for command in module.COMMANDS
        }

        wrong = []
        for view in self._views():
            for envelope, key in UNWRAP.findall(view.read_text()):
                command = by_lowered.get(envelope)
                if command is None:
                    continue  # a hand-written command; checked by its own test
                expected = serialised.get(command.response)
                if expected is None or key == expected:
                    continue
                wrong.append(
                    f"{view.name} reads {envelope}.{key}, but {command.name} "
                    f"serialises its payload as '{expected}'"
                    + (
                        f" ('{key}' is the key the Java reads from the engine,"
                        " not the one CloudStack sends on)"
                        if key == command.items_key
                        else ""
                    )
                )

        assert not wrong, "\n".join(wrong)

    def test_the_views_are_actually_being_read(self) -> None:
        # A regex that matches nothing passes the test above for ever.
        found = sum(len(UNWRAP.findall(v.read_text())) for v in self._views())
        assert found >= 10, f"only {found} response unwraps found in the UI views"


# ---------------------------------------------------------------------------
# CloudStack's own build runs checkstyle with cloud-style.xml, and an unused
# import is an error there, not a warning. The out-of-tree pom does not run
# it, so the generator emitted `import org.apache.cloudstack.api.Parameter`
# unconditionally and sixteen parameterless list commands carried an import
# they never used — invisible here, sixteen build failures in a CloudStack
# tree. The rule is general rather than about that one import: an import a
# generated file does not use is a defect whichever it is.


class TestGeneratedCommandsImportOnlyWhatTheyUse:
    def test_no_command_imports_a_type_it_never_names(self) -> None:
        module = generator_module()

        unused = []
        for command in module.COMMANDS:
            source = module.render_command(command)
            head, sep, rest = source.partition("@APICommand")
            tail = sep + rest
            for line in head.splitlines():
                line = line.strip()
                if not line.startswith("import ") or not line.endswith(";"):
                    continue
                simple = line[len("import "):-1].rsplit(".", 1)[-1]
                if not re.search(rf"\b{re.escape(simple)}\b", tail):
                    unused.append(f"{command.name}: imports {simple} and never uses it")

        assert not unused, "\n".join(unused)


# ---------------------------------------------------------------------------
# A UI action names the command's parameters. CloudStack drops a parameter it
# does not know, in silence — the same thing that let the Terraform provider
# send `allowedmodels` to a command taking `models`, so every key was created
# unrestricted while the plan said otherwise.
#
# The section had thirteen of them. `createAiApiKey` was the Terraform bug
# exactly, in the UI: the dialog offered a model allow-list, a CIDR list and an
# expiry under names the command does not take, so a tenant who restricted a
# key got one that was unrestricted and never expired.


class TestEveryUiActionSendsParametersItsCommandTakes:
    def test_no_action_offers_a_field_the_command_would_drop(self) -> None:
        module = generator_module()
        commands = {c.name: c for c in module.COMMANDS}

        root = Path(__file__).resolve().parents[2]
        source = (root / "ui" / "src" / "config" / "section" / "aiservices.js").read_text()

        wrong = []
        for block in re.finditer(r"\{\s*\n\s*api: '([A-Za-z]+)',(.*?)\n        \}", source, re.S):
            name, body = block.group(1), block.group(2)
            args = re.search(r"args: \[([^\]]*)\]", body)
            if not args:
                continue
            command = commands.get(name)
            if command is None:
                continue  # hand-written; its own test covers it
            declared = {p.name for p in command.params}
            for arg in re.findall(r"'([a-z0-9]+)'", args.group(1)):
                if arg not in declared:
                    wrong.append(
                        f"the {name} action offers '{arg}', which the command does not "
                        f"declare, so CloudStack drops it: it takes {sorted(declared)}"
                    )

        assert not wrong, "\n".join(wrong)

    def test_the_actions_are_actually_being_read(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "ui" / "src" / "config" / "section" / "aiservices.js").read_text()
        found = len(re.findall(r"args: \[", source))
        assert found >= 10, f"only {found} action argument lists found in the section"
