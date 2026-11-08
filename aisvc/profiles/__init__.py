"""Generation profile resolution (SPEC.md section 6).

One implementation, used in two places: the engine answers
`getAiEffectiveParameters` with it, and the gateway's pre-call hook
enforces requests with it ([ADR-0002](../../docs/adr/0002-gateway.md)).
They cannot disagree because there is nothing to disagree with.

Precedence, most specific last:

    platform -> model -> domain -> account -> deployment -> key -> request

Three rules carry the weight:

* **A lock binds everything below it.** Once a level marks a parameter
  `locked`, no more specific level and no request can change it.
* **Bounds narrow, never widen.** `min` and `max` intersect down the
  chain, so a tenant cannot escape a bound an admin set.
* **Policy decides what a request may do.** `default` permits an
  override inside the bounds and rejects one outside; `clamp` forces the
  value into range; `locked` ignores it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from aisvc.spec.models import GenerationValue, Policy

__all__ = [
    "EffectiveParams",
    "Level",
    "ParameterRejected",
    "ProfileLayer",
    "ResolvedValue",
    "resolve",
]


class Level(IntEnum):
    """Precedence levels, lowest first (SPEC.md section 6)."""

    PLATFORM = 1
    MODEL = 2
    DOMAIN = 3
    ACCOUNT = 4
    DEPLOYMENT = 5
    KEY = 6
    REQUEST = 7

    @property
    def label(self) -> str:
        return self.name.lower()


class ParameterRejected(Exception):
    """A request asked for a value its profile does not permit.

    The gateway turns this into a 403 or 400 naming the parameter, the
    bound and the level that set it, so a tenant can see why.
    """

    def __init__(
        self,
        parameter: str,
        message: str,
        *,
        source: Level | None = None,
    ) -> None:
        super().__init__(message)
        self.parameter = parameter
        self.source = source


@dataclass(frozen=True, slots=True)
class ProfileLayer:
    """One profile bound at one precedence level."""

    level: Level
    params: Mapping[str, GenerationValue]
    name: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedValue:
    """One parameter's resolved value, and where it came from."""

    name: str
    value: Any
    source: Level
    policy: Policy = "default"
    locked_at: Level | None = None
    min: float | None = None
    max: float | None = None
    #: Set when a `clamp` policy moved a request value into range.
    clamped_from: Any = None

    @property
    def is_locked(self) -> bool:
        return self.locked_at is not None


@dataclass(frozen=True)
class EffectiveParams(Mapping[str, ResolvedValue]):
    """Every resolved parameter, keyed by name."""

    entries: dict[str, ResolvedValue] = field(default_factory=dict)

    def __getitem__(self, key: str) -> ResolvedValue:
        return self.entries[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def to_overrides(self) -> dict[str, Any]:
        """The parameters to apply to the upstream request."""
        return {name: resolved.value for name, resolved in self.entries.items()}

    def describe(self) -> dict[str, dict[str, Any]]:
        """The shape `getAiEffectiveParameters` returns (SPEC.md section 6)."""
        return {
            name: {
                "value": resolved.value,
                "source": resolved.source.label,
                "policy": resolved.policy,
                "locked": resolved.is_locked,
                "locked_at": resolved.locked_at.label if resolved.locked_at else None,
                "min": resolved.min,
                "max": resolved.max,
            }
            for name, resolved in sorted(self.entries.items())
        }


@dataclass(slots=True)
class _Accumulator:
    """A parameter's state part-way down the precedence chain."""

    name: str
    value: Any = None
    source: Level = Level.PLATFORM
    policy: Policy = "default"
    locked_at: Level | None = None
    min: float | None = None
    max: float | None = None
    seen: bool = False

    def narrow_bounds(self, spec: GenerationValue) -> None:
        """Intersect bounds; a more specific level may only tighten."""
        if spec.min is not None:
            self.min = spec.min if self.min is None else max(self.min, spec.min)
        if spec.max is not None:
            self.max = spec.max if self.max is None else min(self.max, spec.max)

    def apply(self, level: Level, spec: GenerationValue) -> None:
        """Fold one level's declaration into the running state."""
        self.narrow_bounds(spec)

        if self.locked_at is not None:
            # A lock binds every level below it; bounds still narrow.
            return

        declared = spec.value if spec.value is not None else spec.default
        if declared is not None:
            self.value = declared
            self.source = level
            self.seen = True

        if spec.policy != "default":
            self.policy = spec.policy
        if spec.policy == "locked":
            self.locked_at = level
            self.policy = "locked"

    def finish(self) -> ResolvedValue:
        return ResolvedValue(
            name=self.name,
            value=self.value,
            source=self.source,
            policy=self.policy,
            locked_at=self.locked_at,
            min=self.min,
            max=self.max,
        )


def _as_number(parameter: str, raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ParameterRejected(
            parameter, f"{parameter} must be numeric to be bounded, got {raw!r}"
        )
    return float(raw)


def _apply_request(state: _Accumulator, raw: Any) -> ResolvedValue:
    """Fold the client's own value in, honouring the parameter's policy."""
    resolved = state.finish()

    if state.locked_at is not None:
        # Locked: the request is ignored, not an error.
        return resolved

    bounded = state.min is not None or state.max is not None

    if state.policy == "clamp" and bounded:
        number = _as_number(state.name, raw)
        clamped = number
        if state.min is not None:
            clamped = max(clamped, state.min)
        if state.max is not None:
            clamped = min(clamped, state.max)
        return ResolvedValue(
            name=state.name,
            value=clamped,
            source=Level.REQUEST,
            policy=state.policy,
            min=state.min,
            max=state.max,
            clamped_from=raw if clamped != number else None,
        )

    if bounded:
        number = _as_number(state.name, raw)
        if state.min is not None and number < state.min:
            raise ParameterRejected(
                state.name,
                f"{state.name} must be at least {state.min}, got {raw!r} "
                f"(bound set at {state.source.label} level)",
                source=state.source,
            )
        if state.max is not None and number > state.max:
            raise ParameterRejected(
                state.name,
                f"{state.name} must be at most {state.max}, got {raw!r} "
                f"(bound set at {state.source.label} level)",
                source=state.source,
            )

    return ResolvedValue(
        name=state.name,
        value=raw,
        source=Level.REQUEST,
        policy=state.policy,
        min=state.min,
        max=state.max,
    )


def resolve(
    layers: Sequence[ProfileLayer],
    *,
    request: Mapping[str, Any] | None = None,
) -> EffectiveParams:
    """Resolve every parameter across the precedence chain.

    Raises `ParameterRejected` when the request asks for a value its
    bounds forbid under a `default` policy.
    """
    seen_levels: set[Level] = set()
    for one in layers:
        if one.level in seen_levels:
            raise ValueError(
                f"more than one profile bound at the {one.level.label} level; "
                "exactly one profile applies per level"
            )
        seen_levels.add(one.level)

    ordered = sorted(layers, key=lambda one: one.level)

    states: dict[str, _Accumulator] = {}
    for one in ordered:
        for name, spec in one.params.items():
            state = states.setdefault(name, _Accumulator(name=name, source=one.level))
            state.apply(one.level, spec)

    resolved: dict[str, ResolvedValue] = {}
    request = request or {}

    for name, state in states.items():
        if name in request and request[name] is not None:
            resolved[name] = _apply_request(state, request[name])
        elif state.seen:
            resolved[name] = state.finish()

    # Parameters no profile mentions pass through; profiles govern what
    # they cover, they are not an allow-list of what a client may send.
    for name, raw in request.items():
        if name not in resolved and raw is not None:
            resolved[name] = ResolvedValue(name=name, value=raw, source=Level.REQUEST)

    return EffectiveParams(entries=resolved)
