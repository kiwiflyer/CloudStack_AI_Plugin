"""No dependency may carry a licence incompatible with Apache-2.0.

SPEC.md section 12 puts supply chain under the security test layer. This
is the licence half: it fails CI the moment a GPL/LGPL/AGPL dependency
enters the tree, rather than at release review.
"""

from __future__ import annotations

import pytest

from tests.security.license_policy import (
    PackageLicense,
    classify,
    installed_licenses,
)

#: MPL-2.0 dependencies we accept. ASF Category B is allowed as a
#: dependency with attribution; each is listed in
#: docs/third-party-licenses.md with why it is acceptable.
ACCEPTED_CATEGORY_B = frozenset({"certifi", "hypothesis", "pathspec", "tqdm"})


class TestClassify:
    @pytest.mark.parametrize(
        "text",
        [
            "GPL-3.0",
            "LGPL-3.0-only",
            "LGPL-2.1",
            "AGPL-3.0",
            "GNU General Public License v3 (GPLv3)",
            "GNU Lesser General Public License v3 (LGPLv3)",
            "SSPL-1.0",
            "Proprietary",
        ],
    )
    def test_copyleft_and_proprietary_are_category_x(self, text: str) -> None:
        assert classify(text) == "X"

    @pytest.mark.parametrize(
        "text",
        ["MPL-2.0", "Mozilla Public License 2.0 (MPL 2.0)", "EPL-2.0", "CDDL-1.0"],
    )
    def test_weak_copyleft_is_category_b(self, text: str) -> None:
        assert classify(text) == "B"

    @pytest.mark.parametrize(
        "text",
        [
            "Apache-2.0",
            "MIT",
            "BSD-3-Clause",
            "BSD License",
            "ISC License",
            "PSF-2.0",
            "Apache-2.0 OR BSD-2-Clause",
            "MIT-0",
        ],
    )
    def test_permissive_is_category_a(self, text: str) -> None:
        assert classify(text) == "A"

    def test_lgpl_is_x_not_a_despite_containing_no_gpl_word_boundary(self) -> None:
        """'LGPL-3.0-only' must not fall through to anything permissive."""
        assert classify("LGPL-3.0-only") == "X"

    def test_an_unknown_licence_is_flagged_for_review(self) -> None:
        assert classify("Some Bespoke Licence v1") == "?"
        assert classify("UNKNOWN") == "?"


class TestInstalledTree:
    def test_no_dependency_is_category_x(self) -> None:
        """GPL, LGPL, AGPL and proprietary are prohibited outright."""
        offenders = [p for p in installed_licenses() if p.category == "X"]
        assert not offenders, "licence-incompatible dependencies:\n" + "\n".join(
            f"  {p}" for p in offenders
        )

    def test_no_dependency_has_an_unrecognised_licence(self) -> None:
        """An unclassifiable licence is reviewed, not ignored."""
        unknown = [p for p in installed_licenses() if p.category == "?"]
        assert not unknown, (
            "licences needing review (add to license_policy once checked):\n"
            + "\n".join(f"  {p}" for p in unknown)
        )

    def test_category_b_dependencies_are_the_ones_we_accepted(self) -> None:
        """A new weak-copyleft dependency should be a deliberate choice."""
        actual = {p.name for p in installed_licenses() if p.category == "B"}
        unexpected = actual - ACCEPTED_CATEGORY_B
        assert not unexpected, (
            "new Category B dependencies; review and add to "
            f"docs/third-party-licenses.md: {sorted(unexpected)}"
        )

    def test_the_postgres_driver_is_not_psycopg(self) -> None:
        """psycopg is LGPL-3.0; we use pg8000 (BSD-3-Clause) instead."""
        names = {p.name for p in installed_licenses()}
        assert "psycopg" not in names
        assert "psycopg2" not in names
        assert "psycopg2-binary" not in names

    def test_the_tree_is_not_empty(self) -> None:
        """Guards against the scan silently finding nothing."""
        assert len(installed_licenses()) > 20


class TestPackageLicense:
    def test_it_prints_readably_in_a_failure(self) -> None:
        line = str(PackageLicense(name="badpkg", version="1.0", license="LGPL-3.0", category="X"))
        assert "badpkg" in line
        assert "LGPL-3.0" in line
        assert "Category X" in line
