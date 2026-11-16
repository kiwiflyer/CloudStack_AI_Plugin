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
"""Cross-reference, supply-chain and capacity validation (SPEC.md section 3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from aisvc.capacity import (
    DEFAULT_UTILIZATION,
    MIN_BLOCK_TOKENS,
    ModelArchitecture,
    estimate,
    kv_budget,
)
from aisvc.spec import engines
from aisvc.spec.models import (
    API_VERSION,
    GenerationProfile,
    GpuClass,
    Model,
    NodePool,
    PlatformDocument,
    ServingProfile,
    Tenant,
    Zone,
)

__all__ = ["PlatformSpec", "Problem", "SpecError", "validate"]


@dataclass(frozen=True, slots=True)
class Problem:
    """One validation failure, with the spec path that caused it."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class SpecError(Exception):
    """platform.yaml is invalid; `problems` lists every failure found."""

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{len(problems)} problem(s) in the platform spec:\n{body}")


@dataclass(frozen=True)
class PlatformSpec:
    """A validated platform.yaml, plus any non-fatal warnings."""

    document: PlatformDocument
    warnings: list[str] = field(default_factory=list)
    #: Informational findings: true, worth printing, but nothing to fix.
    notes: list[str] = field(default_factory=list)

    @property
    def api_version(self) -> str:
        return self.document.api_version

    @property
    def zones(self) -> list[Zone]:
        return list(self.document.zones)

    @property
    def models(self) -> list[Model]:
        return list(self.document.models)

    @property
    def node_pools(self) -> list[NodePool]:
        return list(self.document.node_pools)

    @property
    def gpu_classes(self) -> list[GpuClass]:
        return list(self.document.gpu_classes)

    @property
    def tenants(self) -> list[Tenant]:
        return list(self.document.tenants)

    def node_pool(self, name: str) -> NodePool:
        for pool in self.document.node_pools:
            if pool.name == name:
                return pool
        raise KeyError(f"no node pool named {name!r}")

    def gpu_class(self, name: str) -> GpuClass:
        for klass in self.document.gpu_classes:
            if klass.name == name:
                return klass
        raise KeyError(f"no gpu class named {name!r}")

    def model(self, name: str) -> Model:
        for model in self.document.models:
            if model.name == name:
                return model
        raise KeyError(f"no model named {name!r}")

    def serving_profile_for(self, model: Model) -> ServingProfile | None:
        for profile in self.document.tuning_profiles.serving:
            if profile.name == model.serving_profile:
                return profile
        return None


def _is_power_of_two(n: int) -> bool:
    return n > 0 and n & (n - 1) == 0


class _Validator:
    def __init__(self, doc: PlatformDocument, allowed_licenses: list[str]) -> None:
        self.doc = doc
        self.allowed_licenses = {lic.lower() for lic in allowed_licenses}
        self.problems: list[Problem] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

        self.zones = {z.name for z in doc.zones}
        self.classes = {c.name: c for c in doc.gpu_classes}
        self.pools = {p.name: p for p in doc.node_pools}
        self.model_names = {m.name for m in doc.models}
        self.price_tiers = {t.name for t in doc.price_tiers}
        self.serving_profiles = {p.name: p for p in doc.tuning_profiles.serving}
        self.generation_profiles = {p.name: p for p in doc.tuning_profiles.generation}
        self.guardrail_profiles = {p.name for p in doc.tuning_profiles.guardrail}
        self.tenant_names = {t.name for t in doc.tenants}

    def fail(self, path: str, message: str) -> None:
        self.problems.append(Problem(path, message))

    # -- individual rule groups ------------------------------------------

    def check_api_version(self) -> None:
        if self.doc.api_version != API_VERSION:
            self.fail(
                "apiVersion",
                f"expected {API_VERSION!r}, got {self.doc.api_version!r}",
            )

    def check_duplicates(self) -> None:
        for label, names in (
            ("zones", [z.name for z in self.doc.zones]),
            ("gpuClasses", [c.name for c in self.doc.gpu_classes]),
            ("nodePools", [p.name for p in self.doc.node_pools]),
            ("models", [m.name for m in self.doc.models]),
            ("priceTiers", [t.name for t in self.doc.price_tiers]),
            ("tenants", [t.name for t in self.doc.tenants]),
        ):
            seen: set[str] = set()
            for name in names:
                if name in seen:
                    self.fail(label, f"duplicate name {name!r}")
                seen.add(name)

    def check_pools(self) -> None:
        for i, pool in enumerate(self.doc.node_pools):
            path = f"nodePools[{i}]"
            if pool.zone not in self.zones:
                self.fail(f"{path}.zone", f"unknown zone {pool.zone!r}")
            if pool.gpu_class not in self.classes:
                self.fail(f"{path}.gpu_class", f"unknown gpu class {pool.gpu_class!r}")
            if pool.max_nodes < pool.min_nodes:
                self.fail(
                    f"{path}.max_nodes",
                    f"max_nodes {pool.max_nodes} is below min_nodes {pool.min_nodes}",
                )
            if pool.tenant is not None and pool.tenant not in self.tenant_names:
                self.fail(f"{path}.tenant", f"unknown tenant {pool.tenant!r}")

    def check_models(self) -> None:
        for i, model in enumerate(self.doc.models):
            path = f"models[{i}]"
            self._check_model_refs(model, path)
            self._check_model_source(model, path)
            self._check_model_capacity(model, path)

    def _check_model_refs(self, model: Model, path: str) -> None:
        if model.node_pool not in self.pools:
            self.fail(f"{path}.node_pool", f"unknown node pool {model.node_pool!r}")
        if model.price_tier is not None and model.price_tier not in self.price_tiers:
            self.fail(f"{path}.price_tier", f"unknown price tier {model.price_tier!r}")
        if model.serving_profile is not None and model.serving_profile not in self.serving_profiles:
            self.fail(
                f"{path}.serving_profile",
                f"unknown serving profile {model.serving_profile!r}",
            )
        if model.replicas.max < model.replicas.min:
            self.fail(
                f"{path}.replicas",
                f"max {model.replicas.max} is below min {model.replicas.min}",
            )

    def _check_model_source(self, model: Model, path: str) -> None:
        if not model.source_is_pinned:
            self.fail(
                f"{path}.source",
                f"{model.source!r} must pin a 40-character commit SHA, "
                "as in hf://org/repo@<commit SHA>",
            )
        if model.license.lower() not in self.allowed_licenses:
            self.fail(
                f"{path}.license",
                f"license {model.license!r} is not on the allow-list",
            )

    def _gpu_class_of(self, model: Model) -> GpuClass | None:
        pool = self.pools.get(model.node_pool)
        if pool is None:
            return None
        return self.classes.get(pool.gpu_class)

    def _check_model_capacity(self, model: Model, path: str) -> None:
        gpu_class = self._gpu_class_of(model)
        if gpu_class is None:
            return  # the unresolved reference is already reported

        if not _is_power_of_two(model.tensor_parallel):
            self.fail(
                f"{path}.tensor_parallel",
                f"{model.tensor_parallel} must be a power of two",
            )
        if model.tensor_parallel > gpu_class.gpus_per_node:
            self.fail(
                f"{path}.tensor_parallel",
                f"{model.tensor_parallel} exceeds gpu class {gpu_class.name!r} "
                f"gpus_per_node {gpu_class.gpus_per_node}",
            )
            return

        if model.architecture is None:
            self.warnings.append(
                f"{path}: no architecture facts, so capacity was not checked; "
                "they are filled in when the model is mirrored"
            )
            return

        arch = ModelArchitecture(
            params_b=model.architecture.params_b,
            num_layers=model.architecture.num_layers,
            num_kv_heads=model.architecture.num_kv_heads,
            head_dim=model.architecture.head_dim,
        )
        profile = self.serving_profiles.get(model.serving_profile or "")
        params: dict[str, object] = dict(profile.params) if profile else {}

        kv_dtype = str(params.get(engines.KV_DTYPE_KEY.get(model.engine, ""), "fp16"))
        utilization = float(
            params.get(  # type: ignore[arg-type]
                engines.UTILIZATION_KEY.get(model.engine, ""), DEFAULT_UTILIZATION
            )
        )

        try:
            requirement = estimate(
                arch,
                quantization=model.quantization,
                kv_cache_dtype=kv_dtype,
                max_model_len=model.max_model_len,
                tensor_parallel=model.tensor_parallel,
                gpu_mem_gb=gpu_class.gpu_mem_gb,
                utilization=utilization,
            )
        except ValueError as exc:
            self.fail(path, str(exc))
            return

        if not requirement.fits:
            self.fail(
                path,
                f"does not fit on {model.tensor_parallel} x {gpu_class.name}: "
                f"{requirement.describe()}",
            )
            return

        self._check_concurrency(model, path, arch, gpu_class, params, kv_dtype, utilization)

    def _check_concurrency(
        self,
        model: Model,
        path: str,
        arch: ModelArchitecture,
        gpu_class: GpuClass,
        params: dict[str, object],
        kv_dtype: str,
        utilization: float,
    ) -> None:
        """Check the concurrency cap against the KV budget.

        SPEC.md section 3 asks that `max_model_len x max_num_seqs` fit the
        budget. That reads the cap as a reservation, which a paged engine
        never makes: KV blocks are allocated per token as sequences grow,
        and the scheduler queues whatever does not fit. Taken literally the
        rule rejects SPEC.md's own example, so it is applied in two parts:

        * a hard floor, since a cap that cannot be given one block per
          sequence can never be approached at any request length; and
        * the declared workload, `concurrency.working_context_tokens`,
          which turns the cap into something checkable. Undeclared, the
          spec reports the context length at which the cap is reachable
          rather than guessing one.
        """
        key = engines.CONCURRENCY_KEY.get(model.engine)
        if key is None or key not in params:
            return

        try:
            requested = int(params[key])  # type: ignore[call-overload]
        except (TypeError, ValueError):
            self.fail(f"{path}.{key}", f"{params[key]!r} is not a whole number")
            return
        if requested < 1:
            self.fail(f"{path}.{key}", f"{requested} must be at least 1")
            return

        budget = kv_budget(
            arch,
            quantization=model.quantization,
            kv_cache_dtype=kv_dtype,
            tensor_parallel=model.tensor_parallel,
            gpu_mem_gb=gpu_class.gpu_mem_gb,
            utilization=utilization,
        )
        available = budget.max_tokens()

        if requested * MIN_BLOCK_TOKENS > available:
            self.fail(
                path,
                f"{key} {requested} can never be approached: the KV budget holds "
                f"{available} tokens ({budget.kv_cache_gb:.1f} GiB), fewer than one "
                f"{MIN_BLOCK_TOKENS}-token block per sequence; lower it to at most "
                f"{available // MIN_BLOCK_TOKENS}",
            )
            return

        declared = model.concurrency.working_context_tokens
        if declared is None:
            self.notes.append(
                f"{path}: {key} {requested} is reachable while sequences average "
                f"{budget.context_for_seqs(requested)} tokens or fewer "
                f"({budget.max_full_context_seqs(model.max_model_len)} at the full "
                f"{model.max_model_len}-token context); set "
                f"concurrency.working_context_tokens to have this checked"
            )
            return

        needed = requested * declared
        if needed > available:
            self.fail(
                path,
                f"{key} {requested} x working_context_tokens {declared} needs "
                f"{needed} tokens of KV cache but only {available} are available "
                f"({budget.kv_cache_gb:.1f} GiB); lower {key} to "
                f"{available // declared} or working_context_tokens to "
                f"{budget.context_for_seqs(requested)}",
            )

    def check_serving_profiles(self) -> None:
        engines_by_profile: dict[str, set[str]] = {}
        for model in self.doc.models:
            if model.serving_profile:
                engines_by_profile.setdefault(model.serving_profile, set()).add(model.engine)

        for i, profile in enumerate(self.doc.tuning_profiles.serving):
            path = f"tuningProfiles.serving[{i}]"
            used_by = engines_by_profile.get(profile.name) or {"vllm"}
            for engine in sorted(used_by):
                allowed = engines.allowed_params(engine)
                for key in sorted(profile.params):
                    if key not in allowed:
                        self.fail(
                            f"{path}.params.{key}",
                            f"{key!r} is not an allow-listed {engine} parameter",
                        )

    def check_generation_profiles(self) -> None:
        contexts = [m.max_model_len for m in self.doc.models]
        largest = max(contexts) if contexts else None

        for i, profile in enumerate(self.doc.tuning_profiles.generation):
            path = f"tuningProfiles.generation[{i}]"
            self._check_generation_bounds(profile, path)
            if largest is not None:
                self._check_generation_context(profile, path, largest)

    def _check_generation_bounds(self, profile: GenerationProfile, path: str) -> None:
        for key, value in profile.params.items():
            where = f"{path}.params.{key}"
            if value.min is not None and value.max is not None and value.min > value.max:
                self.fail(where, f"min {value.min} is above max {value.max}")
            if isinstance(value.default, int | float) and not isinstance(value.default, bool):
                if value.min is not None and value.default < value.min:
                    self.fail(where, f"default {value.default} is below min {value.min}")
                if value.max is not None and value.default > value.max:
                    self.fail(where, f"default {value.default} is above max {value.max}")

    def _check_generation_context(
        self, profile: GenerationProfile, path: str, largest_context: int
    ) -> None:
        value = profile.params.get("max_tokens")
        if value is None:
            return
        for label, candidate in (("max", value.max), ("default", value.default)):
            if isinstance(candidate, int | float) and candidate > largest_context:
                self.fail(
                    f"{path}.params.max_tokens",
                    f"{label} {int(candidate)} exceeds the largest model context "
                    f"length {largest_context}",
                )

    def check_assignments(self) -> None:
        for i, assignment in enumerate(self.doc.assignments):
            path = f"assignments[{i}]"
            if assignment.scope == "deployment" and assignment.target not in self.model_names:
                self.fail(f"{path}.target", f"unknown deployment {assignment.target!r}")
            if (
                assignment.serving_profile
                and assignment.serving_profile not in self.serving_profiles
            ):
                self.fail(
                    f"{path}.serving_profile",
                    f"unknown serving profile {assignment.serving_profile!r}",
                )
            if (
                assignment.generation_profile
                and assignment.generation_profile not in self.generation_profiles
            ):
                self.fail(
                    f"{path}.generation_profile",
                    f"unknown generation profile {assignment.generation_profile!r}",
                )
            if (
                assignment.guardrail_profile
                and assignment.guardrail_profile not in self.guardrail_profiles
            ):
                self.fail(
                    f"{path}.guardrail_profile",
                    f"unknown guardrail profile {assignment.guardrail_profile!r}",
                )

    def check_tenants(self) -> None:
        for i, tenant in enumerate(self.doc.tenants):
            path = f"tenants[{i}]"
            for name in tenant.models:
                if name not in self.model_names:
                    self.fail(f"{path}.models", f"unknown model {name!r}")

            owned = [p for p in self.doc.node_pools if p.tenant == tenant.name]
            if tenant.tier == "dedicated" and not owned:
                self.fail(
                    f"{path}.tier",
                    f"tenant {tenant.name!r} is dedicated but no nodePools entry "
                    f"sets tenant: {tenant.name}",
                )

            if tenant.tier == "shared":
                for name in tenant.models:
                    model = next((m for m in self.doc.models if m.name == name), None)
                    if model is None:
                        continue
                    pool = self.pools.get(model.node_pool)
                    if pool is not None and pool.tenant not in (None, tenant.name):
                        self.fail(
                            f"{path}.models",
                            f"shared tenant {tenant.name!r} uses model {name!r} on "
                            f"pool {pool.name!r}, which is dedicated to "
                            f"{pool.tenant!r}",
                        )

    def run(self) -> PlatformSpec:
        self.check_api_version()
        self.check_duplicates()
        self.check_pools()
        self.check_models()
        self.check_serving_profiles()
        self.check_generation_profiles()
        self.check_assignments()
        self.check_tenants()
        if self.problems:
            raise SpecError(self.problems)
        return PlatformSpec(document=self.doc, warnings=self.warnings, notes=self.notes)


def validate(doc: PlatformDocument, *, allowed_licenses: list[str]) -> PlatformSpec:
    """Apply every cross-cutting rule, raising `SpecError` with all failures."""
    return _Validator(doc, allowed_licenses).run()
