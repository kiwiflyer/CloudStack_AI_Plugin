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
"""The GPU cards CloudStack knows about (`listGpuCards`, 4.21+).

A GPU class may narrow its capacity count to one card name, which
`take_snapshot` matches exactly against `gpucardname` from
`listGpuDevices`. That exactness is deliberate — a host carrying two
kinds of GPU has to be countable — but it makes a typo invisible: the
class counts zero GPUs, and the only symptom is a capacity warning
saying there is no room to place a node, on hosts full of idle cards.

So the name is checked against CloudStack's own catalogue. Discovery
of the devices themselves already goes through `listGpuDevices`; this
is the catalogue behind it.

**A failed lookup is never an empty catalogue.** An empty set reads as
"CloudStack knows no GPU cards at all", which would fail every GPU
class in the spec over an unreachable management server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["GpuCardCatalogue", "GpuCards", "read_cards"]

#: CloudStack's list item key for a GPU card.
ITEM_KEY = "gpucard"


class GpuCards:
    """Reads CloudStack's GPU card catalogue."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def listed(self) -> Sequence[dict[str, Any]]:
        found: Sequence[dict[str, Any]] = await self.client.call_paged("listGpuCards", ITEM_KEY)
        return found

    async def names(self) -> frozenset[str]:
        """Every name a `gpu_card` could legitimately be.

        Both `name` and `devicename` count. `listGpuCards` carries the
        two and which one `gpucardname` echoes is not documented, so
        accepting either is right where the cost of being wrong is
        refusing a name that would have worked.
        """
        return frozenset(_names_in(await self.listed()))


def _names_in(cards: Sequence[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for card in cards:
        for key in ("name", "devicename"):
            value = str(card.get(key, ""))
            if value:
                found.add(value)
    return found


@dataclass(frozen=True, slots=True)
class GpuCardCatalogue:
    """What CloudStack knows about GPU cards, or why it could not say."""

    known: frozenset[str] = frozenset()
    #: Why the lookup failed. Set means `known` says nothing, and a
    #: card must not be called unknown on the strength of it.
    error: str = ""

    def has(self, card: str) -> bool:
        return card in self.known

    @property
    def usable(self) -> bool:
        return not self.error


async def read_cards(client: Any) -> GpuCardCatalogue:
    """The catalogue, with a failure carried rather than raised.

    Preflight's contract is that every check runs even after one fails,
    so an unreachable CloudStack must not abort the whole report.
    """
    try:
        return GpuCardCatalogue(known=await GpuCards(client).names())
    except Exception as failure:
        return GpuCardCatalogue(error=str(failure))
