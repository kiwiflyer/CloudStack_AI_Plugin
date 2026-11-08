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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from aisvc.spec import engines

if TYPE_CHECKING:
    from aisvc.spec.models import GpuClass, Model, NodePool, ServingProfile

__all__ = [
    "DEFAULT_NAMESPACE",
    "ENGINE_PORT",
    "WEIGHT_CACHE_PATH",
    "DeploymentTarget",
    "engine_args",
    "render",
    "render_yaml",
]

DEFAULT_NAMESPACE = "aisvc-serving"
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


def engine_args(model: Model, profile: ServingProfile | None) -> list[str]:
    """Engine command-line flags for one deployment.

    Parameters the deployment owns — tensor parallelism, context length,
    quantization — always come from the model, so a serving profile
    cannot contradict what `capacity` validated.
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
        "args": engine_args(target.model, target.serving_profile),
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


def render_yaml(target: DeploymentTarget, *, namespace: str = DEFAULT_NAMESPACE) -> str:
    """The same objects as a multi-document YAML manifest."""
    return yaml.safe_dump_all(
        render(target, namespace=namespace), sort_keys=False, default_flow_style=False
    )
