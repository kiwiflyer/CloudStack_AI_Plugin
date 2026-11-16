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
"""Embeddings, rerankers and vision models (SPEC.md section 8).

v1 served chat and sized everything as if it generated text. The other
three tasks do not:

- An embedding or rerank model produces a fixed-size output per input
  and grows no KV cache across a generation, so sizing it with the chat
  formula overestimates badly — which is exactly why section 8 can say
  these pack onto L40S, vGPU and MIG classes.
- Pricing follows: there are no output tokens to charge for.
- A vision model generates like chat, but its input is not only text.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisvc.capacity import GpuRequirement, ModelArchitecture, estimate
from aisvc.metering import UsageRecord, price_usage
from aisvc.spec.models import PriceTier
from aisvc.tasks import (
    accepts_images,
    endpoint_for,
    generates_output,
    kv_cache_multiplier,
    validate_usage,
)

TIER = PriceTier(name="t", input_per_mtok=0.10, output_per_mtok=0.30, currency="USD")


class TestEndpoints:
    @pytest.mark.parametrize(
        ("task", "path"),
        [
            ("chat", "/v1/chat/completions"),
            ("vision-chat", "/v1/chat/completions"),
            ("embedding", "/v1/embeddings"),
            ("rerank", "/v1/rerank"),
        ],
    )
    def test_each_task_has_its_openai_endpoint(self, task: str, path: str) -> None:
        assert endpoint_for(task) == path

    def test_vision_shares_the_chat_endpoint(self) -> None:
        """OpenAI's API takes images inside a chat request; a separate
        path would make every existing client wrong."""
        assert endpoint_for("vision-chat") == endpoint_for("chat")

    def test_an_unknown_task_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(ValueError, match="task"):
            endpoint_for("summarisation")


class TestGeneration:
    @pytest.mark.parametrize("task", ["chat", "vision-chat"])
    def test_chat_tasks_generate(self, task: str) -> None:
        assert generates_output(task)

    @pytest.mark.parametrize("task", ["embedding", "rerank"])
    def test_the_others_do_not(self, task: str) -> None:
        assert not generates_output(task)

    def test_only_vision_accepts_images(self) -> None:
        assert accepts_images("vision-chat")
        for task in ("chat", "embedding", "rerank"):
            assert not accepts_images(task)


class TestSizing:
    def test_a_generating_task_needs_its_kv_cache(self) -> None:
        assert kv_cache_multiplier("chat") == 1.0

    def test_an_embedding_model_needs_far_less(self) -> None:
        """One forward pass per input, no cache growing across tokens."""
        assert kv_cache_multiplier("embedding") < 1.0

    def test_rerank_is_sized_like_embedding(self) -> None:
        assert kv_cache_multiplier("rerank") == kv_cache_multiplier("embedding")

    def test_vision_is_sized_like_chat(self) -> None:
        """It generates text; the image only enlarges the prompt."""
        assert kv_cache_multiplier("vision-chat") == kv_cache_multiplier("chat")

    def test_the_multiplier_is_never_zero(self) -> None:
        """A single forward pass still needs working memory; zero would
        let the planner pack models onto a GPU that cannot hold them."""
        for task in ("chat", "embedding", "rerank", "vision-chat"):
            assert kv_cache_multiplier(task) > 0


class TestUsageMatchesTheTask:
    def usage(self, **kw: Any) -> UsageRecord:
        defaults: dict[str, Any] = {
            "account": "acme",
            "domain": "/d",
            "model": "bge",
            "input_tokens": 1000,
            "output_tokens": 0,
        }
        defaults.update(kw)
        return UsageRecord(**defaults)

    def test_embedding_usage_has_no_output_tokens(self) -> None:
        assert validate_usage(self.usage(), task="embedding")

    def test_embedding_usage_with_output_tokens_is_a_bug(self) -> None:
        """Section 8 prices these per input token. Output tokens here
        mean something upstream is reporting chat usage for an
        embedding, and billing for them would overcharge."""
        with pytest.raises(ValueError, match="embedding"):
            validate_usage(self.usage(output_tokens=50), task="embedding")

    def test_chat_usage_may_have_both(self) -> None:
        assert validate_usage(self.usage(output_tokens=50), task="chat")

    def test_chat_usage_with_no_output_is_allowed(self) -> None:
        """An empty completion is unusual, not impossible."""
        assert validate_usage(self.usage(output_tokens=0), task="chat")

    def test_rerank_usage_has_no_output_either(self) -> None:
        with pytest.raises(ValueError, match="rerank"):
            validate_usage(self.usage(output_tokens=1), task="rerank")


class TestPricing:
    def test_an_embedding_is_priced_on_its_input_alone(self) -> None:
        usage = UsageRecord(
            account="a",
            domain="/d",
            model="bge",
            input_tokens=1_000_000,
            output_tokens=0,
        )

        assert price_usage(usage, TIER) == pytest.approx(0.10)


class TestCapacityUsesTheTask:
    """The multiplier has to reach `estimate`, or an embedding model is
    still sized as if it generated and still will not fit on an L40S."""

    def arch(self) -> ModelArchitecture:
        return ModelArchitecture(params_b=7.0, num_layers=32, num_kv_heads=8, head_dim=128)

    def estimate_for(self, task: str, **kw: Any) -> GpuRequirement:
        defaults: dict[str, Any] = {
            "quantization": "bf16",
            "kv_cache_dtype": "fp16",
            "max_model_len": 8192,
            "tensor_parallel": 1,
            "gpu_mem_gb": 48.0,
            "task": task,
        }
        defaults.update(kw)
        return estimate(self.arch(), **defaults)

    def test_an_embedding_model_reserves_less_than_a_chat_model(self) -> None:
        assert self.estimate_for("embedding").kv_cache_gb < self.estimate_for("chat").kv_cache_gb

    def test_the_weights_are_the_same_either_way(self) -> None:
        """Only the cache differs; the parameters are the parameters."""
        assert self.estimate_for("embedding").weights_gb == pytest.approx(
            self.estimate_for("chat").weights_gb
        )

    def test_chat_is_unchanged_by_the_task_parameter(self) -> None:
        """v1 behaviour must not move: `chat` is the multiplier of 1.0."""
        without = estimate(
            self.arch(),
            quantization="bf16",
            kv_cache_dtype="fp16",
            max_model_len=8192,
            tensor_parallel=1,
            gpu_mem_gb=48.0,
        )

        assert self.estimate_for("chat").total_gb == pytest.approx(without.total_gb)

    def test_an_embedding_model_fits_where_a_chat_model_does_not(self) -> None:
        """The point of section 8's claim that these pack onto smaller
        classes. A 24 GB card holds the same weights either way; it is
        the cache that decides."""
        chat = self.estimate_for("chat", max_model_len=131072, gpu_mem_gb=24.0)
        embedding = self.estimate_for("embedding", max_model_len=131072, gpu_mem_gb=24.0)

        assert not chat.fits
        assert embedding.fits
