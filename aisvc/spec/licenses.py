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
