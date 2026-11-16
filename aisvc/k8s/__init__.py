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
"""Rendering serving workloads (SPEC.md section 4).

[ADR-0001](../../docs/adr/0001-serving-layer.md) chose plain Kubernetes
objects over KServe: a `Deployment`, a `Service` and a
`PodDisruptionBudget` per model per colour. Blue and green coexist as
separate Deployments and Services, and the gateway shifts weight between
them during a rollout.

Serving profile parameters become engine command-line flags, which is the
mechanism SPEC.md section 6 specifies; only the allow-listed parameters in
`aisvc.spec.engines` are rendered.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from aisvc.spec import engines

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

from aisvc.spec.models import GpuClass, Model, NodePool, ServingProfile

__all__ = [
    "DEFAULT_ENGINE_IMAGES",
    "DEFAULT_NAMESPACE",
    "ENGINE_PORT",
    "WEIGHT_CACHE_PATH",
    "DeploymentTarget",
    "config_hash",
    "engine_args",
    "image_for",
    "render",
    "render_yaml",
    "spec_hash",
]

DEFAULT_NAMESPACE = "aisvc-serving"

#: Engine images used when a model does not pin its own. Operators
#: override per model with `image:` in platform.yaml.
DEFAULT_ENGINE_IMAGES: dict[str, str] = {
    "vllm": "vllm/vllm-openai:v0.11.0",
    "sglang": "lmsysorg/sglang:v0.4.6-cu124",
    "trtllm": "nvcr.io/nvidia/tritonserver:25.01-trtllm-python-py3",
}


def image_for(model: Model) -> str:
    """The container image to run this model's engine with."""
    if model.image:
        return model.image
    try:
        return DEFAULT_ENGINE_IMAGES[model.engine]
    except KeyError:
        raise ValueError(f"no default image for engine {model.engine!r}") from None


ENGINE_PORT = 8000

#: Where the cache DaemonSet stages weights on node-local NVMe
#: (SPEC.md section 11); milestone 2's cloud-init mounts it.
WEIGHT_CACHE_PATH = "/var/lib/aisvc/weights"

#: Cold start budget for a 70B FP8 model (SPEC.md section 11).
STARTUP_PROBE_PERIOD_SECONDS = 10
STARTUP_PROBE_FAILURES = 30  # 300s, comfortably over the 3-minute target

#: Parameters the deployment owns; a serving profile must not contradict
#: them, since they were validated against the GPU class.
_MODEL_OWNED_ARGS: frozenset[str] = frozenset(
    {
        "tensor_parallel_size",
        "tp_size",
        "max_model_len",
        "context_length",
        "quantization",
        "served_model_name",
        "model",
    }
)


@dataclass(frozen=True, slots=True)
class DeploymentTarget:
    """Everything needed to render one colour of one model's serving stack."""

    model: Model
    pool: NodePool
    gpu_class: GpuClass
    image: str
    replicas: int
    weights_uri: str
    serving_profile: ServingProfile | None = None
    colour: str = "blue"
    color: str = ""  # accepted spelling; `colour` is canonical
    #: LoRA adapters to load into this process (SPEC.md section 8), as
    #: {"name": ..., "uri": ...}. vLLM registers each as a served model
    #: under its own name, which is what the gateway addresses.
    adapters: tuple[Mapping[str, str], ...] = ()
    #: Must match what adapters were validated against, or an adapter
    #: that passed validation fails to load at request time.
    max_lora_rank: int = 32

    def __post_init__(self) -> None:
        if self.color:
            object.__setattr__(self, "colour", self.color)
        if self.colour not in {"blue", "green"}:
            raise ValueError(f"colour must be 'blue' or 'green', got {self.colour!r}")
        if self.replicas < 0:
            raise ValueError("replicas cannot be negative")

    @property
    def name(self) -> str:
        return f"{self.model.name}-{self.colour}"

    @property
    def labels(self) -> dict[str, str]:
        return {
            "app.kubernetes.io/name": self.model.name,
            "app.kubernetes.io/component": "inference",
            "app.kubernetes.io/managed-by": "aisvc",
            "aisvc.io/model": self.model.name,
            "aisvc.io/colour": self.colour,
            "aisvc.io/node-pool": self.pool.name,
            "aisvc.io/gpu-class": self.gpu_class.name,
        }

    @property
    def selector(self) -> dict[str, str]:
        return {
            "aisvc.io/model": self.model.name,
            "aisvc.io/colour": self.colour,
        }


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def engine_args(
    model: Model,
    profile: ServingProfile | None,
    *,
    adapters: Sequence[Mapping[str, str]] = (),
    max_lora_rank: int = 32,
) -> list[str]:
    """Engine command-line flags for one deployment.

    Parameters the deployment owns — tensor parallelism, context length,
    quantization — always come from the model, so a serving profile
    cannot contradict what `capacity` validated.

    LoRA is off unless adapters are given: `--enable-lora` reserves
    memory for the adapter slots whether or not any are in use, and a
    chat deployment with no adapters should not pay for it.
    """
    allowed = engines.allowed_params(model.engine)
    if not allowed:
        raise ValueError(f"unknown engine {model.engine!r}")

    args: list[str] = [
        "--model",
        model.repo_id,
        "--served-model-name",
        model.name,
        "--port",
        str(ENGINE_PORT),
    ]

    if model.engine == "vllm":
        args += [
            "--tensor-parallel-size",
            str(model.tensor_parallel),
            "--max-model-len",
            str(model.max_model_len),
        ]
        if model.quantization not in {"none", "bf16", "fp16"}:
            args += ["--quantization", model.quantization]
        if adapters:
            args += [
                "--enable-lora",
                "--max-lora-rank",
                str(max_lora_rank),
                "--max-loras",
                str(len(adapters)),
                "--lora-modules",
                *[f"{a['name']}={a['uri']}" for a in adapters],
            ]
    elif model.engine == "sglang":
        args += [
            "--tp-size",
            str(model.tensor_parallel),
            "--context-length",
            str(model.max_model_len),
        ]
    else:  # trtllm
        args += [
            "--tensor-parallel-size",
            str(model.tensor_parallel),
            "--max-input-len",
            str(model.max_model_len),
        ]

    if profile is None:
        return args

    for key in sorted(profile.params):
        if key not in allowed or key in _MODEL_OWNED_ARGS:
            continue
        raw = profile.params[key]
        if isinstance(raw, bool):
            if raw:
                args.append(_flag(key))
            continue
        args += [_flag(key), str(raw)]

    return args


def _container(target: DeploymentTarget) -> dict[str, Any]:
    return {
        "name": "engine",
        "image": target.image,
        "imagePullPolicy": "IfNotPresent",
        "args": engine_args(
            target.model,
            target.serving_profile,
            adapters=target.adapters,
            max_lora_rank=target.max_lora_rank,
        ),
        "ports": [{"name": "http", "containerPort": ENGINE_PORT}],
        "env": [
            {"name": "HF_HUB_OFFLINE", "value": "1"},
            {"name": "HF_HOME", "value": WEIGHT_CACHE_PATH},
            {"name": "AISVC_MODEL", "value": target.model.name},
            {"name": "AISVC_COLOUR", "value": target.colour},
        ],
        "resources": {
            "limits": {"nvidia.com/gpu": target.model.tensor_parallel},
            "requests": {"nvidia.com/gpu": target.model.tensor_parallel},
        },
        "volumeMounts": [
            {"name": "weight-cache", "mountPath": WEIGHT_CACHE_PATH, "readOnly": True},
            {"name": "shm", "mountPath": "/dev/shm"},
        ],
        # The engine loads weights before it serves; the startup probe owns
        # the cold start, and liveness only takes over once it passes.
        "startupProbe": {
            "httpGet": {"path": "/health", "port": ENGINE_PORT},
            "periodSeconds": STARTUP_PROBE_PERIOD_SECONDS,
            "failureThreshold": STARTUP_PROBE_FAILURES,
        },
        "readinessProbe": {
            "httpGet": {"path": "/health", "port": ENGINE_PORT},
            "periodSeconds": 5,
            "failureThreshold": 3,
        },
        "livenessProbe": {
            "httpGet": {"path": "/health", "port": ENGINE_PORT},
            "periodSeconds": 20,
            "failureThreshold": 3,
        },
        "securityContext": {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }


def _deployment(target: DeploymentTarget, namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": target.name,
            "namespace": namespace,
            "labels": target.labels,
            "annotations": {
                "aisvc.io/engine": target.model.engine,
                "aisvc.io/engine-image": target.image,
                "aisvc.io/weights-uri": target.weights_uri,
                "aisvc.io/model-source": target.model.source,
            },
        },
        "spec": {
            "replicas": target.replicas,
            "selector": {"matchLabels": target.selector},
            "strategy": {"type": "RollingUpdate"},
            "template": {
                "metadata": {"labels": target.labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "nodeSelector": {"aisvc.io/node-pool": target.pool.name},
                    "affinity": {
                        "podAntiAffinity": {
                            # Hard, not preferred: SPEC.md section 11 requires
                            # replicas on separate hosts.
                            "requiredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "topologyKey": "kubernetes.io/hostname",
                                    "labelSelector": {
                                        "matchLabels": {"aisvc.io/model": target.model.name}
                                    },
                                }
                            ]
                        }
                    },
                    "tolerations": [
                        {
                            "key": "nvidia.com/gpu",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        }
                    ],
                    "volumes": [
                        {
                            "name": "weight-cache",
                            "hostPath": {
                                "path": WEIGHT_CACHE_PATH,
                                "type": "DirectoryOrCreate",
                            },
                        },
                        {
                            "name": "shm",
                            "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"},
                        },
                    ],
                    "containers": [_container(target)],
                },
            },
        },
    }


def _service(target: DeploymentTarget, namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": target.name,
            "namespace": namespace,
            "labels": target.labels,
        },
        "spec": {
            "type": "ClusterIP",
            "selector": target.selector,
            "ports": [
                {
                    "name": "http",
                    "port": ENGINE_PORT,
                    "targetPort": ENGINE_PORT,
                    "protocol": "TCP",
                }
            ],
        },
    }


def _pdb(target: DeploymentTarget, namespace: str) -> dict[str, Any]:
    # minAvailable 1 against a single replica would block every drain
    # forever, which fights the maintenance flow in SPEC.md section 7.
    min_available = 1 if target.replicas > 1 else 0
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {
            "name": target.name,
            "namespace": namespace,
            "labels": target.labels,
        },
        "spec": {
            "minAvailable": min_available,
            "selector": {"matchLabels": target.selector},
        },
    }


def render(target: DeploymentTarget, *, namespace: str = DEFAULT_NAMESPACE) -> list[dict[str, Any]]:
    """The Kubernetes objects for one colour of one model."""
    return [
        _deployment(target, namespace),
        _service(target, namespace),
        _pdb(target, namespace),
    ]


def spec_hash(objects: Sequence[dict[str, Any]]) -> str:
    """A short digest of a rendered manifest.

    Stamped onto the objects as an annotation so the next plan can tell
    "already correct" from "changed" without diffing against a server
    that fills in its own defaults.
    """
    canonical = json.dumps(objects, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def config_hash(target: DeploymentTarget, *, namespace: str = DEFAULT_NAMESPACE) -> str:
    """A digest of everything about a deployment except its replica count.

    Replicas move under autoscaling, which is an in-place change. Folding
    them into the identity would make every scale look like a spec change
    and start a blue/green rollout over a number.
    """
    from dataclasses import replace as _replace

    normalised = _replace(target, replicas=0)
    return spec_hash(render(normalised, namespace=namespace))


def render_yaml(target: DeploymentTarget, *, namespace: str = DEFAULT_NAMESPACE) -> str:
    """The same objects as a multi-document YAML manifest."""
    return yaml.safe_dump_all(
        render(target, namespace=namespace), sort_keys=False, default_flow_style=False
    )
