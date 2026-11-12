# ADR-0006: How a GPU node joins the cluster

**Status:** Proposed. Nothing is built; `aisvc/provision/` is unchanged.
**Date:** 2026-11-12
**Deciders:** Simon Weller
**Resolves, partly:** SPEC.md section 14's "Target CloudStack release, and how much
its GPU offerings, resource limits and Extensions framework already cover"
**Depends on:** [ADR-0003](0003-cloudstack-release.md) (4.23 target, 4.22 LTS floor)

## Context

The engine deploys a GPU node as a plain CloudStack VM and lets it join Kubernetes
by itself. `templates/cloud-init/gpu-node.yaml.j2` ends in:

```
kubeadm join {{ join_endpoint }} --token {{ join_token }} \
  --discovery-token-ca-cert-hash {{ ca_cert_hash }} --node-name {{ hostname }}
```

Those three values come from `AISVC_CKS_JOIN_ENDPOINT`, `AISVC_CKS_JOIN_TOKEN` and
`AISVC_CKS_CA_CERT_HASH`, read from the environment by `_cluster_join()` in
`aisvc/cli/main.py` and never derived from anything. CKS is used for nothing: no
command in the engine creates, scales or even reads a Kubernetes cluster, and
`provision.scale_cluster` — the one `scaleKubernetesCluster` call in the tree — is
dead code that only tests call.

Three consequences follow, and they are not hypothetical.

**A kubeadm bootstrap token expires, by default in 24 hours.** An operator who writes
`.env` once has a platform that silently stops being able to grow. Nothing in the
engine refreshes the token, and nothing notices it has gone stale.

**The failure lands on the node, after the VM is billed.** A wrong endpoint, an
expired token or a mistyped CA hash all pass every check the engine makes.
`deployVirtualMachine` succeeds, the VM boots, consumes a GPU, and `kubeadm join`
fails where nobody is looking. The engine finds out when the node never appears.

**CKS does not know the node exists.** Because the node joins behind CloudStack's
back, `listKubernetesClusters` does not count it, and a CKS upgrade or scale has no
idea it is there.

## What CKS actually offers

Read from the 4.22 API reference and admin guide, not from memory.

| Command | What it does | Since |
| --- | --- | --- |
| `addNodesToKubernetesCluster` | Adds existing physical or virtual machines as **worker nodes** to a managed CKS cluster. Async. | 4.21.0 |
| `removeNodesFromKubernetesCluster` | Drains on the control node, resets the worker, deletes it from the cluster, and removes port-forward and firewall rules. Async. | 4.21.0 |
| `getKubernetesClusterConfig` | Retrieves kubeconfig file data for a cluster. | earlier |
| `createKubernetesCluster` | Takes `nodeofferings`, a node-type-to-service-offering mapping, overriding `serviceofferingid`. | earlier |
| `addVirtualMachinesToKubernetesCluster` | The **ExternalManaged** equivalent. Explicitly *"not applicable for CloudManaged kubernetes clusters"*. | 4.19 |

`addNodesToKubernetesCluster` landed in **4.21.0**, which is inside the 4.22 LTS floor
ADR-0003 holds the plugin to. This is not a 4.23-only API.

The last row matters: the two add-node commands are not interchangeable. Which one
applies depends on how the operator created the cluster, and a design that picks one
has picked a `clustertype`.

## Decision

**Proposed: deploy the VM as now, then hand it to CKS with
`addNodesToKubernetesCluster`, and drop the kubeadm token path entirely.**

The engine keeps doing what it is good at — choosing a template, an offering, an
affinity group and an ordinal-stable name — and stops doing the one thing CloudStack
does better, which is knowing how its own Kubernetes clusters are joined.

Three secondary changes follow from it:

1. **Fetch the kubeconfig with `getKubernetesClusterConfig`** instead of requiring
   `KUBECONFIG`. The engine already holds CloudStack admin credentials; asking for a
   second, separately-managed credential to reach a cluster CloudStack provisioned is
   a step that buys nothing.
2. **Drain through `removeNodesFromKubernetesCluster`.** It already drains on the
   control node and resets the worker, which is most of what `aisvc drain` does by
   hand. ADR-0003's rule stands either way: destroy, never stop.
3. **Keep `cks_cluster` in `platform.yaml`,** now as a real reference rather than a
   label. The preflight check added in `bdc4f32` already verifies it.

## What this costs

The requirements for adding an external VM as a CKS worker are real, and they land on
`templates/packer/gpu-golden.pkr.hcl`.

| Requirement | Where the image stands |
| --- | --- |
| ≥8 GB root disk, 2 cores, 2 GB RAM | Met trivially by any GPU node |
| `containerd`, `kubelet`, `kubeadm`, `cloud-init`, `ca-certificates`, `curl`, `gnupg` | **Already installed** |
| `conntrack`, `cloud-guest-utils`, `apt-transport-https`, `lsb-release`, `python3-jsonschema` | Not installed; the admin guide lists them for custom templates |
| `containerd.io` specifically | The image installs Ubuntu's `containerd`. Whether CKS requires Docker's `containerd.io` package is **unverified** |
| A NIC on the Kubernetes cluster network | A placement constraint the pool spec does not currently express |
| **The management server's SSH public key in the `cloud` user's `authorized_keys`** | Not there. The image's cleanup step deliberately *removes* `authorized_keys`, and there is no `cloud` user |

The SSH key is the only genuinely new thing, and it has a clean mechanism:
`deployVirtualMachine` takes `keypair` (and `keypairs`). An operator registers the
management server's public key once with `registerSSHKeyPair`, names it, and the
provisioner passes it at deploy time.

**That trade is the heart of this ADR.** Today an operator hand-copies a *secret that
expires in 24 hours*. Afterwards they hand-copy a *public key that does not expire*.
Both are one-time manual steps; only one of them silently breaks the platform a day
later.

## What would become harder

- **It makes `addNodesToKubernetesCluster` a hard dependency.** Today any Kubernetes
  cluster reachable by kubeadm will do. Afterwards the cluster must be a CKS cluster
  CloudStack manages, of the right `clustertype`. That is a narrowing, and for an
  operator with an existing cluster it is a migration.
- **The golden image becomes CKS-compatible, not merely NVIDIA-compatible.** Its
  package list is now constrained by what CKS expects of a worker, and that list is
  documentation, not a contract.
- **Two more async jobs per node.** Deploy, then add, then on teardown remove. Each is
  a failure point the reconciler has to plan, record and retry, and a node that is
  deployed but not added is a new partial state that `aisvc-<pool>-<ordinal>` naming
  does not by itself describe.

## The unknown that decides it

**Nothing in the CKS documentation mentions GPUs.** Not passthrough, not vGPU, not
GPU-bearing node offerings. The entire plugin exists to schedule passthrough GPUs,
which cannot live-migrate, and ADR-0003's drain-and-replace rule exists because of
that.

So the question this ADR cannot answer from documents is whether CKS's own node
lifecycle is safe for a passthrough-GPU node:

- `manualupgrade` on `addNodesToKubernetesCluster` marks a node as excluded from the
  cluster **upgrade** operation. The admin guide does not say whether it is excluded
  from **scaling**, and `scaleKubernetesCluster` is the operation most likely to stop
  a node.
- A stopped node keeps its GPU allocated, because `gpu.detach.on.stop` defaults to
  false (ADR-0003). A CKS operation that stops rather than destroys a GPU worker
  leaks a GPU.

`manualupgrade` existing at all is a strong signal that this path was designed for
nodes the cluster should not touch, which is exactly what a GPU node is. But that is
an inference from a flag's existence, not a documented guarantee.

## Recommendation

1. **Adopt it, gated on one experiment.** Before writing any of it, add one GPU node
   to a real CKS cluster by hand with `addNodesToKubernetesCluster`, then run
   `scaleKubernetesCluster` on that cluster and watch what happens to the GPU node.
   If scaling leaves a `manualupgrade` node alone, the whole design holds. If it stops
   it, the GPU leaks and this ADR needs rethinking before it needs implementing.
2. **Do `getKubernetesClusterConfig` first regardless.** It is independent of the join
   question, removes a prerequisite from `docs/installation.md`, and cannot leak a GPU
   if it is wrong.
3. **Do not touch `provision.scale_cluster`.** It is dead code pointing at the wrong
   abstraction — CKS scaling its own nodes, when the nodes this platform cares about
   are ones CKS did not create. Delete it rather than wire it up.

## What this does not decide

Whether the cluster itself should be created by the engine. `createKubernetesCluster`
with `nodeofferings` would let the setup wizard build a zone from genuinely empty,
which is section 14's first acceptance criterion. It is a larger question — it makes
the engine an owner of cluster lifecycle, not just of GPU capacity — and nothing here
depends on it.

## Sources

- [CloudStack Kubernetes Service, 4.22.0.0 admin guide](https://docs.cloudstack.apache.org/en/4.22.0.0/plugins/cloudstack-kubernetes-service.html)
  — the external-node requirements, the `cloud` user's `authorized_keys`, the
  "From 4.21.0" version note, `removeNodesFromKubernetesCluster`'s drain-reset-delete
  sequence and `cloud.kubernetes.cluster.remove.node.timeout`, and that
  `getKubernetesClusterConfig` retrieves kubeconfig file data.
- [`addNodesToKubernetesCluster`](https://cloudstack.apache.org/api/apidocs-4.22/apis/addNodesToKubernetesCluster.html)
  — parameters, and `manualupgrade` excluding a node from cluster upgrades.
- [`addVirtualMachinesToKubernetesCluster`](https://cloudstack.apache.org/api/apidocs-4.22/apis/addVirtualMachinesToKubernetesCluster.html)
  — "Not applicable for CloudManaged kubernetes clusters".
- [`createKubernetesCluster`](https://cloudstack.apache.org/api/apidocs-4.22/apis/createKubernetesCluster.html)
  — `clustertype` (CloudManaged default, ExternalManaged) and `nodeofferings`.
- [`deployVirtualMachine`](https://cloudstack.apache.org/api/apidocs-4.22/apis/deployVirtualMachine.html)
  — `keypair` and `keypairs`, both optional.
- [CKS Enhancements design wiki](https://cwiki.apache.org/confluence/spaces/CLOUDSTACK/pages/337676845/CKS+Enhancements)
  — ISO handling for added nodes, and the worker-package prerequisite. A design page,
  so the release docs are the authority for what shipped.

**Not verified against anything running.** No CloudStack has answered any of these
calls for this project, no CKS cluster has accepted a GPU node, and the package-list
and `containerd.io` gaps above were read from a document rather than from a failed
build.
