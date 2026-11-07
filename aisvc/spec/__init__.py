"""platform.yaml: the declarative source of truth (SPEC.md section 3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from aisvc.spec.licenses import DEFAULT_LICENSES, load_licenses
from aisvc.spec.models import (
    API_VERSION,
    Architecture,
    Assignment,
    GenerationProfile,
    GenerationValue,
    GpuClass,
    Model,
    NodePool,
    PlatformDocument,
    PriceTier,
    ServingProfile,
    Tenant,
    Zone,
)
from aisvc.spec.validate import PlatformSpec, Problem, SpecError, validate

__all__ = [
    "API_VERSION",
    "DEFAULT_LICENSES",
    "Architecture",
    "Assignment",
    "GenerationProfile",
    "GenerationValue",
    "GpuClass",
    "Model",
    "NodePool",
    "PlatformDocument",
    "PlatformSpec",
    "PriceTier",
    "Problem",
    "ServingProfile",
    "SpecError",
    "Tenant",
    "Zone",
    "load_licenses",
    "load_spec",
    "load_spec_from_string",
    "validate",
]


def _problems_from_validation_error(exc: ValidationError) -> list[Problem]:
    problems: list[Problem] = []
    for error in exc.errors():
        parts: list[str] = []
        for loc in error["loc"]:
            if isinstance(loc, int):
                parts[-1] = f"{parts[-1]}[{loc}]" if parts else f"[{loc}]"
            else:
                parts.append(str(loc))
        problems.append(Problem(".".join(parts) or "<document>", error["msg"]))
    return problems


def parse_document(data: Any) -> PlatformDocument:
    """Parse a already-loaded YAML document, raising `SpecError` on failure."""
    if not isinstance(data, dict):
        raise SpecError([Problem("<document>", f"expected a mapping, got {type(data).__name__}")])
    try:
        return PlatformDocument.model_validate(data)
    except ValidationError as exc:
        raise SpecError(_problems_from_validation_error(exc)) from exc


def load_spec_from_string(text: str, *, allowed_licenses: list[str] | None = None) -> PlatformSpec:
    """Load and fully validate a platform spec from YAML text."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError([Problem("<document>", f"not valid YAML: {exc}")]) from exc
    document = parse_document(data)
    return validate(
        document,
        allowed_licenses=allowed_licenses if allowed_licenses is not None else DEFAULT_LICENSES,
    )


def load_spec(path: str | Path, *, licenses_path: str | Path | None = None) -> PlatformSpec:
    """Load and fully validate `platform.yaml` from disk."""
    spec_path = Path(path)
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError([Problem(str(spec_path), f"cannot be read: {exc}")]) from exc

    allowed = load_licenses(licenses_path) if licenses_path else DEFAULT_LICENSES
    return load_spec_from_string(text, allowed_licenses=allowed)
