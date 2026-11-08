"""What each catalog task implies (SPEC.md section 8).

v1 served `chat` and treated it as the only shape there was: everything
was sized for a growing KV cache and priced on input plus output. The
other three tasks break both assumptions, and they break them in the
same places — `capacity`, `gateway` and `metering` — so the facts live
here once rather than being re-derived in each.

The sizing difference is the one that matters commercially. An
embedding or rerank model runs one forward pass per input and keeps no
cache across a generation, so sizing it with the chat formula reserves
memory it will never use. That overestimate is the only reason section
8's "small embedding and rerank models can be packed onto L40S, vGPU or
MIG classes" would otherwise be impossible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aisvc.metering import UsageRecord

__all__ = [
    "accepts_images",
    "endpoint_for",
    "generates_output",
    "kv_cache_multiplier",
    "validate_usage",
]

#: Where the gateway exposes each task. Vision shares chat's path
#: deliberately: OpenAI carries images inside a chat request, and a
#: path of its own would make every existing client wrong.
ENDPOINTS = {
    "chat": "/v1/chat/completions",
    "vision-chat": "/v1/chat/completions",
    "embedding": "/v1/embeddings",
    "rerank": "/v1/rerank",
}

#: Tasks that produce tokens rather than a fixed-size result.
GENERATING = frozenset({"chat", "vision-chat"})

#: Fraction of the chat KV-cache budget a task actually needs.
#: Non-generating tasks still need working memory for one forward pass
#: — never zero, or the planner would pack models onto a GPU that
#: cannot hold them — but nothing like a full generation's cache.
KV_CACHE_MULTIPLIERS = {
    "chat": 1.0,
    "vision-chat": 1.0,
    "embedding": 0.15,
    "rerank": 0.15,
}


def _known(task: str) -> str:
    if task not in ENDPOINTS:
        raise ValueError(f"unknown task {task!r}; expected one of {', '.join(sorted(ENDPOINTS))}")
    return task


def endpoint_for(task: str) -> str:
    """The OpenAI-compatible path this task is served on."""
    return ENDPOINTS[_known(task)]


def generates_output(task: str) -> bool:
    """Whether this task produces output tokens to charge for."""
    return _known(task) in GENERATING


def accepts_images(task: str) -> bool:
    """Whether image parts are allowed in a request's content."""
    return _known(task) == "vision-chat"


def kv_cache_multiplier(task: str) -> float:
    """How much of a chat model's KV budget this task needs."""
    return KV_CACHE_MULTIPLIERS[_known(task)]


def validate_usage(usage: UsageRecord, *, task: str) -> bool:
    """Check a usage record is the shape its task can produce.

    A non-generating task reporting output tokens means something
    upstream is metering it as chat, and section 8 prices these per
    input token — so billing for those tokens would overcharge the
    tenant for work that never happened.
    """
    if not generates_output(task) and usage.output_tokens:
        raise ValueError(
            f"a {task} model produced {usage.output_tokens} output tokens, which "
            f"it cannot do; {task} usage is priced on input alone, so this would "
            "overcharge"
        )
    return True
