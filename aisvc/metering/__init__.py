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
"""Usage, pricing and budgets (SPEC.md sections 4 and 7).

"Tokens and GPU-hours per tenant hourly; budget thresholds; usage export
to the plugin."

Two things here carry money, so both are deliberate. Input and output
tokens are priced **separately**, because output costs roughly three
times input in the example tiers and charging one rate would misbill
every tenant. And a threshold is reported only when it is newly crossed,
because a budget alert fired per request at 80% is an alert nobody
reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from aisvc.spec.models import PriceTier

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from aisvc.spec.models import Budget

__all__ = [
    "DEFAULT_THRESHOLDS",
    "UNPRICED",
    "BudgetState",
    "UsageMeter",
    "UsageRecord",
    "crossed_thresholds",
    "evaluate_budget",
    "price_usage",
]

log = logging.getLogger("aisvc.metering")

#: Budget notification points, as percentages (SPEC.md section 7).
DEFAULT_THRESHOLDS: tuple[int, ...] = (50, 80, 100)

TOKENS_PER_PRICING_UNIT = 1_000_000


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """What one tenant used of one model over one period."""

    account: str
    domain: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    gpu_hours: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: UsageRecord) -> UsageRecord:
        """Add another period's usage for the same tenant and model.

        Refuses to merge across tenants or models: silently combining
        them would misbill both.
        """
        if (self.account, self.domain, self.model) != (
            other.account,
            other.domain,
            other.model,
        ):
            raise ValueError(
                "cannot merge usage for different tenants or models: "
                f"{self.account}/{self.model} and {other.account}/{other.model}"
            )
        return replace(
            self,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            gpu_hours=self.gpu_hours + other.gpu_hours,
        )


def price_usage(record: UsageRecord, tier: PriceTier) -> float:
    """What this usage costs under a price tier.

    Input and output are priced apart: they are different rates, and the
    difference is large enough that one blended rate would be wrong for
    every tenant in one direction or the other.
    """
    input_cost = record.input_tokens / TOKENS_PER_PRICING_UNIT * tier.input_per_mtok
    output_cost = record.output_tokens / TOKENS_PER_PRICING_UNIT * tier.output_per_mtok
    return input_cost + output_cost


def crossed_thresholds(
    *,
    spent: float,
    limit: float,
    already_notified: Sequence[int] = (),
    thresholds: Sequence[int] = DEFAULT_THRESHOLDS,
) -> list[int]:
    """Which notification thresholds this spend newly crosses.

    `already_notified` is what has been sent this period, so a tenant
    gets one alert per threshold rather than one per request.
    """
    sent = set(already_notified)

    if limit <= 0:
        # Any spend against no budget is over every threshold at once.
        return [] if spent <= 0 else [t for t in thresholds if t not in sent]

    percent = spent / limit * 100
    return [t for t in thresholds if percent >= t and t not in sent]


@dataclass(frozen=True, slots=True)
class BudgetState:
    """Where a tenant stands against its budget (SPEC.md section 7)."""

    limit: float
    spent: float
    currency: str
    on_exceed: str
    newly_crossed: list[int]

    @property
    def percent_used(self) -> float:
        if self.limit <= 0:
            return 100.0 if self.spent > 0 else 0.0
        return self.spent / self.limit * 100

    @property
    def exceeded(self) -> bool:
        return self.percent_used >= 100

    @property
    def should_block(self) -> bool:
        """Whether the gateway should refuse further requests.

        Only when the budget says `block`: an `alert` budget notifies
        and keeps serving, because cutting a tenant off is a decision
        the operator makes in advance, not a default.
        """
        return self.exceeded and self.on_exceed == "block"


def evaluate_budget(
    budget: Budget, *, spent: float, already_notified: Sequence[int] = ()
) -> BudgetState:
    """Decide what a budget means for a tenant's current spend."""
    return BudgetState(
        limit=budget.monthly,
        spent=spent,
        currency=budget.currency,
        on_exceed=budget.on_exceed,
        newly_crossed=crossed_thresholds(
            spent=spent, limit=budget.monthly, already_notified=already_notified
        ),
    )


#: A tier of zero, for a model the catalog does not price. Usage is
#: still recorded: an uncatalogued model is a cataloguing mistake, and
#: dropping the rows would make it unrecoverable.
UNPRICED = PriceTier(name="unpriced", input_per_mtok=0.0, output_per_mtok=0.0, currency="USD")


class UsageMeter:
    """Records usage and raises budget alerts as thresholds are crossed.

    This is what the gateway's metering hook calls. The alerting lives
    here rather than in the reconciler because nothing else runs often
    enough: a reconcile is minutes apart and a tenant can go from 80% to
    over budget inside one.
    """

    def __init__(
        self,
        store: Any,
        *,
        tier_for: Callable[[str], PriceTier | None],
        sink: Any,
        adapter_premium: float = 0.0,
        task_for: Callable[[str], str] | None = None,
    ) -> None:
        self.store = store
        self.tier_for = tier_for
        self.sink = sink
        self.adapter_premium = adapter_premium
        self.task_for = task_for

    async def __call__(self, usage: UsageRecord) -> None:
        from aisvc.adapters import ADAPTER_SEPARATOR

        # An adapter is billed on its base's tier plus a premium
        # (SPEC.md section 8). The catalog has never heard of
        # `base:adapter`, so the tier is looked up for the base.
        base, separator, _ = usage.model.partition(ADAPTER_SEPARATOR)
        tier = self.tier_for(base) or UNPRICED

        if self.task_for is not None:
            from aisvc.tasks import validate_usage

            # Raises rather than recording: a non-generating task with
            # output tokens means something upstream is metering it as
            # chat, and writing that row would overcharge the tenant.
            validate_usage(usage, task=self.task_for(base))

        if separator and self.adapter_premium:
            # A pydantic model, so model_copy rather than dataclasses.replace.
            tier = tier.model_copy(
                update={
                    "input_per_mtok": tier.input_per_mtok * (1 + self.adapter_premium),
                    "output_per_mtok": tier.output_per_mtok * (1 + self.adapter_premium),
                }
            )

        # Recorded first and unconditionally. Billing outranks alerting:
        # a row that is never written cannot be recovered, while a
        # missed alert is a missed alert.
        self.store.record(usage, tier=tier)

        state = self.store.budget_state(usage.account, usage.domain)
        if state is None:
            return

        newly = self.store.take_notifications(usage.account, usage.domain)
        if not newly:
            return

        from aisvc.observability.alerts import budget_alerts

        for alert in budget_alerts(
            usage.account, usage.domain, replace(state, newly_crossed=newly)
        ):
            try:
                await self.sink.send(alert)
            except Exception:
                log.exception("could not deliver a budget alert for %s", usage.account)
