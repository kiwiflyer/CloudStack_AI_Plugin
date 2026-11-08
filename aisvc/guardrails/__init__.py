"""Guardrail profiles (SPEC.md sections 6, 8 and 11).

The third profile type, with section 6's scopes and lock semantics: a
school's domain admin locks a safe profile and no account or key
beneath it can turn a check off.

**A block records the category and nothing else.** Not the prompt, not
the matched phrase, not a snippet "for context". A guardrail sees every
prompt that is most likely to be sensitive, so a log of what it blocked
would be the richest collection of tenant content on the platform —
exactly what section 11 forbids. `GuardrailDecision` therefore keeps no
prompt text at all, and its `__repr__` is safe to put in a traceback.

What is here is the cheap, deterministic half: input size, topic
deny-lists and PII patterns. The guard model in section 8 is served
internally like any other model, and calling it belongs with the
gateway hook — this module decides *what* to ask of it and what to do
with the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from aisvc.profiles import Level
from aisvc.spec.models import GuardrailCheck

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "EDUCATION_SAFE",
    "Action",
    "GuardrailDecision",
    "GuardrailLayer",
    "ResolvedCheck",
    "check_input",
    "presets",
    "resolve_guardrails",
]

EDUCATION_SAFE = "education-safe"


class Action(StrEnum):
    BLOCK = "block"
    REDACT = "redact"
    FLAG = "flag"


@dataclass(frozen=True, slots=True)
class GuardrailLayer:
    """One guardrail profile bound at one precedence level."""

    level: Level
    checks: Mapping[str, GuardrailCheck]
    name: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedCheck:
    """One check after the precedence chain has been applied."""

    name: str
    action: Action
    value: Any = None
    source: Level = Level.PLATFORM
    locked: bool = False


def resolve_guardrails(layers: Sequence[GuardrailLayer]) -> dict[str, ResolvedCheck]:
    """Resolve checks across the precedence chain (SPEC.md section 6).

    Same shape as `aisvc.profiles.resolve`, and deliberately not shared
    with it: a generation parameter resolves a *value* within bounds,
    while a check resolves an *action*, and folding them together would
    mean one set of policy rules doing two different jobs.
    """
    seen: set[Level] = set()
    for layer in layers:
        if layer.level in seen:
            raise ValueError(
                f"more than one guardrail profile bound at the {layer.level.label} "
                "level; exactly one profile applies per level"
            )
        seen.add(layer.level)

    resolved: dict[str, ResolvedCheck] = {}
    for layer in sorted(layers, key=lambda one: one.level):
        for name, spec in layer.checks.items():
            existing = resolved.get(name)
            if existing is not None and existing.locked:
                # Locked above: nothing below may weaken or change it.
                continue
            resolved[name] = ResolvedCheck(
                name=name,
                action=Action(spec.action or "flag"),
                value=spec.value,
                source=layer.level,
                locked=spec.policy == "locked",
            )
    return resolved


@dataclass(frozen=True)
class GuardrailDecision:
    """What the guardrails concluded. Carries no prompt text.

    `redacted` is the only field that can hold anything derived from
    the prompt, and it exists to be sent upstream rather than logged.
    """

    allowed: bool = True
    category: str = ""
    action: Action | None = None
    flagged: list[str] = field(default_factory=list)
    redacted: str | None = field(default=None, repr=False)

    def as_event(self) -> dict[str, Any]:
        """What is recorded when a guardrail fires.

        Category and action only (SPEC.md section 11).
        """
        return {
            "category": self.category,
            "action": self.action.value if self.action else "",
            "at": datetime.now(UTC).isoformat(),
        }


#: Patterns that are specific enough not to fire on ordinary prose.
#: Deliberately conservative: a false positive redacts something a
#: tenant meant to send, which they experience as the model ignoring
#: them.
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("card", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    (
        "phone",
        re.compile(r"\+\d{1,3}[\s-]?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}\b"),
    ),
)


def _redact_pii(text: str) -> tuple[str, bool]:
    redacted = text
    found = False
    for label, pattern in PII_PATTERNS:
        redacted, count = pattern.subn(f"[redacted:{label}]", redacted)
        found = found or count > 0
    return redacted, found


def check_input(text: str, checks: Mapping[str, ResolvedCheck]) -> GuardrailDecision:
    """Apply the resolved checks to one request's input.

    Ordered cheapest first: a prompt rejected for size never reaches the
    pattern matching, and nothing here reaches the guard model.
    """
    flagged: list[str] = []

    size = checks.get("input_size")
    if size is not None and isinstance(size.value, int | float) and len(text) > size.value:
        if size.action is Action.BLOCK:
            return GuardrailDecision(allowed=False, category="input_size", action=size.action)
        flagged.append("input_size")

    topics = checks.get("topics")
    if topics is not None and isinstance(topics.value, list | tuple):
        lowered = text.lower()
        # Word boundaries, so a deny-list entry cannot fire on a
        # substring of an innocent word.
        hit = any(
            re.search(rf"\b{re.escape(str(topic).lower())}\b", lowered)
            for topic in topics.value
            if str(topic)
        )
        if hit:
            if topics.action is Action.BLOCK:
                return GuardrailDecision(allowed=False, category="topics", action=topics.action)
            flagged.append("topics")

    pii = checks.get("pii")
    redacted: str | None = None
    if pii is not None:
        cleaned, found = _redact_pii(text)
        if found:
            if pii.action is Action.BLOCK:
                return GuardrailDecision(allowed=False, category="pii", action=pii.action)
            if pii.action is Action.REDACT:
                redacted = cleaned
            else:
                flagged.append("pii")

    return GuardrailDecision(allowed=True, flagged=flagged, redacted=redacted)


def presets() -> dict[str, dict[str, GuardrailCheck]]:
    """Profiles that ship with the platform (SPEC.md section 8).

    The education-safe one is locked throughout: a school sets it on
    their domain, and an account beneath must not be able to relax it,
    which is the whole reason they are buying it.
    """
    return {
        EDUCATION_SAFE: {
            "pii": GuardrailCheck(action="block", policy="locked"),
            "topics": GuardrailCheck(
                action="block",
                policy="locked",
                value=[
                    "self-harm",
                    "suicide",
                    "weapons",
                    "explicit",
                    "drugs",
                ],
            ),
            "input_size": GuardrailCheck(action="block", policy="locked", value=8000),
        }
    }
