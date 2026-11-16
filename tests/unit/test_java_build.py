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
"""The plugin's dependencies have to be in the list that builds them.

CloudStack publishes no Maven artifacts, so every module the plugin
depends on must be built from source into `~/.m2` first. That list
lives in four places — both CI jobs and two documents — and none of
them is derived from the pom, so adding a dependency and forgetting
one of them leaves a build that works for anyone whose `~/.m2` already
has the artifact and fails for everyone else.

That is exactly how `cloud-framework-events` and `cloud-engine-schema`
came to be missing: they arrived with the maintenance listener and the
ownership DAO in milestone 8, and the build commands were never
updated. Local builds kept passing because the artifacts were already
there from an earlier, wider build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POM = ROOT / "java-plugin" / "pom.xml"

#: Every file carrying a `-pl` list of CloudStack modules to build.
BUILD_LISTS = (
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / "java-plugin" / "README.md",
    ROOT / "docs" / "installation.md",
)


def module_for(artifact: str) -> str:
    """The CloudStack source module an artifact is built from.

    CloudStack's own naming is the mapping: `cloud-framework-config`
    is built from `framework/config`, `cloud-engine-schema` from
    `engine/schema`, and a bare `cloud-utils` from `utils`.
    """
    rest = artifact.removeprefix("cloud-")
    for group in ("framework", "engine", "services", "plugins"):
        if rest.startswith(f"{group}-"):
            return f"{group}/{rest.removeprefix(f'{group}-')}"
    return rest


def required_modules() -> set[str]:
    """Every CloudStack module the plugin's pom depends on."""
    pom = POM.read_text()
    artifacts = re.findall(
        r"<groupId>org\.apache\.cloudstack</groupId>\s*<artifactId>([^<]+)</artifactId>",
        pom,
    )
    return {
        module_for(name)
        for name in artifacts
        # The plugin's own coordinates are not something to build first.
        if name != "cloud-plugin-api-ai-services"
    }


def build_lists_in(path: Path) -> list[set[str]]:
    """The module sets named by each `-pl` in `path`."""
    return [
        {part.strip() for part in found.split(",") if part.strip()}
        for found in re.findall(r"-pl\s+([A-Za-z0-9,/._-]+)", path.read_text())
    ]


class TestEveryDependencyIsBuilt:
    def test_the_pom_names_the_modules_we_expect(self) -> None:
        # A guard on the guard: if this drifts, the mapping above is
        # what needs looking at, not the build lists.
        assert "framework/events" in required_modules()
        assert "engine/schema" in required_modules()

    @pytest.mark.parametrize("path", BUILD_LISTS, ids=lambda p: p.name)
    def test_the_file_has_at_least_one_build_list(self, path: Path) -> None:
        assert build_lists_in(path), f"{path} names no -pl list"

    @pytest.mark.parametrize("path", BUILD_LISTS, ids=lambda p: p.name)
    def test_every_build_list_covers_every_dependency(self, path: Path) -> None:
        needed = required_modules()
        for found in build_lists_in(path):
            missing = needed - found
            assert not missing, (
                f"{path.relative_to(ROOT)} builds {sorted(found)} but the plugin "
                f"also needs {sorted(missing)}; without it the build fails with "
                f"'Could not find artifact' for anyone whose ~/.m2 does not "
                f"already have it"
            )

    def test_the_two_ci_jobs_build_the_same_modules(self) -> None:
        # They diverged once: engine/schema was added to the LTS job
        # and not the other, so the two jobs tested different things.
        lists = build_lists_in(ROOT / ".github" / "workflows" / "ci.yml")
        assert len(lists) >= 2
        assert all(found == lists[0] for found in lists[1:]), (
            "the CI jobs build different module sets, so one can pass while "
            "the other cannot resolve its dependencies"
        )
