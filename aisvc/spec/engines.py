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
"""Per-engine serving-parameter allow-lists (SPEC.md section 6).

Only allow-listed parameters may appear in a serving profile; everything
else is rejected before it can reach an engine command line.
"""

from __future__ import annotations

VLLM_PARAMS: frozenset[str] = frozenset(
    {
        "block_size",
        "chat_template",
        "cpu_offload_gb",
        "disable_log_stats",
        "dtype",
        "enable_auto_tool_choice",
        "enable_chunked_prefill",
        "enable_lora",
        "enable_prefix_caching",
        "enforce_eager",
        "gpu_memory_utilization",
        "kv_cache_dtype",
        "max_logprobs",
        "max_lora_rank",
        "max_loras",
        "max_model_len",
        "max_num_batched_tokens",
        "max_num_seqs",
        "max_seq_len_to_capture",
        "num_speculative_tokens",
        "quantization",
        "reasoning_parser",
        "seed",
        "speculative_model",
        "swap_space",
        "tensor_parallel_size",
        "tool_call_parser",
        "trust_remote_code",
    }
)

SGLANG_PARAMS: frozenset[str] = frozenset(
    {
        "chat_template",
        "chunked_prefill_size",
        "context_length",
        "disable_radix_cache",
        "dtype",
        "enable_torch_compile",
        "kv_cache_dtype",
        "max_prefill_tokens",
        "max_running_requests",
        "mem_fraction_static",
        "quantization",
        "schedule_policy",
        "tp_size",
    }
)

TRTLLM_PARAMS: frozenset[str] = frozenset(
    {
        "dtype",
        "enable_chunked_context",
        "kv_cache_free_gpu_mem_fraction",
        "max_batch_size",
        "max_input_len",
        "max_num_tokens",
        "max_output_len",
        "quantization",
        "tensor_parallel_size",
        "use_paged_context_fmha",
    }
)

ALLOW_LISTS: dict[str, frozenset[str]] = {
    "vllm": VLLM_PARAMS,
    "sglang": SGLANG_PARAMS,
    "trtllm": TRTLLM_PARAMS,
}


def allowed_params(engine: str) -> frozenset[str]:
    return ALLOW_LISTS.get(engine, frozenset())


#: Serving-profile keys that map onto capacity inputs, per engine.
CONCURRENCY_KEY: dict[str, str] = {
    "vllm": "max_num_seqs",
    "sglang": "max_running_requests",
    "trtllm": "max_batch_size",
}

KV_DTYPE_KEY: dict[str, str] = {
    "vllm": "kv_cache_dtype",
    "sglang": "kv_cache_dtype",
    "trtllm": "dtype",
}

UTILIZATION_KEY: dict[str, str] = {
    "vllm": "gpu_memory_utilization",
    "sglang": "mem_fraction_static",
    "trtllm": "kv_cache_free_gpu_mem_fraction",
}
