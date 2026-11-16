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
"""What a CLI command said, without the terminal styling.

Typer renders help and output through Rich, and Rich styles each span
separately — it writes `--dry-run` as three of them, so the literal
string is not in the bytes at all. A test asserting on raw output
therefore passes or fails on whether colour happens to be on, which is
decided by the environment: a CI runner exporting `FORCE_COLOR` turns
every such assertion into a failure that nobody can reproduce locally.

Stripping is the fix rather than forcing colour off, because the
precedence is not what you would guess. With `FORCE_COLOR` already in
the environment, neither `NO_COLOR=1` nor `FORCE_COLOR=0` nor
`FORCE_COLOR=""` turns it off — only `TERM=dumb` does. Depending on
that is depending on one library's ordering; removing the escapes
depends on nothing.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["plain"]

#: CSI sequences, which is all Rich emits for styling.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(value: Any) -> str:
    """The text of a `Result` (or a string), free of styling."""
    text = value if isinstance(value, str) else value.output
    return _ANSI.sub("", text)
