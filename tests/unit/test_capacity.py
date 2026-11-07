"""Capacity sizing: weights + KV cache + 10% headroom (SPEC.md sections 3 and 4)."""

import pytest

from aisvc.capacity import (
    GpuRequirement,
    KvBudget,
    ModelArchitecture,
    bytes_per_param,
    estimate,
    kv_budget,
)

LLAMA_70B = ModelArchitecture(
    params_b=70.6,
    num_layers=80,
    num_kv_heads=8,
    head_dim=128,
)

LLAMA_8B = ModelArchitecture(
    params_b=8.03,
    num_layers=32,
    num_kv_heads=8,
    head_dim=128,
)


class TestBytesPerParam:
    @pytest.mark.parametrize(
        ("quantization", "expected"),
        [
            ("fp32", 4.0),
            ("bf16", 2.0),
            ("fp16", 2.0),
            ("fp8", 1.0),
            ("int8", 1.0),
            ("int4", 0.5),
            ("awq", 0.5),
            ("gptq", 0.5),
        ],
    )
    def test_known_quantizations(self, quantization: str, expected: float) -> None:
        assert bytes_per_param(quantization) == expected

    def test_unknown_quantization_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown quantization 'fp3'"):
            bytes_per_param("fp3")


class TestEstimate:
    def test_weights_are_params_times_bytes_per_param(self) -> None:
        req = estimate(
            LLAMA_70B,
            quantization="fp8",
            kv_cache_dtype="fp8",
            max_model_len=32768,
            tensor_parallel=2,
            gpu_mem_gb=80,
        )
        # 70.6e9 params x 1 byte = 70.6 GB = 65.75 GiB
        assert req.weights_gb == pytest.approx(65.75, abs=0.05)

    def test_kv_cache_is_sized_for_one_sequence_at_full_context(self) -> None:
        req = estimate(
            LLAMA_70B,
            quantization="fp8",
            kv_cache_dtype="fp8",
            max_model_len=32768,
            tensor_parallel=2,
            gpu_mem_gb=80,
        )
        # 2 (K+V) x 80 layers x 8 kv heads x 128 head_dim x 1 byte = 160 KiB/token
        # x 32768 tokens = 5 GiB
        assert req.kv_cache_gb == pytest.approx(5.0, abs=0.01)

    def test_headroom_is_ten_percent_of_weights_plus_kv(self) -> None:
        req = estimate(
            LLAMA_70B,
            quantization="fp8",
            kv_cache_dtype="fp8",
            max_model_len=32768,
            tensor_parallel=2,
            gpu_mem_gb=80,
        )
        assert req.headroom_gb == pytest.approx(0.10 * (req.weights_gb + req.kv_cache_gb))
        assert req.total_gb == pytest.approx(req.weights_gb + req.kv_cache_gb + req.headroom_gb)

    def test_available_memory_applies_the_utilization_factor(self) -> None:
        req = estimate(
            LLAMA_70B,
            quantization="fp8",
            kv_cache_dtype="fp8",
            max_model_len=32768,
            tensor_parallel=2,
            gpu_mem_gb=80,
        )
        assert req.available_gb == pytest.approx(2 * 80 * 0.9)

    def test_70b_fp8_fits_on_two_h100s(self) -> None:
        req = estimate(
            LLAMA_70B,
            quantization="fp8",
            kv_cache_dtype="fp8",
            max_model_len=32768,
            tensor_parallel=2,
            gpu_mem_gb=80,
        )
        assert req.fits
        assert req.shortfall_gb == 0.0

    def test_capacity_rejects_model_that_cannot_fit(self) -> None:
        """SPEC.md section 12: blocks impossible specs with the GB shortfall."""
        req = estimate(
            LLAMA_70B,
            quantization="bf16",
            kv_cache_dtype="fp16",
            max_model_len=32768,
            tensor_parallel=1,
            gpu_mem_gb=48,
        )
        assert not req.fits
        assert req.shortfall_gb > 0
        # 70.6e9 x 2 = 131.5 GiB weights alone against 43.2 GiB available
        assert req.shortfall_gb == pytest.approx(req.total_gb - req.available_gb)

    def test_shortfall_is_zero_when_it_fits(self) -> None:
        req = estimate(
            LLAMA_8B,
            quantization="bf16",
            kv_cache_dtype="fp16",
            max_model_len=8192,
            tensor_parallel=1,
            gpu_mem_gb=48,
        )
        assert req.fits
        assert req.shortfall_gb == 0.0

    def test_tensor_parallel_multiplies_available_memory(self) -> None:
        single = estimate(
            LLAMA_70B,
            quantization="bf16",
            kv_cache_dtype="fp16",
            max_model_len=8192,
            tensor_parallel=1,
            gpu_mem_gb=80,
        )
        quad = estimate(
            LLAMA_70B,
            quantization="bf16",
            kv_cache_dtype="fp16",
            max_model_len=8192,
            tensor_parallel=4,
            gpu_mem_gb=80,
        )
        assert not single.fits
        assert quad.fits
        assert quad.available_gb == 4 * single.available_gb

    def test_requirement_is_immutable(self) -> None:
        req = estimate(
            LLAMA_8B,
            quantization="bf16",
            kv_cache_dtype="fp16",
            max_model_len=8192,
            tensor_parallel=1,
            gpu_mem_gb=48,
        )
        assert isinstance(req, GpuRequirement)
        with pytest.raises((AttributeError, TypeError)):
            req.weights_gb = 1.0  # type: ignore[misc]

    def test_rejects_non_positive_context_length(self) -> None:
        with pytest.raises(ValueError, match="max_model_len must be positive"):
            estimate(
                LLAMA_8B,
                quantization="bf16",
                kv_cache_dtype="fp16",
                max_model_len=0,
                tensor_parallel=1,
                gpu_mem_gb=48,
            )

    def test_rejects_non_positive_tensor_parallel(self) -> None:
        with pytest.raises(ValueError, match="tensor_parallel must be positive"):
            estimate(
                LLAMA_8B,
                quantization="bf16",
                kv_cache_dtype="fp16",
                max_model_len=8192,
                tensor_parallel=0,
                gpu_mem_gb=48,
            )


class TestKvBudget:
    def test_budget_is_what_is_left_after_weights_and_headroom(self) -> None:
        budget = kv_budget(
            LLAMA_70B,
            quantization="fp8",
            kv_cache_dtype="fp8",
            tensor_parallel=2,
            gpu_mem_gb=80,
        )
        assert isinstance(budget, KvBudget)
        # Inverts the headroom rule: 1.1 x (weights + kv) <= 144 GiB
        assert budget.kv_cache_gb == pytest.approx(144.0 / 1.1 - 65.75, abs=0.2)

    def test_reports_how_many_full_context_sequences_fit(self) -> None:
        budget = kv_budget(
            LLAMA_70B,
            quantization="fp8",
            kv_cache_dtype="fp8",
            tensor_parallel=2,
            gpu_mem_gb=80,
        )
        # 160 KiB/token; 32768-token sequences cost 5 GiB each
        assert budget.max_full_context_seqs(32768) == int(budget.kv_cache_gb // 5.0)

    def test_no_budget_when_weights_do_not_fit(self) -> None:
        budget = kv_budget(
            LLAMA_70B,
            quantization="bf16",
            kv_cache_dtype="fp16",
            tensor_parallel=1,
            gpu_mem_gb=48,
        )
        assert budget.kv_cache_gb == 0.0
        assert budget.max_full_context_seqs(8192) == 0
