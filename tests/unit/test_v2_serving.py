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
"""Serving the v2 model shapes (SPEC.md section 8).

The v2 modules decided things nothing acted on. These are the acting
parts: the gateway has to route an embedding request to an embedding
model, and an adapter has to be addressable as `<base>:<adapter>` and
reach the base that holds it.
"""

from __future__ import annotations

from typing import Any

import yaml

from aisvc.adapters import AdapterRequest
from aisvc.gateway import build_litellm_config
from aisvc.k8s import DeploymentTarget, image_for, render
from aisvc.spec import PlatformSpec, load_spec_from_string
from aisvc.tasks import litellm_mode
from tests.unit.test_spec import LICENSES, MINIMAL

BASE = "llama-3.3-70b-instruct"


def spec_with_tasks(*tasks: str) -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    template = doc["models"][0]
    doc["models"] = []
    for index, task in enumerate(tasks):
        model = dict(template)
        model["name"] = f"model-{index}"
        model["task"] = task
        doc["models"].append(model)
    # The fixture's assignments and tenants name the original model;
    # renaming it would leave them dangling and the spec would not load.
    doc["assignments"] = []
    for tenant in doc.get("tenants", []):
        tenant["models"] = [m["name"] for m in doc["models"]]
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


def spec() -> PlatformSpec:
    return load_spec_from_string(MINIMAL, allowed_licenses=LICENSES)


def entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    model_list: list[dict[str, Any]] = config["model_list"]
    return model_list


def adapter(name: str = "legal-tone", **kw: Any) -> AdapterRequest:
    defaults: dict[str, Any] = {
        "name": name,
        "base_model": BASE,
        "account": "acme",
        "domain": "/Customers/Acme",
        "rank": 16,
        "size_bytes": 1024,
        "files": ("adapter_model.safetensors", "adapter_config.json"),
    }
    defaults.update(kw)
    return AdapterRequest(**defaults)


class TestTaskModes:
    def test_litellm_mode_per_task(self) -> None:
        assert litellm_mode("chat") == "chat"
        assert litellm_mode("vision-chat") == "chat"
        assert litellm_mode("embedding") == "embedding"
        assert litellm_mode("rerank") == "rerank"

    def test_an_embedding_model_is_declared_as_one(self) -> None:
        """Without mode, LiteLLM will not serve it on /v1/embeddings."""
        config = build_litellm_config(spec_with_tasks("embedding"))

        assert entries(config)[0]["model_info"]["mode"] == "embedding"

    def test_a_rerank_model_is_declared_as_one(self) -> None:
        config = build_litellm_config(spec_with_tasks("rerank"))

        assert entries(config)[0]["model_info"]["mode"] == "rerank"

    def test_a_vision_model_is_served_as_chat(self) -> None:
        """Images ride inside a chat request; a mode of its own would
        stop every existing client working."""
        config = build_litellm_config(spec_with_tasks("vision-chat"))

        assert entries(config)[0]["model_info"]["mode"] == "chat"

    def test_mixed_tasks_each_get_their_own_mode(self) -> None:
        config = build_litellm_config(spec_with_tasks("chat", "embedding", "rerank"))

        assert [e["model_info"]["mode"] for e in entries(config)] == ["chat", "embedding", "rerank"]


class TestAdapterRoutes:
    def test_an_adapter_gets_its_own_model_entry(self) -> None:
        config = build_litellm_config(spec(), adapters=[adapter()])

        names = [e["model_name"] for e in entries(config)]
        assert f"{BASE}:legal-tone" in names

    def test_the_base_is_still_served_on_its_own(self) -> None:
        config = build_litellm_config(spec(), adapters=[adapter()])

        assert BASE in [e["model_name"] for e in entries(config)]

    def test_an_adapter_routes_to_its_base_deployment(self) -> None:
        """There is one process; the adapter is loaded inside it."""
        config = build_litellm_config(spec(), adapters=[adapter()])

        base = next(e for e in entries(config) if e["model_name"] == BASE)
        served = next(e for e in entries(config) if e["model_name"] == f"{BASE}:legal-tone")

        assert served["litellm_params"]["api_base"] == base["litellm_params"]["api_base"]

    def test_the_upstream_model_is_the_adapter_name(self) -> None:
        """vLLM registers each --lora-modules entry as a served model
        under its own name, so that is what the base expects to see."""
        config = build_litellm_config(spec(), adapters=[adapter()])

        served = next(e for e in entries(config) if e["model_name"] == f"{BASE}:legal-tone")
        assert served["litellm_params"]["model"] == "openai/legal-tone"

    def test_the_entry_records_who_owns_it(self) -> None:
        """Visibility is enforced at the hook, which needs the owner."""
        config = build_litellm_config(spec(), adapters=[adapter()])

        served = next(e for e in entries(config) if e["model_name"] == f"{BASE}:legal-tone")
        assert served["model_info"]["account"] == "acme"
        assert served["model_info"]["domain"] == "/Customers/Acme"

    def test_an_adapter_for_an_undeployed_base_is_left_out(self) -> None:
        """Registering a route to a base that is not there would answer
        a tenant's request with a connection error."""
        config = build_litellm_config(spec(), adapters=[adapter(base_model="ghost")])

        assert not any(":" in e["model_name"] for e in entries(config))

    def test_no_adapters_renders_as_before(self) -> None:
        assert build_litellm_config(spec()) == build_litellm_config(spec(), adapters=[])

    def test_adapters_follow_the_live_colour(self) -> None:
        """An adapter must not keep pointing at a retired deployment."""
        config = build_litellm_config(
            spec(),
            weights={BASE: {"blue": 0, "green": 100}},
            adapters=[adapter()],
        )

        served = next(e for e in entries(config) if e["model_name"] == f"{BASE}:legal-tone")
        assert "green" in served["litellm_params"]["api_base"]


class TestRenderingLora:
    def target(self, *, adapters: tuple[str, ...] = (), max_rank: int = 32) -> Any:
        loaded = spec()
        model = loaded.models[0]
        pool = loaded.node_pool(model.node_pool)
        return DeploymentTarget(
            model=model,
            pool=pool,
            gpu_class=loaded.gpu_class(pool.gpu_class),
            serving_profile=loaded.serving_profile_for(model),
            image=image_for(model),
            replicas=1,
            weights_uri="s3://models/x/y/",
            colour="blue",
            adapters=tuple({"name": name, "uri": f"s3://adapters/{name}/"} for name in adapters),
            max_lora_rank=max_rank,
        )

    def args_of(self, objects: list[dict[str, Any]]) -> list[str]:
        for obj in objects:
            if obj["kind"] == "Deployment":
                container = obj["spec"]["template"]["spec"]["containers"][0]
                args: list[str] = container.get("args", [])
                return args
        raise AssertionError("no Deployment rendered")

    def test_no_adapters_does_not_enable_lora(self) -> None:
        """--enable-lora costs memory; it should not be on by default."""
        args = self.args_of(render(self.target()))

        assert "--enable-lora" not in args

    def test_adapters_enable_lora(self) -> None:
        args = self.args_of(render(self.target(adapters=("legal-tone",))))

        assert "--enable-lora" in args

    def test_each_adapter_is_registered_by_name(self) -> None:
        args = self.args_of(render(self.target(adapters=("legal-tone", "support"))))
        joined = " ".join(args)

        assert "legal-tone=" in joined
        assert "support=" in joined

    def test_the_max_rank_is_passed_to_vllm(self) -> None:
        """It has to match what adapters were validated against, or a
        valid adapter fails to load at request time."""
        args = self.args_of(render(self.target(adapters=("a",), max_rank=64)))

        assert "--max-lora-rank" in args
        assert "64" in args


class TestTheDeployerLoadsAdapters:
    """The LoRA args are rendered from DeploymentTarget.adapters, so
    something has to put them there. Without this, uploading an adapter
    registers a gateway route to a vLLM process that never loaded it —
    a 404 from the engine the tenant cannot explain."""

    def deployer(self, adapters: Any = ()) -> Any:
        from aisvc.k8s.client import FakeKubernetes
        from aisvc.reconciler.deployer import ServingDeployer
        from aisvc.registry import InMemoryObjectStore, ModelRegistry

        return ServingDeployer(
            FakeKubernetes(),
            spec(),
            ModelRegistry(InMemoryObjectStore(), bucket="models"),
            adapters_for=lambda _base: adapters,
        )

    def args_for(self, deployer: Any) -> list[str]:
        from aisvc.k8s import render

        target = deployer._target_for(f"{BASE}-blue")
        for obj in render(target):
            if obj["kind"] == "Deployment":
                container = obj["spec"]["template"]["spec"]["containers"][0]
                args: list[str] = container.get("args", [])
                return args
        raise AssertionError("no Deployment rendered")

    def test_no_adapters_renders_no_lora_flags(self) -> None:
        assert "--enable-lora" not in self.args_for(self.deployer())

    def test_an_adapter_reaches_the_rendered_args(self) -> None:
        deployer = self.deployer(
            adapters=({"name": "legal-tone", "uri": "s3://adapters/legal-tone/"},)
        )

        args = self.args_for(deployer)
        assert "--enable-lora" in args
        assert any("legal-tone=" in arg for arg in args)

    def test_the_rank_comes_from_the_serving_profile(self) -> None:
        """It has to match what the adapter was validated against."""
        deployer = self.deployer(adapters=({"name": "a", "uri": "s3://a/"},))

        args = self.args_for(deployer)
        assert "--max-lora-rank" in args

    def test_without_a_lookup_nothing_loads(self) -> None:
        from aisvc.k8s.client import FakeKubernetes
        from aisvc.reconciler.deployer import ServingDeployer
        from aisvc.registry import InMemoryObjectStore, ModelRegistry

        deployer = ServingDeployer(
            FakeKubernetes(),
            spec(),
            ModelRegistry(InMemoryObjectStore(), bucket="models"),
        )

        assert "--enable-lora" not in self.args_for(deployer)
