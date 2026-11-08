"""Applying and observing serving workloads (SPEC.md section 4).

Server-side apply with a fixed field manager, so the reconciler owns the
fields it sets and leaves everything else alone. Observation reads back
only what this platform manages — anything without the `managed-by`
label belongs to someone else.

`Kubernetes` is a narrow protocol; `FakeKubernetes` backs the tests and
`ClusterKubernetes` wraps the official client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from aisvc.k8s import DEFAULT_NAMESPACE, spec_hash

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "FIELD_MANAGER",
    "ClusterKubernetes",
    "DeployedModel",
    "FakeKubernetes",
    "Kubernetes",
    "KubernetesError",
    "apply_objects",
    "delete_deployment",
    "observe_deployments",
    "observe_route_hash",
]

#: Server-side apply field manager. Fixed, so repeated applies converge
#: rather than fighting another controller for ownership.
FIELD_MANAGER = "aisvc"

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MODEL_LABEL = "aisvc.io/model"
COLOUR_LABEL = "aisvc.io/colour"
SPEC_HASH_ANNOTATION = "aisvc.io/spec-hash"

#: Kinds a model deployment is made of, in deletion order (dependants first).
MANAGED_KINDS = ("PodDisruptionBudget", "Service", "Deployment")


class KubernetesError(Exception):
    """An object could not be applied, read or removed."""


class Kubernetes(Protocol):
    """The slice of the Kubernetes API this module needs."""

    def apply(
        self,
        kind: str,
        namespace: str,
        obj: dict[str, Any],
        *,
        field_manager: str,
        force: bool,
    ) -> None: ...

    def delete(self, kind: str, namespace: str, name: str) -> None: ...

    def list(self, kind: str, namespace: str) -> list[dict[str, Any]]: ...


@dataclass
class FakeKubernetes:
    """An in-memory cluster. For tests and `--dry-run` rehearsals."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.applied: list[tuple[str, str, str]] = []
        self.options: list[dict[str, Any]] = []
        self.fail_on: str | None = None
        self._ready: dict[str, int] = {}

    def apply(
        self,
        kind: str,
        namespace: str,
        obj: dict[str, Any],
        *,
        field_manager: str,
        force: bool,
    ) -> None:
        if self.fail_on == kind:
            raise RuntimeError(f"scripted failure applying {kind}")
        name = obj["metadata"]["name"]
        self.objects[(kind, namespace, name)] = obj
        self.applied.append((kind, namespace, name))
        self.options.append({"field_manager": field_manager, "force": force})

    def delete(self, kind: str, namespace: str, name: str) -> None:
        self.objects.pop((kind, namespace, name), None)

    def list(self, kind: str, namespace: str) -> list[dict[str, Any]]:
        found = []
        for (obj_kind, obj_namespace, name), obj in self.objects.items():
            if obj_kind != kind or obj_namespace != namespace:
                continue
            enriched = dict(obj)
            enriched["status"] = {"readyReplicas": self._ready.get(name, 0)}
            found.append(enriched)
        return found

    def get(self, kind: str, namespace: str, name: str) -> dict[str, Any]:
        return self.objects[(kind, namespace, name)]

    def set_ready(self, name: str, ready: int) -> None:
        """Pretend `ready` replicas of `name` have passed their probes."""
        self._ready[name] = ready


class ClusterKubernetes:  # pragma: no cover - needs a real cluster
    """The official Kubernetes client behind the `Kubernetes` protocol."""

    def __init__(self, kubeconfig: str | None = None) -> None:
        from kubernetes import client, config

        # The kubernetes client ships only partial type information for
        # its config loaders.
        if kubeconfig:
            config.load_kube_config(config_file=kubeconfig)  # type: ignore[no-untyped-call]
        else:
            config.load_incluster_config()  # type: ignore[no-untyped-call]
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()
        self.policy = client.PolicyV1Api()

    def _api_for(self, kind: str) -> Any:
        return {
            "Deployment": self.apps,
            "Service": self.core,
            "PodDisruptionBudget": self.policy,
        }[kind]

    def apply(
        self,
        kind: str,
        namespace: str,
        obj: dict[str, Any],
        *,
        field_manager: str,
        force: bool,
    ) -> None:
        api = self._api_for(kind)
        name = obj["metadata"]["name"]
        patch = getattr(api, f"patch_namespaced_{_snake(kind)}")
        try:
            patch(
                name=name,
                namespace=namespace,
                body=obj,
                field_manager=field_manager,
                force=force,
            )
        except Exception as exc:
            create = getattr(api, f"create_namespaced_{_snake(kind)}")
            try:
                create(namespace=namespace, body=obj, field_manager=field_manager)
            except Exception as create_exc:
                raise KubernetesError(f"could not apply {kind} {name}: {create_exc}") from exc

    def delete(self, kind: str, namespace: str, name: str) -> None:
        api = self._api_for(kind)
        delete = getattr(api, f"delete_namespaced_{_snake(kind)}")
        try:
            delete(name=name, namespace=namespace)
        except Exception as exc:
            if "404" not in str(exc):
                raise KubernetesError(f"could not delete {kind} {name}: {exc}") from exc

    def list(self, kind: str, namespace: str) -> list[dict[str, Any]]:
        api = self._api_for(kind)
        lister = getattr(api, f"list_namespaced_{_snake(kind)}")
        return [item.to_dict() for item in lister(namespace=namespace).items]


def _snake(kind: str) -> str:  # pragma: no cover - trivial
    out = []
    for index, char in enumerate(kind):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


@dataclass(frozen=True, slots=True)
class DeployedModel:
    """One colour of one model, as the cluster currently has it."""

    model: str
    colour: str
    replicas_desired: int
    replicas_ready: int
    image: str
    spec_hash: str
    weights_uri: str = ""

    @property
    def fully_ready(self) -> bool:
        return self.replicas_desired > 0 and self.replicas_ready >= self.replicas_desired


def apply_objects(
    cluster: Kubernetes,
    objects: Sequence[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> None:
    """Server-side apply a rendered manifest, stamping its spec hash.

    The hash lets the next plan tell "already correct" from "changed",
    without comparing whole objects against a server that adds defaults.
    """
    if dry_run:
        return

    digest = spec_hash(objects)
    for obj in objects:
        kind = str(obj["kind"])
        namespace = str(obj["metadata"].get("namespace", DEFAULT_NAMESPACE))
        stamped = _with_hash(obj, digest)
        try:
            cluster.apply(
                kind,
                namespace,
                stamped,
                field_manager=FIELD_MANAGER,
                force=True,
            )
        except Exception as exc:
            name = obj["metadata"]["name"]
            raise KubernetesError(f"could not apply {kind} {name}: {exc}") from exc


def _with_hash(obj: dict[str, Any], digest: str) -> dict[str, Any]:
    stamped = dict(obj)
    metadata = dict(stamped["metadata"])
    annotations = dict(metadata.get("annotations") or {})
    annotations[SPEC_HASH_ANNOTATION] = digest
    metadata["annotations"] = annotations
    stamped["metadata"] = metadata
    return stamped


def delete_deployment(
    cluster: Kubernetes,
    model: str,
    colour: str,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    dry_run: bool = False,
) -> None:
    """Remove every object belonging to one colour of one model."""
    if dry_run:
        return
    name = f"{model}-{colour}"
    for kind in MANAGED_KINDS:
        try:
            cluster.delete(kind, namespace, name)
        except Exception as exc:
            raise KubernetesError(f"could not delete {kind} {name}: {exc}") from exc


def observe_deployments(
    cluster: Kubernetes, *, namespace: str = DEFAULT_NAMESPACE
) -> dict[tuple[str, str], DeployedModel]:
    """What this platform currently has running, keyed by (model, colour)."""
    observed: dict[tuple[str, str], DeployedModel] = {}

    for obj in cluster.list("Deployment", namespace):
        metadata = obj.get("metadata") or {}
        labels = metadata.get("labels") or {}
        if labels.get(MANAGED_BY_LABEL) != FIELD_MANAGER:
            continue

        model = labels.get(MODEL_LABEL)
        colour = labels.get(COLOUR_LABEL)
        if not model or not colour:
            continue

        annotations = metadata.get("annotations") or {}
        spec = obj.get("spec") or {}
        status = obj.get("status") or {}
        containers = (spec.get("template") or {}).get("spec", {}).get("containers") or [{}]

        observed[(model, colour)] = DeployedModel(
            model=model,
            colour=colour,
            replicas_desired=int(spec.get("replicas") or 0),
            replicas_ready=int(status.get("readyReplicas") or 0),
            image=str(containers[0].get("image", "")),
            spec_hash=str(annotations.get(SPEC_HASH_ANNOTATION, "")),
            weights_uri=str(annotations.get("aisvc.io/weights-uri", "")),
        )

    return observed


def observe_route_hash(
    cluster: Kubernetes,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    name: str = "aisvc-gateway-config",
) -> str:
    """The digest of the gateway config currently in the cluster.

    Empty when there is none, which makes the first plan write one.
    """
    for obj in cluster.list("ConfigMap", namespace):
        metadata = obj.get("metadata") or {}
        if metadata.get("name") != name:
            continue
        annotations = metadata.get("annotations") or {}
        return str(annotations.get("aisvc.io/route-hash", ""))
    return ""
