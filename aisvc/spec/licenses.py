"""The license allow-list (SPEC.md section 3).

Every model's `license` must appear here, enforced both at mirror time and
at deploy time. Shipped as a starting point; operators edit `licenses.yaml`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

__all__ = ["DEFAULT_LICENSES", "load_licenses"]

DEFAULT_LICENSES: list[str] = [
    "apache-2.0",
    "bsd-3-clause",
    "cc-by-4.0",
    "cc-by-sa-4.0",
    "gemma",
    "llama3",
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "mit",
    "openrail",
    "qwen",
]


def load_licenses(path: str | Path) -> list[str]:
    """Read an allow-list from `licenses.yaml`.

    Accepts either a bare list or a mapping with an `allowed` key.
    """
    data: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("allowed", [])
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError(f"{path}: expected a list of license identifiers")
    return [str(item) for item in data]
