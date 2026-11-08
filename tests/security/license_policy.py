"""License policy for this project's dependency tree.

This is a CloudStack plugin, and Apache CloudStack is an Apache Software
Foundation project under Apache-2.0. SPEC.md section 14 keeps open the
option of upstreaming a generic `ai-services` plugin, so the dependency
tree is held to the ASF's third-party licensing policy from the start:

* **Category A** — Apache-2.0, MIT, BSD, ISC, PSF, CC0 and friends. Fine.
* **Category B** — MPL, EPL, CDDL, CPL. Allowed as a dependency, with
  attribution; not bundled as source into an Apache release.
* **Category X** — GPL, LGPL, AGPL, SSPL and anything proprietary.
  Prohibited, including LGPL, and including for dynamically linked
  Python imports.

See docs/third-party-licenses.md for the current inventory and the
non-code obligations (GPU driver EULA, model licences) this does not
cover.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import distributions

__all__ = [
    "Category",
    "PackageLicense",
    "classify",
    "installed_licenses",
]

Category = str  # "A", "B", "X" or "?"

#: Checked before anything else: "LGPL" contains "GPL", and "AGPL" contains
#: both, so substring order matters.
_CATEGORY_X = ("AGPL", "LGPL", "SSPL", "GPL", "COMMONS CLAUSE", "BUSL", "PROPRIETARY")
_CATEGORY_B = ("MPL", "MOZILLA", "EPL", "ECLIPSE PUBLIC", "CDDL", "COMMON DEVELOPMENT")
_CATEGORY_A = (
    "APACHE",
    "MIT",
    "BSD",
    "ISC",
    "PSF",
    "PYTHON SOFTWARE FOUNDATION",
    "CC0",
    "ZLIB",
    "UNLICENSE",
    "POSTGRESQL",
)

#: Packages whose metadata is wrong or absent, with the licence verified by
#: hand. Each entry needs a reason; this list is reviewed, not grown casually.
_OVERRIDES: dict[str, tuple[str, str]] = {
    # LGPL in metadata, but the classifier is the GPL-with-exception text.
    # (Empty today; kept so an override has an obvious home and a reason.)
}


@dataclass(frozen=True, slots=True)
class PackageLicense:
    name: str
    version: str
    license: str
    category: Category

    def __str__(self) -> str:
        return f"{self.name} {self.version} ({self.license}) = Category {self.category}"


def classify(license_text: str) -> Category:
    """Sort a licence string into an ASF category.

    Deliberately conservative: anything unrecognised comes back "?" so it
    is reviewed rather than waved through.
    """
    upper = license_text.upper()
    for needle in _CATEGORY_X:
        if needle in upper:
            # "GPL" inside "LGPL" is already handled: X wins either way.
            return "X"
    for needle in _CATEGORY_B:
        if needle in upper:
            return "B"
    for needle in _CATEGORY_A:
        if needle in upper:
            return "A"
    return "?"


def _license_of(metadata: object) -> str:
    """Best licence string available from a distribution's metadata."""
    get = metadata.get  # type: ignore[attr-defined]
    text = get("License-Expression") or get("License") or ""
    # Some packages paste their whole licence into the License field.
    if text and len(text) > 60:
        text = ""
    if not text:
        classifiers = metadata.get_all("Classifier") or []  # type: ignore[attr-defined]
        text = "; ".join(
            c.split("::")[-1].strip() for c in classifiers if c.startswith("License ::")
        )
    return (text or "UNKNOWN").strip()


def installed_licenses(*, skip: frozenset[str] = frozenset({"aisvc"})) -> list[PackageLicense]:
    """Every installed distribution, with its licence and category."""
    found: dict[str, PackageLicense] = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if not name or name.lower() in skip:
            continue
        key = name.lower()
        override = _OVERRIDES.get(key)
        text = override[0] if override else _license_of(dist.metadata)
        found[key] = PackageLicense(
            name=key,
            version=dist.version,
            license=text,
            category=classify(text),
        )
    return sorted(found.values(), key=lambda p: p.name)
