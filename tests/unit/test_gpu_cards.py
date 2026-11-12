"""The GPU cards CloudStack actually knows about (`listGpuCards`, 4.21+).

A GPU class may narrow its capacity count to one card name, which
`take_snapshot` matches exactly against `gpucardname` from
`listGpuDevices`. A name that matches nothing counts zero GPUs, and the
only symptom is a capacity warning saying there is no room to place a
node — on hosts full of idle GPUs.

So the name is checked against CloudStack's own card catalogue, and a
mismatch says what CloudStack does have.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.cloudstack.gpus import GpuCards, read_cards

CARDS = [
    {
        "id": "c-h100",
        "name": "NVIDIA H100 80GB HBM3",
        "devicename": "H100 80GB HBM3",
        "vendorname": "NVIDIA Corporation",
    },
    {
        "id": "c-a100",
        "name": "NVIDIA A100-SXM4-80GB",
        "devicename": "A100-SXM4-80GB",
        "vendorname": "NVIDIA Corporation",
    },
]


class FakeClient:
    def __init__(self, cards: list[dict[str, Any]] | None = None, fail: str = "") -> None:
        self.cards = CARDS if cards is None else cards
        self.fail = fail
        self.calls: list[str] = []

    async def call_paged(self, command: str, item_key: str, **params: Any) -> list[Any]:
        self.calls.append(command)
        if self.fail:
            raise RuntimeError(self.fail)
        assert command == "listGpuCards"
        assert item_key == "gpucard"
        return list(self.cards)


class TestReadingTheCatalogue:
    async def test_it_reports_every_card_name(self) -> None:
        known = await GpuCards(FakeClient()).names()

        assert "NVIDIA H100 80GB HBM3" in known

    async def test_the_device_name_counts_too(self) -> None:
        # listGpuCards carries both `name` and `devicename`, and which
        # of them `gpucardname` echoes is not documented. Accepting
        # either is right where the cost of being wrong is refusing a
        # name that would have worked.
        known = await GpuCards(FakeClient()).names()

        assert "H100 80GB HBM3" in known

    async def test_a_failure_is_not_an_empty_catalogue(self) -> None:
        # An empty set reads as "CloudStack knows no GPU cards", which
        # would fail every GPU class over an unreachable API.
        with pytest.raises(RuntimeError):
            await GpuCards(FakeClient(fail="boom")).names()


class TestReadingForAReport:
    async def test_it_comes_back_with_what_is_known(self) -> None:
        catalogue = await read_cards(FakeClient())

        assert catalogue.known
        assert not catalogue.error

    async def test_a_failure_is_carried_rather_than_raised(self) -> None:
        # Preflight's contract: every check still runs.
        catalogue = await read_cards(FakeClient(fail="connection refused"))

        assert "connection refused" in catalogue.error
        assert catalogue.known == frozenset()

    async def test_a_known_card_is_recognised(self) -> None:
        catalogue = await read_cards(FakeClient())

        assert catalogue.has("NVIDIA A100-SXM4-80GB")

    async def test_an_unknown_card_is_not(self) -> None:
        catalogue = await read_cards(FakeClient())

        assert not catalogue.has("NVIDIA H200")

    async def test_nothing_is_recognised_when_the_lookup_failed(self) -> None:
        catalogue = await read_cards(FakeClient(fail="boom"))

        assert not catalogue.has("NVIDIA A100-SXM4-80GB")
