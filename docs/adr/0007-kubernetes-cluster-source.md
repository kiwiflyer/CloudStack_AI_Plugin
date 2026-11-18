# ADR-0007: Where the Kubernetes cluster comes from

**Status:** Proposed, **partly built** (reviewed 2026-11-18). `platform.yaml` now
carries the `kubernetes` block this ADR proposed — `source` (default `cks`),
`cluster`, and a separate `create` permission — and `check_clusters` reads it,
so an `external` zone is no longer failed for a cluster CloudStack never owned.
What is still unbuilt is the part that would *create* a cluster, and ADR-0006's
GPU experiment remains unrun.
**Date:** 2026-11-12
**Deciders:** Simon Weller
**Amends:** [ADR-0006](0006-gpu-node-join.md), whose recommendation to drop the
kubeadm path entirely does not survive this decision
**Depends on:** [ADR-0003](0003-cloudstack-release.md) (4.23 target, 4.22 LTS floor)

## Context

The engine has always assumed a Kubernetes cluster exists and is reachable. Until
`bdc4f32` nothing even checked: `cks_cluster` was a name in `platform.yaml` that
reached cloud-init as a label and was otherwise unused.

[ADR-0006](0006-gpu-node-join.md) proposed handing GPU node VMs to
`addNodesToKubernetesCluster` instead of running `kubeadm join` from cloud-init,
which is a clear improvement — an expiring 24-hour token becomes a non-expiring
public key, and the join stops failing silently on a VM that is already billed.

But that ADR recommended dropping the kubeadm path *entirely*, and
`addNodesToKubernetesCluster` only works on a CloudStack-managed cluster. Adopting it
as written would quietly make CKS mandatory, which is a product decision disguised as
an implementation one.

## Scope: installing CKS is not this project's job

A CloudStack administrator enables CKS once per installation: the
`cloud.kubernetes.service.enabled` global setting and a management server restart,
`endpoint.url`, `cloud.kubernetes.cluster.network.offering`, and a registered
Kubernetes binaries ISO via `addKubernetesSupportedVersion`.

**All of that is out of scope.** It is a one-time CloudStack install step, not
per-zone setup, and the restart is something this engine must never do to a
management server it does not own. This ADR assumes only that **a Kubernetes service
is available** — either CKS is enabled and usable, or the operator has a cluster of
their own.

That assumption is what the setup wizard should *detect and report*, not what it
should *perform*.

## Decision

**Proposed: a zone declares where its cluster comes from, and the engine supports
three sources rather than assuming one.**

| Source | Who owns the cluster | Join mechanism |
| --- | --- | --- |
| **Existing CKS cluster** | CloudStack | `addNodesToKubernetesCluster`, or kubeadm |
| **CKS cluster the engine creates** | CloudStack | `addNodesToKubernetesCluster`, or kubeadm |
| **External cluster** | Nobody here | `kubeadm join` only |

The dependency runs one way. The cluster's source determines which join mechanisms
are available; the join choice never constrains the source. That asymmetry is the
whole design, and it is why these are two ADRs rather than one.

### What `platform.yaml` would say

```yaml
zones:
  - name: zone-east-1
    cloudstack_zone: East-1
    kubernetes:
      source: cks          # cks | external
      cluster: aisvc-east-1
      create: false        # cks only: build it if CloudStack has none
```

Two orthogonal fields, because they answer different questions. `source` says whether
CloudStack is the authority on this cluster — which decides whether
`listKubernetesClusters` not knowing it is a problem or a non-event. `create` says
whether the engine may build one, which is a separate permission an operator grants
deliberately.

An existing `cks_cluster: X` reads as `source: cks, cluster: X, create: false`, so
nothing already written changes meaning.

## Why keep the external path at all

**Because most operators already have a cluster.** ADR-0003 reasoned that a product
built only against a non-LTS release cannot be installed by customers who run LTS.
The same argument applies here: a platform that only runs on a CKS cluster it
provisioned itself cannot be installed by a provider who already runs Kubernetes, and
that is a large share of the people this is for.

**Because it demotes ADR-0006's unresolved risk.** Nothing in CKS's documentation
mentions GPUs, and whether `scaleKubernetesCluster` leaves a `manualupgrade` GPU node
alone is unknown — a CKS operation that stops rather than destroys one leaks its GPU,
since `gpu.detach.on.stop` defaults to false. With an external path available, that
unknown stops being load-bearing. If the experiment in ADR-0006 goes badly, operators
have somewhere to stand.

## What it costs

**Both join mechanisms stay.** ADR-0006 framed removing `kubeadm join` as a
simplification; this makes it an addition instead. Two paths, two sets of failure
modes, two things to test — and the engine has never run either against a real
cluster.

The expiring-token footgun does not disappear. It is confined to the path where an
operator deliberately chose a cluster CloudStack does not manage, which is the right
place for a sharp edge, but it is still there and
`docs/installation.md` has to keep saying so.

## A defect this exposes in `bdc4f32`

`check_clusters` fails a zone when CloudStack lists no cluster by that name, with a
remedy telling the operator to fix `cks_cluster` in `platform.yaml`.

Under `source: external` that is wrong, and loudly so: an operator pointing at their
own EKS or kubeadm cluster would get a hard preflight failure for a cluster CloudStack
was never supposed to know about. The check cannot tell "typo" from "not CloudStack's
business" without the source field, so **the check is correct today only because
`cks_cluster` is mandatory and every cluster is assumed to be a CKS one.** It becomes
a bug the moment this ADR is implemented, and the fix belongs in the same change.

**Fixed since (reviewed 2026-11-18), ahead of the rest of this ADR.**
`check_clusters` now branches on `zone.kubernetes.source`: a zone declared
`external` passes with *"declared external, so CloudStack is not asked about it;
reachability is what speaks for it"*, and only a `cks` zone is failed for a
cluster CloudStack cannot find. A zone not yet observed is skipped rather than
failed, as capacity is. So the schema field this ADR proposes exists and the
preflight reads it — what remains unbuilt is everything that would *create* a
cluster.

The Kubernetes reachability half of that commit needs no change — it already asks the
only question that matters for an external cluster.

## Consequences

**Easier**

- A greenfield operator can be taken from "CKS is available" to a running cluster by
  the wizard, which is as close to section 14's "empty zone" criterion as this
  project can honestly get.
- Preflight gains a real answer for every source instead of one assumption.
- ADR-0006 can be adopted on its merits without taking the product with it.

**Harder**

- The spec schema grows a block, and `aisvc/spec` validation has to reject the
  combinations that cannot work — `create: true` with `source: external` most
  obviously.
- `aisvc/provision` carries two join paths, selected per zone.
- The setup wizard needs to detect CKS availability (`listKubernetesSupportedVersions`
  returning a usable version, and the API being registered at all) to know whether to
  offer the create option, and say what is missing when it cannot.

## Recommendation

1. **Adopt the three-source model**, with `source: cks, create: false` as the default
   so existing specs keep working untouched.
2. **Build the schema and the preflight branch first.** They are independent of
   ADR-0006's experiment, they fix the defect above before it can bite, and neither
   can leak a GPU.
3. **Leave creation until last.** It is the only part that needs CKS prerequisites to
   be in place, and the only part an operator can do perfectly well by hand in the
   CloudStack UI meanwhile.

## What is still unknown

*Reviewed 2026-11-18. Nothing here is built yet; `platform.yaml`'s schema is
still unchanged. What has happened since is that CKS itself has been driven
against the 4.23.0.0 simulator — a cluster's control node and worker both
reached `Running` — which removes three undocumented obstacles from anyone
attempting ADR-0006's experiment. They are listed there. The cluster never
leaves `Starting`, because the simulator's hypervisor runs nothing inside those
VMs, so none of the questions below moved.*

- The GPU lifecycle question from [ADR-0006](0006-gpu-node-join.md) is untouched by
  this and still needs its experiment — and the simulator cannot stand in for it,
  having neither a real GPU nor a real kubelet.
- The admin guide says CKS supports Shared, Isolated and VPC networks via `networkid`
  but describes VPC generally, without a tier-specific parameter. Given how much
  trouble the tier question caused for private endpoints, confirm it rather than
  assume it.
- Whether a CKS cluster created by the engine should be owned by the admin account or
  the operator's, and what should happen to it if deleted from under a running
  engine. The same question [ADR-0005](0005-model-artifact-storage.md) sidestepped for
  registry buckets, and sidestepped here too.

## Sources

- [CloudStack Kubernetes Service, 4.22.0.0 admin guide](https://docs.cloudstack.apache.org/en/4.22.0.0/plugins/cloudstack-kubernetes-service.html)
  — the CKS install prerequisites (global setting and restart, `endpoint.url`, the
  network offering, `addKubernetesSupportedVersion`), and that Shared, Isolated and
  VPC networks are supported.
- [`createKubernetesCluster`](https://cloudstack.apache.org/api/apidocs-4.22/apis/createKubernetesCluster.html)
  — the minimum for a CloudManaged cluster, and `clustertype`.
- [`addNodesToKubernetesCluster`](https://cloudstack.apache.org/api/apidocs-4.22/apis/addNodesToKubernetesCluster.html)
  — adds existing machines as workers to a managed cluster.

**Not verified against anything running.** No CloudStack has created a cluster for
this project, accepted a GPU node into one, or been asked whether CKS is available.
